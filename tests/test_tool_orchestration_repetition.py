from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class CountingTool(BaseTool):
    def __init__(self, name="echo", output="ran command"):
        super().__init__(name, "Counting test tool")
        self.calls = 0
        self.output = output

    def execute(self, command: str) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, output=self.output, metadata={"command": command})


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
            return ToolResult(success=True, output="Java needs configuration")
        if action == "install_java":
            return ToolResult(success=True, output=f"Installed Java {java_version}")
        return ToolResult(success=False, output="", error=f"Unsupported action: {action}")


def _signature(tool_name, command, **extra_params):
    params = {"command": command, **extra_params}
    return f"{tool_name}:{str(sorted(params.items()))}"


def _bash_signature(command):
    return _signature("bash", command, working_directory="/workspace")


def _recent_executions(signature, count, *, success=False):
    return [
        {"signature": signature, "success": success, "timestamp": f"ts-{index}"}
        for index in range(count)
    ]


def _orchestrator(*, tools, recent_tool_executions, context_manager=None, tracking_calls=None):
    if tracking_calls is None:
        tracking_calls = []

    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=recent_tool_executions,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracking_calls.append(
            (signature, success)
        ),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )


def test_repetition_level_one_executes_with_warning_output():
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
    assert execution.result.output.startswith("REPETITIVE EXECUTION WARNING")
    assert "real output" in execution.result.output
    assert execution.metadata["repetition_level"] == 1
    assert tracking_calls == [(signature, True)]


def test_repetition_level_two_executes_with_force_thinking_metadata():
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
    assert execution.result.output.startswith("REPETITIVE EXECUTION WARNING")
    assert "Consider alternative approaches" in execution.result.output
    assert "guided output" in execution.result.output
    assert execution.metadata["repetition_level"] == 2
    assert execution.metadata["force_thinking_next"] is True
    assert tracking_calls == [(signature, True)]


def test_repetition_level_three_breaks_without_execution_and_forces_next_task():
    tool = CountingTool(output="should not run")
    context = ContextWithForceNextTask()
    signature = _signature("echo", "pwd")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"echo": tool},
        context_manager=context,
        recent_tool_executions=_recent_executions(signature, 5),
        tracking_calls=tracking_calls,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "repetition_blocked"
    assert execution.result.success is False
    assert execution.result.error_code == "INFINITE_LOOP_BROKEN"
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert tool.calls == 0
    assert execution.metadata["repetition_level"] == 3
    assert execution.metadata["force_next_task"] is True
    assert context.force_next_task_calls == 0
    assert tracking_calls == [(signature, False)]


def test_java_repetition_triggers_auto_fix():
    bash_tool = CountingTool(name="bash", output="should not run")
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

    assert execution.status == "recovered"
    assert execution.result.success is True
    assert "Auto-fixed Java configuration" in execution.result.output
    assert execution.recovery_strategy == "java_configuration_auto_fix"
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert bash_tool.calls == 0
    assert system_tool.calls == [
        {"action": "verify_java", "java_version": ""},
        {"action": "install_java", "java_version": "17"},
    ]
    assert execution.metadata["repetition_level"] == 3
    assert execution.metadata["recovery_strategy"] == "java_configuration_auto_fix"
    assert tracking_calls == [(signature, True)]


def test_generic_java_path_repetition_breaks_without_java_auto_fix(monkeypatch):
    tool = CountingTool(name="bash", output="should not run")
    signature = _bash_signature("ls src/main/java")
    tracking_calls = []
    orchestrator = _orchestrator(
        tools={"bash": tool},
        recent_tool_executions=_recent_executions(signature, 5),
        tracking_calls=tracking_calls,
    )
    auto_fix_calls = []

    def auto_fix_java_configuration():
        auto_fix_calls.append(True)
        return ToolResult(success=True, output="unexpected auto-fix")

    monkeypatch.setattr(
        orchestrator,
        "_auto_fix_java_configuration",
        auto_fix_java_configuration,
    )

    execution = orchestrator.execute(
        ToolCall(name="bash", raw_params={"command": "ls src/main/java"})
    )

    assert execution.status == "repetition_blocked"
    assert execution.result.error_code == "INFINITE_LOOP_BROKEN"
    assert execution.metadata["force_next_task"] is True
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert tool.calls == 0
    assert auto_fix_calls == []
    assert tracking_calls == [(signature, False)]
