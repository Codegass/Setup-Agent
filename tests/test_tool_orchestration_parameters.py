import pytest

from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class BashLikeTool(BaseTool):
    def __init__(self):
        super().__init__("bash", "Bash-like test tool")

    def execute(
        self, command: str, timeout: int, working_directory: str = ""
    ) -> ToolResult:
        return ToolResult(
            success=True,
            output=f"{working_directory}: {command} ({timeout})",
            metadata={
                "command": command,
                "timeout": timeout,
                "working_directory": working_directory,
            },
        )


def _orchestrator(**overrides):
    events = overrides.pop("events", [])
    tracking_calls = overrides.pop("tracking_calls", [])
    state_updates = overrides.pop("state_updates", [])

    orchestrator = ToolOrchestrator(
        tools=overrides.pop("tools", {"bash": BashLikeTool()}),
        context_manager=None,
        recent_tool_executions=[],
        successful_states=overrides.pop(
            "successful_states",
            {
                "working_directory": "/workspace/project",
                "maven_success": False,
                "cloned_repos": set(),
            },
        ),
        repository_url=overrides.pop("repository_url", None),
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
        **overrides,
    )
    return orchestrator, events, tracking_calls, state_updates


def test_parameter_alias_default_and_state_injection_are_recorded():
    orchestrator, events, tracking_calls, state_updates = _orchestrator()

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "echo hi"}))

    assert execution.status == "success"
    assert execution.executed_params == {
        "command": "echo hi",
        "timeout": 60,
        "working_directory": "/workspace/project",
    }
    assert execution.call.validated_params == execution.executed_params
    assert [event.event_type for event in events] == [
        "tool_start",
        "tool_parameters_fixed",
        "tool_result",
    ]

    fixed_event = events[1]
    assert fixed_event.metadata["raw_params"] == {"cmd": "echo hi"}
    assert fixed_event.metadata["validated_params"] == execution.executed_params
    assert fixed_event.metadata["parameter_fixes"] == execution.parameter_fixes
    assert fixed_event.metadata["params_changed"] is True

    fixes = {
        (fix.source, fix.field, fix.before, fix.after)
        for fix in execution.parameter_fixes
    }
    assert ("schema_alias", "command", "help", "echo hi") in fixes
    assert ("default", "timeout", None, 60) in fixes
    assert (
        "state_injection",
        "working_directory",
        None,
        "/workspace/project",
    ) in fixes
    assert tracking_calls == [
        (
            "bash:[('command', 'echo hi'), ('timeout', 60), "
            "('working_directory', '/workspace/project')]",
            True,
        )
    ]
    assert len(state_updates) == 1


def test_validation_failed_status_when_fixing_raises(monkeypatch):
    execution_attempts = []

    class EchoTool(BaseTool):
        def __init__(self):
            super().__init__("echo", "Echo test tool")

        def execute(self, command: str) -> ToolResult:
            execution_attempts.append(command)
            return ToolResult(success=True, output=command)

    orchestrator, events, tracking_calls, state_updates = _orchestrator(
        tools={"echo": EchoTool()}
    )

    def raise_validation(tool_name, params, parameter_fixes=None):
        raise RuntimeError("schema broke")

    monkeypatch.setattr(
        orchestrator,
        "_validate_and_fix_parameters",
        raise_validation,
        raising=False,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "run"}))

    assert execution.status == "validation_failed"
    assert execution.result.success is False
    assert execution.result.error_code == "PARAMETER_VALIDATION_FAILED"
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert execution.validated_params is None
    assert execution_attempts == []
    assert tracking_calls == []
    assert state_updates == []
    assert [event.event_type for event in events] == ["tool_start", "tool_error"]


def test_react_engine_no_longer_exposes_parameter_wrapper():
    engine = ReActEngine.__new__(ReActEngine)

    with pytest.raises(AttributeError):
        getattr(engine, "_validate_and_fix_parameters")
