"""Shared evidence state models for SAG tools, context, reports, and Web UI."""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field, field_serializer


class EvidenceAssessment(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class InvocationStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    CANCELLED = "cancelled"


class OperationOutcome(str, Enum):
    UNKNOWN = "unknown"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class EvidenceRef(BaseModel):
    ref: str
    kind: str = "output"
    source: str = ""
    task_id: str | None = None
    label: str = ""


class EvidenceFinding(BaseModel):
    type: str
    reason: str
    status: EvidenceAssessment = EvidenceAssessment.UNKNOWN
    refs: list[str] = Field(default_factory=list)
    details: dict[str, object] = Field(default_factory=dict)

    @field_serializer("status")
    def _serialize_status(self, status: EvidenceAssessment) -> str:
        return status.value


class TestStats(BaseModel):
    __test__ = False

    discovered: int | None = None
    executed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def pass_rate(self) -> float:
        if self.executed <= 0:
            return 0.0
        return round((self.passed / self.executed) * 100, 1)

    @property
    def execution_rate(self) -> float | None:
        if not self.discovered:
            return None
        # Clamp at 100%: `discovered` is a collect-only/static denominator, so
        # a re-run or parameterized drift can push `executed` past it (live
        # paramiko run 5: 559 detected, 560 executed read "100.2%"). Executed
        # >= discovered is full coverage; the raw counts are never altered.
        return min(round((self.executed / self.discovered) * 100, 1), 100.0)

    def as_summary(self) -> str:
        # Be explicit when nothing ran: "0 / 0 passed, 0.0% pass rate" reads like a
        # clean result and hides that a discovered suite was never executed. Report
        # the detected-but-not-executed case honestly so it cannot be mistaken for a
        # pass (Bigtop: 57 tests detected, 0 executed because the build produced no
        # classes). When tests DID run, the discovered/executed ratio is surfaced
        # separately via execution_rate, so the summary stays unchanged there.
        if self.executed <= 0:
            if self.discovered and self.discovered > 0:
                return f"0 of {self.discovered} detected tests executed (no tests ran)"
            return "no tests executed"
        return (
            f"{self.passed} / {self.executed} passed, "
            f"{self.pass_rate:.1f}% pass rate, "
            f"{self.failed} failed, {self.skipped} skipped"
        )


def coerce_evidence_status(value: EvidenceAssessment | str | None) -> EvidenceAssessment:
    if isinstance(value, EvidenceAssessment):
        return value
    if not value:
        return EvidenceAssessment.UNKNOWN
    try:
        return EvidenceAssessment(str(value).strip().lower())
    except ValueError:
        return EvidenceAssessment.UNKNOWN


def aggregate_evidence_status(
    statuses: Iterable[EvidenceAssessment | str | None],
) -> EvidenceAssessment:
    normalized = [coerce_evidence_status(status) for status in statuses]
    if not normalized:
        return EvidenceAssessment.UNKNOWN
    for candidate in (
        EvidenceAssessment.BLOCKED,
        EvidenceAssessment.CONFLICT,
        EvidenceAssessment.PARTIAL,
        EvidenceAssessment.UNKNOWN,
    ):
        if candidate in normalized:
            return candidate
    return EvidenceAssessment.SUCCESS
