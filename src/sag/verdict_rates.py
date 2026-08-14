"""Pure band table, rate grains and execution prose for the rate-banded verdict.

One place decides what a percentage is called. Grains are never folded: no
minimum, weighting, or hidden aggregate combines them. The only band mutation
is the heavy-red weak signal, which can demote the test-cases grain from
``fully`` to ``most`` without changing its underlying fraction.

One place also decides how an execution READS (:func:`execution_sentence`).
Gate prose that renders itself from a pass rate is how ignite sealed "Tests
below the 80% pass threshold: 29/37" beside `validator_state: green`.
"""

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
# The two doors a report leaves the headline by, named as ONE class because the
# verdict must grade them alike. AUXILIARY is claimed by nobody; STALE was
# claimed and the bytes were then rewritten. Both volumes are measured, named,
# pathed and counted — and neither is ever counted INTO the headline, which is
# why neither has a second claim on the verdict to make. Grading them
# differently made the door a receipt's presence chooses, so deleting a receipt
# moved a report stale->auxiliary and lifted the word (spec 2026-08-14
# amendment item 7). A third door added later belongs in this set.
EXCLUDED_VOLUME_CONFLICTS = frozenset({UNATTRIBUTED_CONFLICT, STALE_CONFLICT})


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


def derived_verdict_word(build_modules: GrainRate, test_cases: GrainRate) -> str:
    """Derive the legacy word mechanically from two named bands."""

    if build_modules.band == BAND_NONE:
        return "failed"
    if build_modules.band == BAND_FULLY and test_cases.band == BAND_FULLY:
        return "success"
    return "partial"


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
