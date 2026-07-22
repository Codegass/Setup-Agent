# tests/test_python_module_metrics.py
"""Suppress the Java module-metrics pipeline on Python projects.

Live-run 2026-06-24 (pyyaml probe) false-red, root cause (2): the Java module
scan (scan_modules: .class globs, surefire report dirs) ran on a Python
project. _compute_module_metrics physically detected build_system 'python' but
the maven/gradle fallback ("build_system = reported if reported in (maven,
gradle) else maven") let python fall through to maven, so the scan produced
"🧩 Modules: 0 built / 1 detected · 0 tested / 1 not tested" and a bogus
module-derived build_modules_incomplete while 1287/1287 pytest tests had run.

Fix under test: when the build system resolves to python (marker files
pyproject.toml/setup.py/requirements.txt — physical_validator's
_detect_build_system already returns 'python' for them), _compute_module_metrics
returns None BEFORE the maven fallback. None suppresses the module breakdown
section, the "🧩 Modules:" line, and every module-derived conflict
(build_modules_incomplete / reactor_scope_narrowed) — the v1 python scope is
single-package; packages-as-modules is future work. Maven/gradle projects keep
the module pipeline byte-for-byte (their tests stay green untouched).
"""

from sag.reporting.utils import render_condensed_summary
from sag.tools.report_tool import ReportTool


class _RecordingValidator:
    """Fake physical validator: configurable detection + call recording.

    scan_modules returns exactly the nonsense record the live pyyaml run
    produced (one '.' pseudo-module, zero classes/jars from Maven globs) so a
    regression would reproduce the "0 built / 1 detected" symptom, not pass
    silently on an empty scan.
    """

    def __init__(self, detected="python"):
        self.detected = detected
        self.calls = {"detect": 0, "scan": 0, "parse": 0}

    def _detect_build_system(self, project_dir):
        self.calls["detect"] += 1
        return self.detected

    def scan_modules(self, project_dir, build_system):
        self.calls["scan"] += 1
        self.scanned_with = build_system
        return [{"path": ".", "name": ".", "class_count": 0, "jar_count": 0,
                 "report_dirs": []}]

    def parse_module_test_reports(self, module_dir, report_dirs):
        self.calls["parse"] += 1
        return {}


def _python_project_info():
    return {
        "directory": "/workspace/pyyaml",
        "type": "Python Project",
        "build_system": "pip/poetry",
    }


def _tool_with(validator, project_info=None):
    tool = ReportTool()
    tool.physical_validator = validator
    tool._get_project_info = lambda: project_info or _python_project_info()
    return tool


# ---------------------------------------------------------------------------
# core: _compute_module_metrics must bail out on python BEFORE the maven fallback
# ---------------------------------------------------------------------------


def test_compute_module_metrics_returns_none_on_python_markers():
    """LIVE-RUN REPRODUCTION (pyyaml): physical detection says python; the Java
    module scan must not run at all and the metrics must be None."""
    validator = _RecordingValidator(detected="python")
    tool = _tool_with(validator)

    assert tool._compute_module_metrics({}, generated_at="t") is None
    assert validator.calls["scan"] == 0, (
        "scan_modules (.class globs / surefire dirs) must never run on a "
        "python project"
    )


def test_compute_module_metrics_none_when_only_reported_system_is_python():
    """Detection can come back 'unknown' (e.g. markers below the scan depth);
    a python REPORTED build system must then suppress the metrics too instead
    of falling through the 'else maven' fallback — that fallback is the exact
    live-run bug."""
    validator = _RecordingValidator(detected="unknown")
    tool = _tool_with(validator)  # project_info reports pip/poetry

    assert tool._compute_module_metrics({}, generated_at="t") is None
    assert validator.calls["scan"] == 0


def test_compute_module_metrics_physical_maven_beats_reported_python():
    """Physical evidence outranks the reported label: a pom.xml project whose
    project_info drifted to 'pip/poetry' still gets the Maven module scan."""
    validator = _RecordingValidator(detected="maven")
    tool = _tool_with(validator)

    metrics = tool._compute_module_metrics({}, generated_at="t")
    assert metrics is not None
    assert validator.scanned_with == "maven"
    assert metrics["module_summary"]["build_systems"] == ["maven"]


# ---------------------------------------------------------------------------
# snapshot + report line: no module counts, no module-derived conflicts
# ---------------------------------------------------------------------------


def _pytest_all_green_accomplishments(total=1287):
    return {
        "repository_cloned": True,
        "build_success": True,
        "test_success": True,
        "physical_validation": {
            "class_files": 0,
            "jar_files": 0,
            "test_analysis": {
                "valid": True,
                "test_success": True,
                "total_tests": total,
                "passed_tests": total,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "pass_rate": 100.0,
            },
        },
    }


def test_python_snapshot_carries_no_module_counts_or_module_conflicts():
    """The report snapshot for a python run must not gain the module-derived
    cap conflicts, and the memoized module metrics feeding the '🧩 Modules:'
    line / dashboard / breakdown must be None."""
    validator = _RecordingValidator(detected="python")
    tool = _tool_with(validator)

    snapshot = tool._build_legacy_report_snapshot(
        "partial",
        "setup-report-pyyaml.md",
        _python_project_info(),
        _pytest_all_green_accomplishments(),
        {},
        {"conflicts": []},
    )

    conflicts = snapshot["evidence_result"].get("conflicts", [])
    assert "build_modules_incomplete" not in conflicts
    assert "reactor_scope_narrowed" not in conflicts
    assert snapshot["status"].get("modules_tested") is None
    assert snapshot["status"].get("modules_test_bearing") is None

    # The ONLY producer of status.modules_detected/modules_built (the
    # "🧩 Modules:" line and the Module Coverage dashboard rows) is the
    # memoized module metrics — None means those lines cannot render.
    assert tool._build_module_metrics({}, generated_at="t") is None
    assert "🧩 Modules:" not in render_condensed_summary(snapshot)


def test_python_snapshot_keeps_honest_build_ladder_conflicts():
    """Scope check: suppressing MODULE-derived conflicts must not scrub the
    honest build_modules_incomplete the python evidence ladder itself emitted
    (validate_build_status partial: C-extensions missing). Evidence conflicts
    passed in stay untouched."""
    validator = _RecordingValidator(detected="python")
    tool = _tool_with(validator)

    snapshot = tool._build_legacy_report_snapshot(
        "partial",
        "setup-report-pyyaml.md",
        _python_project_info(),
        _pytest_all_green_accomplishments(),
        {},
        {"conflicts": ["build_modules_incomplete"]},
    )

    assert snapshot["evidence_result"]["conflicts"] == ["build_modules_incomplete"]


def test_submodule_breakdown_empty_for_python_project():
    """The markdown 'Submodule Breakdown' section renders from the same
    memoized metrics: None -> no section on python."""
    validator = _RecordingValidator(detected="python")
    tool = _tool_with(validator)

    metrics = tool._build_module_metrics({}, generated_at="t")
    assert metrics is None
    assert tool._render_submodule_breakdown(metrics or {}) == []
