"""An outcome/identity gap cannot erase a bound test attempt in Build."""

import pytest
from test_test_execution_completion import bundle, execution, validator_for
from test_test_attempt_policy import _ready_state

from sag.agent.attempt_policy import (
    TestAttemptRequirement as Requirement,
    TestCandidateResolution as Resolution,
    required_test_attempt,
)


def floor(validator):
    primary = Requirement(
        root="/workspace/proj",
        system="maven",
        required_action={
            "tool": "build",
            "params": {"action": "test", "working_directory": "/workspace/proj"},
        },
    )
    return required_test_attempt(
        _ready_state(),
        None,
        phase="test",
        attempt_id="test-1",
        validator=validator,
        resolution=Resolution(
            status="available",
            project_root=primary.root,
            workspace_root="/workspace",
            primary=primary,
            candidates=(primary,),
        ),
    )


@pytest.mark.parametrize("problem", ["identity", "diagnostics", "runner_failure"])
def test_bound_build_test_attempt_prevents_forced_repeat_without_green_verdict(
    monkeypatch, problem
):
    record = execution(
        exit_code=1 if problem == "runner_failure" else 0,
        statuses=("passed", "error") if problem == "diagnostics" else ("passed",),
    )
    record[1]["intent_domain_id"] = "build:/workspace/proj"
    if problem == "identity":
        record[0]["testcase_execution_rows"] = {
            "status": "unavailable",
            "reasons": ["module_qualified_identity_unavailable"],
            "rows": [],
        }
    elif problem == "diagnostics":
        record[0].pop("testcase_outcomes")
    codes = ("execution_fault",) if problem == "runner_failure" else ()
    validator = validator_for(monkeypatch, [record], bundle(record[0], *codes))

    summary = validator._test_execution_receipt_summary("/workspace/proj")
    assert summary["state"] != "completed"
    assert floor(validator) is None
    # Satisfying the dispatch floor never upgrades the separate judgment.
    assert validator._test_execution_receipt_summary("/workspace/proj") == summary


@pytest.mark.parametrize("missing", ["contract", "terminal", "scope", "test_task"])
def test_unbound_or_unexecuted_evidence_cannot_discharge_the_floor(monkeypatch, missing):
    record = execution(statuses=() if missing == "test_task" else ("passed",))
    validator = validator_for(monkeypatch, [record], bundle(record[0]))
    if missing == "contract":
        monkeypatch.setattr(validator, "_test_execution_contract", lambda _receipt: None)
    elif missing == "terminal":
        record[0]["lifecycle_state"] = "vanished"
    elif missing == "scope":
        monkeypatch.setattr(validator, "_current_scoped_receipts", lambda _root: None)
    else:
        record[0]["requested_action"] = record[0]["effective_action"] = "compile"
    assert floor(validator) is not None


def test_no_execution_cannot_borrow_an_unknown_summary(monkeypatch):
    validator = validator_for(monkeypatch, [], [])
    assert floor(validator) is not None
