"""A deterministic Test handoff must use the same pinned-task gate as the actor."""

import pytest
from test_acceptance_task import task_run, retain, dispatch
from test_native_loop_engine import _engine

from sag.agent.phase_machine import PhaseMachine
from sag.agent.verdict_finalizer import VerdictFinalizer, read_verdict_snapshot
from sag.tools.phase_tool import PhaseTool


def reuse_engine(tmp_path, commands=("mvn test",), dispatch_command=None):
    run = task_run(tmp_path, *commands)
    result = retain(run, dispatch(run, dispatch_command or commands[0]))
    engine = _engine([])
    engine.steps = []
    engine._phase_iterations = 0
    engine.config.reuse_verified_test_phase = True
    engine.phase_machine = PhaseMachine(start_phase="test")
    engine.run_evidence_state = run.state
    engine.orchestrator = run.fs
    engine.physical_validator = run.validator
    # ContainerFS cannot execute the in-container XML parser. Supply its
    # observation at that boundary; keep real grading and task receipt binding.
    run.validator.validate_test_status = lambda project_name=None: {
        "has_test_reports": True,
        "status": "SUCCESS",
        "evidence_status": "success",
        "report_files": ["report://current-run"],
        "test_stats": {"executed": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        "receipt_scoped": True,
        "test_execution_state": "completed",
        "test_execution_receipt_ids": [result.metadata["receipt_id"]],
    }
    engine.verdict_finalizer = VerdictFinalizer(
        run.fs,
        validator=run.validator,
        project_name="proj",
        repository=run.task.repo,
        acceptance_task=run.task,
        output_storage=run.storage,
    )
    engine.output_storage = run.storage
    tool = PhaseTool(
        engine.phase_machine, run.validator, run.fs, "proj", run_evidence_state=run.state
    )
    tool.bind_execution_plan_evidence(run.storage)
    engine.tools["phase"] = tool
    # The separate floor tests below cover missing attempts. The production
    # task/receipt reader, runtime checks and physical phase gate stay real.
    engine._missing_required_test_attempt = lambda: None
    return engine, run


def test_reuse_requires_complete_pinned_task(tmp_path):
    engine, run = reuse_engine(tmp_path, ("mvn test", "mvn verify"))
    assert engine._reuse_verified_test_phase() is False
    assert engine.phase_machine.current_phase == "test"
    assert not run.state.sealed


def test_reuse_does_not_discharge_an_outstanding_test_attempt(tmp_path):
    engine, run = reuse_engine(tmp_path)
    engine._missing_required_test_attempt = lambda: object()
    assert engine._reuse_verified_test_phase() is False
    assert not run.state.sealed


def test_reuse_verifies_current_receipts_before_control_transition(tmp_path):
    engine, run = reuse_engine(tmp_path)
    assert engine._reuse_verified_test_phase() is True
    assert engine.phase_machine.current_phase == "report"
    assert run.state.sealed
    # The gate event alone is not the finalizer's evidence input. Its verified
    # facts must survive the automatic transition, just as for an actor close.
    snapshot = read_verdict_snapshot(run.fs)
    assert snapshot.test_stats.receipt_scoped is True
    assert snapshot.test_stats.unique.executed == 1
    assert snapshot.test_stats.unique.passed == 1
    assert snapshot.test_stats.judgment == "success"
    assert "test_executions_unattributed_to_receipts" not in snapshot.conflicts
    assert run.state.fact_value("test.stats")["execution_state"] == "completed"
    assert engine._reuse_verified_test_phase() is False


@pytest.mark.parametrize(
    "command", ["mvn test -DskipTests", "mvn -Pother test", "mvn test -pl child"]
)
def test_narrower_or_different_invocations_cannot_be_reused(tmp_path, command):
    engine, run = reuse_engine(tmp_path, dispatch_command=command)
    assert engine._reuse_verified_test_phase() is False
    assert not run.state.sealed


@pytest.mark.parametrize(
    "defect",
    [
        "missing_receipt",
        "interrupted",
        "test_failure",
        "unbound_test",
        "unknown_completion",
        "runtime",
        "source",
    ],
)
def test_reuse_never_promotes_incomplete_or_mismatched_evidence(tmp_path, defect):
    engine, run = reuse_engine(tmp_path)
    observation = run.validator.validate_test_status()
    if defect == "missing_receipt":
        del run.fs.files[next(p for p in run.fs.files if "/invocation_receipts/" in p)]
    elif defect == "interrupted":
        observation["test_execution_state"] = "partial"
    elif defect == "test_failure":
        observation["test_stats"].update(passed=0, failed=1)
    elif defect == "unbound_test":
        observation["test_execution_receipt_ids"] = ["another-command-receipt"]
    elif defect == "unknown_completion":
        observation.pop("test_execution_state")
    elif defect == "runtime":
        from test_acceptance_task import AcceptanceTask, pin_task

        definition = run.task.model_dump(mode="json")
        definition["steps"][0]["java_major"] = 21
        run.task = AcceptanceTask.model_validate(definition)
        pin_task(run)
        run.fs.acceptance_task = run.task
    else:
        run.fs.dirty = True
    run.validator.validate_test_status = lambda project_name=None: observation
    assert engine._reuse_verified_test_phase() is False
    assert not run.state.sealed
