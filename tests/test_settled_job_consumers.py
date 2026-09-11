"""A real published settlement supersedes the initiating call's pending result."""

from types import SimpleNamespace

import pytest
from test_job_settlement import (
    JOB,
    LOG_PATH,
    POLARIS_LOG,
    ROOT,
    _orchestrator,
    _settled_receipt,
    _with_obligation,
)

from sag.agent import job_obligations
from sag.agent.react_engine import ReActEngine
from sag.tools.base import ToolResult


def settled_run(**kwargs):
    orchestrator = _orchestrator(**kwargs)
    _with_obligation(orchestrator)
    settled = job_obligations.reconcile_job_obligations(orchestrator)
    assert len(settled.settlements) == 1
    return orchestrator, _settled_receipt(orchestrator)


def engine_for(orchestrator, receipt, **metadata):
    engine = ReActEngine.__new__(ReActEngine)
    engine.orchestrator = orchestrator
    result = ToolResult(
        invocation_status="pending",
        operation_outcome="unknown",
        evidence_status="unknown",
        poll_ref=f"job:{JOB}",
        output="Still running after the soft window",
        metadata={
            "system": "gradle",
            "runner_dispatched": True,
            "command": "./gradlew --continue test",
            "working_directory": ROOT,
            "job_id": JOB,
            **metadata,
        },
    )
    engine.run_evidence_state = SimpleNamespace(
        run_id=receipt["run_id"],
        tool_observations=(SimpleNamespace(result=result),),
    )
    return engine


def test_advisor_observes_current_settlement_instead_of_pending_tool_outcome():
    orchestrator, receipt = settled_run()
    engine = engine_for(orchestrator, receipt)
    original = repr(engine.run_evidence_state.tool_observations)
    text = engine._last_test_attempt_line()
    assert "[settled]" in text and "exit 0" in text
    assert receipt["receipt_id"] in text and f"job:{JOB}" in text
    assert "tool_outcome=unknown" not in text
    assert repr(engine.run_evidence_state.tool_observations) == original


def test_advisor_keeps_a_settled_failure_visible():
    orchestrator, receipt = settled_run(exit_code="7")
    text = engine_for(orchestrator, receipt)._last_test_attempt_line()
    assert "[settled]" in text and "exit 7" in text
    assert "exit 0" not in text


def test_advisor_does_not_treat_a_running_job_as_settled():
    orchestrator = _orchestrator(exit_code=None)
    _with_obligation(orchestrator)
    (record,) = job_obligations.read_obligations(orchestrator)
    text = engine_for(orchestrator, record)._last_test_attempt_line()
    assert "[settled]" not in text and "tool_outcome=unknown" in text


@pytest.mark.parametrize("mismatch", ["job", "run", "cwd", "receipt"])
def test_advisor_does_not_borrow_another_or_unreadable_settlement(mismatch):
    orchestrator, receipt = settled_run()
    engine = engine_for(orchestrator, receipt, **({"job_id": "other"} if mismatch == "job" else {}))
    if mismatch == "run":
        engine.run_evidence_state.run_id = "another-run"
    elif mismatch == "cwd":
        engine = engine_for(orchestrator, receipt, working_directory="/workspace/other")
    elif mismatch == "receipt":
        del orchestrator.filesystem.files[
            f"/workspace/.setup_agent/invocation_receipts/{receipt['receipt_id']}.json"
        ]
    text = engine._last_test_attempt_line()
    assert "[settled]" not in text
    assert "tool_outcome=unknown" in text


def test_settled_output_is_read_without_manufacturing_a_tool_result():
    orchestrator, receipt = settled_run()
    assert job_obligations.read_settled_job_output(orchestrator, receipt) == POLARIS_LOG


def test_observed_empty_job_output_is_distinct_from_a_missing_log():
    orchestrator, receipt = settled_run(files={LOG_PATH: ""})
    assert job_obligations.read_settled_job_output(orchestrator, receipt) == ""


@pytest.mark.parametrize(
    "damage", ["log", "missing_log", "receipt", "unpublished_obligation", "forged_hash"]
)
def test_settled_output_requires_the_same_authorized_job_and_output_bytes(damage):
    orchestrator, receipt = settled_run()
    if damage == "log":
        orchestrator.filesystem.files[LOG_PATH] = "BUILD SUCCESSFUL, but different bytes"
    elif damage == "missing_log":
        del orchestrator.filesystem.files[LOG_PATH]
    elif damage == "receipt":
        receipt = {**receipt, "argv": "./gradlew unrelated"}
    elif damage == "unpublished_obligation":
        path = f"/workspace/.setup_agent/job_obligations/{JOB}.json"
        orchestrator.filesystem.files[path] += " "
    else:
        from sag.agent.invocation_receipts import output_content_hash

        orchestrator.filesystem.files[LOG_PATH] = "replacement"
        receipt = {**receipt, "output_content_hash": output_content_hash("replacement")}
    assert job_obligations.read_settled_job_output(orchestrator, receipt) is None
