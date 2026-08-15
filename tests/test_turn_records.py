"""A turn is a sealed record, not an inference (spec §2.1).

Every model turn used to be reconstructed after the fact by pairing an
envelope with a result and guessing which window produced it. `turn_record`
ends the guessing: the engine states the turn's identity, the exact window the
model saw, the call it made, the observation it got back, and what the run was
billed for it.

The window is stated as COMPONENTS — a system-prompt version hash plus the
ordered refs of every message in the rendered array — so a reader can resolve
[A] byte-for-byte instead of approximating it from branch history. The order
is part of the record, because a set of refs reconstructs nothing.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_native_loop_engine import _engine, _phase_turn

from sag.agent.attempt_policy import TestAttemptRequirement as AttemptRequirement
from sag.agent.attempt_policy import TestCandidateResolution as CandidateResolution
from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    ControlEventSink,
    TurnRecordPayload,
    WindowDigestPayload,
)
from sag.agent.output_storage import OutputStorageManager
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType
from sag.agent.token_tracker import TokenTracker
from sag.agent.tool_orchestration import ToolExecution
from sag.tools.base import ToolResult

SHA = "a" * 64
PROMPT_TOKENS = 4134
COMPLETION_TOKENS = 211


def _payload(**overrides):
    payload = {
        "turn_id": 1,
        "phase": "build",
        "iteration": 7,
        "actor": "model",
        "window_digest": {
            "system_prompt_sha256": SHA,
            "component_refs": ["output_aaaaaaaaaaaa", "output_bbbbbbbbbbbb"],
        },
        "envelope_ref": "envelope-000012",
        "observation_ref": "output_cccccccccccc",
        "gate_decision_id": None,
        "tokens_in": 4134,
        "tokens_out": 211,
        "t0": "2026-08-15T10:00:00Z",
        "t1": "2026-08-15T10:00:04Z",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Task 6 — the kind and its payload
# ---------------------------------------------------------------------------


def test_turn_record_is_appended_to_the_vocabulary_never_inserted():
    """Anything reading the tuple by order stays correct."""
    assert CONTROL_EVENT_KINDS[22] == "gate_outcome_revised"
    assert CONTROL_EVENT_KINDS[23] == "turn_record"
    assert CONTROL_EVENT_KINDS[24] == "refusal_record"
    assert len(CONTROL_EVENT_KINDS) == 25


def test_a_sealed_turn_states_what_it_saw_said_and_heard():
    event = ControlEvent(sequence=41, kind="turn_record", payload=_payload())

    sealed = event.typed_payload
    assert isinstance(sealed, TurnRecordPayload)
    assert sealed.turn_id == 1 and sealed.actor == "model" and sealed.iteration == 7
    assert sealed.envelope_ref == "envelope-000012"
    assert sealed.observation_ref == "output_cccccccccccc"
    assert sealed.tokens_in == 4134 and sealed.tokens_out == 211
    assert sealed.window_digest.system_prompt_sha256 == SHA


def test_a_window_keeps_its_components_in_the_order_it_rendered_them():
    """Bytes are stored once; the ORDER is what reconstructs the array.

    Two identical messages resolve to one ref and must still appear twice, in
    their two positions — deduplicating the list would drop a message from the
    window it is supposed to reproduce.
    """
    digest = WindowDigestPayload(
        system_prompt_sha256=SHA,
        component_refs=["output_sys", "output_dup", "output_mid", "output_dup"],
    )

    assert digest.component_refs == (
        "output_sys",
        "output_dup",
        "output_mid",
        "output_dup",
    )


def test_an_invented_field_is_refused():
    with pytest.raises(ValidationError):
        TurnRecordPayload.model_validate(_payload(invented_field=1))


def test_a_turn_is_numbered_from_one():
    with pytest.raises(ValidationError):
        TurnRecordPayload.model_validate(_payload(turn_id=0))


def test_only_the_model_or_the_controller_takes_a_turn():
    with pytest.raises(ValidationError):
        TurnRecordPayload.model_validate(_payload(actor="daemon"))

    assert TurnRecordPayload.model_validate(_payload(actor="controller")).actor == "controller"


def test_a_turn_cannot_end_before_it_began():
    with pytest.raises(ValidationError):
        TurnRecordPayload.model_validate(
            _payload(t0="2026-08-15T10:00:04Z", t1="2026-08-15T10:00:00Z")
        )


def test_a_controller_turn_needs_no_envelope_no_result_and_no_bill():
    sealed = TurnRecordPayload.model_validate(
        _payload(
            actor="controller",
            envelope_ref=None,
            observation_ref=None,
            tokens_in=None,
            tokens_out=None,
            iteration=None,
        )
    )

    assert sealed.envelope_ref is None and sealed.tokens_in is None
    assert sealed.iteration is None


# ---------------------------------------------------------------------------
# Task 7 — the engine seals model turns
# ---------------------------------------------------------------------------


class _BillingClient:
    """The scripted native client, plus the token record a real response leaves.

    `react_llm` tracks one executor row per response before the turn reaches
    the loop. Doing the same here is what puts a real bill in front of the
    in-process join — and keeps `token_usage.csv` out of it entirely, since
    that file is not written until the loop exits.
    """

    def __init__(self, turns, tracker):
        self.turns = list(turns)
        self.tracker = tracker
        self.requests = []

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=True, model="scripted-model")

    def get_native_turn(self, messages, *, include_tools=True):
        self.requests.append([dict(message) for message in messages])
        self.tracker.track_token_usage(
            SimpleNamespace(
                usage=SimpleNamespace(
                    total_tokens=PROMPT_TOKENS + COMPLETION_TOKENS,
                    prompt_tokens=PROMPT_TOKENS,
                    completion_tokens=COMPLETION_TOKENS,
                )
            ),
            "scripted-model",
            "executor",
        )
        if not self.turns:
            raise AssertionError("the scripted client ran out of turns")
        return self.turns.pop(0)


def _sealing_engine(tmp_path, turns):
    """The native-loop harness with a real ledger, store, and token tracker."""
    engine = _engine(turns)
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine.llm_client = _BillingClient(turns, engine.token_tracker)
    return engine


def _events(engine, kind=None):
    path = Path(engine.control_event_sink.path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [row for row in rows if kind is None or row["kind"] == kind]


@pytest.fixture
def sealed_run(tmp_path):
    engine = _sealing_engine(tmp_path, [_phase_turn(index) for index in range(1, 6)])
    engine.run_setup_loop("set up the project", max_iterations=12)
    return engine


def _model_records(engine):
    """The model's turns, which the controller's share a sequence with."""
    return [row for row in _events(engine, "turn_record") if row["payload"]["actor"] == "model"]


def test_every_model_turn_seals_exactly_one_record(sealed_run):
    """One call, one sealed turn — and the numbering has no holes.

    The controller's own moves (this run's two phase-entry advisor consults)
    take turns in the SAME sequence, so the model's ids are the subsequence
    left when they are removed, and the sequence as a whole is unbroken.
    """
    records = _events(sealed_run, "turn_record")
    model_records = _model_records(sealed_run)
    envelopes = _events(sealed_run, "action_envelope")

    model_calls = [
        row["payload"]["envelope_id"]
        for row in envelopes
        if str(row["payload"].get("tool_call_id", "")).startswith("call_")
    ]

    assert len(model_records) == len(model_calls) == 5
    assert [row["payload"]["turn_id"] for row in records] == list(range(1, len(records) + 1))
    assert [row["payload"]["iteration"] for row in model_records] == [1, 2, 3, 4, 5]
    # The phase the call was MADE in, which is the phase it closed.
    assert [row["payload"]["phase"] for row in model_records] == [
        "provision",
        "analyze",
        "build",
        "test",
        "report",
    ]
    assert [row["payload"]["envelope_ref"] for row in model_records] == model_calls
    assert all(row["payload"]["t1"] >= row["payload"]["t0"] for row in records)


def test_a_sealed_window_resolves_to_the_bytes_the_model_saw(sealed_run):
    """[A] becomes a lookup: the refs, in the recorded order, ARE the array.

    This is the fence the whole digest exists for. Nothing is reconstructed
    from branch history, nothing is approximated by compaction shape — the
    components are resolved from the output store and concatenated in list
    order, and what comes out is the exact messages array the client received.
    """
    storage = sealed_run.output_storage
    records = _model_records(sealed_run)

    assert len(records) == len(sealed_run.llm_client.requests)
    for record, request in zip(records, sealed_run.llm_client.requests):
        digest = record["payload"]["window_digest"]
        resolved = [storage.retrieve_output(ref) for ref in digest["component_refs"]]
        assert all(body is not None for body in resolved), "a window ref did not resolve"
        assert [json.loads(body) for body in resolved] == request
        assert (
            digest["system_prompt_sha256"]
            == hashlib.sha256(request[0]["content"].encode("utf-8")).hexdigest()
        )


def test_the_windows_bytes_are_stored_once_and_the_order_carries_the_repeat(sealed_run):
    """Every turn re-sends the system prompt; the store keeps one copy of it."""
    records = _model_records(sealed_run)
    first_components = [row["payload"]["window_digest"]["component_refs"][0] for row in records]
    referenced = [
        ref for row in records for ref in row["payload"]["window_digest"]["component_refs"]
    ]

    assert len(set(first_components)) == 1, "the system prompt was stored more than once"
    assert len(referenced) > len(set(referenced)), "no component was reused across turns"


def test_the_bill_joins_in_process_never_from_the_csv(sealed_run, tmp_path):
    """Tokens come from the accounting that writes the CSV, not from the file.

    `token_usage.csv` is exported when the loop EXITS, so a record that waited
    for it would seal blank for the whole run — which is precisely why the join
    is in-process.
    """
    records = _model_records(sealed_run)

    assert [row["payload"]["tokens_in"] for row in records] == [PROMPT_TOKENS] * 5
    assert [row["payload"]["tokens_out"] for row in records] == [COMPLETION_TOKENS] * 5
    # The harness's own turns are never billed the model's response.
    controller = [
        row for row in _events(sealed_run, "turn_record") if row["payload"]["actor"] == "controller"
    ]
    assert controller and all(row["payload"]["tokens_in"] is None for row in controller)
    assert not list(tmp_path.rglob("token_usage.csv"))


def test_a_sealed_turn_states_the_observation_it_delivered(sealed_run):
    """[C] is resolvable too: the ref names the text the model actually read."""
    storage = sealed_run.output_storage
    records = _model_records(sealed_run)
    delivered = [
        step.content for step in sealed_run.steps if getattr(step, "tool_call_id", None) == "call_5"
    ]

    refs = [row["payload"]["observation_ref"] for row in records]
    assert all(ref for ref in refs)
    assert storage.retrieve_output(refs[-1]) in delivered


def test_sealing_a_turn_never_changes_what_the_model_can_find(sealed_run):
    """The store a record writes into is the store `output_search` reads.

    The engine's `OutputStorageManager` and the search tool's point at the same
    `contexts/full_outputs.jsonl` and `output_index.json` by construction, and
    `search_outputs` scans that index newest-first with no default filter. Every
    rendered message and every delivered observation is a row in it — about two
    an iteration — and a window component is a JSON copy of a message full of
    prior observations and build text, so nearly any pattern matches one. Left
    searchable they consume the model's limit before it reaches the log it
    asked for.

    An observability feature must not change what the model can find. The bytes
    stay where the record's refs resolve them; they are simply not answers to a
    question the model asked.
    """
    store = sealed_run.output_storage
    sealed = _model_records(sealed_run)[-1]["payload"]

    searched = store.search_outputs(pattern="advisor", limit=10)
    listed = store.search_outputs(limit=10)

    assert searched and [row["tool_name"] for row in searched] == ["advisor"] * len(searched)
    assert listed and [row["tool_name"] for row in listed] == ["advisor"] * len(listed)
    # And [A] and [C] are still one lookup away for the reader who holds the ref.
    assert store.retrieve_output(sealed["window_digest"]["component_refs"][0])
    assert store.retrieve_output(sealed["observation_ref"])


# ---------------------------------------------------------------------------
# Task 8 — the controller's turns, observations included
# ---------------------------------------------------------------------------


class _BranchHistory:
    """The context manager, reduced to the one fact this task is about."""

    def __init__(self):
        self.current_task_id = "task-test-1"
        self.entries = []

    def add_to_branch_history(self, task_id, entry):
        self.entries.append((task_id, entry))


def _requirement():
    return AttemptRequirement(
        root="/workspace/demo",
        system="gradle",
        required_action={
            "tool": "build",
            "params": {"action": "test", "working_directory": "/workspace/demo"},
        },
    )


def _forcing_engine(tmp_path):
    """A harness that forces one required test attempt, on a real ledger."""
    requirement = _requirement()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    engine.steps = []
    engine.tools = {}
    engine.current_iteration = 7
    engine.config = SimpleNamespace(verbose=False)
    engine.orchestrator = None
    engine.context_manager = _BranchHistory()
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine._last_test_candidate_resolution = CandidateResolution(
        status="available",
        candidates=(requirement,),
        project_root="/workspace/demo",
        workspace_root="/workspace",
        primary=requirement,
    )
    engine._get_timestamp = lambda: "2026-08-15T00:00:00Z"

    result = ToolResult.completed_success(
        output="50 tests passed",
        metadata={"command": "./gradlew test", "runner_dispatched": True, "exit_code": 0},
    )

    def execute(call):
        return ToolExecution(
            call=call,
            result=result,
            status="success",
            raw_params=call.raw_params,
            validated_params=dict(call.raw_params),
            observation_text="50 tests passed",
            attempted_execution=True,
        )

    def add_observation_step(observation):
        step = ReActStep(
            step_type=StepType.OBSERVATION,
            content=observation,
            timestamp="2026-08-15T00:00:00Z",
        )
        engine.steps.append(step)
        return step

    engine._execute_tool_call = execute
    engine._mark_forced_test_refusals = lambda execution, requirement: None
    engine._record_execution_bundle = lambda execution, call: (
        execution.result,
        "forced-execution-1",
        [],
    )
    engine._emit_control_tool_result = lambda **kwargs: None
    engine._apply_tool_execution_loop_effects = lambda execution: None
    engine._add_observation_step = add_observation_step
    return engine, requirement


def test_a_forced_action_seals_a_controller_turn(tmp_path):
    engine, requirement = _forcing_engine(tmp_path)

    assert engine._force_required_test_attempt(requirement, trigger="phase_floor") is True

    records = _events(engine, "turn_record")
    forced = _events(engine, "forced_action")
    assert len(records) == len(forced) == 1
    sealed = records[0]["payload"]
    assert sealed["actor"] == "controller"
    assert sealed["turn_id"] == 1
    assert sealed["phase"] == "test"
    assert sealed["envelope_ref"] == forced[0]["payload"]["envelope_id"]
    # The harness's move is never billed the model's response.
    assert sealed["tokens_in"] is None and sealed["tokens_out"] is None
    # A controller answers from policy, not from a window: no components are
    # claimed for a turn nobody was shown.
    assert sealed["window_digest"]["component_refs"] == []


def test_a_forced_observation_reaches_the_branch_history(tmp_path):
    """The cayenne/ignite/polaris/camel gap (spec §2.2 rule 2).

    A forced attempt wrote its ACTION and its observation into the window and
    nowhere else, so the phase history the next context read — and every
    post-hoc reconstruction of it — was missing the harness's evidence
    entirely.
    """
    engine, requirement = _forcing_engine(tmp_path)

    engine._force_required_test_attempt(requirement, trigger="phase_floor")

    assert engine.context_manager.entries, "the forced observation never reached branch history"
    task_id, entry = engine.context_manager.entries[-1]
    assert task_id == "task-test-1"
    assert entry["type"] == "action"
    assert entry["tool_name"] == "build"
    assert entry["observation"] == "50 tests passed"
    assert entry["iteration"] == 7

    sealed = _events(engine, "turn_record")[0]["payload"]
    assert engine.output_storage.retrieve_output(sealed["observation_ref"]) == "50 tests passed"


def test_an_engine_close_seals_the_word_it_sealed(tmp_path):
    """An engine-generated gate is a controller turn, on the same sequence."""
    engine, _ = _forcing_engine(tmp_path)
    engine._add_system_guidance = lambda text, priority=0: None
    engine._current_attempt_id = lambda: "test-1"
    sealed_gates = []

    def emit_gate(claim, gate):
        sealed_gates.append(gate)
        engine._last_sealed_decision_id = gate.decision_id
        return SimpleNamespace(sequence=1)

    engine._emit_control_gate = emit_gate

    claim = PhaseClaim(
        phase="test",
        signal="done",
        claimed_outcome=PhaseOutcome.FAILED,
        key_results="the floor was never reached",
    )
    gate = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.FAILED,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.RED,
        reason="phase floor exhausted",
        claim=claim,
    )

    engine._seal_engine_gate(claim, gate)

    records = _events(engine, "turn_record")
    assert len(records) == 1
    sealed = records[0]["payload"]
    assert sealed["actor"] == "controller"
    assert sealed["gate_decision_id"] == gate.decision_id
    assert sealed["envelope_ref"] is None
    # The word the close delivered is resolvable, like any other observation.
    assert "phase floor exhausted" in engine.output_storage.retrieve_output(
        sealed["observation_ref"]
    )


def test_the_controller_and_the_model_share_one_turn_sequence(tmp_path):
    """Two controller moves, two turn ids, no holes and no restarts."""
    engine, requirement = _forcing_engine(tmp_path)

    engine._force_required_test_attempt(requirement, trigger="phase_floor")
    engine._forcing_required_test_attempt = False
    engine._force_required_test_attempt(requirement, trigger="loop_close")

    assert [row["payload"]["turn_id"] for row in _events(engine, "turn_record")] == [1, 2]
