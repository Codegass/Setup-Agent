"""Evidence-backed validation for model-authored phase outcome claims.

The validator describes evidence.  It never mutates ``PhaseMachine`` and never
selects the next phase; routing belongs to ``PhaseTransitionPolicy``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Optional

from loguru import logger

from .control_ownership import BlockerOwner
from .evidence_records import EvidencePublicationBinding, read_live_published_json_records
from .invocation_receipts import RECEIPT_DIR, validate_receipt_v2
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


class GateControlDisposition(str, Enum):
    """What the controller may do after this gate observation.

    This is deliberately orthogonal to :class:`ClaimDisposition`.  The latter
    compares a model claim with project evidence; a running controller-owned
    job or a broken evidence transport is not project evidence and must not be
    encoded as a new kind of claim contradiction.
    """

    TERMINAL_CLAIMABLE = "terminal_claimable"
    WAIT_REQUIRED = "wait_required"
    HARNESS_RECOVERY_REQUIRED = "harness_recovery_required"
    REPAIR_REQUIRED = "repair_required"
    TERMINAL_BLOCKED = "terminal_blocked"


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

# Stable storage-path compatibility for historical/forensic tooling.  Live
# gate reads import the strict assessment-ledger API rather than trusting this
# container directory directly.
ASSESSMENT_DIR = "/workspace/.setup_agent/evidence_assessments"

# Plan 8 §3.2/§3.3: the job ids of every obligation the ledger still has open
# when this phase was graded. Present ONLY when something is open, so a run
# that never detached anything seals byte-identical facts — and a transcript
# recorded before Plan 8 carries no such key, which is why the cap below reads
# the fact rather than the container (replay must reproduce a gate offline).
OPEN_OBLIGATIONS_FACT = "run.open_job_obligations"
# Schema-v2 lifecycle facts.  ``OPEN_OBLIGATIONS_FACT`` remains the compact
# compatibility cap/replay basis; these orthogonal facts say WHY a job has no
# durable terminal receipt so control never confuses waiting, harness recovery,
# and project repair.
JOB_BARRIER_FACT = "run.job_controller_barrier"
TERMINAL_UNPERSISTED_FACT = "run.job_terminal_unpersisted"
JOB_INTEGRITY_FACT = "run.job_integrity_failures"
# The state the PHYSICAL inspection returned, recorded beside the obligations
# that cap it. The cap rewrites the validator state, so the gate result alone
# cannot say whether a PARTIAL came from the physical oracle or from the cap on
# a GREEN one — and two consumers need that distinction (the untried-islands
# rule and the blocked-on-green guard, both of which keyed on `success` being
# reachable). Written by `_inspect_phase` under the same presence rule as the
# obligations fact — only when something is open — so a run that never detached
# anything seals byte-identical facts and a pre-Plan-8 transcript carries
# neither key.
PHYSICAL_STATE_FACT = "run.physical_validator_state"
# Whether the run's evidence was already sealed when this phase was graded. A
# sealed run settles nothing (§3.2), so an obligation it names can never be
# discharged; see `settled_validator_state`.
EVIDENCE_SEALED_FACT = "run.evidence_sealed"
ANALYSIS_RECOVERY_FACT = "run.analysis_recovery"
# How many job ids a capped reason spells out before it says "+N more". The
# count itself is never dropped: a bound on a message is not a bound on a fact.
_MAX_NAMED_JOBS = 3

# Receipt/assessment schema versions this reader understands. v1 wrote no
# ``schema_version`` guarantee beyond the constant 1 and v2 only ADDS keys, so
# both derive identically; an unknown FUTURE version is skipped rather than
# coerced (spec §C4: no silent coercion).

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
        (),
    ),
    "analysis_static_count_missing": (
        "Project survey facts exist, but no static test-count fact was observed.",
        (),
    ),
    "analysis_facts_missing": (
        "No persisted project survey facts were observed.",
        (),
    ),
}
_ANALYSIS_HARNESS_FAILURE_CODES = frozenset(
    {
        "analysis_trunk_missing",
        "analysis_facts_missing",
        "analysis_unavailable",
    }
)
ANALYSIS_FACTS_RECOVERY_CODES = frozenset({"analysis_trunk_missing", "analysis_facts_missing"})


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    validated_outcome: PhaseOutcome | str
    claim_disposition: ClaimDisposition | str
    validator_state: ValidatorState | str
    control_disposition: GateControlDisposition | str = GateControlDisposition.TERMINAL_CLAIMABLE
    blocker_owner: BlockerOwner | str = BlockerOwner.NONE
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
        control_disposition = GateControlDisposition(self.control_disposition)
        blocker_owner = BlockerOwner(self.blocker_owner)
        object.__setattr__(self, "validated_outcome", validated_outcome)
        object.__setattr__(self, "claim_disposition", claim_disposition)
        object.__setattr__(self, "validator_state", validator_state)
        object.__setattr__(self, "control_disposition", control_disposition)
        object.__setattr__(self, "blocker_owner", blocker_owner)
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
            "control_disposition": GateControlDisposition(self.control_disposition).value,
            "blocker_owner": BlockerOwner(self.blocker_owner).value,
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
            control_disposition=value.get(
                "control_disposition", GateControlDisposition.TERMINAL_CLAIMABLE
            ),
            blocker_owner=value.get("blocker_owner", BlockerOwner.NONE),
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


def _open_obligations(validated_facts: Mapping[str, Any]) -> tuple[str, ...]:
    """The job ids this phase was graded with still unsettled.

    Read from the sealed fact, never from the container: replay re-runs this
    gate offline, long after the job and its ledger are gone, and a gate that
    probed would grade a different world each time. A shape this reader does
    not understand states nothing — a corrupt key must not invent a blocker.
    """
    raw = validated_facts.get(OPEN_OBLIGATIONS_FACT)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw if str(item or "").strip()))


def _structured_fact_entries(
    validated_facts: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    raw = validated_facts.get(key)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _barrier_job_ids(validated_facts: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(item.get("job_id") or "").strip()
            for item in _structured_fact_entries(validated_facts, JOB_BARRIER_FACT)
            if str(item.get("job_id") or "").strip()
        )
    )


def _terminal_unpersisted_job_ids(validated_facts: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(item.get("job_id") or "").strip()
            for item in _structured_fact_entries(validated_facts, TERMINAL_UNPERSISTED_FACT)
            if str(item.get("job_id") or "").strip()
        )
    )


def _job_integrity_failures(validated_facts: Mapping[str, Any]) -> tuple[str, ...]:
    raw = validated_facts.get(JOB_INTEGRITY_FACT)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw if str(item or "").strip()))


def _unsettled_clause(
    open_jobs: tuple[str, ...], *, terminal_unpersisted: tuple[str, ...] = ()
) -> str:
    """`job <id>` / `jobs <id>, <id>` — bounded, and the count is never lost."""
    from .job_obligations import LEDGER_UNREADABLE

    if open_jobs == (LEDGER_UNREADABLE,):
        # §6.8 fence 1: the cap held because the LEDGER could not be read, not
        # because a named job is open — say that, or the model hunts for a job
        # id that does not exist.
        return "the job obligations ledger could not be read"
    if terminal_unpersisted:
        named = terminal_unpersisted[:_MAX_NAMED_JOBS]
        subject = "job" if len(terminal_unpersisted) == 1 else "jobs"
        listed = ", ".join(named)
        if len(terminal_unpersisted) > len(named):
            listed += f" (+{len(terminal_unpersisted) - len(named)} more)"
        if len(terminal_unpersisted) == 1:
            return f"{subject} {listed} is terminal but its receipt was not persisted"
        return f"{subject} {listed} are terminal but their receipts were not persisted"
    named = open_jobs[:_MAX_NAMED_JOBS]
    subject = "job" if len(open_jobs) == 1 else "jobs"
    listed = ", ".join(named)
    if len(open_jobs) > len(named):
        listed += f" (+{len(open_jobs) - len(named)} more)"
    verb = "has" if len(open_jobs) == 1 else "have"
    return f"{subject} {listed} {verb} no terminal receipt"


def _evidence_sealed(validated_facts: Mapping[str, Any]) -> bool:
    """Whether the run had closed its evidence when this phase was graded."""
    return validated_facts.get(EVIDENCE_SEALED_FACT) is True


def settled_validator_state(
    validator_state: ValidatorState | str,
    validated_facts: Mapping[str, Any] | None = None,
) -> ValidatorState:
    """The strongest state this evidence supports once open books are counted.

    GREEN with an obligation still open is at most PARTIAL (§3.3: success
    requires settled books). Every other state is returned unchanged — the cap
    only ever removes strength, and it is a pure function of the state and the
    facts, with no view of any claim.

    This is the ONE computation of that cap (P3). `validate_phase_claim` applies
    it when it grades a claim, `check_phase_done` when it projects the same
    question for a nudge, and a caller that DERIVES its claim from validator
    evidence rather than from a model must derive it from THIS state, or it
    states a claim the same gate is about to contradict. The engine's phase
    floor is exactly such a caller: it is the safety net for a starved attempt,
    it has no second move, and a contradiction there is a ValueError out of
    `PhaseMachine.close_attempt` that aborts the whole run.

    A cap is a hold on a verdict until the evidence arrives, so it lives exactly
    as long as the evidence can arrive. After evidence-close the run settles
    NOTHING (§3.2: the sealed verdict has already recorded the job as
    `job_unsettled`, and evidence-close is immutable), so the obligation it
    still names can never be discharged — and a hold nothing can discharge is
    not a cap, it is a dead end. It cost the report phase its own claim: a
    delivered report was CONTRADICTED on a build job's books, with no move that
    could ever change the answer, and a job that DID terminate before the report
    claim recorded `partial` where the evidence was complete. Removing the cap
    here removes no evidence (P4): the unsettled job is on the verdict as a
    conflict, at a strictly higher fidelity than a phase outcome, and the OTHER
    half of §3.3 — no refinement above the claim while an obligation is open —
    is not scoped by the seal, so a sealed gate still cannot upgrade a claim.
    """
    state = ValidatorState(validator_state)
    facts = dict(validated_facts or {})
    if state is ValidatorState.GREEN and _open_obligations(facts) and not _evidence_sealed(facts):
        return ValidatorState.PARTIAL
    return state


def settled_observation(
    validator_state: ValidatorState | str,
    reason: str = "",
    validated_facts: Mapping[str, Any] | None = None,
) -> tuple[ValidatorState, str]:
    """The capped state AND the sentence that says why — one determination.

    P3 is a rule about pairs as much as about deciders: a state whose reason does
    not mention the cap is the same two computations again, one deciding and one
    decorating. Both the gate and the read-only probe the engine nudges from take
    their state and their sentence from here.
    """
    state = ValidatorState(validator_state)
    facts = dict(validated_facts or {})
    capped = settled_validator_state(state, facts)
    if capped is state:
        return state, reason
    terminal_unpersisted = _terminal_unpersisted_job_ids(facts)
    clause = _unsettled_clause(_open_obligations(facts), terminal_unpersisted=terminal_unpersisted)
    clause += (
        " — success requires durable terminal evidence"
        if terminal_unpersisted
        else " — success requires settled books"
    )
    return capped, " · ".join(part for part in (reason, clause) if part)


def claimable_outcome(
    validator_state: ValidatorState | str,
    validated_facts: Mapping[str, Any] | None = None,
) -> PhaseOutcome:
    """The outcome a machine-derived claim may state on this evidence.

    The floor's claim is not a model's opinion: it is a projection of the same
    evidence the gate is about to grade, so it is the gate's own validated
    outcome by construction.
    """
    return _VALIDATED_OUTCOMES[settled_validator_state(validator_state, validated_facts)]


def settlement_capped_outcome(
    validated_facts: Mapping[str, Any] | None = None,
) -> PhaseOutcome | None:
    """The outcome the §3.3 cap leaves in place of `success`, or None.

    PARTIAL exactly when the physical inspection was GREEN and the cap fired on
    it — the one state where `success` is unavailable while the physical oracle
    saw a complete phase. Every caller that keyed on `success` being the top of
    the ladder needs this and not the capped state: the capped PARTIAL and a
    physically partial build are different facts, and the gate result alone
    cannot tell them apart.

    Absent basis is not a green light (P2): a fact dict with no recorded physical
    state states nothing about it, so this returns None rather than assuming the
    inspection was green. The conservative answer keeps every guard armed.
    """
    facts = dict(validated_facts or {})
    physical = str(facts.get(PHYSICAL_STATE_FACT) or "").strip().lower()
    if physical != ValidatorState.GREEN.value:
        return None
    capped = settled_validator_state(ValidatorState.GREEN, facts)
    if capped is ValidatorState.GREEN:
        return None
    return _VALIDATED_OUTCOMES[capped]


@dataclass(frozen=True)
class _ValidatorObservation:
    state: ValidatorState
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    code: str = ""
    validated_facts: Mapping[str, Any] = field(default_factory=dict)
    control_disposition: GateControlDisposition = GateControlDisposition.TERMINAL_CLAIMABLE
    blocker_owner: BlockerOwner = BlockerOwner.NONE


@dataclass(frozen=True)
class _JobLedgerObservation:
    """The control-relevant result of reconciling the obligation ledger."""

    running_job_ids: tuple[str, ...] = ()
    settlement_pending_job_ids: tuple[str, ...] = ()
    terminal_unpersisted: tuple[Mapping[str, Any], ...] = ()
    integrity_failures: tuple[str, ...] = ()

    @property
    def barrier_entries(self) -> tuple[Mapping[str, str], ...]:
        entries = [{"job_id": job_id, "state": "running"} for job_id in self.running_job_ids]
        entries.extend(
            {"job_id": job_id, "state": "settlement_pending"}
            for job_id in self.settlement_pending_job_ids
        )
        return tuple(entries)

    @property
    def terminal_unpersisted_job_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(item.get("job_id") or "").strip()
                for item in self.terminal_unpersisted
                if str(item.get("job_id") or "").strip()
            )
        )

    @property
    def open_job_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.running_job_ids,
                    *self.settlement_pending_job_ids,
                    *self.terminal_unpersisted_job_ids,
                )
            )
        )

    def validated_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if self.open_job_ids:
            facts[OPEN_OBLIGATIONS_FACT] = list(self.open_job_ids)
        if self.barrier_entries:
            facts[JOB_BARRIER_FACT] = [dict(item) for item in self.barrier_entries]
        if self.terminal_unpersisted:
            facts[TERMINAL_UNPERSISTED_FACT] = [dict(item) for item in self.terminal_unpersisted]
        if self.integrity_failures:
            facts[JOB_INTEGRITY_FACT] = list(self.integrity_failures)
        return facts


def validate_phase_claim(
    claim: PhaseClaim | PhaseOutcome | str,
    validator_state: ValidatorState | str,
    *,
    reason: str = "",
    evidence_refs: Iterable[str] = (),
    suggestions: Iterable[str] = (),
    code: str = "",
    validated_facts: Mapping[str, Any] | None = None,
    control_disposition: GateControlDisposition | str = (GateControlDisposition.TERMINAL_CLAIMABLE),
    blocker_owner: BlockerOwner | str | None = None,
) -> GateResult:
    """Compare a claim with validator evidence without routing or mutation."""
    state = ValidatorState(validator_state)
    if isinstance(claim, PhaseClaim):
        phase_claim = claim
    else:
        phase_claim = PhaseClaim(phase="", claimed_outcome=PhaseOutcome(claim))
    claimed = PhaseOutcome(phase_claim.claimed_outcome)
    validated = _VALIDATED_OUTCOMES[state]
    facts = dict(validated_facts or {})
    control = GateControlDisposition(control_disposition)
    owner = BlockerOwner(blocker_owner) if blocker_owner is not None else BlockerOwner.NONE

    # These states belong to the controller, not the project oracle.  Do not
    # run the ordinary claim matrix and accidentally turn an unreadable marker
    # or a still-running job into a project-level failed/partial outcome.  The
    # ClaimDisposition remains the legacy-compatible rejection shape while the
    # orthogonal control disposition states the actual owner and next action.
    integrity_failures = _job_integrity_failures(facts)
    barrier_jobs = _barrier_job_ids(facts)
    if integrity_failures:
        control = GateControlDisposition.HARNESS_RECOVERY_REQUIRED
        owner = BlockerOwner.HARNESS
        details = ", ".join(integrity_failures[:_MAX_NAMED_JOBS])
        if len(integrity_failures) > _MAX_NAMED_JOBS:
            details += f" (+{len(integrity_failures) - _MAX_NAMED_JOBS} more)"
        integrity_reason = (
            "job evidence reconciliation requires harness recovery before the "
            f"project claim can be graded: {details}"
        )
        return GateResult(
            accepted=False,
            validated_outcome=PhaseOutcome.UNKNOWN,
            claim_disposition=ClaimDisposition.CONTRADICTED,
            validator_state=ValidatorState.UNAVAILABLE,
            control_disposition=control,
            blocker_owner=owner,
            reason=" · ".join(part for part in (reason, integrity_reason) if part),
            evidence_refs=tuple(evidence_refs),
            suggestions=(
                "The harness must restore readable lifecycle evidence and reconcile the job; "
                "do not infer a project failure or rerun the project action from this state.",
            ),
            code="job_evidence_integrity",
            validated_facts=facts,
            claim=phase_claim,
        )
    if barrier_jobs:
        control = GateControlDisposition.WAIT_REQUIRED
        owner = BlockerOwner.HARNESS
        barrier_reason = (
            "controller job barrier remains active for "
            f"{', '.join(barrier_jobs[:_MAX_NAMED_JOBS])}; no project claim was graded"
        )
        return GateResult(
            accepted=False,
            validated_outcome=PhaseOutcome.UNKNOWN,
            claim_disposition=ClaimDisposition.CONTRADICTED,
            validator_state=ValidatorState.UNAVAILABLE,
            control_disposition=control,
            blocker_owner=owner,
            reason=" · ".join(part for part in (reason, barrier_reason) if part),
            evidence_refs=tuple(evidence_refs),
            suggestions=(
                "The controller must keep polling and settle every running or pending job; "
                "the model must not dispatch unrelated work.",
            ),
            code="job_controller_barrier",
            validated_facts=facts,
            claim=phase_claim,
        )

    terminal_unpersisted = _terminal_unpersisted_job_ids(facts)
    if terminal_unpersisted:
        control = GateControlDisposition.HARNESS_RECOVERY_REQUIRED
        owner = BlockerOwner.HARNESS

    # A controller failure is not a pessimistic project claim.  In particular,
    # ``done+failed`` must not close an analyze phase merely because the survey
    # manifest could not be read.  The sole closable harness-owned state is a
    # durable terminal-unpersisted job whose underlying physical observation
    # was itself gradable; the missing receipt caps that observation but does
    # not erase it.  Replay receives the post-cap state, so accept either the
    # recorded physical state or its deterministic settled projection here.
    physical_raw = str(facts.get(PHYSICAL_STATE_FACT) or "").strip().lower()
    physical_state = None
    try:
        physical_state = ValidatorState(physical_raw)
    except ValueError:
        pass
    terminal_unpersisted_is_gradable = bool(
        terminal_unpersisted
        and physical_state
        in {
            ValidatorState.GREEN,
            ValidatorState.PARTIAL,
            ValidatorState.RED,
        }
        and state
        in {
            physical_state,
            settled_validator_state(physical_state, facts),
        }
    )
    if (
        control is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
        and not terminal_unpersisted_is_gradable
    ):
        return GateResult(
            accepted=False,
            validated_outcome=PhaseOutcome.UNKNOWN,
            claim_disposition=ClaimDisposition.CONTRADICTED,
            validator_state=ValidatorState.UNAVAILABLE,
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
            reason=reason or "harness evidence recovery is required before this claim can close",
            evidence_refs=tuple(evidence_refs),
            suggestions=(),
            code=code or "harness_recovery_required",
            validated_facts=facts,
            claim=phase_claim,
        )

    # Domain truth table (Plan 5 Stage C, P0-F). Ground-truth review 2026-07-26
    # (§"Partial claims are upgraded"): a global artifact-presence check turned
    # Bigtop's truthful 2/4 partial into validated success, and the per-domain
    # facts were gone before sealing. While ANY surveyed domain is
    # failed/blocked/untried the validated outcome may confirm or downgrade the
    # claim, never refine it upward — a classified blocker is not a green
    # waiver. The cap stops AT the claim: it never manufactures a contradiction
    # the physical oracle did not observe.
    #
    # Plan 8 §3.3 broadens the TRIGGER and leaves the direction alone. The p7d
    # polaris build was graded while its compile job was still running: the
    # model honestly claimed `partial`, the gate said `Built 100% of expected
    # classes … Module coverage: 1/26 built` and upgraded the claim to success.
    # The cap did not fire because polaris's survey reads no Kotlin settings,
    # so its domain list was empty — and an empty domain graph is not evidence
    # that nothing is unfinished. An unsettled obligation is.
    blocking_domains = _unclosed_domains(facts)
    open_jobs = _open_obligations(facts)
    if (
        (blocking_domains or open_jobs)
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
                (
                    "no refinement above the claim while surveyed build domains are "
                    f"unclosed: {', '.join(blocking_domains)}"
                    if blocking_domains
                    else ""
                ),
                (
                    f"{_unsettled_clause(open_jobs, terminal_unpersisted=terminal_unpersisted)} "
                    "— the claim is confirmable at most"
                    if open_jobs
                    else ""
                ),
            )
            if part
        )

    # And success requires settled books: while a job is still out, the run
    # does not yet know what its own dispatch did, so green is not available
    # to it. A downgrade, never an upgrade — a MODEL's success claim capped this
    # way is contradicted by the ordinary truth table below and must be re-made
    # honestly, while a machine-derived claim states the capped outcome from the
    # start (`claimable_outcome`) and is confirmed. The state and its sentence
    # come from `settled_observation`, the one computation of the cap, so the
    # engine's read-only probe cannot answer this question differently.
    capped, reason = settled_observation(state, reason, facts)
    if capped is not state:
        state = capped
        validated = _VALIDATED_OUTCOMES[state]
        # The judge reports the evidence bound without selecting the model's
        # next terminal call. Controller-owned persistence failures expose no
        # project-level recovery action.
        if terminal_unpersisted:
            # This is a harness-owned evidence persistence failure.  Giving
            # the model an exact phase invocation is both a prescription leak
            # and a false recovery action: no model call can restore the
            # missing receipt.  Keep only the typed owner/outcome in the gate
            # result and expose no action suggestion.
            suggestions = ()
        else:
            next_step = (
                f"validated outcome is capped at '{validated.value}' while "
                f"{_unsettled_clause(open_jobs)}; terminal evidence remains unsettled"
            )
            suggestions = (*tuple(suggestions), next_step)

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

    if control is GateControlDisposition.TERMINAL_CLAIMABLE:
        if state is ValidatorState.GREEN:
            owner = BlockerOwner.NONE
        elif state in {ValidatorState.RED, ValidatorState.PARTIAL}:
            owner = BlockerOwner.PROJECT
        else:
            owner = BlockerOwner.UNKNOWN
        if not accepted:
            control = GateControlDisposition.REPAIR_REQUIRED

    return GateResult(
        accepted=accepted,
        validated_outcome=validated,
        claim_disposition=disposition,
        validator_state=state,
        control_disposition=control,
        blocker_owner=owner,
        reason=reason,
        evidence_refs=tuple(evidence_refs),
        suggestions=tuple(suggestions),
        code="evidence_unpersisted" if terminal_unpersisted else code,
        validated_facts=facts,
        claim=phase_claim,
    )


def check_phase_claim(
    phase: str,
    claim: PhaseClaim,
    validator,
    orchestrator,
    project_name: Optional[str],
    *,
    sealed: bool = False,
) -> GateResult:
    """Inspect physical evidence and validate one terminal phase claim.

    `sealed` is the run's evidence seal: a sealed run accepts no further
    evidence, so the gate grades the ledger it finds without settling it (see
    :func:`_settle_before_grading`).
    """
    if claim.phase != phase:
        raise ValueError(f"claim for {claim.phase!r} cannot validate phase {phase!r}")
    observation = _inspect_phase(phase, validator, orchestrator, project_name, sealed=sealed)
    return validate_phase_claim(
        claim,
        observation.state,
        reason=observation.reason,
        evidence_refs=observation.evidence_refs,
        suggestions=observation.suggestions,
        code=observation.code,
        validated_facts=observation.validated_facts,
        control_disposition=observation.control_disposition,
        blocker_owner=observation.blocker_owner,
    )


def check_phase_done(
    phase: str,
    validator,
    orchestrator,
    project_name: Optional[str],
    *,
    sealed: bool = False,
) -> dict[str, Any]:
    """Read-only compatibility projection for engine nudges during WS3.

    Live model claims use :func:`check_phase_claim`; this adapter carries no
    claim and therefore cannot close or advance a phase. It does reach the
    settler at the gate, so it carries the run's evidence seal too.

    `ok` answers "would the completion gate pass", which is the SAME question
    `validate_phase_claim` answers — so it is capped by the same computation
    (§3.3 via `settled_observation`). It was not, and the two answers diverged in
    exactly one state: green physical evidence with a job still out. The
    mid-phase nudge then announced "the completion gate passes on physical
    evidence" and the gate contradicted the success claim it had just invited,
    leaving the model between two harness computations of one question (P3). The
    engine's starved-phase floor reads this same dict, and it closes
    unconditionally, so its probe must carry the cap and the sentence too.
    """
    observation = _inspect_phase(phase, validator, orchestrator, project_name, sealed=sealed)
    state, reason = settled_observation(
        observation.state, observation.reason, observation.validated_facts
    )
    return {
        "ok": state is ValidatorState.GREEN,
        "reason": reason,
        "suggestions": list(observation.suggestions),
        "validator_state": state.value,
        "control_disposition": observation.control_disposition.value,
        "blocker_owner": observation.blocker_owner.value,
        "evidence_refs": list(observation.evidence_refs),
        "code": observation.code,
        "validated_facts": dict(observation.validated_facts),
    }


def _terminal_unpersisted_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Stable gate fact for an already-terminal obligation with no receipt."""
    from .job_obligations import OBLIGATION_DIR

    job_id = str(record.get("job_id") or "").strip()
    payload: dict[str, Any] = {
        "job_id": job_id,
        "exit_code": record.get("terminal_exit_code"),
        "attempted_receipt_id": str(record.get("attempted_receipt_id") or ""),
        "persistence_code": str(record.get("receipt_persistence_code") or ""),
        "attempt_count": record.get("settlement_attempts"),
        "obligation_ref": f"{OBLIGATION_DIR}/{job_id}.json",
        "log_ref": str(record.get("log_path") or ""),
    }
    contract_id = str(record.get("contract_id") or "").strip()
    if contract_id:
        payload["contract_id"] = contract_id
    return payload


def _classify_sealed_obligations(
    records: Iterable[Mapping[str, Any]],
    *,
    execute,
) -> _JobLedgerObservation:
    """Classify without writing: evidence-close never resumes settlement."""
    from .job_obligations import (
        PROCESS_TERMINAL,
        SETTLEMENT_SETTLED,
        is_terminal_unpersisted,
        process_is_live,
        settled_receipt_integrity_failure,
        settlement_attempts_integrity_failure,
        settlement_is_pending,
    )

    running: list[str] = []
    pending: list[str] = []
    unpersisted: list[Mapping[str, Any]] = []
    integrity: list[str] = []
    for record in records:
        job_id = str(record.get("job_id") or "").strip()
        if not job_id:
            integrity.append("obligation_missing_job_id")
            continue
        attempt_failure = settlement_attempts_integrity_failure(record)
        if attempt_failure:
            integrity.append(attempt_failure)
            continue
        if is_terminal_unpersisted(record):
            unpersisted.append(_terminal_unpersisted_record(record))
        elif settlement_is_pending(record):
            pending.append(job_id)
        elif process_is_live(record):
            # Schema-v1 records have no process state.  An unsettled one stays
            # conservatively live until a marker is reconciled in an unsealed
            # run; a sealed gate may observe but never mutate it.
            running.append(job_id)
        elif str(record.get("settled_receipt_id") or "").strip():
            failure = settled_receipt_integrity_failure(
                execute,
                record,
                receipt_ledger_readable=callable(execute),
            )
            if failure:
                integrity.append(failure)
        elif (
            str(record.get("process_state") or "").strip() == PROCESS_TERMINAL
            and str(record.get("settlement_state") or "").strip() == SETTLEMENT_SETTLED
        ):
            failure = settled_receipt_integrity_failure(
                execute,
                record,
                receipt_ledger_readable=callable(execute),
            )
            if failure:
                integrity.append(failure)
        else:
            integrity.append(f"{job_id}:illegal_lifecycle_state")
    return _JobLedgerObservation(
        running_job_ids=tuple(sorted(set(running))),
        settlement_pending_job_ids=tuple(sorted(set(pending))),
        terminal_unpersisted=tuple(
            sorted(unpersisted, key=lambda item: str(item.get("job_id") or ""))
        ),
        integrity_failures=tuple(dict.fromkeys(integrity)),
    )


def _settle_before_grading(orchestrator, *, sealed: bool = False) -> _JobLedgerObservation:
    """Settle the job ledger, then classify its control state. Never raises.

    Plan 8 §3.2 trigger 2. The polaris build gate (p7d,
    `session_20260729_111737_22356`) graded a compile job that was still
    running and upgraded an honest `partial` to success on the snapshot it
    saw. A gate may only ever grade settled books, so settlement runs BEFORE
    the physical inspection — the receipts it writes are the evidence the
    inspection is about to read.

    A SEALED run settles nothing. It classifies the durable lifecycle as-is;
    any stale live/pending record remains a controller barrier and any durable
    terminal-unpersisted record remains an evidence-integrity cap.

    A run that never detached anything costs one glob `cat` that matches no
    file, and states no fact.
    """
    if orchestrator is None:
        return _JobLedgerObservation()
    from .job_obligations import (
        is_terminal_unpersisted,
        read_obligations,
        reconcile_job_obligations,
    )

    try:
        records = read_obligations(orchestrator)
        if records is None:
            # Missing evidence is not evidence of an empty ledger. Keep the
            # failure typed and controller-owned instead of grading a project.
            return _JobLedgerObservation(integrity_failures=("ledger_unreadable",))
        if not records:
            return _JobLedgerObservation()
        if sealed:
            execute = getattr(orchestrator, "execute_command", None)
            if not callable(execute) and callable(orchestrator):
                execute = orchestrator
            return _classify_sealed_obligations(records, execute=execute)

        existing_unpersisted = {
            str(record.get("job_id") or "").strip(): _terminal_unpersisted_record(record)
            for record in records
            if is_terminal_unpersisted(record)
        }
        reconciliation = reconcile_job_obligations(orchestrator, obligations=records)
        for terminal in reconciliation.terminal_unpersisted:
            existing_unpersisted[terminal.job_id] = terminal.event_payload()
        return _JobLedgerObservation(
            running_job_ids=tuple(reconciliation.running_job_ids),
            settlement_pending_job_ids=tuple(reconciliation.settlement_pending_job_ids),
            terminal_unpersisted=tuple(
                existing_unpersisted[job_id] for job_id in sorted(existing_unpersisted)
            ),
            integrity_failures=tuple(reconciliation.integrity_failures),
        )
    except Exception as exc:  # the ledger never becomes a project failure
        logger.warning(f"job obligations were not settled before grading: {exc}")
        return _JobLedgerObservation(
            integrity_failures=(f"ledger_reconciliation_{type(exc).__name__}",)
        )


def _inspect_phase(
    phase, validator, orchestrator, project_name, *, sealed: bool = False
) -> _ValidatorObservation:
    jobs = _settle_before_grading(orchestrator, sealed=sealed)
    lifecycle_facts = jobs.validated_facts()
    if jobs.integrity_failures:
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="job evidence lifecycle requires harness recovery; project evidence was not inspected",
            code="job_evidence_integrity",
            validated_facts=lifecycle_facts,
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
        )
    if jobs.barrier_entries:
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="controller job barrier is active; project evidence was not inspected",
            code="job_controller_barrier",
            validated_facts=lifecycle_facts,
            control_disposition=GateControlDisposition.WAIT_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
        )

    observation = _inspect_phase_evidence(phase, validator, orchestrator, project_name)
    if not jobs.terminal_unpersisted:
        return observation

    # A missing receipt may cap a physical observation only when that
    # observation exists.  Never let the lifecycle wrapper rename an analysis
    # survey failure (or any other ungradable harness observation) to the
    # closable ``evidence_unpersisted`` state.
    gradable = ValidatorState(observation.state) in {
        ValidatorState.GREEN,
        ValidatorState.PARTIAL,
        ValidatorState.RED,
    } and observation.control_disposition not in {
        GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
        GateControlDisposition.WAIT_REQUIRED,
    }
    if not gradable:
        return replace(
            observation,
            validated_facts={
                **dict(observation.validated_facts),
                **lifecycle_facts,
                EVIDENCE_SEALED_FACT: bool(sealed),
            },
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
        )

    # Terminal-unpersisted is no longer a wait: the process result is known and
    # the model may close honestly as partial/failed/unknown.  It is still an
    # evidence-integrity cap, so success is unavailable and replay receives the
    # physical basis plus the exact persistence disposition.
    return replace(
        observation,
        code="evidence_unpersisted",
        validated_facts={
            **dict(observation.validated_facts),
            **lifecycle_facts,
            PHYSICAL_STATE_FACT: ValidatorState(observation.state).value,
            EVIDENCE_SEALED_FACT: bool(sealed),
        },
        control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
        blocker_owner=BlockerOwner.HARNESS,
    )


def _inspect_phase_evidence(phase, validator, orchestrator, project_name) -> _ValidatorObservation:
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
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
        )
    except Exception as exc:
        logger.warning(f"Phase gate '{phase}' evidence unavailable (probe error): {exc}")
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason=f"validator probe unavailable: {exc}",
            code="validator_unavailable",
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
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
                "The expected workspace is absent; no repository evidence can be graded.",
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


def _read_invocation_receipts(
    orchestrator,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Every current host-authorized v2 receipt, or one fail-closed conflict.

    Container files are forensic mirrors, not authority.  Historical v1
    records and fully valid foreign-run v2 records may remain in a reused
    container, but an unknown/future/malformed current shape cannot be skipped:
    it may be the later failure that caps any surveyed domain.
    """
    if orchestrator is None:
        return (), ()

    def scope(
        payload: Mapping[str, Any], current_run_id: str | None
    ) -> Literal["current", "foreign", "forensic"]:
        version = payload.get("schema_version", 1)
        if version == 1 and not isinstance(version, bool):
            return "forensic"
        if version != 2 or isinstance(version, bool):
            return "current"
        try:
            normalized = validate_receipt_v2(
                payload,
                expected_id=str(payload.get("receipt_id") or ""),
            )
        except (TypeError, ValueError):
            return "current"
        return (
            "foreign"
            if current_run_id and normalized.get("run_id") != current_run_id
            else "current"
        )

    read = read_live_published_json_records(
        orchestrator,
        RECEIPT_DIR,
        record_kind="invocation_receipt",
        validator=lambda payload, expected_id: validate_receipt_v2(
            payload,
            expected_id=expected_id,
        ),
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=str(payload["run_id"]),
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
        ),
        record_scope=scope,
    )
    if not read.complete or read.conflict is not None:
        return (), (f"receipt_{read.conflict or 'stream_unreadable'}",)
    receipts = tuple(
        dict(record.payload) for record in read.records if record.payload.get("working_directory")
    )
    return receipts, ()


def _read_evidence_assessments(
    orchestrator,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Every readable ReceiptAssessment, plus named read conflicts.

    An assessment is the append-only typed interpretation of ONE receipt
    (``{assessment_id, receipt_id, typed_code, detail}``). Records that name no
    receipt or no typed code carry no interpretation and are not evidence.
    """
    if orchestrator is None:
        return (), ()
    from .evidence_assessments import read_live_assessment_ledger

    read = read_live_assessment_ledger(orchestrator)
    if not read.complete or read.conflict is not None:
        return (), (f"assessment_{read.conflict or 'stream_unreadable'}",)
    assessments = tuple(
        dict(record.payload)
        for record in read.records
        if str(record.payload.get("receipt_id") or "").strip()
        and str(record.payload.get("typed_code") or "").strip()
    )
    return assessments, ()


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
        if not receipt_id and isinstance(record.get("event_or_intent_id"), str):
            # A ControlAssessment interprets a pre-dispatch event, not a
            # receipt (spec §C4) — same directory, different subject. It can
            # neither condemn a receipt nor count as a missing reference.
            continue
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


# Effective actions that PRODUCE something a domain state may rest on.
# Inspection verbs (dependencies/deps, help, tasks, probes) are visible
# evidence but never an attempt (spec §C2 execution law).
_PRODUCTION_ACTION_PREFIXES = (
    "compile",
    "test",
    "package",
    "install",
    "publish",
    "assemble",
    "build",
    "verify",
    "native",
)


def _is_production_action(value: Any) -> bool:
    action = str(value or "").strip().lower()
    if not action:
        # A receipt that states no effective action states an attempt of the
        # requested verb — the pre-Stage-B shape. Counting it preserves the
        # recorded Plan 5 sessions' domain states unchanged.
        return True
    first = action.split()[0].rsplit(":", 1)[-1]
    return any(first.startswith(prefix) for prefix in _PRODUCTION_ACTION_PREFIXES)


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
        # Live p6v-bigtop-r3: a completed `gradle dependencies` PROBE at the
        # spark root scored the domain green. The edge law deliberately lets
        # dependency probes through so a mismatch can show itself — an
        # inspection is never a production attempt, and only production
        # verbs may settle a domain's state.
        if not _is_production_action(receipt.get("effective_action")):
            continue
        if str(receipt.get("receipt_id") or "").strip() in condemned:
            outcome = "failed"
        # ``working_directory`` is the requested coordinate.  A receipt's
        # authoritative physical binding is ``actual_cwd`` when present (the
        # same field current-run validation already checks); using the request
        # here can credit the wrong island after a legitimate runner redirect.
        directory = _normalized_domain_root(
            receipt.get("actual_cwd") or receipt.get("working_directory")
        )
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
    *,
    receipt_scope: Any = None,
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
        manifest_conflicts: list[str] = []
        if requirements is None:
            from sag.tools.internal.build_preflight import read_live_build_requirements

            manifest_read = read_live_build_requirements(orchestrator)
            if not manifest_read.complete or manifest_read.conflict is not None:
                manifest_conflicts.append(
                    f"build_requirements_{manifest_read.conflict or 'stream_unreadable'}"
                )
                requirements = {}
            else:
                requirements = manifest_read.payload or {}
        receipts, receipt_conflicts = _read_invocation_receipts(orchestrator)
        assessments, assessment_conflicts = _read_evidence_assessments(orchestrator)
        conflicts = [*manifest_conflicts, *receipt_conflicts, *assessment_conflicts]

        # Live callers pass an immutable run/checkout/root scope.  Historical
        # pure-derivation callers omit it and retain the schema-v1 replay
        # contract.  A foreign receipt is known historical evidence; an
        # assessment naming it is therefore ignored rather than mislabeled as
        # a dangling current-run reference.
        if receipt_scope is not None:
            from sag.agent.attempt_policy import current_run_production_build_receipt

            if not bool(getattr(receipt_scope, "available", False)):
                status = str(getattr(receipt_scope, "status", "unavailable") or "unavailable")
                conflicts.append(f"receipt_binding_{status}")
                receipts = ()
                assessments = ()
            else:
                all_receipt_ids = {
                    str(receipt.get("receipt_id") or "").strip() for receipt in receipts
                }
                current_receipts = tuple(
                    receipt
                    for receipt in receipts
                    if current_run_production_build_receipt(
                        receipt,
                        receipt_id=str(receipt.get("receipt_id") or "").strip(),
                        run_id=str(getattr(receipt_scope, "run_id", "") or ""),
                        target_sha=str(getattr(receipt_scope, "target_sha", "") or ""),
                        project_root=str(getattr(receipt_scope, "project_root", "") or ""),
                    )
                )
                current_ids = {
                    str(receipt.get("receipt_id") or "").strip() for receipt in current_receipts
                }
                historical_ids = all_receipt_ids - current_ids
                receipts = current_receipts
                assessments = tuple(
                    record
                    for record in assessments
                    if str(record.get("receipt_id") or "").strip() not in historical_ids
                )

        # A corrupt assessment could be the failure interpretation of an
        # otherwise green receipt; a corrupt receipt could be a later failure
        # at the same root.  Using the readable prefix would therefore invent
        # success.  Keep surveyed domains visible but derive every one as
        # untried until the evidence stream is readable again.
        hard_conflicts = {
            "receipt_stream_unreadable",
            "receipt_record_malformed",
            "receipt_schema_unsupported",
            "assessment_stream_unreadable",
            "assessment_record_malformed",
            "assessment_schema_unsupported",
        }
        if hard_conflicts.intersection(conflicts):
            receipts = ()
            assessments = ()
        derived = _domain_states(requirements, receipts, assessments)
        return replace(
            derived,
            conflicts=tuple(dict.fromkeys((*conflicts, *derived.conflicts))),
        )
    except Exception as exc:
        logger.debug(f"domain states unavailable at the gate: {exc}")
        return _DomainDerivation()


def _live_receipt_scope(validator, orchestrator, requirements=None):
    """Return the validator's explicit live run scope, or ``None`` for replay.

    Pure/historical callers deliberately omit ``receipt_run_id`` and retain
    the schema-v1 derivation contract.  Production validators are bound to the
    setup run at construction, so their domain rollups may consume only
    receipts that match the container run pin, checkout and surveyed root.
    """

    run_id = str(getattr(validator, "receipt_run_id", "") or "").strip()
    if not run_id or orchestrator is None:
        return None
    from sag.agent.attempt_policy import resolve_current_build_receipt_scope

    workspace_root = str(getattr(validator, "project_path", "") or "/workspace").strip()
    return resolve_current_build_receipt_scope(
        orchestrator,
        run_id=run_id,
        manifest=requirements,
        workspace_root=workspace_root,
    )


def _inspect_analyze(validator, project_name) -> _ValidatorObservation:
    method = getattr(validator, "validate_project_analysis_status", None)
    if method is None:
        return _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="project analysis evidence is unavailable",
            code="analysis_unavailable",
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner=BlockerOwner.HARNESS,
        )
    status = method(project_name)
    analysis_code = str(status.get("analysis_status_code") or "")
    state = _state_from_evidence_status(status.get("evidence_status") or status.get("status"))
    if analysis_code in _ANALYSIS_HARNESS_FAILURE_CODES:
        # Category 1 makes survey persistence an engine guarantee. Missing or
        # unreadable survey state therefore indicts the harness, not the
        # project and not the model's choice of another analyze invocation.
        state = ValidatorState.UNAVAILABLE
    elif analysis_code == "analysis_static_count_missing":
        # A denominator can be physically absent.  That is an honest partial
        # survey, not a failed survey and not a reason to prescribe a rerun.
        state = ValidatorState.PARTIAL
    elif state is ValidatorState.UNAVAILABLE:
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
    harness_failure = analysis_code in _ANALYSIS_HARNESS_FAILURE_CODES
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
        control_disposition=(
            GateControlDisposition.HARNESS_RECOVERY_REQUIRED
            if harness_failure
            else GateControlDisposition.TERMINAL_CLAIMABLE
        ),
        blocker_owner=BlockerOwner.HARNESS if harness_failure else BlockerOwner.NONE,
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
            "Build evidence is not green; artifact and module-coverage facts identify "
            "the unresolved scope.",
            "An external impediment requires evidence references; project failures "
            "remain project-owned.",
        )

    # Agent-facing coverage checklist (live 2026-07-18: one bigtop run gave up
    # with islands unattempted because the gate only said "evidence is green";
    # another fixated on a broken island while three healthy ones sat
    # untouched). The gate response NAMES what built and what has no output —
    # on acceptance too, not only on rejection. Same computation the finalizer
    # folds at evidence-close (sag.agent.module_coverage): one algorithm, so
    # mid-run guidance can never disagree with the sealed verdict.
    # Plan 8 §3.5: `shared_module_scan` returns the scan `validate_build_status`
    # just decided on, so the checklist half of this sentence and the verdict
    # half cannot come from two different walks of the tree.
    from sag.agent.module_coverage import coverage_checklist_line, shared_module_scan

    requirements: Mapping[str, Any] | None = None
    islands = None
    if orchestrator is not None:
        try:
            from sag.tools.internal.build_preflight import read_live_build_requirements

            manifest_read = read_live_build_requirements(orchestrator)
            if manifest_read.complete and manifest_read.conflict is None:
                requirements = manifest_read.payload or {}
                islands = requirements.get("build_islands")
        except Exception:
            requirements = None
            islands = None
    checklist = coverage_checklist_line(
        shared_module_scan(validator, project_name), islands=islands
    )
    if checklist:
        reason = f"{reason} · {checklist}"
        if "no output yet" in checklist or "remaining:" in checklist:
            suggestions = (
                *suggestions,
                "Modules without build output remain (see the coverage line); they "
                "cannot be counted as green terminal evidence.",
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
    derived = _gate_domain_states(
        orchestrator,
        requirements,
        receipt_scope=_live_receipt_scope(validator, orchestrator, requirements),
    )
    if derived.states is not None:
        validated_facts["build.domain_states"] = derived.states
    if derived.conflicts:
        # Plan 6 Stage 0: a record we could not read is named, never silently
        # dropped. Absent when the read was clean, so single-domain and
        # recorded-replay runs seal the pre-Plan-6 fact set byte-identically.
        validated_facts["build.evidence_conflicts"] = list(derived.conflicts)
    evidence_integrity_failure = any(
        conflict.startswith(("build_requirements_", "receipt_", "assessment_"))
        for conflict in derived.conflicts
    )
    if evidence_integrity_failure:
        state = ValidatorState.UNAVAILABLE
        reason = "build evidence ledger integrity is unavailable: " + ", ".join(derived.conflicts)
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=(
            "build_evidence_ledger_unavailable"
            if evidence_integrity_failure
            else f"build_{state.value}"
        ),
        validated_facts=validated_facts,
        control_disposition=(
            GateControlDisposition.HARNESS_RECOVERY_REQUIRED
            if evidence_integrity_failure
            else GateControlDisposition.TERMINAL_CLAIMABLE
        ),
        blocker_owner=(BlockerOwner.HARNESS if evidence_integrity_failure else BlockerOwner.NONE),
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
                "The named receipt is unreadable; a truncated or partial write blocks "
                "test closure.",
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
            "Test execution or report evidence is not green; the recorded counts and "
            "evidence references define the unresolved scope.",
            "An external impediment requires evidence references; project failures "
            "remain project-owned.",
        )
    rollup = _validated_test_rollup(status)
    if rollup is not None:
        # Plan 5 Task C2: the domain decomposition scopes the test rollup the
        # same way it scopes the build one. Key absent when no domains were
        # surveyed (established absent-when-inapplicable pattern above).
        requirements: Mapping[str, Any] | None = None
        if orchestrator is not None:
            try:
                from sag.tools.internal.build_preflight import read_live_build_requirements

                manifest_read = read_live_build_requirements(orchestrator)
                if manifest_read.complete and manifest_read.conflict is None:
                    requirements = manifest_read.payload or {}
            except Exception:
                requirements = None
        derived = _gate_domain_states(
            orchestrator,
            requirements,
            receipt_scope=_live_receipt_scope(validator, orchestrator, requirements),
        )
        if derived.states is not None:
            rollup["domain_states"] = derived.states
        if derived.conflicts:
            # Plan 6 Stage 0: named evidence-read conflicts ride the rollup's
            # existing conflicts channel into the sealed snapshot.
            rollup["conflicts"] = list(
                dict.fromkeys([*(rollup.get("conflicts") or ()), *derived.conflicts])
            )
        evidence_integrity_failure = any(
            conflict.startswith(("build_requirements_", "receipt_", "assessment_"))
            for conflict in derived.conflicts
        )
        if evidence_integrity_failure:
            state = ValidatorState.UNAVAILABLE
            code = "test_evidence_ledger_unavailable"
            reason = "test evidence ledger integrity is unavailable: " + ", ".join(
                derived.conflicts
            )
    else:
        evidence_integrity_failure = False
    return _ValidatorObservation(
        state,
        reason=reason,
        evidence_refs=_status_refs(status),
        suggestions=suggestions,
        code=code,
        validated_facts={"test.stats": rollup} if rollup is not None else {},
        control_disposition=(
            GateControlDisposition.HARNESS_RECOVERY_REQUIRED
            if evidence_integrity_failure
            else GateControlDisposition.TERMINAL_CLAIMABLE
        ),
        blocker_owner=(BlockerOwner.HARNESS if evidence_integrity_failure else BlockerOwner.NONE),
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
            suggestions=("A persisted setup-report artifact is required for closure.",),
            code="report_missing",
        )
    return _ValidatorObservation(
        ValidatorState.GREEN,
        reason="report artifact exists",
        evidence_refs=(report_ref,),
        code="report_present",
    )
