"""The condensed log summary must surface the DETECTED (static) test total.

Regression: the static/declared test count was computed during a run but never
rendered into the console/main.log summary — it only ever printed *executed*
tests, and only when an execution count existed. For a "57 detected, 0 executed"
run the vital number vanished entirely.
"""

from sag.reporting.utils import render_condensed_summary


def _snapshot(status, physical_evidence):
    return {
        "status": status,
        "project": {"type": "Maven Java Project", "build_system": "Maven"},
        "phases": {"clone": True, "build": True, "test": bool(physical_evidence.get("tests_total"))},
        "physical_evidence": physical_evidence,
        "attention": {"items": []},
        "report_path": "/workspace/setup-report.md",
    }


def test_detected_but_not_executed_is_surfaced():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "partial", "static_test_count": 57, "execution_rate": 0.0},
            {"class_files": 10, "jar_files": 1, "tests_total": 0, "tests_pass_pct": None},
        )
    )
    assert "57 detected" in out
    assert "0 executed" in out


def test_detected_and_executed_both_surfaced():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "success", "static_test_count": 100, "execution_rate": 57.0},
            {"class_files": 50, "jar_files": 2, "tests_total": 57, "tests_pass_pct": 95.0},
        )
    )
    assert "100 detected" in out
    assert "57 executed" in out
    assert "pass rate" in out


def test_executed_only_still_renders_without_static_count():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "success"},
            {"class_files": 5, "jar_files": 1, "tests_total": 5, "tests_pass_pct": 100.0},
        )
    )
    assert "5 executed" in out
    assert "detected" not in out


def test_modules_built_detected_line_surfaced():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "partial", "modules_detected": 5, "modules_built": 3,
             "modules_failed_count": 2},
            {"class_files": 100, "jar_files": 4, "tests_total": None, "tests_pass_pct": None},
        )
    )
    assert "🧩 Modules: 3 built / 5 detected" in out
    assert "2 failed" in out


def test_modules_tested_not_tested_surfaced():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "partial", "modules_detected": 5, "modules_built": 3,
             "modules_tested": 2, "modules_not_tested": 3},
            {"class_files": 100, "jar_files": 4, "tests_total": None, "tests_pass_pct": None},
        )
    )
    assert "🧩 Modules: 3 built / 5 detected · 2 tested / 3 not tested" in out


def test_modules_not_tested_defaults_to_detected_minus_tested():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "partial", "modules_detected": 5, "modules_built": 3,
             "modules_tested": 2},  # modules_not_tested omitted
            {"class_files": 100, "jar_files": 4, "tests_total": None, "tests_pass_pct": None},
        )
    )
    assert "2 tested / 3 not tested" in out


def test_single_module_built_detected_line():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "success", "modules_detected": 1, "modules_built": 1},
            {"class_files": 33, "jar_files": 0, "tests_total": None, "tests_pass_pct": None},
        )
    )
    assert "🧩 Modules: 1 built / 1 detected" in out


def test_no_modules_detected_omits_module_line():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "success"},
            {"class_files": 5, "jar_files": 1, "tests_total": 5, "tests_pass_pct": 100.0},
        )
    )
    assert "🧩 Modules:" not in out


def test_no_tests_no_static_count_omits_tests_line():
    out = render_condensed_summary(
        _snapshot(
            {"verdict": "success"},
            {"class_files": 5, "jar_files": 1, "tests_total": None, "tests_pass_pct": None},
        )
    )
    assert "🧪 Tests:" not in out
