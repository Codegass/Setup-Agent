"""Evidence-backed validation for model-authored phase outcome claims.

The validator describes evidence.  It never mutates ``PhaseMachine`` and never
selects the next phase; routing belongs to ``PhaseTransitionPolicy``.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from loguru import logger

from .invocation_receipts import RECEIPT_DIR
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

# Plan 5 Stage C (P0-F). The one validator state each terminal claim maps to,
# so an upgrade can be capped AT the claim without inventing a state the
# claim/outcome invariant would reject.
_CLAIM_VALIDATOR_STATE = {
    PhaseOutcome.FAILED: ValidatorState.RED,
    PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
    PhaseOutcome.SUCCESS: ValidatorState.GREEN,
}

# Domain rollup contract (plan §"Binding notes (Stage C)"): the sealed value is
# ``{"<root>": {"state": ..., "blocker": "<detail>"?}}``. A domain in any of
# these states has NOT closed, so no gate may refine a claim upward past it.
_UNCLOSED_DOMAIN_STATES = frozenset({"failed", "blocked", "untried"})

# Append-only evidence assessments (Plan 6 Stage 0, spec §C4). Lane z1 owns the
# writer; this is the documented cross-lane storage path — the sibling of
# ``RECEIPT_DIR`` under the same ``.setup_agent`` root, one single-line JSON
# file per assessment, named by its ``assessment_id``.
ASSESSMENT_DIR = "/workspace/.setup_agent/evidence_assessments"

# Receipt/assessment schema versions this reader understands. v1 wrote no
# ``schema_version`` guarantee beyond the constant 1 and v2 only ADDS keys, so
# both derive identically; an unknown FUTURE version is skipped rather than
# coerced (spec §C4: no silent coercion).
_SUPPORTED_RECORD_VERSIONS = frozenset({1, 2})

# Failure-class typed codes (spec §C4/§C5). ONLY these turn a receipt
# semantically failed, overriding its own exit 0. Deliberately small and
# explicit: per §C5 a mismatch is not automatically a contradiction, so
# unknown/blocked/diagnostic codes leave the raw exit standing and a typed code
# nobody writes yet cannot silently acquire failure authority.
#   compile_no_source_mismatch — the gradle NO-SOURCE downgrade migrated off
#       the deleted ``mark_semantic_failure`` receipt rewrite.
#   semantic_failure — the generic successor of that same field, for any runner
#       that condemns its own zero-exit invocation.
_FAILURE_CLASS_ASSESSMENT_CODES = frozenset(
    {
        "compile_no_source_mismatch",
        "semantic_failure",
    }
)

_ANALYSIS_STATUS_PROJECTIONS = {
    "analysis_trunk_missing": (
        "Project survey facts are not persisted on the trunk.",
        ("Run project(action='analyze') before closing the analyze phase.",),
    ),
    "analysis_static_count_missing": (
        "Project survey facts exist, but no static test-count fact was observed.",
        (
            "Continue as partial when the project has no observable static denominator, "
            "or rerun project(action='analyze') after the checkout changes.",
        ),
    ),
    "analysis_facts_missing": (
        "No persisted project survey facts were observed.",
        ("Run project(action='analyze') before closing the analyze phase.",),
    ),
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
    validated_facts: Mapping[str, Any] = field(default_factory=dict)
    claim: PhaseClaim | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("gate accepted flag must be boolean")
        validated_outcome = PhaseOutcome(self.validated_outcome)
        claim_disposition = ClaimDisposition(self.claim_disposition)
        validator_state = ValidatorState(self.validator_state)
        object.__setattr__(self, "validated_outcome", validated_outcome)
        object.__setattr__(self, "claim_disposition", claim_disposition)
        object.__setattr__(self, "validator_state", validator_state)
        if validated_outcome is not _VALIDATED_OUTCOMES[validator_state]:
            raise ValueError("validated outcome must match the validator state")
        expected_accepted = claim_disposition is not ClaimDisposition.CONTRADICTED
        if self.accepted is not expected_accepted:
            raise ValueError("gate acceptance conflicts with the claim disposition")
        object.__setattr__(self, "evidence_refs", tuple(dict.fromkeys(self.evidence_refs)))
        object.__setattr__(self, "suggestions", tuple(self.suggestions))
        if not isinstance(self.validated_facts, Mapping):
            raise TypeError("validated facts must be a mapping")
        object.__setattr__(self, "validated_facts", dict(self.validated_facts))

    @property
    def disposition(self) -> ClaimDisposition:
        return ClaimDisposition(self.claim_disposition)

    def with_claim(self, claim: PhaseClaim) -> "GateResult":
        if self.claim is not None and self.claim != claim:
            raise ValueError("gate result already belongs to a different phase claim")
        return replace(self, claim=claim)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "validated_outcome": PhaseOutcome(self.validated_outcome).value,
            "claim_disposition": ClaimDisposition(self.claim_disposition).value,
            "validator_state": ValidatorState(self.validator_state).value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "suggestions": list(self.suggestions),
            "code": self.code,
            "validated_facts": dict(self.validated_facts),
        }

    @classmethod
    def from_metadata(
        cls,
        value: dict[str, Any],
        *,
        claim: PhaseClaim | None = None,
    ) -> "GateResult":
        if not isinstance(value, Mapping):
            raise TypeError("gate metadata must be a mapping")
        accepted = value.get("accepted")
        if not isinstance(accepted, bool):
            raise TypeError("gate metadata accepted flag must be boolean")
        facts = value.get("validated_facts") or {}
        if not isinstance(facts, Mapping):
            raise TypeError("gate metadata validated facts must be a mapping")
        return cls(
            accepted=accepted,
            validated_outcome=value.get("validated_outcome", PhaseOutcome.UNKNOWN),
            claim_disposition=value.get("claim_disposition", ClaimDisposition.UNVERIFIABLE),
            validator_state=value.get("validator_state", ValidatorState.UNAVAILABLE),
            reason=str(value.get("reason") or ""),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
            suggestions=tuple(value.get("suggestions") or ()),
            code=str(value.get("code") or ""),
            validated_facts=dict(facts),
            claim=claim,
        )


def _unclosed_domains(validated_facts: Mapping[str, Any]) -> tuple[str, ...]:
    """``<root>=<state>`` for every surveyed domain that has not closed.

    Reads the rollup the gate sealed: the build fact first, the test rollup as
    the fallback for a run that only reached the test gate. No surveyed
    domains means an empty tuple, which is the single-domain (cli/tvm) path and
    keeps the claim ladder byte-identical to its pre-Stage-C behavior.
    """
    states = validated_facts.get("build.domain_states")
    if not isinstance(states, Mapping):
        rollup = validated_facts.get("test.stats")
        states = rollup.get("domain_states") if isinstance(rollup, Mapping) else None
    if not isinstance(states, Mapping):
        return ()
    unclosed: list[str] = []
    for root, entry in states.items():
        raw = entry.get("state") if isinstance(entry, Mapping) else entry
        state = str(raw or "").strip().lower()
        if state in _UNCLOSED_DOMAIN_STATES:
            unclosed.append(f"{root}={state}")
    return tuple(unclosed)


@dataclass(frozen=True)
class _ValidatorObservation:
    state: ValidatorState
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    code: str = ""
    validated_facts: Mapping[str, Any] = field(default_factory=dict)


def validate_phase_claim(
    claim: PhaseClaim | PhaseOutcome | str,
    validator_state: ValidatorState | str,
    *,
    reason: str = "",
    evidence_refs: Iterable[str] = (),
    suggestions: Iterable[str] = (),
    code: str = "",
    validated_facts: Mapping[str, Any] | None = None,
) -> GateResult:
    """Compare a claim with validator evidence without routing or mutation."""
    state = ValidatorState(validator_state)
    if isinstance(claim, PhaseClaim):
        phase_claim = claim
    else:
        phase_claim = PhaseClaim(phase="", claimed_outcome=PhaseOutcome(claim))
    claimed = PhaseOutcome(phase_claim.claimed_outcome)
    validated = _VALIDATED_OUTCOMES[state]

    # Domain truth table (Plan 5 Stage C, P0-F). Ground-truth review 2026-07-26
    # (§"Partial claims are upgraded"): a global artifact-presence check turned
    # Bigtop's truthful 2/4 partial into validated success, and the per-domain
    # facts were gone before sealing. While ANY surveyed domain is
    # failed/blocked/untried the validated outcome may confirm or downgrade the
    # claim, never refine it upward — a classified blocker is not a green
    # waiver. The cap stops AT the claim: it never manufactures a contradiction
    # the physical oracle did not observe.
    facts = dict(validated_facts or {})
    blocking_domains = _unclosed_domains(facts)
    if (
        blocking_domains
        and claimed in _OUTCOME_RANK
        and validated in _OUTCOME_RANK
        and _OUTCOME_RANK[claimed] < _OUTCOME_RANK[validated]
    ):
        state = _CLAIM_VALIDATOR_STATE[claimed]
        validated = _VALIDATED_OUTCOMES[state]
        reason = " · ".join(
            part
            for part in (
                reason,
                "no refinement above the claim while surveyed build domains are "
                f"unclosed: {', '.join(blocking_domains)}",
            )
            if part
        )

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
        validated_facts=facts,
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
        validated_facts=observation.validated_facts,
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
        "validated_facts": dict(observation.validated_facts),
    }


def _inspect_phase(phase, validator, orchestrator, project_name) -> _ValidatorObservation:
    try:
        if phase == "provision":
            return _inspect_provision(orchestrator, project_name)
        if phase == "analyze":
            return _inspect_analyze(validator, project_name)
        if phase == "build":
            return _inspect_build(validator, project_name, orchestrator=orchestrator)
        if phase == "test":
            return _inspect_test(validator, project_name, orchestrator=orchestrator)
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
            validated_facts={"provision.workspace_ready": False},
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
        validated_facts={"provision.workspace_ready": True},
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


def _first_nonnegative_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _validated_test_rollup(status: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a physical-validator result onto the sealed snapshot basis.

    The gate is the last read-only physical scan before evidence-close.  Its
    identity-aware raw/unique rollup is therefore authoritative over an actor
    summary or a backend result that could not parse Maven's console totals.
    """
    supplied = status.get("test_stats")
    test_stats = supplied if isinstance(supplied, Mapping) else {}
    unique_errors = _first_nonnegative_int(
        status.get("unique_error_tests"),
        status.get("error_tests"),
        0,
    )
    unique_failed = _first_nonnegative_int(
        status.get("unique_failed_tests"),
        status.get("failed_tests"),
    )
    if unique_failed is None:
        combined = _first_nonnegative_int(test_stats.get("failed"), 0) or 0
        unique_failed = max(combined - (unique_errors or 0), 0)
    unique = {
        "executed": _first_nonnegative_int(
            status.get("unique_tests"),
            test_stats.get("executed"),
            status.get("total_tests"),
            0,
        )
        or 0,
        "passed": _first_nonnegative_int(
            status.get("unique_passed_tests"),
            test_stats.get("passed"),
            status.get("passed_tests"),
            0,
        )
        or 0,
        "failed": unique_failed,
        "errors": unique_errors or 0,
        "skipped": _first_nonnegative_int(
            status.get("unique_skipped_tests"),
            test_stats.get("skipped"),
            status.get("skipped_tests"),
            0,
        )
        or 0,
    }
    if not status.get("has_test_reports") and unique["executed"] == 0:
        return None

    raw = {
        "executed": _first_nonnegative_int(status.get("raw_total_tests"), unique["executed"]) or 0,
        "passed": _first_nonnegative_int(status.get("raw_passed_tests"), unique["passed"]) or 0,
        "failed": _first_nonnegative_int(status.get("raw_failed_tests"), unique["failed"]) or 0,
        "errors": _first_nonnegative_int(status.get("raw_error_tests"), unique["errors"]) or 0,
        "skipped": _first_nonnegative_int(status.get("raw_skipped_tests"), unique["skipped"]) or 0,
    }
    conflicts = list(status.get("conflicts") or ())
    conflicts.extend(status.get("metrics_conflicts") or ())
    if unique["failed"]:
        conflicts.append("test_failures_detected")
    if unique["errors"]:
        conflicts.append("test_errors_detected")
    if status.get("parsing_errors"):
        conflicts.append("test_report_parse_error")
    if status.get("stale_test_reports"):
        conflicts.append("test_reports_stale")
    collection_summary = str(
        status.get("collection_error_summary") or test_stats.get("collection_error_summary") or ""
    ).strip()
    return {
        "discovered": _first_nonnegative_int(
            test_stats.get("discovered"), status.get("static_test_count")
        ),
        "unique": unique,
        "raw": raw,
        "flaky_count": _first_nonnegative_int(status.get("flaky_count"), 0) or 0,
        "conflicts": list(dict.fromkeys(str(item) for item in conflicts if item)),
        # Plan 4 audit fix: collection facts must survive into the sealed
        # snapshot — dropping them here recreated the projection failure the
        # 2026-07-26 audit diagnosed. Absent facts stay absent keys so
        # pre-Plan-4 rollup shapes (and their exact-dict tests) are unchanged.
        **{
            key: value
            for key, value in {
                "collection_errors": _first_nonnegative_int(
                    status.get("collection_errors"), test_stats.get("collection_errors")
                ),
                "collection_errors_skipped": _first_nonnegative_int(
                    status.get("collection_errors_skipped"),
                    test_stats.get("collection_errors_skipped"),
                ),
                "collection_error_summary": collection_summary or None,
                # Plan 5 Task B2: the receipt-scoped basis travels WITH the
                # counts it produced. Auxiliary reports stay visible next to
                # the primary numerator without ever entering it, and stale
                # (superseded) reports are named rather than silently dropped.
                # A receipt-free run emits none of these keys, so recorded
                # replay fixtures serialize byte-identically.
                "receipt_scoped": True if status.get("receipt_scoped") else None,
                "auxiliary_test_stats": _auxiliary_counts(status.get("auxiliary_test_stats")),
                "stale_test_reports": (
                    [str(item) for item in status.get("stale_test_reports") or ()] or None
                ),
            }.items()
            if value is not None
        },
    }


def _auxiliary_counts(value: Any) -> dict[str, int] | None:
    """Auxiliary reports in the primary rollup's count shape, never merged.

    Present only when the validator observed auxiliary reports at all; an
    all-zero block still counts as observed ("reports existed, no tests ran").
    """
    if not isinstance(value, Mapping) or not value:
        return None
    return {
        name: _first_nonnegative_int(value.get(name), 0) or 0
        for name in ("executed", "passed", "failed", "errors", "skipped")
    }


def _normalized_domain_root(value: Any) -> str:
    raw = str(value or "").strip()
    return raw.rstrip("/") or raw


def _recommendation_fact(requirements: Mapping[str, Any] | None, key: str) -> Any:
    """One recommendation fact, read the way every other rec fact is read.

    The survey manifest projects the recommendation's keys at top level; a
    manifest written before that projection existed carries only the nested
    ``build_recommendation`` (same dual read as ``attempt_policy``'s
    ``build_system`` lookup).
    """
    if not isinstance(requirements, Mapping):
        return None
    value = requirements.get(key)
    if value is None:
        recommendation = requirements.get("build_recommendation")
        if isinstance(recommendation, Mapping):
            value = recommendation.get(key)
    return value


def _surveyed_domain_roots(requirements: Mapping[str, Any] | None) -> tuple[str, ...]:
    """``build_domains`` roots in survey order (Stage C schema v1)."""
    raw = _recommendation_fact(requirements, "build_domains")
    if not isinstance(raw, (list, tuple)):
        return ()
    roots: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        root = _normalized_domain_root(item.get("root"))
        if root and root not in roots:
            roots.append(root)
    return tuple(roots)


def _domain_blockers(requirements: Mapping[str, Any] | None) -> dict[str, str]:
    """Consumer root -> the incompatible edge's detail, sealed before any attempt."""
    raw = _recommendation_fact(requirements, "domain_edges")
    if not isinstance(raw, (list, tuple)):
        return {}
    blockers: dict[str, str] = {}
    for edge in raw:
        if not isinstance(edge, Mapping):
            continue
        if str(edge.get("status") or "").strip().lower() != "version_incompatible":
            continue
        consumer = _normalized_domain_root(edge.get("consumer"))
        if not consumer or consumer in blockers:
            continue
        blockers[consumer] = str(edge.get("detail") or "").strip()
    return blockers


def _receipt_order(receipt: Mapping[str, Any]) -> tuple[int, str]:
    """Invocation order from the receipt id's process-monotonic sequence."""
    receipt_id = str(receipt.get("receipt_id") or "")
    tail = receipt_id.rsplit("-", 1)[-1]
    try:
        sequence = int(tail)
    except ValueError:
        sequence = -1
    return (sequence, receipt_id)


def _record_version(payload: Mapping[str, Any]) -> int | None:
    """A record's schema version, or ``None`` when it is not a version we read.

    An absent ``schema_version`` is v1 (receipts written before the key was a
    stated contract). ``True``/``False`` are not versions.
    """
    raw = payload.get("schema_version", 1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw in _SUPPORTED_RECORD_VERSIONS else None


def _read_json_records(orchestrator, directory: str) -> list[Mapping[str, Any]]:
    """Every single-line JSON record in ``directory``, one shell round-trip.

    The writers persist single-line JSON per file, so one ``cat`` of the
    directory yields one record per line; the glob is ``*.json``, so an atomic
    writer's ``<id>.json.tmp`` temp file is invisible here exactly as it is in
    the container — a partially written record is absent, never half-read.

    Evidence collection never breaks a gate: unreadable transport yields no
    records (absent facts stay absent) and the caller degrades rather than
    guessing.
    """
    if orchestrator is None:
        return []
    try:
        probe = orchestrator.execute_command(
            f"cat {shlex.quote(directory)}/*.json 2>/dev/null",
            workdir=None,
            timeout=30,
        )
    except Exception as exc:
        logger.debug(f"{directory} unavailable at the gate: {exc}")
        return []
    records: list[Mapping[str, Any]] = []
    for line in str((probe or {}).get("output") or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            records.append(payload)
    return records


def _read_invocation_receipts(
    orchestrator,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Every readable Stage B invocation receipt, plus named read conflicts.

    Version-gated (spec §C4): v1 and v2 are both derived, an unknown future
    version is SKIPPED with a named conflict. Failing closed on the file rather
    than the run keeps one unreadable receipt from erasing the domains whose
    receipts we do understand — the skipped domain simply stays untried, which
    the truth table already forbids from closing green.
    """
    receipts: list[Mapping[str, Any]] = []
    conflicts: list[str] = []
    for payload in _read_json_records(orchestrator, RECEIPT_DIR):
        # Version FIRST: a future receipt may name its fields differently, so
        # checking the payload's keys before its version would drop it silently
        # — which is the one outcome version gating exists to prevent.
        if _record_version(payload) is None:
            logger.warning(
                f"invocation receipt {payload.get('receipt_id')!r} has unsupported "
                f"schema_version {payload.get('schema_version')!r} and was skipped"
            )
            conflicts.append("receipt_schema_unsupported")
            continue
        if not payload.get("working_directory"):
            continue
        receipts.append(payload)
    return tuple(receipts), tuple(dict.fromkeys(conflicts))


def _read_evidence_assessments(
    orchestrator,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Every readable ReceiptAssessment, plus named read conflicts.

    An assessment is the append-only typed interpretation of ONE receipt
    (``{assessment_id, receipt_id, typed_code, detail}``). Records that name no
    receipt or no typed code carry no interpretation and are not evidence.
    """
    assessments: list[Mapping[str, Any]] = []
    conflicts: list[str] = []
    for payload in _read_json_records(orchestrator, ASSESSMENT_DIR):
        if _record_version(payload) is None:
            logger.warning(
                f"evidence assessment {payload.get('assessment_id')!r} has unsupported "
                f"schema_version {payload.get('schema_version')!r} and was skipped"
            )
            conflicts.append("assessment_schema_unsupported")
            continue
        if not str(payload.get("receipt_id") or "").strip():
            continue
        if not str(payload.get("typed_code") or "").strip():
            continue
        assessments.append(payload)
    return tuple(assessments), tuple(dict.fromkeys(conflicts))


@dataclass(frozen=True)
class _DomainDerivation:
    """Derived per-domain states plus the named conflicts the read produced.

    ``states`` is ``None`` when no build domains were surveyed; conflicts are
    reported either way, because an unreadable evidence record is a fact about
    the run whether or not the project decomposes into domains.
    """

    states: dict[str, dict[str, str]] | None = None
    conflicts: tuple[str, ...] = ()


def _condemned_receipt_ids(
    receipts: Iterable[Mapping[str, Any]],
    assessments: Iterable[Mapping[str, Any]],
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Receipt ids a failure-class assessment condemns, plus named conflicts.

    A SET, so append-only storage holding the same verdict twice (two
    assessment ids, one receipt) is one state transition and not two. An
    assessment naming a receipt we never read is a named conflict rather than a
    crash or an invented failure: without the receipt there is no working
    directory to attribute it to.
    """
    known = {str(receipt.get("receipt_id") or "").strip() for receipt in receipts}
    condemned: set[str] = set()
    conflicts: list[str] = []
    for record in assessments:
        receipt_id = str(record.get("receipt_id") or "").strip()
        if receipt_id not in known:
            logger.warning(
                f"evidence assessment {record.get('assessment_id')!r} names unknown "
                f"receipt {receipt_id!r}"
            )
            conflicts.append("assessment_receipt_missing")
            continue
        if str(record.get("typed_code") or "").strip() in _FAILURE_CLASS_ASSESSMENT_CODES:
            condemned.add(receipt_id)
    return frozenset(condemned), tuple(dict.fromkeys(conflicts))


def _domain_states(
    requirements: Mapping[str, Any] | None,
    receipts: Iterable[Mapping[str, Any]],
    assessments: Iterable[Mapping[str, Any]] = (),
) -> _DomainDerivation:
    """Per-domain state from the coordinate graph, receipts and assessments.

    ``states`` is ``None`` when no build domains were surveyed — the
    single-domain (cli, tvm) path, where the rollups keep their pre-Stage-C
    shape exactly.

    Semantic failure has TWO immutable sources (spec §C4): the receipt's own
    ``outcome == "failed"`` OR a failure-class ``ReceiptAssessment`` naming it.
    The assessment wins over a raw exit 0 — that is the whole point of deleting
    the ``mark_semantic_failure`` rewrite: the receipt keeps saying what the
    command did, and the append-only assessment says what it means.

    Precedence, strictest first: a FAILED receipt outranks a classified blocker
    (accurate classification never erases an observed failure), a blocker
    outranks both a success receipt and untried (P0-F: "required + blocked
    forbids global success" — a disposition is not success by itself), and a
    domain with neither a receipt nor an edge is untried.

    One invocation belongs to ONE domain: a receipt binds to the NEAREST
    containing domain root. Crediting an aggregator because a nested domain
    built is the overclaim this stage exists to remove.
    """
    receipts = tuple(receipts)
    condemned, conflicts = _condemned_receipt_ids(receipts, assessments)
    roots = _surveyed_domain_roots(requirements)
    if not roots:
        return _DomainDerivation(None, conflicts)
    blockers = _domain_blockers(requirements)
    attempted: dict[str, str] = {}
    for receipt in sorted(receipts, key=_receipt_order):
        outcome = str(receipt.get("outcome") or "").strip().lower()
        if outcome not in {"completed", "failed"}:
            continue
        if str(receipt.get("receipt_id") or "").strip() in condemned:
            outcome = "failed"
        directory = _normalized_domain_root(receipt.get("working_directory"))
        if not directory:
            continue
        containing = [
            root for root in roots if directory == root or directory.startswith(f"{root}/")
        ]
        if containing:
            # Retry semantics (same rule the finalizer's observation aggregate
            # uses): the LATEST receipt at a root is that domain's current
            # attempt state.
            nearest = max(containing, key=len)
            attempted[nearest] = "success" if outcome == "completed" else "failed"
    states: dict[str, dict[str, str]] = {}
    for root in roots:
        blocker = blockers.get(root)
        attempt = attempted.get(root)
        if attempt == "failed":
            state = "failed"
        elif blocker is not None:
            state = "blocked"
        elif attempt is not None:
            state = attempt
        else:
            state = "untried"
        entry = {"state": state}
        if blocker:
            entry["blocker"] = blocker
        states[root] = entry
    return _DomainDerivation(states, conflicts)


def _gate_domain_states(
    orchestrator,
    requirements: Mapping[str, Any] | None = None,
) -> _DomainDerivation:
    """Domain states for one gate pass; never raises, never blocks the gate.

    ``requirements`` lets a caller that already read the survey manifest reuse
    it instead of paying a second container round-trip.

    Replay-safe: the derivation is a pure function of the two record
    directories, so reading the same receipts and assessments twice yields
    byte-identical states and the same named conflicts.
    """
    if orchestrator is None:
        return _DomainDerivation()
    try:
        if requirements is None:
            from sag.tools.internal.build_preflight import read_build_requirements

            requirements = read_build_requirements(orchestrator) or {}
        receipts, receipt_conflicts = _read_invocation_receipts(orchestrator)
        assessments, assessment_conflicts = _read_evidence_assessments(orchestrator)
        derived = _domain_states(requirements, receipts, assessments)
        return replace(
            derived,
            conflicts=tuple(
                dict.fromkeys((*receipt_conflicts, *assessment_conflicts, *derived.conflicts))
            ),
        )
    except Exception as exc:
        logger.debug(f"domain states unavailable at the gate: {exc}")
        return _DomainDerivation()


def _inspect_analyze(validator, project_name) -> _ValidatorObservation:
    method = getattr(validator, "validate_project_analysis_status", None)
    if method is None:
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="project analysis evidence is unavailable",
            code="analysis_unavailable",
        )
    status = method(project_name)
    analysis_code = str(status.get("analysis_status_code") or "")
    state = _state_from_evidence_status(status.get("evidence_status") or status.get("status"))
    if state is ValidatorState.UNAVAILABLE:
        if status.get("analyzed") and status.get("has_static_test_count"):
            state = ValidatorState.GREEN
        elif status.get("analyzed"):
            state = ValidatorState.PARTIAL
        elif status.get("success") is True:
            state = ValidatorState.GREEN
        elif status.get("success") is False or analysis_code:
            state = ValidatorState.RED
    projected_reason, projected_suggestions = _ANALYSIS_STATUS_PROJECTIONS.get(
        analysis_code,
        ("", ()),
    )
    analysis_status_facts = status.get("analysis_status_facts")
    if not isinstance(analysis_status_facts, Mapping):
        analysis_status_facts = {}
    return _ValidatorObservation(
        state,
        reason=projected_reason
        or status.get("reason")
        or "project analysis validator returned no conclusion",
        evidence_refs=_status_refs(status),
        suggestions=projected_suggestions,
        code=analysis_code or f"analysis_{state.value}",
        validated_facts={
            "analysis.build_entry_ready": state in {ValidatorState.GREEN, ValidatorState.PARTIAL},
            "analysis.status_code": analysis_code or None,
            "analysis.status_facts": dict(analysis_status_facts),
        },
    )


def _inspect_build(validator, project_name, orchestrator=None) -> _ValidatorObservation:
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
    suggestions: tuple[str, ...] = ()
    if state is not ValidatorState.GREEN:
        suggestions = (
            "Run build(action='compile') and validate the resulting artifacts",
            "If an external impediment prevents progress, claim blocked with its evidence refs",
        )

    # Agent-facing coverage checklist (live 2026-07-18: one bigtop run gave up
    # with islands unattempted because the gate only said "evidence is green";
    # another fixated on a broken island while three healthy ones sat
    # untouched). The gate response NAMES what built and what has no output —
    # on acceptance too, not only on rejection. Same computation the finalizer
    # folds at evidence-close (sag.agent.module_coverage): one algorithm, so
    # mid-run guidance can never disagree with the sealed verdict.
    from sag.agent.module_coverage import coverage_checklist_line, module_coverage

    requirements: Mapping[str, Any] | None = None
    islands = None
    if orchestrator is not None:
        try:
            from sag.tools.internal.build_preflight import read_build_requirements

            requirements = read_build_requirements(orchestrator) or {}
            islands = requirements.get("build_islands")
        except Exception:
            requirements = None
            islands = None
    checklist = coverage_checklist_line(module_coverage(validator, project_name), islands=islands)
    if checklist:
        reason = f"{reason} · {checklist}"
        if "no output yet" in checklist or "remaining:" in checklist:
            suggestions = (
                *suggestions,
                "Modules without build output remain (see the coverage line) — build "
                "each remaining island, or end the phase honestly with "
                "outcome='partial' naming what was left and why",
            )
    explicit_ready = status.get("test_entry_ready")
    evidence = status.get("evidence") or {}
    if not isinstance(explicit_ready, bool) and isinstance(evidence, dict):
        explicit_ready = evidence.get("test_entry_ready")
    if isinstance(explicit_ready, bool):
        test_entry_ready = explicit_ready
    elif state is ValidatorState.GREEN:
        test_entry_ready = True
    elif state is ValidatorState.PARTIAL and isinstance(evidence, dict):
        test_entry_ready = bool(
            evidence.get("has_artifacts")
            or evidence.get("has_build_fingerprints")
            or evidence.get("test_classpath")
            or evidence.get("test_discovery")
        )
    else:
        test_entry_ready = False
    validated_facts: dict[str, Any] = {"build.test_entry_ready": test_entry_ready}
    compiled_classes = _first_nonnegative_int(
        evidence.get("class_count") if isinstance(evidence, Mapping) else None,
        status.get("compiled_classes"),
    )
    if compiled_classes is not None:
        validated_facts["build.compiled_classes"] = compiled_classes
    # Plan 5 Task C2 (P0-B/P0-F): per-domain outcomes ride WITH the build
    # rollup they scope. Absent when no build domains were surveyed, so
    # single-domain projects seal the pre-Stage-C shape byte-identically.
    derived = _gate_domain_states(orchestrator, requirements)
    if derived.states is not None:
        validated_facts["build.domain_states"] = derived.states
    if derived.conflicts:
        # Plan 6 Stage 0: a record we could not read is named, never silently
        # dropped. Absent when the read was clean, so single-domain and
        # recorded-replay runs seal the pre-Plan-6 fact set byte-identically.
        validated_facts["build.evidence_conflicts"] = list(derived.conflicts)
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=f"build_{state.value}",
        validated_facts=validated_facts,
    )


def _inspect_test(validator, project_name, orchestrator=None) -> _ValidatorObservation:
    if validator is None:
        raise RuntimeError("no physical validator available")
    status = validator.validate_test_status(project_name)
    receipt_error = str(status.get("receipt_error") or "").strip()
    if receipt_error:
        # Plan 5 Task B2 / matrix row "receipt persistence fails": receipts we
        # cannot read leave every scanned report unattributed. RED, not
        # UNAVAILABLE — an unverifiable rollup must block a partial claim too,
        # while an honest failed/blocked claim can still close the phase.
        return _ValidatorObservation(
            ValidatorState.RED,
            reason=f"invocation-receipt evidence is unreadable: {receipt_error}",
            evidence_refs=tuple(str(ref) for ref in status.get("receipt_error_files") or ()),
            suggestions=(
                "Re-run the test invocation so it writes a readable receipt, then re-claim",
                "Inspect the named receipt file; a truncated or partial write blocks closure",
            ),
            code="test_receipt_unreadable",
            validated_facts={},
        )
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
        state = _state_from_evidence_status(status.get("evidence_status") or status.get("status"))
        code = f"test_{state.value}"

    if state is ValidatorState.UNAVAILABLE and not status.get("has_test_reports"):
        detail = str(status.get("reason") or "").strip()
        reason = "no test reports or execution evidence available"
        if detail:
            reason = f"{reason}: {detail}"
    else:
        reason = status.get("reason") or "test validator returned no conclusion"
    suggestions: tuple[str, ...] = ()
    if state is not ValidatorState.GREEN:
        suggestions = (
            "Run build(action='test') and preserve the generated test reports",
            "If an external impediment prevents tests, claim blocked with evidence refs",
        )
    rollup = _validated_test_rollup(status)
    if rollup is not None:
        # Plan 5 Task C2: the domain decomposition scopes the test rollup the
        # same way it scopes the build one. Key absent when no domains were
        # surveyed (established absent-when-inapplicable pattern above).
        derived = _gate_domain_states(orchestrator)
        if derived.states is not None:
            rollup["domain_states"] = derived.states
        if derived.conflicts:
            # Plan 6 Stage 0: named evidence-read conflicts ride the rollup's
            # existing conflicts channel into the sealed snapshot.
            rollup["conflicts"] = list(
                dict.fromkeys([*(rollup.get("conflicts") or ()), *derived.conflicts])
            )
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=code,
        validated_facts={"test.stats": rollup} if rollup is not None else {},
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
