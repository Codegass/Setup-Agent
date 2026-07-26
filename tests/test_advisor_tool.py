# tests/test_advisor_tool.py
"""The advisor is a no-parameter client tool whose server side is the harness.

`AdvisorTool.execute` delegates to `ReActEngine.consult_advisor`, which
assembles the full phase transcript plus a deterministic evidence digest and
consults a fresh-context LLM under a hard output cap (spec §3.2).

The load-bearing property of every test here is that the advisor NEVER blocks
a run: the ablation switch, the per-phase cap and a provider outage all
degrade to a SUCCESS-shaped "proceed with your best judgment" result, never to
an exception and never to a refusal the executor has to route around.
"""

from types import SimpleNamespace

import pytest

from sag.agent.advisor import AdvisorTool
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType
from sag.config.prompt_loader import load_react_engine_prompts


class _ScriptedAdvisorClient:
    """Records every consult request; replays scripted advice or raises."""

    def __init__(self, advice="Run the failing module alone next.", error=None):
        self.advice = advice
        self.error = error
        self.calls = []

    def capabilities_for(self, mode):
        return SimpleNamespace(model="scripted-action-model")

    def get_advisor_response(self, messages, *, model, max_tokens):
        self.calls.append({"messages": list(messages), "model": model, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return self.advice


class _Handoff:
    def __init__(self, text="FACTS: build.entry=pom.xml"):
        self.text = text
        self.requests = []

    def project_for(self, target_phase, *, char_budget):
        self.requests.append((target_phase, char_budget))
        return SimpleNamespace(to_prompt_text=lambda: self.text)


def _advisor_engine(
    *,
    advisor_mode="same-model",
    advisor_phase_cap=4,
    advisor_max_tokens=2048,
    client=None,
    steps=None,
    phase="build",
    handoff=None,
):
    engine = ReActEngine.__new__(ReActEngine)
    engine.config = SimpleNamespace(
        advisor_mode=advisor_mode,
        advisor_phase_cap=advisor_phase_cap,
        advisor_max_tokens=advisor_max_tokens,
        phase_handoff_char_budget=6000,
        verbose=False,
    )
    engine.prompts = load_react_engine_prompts()
    engine.llm_client = client if client is not None else _ScriptedAdvisorClient()
    engine.steps = list(steps or [])
    engine.current_iteration = 7
    engine.phase_machine = SimpleNamespace(current_phase=phase)
    engine.phase_handoff = handoff
    engine.run_evidence_state = None
    engine.physical_validator = None
    engine.loop_memory = None
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine._reset_advisor_run_state()
    return engine


def _tool_call_steps():
    return [
        ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content="=== PHASE: BUILD ===",
            timestamp="t0",
        ),
        ReActStep(
            step_type=StepType.ACTION,
            content="build",
            tool_name="build",
            tool_params={"action": "compile"},
            timestamp="t1",
            tool_call_id="call_1",
            native_text="Compiling now.",
        ),
        ReActStep(
            step_type=StepType.OBSERVATION,
            content="[build] BUILD FAILURE: missing dependency",
            timestamp="t2",
            tool_call_id="call_1",
        ),
    ]


# --- (a) an unwired tool is an honest wiring failure, not a crash -----------


def test_unbound_advisor_tool_reports_that_it_is_not_wired():
    tool = AdvisorTool()

    result = tool.execute()

    assert not result.succeeded
    assert "advisor not wired" in result.output.lower()


def test_bound_advisor_tool_delegates_to_the_engine_consult():
    engine = _advisor_engine()
    tool = AdvisorTool()
    tool.consult_fn = engine.consult_advisor

    result = tool.execute()

    assert result.succeeded
    assert result.metadata["advisor"] == "advice"


def test_advisor_tool_takes_no_parameters():
    schema = AdvisorTool().get_parameter_schema()

    assert schema == {"type": "object", "properties": {}, "additionalProperties": False}


# --- (b) the ablation switch: mode "off" consults nothing -------------------


def test_advisor_mode_off_returns_a_success_shaped_disabled_result():
    client = _ScriptedAdvisorClient()
    engine = _advisor_engine(advisor_mode="off", client=client)

    result = engine.consult_advisor()

    assert result.succeeded
    assert "advisor is disabled for this run" in result.output
    assert "proceed with your best judgment" in result.output
    assert result.metadata["advisor"] == "off"
    assert client.calls == []
    # A disabled consult is not a call: it must not consume the phase cap.
    assert engine.advisor_telemetry["calls"] == []
    assert engine.advisor_telemetry["mode"] == "off"


# --- (c) the per-phase cap degrades, it does not refuse ---------------------


def test_advisor_phase_cap_degrades_to_a_success_shaped_result():
    client = _ScriptedAdvisorClient()
    engine = _advisor_engine(advisor_phase_cap=1, client=client)

    first = engine.consult_advisor()
    second = engine.consult_advisor()

    assert first.metadata["advisor"] == "advice"
    assert second.succeeded
    assert "advisor cap reached for this phase" in second.output
    assert "proceed with your best judgment" in second.output
    assert second.metadata["advisor"] == "cap"
    assert len(client.calls) == 1
    assert [call["outcome"] for call in engine.advisor_telemetry["calls"]] == ["advice"]


def test_advisor_cap_resets_when_the_phase_changes():
    client = _ScriptedAdvisorClient()
    engine = _advisor_engine(advisor_phase_cap=1, client=client)

    engine.consult_advisor()
    assert engine.consult_advisor().metadata["advisor"] == "cap"

    engine._reset_advisor_phase_state()

    assert engine.consult_advisor().metadata["advisor"] == "advice"
    assert len(client.calls) == 2


# --- (d) a provider outage degrades to Plan-2 behavior ----------------------


def test_provider_error_degrades_to_a_success_shaped_unavailable_result():
    client = _ScriptedAdvisorClient(error=RuntimeError("gateway timeout"))
    engine = _advisor_engine(client=client)

    result = engine.consult_advisor()

    assert result.succeeded
    assert "advisor unavailable" in result.output
    assert "proceed with your best judgment" in result.output
    assert result.metadata["advisor"] == "error"
    telemetry = engine.advisor_telemetry["calls"]
    assert [call["outcome"] for call in telemetry] == ["error"]
    assert telemetry[0]["phase"] == "build"
    # An errored consult still counts as a consult: the run must be able to
    # move on when the advisor is down.
    assert engine._had_failure_since_consult is False


# --- (e) the happy path ----------------------------------------------------


def test_successful_consult_returns_the_advice_and_records_telemetry():
    client = _ScriptedAdvisorClient(advice="Install the provider first, then retry.")
    engine = _advisor_engine(client=client)
    engine._had_failure_since_consult = True
    engine._advisor_redirect_armed = True

    result = engine.consult_advisor()

    assert result.succeeded
    assert result.output == "Install the provider first, then retry."
    assert result.metadata["advisor"] == "advice"
    assert result.metadata["advisor_call_index"] == 1
    assert result.metadata["advisor_model"] == "scripted-action-model"

    telemetry = engine.advisor_telemetry
    assert telemetry["mode"] == "same-model"
    assert telemetry["calls"] == [
        {
            "iteration": 7,
            "phase": "build",
            "advice_chars": len("Install the provider first, then retry."),
            "outcome": "advice",
        }
    ]
    # The consult clears the two Task-5 state bits (the plan's
    # `_advisor_consulted_since_failure` is expressed as this bit being False).
    assert engine._had_failure_since_consult is False
    assert engine._advisor_redirect_armed is False

    # The hard output cap and the resolved model both reach the provider.
    assert client.calls[0]["max_tokens"] == 2048
    assert client.calls[0]["model"] == "scripted-action-model"


def test_explicit_advisor_model_overrides_the_action_model():
    client = _ScriptedAdvisorClient()
    engine = _advisor_engine(advisor_mode="anthropic/claude-fable-5", client=client)

    result = engine.consult_advisor()

    assert client.calls[0]["model"] == "anthropic/claude-fable-5"
    assert result.metadata["advisor_model"] == "anthropic/claude-fable-5"


# --- (f) message assembly --------------------------------------------------


def test_consult_messages_carry_the_flattened_transcript_and_the_digest():
    client = _ScriptedAdvisorClient()
    handoff = _Handoff(text="FACTS: build.entry=pom.xml")
    engine = _advisor_engine(client=client, steps=_tool_call_steps(), handoff=handoff)

    engine.consult_advisor()

    messages = client.calls[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system_text = messages[0]["content"]
    assert "senior reviewer" in system_text
    assert "Never advise giving up while a mechanical repair is untried." in system_text

    user_text = messages[1]["content"]
    # The whole phase transcript is forwarded, flattened role by role.
    assert "USER: === PHASE: BUILD ===" in user_text
    assert "ASSISTANT: Compiling now." in user_text
    assert "TOOL: [build] BUILD FAILURE: missing dependency" in user_text
    # ...followed by the deterministic evidence digest.
    assert "EVIDENCE DIGEST" in user_text
    assert "FACTS: build.entry=pom.xml" in user_text
    assert handoff.requests == [("build", 6000)]


def test_consult_survives_a_broken_evidence_digest():
    class _ExplodingHandoff:
        def project_for(self, target_phase, *, char_budget):
            raise RuntimeError("handoff storage is unavailable")

    client = _ScriptedAdvisorClient()
    engine = _advisor_engine(client=client, steps=_tool_call_steps(), handoff=_ExplodingHandoff())

    result = engine.consult_advisor()

    assert result.metadata["advisor"] == "advice"
    assert (
        "TOOL: [build] BUILD FAILURE: missing dependency"
        in client.calls[0]["messages"][1]["content"]
    )


# --- wiring: registration, consult binding, run pin ------------------------


def _registration_agent():
    from pathlib import Path

    from sag.agent.agent import SetupAgent
    from sag.agent.phase_machine import PhaseMachine

    agent = object.__new__(SetupAgent)
    agent.config = SimpleNamespace(
        workspace_path="/workspace",
        test_pass_threshold=0.95,
        build_coverage_threshold=0.75,
        test_execution_threshold=0.8,
    )
    agent.orchestrator = SimpleNamespace(
        project_name="demo",
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": ""},
    )
    agent.context_manager = SimpleNamespace(contexts_dir=Path("/workspace/.setup_agent/contexts"))
    agent.phase_machine = PhaseMachine()
    agent.context_journal = None
    agent.project_name = "demo"
    return agent


def test_the_advisor_is_registered_in_both_workflow_modes():
    # Mode "off" answers through consult_advisor, so the tool is ALWAYS
    # registered: the ablation switch must not change the tool surface.
    for mode in ("setup", "run_task"):
        agent = _registration_agent()
        if mode == "run_task":
            agent.phase_machine = None
        names = {tool.name for tool in agent._initialize_tools(workflow_mode=mode)}
        assert "advisor" in names, f"advisor missing from the {mode} tool surface"


def test_the_agent_binds_the_engine_consult_to_the_registered_tool():
    from sag.agent.agent import SetupAgent

    agent = _registration_agent()
    tools = agent._initialize_tools(workflow_mode="setup")
    agent.tools = tools
    engine = _advisor_engine()
    agent.react_engine = engine

    SetupAgent._bind_advisor_consult(agent)

    advisor = next(tool for tool in tools if tool.name == "advisor")
    assert advisor.consult_fn == engine.consult_advisor
    assert advisor.execute().metadata["advisor"] == "advice"


def test_the_run_pin_carries_advisor_telemetry(tmp_path):
    from sag.agent.agent import SetupAgent
    from sag.agent.control_events import RunPin

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent._run_pin_mirror = None
    agent._run_pin_template = {
        "container_image_digest": "sha256:" + "b" * 64,
        "sag_git_sha": "c" * 40,
        "thinking_model": "thinking-model",
        "action_model": "action-model",
        "sanitized_config": {"max_iterations": 50},
        "prompt_bundle_sha256": "d" * 64,
        "feature_flags": {"control_events": True},
        "random_seed_or_null": None,
        "run_order_index": None,
        "dependency_cache_state": "warm",
        "host_arch": "arm64",
    }
    agent.agent_logger = SimpleNamespace(warning=lambda *a, **k: None)
    engine = _advisor_engine()
    engine.consult_advisor()
    agent.react_engine = engine

    agent._write_run_pin(target_repo_sha="a" * 40)

    pin = RunPin.model_validate_json((tmp_path / "run-pin.json").read_text(encoding="utf-8"))
    assert pin.advisor == {
        "mode": "same-model",
        "calls": [
            {
                "iteration": 7,
                "phase": "build",
                "advice_chars": len("Run the failing module alone next."),
                "outcome": "advice",
            }
        ],
    }


# --- config surface --------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, ("same-model", 2048, 4)),
        (
            {
                "SAG_ADVISOR_MODE": "off",
                "SAG_ADVISOR_MAX_TOKENS": "512",
                "SAG_ADVISOR_PHASE_CAP": "2",
            },
            ("off", 512, 2),
        ),
    ],
)
def test_advisor_settings_read_from_the_environment(monkeypatch, env, expected):
    from sag.config.settings import Config

    for key in ("SAG_ADVISOR_MODE", "SAG_ADVISOR_MAX_TOKENS", "SAG_ADVISOR_PHASE_CAP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    config = Config.from_env()

    assert (config.advisor_mode, config.advisor_max_tokens, config.advisor_phase_cap) == expected


def test_advisor_defaults_are_the_spec_values():
    from sag.config.settings import Config

    config = Config()

    assert config.advisor_mode == "same-model"
    assert config.advisor_max_tokens == 2048
    assert config.advisor_phase_cap == 4
