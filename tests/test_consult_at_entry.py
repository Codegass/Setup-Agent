# tests/test_consult_at_entry.py
"""Guarantee 1 (spec §3.2) is a harness-authored consult at build/test phase
entry, NOT a redirect of the model's first state-changing call.

The 2026-07-26 post-acceptance audit falsified the before-acting redirect: in
bigtop r1 it cancelled two correctly-planned 4-island batches (8 wasted calls),
and phase re-entry reset `_advisor_calls_in_phase`, re-arming the trap. A weak
model cannot be required to re-remember a batch the harness threw away.

The replacement buys the same advice with zero cancelled work: on entering the
build or test phase the HARNESS consults the advisor itself and appends the
result to the fresh phase window as a synthetic assistant tool_call
(`advisor-entry-<n>`) plus its tool result — the forced-attempt precedent, so
the pairing invariant and the evidence trail hold without a second code path.

The advisor still never blocks a run: `advisor_mode="off"` and an exhausted
phase cap both skip the entry consult entirely.
"""

from types import SimpleNamespace

import pytest
from test_verdict_finalizer import FakeVerdictOrchestrator

import sag.agent.native_messages as native_messages
from sag.agent.advisor import AdvisorTool
from sag.agent.evidence_state import EvidenceRole, RunEvidenceState, StateScope
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

ADVICE = "Compile the three untouched islands before you conclude anything."

_PHASE_FACTS = {
    "provision": {"provision.workspace_ready": True},
    "analyze": {"analysis.build_entry_ready": True},
    "build": {"build.test_entry_ready": True},
}

_VALIDATOR_STATES = {
    PhaseOutcome.SUCCESS: ValidatorState.GREEN,
    PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
    PhaseOutcome.FAILED: ValidatorState.RED,
    PhaseOutcome.UNKNOWN: ValidatorState.UNAVAILABLE,
}


# --- unit-level engine (the same scripted shape test_advisor_guarantees uses) --


class _ScriptedAdvisorClient:
    def __init__(self, advice=ADVICE):
        self.advice = advice
        self.calls = []

    def capabilities_for(self, mode):
        return SimpleNamespace(model="scripted-action-model")

    def get_advisor_response(self, messages, *, model, max_tokens):
        self.calls.append(model)
        return self.advice


def _unit_engine(*, phase="build", advisor_mode="same-model", advisor_phase_cap=4):
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
    engine.phase_machine = SimpleNamespace(current_phase=phase, current_attempt_id="attempt-1")
    engine.phase_handoff = None
    engine.physical_validator = None
    engine.loop_memory = None
    engine.output_storage = None
    engine.control_event_sink = None
    engine.prompts = load_react_engine_prompts()
    engine.llm_client = _ScriptedAdvisorClient()
    engine.emitted_tool_results = []
    engine._get_timestamp = lambda: "2026-07-26T00:00:00Z"
    engine._record_execution_bundle = lambda execution, call: (
        execution.result,
        "execution-1",
        [],
    )
    engine._emit_control_action_envelope = lambda tool, params: "envelope-1"
    engine._emit_control_tool_result = lambda **kwargs: engine.emitted_tool_results.append(kwargs)

    def fake_add_observation_step(observation):
        step = ReActStep(
            step_type=StepType.OBSERVATION,
            content=observation,
            timestamp="2026-07-26T00:00:00Z",
        )
        engine.steps.append(step)
        return step

    engine._add_observation_step = fake_add_observation_step
    engine._reset_advisor_run_state()
    return engine


def _actions(engine):
    return [step for step in engine.steps if step.step_type == StepType.ACTION]


def _observations(engine):
    return [step for step in engine.steps if step.step_type == StepType.OBSERVATION]


# --- (a) one harness-authored pair at entry -------------------------------


def test_entering_the_build_phase_consults_the_advisor_once():
    engine = _unit_engine(phase="build")

    assert engine._maybe_consult_advisor_at_phase_entry() is True

    action = _actions(engine)[0]
    assert action.tool_name == "advisor"
    assert action.tool_call_id == "advisor-entry-1"
    assert action.native_text == "[harness] consulting the advisor at phase entry"
    assert action.model_used == "harness"
    # Pairing invariant: the observation answers exactly the id the harness
    # minted, the forced-attempt way.
    assert _observations(engine)[0].tool_call_id == "advisor-entry-1"
    assert ADVICE in _observations(engine)[0].content
    # It is a real consult: telemetry counts it and the phase cap sees it.
    assert [call["phase"] for call in engine.advisor_telemetry["calls"]] == ["build"]
    assert engine._advisor_calls_in_phase == 1


def test_the_entry_consult_covers_the_test_phase_too():
    engine = _unit_engine(phase="test")

    assert engine._maybe_consult_advisor_at_phase_entry() is True
    assert [call["phase"] for call in engine.advisor_telemetry["calls"]] == ["test"]


@pytest.mark.parametrize("phase", ["provision", "analyze", "report"])
def test_no_entry_consult_outside_build_and_test(phase):
    engine = _unit_engine(phase=phase)

    assert engine._maybe_consult_advisor_at_phase_entry() is False
    assert engine.steps == []
    assert engine.advisor_telemetry["calls"] == []


def test_the_entry_consult_happens_at_most_once_per_entry():
    engine = _unit_engine(phase="build")

    assert engine._maybe_consult_advisor_at_phase_entry() is True
    assert engine._maybe_consult_advisor_at_phase_entry() is False

    assert len(_actions(engine)) == 1


def test_a_broken_consult_never_blocks_the_entry():
    """The advisor must NEVER block a run: a harness-side failure inside the
    entry consult degrades to no consult, not to an exception."""
    engine = _unit_engine(phase="build")

    def boom(execution, call):
        raise RuntimeError("evidence sink unavailable")

    engine._record_execution_bundle = boom

    assert engine._maybe_consult_advisor_at_phase_entry() is False


# --- (c) re-entry consults again, bounded by the cap ----------------------


def test_re_entry_consults_again():
    """Phase re-entry after a repair loop gets fresh advice — the audit's
    complaint about the deleted redirect was that re-entry re-armed a TRAP,
    not that re-entry should stay silent."""
    engine = _unit_engine(phase="build")
    engine._maybe_consult_advisor_at_phase_entry()

    engine._reset_advisor_phase_state()  # what _apply_phase_decision does
    assert engine._maybe_consult_advisor_at_phase_entry() is True

    ids = [step.tool_call_id for step in _actions(engine)]
    assert ids == ["advisor-entry-1", "advisor-entry-2"]


def test_an_exhausted_cap_skips_the_entry_consult():
    engine = _unit_engine(phase="build", advisor_phase_cap=0)

    assert engine._maybe_consult_advisor_at_phase_entry() is False
    assert engine.steps == []
    assert engine.llm_client.calls == []


# --- (d) the ablation ------------------------------------------------------


def test_advisor_mode_off_performs_no_entry_consult():
    engine = _unit_engine(phase="build", advisor_mode="off")

    assert engine._maybe_consult_advisor_at_phase_entry() is False
    assert engine.steps == []
    assert engine.llm_client.calls == []
    assert engine.advisor_telemetry == {"mode": "off", "calls": []}


# --- (e) the digest names the last test attempt's collection facts ---------


def _pytest_observation_state(**metadata_overrides):
    state = RunEvidenceState(run_id="digest")
    metadata = {
        "operation": "test",
        "command": "/venv/bin/python -m pytest tests/python/all-platform-minimal-test",
        "collection_scope": "filtered",
        "collected": None,
        "collected_after_deselection": 12,
        "executed": 12,
        "collection_errors": 0,
        **metadata_overrides,
    }
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(output="12 passed", metadata=metadata),
        provenance="tool:build:1",
        roles=(EvidenceRole.TEST,),
        execution_id="exec-test-1",
        params={"action": "test"},
    )
    return state


def test_the_digest_names_the_last_test_attempt():
    engine = _unit_engine(phase="test")
    engine.run_evidence_state = _pytest_observation_state()

    digest = engine._advisor_evidence_digest()

    assert (
        "Last test attempt: /venv/bin/python -m pytest "
        "tests/python/all-platform-minimal-test — scope=filtered, collected=unknown, "
        "selected=12, executed=12, collection_errors=0"
    ) in digest


def test_the_digest_reports_a_collection_failure_honestly():
    engine = _unit_engine(phase="test")
    engine.run_evidence_state = _pytest_observation_state(
        collection_scope="full",
        collected=56,
        collected_after_deselection=56,
        executed=0,
        collection_errors=28,
    )

    digest = engine._advisor_evidence_digest()

    assert "executed=0, collection_errors=28" in digest


def test_the_digest_has_no_test_attempt_line_without_one():
    engine = _unit_engine(phase="build")
    engine.run_evidence_state = RunEvidenceState(run_id="digest-empty")

    assert "Last test attempt:" not in engine._advisor_evidence_digest()


# --- (b) the bigtop regression: a 4-island batch survives phase entry ------


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


class _IslandBuildTool(BaseTool):
    """Records the island each compile targeted — the bigtop batch shape."""

    def __init__(self):
        super().__init__("build", "Compile and test the project")
        self.calls = []

    def execute(self, action: str = "compile", working_directory: str = "") -> ToolResult:
        self.calls.append(working_directory)
        return ToolResult.completed_success(output=f"BUILD SUCCESS in {working_directory}")


class _PromptBuilder:
    def invalidate_trunk_cache(self):
        pass

    def build_initial_system_prompt(self, **kwargs):
        return "SYSTEM PROMPT"


class _ScriptedClient:
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


_ISLANDS = (
    "/workspace/bigtop/bigtop-manager",
    "/workspace/bigtop/bigtop-data-generators",
    "/workspace/bigtop/bigtop-packages",
    "/workspace/bigtop/bigtop-tests",
)


def _one(index, name, arguments, text="working"):
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


def _island_batch(index):
    """One assistant turn planning all four islands — the exact shape the
    before-acting redirect cancelled twice in bigtop r1."""
    return NativeTurn(
        text="compiling all four independent islands",
        tool_calls=tuple(
            NativeToolCall(
                id=f"call_{index}_{n}",
                name="build",
                arguments={"action": "compile", "working_directory": root},
                raw_arguments="{}",
            )
            for n, root in enumerate(_ISLANDS, start=1)
        ),
        model_used="scripted-model",
    )


def _batch_script():
    return [
        _one(1, "phase", {"action": "done", "outcome": "success"}),  # provision
        _one(2, "phase", {"action": "done", "outcome": "success"}),  # analyze
        _island_batch(3),  # build: four islands, one turn
        _one(4, "phase", {"action": "done", "outcome": "success"}),  # build
        _one(5, "phase", {"action": "done", "outcome": "partial"}),  # test
        _one(6, "phase", {"action": "done", "outcome": "partial"}),  # report
        _one(7, "phase", {"action": "done", "outcome": "partial"}),  # padding
    ]


def _flow_engine(tmp_path, *, advisor_mode="same-model", max_iterations=20):
    machine = PhaseMachine()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = machine
    engine.max_iterations = max_iterations
    engine.run_evidence_state = RunEvidenceState(run_id="consult-at-entry")
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
    engine.llm_client = _ScriptedClient(_batch_script())
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
        "build": _IslandBuildTool(),
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
    """Record (in, out) message counts for every EXECUTOR renderer repair pass."""
    sizes = []
    original = native_messages._repair_pairing

    def spy(messages):
        repaired = original(messages)
        if messages and str(messages[0].get("content") or "").startswith("SYSTEM PROMPT"):
            sizes.append((len(messages), len(repaired)))
        return repaired

    monkeypatch.setattr(native_messages, "_repair_pairing", spy)
    return sizes


def _redirect_spy(engine):
    rules = []
    original = engine._advisor_redirect_for_call

    def spy(call):
        execution = original(call)
        if execution is not None:
            rules.append(execution.metadata["advisor_redirect"])
        return execution

    engine._advisor_redirect_for_call = spy
    return rules


def test_a_four_island_batch_after_entry_executes_with_zero_redirects(tmp_path, pairing_spy):
    """The bigtop r1 regression: the whole planned batch runs, and the advice
    is already in the window when the model plans it."""
    engine = _flow_engine(tmp_path)
    rules = _redirect_spy(engine)

    termination = engine.run_setup_loop("set up the project", max_iterations=20)

    assert rules == []
    assert engine.tools["build"].calls == list(_ISLANDS)
    # The advice arrived BEFORE the batch turn: the request that produced it
    # already carried the harness consult's tool result.
    batch_request = engine.llm_client.requests[2]
    assert any(
        message["role"] == "tool" and ADVICE in str(message["content"] or "")
        for message in batch_request
    )
    assert any(
        call["id"].startswith("advisor-entry-")
        for message in batch_request
        for call in message.get("tool_calls") or ()
    )
    # Build and test each get exactly one entry consult; nothing else consults.
    assert [call["phase"] for call in engine.advisor_telemetry["calls"]] == ["build", "test"]
    assert pairing_spy, "the renderer must have run"
    assert all(before == after for before, after in pairing_spy)
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete


def test_advisor_mode_off_leaves_the_batch_run_untouched(tmp_path):
    engine = _flow_engine(tmp_path, advisor_mode="off")
    rules = _redirect_spy(engine)

    termination = engine.run_setup_loop("set up the project", max_iterations=20)

    assert rules == []
    assert engine.llm_client.advisor_calls == [], "mode 'off' must consult no provider"
    assert engine.advisor_telemetry == {"mode": "off", "calls": []}
    assert engine.tools["build"].calls == list(_ISLANDS)
    assert termination.termination is RunTerminationStatus.COMPLETED
