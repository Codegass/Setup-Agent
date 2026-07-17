from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ValidatorState, validate_phase_claim
from sag.agent.phase_machine import (
    PhaseAttemptRecord,
    PhaseClaim,
    PhaseMachine,
    PhaseOutcome,
    PhaseTermination,
)
from sag.agent.phase_transitions import (
    PhaseTransitionPolicy,
    RepairBudgets,
    RepairRequest,
)


def _state() -> RunEvidenceState:
    return RunEvidenceState(run_id="repair-test")


def _request(
    *,
    from_phase="test",
    target_phase="build",
    signature="missing_sibling_artifact:module-a",
    evidence_ref="log://test-2/tail",
):
    return RepairRequest(
        from_phase=from_phase,
        target_phase=target_phase,
        source_attempt_id=f"{from_phase}-1",
        reason_code="missing_sibling_artifact",
        failure_signature=signature,
        hypothesis="root install will produce the sibling artifact",
        evidence_refs=(evidence_ref,),
    )


def test_test_can_repair_build_once_with_new_evidence():
    state = _state()
    request = _request()
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.route.kind == "repair"
    assert decision.route.target == "build"
    assert decision.route.new_attempt is True
    assert state.repair_records[-1].accepted is True


def test_same_repair_signature_without_progress_is_rejected():
    state = _state()
    request = _request()
    vector = {"environment": 1, "dependencies": 3, "artifacts": 4}
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)
    state.record_repair(request, state_vector=vector, accepted=True)

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.available(),
        current_state_vector=vector,
    )

    assert decision.route.kind == "evidence_close"
    assert decision.reason_code == "repair_without_progress"
    assert state.repair_records[-1].accepted is False


def test_relevant_progress_allows_same_repair_signature_again():
    state = _state()
    request = _request()
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)
    state.record_repair(
        request,
        state_vector={"environment": 1, "dependencies": 3, "artifacts": 4},
        accepted=True,
    )

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.available(),
        current_state_vector={"environment": 1, "dependencies": 4, "artifacts": 4},
    )

    assert decision.route.kind == "repair"


def test_non_dependency_backjump_is_rejected():
    state = _state()
    request = _request(target_phase="analyze")
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.reason_code == "illegal_edge"
    assert decision.route.kind == "evidence_close"


def test_repair_requires_evidence_from_current_attempt():
    state = _state()
    request = _request()
    state.record_phase_evidence("test-0", request.evidence_refs)

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.available(),
    )

    assert decision.reason_code == "stale_repair_evidence"
    assert decision.route.kind == "evidence_close"


def test_repair_requires_both_global_and_phase_budget():
    state = _state()
    request = _request()
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.none(),
    )

    assert decision.reason_code == "repair_budget_exhausted"
    assert decision.route.kind == "evidence_close"


def test_rejected_repair_still_uses_the_validated_ordinary_route():
    state = _state()
    request = _request(from_phase="build", target_phase="analyze")
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)
    state.set_fact(
        "build.test_entry_ready",
        True,
        evidence_ref="artifact://test-classpath",
    )
    source = PhaseAttemptRecord(
        phase="build",
        attempt_id="build-1",
        termination=PhaseTermination.COMPLETED,
        outcome=PhaseOutcome.PARTIAL,
    )

    decision = PhaseTransitionPolicy().request_repair(
        request,
        state=state,
        budgets=RepairBudgets.none(),
        source_record=source,
    )

    assert state.repair_records[-1].decision_reason == "repair_budget_exhausted"
    assert decision.route.kind == "advance"
    assert decision.route.target == "test"
    assert decision.reason_code == "test_entry_ready"


def test_repair_reentry_uses_monotonic_attempt_ids_without_rewriting_history():
    state = _state()
    policy = PhaseTransitionPolicy()
    machine = PhaseMachine(start_phase="build")
    state.set_fact(
        "build.test_entry_ready",
        True,
        evidence_ref="artifact://test-classpath",
    )
    build_gate = validate_phase_claim(
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        ValidatorState.GREEN,
    )
    build_record = machine.close_attempt(build_gate)
    applied_build = machine.apply(
        policy.decide(build_record, state=state, budgets=RepairBudgets.available())
    )[0]
    state.record_phase_attempt(applied_build)

    request = RepairRequest(
        from_phase="test",
        target_phase="build",
        source_attempt_id="test-1",
        reason_code="missing_sibling_artifact",
        failure_signature="missing_sibling_artifact:module-a",
        hypothesis="root install will publish the sibling artifact",
        evidence_refs=("log://test-1/tail",),
    )
    state.record_phase_evidence(request.source_attempt_id, request.evidence_refs)
    test_gate = validate_phase_claim(
        PhaseClaim(
            phase="test",
            claimed_outcome=PhaseOutcome.FAILED,
            evidence_refs=request.evidence_refs,
        ),
        ValidatorState.RED,
    )
    test_record = machine.close_attempt(test_gate)
    machine.apply(
        policy.request_repair(
            request,
            state=state,
            budgets=RepairBudgets.available(),
            source_outcome=test_record.outcome,
        )
    )

    assert [record.attempt_id for record in machine.records] == ["build-1", "test-1"]
    assert machine.current_phase == "build"
    assert machine.current_attempt_id == "build-2"
