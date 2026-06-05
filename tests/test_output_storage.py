from pathlib import Path

from sag.agent.output_storage import OutputStorageManager


class FakeOutputStorageOrchestrator:
    def __init__(self):
        self.commands = []
        self.files = {}

    def execute_command(self, command):
        self.commands.append(command)

        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("test -f ") and "output_index.json" in command:
            return {"success": False, "output": "", "exit_code": 1}

        if "wc -l <" in command:
            output = self.files.get("/workspace/.setup_agent/contexts/full_outputs.jsonl", "")
            return {"success": True, "output": str(len(output.splitlines())), "exit_code": 0}

        if command.startswith("cat >> ") or command.startswith("cat > "):
            operator = ">>" if command.startswith("cat >> ") else ">"
            path = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            if operator == ">>":
                self.files[path] = self.files.get(path, "") + payload + "\n"
            else:
                self.files[path] = payload + "\n"
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
