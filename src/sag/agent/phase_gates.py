"""Evidence-backed validation for model-authored phase outcome claims.

The validator describes evidence.  It never mutates ``PhaseMachine`` and never
selects the next phase; routing belongs to ``PhaseTransitionPolicy``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Optional

from loguru import logger

from .phase_machine import PhaseClaim, PhaseOutcome


class ValidatorState(str, Enum):
    GREEN = "green"
    PARTIAL = "partial"
    RED = "red"
    UNAVAILABLE = "unavailable"


class ClaimDisposition(str, Enum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    PESSIMISTIC = "pessimistic"
    UNVERIFIABLE = "unverifiable"
    REFINED = "refined"


_VALIDATED_OUTCOMES = {
    ValidatorState.GREEN: PhaseOutcome.SUCCESS,
    ValidatorState.PARTIAL: PhaseOutcome.PARTIAL,
    ValidatorState.RED: PhaseOutcome.FAILED,
    ValidatorState.UNAVAILABLE: PhaseOutcome.UNKNOWN,
}

_OUTCOME_RANK = {
    PhaseOutcome.FAILED: 0,
    PhaseOutcome.PARTIAL: 1,
    PhaseOutcome.SUCCESS: 2,
}


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    validated_outcome: PhaseOutcome | str
    claim_disposition: ClaimDisposition | str
    validator_state: ValidatorState | str
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    code: str = ""
    claim: PhaseClaim | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "validated_outcome", PhaseOutcome(self.validated_outcome))
        object.__setattr__(self, "claim_disposition", ClaimDisposition(self.claim_disposition))
        object.__setattr__(self, "validator_state", ValidatorState(self.validator_state))
        object.__setattr__(self, "evidence_refs", tuple(dict.fromkeys(self.evidence_refs)))
        object.__setattr__(self, "suggestions", tuple(self.suggestions))

    @property
    def disposition(self) -> ClaimDisposition:
        return self.claim_disposition

    def with_claim(self, claim: PhaseClaim) -> "GateResult":
        if self.claim is not None and self.claim != claim:
            raise ValueError("gate result already belongs to a different phase claim")
        return replace(self, claim=claim)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "validated_outcome": self.validated_outcome.value,
            "claim_disposition": self.claim_disposition.value,
            "validator_state": self.validator_state.value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "suggestions": list(self.suggestions),
            "code": self.code,
        }

    @classmethod
    def from_metadata(
        cls,
        value: dict[str, Any],
        *,
        claim: PhaseClaim | None = None,
    ) -> "GateResult":
        return cls(
            accepted=bool(value.get("accepted")),
            validated_outcome=value.get("validated_outcome", PhaseOutcome.UNKNOWN),
            claim_disposition=value.get(
                "claim_disposition", ClaimDisposition.UNVERIFIABLE
            ),
            validator_state=value.get("validator_state", ValidatorState.UNAVAILABLE),
            reason=str(value.get("reason") or ""),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
            suggestions=tuple(value.get("suggestions") or ()),
            code=str(value.get("code") or ""),
            claim=claim,
        )


@dataclass(frozen=True)
class _ValidatorObservation:
    state: ValidatorState
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    code: str = ""


def validate_phase_claim(
    claim: PhaseClaim | PhaseOutcome | str,
    validator_state: ValidatorState | str,
    *,
    reason: str = "",
    evidence_refs: Iterable[str] = (),
    suggestions: Iterable[str] = (),
    code: str = "",
) -> GateResult:
    """Compare a claim with validator evidence without routing or mutation."""
    state = ValidatorState(validator_state)
    if isinstance(claim, PhaseClaim):
        phase_claim = claim
    else:
        phase_claim = PhaseClaim(phase="", claimed_outcome=PhaseOutcome(claim))
    claimed = phase_claim.claimed_outcome
    validated = _VALIDATED_OUTCOMES[state]

    if claimed is PhaseOutcome.UNKNOWN:
        disposition = (
            ClaimDisposition.CONFIRMED
            if validated is PhaseOutcome.UNKNOWN
            else ClaimDisposition.REFINED
        )
        accepted = True
    elif validated is PhaseOutcome.UNKNOWN:
        if claimed is PhaseOutcome.SUCCESS:
            disposition = ClaimDisposition.CONTRADICTED
            accepted = False
        else:
            disposition = ClaimDisposition.UNVERIFIABLE
            accepted = True
    elif claimed is validated:
        disposition = ClaimDisposition.CONFIRMED
        accepted = True
    elif _OUTCOME_RANK[claimed] < _OUTCOME_RANK[validated]:
        disposition = ClaimDisposition.PESSIMISTIC
        accepted = True
    else:
        disposition = ClaimDisposition.CONTRADICTED
        accepted = False

    return GateResult(
        accepted=accepted,
        validated_outcome=validated,
        claim_disposition=disposition,
        validator_state=state,
        reason=reason,
        evidence_refs=tuple(evidence_refs),
        suggestions=tuple(suggestions),
        code=code,
        claim=phase_claim,
    )


def check_phase_claim(
    phase: str,
    claim: PhaseClaim,
    validator,
    orchestrator,
    project_name: Optional[str],
) -> GateResult:
    """Inspect physical evidence and validate one terminal phase claim."""
    if claim.phase != phase:
        raise ValueError(f"claim for {claim.phase!r} cannot validate phase {phase!r}")
    observation = _inspect_phase(phase, validator, orchestrator, project_name)
    return validate_phase_claim(
        claim,
        observation.state,
        reason=observation.reason,
        evidence_refs=observation.evidence_refs,
        suggestions=observation.suggestions,
        code=observation.code,
    )


def check_phase_done(
    phase: str,
    validator,
    orchestrator,
    project_name: Optional[str],
) -> dict[str, Any]:
    """Read-only compatibility projection for engine nudges during WS3.

    Live model claims use :func:`check_phase_claim`; this adapter carries no
    claim and therefore cannot close or advance a phase.
    """
    observation = _inspect_phase(phase, validator, orchestrator, project_name)
    return {
        "ok": observation.state is ValidatorState.GREEN,
        "reason": observation.reason,
        "suggestions": list(observation.suggestions),
        "validator_state": observation.state.value,
        "evidence_refs": list(observation.evidence_refs),
        "code": observation.code,
    }


def _inspect_phase(phase, validator, orchestrator, project_name) -> _ValidatorObservation:
    try:
        if phase == "provision":
            return _inspect_provision(orchestrator, project_name)
        if phase == "analyze":
            return _inspect_analyze(validator, project_name)
        if phase == "build":
            return _inspect_build(validator, project_name)
        if phase == "test":
            return _inspect_test(validator, project_name)
        if phase == "report":
            return _inspect_report(orchestrator)
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason=f"unknown phase: {phase}",
            code="unknown_phase",
        )
    except Exception as exc:
        logger.warning(f"Phase gate '{phase}' evidence unavailable (probe error): {exc}")
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason=f"validator probe unavailable: {exc}",
            code="validator_unavailable",
        )


def _inspect_provision(orchestrator, project_name) -> _ValidatorObservation:
    if orchestrator is None:
        raise RuntimeError("no orchestrator available")
    workdir = f"/workspace/{project_name}" if project_name else "/workspace"
    probe = orchestrator.execute_command(
        f"test -d {shlex.quote(workdir)} && echo exists || echo missing",
        workdir=None,
        timeout=30,
    )
    if "exists" not in (probe.get("output") or ""):
        return _ValidatorObservation(
            ValidatorState.RED,
            reason=f"workspace {workdir} does not exist — repository not cloned",
            evidence_refs=(workdir,),
            suggestions=(
                "Clone first: project(action='clone', repo_url=...)",
                "If the repo cloned elsewhere, verify with bash ls /workspace",
            ),
            code="workspace_missing",
        )
    return _ValidatorObservation(
        ValidatorState.GREEN,
        reason=f"workspace {workdir} exists",
        evidence_refs=(workdir,),
        code="workspace_present",
    )


def _state_from_evidence_status(value: Any) -> ValidatorState:
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "green", "verified"}:
        return ValidatorState.GREEN
    if normalized in {"partial", "warning"}:
        return ValidatorState.PARTIAL
    if normalized in {"blocked", "failed", "red", "conflict"}:
        return ValidatorState.RED
    return ValidatorState.UNAVAILABLE


def _status_refs(status: dict[str, Any]) -> tuple[str, ...]:
    explicit = status.get("evidence_refs") or status.get("report_files") or ()
    if isinstance(explicit, str):
        explicit = (explicit,)
    evidence = status.get("evidence") or {}
    samples = (evidence.get("artifact_samples") or ()) if isinstance(evidence, dict) else ()
    return tuple(dict.fromkeys(str(ref) for ref in (*explicit, *samples) if ref))


def _inspect_analyze(validator, project_name) -> _ValidatorObservation:
    method = getattr(validator, "validate_project_analysis_status", None)
    if method is None:
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="project analysis evidence is unavailable",
            code="analysis_unavailable",
        )
    status = method(project_name)
    state = _state_from_evidence_status(
        status.get("evidence_status") or status.get("status")
    )
    if state is ValidatorState.UNAVAILABLE:
        if status.get("analyzed") and status.get("has_static_test_count"):
            state = ValidatorState.GREEN
        elif status.get("analyzed"):
            state = ValidatorState.PARTIAL
        elif status.get("success") is True:
            state = ValidatorState.GREEN
        elif status.get("success") is False or status.get("missing_analysis_prompt"):
            state = ValidatorState.RED
    return _ValidatorObservation(
        state,
        reason=(
            status.get("reason")
            or status.get("missing_analysis_prompt")
            or "project analysis validator returned no conclusion"
        ),
        evidence_refs=_status_refs(status),
        code=f"analysis_{state.value}",
    )


def _inspect_build(validator, project_name) -> _ValidatorObservation:
    if validator is None:
        raise RuntimeError("no physical validator available")
    status = validator.validate_build_status(project_name)
    state = _state_from_evidence_status(status.get("evidence_status"))
    if state is ValidatorState.UNAVAILABLE:
        if status.get("success") and status.get("build_complete", True):
            state = ValidatorState.GREEN
        elif status.get("success"):
            state = ValidatorState.PARTIAL
        elif status.get("success") is False:
            state = ValidatorState.RED
    reason = status.get("reason") or "build validator returned no conclusion"
    suggestions = ()
    if state is not ValidatorState.GREEN:
        suggestions = (
            "Run build(action='compile') and validate the resulting artifacts",
            "If an external impediment prevents progress, claim blocked with its evidence refs",
        )
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=f"build_{state.value}",
    )


def _inspect_test(validator, project_name) -> _ValidatorObservation:
    if validator is None:
        raise RuntimeError("no physical validator available")
    status = validator.validate_test_status(project_name)
    test_stats = status.get("test_stats") or {}
    executed = int(test_stats.get("executed", status.get("total_tests", 0)) or 0)
    discovered = test_stats.get("discovered", status.get("static_test_count"))
    discovered = int(discovered or 0)
    errors = int(status.get("error_tests", 0) or 0)
    total = int(status.get("total_tests", executed) or 0)

    if errors == total and total > 0:
        state = ValidatorState.RED
        code = "test_collection_failed"
    elif discovered > 0 and executed == 0:
        state = ValidatorState.RED
        code = "tests_not_executed"
    else:
        state = _state_from_evidence_status(
            status.get("evidence_status") or status.get("status")
        )
        code = f"test_{state.value}"

    if state is ValidatorState.UNAVAILABLE and not status.get("has_test_reports"):
        detail = str(status.get("reason") or "").strip()
        reason = "no test reports or execution evidence available"
        if detail:
            reason = f"{reason}: {detail}"
    else:
        reason = status.get("reason") or "test validator returned no conclusion"
    suggestions = ()
    if state is not ValidatorState.GREEN:
        suggestions = (
            "Run build(action='test') and preserve the generated test reports",
            "If an external impediment prevents tests, claim blocked with evidence refs",
        )
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=code,
    )


def _inspect_report(orchestrator) -> _ValidatorObservation:
    if orchestrator is None:
        raise RuntimeError("no orchestrator available")
    probe = orchestrator.execute_command(
        "find /workspace -maxdepth 1 -name 'setup-report-*.md' | head -1",
        workdir=None,
        timeout=30,
    )
    report_ref = (probe.get("output") or "").strip()
    if not report_ref:
        return _ValidatorObservation(
            ValidatorState.RED,
            reason="report phase has no setup-report-*.md artifact",
            suggestions=("Generate it with the report tool, then re-claim",),
            code="report_missing",
        )
    return _ValidatorObservation(
        ValidatorState.GREEN,
        reason="report artifact exists",
        evidence_refs=(report_ref,),
        code="report_present",
    )
