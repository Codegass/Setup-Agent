"""The band table and rate grains — the spec's §3 contract, edge-exact.

Bands: fully==100, most>=75, half>=50, few>0, none==0, unavailable when the
denominator is absent. Grains are never folded; red tests demote exactly
fully->most on the cases grain and nothing else (spec §4).
"""

import pytest

from sag.verdict_rates import (
    GrainRate,
    band_for,
    demote_heavy_red,
    derived_verdict_word,
    render_rate_lines,
)


@pytest.mark.parametrize(
    "numerator, denominator, band",
    [
        (14, 14, "fully"),
        (99, 100, "most"),
        (75, 100, "most"),
        (74, 100, "half"),
        (50, 100, "half"),
        (49, 100, "few"),
        (1, 100, "few"),
        (0, 100, "none"),
        (0, 0, "unavailable"),
        (0, None, "unavailable"),
    ],
)
def test_band_edges(numerator, denominator, band):
    assert band_for(numerator, denominator) == band


def test_grain_rate_payload_and_percent():
    grain = GrainRate(numerator=6, denominator=18414)
    assert grain.band == "few"
    assert grain.rate == 0.0  # 6/18414 rounds to 0.0 but band stays few, not none
    assert grain.payload() == {
        "rate": 0.0,
        "band": "few",
        "numerator": 6,
        "denominator": 18414,
    }


def test_unavailable_grain_serializes_reason_never_zero():
    grain = GrainRate(numerator=0, denominator=None, reason="static discovery found no count")
    assert grain.band == "unavailable"
    assert grain.rate is None
    assert grain.payload() == {
        "band": "unavailable",
        "reason": "static discovery found no count",
    }


def test_heavy_red_demotes_exactly_fully_to_most():
    fully = GrainRate(numerator=100, denominator=100)
    demoted, conflicts = demote_heavy_red(fully, failed=60, errors=0, executed=100)
    assert demoted.band == "most" and conflicts == ("test_failures_heavy",)
    # at-or-below most: signal recorded, band untouched
    half = GrainRate(numerator=60, denominator=100)
    kept, conflicts = demote_heavy_red(half, failed=40, errors=21, executed=60)
    assert kept is half and conflicts == ("test_failures_heavy",)
    # no heavy red: nothing happens
    same, conflicts = demote_heavy_red(fully, failed=10, errors=0, executed=100)
    assert same is fully and conflicts == ()


def test_demoted_band_overrides_the_fraction():
    demoted, _ = demote_heavy_red(
        GrainRate(numerator=100, denominator=100), failed=80, errors=0, executed=100
    )
    assert demoted.payload()["band"] == "most"
    assert demoted.payload()["rate"] == 100.0  # the fraction stays honest


@pytest.mark.parametrize(
    "build_band_args, test_band_args, word",
    [
        ((0, 14), (100, 100), "failed"),  # none build -> failed
        ((14, 14), (100, 100), "success"),  # fully + fully -> success
        ((14, 14), (99, 100), "partial"),
        ((13, 14), (100, 100), "partial"),
        ((14, 14), (0, None), "partial"),  # unavailable is never success
    ],
)
def test_derived_verdict_word(build_band_args, test_band_args, word):
    build = GrainRate(numerator=build_band_args[0], denominator=build_band_args[1])
    test = GrainRate(numerator=test_band_args[0], denominator=test_band_args[1])
    assert derived_verdict_word(build, test) == word


def test_render_rate_lines_keeps_all_three_grains_visible():
    rates = {
        "build": {
            "modules": GrainRate(14, 14).payload(),
            "classes": GrainRate(3400, 3412).payload(),
        },
        "test": {
            "cases": GrainRate(100, 100).payload(),
            "modules": GrainRate(1, 2).payload(),
        },
        "coverage": {
            "status": "collected",
            "line_rate": 55.5,
            "source": "jacoco-injected",
        },
    }

    assert render_rate_lines(rates) == [
        "Build:    modules 14/14 (fully) · classes 3400/3412 (most)",
        "Tests:    cases 100/100 (fully) · modules 1/2 (half)",
        "Coverage: 55.5% line (jacoco-injected)",
    ]


# ---------------------------------------------------------------------------
# A numerator larger than its denominator: the denominator is not a bound
# ---------------------------------------------------------------------------


def test_a_numerator_past_its_denominator_is_never_the_top_band():
    """Live 2026-08-10 acceptance sealed `classes 201/68 (fully)` at 295.6%
    and `cases 1605/1163 (fully)` at 138%, with zero conflicts, and the word
    `success` was manufactured from those two `fully` bands.

    One Java source emits zero, one, or many class files; one statically
    discovered test emits many parameterized executions. A count that cannot
    bound its numerator is not a denominator, and P4 forbids naming the most
    flattering band when the arithmetic is impossible."""
    from sag.verdict_rates import BAND_UNBOUNDED, band_for

    assert band_for(201, 68) == BAND_UNBOUNDED
    assert band_for(1605, 1163) == BAND_UNBOUNDED
    # The exact-equality edge remains honestly full: 14 of 14 IS everything.
    assert band_for(14, 14) == "fully"


def test_an_unbounded_grain_keeps_both_counts_and_states_why():
    """Both numbers are real observations; only their ratio is meaningless.
    Unlike `unavailable` (nothing to report), the counts stay visible."""
    from sag.verdict_rates import BAND_UNBOUNDED, GrainRate

    grain = GrainRate(numerator=201, denominator=68)

    assert grain.band == BAND_UNBOUNDED
    assert grain.rate is None, "an impossible ratio is not a percentage"
    payload = grain.payload()
    assert payload["numerator"] == 201 and payload["denominator"] == 68
    assert payload["band"] == BAND_UNBOUNDED
    assert "reason" in payload


def test_an_unbounded_grain_cannot_manufacture_success():
    from sag.verdict_rates import GrainRate, derived_verdict_word

    fully = GrainRate(numerator=1, denominator=1)
    unbounded = GrainRate(numerator=1605, denominator=1163)

    assert derived_verdict_word(fully, unbounded) == "partial"
    assert derived_verdict_word(unbounded, fully) == "partial"


def test_heavy_red_never_promotes_an_unbounded_grain_into_a_band():
    """The weak signal demotes; it must not hand `most` to a grain that has
    no defensible band at all."""
    from sag.verdict_rates import BAND_UNBOUNDED, GrainRate, demote_heavy_red

    grain, conflicts = demote_heavy_red(
        GrainRate(numerator=1605, denominator=1163), failed=900, errors=0, executed=1605
    )

    assert grain.band == BAND_UNBOUNDED
    assert conflicts == ("test_failures_heavy",)


def test_the_banner_shows_the_counts_and_the_reason_for_an_unbounded_grain():
    from sag.verdict_rates import GrainRate, render_rate_lines

    rates = {
        "build": {
            "modules": GrainRate(1, 1).payload(),
            "classes": GrainRate(201, 68).payload(),
        },
        "test": {"cases": GrainRate(1605, 1163).payload(), "modules": GrainRate(1, 1).payload()},
        "coverage": {"status": "collected", "line_rate": 68.3, "source": "jacoco-existing"},
    }

    build_line, test_line, _ = render_rate_lines(rates)

    assert "201/68 (unbounded" in build_line
    assert "1605/1163 (unbounded" in test_line
    assert "(fully)" in build_line  # the modules grain is untouched


def test_any_unbounded_grain_raises_exactly_one_conflict():
    """Silence is what let `classes 201/68` seal with `conflicts: []`."""
    from sag.verdict_rates import UNBOUNDED_CONFLICT, GrainRate, unbounded_conflicts

    ok = GrainRate(1, 1)
    bad = GrainRate(201, 68)

    assert unbounded_conflicts(ok, ok) == ()
    assert unbounded_conflicts(ok, bad) == (UNBOUNDED_CONFLICT,)
    assert unbounded_conflicts(bad, bad) == (UNBOUNDED_CONFLICT,), "one conflict, not two"
    assert unbounded_conflicts(GrainRate(0, None), ok) == (), "absence is not unboundedness"
