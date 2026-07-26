from types import SimpleNamespace

from test_verdict_finalizer import FakeVerdictOrchestrator

import sag.agent.react_engine as react_engine_module
import sag.tools.base as tool_base_module
from sag.agent.evidence_state import EvidenceRole, RunEvidenceState
from sag.agent.phase_machine import PhaseMachine, PhaseTermination
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.verdict_finalizer import RunTerminationStatus, VerdictFinalizer
from sag.evidence import OperationOutcome
from sag.tools.base import BaseTool, ToolResult


class _PromptBuilder:
    def build_initial_system_prompt(self, **kwargs):
        return "system prompt"


class _LLMClient:
    """Scripted native executor: one turn per iteration, or a raise."""

    def __init__(self, turn=None, error=None):
        self.turn = turn
        self.error = error

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=True, model="test-model")

    def get_native_turn(self, messages, **kwargs):
        if self.error is not None:
            raise self.error
        if self.turn is not None:
            return self.turn
        return NativeTurn(text="still thinking", tool_calls=(), model_used="test-model")


def _engine(*, turn=None, error=None, wall_clock_cap=0):
    engine = ReActEngine.__new__(ReActEngine)
    engine.max_iterations = 3
    engine.phase_machine = PhaseMachine()
    engine.run_evidence_state = RunEvidenceState(run_id="abort-wiring")
    engine.verdict_finalizer = VerdictFinalizer(FakeVerdictOrchestrator())
    engine._report_attempted = False
    engine._report_delivered = False
    engine._report_failed = False
    engine.config = SimpleNamespace(max_wall_clock_seconds=wall_clock_cap)
    engine.agent_logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    engine.prompt_builder = _PromptBuilder()
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _LLMClient(turn=turn, error=error)
    engine.token_tracker = SimpleNamespace(set_iteration=lambda iteration: None)
    engine._get_timestamp = lambda: "2026-07-26 00:00:00"
    engine.context_journal = None
    engine.steps_since_context_switch = 0
    engine._phase_intro_step = lambda: SimpleNamespace(content="phase intro")
    engine._start_phase_branch = lambda: None
    engine._enforce_phase_floors = lambda: False
    engine._export_token_usage_csv = lambda: None
    return engine


def _assert_setup_abort(engine, reason):
    assert len(engine.phase_machine.records) == 1
    record = engine.phase_machine.records[0]
    assert record.termination is PhaseTermination.ABORTED
    assert record.reason == reason
    assert engine.phase_machine.current_phase == "provision"
    assert engine.phase_machine.termination_state() == "aborted"
    assert any(f"ABORTED: {reason}" in line for line in engine.phase_machine.digest_lines())


def test_setup_wall_clock_exhaustion_records_abort_without_advancing(monkeypatch):
    engine = _engine(wall_clock_cap=1)
    clock = iter([100.0, 102.0, 102.0])
    monkeypatch.setattr(react_engine_module.time, "time", lambda: next(clock))

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.ABORTED
    _assert_setup_abort(engine, "wall clock cap exceeded")


def test_setup_provider_failure_records_abort_naming_the_cause():
    """`get_native_turn` raises instead of returning None, so the abort reason
    carries the provider error rather than a bare 'unavailable'."""
    engine = _engine(error=RuntimeError("LLM transport failed"))

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.ABORTED
    _assert_setup_abort(engine, "LLM response unavailable: LLM transport failed")


def test_setup_iteration_exhaustion_records_abort_without_advancing():
    engine = _engine()

    termination = engine.run_setup_loop("set up project", max_iterations=2)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.current_iteration == 2
    _assert_setup_abort(engine, "iteration budget exhausted")


def test_setup_engine_exception_records_abort_without_advancing():
    engine = _engine()

    def explode():
        raise RuntimeError("floor probe failed")

    engine._enforce_phase_floors = explode

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.ABORTED
    _assert_setup_abort(engine, "engine exception: RuntimeError")


def test_construction_persistence_failure_is_audited_before_setup_abort():
    class FailedConstructionTool(BaseTool):
        def __init__(self):
            super().__init__("build", "Failed construction tool")

        def execute(self, action: str) -> ToolResult:
            return ToolResult.completed_failure(
                output="compile failed before result return",
                error="compile failed",
                error_code="CONSTRUCTION_FAILED",
            )

    class TotalFailureStorage:
        def __init__(self):
            self.primary_calls = 0
            self.emergency_calls = 0

        def store_output(self, **kwargs):
            self.primary_calls += 1
            return ""

        def store_emergency_output(self, **kwargs):
            self.emergency_calls += 1
            return ""

        def retrieve_output(self, ref_id):
            return None

    storage = TotalFailureStorage()
    engine = _engine(
        turn=NativeTurn(
            text="compiling",
            tool_calls=(
                NativeToolCall(
                    id="call_build_1",
                    name="build",
                    arguments={"action": "compile"},
                    raw_arguments='{"action": "compile"}',
                ),
            ),
            model_used="test-model",
        )
    )
    engine.config = SimpleNamespace(max_wall_clock_seconds=0, verbose=False)
    engine.context_manager = SimpleNamespace(current_task_id=None)
    engine.control_event_sink = None
    engine.emit = lambda *args, **kwargs: None
    engine.token_tracker = SimpleNamespace(
        set_iteration=lambda iteration: None,
        update_last_tool_name=lambda tool_name: None,
    )
    engine._add_observation_step = lambda observation: None
    orchestrator = ToolOrchestrator(
        tools={"build": FailedConstructionTool()},
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=storage,
    )
    engine._get_tool_orchestrator = lambda: orchestrator

    termination = engine.run_setup_loop("set up project", max_iterations=1)

    assert issubclass(tool_base_module.OutputPersistenceError, RuntimeError)
    assert termination.termination is RunTerminationStatus.ABORTED
    assert storage.primary_calls == 1
    assert storage.emergency_calls == 1
    assert len(engine.run_evidence_state.action_attempts) == 1
    attempt = engine.run_evidence_state.action_attempts[0]
    assert attempt.action == "build:compile"
    assert attempt.outcome is OperationOutcome.FAILED
    assert attempt.evidence_refs == []
    assert len(engine.run_evidence_state.tool_observations) == 1
    observation = engine.run_evidence_state.tool_observations[0]
    assert observation.tool_name == "build"
    assert observation.roles == (EvidenceRole.BUILD,)
    assert observation.result.operation_outcome is OperationOutcome.FAILED
    assert observation.result.output_ref is None
    assert engine.run_evidence_state.conflicts == ("output_storage_failed",)
    assert engine.run_evidence_state.sealed is True
    assert engine.run_evidence_state.close_reason == "aborted"
    _assert_setup_abort(engine, "engine exception: OutputPersistenceError")


def test_setup_duplicate_cleanup_keeps_first_abort_record():
    engine = _engine(error=RuntimeError("LLM transport failed"))
    engine.run_setup_loop("set up project", max_iterations=3)
    first_record = engine.phase_machine.records[0]

    engine._record_setup_abort(True, "duplicate cleanup")

    assert engine.phase_machine.records == (first_record,)
    _assert_setup_abort(engine, "LLM response unavailable: LLM transport failed")


def test_run_task_abnormal_exit_does_not_record_setup_abort():
    engine = _engine(error=RuntimeError("LLM transport failed"))

    succeeded = engine.run_react_loop(
        "perform one task",
        max_iterations=3,
        completion_mode="run_task",
    )

    assert succeeded is False
    assert engine.phase_machine.records == ()
    assert engine.phase_machine.current_phase == "provision"
    assert engine.phase_machine.termination_state() == "open"
