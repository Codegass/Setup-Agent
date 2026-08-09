"""Internal runners have no independent authority or contract producer."""

import pytest

from sag.agent.invocation_contracts import CONTRACT_AUTHORITY_MISSING
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.python_tool import PythonTool


class _NoDispatchOrchestrator:
    def __init__(self):
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return {"success": True, "exit_code": 0, "output": ""}


@pytest.mark.parametrize(
    ("factory", "call"),
    [
        (MavenTool, {"command": "validate", "working_directory": "/workspace/repo"}),
        (GradleTool, {"tasks": "help", "working_directory": "/workspace/repo"}),
        (PythonTool, {"operation": "setup_env", "working_directory": "/workspace/repo"}),
    ],
)
def test_direct_internal_tool_refuses_before_any_probe_or_runner(factory, call):
    orchestrator = _NoDispatchOrchestrator()
    tool = factory(orchestrator)

    result = tool.execute(**call)

    assert result.error_code == CONTRACT_AUTHORITY_MISSING
    assert result.metadata["runner_dispatched"] is False
    # This single assertion covers manifest reads, preflight, wrapper/tool
    # discovery, report snapshots, runner dispatch, receipt writes, and job
    # obligations: none may happen before authority exists.
    assert orchestrator.commands == []
