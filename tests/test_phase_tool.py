# tests/test_phase_tool.py
"""phase(action: done|blocked|note|repair) lifecycle surface."""

from types import SimpleNamespace

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseOutcome
from sag.tools.phase_tool import PhaseTool


class GateRecorder:
    def __init__(
        self,
        ok=True,
        reason="",
        suggestions=None,
        *,
        validated_outcome=PhaseOutcome.SUCCESS,
        validator_state=ValidatorState.GREEN,
    ):
        self.calls = []
        self.result = GateResult(
            accepted=ok,
            validated_outcome=validated_outcome,
            claim_disposition=(
                ClaimDisposition.CONFIRMED if ok else ClaimDisposition.CONTRADICTED
            ),
            validator_state=validator_state,
            reason=reason,
            suggestions=tuple(suggestions or ()),
        )

    def __call__(self, phase, claim, validator, orchestrator, project_name, *, sealed=False):
        self.calls.append(phase)
        self.sealed = sealed
        return self.result.with_claim(claim)


def _tool(gate, phase="build"):
    machine = SimpleNamespace(current_phase=phase, is_complete=False)
    return PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=None,
        project_name="demo",
        gate_fn=gate,
    )


def test_done_passes_gate_and_signals_engine():
    gate = GateRecorder(ok=True)
    tool = _tool(gate)

    result = tool.execute(
        action="done",
        outcome="success",
        key_results="compiled 115 classes",
        evidence=["output_x"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "done"
    assert result.metadata["phase_claim"]["key_results"] == "compiled 115 classes"
    assert gate.calls == ["build"]


def test_a_claim_carries_the_evidence_seal_to_the_gate():
    """The gate settles the job ledger before it grades (spec §3.2 trigger 2),
    and a SEALED run accepts no further evidence: the report phase can still
    claim after evidence-close, and settling for it would write a receipt the
    sealed verdict has already recorded as `job_unsettled`."""
    gate = GateRecorder(ok=True)
    tool = _tool(gate)
    tool.run_evidence_state = RunEvidenceState(run_id="phase-tool-seal")

    tool.execute(action="done", outcome="success", key_results="115 classes", evidence=["ref"])
    assert gate.sealed is False

    tool.run_evidence_state.seal(
        finalized_at="2026-07-29T11:17:37Z", close_reason="test_terminated"
    )
    tool.execute(action="done", outcome="success", key_results="115 classes", evidence=["ref"])
    assert gate.sealed is True


def test_done_rejected_by_gate_returns_options_no_signal():
    gate = GateRecorder(ok=False, reason="no artifacts", suggestions=["build(action='compile')"])
    tool = _tool(gate)

    result = tool.execute(action="done", outcome="success", key_results="done!", evidence=[])

    assert result.succeeded is False
    assert "phase_signal" not in result.metadata
    assert result.operation_outcome.value == "failed"
    assert any("compile" in s for s in result.suggestions)
    assert result.metadata["gate_result"]["claim_disposition"] == "contradicted"


def test_external_blocked_claim_is_accepted_when_evidence_is_unavailable():
    gate = GateRecorder(
        ok=True,
        reason="external repository unavailable",
        validated_outcome=PhaseOutcome.UNKNOWN,
        validator_state=ValidatorState.UNAVAILABLE,
    )
    tool = _tool(gate)

    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="develocity plugin unresolvable",
        evidence=["job:a"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "blocked"
    assert result.metadata["phase_claim"]["reason"] == "develocity plugin unresolvable"
    assert result.metadata["gate_result"]["validated_outcome"] == "unknown"
    assert gate.calls == ["build"]


def test_blocked_requires_reason():
    result = _tool(GateRecorder()).execute(action="blocked", outcome="failed", reason="")
    assert result.succeeded is False


def test_note_signals_engine_for_durable_phase_notes():
    result = _tool(GateRecorder()).execute(action="note", text="trying maven 3.9.9 next")
    assert result.succeeded is True
    assert result.metadata == {
        "phase_signal": "note",
        "text": "trying maven 3.9.9 next",
    }


def test_machine_complete_rejects_actions():
    machine = SimpleNamespace(current_phase=None, is_complete=True)
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=None,
        project_name="demo",
        gate_fn=GateRecorder(),
    )
    result = tool.execute(action="done", outcome="unknown", key_results="x")
    assert result.succeeded is False
