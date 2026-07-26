# tests/test_advisor_guarantees.py
"""The three mechanical guarantees (spec §3.2): consult-at-entry,
before-giving-up, when-stuck.

Guarantees 2 and 3 are engine-level PRE-EXECUTION redirects: the call never
reaches the tool, and the model gets a tool result naming the one concrete next
action (`advisor()`). Redirects travel the same evidence-recording path as the
Plan-2 refusals, so the pairing invariant and the audit trail hold.

SUPERSEDED (2026-07-26 post-acceptance audit): guarantee 1 used to be a THIRD
redirect, "before-acting" — the phase's first state-changing call was refused
and pointed at `advisor()`. It cancelled two correctly-planned 4-island batches
in bigtop r1 (8 wasted calls) and re-armed on every phase re-entry, so it was
replaced by the harness-authored consult at phase entry. Its expectations below
are inverted accordingly; the mechanism itself is covered by
`tests/test_consult_at_entry.py`.

Two inertness properties are as load-bearing as the redirects themselves: with
`advisor_mode == "off"` (the §3.7.6 ablation) and with the phase cap
exhausted, every rule stands down. A guarantee that outlives the advisor's
ability to answer it is a dead-lock, not a guarantee.
"""

from types import SimpleNamespace

import pytest

from sag.agent.loop_memory import ActionKey, LoopDecision, OutcomeKey, RelevantStateVector
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import ToolCall
from sag.config.prompt_loader import load_react_engine_prompts


class _ScriptedAdvisorClient:
    def __init__(self, advice="Check the provider install first."):
        self.advice = advice
        self.calls = []

    def capabilities_for(self, mode):
        return SimpleNamespace(model="scripted-action-model")

    def get_advisor_response(self, messages, *, model, max_tokens):
        self.calls.append(model)
        return self.advice


def _scripted_result(outcome="success"):
    """A stand-in for ToolResult carrying only what the ACTION path reads."""
    return SimpleNamespace(
        succeeded=outcome == "success",
        error_code=None,
        error=None,
        output="ok",
        output_ref=None,
        evidence_refs=[],
        refs=[],
        metadata={},
        invocation_status=SimpleNamespace(value="completed"),
        operation_outcome=SimpleNamespace(value=outcome),
        evidence_status=SimpleNamespace(value="present"),
        evidence_assessment=SimpleNamespace(value="supported"),
        failure_signature=None,
        error_tail_preview=None,
    )


def _loop_decision(kind, *, recurrence_count):
    """A real LoopDecision, so the arming rule is tested against real fields."""
    return LoopDecision(
        decision=kind,
        action_key=ActionKey(tool_name="build", normalized_target=("compile", "/workspace")),
        outcome_key=OutcomeKey(
            operation_outcome="failed",
            error_code="BUILD_FAILED",
            failure_signature="BUILD_FAILED:abc",
        ),
        relevant_state_vector=RelevantStateVector(values=(("dependencies", 1),)),
        recurrence_count=recurrence_count,
        prior_attempt_ids=("build-1",),
        missing_progress_scopes=("dependencies",),
        request_thinking=kind in {"guide", "force_break"},
    )


def _engine(
    *,
    phase="build",
    advisor_mode="same-model",
    advisor_phase_cap=4,
    outcome="success",
    loop_decision=None,
):
    engine = ReActEngine.__new__(ReActEngine)
    engine.steps = []
    engine.current_iteration = 4
    engine.config = SimpleNamespace(
        verbose=False,
        advisor_mode=advisor_mode,
        advisor_phase_cap=advisor_phase_cap,
        advisor_max_tokens=2048,
        phase_handoff_char_budget=6000,
    )
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine.context_manager = SimpleNamespace(current_task_id=None)
    engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda name: None)
    engine.emit = lambda *a, **k: None
    engine.run_evidence_state = None
    engine.phase_machine = SimpleNamespace(current_phase=phase)
    engine.phase_handoff = None
    engine.physical_validator = None
    engine.loop_memory = None
    engine.output_storage = None
    engine.prompts = load_react_engine_prompts()
    engine.llm_client = _ScriptedAdvisorClient()
    engine.executed_calls = []
    engine.emitted_tool_results = []

    def fake_execute_tool_call(call):
        engine.executed_calls.append(call)
        return SimpleNamespace(
            call=call,
            result=_scripted_result(outcome),
            status="success",
            raw_params=call.raw_params,
            validated_params=dict(call.raw_params),
            observation_text=f"[{call.name}] executed",
            attempted_execution=True,
            metadata={},
            actual_executions=[],
        )

    def fake_add_observation_step(observation):
        step = ReActStep(
            step_type=StepType.OBSERVATION,
            content=observation,
            timestamp="scripted",
        )
        engine.steps.append(step)
        return step

    engine._execute_tool_call = fake_execute_tool_call
    engine._record_execution_bundle = lambda execution, call: (
        execution.result,
        "execution-1",
        [],
    )
    engine._emit_control_action_envelope = lambda tool, params: None
    engine._emit_control_tool_result = lambda **kwargs: engine.emitted_tool_results.append(kwargs)
    engine._apply_tool_execution_loop_effects = lambda execution: loop_decision
    engine._close_phase_for_loop = lambda decision, execution: False
    engine._missing_required_test_attempt = lambda: None
    engine._add_observation_step = fake_add_observation_step
    engine._reset_advisor_run_state()
    return engine


def _call(name, params=None):
    return ToolCall(name=name, raw_params=dict(params or {}))


def _step(name, params=None, call_id="call_1"):
    return ReActStep(
        step_type=StepType.ACTION,
        content=name,
        tool_name=name,
        tool_params=dict(params or {}),
        timestamp="scripted",
        tool_call_id=call_id,
    )


def _observations(engine):
    return [step for step in engine.steps if step.step_type == StepType.OBSERVATION]


# --- (a) guarantee 1 is no longer a redirect -------------------------------
#
# SUPERSEDED EXPECTATION (audit 2026-07-26): these three used to assert that
# the phase's first state-changing call was refused and redirected. That is
# exactly the behavior the audit falsified — the redirect cancelled planned
# work — so the expectation is inverted: the first call EXECUTES, and the
# advice reaches the model through the entry consult instead
# (tests/test_consult_at_entry.py).


def test_the_first_state_changing_build_action_is_no_longer_redirected():
    engine = _engine(phase="build")

    engine._execute_action_step(_step("build", {"action": "compile"}))

    assert [call.name for call in engine.executed_calls] == ["build"]
    assert _observations(engine)[0].content == "[build] executed"
    assert _observations(engine)[0].tool_call_id == "call_1"


def test_a_whole_planned_batch_survives_the_phase_without_a_consult():
    """The bigtop r1 shape at unit scale: four islands planned in one turn, all
    four executed. Under before-acting the first was cancelled and the model
    had to re-remember the other three."""
    engine = _engine(phase="build")

    for index, root in enumerate(("/a", "/b", "/c", "/d"), start=1):
        engine._execute_action_step(
            _step(
                "build", {"action": "compile", "working_directory": root}, call_id=f"call_{index}"
            )
        )

    assert [call.raw_params["working_directory"] for call in engine.executed_calls] == [
        "/a",
        "/b",
        "/c",
        "/d",
    ]


def test_no_redirect_fires_in_the_test_phase_before_the_first_consult_either():
    engine = _engine(phase="test")

    assert engine._advisor_redirect_for_call(_call("build", {"action": "test"})) is None


# --- (b) before-giving-up --------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"action": "blocked", "outcome": "failed"},
        {"action": "done", "outcome": "failed"},
    ],
)
def test_closing_a_phase_after_a_failure_is_redirected(params):
    engine = _engine(phase="build", outcome="failed")
    # A real failed execution arms the rule.
    engine.consult_advisor()
    engine._execute_action_step(_step("build", {"action": "compile"}))
    assert engine._had_failure_since_consult is True

    engine._execute_action_step(_step("phase", params, call_id="call_2"))

    assert [call.name for call in engine.executed_calls] == ["build"]
    observation = _observations(engine)[1].content
    assert "A failure occurred since your last advisor consult" in observation
    assert "This claim was not evaluated." in observation
    assert (
        engine.emitted_tool_results[1]["result"].metadata["advisor_redirect"] == "before-giving-up"
    )


def test_a_successful_phase_done_is_never_redirected():
    engine = _engine(phase="build", outcome="failed")
    engine.consult_advisor()
    engine._execute_action_step(_step("build", {"action": "compile"}))

    engine._execute_action_step(
        _step("phase", {"action": "done", "outcome": "success"}, call_id="call_2")
    )

    assert [call.name for call in engine.executed_calls] == ["build", "phase"]


def test_closing_a_phase_passes_once_the_advisor_has_been_consulted():
    engine = _engine(phase="build", outcome="failed")
    engine.consult_advisor()
    engine._execute_action_step(_step("build", {"action": "compile"}))
    engine.consult_advisor()

    engine._execute_action_step(
        _step("phase", {"action": "blocked", "outcome": "failed"}, call_id="call_2")
    )

    assert [call.name for call in engine.executed_calls] == ["build", "phase"]


# --- (c) when-stuck --------------------------------------------------------


def test_an_armed_recurrence_redirects_the_next_state_changing_call():
    engine = _engine(phase="analyze", loop_decision=_loop_decision("guide", recurrence_count=2))
    engine.consult_advisor()  # clear rule 1 so only the recurrence rule can fire

    engine._execute_action_step(_step("bash", {"command": "make install"}))
    assert engine._advisor_redirect_armed is True

    engine._execute_action_step(_step("bash", {"command": "make install"}, call_id="call_2"))

    assert [call.name for call in engine.executed_calls] == ["bash"]
    observation = _observations(engine)[1].content
    assert "You are repeating an action that has already failed without progress" in observation
    assert "This call was not executed." in observation
    assert engine.emitted_tool_results[1]["result"].metadata["advisor_redirect"] == "when-stuck"


def test_a_single_occurrence_does_not_arm_the_recurrence_rule():
    engine = _engine(phase="analyze", loop_decision=_loop_decision("continue", recurrence_count=1))
    engine.consult_advisor()

    engine._execute_action_step(_step("bash", {"command": "make install"}))

    assert engine._advisor_redirect_armed is False


def test_consulting_the_advisor_disarms_the_recurrence_rule():
    engine = _engine(phase="analyze", loop_decision=_loop_decision("guide", recurrence_count=3))
    engine.consult_advisor()
    engine._execute_action_step(_step("bash", {"command": "make install"}))

    engine.consult_advisor()

    assert engine._advisor_redirect_armed is False
    engine._execute_action_step(_step("bash", {"command": "make install"}, call_id="call_2"))
    assert [call.name for call in engine.executed_calls] == ["bash", "bash"]


# --- (d)/(e) inertness: the advisor never dead-locks a run -----------------


def test_advisor_mode_off_makes_every_rule_inert():
    engine = _engine(phase="build", advisor_mode="off", outcome="failed")
    engine._had_failure_since_consult = True
    engine._advisor_redirect_armed = True

    assert (
        engine._advisor_redirect_for_call(
            _call("phase", {"action": "blocked", "outcome": "failed"})
        )
        is None
    )
    assert engine._advisor_redirect_for_call(_call("bash", {"command": "make install"})) is None
    # Guarantee 1 under the ablation: no entry consult, no provider call.
    assert engine._maybe_consult_advisor_at_phase_entry() is False
    assert engine.llm_client.calls == []


def test_an_exhausted_phase_cap_makes_every_rule_inert():
    engine = _engine(phase="build", advisor_phase_cap=1)
    engine.consult_advisor()
    assert engine.consult_advisor().metadata["advisor"] == "cap"
    engine._had_failure_since_consult = True
    engine._advisor_redirect_armed = True

    assert (
        engine._advisor_redirect_for_call(
            _call("phase", {"action": "blocked", "outcome": "failed"})
        )
        is None
    )
    assert engine._advisor_redirect_for_call(_call("bash", {"command": "make install"})) is None
    assert engine._maybe_consult_advisor_at_phase_entry() is False


def test_a_redirect_preempts_the_closed_evidence_refusal():
    """Rule ordering: a redirected call must not be described to the model in
    closed-evidence refusal wording — it was stopped for a different reason and
    has a different next action.

    (Audit 2026-07-26: this used to be demonstrated with the deleted
    before-acting rule; when-stuck is the surviving state-changing redirect.)"""
    engine = _engine(phase="build", loop_decision=_loop_decision("guide", recurrence_count=2))
    engine.consult_advisor()
    engine._execute_action_step(_step("build", {"action": "compile"}))
    assert engine._advisor_redirect_armed is True
    engine.run_evidence_state = SimpleNamespace(sealed=True)

    redirect = engine._advisor_redirect_for_call(_call("build", {"action": "compile"}))
    assert redirect.metadata["advisor_redirect"] == "when-stuck"

    engine._execute_action_step(_step("build", {"action": "compile"}, call_id="call_2"))
    observation = _observations(engine)[1].content
    assert "Consult advisor()" in observation
    assert "sealed" not in observation


def test_the_advisor_is_never_refused_even_after_evidence_seals():
    """Otherwise the before-giving-up guarantee would strand the run: the
    redirect demands a consult the harness would then refuse."""
    engine = _engine(phase="report")
    engine.run_evidence_state = SimpleNamespace(sealed=True)

    assert engine._refusal_for_call(_call("advisor")) is None
    assert engine._refusal_for_call(_call("build", {"action": "compile"})) is not None


def test_the_advisor_tool_itself_is_never_redirected():
    engine = _engine(phase="build")
    engine._had_failure_since_consult = True
    engine._advisor_redirect_armed = True

    assert engine._advisor_redirect_for_call(_call("advisor")) is None


# --- (f)/(g) what counts as a state-changing action ------------------------


@pytest.mark.parametrize(
    "name,params,state_changing",
    [
        ("bash", {"command": "ls -la /workspace"}, False),
        ("bash", {"command": "grep -r pom ."}, False),
        ("bash", {"command": "rm -rf /workspace/target"}, True),
        ("bash", {"command": ""}, True),
        ("project", {"action": "analyze"}, False),
        ("project", {"action": "clone"}, True),
        ("file_io", {"action": "read", "file_path": "/p"}, False),
        ("file_io", {"action": "list", "file_path": "/p"}, False),
        ("file_io", {"action": "write", "file_path": "/p"}, True),
        ("search", {"target": "web:maven"}, False),
        ("phase", {"action": "note", "text": "x"}, False),
        ("report", {}, False),
        ("advisor", {}, False),
        ("build", {"action": "compile"}, True),
    ],
)
def test_state_changing_classification(name, params, state_changing):
    engine = _engine(phase="build")

    assert engine._is_state_changing(name, params) is state_changing


def test_read_only_reconnaissance_is_never_redirected_by_the_recurrence_rule():
    """(Audit 2026-07-26: the classification used to be exercised through
    before-acting; when-stuck is the surviving rule that reads it.)"""
    engine = _engine(phase="build")
    engine.consult_advisor()
    engine._advisor_redirect_armed = True

    assert engine._advisor_redirect_for_call(_call("bash", {"command": "ls -la"})) is None
    assert engine._advisor_redirect_for_call(_call("project", {"action": "analyze"})) is None
    assert engine._advisor_redirect_for_call(_call("file_io", {"action": "write"})) is not None
    assert (
        engine._advisor_redirect_for_call(_call("bash", {"command": "rm -rf /workspace"}))
        is not None
    )


# --- timing surfaces -------------------------------------------------------


def test_the_system_prompt_teaches_when_to_consult():
    guidance = load_react_engine_prompts().get("initial_system.advisor_guidance")

    assert "advisor()" in guidance
    for cue in ("before substantive work", "when stuck", "before closing a phase"):
        assert cue in guidance, f"the timing block must name the '{cue}' trigger"
    assert "adapt" in guidance


def test_the_advisor_guidance_key_is_required():
    from sag.config.prompt_loader import REACT_ENGINE_REQUIRED_PROMPT_KEYS

    assert "initial_system.advisor_guidance" in REACT_ENGINE_REQUIRED_PROMPT_KEYS
