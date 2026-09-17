from types import SimpleNamespace

from engine_driver import execute_action_steps
from test_framework_survey import SurveyOrch

from sag.agent.output_storage import OutputStorageManager
from sag.agent.react_engine import ReActEngine, ReActStep, StepType
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.tool_orchestration import (
    ToolCall,
    ToolExecution,
    ToolOrchestrator,
)
from sag.config.prompt_loader import load_react_engine_prompts
from sag.evidence import EvidenceAssessment, EvidenceStatus, InvocationStatus, OperationOutcome
from sag.project_fact_sheet import with_project_fact_sheet_identity
from sag.tools.base import BaseTool, ToolResult
from sag.tools.context_tool import ContextTool
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool
from sag.tools.project_tool import ProjectTool


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

    def load_branch_history(self, task_id):
        return type(
            "BranchHistory",
            (),
            {
                "history": [
                    entry for entry_task_id, entry in self.entries if entry_task_id == task_id
                ]
            },
        )()


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
    assert orchestrator.event_sink is None


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

    assert execute_action_steps(engine, [step]) is None
    assert engine.seen_call.name == "example"
    assert engine.seen_call.raw_params == {"command": "pwd"}
    assert step.tool_result is result
    assert any(
        s.step_type == StepType.OBSERVATION and s.content == "formatted observation"
        for s in engine.steps
    )


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

    assert execute_action_steps(engine, [step]) is None

    assert context.entries[0][0] == "phase_build"
    entry = context.entries[0][1]
    assert entry["type"] == "action"
    assert entry["iteration"] == 12
    assert entry["tool_name"] == "build"
    assert entry["parameters"] == {"action": "compile"}
    assert entry["observation"] == "build succeeded"
    assert entry["output_refs"] == ["output_build"]


def test_project_fact_sheet_branch_history_is_bounded(monkeypatch, tmp_path):
    context = RecordingBranchContext()
    result = ToolResult.completed_success(
        output="{" + '"facts":"' + "x" * 2_000 + '"}',
        metadata=with_project_fact_sheet_identity(
            {
                "project_path": "/workspace/demo",
                "project_type": "Java",
                "build_system": "Maven",
            }
        ),
    )
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: project analyze",
        tool_name="project",
        tool_params={"action": "analyze"},
        timestamp="ts",
        model_used="model",
    )
    execution = ToolExecution(
        call=ToolCall(name="project", raw_params={"action": "analyze"}),
        result=result,
        status="success",
        raw_params={"action": "analyze"},
        validated_params={"action": "analyze"},
        executed_params={"action": "analyze"},
        observation_text="PROJECT ANALYSIS COMPLETED\n" + "y" * 20_000,
        attempted_execution=True,
    )
    engine = _engine_with_context(context=context)
    engine.tools = {}
    engine.output_storage = OutputStorageManager(tmp_path)

    monkeypatch.setattr(
        engine,
        "_get_tool_orchestrator",
        lambda: type("Orchestrator", (), {"execute": lambda self, call: execution})(),
    )

    assert execute_action_steps(engine, [step]) is None

    entry = context.entries[0][1]
    assert entry["metadata"]["fact_sheet_schema"] == "sag.project-facts"
    assert len(entry["output"]) <= 850
    assert len(entry["observation"]) <= 6_000
    assert "projection truncated in branch history" in entry["observation"]
    assert entry["output_refs"]


def test_real_project_facade_fact_receipt_reaches_context_completion(monkeypatch, tmp_path):
    """Analyzer -> public facade -> orchestration -> engine history/storage ->
    ContextTool uses production objects at every handoff."""

    class AnalysisContext(RecordingBranchContext):
        current_task_id = "phase_analyze"

        def __init__(self):
            super().__init__()
            self.trunk = SimpleNamespace(environment_summary={})
            self.saved = False
            self.output_storage = None

        def load_trunk_context(self):
            return self.trunk

        def _save_trunk_context(self, trunk):
            self.trunk = trunk
            self.saved = True

    context = AnalysisContext()
    storage = OutputStorageManager(tmp_path)
    context.output_storage = storage
    analyzer = ProjectAnalyzerTool(SurveyOrch(), context)
    project = ProjectTool(analyzer_tool=analyzer)
    engine = _engine_with_context(context=context)
    engine.tools = {"project": project}
    engine.output_storage = storage
    orchestration = ToolOrchestrator(
        tools=engine.tools,
        context_manager=context,
        recent_tool_executions=engine.recent_tool_executions,
        successful_states=engine.successful_states,
        repository_url=engine.repository_url,
        repository_ref=engine.repository_ref,
        track_tool_execution=lambda *_args, **_kwargs: None,
        update_successful_states=lambda *_args, **_kwargs: None,
        add_system_guidance=lambda *_args, **_kwargs: None,
        get_timestamp=lambda: "2026-07-26T22:00:00Z",
        output_storage=storage,
    )
    monkeypatch.setattr(engine, "_get_tool_orchestrator", lambda: orchestration)
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: project analyze",
        tool_name="project",
        tool_params={"action": "analyze", "project_path": "/workspace/proj"},
        timestamp="ts",
        model_used="weak-model",
    )

    assert execute_action_steps(engine, [step]) is None

    assert context.saved is True
    assert step.tool_result.metadata["fact_sheet_schema"] == "sag.project-facts"
    [entry] = [value for task_id, value in context.entries if task_id == "phase_analyze"]
    assert entry["parameters"]["action"] == "analyze"
    assert entry["metadata"]["fact_sheet_schema"] == "sag.project-facts"
    assert "PROJECT ANALYSIS COMPLETED" in entry["observation"]
    assert ContextTool(context)._check_project_analyzer_execution() is True
    completion = ContextTool(context)._validate_task_completion(
        SimpleNamespace(id="phase_analyze", description="Analyze project structure"),
        "Survey facts recorded.",
        "Structured fact receipt and trunk survey are present.",
    )
    assert completion["valid"] is True


def test_execute_steps_persists_short_failed_output_ref_in_branch_history(
    monkeypatch, durable_tool_result_storage
):
    context = RecordingBranchContext()
    result = ToolResult.completed_failure(
        output="compiler cannot find symbol Widget",
        error="build failed",
        error_code="BUILD_FAILED",
    )
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
        status="failure",
        raw_params={"action": "compile"},
        validated_params={"action": "compile"},
        executed_params={"action": "compile"},
        observation_text="build failed",
        attempted_execution=True,
    )
    engine = _engine_with_context(context=context)
    engine.tools = {}

    monkeypatch.setattr(
        engine,
        "_get_tool_orchestrator",
        lambda: type("Orchestrator", (), {"execute": lambda self, call: execution})(),
    )

    assert execute_action_steps(engine, [step]) is None

    entry = context.load_branch_history("phase_build").history[0]
    assert result.output_ref in entry["output_refs"]
    assert entry["failure_signature"] == result.failure_signature
    assert entry["error_tail_preview"] == result.error_tail_preview
    assert durable_tool_result_storage.retrieve_output(result.output_ref) == result.output


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

    assert execute_action_steps(engine, [step]) is None

    assert context.entries[0][0] == "phase_build"
    entry = context.entries[0][1]
    assert entry["type"] == "action"
    assert entry["iteration"] == 35
    assert entry["tool_name"] == "report"
    assert entry["output"] == "Final setup report generated."
    assert entry["observation"] == "report generated"


def test_execute_steps_records_one_observation_step_with_real_orchestrator():
    engine = _engine_with_context()
    engine.tools = {"echo": EchoTool()}
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: echo",
        tool_name="echo",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="model",
    )

    assert execute_action_steps(engine, [step]) is None

    observation_steps = [
        recorded for recorded in engine.steps if recorded.step_type is StepType.OBSERVATION
    ]
    assert len(observation_steps) == 1
    assert "echo executed successfully" in observation_steps[0].content


def test_apply_tool_execution_loop_effects_ignores_legacy_force_next_task():
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

    # Plan 2 Task 8: `force_thinking_next` and the flat-prompt trunk cache both
    # died with the dual-role protocol; the legacy force_next_task hook stays
    # unreachable.
    assert context.force_next_task_calls == 0


def test_apply_tool_execution_loop_effects_skips_unavailable_force_next_task():
    engine = _engine_with_context(context=ContextWithoutForceNextTask())
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        result=ToolResult.completed_failure(output="loop broken"),
        status="repetition_blocked",
        raw_params={"command": "pwd"},
        metadata={"force_next_task": True},
    )

    # Plan 2 Task 8: no dual-role flag survives; the legacy force_next_task
    # hook must stay unreachable and the call must not raise.
    assert engine._apply_tool_execution_loop_effects(execution) is None


# Plan 2 Task 8: old protocol removed
