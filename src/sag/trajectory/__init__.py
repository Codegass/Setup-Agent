"""Read-only derivation of a run's trajectory from the authoritative layer.

One incremental reducer serves both feeds (spec §1): live tail-follow and
post-hoc batch replay fold the same events through the same code path. This
package never writes, reorders, or mutates a source file, and never reads a
console log — control events and the other authoritative artifacts are the
only inputs.
"""

from sag.trajectory.builder import build_trajectory, follow_trajectory
from sag.trajectory.reducer import TrajectoryReducer
from sag.trajectory.schema import (
    DETAIL_TIERS,
    SCHEMA_VERSION,
    Actor,
    Annotation,
    AnnotationKind,
    CallInfo,
    DetailTier,
    GateInfo,
    ObservationInfo,
    PhaseInfo,
    SessionInfo,
    TokenUsage,
    Trajectory,
    TrajectoryDelta,
    Turn,
    Warning,
)

__all__ = [
    "SCHEMA_VERSION",
    "DETAIL_TIERS",
    "Actor",
    "Annotation",
    "AnnotationKind",
    "CallInfo",
    "DetailTier",
    "GateInfo",
    "ObservationInfo",
    "PhaseInfo",
    "SessionInfo",
    "TokenUsage",
    "Trajectory",
    "TrajectoryDelta",
    "TrajectoryReducer",
    "Turn",
    "Warning",
    "build_trajectory",
    "follow_trajectory",
]
