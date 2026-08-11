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
