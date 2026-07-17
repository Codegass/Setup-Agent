import pytest

from sag.agent.phase_gates import ClaimDisposition, ValidatorState, validate_phase_claim
from sag.agent.phase_machine import PhaseOutcome


@pytest.mark.parametrize(
    ("claim", "gate", "accepted", "disposition", "validated"),
    [
        (
            PhaseOutcome.SUCCESS,
            ValidatorState.GREEN,
            True,
            ClaimDisposition.CONFIRMED,
            PhaseOutcome.SUCCESS,
        ),
        (
            PhaseOutcome.SUCCESS,
            ValidatorState.RED,
            False,
            ClaimDisposition.CONTRADICTED,
            PhaseOutcome.FAILED,
        ),
        (
            PhaseOutcome.FAILED,
            ValidatorState.GREEN,
            True,
            ClaimDisposition.PESSIMISTIC,
            PhaseOutcome.SUCCESS,
        ),
        (
            PhaseOutcome.PARTIAL,
            ValidatorState.UNAVAILABLE,
            True,
            ClaimDisposition.UNVERIFIABLE,
            PhaseOutcome.UNKNOWN,
        ),
        (
            PhaseOutcome.UNKNOWN,
            ValidatorState.UNAVAILABLE,
            True,
            ClaimDisposition.CONFIRMED,
            PhaseOutcome.UNKNOWN,
        ),
        (
            PhaseOutcome.SUCCESS,
            ValidatorState.UNAVAILABLE,
            False,
            ClaimDisposition.CONTRADICTED,
            PhaseOutcome.UNKNOWN,
        ),
    ],
)
def test_phase_claim_matrix(claim, gate, accepted, disposition, validated):
    result = validate_phase_claim(claim, gate)

    assert result.accepted is accepted
    assert result.claim_disposition is disposition
    assert result.validated_outcome is validated


def test_unknown_claim_is_refined_by_available_evidence():
    result = validate_phase_claim(PhaseOutcome.UNKNOWN, ValidatorState.PARTIAL)

    assert result.accepted is True
    assert result.claim_disposition is ClaimDisposition.REFINED
    assert result.validated_outcome is PhaseOutcome.PARTIAL


def test_record_outcome_always_comes_from_gate_not_claim():
    machine = __import__(
        "sag.agent.phase_machine", fromlist=["PhaseMachine"]
    ).PhaseMachine()
    validation = validate_phase_claim(PhaseOutcome.FAILED, ValidatorState.GREEN)

    record = machine.close_attempt(validation)

    assert record.claim.claimed_outcome is PhaseOutcome.FAILED
    assert record.validated_outcome is PhaseOutcome.SUCCESS
    assert record.outcome is PhaseOutcome.SUCCESS
