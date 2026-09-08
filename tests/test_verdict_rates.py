"""The band table, historical grains, and same-grain snapshot metrics.

Bands: fully==100, most>=75, half>=50, few>0, none==0, unavailable when the
denominator is absent. The historical heavy-red and unbounded validators remain
strict, while new canonical test cases account runtime outcomes against runtime
executions and expose project-owned red separately.
"""

from copy import deepcopy

import pytest

from sag.verdict_rates import (
    GrainRate,
    band_for,
    demote_heavy_red,
    derived_verdict_word,
    render_rate_lines,
    render_snapshot_metric_lines,
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
    "build_judgment, test_band_args, word",
    [
        ("failed", (100, 100), "failed"),
        ("success", (100, 100), "success"),
        ("success", (99, 100), "partial"),
        ("success", (0, None), "partial"),
        ("partial", (100, 100), "partial"),
        (None, (100, 100), "partial"),
        ("unknown", (100, 100), "partial"),
    ],
)
def test_derived_verdict_word(build_judgment, test_band_args, word):
    test = GrainRate(numerator=test_band_args[0], denominator=test_band_args[1])
    assert derived_verdict_word(build_judgment, test) == word


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

    assert derived_verdict_word("success", unbounded) == "partial"


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


def _sealed_commons_cli_snapshot() -> dict:
    return {
        "schema_version": 4,
        "verdict": "success",
        "build_evidence": {
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "source_files": 36,
            "compiled_classes": 56,
            "refs": ["receipt://maven-build"],
        },
        "test_stats": {
            "discovered": 468,
            "judgment": "success",
            "unique": {
                "executed": 987,
                "passed": 926,
                "failed": 0,
                "errors": 0,
                "skipped": 61,
            },
        },
        "rates": {
            "build": {
                "modules": GrainRate(1, 1).payload(),
                "classes": GrainRate(
                    0,
                    None,
                    reason="class files are diagnostic and not comparable to Java source files",
                ).payload(),
            },
            "test": {
                "cases": GrainRate(987, 987).payload(),
                "modules": GrainRate(1, 1).payload(),
            },
            "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
        },
        "conflicts": [],
        "phase_records": [
            {
                "phase": "build",
                "termination": "completed",
                "validated_outcome": "success",
            }
        ],
    }


def test_snapshot_metric_lines_state_execution_and_scan_diagnostics():
    assert render_snapshot_metric_lines(_sealed_commons_cli_snapshot()) == [
        "Build: SUCCESS · modules built 1 of 1 declared on disk (diagnostic) · class files 56 (diagnostic) · production Java sources 36 (diagnostic)",
        "Tests: EXECUTED · outcomes accounted 987/987 · non-skipped passed 926/926 · skipped 61 · failed 0 · errors 0 · static declarations 468 (diagnostic)",
        "Coverage: unavailable — coverage pass not run",
    ]


def test_red_tests_remain_executed_and_report_red_counts():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["test_stats"]["unique"].update(passed=925, failed=1)
    _, test_line, _ = render_snapshot_metric_lines(snapshot)
    assert test_line.startswith("Tests: EXECUTED · outcomes accounted 987/987")
    assert "non-skipped passed 925/926" in test_line
    assert "failed 1 · errors 0" in test_line
    assert "FAILED" not in test_line


def test_partial_scan_is_a_disclosure_not_a_build_grade():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["rates"]["build"]["modules"] = GrainRate(21, 26).payload()
    snapshot["conflicts"] = ["build_modules_incomplete"]
    build_line, _, _ = render_snapshot_metric_lines(snapshot)
    assert build_line.startswith(
        "Build: SUCCESS · modules built 21 of 26 declared on disk (diagnostic)"
    )
    assert "%" not in build_line


def test_terminal_reactor_count_is_not_mislabeled_as_disk_scan():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["build_evidence"].update(reactor_modules_succeeded=41, reactor_modules_total=41)
    snapshot["rates"]["build"]["modules"] = GrainRate(41, 41).payload()
    build_line, _, _ = render_snapshot_metric_lines(snapshot)
    assert "modules built 41 of 41 in the build run (diagnostic)" in build_line
    assert "declared on disk" not in build_line


@pytest.mark.parametrize(
    "judgment, label",
    [
        ("success", "EXECUTED"),
        ("partial", "INTERRUPTED"),
        ("failed", "FAILED TO RUN"),
        ("unknown", "UNAVAILABLE"),
    ],
)
def test_tests_line_names_execution_state(judgment, label):
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["test_stats"]["judgment"] = judgment
    assert render_snapshot_metric_lines(snapshot)[1].startswith(f"Tests: {label} ·")


def test_missing_diagnostics_are_unavailable_not_zero():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["build_evidence"].pop("compiled_classes")
    snapshot["build_evidence"].pop("source_files")
    snapshot["rates"]["build"]["modules"] = GrainRate(0, None).payload()
    build_line, _, _ = render_snapshot_metric_lines(snapshot)
    assert (
        build_line
        == "Build: SUCCESS · modules built unavailable · class files unavailable · production Java sources unavailable"
    )
