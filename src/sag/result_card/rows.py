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


_TEST_JUDGMENT_WORD = {
    "success": "executed",
    "partial": "interrupted",
    "failed": "failed to run",
    "unknown": "unavailable",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def build_row(snapshot: Any, *, module_metrics: Any = None) -> ResultRow:
    """What compiled, with the module count marked as the diagnostic it is."""

    evidence = snapshot.build_evidence
    judgment = str(evidence.judgment)
    succeeded = evidence.reactor_modules_succeeded
    total = evidence.reactor_modules_total
    if total:
        headline = f"{succeeded or 0}/{total} modules built"
    else:
        headline = judgment

    rollup = _mapping(_mapping(module_metrics).get("module_summary"))
    jars = None
    for module in _mapping(module_metrics).get("modules") or ():
        count = module.get("jar_count") if isinstance(module, dict) else None
        if isinstance(count, int):
            jars = (jars or 0) + count
    pieces = []
    if evidence.compiled_classes is not None:
        pieces.append(f"{evidence.compiled_classes:,} class files")
    if jars is not None:
        pieces.append(f"{jars:,} jars")
    if rollup.get("modules_failed"):
        pieces.append(f"{rollup['modules_failed']:,} modules failed")
    detail = _join(*pieces)
    if detail:
        detail = f"{detail} · counts are diagnostic, CI defines scope"

    reason = None
    if judgment == "unknown":
        reason = "no build result was recorded for this run"

    return ResultRow(
        key="build",
        label=ROW_LABELS["build"],
        status=judgment,
        tone=_VERDICT_TONE.get(judgment, "attention"),
        headline=headline,
        detail=detail or None,
        reason=reason,
        refs=tuple(evidence.refs or ())[:12],
    )


def _lower_bounded(report_metrics: Any) -> bool:
    """True when the run's own accounting says its execution total is a floor."""

    layers = _mapping(_mapping(report_metrics).get("tests"))
    executions = _mapping(_mapping(layers.get("claimed")).get("receipt_executions"))
    return executions.get("availability") == "partial" and executions.get("bound") == "lower"


def tests_row(snapshot: Any, *, report_metrics: Any = None) -> ResultRow:
    """How many tests ran and how they came out, with skips out of the rate."""

    stats = snapshot.test_stats
    unique = stats.unique
    raw = stats.raw
    judgment = str(stats.judgment)
    status = _TEST_JUDGMENT_WORD.get(judgment, "unavailable")

    if unique.executed <= 0:
        return ResultRow(
            key="tests",
            label=ROW_LABELS["tests"],
            status="unavailable",
            tone="attention",
            headline="no test results were recorded",
            reason="the run recorded no test outcomes",
        )

    bound = "≥" if _lower_bounded(report_metrics) else ""
    headline = (
        f"{bound}{unique.executed:,} executed · {unique.passed:,} passed · "
        f"{unique.failed:,} failed · {unique.errors:,} errors · {unique.skipped:,} skipped"
    )

    non_skipped = unique.passed + unique.failed + unique.errors
    red = unique.failed + unique.errors
    rate_text = None
    if non_skipped > 0:
        percent = unique.passed / non_skipped * 100.0
        if red and percent >= 99.95:
            rate_text = "<100% of non-skipped passed"
        elif percent >= 99.95:
            rate_text = "100% of non-skipped passed"
        else:
            rate_text = f"{percent:.1f}% of non-skipped passed"
    raw_text = None
    if raw.executed and raw.executed != unique.executed:
        raw_text = f"{raw.executed:,} raw executions"

    if status == "failed to run":
        tone: Tone = "failed"
    elif status == "interrupted" or red > 0 or status == "unavailable":
        tone = "attention"
    else:
        tone = "success"

    return ResultRow(
        key="tests",
        label=ROW_LABELS["tests"],
        status=status,
        tone=tone,
        headline=headline,
        detail=_join(rate_text, raw_text) or None,
    )


# pytest collects any imported module-level name matching `test*`, and every
# test module that renders this row imports it by name (cf. TestStats in
# sag/evidence.py).
tests_row.__test__ = False


def coverage_row(snapshot: Any) -> ResultRow:
    """Line coverage when it was collected, and why not when it was not."""

    coverage = _mapping(_mapping(snapshot.rates).get("coverage"))
    if coverage.get("status") == "collected" and coverage.get("line_rate") is not None:
        source = str(coverage.get("source") or "").strip()
        return ResultRow(
            key="coverage",
            label=ROW_LABELS["coverage"],
            status="collected",
            tone="neutral",
            headline=f"{float(coverage['line_rate']):g}% line coverage",
            detail=f"source {source}" if source else None,
        )
    reason = str(coverage.get("reason") or "").strip() or "coverage was not collected"
    return ResultRow(
        key="coverage",
        label=ROW_LABELS["coverage"],
        status="not collected",
        tone="neutral",
        headline="not collected",
        reason=reason,
    )



_CI_TONE: dict[str, Tone] = {
    "met": "success",
    "exceeded": "success",
    "partial": "attention",
    "not_met": "failed",
    "invalid": "failed",
}

_MAX_RED_IDS = 10

NOT_COMPARED = "not compared"

_REPORT_TONE: dict[str, Tone] = {
    "delivered": "neutral",
    "skipped": "neutral",
    "failed": "attention",
    "unavailable": "attention",
}


def ci_row(snapshot: Any) -> ResultRow:
    """How this run measures against the project's own CI on the same commit."""

    comparison = getattr(snapshot, "ci_comparison", None)
    if comparison is None:
        return ResultRow(
            key="ci",
            label=ROW_LABELS["ci"],
            status=NOT_COMPARED,
            tone="neutral",
            headline=NOT_COMPARED,
            reason=gloss("official_ci_target_not_supplied"),
        )

    result = getattr(comparison, "attainment", None)
    if str(comparison.status) != "evaluated" or result is None:
        return ResultRow(
            key="ci",
            label=ROW_LABELS["ci"],
            status=NOT_COMPARED,
            tone="neutral",
            headline=NOT_COMPARED,
            reason=_first_reason(getattr(comparison, "reasons", ()))
            or "the comparison produced no result",
        )

    verdict = str(result.verdict)
    alpha = getattr(result, "alpha", None)
    if alpha is not None:
        headline = f"{verdict} {alpha.numerator:,}/{alpha.denominator:,}"
    else:
        headline = f"{verdict} · scope score unavailable"

    parity = getattr(result, "lifecycle_parity", None)
    detail_parts = []
    if result.cell_id:
        detail_parts.append(f'cell "{result.cell_id}"')
    if parity is not None:
        parity_text = f"lifecycle {parity.status}"
        if parity.missing:
            parity_text = f"{parity_text} · missing {', '.join(parity.missing)}"
        if parity.extra:
            parity_text = f"{parity_text} · extra {', '.join(parity.extra)}"
        detail_parts.append(parity_text)

    items = [f"{code}: {gloss(code)}" for code in getattr(result, "reason_codes", ()) or ()]
    red_ids = tuple(getattr(result, "unexpected_red_ids", ()) or ())
    if red_ids:
        items.append(f"red beyond CI: {len(red_ids):,} tests")
        shown = ", ".join(red_ids[:_MAX_RED_IDS])
        if len(red_ids) > _MAX_RED_IDS:
            shown = f"{shown}, +{len(red_ids) - _MAX_RED_IDS} more"
        items.append(shown)

    return ResultRow(
        key="ci",
        label=ROW_LABELS["ci"],
        status=verdict,
        tone=_CI_TONE.get(verdict, "attention"),
        headline=headline,
        detail=_join(*detail_parts) or None,
        items=tuple(items),
        refs=tuple(getattr(comparison, "receipt_ids", ()) or ()),
    )


def report_row(termination: Any | None, *, report_path: str | None = None) -> ResultRow:
    """Whether the written setup report exists, and where."""

    status = getattr(getattr(termination, "report_delivery_status", None), "value", None)
    if status is None:
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status="unavailable",
            tone="attention",
            headline="no report was recorded",
            reason="the run did not record whether a report was written",
        )
    if status == "delivered":
        headline = report_path or "written inside the container"
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone="neutral",
            headline=headline,
            refs=(report_path,) if report_path else (),
        )
    if status == "failed":
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone="attention",
            headline="the setup report was not written",
            reason="the run result itself is unchanged",
        )
    return ResultRow(
        key="report",
        label=ROW_LABELS["report"],
        status=status,
        tone="neutral",
        headline="no report was requested",
    )



__all__ = [
    "NOT_COMPARED",
    "NOT_SUPPLIED",
    "build_row",
    "ci_row",
    "coverage_row",
    "report_row",
    "setup_row",
    "task_row",
    "tests_row",
]
