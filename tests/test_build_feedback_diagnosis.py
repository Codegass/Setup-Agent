"""Model-visible phase scope and bounded diagnosis, with real task receipts."""

from types import SimpleNamespace

import pytest
from test_acceptance_task import completion, dispatch, retain, task_run
from test_ci_comparison import Runner, XML
from test_advisor_tool import _advisor_engine

from sag.agent.phase_gates import GateResult, gate_observation_text
from sag.agent.phase_machine import PhaseMachine, PhaseOutcome
from sag.agent.react_engine import FAILURE_DIAGNOSIS_GUIDANCE, ReActEngine
from sag.agent.tool_orchestration import format_tool_result
from sag.tools.phase_tool import PhaseTool


@pytest.mark.parametrize("case", ["failed", "remaining", "complete", "missing"])
def test_build_handoff_discloses_task_without_blocking_test_entry(tmp_path, monkeypatch, case):
    commands = ("mvn package", "mvn verify") if case == "remaining" else ("mvn package",)
    run = task_run(tmp_path, *commands)
    if case == "failed":
        red_xml = XML.replace('failures="0"', 'failures="1"').replace(
            "/></testsuite>", '><failure message="wrong"/></testcase></testsuite>'
        )
        monkeypatch.setattr(
            "test_fixed_command_verification.Runner",
            lambda fs: Runner(fs, exit_code=1, xml=red_xml),
        )
    if case != "missing":
        retain(run, dispatch(run, commands[0]))
    before = completion(run).model_dump(mode="json")
    run.validator.validate_build_status = lambda _: {
        "success": True,
        "build_complete": case == "complete",
        "evidence": {"build_system": "maven", "class_count": 42},
    }
    tool = PhaseTool(
        PhaseMachine(start_phase="build"),
        run.validator,
        run.fs,
        "proj",
        run_evidence_state=run.state,
    )
    tool.bind_execution_plan_evidence(run.storage)
    result = tool.execute(action="done", outcome="partial", key_results="Compiled output exists")
    assert result.succeeded, result
    gate = GateResult.from_metadata(result.metadata["gate_result"])
    assert gate.validated_facts["build.test_entry_ready"] is True
    assert gate.validated_outcome in {PhaseOutcome.SUCCESS, PhaseOutcome.PARTIAL}
    assert gate.validated_facts["task_completion"] == before
    text = format_tool_result("phase", result)
    assert "executed successfully" not in text
    assert "compilation-evidence outcome" in text
    assert "does not establish whole-task completion" in text
    if case == "complete":
        assert "Required task: complete (1/1 steps)" in text
    else:
        assert "Required task: incomplete" in text
        assert "Required task: complete" not in text
    if case == "failed":
        assert "Task step-0: failed" in text and "exit_code=1" in text
    if case == "remaining":
        assert "1/2 steps" in text and "Task step-1: missing" in text
    # The disclosure is the same gate object at every delivery origin, and
    # rendering doesn't mutate receipts or turn diagnostics into acceptance.
    for origin in ("terminal_claim", "engine_close", "outcome_revision"):
        visible = gate_observation_text(gate, phase="build", origin=origin, superseded=gate)
        assert "compilation-evidence outcome" in visible
        assert f"Required task: {before['status']}" in visible
    assert completion(run).model_dump(mode="json") == before
    assert not run.state.sealed


def test_unassessed_build_does_not_imply_an_empty_completed_task():
    from test_gate_one_word_fence import _gate

    text = gate_observation_text(_gate(PhaseOutcome.SUCCESS), phase="build")
    assert "Required task completion: unassessed" in text
    assert "Required task: complete" not in text


@pytest.mark.parametrize("phase", ["build", "test"])
def test_executor_and_advisor_receive_the_same_nonblocking_diagnosis_guidance(phase):
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase=phase)
    engine.config = SimpleNamespace(phase_handoff_char_budget=6000)
    engine._phase_budget_numbers = lambda _: (100, 0, 20)
    engine._detected_build_system = lambda: "maven"
    assert FAILURE_DIAGNOSIS_GUIDANCE in engine._phase_intro_step().content
    advisor = _advisor_engine(phase=phase)
    advisor.config.advisor_context_window = 4096
    assert advisor.consult_advisor().succeeded
    messages = [m for call in advisor.llm_client.calls for m in call["messages"]]
    text = "\n".join(m["content"] for m in messages)
    # A small context may split a kept section across consultations. Check the
    # instructions delivered in those actual requests, not only request one.
    for fact in (
        "latest terminal receipt",
        "condition in the failing source code",
        "one concrete read-only measurement",
        "measured versus unknown",
    ):
        assert fact in text
    assert (
        advisor._last_advisor_context["estimated_input_tokens"]
        <= advisor._last_advisor_context["input_token_budget"]
    )
    # Prompt policy only: no new judge, execution, or sealed evidence is produced.
    assert advisor.run_evidence_state is None


def test_advisor_sees_the_required_command_failure_beside_compile_evidence(tmp_path, monkeypatch):
    run = task_run(tmp_path, "mvn package", "mvn verify")
    monkeypatch.setattr(
        "test_fixed_command_verification.Runner", lambda fs: Runner(fs, exit_code=1)
    )
    retain(run, dispatch(run, "mvn package"))
    advisor = _advisor_engine(phase="test")
    advisor.orchestrator = run.fs
    advisor.run_evidence_state = run.state
    advisor.physical_validator = run.validator
    advisor.output_storage = run.storage
    before = completion(run).model_dump(mode="json")
    digest = advisor._advisor_evidence_digest()
    assert "Required task: incomplete (0/2 steps)" in digest
    assert "Task step-0: failed" in digest and "exit_code=1" in digest
    assert "Task step-1: missing" in digest
    assert completion(run).model_dump(mode="json") == before
