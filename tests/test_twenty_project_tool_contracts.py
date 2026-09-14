"""Regression boundaries from the twenty-project execution traces."""

import subprocess

import pytest

from sag.agent.tool_orchestration import format_tool_result
from sag.evidence import EvidenceAssessment, TestStats
from sag.metrics.build_scope import parse_ci_command
from sag.metrics.parity import command_parity
from sag.reporting import format_percentage
from sag.tools.base import ToolResult
from sag.tools.bash import BashTool, BashToolConfig
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.project_tool import ProjectTool
from sag.tools.search_tool import SearchTool


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s\\n' 'a;b'",
        "printf '%s\\n' 'ab'",
        "printf '%s\\n' a; printf '%s\\n' b",
        "awk 'BEGIN {t=1; t+=2; print t}'",
        "printf '%s\\n' 'a&&b||c'",
        r"printf '%s\n' a\;b",
        "printf '%s\\n' 'a;b' 2>&1",
        "printf '%s\\n' a | sed 's/a/b/; s/b/c/'",
    ],
)
def test_valid_quoted_inspection_reaches_bash(command):
    assert subprocess.run(["/bin/bash", "-n", "-c", command], capture_output=True).returncode == 0
    tool = BashTool(config=BashToolConfig(allowed_commands=["printf", "awk", "sed"]))
    assert tool._validate_command(command) == (True, "")


@pytest.mark.parametrize("separator", [";", "&&", "||", "|", "\n"])
def test_real_separator_keeps_blocked_command_visible(separator):
    tool = BashTool(config=BashToolConfig(blocked_commands=["blocked-tool"]))
    valid, reason = tool._validate_command(f"printf 'safe; text' {separator} blocked-tool")
    assert not valid and "blocked" in reason


def test_unclosed_quote_is_a_parameter_error_not_a_crash():
    valid, reason = BashTool()._validate_command("printf 'unfinished")
    assert not valid and "quoting" in reason


@pytest.mark.parametrize(
    "failures,errors,skipped", [(2, 9, 13), (0, 1, 0), (1, 0, 0), (0, 0, 2), (0, 0, 0)]
)
def test_maven_feedback_preserves_negative_outcomes(failures, errors, skipped):
    analysis = {
        "tests_run": {"total": 10000, "failures": failures, "errors": errors, "skipped": skipped},
        "exit_code": 0,
    }
    fields = MavenTool.__new__(MavenTool)._maven_evidence_fields(analysis)
    result = ToolResult.completed_success(output="runner returned zero", **fields)
    stats = result.test_stats
    assert (stats.failed, stats.errors, stats.skipped) == (failures, errors, skipped)
    assert stats.passed + failures + errors + skipped == 10000
    visible = format_tool_result("build", result)
    assert f"{failures} failed" in visible
    if errors:
        assert f"{errors} errors" in visible
    if failures or errors:
        assert result.evidence_assessment is EvidenceAssessment.PARTIAL
        assert "<100.0%" in visible if failures + errors + skipped < 5 else "100.0%" not in visible
    else:
        assert "maven_success_vs_test_failures" not in result.conflicts


def test_percentage_keeps_exact_numeric_value_and_honest_display():
    stats = TestStats(executed=10000, passed=9999, errors=1)
    assert stats.pass_rate == 99.99
    assert "<100.0%" in stats.as_summary()
    assert format_percentage(100) == "100.0%"
    assert format_percentage(None) == "N/A"


@pytest.mark.parametrize(
    "launcher", ["/usr/bin/mvn", "/opt/apache-maven-3.9.16/bin/mvn", "/workspace/p/mvnw"]
)
def test_absolute_maven_lifecycle_is_same_without_claiming_more_scope(launcher):
    current = parse_ci_command(f"{launcher} -Pci clean install")
    assert (
        command_parity(parse_ci_command("mvn -Pci clean install"), [current]).status == "equivalent"
    )
    assert current.text == f"{launcher} -Pci clean install"
    assert parse_ci_command(f"{launcher} test && echo done").unsupported_scope_reason
    assert (
        command_parity(
            parse_ci_command("mvn -Pci verify"), [parse_ci_command(f"{launcher} test")]
        ).status
        != "equivalent"
    )


def test_mixed_provision_reports_current_calls_without_installing_half():
    class Installer:
        calls = []

        def execute(
            self,
            action,
            java_version=None,
            java_distribution=None,
            java_capabilities=None,
            maven_version=None,
            packages=None,
        ):
            self.calls.append(
                (
                    action,
                    java_version,
                    java_distribution,
                    java_capabilities,
                    maven_version,
                    packages,
                )
            )
            return ToolResult.completed_success(output="installed")

    installer = Installer()
    tool = ProjectTool(system_tool=installer)
    result = tool.safe_execute(
        action="provision",
        java_version="21",
        java_distribution="graalvm",
        java_capabilities=["native-image"],
        maven_version="3.9.16",
        packages=["git"],
    )
    assert result.error_code == "PROJECT_PROVISION_AMBIGUOUS" and not installer.calls
    calls = result.facts["separate_calls_for_supplied_routes"]
    assert len(calls) == 3
    visible = format_tool_result("project", result)
    assert "separate_calls_for_supplied_routes" in visible and "3.9.16" in visible
    for call in calls:
        assert call["tool"] == "project"
        assert tool.safe_execute(**call["arguments"]).succeeded
    assert len(installer.calls) == 3
    assert installer.calls[0][2:4] == ("graalvm", ["native-image"])


def test_invalid_search_parameters_teach_current_schema_only():
    tool = SearchTool(None)
    result = tool.safe_execute(target="file:/workspace/p", pattern="test", recursive=True)
    assert result.error_code == "UNEXPECTED_PARAMETERS"
    assert result.facts["required_parameters"] == ["target"]
    assert "ignore_case" in result.facts["accepted_parameters"]
    visible = format_tool_result("search", result)
    assert "accepted_parameters" in visible and "ignore_case" in visible
    assert "Suggestions:" not in visible


def test_glob_path_failure_offers_a_valid_name_lookup(tmp_path):
    from test_tool_output_access import ShellTransport

    (tmp_path / "module").mkdir()
    (tmp_path / "module" / "TEST-a.xml").write_text('<testsuite tests="2"/>')
    tool = SearchTool(ShellTransport(tmp_path))
    result = tool.safe_execute(target=f"file:{tmp_path}/**/TEST-*.xml", pattern="testsuite")
    assert result.succeeded is False and result.facts["matched"] is None
    visible = format_tool_result("search", result)
    assert "do not expand globs" in visible and "name_lookup_call" in visible
    call = result.facts["name_lookup_call"]
    recovered = tool.safe_execute(**call["arguments"])
    assert recovered.succeeded and "TEST-a.xml" in recovered.output
