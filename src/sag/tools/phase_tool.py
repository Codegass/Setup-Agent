# src/sag/tools/phase_tool.py
"""phase(action: done | blocked | note, outcome=...) lifecycle surface.

Terminal actions are model claims.  The tool validates them against physical
evidence and emits both claim and gate records; it never mutates phase state or
selects the next phase."""

import json
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from sag.agent.attempt_policy import (
    build_attempt_requirement,
    required_test_attempt,
    resolve_survey_test_candidates,
    run_test_receipts,
    terminal_test_receipts,
    untried_islands_requirement,
)
from sag.agent.job_obligations import read_obligations
from sag.agent.phase_gates import (
    ANALYSIS_FACTS_RECOVERY_CODES,
    ANALYSIS_RECOVERY_FACT,
    JOB_INTEGRITY_FACT,
    ClaimDisposition,
    GateControlDisposition,
    GateResult,
    ValidatorState,
    check_phase_claim,
    gate_observation_text,
    settlement_capped_outcome,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome

from .base import BaseTool, ToolResult


class PhaseTool(BaseTool):
    def __init__(
        self,
        machine,
        validator,
        orchestrator,
        project_name,
        gate_fn=check_phase_claim,
        run_evidence_state=None,
    ):
        super().__init__(
            name="phase",
            description=(
                "Phase lifecycle: action='done' with outcome and evidence claims the current "
                "phase ended; action='blocked' with outcome, reason, and evidence claims an "
                "external impediment; both are checked against physical evidence. "
                "action='note' records a working note. A rejected terminal claim returns "
                "typed judge facts; the model chooses its next ordinary project action. "
                "The engine alone routes or skips phases."
            ),
        )
        self.machine = machine
        self.validator = validator
        self.orchestrator = orchestrator
        self.project_name = project_name
        self.gate_fn = gate_fn
        self.run_evidence_state = run_evidence_state
        self._analysis_facts_recovery: Optional[Callable[[], Optional[str]]] = None
        self._analysis_facts_recovery_attempted = False

    def bind_analysis_facts_recovery(
        self,
        callback: Callable[[], Optional[str]],
    ) -> None:
        """Bind the controller's one-shot framework-survey recovery seam."""

        self._analysis_facts_recovery = callback

    def _grade(self, claim: PhaseClaim, phase: str, *, sealed: bool):
        """One initial grade plus at most one controller-owned survey regrade.

        The gate settles the job ledger before it grades (spec §3.2 trigger 2),
        and a sealed run accepts no further evidence: after evidence-close the
        report phase can still make a claim, and settling for it would write a
        receipt the sealed verdict cannot state.
        """
        gate = self.gate_fn(
            phase,
            claim,
            self.validator,
            self.orchestrator,
            self.project_name,
            sealed=sealed,
        )
        gate = gate if gate.claim is not None else gate.with_claim(claim)
        callback = self._analysis_facts_recovery
        if (
            phase != "analyze"
            or sealed
            or gate.code not in ANALYSIS_FACTS_RECOVERY_CODES
            or self._analysis_facts_recovery_attempted
            or not callable(callback)
        ):
            return gate

        # This is an engine-owned repair of its Category-1 survey guarantee,
        # not a model project action.  Spend the single run-local attempt before
        # calling so exceptions cannot manufacture retry authority.
        self._analysis_facts_recovery_attempted = True
        try:
            survey_status = str(callback() or "failed").strip().lower()
        except Exception:
            survey_status = "failed"
        if survey_status not in {"created", "present", "failed"}:
            survey_status = "failed"
        before_code = gate.code
        final_gate = self.gate_fn(
            phase,
            claim,
            self.validator,
            self.orchestrator,
            self.project_name,
            sealed=sealed,
        )
        final_gate = final_gate if final_gate.claim is not None else final_gate.with_claim(claim)
        after_code = final_gate.code or "unclassified"
        resolved = bool(
            after_code not in ANALYSIS_FACTS_RECOVERY_CODES
            and (
                final_gate.control_disposition
                is not GateControlDisposition.HARNESS_RECOVERY_REQUIRED
                or after_code == "evidence_unpersisted"
            )
        )
        return replace(
            final_gate,
            validated_facts={
                **dict(final_gate.validated_facts),
                ANALYSIS_RECOVERY_FACT: {
                    "kind": "framework_survey",
                    "attempt": 1,
                    "before_code": before_code,
                    "survey_status": survey_status,
                    "after_code": after_code,
                    "resolved": resolved,
                },
            },
        )

    def _test_receipt_scope_facts(self, required_attempt) -> Dict[str, Any]:
        """Both receipt counts, each under a name that says which one it is.

        The sealed number was a hard-coded 0 that told freemarker's run it had
        no receipts while it held one. Counting fixed the lie and left a subtler
        one: the count is RUN-WIDE, while the requirement that produced this
        rejection asked the candidate-bound question — so `1` sat next to
        `TEST_ATTEMPT_REQUIRED` ("no terminal test receipt") answering a
        question nobody beside it had asked. Both are sealed, both are named.

        The candidate-bound count exists only where candidates do: an
        unresolvable survey has nothing to bind to, and an absent fact stays
        absent rather than seal a 0 that means "not asked".
        """
        facts: Dict[str, Any] = {
            "run_wide_test_receipts": len(run_test_receipts(self.run_evidence_state)),
            "test_attempt_requirement": required_attempt.to_metadata(),
        }
        resolved = resolve_survey_test_candidates(self.orchestrator)
        if resolved.status == "available":
            candidates = (
                (resolved.primary,) if resolved.primary is not None else resolved.candidates
            )
            facts["candidate_bound_test_receipts"] = len(
                terminal_test_receipts(
                    self.run_evidence_state,
                    attempt_id=getattr(self.machine, "current_attempt_id", None),
                    candidates=candidates,
                )
            )
        return facts

    @staticmethod
    def _rejection_output(reason: str, facts: Dict[str, Any]) -> str:
        """Bounded model-visible judge facts; no hidden metadata dependency."""

        if not facts:
            return reason
        try:
            rendered = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = repr(facts)
        if len(rendered) > 4_000:
            rendered = rendered[:3_960] + "… [fact projection truncated]"
        return f"{reason}\nObserved judge facts: {rendered}"

    @staticmethod
    def _rejected_claim_result(
        claim: PhaseClaim,
        *,
        code: str,
        reason: str,
        control_disposition: GateControlDisposition,
        blocker_owner: str,
        validated_facts: Optional[Dict[str, Any]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> ToolResult:
        """One non-prescriptive control rejection for the engine to route."""

        gate = GateResult(
            accepted=False,
            validated_outcome=PhaseOutcome.UNKNOWN,
            claim_disposition=ClaimDisposition.CONTRADICTED,
            validator_state=ValidatorState.UNAVAILABLE,
            reason=reason,
            evidence_refs=tuple(evidence_refs or claim.evidence_refs),
            suggestions=(),
            code=code,
            validated_facts=dict(validated_facts or {}),
            claim=claim,
            control_disposition=control_disposition,
            blocker_owner=blocker_owner,
        )
        facts = dict(validated_facts or {})
        return ToolResult.completed_failure(
            output=PhaseTool._rejection_output(reason, facts),
            error=reason,
            error_code=code,
            facts=facts,
            metadata={
                "phase": claim.phase,
                "control_disposition": control_disposition.value,
                "blocker_owner": blocker_owner,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        )

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

        sealed = bool(getattr(self.run_evidence_state, "sealed", False))
        gate = None
        try:
            ledger_records = read_obligations(self.orchestrator)
        except Exception:
            ledger_records = None
        if ledger_records is None:
            return self._rejected_claim_result(
                claim,
                code="job_evidence_integrity",
                reason=(
                    "Job evidence reconciliation requires harness recovery; "
                    "the obligations ledger could not be read and no project "
                    "claim was graded."
                ),
                control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
                blocker_owner="harness",
                validated_facts={JOB_INTEGRITY_FACT: ["ledger_unreadable"]},
            )
        if ledger_records:
            # Ledger control truth precedes attempt floors and island policy.
            # The grade performs typed reconciliation and returns before any
            # physical project inspection when a job is running, settlement is
            # pending, or a settled receipt has lost its identity witness.
            gate = self._grade(claim, phase, sealed=sealed)
            if not gate.accepted and gate.control_disposition in {
                GateControlDisposition.WAIT_REQUIRED,
                GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            }:
                return ToolResult.completed_failure(
                    output=self._rejection_output(
                        f"Phase '{phase}' {verb}-claim rejected: {gate.reason}",
                        dict(gate.validated_facts),
                    ),
                    error=gate.reason or "job evidence reconciliation failed",
                    error_code=gate.code or "job_evidence_integrity",
                    suggestions=list(gate.suggestions),
                    facts=dict(gate.validated_facts),
                    metadata={
                        "control_disposition": gate.control_disposition.value,
                        "blocker_owner": gate.blocker_owner.value,
                        "phase_claim": claim.to_metadata(),
                        "gate_result": gate.to_metadata(),
                    },
                )

        required_attempt = required_test_attempt(
            self.run_evidence_state,
            self.orchestrator,
            phase=phase,
            attempt_id=getattr(self.machine, "current_attempt_id", None),
        )
        if required_attempt is not None:
            return self._rejected_claim_result(
                claim,
                code="TEST_ATTEMPT_REQUIRED",
                reason=(
                    "Test phase cannot terminate before one real terminal test "
                    "execution receipt. The controller owns and will execute the "
                    "registered phase-floor action before the model continues."
                ),
                control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
                blocker_owner="harness",
                validated_facts=self._test_receipt_scope_facts(required_attempt),
            )

        if verb == "blocked" or claimed_outcome is PhaseOutcome.FAILED:
            build_requirement = build_attempt_requirement(
                self.run_evidence_state,
                self.orchestrator,
                phase=phase,
                attempt_id=getattr(self.machine, "current_attempt_id", None),
            )
            if build_requirement is not None:
                return self._rejected_claim_result(
                    claim,
                    code="BUILD_ATTEMPT_REQUIRED",
                    reason=(
                        "Build phase has no terminal build attempt receipt; no project "
                        "outcome can be claimed yet."
                    ),
                    control_disposition=GateControlDisposition.REPAIR_REQUIRED,
                    blocker_owner="unknown",
                    validated_facts={"build_attempt_requirement": build_requirement.to_metadata()},
                )
        # The §3.3 cap can make `success` unavailable, and the island rule below
        # exempts the claim the gate checks — so in that ONE state the two must
        # read one determination rather than contradict each other. The gate is
        # what knows it, so it runs first, but only in the state where the
        # question exists: the build phase, unsealed, with something actually
        # open in the ledger. The extra read is one glob `cat` (the same read
        # `_settle_before_grading` makes a moment later), and a refusal that
        # costs no physical probe stays free for every other run — a giving-up
        # claim with untried islands is still answered before any inspection.
        capped_outcome = None
        if phase == "build" and not sealed and gate is not None:
            capped_outcome = settlement_capped_outcome(gate.validated_facts)

        # Closure-by-giving-up may not abandon surveyed islands that were
        # never attempted (spec §3.4 island guarantee, named per §3.3). A `done`
        # claim of the outcome the gate validates is exempt: the physical gate
        # below checks it — which is `success` on settled books, and the capped
        # `partial` while an obligation is open.
        islands = untried_islands_requirement(
            self.run_evidence_state,
            self.orchestrator,
            phase=phase,
            signal=verb,
            outcome=claimed_outcome.value,
            capped_outcome=capped_outcome.value if capped_outcome is not None else None,
        )
        if islands is not None:
            return self._rejected_claim_result(
                claim,
                code="ISLAND_ATTEMPT_REQUIRED",
                reason=(
                    "Build closure cannot abandon surveyed coordinates that have no "
                    "attempt receipt. The judge records the missing coverage; the model "
                    "must choose the next action."
                ),
                control_disposition=GateControlDisposition.REPAIR_REQUIRED,
                blocker_owner="unknown",
                validated_facts={"untried_islands": islands.to_metadata()},
            )

        if gate is None:
            gate = self._grade(claim, phase, sealed=sealed)

        # A blocked record cannot carry the otherwise-valid pessimistic
        # ``blocked + success`` combination from the generic claim matrix. The
        # guard is on the physical evidence, not on the label: the §3.3 cap
        # rewrites a green phase's validated outcome to `partial` while a job is
        # open, which disarmed this guard exactly when the model was most likely
        # to reach for `blocked` — mid-dispatch, with a green build on disk.
        green_evidence = (
            gate.validated_outcome is PhaseOutcome.SUCCESS
            or settlement_capped_outcome(gate.validated_facts) is not None
        )
        if verb == "blocked" and green_evidence:
            gate = type(gate)(
                accepted=False,
                validated_outcome=gate.validated_outcome,
                claim_disposition=ClaimDisposition.CONTRADICTED,
                validator_state=gate.validator_state,
                reason=(
                    "blocked is reserved for external impediments, but the phase "
                    f"evidence shows a real green build ({gate.reason}). Any remaining "
                    "modules stay unresolved, and the terminal outcome is bounded by "
                    "their recorded evidence."
                ),
                evidence_refs=gate.evidence_refs,
                suggestions=gate.suggestions,
                code="blocked_contradicted_by_green_evidence",
                validated_facts=gate.validated_facts,
                claim=claim,
                control_disposition=gate.control_disposition,
                blocker_owner=gate.blocker_owner,
            )

        if not gate.accepted:
            control_disposition = GateControlDisposition(gate.control_disposition).value
            # A project/unknown repair belongs to the model.  The judge exposes
            # facts and a maximum supported outcome, never a preferred project
            # command or ordered next step.  Controller-owned dispositions may
            # retain mechanical recovery diagnostics for the engine.
            if gate.control_disposition is GateControlDisposition.REPAIR_REQUIRED:
                gate = replace(gate, suggestions=())
            return ToolResult.completed_failure(
                output=self._rejection_output(
                    f"Phase '{phase}' {verb}-claim rejected: {gate.reason}",
                    dict(gate.validated_facts),
                ),
                error=gate.reason or "phase claim contradicted by validator evidence",
                error_code=gate.code or "phase_claim_contradicted",
                suggestions=list(gate.suggestions),
                facts=dict(gate.validated_facts),
                metadata={
                    "control_disposition": control_disposition,
                    "blocker_owner": gate.blocker_owner.value,
                    "phase_claim": claim.to_metadata(),
                    "gate_result": gate.to_metadata(),
                },
            )

        control_disposition = GateControlDisposition(gate.control_disposition).value
        return ToolResult.completed_success(
            # The word the model reads and the word the record seals come out of
            # one renderer reading one object (spec §3.1).
            output=gate_observation_text(gate, phase=phase, origin="terminal_claim"),
            facts={"phase": phase},
            metadata={
                "control_disposition": control_disposition,
                "blocker_owner": gate.blocker_owner.value,
                "phase_signal": verb,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["done", "blocked", "note"],
                },
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
