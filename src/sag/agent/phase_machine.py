"""Engine-owned phase lifecycle records for ``sag project``."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

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
class PhaseAttemptRecord:
    phase: str
    attempt_id: str
    termination: PhaseTermination | str
    outcome: PhaseOutcome | str
    transition: str = ""
    key_results: str = ""
    reason: str = ""
    evidence: List[str] = field(default_factory=list)
    legacy_claim: bool = False

    def __post_init__(self) -> None:
        termination = PhaseTermination(self.termination)
        outcome = PhaseOutcome(self.outcome)
        if outcome not in LEGAL_PHASE_STATES[termination]:
            raise ValueError(
                f"illegal phase state: termination={termination.value}, outcome={outcome.value}"
            )
        object.__setattr__(self, "termination", termination)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "evidence", list(self.evidence))


@dataclass(frozen=True)
class PhaseSkipRecord(PhaseAttemptRecord):
    termination: PhaseTermination | str = field(default=PhaseTermination.SKIPPED, init=False)
    outcome: PhaseOutcome | str = field(default=PhaseOutcome.SKIPPED, init=False)


class PhaseMachine:
    def __init__(self):
        self._index = 0
        self.records: List[PhaseAttemptRecord] = []

    @property
    def current_phase(self) -> Optional[str]:
        return PHASE_NAMES[self._index] if self._index < len(PHASE_NAMES) else None

    @property
    def is_complete(self) -> bool:
        return self._index >= len(PHASE_NAMES)

    def _advance(self, record: PhaseAttemptRecord) -> None:
        if self.is_complete:
            raise RuntimeError("phase machine already complete")
        if record.phase != self.current_phase:
            raise ValueError(
                f"phase record for {record.phase!r} cannot advance {self.current_phase!r}"
            )
        self.records.append(record)
        self._index += 1

    def _attempt_id(self) -> str:
        return f"{self.current_phase}-{len(self.records) + 1}"

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

    def termination_state(self) -> str:
        """Report flow closure only; phase outcomes never become a run verdict."""
        if any(record.termination is PhaseTermination.ABORTED for record in self.records):
            return "aborted"
        if self.is_complete:
            return "completed"
        return "open"

    def digest_lines(self) -> List[str]:
        """Compact trunk picture for the phase-start window (GTD digest)."""
        lines = []
        for record in self.records:
            if record.termination is PhaseTermination.COMPLETED:
                lines.append(f"✓ {record.phase}: {record.key_results[:200]}")
            else:
                lines.append(f"⛔ {record.phase} BLOCKED: {record.reason[:200]}")
        if not self.is_complete:
            lines.append(f"→ current: {self.current_phase}")
        return lines
