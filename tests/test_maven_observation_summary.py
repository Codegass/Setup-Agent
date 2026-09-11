"""A log-banner parser cannot override or imply actual execution evidence."""

from copy import deepcopy

import pytest

from sag.tools.internal.maven_tool import MavenTool


def analysis(**updates):
    return {
        "phases_executed": [],
        "tests_run": None,
        "artifacts_created": [],
        "warnings": [],
        **updates,
    }


def render(value):
    before = deepcopy(value)
    result = MavenTool.__new__(MavenTool)._format_success_output_enhanced(value, "output_current")
    assert value == before
    assert "output_current" in result
    return result


def test_current_xml_is_visible_without_recognized_plugin_banners():
    result = render(
        analysis(
            test_stats_basis="invocation_report_xml",
            tests_run={"total": 1605, "failures": 0, "errors": 0, "skipped": 9},
        )
    )
    assert "Invocation XML test records: 1605 reported, 0 failures, 0 errors, 9 skipped" in result
    assert "NONE DETECTED" not in result and "Phases executed" not in result


@pytest.mark.parametrize("phases", [[], ["compile", "test", "surefire"]])
def test_plugin_labels_do_not_prove_tests_or_compilation_when_counts_are_missing(phases):
    result = render(analysis(phases_executed=phases))
    assert "Test counts: unavailable" in result
    assert "Test phase ran" not in result and "Compilation: successful" not in result
    assert "0 reported" not in result


def test_console_counts_remain_diagnostic_even_when_the_native_command_exited_zero():
    result = render(
        analysis(
            test_stats_basis="stdout_summary",
            tests_run={"total": 20, "failures": 2, "errors": 1, "skipped": 3},
        )
    )
    assert "Console test summary: 20 reported, 2 failures, 1 errors, 3 skipped" in result
    assert "Invocation XML" not in result


def test_explicit_zero_report_counts_are_different_from_unavailable():
    result = render(
        analysis(
            test_stats_basis="invocation_report_xml",
            tests_run={"total": 0, "failures": 0, "errors": 0, "skipped": 0},
        )
    )
    assert "Invocation XML test records: 0 reported" in result
    assert "unavailable" not in result
