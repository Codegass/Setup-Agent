"""Phase 4.3 — detached dispatch + log-tail polling (soft-timeout handoff).

Long build/test commands run detached with output in a container log file.
If they finish inside the soft window the tool gets a normal result; if not,
the agent gets the log tail + poll instructions and the process keeps running.
"""

from types import SimpleNamespace

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.agent.output_storage import OutputStorageManager
from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.base import bind_tool_result_output_storage
from sag.tools.internal.build_utils import (
    classify_detached_completion,
    detached_handoff_tool_result,
)
from sag.tools.search_tool import SearchTool


def build_orchestrator(execute_command=None):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.container_name = "sag-demo"
    orchestrator.command_log = []
    if execute_command is not None:
        orchestrator.execute_command = execute_command
    return orchestrator


# --- execute_command_detached ----------------------------------------------


def test_detached_dispatch_builds_nohup_launcher_and_returns_handle():
    orchestrator = build_orchestrator()

    def fake_execute(command, **kwargs):
        orchestrator.command_log.append(command)
        return {"exit_code": 0, "output": "12345"}

    orchestrator.execute_command = fake_execute
    orchestrator._runtime_profile_prefix = lambda: "true"

    handle = orchestrator.execute_command_detached(
        "./gradlew compileJava --no-daemon", workdir="/workspace/beam"
    )

    assert handle["started"] is True
    assert handle["pid"] == 12345
    assert handle["log_path"].startswith("/tmp/sag_jobs/")
    assert handle["exit_code_path"] == handle["log_path"] + ".exit"
    assert handle["pid_path"].endswith(".pid")
    launcher = orchestrator.command_log[0]
    assert "nohup bash -c" in launcher
    assert "pid=$!" in launcher
    assert handle["pid_path"] in launcher
    assert "/workspace/beam" in launcher
    assert "./gradlew compileJava --no-daemon" in launcher


def test_detached_dispatch_failure_reported():
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 1, "output": "boom"}
    )
    orchestrator._runtime_profile_prefix = lambda: "true"

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is False


# --- poll_detached_command ---------------------------------------------------


def _handle():
    return {
        "started": True,
        "job_id": "abc",
        "pid": 12345,
        "pid_path": "/tmp/sag_jobs/abc.pid",
        "log_path": "/tmp/sag_jobs/abc.log",
        "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
        "command": "./gradlew compileJava",
    }


def test_poll_running_returns_tail():
    output = "STATE:RUNNING\nSIZE:2048\n---TAIL---\n> Task :compileJava\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["running"] is True
    assert poll["finished"] is False
    assert poll["exit_code"] is None
    assert "Task :compileJava" in poll["tail"]
    assert poll["log_size"] == 2048


def test_poll_finished_returns_exit_code():
    output = "STATE:EXIT:0\nSIZE:4096\n---TAIL---\nBUILD SUCCESSFUL in 32m\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["finished"] is True
    assert poll["exit_code"] == 0
    assert "BUILD SUCCESSFUL" in poll["tail"]


def test_poll_failed_exit_code_parsed():
    output = "STATE:EXIT:1\nSIZE:4096\n---TAIL---\nBUILD FAILED\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["finished"] is True
    assert poll["exit_code"] == 1


# --- execute_command_with_soft_timeout ---------------------------------------


def _soft_timeout_orchestrator(poll_results, log_content="BUILD SUCCESSFUL"):
    orchestrator = build_orchestrator()
    orchestrator.execute_command_detached = lambda command, **kwargs: _handle()
    polls = iter(poll_results)
    last = poll_results[-1]
    orchestrator.poll_detached_command = lambda handle, **kwargs: next(polls, last)
    orchestrator.execute_command = lambda command, **kwargs: {
        "exit_code": 0,
        "output": log_content,
    }
    return orchestrator


RUNNING_POLL = {
    "finished": False,
    "running": True,
    "exit_code": None,
    "tail": "> Task :compileJava",
    "log_size": 100,
    "probe_success": True,
}
FINISHED_POLL = {
    "finished": True,
    "running": False,
    "exit_code": 0,
    "tail": "BUILD SUCCESSFUL",
    "log_size": 200,
    "probe_success": True,
}


def test_soft_timeout_returns_full_result_when_finished_in_window():
    orchestrator = _soft_timeout_orchestrator([RUNNING_POLL, FINISHED_POLL])

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=30, poll_interval=0.01
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["dispatch_status"] == "completed_detached"
    assert "BUILD SUCCESSFUL" in result["output"]
    assert result["termination_reason"] is None


def test_collect_detached_result_preserves_full_log_untruncated_for_storage():
    """A finished detached build log must be read WITHOUT the orchestrator's
    emergency truncation and handed back complete under `full_output`, so the
    build tools can persist the real error to the output store. The inline
    `output` stays bounded so it never floods the model context (Brooklyn: the
    truncated cat hid the compile error and the agent looped blind)."""
    orchestrator = build_orchestrator()
    middle_marker = "[ERROR] COMPILATION ERROR in BrooklynModule.java"
    big_log = (
        "\n".join(f"[INFO] downloading dep {i}" for i in range(1500))
        + f"\n{middle_marker}\n"
        + "\n".join(f"[INFO] trailing line {i}" for i in range(1500))
        + "\nBUILD FAILURE\n"
    )
    assert len(big_log) > 10000  # large enough to trigger inline bounding

    seen = {}

    def fake_execute(command, **kwargs):
        seen["truncate_output"] = kwargs.get("truncate_output", True)
        seen["command"] = command
        return {"exit_code": 0, "output": big_log}

    orchestrator.execute_command = fake_execute

    result = orchestrator._collect_detached_result(
        _handle(), {"finished": True, "exit_code": 1, "tail": "BUILD FAILURE"}
    )

    # The log was read with truncation explicitly disabled...
    assert seen["truncate_output"] is False
    assert seen["command"].startswith("cat ")
    # ...so the complete log (including the mid-stream error) is preserved.
    assert result["full_output"] == big_log
    assert middle_marker in result["full_output"]
    # The inline output is bounded for context safety.
    assert len(result["output"]) < len(result["full_output"])
    assert result["exit_code"] == 1
    assert result["dispatch_status"] == "completed_detached"


def test_soft_timeout_hands_off_still_running_command():
    orchestrator = _soft_timeout_orchestrator([RUNNING_POLL])

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=1, poll_interval=0.01
    )

    assert result["dispatch_status"] == "running_detached"
    assert result["success"] is True, "a handoff is not a failure"
    assert result["exit_code"] is None
    assert result["termination_reason"] is None
    assert "/tmp/sag_jobs/abc.log" in result["output"]
    assert "tail -n" in result["output"]
    assert "do NOT start the same build again" in result["output"]


def test_soft_timeout_vanished_process_fails_safe():
    vanished = {
        "finished": False,
        "running": False,
        "exit_code": None,
        "tail": "killed",
        "log_size": 10,
        "probe_success": True,
    }
    orchestrator = _soft_timeout_orchestrator([vanished], log_content="killed")

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=30, poll_interval=0.01
    )

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["dispatch_status"] == "completed_detached"


def test_soft_timeout_unknown_liveness_is_not_handed_off_as_active():
    unknown = {
        "finished": False,
        "running": False,
        "exit_code": None,
        "tail": "last known output",
        "log_size": 17,
        "probe_success": False,
        "state": "unknown",
    }
    orchestrator = _soft_timeout_orchestrator([unknown], log_content="last known output")

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=1, poll_interval=0.01
    )

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["dispatch_status"] == "completed_detached"
    assert result["lifecycle_state"] == "unknown"


def test_soft_timeout_dispatch_failure_is_failure_result():
    orchestrator = build_orchestrator()
    orchestrator.execute_command_detached = lambda command, **kwargs: {
        "started": False,
        "launch_output": "no shell",
        "pid": None,
        "log_path": "x",
        "exit_code_path": "x.exit",
        "job_id": "x",
        "command": command,
    }

    result = orchestrator.execute_command_with_soft_timeout("mvn package", soft_timeout=1)

    assert result["success"] is False
    assert result["dispatch_status"] == "dispatch_failed"


# --- tool-level handoff -------------------------------------------------------


def test_detached_handoff_is_a_promise_not_success():
    result = {
        "output": "still running; tail -n 50 /tmp/sag_jobs/abc.log",
        "dispatch_status": "running_detached",
        "dispatch": {
            "job_id": "abc",
            "pid": 12345,
            "log_path": "/tmp/sag_jobs/abc.log",
            "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            "soft_timeout": 900,
        },
    }

    tool_result = detached_handoff_tool_result("gradle", "./gradlew build", result)

    assert tool_result.invocation_status is InvocationStatus.PENDING
    assert tool_result.operation_outcome is OperationOutcome.UNKNOWN
    assert tool_result.evidence_status is EvidenceStatus.UNKNOWN
    assert tool_result.poll_ref == "job:abc"
    assert "tail -n 50" in tool_result.output
    assert tool_result.metadata["dispatch_status"] == "running_detached"
    assert tool_result.metadata["pid"] == 12345
    assert tool_result.metadata["log_path"] == "/tmp/sag_jobs/abc.log"


def test_fatal_tail_overrides_zero_exit_plumbing():
    result = classify_detached_completion(
        exit_code=0,
        tail="CMake Error: configuration failed",
        full_output_ref="output_cmake",
    )

    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.error_code == "DETACHED_OPERATION_FAILED"
    assert result.failure_signature
    assert result.error_tail_preview == "CMake Error: configuration failed"
    assert result.output_ref == "output_cmake"


def test_missing_exit_code_is_unknown_not_success():
    result = classify_detached_completion(
        exit_code=None,
        tail="detached process state could not be established",
        full_output_ref="output_detached_unknown",
    )

    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.succeeded is False


class PollingJobOrchestrator:
    def __init__(self, poll, collected=None):
        self.poll = poll
        self.collected = collected or {}
        self.handles = []

    def detached_handle(self, job_id):
        return {
            "job_id": job_id,
            "log_path": f"/tmp/sag_jobs/{job_id}.log",
            "exit_code_path": f"/tmp/sag_jobs/{job_id}.log.exit",
            "pid_path": f"/tmp/sag_jobs/{job_id}.pid",
        }

    def poll_detached_command(self, handle, **kwargs):
        self.handles.append(handle)
        return dict(self.poll)

    def collect_detached_result(self, handle, poll):
        return dict(self.collected)

    def execute_command(self, command, **kwargs):
        # The old implementation greps the log and reports that grep invocation
        # as the original operation's successful completion.
        return {"exit_code": 0, "output": self.poll.get("tail", "")}


def test_search_job_poll_preserves_pending_for_active_original_operation():
    orchestrator = PollingJobOrchestrator(
        {
            "finished": False,
            "running": True,
            "exit_code": None,
            "tail": "compiling module 3/10",
            "log_size": 200,
            "probe_success": True,
            "state": "running",
        }
    )

    result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.poll_ref == "job:abc"
    assert "compiling module 3/10" in result.output


def test_search_job_poll_classifies_terminal_fatal_tail_for_original_operation(tmp_path):
    orchestrator = PollingJobOrchestrator(
        {
            "finished": True,
            "running": False,
            "exit_code": 0,
            "tail": "CMake Error: configuration failed",
            "log_size": 42,
            "probe_success": True,
            "state": "finished",
        },
        {
            "exit_code": 0,
            "output": "CMake Error: configuration failed",
            "full_output": "CMake Error: configuration failed",
            "dispatch_status": "completed_detached",
        },
    )
    storage = OutputStorageManager(tmp_path)

    with bind_tool_result_output_storage(storage, task_id="detached", tool_name="search"):
        result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.poll_ref == "job:abc"
    assert result.output_ref.startswith("output_")
    assert storage.retrieve_output(result.output_ref) == "CMake Error: configuration failed"
    assert result.error_code == "DETACHED_OPERATION_FAILED"


# --- review fixes: spoof-proof poll parsing, atomic exit file ----------------


def test_poll_markers_in_log_tail_cannot_spoof_completion():
    """STATE:/SIZE: lines printed by the build itself land in the tail and
    must not be parsed as completion markers."""
    output = "STATE:RUNNING\nSIZE:100\n---TAIL---\n" "some build output\nSTATE:EXIT:0\nSIZE:99999\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["running"] is True
    assert poll["finished"] is False
    assert poll["exit_code"] is None
    assert poll["log_size"] == 100


def test_detached_launcher_writes_exit_code_atomically():
    orchestrator = build_orchestrator()

    def fake_execute(command, **kwargs):
        orchestrator.command_log.append(command)
        return {"exit_code": 0, "output": "999"}

    orchestrator.execute_command = fake_execute
    orchestrator._runtime_profile_prefix = lambda: "true"

    handle = orchestrator.execute_command_detached("mvn package")

    launcher = orchestrator.command_log[0]
    assert ".exit.tmp" in launcher
    assert "&& mv" in launcher


# --- review fixes: tools actually route long commands through dispatch ------


class RoutingOrchestrator:
    """Fake with both execution APIs to prove the dispatch path is preferred.

    execute_command answers the tools' executable/wrapper probes the same way
    tests/test_maven_gradle_tool_contracts.py's fake does.
    """

    def __init__(self, handoff=True):
        self.soft_timeout_calls = []
        self.monitoring_calls = []
        self.project_name = None
        self._handoff = handoff

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        if command in ("which mvn", "command -v mvn"):
            return {"success": True, "output": "/usr/bin/mvn", "exit_code": 0}
        if command in ("which gradle", "command -v gradle"):
            return {"success": True, "output": "/usr/bin/gradle", "exit_code": 0}
        if command.startswith("test -x /usr/bin/mvn") or command.startswith(
            "test -x /usr/bin/gradle"
        ):
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if command == "/usr/bin/mvn -version":
            return {"success": True, "output": "Apache Maven 3.9.6", "exit_code": 0}
        if command == "/usr/bin/gradle -version":
            return {"success": True, "output": "Gradle 8.5", "exit_code": 0}
        if "pom.xml && echo 'EXISTS'" in command:
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if "grep -q '<modules>'" in command:
            return {"success": False, "output": "NO_MODULES", "exit_code": 1}
        if "settings.gradle" in command and "grep -q 'include'" in command:
            return {"success": False, "output": "", "exit_code": 1}
        return {"exit_code": 0, "output": "", "success": True}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitoring_calls.append(command)
        return {"exit_code": 0, "output": "BUILD SUCCESSFUL", "success": True}

    def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
        self.soft_timeout_calls.append(command)
        if self._handoff:
            return {
                "success": True,
                "exit_code": None,
                "output": "still running; poll /tmp/sag_jobs/abc.log",
                "termination_reason": None,
                "dispatch_status": "running_detached",
                "dispatch": {
                    "pid": 1,
                    "log_path": "/tmp/sag_jobs/abc.log",
                    "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
                },
            }
        return {
            "success": True,
            "exit_code": 0,
            "output": "BUILD SUCCESSFUL",
            "termination_reason": None,
            "dispatch_status": "completed_detached",
            "dispatch": {
                "job_id": "abc",
                "log_path": "/tmp/sag_jobs/abc.log",
                "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            },
        }


def test_gradle_tool_routes_build_through_dispatch_and_returns_handoff():
    from sag.tools.internal.gradle_tool import GradleTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = GradleTool(orchestrator)

    result = tool.execute(tasks="build", working_directory="/workspace/p")

    assert orchestrator.soft_timeout_calls, "gradle build must use dispatch-and-poll"
    assert orchestrator.monitoring_calls == []
    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.metadata["dispatch_status"] == "running_detached"
    assert "/tmp/sag_jobs/abc.log" in result.output


def test_maven_tool_routes_test_through_dispatch():
    from sag.tools.internal.maven_tool import MavenTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = MavenTool(orchestrator)

    result = tool.execute(command="test", working_directory="/workspace/p")

    assert orchestrator.soft_timeout_calls, "maven test must use dispatch-and-poll"
    assert result.metadata["dispatch_status"] == "running_detached"


def test_bash_tool_routes_long_command_through_dispatch():
    from sag.tools.bash import BashTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = BashTool(orchestrator)

    result = tool.execute(command="mvn verify", timeout=1200)

    assert orchestrator.soft_timeout_calls, "mvn verify must use dispatch-and-poll"
    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.metadata["dispatch_status"] == "running_detached"


def test_bash_quick_inspection_commands_not_dispatched():
    from sag.tools.bash import BashTool

    orchestrator = RoutingOrchestrator()
    tool = BashTool(orchestrator)

    for command in (
        "cat build.gradle",
        "ls src/test/java",
        "tail -n 50 /tmp/sag_jobs/abc.log",
        "test -d /workspace/p",
    ):
        tool.execute(command=command, timeout=60)

    assert orchestrator.soft_timeout_calls == []


# --- review fixes: repetition detector must not fight polling ---------------


def test_repetition_detector_exempts_dispatch_poll_commands():
    from sag.agent.tool_orchestration import ToolOrchestrator

    poll_signature = (
        "bash:[('command', 'tail -n 50 /tmp/sag_jobs/abc.log'), "
        "('working_directory', '/workspace')]"
    )
    recent = [
        {
            "signature": poll_signature,
            "invocation_status": "completed",
            "operation_outcome": "success",
            "timestamp": f"ts-{i}",
        }
        for i in range(9)
    ]
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=recent,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    # The poll command itself is never treated as a loop...
    assert orchestrator._get_repetition_level(poll_signature) == 0
    # ...and 9 polls must not inflate the bash flood count for other commands.
    other_signature = "bash:[('command', 'ls /workspace')]"
    assert orchestrator._get_repetition_level(other_signature) == 0


# --- round-3 fix: dispatch routing scans the command line, not heredoc bodies


def test_heredoc_body_keywords_do_not_trigger_dispatch():
    """Round-3 finding: `python - <<'PY' ... import unittest ...` was dispatched
    because keywords in the heredoc BODY matched. Only the command line before
    the heredoc marker may be scanned."""
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert (
        tool._is_long_running_command(
            "python - <<'PY'\nimport subprocess\nsubprocess.run(['mvn','test'])\nPY"
        )
        is False
    )


def test_version_probe_flags_are_quick():
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert tool._is_long_running_command("./mvnw -v") is False
    assert tool._is_long_running_command("mvn --version") is False
    assert tool._is_long_running_command("./gradlew --version") is False


def test_real_builds_still_dispatch():
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert tool._is_long_running_command("mvn clean install") is True
    assert tool._is_long_running_command("./gradlew compileJava --no-daemon") is True
