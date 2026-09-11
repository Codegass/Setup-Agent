"""A missing required step bounds success, without closing ordinary actions."""

import pytest

from test_acceptance_task import completion, retain, task_run
from test_ci_comparison import ROOT, Runner
from test_fixed_command_verification import dispatch
from test_terminal_claim_convergence import _engine

from sag.agent.invocation_contracts import clear_action_context
from sag.agent.phase_gates import (
    GateControlDisposition,
    GateResult,
    ValidatorState,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseMachine
from sag.agent.replay import recover_active_repair_context
from sag.agent.tool_orchestration import ToolCall, ToolExecution
from sag.tools.build.build_tool import BuildTool
from sag.tools.phase_tool import PhaseTool


@pytest.mark.parametrize("next_tool", ["build", "bash", "project", "file_io", "advisor"])
def test_unfinished_task_feedback_keeps_ordinary_actions_open(tmp_path, next_tool):
    run = task_run(tmp_path, "mvn test", "mvn verify")
    retain(run, dispatch(run, "mvn test"))
    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="test")
    engine.orchestrator = run.fs
    engine.run_evidence_state = run.state
    engine.successful_states = {"working_directory": ROOT}
    engine._active_native_tool_call_id = "continue-required-task"
    phase = PhaseTool(
        engine.phase_machine,
        run.validator,
        run.fs,
        "proj",
        run_evidence_state=run.state,
        gate_fn=lambda phase, claim, *args, **kwargs: validate_phase_claim(
            claim, ValidatorState.GREEN
        ),
    )
    phase.bind_execution_plan_evidence(run.storage)
    result = phase.execute(action="done", outcome="success")
    assert result.error_code == "required_task_incomplete"
    assert "step-1" in result.output and "missing" in result.output
    call = ToolCall(name="phase", raw_params={"action": "done", "outcome": "success"})
    execution = ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        attempted_execution=True,
        observation_text=result.output,
    )
    prepared = engine._prepare_rejected_completion(execution)
    assert prepared is not None and not prepared.gate.accepted
    assert engine._apply_rejected_completion_control(prepared) is False
    assert engine._pending_repair_context is None
    assert recover_active_repair_context(engine.control_event_sink.events).context is None

    # Replay applies the same claim matrix to the persisted grading.
    gate = prepared.gate
    regraded = validate_phase_claim(
        prepared.claim,
        gate.validator_state,
        reason=gate.reason,
        evidence_refs=gate.evidence_refs,
        code=gate.code,
        validated_facts=gate.validated_facts,
        control_disposition=gate.control_disposition,
        blocker_owner=gate.blocker_owner,
    )
    assert regraded.to_metadata() == gate.to_metadata()
    assert GateResult.from_metadata(gate.to_metadata(), claim=prepared.claim) == gate

    params = {
        "build": {"command": "mvn verify", "working_directory": ROOT},
        "bash": {"command": "./native-test", "working_directory": ROOT},
        "project": {"action": "provision", "java_version": "21"},
        "file_io": {"action": "read", "path": ROOT + "/pom.xml"},
        "advisor": {"question": "What is needed for the remaining required step?"},
    }[next_tool]
    call = ToolCall(name=next_tool, raw_params=params)
    try:
        assert engine._prepare_control_action(call, params)
        assert call.action_intent.repair_context_id is None
        assert engine.control_event_sink.events[-1].payload["exact_params"] == params
        if next_tool == "build":
            runner = Runner(run.fs)
            runner.sequence = 40
            result = BuildTool(run.fs, maven_tool=runner).execute(**params)
            assert result.succeeded, result
            retain(run, result)
            assert completion(run).status == "complete"
            assert phase.execute(action="done", outcome="success").succeeded
    finally:
        clear_action_context()
    assert not engine.run_evidence_state.sealed


@pytest.mark.parametrize(
    "change", ["missing_facts", "bad_status", "wrong_code", "red", "other_phase"]
)
def test_task_feedback_does_not_relax_unrelated_failure_repair(tmp_path, change):
    run = task_run(tmp_path, "mvn test")
    task = completion(run).model_dump(mode="json")
    facts = {"task_completion": task}
    code, phase, state = "required_task_incomplete", "test", ValidatorState.PARTIAL
    if change == "missing_facts":
        facts = {}
    elif change == "bad_status":
        task["status"] = "unsupported"
    elif change == "wrong_code":
        code = "required_task_unavailable"
    elif change == "red":
        state = ValidatorState.RED
    else:
        phase = "build"
    gate = validate_phase_claim(
        PhaseClaim(phase=phase, signal="done", claimed_outcome="success"),
        state,
        code=code,
        validated_facts=facts,
    )
    assert not gate.accepted
    assert gate.control_disposition is GateControlDisposition.REPAIR_REQUIRED
