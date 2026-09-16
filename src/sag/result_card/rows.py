"""One builder per row. Each copies what the run recorded and states absence.

No builder computes a judgment: a status word always comes from the seal, and
a count always comes from the seal or a sealed sibling artifact.
"""

from __future__ import annotations

from typing import Any

from sag.result_card.glosses import gloss
from sag.result_card.models import ROW_LABELS, ResultRow, ResultStats, Tone

_VERDICT_TONE: dict[str, Tone] = {
    "success": "success",
    "partial": "attention",
    "failed": "failed",
    "unknown": "attention",
}

_TASK_TONE: dict[str, Tone] = {
    "complete": "success",
    "incomplete": "failed",
    "unavailable": "attention",
    "not supplied": "neutral",
}

NOT_SUPPLIED = "not supplied"


def _join(*parts: str | None) -> str:
    return " · ".join(part for part in parts if part)


def _duration_text(seconds: float | None) -> str | None:
    """`6m 30s`, `41.0s`, `1h 04m`. Sub-minute keeps a decimal; hours drop seconds."""

    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    if total < 60:
        return f"{seconds:.1f}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _first_reason(reasons: Any) -> str | None:
    for reason in reasons or ():
        text = str(reason).strip()
        if text:
            return f"{gloss(text)} ({text})"
    return None


def setup_row(snapshot: Any, *, stats: ResultStats, termination: Any | None) -> ResultRow:
    """How the run went, as counts, plus anything unusual about how it ended."""

    verdict = str(snapshot.verdict)
    phases = None
    if stats.phases_total:
        phases = f"{stats.phases_completed or 0}/{stats.phases_total} phases"
    headline = _join(
        phases,
        f"{stats.turns:,} turns" if stats.turns is not None else None,
        f"{stats.tool_calls:,} tool calls" if stats.tool_calls is not None else None,
        _duration_text(stats.wall_clock_seconds),
    )
    detail = None
    ending = getattr(getattr(termination, "termination", None), "value", None)
    if ending and ending != "completed":
        detail = f"the run was {ending}"
    else:
        for record in getattr(snapshot, "phase_records", ()) or ():
            if str(getattr(record, "termination", "")) not in {"completed", ""}:
                reason = str(getattr(record, "reason", "") or "").strip()
                detail = f"blocked at {record.phase}"
                if reason:
                    detail = f"{detail}: {reason}"
                break
    return ResultRow(
        key="setup",
        label=ROW_LABELS["setup"],
        status=verdict,
        tone=_VERDICT_TONE.get(verdict, "attention"),
        headline=headline or "no run counts were recorded",
        detail=detail,
    )


def task_row(snapshot: Any) -> ResultRow:
    """Whether the commands the run was required to execute actually ran."""

    completion = getattr(snapshot, "task_completion", None)
    if completion is None:
        return ResultRow(
            key="task",
            label=ROW_LABELS["task"],
            status=NOT_SUPPLIED,
            tone="neutral",
            headline="no required task was supplied",
            reason="run with --acceptance-task-file to require specific commands",
        )

    status = str(completion.status)
    steps = tuple(completion.steps or ())
    complete = sum(1 for step in steps if step.status == "complete")
    items: list[str] = []
    refs: list[str] = []
    for step in steps:
        exit_text = f" → exit {step.exit_code}" if step.exit_code is not None else ""
        line = f"{step.id}: {step.status} — {step.command}{exit_text}"
        if step.reason:
            line = f"{line}; {step.reason}"
        items.append(line)
        if step.receipt_id:
            refs.append(step.receipt_id)

    if steps:
        headline = f"{status} {complete}/{len(steps)} steps"
        first = steps[0]
        exit_text = f" → exit {first.exit_code}" if first.exit_code is not None else ""
        detail = f"{first.command}{exit_text}" if first.receipt_id else None
    else:
        headline = "step definition unavailable"
        detail = None

    return ResultRow(
        key="task",
        label=ROW_LABELS["task"],
        status=status,
        tone=_TASK_TONE.get(status, "attention"),
        headline=headline,
        detail=detail,
        reason=_first_reason(getattr(completion, "reasons", ())),
        items=tuple(items),
        refs=tuple(refs),
    )


__all__ = ["NOT_SUPPLIED", "setup_row", "task_row"]
