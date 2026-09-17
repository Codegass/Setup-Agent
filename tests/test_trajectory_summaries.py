"""A turn reads as a line: what was asked, and what came back."""

from sag.trajectory.schema import SUMMARY_MAX_CHARS
from sag.trajectory.summaries import (
    call_summary,
    observation_outcome,
    observation_summary,
    refusal_summary,
)


def test_build_call_names_its_action_and_command():
    assert (
        call_summary(
            "build",
            {
                "command": "mvn clean verify",
                "system": "maven",
                "working_directory": "/workspace/commons-cli",
            },
        )
        == "verify mvn clean verify"
    )


def test_build_call_without_a_recognisable_action_shows_the_command():
    assert call_summary("build", {"command": "./gradlew check"}) == "./gradlew check"


def test_bash_call_collapses_the_command_to_its_first_line():
    assert call_summary("bash", {"command": "cat <<'EOF'\nline two\nEOF"}) == "cat <<'EOF'"


def test_project_clone_names_the_repository_and_ref():
    assert (
        call_summary(
            "project",
            {
                "action": "clone",
                "repo_url": "https://github.com/apache/commons-cli.git",
                "ref": "e17111798da51037659b3594d9c0b3b525040081",
            },
        )
        == "clone apache/commons-cli@e171117"
    )


def test_project_provision_names_the_toolchain():
    assert (
        call_summary(
            "project", {"action": "provision", "java_distribution": "openjdk", "java_version": "17"}
        )
        == "provision openjdk 17"
    )
    assert (
        call_summary("project", {"action": "provision", "maven_version": "3.9"})
        == "provision maven 3.9"
    )


def test_files_and_search_and_phase_and_advisor_and_report():
    assert call_summary("files", {"action": "write", "path": "/workspace/x/pom.xml"}) == (
        "write /workspace/x/pom.xml"
    )
    assert call_summary("search", {"target": "output_abc", "pattern": "ERROR"}) == (
        "output_abc /ERROR/"
    )
    assert call_summary("search", {"query": "maven enforcer minimum"}) == ("maven enforcer minimum")
    assert call_summary("phase", {"signal": "done", "phase": "build"}) == "done build"
    assert call_summary("advisor", {"question": "what now"}) == "consult"
    assert call_summary("report", {}) == "generate"


def test_an_unknown_tool_shows_its_first_three_scalar_parameters():
    summary = call_summary("mystery", {"a": 1, "b": "two", "c": True, "d": [1, 2], "e": "five"})
    assert summary == "a=1 b=two c=True"


def test_a_summary_is_truncated_with_an_ellipsis():
    summary = call_summary("bash", {"command": "echo " + "x" * 200})
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_no_params_yields_no_summary():
    assert call_summary("build", None) is None
    assert call_summary("build", {}) is None


def test_outcome_reads_success_as_ok():
    assert observation_outcome({"operation_outcome": "success"}) == "ok"


def test_outcome_reads_a_running_dispatch_as_pending():
    assert observation_outcome({"invocation_status": "dispatched"}) == "pending"
    assert observation_outcome({"invocation_status": "running"}) == "pending"


def test_outcome_falls_back_to_failed():
    assert observation_outcome({"operation_outcome": "failed"}) == "failed"
    assert observation_outcome({}) == "failed"


def test_no_result_yields_no_outcome():
    assert observation_outcome(None) is None


def test_build_result_summarises_exit_tests_and_artifacts():
    result = {
        "operation_outcome": "success",
        "facts": {"executed": 994, "passed": 933, "failed": 0, "skipped": 61},
        "metadata": {
            "analysis": {
                "exit_code": 0,
                "artifacts_created": ["a.jar", "b.jar"],
                "log_tests_run": {"errors": 0},
            }
        },
    }
    assert observation_summary("build", result) == "exit 0 · 994 tests · 0 F · 0 E · 61 S · 2 jars"


def test_failed_build_result_names_its_error():
    result = {
        "operation_outcome": "failed",
        "error_code": "MAVEN_VERSION_BELOW_MINIMUM",
        "metadata": {"analysis": {"exit_code": 1}},
    }
    assert observation_summary("build", result) == "exit 1 · MAVEN_VERSION_BELOW_MINIMUM"


def test_provision_result_names_the_version_it_verified():
    result = {
        "operation_outcome": "success",
        "metadata": {"verified_java_version": "17.0.20", "java_version": "17"},
    }
    assert observation_summary("project", result) == "java 17.0.20"


def test_clone_result_names_the_commit_and_path():
    result = {
        "operation_outcome": "success",
        "metadata": {
            "resolved_commit": "e17111798da51037659b3594d9c0b3b525040081",
            "clone_path": "/workspace/commons-cli",
        },
    }
    assert observation_summary("project", result) == "e171117 → /workspace/commons-cli"


def test_phase_result_names_the_gate_word_and_reason():
    result = {
        "operation_outcome": "success",
        "facts": {"gate": "success", "reason": "workspace /workspace/commons-cli exists"},
    }
    assert observation_summary("phase", result) == (
        "gate success · workspace /workspace/commons-cli exists"
    )


def test_advisor_result_says_advice_was_delivered():
    assert observation_summary("advisor", {"operation_outcome": "success"}) == "advice delivered"


def test_a_pending_job_names_its_handle():
    result = {"invocation_status": "dispatched", "metadata": {"job_id": "c523e63040aa"}}
    assert observation_summary("build", result) == "running · job c523e63040aa"


def test_a_refusal_states_its_code():
    outcome, summary = refusal_summary(
        {"refusal_code": "CALL_NOT_EXECUTED", "reason": "phase transition applied"}
    )
    assert outcome == "cancelled"
    assert summary == "cancelled: phase transition applied"


def test_a_non_cancellation_refusal_is_a_refusal():
    outcome, summary = refusal_summary({"refusal_code": "TOOL_PARAMETERS_INVALID"})
    assert outcome == "refused"
    assert summary == "TOOL_PARAMETERS_INVALID"
