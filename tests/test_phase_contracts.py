import pytest

from sag.agent.phase_machine import (
    PhaseAttemptRecord,
    PhaseMachine,
    PhaseOutcome,
    PhaseSkipRecord,
    PhaseTermination,
)


@pytest.mark.parametrize(
    ("termination", "outcome"),
    [
        ("running", "failed"),
        ("blocked", "success"),
        ("completed", "skipped"),
        ("skipped", "success"),
    ],
)
def test_illegal_phase_state_pairs_are_rejected(termination, outcome):
    with pytest.raises(ValueError, match="phase state"):
        PhaseAttemptRecord(
            phase="build",
            attempt_id="build-1",
            termination=termination,
            outcome=outcome,
        )


@pytest.mark.parametrize(
    ("termination", "outcome"),
    [
        ("running", "unknown"),
        ("completed", "unknown"),
        ("completed", "success"),
        ("completed", "partial"),
        ("completed", "failed"),
        ("blocked", "unknown"),
        ("blocked", "partial"),
        ("blocked", "failed"),
        ("aborted", "unknown"),
        ("aborted", "partial"),
        ("aborted", "failed"),
        ("skipped", "skipped"),
    ],
)
def test_legal_phase_state_pairs_are_accepted(termination, outcome):
    record = PhaseAttemptRecord(
        phase="build",
        attempt_id="build-1",
        termination=termination,
        outcome=outcome,
    )

    assert record.termination is PhaseTermination(termination)
    assert record.outcome is PhaseOutcome(outcome)


def test_completed_unknown_is_legal_and_not_a_run_verdict():
    record = PhaseAttemptRecord(
        phase="build",
        attempt_id="build-1",
        termination="completed",
        outcome="unknown",
    )

    assert record.termination == "completed"
    assert record.outcome == "unknown"
    assert not hasattr(PhaseMachine(), "overall_outcome")


def test_terminal_failure_has_no_transition_before_policy():
    record = PhaseAttemptRecord(
        phase="build",
        attempt_id="build-1",
        termination=PhaseTermination.COMPLETED,
        outcome=PhaseOutcome.FAILED,
    )

    assert record.is_terminal is True
    assert record.transition is None


def test_model_cannot_emit_skipped_record():
    machine = PhaseMachine()

    with pytest.raises(PermissionError, match="transition policy"):
        machine.record_model_skip(phase="test", reason="build failed")


def test_phase_records_are_append_only_and_skip_has_its_own_type():
    machine = PhaseMachine()
    machine.mark_done("provisioned", ["overlay:java"])
    first_record = machine.records[0]

    machine.mark_blocked("analysis unavailable", ["job:abc"])

    assert machine.records[0] is first_record
    assert first_record.legacy_claim is True
    assert first_record.termination is PhaseTermination.COMPLETED
    assert first_record.outcome is PhaseOutcome.UNKNOWN
    assert machine.records[1].legacy_claim is True
    assert machine.records[1].termination is PhaseTermination.BLOCKED
    assert machine.records[1].outcome is PhaseOutcome.UNKNOWN
    assert isinstance(
        PhaseSkipRecord(phase="test", attempt_id="test-skip-1", transition="policy"),
        PhaseSkipRecord,
    )


def test_phase_record_evidence_and_history_reject_external_mutation():
    machine = PhaseMachine()
    machine.mark_done("provisioned", ["overlay:java"])
    record = machine.records[0]

    assert record.evidence == ("overlay:java",)
    with pytest.raises(AttributeError):
        record.evidence.append("fabricated")
    with pytest.raises(AttributeError):
        machine.records.append(record)

    projected_history = machine.records
    with pytest.raises(TypeError):
        projected_history[0] = record

    assert machine.records == (record,)
