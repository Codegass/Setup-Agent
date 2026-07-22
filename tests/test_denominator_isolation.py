"""Java @Test static counts must never feed the Python execution denominator.

LIVE EVIDENCE (TVM, session 20260713_014403_27874): the report said
"17 test methods detected / 0 executed (0.0%)" for TVM — a PYTHON project with
thousands of pytest tests. The 17 were Java @Test annotations from the vendored
``jvm/`` binding, counted by the Java static catalog scanner and stored on the
trunk as ``static_test_count``. At report time that Java count became the
execution-coverage DENOMINATOR for a pytest run: 0 executed of 17 read as 0.0%
coverage and falsely fired ``tests_not_fully_executed``, capping an otherwise
honest run at PARTIAL. Cross-language contamination — the denominator judged
pytest execution against a Java count.

Fix under test: when the resolved build system for the run is Python, the Java
@Test static count must NEVER become ``static_test_count`` / the
``tests_not_fully_executed`` denominator. Python denominator priority:
pytest ``--collect-only`` (COLLECTED_JSON, carried on
physical_validation.test_status.test_stats.discovered) FIRST; when it is absent
the denominator is honestly UNKNOWN — no static fallback from the Java scanner.
The tests line then renders "N executed" WITHOUT a fabricated detected figure
rather than a wrong "0 of 17". Java projects keep the Java counter untouched.
"""

from types import SimpleNamespace

from sag.reporting.utils import render_condensed_summary
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool
from sag.tools.report_tool import ReportTool


# --------------------------------------------------------------------------- #
# Fakes: a build-system detector and a trunk carrying a static count.
# --------------------------------------------------------------------------- #
class _Validator:
    """Physical validator stub: reports one build system for _detect_build_system.

    scan_modules is inert (python suppresses module metrics; java/maven runs
    are single-module here) so the snapshot builds without a real container.
    """

    def __init__(self, detected):
        self.detected = detected
        self.test_execution_threshold = 0.8

    def _detect_build_system(self, project_dir):
        return self.detected

    def scan_modules(self, project_dir, build_system):
        return []

    def parse_module_test_reports(self, module_dir, report_dirs):
        return {}

    def detect_java_build_systems(self, project_dir):
        return [self.detected] if self.detected in ("maven", "gradle") else []


class _FakeCM:
    """Trunk context manager carrying a static_test_count on the env summary."""

    def __init__(self, static_test_count):
        self.saved = None
        self._env = {}
        if static_test_count is not None:
            self._env["static_test_count"] = static_test_count

    def load_trunk_context(self):
        # A fresh mutable env each load mirrors ContextManager semantics; the
        # report may attempt a best-effort persist of a backfilled count.
        return SimpleNamespace(environment_summary=dict(self._env))

    def _save_trunk_context(self, trunk_context):
        self.saved = dict(trunk_context.environment_summary)


def _tool(*, detected, static_test_count, project_info):
    tool = ReportTool(context_manager=_FakeCM(static_test_count))
    tool.physical_validator = _Validator(detected)
    tool._get_project_info = lambda: project_info
    return tool


def _python_info():
    return {
        "directory": "/workspace/tvm",
        "type": "Python Project",
        "build_system": "pip/poetry",
    }


def _java_info():
    return {
        "directory": "/workspace/bigtop",
        "type": "Java Project",
        "build_system": "Maven",
    }


def _green_run(total):
    """A fully-green run of ``total`` executed tests (pytest or surefire)."""
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


def _green_run_with_collected(total, collected):
    """Green run that ALSO carries a pytest --collect-only denominator."""
    run = _green_run(total)
    run["physical_validation"]["test_status"] = {
        "test_stats": {"discovered": collected},
    }
    return run


def _build(tool, project_info, accomplishments):
    return tool._build_legacy_report_snapshot(
        "partial",
        "setup-report.md",
        project_info,
        accomplishments,
        {},
        {"conflicts": []},
    )


# --------------------------------------------------------------------------- #
# 1. TVM reproduction: python + Java static 17 + no collected -> UNKNOWN denom
# --------------------------------------------------------------------------- #
def test_python_run_ignores_java_static_count_without_collected():
    """LIVE REPRO (TVM): the trunk carries the Java @Test count 17 and pytest
    ran with no collect-only manifest. The Java 17 must NOT become the
    denominator: no static_test_count, no execution_rate, no
    tests_not_fully_executed, and no '0 of 17' anywhere in the rendered
    summary."""
    tool = _tool(
        detected="python",
        static_test_count=17,  # Java @Test annotations from vendored jvm/
        project_info=_python_info(),
    )
    # A real pytest subset executed (say 42 green); no COLLECTED_JSON present.
    snapshot = _build(tool, _python_info(), _green_run(42))
    status = snapshot["status"]

    assert status.get("static_test_count") is None, (
        "the Java @Test count 17 must never become the python denominator"
    )
    assert status.get("execution_rate") is None
    conflicts = snapshot["evidence_result"].get("conflicts", [])
    assert "tests_not_fully_executed" not in conflicts

    rendered = render_condensed_summary(snapshot)
    assert "17" not in rendered, "the Java count 17 must not surface at all"
    assert "of 17" not in rendered
    assert "17 detected" not in rendered
    # The false 0.0% execution-rate the Java denominator produced is gone. (The
    # pass rate is a different metric — a green run legitimately shows 100.0%.)
    assert "execution rate" not in rendered
    # Honest UNKNOWN denominator: executed figure without a fabricated total.
    assert "42 executed" in rendered
    assert "detected" not in rendered  # no fabricated 'N detected'


# --------------------------------------------------------------------------- #
# 2. python + collected 3000 -> denominator is the collect-only count
# --------------------------------------------------------------------------- #
def test_python_run_uses_collected_denominator_when_present():
    """With a pytest --collect-only manifest (3000 collected), the denominator
    is the collected count — never the stale Java static count."""
    tool = _tool(
        detected="python",
        static_test_count=17,  # Java noise still on the trunk
        project_info=_python_info(),
    )
    snapshot = _build(
        tool, _python_info(), _green_run_with_collected(3000, 3000)
    )
    status = snapshot["status"]

    assert status.get("static_test_count") == 3000
    # 3000 of 3000 executed = full coverage, so the shortfall gate stays quiet.
    conflicts = snapshot["evidence_result"].get("conflicts", [])
    assert "tests_not_fully_executed" not in conflicts

    rendered = render_condensed_summary(snapshot)
    assert "3000 detected" in rendered
    assert "17" not in rendered


def test_python_collected_denominator_surfaces_real_shortfall():
    """A genuine diagnostic-subset run (8 executed of 3000 collected) must
    still fire tests_not_fully_executed off the COLLECTED count — the fix
    suppresses the JAVA denominator, not honest python shortfalls."""
    tool = _tool(
        detected="python",
        static_test_count=17,
        project_info=_python_info(),
    )
    snapshot = _build(
        tool, _python_info(), _green_run_with_collected(8, 3000)
    )
    status = snapshot["status"]

    assert status.get("static_test_count") == 3000
    conflicts = snapshot["evidence_result"].get("conflicts", [])
    assert "tests_not_fully_executed" in conflicts


# --------------------------------------------------------------------------- #
# 3. Java project: static 57 unchanged (bigtop stays green)
# --------------------------------------------------------------------------- #
def test_java_run_keeps_static_denominator():
    """Java projects keep the Java catalog counter untouched: static 57 stays
    the denominator and drives execution coverage exactly as before."""
    tool = _tool(
        detected="maven",
        static_test_count=57,
        project_info=_java_info(),
    )
    snapshot = _build(tool, _java_info(), _green_run(57))
    status = snapshot["status"]

    assert status.get("static_test_count") == 57
    assert status.get("execution_rate") == 100.0

    rendered = render_condensed_summary(snapshot)
    assert "57 detected" in rendered


def test_java_static_denominator_fires_shortfall():
    """A Java run that executed only a fraction of its 57 declared methods
    still fires tests_not_fully_executed off the static count — Java path
    byte-identical to before the python isolation."""
    tool = _tool(
        detected="maven",
        static_test_count=57,
        project_info=_java_info(),
    )
    snapshot = _build(tool, _java_info(), _green_run(3))
    status = snapshot["status"]

    assert status.get("static_test_count") == 57
    conflicts = snapshot["evidence_result"].get("conflicts", [])
    assert "tests_not_fully_executed" in conflicts


# --------------------------------------------------------------------------- #
# Analyzer side: the Java @Test catalog must not run / store on a python repo.
#
# The report-side fix neutralizes any Java static count that reaches the trunk,
# but the FIRST resolution site is the analyzer: the Java catalog scan
# (build_java_test_catalog) and the static_test_count storage are gated on
# project_type == "Java". These tests lock that gate so a future
# hybrid-detection change can't silently start persisting the vendored jvm/
# @Test count on a python repo.
# --------------------------------------------------------------------------- #
def test_analyzer_does_not_store_static_count_for_python_analysis():
    """A python analysis dict never carries a static_test_count (the analyzer
    default is None; only the Java catalog branch sets it). Recording env
    metrics from it must leave static_test_count absent on the trunk."""
    tool = ProjectAnalyzerTool()
    trunk = SimpleNamespace(environment_summary={})
    python_analysis = {
        "build_system": "pip/poetry",
        "project_type": "Python",
        "static_test_count": None,  # analyzer default — Java branch never ran
    }

    tool._record_environment_metrics(trunk, python_analysis)

    assert "static_test_count" not in trunk.environment_summary, (
        "the Java @Test static count must never be persisted on a python repo"
    )


def test_analyzer_still_stores_static_count_for_java_analysis():
    """Java analysis keeps storing its catalog count on the trunk untouched."""
    tool = ProjectAnalyzerTool()
    trunk = SimpleNamespace(environment_summary={})
    java_analysis = {
        "build_system": "Maven",
        "project_type": "Java",
        "static_test_count": 57,
        "method_count": 57,
        "test_count_method": "catalog_based_discovery",
    }

    tool._record_environment_metrics(trunk, java_analysis)

    assert trunk.environment_summary["static_test_count"] == 57
