"""Engine-owned phase skeleton for `sag project` (spec §3.1).

Descriptive, never prescriptive: the machine tracks which phase the run is
in and what evidence ended each phase. It never restricts tools. `blocked`
is always accepted — an unforeseen condition degrades into an honest
partial/failed verdict, never a fight (the round-1..3 lesson)."""

from dataclasses import dataclass, field
from typing import List, Optional

PHASE_NAMES = ["provision", "analyze", "build", "test", "report"]

# Phases whose block degrades the run to failed (build) or partial (test).
CRITICAL_PHASE = "build"
CORE_PHASES = ("build", "test")


@dataclass
class PhaseRecord:
    name: str
    status: str  # "done" | "blocked"
    key_results: str = ""
    reason: str = ""
    evidence: List[str] = field(default_factory=list)


class PhaseMachine:
    def __init__(self):
        self._index = 0
        self.records: List[PhaseRecord] = []

    @property
    def current_phase(self) -> Optional[str]:
        return PHASE_NAMES[self._index] if self._index < len(PHASE_NAMES) else None

    @property
    def is_complete(self) -> bool:
        return self._index >= len(PHASE_NAMES)

    def _advance(self, record: PhaseRecord):
        if self.is_complete:
            raise RuntimeError("phase machine already complete")
        self.records.append(record)
        self._index += 1

    def mark_done(self, key_results: str, evidence: List[str]):
        self._advance(PhaseRecord(
            name=self.current_phase, status="done",
            key_results=key_results or "", evidence=list(evidence or []),
        ))

    def mark_blocked(self, reason: str, evidence: List[str]):
        self._advance(PhaseRecord(
            name=self.current_phase, status="blocked",
            reason=reason or "", evidence=list(evidence or []),
        ))

    def overall_outcome(self) -> str:
        """success | partial | failed — consumed by the run verdict."""
        blocked = {r.name for r in self.records if r.status == "blocked"}
        if CRITICAL_PHASE in blocked:
            return "failed"
        if blocked & set(CORE_PHASES):
            return "partial"
        if blocked:
            return "partial"
        return "success"

    def digest_lines(self) -> List[str]:
        """Compact trunk picture for the phase-start window (GTD digest)."""
        lines = []
        for r in self.records:
            if r.status == "done":
                lines.append(f"✓ {r.name}: {r.key_results[:200]}")
            else:
                lines.append(f"⛔ {r.name} BLOCKED: {r.reason[:200]}")
        if not self.is_complete:
            lines.append(f"→ current: {self.current_phase}")
        return lines
