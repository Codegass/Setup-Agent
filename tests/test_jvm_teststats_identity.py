"""The live compact reader must not merge different module executions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_receipt_scoped_rollup import ReceiptWorkspace, ReceiptOrchestrator, _receipt, _write

from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_test_rows import _row_parser_program, seal_testcase_execution_rows
from sag.agent.invocation_receipts import validate_receipt_v2

FIXTURES = Path(__file__).parent / "fixtures/surefire_retry"


def reader(tmp_path, reports):
    workspace = ReceiptWorkspace(tmp_path)
    paths = [
        _write(workspace.project / name / "target/surefire-reports/TEST-case.xml", body)
        for name, body in reports.items()
    ]
    receipt = _receipt("inv-maven-stats-0001", workspace.project, new=paths)
    receipt["domain_id"] = str(workspace.project)
    manifest = tmp_path / "reports.json"
    manifest.write_text(json.dumps(receipt["report_delta"]["new"]))
    parsed = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", _row_parser_program(), str(manifest)], text=True
        )
    )
    receipt["testcase_execution_rows"] = seal_testcase_execution_rows(
        parsed,
        run_id=receipt["run_id"],
        receipt_id=receipt["receipt_id"],
        tool="maven",
        target_sha=receipt["target_sha"],
        domain_id=receipt["domain_id"],
        working_directory=receipt["working_directory"],
    )
    assert validate_receipt_v2(receipt) == receipt
    orchestrator = ReceiptOrchestrator(workspace)
    validator = PhysicalValidator(
        docker_orchestrator=orchestrator, project_path=str(workspace.workspace)
    )

    def scan(records=None, owners=None):
        return validator._parse_test_reports_compact_in_container(
            str(workspace.project),
            primary_root=str(workspace.project),
            receipt_records=[receipt] if records is None else records,
            owner_receipt_ids=owners,
        )

    return workspace, paths, receipt, scan


XML = '<testsuite tests="1"><testcase classname="org.example.SameTest" name="same"/></testsuite>'


def test_identical_test_names_in_two_receipt_modules_are_two_cases(tmp_path):
    _, _, _, scan = reader(tmp_path, {"one": XML, "two": XML})
    result = scan()
    assert result["raw_total_tests"] == result["unique_tests"] == result["unique_methods"] == 2
    assert len(result["test_histories"]) == 2
    identities = [r["identity"]["module_or_file"] for r in result["test_histories"]]
    assert len(set(identities)) == 2


def test_red_in_one_module_does_not_erase_pass_in_the_other(tmp_path):
    red = XML.replace("/></testsuite>", '><failure message="red"/></testcase></testsuite>')
    _, _, _, scan = reader(tmp_path, {"one": XML, "two": red})
    result = scan()
    assert result["unique_tests"] == 2
    assert result["unique_passed_tests"] == 1 and result["unique_failed_tests"] == 1


def test_partial_module_metadata_does_not_split_duplicate_report_projections(tmp_path):
    # One claimed projection has no testcase row metadata (historical or
    # unavailable). Mixing qualified and raw names would invent a second
    # logical execution of that same case.
    workspace, paths, receipt, scan = reader(tmp_path, {"one": XML})
    duplicate = _write(paths[0].with_name("TEST-copy.xml"), XML)
    unqualified = _receipt("inv-maven-stats-0002", workspace.project, new=[duplicate])
    result = scan([receipt, unqualified])
    assert result["raw_total_tests"] == 2
    assert result["unique_tests"] == result["unique_methods"] == 1
    assert result["flaky_count"] == result["retried_count"] == 0


@pytest.mark.parametrize(
    "fixture,flaky,reruns",
    [
        ("v1-m5-failure-with-retry.xml", 1, 1),
        ("v2-m5-split-retry-pass.xml", 2, 3),
        ("v2-m5-split-retry-red.xml", 2, 5),
        ("v2-newer-split-retry-red.xml", 2, 5),
    ],
)
def test_native_retry_history_reaches_teststats_without_extra_executions(
    tmp_path, fixture, flaky, reruns
):
    _, _, receipt, scan = reader(tmp_path, {"one": (FIXTURES / fixture).read_text()})
    result = scan()
    rows = receipt["testcase_execution_rows"]["rows"]
    assert result["raw_total_tests"] == result["unique_tests"] == len(rows)
    assert result["flaky_count"] == flaky and result["retried_count"] == reruns
    assert sum(h["flaky"] for h in result["test_histories"]) == flaky
    for history in result["test_histories"]:
        if history["flaky"]:
            assert history["first"] in ("failed", "error") and history["worst"] in (
                "failed",
                "error",
            )
            assert history["latest"] == "passed"
    assert result["failed_tests"] + result["error_tests"] == sum(
        r["outcome"] in ("failed", "error") for r in rows
    )


def test_repeated_receipt_claims_do_not_count_native_retries_twice(tmp_path):
    _, _, receipt, scan = reader(
        tmp_path, {"one": (FIXTURES / "v2-m5-split-retry-pass.xml").read_text()}
    )
    result = scan([receipt, receipt])
    assert result["raw_total_tests"] == result["unique_tests"] == 5
    assert result["flaky_count"] == 2 and result["retried_count"] == 3


def test_unclaimed_and_stale_reports_cannot_supply_module_or_retry_evidence(tmp_path):
    _, paths, receipt, scan = reader(
        tmp_path, {"one": (FIXTURES / "v1-m5-failure-with-retry.xml").read_text()}
    )
    absent = scan([], [])
    assert absent["total_tests"] == absent["flaky_count"] == 0
    paths[0].write_text(paths[0].read_text().replace('name="failsOnce"', 'name="changed"'))
    stale = scan()
    assert stale["total_tests"] == stale["flaky_count"] == 0
    assert stale["stale_test_stats"]["executed"] == 3
