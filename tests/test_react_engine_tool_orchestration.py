from sag.agent.react_engine import ReActEngine, ReActStep, StepType
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.tool_orchestration import ToolCall, ToolExecution, ToolLifecycleEvent
from sag.config.prompt_loader import load_react_engine_prompts
from sag.evidence import EvidenceAssessment, EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import EventType


class ContextWithForceNextTask:
    def __init__(self):
        self.force_next_task_calls = 0

    def force_next_task(self):
        self.force_next_task_calls += 1


class ContextWithoutForceNextTask:
    current_task_id = None


class RecordingBranchContext:
    current_task_id = "phase_build"

    def __init__(self):
        self.entries = []

    def add_to_branch_history(self, task_id, entry):
        self.entries.append((task_id, entry))
        return {"success": True}


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
        return ToolResult.completed_success(output=f"ran {command}")


def _engine_with_context(context=None):
    if context is None:
        context = ContextWithoutForceNextTask()

    engine = object.__new__(ReActEngine)
    engine.tools = {"bash": object()}
    engine.context_manager = context
    engine.prompts = load_react_engine_prompts()
    engine.prompt_builder = ReActPromptBuilder(
        prompts=engine.prompts,
        context_manager=engine.context_manager,
        tools=engine.tools,
    )
    engine.recent_tool_executions = []
    engine.max_recent_executions = 10
    engine.successful_states = {"working_directory": None}
    engine.repository_url = "https://example.test/repo.git"
    engine.current_iteration = 7
    engine.steps = []
    engine.config = FakeConfig()
    engine.agent_logger = FakeAgentLogger()
    engine.token_tracker = FakeTokenTracker()
    engine.repository_ref = "rel/commons-cli-1.11.0"
    engine.output_storage = None
    engine.emit = lambda *args, **kwargs: None
    engine._force_thinking_next = False
    engine._force_thinking_after_success = False
    engine.prompt_builder._cached_trunk_context = "cached"
    engine.prompt_builder._trunk_context_cache_timestamp = 123
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


def test_react_engine_tracks_pending_with_canonical_lifecycle_record():
    engine = _engine_with_context()
    result = ToolResult(
        invocation_status=InvocationStatus.PENDING,
        operation_outcome=OperationOutcome.UNKNOWN,
        evidence_status=EvidenceStatus.UNKNOWN,
        poll_ref="job:pending-1",
        output="still running",
    )

    engine._track_tool_execution("build:[('action', 'test')]", result)

    record = engine.recent_tool_executions[0]
    assert record.invocation_status is InvocationStatus.PENDING
    assert record.operation_outcome is OperationOutcome.UNKNOWN
    assert not hasattr(record, "success")


def test_get_tool_orchestrator_wires_engine_dependencies():
    engine = _engine_with_context(context=ContextWithForceNextTask())

    orchestrator = engine._get_tool_orchestrator()

    assert orchestrator.tools is engine.tools
    assert orchestrator.context_manager is engine.context_manager
    assert orchestrator.recent_tool_executions is engine.recent_tool_executions
    assert orchestrator.successful_states is engine.successful_states
    assert orchestrator.repository_url == "https://example.test/repo.git"
    assert orchestrator.repository_ref == "rel/commons-cli-1.11.0"
    assert orchestrator.track_tool_execution.__self__ is engine
    assert orchestrator.track_tool_execution.__func__ is ReActEngine._track_tool_execution
    assert orchestrator.update_successful_states.__self__ is engine
    assert orchestrator.update_successful_states.__func__ is ReActEngine._update_successful_states
    assert orchestrator.add_system_guidance.__self__ is engine
    assert orchestrator.add_system_guidance.__func__ is ReActEngine._add_system_guidance
    assert orchestrator.get_timestamp.__self__ is engine
    assert orchestrator.get_timestamp.__func__ is ReActEngine._get_timestamp
    assert orchestrator.event_sink.__self__ is engine
    assert orchestrator.event_sink.__func__ is ReActEngine._handle_tool_lifecycle_event


def test_react_engine_set_repository_url_accepts_ref():
    engine = _engine_with_context()

    engine.set_repository_url(
        "https://example.test/other.git",
        repository_ref="ae44dcd",
    )

    assert engine.repository_url == "https://example.test/other.git"
    assert engine.repository_ref == "ae44dcd"


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


def test_react_engine_maps_tool_lifecycle_events_to_ui_events():
    engine = _engine_with_context()
    emitted = []
    engine.emit_event = lambda event: emitted.append(event)

    call = ToolCall(name="maven", raw_params={"goal": "compile"})
    engine._handle_tool_lifecycle_event(
        ToolLifecycleEvent(
            event_type="tool_start",
            call=call,
            message="Starting maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )

    assert emitted[0].event_type == EventType.TOOL_START


def test_react_engine_preserves_real_tool_result_lifecycle_metadata():
    engine = _engine_with_context()
    emitted = []
    engine.emit_event = lambda event: emitted.append(event)

    call = ToolCall(
        name="maven",
        raw_params={"goal": "compile"},
        validated_params={"goal": "compile", "working_directory": "/workspace/app"},
    )
    engine._handle_tool_lifecycle_event(
        ToolLifecycleEvent(
            event_type="tool_result",
            call=call,
            message="maven compile completed",
            level="success",
            metadata={
                "status": "success",
                "duration_ms": 125.0,
                "result_succeeded": True,
                "error_code": None,
                "executed_params": {
                    "goal": "compile",
                    "working_directory": "/workspace/app",
                },
                "recovery_applied": False,
                "execution_signature": "maven:[('goal', 'compile')]",
            },
        )
    )

    emitted_event = emitted[0]
    metadata = emitted_event.metadata
    assert emitted_event.event_type == EventType.TOOL_RESULT
    assert metadata["tool_name"] == "maven"
    assert metadata["tool_params"]["goal"] == "compile"
    assert metadata["executed_params"]["working_directory"] == "/workspace/app"


def test_react_engine_lifecycle_metadata_preserves_reserved_ui_event_keys():
    engine = _engine_with_context()
    emitted = []
    engine.emit_event = lambda event: emitted.append(event)

    call = ToolCall(name="maven", raw_params={"goal": "compile"})
    engine._handle_tool_lifecycle_event(
        ToolLifecycleEvent(
            event_type="tool_result",
            call=call,
            message="outer message",
            level="error",
            metadata={
                "message": "inner message",
                "level": "inner-level",
                "phase": "inner-phase",
                "details": "inner details",
            },
        )
    )

    emitted_event = emitted[0]
    assert emitted_event.event_type == EventType.TOOL_RESULT
    assert emitted_event.message == "outer message"
    assert emitted_event.level == "error"
    assert emitted_event.metadata["message"] == "inner message"
    assert emitted_event.metadata["level"] == "inner-level"
    assert emitted_event.metadata["phase"] == "inner-phase"
    assert emitted_event.metadata["details"] == "inner details"
    assert emitted_event.metadata["tool_name"] == "maven"
    assert emitted_event.metadata["tool_params"] == {"goal": "compile"}
    assert emitted_event.metadata["tool_message"] == "outer message"


def test_react_engine_tool_event_adapter_emits_typed_lifecycle_ui_events():
    engine = _engine_with_context()
    emitted = []
    engine.emit_event = lambda event: emitted.append(event)

    result_event = ToolLifecycleEvent(
        event_type="tool_result",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo finished",
        metadata={"status": "success", "result_succeeded": True},
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
    fixed_event = ToolLifecycleEvent(
        event_type="tool_parameters_fixed",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo params normalized",
        level="warning",
        metadata={"field": "working_directory"},
    )

    engine._handle_tool_lifecycle_event(result_event)
    engine._handle_tool_lifecycle_event(recovery_event)
    engine._handle_tool_lifecycle_event(error_event)
    engine._handle_tool_lifecycle_event(fixed_event)

    assert len(emitted) == 4
    assert emitted[0].event_type == EventType.TOOL_RESULT
    assert emitted[0].message == "echo finished"
    assert emitted[0].level == "info"
    assert emitted[0].metadata["status"] == "success"
    assert emitted[0].metadata["tool_name"] == "echo"
    assert emitted[0].metadata["tool_params"] == {"command": "pwd"}
    assert emitted[0].metadata["tool_message"] == "echo finished"
    assert emitted[1].event_type == EventType.TOOL_RECOVERY
    assert emitted[1].message == "echo recovered"
    assert emitted[1].level == "warning"
    assert emitted[1].metadata["recovery_strategy"] == "retry"
    assert emitted[1].metadata["tool_name"] == "echo"
    assert emitted[1].metadata["tool_params"] == {"command": "pwd"}
    assert emitted[1].metadata["tool_message"] == "echo recovered"
    assert emitted[2].event_type == EventType.TOOL_ERROR
    assert emitted[2].message == "echo failed"
    assert emitted[2].level == "info"
    assert emitted[2].metadata["error_code"] == "FAIL"
    assert emitted[2].metadata["tool_name"] == "echo"
    assert emitted[2].metadata["tool_params"] == {"command": "pwd"}
    assert emitted[2].metadata["tool_message"] == "echo failed"
    assert emitted[3].event_type == EventType.TOOL_PARAMETERS_FIXED
    assert emitted[3].message == "echo params normalized"
    assert emitted[3].level == "warning"
    assert emitted[3].metadata["field"] == "working_directory"
    assert emitted[3].metadata["tool_name"] == "echo"
    assert emitted[3].metadata["tool_params"] == {"command": "pwd"}
    assert emitted[3].metadata["tool_message"] == "echo params normalized"


def test_execute_steps_delegates_action_to_orchestrator_after_migration(monkeypatch):
    result = ToolResult.completed_success(output="ok")
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


def test_execute_steps_records_action_trace_for_phase_context(monkeypatch):
    context = RecordingBranchContext()
    result = ToolResult.completed_success(output="Full output ref: output_build")
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: build",
        tool_name="build",
        tool_params={"action": "compile"},
        timestamp="ts",
        model_used="model",
    )
    execution = ToolExecution(
        call=ToolCall(name="build", raw_params={"action": "compile"}),
        result=result,
        status="success",
        raw_params={"action": "compile"},
        validated_params={"action": "compile"},
        executed_params={"action": "compile"},
        observation_text="build succeeded",
        attempted_execution=True,
    )
    engine = _engine_with_context(context=context)
    engine.tools = {}
    engine.current_iteration = 12

    class FakeOrchestrator:
        def execute(self, call):
            return execution

    monkeypatch.setattr(engine, "_get_tool_orchestrator", lambda: FakeOrchestrator())

    assert engine._execute_steps([step]) is True

    assert context.entries[0][0] == "phase_build"
    entry = context.entries[0][1]
    assert entry["type"] == "action"
    assert entry["iteration"] == 12
    assert entry["tool_name"] == "build"
    assert entry["parameters"] == {"action": "compile"}
    assert entry["observation"] == "build succeeded"
    assert entry["output_refs"] == ["output_build"]


def test_execute_steps_records_action_even_if_tool_clears_current_task(monkeypatch):
    context = RecordingBranchContext()
    result = ToolResult.completed_success(output="Final setup report generated.")
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: report",
        tool_name="report",
        tool_params={"summary": "done"},
        timestamp="ts",
        model_used="model",
    )
    execution = ToolExecution(
        call=ToolCall(name="report", raw_params={"summary": "done"}),
        result=result,
        status="success",
        raw_params={"summary": "done"},
        validated_params={"summary": "done"},
        executed_params={"summary": "done"},
        observation_text="report generated",
        attempted_execution=True,
    )
    engine = _engine_with_context(context=context)
    engine.tools = {}
    engine.current_iteration = 35

    class FakeOrchestrator:
        def execute(self, call):
            context.current_task_id = None
            return execution

    monkeypatch.setattr(engine, "_get_tool_orchestrator", lambda: FakeOrchestrator())

    assert engine._execute_steps([step]) is True

    assert context.entries[0][0] == "phase_build"
    entry = context.entries[0][1]
    assert entry["type"] == "action"
    assert entry["iteration"] == 35
    assert entry["tool_name"] == "report"
    assert entry["output"] == "Final setup report generated."
    assert entry["observation"] == "report generated"


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

    observation_events = [event for event in emitted if event[0][0] == EventType.AGENT_OBSERVATION]
    assert len(observation_events) == 1
    assert "echo executed successfully" in observation_events[0][1]["message"]


def test_execute_steps_forces_thinking_after_partial_assessment_without_success(monkeypatch):
    result = ToolResult.completed_failure(
        evidence_assessment=EvidenceAssessment.PARTIAL, output="needs review"
    )
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
        status="recovery_attempted",
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
    assert engine._force_thinking_after_success is True


def test_execute_steps_forces_thinking_after_string_partial_assessment(monkeypatch):
    result = ToolResult.completed_failure(evidence_assessment="partial", output="needs review")
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
        status="recovery_attempted",
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
    assert engine._force_thinking_after_success is True


def test_apply_tool_execution_loop_effects_applies_metadata_side_effects():
    context = ContextWithForceNextTask()
    engine = _engine_with_context(context=context)
    execution = ToolExecution(
        call=ToolCall(name="manage_context", raw_params={"action": "complete_task"}),
        result=ToolResult.completed_failure(output="loop broken"),
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
    assert engine.prompt_builder._cached_trunk_context is None
    assert engine.prompt_builder._trunk_context_cache_timestamp is None
    assert context.force_next_task_calls == 1


def test_apply_tool_execution_loop_effects_skips_unavailable_force_next_task():
    engine = _engine_with_context(context=ContextWithoutForceNextTask())
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        result=ToolResult.completed_failure(output="loop broken"),
        status="repetition_blocked",
        raw_params={"command": "pwd"},
        metadata={"force_next_task": True},
    )

    engine._apply_tool_execution_loop_effects(execution)

    assert engine._force_thinking_next is False
