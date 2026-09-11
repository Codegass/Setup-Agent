import pytest

from sag.tools.internal.maven_tool import MavenTool


@pytest.mark.parametrize("plugin", ["surefire", "maven-surefire-plugin"])
def test_module_summaries_with_modern_or_classic_plugin_headers_are_not_overwritten(plugin):
    output = "\n".join(
        line
        for module, count in [
            ("curator-test", 2),
            ("curator-client", 27),
            ("curator-framework", 244),
        ]
        for line in [
            f"[INFO] --- {plugin}:3.0.0-M5:test (default-test) @ {module} ---",
            "[INFO] Results:",
            f"[WARNING] Tests run: {count}, Failures: 0, Errors: 0, Skipped: 0"
            + (", Flakes: 1" if module == "curator-framework" else ""),
        ]
    )
    analysis = MavenTool(None)._analyze_maven_output(output, 1)
    assert analysis["tests_run"] == {"total": 273, "failures": 0, "errors": 0, "skipped": 0}
    assert analysis["build_success"] is False


@pytest.mark.parametrize("prefix,suffix", [("", ""), ("maven-", "-plugin")])
def test_unit_and_integration_test_summaries_keep_separate_execution_boundaries(prefix, suffix):
    output = "\n".join(
        [
            f"[INFO] --- {prefix}surefire{suffix}:3.5.6:test (default-test) @ demo ---",
            "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 - in a.UnitTest",
            "[INFO] Results:",
            "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0",
            f"[INFO] --- {prefix}failsafe{suffix}:3.5.6:integration-test (integration) @ demo ---",
            "[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 2 - in a.IntegrationTest",
            "[INFO] Results:",
            "[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 2",
            "[INFO] BUILD FAILURE",
        ]
    )
    analysis = MavenTool(None)._analyze_maven_output(output, 1)
    assert analysis["tests_run"] == {"total": 8, "failures": 1, "errors": 1, "skipped": 2}
    assert analysis["test_failure_count"] == analysis["test_error_count"] == 1


@pytest.mark.parametrize("receipt_present", [True, False])
def test_current_xml_counts_need_a_receipt_before_overriding_console_diagnostics(receipt_present):
    tool = MavenTool(None)
    analysis = tool._analyze_maven_output(
        "[INFO] --- surefire:3.5.6:test (default-test) @ demo ---\n"
        "[INFO] Results:\n[INFO] Tests run: 273, Failures: 0, Errors: 0, Skipped: 0",
        1,
    )
    tool._pending_invocation_receipt = {
        "report_test_counts": {
            "reported": 270,
            "passed": 268,
            "failed": 1,
            "errors": 0,
            "skipped": 1,
        },
        **({"receipt_id": "inv-console-xml-precedence"} if receipt_present else {}),
    }
    tool._prefer_current_report_counts(analysis)
    expected = (
        {"total": 270, "failures": 1, "errors": 0, "skipped": 1}
        if receipt_present
        else {"total": 273, "failures": 0, "errors": 0, "skipped": 0}
    )
    assert analysis["tests_run"] == expected
    assert analysis["test_stats_basis"] == (
        "invocation_report_xml" if receipt_present else "stdout_summary"
    )
    assert analysis["build_success"] is False
