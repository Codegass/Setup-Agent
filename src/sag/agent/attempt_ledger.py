"""Rolling compaction of phase history into an attempt ledger (spec §3.2).

Old ACTION steps become one line each — `tool ✓/✗ summary → ref` — so a
long debugging phase stays inside a small window while every attempt
(especially failures) remains visible. Old thoughts drop: the lasting
record of judgment is the ledger line + notes, not the musing.

The ledger ACCUMULATES across waves: when a prior wave's ledger step itself
ages into the compacted slice, its lines merge into the new ledger instead
of vanishing — failed approaches stay visible for the whole phase. The size
cap sheds oldest ✓ lines first and never sheds ✗ lines."""

from typing import List, Optional, Tuple

LEDGER_HEADER = "ATTEMPT LEDGER (older work, compacted — do NOT retry ✗ entries the same way):"
MAX_LEDGER_LINES = 60


def _step_kind(step) -> str:
    kind = getattr(getattr(step, "step_type", None), "value", None)
    return kind or str(getattr(step, "step_type", ""))


def _previous_ledger_lines(step) -> List[str]:
    """Lines carried by a prior wave's ledger step ([] for any other step)."""
    content = getattr(step, "content", "") or ""
    if "ATTEMPT LEDGER" not in content:
        return []
    return [line for line in content.split("\n")[1:] if line.strip()]


def _cap_lines(lines: List[str]) -> List[str]:
    """Cap the ledger by dropping oldest ✓ lines first; ✗ lines never drop."""
    overflow = len(lines) - MAX_LEDGER_LINES
    if overflow <= 0:
        return lines
    kept = []
    for line in lines:
        if overflow > 0 and not line.startswith("✗"):
            overflow -= 1
            continue
        kept.append(line)
    return kept


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
        ok = bool(getattr(result, "succeeded", False))
        summary = (getattr(result, "output", "") or "")[:90].replace("\n", " ")
        ref = ""
        metadata = getattr(result, "metadata", None) or {}
        ref_id = metadata.get("output_ref_id")
        if ref_id:
            ref = f" → {ref_id}"
        lines.append(f"{'✓' if ok else '✗'} {getattr(step, 'tool_name', '?')}: {summary}{ref}")

    if not lines:
        return None, recent
    ledger = LEDGER_HEADER + "\n" + "\n".join(_cap_lines(lines))
    return ledger, recent
