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
