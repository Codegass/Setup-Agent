"""Rolling compaction of phase history into an attempt ledger (spec §3.2).

Old ACTION steps become one line each — `tool ✓/✗ summary → ref` — so a
long debugging phase stays inside a small window while every attempt
(especially failures) remains visible. Old thoughts drop: the lasting
record of judgment is the ledger line + notes, not the musing."""

from typing import List, Optional, Tuple


def _step_kind(step) -> str:
    kind = getattr(getattr(step, "step_type", None), "value", None)
    return kind or str(getattr(step, "step_type", ""))


def compact_steps(steps: List, keep_recent: int = 30) -> Tuple[Optional[str], List]:
    """Returns (ledger_text or None, remaining_steps)."""
    if len(steps) <= keep_recent:
        return None, list(steps)

    old, recent = steps[:-keep_recent], steps[-keep_recent:]
    lines = []
    for step in old:
        if "action" not in _step_kind(step).lower():
            continue
        result = getattr(step, "tool_result", None)
        ok = bool(getattr(result, "success", False))
        summary = (getattr(result, "output", "") or "")[:90].replace("\n", " ")
        ref = ""
        metadata = getattr(result, "metadata", None) or {}
        ref_id = metadata.get("output_ref_id")
        if ref_id:
            ref = f" → {ref_id}"
        lines.append(f"{'✓' if ok else '✗'} {getattr(step, 'tool_name', '?')}: {summary}{ref}")

    if not lines:
        return None, recent
    ledger = "ATTEMPT LEDGER (older work, compacted — do NOT retry ✗ entries the same way):\n" + \
             "\n".join(lines[-60:])
    return ledger, recent
