# tests/test_search_tool.py
"""search(target, pattern): one retrieval tool for refs, files, job logs, web.

Spec §4: target = ref id | file:<path> | job:<id> | web:<query>.
The ref/web paths DELEGATE to the existing OutputSearchTool/WebSearchTool
internals (stage-1 consolidates the surface, not the implementations).
"""

import subprocess
from types import SimpleNamespace

from container_evidence_fakes import ContainerFS
from test_job_settlement import DOCKER_EXEC_ID, TERMINAL_IDENTITY

from sag.agent.evidence_records import named_json_record_stream_command
from sag.agent.job_obligations import OBLIGATION_DIR, build_obligation, write_obligation
from sag.tools.base import ToolResult
from sag.tools.search_tool import SearchTool


class FakeOrchestrator:
    def __init__(self, responses=None):
        self.commands = []
        self.responses = responses or {}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        for marker, resp in self.responses.items():
            if marker in command:
                return resp
        return {"success": True, "output": "", "exit_code": 0}

    def detached_handle(self, job_id):
        return {
            "job_id": job_id,
            "log_path": f"/tmp/sag_jobs/{job_id}.log",
            "exit_code_path": f"/tmp/sag_jobs/{job_id}.log.exit",
            "pid_path": f"/tmp/sag_jobs/{job_id}.pid",
        }

    def poll_detached_command(self, handle, **kwargs):
        response = self.responses.get("sag_jobs/abc.log", {})
        exit_code = response.get("exit_code")
        return {
            "finished": exit_code is not None,
            "running": exit_code is None,
            "exit_code": exit_code,
            "tail": response.get("output", ""),
            "log_size": len(response.get("output", "")),
            "probe_success": True,
            "state": "finished" if exit_code is not None else "running",
        }

    def collect_detached_result(self, handle, poll):
        return {
            "exit_code": poll["exit_code"],
            "output": poll["tail"],
            "full_output": poll["tail"],
            "dispatch_status": "completed_detached",
        }


def test_file_target_greps_in_container():
    orch = FakeOrchestrator(
        responses={
            "pom.xml": {"success": True, "output": "42:<requireMavenVersion>", "exit_code": 0}
        }
    )
    tool = SearchTool(orch, output_search=None, web_search=None)

    result = tool.execute(target="file:/workspace/p/pom.xml", pattern="requireMavenVersion")

    assert result.succeeded is True
    assert "requireMavenVersion" in result.output
    assert any("grep" in c and "pom.xml" in c for c in orch.commands)


def test_job_target_polls_original_operation():
    orch = FakeOrchestrator(
        responses={
            "sag_jobs/abc.log": {"success": True, "output": "BUILD SUCCESSFUL", "exit_code": 0}
        }
    )
    tool = SearchTool(orch, output_search=None, web_search=None)

    result = tool.execute(target="job:abc", pattern="BUILD")

    assert result.succeeded is True
    assert result.poll_ref == "job:abc"
    assert "BUILD SUCCESSFUL" in result.output
    # Same contract, current transport: the poll consults the obligation
    # ledger through the bounded record stream — now the filename-bound
    # (named) variant the strict live reader requires — and nothing else.
    assert orch.commands == [named_json_record_stream_command(OBLIGATION_DIR)]


class PublishedLedgerOrchestrator(FakeOrchestrator):
    """`FakeOrchestrator` whose evidence files live in a real published store.

    Marker responses keep serving the job-log surface; every other command —
    notably the strict named obligation-ledger stream — hits the shared
    `ContainerFS` so host-published bytes and container bytes stay one store.
    """

    def __init__(self, responses=None):
        super().__init__(responses)
        self.filesystem = ContainerFS()

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        for marker, resp in self.responses.items():
            if marker in command:
                return resp
        return self.filesystem(command, **kwargs)

    # Strict evidence transport refuses a bound project-runtime executor and
    # requires the clean host-control channel.
    execute_control_command = execute_command


def test_terminal_job_poll_uses_the_runner_recorded_by_its_obligation():
    # The live ledger only serves strict host-published schema-v3 records, so
    # the recorded runner must ride a complete settled obligation instead of
    # the old bare four-field mirror file.
    obligation = build_obligation(
        job_id="abc",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/p",
        before={},
        log_path="/tmp/sag_jobs/abc.log",
        exit_code_path="/tmp/sag_jobs/abc.log.exit",
        **TERMINAL_IDENTITY,
    )
    obligation.update(
        process_state="terminal",
        settlement_state="settled",
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=1,
        attempted_receipt_id="inv-gradle-1-0001",
        settled_receipt_id="inv-gradle-1-0001",
    )
    tail = "BUILD SUCCESSFUL\nCMake Error: optional native diagnostic"
    orch = PublishedLedgerOrchestrator(
        responses={
            "sag_jobs/abc.log": {
                "success": True,
                "output": tail,
                "exit_code": 0,
            },
        }
    )
    assert write_obligation(orch.execute_command, obligation) is True

    result = SearchTool(orch).execute(target="job:abc")

    assert result.succeeded is True
    assert result.metadata["runner"] == "gradle"


def test_ref_target_delegates_to_output_search():
    calls = []

    class FakeOutputSearch:
        def execute(self, **kwargs):
            calls.append(kwargs)
            return ToolResult.completed_success(output="matched line")

    tool = SearchTool(FakeOrchestrator(), output_search=FakeOutputSearch(), web_search=None)

    result = tool.execute(target="output_5b9a", pattern="FAIL")

    assert result.succeeded and "matched line" in result.output
    assert calls[0]["ref_id"] == "output_5b9a"
    assert calls[0]["grep_pattern"] == "FAIL"


def test_web_target_delegates_to_web_search():
    calls = []

    class FakeWebSearch:
        def execute(self, query, max_results=5):
            calls.append(query)
            return ToolResult.completed_success(output="result snippet")

    tool = SearchTool(FakeOrchestrator(), output_search=None, web_search=FakeWebSearch())

    result = tool.execute(target="web:gradle develocity plugin 3.19")

    assert result.succeeded
    assert calls == ["gradle develocity plugin 3.19"]


def test_unknown_target_is_failed_with_options():
    tool = SearchTool(FakeOrchestrator(), output_search=None, web_search=None)
    result = tool.execute(target="bogus^target", pattern="x")
    assert result.operation_outcome.value == "failed"
    assert any("file:" in s or "job:" in s for s in result.suggestions)


class LocalShellOrchestrator:
    """Run the tool's real command through a real ``/bin/bash``, no container.

    `DockerOrchestrator.execute_command` wraps every command in ``/bin/bash
    -c`` and demuxes the result into exactly these keys (combined ``output``,
    plus separate ``stdout``/``stderr``).  A dict-marker fake can only replay
    an exit code someone typed by hand; the defect here is what a real shell
    does to grep's exit code on the far side of a ``| head`` pipe, so these
    cases run the real pipeline over real files.
    """

    def __init__(self, root):
        self.root = str(root)
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        proc = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=workdir or self.root,
            capture_output=True,
            text=True,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": (stdout + "\n" + stderr).strip() if stderr else stdout,
            "stdout": stdout,
            "stderr": stderr,
        }


def test_file_match_returns_the_numbered_lines_a_real_grep_printed(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project>\n  <requireMavenVersion>3.9</requireMavenVersion>\n</project>\n")
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{pom}", pattern="requireMavenVersion")

    assert result.succeeded is True
    assert result.facts["matched"] is True
    assert result.output == "2:  <requireMavenVersion>3.9</requireMavenVersion>"


def test_genuine_no_match_stays_a_successful_found_nothing(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project/>\n")
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{pom}", pattern="requireMavenVersion")

    # grep exit 1 is the one honest "I looked and it is not there".
    assert result.succeeded is True
    assert result.facts["matched"] is False
    assert result.output == f"No matches for 'requireMavenVersion' in {pom}"


def test_absent_path_is_a_typed_failure_that_quotes_grep(tmp_path):
    missing = tmp_path / "nope.xml"
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{missing}", pattern="anything")

    assert result.succeeded is False
    assert result.operation_outcome.value == "failed"
    assert result.error_code == "SEARCH_FAILED"
    assert "No such file or directory" in result.output
    assert "No matches" not in result.output
    # Never a found-nothing claim: the search did not run.
    assert result.facts["matched"] is None


def test_invalid_regex_is_a_typed_failure_naming_the_cause(tmp_path):
    subject = tmp_path / "a.txt"
    subject.write_text("x\n")
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{subject}", pattern="foo(")

    assert result.succeeded is False
    assert result.error_code == "SEARCH_FAILED"
    assert "grep:" in result.output  # the real diagnostic, whatever grep called it
    assert "No matches" not in result.output


def test_extended_regex_alternation_finds_the_gradlew_path(tmp_path):
    # tapestry-5 HAD build.gradle and gradlew on disk. Plain `grep` is BRE,
    # where `|` is a literal character, so the harness's own build-file probe
    # could never match and the model was told the build files did not exist.
    listing = tmp_path / "paths.txt"
    listing.write_text("/workspace/tapestry-5/README.md\n/workspace/tapestry-5/gradlew\n")
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(
        target=f"file:{listing}",
        pattern=r"(^|/)build\.gradle$|(^|/)gradlew$",
    )

    assert result.succeeded is True
    assert result.facts["matched"] is True
    assert "2:/workspace/tapestry-5/gradlew" in result.output


def test_directory_target_is_searched_recursively(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "build.gradle").write_text("apply plugin: 'java'\n")
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{tmp_path}", pattern="apply plugin")

    assert result.succeeded is True
    assert result.facts["matched"] is True
    assert "build.gradle" in result.output
    assert "Is a directory" not in result.output
    assert "No matches" not in result.output


def test_grep_error_survives_the_head_pipe(tmp_path):
    subject = tmp_path / "a.txt"
    subject.write_text("x\n")

    # The trap, run for real: `head` is the LAST command in the pipeline, so an
    # unguarded `$?` is head's 0 and the erroring grep vanishes behind it.
    naive = subprocess.run(
        ["/bin/bash", "-c", f"grep -nE 'foo(' {subject} | head -50"],
        capture_output=True,
        text=True,
    )
    assert naive.returncode == 0
    assert "grep:" in naive.stderr

    orch = LocalShellOrchestrator(tmp_path)
    result = SearchTool(orch).execute(target=f"file:{subject}", pattern="foo(")

    # Same shell, same pipe, same head -- the tool's command keeps grep's status.
    assert orch.commands and "head -" in orch.commands[0]
    assert result.succeeded is False
    assert result.error_code == "SEARCH_FAILED"
    assert "No matches" not in result.output


def test_transport_error_exit_is_never_reported_as_no_matches():
    diagnostic = "grep: /workspace/p: Is a directory"
    orch = FakeOrchestrator(
        responses={
            "/workspace/p": {
                "success": False,
                "exit_code": 2,
                "output": diagnostic,
                "stdout": "",
                "stderr": diagnostic,
            }
        }
    )

    result = SearchTool(orch).execute(target="file:/workspace/p", pattern="gradle")

    assert result.succeeded is False
    assert result.error_code == "SEARCH_FAILED"
    assert diagnostic in result.output


def test_dispatch_failure_is_a_typed_failure_not_an_empty_answer():
    orch = FakeOrchestrator(
        responses={
            "/workspace/p": {
                "success": False,
                "exit_code": -1,
                "output": "docker exec was never accepted",
                "dispatch_status": "dispatch_failed",
            }
        }
    )

    result = SearchTool(orch).execute(target="file:/workspace/p/pom.xml", pattern="gradle")

    assert result.succeeded is False
    assert result.error_code == "SEARCH_FAILED"
    assert "No matches" not in result.output


def test_output_capped_by_head_is_a_capped_read_not_a_failure(tmp_path):
    # Enough output to overrun the pipe buffer, so `head` closes the pipe while
    # grep is still writing and grep dies of SIGPIPE (141). Exit 141 means the
    # read was CAPPED, not that it errored.
    big = tmp_path / "big.txt"
    big.write_text("".join(f"match line {index}\n" for index in range(200_000)))
    orch = LocalShellOrchestrator(tmp_path)

    result = SearchTool(orch).execute(target=f"file:{big}", pattern="match", max_results=5)

    assert result.succeeded is True
    assert result.facts["matched"] is True
    assert result.output.splitlines()[0] == "1:match line 0"
    assert len([line for line in result.output.splitlines() if line[:1].isdigit()]) == 5
    assert "capped at 5" in result.output
