# tests/test_execution_rate_clamp.py
"""Execution rate must clamp at 100% when executed exceeds the detected total.

Live evidence (paramiko run 5, 2026-06): banner read
"🧪 Tests: 559 detected, 560 executed (pass rate 96.6%, execution rate 100.2%)".
The pytest --collect-only denominator said 559; the per-test XML aggregation
counted 560 executed (a re-run / parameterized drift adds one). An execution
rate above 100% is nonsense on a report.

Contract under test:
- when executed > detected the RATE clamps to exactly 100.0 — the raw counts
  themselves are never altered and both stay visible on every surface;
- the tests_not_fully_executed gate treats executed >= detected as full
  coverage and can never fire there (even at a strict 100% threshold);
- executed < detected keeps the exact ratio and the gate semantics;
- detected=0 / missing keeps execution_rate None (behavior unchanged).
"""

import pytest

from sag.evidence import TestStats
from sag.reporting.utils import render_condensed_summary
from sag.tools.report_tool import ReportTool


def _snapshot(tool, *, detected, executed, passed, failed, verified_status="partial"):
    accomplishments = {
        "physical_validation": {
            "test_analysis": {
                "total_tests": executed,
                "passed_tests": passed,
                "failed_tests": failed,
                "error_tests": 0,
                "skipped_tests": 0,
            },
        },
    }
    if detected is not None:
        accomplishments["physical_validation"]["test_status"] = {
            "static_test_count": detected
        }
    return tool._build_report_snapshot(
        verified_status=verified_status,
        report_filename="setup-report-test.md",
        project_info={"build_system": "pip/poetry"},
        actual_accomplishments=accomplishments,
        execution_metrics={},
    )


# ---------------------------------------------------------------------------
# Repro: paramiko run 5 — 559 collected, 560 executed
# ---------------------------------------------------------------------------


def test_paramiko_executed_exceeds_detected_clamps_rate_at_100():
    tool = ReportTool()
    snapshot = _snapshot(tool, detected=559, executed=560, passed=541, failed=19)

    status = snapshot["status"]
    # Counts are NEVER altered — both raw numbers survive.
    assert status["static_test_count"] == 559
    assert status["tests_total"] == 560
    # The rate clamps at exactly 100.0 (was 100.2 live).
    assert status["execution_rate"] == 100.0
    # Full coverage: the gate stays silent.
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get(
        "conflicts", []
    )


def test_gate_never_fires_when_executed_exceeds_detected_even_at_strict_threshold():
    """A strict 100%-execution threshold is a legal config; executed >= detected
    must still read as full coverage."""

    class StrictValidator:
        test_execution_threshold = 1.0  # require 100% execution

    tool = ReportTool()
    tool.physical_validator = StrictValidator()
    snapshot = _snapshot(tool, detected=559, executed=560, passed=541, failed=19)

    assert snapshot["status"]["execution_rate"] == 100.0
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get(
        "conflicts", []
    )


def test_condensed_banner_renders_both_raw_numbers_and_clamped_rate():
    tool = ReportTool()
    snapshot = _snapshot(tool, detected=559, executed=560, passed=541, failed=19)

    out = render_condensed_summary(snapshot)
    assert "559 detected" in out
    assert "560 executed" in out
    assert "execution rate 100.0%" in out
    assert "100.2" not in out


def test_markdown_test_coverage_line_clamps_at_100():
    tool = ReportTool()
    snapshot = _snapshot(tool, detected=559, executed=560, passed=541, failed=19)

    lines = tool._render_execution_details_simplified(
        snapshot=snapshot,
        execution_metrics={
            "total_runtime": 12.0,
            "total_iterations": 3,
            "total_thoughts": 5,
            "total_actions": 7,
            "successful_actions": 7,
            "success_rate": 100.0,
        },
    )
    coverage_lines = [ln for ln in lines if "Test Coverage" in ln]
    assert coverage_lines, "Test Coverage line must render"
    # Both raw counts stay visible; the rate clamps.
    assert "560/559 tests executed" in coverage_lines[0]
    assert "(100.0% execution rate)" in coverage_lines[0]
    assert "100.2" not in coverage_lines[0]


# ---------------------------------------------------------------------------
# Regression: executed < detected keeps the exact ratio and the gate semantics
# ---------------------------------------------------------------------------


def test_partial_execution_keeps_exact_ratio_and_fires_gate():
    tool = ReportTool()
    snapshot = _snapshot(tool, detected=635, executed=8, passed=0, failed=8)

    status = snapshot["status"]
    assert status["static_test_count"] == 635
    assert status["tests_total"] == 8
    assert status["execution_rate"] == pytest.approx(8 / 635 * 100, abs=0.01)
    assert "tests_not_fully_executed" in snapshot["evidence_result"]["conflicts"]

    out = render_condensed_summary(snapshot)
    assert "635 detected" in out
    assert "8 executed" in out
    assert "execution rate 1.3%" in out


def test_exact_full_execution_is_exactly_100_and_gate_silent():
    tool = ReportTool()
    snapshot = _snapshot(
        tool, detected=50, executed=50, passed=50, failed=0, verified_status="success"
    )

    status = snapshot["status"]
    assert status["execution_rate"] == pytest.approx(100.0, abs=0.01)
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get(
        "conflicts", []
    )


# ---------------------------------------------------------------------------
# detected=0 / missing: behavior unchanged
# ---------------------------------------------------------------------------


def test_no_detected_count_keeps_execution_rate_none():
    tool = ReportTool()
    snapshot = _snapshot(tool, detected=None, executed=5, passed=5, failed=0)

    status = snapshot["status"]
    assert status["static_test_count"] in (None, 0)
    assert status["execution_rate"] is None
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get(
        "conflicts", []
    )

    out = render_condensed_summary(snapshot)
    assert "5 executed" in out
    assert "execution rate" not in out


# ---------------------------------------------------------------------------
# Shared TestStats model: same clamp, same regressions
# ---------------------------------------------------------------------------


def test_teststats_execution_rate_clamps_at_100():
    assert TestStats(discovered=559, executed=560).execution_rate == 100.0


def test_teststats_execution_rate_keeps_exact_ratio_below_100():
    assert TestStats(discovered=635, executed=8).execution_rate == round(
        8 / 635 * 100, 1
    )


def test_teststats_execution_rate_none_without_discovered():
    assert TestStats(discovered=None, executed=5).execution_rate is None
    assert TestStats(discovered=0, executed=5).execution_rate is None
