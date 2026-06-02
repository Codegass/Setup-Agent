"""Typed read models for CLI/TUI run state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Optional

from sag.ui.events import PhaseType

PhaseStatus = Literal["pending", "running", "success", "error", "skipped"]
FailureClassification = Literal[
    "tool_failure",
    "command_timeout",
    "parameter_normalization",
    "recovery_attempt",
    "verification_failure",
    "warning",
    "final_failure",
]
TimelineKind = Literal[
    "phase",
    "step",
    "status",
    "thought",
    "tool",
    "observation",
    "stream",
    "recovery",
    "warning",
    "error",
    "evidence",
    "report",
    "completion",
]


@dataclass(frozen=True, slots=True)
class PhaseSnapshot:
    phase: PhaseType
    status: PhaseStatus = "pending"
    steps: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    tool_name: Optional[str] = None
    action: Optional[str] = None
    workdir: Optional[str] = None
    visible_params: str = ""
    started_at: Optional[datetime] = None
    detail: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UITimelineEntry:
    timestamp: datetime
    kind: TimelineKind | str
    message: str
    level: str = "info"
    details: Optional[str] = None
    failure_classification: Optional[FailureClassification | str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UIEvidenceRecord:
    timestamp: datetime
    kind: str
    summary: str
    details: Optional[str] = None
    path: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    active: bool = False
    strategy: Optional[str] = None
    retry_count: int = 0
    message: Optional[str] = None
    unresolved_risk: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UIRunState:
    project_name: str
    start_time: datetime
    current_phase: Optional[PhaseType]
    phases: tuple[PhaseSnapshot, ...]
    current_status: str = "Initializing"
    active_operation: ActiveOperation = field(default_factory=ActiveOperation)
    recovery: RecoverySnapshot = field(default_factory=RecoverySnapshot)
    timeline: tuple[UITimelineEntry, ...] = ()
    evidence: tuple[UIEvidenceRecord, ...] = ()
    latest_error: Optional[UITimelineEntry] = None
    latest_warning: Optional[UITimelineEntry] = None
    is_complete: bool = False
    final_status: Optional[str] = None
    report_data: Optional[dict[str, Any]] = None

    def with_phase(self, phase: PhaseType, status: PhaseStatus) -> "UIRunState":
        phases = tuple(
            replace(item, status=status) if item.phase == phase else item for item in self.phases
        )
        return replace(self, phases=phases, current_phase=phase)


def initial_run_state(project_name: str, start_time: datetime) -> UIRunState:
    return UIRunState(
        project_name=project_name,
        start_time=start_time,
        current_phase=None,
        phases=tuple(PhaseSnapshot(phase=phase) for phase in PhaseType),
    )
