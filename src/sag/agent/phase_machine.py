"""Engine-owned phase lifecycle records for ``sag project``."""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from .phase_gates import GateResult

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
    def from_metadata(cls, value: dict[str, Any]) -> "PhaseClaim":
        return cls(
            phase=str(value.get("phase") or ""),
            signal=str(value.get("signal") or "done"),
            claimed_outcome=value.get("claimed_outcome", PhaseOutcome.UNKNOWN),
            key_results=str(value.get("key_results") or ""),
            reason=str(value.get("reason") or ""),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
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


class PhaseMachine:
    def __init__(self):
        self._index = 0
        self._records: List[PhaseAttemptRecord] = []

    @property
    def records(self) -> tuple[PhaseAttemptRecord, ...]:
        return tuple(self._records)

    @property
    def current_phase(self) -> Optional[str]:
        return PHASE_NAMES[self._index] if self._index < len(PHASE_NAMES) else None

    @property
    def is_complete(self) -> bool:
        return self._index >= len(PHASE_NAMES)

    def _advance(self, record: PhaseAttemptRecord) -> None:
        self._append(record)
        self._index += 1

    def _append(self, record: PhaseAttemptRecord) -> None:
        if self.is_complete:
            raise RuntimeError("phase machine already complete")
        if record.phase != self.current_phase:
            raise ValueError(
                f"phase record for {record.phase!r} cannot advance {self.current_phase!r}"
            )
        self._records.append(record)

    def _attempt_id(self) -> str:
        return f"{self.current_phase}-{len(self._records) + 1}"

    def close_attempt(
        self,
        validation: "GateResult",
        *,
        termination: PhaseTermination | str | None = None,
    ) -> PhaseAttemptRecord:
        """Append a validated terminal record without selecting a transition."""
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
        self._append(record)
        return record

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
            else:
                lines.append(f"⛔ {record.phase} ABORTED: {record.reason[:200]}")
        if not self.is_complete:
            lines.append(f"→ current: {self.current_phase}")
        return lines
