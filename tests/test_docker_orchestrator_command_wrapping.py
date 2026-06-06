import shlex

from sag.docker_orch import orch
from sag.docker_orch.orch import DockerOrchestrator


class FakeExecResult:
    def __init__(self, exit_code=0, output=(b"ok", b"")):
        self.exit_code = exit_code
        self.output = output


class FakeStreamingExecResult:
    def __init__(self, exit_code=0, output=()):
        self.exit_code = exit_code
        self.output = iter(output)


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
    workdir = "/workspace/project"

    result = orchestrator.execute_command("echo hi", workdir=workdir)

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][2]
    assert wrapped_command.startswith(
        "source /workspace/.setup_agent/env_overlay.sh 2>/dev/null || true; "
        "source /etc/profile 2>/dev/null || true; "
        "source ~/.bashrc 2>/dev/null || true; "
        f"cd {shlex.quote(workdir)} && echo hi"
    )


def test_execute_command_shell_quotes_workdir_with_space_and_single_quote():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project with ' quote"

    result = orchestrator.execute_command("echo hi", workdir=workdir)

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][2]
    assert f"cd {shlex.quote(workdir)} && echo hi" in wrapped_command
    assert "cd /workspace/project with" not in wrapped_command


def test_execute_command_with_monitoring_sources_env_overlay_before_cd_and_command():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project"

    result = orchestrator.execute_command_with_monitoring(
        "echo hi",
        workdir=workdir,
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][2]
    assert wrapped_command.startswith(
        "source /workspace/.setup_agent/env_overlay.sh 2>/dev/null || true; "
        "source /etc/profile 2>/dev/null || true; "
        "source ~/.bashrc 2>/dev/null || true; "
        f"cd {shlex.quote(workdir)} && echo hi"
    )


def test_execute_command_with_monitoring_treats_unknown_exit_build_failure_as_failure():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[(b"[ERROR] BUILD FAILURE\nCould not resolve dependency\n", b"")],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] == 1


def test_execute_command_with_monitoring_treats_unknown_exit_ordinary_output_as_success():
    container = FakeContainer(
        FakeStreamingExecResult(exit_code=None, output=[(b"[INFO] BUILD SUCCESS\n", b"")])
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0


def test_execute_command_with_monitoring_keeps_unknown_exit_could_not_resolve_narrative_success():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[
                (
                    b"Diagnostic note: Could not resolve whether optional docs are installed.\n",
                    b"",
                )
            ],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "bash -lc 'printf diagnostics'",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0


def test_execute_command_with_monitoring_keeps_unknown_exit_allowed_range_narrative_success():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[
                (
                    b"Release note: values not in the allowed range are normalized later.\n",
                    b"",
                )
            ],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "bash -lc 'printf diagnostics'",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0


def test_execute_command_with_monitoring_preserves_quoted_workdir_in_timeout_wrapper():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project with ' quote"

    result = orchestrator.execute_command_with_monitoring(
        "echo hi",
        workdir=workdir,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    final_command = container.exec_calls[-1]["exec_command"][2]
    timeout_args = shlex.split(final_command)
    assert timeout_args[:5] == ["timeout", "--preserve-status", "2400", "bash", "-c"]
    base_command = timeout_args[5]
    assert f"cd {shlex.quote(workdir)} && echo hi" in base_command
    assert "cd /workspace/project with" not in base_command
