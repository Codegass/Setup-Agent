"""Docker streaming completion must be observed, never inferred from its output."""

import subprocess

import pytest
from test_docker_orchestrator_command_wrapping import (
    FakeContainer,
    FakeDaemonAPI,
    FakeStreamingExecResult,
    build_orchestrator,
)


class SubprocessExecAPI(FakeDaemonAPI):
    """Run actual shell argv locally, supplying Docker's exec observation shape."""

    def __init__(self, container_id):
        super().__init__(container_id)
        self.process = None
        self.inspect_override = None
        self.inspect_calls = []

    def exec_start(self, exec_id, **kwargs):
        self.start_calls.append({"exec_id": exec_id, "kwargs": kwargs})
        call = self.create_calls[-1]
        self.process = subprocess.run(call["cmd"], capture_output=True, timeout=10)
        return iter([(self.process.stdout, self.process.stderr)])

    def exec_inspect(self, exec_id):
        self.inspect_calls.append(exec_id)
        if isinstance(self.inspect_override, Exception):
            raise self.inspect_override
        return self.inspect_override or {
            "ID": exec_id,
            "ContainerID": self.container_id,
            "Running": False,
            "ExitCode": self.process.returncode,
        }


def monitored(command, override=None):
    container = FakeContainer(FakeStreamingExecResult(exit_code=None, output=[]))
    orchestrator = build_orchestrator(container)
    api = SubprocessExecAPI(container.id)
    api.inspect_override = override
    orchestrator.client.api = api
    result = orchestrator.execute_command_with_monitoring(
        command, use_timeout_wrapper=False, enable_cpu_monitoring=False, optimize_for_maven=False
    )
    return result, api


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/workspace/nonexistent-python-l1-regression -m pytest", 127),
        ("exit 19", 19),
        ("printf 'BUILD SUCCESS'; exit 19", 19),
        ("printf 'BUILD FAILURE in a diagnostic example'; exit 0", 0),
    ],
)
def test_monitoring_preserves_real_process_exit_independent_of_output(command, expected):
    result, api = monitored(command)
    assert api.process.returncode == expected
    assert api.inspect_calls == [api.exec_id]
    assert result["exit_code"] == expected
    assert result["observed_exit_code"] == expected
    assert result["exit_code_inferred"] is False
    assert result["success"] is (expected == 0)
    assert result["runner_dispatched"] is True
    assert result["execution_observation_complete"] is True
    assert result["full_output"] == (api.process.stdout + api.process.stderr).decode()


@pytest.mark.parametrize(
    "override",
    [
        {"ID": "d" * 64, "ContainerID": "c" * 64, "Running": True, "ExitCode": 0},
        {"ID": "d" * 64, "ContainerID": "c" * 64, "Running": False, "ExitCode": None},
        {"ID": "f" * 64, "ContainerID": "c" * 64, "Running": False, "ExitCode": 0},
        {"ID": "d" * 64, "ContainerID": "f" * 64, "Running": False, "ExitCode": 0},
        RuntimeError("Docker inspect unavailable"),
    ],
)
def test_monitoring_unknown_or_foreign_terminal_observation_never_becomes_success(override):
    result, api = monitored("printf 'BUILD SUCCESS'", override)
    assert api.process.returncode == 0
    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["observed_exit_code"] is None
    assert result["exit_code_inferred"] is False
    assert result["runner_dispatched"] is True
    assert result["execution_observation_complete"] is False
    assert result["dispatch_status"] == "execution_observation_failed"
    assert result["full_output"] == "BUILD SUCCESS"
    assert "result unavailable" in result["output"]
