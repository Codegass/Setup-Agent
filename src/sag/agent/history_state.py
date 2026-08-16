"""Canonical decoding for persisted branch-history action states."""

from enum import Enum
from typing import Any, Mapping, Optional

# The advisor's consult persists to phase history like any other action
# (closure-contract rule 2), which is what puts reviewer PROSE into the record
# the completion gates text-sniff: `_documents_unmet_requirement` reads
# observations for "requires java", `_has_remediation_action` reads actions for
# "openjdk"/"export java_home", and an advisor sentence naturally contains both.
# The entry states its own kind so a predicate can decline to read it — the
# `type: "thought"` exclusion those predicates already have, for the one other
# author of prose in the history.
ADVISOR_HISTORY_ENTRY_KIND = "advisor_consult"

# The tool whose output IS that prose. `advisor()` is model-callable, so the
# same reviewer text reaches phase history by two paths: the harness's
# phase-entry consult and an ordinary model-issued tool execution. The kind
# marks WHAT an entry is, never who asked for it, so it is resolved from the
# tool that produced the text and every write path gets it.
ADVISOR_TOOL_NAME = "advisor"


def history_entry_kind_for_tool(tool_name: Any) -> Optional[str]:
    """The kind a persisted action entry carries for the tool that wrote it.

    None for every tool that answers from the container: its output is
    evidence, and evidence is read.
    """
    if str(tool_name or "").strip().lower() == ADVISOR_TOOL_NAME:
        return ADVISOR_HISTORY_ENTRY_KIND
    return None


def is_advisor_history_entry(entry: Any) -> bool:
    """Whether a persisted history entry is the advisor's own consult record."""
    return isinstance(entry, Mapping) and entry.get("entry_kind") == ADVISOR_HISTORY_ENTRY_KIND


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
