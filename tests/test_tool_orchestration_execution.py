from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolError, ToolResult


class EchoTool(BaseTool):
    def __init__(self, name="echo"):
        super().__init__(name, "Echo test tool")

    def execute(self, command: str, working_directory: str = "/workspace") -> ToolResult:
        return ToolResult(success=True, output=f"ran {command}", metadata={"command": command})


class ManageContextTool(BaseTool):
    def __init__(self, *, success=True):
        super().__init__("manage_context", "Manage context test tool")
        self.success = success

    def execute(self, action: str, summary: str = "") -> ToolResult:
        return ToolResult(success=self.success, output=f"{action} result")


def test_orchestrator_executes_successful_tool_and_emits_events():
    events = []
    tracking_calls = []
    state_updates = []

    def update_successful_states(tool_name, params, result):
        state_updates.append((tool_name, params, result))

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=update_successful_states,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"
    assert execution.attempted_execution is True
    assert execution.executed_params == {"command": "pwd"}
    assert "echo executed successfully" in execution.observation_text
    assert [event.event_type for event in events] == ["tool_start", "tool_result"]
    assert events[-1].metadata["status"] == "success"
    assert events[-1].metadata["result_success"] is True
    assert events[-1].metadata["error_code"] is None
    assert events[-1].metadata["executed_params"] == {"command": "pwd"}
    assert events[-1].metadata["recovery_applied"] is False
    assert execution.call.execution_signature == "echo:[('command', 'pwd')]"
    assert tracking_calls == [(execution.call.execution_signature, True)]
    assert len(state_updates) == 1
    assert state_updates[0][0] == "echo"
    assert state_updates[0][1] == {"command": "pwd"}
    assert state_updates[0][2] is execution.result


def test_lifecycle_events_include_required_metadata():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"bash": EchoTool("bash")},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(
        ToolCall(name="bash", raw_params={"cmd": "pwd"}, source_step_index=4)
    )

    start = events[0]
    parameters_fixed = next(
        event for event in events if event.event_type == "tool_parameters_fixed"
    )
    result = events[-1]

    assert start.metadata["tool_name"] == "bash"
    assert start.metadata["source_step_index"] == 4
    assert start.metadata["raw_params"] == {"cmd": "pwd"}
    assert "execution_signature" in start.metadata
    assert parameters_fixed.metadata["raw_params"] == {"cmd": "pwd"}
    assert parameters_fixed.metadata["validated_params"] == {
        "command": "pwd",
        "working_directory": "/workspace",
    }
    assert parameters_fixed.metadata["parameter_fixes"]
    assert parameters_fixed.metadata["params_changed"] is True
    assert result.metadata["status"] == execution.status
    assert result.metadata["duration_ms"] is not None
    assert result.metadata["result_success"] is True
    assert result.metadata["error_code"] is None
    assert result.metadata["executed_params"] == {
        "command": "pwd",
        "working_directory": "/workspace",
    }
    assert result.metadata["recovery_applied"] is False


def test_orchestrator_returns_missing_tool_execution_with_existing_feedback():
    events = []
    tracking_calls = []
    state_updates = []
    orchestrator = ToolOrchestrator(
        tools={"bash": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="ls", raw_params={"path": "/workspace"}))

    assert execution.status == "missing_tool"
    assert execution.result.success is False
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert "Tool 'ls' does not exist" in execution.result.output
    assert "Did you mean: bash" in execution.result.output
    assert events[-1].event_type == "tool_error"
    assert tracking_calls == []
    assert state_updates == []


def test_empty_validated_params_are_used_instead_of_raw_params():
    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(
        ToolCall(name="echo", raw_params={"command": "raw"}, validated_params={})
    )

    assert execution.status == "failure"
    assert execution.executed_params == {}
    assert execution.result.success is False
    assert execution.result.error_code == "MISSING_PARAMETERS"


def test_tool_error_metadata_and_suggestions_are_preserved():
    class ErrorTool(BaseTool):
        def __init__(self):
            super().__init__("error_tool", "Error tool")

        def execute(self, command: str) -> ToolResult:
            raise ToolError(
                "bad input",
                category="validation",
                error_code="BAD_INPUT",
                suggestions=["try a better command"],
                details={"command": command},
                retryable=True,
            )

    tracking_calls = []
    state_updates = []
    orchestrator = ToolOrchestrator(
        tools={"error_tool": ErrorTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="error_tool", raw_params={"command": "bad"}))

    assert execution.status == "failure"
    assert execution.result.error_code == "BAD_INPUT"
    assert execution.result.suggestions == ["try a better command"]
    assert execution.result.metadata["failure_category"] == "validation"
    assert execution.result.metadata["retryable"] is True
    assert execution.call.execution_signature == "error_tool:[('command', 'bad')]"
    assert tracking_calls == [(execution.call.execution_signature, False)]
    assert state_updates == []


def test_manage_context_invalidation_metadata_only_for_successful_context_changes():
    def execute_manage_context(action, *, success=True):
        orchestrator = ToolOrchestrator(
            tools={"manage_context": ManageContextTool(success=success)},
            context_manager=None,
            recent_tool_executions=[],
            successful_states={},
            repository_url=None,
            track_tool_execution=lambda signature, success: None,
            update_successful_states=lambda tool_name, params, result: None,
            add_system_guidance=lambda message, priority=5: None,
            get_timestamp=lambda: "ts",
        )
        return orchestrator.execute(
            ToolCall(name="manage_context", raw_params={"action": action})
        )

    changing_execution = execute_manage_context("complete_task")
    info_execution = execute_manage_context("get_info")
    failed_changing_execution = execute_manage_context("complete_task", success=False)

    assert changing_execution.status == "success"
    assert changing_execution.metadata.get("invalidate_trunk_cache") is True
    assert "invalidate_trunk_cache" not in info_execution.metadata
    assert "invalidate_trunk_cache" not in failed_changing_execution.metadata


def test_unexpected_safe_execute_exception_returns_exception_status():
    class ExplodingTool(BaseTool):
        def __init__(self):
            super().__init__("explode", "Exploding tool")

        def execute(self, command: str) -> ToolResult:
            return ToolResult(success=True, output="unused")

        def safe_execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("boom")

    tracking_calls = []
    orchestrator = ToolOrchestrator(
        tools={"explode": ExplodingTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="explode", raw_params={"command": "pwd"}))

    assert execution.status == "exception"
    assert execution.result.success is False
    assert execution.result.error_code == "TOOL_EXECUTION_EXCEPTION"
    assert execution.attempted_execution is True
    assert tracking_calls == [("explode:[('command', 'pwd')]", False)]


def test_event_sink_exception_does_not_abort_successful_execution():
    def event_sink(event):
        raise RuntimeError("event sink failed")

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=event_sink,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"


def test_successful_state_callback_exception_does_not_abort_successful_execution():
    def update_successful_states(tool_name, params, result):
        raise RuntimeError("state update failed")

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=update_successful_states,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"
