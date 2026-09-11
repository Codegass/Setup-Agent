"""Native Surefire failure headings must reach the model as failure names."""

from pathlib import Path

import pytest

from sag.evidence import EvidenceAssessment
from sag.tools.internal.maven_tool import MavenTool
from test_maven_report_feedback import execute_with_metadata, report_metadata

FIXTURES = Path(__file__).parent / "fixtures/maven-modern-failures"
FAILURE = "MoveTest>AbstractJCRTest.run:475->testMoveVisibilityAcrossSessions:47"
ERROR = "org.example.DuplicateSuiteTest$Probe.testRepeated"


@pytest.mark.parametrize("name,expected", [("failure", FAILURE), ("error", ERROR)])
def test_native_modern_failure_sections_keep_case_names_without_summary_noise(name, expected):
    output = (FIXTURES / (name + ".txt")).read_text()
    analysis = MavenTool(None)._analyze_maven_output(output, 0 if name == "failure" else 1)
    assert analysis["failed_tests"] == [expected]


@pytest.mark.parametrize("header", ["Failed tests:", "Tests in error:", "Tests in failure:"])
@pytest.mark.parametrize("level", ["", "[ERROR] "])
def test_legacy_failure_sections_stop_at_a_maven_blank_line(header, level):
    output = f"{header}\n{level}example.Test.testRed:42 expected true\n[INFO] \n[INFO] Building next module\n"
    assert MavenTool(None)._analyze_maven_output(output, 1)["failed_tests"] == [
        "example.Test.testRed:42 expected true"
    ]


@pytest.mark.parametrize("header", ["Failures:", "Errors:"])
def test_unrelated_error_headings_do_not_invent_testcase_names(header):
    output = f"[ERROR] {header}\n[ERROR] Could not resolve dependency\n[INFO] BUILD FAILURE"
    assert MavenTool(None)._analyze_maven_output(output, 1)["failed_tests"] == []


def test_a_summary_without_blank_separation_does_not_become_a_failing_name():
    output = "\n".join(
        [
            "[INFO] Results:",
            "[ERROR] Failures:",
            "[ERROR] sample.Test.red:2 mismatch",
            "[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0",
            "[INFO] BUILD FAILURE",
        ]
    )
    assert MavenTool(None)._analyze_maven_output(output, 1)["failed_tests"] == [
        "sample.Test.red:2 mismatch"
    ]


@pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")
@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("report_metadata", [1], indirect=True)
def test_failure_name_is_visible_in_real_tool_feedback_without_laundering_exit_zero(
    report_metadata, detached
):
    result = execute_with_metadata(
        report_metadata,
        detached=detached,
        exit_code=0,
        output_override=(FIXTURES / "failure.txt").read_text(),
    )
    assert result.metadata["analysis"]["failed_tests"] == [FAILURE]
    assert FAILURE in result.output
    assert result.test_stats.failed == 1
    assert result.metadata["exit_code"] == 0
    assert "maven_success_vs_test_failures" in result.conflicts
    assert result.completed
    assert result.evidence_assessment is EvidenceAssessment.PARTIAL
