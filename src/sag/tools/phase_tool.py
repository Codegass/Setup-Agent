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
    run_test_receipt_scope,
    run_test_receipts,
    terminal_test_receipts,
    test_closure_survey,
    untried_islands_requirement,
)
from sag.agent.document_map import read_live_document_map
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
    claim_identity,
    disclosed_live_job_ids,
    gate_observation_text,
    settlement_capped_outcome,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.project_execution_plan import (
    PROJECT_EXECUTION_PLAN_PATH,
    ProjectExecutionPlan,
    ProjectExecutionPlanValidationError,
    bind_project_execution_plan_inventory,
    canonical_authored_plan_sha256,
    seal_project_execution_plan,
    validate_authored_plan,
    validate_reviewed_document_evidence,
)

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
                "Analyze may state strategy in key_results. execution_plan is optional guidance; "
                "its validation warnings do not prevent bounded project execution or prove completion. "
                "Revise strategy using note. The engine alone routes phases and verifies actual results."
            ),
        )
        self.machine = machine
        self.validator = validator
        self.orchestrator = orchestrator
        self.project_name = project_name
        self.gate_fn = gate_fn
        self.run_evidence_state = run_evidence_state
        self._execution_plan_output_storage = None
        self._analysis_facts_recovery: Optional[Callable[[], Optional[str]]] = None
        self._analysis_facts_recovery_attempted = False

    def bind_analysis_facts_recovery(
        self,
        callback: Callable[[], Optional[str]],
    ) -> None:
        """Bind the controller's one-shot framework-survey recovery seam."""

        self._analysis_facts_recovery = callback

    def bind_execution_plan_evidence(self, output_storage: Any) -> None:
        """Bind the engine-owned store that resolves this run's output refs."""

        self._execution_plan_output_storage = output_storage

    def validate_execution_plan_evidence(
        self,
        plan: Any,
        *,
        source_attempt_id: str | None = None,
    ) -> ProjectExecutionPlan:
        """Bind claimed document reviews to this Analyze attempt's real reads."""

        state = self.run_evidence_state
        storage = self._execution_plan_output_storage
        observations = getattr(state, "tool_observations", ()) if state is not None else ()
        reader = getattr(storage, "retrieve_output", None) if storage is not None else None
        if not callable(reader):
            raise ProjectExecutionPlanValidationError(
                "document review evidence output reader is unavailable"
            )
        attempt_id = str(
            source_attempt_id or getattr(self.machine, "current_attempt_id", "") or ""
        ).strip()
        return validate_reviewed_document_evidence(
            plan,
            observations=observations,
            output_reader=reader,
            source_attempt_id=attempt_id,
        )

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
            disclosed_job_ids=disclosed_live_job_ids(self.run_evidence_state),
        )
        gate = gate if gate.claim is not None else gate.with_claim(claim)
        if (
            phase in {"build", "test"}
            and not sealed
            and self.run_evidence_state is not None
            and gate.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
            and gate.validator_state in {ValidatorState.GREEN, ValidatorState.PARTIAL}
        ):
            from sag.agent.acceptance_task import build_task_completion

            completion = build_task_completion(
                self.orchestrator,
                self.run_evidence_state,
                validator=self.validator,
                project_root=f"/workspace/{self.project_name}" if self.project_name else None,
                repository=getattr(self.orchestrator, "acceptance_task_repository", None),
                task=getattr(self.orchestrator, "acceptance_task", None),
                output_storage=self._execution_plan_output_storage,
            )
            if completion is not None:
                gate = replace(
                    gate,
                    validated_facts={
                        **dict(gate.validated_facts),
                        "task_completion": completion.model_dump(mode="json"),
                    },
                )
            # Build may hand off with usable compilation evidence even when a
            # required verify/package step failed or belongs to the next phase.
            # Disclose that task status without turning it into a routing gate.
            if phase == "test" and completion is not None and completion.status != "complete":
                # This bounds the completion claim, not the next executable
                # action. A truthful partial claim can still terminate.
                remaining = ", ".join(
                    f"{step.id}={step.status}"
                    for step in completion.steps
                    if step.status != "complete"
                ) or ", ".join(completion.reasons)
                return validate_phase_claim(
                    claim,
                    ValidatorState.PARTIAL,
                    reason=(
                        f"The required task has unfinished or unverified steps: {remaining}. "
                        "Ordinary tools remain available to continue the task; "
                        "an honest partial claim can also close this phase."
                    ),
                    evidence_refs=gate.evidence_refs,
                    code=f"required_task_{completion.status}",
                    validated_facts={
                        **dict(gate.validated_facts),
                        "physical_test_validator_state": gate.validator_state.value,
                        "task_completion": completion.model_dump(mode="json"),
                    },
                )
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
            disclosed_job_ids=disclosed_live_job_ids(self.run_evidence_state),
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

    def _test_receipt_scope_facts(self, required_attempt, resolved) -> Dict[str, Any]:
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

        ``resolved`` is the survey the REQUIREMENT was graded against, handed
        down by the caller. Re-probing it here asked the same question a second
        time and could have answered it differently.

        The run-wide count also states the directory it counted over. It was
        taken over all of ``/workspace`` unconditionally — this call passed no
        boundary at all — so a sibling checkout's receipt was reported to the
        model as this run's evidence. It is bounded now, and where the widest
        boundary is still the only one available the fact says which one it was
        rather than leaving a number that reads as narrower than it is.
        """
        scope = run_test_receipt_scope(
            self.run_evidence_state,
            project_root=resolved.project_root if resolved is not None else None,
        )
        facts: Dict[str, Any] = {
            "run_wide_test_receipts": len(run_test_receipts(self.run_evidence_state, scope=scope)),
            "run_wide_test_receipt_scope": scope.to_metadata(),
            "test_attempt_requirement": required_attempt.to_metadata(),
        }
        if resolved is not None and resolved.status == "available":
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

    def _prepare_execution_plan(
        self,
        plan: ProjectExecutionPlan,
        claim: PhaseClaim,
        document_map: Any,
    ) -> Dict[str, Any]:
        """Cross-check a model plan without publishing the authoritative file.

        The document map is an inventory and gap-checking input only.  The plan
        model also permits checkout-contained sources discovered outside that
        bounded map when the model cites a direct output observation.
        Publication remains engine-owned and happens only after the delivered
        phase gate is accepted.
        """

        attempt_id = str(getattr(self.machine, "current_attempt_id", "") or "").strip()
        artifact = seal_project_execution_plan(
            plan,
            source_attempt_id=attempt_id,
            claim_sha256=claim_identity(claim),
            document_map=document_map,
        )
        return {
            "authored_plan": artifact.plan.model_dump(mode="json"),
            "authored_plan_sha256": artifact.authored_plan_sha256,
            "document_map_fingerprint": artifact.document_map_fingerprint,
            "inventory_coverage": artifact.inventory_coverage.model_dump(mode="json"),
            "inventory_warnings": list(artifact.inventory_warnings),
        }

    def _execution_plan_document_map(self) -> Any:
        """Read the optional live inventory used only for binding and gap audit."""

        document_map = None
        try:
            observed = read_live_document_map(self.orchestrator)
            if observed.complete and observed.conflict is None:
                document_map = observed.payload
        except Exception:
            # The ordinary Analyze gate owns survey/map availability.  Plan
            # validation can still bind map-external sources to direct refs;
            # it must not invent a replacement inventory.
            document_map = None
        return document_map

    def execute(
        self,
        action: str,
        outcome: Optional[str] = None,
        key_results: str = "",
        reason: str = "",
        evidence: Optional[List[str]] = None,
        text: str = "",
        execution_plan: Optional[Dict[str, Any]] = None,
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
            if execution_plan is not None:
                return ToolResult.completed_failure(
                    output="note does not accept an execution plan",
                    error="execution_plan is forbidden for note",
                    error_code="ANALYSIS_EXECUTION_PLAN_FORBIDDEN",
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

        # Strategy is advisory. Invalid guidance cannot authorize an action or
        # certify a result, but must not prevent a surveyed project being tried.
        normalized_plan = None
        plan_seal_candidate = None
        plan_candidate = None
        plan_warning = None
        plan_sha256 = ""
        plan_document_map = None
        if execution_plan is not None:
            if phase != "analyze" or verb != "done":
                return ToolResult.completed_failure(
                    output="Structured execution_plan is accepted only by Analyze done; use phase note to revise strategy later.",
                    error="execution_plan is not valid for this phase action",
                    error_code="ANALYSIS_EXECUTION_PLAN_FORBIDDEN",
                )
            try:
                normalized_plan = validate_authored_plan(
                    execution_plan, require_test_disposition=True
                )
                normalized_plan = self.validate_execution_plan_evidence(normalized_plan)
                plan_seal_candidate = normalized_plan
                plan_document_map = self._execution_plan_document_map()
                normalized_plan = bind_project_execution_plan_inventory(
                    normalized_plan, plan_document_map
                )
                plan_sha256 = canonical_authored_plan_sha256(normalized_plan)
            except ProjectExecutionPlanValidationError as exc:
                plan_warning = str(exc)[:1000]
                normalized_plan = None

        claim = PhaseClaim(
            phase=phase,
            signal=verb,
            claimed_outcome=claimed_outcome,
            key_results=key_results,
            reason=reason,
            evidence_refs=tuple(evidence or ()),
            execution_plan_sha256=plan_sha256,
            execution_plan_ref=(PROJECT_EXECUTION_PLAN_PATH if plan_sha256 else ""),
        )
        if normalized_plan is not None:
            try:
                plan_candidate = self._prepare_execution_plan(
                    plan_seal_candidate, claim, plan_document_map
                )
            except ProjectExecutionPlanValidationError as exc:
                plan_warning = str(exc)[:1000]
                plan_sha256 = ""
                claim = replace(claim, execution_plan_sha256="", execution_plan_ref="")

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

        # One question, one computation: the survey is read here at most once
        # (never for a phase that does not ask the test-closure question) and
        # the same resolution grades the requirement and the facts beside it.
        survey = test_closure_survey(self.run_evidence_state, self.orchestrator, phase=phase)
        required_attempt = required_test_attempt(
            self.run_evidence_state,
            self.orchestrator,
            phase=phase,
            attempt_id=getattr(self.machine, "current_attempt_id", None),
            resolution=survey,
            validator=self.validator,
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
                validated_facts=self._test_receipt_scope_facts(required_attempt, survey),
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
                    "blocked is reserved for external impediments, but the "
                    f"{phase} evidence is green ({gate.reason}). The terminal outcome is bounded "
                    "by the recorded evidence for the remaining scope."
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

        if plan_candidate is not None:
            gate = replace(
                gate,
                validated_facts={
                    **dict(gate.validated_facts),
                    "analysis.execution_plan_candidate_valid": True,
                    "analysis.execution_plan_sha256": plan_sha256,
                },
            )

        control_disposition = GateControlDisposition(gate.control_disposition).value
        return ToolResult.completed_success(
            # The word the model reads and the word the record seals come out of
            # one renderer reading one object (spec §3.1).
            output=gate_observation_text(gate, phase=phase, origin="terminal_claim")
            + (
                "\n[plan advisory] Structured plan was not sealed: "
                + plan_warning
                + ". Continue from task requirements; revise strategy with phase note. Completion still requires physical evidence."
                if plan_warning
                else ""
            ),
            facts={"phase": phase},
            metadata={
                "control_disposition": control_disposition,
                "blocker_owner": gate.blocker_owner.value,
                "phase_signal": verb,
                "plan_warning": plan_warning,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
                **(
                    {"execution_plan_candidate": plan_candidate}
                    if plan_candidate is not None
                    else {}
                ),
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
                "execution_plan": {
                    "type": "object",
                    "description": (
                        "Optional structured guidance for Analyze done. key_results may state the strategy instead. "
                        "Validation problems are advisory; use phase note to revise strategy later. "
                        "Your model-authored, "
                        "evidence-linked project build/test strategy. Harness inventory "
                        "and extracted claims are hints, not commands."
                    ),
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string", "maxLength": 2000},
                        "documents_reviewed": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 24,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "path": {"type": "string", "maxLength": 2048},
                                    "reason": {"type": "string", "maxLength": 1000},
                                    "evidence_refs": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 12,
                                        "items": {"type": "string", "maxLength": 512},
                                        "description": (
                                            "Include at least one readable output_* produced "
                                            "by a current Analyze call that names this exact "
                                            "path. If a claim is rejected, the error lists the "
                                            "exact valid refs already observed for the path."
                                        ),
                                    },
                                    "entry_id": {
                                        "type": "string",
                                        "maxLength": 128,
                                        "description": (
                                            "Optional inventory hint; normally omit it because "
                                            "the Harness derives the authoritative binding."
                                        ),
                                    },
                                    "source_hash": {
                                        "type": "string",
                                        "maxLength": 128,
                                        "description": (
                                            "Optional inventory hint; normally omit it because "
                                            "the Harness derives the authoritative binding."
                                        ),
                                    },
                                },
                                "required": ["path", "reason", "evidence_refs"],
                            },
                        },
                        "test_disposition": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "status": {
                                    "type": "string",
                                    "enum": ["planned", "blocked"],
                                },
                                "reason": {"type": "string", "maxLength": 1000},
                                "execution_mechanism": {
                                    "type": "string",
                                    "maxLength": 1000,
                                    "description": (
                                        "What the exact selected entry dispatches according to "
                                        "its reviewed definition, including the runner/plugin/task."
                                    ),
                                },
                                "verdict_scope": {
                                    "type": "string",
                                    "enum": [
                                        "product_test_cases",
                                        "test_metadata",
                                        "quality_only",
                                        "benchmark_or_manual",
                                        "unknown",
                                    ],
                                    "description": (
                                        "Classify what the entry's reviewed definition actually "
                                        "executes. Unit, integration, and system behavior cases "
                                        "are product_test_cases; inventory/suite-membership checks "
                                        "are test_metadata."
                                    ),
                                },
                                "readiness": {
                                    "type": "string",
                                    "enum": ["ready", "blocked", "unknown"],
                                    "description": (
                                        "Whether required services, checkouts, coordination, "
                                        "runner support, and durable reports are established."
                                    ),
                                },
                                "evidence_refs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 12,
                                    "items": {"type": "string", "maxLength": 512},
                                    "description": (
                                        "Cite reviewed-document output refs supporting the "
                                        "planned entry or concrete blocker."
                                    ),
                                },
                                "definition_evidence_refs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 12,
                                    "items": {"type": "string", "maxLength": 512},
                                    "description": (
                                        "Cite the reviewed output that shows the actual definition "
                                        "or dispatch mechanism of the selected entry."
                                    ),
                                },
                            },
                            "required": [
                                "status",
                                "reason",
                                "execution_mechanism",
                                "verdict_scope",
                                "readiness",
                                "evidence_refs",
                                "definition_evidence_refs",
                            ],
                        },
                        **{
                            lane: {
                                "type": "array",
                                "minItems": (0 if lane == "test_steps" else 1),
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "tool": {"type": "string", "maxLength": 64},
                                        "params": {
                                            "type": "object",
                                            "description": (
                                                "Exact public tool parameters with explicit working_directory. "
                                                "Build execution steps require system and source_command matching action/args. "
                                                "For Maven, args is source_command after removing the runner and one action goal; "
                                                "preserve every other goal and flag in order. test_steps may use action=verify. "
                                                "e.g. action=install, system=maven, args='clean -DskipTests', "
                                                "source_command='./mvnw clean install -DskipTests'. "
                                                "Keep wrapper setup and producer package/install as preceding steps. "
                                                "For Make targets inspect the recipe, then use explicit setup plus the actual "
                                                "pytest command/cwd; do not pass the Make target name to Gradle or pytest. "
                                                "deps/native describe project-declared setup and do not require source_command."
                                            ),
                                        },
                                        "purpose": {"type": "string", "maxLength": 1000},
                                        "evidence_refs": {
                                            "type": "array",
                                            "minItems": 0,
                                            "maxItems": 12,
                                            "items": {"type": "string", "maxLength": 512},
                                        },
                                    },
                                    "required": ["tool", "params", "purpose"],
                                },
                            }
                            for lane in ("build_steps", "test_steps")
                        },
                        **{
                            name: {
                                "type": "array",
                                "minItems": (1 if "success_criteria" in name else 0),
                                "maxItems": 16,
                                "items": {"type": "string", "maxLength": 1000},
                            }
                            for name in (
                                "build_success_criteria",
                                "test_success_criteria",
                                "environment_constraints",
                                "risks",
                                "unresolved_questions",
                            )
                        },
                    },
                    "required": [
                        "summary",
                        "documents_reviewed",
                        "build_steps",
                        "build_success_criteria",
                        "test_disposition",
                        "test_steps",
                        "test_success_criteria",
                    ],
                },
            },
            "required": ["action"],
        }
