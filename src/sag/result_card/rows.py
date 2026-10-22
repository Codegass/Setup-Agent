"""One builder per row. Each copies what the run recorded and states absence.

No builder computes a judgment: a status word always comes from the seal, and
a count always comes from the seal or a sealed sibling artifact.
"""

from __future__ import annotations

from typing import Any, Mapping

from sag.result_card.glosses import cited, explain, gloss
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


def duration_text(seconds: float | None) -> str | None:
    """`6m 30s`, `41.0s`, `1h 04m`. Sub-minute keeps a decimal; hours drop seconds.

    Public because the written report opens its account of the run with the
    same duration the Run row states, and one run cannot last two lengths.
    """

    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    if total < 60:
        return f"{seconds:.1f}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _first_reason(reasons: Any) -> str | None:
    """The first reason a row cites, said once.

    ``explain`` keeps the code beside its sentence — the code is the handle a
    reader quotes — and drops the parenthesis when there is no sentence, so an
    unglossed code reads as one code rather than as the same word twice.
    """

    for reason in reasons or ():
        text = str(reason).strip()
        if text:
            return explain(text)
    return None


def setup_row(snapshot: Any, *, stats: ResultStats, termination: Any | None) -> ResultRow:
    """How the run went, as counts, plus anything unusual about how it ended."""

    verdict = str(snapshot.verdict)
    phases = None
    # Both halves or no fraction. A missing numerator printed as 0 would read
    # as "nothing finished" when the truth is "nobody counted".
    if stats.phases_total and stats.phases_completed is not None:
        phases = f"{stats.phases_completed}/{stats.phases_total} phases"
    headline = _join(
        phases,
        f"{stats.turns:,} turns" if stats.turns is not None else None,
        f"{stats.tool_calls:,} tool calls" if stats.tool_calls is not None else None,
        duration_text(stats.wall_clock_seconds),
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
        # A surface that cannot reach the counts says so about itself. The run
        # did record them — the ledger and the run pin hold them — so claiming
        # they were never recorded would state a falsehood about the run.
        headline=headline or "run counts unavailable",
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
    """Any mapping read as a plain dict; anything else read as nothing.

    ``Mapping``, not ``dict``: the sibling artifacts arrive from whichever
    caller read them, and a read-only view of a payload is the same payload.
    A narrower test here than the one `sag.result_card.build` applies would
    drop a count out of a row while the same payload still raised an
    attention item — the card saying two different things about one file.
    """

    return dict(value) if isinstance(value, Mapping) else {}


def _build_facts(snapshot: Any, *, module_metrics: Any = None) -> list[str]:
    """Class files, jars and failed modules: what the build left, as counts."""

    evidence = snapshot.build_evidence
    rollup = _mapping(_mapping(module_metrics).get("module_summary"))
    jars = None
    for module in _mapping(module_metrics).get("modules") or ():
        count = module.get("jar_count") if isinstance(module, Mapping) else None
        if isinstance(count, int):
            jars = (jars or 0) + count
    pieces = []
    if evidence.compiled_classes is not None:
        pieces.append(f"{evidence.compiled_classes:,} class files")
    if jars is not None:
        pieces.append(f"{jars:,} jars")
    if rollup.get("modules_failed"):
        pieces.append(f"{rollup['modules_failed']:,} modules failed")
    return pieces


def build_row(snapshot: Any, *, module_metrics: Any = None) -> ResultRow:
    """What compiled, with the module count marked as the diagnostic it is."""

    evidence = snapshot.build_evidence
    judgment = str(evidence.judgment)
    succeeded = evidence.reactor_modules_succeeded
    total = evidence.reactor_modules_total
    # The two counts are parsed independently, so either can be absent. "0/4
    # modules built" for an uncounted numerator reads as a total build failure.
    if total and succeeded is not None:
        headline = f"{succeeded}/{total} modules built"
    else:
        headline = judgment

    detail = _join(*_build_facts(snapshot, module_metrics=module_metrics))
    if detail:
        detail = f"{detail} · counts are diagnostic, CI defines scope"
    if (succeeded is None) != (total is None):
        detail = _join("module count not recorded", detail)

    reason = None
    if judgment == "unknown":
        reason = "no build result was recorded for this run"
    elif headline == judgment and not detail:
        # Nothing was counted anywhere: no reactor fraction, no class files,
        # no jars. A non-reactor project and a build read back from
        # observations both land here, and the row would otherwise be a
        # status word beside an empty column — a blank a reader has to read
        # as either zero or unknown. The row states which one it is.
        reason = "the run recorded no module or class counts"

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


def _test_tone(status: str, *, red: int = 0) -> Tone:
    """One rule for the tests row's colour, whether or not anything ran.

    Kept in one place so a row with nothing to count cannot be coloured by a
    different rule than the same judgment with counts behind it.
    """

    if status == "failed to run":
        return "failed"
    if status == "interrupted" or red > 0 or status == "unavailable":
        return "attention"
    return "success"


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
        # Nothing to count is not the same as nothing decided. A record that
        # sealed `failed` or `partial` here judged an execution that produced
        # no rows; calling that `unavailable` would be the card re-judging the
        # run, and it turns a red row yellow. The word comes from the seal;
        # only a judgment the seal never made reads `unavailable`, and that
        # row says why.
        return ResultRow(
            key="tests",
            label=ROW_LABELS["tests"],
            status=status,
            tone=_test_tone(status),
            headline="no test results were recorded",
            reason="the run recorded no test outcomes" if status == "unavailable" else None,
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

    tone = _test_tone(status, red=red)

    # The counts are real and stay in the headline; what is missing is the
    # run's word for them, and an unavailable row never leaves that unsaid.
    reason = None
    if status == "unavailable":
        reason = "the run recorded test outcomes but no judgment about them"

    return ResultRow(
        key="tests",
        label=ROW_LABELS["tests"],
        status=status,
        tone=tone,
        headline=headline,
        detail=_join(rate_text, raw_text) or None,
        reason=reason,
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

#: No CI job was reached at all: none was supplied, or none on this commit
#: matches the run's JDK and OS. Nothing was compared, and the row says so.
NOT_COMPARED = "not compared"

#: A CI job WAS reached and no score came out of the comparison.
NOT_SCORED = "not scored"

#: The record's word for a comparison, said the way a reader says it. The record
#: is what tone keys off; this is only how the word is spelled on a surface, so
#: all three spell it the same and none of them keeps its own copy.
#:
#: ``invalid`` is the record's word for a comparison that reached a CI job and
#: could not put a number on it — 135 of the 342 evaluated comparisons under
#: ``logs/``. The bare word reads on a screen as a judgment on the project
#: rather than on the comparison. It was spelled ``not compared``, which is the
#: phrase the no-CI-job cases already use: one phrase over two different facts,
#: and wrong for this one — the row beneath it names the CI job, the commit and
#: the command, under a heading that reads WHAT WAS COMPARED. The row's reason
#: still names why no score came out.
_CI_STATUS_WORD: dict[str, str] = {"not_met": "not met", "invalid": NOT_SCORED}

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
    word = _CI_STATUS_WORD.get(verdict, verdict)
    alpha = getattr(result, "alpha", None)
    if alpha is not None:
        headline = f"{word} {alpha.numerator:,}/{alpha.denominator:,}"
    elif word == NOT_SCORED:
        # A row whose word is already "not scored" does not then report a
        # missing score: every `invalid` record under `logs/` carries none, and
        # saying it twice in one row reads as two separate facts. The findings
        # below the row still say why.
        headline = NOT_SCORED
    else:
        headline = f"{word} · scope score unavailable"

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

    items = [cited(str(code)) for code in getattr(result, "reason_codes", ()) or ()]
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
        status=word,
        # Tone keys off the record's own word, never off the spelled one.
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
            tone=_REPORT_TONE["unavailable"],
            headline="no report was recorded",
            reason="the run did not record whether a report was written",
        )
    if status == "delivered":
        headline = report_path or "written inside the container"
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone=_REPORT_TONE.get(status, "attention"),
            headline=headline,
            refs=(report_path,) if report_path else (),
        )
    if status == "failed":
        return ResultRow(
            key="report",
            label=ROW_LABELS["report"],
            status=status,
            tone=_REPORT_TONE.get(status, "attention"),
            headline="the setup report was not written",
            reason="the run result itself is unchanged",
        )
    return ResultRow(
        key="report",
        label=ROW_LABELS["report"],
        status=status,
        tone=_REPORT_TONE.get(status, "attention"),
        headline="no report was requested",
    )


# --- the card's rows (spec 2026-10-22) -------------------------------------
#
# The rows above are fact rows: each still states one measurement and its
# tests pin it, but the card no longer prints them one by one. The five rows
# below put what the project's CI command did first, how this run compares with
# the official CI run second, what SAG could not confirm third, how the run
# ended fourth, and the word derived from those lines last.

NO_PINNED_COMMAND = "no pinned CI command; the build and tests below are what the run did"
DERIVED = "derived from the lines above"
ALL_CONFIRMED = "SAG confirmed each of the above from its own records."
SOME_UNCONFIRMED = "SAG could not confirm these from its own records"

_COMMAND_WORD: dict[str, str] = {
    "passed": "passed",
    "failed": "failed",
    "not_finished": "did not finish",
    "unavailable": "unavailable",
}

_COMMAND_TONE: dict[str, Tone] = {
    "passed": "success",
    "failed": "failed",
    "not_finished": "attention",
    "unavailable": "attention",
}

#: What happened to the first step that did not pass, after "step i of n".
_STEP_FATE: dict[str, str] = {
    "missing": "did not finish",
    "out_of_order": "ran out of order",
    "unavailable": "could not be confirmed",
}

#: How each of SAG's four checks reads when it is not confirmed.
_CHECK_QUESTION: dict[str, str] = {
    "java requirement decided": "whether the installed Java version meets the project's requirement",
    "built modules confirmed": "which modules the build produced",
    "test outcome graded": "whether the tests passed",
    "test reports tied to the command": "which command the test reports came from",
}

_RUN_WORD: dict[str, str] = {
    "normally": "ended normally",
    "time_limit": "time limit",
    "job_control_failure": "stopped",
    "aborted": "aborted",
}

_RUN_TONE: dict[str, Tone] = {
    "normally": "success",
    "time_limit": "attention",
    "job_control_failure": "failed",
    "aborted": "failed",
}

COMPARED = "compared"


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _toolchain_text(command: Any) -> str:
    if command.toolchain_as_pinned == "yes":
        pins = [f"Java {major}" for major in command.pinned_java]
        pins += [f"Maven {version}" for version in command.pinned_maven]
        return f"{_and(pins)} as pinned"
    if command.toolchain_as_pinned == "no":
        return f"toolchain not as pinned: {command.toolchain_reason}"
    return f"toolchain not checked: {command.toolchain_reason}"


def _sources_text(command: Any) -> str:
    if command.source_unchanged == "yes":
        if command.source_detail:
            return f"sources changed by the command's own tests: {command.source_detail}"
        return "sources unchanged"
    if command.source_unchanged == "no":
        return f"sources changed: {command.source_detail}"
    return f"sources not checked: {explain(command.source_detail or 'task_source_changed_or_unverified')}"


def _first_unpassed_text(command: Any, completion: Any) -> str | None:
    first = command.first_unpassed_step
    if first is None:
        return None
    ids = [str(step.id) for step in getattr(completion, "steps", ()) or ()]
    position = ids.index(first.id) + 1 if first.id in ids else None
    where = f"step {position} of {command.steps_total}" if position else f"step {first.id}"
    if first.status == "failed" and first.exit_code is not None:
        return f"{where} exited {first.exit_code}"
    return f"{where} {_STEP_FATE.get(first.status, 'did not pass')}"


def ci_command_row(snapshot: Any, command: Any) -> ResultRow:
    """Line 1: whether the project's pinned CI command passed in this run."""

    if command is None:
        return ResultRow(
            key="ci_command",
            label=ROW_LABELS["ci_command"],
            status=NOT_SUPPLIED,
            tone="neutral",
            headline=NO_PINNED_COMMAND,
        )
    completion = getattr(snapshot, "task_completion", None)
    steps = task_row(snapshot)
    result = str(command.result)
    if command.steps_total:
        headline = _join(
            f"{command.steps_passed} of {command.steps_total} steps passed",
            _first_unpassed_text(command, completion),
        )
    else:
        headline = "no step result was recorded"
    return ResultRow(
        key="ci_command",
        label=ROW_LABELS["ci_command"],
        status=_COMMAND_WORD.get(result, result),
        tone=_COMMAND_TONE.get(result, "attention"),
        headline=headline,
        detail=_join(_toolchain_text(command), _sources_text(command)),
        items=steps.items,
        refs=steps.refs,
    )


def _comparison(official: Any) -> tuple[list[str], bool, bool]:
    """The comparison phrases, whether any side-by-side number exists, and
    whether this run fell short of the official CI run on one of them."""

    parts: list[str] = []
    compared = below = False
    if official.ci_modules is not None:
        ci_modules = set(official.ci_modules)
        if official.agent_modules_built is not None:
            built = len(ci_modules & set(official.agent_modules_built))
            parts.append(f"built {built:,} of {len(ci_modules):,} modules")
            compared, below = True, below or built < len(ci_modules)
        else:
            parts.append(f"official CI built {len(ci_modules):,} modules")
    if official.ci_tests_executed is not None:
        if official.agent_tests_executed is not None:
            parts.append(
                f"ran {official.agent_tests_executed:,} of {official.ci_tests_executed:,} tests"
            )
            compared = True
            below = below or official.agent_tests_executed < official.ci_tests_executed
        else:
            parts.append(f"official CI ran {official.ci_tests_executed:,} tests")
    return parts, compared, below


def official_ci_row(
    snapshot: Any,
    official: Any,
    *,
    module_metrics: Any = None,
    report_metrics: Any = None,
) -> ResultRow:
    """Line 2: this run's modules and tests beside the official CI run's."""

    stats = snapshot.test_stats
    tests = tests_row(snapshot, report_metrics=report_metrics)
    build = build_row(snapshot, module_metrics=module_metrics)
    # The Build row's module fraction when it has one; its word alone is the
    # class-file judgment the CI command line now answers, so it is not repeated.
    if build.headline != build.status:
        built = build.headline
    elif build.status == "unknown":
        built = "no build result was recorded"
    else:
        built = None
    extras = [*_build_facts(snapshot, module_metrics=module_metrics)]
    # Retries show as the wider execution grain, as the Tests row always said
    # them: a flaky count is a number the card would have to name itself.
    raw = stats.raw.executed
    if raw and raw != stats.unique.executed:
        extras.append(f"{raw:,} raw executions")

    parts, compared, below = _comparison(official)
    failures = (
        official.agent_failures if official.agent_failures is not None else stats.unique.failed
    )
    errors = official.agent_errors if official.agent_errors is not None else stats.unique.errors
    if parts:
        headline = f"{', '.join(parts)} (CI cell '{official.cell}')"
    elif official.basis == "unavailable":
        headline = f"official CI numbers not available: {official.reason}"
    else:
        headline = (
            "official CI numbers not available: the CI record for cell "
            f"'{official.cell}' states no module list or test count"
        )
    if compared:
        detail = _join(f"{failures:,} failures, {errors:,} errors in this run", *extras)
    else:
        detail = f"this run: {_join(built, tests.headline, *extras)}"

    comparison = ci_row(snapshot)
    items: list[str] = []
    if comparison.status not in {NOT_COMPARED, "not supplied"}:
        items.append("CI comparison score: " + _join(comparison.headline, comparison.detail))
        items.extend(comparison.items)

    if failures or errors or below:
        tone: Tone = "attention"
    elif compared:
        tone = "success"
    else:
        tone = "neutral"
    return ResultRow(
        key="official_ci",
        label=ROW_LABELS["official_ci"],
        status=COMPARED if compared else NOT_COMPARED,
        tone=tone,
        headline=headline,
        detail=detail,
        items=tuple(items),
        refs=comparison.refs if items else (),
    )


def unconfirmed_row(checks: Any) -> ResultRow:
    """Line 3: what SAG could not confirm from its own records, one line each."""

    checks = tuple(checks or ())
    open_checks = [check for check in checks if not check.confirmed]
    if not open_checks:
        return ResultRow(
            key="unconfirmed",
            label=ROW_LABELS["unconfirmed"],
            status="none",
            tone="success",
            headline=ALL_CONFIRMED,
        )
    return ResultRow(
        key="unconfirmed",
        label=ROW_LABELS["unconfirmed"],
        status=f"{len(open_checks)} of {len(checks)}",
        tone="attention",
        headline=SOME_UNCONFIRMED,
        items=tuple(
            f"{_CHECK_QUESTION.get(str(check.check), str(check.check))}: {check.reason}"
            for check in open_checks
        ),
    )


def run_row(
    snapshot: Any, control: Any, *, stats: ResultStats, termination: Any | None
) -> ResultRow:
    """Line 4: how the run ended, with its counts."""

    ended = str(control.ended)
    if ended == "time_limit":
        running = control.commands_still_running
        noun = "command" if running == 1 else "commands"
        headline = "stopped at the time limit" + (
            f" with {running} {noun} still running" if running else ""
        )
    elif ended == "job_control_failure":
        headline = "stopped after a command could not be stopped cleanly"
    elif ended == "aborted":
        headline = f"aborted: {control.detail}" if control.detail else "aborted"
    else:
        headline = "ended normally"
    setup = setup_row(snapshot, stats=stats, termination=termination)
    return ResultRow(
        key="run",
        label=ROW_LABELS["run"],
        status=_RUN_WORD.get(ended, ended),
        tone=_RUN_TONE.get(ended, "attention"),
        headline=headline,
        detail=_join(
            control.detail if ended == "time_limit" else None, setup.headline, setup.detail
        ),
    )


def summary_row(snapshot: Any) -> ResultRow:
    """Line 5: the one word, derived from the lines above it."""

    verdict = str(snapshot.verdict)
    return ResultRow(
        key="summary",
        label=ROW_LABELS["summary"],
        status=verdict,
        tone=_VERDICT_TONE.get(verdict, "attention"),
        headline=DERIVED,
    )



__all__ = [
    "ALL_CONFIRMED",
    "DERIVED",
    "NO_PINNED_COMMAND",
    "NOT_COMPARED",
    "NOT_SCORED",
    "NOT_SUPPLIED",
    "build_row",
    "duration_text",
    "ci_command_row",
    "ci_row",
    "coverage_row",
    "official_ci_row",
    "report_row",
    "run_row",
    "setup_row",
    "summary_row",
    "task_row",
    "tests_row",
    "unconfirmed_row",
]
