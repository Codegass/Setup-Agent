# src/sag/tools/phase_tool.py
"""phase(action: done | blocked | note, outcome=...) lifecycle surface.

Terminal actions are model claims.  The tool validates them against physical
evidence and emits both claim and gate records; it never mutates phase state or
selects the next phase."""

from typing import Any, Dict, List, Optional

from sag.agent.phase_gates import ClaimDisposition, check_phase_claim
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome

from .base import BaseTool, ToolResult


class PhaseTool(BaseTool):
    def __init__(self, machine, validator, orchestrator, project_name, gate_fn=check_phase_claim):
        super().__init__(
            name="phase",
            description=(
                "Phase lifecycle: action='done' with outcome and evidence claims the current "
                "phase ended; action='blocked' with outcome, reason, and evidence claims an "
                "external impediment; both are checked against physical evidence. "
                "action='note' (text) records a working note. The engine advances phases; "
                "you never pick or reorder them."
            ),
        )
        self.machine = machine
        self.validator = validator
        self.orchestrator = orchestrator
        self.project_name = project_name
        self.gate_fn = gate_fn

    def execute(
        self,
        action: str,
        outcome: Optional[str] = None,
        key_results: str = "",
        reason: str = "",
        evidence: Optional[List[str]] = None,
        text: str = "",
    ) -> ToolResult:
        if self.machine.is_complete:
            return ToolResult.completed_failure(
                output="All phases already complete.",
                error="machine complete",
            )
        verb = (action or "").strip().lower()
        phase = self.machine.current_phase

        if verb == "note":
            if outcome is not None:
                return ToolResult.completed_failure(
                    output="note does not accept a phase outcome",
                    error="outcome is forbidden for note",
                    error_code="phase_note_outcome_forbidden",
                )
            if not text:
                return ToolResult.completed_failure(
                    output="note requires text",
                    error="missing text",
                )
            return ToolResult.completed_success(
                output=f"Noted ({phase}): {text}",
                facts={"phase": phase},
                metadata={"phase_signal": "note", "text": text},
            )

        if verb not in {"done", "blocked"}:
            return ToolResult.completed_failure(
                output=f"Unknown phase action: {action!r}",
                error="invalid action",
                error_code="phase_action_invalid",
                suggestions=["Use action= done | blocked | note"],
            )

        if outcome is None or not str(outcome).strip():
            return ToolResult.completed_failure(
                output=f"{verb} requires an explicit evidence outcome",
                error="terminal phase signal requires outcome",
                error_code="phase_outcome_required",
            )
        try:
            claimed_outcome = PhaseOutcome(str(outcome).strip().lower())
        except ValueError:
            return ToolResult.completed_failure(
                output=f"Invalid phase outcome: {outcome!r}",
                error="invalid phase outcome",
                error_code="phase_outcome_invalid",
            )
        if claimed_outcome is PhaseOutcome.SKIPPED:
            return ToolResult.completed_failure(
                output="Only the transition policy may skip a phase",
                error="model cannot claim skipped",
                error_code="phase_skip_forbidden",
            )
        if verb == "blocked" and claimed_outcome is PhaseOutcome.SUCCESS:
            return ToolResult.completed_failure(
                output="blocked cannot claim a successful phase outcome",
                error="blocked+success is an illegal phase state",
                error_code="phase_state_illegal",
            )
        if verb == "blocked" and not (reason or "").strip():
            return ToolResult.completed_failure(
                output="blocked requires a concrete external impediment and evidence refs",
                error="missing reason",
                error_code="phase_blocker_reason_required",
            )

        claim = PhaseClaim(
            phase=phase,
            signal=verb,
            claimed_outcome=claimed_outcome,
            key_results=key_results,
            reason=reason,
            evidence_refs=tuple(evidence or ()),
        )
        gate = self.gate_fn(
            phase,
            claim,
            self.validator,
            self.orchestrator,
            self.project_name,
        )
        if gate.claim is None:
            gate = gate.with_claim(claim)

        # A blocked record cannot carry the otherwise-valid pessimistic
        # ``blocked + success`` combination from the generic claim matrix.
        if verb == "blocked" and gate.validated_outcome is PhaseOutcome.SUCCESS:
            gate = type(gate)(
                accepted=False,
                validated_outcome=gate.validated_outcome,
                claim_disposition=ClaimDisposition.CONTRADICTED,
                validator_state=gate.validator_state,
                reason="phase evidence is green; an external blocked termination is not valid",
                evidence_refs=gate.evidence_refs,
                suggestions=gate.suggestions,
                code="blocked_contradicted_by_green_evidence",
                claim=claim,
            )

        if not gate.accepted:
            return ToolResult.completed_failure(
                output=f"Phase '{phase}' {verb}-claim rejected: {gate.reason}",
                error=gate.reason or "phase claim contradicted by validator evidence",
                error_code=gate.code or "phase_claim_contradicted",
                suggestions=list(gate.suggestions),
                metadata={
                    "phase_claim": claim.to_metadata(),
                    "gate_result": gate.to_metadata(),
                },
            )

        return ToolResult.completed_success(
            output=(
                f"Phase '{phase}' terminal claim accepted with validated outcome "
                f"'{gate.validated_outcome.value}'. Awaiting engine routing."
            ),
            facts={"phase": phase},
            metadata={
                "phase_signal": verb,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
                # Temporary presentation aliases consumed by the WS3 engine
                # seam until Task 7 installs PhaseTransitionPolicy.
                "key_results": key_results,
                "reason": reason,
                "evidence": list(evidence or []),
            },
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["done", "blocked", "note"]},
                "outcome": {
                    "type": "string",
                    "enum": ["unknown", "success", "partial", "failed"],
                    "description": "Required for done/blocked; forbidden for note",
                },
                "key_results": {
                    "type": "string",
                    "description": "done: lasting record of this phase (facts, versions, paths)",
                },
                "reason": {"type": "string", "description": "blocked: why the phase cannot finish"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "refs supporting the claim (output_*, job:*, file:*)",
                },
                "text": {"type": "string", "description": "note: working note"},
            },
            "required": ["action"],
        }
