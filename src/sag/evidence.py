"""Shared evidence state models for SAG tools, context, reports, and Web UI."""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field, field_serializer


class EvidenceStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
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
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    refs: list[str] = Field(default_factory=list)
    details: dict[str, object] = Field(default_factory=dict)

    @field_serializer("status")
    def _serialize_status(self, status: EvidenceStatus) -> str:
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
        return round((self.executed / self.discovered) * 100, 1)

    def as_summary(self) -> str:
        return (
            f"{self.passed} / {self.executed} passed, "
            f"{self.pass_rate:.1f}% pass rate, "
            f"{self.failed} failed, {self.skipped} skipped"
        )


def coerce_evidence_status(value: EvidenceStatus | str | None) -> EvidenceStatus:
    if isinstance(value, EvidenceStatus):
        return value
    if not value:
        return EvidenceStatus.UNKNOWN
    try:
        return EvidenceStatus(str(value).strip().lower())
    except ValueError:
        return EvidenceStatus.UNKNOWN


def aggregate_evidence_status(statuses: Iterable[EvidenceStatus | str | None]) -> EvidenceStatus:
    normalized = [coerce_evidence_status(status) for status in statuses]
    if not normalized:
        return EvidenceStatus.UNKNOWN
    for candidate in (
        EvidenceStatus.BLOCKED,
        EvidenceStatus.CONFLICT,
        EvidenceStatus.PARTIAL,
        EvidenceStatus.UNKNOWN,
    ):
        if candidate in normalized:
            return candidate
    return EvidenceStatus.SUCCESS
