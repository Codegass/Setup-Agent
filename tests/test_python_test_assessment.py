"""Python completion requires the exact public selector and its claimed report."""

import shlex
from types import SimpleNamespace

import pytest
from test_receipt_assessor import ABSENT, CURRENT, contract_for, receipt_for, wrote_reports

from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_assessments import _python_semantic_result, assess_receipt
from sag.agent.invocation_contracts import (
    PYTHON_FACADE_EXECUTION_BINDING,
    expected_observations,
)
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_test_rows import seal_testcase_execution_rows

REPORT = "/workspace/.setup_agent/pytest-reports/pytest-attempt-000001.xml"


def python_test_receipt(*, args=None, actual_args=None, outcome="passed", exit_code=0):
    params = {"action": "test", "working_directory": "/workspace/proj"}
    if args is not None:
        params["args"] = args
    contract = contract_for(
        params=params,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id="test:/workspace/proj", tool="build", params=params
        ),
        effective_tool="python",
        execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        expected_observations=expected_observations("test", PYTHON_FACADE_EXECUTION_BINDING),
        direct_falsifiers=[],
    )
    selection = args if actual_args is None else actual_args
    argv = shlex.join(
        ["/workspace/proj/.venv/bin/python", "-m", "pytest"]
        + shlex.split(selection or "")
        + [f"--junitxml={REPORT}"]
    )
    receipt = receipt_for(
        receipt_id="inv-python-test-0001",
        tool="python",
        argv=argv,
        execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
        compliance=ABSENT,
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
        exit_code=exit_code,
        outcome="completed" if exit_code == 0 else "failed",
        report_delta=wrote_reports((REPORT,)),
    )
    parsed = {
        "schema_version": 2,
        "status": "complete",
        "report_count": 1,
        "rows": [
            {
                "report_path": REPORT,
                "report_sha256": "a" * 64,
                "classname": "tests.test_smoke",
                "name": "test_one",
                "source_file": "tests/test_smoke.py",
                "outcome": outcome,
                "execution_ordinal": 1,
            }
        ],
    }
    receipt["testcase_execution_rows"] = seal_testcase_execution_rows(
        parsed,
        run_id=receipt["run_id"],
        receipt_id=receipt["receipt_id"],
        tool="python",
        target_sha=receipt["target_sha"],
        domain_id=receipt["domain_id"],
        working_directory=receipt["working_directory"],
    )
    return contract, receipt


@pytest.mark.parametrize("args", [None, "tests/test_smoke.py", "-k 'one or two'"])
def test_python_exact_selector_and_current_report_prove_test_operation(args):
    contract, receipt = python_test_receipt(args=args)
    result = assess_receipt(contract, receipt, current_fingerprints=CURRENT)
    assert result.typed_code == "expectation_met"


@pytest.mark.parametrize(
    "args,actual_args",
    [(None, "tests/smoke"), ("tests", "tests/smoke"), (None, "-k one")],
)
def test_python_hidden_or_narrowed_selection_stays_unobserved(args, actual_args):
    contract, receipt = python_test_receipt(args=args, actual_args=actual_args)
    assert assess_receipt(contract, receipt, current_fingerprints=CURRENT).typed_code == (
        "expectation_unobserved"
    )


@pytest.mark.parametrize("missing", ["report_claim", "rows", "runner", "junit_path"])
def test_python_exit_zero_cannot_replace_missing_test_evidence(missing):
    contract, receipt = python_test_receipt()
    if missing == "report_claim":
        receipt["report_delta"] = {"new": [], "changed": []}
    elif missing == "rows":
        receipt.pop("testcase_execution_rows")
    elif missing == "runner":
        receipt["argv"] = receipt["argv"].replace("-m pytest", "-m other_runner")
    else:
        receipt["argv"] = receipt["argv"].replace(REPORT, "/tmp/unclaimed.xml")
    assert _python_semantic_result(contract, receipt, CURRENT) == "unobserved"


@pytest.mark.parametrize("args", ["--collect-only", "--co", "--help", "--version"])
def test_python_nonexecution_option_cannot_claim_test_completion(args):
    contract, receipt = python_test_receipt(args=args)
    assert _python_semantic_result(contract, receipt, CURRENT) == "unobserved"


def test_python_assertion_red_keeps_the_exact_operation_and_failure_exit_separate():
    contract, receipt = python_test_receipt(outcome="failed", exit_code=1)
    assert _python_semantic_result(contract, receipt, CURRENT) == "met"
    assert assess_receipt(contract, receipt, current_fingerprints=CURRENT).typed_code == (
        "expectation_unmet"
    )


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("actual_args,allowed", [("tests", True), ("tests/smoke", False)])
def test_python_completion_consumer_checks_the_real_selector_boundary(
    monkeypatch, exit_code, actual_args, allowed
):
    contract, receipt = python_test_receipt(
        args="tests",
        actual_args=actual_args,
        outcome="failed" if exit_code else "passed",
        exit_code=exit_code,
    )
    # Isolate only the already-tested published-contract read. The production
    # consumer must still check the binding and the Python selector itself.
    monkeypatch.setattr(
        "sag.agent.invocation_contracts.read_frozen_contract", lambda *_args: contract
    )
    validator = PhysicalValidator(
        docker_orchestrator=SimpleNamespace(execute_command=lambda _command: {}),
        project_path="/workspace",
    )
    assert (validator._test_execution_contract(receipt) is not None) is allowed
