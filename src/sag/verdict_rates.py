"""Pure band table, rate grains and execution prose for the rate-banded verdict.

One place decides what a percentage is called. Grains are never folded: no
minimum, weighting, or hidden aggregate combines them. The only band mutation
is the heavy-red weak signal, which can demote the test-cases grain from
``fully`` to ``most`` without changing its underlying fraction.

One place also decides how an execution READS (:func:`execution_sentence`).
Gate prose that renders itself from a pass rate is how ignite sealed "Tests
below the 80% pass threshold: 29/37" beside `validator_state: green`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

BAND_FULLY = "fully"
BAND_MOST = "most"
BAND_HALF = "half"
BAND_FEW = "few"
BAND_NONE = "none"
BAND_UNAVAILABLE = "unavailable"
# Both counts are real; their ratio is not. A JVM source emits zero, one, or
# many class files, and one statically discovered test emits many
# parameterized executions — so such a count can never bound its numerator.
# Distinct from ``unavailable`` (nothing observed) because the observations
# exist and stay visible; naming the top band here would let broken
# arithmetic manufacture the most flattering verdict (P4).
BAND_UNBOUNDED = "unbounded"

MOST_FLOOR = 75.0
HALF_FLOOR = 50.0
HEAVY_RED_DEMOTED_BAND = BAND_MOST
HEAVY_RED_CONFLICT = "test_failures_heavy"
UNBOUNDED_CONFLICT = "rate_denominator_not_a_bound"
UNBOUNDED_REASON = "numerator exceeds denominator; this count cannot bound it"
UNATTRIBUTED_CONFLICT = "test_executions_unattributed_to_receipts"
STALE_CONFLICT = "test_reports_stale"
UNREADABLE_REPORT_CONFLICT = "test_report_parse_error"
# The two doors a report leaves the headline by with its volume MEASURED, named
# as ONE class because the verdict must grade them alike. AUXILIARY is claimed
# by nobody; STALE was claimed and the bytes were then rewritten. Both volumes
# are measured, named, pathed and counted — and neither is ever counted INTO the
# headline, which is why neither has a second claim on the verdict to make.
# Grading them differently made the door a receipt's presence chooses, so
# deleting a receipt moved a report stale->auxiliary and lifted the word
# (spec 2026-08-14 amendment item 7).
EXCLUDED_VOLUME_CONFLICTS = frozenset({UNATTRIBUTED_CONFLICT, STALE_CONFLICT})
# The third door: a report whose volume could not be measured AT ALL, at any
# destination. It is not excluded volume — there is no volume — so it keeps its
# own name; it is ungraded for a stronger reason than the other two. An
# unreadable report can always be DELETED, and a claimed report that is deleted
# attributes nothing and says nothing, so any treatment that caps on its
# presence pays a run for `rm`. There is no monotone reading in which an
# unreadable report grades anything (spec 2026-08-14 amendment item 12).
#
# This is the set the verdict kernel must leave alone: every report fact a run
# can add or remove without ever changing what it EXECUTED.
UNCOUNTED_REPORT_CONFLICTS = frozenset({*EXCLUDED_VOLUME_CONFLICTS, UNREADABLE_REPORT_CONFLICT})


# The affirming sentence's opening token. Everything that grades execution
# renders through :func:`execution_sentence`, so a decision can check whether a
# reason IS that sentence without parsing prose.
EXECUTION_SENTENCE_PREFIX = "executed "
# The deficiency classes spec §2.3 names — below / insufficient / missing —
# written as the PREDICATE each takes rather than as bare words, and matched on
# a lowered reason.
#
# The class is "this sentence grades the evidence short", not "this sentence
# contains a negative word". The distinction is load-bearing in both
# directions and both directions are drawn from the corpus:
#
#   * bigtop's green build sealed "The repair produced the missing local
#     artifact" — a repaired absence named by the branch that repaired it. A
#     bare `missing` refuses that honest sentence.
#   * a green build reason carries an INVENTORY of what is left ("no output
#     yet: [...]", "remaining: gradle 'build' in ..."). Naming remaining work
#     is why the checklist exists; it is not a grade of what was observed.
#
# This is a bounded refusal list over the phrasings this harness produces, not
# a semantic classifier: "only 29 of 37 passed" asserts the class and is not
# matched here. It can only fire on a programming error, because the guarantee
# that a green reason SAYS something green is render-from-branch (one decision
# produces the state, the code and the sentence together) — see
# ``phase_gates._GradedDecision``. This predicate is the construction-time
# refusal behind it, never the fence itself.
DEFICIENCY_MARKERS = (
    "below the",
    "below threshold",
    "below minimum",
    "insufficient",
    "fell short",
    "too few",
    "is missing",
    "are missing",
    "was missing",
    "were missing",
    "remains missing",
    "remain missing",
    "still missing",
)


def execution_sentence(
    *,
    executed: int,
    discovered: int | None,
    passed: int,
    failed: int,
    errors: int = 0,
    skipped: int = 0,
) -> str:
    """What the dispatch ran, against what was discovered, and the red as fact.

    ``executed 3,571 of 20,497 discovered · 3,568 passed, 2 failed, 1 skipped``.
    No percentage appears: a rate is a band's job (:func:`band_for`), and a rate
    in a gate reason is what invited the 80% cliff back in every time. Errors are
    their own clause when present rather than being folded into ``failed``.
    """

    if discovered and discovered > 0:
        head = f"executed {executed:,} of {discovered:,} discovered"
    else:
        head = f"executed {executed:,} of an undetermined discovery"
    counts = [f"{passed:,} passed", f"{failed:,} failed"]
    if errors:
        counts.append(f"{errors:,} errored")
    counts.append(f"{skipped:,} skipped")
    return f"{head} · {', '.join(counts)}"


def no_execution_sentence(discovered: int | None) -> str:
    """Zero executions stated as zero executions, never as a failed percentage.

    geode sealed ``Tests below the 80% pass threshold: 0/0 (0.0%)`` — a run that
    executed nothing described as a run that ran and scored badly.
    """

    if discovered and discovered > 0:
        return f"no tests executed of {discovered:,} discovered"
    return "no tests executed"


def band_for(numerator: int, denominator: int | None) -> str:
    """Name one exact fraction using the shared verdict band table."""

    if not denominator or denominator <= 0:
        return BAND_UNAVAILABLE
    if numerator <= 0:
        return BAND_NONE
    if numerator > denominator:
        return BAND_UNBOUNDED
    if numerator >= denominator:
        return BAND_FULLY
    percent = numerator / denominator * 100.0
    if percent >= MOST_FLOOR:
        return BAND_MOST
    if percent >= HALF_FLOOR:
        return BAND_HALF
    return BAND_FEW


@dataclass(frozen=True)
class GrainRate:
    """One independently reported numerator/denominator grain."""

    numerator: int
    denominator: int | None
    reason: str | None = None
    band_override: str | None = None

    @property
    def band(self) -> str:
        return self.band_override or band_for(self.numerator, self.denominator)

    @property
    def rate(self) -> float | None:
        if not self.denominator or self.denominator <= 0:
            return None
        if self.numerator > self.denominator:
            # 295.6% is not a completion percentage; publishing it invites the
            # reader to treat it as one.
            return None
        return round(self.numerator / self.denominator * 100.0, 1)

    def payload(self) -> dict:
        """Serialize the grain without turning absence into a zero rate."""

        if self.band == BAND_UNAVAILABLE:
            return {
                "band": BAND_UNAVAILABLE,
                "reason": self.reason or "denominator unavailable",
            }
        if self.band == BAND_UNBOUNDED:
            return {
                "band": BAND_UNBOUNDED,
                "reason": self.reason or UNBOUNDED_REASON,
                "numerator": self.numerator,
                "denominator": self.denominator,
            }
        payload = {
            "rate": self.rate,
            "band": self.band,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }
        if self.reason:
            # A measured fraction can still need a sentence: a zero numerator
            # over a real denominator says nothing about the executions that
            # were excluded from it. Absent reason stays an absent key, so
            # recorded snapshots keep verifying byte-identically.
            payload["reason"] = self.reason
        return payload


def demote_heavy_red(
    cases: GrainRate, failed: int, errors: int, executed: int
) -> tuple[GrainRate, tuple[str, ...]]:
    """Record heavy project-owned red and demote only ``fully`` to ``most``."""

    if executed <= 0 or (failed + errors) * 2 <= executed:
        return cases, ()
    if cases.band == BAND_FULLY:
        return replace(cases, band_override=HEAVY_RED_DEMOTED_BAND), (HEAVY_RED_CONFLICT,)
    return cases, (HEAVY_RED_CONFLICT,)


def unbounded_conflicts(*grains: GrainRate) -> tuple[str, ...]:
    """Name the broken cardinality once, so no unbounded grain ships silently."""

    if any(grain.band == BAND_UNBOUNDED for grain in grains):
        return (UNBOUNDED_CONFLICT,)
    return ()


def derived_verdict_word(build_judgment: str | None, test_cases: GrainRate) -> str:
    """Judge build execution and complete test outcomes, never disk scan scope.

    The physical build judgment retains the Python ladder's completeness.
    Missing or incomparable test evidence cannot establish execution success.
    """
    if build_judgment == "failed":
        return "failed"
    if build_judgment == "success" and test_cases.band == BAND_FULLY:
        return "success"
    return "partial"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _strict_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _word(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value or "").strip().lower()


def render_snapshot_metric_lines(snapshot_payload: Mapping[str, object]) -> list[str]:
    """Render three same-grain, user-facing metric lines from one snapshot."""

    build = _mapping(snapshot_payload.get("build_evidence"))
    rates = _mapping(snapshot_payload.get("rates"))
    build_rates = _mapping(rates.get("build"))
    modules = _mapping(build_rates.get("modules"))
    module_numerator = _strict_count(modules.get("numerator"))
    module_denominator = _strict_count(modules.get("denominator"))
    reactor_total = _strict_count(build.get("reactor_modules_total"))
    reactor_built = _strict_count(build.get("reactor_modules_succeeded"))
    module_basis = (
        "in the build run"
        if reactor_total is not None
        and reactor_total > 0
        and reactor_total == module_denominator
        and reactor_built == module_numerator
        else "declared on disk"
    )
    modules_text = (
        f"modules built {module_numerator:,} of {module_denominator:,} {module_basis} (diagnostic)"
        if module_numerator is not None
        and module_denominator is not None
        and module_denominator > 0
        else "modules built unavailable"
    )
    class_files = _strict_count(build.get("compiled_classes"))
    source_files = _strict_count(build.get("source_files"))
    class_text = (
        f"class files {class_files:,} (diagnostic)"
        if class_files is not None
        else "class files unavailable"
    )
    source_text = (
        f"production Java sources {source_files:,} (diagnostic)"
        if source_files is not None
        else "production Java sources unavailable"
    )
    build_state = _word(build.get("judgment")) or "unknown"
    build_line = f"Build: {build_state.upper()} · {modules_text} · {class_text} · {source_text}"

    tests = _mapping(snapshot_payload.get("test_stats"))
    unique = _mapping(tests.get("unique"))
    executed = _strict_count(unique.get("executed"))
    passed = _strict_count(unique.get("passed"))
    failed = _strict_count(unique.get("failed"))
    errors = _strict_count(unique.get("errors"))
    skipped = _strict_count(unique.get("skipped"))
    counts = (executed, passed, failed, errors, skipped)
    reconciled = all(value is not None for value in counts) and executed == sum(
        value for value in (passed, failed, errors, skipped) if value is not None
    )
    test_state = {
        "success": "EXECUTED",
        "partial": "INTERRUPTED",
        "failed": "FAILED TO RUN",
    }.get(_word(tests.get("judgment")), "UNAVAILABLE")
    outcomes_text = (
        f"{executed:,}/{executed:,}"
        if reconciled and executed is not None and executed > 0
        else "unavailable"
    )
    non_skipped = (
        passed + failed + errors
        if passed is not None and failed is not None and errors is not None
        else None
    )
    passed_text = (
        f"{passed:,}/{non_skipped:,}"
        if reconciled and passed is not None and non_skipped is not None and non_skipped > 0
        else "unavailable"
    )
    skipped_text = f"{skipped:,}" if skipped is not None else "unavailable"
    failed_text = f"{failed:,}" if failed is not None else "unavailable"
    errors_text = f"{errors:,}" if errors is not None else "unavailable"
    declarations = _strict_count(tests.get("discovered"))
    declarations_text = (
        f"{declarations:,} (diagnostic)" if declarations is not None else "unavailable"
    )
    test_line = (
        f"Tests: {test_state} · outcomes accounted {outcomes_text} · "
        f"non-skipped passed {passed_text} · skipped {skipped_text} · "
        f"failed {failed_text} · errors {errors_text} · "
        f"static declarations {declarations_text}"
    )

    coverage = _mapping(rates.get("coverage"))
    line_rate = coverage.get("line_rate")
    source = coverage.get("source")
    if (
        _word(coverage.get("status")) == "collected"
        and type(line_rate) in (int, float)
        and not isinstance(line_rate, bool)
        and 0.0 <= float(line_rate) <= 100.0
        and isinstance(source, str)
        and source.strip()
    ):
        coverage_line = f"Coverage: {float(line_rate):g}% line ({source.strip()})"
    else:
        reason = coverage.get("reason")
        reason_text = (
            reason.strip() if isinstance(reason, str) and reason.strip() else "not collected"
        )
        coverage_line = f"Coverage: unavailable — {reason_text}"
    return [build_line, test_line, coverage_line]


def render_rate_lines(rates: dict) -> list[str]:
    """Render the one three-line rate headline from its serialized block."""

    def grain(value: object) -> str:
        item = value if isinstance(value, dict) else {}
        if item.get("band") == BAND_UNAVAILABLE:
            return f"unavailable — {item.get('reason') or 'rate unavailable'}"
        numerator = item.get("numerator")
        denominator = item.get("denominator")
        band = item.get("band")
        if band == BAND_UNBOUNDED:
            # Both counts stay on the line: they are the observations, and the
            # reason says why their ratio is not one.
            return (
                f"{numerator}/{denominator} "
                f"({BAND_UNBOUNDED} — {item.get('reason') or UNBOUNDED_REASON})"
            )
        if numerator is None or denominator is None or not band:
            return "unavailable — rate unavailable"
        return f"{numerator}/{denominator} ({band})"

    build = rates.get("build") if isinstance(rates, dict) else {}
    tests = rates.get("test") if isinstance(rates, dict) else {}
    coverage = rates.get("coverage") if isinstance(rates, dict) else {}
    build = build if isinstance(build, dict) else {}
    tests = tests if isinstance(tests, dict) else {}
    coverage = coverage if isinstance(coverage, dict) else {}
    if coverage.get("status") == "collected":
        coverage_line = f"{coverage.get('line_rate')}% line ({coverage.get('source')})"
    else:
        coverage_line = f"unavailable — {coverage.get('reason') or 'not collected'}"
    return [
        "Build:    "
        f"modules {grain(build.get('modules'))} · classes {grain(build.get('classes'))}",
        "Tests:    " f"cases {grain(tests.get('cases'))} · modules {grain(tests.get('modules'))}",
        f"Coverage: {coverage_line}",
    ]
