# tests/test_native_loop_engine.py
"""End-to-end proof of the flag-gated native executor loop (Plan 2 Task 6).

A real `ReActEngine` runs `run_setup_loop` against a scripted native LLM: one
`NativeTurn` per iteration, real phase machine, real transition policy, real
dispatcher, real renderer. The point is that the loop drives provision →
report to COMPLETED while the pairing invariant holds on EVERY rendered
request — an unanswered assistant tool_use is a provider 400 (anatomy map
risk 5), so the renderer's repair pass must never have anything to do."""

from types import SimpleNamespace

import pytest
from test_verdict_finalizer import FakeVerdictOrchestrator

import sag.agent.native_messages as native_messages
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.native_messages import render_messages
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.verdict_finalizer import RunTerminationStatus, VerdictFinalizer
from sag.tools.base import BaseTool, ToolResult

_PHASE_FACTS = {
    "provision": {"provision.workspace_ready": True},
    "analyze": {"analysis.build_entry_ready": True},
    "build": {"build.test_entry_ready": True},
}


class _PhaseTool(BaseTool):
    """A phase tool that always accepts, so the loop (not the gate) is on trial."""

    def __init__(self, machine: PhaseMachine):
        super().__init__("phase", "Signal phase lifecycle transitions")
        self.machine = machine

    def execute(self, action: str, outcome: str = "success", key_results: str = "") -> ToolResult:
        phase = self.machine.current_phase
        claimed = PhaseOutcome(outcome)
        claim = PhaseClaim(
            phase=phase,
            signal=action,
            claimed_outcome=claimed,
            key_results=key_results or f"{phase} finished",
        )
        gate = GateResult(
            accepted=True,
            validated_outcome=claimed,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=ValidatorState.GREEN,
            reason="scripted gate",
            validated_facts=dict(_PHASE_FACTS.get(phase, {})),
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


class _PromptBuilder:
    def invalidate_trunk_cache(self):
        pass

    def build_initial_system_prompt(self, **kwargs):
        return "SYSTEM PROMPT"


class _ScriptedNativeClient:
    """Hands back one prepared turn per iteration and records what it saw."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=True, model="scripted-model")

    def get_native_turn(self, messages, *, include_tools=True):
        self.requests.append(list(messages))
        if not self.turns:
            raise AssertionError("the scripted client ran out of turns")
        return self.turns.pop(0)


class _Journal:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def _phase_turn(index, action="done", outcome="success"):
    return NativeTurn(
        text=f"Closing phase {index}.",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name="phase",
                arguments={"action": action, "outcome": outcome},
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )


def _engine(turns, *, max_iterations=12):
    machine = PhaseMachine()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = machine
    engine.max_iterations = max_iterations
    engine.run_evidence_state = RunEvidenceState(run_id="native-loop")
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
    )
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine.prompt_builder = _PromptBuilder()
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _ScriptedNativeClient(turns)
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
    engine.output_storage = None
    engine.orchestrator = None
    engine.successful_states = {}
    engine.recent_tool_executions = []
    engine.steps_since_context_switch = 0
    engine.tools = {"phase": _PhaseTool(machine)}
    engine.emit = lambda *a, **k: None
    engine._get_timestamp = lambda: "2026-07-26T00:00:00Z"
    # The phase digest is exercised by its own suite; here it only has to be a
    # deterministic SYSTEM_GUIDANCE step so the window opens with a user turn.
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
    # Forced test attempts have their own suite (test_forced_attempt_native.py);
    # without a surveyable orchestrator the policy would demand a project
    # refresh in the test phase and this script is about the loop, not receipts.
    engine._missing_required_test_attempt = lambda: None

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
        output_storage=None,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    return engine


@pytest.fixture
def pairing_spy(monkeypatch):
    """Record (in, out) message counts for every renderer repair pass."""
    sizes = []
    original = native_messages._repair_pairing

    def spy(messages):
        repaired = original(messages)
        sizes.append((len(messages), len(repaired)))
        return repaired

    monkeypatch.setattr(native_messages, "_repair_pairing", spy)
    return sizes


def test_native_loop_drives_the_phase_machine_to_completion(pairing_spy):
    engine = _engine([_phase_turn(index) for index in range(1, 6)])
    executed = []
    dispatch = engine._execute_native_calls

    def spy(turn):
        steps = dispatch(turn)
        executed.extend(steps)
        return steps

    engine._execute_native_calls = spy

    termination = engine.run_setup_loop("set up the project", max_iterations=12)

    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete
    # One LLM call per iteration, one phase closed per call.
    assert engine.current_iteration == 5
    assert len(engine.llm_client.requests) == 5

    # Every dispatched call keeps its provider id, so every observation can
    # answer it by id rather than by position.
    assert [step.tool_call_id for step in executed] == [f"call_{i}" for i in range(1, 6)]
    assert all(step.step_type == StepType.ACTION for step in executed)

    # The dispatcher paired everything: the renderer's repair pass never had to
    # add a cancellation or drop an orphan, on any request of the run.
    assert pairing_spy, "the renderer must have run"
    assert all(before == after for before, after in pairing_spy)

    # One journal line per iteration that did not end the run. The label is the
    # phase current when the line is written, so a closing iteration is
    # journaled under the phase it just opened (unchanged from the old loop).
    assert [record["phase"] for record in engine.context_journal.records] == [
        "analyze",
        "build",
        "test",
        "report",
    ]
    assert [record["iteration"] for record in engine.context_journal.records] == [1, 2, 3, 4]
    assert all(record["total_chars"] > 0 for record in engine.context_journal.records)


def test_every_request_carries_the_system_prompt_and_a_paired_history():
    engine = _engine([_phase_turn(index) for index in range(1, 6)])

    engine.run_setup_loop("set up the project", max_iterations=12)

    for messages in engine.llm_client.requests:
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].startswith("SYSTEM PROMPT")
        # The kickoff instruction rides the system message on EVERY request;
        # the old loop rendered it once and then overwrote it.
        assert "set up the project" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        index = 1
        while index < len(messages):
            message = messages[index]
            index += 1
            for call in message.get("tool_calls") or ():
                reply = messages[index]
                assert reply["role"] == "tool"
                assert reply["tool_call_id"] == call["id"]
                index += 1


def test_final_window_renders_without_pairing_repair():
    engine = _engine([_phase_turn(index) for index in range(1, 6)])

    engine.run_setup_loop("set up the project", max_iterations=12)

    messages = render_messages("SYSTEM PROMPT", engine.steps)
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[2]["tool_calls"][0]["id"] == "call_5"
    assert messages[3]["tool_call_id"] == "call_5"


def test_flag_off_keeps_the_old_protocol_in_charge():
    """The native loop is a strangler behind `native_executor_loop`."""
    engine = _engine([_phase_turn(1)])
    engine.config.native_executor_loop = False
    engine._run_native_loop = lambda *a, **k: pytest.fail("the flag must gate the native loop")
    engine.state_evaluator = SimpleNamespace(completion_mode="setup")
    engine.reasoning_scheduler = None
    engine.prompt_builder.build_mode_prompt = lambda prompt, mode, **kwargs: prompt
    engine.llm_client.get_response = lambda prompt, mode: ""
    engine._should_use_thinking_model = lambda: True

    termination = engine.run_setup_loop("set up the project", max_iterations=1)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.llm_client.requests == [], "no native request may be issued"


def test_toolless_turn_injects_the_continuation_cue_and_the_loop_proceeds():
    toolless = NativeTurn(
        text="I should think about this some more.",
        tool_calls=(),
        model_used="scripted-model",
    )
    engine = _engine([toolless, *[_phase_turn(index) for index in range(1, 6)]])

    termination = engine.run_setup_loop("set up the project", max_iterations=12)

    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.current_iteration == 6

    # The prose and the cue both landed in the window the SECOND request saw.
    second_request = engine.llm_client.requests[1]
    assistant_texts = [m["content"] for m in second_request if m["role"] == "assistant"]
    assert "I should think about this some more." in assistant_texts
    cues = [m["content"] for m in second_request if m["role"] == "user"]
    assert any("No tool was called." in cue for cue in cues)
