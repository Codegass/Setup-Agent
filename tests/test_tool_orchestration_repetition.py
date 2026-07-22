from sag.agent.tool_orchestration import ToolCall, ToolExecutionRecord, ToolOrchestrator
from sag.evidence import InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolResult


class CountingTool(BaseTool):
    def __init__(self, name="echo", output="ran command"):
        super().__init__(name, "Counting test tool")
        self.calls = 0
        self.output = output

    def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
    ) -> ToolResult:
        del working_directory
        self.calls += 1
        return ToolResult.completed_success(output=self.output, metadata={"command": command})


class ContextWithForceNextTask:
    def __init__(self):
        self.force_next_task_calls = 0

    def force_next_task(self):
        self.force_next_task_calls += 1


class ContextWithCurrentContext:
    def get_current_context(self):
        return "Project requires Java 17"


class SystemTool(BaseTool):
    def __init__(self):
        super().__init__("system", "Fake system test tool")
        self.calls = []

    def execute(self, action: str, java_version: str = "") -> ToolResult:
        self.calls.append({"action": action, "java_version": java_version})
        if action == "verify_java":
            return ToolResult.completed_success(output="Java needs configuration")
        if action == "install_java":
            return ToolResult.completed_success(output=f"Installed Java {java_version}")
        return ToolResult.completed_failure(output="", error=f"Unsupported action: {action}")


def _signature(tool_name, command, **extra_params):
    params = {"command": command, **extra_params}
    return f"{tool_name}:{str(sorted(params.items()))}"


def _bash_signature(command):
    return _signature("bash", command, working_directory="/workspace")


def _recent_executions(signature, count, *, success=False):
    return [
        ToolExecutionRecord(
            signature=signature,
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=(OperationOutcome.SUCCESS if success else OperationOutcome.FAILED),
            timestamp=f"ts-{index}",
        )
        for index in range(count)
    ]


def _orchestrator(
    *,
    tools,
    recent_tool_executions,
    context_manager=None,
    tracking_calls=None,
    events=None,
):
    if tracking_calls is None:
        tracking_calls = []

    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=recent_tool_executions,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append if events is not None else None,
    )


def test_java_repetition_history_emits_only_the_real_tool_result():
    bash_tool = CountingTool(name="bash", output="real java command output")
    system_tool = SystemTool()
    signature = _bash_signature("update-alternatives --config java")
    events = []
    orchestrator = ToolOrchestrator(
        tools={"bash": bash_tool, "system": system_tool},
        context_manager=ContextWithCurrentContext(),
        recent_tool_executions=_recent_executions(signature, 5),
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(
        ToolCall(name="bash", raw_params={"command": "update-alternatives --config java"})
    )

    result_event = events[-1]
    assert result_event.event_type == "tool_result"
    assert not any(event.event_type == "tool_recovery" for event in events)
    assert result_event.metadata["status"] == execution.status
    assert result_event.metadata["duration_ms"] is not None
    assert result_event.metadata["recovery_applied"] is False
    assert bash_tool.calls == 1
    assert system_tool.calls == []


def test_three_prior_exact_calls_do_not_inject_warning_output():
    tool = CountingTool(output="real output")
    signature = _signature("echo", "pwd")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"echo": tool},
        recent_tool_executions=_recent_executions(signature, 3),
        tracking_calls=tracking_calls,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.attempted_execution is True
    assert tool.calls == 1
    assert execution.result.output == "real output"
    assert "repetition_level" not in execution.metadata
    assert tracking_calls == [(signature, execution.result)]


def test_four_prior_exact_calls_do_not_force_thinking_in_orchestrator():
    tool = CountingTool(output="guided output")
    signature = _signature("echo", "pwd")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"echo": tool},
        recent_tool_executions=_recent_executions(signature, 4),
        tracking_calls=tracking_calls,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.attempted_execution is True
    assert tool.calls == 1
    assert execution.result.output == "guided output"
    assert "repetition_level" not in execution.metadata
    assert "force_thinking_next" not in execution.metadata
    assert tracking_calls == [(signature, execution.result)]


def test_five_prior_exact_calls_still_execute_without_forcing_next_task():
    tool = CountingTool(output="real output")
    context = ContextWithForceNextTask()
    signature = _signature("echo", "pwd")
    tracking_calls = []
    events = []
    orchestrator = _orchestrator(
        tools={"echo": tool},
        context_manager=context,
        recent_tool_executions=_recent_executions(signature, 5),
        tracking_calls=tracking_calls,
        events=events,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.succeeded is True
    assert execution.attempted_execution is True
    assert execution.executed_params == {"command": "pwd"}
    assert tool.calls == 1
    assert "repetition_level" not in execution.metadata
    assert "force_next_task" not in execution.metadata
    assert context.force_next_task_calls == 0
    assert tracking_calls == [(signature, execution.result)]
    assert events[-1].event_type == "tool_result"


def test_java_repetition_no_longer_triggers_direct_auto_fix():
    bash_tool = CountingTool(name="bash", output="real output")
    system_tool = SystemTool()
    signature = _bash_signature("update-alternatives --config java")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"bash": bash_tool, "system": system_tool},
        context_manager=ContextWithCurrentContext(),
        recent_tool_executions=_recent_executions(signature, 5),
        tracking_calls=tracking_calls,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="bash",
            raw_params={"command": "update-alternatives --config java"},
        )
    )

    assert execution.status == "success"
    assert execution.result.succeeded is True
    assert execution.result.output == "real output"
    assert execution.recovery_strategy is None
    assert execution.attempted_execution is True
    assert bash_tool.calls == 1
    assert system_tool.calls == []
    assert "repetition_level" not in execution.metadata
    assert tracking_calls == [(signature, execution.result)]


def test_generic_java_path_repetition_executes_without_java_auto_fix():
    tool = CountingTool(name="bash", output="real output")
    signature = _bash_signature("ls src/main/java")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"bash": tool},
        recent_tool_executions=_recent_executions(signature, 5),
        tracking_calls=tracking_calls,
    )
    execution = orchestrator.execute(
        ToolCall(name="bash", raw_params={"command": "ls src/main/java"})
    )

    assert execution.status == "success"
    assert "force_next_task" not in execution.metadata
    assert execution.attempted_execution is True
    assert tool.calls == 1
    assert tracking_calls == [(signature, execution.result)]
