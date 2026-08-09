# tests/test_search_tool.py
"""search(target, pattern): one retrieval tool for refs, files, job logs, web.

Spec §4: target = ref id | file:<path> | job:<id> | web:<query>.
The ref/web paths DELEGATE to the existing OutputSearchTool/WebSearchTool
internals (stage-1 consolidates the surface, not the implementations).
"""

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
