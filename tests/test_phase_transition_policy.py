from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ValidatorState, validate_phase_claim
from sag.agent.phase_machine import (
    PhaseAttemptRecord,
    PhaseClaim,
    PhaseMachine,
    PhaseOutcome,
    PhaseSkipRecord,
    PhaseTermination,
)
from sag.agent.phase_transitions import PhaseTransitionPolicy, RepairBudgets


def _record(phase: str, outcome: PhaseOutcome) -> PhaseAttemptRecord:
    return PhaseAttemptRecord(
        phase=phase,
        attempt_id=f"{phase}-1",
        termination=PhaseTermination.COMPLETED,
        outcome=outcome,
        evidence_refs=(f"validator://{phase}",),
    )


def _state() -> RunEvidenceState:
    return RunEvidenceState(run_id="routing-test")


def _analysis_record(
    *,
    attempt_id: str = "analyze-1",
    plan_sha256: str = "",
    signal: str = "done",
    outcome: PhaseOutcome = PhaseOutcome.SUCCESS,
) -> PhaseAttemptRecord:
    claim = PhaseClaim(
        phase="analyze",
        signal=signal,
        claimed_outcome=outcome,
        execution_plan_sha256=plan_sha256,
        execution_plan_ref=(
            "/workspace/.setup_agent/project_execution_plan.json" if plan_sha256 else ""
        ),
    )
    return PhaseAttemptRecord(
        phase="analyze",
        attempt_id=attempt_id,
        termination=(
            PhaseTermination.BLOCKED if signal == "blocked" else PhaseTermination.COMPLETED
        ),
        outcome=outcome,
        evidence_refs=("validator://analyze",),
        claim=claim,
    )


def test_failed_build_without_repair_skips_test():
    state = _state()
    policy = PhaseTransitionPolicy()

    decision = policy.decide(
        _record("build", PhaseOutcome.FAILED),
        state=state,
        budgets=RepairBudgets.none(),
    )

    assert decision.route.kind == "evidence_close"
    assert decision.route.target is None
    assert len(decision.skips) == 1
    skip = decision.skips[0]
    assert isinstance(skip, PhaseSkipRecord)
    assert skip.phase == "test"
    assert skip.termination is PhaseTermination.SKIPPED
    assert skip.outcome is PhaseOutcome.SKIPPED
    assert skip.reason == "build_not_ready"


def test_partial_build_advances_only_when_test_entry_is_ready():
    state = _state()
    policy = PhaseTransitionPolicy()
    partial = _record("build", PhaseOutcome.PARTIAL)

    state.set_fact(
        "build.test_entry_ready",
        False,
        evidence_ref="artifact://missing-tests",
    )
    blocked = policy.decide(partial, state=state, budgets=RepairBudgets.available())

    assert blocked.route.kind == "evidence_close"
    assert blocked.skips[0].phase == "test"

    state.set_fact(
        "build.test_entry_ready",
        True,
        evidence_ref="artifact://test-classpath",
    )
    ready = policy.decide(partial, state=state, budgets=RepairBudgets.available())

    assert ready.route.kind == "advance"
    assert ready.route.target == "test"


def test_successful_build_still_requires_test_entry_prerequisite():
    decision = PhaseTransitionPolicy().decide(
        _record("build", PhaseOutcome.SUCCESS),
        state=_state(),
        budgets=RepairBudgets.available(),
    )

    assert decision.route.kind == "evidence_close"
    assert decision.reason_code == "build_not_ready"


def test_historical_strict_policy_requires_both_survey_readiness_and_a_sealed_model_plan():
    state = _state()
    plan_sha256 = "a" * 64
    state.set_fact(
        "analysis.build_entry_ready",
        True,
        evidence_ref="validator://analysis",
    )

    missing = PhaseTransitionPolicy(require_analysis_plan=True).decide(
        _analysis_record(plan_sha256=plan_sha256),
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert missing.route.kind == "evidence_close"
    assert missing.reason_code == "analysis_plan_not_sealed"
    assert [record.phase for record in missing.skips] == ["build", "test"]

    state.set_fact(
        "analysis.execution_plan_sealed",
        True,
        evidence_ref="/workspace/.setup_agent/project_execution_plan.json",
    )
    state.set_fact(
        "analysis.execution_plan_sha256",
        plan_sha256,
        evidence_ref="/workspace/.setup_agent/project_execution_plan.json",
    )
    state.set_fact(
        "analysis.execution_plan_source_attempt_id",
        "analyze-1",
        evidence_ref="/workspace/.setup_agent/project_execution_plan.json",
    )

    ready = PhaseTransitionPolicy(require_analysis_plan=True).decide(
        _analysis_record(plan_sha256=plan_sha256),
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert ready.route.kind == "advance"
    assert ready.route.target == "build"


def test_analyze_one_plan_cannot_license_analyze_two():
    state = _state()
    plan_sha256 = "b" * 64
    for key, value in (
        ("analysis.build_entry_ready", True),
        ("analysis.execution_plan_sealed", True),
        ("analysis.execution_plan_sha256", plan_sha256),
        ("analysis.execution_plan_source_attempt_id", "analyze-1"),
    ):
        state.set_fact(key, value, evidence_ref="artifact://analyze-1-plan")

    decision = PhaseTransitionPolicy(require_analysis_plan=True).decide(
        _analysis_record(attempt_id="analyze-2", plan_sha256=plan_sha256),
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.route.kind == "evidence_close"
    assert decision.reason_code == "analysis_plan_not_current"
    assert [record.phase for record in decision.skips] == ["build", "test"]


def test_blocked_analyze_without_a_plan_claim_cannot_borrow_a_sealed_artifact():
    state = _state()
    for key, value in (
        ("analysis.build_entry_ready", True),
        ("analysis.execution_plan_sealed", True),
        ("analysis.execution_plan_sha256", "c" * 64),
        ("analysis.execution_plan_source_attempt_id", "analyze-2"),
    ):
        state.set_fact(key, value, evidence_ref="artifact://analyze-2-plan")

    decision = PhaseTransitionPolicy(require_analysis_plan=True).decide(
        _analysis_record(
            attempt_id="analyze-2",
            signal="blocked",
            outcome=PhaseOutcome.PARTIAL,
        ),
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.route.kind == "evidence_close"
    assert decision.reason_code == "analysis_plan_not_current"
    assert [record.phase for record in decision.skips] == ["build", "test"]


def test_legacy_replay_policy_can_replay_pre_plan_analysis_transition():
    state = _state()
    state.set_fact(
        "analysis.build_entry_ready",
        True,
        evidence_ref="validator://legacy-analysis",
    )

    decision = PhaseTransitionPolicy(require_analysis_plan=False).decide(
        _record("analyze", PhaseOutcome.SUCCESS),
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.route.kind == "advance"
    assert decision.route.target == "build"


def test_terminal_test_routes_to_evidence_close_for_every_outcome():
    policy = PhaseTransitionPolicy()
    for outcome in (
        PhaseOutcome.SUCCESS,
        PhaseOutcome.PARTIAL,
        PhaseOutcome.FAILED,
        PhaseOutcome.UNKNOWN,
    ):
        decision = policy.decide(
            _record("test", outcome),
            state=_state(),
            budgets=RepairBudgets.available(),
        )
        assert decision.route.kind == "evidence_close"
        assert decision.skips == ()


def test_report_terminal_record_routes_to_flow_close():
    decision = PhaseTransitionPolicy().decide(
        _record("report", PhaseOutcome.SUCCESS),
        state=_state(),
        budgets=RepairBudgets.none(),
    )

    assert decision.route.kind == "flow_close"


def test_determined_collection_failure_closes_within_two_gate_interactions():
    machine = PhaseMachine(start_phase="test")
    optimistic = validate_phase_claim(PhaseOutcome.SUCCESS, ValidatorState.RED)
    honest = validate_phase_claim(PhaseOutcome.FAILED, ValidatorState.RED)

    assert optimistic.accepted is False
    record = machine.close_attempt(honest)

    assert honest.accepted is True
    assert record.termination is PhaseTermination.COMPLETED
    assert record.outcome is PhaseOutcome.FAILED
