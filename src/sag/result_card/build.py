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
    ResultDimensions,
    ResultRow,
    ResultStats,
    RunResultCard,
)
from sag.result_card.rows import (
    _lower_bounded,
    build_row,
    ci_command_row,
    coverage_row,
    official_ci_row,
    report_row,
    run_row,
    summary_row,
    tests_row,
    unconfirmed_row,
)
from sag.trajectory.phases import band_finished

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


def _totalled(
    rows: Iterable[Mapping[str, Any]] | None,
) -> tuple[int | None, int | None]:
    """Add up one spender's rows. Absent until a row actually supplies a number:
    a run whose usage was never recorded reports no tokens, not zero tokens."""
    prompt_total = output_total = None
    for row in rows or ():
        prompt = row.get("prompt_tokens")
        output = row.get("output_tokens")
        if isinstance(prompt, int):
            prompt_total = prompt if prompt_total is None else prompt_total + prompt
        if isinstance(output, int):
            output_total = output if output_total is None else output_total + output
    return prompt_total, output_total


def _stats(
    snapshot: Any,
    *,
    run_pin: Any,
    token_usage: Iterable[Mapping[str, Any]] | None,
    advisor_token_usage: Iterable[Mapping[str, Any]] | None,
    trajectory_session: Any,
    trajectory_phases: Iterable[Mapping[str, Any]] | None,
    turn_count: int | None,
    tool_calls: int | None,
    tool_failures: int | None,
) -> ResultStats:
    phases_completed, phases_total = _phases(snapshot, trajectory_phases)

    # Absent until a row actually supplies a number: a run whose usage was never
    # recorded reports no tokens, not zero tokens.
    tokens_in, tokens_out = _totalled(token_usage)
    # Summed apart and kept apart. The executor and the advisor are two models,
    # and one number for both would say the run spent it all in one place.
    advisor_tokens_in, advisor_tokens_out = _totalled(advisor_token_usage)

    pin = _mapping(run_pin)
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
        advisor_tokens_in=advisor_tokens_in,
        advisor_tokens_out=advisor_tokens_out,
        wall_clock_seconds=wall_clock if isinstance(wall_clock, (int, float)) else None,
        model=pin.get("action_model") or None,
        advisor_model=_advisor_model(pin),
    )


#: How the pin says the advisor was configured when it names no model of its
#: own. Measured over the 193 archived run pins: every one of them records
#: this mode, none records an explicit model, and no advisor call ever ran
#: against a model other than the one the run was already running.
_SAME_MODEL = "same-model"


def _advisor_model(pin: Mapping[str, Any]) -> str | None:
    """Which model answered when the run asked for advice, or nothing.

    A pin that names the advisor's model states it; a pin that says the
    advisor ran the same model as the run states it just as plainly, one step
    further along. Reading only the first spelling printed an em dash beside a
    bill of 81,516 tokens for a model the record names.
    """

    advisor = _mapping(pin.get("advisor"))
    stated = advisor.get("model")
    if stated:
        return str(stated)
    if str(advisor.get("mode") or "") == _SAME_MODEL:
        return pin.get("action_model") or None
    return None


def _phases(
    snapshot: Any, trajectory_phases: Iterable[Mapping[str, Any]] | None
) -> tuple[int | None, int | None]:
    """How many phases the run ran, and how many of them finished.

    From the run's own bands when the ledger is readable, and from the seal's
    phase records otherwise. Both count one entry per phase attempt, but the
    seal is finalized when the evidence closes, which is before the report
    phase exists — reading it alone told every surface that the archived
    commons-cli run ran four phases. It ran five, and the fifth is the one
    that wrote the report the reader is holding.

    Whether a band finished is `trajectory.phases.band_finished`, which is
    also what decides whether the turn stream ticks that band or prints
    `✗ <phase> blocked`. A phase graded failed still transitions out, so
    counting the transition alone counted a blocked phase as a finished one.
    """

    bands = tuple(trajectory_phases or ())
    if bands:
        return sum(1 for band in bands if band_finished(band)), len(bands)

    records = tuple(getattr(snapshot, "phase_records", ()) or ())
    if not records:
        return None, None
    return (
        sum(1 for record in records if str(getattr(record, "termination", "")) == "completed"),
        len(records),
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

    # The Run row already states the first blocked phase's reason, in full.
    # Restating it here printed the same paragraph twice on one screen — 450
    # characters of it on a real run — so the item names the phase and leaves
    # the words to the row. A reason no row carries is still stated.
    said_by_setup = rows["run"].detail or ""
    for record in getattr(snapshot, "phase_records", ()) or ():
        termination = str(getattr(record, "termination", ""))
        if termination in {"completed", ""}:
            continue
        reason = str(getattr(record, "reason", "") or "").strip()
        items.append(
            AttentionItem(
                kind="blocked_phase",
                title=f"The {record.phase} phase did not finish",
                detail=None if reason and reason in said_by_setup else (reason or None),
                refs=tuple(getattr(record, "evidence_refs", ()) or ())[:5],
            )
        )

    # The run's own red count, from the numbers the card's test counts print. The
    # module walk below names WHICH module, and is the better item when it can
    # be built — but `module_metrics.json` occurs in none of the 792 archived
    # campaign directories, so for every real run so far it built nothing, and
    # a run with failing tests was never named here at all. The dashboard rail
    # meanwhile flags exactly these runs off the same two numbers, so the two
    # halves of one product disagreed about what needs attention. One
    # derivation; both surfaces read it.
    named_modules = False
    for module in _mapping(module_metrics).get("modules") or ():
        if not isinstance(module, Mapping):
            continue
        # An uncounted module is not a module with nothing failing, so an
        # absent count is passed over rather than read as zero.
        failing = module.get("failing_count")
        if not isinstance(failing, int) or failing <= 0:
            continue
        named_modules = True
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

    if not named_modules:
        counts = getattr(getattr(snapshot, "test_stats", None), "unique", None)
        failed = getattr(counts, "failed", 0) or 0
        errors = getattr(counts, "errors", 0) or 0
        if failed > 0 or errors > 0:
            said = []
            if failed:
                said.append(f"{failed:,} failed")
            if errors:
                said.append(
                    f"{errors:,} ended in an error"
                    if errors == 1
                    else f"{errors:,} ended in errors"
                )
            items.append(AttentionItem(kind="failing_tests", title=f"Tests: {' · '.join(said)}"))

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


#: Said where a record does not carry a line the current finalizer seals.
_UNRECORDED_OFFICIAL_CI = "this run record does not carry them"


def _dimensions(snapshot: Any) -> tuple[Any, Any, Any, Any]:
    """The four measured lines: as sealed, or read off an older record's fields.

    A record sealed before verdict schema v6 has the facts these lines are made
    of — its task steps, its conflicts, its phase records — but not the lines
    themselves. The same derivations the finalizer runs read them here, with
    what the older record never kept (receipts, the CI record, the job ledger)
    stated as unavailable. The word on the Summary row is still the one the
    record sealed.
    """

    from sag.agent.verdict_dimensions import (
        OfficialCI,
        ci_command,
        run_control,
        sag_checks,
    )

    completion = getattr(snapshot, "task_completion", None)
    command = snapshot.ci_command
    if command is None and completion is not None:
        command = ci_command(completion, task=None, receipts={})
    official = snapshot.official_ci or OfficialCI(
        basis="unavailable", reason=_UNRECORDED_OFFICIAL_CI
    )
    checks = snapshot.sag_checks
    if not checks:
        tests = [
            record
            for record in getattr(snapshot, "phase_records", ()) or ()
            if str(getattr(record, "phase", "")) == "test"
        ]
        checks = sag_checks(
            conflicts=snapshot.conflicts,
            test_judgment=snapshot.test_stats.judgment,
            test_reason=(str(tests[-1].reason or "") or None) if tests else None,
            java_unresolved=(),
        )
    control = snapshot.run_control or run_control(
        conflicts=snapshot.conflicts,
        close_reason=None,
        phase_records=getattr(snapshot, "phase_records", ()) or (),
        live_job_reasons={},
        obligations={},
    )
    return command, official, checks, control


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
    advisor_token_usage: Iterable[Mapping[str, Any]] | None = None,
    trajectory_session: Any = None,
    trajectory_phases: Iterable[Mapping[str, Any]] | None = None,
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
        advisor_token_usage=advisor_token_usage,
        trajectory_session=trajectory_session,
        trajectory_phases=trajectory_phases,
        turn_count=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
    )
    command, official, checks, control = _dimensions(sealed)
    build = sealed.build_evidence
    tests = sealed.test_stats
    jar_counts = [
        item["jar_count"]
        for item in _mapping(module_metrics).get("modules") or ()
        if isinstance(item, Mapping)
        and type(item.get("jar_count")) is int
        and item["jar_count"] >= 0
    ]
    comparison = getattr(sealed, "ci_comparison", None)
    rows = {
        "ci_command": ci_command_row(sealed, command),
        "official_ci": official_ci_row(
            sealed, official, module_metrics=module_metrics, report_metrics=report_metrics
        ),
        "unconfirmed": unconfirmed_row(checks),
        "run": run_row(sealed, control, stats=stats, termination=termination),
        "summary": summary_row(sealed),
        "coverage": coverage_row(sealed),
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
        dimensions=ResultDimensions.model_validate(
            {
                "ci_command": command.model_dump() if command is not None else None,
                "official_ci": official.model_dump(),
                "sag_checks": [check.model_dump() for check in checks],
                "run_control": control.model_dump(),
                "build": {
                    "fact": build_row(sealed, module_metrics=module_metrics),
                    "modules_built": build.reactor_modules_succeeded,
                    "modules_total": build.reactor_modules_total,
                    "class_files": build.compiled_classes,
                    "jars": sum(jar_counts) if jar_counts else None,
                },
                "tests": {
                    "fact": tests_row(sealed, report_metrics=report_metrics),
                    # Default zero counts are not evidence of an empty test run.
                    "counts": tests.unique.model_dump() if tests.unique.executed > 0 else None,
                    "lower_bound": _lower_bounded(report_metrics),
                    "raw_executions": tests.raw.executed or None,
                    "flaky_count": tests.flaky_count,
                },
                "has_ci_reference": bool(
                    official.basis != "unavailable"
                    or official.cell
                    or getattr(comparison, "target_record_sha256", None)
                ),
            }
        ),
        attention=_attention(sealed, module_metrics=module_metrics, rows=rows),
        notes=_notes(sealed),
    )


__all__ = ["build_result_card"]
