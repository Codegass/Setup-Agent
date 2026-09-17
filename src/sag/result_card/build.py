"""Assemble one card from the run's record and whatever artifacts came with it.

Every input but the verdict payload is optional. A missing input degrades the
fields that depend on it to a stated absence; it never invents a value and
never changes a row's status word.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from sag.result_card.glosses import cited, gloss
from sag.result_card.models import (
    ROW_ORDER,
    AttentionItem,
    ResultRow,
    ResultStats,
    RunResultCard,
)
from sag.result_card.rows import (
    build_row,
    ci_row,
    coverage_row,
    report_row,
    setup_row,
    task_row,
    tests_row,
)

_MAX_FAILING_NAMES = 5

#: The comparison findings that name work, as opposed to the ones that describe
#: why no comparison happened. Spelled here so this package keeps reading the
#: record through plain attributes and imports nothing from the metrics layer.
_CI_FINDING_CODES = frozenset(
    {
        "NEW_RED_BEYOND_TARGET",
        "MODULES_BELOW_TARGET",
        "EXECUTION_BELOW_TARGET",
        "BUILD_AXIS_NOT_SUCCESSFUL",
    }
)


def _verdict_source(schema_version: int) -> str:
    """Where the card's words came from: a reading, or a reconstruction.

    Any seal older than the one this system writes is a reconstruction, because
    the reader that authorizes seals (`read_live_verdict_snapshot`) answers
    `unknown` for it and cannot confirm a thing about it. Pinned to the
    finalizer's own constant rather than a number copied here, so when the
    schema moves every surface's provenance moves with it in one place — a
    hardcoded 3 is what let 184 archived v4 records call themselves readings.
    """

    from sag.agent.verdict_finalizer import VERDICT_SCHEMA_VERSION

    return "legacy" if schema_version < VERDICT_SCHEMA_VERSION else "snapshot"


def _as_snapshot(snapshot: Any) -> Any:
    from sag.agent.verdict_finalizer import RunVerdictSnapshot

    if isinstance(snapshot, RunVerdictSnapshot):
        return snapshot
    return RunVerdictSnapshot.model_validate(snapshot)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _stats(
    snapshot: Any,
    *,
    run_pin: Any,
    token_usage: Iterable[Mapping[str, Any]] | None,
    trajectory_session: Any,
    turn_count: int | None,
    tool_calls: int | None,
    tool_failures: int | None,
) -> ResultStats:
    records = tuple(getattr(snapshot, "phase_records", ()) or ())
    phases_total = len(records) or None
    phases_completed = (
        sum(1 for record in records if str(getattr(record, "termination", "")) == "completed")
        if records
        else None
    )

    # Absent until a row actually supplies a number: a run whose usage was never
    # recorded reports no tokens, not zero tokens.
    tokens_in = tokens_out = None
    for row in token_usage or ():
        prompt = row.get("prompt_tokens")
        output = row.get("output_tokens")
        if isinstance(prompt, int):
            tokens_in = prompt if tokens_in is None else tokens_in + prompt
        if isinstance(output, int):
            tokens_out = output if tokens_out is None else tokens_out + output

    pin = _mapping(run_pin)
    advisor = _mapping(pin.get("advisor"))
    session = _mapping(trajectory_session)
    wall_clock = session.get("wall_clock_seconds")

    return ResultStats(
        phases_completed=phases_completed,
        phases_total=phases_total,
        turns=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        wall_clock_seconds=wall_clock if isinstance(wall_clock, (int, float)) else None,
        model=pin.get("action_model") or None,
        advisor_model=advisor.get("model") or None,
    )


def _attention(
    snapshot: Any, *, module_metrics: Any, rows: dict[str, ResultRow]
) -> tuple[AttentionItem, ...]:
    items: list[AttentionItem] = []

    completion = getattr(snapshot, "task_completion", None)
    for step in getattr(completion, "steps", ()) or ():
        if step.status == "complete":
            continue
        exit_text = f" → exit {step.exit_code}" if step.exit_code is not None else ""
        items.append(
            AttentionItem(
                kind="task_step",
                title=f"Required task step {step.id} {step.status}",
                detail=f"{step.command}{exit_text}",
                refs=(step.receipt_id,) if step.receipt_id else (),
            )
        )

    for record in getattr(snapshot, "phase_records", ()) or ():
        termination = str(getattr(record, "termination", ""))
        if termination in {"completed", ""}:
            continue
        reason = str(getattr(record, "reason", "") or "").strip()
        items.append(
            AttentionItem(
                kind="blocked_phase",
                title=f"The {record.phase} phase did not finish",
                detail=reason or None,
                refs=tuple(getattr(record, "evidence_refs", ()) or ())[:5],
            )
        )

    for module in _mapping(module_metrics).get("modules") or ():
        if not isinstance(module, Mapping):
            continue
        # An uncounted module is not a module with nothing failing, so an
        # absent count is passed over rather than read as zero.
        failing = module.get("failing_count")
        if not isinstance(failing, int) or failing <= 0:
            continue
        names = [str(name) for name in module.get("failing_names") or ()]
        shown = ", ".join(names[:_MAX_FAILING_NAMES])
        if len(names) > _MAX_FAILING_NAMES:
            shown = f"{shown}, +{len(names) - _MAX_FAILING_NAMES} more"
        items.append(
            AttentionItem(
                kind="failing_tests",
                title=f"{module.get('name') or module.get('path') or 'module'} · {failing:,} failing",
                detail=shown or None,
                refs=tuple(str(ref) for ref in module.get("evidence_refs") or ())[:5],
            )
        )

    # The same codes the CI row lists, read from the record rather than
    # recovered from the row's rendered lines. `_consistent_subject` on
    # CIComparisonSnapshot already refuses a result on anything but an evaluated
    # comparison; the row states both halves of that gate, so this states both.
    comparison = getattr(snapshot, "ci_comparison", None)
    result = getattr(comparison, "attainment", None)
    if str(getattr(comparison, "status", "")) == "evaluated" and result is not None:
        for code in getattr(result, "reason_codes", ()) or ():
            if str(code) in _CI_FINDING_CODES:
                items.append(AttentionItem(kind="ci_finding", title=cited(str(code))))

    for module in _mapping(module_metrics).get("modules") or ():
        if not isinstance(module, Mapping):
            continue
        for sample in module.get("build_error_samples") or ():
            items.append(
                AttentionItem(
                    kind="build_warning",
                    title=f"{module.get('name') or 'module'}: {sample}",
                )
            )

    if rows["report"].status == "failed":
        items.append(
            AttentionItem(
                kind="report",
                title="The setup report was not written",
                detail=rows["report"].reason,
            )
        )
    return tuple(items)


def _notes(snapshot: Any) -> tuple[str, ...]:
    seen: list[str] = []
    for conflict in getattr(snapshot, "conflicts", ()) or ():
        text = gloss(str(conflict))
        if text not in seen:
            seen.append(text)
    return tuple(seen)


def build_result_card(
    snapshot: Any,
    *,
    module_metrics: Any = None,
    report_metrics: Any = None,
    run_pin: Any = None,
    token_usage: Iterable[Mapping[str, Any]] | None = None,
    trajectory_session: Any = None,
    turn_count: int | None = None,
    tool_calls: int | None = None,
    tool_failures: int | None = None,
    termination: Any = None,
    project: str | None = None,
    container: str | None = None,
    session_dir: str | None = None,
    report_path: str | None = None,
    goal: str | None = None,
    commit: str | None = None,
) -> RunResultCard:
    """Derive the card. Reads; never writes, never re-judges."""

    sealed = _as_snapshot(snapshot)
    stats = _stats(
        sealed,
        run_pin=run_pin,
        token_usage=token_usage,
        trajectory_session=trajectory_session,
        turn_count=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
    )
    rows = {
        "setup": setup_row(sealed, stats=stats, termination=termination),
        "task": task_row(sealed),
        "build": build_row(sealed, module_metrics=module_metrics),
        "tests": tests_row(sealed, report_metrics=report_metrics),
        "coverage": coverage_row(sealed),
        "ci": ci_row(sealed),
        "report": report_row(termination, report_path=report_path),
    }

    source = _verdict_source(sealed.schema_version)
    resolved_commit = commit
    if resolved_commit is None:
        comparison = getattr(sealed, "ci_comparison", None)
        resolved_commit = getattr(comparison, "target_sha", None)

    return RunResultCard(
        run_id=sealed.run_id,
        project=project,
        goal=goal,
        commit=resolved_commit,
        container=container,
        session_dir=session_dir,
        verdict=sealed.verdict,
        verdict_source=source,
        rows=tuple(rows[key] for key in ROW_ORDER),
        stats=stats,
        attention=_attention(sealed, module_metrics=module_metrics, rows=rows),
        notes=_notes(sealed),
    )


__all__ = ["build_result_card"]
