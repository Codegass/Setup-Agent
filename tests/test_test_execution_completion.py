"""Execution completion needs a bound task, complete assessment, and real outcomes."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_receipt_assessor import contract_for, receipt_for, wrote_reports

from sag.agent.action_intents import action_fingerprint
from sag.agent.physical_validator import PhysicalValidator


def execution(
    sequence=1,
    *,
    args=None,
    exit_code=0,
    statuses=("passed",),
    runner="mvn",
    timeout=None,
    extra_argv="",
):
    params = {"action": "test", "working_directory": "/workspace/proj"}
    if args:
        params["args"] = args
    if timeout is not None:
        params["timeout"] = timeout
    expected_argv = "test" + (f" {args}" if args else "")
    argv = f"{runner} {expected_argv}"
    contract = contract_for(
        params=params,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id="test:/workspace/proj", tool="build", params=params
        ),
        expected_argv=expected_argv,
    )
    receipt = receipt_for(
        receipt_id=f"inv-maven-1-{sequence:04d}",
        argv=argv + extra_argv,
        compliance="equivalent" if extra_argv else "exact",
        exit_code=exit_code,
        outcome="completed" if exit_code == 0 else "failed",
        lifecycle_state="finished",
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
        report_delta=wrote_reports() if statuses else {"new": [], "changed": []},
        output_content_hash=hashlib.sha256(f"output-{sequence}".encode()).hexdigest(),
        testcase_execution_rows={
            "status": "complete",
            "rows": [
                {"execution_id": f"{sequence}-{index}", "outcome": status}
                for index, status in enumerate(statuses)
            ],
        },
        testcase_outcomes={
            "nodes": [
                {
                    "node_id": f"case-{index}",
                    "status": status,
                    **(
                        {"reason": "expected result differed"}
                        if status in {"failed", "error"}
                        else {}
                    ),
                }
                for index, status in enumerate(statuses)
            ]
        },
    )
    return receipt, contract


def bundle(receipt, *codes):
    return [
        {"receipt_id": receipt["receipt_id"], "typed_code": code}
        for code in (
            "expectation_met" if receipt["exit_code"] == 0 else "expectation_unmet",
            *codes,
        )
    ] + [
        {
            "receipt_id": receipt["receipt_id"],
            "typed_code": "assessment_bundle_complete",
            "scope": receipt["output_content_hash"],
            "fingerprints": {
                key: str(receipt[key])
                for key in (
                    "target_sha",
                    "survey_fingerprint",
                    "config_fingerprint",
                    "document_map_fingerprint",
                    "domain_id",
                    "fact_epoch",
                )
                if key in receipt
            },
        }
    ]


def validator_for(monkeypatch, records, assessments):
    validator = PhysicalValidator(project_path="/workspace")
    receipts = [receipt for receipt, _contract in records]
    contracts = {receipt["receipt_id"]: contract for receipt, contract in records}
    monkeypatch.setattr(validator, "_current_scoped_receipts", lambda _root: receipts)
    monkeypatch.setattr(validator, "_read_live_evidence_assessments", lambda: assessments)
    # Unit-test the consumer after the independent current-authority readers.
    monkeypatch.setattr(
        validator,
        "_test_execution_contract",
        lambda receipt: contracts[receipt["receipt_id"]],
        raising=False,
    )
    return validator


@pytest.mark.parametrize(
    "exit_code,statuses,codes,expected",
    [
        (0, ("passed",), (), "completed"),
        (0, ("passed",), ("prerequisite_executable_missing",), "completed"),
        (0, ("failed",), (), "completed"),
        (1, ("failed",), ("test_failure_exit",), "completed"),
        (1, ("passed",), (), "unknown"),
        (0, ("error",), ("execution_fault",), "partial"),
        (1, ("passed",), ("prerequisite_executable_missing",), "partial"),
        (1, ("skipped",), ("execution_fault",), "failed"),
        (1, (), ("execution_fault",), "failed"),
        (0, ("passed",), ("contract_binding_unknown",), "completed"),
        (0, ("passed",), ("stale_fingerprint",), "unknown"),
    ],
)
def test_completion_is_not_terminal_exit_or_green_report(
    monkeypatch, exit_code, statuses, codes, expected
):
    record = execution(exit_code=exit_code, statuses=statuses)
    validator = validator_for(monkeypatch, [record], bundle(record[0], *codes))
    result = validator._test_execution_receipt_summary("/workspace/proj")
    assert result["state"] == expected
    assert result["observed_rows"] == len(statuses)


@pytest.mark.parametrize(
    "missing", ["ledger", "bundle", "contract", "output_binding", "fingerprint_binding"]
)
def test_missing_current_evidence_never_completes(monkeypatch, missing):
    record = execution()
    assessments = bundle(record[0])
    if missing == "ledger":
        assessments = None
    elif missing == "bundle":
        assessments = assessments[:-1]
    elif missing == "output_binding":
        assessments[-1]["scope"] = "0" * 64
    elif missing == "fingerprint_binding":
        assessments[-1]["fingerprints"]["target_sha"] = "0" * 40
    validator = validator_for(monkeypatch, [record], assessments)
    if missing == "contract":
        monkeypatch.setattr(validator, "_test_execution_contract", lambda _receipt: None)
    assert validator._test_execution_receipt_summary("/workspace/proj")["state"] == "unknown"


@pytest.mark.parametrize("status", ["failed", "error"])
@pytest.mark.parametrize("missing", ["diagnostics", "reason", "red_completeness"])
def test_ignored_red_with_missing_diagnostics_cannot_complete(monkeypatch, status, missing):
    record = execution(statuses=("passed", status))
    if missing == "diagnostics":
        record[0].pop("testcase_outcomes")
    elif missing == "reason":
        record[0]["testcase_outcomes"]["nodes"][1].pop("reason")
    else:
        record[0]["testcase_outcomes"]["truncated"] = True
        record[0]["testcase_row_disclosure"] = {
            "rows_source": "delta_xml",
            "red_rows_complete": False,
        }
    validator = validator_for(monkeypatch, [record], bundle(record[0]))
    assert validator._test_execution_receipt_summary("/workspace/proj")["state"] == "unknown"


@pytest.mark.parametrize("extra_argv", ["", " -Dmaven.test.failure.ignore=true"])
def test_same_task_recovery_can_change_runner_without_inheriting_old_failure(
    monkeypatch, extra_argv
):
    first = execution(1, exit_code=1, runner="/old/bin/mvn", extra_argv=extra_argv, timeout=10)
    second = execution(2, runner="/new/bin/mvn", extra_argv=extra_argv, timeout=20)
    validator = validator_for(
        monkeypatch,
        [first, second],
        bundle(first[0], "execution_fault") + bundle(second[0]),
    )
    result = validator._test_execution_receipt_summary("/workspace/proj")
    assert result["state"] == "completed"
    assert result["receipt_ids"] == [second[0]["receipt_id"]]


@pytest.mark.parametrize(
    "extra_argv,allowed",
    [
        (" -Dmaven.test.failure.ignore=true", True),
        (" -pl core", False),
        (" -Dtest=SmokeTest", False),
        (" -Psmall", False),
    ],
)
def test_subsequence_compliance_does_not_prove_the_frozen_task_scope(
    monkeypatch, extra_argv, allowed
):
    from sag.agent.evidence_assessments import contract_receipt_binding_problem

    receipt, contract = execution(extra_argv=extra_argv)
    assert contract_receipt_binding_problem(contract, receipt) == ""
    validator = PhysicalValidator(
        docker_orchestrator=SimpleNamespace(execute_command=lambda _command: {}),
        project_path="/workspace",
    )
    monkeypatch.setattr(
        "sag.agent.invocation_contracts.read_frozen_contract", lambda *_args: contract
    )
    assert (validator._test_execution_contract(receipt) is not None) is allowed


def test_narrow_retry_cannot_discharge_wider_task(monkeypatch):
    first = execution(1, exit_code=1)
    second = execution(2, args="-pl core -Dtest=SmokeTest")
    validator = validator_for(
        monkeypatch,
        [first, second],
        bundle(first[0], "execution_fault") + bundle(second[0]),
    )
    result = validator._test_execution_receipt_summary("/workspace/proj")
    assert result["state"] == "partial"
    assert result["receipt_ids"] == [first[0]["receipt_id"], second[0]["receipt_id"]]


def test_unbound_retry_cannot_erase_bound_failure(monkeypatch):
    first = execution(1, exit_code=1)
    second = execution(2)
    validator = validator_for(
        monkeypatch,
        [first, second],
        bundle(first[0], "execution_fault") + bundle(second[0]),
    )
    monkeypatch.setattr(
        validator,
        "_test_execution_contract",
        lambda receipt: first[1] if receipt["receipt_id"] == first[0]["receipt_id"] else None,
    )
    assert validator._test_execution_receipt_summary("/workspace/proj")["state"] != "completed"


@pytest.mark.parametrize(
    "state,status,evidence", [("unknown", "WARNING", "unknown"), ("failed", "FAILED", "blocked")]
)
def test_green_report_cannot_override_unfinished_execution(monkeypatch, state, status, evidence):
    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda _root: {
            "valid": True,
            "total_tests": 10,
            "passed_tests": 10,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 0,
        },
    )
    monkeypatch.setattr(
        validator,
        "_test_execution_receipt_summary",
        lambda _root: {"state": state, "reason": "current execution is not established"},
    )
    result = validator.validate_test_status("proj")
    assert result["status"] == status
    assert result["evidence_status"] == evidence
    assert result["passed_tests"] == 10


CALIBRATION_CASES = json.loads(
    (
        Path(__file__).parent / "fixtures/d3r1_remediation/execution_completion_cases.json"
    ).read_text()
)["cases"]


@pytest.mark.parametrize("case", CALIBRATION_CASES, ids=lambda case: case["id"])
def test_d3r1_known_false_completions_remain_rejected(monkeypatch, case):
    from sag.agent.evidence_assessments import execution_faults, prerequisite_assessments

    # A read-only semantic projection, NOT a reconstructed or newly authorized
    # historical receipt. Sampled identities never become complete identities.
    receipt = {
        **case["receipt_fields"],
        "testcase_execution_rows": {
            "status": "partial",
            "rows": [row["record"] for row in case["row_samples"]],
        },
        "testcase_outcomes": {"nodes": [row["record"] for row in case["testcase_outcome_samples"]]},
    }
    assessments = [item["record"] for item in case["assessments"]]
    fragments = None
    if case["output"]["record_output_matches_receipt_hash"]:
        # These exact excerpts exercise only the pure diagnostic recognizers.
        # They must never mint a bundle-complete marker for the old full log.
        fragments = "\n".join(part["text"] for part in case["output"]["excerpts"])
    assessments += [
        item.payload()
        for item in [
            *execution_faults(receipt, fragments),
            *prerequisite_assessments(receipt, fragments),
        ]
    ]
    validator = validator_for(monkeypatch, [(receipt, case["contract"])], assessments)
    result = validator._test_execution_receipt_summary(receipt["working_directory"])
    assert result["state"] != case["expected"]["disallowed_state"]
    if case["id"] == "kafka":
        assert result["state"] == "partial"
        assert result["observed_rows"] == 0
        assert result["reported_executions"] == 15641
    if case["id"] == "storm":
        assert result["state"] == "failed"
    if case["id"] == "seatunnel":
        # The archived console record is truncated and cannot certify that
        # the execution-fault scan covered the full original output.
        assert case["output"]["record_output_matches_receipt_hash"] is False
        assert result["state"] == "partial"


def test_all_skipped_counts_are_not_test_body_executions(monkeypatch):
    record = execution(statuses=("skipped",))
    validator = validator_for(monkeypatch, [record], bundle(record[0]))
    result = validator._test_execution_receipt_summary("/workspace/proj")
    assert result["state"] == "completed"
    assert result["reported_executions"] == 1
    assert validator._test_receipt_observations(record[0]) == (1, 0, 0, 1)
