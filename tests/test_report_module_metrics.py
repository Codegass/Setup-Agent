# tests/test_report_module_metrics.py
import json

from sag.tools.report_tool import ReportTool


def test_load_test_history_aggregates_reactor_records_and_failed_modules():
    """The reactor status feeding module metrics comes from test_summary.jsonl;
    _load_test_history must aggregate reactor_summary + failed_modules across
    every line (this is the data source for per-module build status)."""
    line1 = json.dumps({
        "tests_total": 10, "tests_failures": 1, "tests_skipped": 0,
        "reactor_summary": [{"module": "core", "status": "success"}],
        "failed_modules": [],
    })
    line2 = json.dumps({
        "tests_total": 5, "tests_failures": 0, "tests_skipped": 0,
        "reactor_summary": [{"module": "api", "status": "failure"}],
        "failed_modules": ["api"],
    })

    class Orch:
        def execute_command(self, command, **kwargs):
            if "test_summary.jsonl" in command:
                return {"exit_code": 0, "output": f"{line1}\n{line2}"}
            return {"exit_code": 0, "output": ""}

    tool = ReportTool(docker_orchestrator=Orch())
    history = tool._load_test_history()
    labels = {r["module"]: r["status"] for r in history["reactor_records"]}
    assert labels == {"core": "success", "api": "failure"}
    assert "api" in history["failed_modules"]


def test_build_module_metrics_is_memoized_per_run():
    """_build_module_metrics is called for both persistence and the markdown
    breakdown; it must scan the container at most once per report run."""
    tool = ReportTool()
    tool._get_project_info = lambda: {"directory": "/workspace/p", "build_system": "Maven"}
    calls = {"scan": 0}

    class V:
        def _detect_build_system(self, project_dir):
            return "maven"

        def scan_modules(self, project_dir, build_system):
            calls["scan"] += 1
            return [{"path": "core", "name": "core", "class_count": 1, "jar_count": 0,
                     "report_dirs": []}]

        def parse_module_test_reports(self, module_dir, report_dirs):
            return {}

    tool.physical_validator = V()
    a = tool._build_module_metrics({}, generated_at="t")
    b = tool._build_module_metrics({}, generated_at="t")
    assert a is b               # same cached object
    assert calls["scan"] == 1   # scanned only once


def test_build_module_metrics_reconciles_scan_and_reactor(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/p", "build_system": "Maven"})

    class V:
        def scan_modules(self, project_dir, build_system):
            return [
                {"path": "core", "name": "core", "class_count": 50, "jar_count": 1,
                 "report_dirs": ["/workspace/p/core/target/surefire-reports"]},
                {"path": "api", "name": "api", "class_count": 0, "jar_count": 0,
                 "report_dirs": []},
            ]

        def parse_module_test_reports(self, module_dir, report_dirs):
            if report_dirs:
                return {"tests_total": 10, "tests_passed": 9, "tests_failed": 1,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": ["core.FooTest.bad"], "failing_count": 1,
                        "evidence_refs": report_dirs}
            return {}

    tool.physical_validator = V()
    test_history = {
        "reactor_records": [{"module": "core", "status": "success"},
                            {"module": "api", "status": "failure"}],
        "failed_modules": ["api"],
    }
    metrics = tool._build_module_metrics(test_history, generated_at="t")
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["core"]["build_status"] == "success"
    assert by_path["core"]["tests_failed"] == 1
    assert by_path["api"]["build_status"] == "failure"
    assert metrics["module_summary"]["modules_with_test_failures"] == 1


def test_build_module_metrics_detects_gradle_physically(monkeypatch):
    """The build system must be detected physically (via _detect_build_system),
    not from project_info.build_system which is often 'Unknown' at report time.
    Live caffeine run: a Gradle multi-project collapsed to 1 maven module / 0
    classes because the build system defaulted to maven."""
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/caffeine", "build_system": "Unknown"})

    seen = {}

    class V:
        def _detect_build_system(self, project_dir):
            return "gradle"

        def scan_modules(self, project_dir, build_system):
            seen["build_system"] = build_system
            return [
                {"path": "caffeine", "name": "caffeine", "class_count": 200, "jar_count": 1,
                 "report_dirs": ["/workspace/caffeine/caffeine/build/test-results/test"]},
                {"path": "guava", "name": "guava", "class_count": 30, "jar_count": 1,
                 "report_dirs": []},
            ]

        def parse_module_test_reports(self, module_dir, report_dirs):
            if report_dirs:
                return {"tests_total": 500, "tests_passed": 500, "tests_failed": 0,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": [], "failing_count": 0, "evidence_refs": report_dirs}
            return {}

    tool.physical_validator = V()
    metrics = tool._build_module_metrics({}, generated_at="t")
    assert seen["build_system"] == "gradle"            # physical detection won
    assert metrics["module_summary"]["build_systems"] == ["gradle"]
    assert metrics["module_summary"]["modules_total"] == 2
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["caffeine"]["build_status"] == "success"  # artifacts present
    assert by_path["caffeine"]["tests_total"] == 500


def test_build_module_metrics_matches_descriptive_reactor_labels(monkeypatch):
    # Real Maven reactor summaries carry the module <name> display label, not the
    # path-derived key scan_modules produces. The end-to-end producer -> assembler
    # path must still line them up (e.g. "Apache Kafka :: Connect :: API" -> connect/api).
    tool = ReportTool()
    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/p", "build_system": "Maven"})

    class V:
        def scan_modules(self, project_dir, build_system):
            return [
                {"path": "connect/api", "name": "connect:api", "class_count": 0,
                 "jar_count": 0, "report_dirs": []},
                {"path": "connect/runtime", "name": "connect:runtime", "class_count": 0,
                 "jar_count": 0, "report_dirs": []},
            ]

        def parse_module_test_reports(self, module_dir, report_dirs):
            return {}

    tool.physical_validator = V()
    test_history = {
        "reactor_records": [
            {"module": "Apache Kafka :: Connect :: API", "status": "SUCCESS"},
            {"module": "Apache Kafka :: Connect :: Runtime", "status": "FAILURE"},
        ],
    }
    metrics = tool._build_module_metrics(test_history, generated_at="t")
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["connect/api"]["build_status"] == "success"
    assert by_path["connect/runtime"]["build_status"] == "failure"
    assert by_path["connect/runtime"]["build_source"] == "reactor"
    assert metrics["module_summary"]["modules_failed"] == 1


def test_build_module_metrics_returns_none_without_validator():
    tool = ReportTool()
    tool.physical_validator = None
    assert tool._build_module_metrics({}, generated_at="t") is None


def test_submodule_breakdown_section_renders_failures_first():
    from sag.tools.report_tool import ReportTool
    tool = ReportTool()
    metrics = {
        "module_summary": {"modules_total": 3, "modules_built": 1, "modules_failed": 1,
                           "modules_skipped": 1, "modules_with_test_failures": 1,
                           "build_systems": ["maven"], "single_module": False},
        "modules": [
            {"name": "api", "path": "api", "build_status": "success",
             "tests_total": 10, "tests_passed": 10, "tests_failed": 0, "failing_count": 0},
            {"name": "runtime", "path": "runtime", "build_status": "failure",
             "tests_total": None, "failing_count": None},
            {"name": "core", "path": "core", "build_status": "success",
             "tests_total": 20, "tests_passed": 18, "tests_failed": 2, "failing_count": 2},
        ],
    }
    lines = tool._render_submodule_breakdown(metrics)
    body = "\n".join(lines)
    assert "Submodule Breakdown" in body
    assert "3 modules" in body and "1 failed" in body
    # failed/test-failing modules listed before all-green ones
    assert body.index("runtime") < body.index("api")


def test_submodule_breakdown_empty_for_single_module():
    from sag.tools.report_tool import ReportTool
    tool = ReportTool()
    assert tool._render_submodule_breakdown(
        {"module_summary": {"single_module": True}, "modules": [{"name": ".", "path": "."}]}
    ) == []
