"""Engine-owned phase lifecycle records for ``sag project``."""

from dataclasses import InitVar, dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, List, Mapping, Optional

if TYPE_CHECKING:
    from .phase_gates import GateResult
    from .phase_transitions import TransitionDecision

PHASE_NAMES = ["provision", "analyze", "build", "test", "report"]


class PhaseTermination(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABORTED = "aborted"
    SKIPPED = "skipped"


class PhaseOutcome(str, Enum):
    UNKNOWN = "unknown"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


LEGAL_PHASE_STATES = {
    PhaseTermination.RUNNING: {PhaseOutcome.UNKNOWN},
    PhaseTermination.COMPLETED: {
        PhaseOutcome.UNKNOWN,
        PhaseOutcome.SUCCESS,
        PhaseOutcome.PARTIAL,
        PhaseOutcome.FAILED,
    },
    PhaseTermination.BLOCKED: {
        PhaseOutcome.UNKNOWN,
        PhaseOutcome.PARTIAL,
        PhaseOutcome.FAILED,
    },
    PhaseTermination.ABORTED: {
        PhaseOutcome.UNKNOWN,
        PhaseOutcome.PARTIAL,
        PhaseOutcome.FAILED,
    },
    PhaseTermination.SKIPPED: {PhaseOutcome.SKIPPED},
}


@dataclass(frozen=True)
class PhaseClaim:
    """A model-authored terminal claim kept intact for audit.

    Claims never become evidence merely by reaching the phase tool.  The gate
    supplies the separately stored validated outcome.
    """

    phase: str
    claimed_outcome: PhaseOutcome | str
    signal: str = "done"
    key_results: str = ""
    reason: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        signal = str(self.signal).strip().lower()
        if signal not in {"done", "blocked"}:
            raise ValueError(f"terminal phase claim has invalid signal: {self.signal!r}")
        claimed_outcome = PhaseOutcome(self.claimed_outcome)
        if claimed_outcome is PhaseOutcome.SKIPPED:
            raise PermissionError("only the transition policy may claim skipped phases")
        object.__setattr__(self, "signal", signal)
        object.__setattr__(self, "claimed_outcome", claimed_outcome)
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def to_metadata(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "signal": self.signal,
            "claimed_outcome": self.claimed_outcome.value,
            "key_results": self.key_results,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> "PhaseClaim":
        if not isinstance(value, Mapping):
            raise TypeError("phase claim metadata must be a mapping")
        evidence_refs = value.get("evidence_refs") or ()
        if isinstance(evidence_refs, str) or not isinstance(evidence_refs, (list, tuple)):
            raise TypeError("phase claim evidence refs must be a list")
        return cls(
            phase=str(value.get("phase") or ""),
            signal=str(value.get("signal") or "done"),
            claimed_outcome=value.get("claimed_outcome", PhaseOutcome.UNKNOWN),
            key_results=str(value.get("key_results") or ""),
            reason=str(value.get("reason") or ""),
            evidence_refs=tuple(evidence_refs),
        )


@dataclass(frozen=True)
class PhaseAttemptRecord:
    phase: str
    attempt_id: str
    termination: PhaseTermination | str
    outcome: PhaseOutcome | str
    transition: Optional[str] = None
    key_results: str = ""
    reason: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    claim: Optional[PhaseClaim] = None
    validated_outcome: PhaseOutcome | str | None = None
    claim_disposition: Optional[str] = None
    legacy_claim: bool = False

    def __post_init__(self) -> None:
        termination = PhaseTermination(self.termination)
        outcome = PhaseOutcome(self.outcome)
        validated_outcome = (
            outcome
            if self.validated_outcome is None
            else PhaseOutcome(self.validated_outcome)
        )
        if outcome is not validated_outcome:
            raise ValueError("phase record outcome must equal its validated outcome")
        if outcome not in LEGAL_PHASE_STATES[termination]:
            raise ValueError(
                f"illegal phase state: termination={termination.value}, outcome={outcome.value}"
            )
        object.__setattr__(self, "termination", termination)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "validated_outcome", validated_outcome)
        evidence = tuple(self.evidence)
        evidence_refs = tuple(self.evidence_refs) or evidence
        if evidence and evidence_refs != evidence:
            raise ValueError("phase record evidence aliases disagree")
        object.__setattr__(self, "evidence", evidence_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        if self.claim is not None and self.claim.phase and self.claim.phase != self.phase:
            raise ValueError("phase record claim belongs to a different phase")

    @property
    def is_terminal(self) -> bool:
        return self.termination is not PhaseTermination.RUNNING


@dataclass(frozen=True)
class PhaseSkipRecord(PhaseAttemptRecord):
    termination: PhaseTermination | str = field(default=PhaseTermination.SKIPPED, init=False)
    outcome: PhaseOutcome | str = field(default=PhaseOutcome.SKIPPED, init=False)
    prerequisite_ref: str = ""
    _policy_token: InitVar[object | None] = None

    def __post_init__(self, _policy_token: object | None) -> None:
        if _policy_token is not _PHASE_POLICY_TOKEN:
            raise PermissionError("only the transition policy may construct skipped phase records")
        super().__post_init__()


_PHASE_POLICY_TOKEN = object()


def _policy_skip_record(
    *,
    phase: str,
    attempt_id: str,
    reason: str,
    evidence_refs: tuple[str, ...] = (),
    prerequisite_ref: str = "",
) -> PhaseSkipRecord:
    """Restricted construction seam used only by ``PhaseTransitionPolicy``."""
    return PhaseSkipRecord(
        phase=phase,
        attempt_id=attempt_id,
        transition="policy",
        reason=reason,
        evidence_refs=evidence_refs,
        prerequisite_ref=prerequisite_ref,
        _policy_token=_PHASE_POLICY_TOKEN,
    )


class PhaseMachine:
    def __init__(self, *, start_phase: str = "provision"):
        if start_phase not in PHASE_NAMES:
            raise ValueError(f"unknown start phase: {start_phase}")
        self._current_phase: Optional[str] = start_phase
        self._flow_closed = False
        self._records: List[PhaseAttemptRecord] = []
        self._attempt_counts = {phase: 0 for phase in PHASE_NAMES}
        self._current_attempt_id = self._open_attempt(start_phase)
        self._pending_record: Optional[PhaseAttemptRecord] = None

    @property
    def records(self) -> tuple[PhaseAttemptRecord, ...]:
        return tuple(self._records)

    @property
    def current_phase(self) -> Optional[str]:
        return self._current_phase

    @property
    def current_attempt_id(self) -> Optional[str]:
        return self._current_attempt_id

    @property
    def current_record(self) -> Optional[PhaseAttemptRecord]:
        if self._pending_record is not None:
            return self._pending_record
        if (
            self._records
            and self._records[-1].attempt_id == self._current_attempt_id
            and self._records[-1].is_terminal
        ):
            return self._records[-1]
        if self._current_phase is None or self._current_attempt_id is None:
            return None
        return PhaseAttemptRecord(
            phase=self._current_phase,
            attempt_id=self._current_attempt_id,
            termination=PhaseTermination.RUNNING,
            outcome=PhaseOutcome.UNKNOWN,
        )

    @property
    def is_complete(self) -> bool:
        return self._flow_closed

    def _open_attempt(self, phase: str) -> str:
        self._attempt_counts[phase] += 1
        return f"{phase}-{self._attempt_counts[phase]}"

    def _advance(self, record: PhaseAttemptRecord) -> None:
        """Replay-only adapter for pre-WS3 transcripts and fixtures."""
        if self._pending_record is not None:
            raise RuntimeError("pending validated attempt must be routed before replay")
        self._append(record)
        index = PHASE_NAMES.index(record.phase)
        if index + 1 >= len(PHASE_NAMES):
            self._current_phase = None
            self._current_attempt_id = None
            self._flow_closed = True
            return
        target = PHASE_NAMES[index + 1]
        self._current_phase = target
        self._current_attempt_id = self._open_attempt(target)

    def _append(self, record: PhaseAttemptRecord) -> None:
        if self.is_complete:
            raise RuntimeError("phase machine already complete")
        if record.phase != self.current_phase:
            raise ValueError(
                f"phase record for {record.phase!r} cannot advance {self.current_phase!r}"
            )
        self._records.append(record)

    def _attempt_id(self) -> str:
        if self._current_attempt_id is None:
            raise RuntimeError("phase machine has no open attempt")
        return self._current_attempt_id

    def close_attempt(
        self,
        validation: "GateResult",
        *,
        termination: PhaseTermination | str | None = None,
    ) -> PhaseAttemptRecord:
        """Close the open attempt pending a separate transition-policy decision."""
        if self.is_complete or self.current_phase is None:
            raise RuntimeError("phase machine already complete")
        if self._pending_record is not None:
            raise RuntimeError("phase attempt already awaits a transition decision")
        if not validation.accepted:
            raise ValueError("a rejected phase claim cannot close an attempt")
        claim = validation.claim
        if claim is None:
            raise ValueError("gate result must retain the phase claim")
        expected_phase = claim.phase or self.current_phase
        if expected_phase != self.current_phase:
            raise ValueError(
                f"phase claim for {expected_phase!r} cannot close {self.current_phase!r}"
            )
        resolved_termination = termination or (
            PhaseTermination.BLOCKED
            if claim.signal == "blocked"
            else PhaseTermination.COMPLETED
        )
        record = PhaseAttemptRecord(
            phase=self.current_phase,
            attempt_id=self._attempt_id(),
            termination=resolved_termination,
            outcome=validation.validated_outcome,
            transition=None,
            key_results=claim.key_results,
            reason=claim.reason or validation.reason,
            evidence_refs=tuple(dict.fromkeys((*claim.evidence_refs, *validation.evidence_refs))),
            claim=claim,
            validated_outcome=validation.validated_outcome,
            claim_disposition=validation.claim_disposition.value,
        )
        self._pending_record = record
        return record

    def apply(self, decision: "TransitionDecision") -> tuple[PhaseAttemptRecord, ...]:
        """Atomically append the pending attempt, skips, and open the decided route."""
        if self._pending_record is None:
            raise RuntimeError("no validated phase attempt awaits routing")
        route = decision.route
        pending = self._pending_record
        if route.source_attempt_id != pending.attempt_id:
            raise ValueError("transition source does not match the pending phase attempt")
        if route.kind in {"advance", "repair", "report"} and route.target not in PHASE_NAMES:
            raise ValueError(f"transition target is not a phase: {route.target!r}")
        skip_ids: set[str] = set()
        existing_ids = {record.attempt_id for record in self._records}
        for skip in decision.skips:
            if not isinstance(skip, PhaseSkipRecord):
                raise TypeError("transition skips must be PhaseSkipRecord instances")
            if skip.phase not in PHASE_NAMES:
                raise ValueError(f"transition skip is not a phase: {skip.phase!r}")
            if skip.attempt_id in existing_ids or skip.attempt_id in skip_ids:
                raise ValueError(f"duplicate phase attempt id: {skip.attempt_id}")
            skip_ids.add(skip.attempt_id)

        finalized = replace(pending, transition=route.kind)
        self._records.append(finalized)
        appended: list[PhaseAttemptRecord] = [finalized]
        for skip in decision.skips:
            self._records.append(skip)
            appended.append(skip)

        if route.kind in {"advance", "repair"}:
            self._current_phase = route.target
            self._current_attempt_id = self._open_attempt(route.target)
        elif route.kind == "evidence_close":
            self._current_phase = "report"
            self._current_attempt_id = self._open_attempt("report")
        elif route.kind == "report":
            self._current_phase = "report"
            self._current_attempt_id = self._open_attempt("report")
        elif route.kind == "flow_close":
            self._current_phase = None
            self._current_attempt_id = None
            self._flow_closed = True
        else:
            raise ValueError(f"unsupported phase route kind: {route.kind!r}")
        self._pending_record = None
        return tuple(appended)

    def record_model_skip(self, *, phase: str, reason: str) -> None:
        del phase, reason
        raise PermissionError("only the transition policy may construct a skipped phase record")

    def mark_done(self, key_results: str, evidence: List[str]) -> None:
        """Adapt a legacy done claim without inventing an evidence outcome."""
        self._advance(
            PhaseAttemptRecord(
                phase=self.current_phase,
                attempt_id=self._attempt_id(),
                termination=PhaseTermination.COMPLETED,
                outcome=PhaseOutcome.UNKNOWN,
                transition="advance",
                key_results=key_results or "",
                evidence=list(evidence or []),
                legacy_claim=True,
            )
        )

    def mark_blocked(self, reason: str, evidence: List[str]) -> None:
        """Adapt an external blocked claim without computing a run verdict."""
        self._advance(
            PhaseAttemptRecord(
                phase=self.current_phase,
                attempt_id=self._attempt_id(),
                termination=PhaseTermination.BLOCKED,
                outcome=PhaseOutcome.UNKNOWN,
                transition="advance",
                reason=reason or "",
                evidence=list(evidence or []),
                legacy_claim=True,
            )
        )

    def record_abort(self, reason: str, evidence: List[str]) -> PhaseAttemptRecord:
        """Record an abnormal exit for the current attempt without advancing."""
        if self._records and self._records[-1].termination is PhaseTermination.ABORTED:
            return self._records[-1]
        record = PhaseAttemptRecord(
            phase=self.current_phase,
            attempt_id=self._attempt_id(),
            termination=PhaseTermination.ABORTED,
            outcome=PhaseOutcome.UNKNOWN,
            transition="abort",
            reason=reason or "setup phase aborted",
            evidence=list(evidence or []),
        )
        self._pending_record = None
        self._append(record)
        return record

    def termination_state(self) -> str:
        """Report flow closure only; phase outcomes never become a run verdict."""
        if any(record.termination is PhaseTermination.ABORTED for record in self._records):
            return "aborted"
        if self.is_complete:
            return "completed"
        return "open"

    def digest_lines(self) -> List[str]:
        """Compact trunk picture for the phase-start window (GTD digest)."""
        lines = []
        for record in self._records:
            if record.termination is PhaseTermination.COMPLETED:
                marker = "✓" if record.outcome is PhaseOutcome.SUCCESS else "•"
                summary = record.key_results or record.reason
                lines.append(
                    f"{marker} {record.phase} [{record.outcome.value}]: {summary[:200]}"
                )
            elif record.termination is PhaseTermination.BLOCKED:
                lines.append(
                    f"⛔ {record.phase} BLOCKED [{record.outcome.value}]: "
                    f"{record.reason[:200]}"
                )
            elif record.termination is PhaseTermination.SKIPPED:
                lines.append(f"↷ {record.phase} SKIPPED: {record.reason[:200]}")
            else:
                lines.append(f"⛔ {record.phase} ABORTED: {record.reason[:200]}")
        if not self.is_complete:
            lines.append(f"→ current: {self.current_phase}")
        return lines
