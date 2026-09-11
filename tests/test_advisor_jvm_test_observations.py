"""Keep observed Maven tests visible to the next-phase advisor without regrading."""

from types import SimpleNamespace

import pytest

from sag.agent.react_engine import ReActEngine
from sag.tools.base import ToolResult

CSV = {
    "system": "maven",
    "command": "mvn -Ddoclint=all --batch-mode",
    "working_directory": "/workspace/commons-csv",
    "receipt_id": "inv-maven-csv-1",
    "output_ref_id": "output_csv_build",
    "runner_dispatched": True,
    "test_stats_basis": "invocation_report_xml",
    "report_test_counts": {"reported": 985, "passed": 974, "failed": 0, "errors": 0, "skipped": 11},
}


def observation(metadata):
    result = (
        metadata
        if isinstance(metadata, ToolResult)
        else ToolResult.completed_success(output="runner result", metadata=metadata)
    )
    return SimpleNamespace(result=result)


def digest(*metadata):
    engine = ReActEngine.__new__(ReActEngine)
    engine.run_evidence_state = SimpleNamespace(
        tool_observations=tuple(observation(m) for m in metadata)
    )
    before = repr(engine.run_evidence_state)
    result = engine._last_test_attempt_line()
    assert repr(engine.run_evidence_state) == before
    return result


def test_build_phase_maven_reports_are_visible_without_a_literal_test_goal():
    text = digest(CSV)
    assert "mvn -Ddoclint=all --batch-mode" in text
    assert "receipt=inv-maven-csv-1" in text and "output=output_csv_build" in text
    assert "reported=985, passed=974, failed=0, errors=0, skipped=11" in text
    assert "not a completion verdict" in text


def test_a_newer_unmeasured_invocation_does_not_reuse_old_green_counts():
    text = digest(CSV, {**CSV, "receipt_id": "inv-maven-csv-2", "test_stats_basis": "unavailable"})
    assert "receipt=inv-maven-csv-2" in text
    assert "reported=unknown, passed=unknown" in text
    assert "985" not in text


@pytest.mark.parametrize(
    "change",
    [
        {"test_stats_basis": "console_summary"},
        {"report_test_counts": None},
        {"report_test_counts": []},
    ],
)
def test_only_invocation_xml_counts_are_named_as_observed_counts(change):
    text = digest({**CSV, **change})
    assert "reported=unknown" in text and "985" not in text


def test_failed_and_skipped_outcomes_are_kept_separate():
    text = digest(
        {
            **CSV,
            "report_test_counts": {
                "reported": 10,
                "passed": 5,
                "failed": 2,
                "errors": 1,
                "skipped": 2,
            },
        }
    )
    assert "reported=10, passed=5, failed=2, errors=1, skipped=2" in text


def test_a_failed_latest_runner_is_not_hidden_by_an_earlier_success():
    result = ToolResult.completed_failure(
        output="test error",
        error="one failed test",
        metadata={
            **CSV,
            "receipt_id": "inv-maven-csv-failed",
            "report_test_counts": {
                "reported": 2,
                "passed": 1,
                "failed": 1,
                "errors": 0,
                "skipped": 0,
            },
        },
    )
    text = digest(CSV, result)
    assert "receipt=inv-maven-csv-failed" in text and "tool_outcome=failed" in text
    assert "reported=2, passed=1, failed=1" in text and "985" not in text


def test_latest_pytest_attempt_takes_precedence_over_older_maven_reports():
    text = digest(
        CSV,
        {"collection_scope": "full", "command": "pytest", "executed": 0, "collection_errors": 3},
    )
    assert text.startswith("Last test attempt: pytest")
    assert "executed=0, collection_errors=3" in text and "985" not in text


def test_latest_maven_reports_take_precedence_over_older_pytest():
    text = digest({"collection_scope": "full", "command": "pytest", "executed": 0}, CSV)
    assert text.startswith("Last JVM runner observation:") and "reported=985" in text


def test_a_pre_dispatch_parameter_refusal_does_not_invent_an_invocation():
    assert digest({"system": "maven", "runner_dispatched": False}) == ""


def test_gradle_observations_use_the_same_count_and_reference_projection():
    text = digest(
        {**CSV, "system": "gradle", "command": "./gradlew test", "receipt_id": "inv-gradle-1"}
    )
    assert "./gradlew test" in text and "receipt=inv-gradle-1" in text
    assert "reported=985" in text
