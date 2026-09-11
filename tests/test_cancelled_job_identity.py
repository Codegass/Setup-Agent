"""Cancellation must not report a durable invocation as unpersisted as well."""

import pytest
from test_job_settlement import JOB, _orchestrator, _with_obligation
from test_native_loop_engine import _engine

from sag.agent import job_obligations


def cancelled_engine(exit_code):
    orchestrator = _with_obligation(_orchestrator(exit_code=str(exit_code)))
    (record,) = job_obligations.read_obligations(orchestrator)
    # Like DockerOrchestrator, retain the accepted handle after the tool has
    # returned and its complete obligation has been published.
    handle = {
        key: record[key]
        for key in (
            "job_id",
            "terminal_authority",
            "docker_exec_id",
            "container_id",
            "process_identity_token",
            "pid",
            "pgid",
            "log_path",
            "start_accepted",
        )
    }
    orchestrator._detached_handles = {JOB: handle}
    engine = _engine([], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._detached_handle_baseline = frozenset()
    engine._capture_unreturned_detached_handles()
    return engine, orchestrator, handle


@pytest.mark.parametrize("exit_code", [0, 15])
@pytest.mark.parametrize("already_settled", [False, True])
def test_cancelled_durable_job_has_one_terminal_truth(exit_code, already_settled):
    engine, orchestrator, _ = cancelled_engine(exit_code)
    if already_settled:
        result = job_obligations.reconcile_job_obligations(orchestrator)
        assert len(result.settlements) == 1
    assert engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _: None) == "cleared"
    assert not engine._ephemeral_job_handles()
    assert f"job_terminal_unpersisted:{JOB}" not in engine.run_evidence_state.conflicts
    (record,) = job_obligations.read_obligations(orchestrator)
    settlement = job_obligations.settlement_from_ledger(orchestrator, record)
    assert settlement is not None and settlement.exit_code == exit_code


@pytest.mark.parametrize(
    "damage",
    [
        "docker_exec_id",
        "container_id",
        "process_identity_token",
        "missing_identity",
        "unpublished_ledger",
        "missing_ledger",
    ],
)
def test_ephemeral_terminal_needs_the_same_authorized_durable_identity(damage):
    engine, orchestrator, _ = cancelled_engine(15)
    handle = engine._ephemeral_job_handles()[JOB]
    path = f"/workspace/.setup_agent/job_obligations/{JOB}.json"
    if damage == "unpublished_ledger":
        orchestrator.filesystem.files[path] += " "
    elif damage == "missing_ledger":
        del orchestrator.filesystem.files[path]
    elif damage == "missing_identity":
        del handle["process_identity_token"]
    else:
        handle[damage] = "f" * 64
    running, failures = engine._reconcile_ephemeral_jobs()
    assert not running
    # A mismatched transport identity can itself be unobservable. It must
    # never be silently discharged by the other invocation's durable ledger.
    assert failures or f"job_terminal_unpersisted:{JOB}" in engine.run_evidence_state.conflicts
