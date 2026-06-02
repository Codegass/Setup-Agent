from sag.agent.react_engine import ReActEngine, ReActStep, StepType
from sag.agent.tool_orchestration import ToolCall, ToolExecution, ToolLifecycleEvent
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import EventType


class ContextWithForceNextTask:
    def __init__(self):
        self.force_next_task_calls = 0

    def force_next_task(self):
        self.force_next_task_calls += 1


class ContextWithoutForceNextTask:
    current_task_id = None


class FakeAgentLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class FakeConfig:
    verbose = False


class FakeTokenTracker:
    def __init__(self):
        self.tool_names = []

    def update_last_tool_name(self, tool_name):
        self.tool_names.append(tool_name)


class EchoTool(BaseTool):
    def __init__(self):
        super().__init__("echo", "Echo test tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=f"ran {command}")


def _engine_with_context(context=None):
    if context is None:
        context = ContextWithoutForceNextTask()

    engine = object.__new__(ReActEngine)
    engine.tools = {"bash": object()}
    engine.context_manager = context
    engine.recent_tool_executions = []
    engine.successful_states = {"working_directory": None}
    engine.repository_url = "https://example.test/repo.git"
    engine.current_iteration = 7
    engine.steps = []
    engine.config = FakeConfig()
    engine.agent_logger = FakeAgentLogger()
    engine.token_tracker = FakeTokenTracker()
    engine.output_storage = None
    engine.emit = lambda *args, **kwargs: None
    engine._force_thinking_next = False
    engine._force_thinking_after_success = False
    engine._cached_trunk_context = "cached"
    engine._trunk_context_cache_timestamp = 123
    return engine


def test_build_tool_call_from_step_preserves_action_metadata():
    engine = _engine_with_context()
    params = {"command": "pwd", "working_directory": "/workspace"}
    step = ReActStep(
        step_type=StepType.ACTION,
        content='ACTION: bash\nPARAMETERS: {"command": "pwd"}',
        tool_name="bash",
        tool_params=params,
        timestamp="2026-06-02 12:00:00",
        model_used="action-model",
    )

    call = engine._build_tool_call_from_step(step)

    assert isinstance(call, ToolCall)
    assert call.name == "bash"
    assert call.raw_params == params
    assert call.raw_action_text == step.content
    assert call.source_step_index == 7
    assert call.model_used == "action-model"
    assert call.validated_params is None


def test_get_tool_orchestrator_wires_engine_dependencies():
    engine = _engine_with_context(context=ContextWithForceNextTask())

    orchestrator = engine._get_tool_orchestrator()

    assert orchestrator.tools is engine.tools
    assert orchestrator.context_manager is engine.context_manager
    assert orchestrator.recent_tool_executions is engine.recent_tool_executions
    assert orchestrator.successful_states is engine.successful_states
    assert orchestrator.repository_url == "https://example.test/repo.git"
    assert orchestrator.track_tool_execution.__self__ is engine
    assert orchestrator.track_tool_execution.__func__ is ReActEngine._track_tool_execution
    assert orchestrator.update_successful_states.__self__ is engine
    assert (
        orchestrator.update_successful_states.__func__
        is ReActEngine._update_successful_states
    )
    assert orchestrator.add_system_guidance.__self__ is engine
    assert orchestrator.add_system_guidance.__func__ is ReActEngine._add_system_guidance
    assert orchestrator.get_timestamp.__self__ is engine
    assert orchestrator.get_timestamp.__func__ is ReActEngine._get_timestamp
    assert orchestrator.event_sink.__self__ is engine
    assert orchestrator.event_sink.__func__ is ReActEngine._handle_tool_lifecycle_event


def test_add_system_guidance_accepts_string_priority():
    engine = _engine_with_context()
    engine.steps = []
    engine.agent_logger = FakeAgentLogger()

    engine._add_system_guidance("Use Maven retry guidance", priority="high")

    assert len(engine.steps) == 1
    step = engine.steps[0]
    assert step.step_type == StepType.SYSTEM_GUIDANCE
    assert "IMPORTANT GUIDANCE" in step.content
    assert "(Priority: 8)" in step.content
    assert "Use Maven retry guidance" in step.content
    assert engine.agent_logger.messages


def test_format_tool_result_delegates_to_orchestrator_formatter(monkeypatch):
    engine = _engine_with_context()
    result = ToolResult(success=True, output="ok")

    def fake_formatter(tool_name, tool_result):
        assert tool_name == "bash"
        assert tool_result is result
        return "delegated observation"

    monkeypatch.setattr(
        "sag.agent.react_engine.format_orchestrated_tool_result",
        fake_formatter,
    )

    assert engine._format_tool_result("bash", result) == "delegated observation"


def test_handle_tool_lifecycle_event_is_temporary_no_op():
    engine = _engine_with_context()
    event = ToolLifecycleEvent(
        event_type="tool_start",
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        message="Starting bash",
    )

    assert engine._handle_tool_lifecycle_event(event) is None


def test_react_engine_tool_event_adapter_emits_existing_ui_events():
    engine = _engine_with_context()
    emitted = []
    engine.emit = lambda *args, **kwargs: emitted.append((args, kwargs))

    result_event = ToolLifecycleEvent(
        event_type="tool_result",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo finished",
        metadata={"status": "success", "result_success": True},
    )
    recovery_event = ToolLifecycleEvent(
        event_type="tool_recovery",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo recovered",
        level="warning",
        metadata={"recovery_strategy": "retry"},
    )
    error_event = ToolLifecycleEvent(
        event_type="tool_error",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo failed",
        metadata={"error_code": "FAIL"},
    )

    engine._handle_tool_lifecycle_event(result_event)
    engine._handle_tool_lifecycle_event(recovery_event)
    engine._handle_tool_lifecycle_event(error_event)

    assert len(emitted) == 2
    assert emitted[0][0][0] == EventType.WARNING
    assert emitted[0][1]["message"] == "echo recovered"
    assert emitted[0][1]["level"] == "warning"
    assert emitted[0][1]["recovery_strategy"] == "retry"
    assert emitted[1][0][0] == EventType.ERROR
    assert emitted[1][1]["message"] == "echo failed"
    assert emitted[1][1]["level"] == "error"
    assert emitted[1][1]["error_code"] == "FAIL"


def test_execute_steps_delegates_action_to_orchestrator_after_migration(monkeypatch):
    result = ToolResult(success=True, output="ok")
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: example",
        tool_name="example",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="model",
    )
    execution = ToolExecution(
        call=ToolCall(name="example", raw_params={"command": "pwd"}),
        result=result,
        status="success",
        raw_params={"command": "pwd"},
        validated_params={"command": "pwd"},
        executed_params={"command": "pwd"},
        observation_text="formatted observation",
        attempted_execution=True,
    )
    engine = _engine_with_context()
    engine.tools = {}

    class FakeOrchestrator:
        def execute(self, call):
            engine.seen_call = call
            return execution

    monkeypatch.setattr(engine, "_get_tool_orchestrator", lambda: FakeOrchestrator())

    assert engine._execute_steps([step]) is True
    assert engine.seen_call.name == "example"
    assert engine.seen_call.raw_params == {"command": "pwd"}
    assert step.tool_result is result
    assert any(
        s.step_type == StepType.OBSERVATION and s.content == "formatted observation"
        for s in engine.steps
    )
    assert engine._force_thinking_after_success is True


def test_execute_steps_emits_single_observation_ui_event_with_real_orchestrator():
    engine = _engine_with_context()
    engine.tools = {"echo": EchoTool()}
    emitted = []
    engine.emit = lambda *args, **kwargs: emitted.append((args, kwargs))
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: echo",
        tool_name="echo",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="model",
    )

    assert engine._execute_steps([step]) is True

    observation_events = [
        event for event in emitted if event[0][0] == EventType.AGENT_OBSERVATION
    ]
    assert len(observation_events) == 1
    assert "echo executed successfully" in observation_events[0][1]["message"]


def test_apply_tool_execution_loop_effects_applies_metadata_side_effects():
    context = ContextWithForceNextTask()
    engine = _engine_with_context(context=context)
    execution = ToolExecution(
        call=ToolCall(name="manage_context", raw_params={"action": "complete_task"}),
        result=ToolResult(success=False, output="loop broken"),
        status="repetition_blocked",
        raw_params={"action": "complete_task"},
        metadata={
            "force_thinking_next": True,
            "invalidate_trunk_cache": True,
            "force_next_task": True,
        },
    )

    engine._apply_tool_execution_loop_effects(execution)

    assert engine._force_thinking_next is True
    assert engine._cached_trunk_context is None
    assert engine._trunk_context_cache_timestamp is None
    assert context.force_next_task_calls == 1


def test_apply_tool_execution_loop_effects_skips_unavailable_force_next_task():
    engine = _engine_with_context(context=ContextWithoutForceNextTask())
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        result=ToolResult(success=False, output="loop broken"),
        status="repetition_blocked",
        raw_params={"command": "pwd"},
        metadata={"force_next_task": True},
    )

    engine._apply_tool_execution_loop_effects(execution)

    assert engine._force_thinking_next is False
