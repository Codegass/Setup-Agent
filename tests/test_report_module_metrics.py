# tests/test_report_module_metrics.py
from sag.tools.report_tool import ReportTool


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
