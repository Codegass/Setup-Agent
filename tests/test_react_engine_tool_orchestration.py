from sag.agent.react_engine import ReActEngine, ReActStep, StepType
from sag.agent.tool_orchestration import ToolCall, ToolExecution, ToolLifecycleEvent
from sag.tools.base import ToolResult


class ContextWithForceNextTask:
    def __init__(self):
        self.force_next_task_calls = 0

    def force_next_task(self):
        self.force_next_task_calls += 1


class ContextWithoutForceNextTask:
    pass


class FakeAgentLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


def _engine_with_context(context=None):
    engine = object.__new__(ReActEngine)
    engine.tools = {"bash": object()}
    engine.context_manager = context
    engine.recent_tool_executions = []
    engine.successful_states = {"working_directory": None}
    engine.repository_url = "https://example.test/repo.git"
    engine.current_iteration = 7
    engine._force_thinking_next = False
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


def test_handle_tool_lifecycle_event_is_temporary_no_op():
    engine = _engine_with_context()
    event = ToolLifecycleEvent(
        event_type="tool_start",
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        message="Starting bash",
    )

    assert engine._handle_tool_lifecycle_event(event) is None


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
