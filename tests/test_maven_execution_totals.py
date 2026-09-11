"""Maven counts survive bounded or unavailable identities on the live path."""

import copy
import json
from pathlib import Path

import pytest
from test_ci_comparison import ROOT, REPORT, XML, setup_run

from sag.agent import receipt_test_rows
from sag.agent.invocation_receipts import build_receipt, validate_receipt_v2
from sag.agent.java_success_certificates import receipt_bound_test_counts
from sag.agent.receipt_suite_totals import receipt_suite_totals
from sag.tools.report_metrics import _receipt_row_projection


@pytest.fixture(autouse=True)
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt_test_rows, "REPORT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


def test_receipt(run):
    return next(
        json.loads(raw)
        for path, raw in run.fs.files.items()
        if "/invocation_receipts/" in path
        and path.endswith(".json")
        and json.loads(raw).get("effective_action") == "test"
    )


test_receipt.__test__ = False


def projection(receipt):
    return _receipt_row_projection(
        [receipt], run_id=receipt["run_id"], run_target_sha=receipt["target_sha"]
    )


def test_bounded_maven_rows_keep_complete_counts_through_publication_and_consumers():
    cases = "".join(f'<testcase classname="a.T" name="case{i}"/>' for i in range(2100))
    run = setup_run(xml=f'<testsuite tests="2100">{cases}</testsuite>')
    receipt = test_receipt(run)
    assert len(receipt["testcase_execution_rows"]["rows"]) == 2048
    assert receipt["testcase_row_disclosure"]["rows_truncated"]["dropped_green"] == 52
    assert receipt["testcase_execution_totals"] == {
        "report_count": 1,
        "reported": 2100,
        "passed": 2100,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert validate_receipt_v2(json.loads(json.dumps(receipt))) == receipt
    assert receipt_suite_totals(receipt).complete_claims is True
    assert receipt_bound_test_counts(receipt).counts.reported == 2100
    assert projection(receipt)["executions"]["executed"] == 2100
    assert (
        projection(receipt)["executions"]["basis"] == "report XML totals over every claimed report"
    )
    summary = run.validator._test_execution_receipt_summary(ROOT)
    assert summary["state"] == "completed" and summary["reported_executions"] == 2100


def test_mixed_maven_gradle_reports_keep_counts_without_inventing_module_identity():
    run = setup_run(
        report_files={
            REPORT: XML,
            ROOT + "/plugin/build/test-results/test/TEST-native.xml": XML,
        }
    )
    receipt = test_receipt(run)
    assert receipt["testcase_execution_rows"]["status"] == "unavailable"
    assert receipt_suite_totals(receipt).tests == 2
    metrics = projection(receipt)
    assert metrics["claimed"] is None and metrics["identity_complete"] is False
    assert metrics["executions"]["executed"] == 2
    summary = run.validator._test_execution_receipt_summary(ROOT)
    assert summary["state"] == "completed" and summary["reported_executions"] == 2


def test_real_surefire_retry_totals_count_logical_cases():
    fixture = Path(__file__).parent / "fixtures/surefire_retry/v1-m5-failure-with-retry.xml"
    receipt = test_receipt(setup_run(xml=fixture.read_text()))
    counts = receipt_bound_test_counts(receipt).counts
    assert counts.reported == counts.passed == 3 and counts.red == 0
    assert (
        sum(bool(r.get("runner_reruns")) for r in receipt["testcase_execution_rows"]["rows"]) == 1
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"reported": True},
        {"reported": 1.0},
        {"passed": -1},
        {"passed": 2},
        {"reported": 0, "passed": 0},
        {"reported": 2, "passed": 2},
        {"report_count": 0},
        {"report_count": 2},
        {"unexpected": 1},
    ],
)
def test_invalid_totals_are_rejected_by_the_strict_reader(bad):
    receipt = test_receipt(setup_run())
    receipt["testcase_execution_totals"].update(bad)
    with pytest.raises(ValueError, match="testcase_execution_totals"):
        validate_receipt_v2(receipt)


def test_totals_cannot_hide_a_red_identity():
    red = '<testsuite tests="1" failures="1"><testcase classname="a.T" name="red"><failure message="red"/></testcase></testsuite>'
    receipt = test_receipt(setup_run(xml=red, exit_code=1))
    assert receipt_suite_totals(receipt).failed == 1
    receipt["testcase_execution_totals"].update(passed=1, failed=0)
    with pytest.raises(ValueError, match="testcase_execution_totals"):
        validate_receipt_v2(receipt)


def test_impossible_totals_do_not_destroy_the_native_receipt():
    receipt = build_receipt(
        receipt_id="inv-maven-totals-0001",
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory=ROOT,
        exit_code=0,
        before={},
        after={REPORT: "a" * 64},
        testcase_execution_totals={
            "report_count": 1,
            "reported": 1,
            "passed": 2,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        },
    )
    assert receipt["exit_code"] == 0 and receipt["argv"] == "mvn test"
    assert "testcase_execution_totals" not in receipt
    assert any(e["field"] == "testcase_execution_totals" for e in receipt["evidence_omissions"])
    assert receipt_suite_totals(receipt) is None


def test_historical_maven_receipts_do_not_acquire_totals_or_zeroes():
    receipt = copy.deepcopy(test_receipt(setup_run()))
    receipt.pop("testcase_execution_totals", None)
    assert validate_receipt_v2(receipt) == receipt
    assert receipt_suite_totals(receipt) is None


def test_unparseable_xml_never_publishes_complete_counts():
    receipt = test_receipt(
        setup_run(xml='<testsuite tests="4"><testcase classname="a.T" name="one"/></testsuite>')
    )
    assert "testcase_execution_totals" not in receipt
    assert receipt_suite_totals(receipt) is None


def test_complete_counts_do_not_replace_assessment_authority():
    run = setup_run()
    assert receipt_suite_totals(test_receipt(run)).tests == 1
    for path in list(run.fs.files):
        if "/evidence_assessments/" in path:
            del run.fs.files[path]
    assert run.validator._test_execution_receipt_summary(ROOT)["state"] == "unknown"
