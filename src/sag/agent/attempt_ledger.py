"""Rolling compaction of phase history into an attempt ledger (spec §3.2).

Old ACTION steps become one lifecycle-aware line each so a
long debugging phase stays inside a small window while every attempt
(especially failures) remains visible. Old thoughts drop: the lasting
record of judgment is the ledger line + notes, not the musing.

The ledger ACCUMULATES across waves: when a prior wave's ledger step itself
ages into the compacted slice, its lines merge into the new ledger instead
of vanishing — failed approaches stay visible for the whole phase. The size
cap sheds terminal non-failures first and never sheds failed or pending lines."""

import re
from typing import List, Optional, Tuple

LEDGER_HEADER = (
    "ATTEMPT LEDGER (older work, compacted — do NOT retry ✗ entries the same way; "
    "… pending handoffs require polling unless a later terminal entry closes the same ref):"
)
MAX_LEDGER_LINES = 60
_JOB_REF_PATTERN = re.compile(r"\bjob:[A-Za-z0-9_-]+\b")


def _step_kind(step) -> str:
    kind = getattr(getattr(step, "step_type", None), "value", None)
    return kind or str(getattr(step, "step_type", ""))


def _previous_ledger_lines(step) -> List[str]:
    """Lines carried by a prior wave's ledger step ([] for any other step)."""
    content = getattr(step, "content", "") or ""
    if "ATTEMPT LEDGER" not in content:
        return []
    return [line for line in content.split("\n")[1:] if line.strip()]


def _job_refs(line: str) -> set[str]:
    _, separator, ref_text = line.rpartition(" → ")
    return set(_JOB_REF_PATTERN.findall(ref_text)) if separator else set()


def _terminal_poll_refs(steps: List) -> set[str]:
    refs = set()
    for step in steps:
        result = getattr(step, "tool_result", None)
        poll_ref = getattr(result, "poll_ref", None)
        invocation = getattr(getattr(result, "invocation_status", None), "value", None)
        if poll_ref and invocation != "pending":
            refs.add(poll_ref)
    return refs


def _reconcile_job_lifecycle(
    lines: List[str], terminal_refs: set[str]
) -> Tuple[List[str], set[str]]:
    pending_refs = {
        ref for line in lines if line.startswith("…") for ref in _job_refs(line)
    }
    terminal_line_refs = {
        ref for line in lines if not line.startswith("…") for ref in _job_refs(line)
    }
    closed_refs = pending_refs & (terminal_refs | terminal_line_refs)
    reconciled = [
        line
        for line in lines
        if not (line.startswith("…") and bool(_job_refs(line) & closed_refs))
    ]
    return reconciled, pending_refs & terminal_line_refs


def _cap_lines(lines: List[str], protected_job_refs: set[str]) -> List[str]:
    """Drop oldest terminal non-failures first; failed/pending lines survive."""
    overflow = len(lines) - MAX_LEDGER_LINES
    if overflow <= 0:
        return lines
    kept = []
    for line in lines:
        protected = line.startswith(("✗", "…")) or bool(
            _job_refs(line) & protected_job_refs
        )
        if overflow > 0 and not protected:
            overflow -= 1
            continue
        kept.append(line)
    return kept


def _result_marker(result) -> Tuple[str, str]:
    invocation = getattr(getattr(result, "invocation_status", None), "value", None)
    outcome = getattr(getattr(result, "operation_outcome", None), "value", None)
    if invocation == "pending":
        return "…", "PENDING"
    return {
        "success": ("✓", "SUCCESS"),
        "failed": ("✗", "FAILED"),
        "partial": ("~", "PARTIAL"),
        "unknown": ("?", "UNKNOWN"),
        "skipped": ("-", "SKIPPED"),
    }.get(outcome, ("?", "UNKNOWN"))


def compact_steps(steps: List, keep_recent: int = 30) -> Tuple[Optional[str], List]:
    """Returns (ledger_text or None, remaining_steps)."""
    if len(steps) <= keep_recent:
        return None, list(steps)

    old, recent = steps[:-keep_recent], steps[-keep_recent:]
    lines = []
    for step in old:
        if "action" not in _step_kind(step).lower():
            # A prior wave's ledger ages out like any old step — merge its
            # lines (it sits first in the window, so chronology is preserved).
            lines.extend(_previous_ledger_lines(step))
            continue
        result = getattr(step, "tool_result", None)
        marker, state = _result_marker(result)
        summary = (
            (getattr(result, "output", "") or "")[:90]
            .replace("\n", " ")
            .replace("→", "->")
        )
        metadata = getattr(result, "metadata", None) or {}
        refs = []
        for ref_id in (
            getattr(result, "poll_ref", None),
            metadata.get("output_ref_id"),
            getattr(result, "output_ref", None),
        ):
            if ref_id and ref_id not in refs:
                refs.append(ref_id)
        ref_text = f" → {', '.join(refs)}" if refs else ""
        lines.append(
            f"{marker} {getattr(step, 'tool_name', '?')} [{state}]: {summary}{ref_text}"
        )

    lines, protected_job_refs = _reconcile_job_lifecycle(
        lines,
        _terminal_poll_refs(steps),
    )
    if not lines:
        return None, recent
    ledger = LEDGER_HEADER + "\n" + "\n".join(_cap_lines(lines, protected_job_refs))
    return ledger, recent
