"""Phase 4.3 — detached dispatch + log-tail polling (soft-timeout handoff).

Long build/test commands run detached with output in a container log file.
If they finish inside the soft window the tool gets a normal result; if not,
the agent gets the log tail + poll instructions and the process keeps running.
"""

from types import SimpleNamespace

from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.build_utils import detached_handoff_tool_result


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
    launcher = orchestrator.command_log[0]
    assert "nohup bash -c" in launcher
    assert "echo $!" in launcher
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


def test_detached_handoff_tool_result_shape():
    result = {
        "output": "still running; tail -n 50 /tmp/sag_jobs/abc.log",
        "dispatch_status": "running_detached",
        "dispatch": {
            "pid": 12345,
            "log_path": "/tmp/sag_jobs/abc.log",
            "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            "soft_timeout": 900,
        },
    }

    tool_result = detached_handoff_tool_result("gradle", "./gradlew build", result)

    assert tool_result.success is True
    assert "tail -n 50" in tool_result.output
    assert tool_result.metadata["dispatch_status"] == "running_detached"
    assert tool_result.metadata["pid"] == 12345
    assert tool_result.metadata["log_path"] == "/tmp/sag_jobs/abc.log"


# --- review fixes: spoof-proof poll parsing, atomic exit file ----------------


def test_poll_markers_in_log_tail_cannot_spoof_completion():
    """STATE:/SIZE: lines printed by the build itself land in the tail and
    must not be parsed as completion markers."""
    output = (
        "STATE:RUNNING\nSIZE:100\n---TAIL---\n"
        "some build output\nSTATE:EXIT:0\nSIZE:99999\n"
    )
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
            "dispatch": {},
        }


def test_gradle_tool_routes_build_through_dispatch_and_returns_handoff():
    from sag.tools.gradle_tool import GradleTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = GradleTool(orchestrator)

    result = tool.execute(tasks="build", working_directory="/workspace/p")

    assert orchestrator.soft_timeout_calls, "gradle build must use dispatch-and-poll"
    assert orchestrator.monitoring_calls == []
    assert result.success is True
    assert result.metadata["dispatch_status"] == "running_detached"
    assert "/tmp/sag_jobs/abc.log" in result.output


def test_maven_tool_routes_test_through_dispatch():
    from sag.tools.maven_tool import MavenTool

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
    assert result.success is True
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
        {"signature": poll_signature, "success": True, "timestamp": f"ts-{i}"}
        for i in range(9)
    ]
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=recent,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    # The poll command itself is never treated as a loop...
    assert orchestrator._get_repetition_level(poll_signature) == 0
    # ...and 9 polls must not inflate the bash flood count for other commands.
    other_signature = "bash:[('command', 'ls /workspace')]"
    assert orchestrator._get_repetition_level(other_signature) == 0
