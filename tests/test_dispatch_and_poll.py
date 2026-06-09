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
