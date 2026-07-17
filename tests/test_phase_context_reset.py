from types import SimpleNamespace

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.phase_transitions import PhaseTransitionPolicy, RepairBudgets
from sag.agent.react_engine import ReActEngine
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.tools.base import ToolResult


def test_phase_reset_injects_cumulative_typed_handoff(tmp_path):
    state = RunEvidenceState(run_id="context-reset")
    state.register_fact(
        scope="project_analysis",
        key="java.required_version",
        value="17",
        source_ref="file://pom.xml#release",
        source_phase="analyze",
    )
    state.ingest_tool_result(
        "artifacts",
        "build",
        result=ToolResult.completed_failure(
            output="configure\nCMake Error: target missing",
            error="native build failed",
            error_code="BUILD_FAILED",
            failure_signature="cmake:target-missing",
            error_tail_preview="CMake Error: target missing",
        ),
        provenance="log://build/full",
        params={"command": "cmake --build build"},
    )
    state.set_fact(
        "build.test_entry_ready",
        True,
        evidence_ref="artifact://test-runtime",
    )
    handoff = PhaseHandoff(state, storage_path=tmp_path / "phase-handoff.json")

    machine = PhaseMachine(start_phase="build")
    claim = PhaseClaim(
        phase="build",
        signal="done",
        claimed_outcome=PhaseOutcome.PARTIAL,
        evidence_refs=("log://build/full",),
    )
    gate = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.PARTIAL,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.PARTIAL,
        reason="partial native build with runnable tests",
        evidence_refs=("log://build/full",),
        validated_facts={"build.test_entry_ready": True},
        claim=claim,
    )
    record = machine.close_attempt(gate)
    decision = PhaseTransitionPolicy().decide(
        record,
        state=state,
        budgets=RepairBudgets(global_remaining=2, phase_remaining={"build": 1, "test": 1}),
    )

    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = machine
    engine.run_evidence_state = state
    engine.phase_handoff = handoff
    engine.prompt_builder = ReActPromptBuilder.__new__(ReActPromptBuilder)
    engine.config = SimpleNamespace(max_iterations=150, phase_min_floors={})
    engine._run_max_iterations = 150
    engine.current_iteration = 10
    engine._phase_iterations = 3
    engine.steps_since_context_switch = 3
    engine.steps = [SimpleNamespace(content="obsolete transient phase text")]
    engine._archive_window_steps = lambda: None
    engine._start_phase_branch = lambda: None
    engine._persist_phase_record = lambda *args, **kwargs: None
    engine._get_timestamp = lambda: "2026-07-17T00:00:00Z"
    engine._detected_build_system = lambda: None
    engine._recommended_build_line = lambda phase: None
    engine._python_phase_guidance = lambda phase: None

    engine._apply_phase_decision(record, decision)

    assert len(engine.steps) == 1
    assert "obsolete transient phase text" not in engine.steps[0].content
    assert "java.required_version" in engine.steps[0].content
    assert "CMake Error: target missing" in engine.steps[0].content
    assert "cmake:target-missing" in engine.steps[0].content
