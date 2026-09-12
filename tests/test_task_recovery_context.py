"""The executor must see unfinished pinned work after the phase window resets."""

from types import SimpleNamespace

import pytest

from sag.agent.native_messages import render_messages
from sag.agent.invocation_receipts import RECEIPT_DIR
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from test_acceptance_task import dispatch, retain, task_run
from test_system_prompt_native import _prompt


def engine_for(run, phase="test"):
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase=phase)
    engine.orchestrator = run.fs
    engine.run_evidence_state = run.state
    engine.physical_validator = run.validator
    engine.output_storage = run.storage
    engine.config = SimpleNamespace()
    engine._phase_budget_numbers = lambda _: (150, 10, 100)
    engine._ensure_project_facts = lambda: "present"
    engine._detected_build_system = lambda: "maven"
    engine._recommended_build_line = lambda _: None
    engine._native_smoke_guidance = lambda _: None
    return engine


def test_native_window_keeps_missing_work_and_current_receipt_after_phase_reset(tmp_path):
    run = task_run(tmp_path, "mvn test", "mvn verify")
    result = retain(run, dispatch(run, "mvn test"))
    engine = engine_for(run)
    before = dict(run.fs.files)

    messages = render_messages(_prompt(), [engine._phase_intro_step()])
    text = "\n".join(m["content"] for m in messages)
    assert "Required task: incomplete (1/2 steps)" in text
    assert "Task step-0: complete" in text
    assert "Task step-1: missing — mvn verify" in text
    assert result.metadata["receipt_id"] in text
    receipt_path = f"{RECEIPT_DIR}/{result.metadata['receipt_id']}.json"
    assert receipt_path in text
    assert receipt_path in run.fs.files
    assert "search(target='file:<path>')" in text
    assert "Carry out a feasible recovery action" in text
    assert "success is never required to exit" in text
    assert not run.state.sealed
    assert run.fs.files == before  # Projection neither dispatches nor publishes evidence.

    retain(run, dispatch(run, "mvn verify", suffix="recovery"))
    assert "Required task: complete (2/2 steps)" in engine._phase_intro_step().content


def test_missing_output_cannot_be_projected_as_completed_work(tmp_path, monkeypatch):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    monkeypatch.setattr(run.storage, "retrieve_output", lambda _: None)
    text = engine_for(run)._phase_intro_step().content
    assert "Task step-0: unavailable" in text
    assert "Required task: complete" not in text


def test_unavailable_projection_preserves_task_and_does_not_block_the_phase(tmp_path, monkeypatch):
    run = task_run(tmp_path, "mvn test")
    engine = engine_for(run)

    def unavailable(*args, **kwargs):
        raise OSError("receipt reader temporarily unavailable")

    monkeypatch.setattr("sag.agent.acceptance_task.build_task_completion", unavailable)
    text = engine._phase_intro_step().content
    assert "Current task progress: unavailable; the pinned task remains required" in text
    assert engine.phase_machine.current_phase == "test"
    assert not run.state.sealed


@pytest.mark.parametrize("phase", ["provision", "report"])
def test_projection_does_not_read_or_reopen_evidence_outside_execution_phases(
    tmp_path, monkeypatch, phase
):
    run = task_run(tmp_path, "mvn test")
    engine = engine_for(run, phase)

    def unexpected(*args, **kwargs):
        pytest.fail("task projection must not re-assess provision or sealed report")

    monkeypatch.setattr(engine, "_required_task_progress_lines", unexpected)
    engine._phase_intro_step()


def test_historical_task_without_pinned_definition_gets_no_invented_scope(tmp_path):
    run = task_run(tmp_path, "mvn test")
    run.fs.acceptance_task = None
    assert engine_for(run)._required_task_progress_lines() == []
