from sag.docker_orch import orch
from sag.docker_orch.orch import DockerOrchestrator


class FakeExecResult:
    def __init__(self, exit_code=0, output=(b"ok", b"")):
        self.exit_code = exit_code
        self.output = output


class FakeStreamingExecResult:
    exit_code = 0
    output = iter(())


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

    def get(self, _container_name):
        return self.container


class FakeClient:
    def __init__(self, container):
        self.containers = FakeContainers(container)


def build_orchestrator(container):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.client = FakeClient(container)
    orchestrator.container_name = "sag-demo"
    orchestrator.is_container_running = lambda: True
    return orchestrator


def test_runtime_profile_prefix_sources_env_overlay_before_shell_profiles():
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)

    prefix = orchestrator._runtime_profile_prefix()

    assert orch.ENV_OVERLAY_SCRIPT_PATH == "/workspace/.setup_agent/env_overlay.sh"
    assert prefix.index("source /workspace/.setup_agent/env_overlay.sh 2>/dev/null || true") < (
        prefix.index("source /etc/profile 2>/dev/null || true")
    )
    assert prefix.index("source /etc/profile 2>/dev/null || true") < (
        prefix.index("source ~/.bashrc 2>/dev/null || true")
    )


def test_execute_command_sources_env_overlay_before_cd_and_command():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command("echo hi", workdir="/workspace/project")

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][2]
    assert wrapped_command.startswith(
        "source /workspace/.setup_agent/env_overlay.sh 2>/dev/null || true; "
        "source /etc/profile 2>/dev/null || true; "
        "source ~/.bashrc 2>/dev/null || true; "
        "cd '/workspace/project' && echo hi"
    )


def test_execute_command_with_monitoring_sources_env_overlay_before_cd_and_command():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "echo hi",
        workdir="/workspace/project",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][2]
    assert wrapped_command.startswith(
        "source /workspace/.setup_agent/env_overlay.sh 2>/dev/null || true; "
        "source /etc/profile 2>/dev/null || true; "
        "source ~/.bashrc 2>/dev/null || true; "
        "cd /workspace/project && echo hi"
    )
