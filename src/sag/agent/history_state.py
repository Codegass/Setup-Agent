"""Canonical decoding for persisted branch-history action states."""

from enum import Enum
from typing import Any, Mapping


class HistoryActionState(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


def decode_history_action_state(entry: Mapping[str, Any]) -> HistoryActionState:
    """Decode canonical axes first, then current and legacy persisted booleans."""
    invocation = entry.get("invocation_status")
    outcome = entry.get("operation_outcome")
    if invocation is not None or outcome is not None:
        if invocation == "pending":
            return HistoryActionState.PENDING
        if invocation == "completed" and outcome == "success":
            return HistoryActionState.SUCCESS
        if outcome == "failed":
            return HistoryActionState.FAILED
        return HistoryActionState.UNKNOWN

    if isinstance(entry.get("succeeded"), bool):
        return HistoryActionState.SUCCESS if entry["succeeded"] else HistoryActionState.FAILED
    if isinstance(entry.get("success"), bool):
        return HistoryActionState.SUCCESS if entry["success"] else HistoryActionState.FAILED
    return HistoryActionState.UNKNOWN
