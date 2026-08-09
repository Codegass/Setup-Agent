from sag.agent.evidence_publications import EvidencePublicationAuthority
from sag.docker_orch.orch import DockerOrchestrator
from sag.evidence import InvocationStatus
from sag.tools.bash import BashTool


class FakeBashOrchestrator:
    def __init__(self, command_result=None, monitoring_result=None):
        self.project_name = "demo"
        self.command_calls = []
        self.monitoring_calls = []
        self.command_result = command_result
        self.monitoring_result = monitoring_result

    def execute_command(
        self,
        command,
        workdir=None,
        capture_stderr=True,
        environment=None,
        timeout=None,
    ):
        self.command_calls.append(
            {
                "command": command,
                "workdir": workdir,
                "capture_stderr": capture_stderr,
                "environment": environment,
                "timeout": timeout,
            }
        )
        if "test -d -- " in command:
            # A real POSIX test refuses `--` (three args make the middle a
            # binary operator): the permissive old fake accepted it, which is
            # exactly how the live 2026-08-09 all-bash-refused defect passed
            # its tests. Model the shell, not the hope.
            return {
                "success": False,
                "output": "test: --: unexpected operator",
                "exit_code": 2,
                "stdout": "",
                "stderr": "test: --: unexpected operator",
            }
        if "test -d /workspace" in command:
            return {
                "success": True,
                "output": "EXISTS",
                "exit_code": 0,
                "stdout": "EXISTS",
                "stderr": "",
            }
        if self.command_result is not None:
            return self.command_result
        return {
            "success": True,
            "output": "ok",
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }

    def execute_command_with_monitoring(self, **kwargs):
        self.monitoring_calls.append(dict(kwargs))
        if self.monitoring_result is not None:
            return self.monitoring_result
        return {
            "success": True,
            "output": "installed",
            "exit_code": 0,
            "stdout": "installed",
            "stderr": "",
            "monitoring_info": {},
        }


class FakeExecResult:
    def __init__(self, exit_code=0, output=(b"ok", b"")):
        self.exit_code = exit_code
        self.output = output


class FakeContainer:
    def __init__(self, exec_result=None):
        self.exec_calls = []
        self.exec_result = exec_result or FakeExecResult()

    def exec_run(self, exec_command, **kwargs):
        self.exec_calls.append({"exec_command": exec_command, "kwargs": kwargs})
        return self.exec_result


class FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, container_name):
        return self.container


class FakeClient:
    def __init__(self, container):
        self.containers = FakeContainers(container)


def _authorized_orchestrator(container):
    """House pattern (tests/test_docker_orchestrator_command_wrapping.py):
    a normal runner dispatch requires a host publication authority, and the
    runtime overlay read is stubbed to the fixed control environment."""
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.client = FakeClient(container)
    orchestrator.container_name = "sag-demo"
    orchestrator.is_container_running = lambda: True
    orchestrator._sag_evidence_publication_authority = EvidencePublicationAuthority(
        run_id=f"run-orchestrator-{id(orchestrator)}"
    )

    def runtime_environment(environment=None):
        runtime = orchestrator._control_exec_environment()
        for key, value in (environment or {}).items():
            if key not in {"PATH", "BASH_ENV", "ENV", "CDPATH"}:
                runtime[key] = value
        return runtime

    orchestrator._default_exec_environment = runtime_environment
    return orchestrator


def test_docker_orchestrator_wraps_regular_command_with_timeout():
    container = FakeContainer()
    orchestrator = _authorized_orchestrator(container)

    result = orchestrator.execute_command("echo hi", timeout=9)

    assert result["success"] is True
    exec_command = container.exec_calls[-1]["exec_command"]
    # Premise: the timeout wrapper moved from a shell-string prefix to the
    # isolated exec argv, so controller utilities never resolve project PATH.
    assert exec_command[:3] == ["/usr/bin/timeout", "--preserve-status", "9"]
    assert exec_command[3] == "/usr/bin/env"
    assert exec_command[-3:-1] == ["/bin/bash", "-c"]
    assert "echo hi" in exec_command[-1]


def test_docker_orchestrator_omits_timeout_wrapper_when_timeout_is_none():
    container = FakeContainer()
    orchestrator = _authorized_orchestrator(container)

    result = orchestrator.execute_command("echo hi")

    assert result["success"] is True
    exec_command = container.exec_calls[-1]["exec_command"]
    assert "/usr/bin/timeout" not in exec_command
    assert "timeout --preserve-status" not in exec_command[-1]
    assert "echo hi" in exec_command[-1]


def test_docker_orchestrator_reports_timeout_wrapper_exit_as_absolute_timeout():
    container = FakeContainer(exec_result=FakeExecResult(exit_code=124, output=(b"", b"")))
    orchestrator = _authorized_orchestrator(container)

    result = orchestrator.execute_command("sleep 99", timeout=5)

    assert result["success"] is False
    assert result["termination_reason"] == "absolute_timeout"
    assert result["monitoring_info"]["execution_time"] == 5
    assert result["timeout"] == 5


def test_bash_tool_regular_timeout_result_uses_timeout_error_code():
    orchestrator = FakeBashOrchestrator(
        command_result={
            "success": False,
            "output": "",
            "exit_code": 124,
            "termination_reason": "absolute_timeout",
            "monitoring_info": {"execution_time": 5},
        }
    )
    tool = BashTool(orchestrator)

    result = tool.execute(command="sleep 99", timeout=5)

    assert result.succeeded is False
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.error_code == "TIMEOUT_ABSOLUTE_TIMEOUT"
    assert result.metadata["timeout"] == 5
    assert result.metadata["termination_reason"] == "absolute_timeout"


def test_bash_tool_accepts_timeout_and_passes_it_to_regular_execution():
    orchestrator = FakeBashOrchestrator()
    tool = BashTool(orchestrator)

    result = tool.execute(command="echo hi", timeout=7)

    assert result.succeeded is True
    command_call = next(call for call in orchestrator.command_calls if call["command"] == "echo hi")
    assert command_call["timeout"] == 7
    assert result.metadata["timeout"] == 7


def test_bash_tool_safe_execute_accepts_schema_timeout():
    orchestrator = FakeBashOrchestrator()
    tool = BashTool(orchestrator)

    result = tool.safe_execute(command="echo hi", timeout=11)

    assert result.succeeded is True
    command_call = next(call for call in orchestrator.command_calls if call["command"] == "echo hi")
    assert command_call["timeout"] == 11


def test_bash_tool_timeout_overrides_monitored_absolute_timeout():
    orchestrator = FakeBashOrchestrator()
    tool = BashTool(orchestrator)

    result = tool.execute(command="npm install", timeout=120)

    assert result.succeeded is True
    assert orchestrator.monitoring_calls
    monitoring_call = orchestrator.monitoring_calls[-1]
    assert monitoring_call["absolute_timeout"] == 120
    assert monitoring_call["silent_timeout"] <= 120
    assert result.metadata["timeout"] == 120


def test_bash_tool_timeout_termination_reports_requested_timeout():
    orchestrator = FakeBashOrchestrator(
        monitoring_result={
            "success": False,
            "output": "still running",
            "exit_code": 124,
            "termination_reason": "absolute_timeout",
            "monitoring_info": {"execution_time": 120.0},
        }
    )
    tool = BashTool(orchestrator)

    result = tool.execute(command="npm install", timeout=120)

    assert result.succeeded is False
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert "2 minutes" in result.error
    assert result.metadata["timeout"] == 120
    assert result.metadata["termination_reason"] == "absolute_timeout"


def test_bash_build_command_preserves_submitted_command_and_cwd_despite_project_name():
    orchestrator = FakeBashOrchestrator()
    orchestrator.project_name = "must-not-be-injected"
    tool = BashTool(orchestrator)

    result = tool.execute(
        command="mvn verify",
        working_directory="/workspace",
        timeout=120,
    )

    assert result.succeeded is True
    assert orchestrator.monitoring_calls
    dispatched = orchestrator.monitoring_calls[-1]
    assert dispatched["command"] == "mvn verify"
    assert dispatched["workdir"] == "/workspace"
    assert "must-not-be-injected" not in dispatched["command"]
    assert result.metadata["execution"]["command"] == "mvn verify"
    assert result.metadata["execution"]["cwd"] == "/workspace"


def test_bash_missing_cwd_refuses_without_mkdir_or_fallback_dispatch():
    class MissingCwdOrchestrator(FakeBashOrchestrator):
        def execute_command(self, command, **kwargs):
            self.command_calls.append({"command": command, **kwargs})
            return {
                "success": False,
                "output": "",
                "exit_code": 1,
                "stdout": "",
                "stderr": "",
            }

    orchestrator = MissingCwdOrchestrator()
    tool = BashTool(orchestrator)

    result = tool.execute(
        command="echo should-not-run",
        working_directory="/workspace/missing",
    )

    assert result.succeeded is False
    assert result.error_code == "WORKING_DIRECTORY_UNAVAILABLE"
    assert result.metadata["execution"] == {
        "command": "echo should-not-run",
        "cwd": "/workspace/missing",
        "executed": False,
        "exit_code": None,
        "timed_out": False,
        "duration": 0,
    }
    assert len(orchestrator.command_calls) == 1
    # Two-argument form only: POSIX test has no `--`, and the three-argument
    # form is an unconditional exit 2 (see the fake's shell-faithful branch).
    assert orchestrator.command_calls[0]["command"] == "test -d /workspace/missing"
    assert not orchestrator.monitoring_calls
    assert all("mkdir" not in call["command"] for call in orchestrator.command_calls)
    assert all(
        fallback not in call["command"]
        for call in orchestrator.command_calls
        for fallback in ("/root", "/tmp")
    )


def test_bash_tool_monitoring_error_is_not_marked_as_timeout():
    orchestrator = FakeBashOrchestrator(
        monitoring_result={
            "success": False,
            "output": "monitor failed",
            "exit_code": 70,
            "termination_reason": "monitoring_error",
            "monitoring_info": {"execution_time": 12.0},
        }
    )
    tool = BashTool(orchestrator)

    result = tool.execute(command="npm install", timeout=120)

    assert result.succeeded is False
    assert result.error_code == "MONITORING_ERROR"
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["termination_reason"] == "monitoring_error"
