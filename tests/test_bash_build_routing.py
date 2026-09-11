"""Literal JVM calls share evidence; shell semantics are never silently erased."""

from types import SimpleNamespace

import pytest
from test_bash_tool_timeout import FakeBashOrchestrator

from sag.tools.base import ToolResult
from sag.tools.bash import BashTool


@pytest.mark.parametrize(
    "command", ["mvn clean install", "./gradlew clean build", "mvn -V clean test"]
)
def test_literal_jvm_calls_reach_shared_runner(command):
    calls = []
    expected = ToolResult.completed_success(output="shared runner")
    facade = SimpleNamespace(execute_bash_command=lambda **params: calls.append(params) or expected)
    result = BashTool(FakeBashOrchestrator(), build_tool=facade).execute(
        command=command, timeout=17
    )
    assert result is expected
    assert calls == [
        {"command": command, "timeout": 17, "working_directory": "/workspace", "environment": None}
    ]


@pytest.mark.parametrize(
    "command, environment",
    [
        ("mvn -v", None),
        ("./gradlew --version", None),
        ("mvn clean install && echo done", None),
        ("mvn clean install", {"MAVEN_OPTS": "-Xmx1g"}),
    ],
)
def test_shell_only_calls_keep_their_command_and_environment(command, environment):
    class Shell(FakeBashOrchestrator):
        def execute_command_with_soft_timeout(self, **params):
            self.shell_params = params
            return {"success": True, "exit_code": 0, "output": "ok"}

    def unexpected(**params):
        raise AssertionError("this call must retain shell semantics")

    orch = Shell()
    result = BashTool(orch, build_tool=SimpleNamespace(execute_bash_command=unexpected)).execute(
        command=command, environment=environment
    )
    assert result.succeeded, result
    call = getattr(orch, "shell_params", None) or next(
        call for call in orch.command_calls if call["command"] == command
    )
    assert call["command"] == command
    if environment:
        for key, value in environment.items():
            assert call["environment"][key] == value
