# tests/test_advisor_engine_flow.py
"""The advisor guarantees drive a real engine run (spec §3.2, §3.7.6).

A real `ReActEngine` runs `run_setup_loop` against a scripted native LLM and a
scripted advisor client: real phase machine, real transition policy, real
dispatcher, real renderer, real redirects. The script is the failure shape the
guarantees exist for — act before thinking, then give up after one failure —
and the assertions are that the harness redirected exactly twice, that the run
still reached COMPLETED, and that the pairing invariant never needed repair.

The second test is the ablation demonstration: the identical script under
`advisor_mode="off"` produces zero redirects and zero advisor provider calls,
and still completes. That is what makes the advisor's effect measurable
against the Plan-2 churn baseline instead of merely asserted.
"""

from types import SimpleNamespace

import pytest
from test_verdict_finalizer import FakeVerdictOrchestrator

import sag.agent.native_messages as native_messages
from sag.agent.advisor import AdvisorTool
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.output_storage import OutputStorageManager
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.verdict_finalizer import RunTerminationStatus, VerdictFinalizer
from sag.config.prompt_loader import load_react_engine_prompts
from sag.tools.base import BaseTool, ToolResult

_PHASE_FACTS = {
    "provision": {"provision.workspace_ready": True},
    "analyze": {"analysis.build_entry_ready": True},
    "build": {"build.test_entry_ready": True},
}

ADVICE = "The provider is below the declared floor; install it, then retry the root install."

_VALIDATOR_STATES = {
    PhaseOutcome.SUCCESS: ValidatorState.GREEN,
    PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
    PhaseOutcome.FAILED: ValidatorState.RED,
    PhaseOutcome.UNKNOWN: ValidatorState.UNAVAILABLE,
}


class _PhaseTool(BaseTool):
    """A phase tool that always accepts, so the loop (not the gate) is on trial."""

    def __init__(self, machine: PhaseMachine):
        super().__init__("phase", "Signal phase lifecycle transitions")
        self.machine = machine

    def execute(
        self,
        action: str,
        outcome: str = "success",
        key_results: str = "",
        reason: str = "",
    ) -> ToolResult:
        phase = self.machine.current_phase
        claimed = PhaseOutcome(outcome)
        claim = PhaseClaim(
            phase=phase,
            signal=action,
            claimed_outcome=claimed,
            key_results=key_results or f"{phase} finished",
            reason=reason,
        )
        gate = GateResult(
            accepted=True,
            validated_outcome=claimed,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=_VALIDATOR_STATES[claimed],
            reason="scripted gate",
            validated_facts=(
                {} if claimed is PhaseOutcome.FAILED else dict(_PHASE_FACTS.get(phase, {}))
            ),
            claim=claim,
        )
        return ToolResult.completed_success(
            output=f"phase {phase} {action}",
            metadata={
                "phase_signal": action,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        )


class _BuildTool(BaseTool):
    """Compiles once, and always fails: the script needs a real failure so the
    before-giving-up guarantee has something to guard."""

    def __init__(self):
        super().__init__("build", "Compile and test the project")
        self.calls = []

    def execute(self, action: str = "compile", working_directory: str = "") -> ToolResult:
        # `working_directory` is injected by the orchestrator's parameter
        # self-healing; it has to be declared or the call fails validation.
        self.calls.append(action)
        return ToolResult.completed_failure(
            output="BUILD FAILURE: no matching distribution for the local provider",
            error="build failed",
            error_code="BUILD_FAILED",
        )


class _PromptBuilder:
    def invalidate_trunk_cache(self):
        pass

    def build_initial_system_prompt(self, **kwargs):
        return "SYSTEM PROMPT"


class _ScriptedClient:
    """One prepared executor turn per iteration; fixed advice per consult."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.advisor_calls = []

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=True, model="scripted-model")

    def get_native_turn(self, messages, *, include_tools=True):
        self.requests.append(list(messages))
        if not self.turns:
            raise AssertionError("the scripted client ran out of turns")
        return self.turns.pop(0)

    def get_advisor_response(self, messages, *, model, max_tokens):
        self.advisor_calls.append({"model": model, "max_tokens": max_tokens})
        return ADVICE


class _Journal:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def _turn(index, name, arguments, text="working"):
    return NativeTurn(
        text=text,
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name=name,
                arguments=dict(arguments),
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )


def _script():
    """Act-before-thinking, then give-up-after-one-failure."""
    return [
        _turn(1, "phase", {"action": "done", "outcome": "success"}),  # provision
        _turn(2, "phase", {"action": "done", "outcome": "success"}),  # analyze
        _turn(3, "build", {"action": "compile"}),  # -> before-acting redirect
        _turn(4, "advisor", {}),
        _turn(5, "build", {"action": "compile"}),  # executes, fails
        _turn(6, "phase", {"action": "blocked", "outcome": "failed", "reason": "provider"}),
        _turn(7, "advisor", {}),
        _turn(8, "phase", {"action": "blocked", "outcome": "failed", "reason": "provider"}),
        # Padding: whatever phase the policy routes to closes honestly.
        _turn(9, "phase", {"action": "done", "outcome": "partial"}),
        _turn(10, "phase", {"action": "done", "outcome": "partial"}),
    ]


def _engine(tmp_path, *, advisor_mode="same-model", max_iterations=20):
    machine = PhaseMachine()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = machine
    engine.max_iterations = max_iterations
    engine.run_evidence_state = RunEvidenceState(run_id="advisor-flow")
    engine.verdict_finalizer = VerdictFinalizer(FakeVerdictOrchestrator())
    engine.transition_policy = PhaseTransitionPolicy()
    engine._repair_global_remaining = 2
    engine._repair_phase_remaining = {"build": 1, "test": 1}
    engine._report_attempted = False
    engine._report_delivered = False
    engine._report_failed = False
    engine.config = SimpleNamespace(
        native_executor_loop=True,
        max_wall_clock_seconds=0,
        verbose=False,
        max_iterations=max_iterations,
        phase_min_floors={"analyze": 1, "build": 1, "test": 1, "report": 1},
        advisor_mode=advisor_mode,
        advisor_phase_cap=4,
        advisor_max_tokens=2048,
        phase_handoff_char_budget=6000,
    )
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine.prompts = load_react_engine_prompts()
    engine.prompt_builder = _PromptBuilder()
    engine.phase_handoff = None
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _ScriptedClient(_script())
    engine.token_tracker = SimpleNamespace(
        set_iteration=lambda iteration: None,
        update_last_tool_name=lambda tool_name: None,
    )
    engine.context_manager = SimpleNamespace(
        current_task_id=None,
        update_task_status=lambda *a, **k: True,
    )
    engine.context_journal = _Journal()
    engine.control_event_sink = None
    engine.loop_memory = None
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.orchestrator = None
    engine.successful_states = {}
    engine.recent_tool_executions = []
    engine.steps_since_context_switch = 0

    advisor_tool = AdvisorTool()
    advisor_tool.consult_fn = engine.consult_advisor
    engine.tools = {
        "phase": _PhaseTool(machine),
        "build": _BuildTool(),
        "advisor": advisor_tool,
    }
    engine.emit = lambda *a, **k: None
    engine._get_timestamp = lambda: "2026-07-26T00:00:00Z"
    engine._phase_intro_step = lambda: ReActStep(
        step_type=StepType.SYSTEM_GUIDANCE,
        content=f"=== PHASE: {machine.current_phase.upper()} ===",
        timestamp="2026-07-26T00:00:00Z",
    )
    engine._start_phase_branch = lambda: None
    engine._enforce_phase_floors = lambda: False
    engine._export_token_usage_csv = lambda: None
    engine.physical_validator = SimpleNamespace(
        validate_build_artifacts=lambda project_name=None: {},
    )
    # Forced test attempts have their own suite; without a surveyable
    # orchestrator the policy would demand a project refresh in the test phase
    # and this script is about the advisor, not receipts.
    engine._missing_required_test_attempt = lambda: None
    engine._reset_advisor_run_state()

    orchestrator = ToolOrchestrator(
        tools=engine.tools,
        context_manager=engine.context_manager,
        recent_tool_executions=engine.recent_tool_executions,
        successful_states=engine.successful_states,
        repository_url=engine.repository_url,
        track_tool_execution=lambda *a, **k: None,
        update_successful_states=lambda *a, **k: None,
        add_system_guidance=lambda *a, **k: None,
        get_timestamp=lambda: "2026-07-26T00:00:00Z",
        output_storage=engine.output_storage,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    return engine


@pytest.fixture
def pairing_spy(monkeypatch):
    """Record (in, out) message counts for every EXECUTOR renderer repair pass.

    The advisor consult renders the same window with an empty system prompt to
    flatten it, and it does so mid-batch — its own tool_use is still in flight,
    so that render legitimately synthesizes one cancellation. Only the
    executor's requests (the ones the provider actually sees) carry the
    invariant, and they are the ones keyed by the real system prompt."""
    sizes = []
    original = native_messages._repair_pairing

    def spy(messages):
        repaired = original(messages)
        if messages and str(messages[0].get("content") or "").startswith("SYSTEM PROMPT"):
            sizes.append((len(messages), len(repaired)))
        return repaired

    monkeypatch.setattr(native_messages, "_repair_pairing", spy)
    return sizes


def _assert_pairing(messages):
    """Every emitted tool_call is answered by the message right after it."""
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        for call in message.get("tool_calls") or ():
            reply = messages[index]
            assert reply["role"] == "tool", f"call {call['id']} is unanswered"
            assert reply["tool_call_id"] == call["id"]
            index += 1


def _redirect_spy(engine):
    """Record the rule of every redirect the engine issues, in order."""
    rules = []
    original = engine._advisor_redirect_for_call

    def spy(call):
        execution = original(call)
        if execution is not None:
            rules.append(execution.metadata["advisor_redirect"])
        return execution

    engine._advisor_redirect_for_call = spy
    return rules


def test_the_guarantees_redirect_twice_and_the_run_still_completes(tmp_path, pairing_spy):
    engine = _engine(tmp_path)
    rules = _redirect_spy(engine)

    termination = engine.run_setup_loop("set up the project", max_iterations=20)

    # Exactly the two guarantees the script trips, in order.
    assert rules == ["before-acting", "before-giving-up"]
    # The redirected build never ran; the post-consult retry did.
    assert engine.tools["build"].calls == ["compile"]

    telemetry = engine.advisor_telemetry
    assert telemetry["mode"] == "same-model"
    assert [call["phase"] for call in telemetry["calls"]] == ["build", "build"]
    assert [call["outcome"] for call in telemetry["calls"]] == ["advice", "advice"]
    assert all(call["advice_chars"] == len(ADVICE) for call in telemetry["calls"])
    assert [call["max_tokens"] for call in engine.llm_client.advisor_calls] == [2048, 2048]

    # The redirects flow through the ordinary evidence-recording path, so the
    # dispatcher paired everything and the renderer never had to repair.
    assert pairing_spy, "the renderer must have run"
    assert all(before == after for before, after in pairing_spy)
    for request in engine.llm_client.requests:
        _assert_pairing(request)

    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete


def test_the_redirects_reach_the_model_as_tool_results(tmp_path):
    engine = _engine(tmp_path)

    engine.run_setup_loop("set up the project", max_iterations=20)

    tool_messages = [
        message["content"]
        for request in engine.llm_client.requests
        for message in request
        if message["role"] == "tool"
    ]
    assert any("Consult advisor() before this phase's first" in text for text in tool_messages)
    assert any(
        "A failure occurred since your last advisor consult" in text for text in tool_messages
    )
    # ...and so does the advice itself.
    assert any(ADVICE in text for text in tool_messages)


def test_advisor_mode_off_removes_every_redirect_and_the_run_still_completes(tmp_path):
    """The §3.7.6 ablation: same script, no advisor, no dead-lock."""
    engine = _engine(tmp_path, advisor_mode="off")
    rules = _redirect_spy(engine)

    termination = engine.run_setup_loop("set up the project", max_iterations=20)

    assert rules == []
    assert engine.llm_client.advisor_calls == [], "mode 'off' must consult no provider"
    assert engine.advisor_telemetry == {"mode": "off", "calls": []}
    # The unredirected script reaches the tool one call earlier and burns the
    # ceremony the guarantees would have converted into a consult.
    assert engine.tools["build"].calls == ["compile", "compile"]
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete
