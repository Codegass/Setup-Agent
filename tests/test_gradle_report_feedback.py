"""Gradle's existing XML receipts must reach the Agent's tool observation."""

import pytest

from test_gradle_receipt_rows_e2e import _run_reactor
from test_invocation_receipts import argv_contract_authority
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator, FakeOutputStorage

from sag.evidence import EvidenceAssessment, InvocationStatus
from sag.tools.internal.gradle_tool import GradleTool

pytestmark = pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")


def xml(*outcomes):
    tags = {"passed": "", "failed": "<failure/>", "error": "<error/>", "skipped": "<skipped/>"}
    cases = "".join(
        f'<testcase classname="example.Cases" name="case{i}">{tags[outcome]}</testcase>'
        for i, outcome in enumerate(outcomes)
    )
    return (
        f'<testsuite tests="{len(outcomes)}" failures="{outcomes.count("failed")}" '
        f'errors="{outcomes.count("error")}" skipped="{outcomes.count("skipped")}">'
        + cases
        + "</testsuite>"
    ).encode()


@pytest.fixture
def report_result(tmp_path, monkeypatch, request):
    options = getattr(request, "param", False)
    options = options if isinstance(options, dict) else {"red": options}
    red = options.get("red", False)
    first = xml("failed", "error", "skipped") if red else xml("passed", "passed", "skipped")
    delivered = []
    original = GradleTool._apply_invocation_receipt

    def capture(tool, result):
        result = original(tool, result)
        delivered.append(result)
        return result

    monkeypatch.setattr(GradleTool, "_apply_invocation_receipt", capture)
    receipt, orchestrator = _run_reactor(
        tmp_path,
        [
            ("core", "customChecks", first, "first"),
            ("native", "test", xml("passed", "passed"), "last"),
        ],
        action="checkFromCI",
        served_from_cache=options.get("cached", False),
    )
    assert len(delivered) == 1
    result = delivered[0]
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    return result, orchestrator


def test_real_gradle_xml_counts_reach_feedback_without_any_console_summary(report_result):
    result, _ = report_result
    assert result.metadata["report_test_counts"] == {
        "reported": 5,
        "passed": 4,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
    }
    assert result.metadata["test_stats_basis"] == "invocation_report_xml"
    assert result.metadata["analysis"]["log_test_results"] is None
    assert result.test_stats.executed == 5 and result.test_stats.passed == 4
    assert result.test_stats.skipped == 1
    assert "hash-verified XML" in result.output
    assert (
        "Invocation XML test records: 5 reported, 0 failures, 0 errors, 1 skipped" in result.output
    )
    assert "no results captured" not in result.output


@pytest.mark.parametrize("report_result", [True], indirect=True)
def test_report_failures_and_errors_are_not_hidden_by_a_green_log(report_result):
    report_result, _ = report_result
    assert report_result.test_stats.failed == report_result.test_stats.errors == 1
    assert report_result.test_stats.passed == 2 and report_result.test_stats.skipped == 1
    assert report_result.evidence_assessment is EvidenceAssessment.PARTIAL
    assert "gradle_success_vs_test_failures" in report_result.conflicts


@pytest.mark.parametrize("report_result", [{"cached": True}], indirect=True)
def test_cached_reports_are_claimed_without_saying_they_were_written_again(report_result):
    result, _ = report_result
    assert result.metadata["report_test_counts"]["reported"] == 5
    assert "XML claimed by this invocation" in result.output
    assert "XML written by this invocation" not in result.output


def execute_with_metadata(
    metadata, *, orchestrator=None, exit_code=0, detached=False, termination=None
):
    output = "> Task :test\n2 tests completed, 0 failed\n" + (
        "BUILD SUCCESSFUL in 1s" if exit_code == 0 else "BUILD FAILED in 1s"
    )
    native = {"output": output, "exit_code": exit_code, "runner_dispatched": True}
    if detached:
        native.update(
            dispatch_status="completed_detached",
            full_output=output,
            dispatch={"job_id": "0123456789ab"},
            lifecycle_state="finished",
        )
    if termination:
        native.update(termination_reason=termination, execution_time=1200.0)
    orchestrator = orchestrator or FakeBuildToolOrchestrator(native)
    orchestrator.monitored_result = native
    working_directory = getattr(
        getattr(orchestrator, "reactor", None), "root", "/workspace/project"
    )
    tool = GradleTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_gradle_feedback")

    def attach(**kwargs):
        tool._pending_invocation_receipt = {
            key: value
            for key, value in metadata.items()
            if key
            in {"receipt_id", "report_test_counts", "receipt_persisted", "receipt_persistence_code"}
        }

    tool._record_invocation_receipt = attach
    with argv_contract_authority(
        executor="gradle",
        action="build",
        expected_argv="--build-cache build",
        cwd=working_directory,
    ):
        return tool.execute(tasks="build", working_directory=working_directory)


@pytest.mark.parametrize("detached", [False, True])
def test_observed_xml_does_not_upgrade_a_failed_runner(report_result, detached):
    source, orchestrator = report_result
    result = execute_with_metadata(
        source.metadata, orchestrator=orchestrator, exit_code=1, detached=detached
    )
    assert not result.succeeded
    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.test_stats.executed == 5 and result.test_stats.passed == 4


def test_observed_xml_does_not_upgrade_a_timeout(report_result):
    source, orchestrator = report_result
    result = execute_with_metadata(
        source.metadata, orchestrator=orchestrator, termination="silent_timeout"
    )
    assert not result.succeeded
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.test_stats.executed == 5


@pytest.mark.parametrize(
    "metadata",
    [
        {"receipt_id": "no-current-reports"},
        {"receipt_persisted": False, "receipt_persistence_code": "transport_write_failed"},
    ],
)
def test_console_counts_are_named_as_diagnostics_when_bound_counts_are_missing(metadata):
    result = execute_with_metadata(metadata)
    assert result.test_stats.executed == 2
    assert result.metadata["test_stats_basis"] == "stdout_summary"
    assert "complete current XML counts are unavailable" in result.output


def test_unparseable_xml_does_not_become_zero_or_a_claim_of_complete_counts(tmp_path, monkeypatch):
    delivered = []
    original = GradleTool._apply_invocation_receipt

    def capture(tool, result):
        result = original(tool, result)
        delivered.append(result)
        return result

    monkeypatch.setattr(GradleTool, "_apply_invocation_receipt", capture)
    _run_reactor(tmp_path, [("core", "test", b"<broken>", "broken")])
    result = delivered[-1]
    assert "report_test_counts" not in result.metadata
    assert result.test_stats is None
    assert "Test counts: unavailable" in result.output
