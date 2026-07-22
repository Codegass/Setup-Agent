from types import SimpleNamespace

from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseOutcome
from sag.tools.phase_tool import PhaseTool


def _tool(gate_result):
    machine = SimpleNamespace(
        current_phase="build",
        current_attempt_id="build-1",
        is_complete=False,
    )
    return PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=None,
        project_name="demo",
        gate_fn=lambda *args, **kwargs: gate_result,
    )


def _gate(*, accepted=True, outcome=PhaseOutcome.SUCCESS):
    return GateResult(
        accepted=accepted,
        validated_outcome=outcome,
        claim_disposition=(
            ClaimDisposition.CONFIRMED if accepted else ClaimDisposition.CONTRADICTED
        ),
        validator_state=(
            ValidatorState.GREEN if outcome is PhaseOutcome.SUCCESS else ValidatorState.RED
        ),
        reason="scripted gate",
        evidence_refs=("artifact://build",),
    )


def test_terminal_signal_requires_outcome():
    result = _tool(_gate()).execute(action="done", key_results="compiled")

    assert result.succeeded is False
    assert result.error_code == "phase_outcome_required"


def test_note_rejects_outcome():
    result = _tool(_gate()).execute(
        action="note", text="trying another compiler", outcome="unknown"
    )

    assert result.succeeded is False
    assert result.error_code == "phase_note_outcome_forbidden"


def test_done_carries_claim_and_validation_without_advancing_machine():
    result = _tool(_gate()).execute(
        action="done",
        outcome="success",
        key_results="compiled",
        evidence=["output_7"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "done"
    assert result.metadata["phase_claim"]["claimed_outcome"] == "success"
    assert result.metadata["gate_result"]["validated_outcome"] == "success"
    assert result.metadata["gate_result"]["evidence_refs"] == ["artifact://build"]


def test_rejected_optimistic_claim_emits_no_phase_signal():
    result = _tool(_gate(accepted=False, outcome=PhaseOutcome.FAILED)).execute(
        action="done", outcome="success", key_results="compiled"
    )

    assert result.succeeded is False
    assert "phase_signal" not in result.metadata


def test_phase_schema_requires_outcome_for_terminal_claims_semantically():
    schema = _tool(_gate()).get_parameter_schema()

    assert schema["properties"]["outcome"]["enum"] == [
        "unknown",
        "success",
        "partial",
        "failed",
    ]
    assert "skipped" not in schema["properties"]["outcome"]["enum"]


def test_repair_emits_typed_proposal_without_gate_or_state_mutation():
    result = _tool(_gate()).execute(
        action="repair",
        target_phase="analyze",
        reason_code="manifest_incomplete",
        failure_signature="missing-root-manifest",
        hypothesis="re-analysis will discover the actual reactor root",
        evidence=["output_9"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "repair"
    assert result.metadata["repair_request"] == {
        "from_phase": "build",
        "target_phase": "analyze",
        "source_attempt_id": "build-1",
        "reason_code": "manifest_incomplete",
        "failure_signature": "missing-root-manifest",
        "hypothesis": "re-analysis will discover the actual reactor root",
        "evidence_refs": ["output_9"],
    }


def test_repair_requires_current_attempt_evidence_and_forbids_outcome():
    no_evidence = _tool(_gate()).execute(
        action="repair",
        target_phase="analyze",
        reason_code="manifest_incomplete",
        failure_signature="missing-root-manifest",
        hypothesis="inspect manifests again",
    )
    with_outcome = _tool(_gate()).execute(
        action="repair",
        outcome="failed",
        target_phase="analyze",
        reason_code="manifest_incomplete",
        failure_signature="missing-root-manifest",
        hypothesis="inspect manifests again",
        evidence=["output_9"],
    )

    assert no_evidence.error_code == "phase_repair_invalid"
    assert with_outcome.error_code == "phase_repair_outcome_forbidden"
