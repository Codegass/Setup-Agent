"""Recovery follows the bound native task, not the phase or facade spelling."""

import pytest
from test_test_execution_completion import bundle, execution, validator_for


@pytest.mark.parametrize("difference", ["phase", "source_command", "system", "combined", "facade"])
def test_same_native_task_retry_supersedes_earlier_runtime_failure(monkeypatch, difference):
    first = execution(1, exit_code=1)
    second = execution(2)
    if difference in {"phase", "combined"}:
        first[1]["intent_domain_id"] = "build:/workspace/proj"
    params = second[1]["requested_call"]["params"]
    if difference in {"source_command", "combined"}:
        params["source_command"] = "mvn test"
    if difference in {"system", "combined"}:
        params["system"] = "maven"
    if difference == "facade":
        second[1]["requested_call"] = {
            "tool": "bash",
            "params": {"command": "./mvnw test", "working_directory": "/workspace/proj"},
        }
    validator = validator_for(monkeypatch, [first, second], bundle(first[0], "execution_fault") + bundle(second[0]))
    summary = validator._test_execution_receipt_summary("/workspace/proj")
    assert summary["state"] == "completed"
    assert summary["receipt_ids"] == [second[0]["receipt_id"]]


@pytest.mark.parametrize("difference", ["environment", "unmapped_parameter", "cwd", "target", "run"])
def test_retry_cannot_erase_a_task_with_different_non_argv_scope(monkeypatch, difference):
    first = execution(1, exit_code=1)
    second = execution(2)
    if difference in {"environment", "unmapped_parameter"}:
        second[1]["requested_call"]["params"][difference] = {"TEST_MODE": "smoke"}
    else:
        field = {"cwd": "expected_cwd", "target": "target_sha", "run": "run_id"}[difference]
        second[1][field] = "different"
    validator = validator_for(monkeypatch, [first, second], bundle(first[0], "execution_fault") + bundle(second[0]))
    summary = validator._test_execution_receipt_summary("/workspace/proj")
    assert summary["state"] == "partial"
    assert summary["receipt_ids"] == [first[0]["receipt_id"], second[0]["receipt_id"]]


def test_successful_retry_with_conflicting_xml_remains_unknown(monkeypatch):
    first = execution(1, exit_code=1)
    second = execution(2)
    first[1]["intent_domain_id"] = "build:/workspace/proj"
    second[0]["testcase_execution_rows"] = {
        "status": "unavailable", "reasons": ["declared_testcase_count_mismatch"], "rows": [],
    }
    validator = validator_for(monkeypatch, [first, second], bundle(first[0], "execution_fault") + bundle(second[0]))
    summary = validator._test_execution_receipt_summary("/workspace/proj")
    assert summary["state"] == "unknown"
    assert summary["receipt_ids"] == [second[0]["receipt_id"]]
    assert summary["reported_executions"] is None


def test_unavailable_testcase_rows_are_not_reported_as_zero_results(monkeypatch):
    record = execution(exit_code=1)
    record[0]["testcase_execution_rows"] = {
        "status": "unavailable", "reasons": ["declared_testcase_count_mismatch"], "rows": [],
    }
    validator = validator_for(monkeypatch, [record], bundle(record[0], "execution_fault"))
    summary = validator._test_execution_receipt_summary("/workspace/proj")
    assert summary["state"] == "failed"
    assert summary["reported_executions"] is None
    assert "counts unavailable" in summary["reason"]
    assert "0 reported" not in summary["reason"]
