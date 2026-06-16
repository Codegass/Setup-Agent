import base64
import re
from pathlib import Path

from sag.agent.output_storage import OutputStorageManager

INDEX_PATH = "/workspace/.setup_agent/contexts/output_index.json"
STORAGE_PATH = "/workspace/.setup_agent/contexts/full_outputs.jsonl"


class FakeOutputStorageOrchestrator:
    def __init__(self):
        self.commands = []
        self.files = {}

    def execute_command(self, command):
        self.commands.append(command)

        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}

        # `test -f <index> && cat <index>` — return current contents so a manager
        # can (re)load the durable index that another instance wrote.
        if command.startswith("test -f ") and "output_index.json" in command:
            contents = self.files.get(INDEX_PATH)
            if contents:
                return {"success": True, "output": contents, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if "wc -l <" in command:
            output = self.files.get(STORAGE_PATH, "")
            return {"success": True, "output": str(len(output.splitlines())), "exit_code": 0}

        # `sed -n '<n>p' <storage>` — return the nth line of the JSONL store.
        if command.startswith("sed -n "):
            try:
                line_no = int(command.split("'", 2)[1].rstrip("p"))
            except (IndexError, ValueError):
                return {"success": True, "output": "", "exit_code": 0}
            lines = self.files.get(STORAGE_PATH, "").splitlines()
            if 1 <= line_no <= len(lines):
                return {"success": True, "output": lines[line_no - 1], "exit_code": 0}
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("cat >> ") or command.startswith("cat > "):
            operator = ">>" if command.startswith("cat >> ") else ">"
            path = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            if operator == ">>":
                self.files[path] = self.files.get(path, "") + payload + "\n"
            else:
                self.files[path] = payload + "\n"
            return {"success": True, "output": "", "exit_code": 0}

        # --- chunked (base64) write path ---
        if command.startswith("rm -f "):
            self.files.pop(command[len("rm -f ") :].split()[0], None)
            return {"success": True, "output": "", "exit_code": 0}

        m = re.match(r"printf '%s' '(.*)' >> (\S+)$", command, re.DOTALL)
        if m:
            chunk, target = m.group(1), m.group(2)
            self.files[target] = self.files.get(target, "") + chunk
            return {"success": True, "output": "", "exit_code": 0}

        m = re.match(r"base64 -d (\S+) (>>|>) (\S+) && printf '\\n' >> \S+ && rm -f \S+$", command)
        if m:
            tmp, operator, path = m.group(1), m.group(2), m.group(3)
            decoded = base64.b64decode(self.files.get(tmp, "")).decode("utf-8", "replace")
            payload = decoded + "\n"
            if operator == ">>":
                self.files[path] = self.files.get(path, "") + payload
            else:
                self.files[path] = payload
            self.files.pop(tmp, None)
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}


def test_output_storage_uses_safe_container_writes_for_backticks(tmp_path):
    orchestrator = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)

    ref_id = storage.store_output(
        task_id="task_1",
        tool_name="project_analyzer",
        output="Run tests using documented commands: mvn` without arguments",
    )

    assert ref_id
    assert all('echo "' not in command for command in orchestrator.commands)
    assert "/workspace/.setup_agent/contexts/full_outputs.jsonl" in orchestrator.files
    assert "/workspace/.setup_agent/contexts/output_index.json" in orchestrator.files
    assert "mvn` without arguments" in orchestrator.files[
        "/workspace/.setup_agent/contexts/full_outputs.jsonl"
    ]
    assert "mvn` without arguments" in orchestrator.files[
        "/workspace/.setup_agent/contexts/output_index.json"
    ]


def test_retrieve_reloads_index_for_output_stored_by_another_instance():
    """A reader manager must see outputs a separate writer instance stored after it
    loaded its in-memory index (the detached build-log blindness bug).

    Reproduces Brooklyn: OutputSearchTool builds its manager once at session start
    (empty index); the build tool stores the maven log through its own manager; the
    reader must reload the durable index instead of reporting "No output found".
    """
    orchestrator = FakeOutputStorageOrchestrator()

    # Reader is constructed FIRST, so its in-memory index is empty/stale.
    reader = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)

    # A separate writer instance stores a build log afterwards.
    writer = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)
    build_log = "[INFO] Scanning for projects...\n[ERROR] BUILD FAILURE\n" * 50
    ref_id = writer.store_output(task_id="maven_build", tool_name="maven", output=build_log)
    assert ref_id

    # Stale cache: the reader has not seen this ref yet.
    assert ref_id not in reader.current_index

    # retrieve_output must reload the durable index and return the full content.
    retrieved = reader.retrieve_output(ref_id)
    assert retrieved == build_log

    # search_outputs must likewise refresh and surface the new ref.
    found = reader.search_outputs(tool_name="maven")
    assert any(item["ref_id"] == ref_id for item in found)


def test_retrieve_returns_none_for_genuinely_missing_ref():
    """Reloading on a miss must not turn an absent ref into a false positive."""
    orchestrator = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)
    assert storage.retrieve_output("output_does_not_exist") is None


def test_store_and_retrieve_large_output_uses_chunked_write_and_round_trips():
    """A multi-hundred-KB output (e.g. a full Maven build log) must store and
    retrieve intact. A single heredoc would exceed the kernel's per-arg limit
    ('argument list too long', the Fix 2b regression); the chunked base64 path
    must round-trip it, and no single command may exceed the char cap.
    """
    orchestrator = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)

    big = "[INFO] Downloading from central: progress line\n" * 6000
    assert len(big) > storage._MAX_CMD_CHARS

    ref_id = storage.store_output(task_id="maven_build", tool_name="maven", output=big)
    assert ref_id

    # No single command argument blew past the per-arg cap (the regression cause).
    assert max(len(cmd) for cmd in orchestrator.commands) <= storage._MAX_CMD_CHARS + 200

    assert storage.retrieve_output(ref_id) == big
