"""The ledger closes: no call is silent, and a refusal is a record (spec §2.2).

Two measured holes are closed here, both taken from the d2r3 campaign:

- **rule 1** — phase-tool calls emitted no `loop_decision` at all, so ten
  camel-quarkus calls could only be placed by matching their parameters by hand
  (`logs/d2r3-serial-20260814/slices/camel-quarkus.md`). Every call the engine
  dispatches now states the control layer's reading of it. The recurrence
  LADDER still does not own a claim tool — a repeated phase claim is bounded by
  `completion_claim_decision`, which is its own instrument — so the decision a
  claim tool gets is `continue`, recorded, and never counted.
- **rule 4** — a refused call produced a `loop_decision` and nothing else: no
  envelope, no `tool_result` (cassandra ×2, samza-hello, camel, tapestry-5,
  camel-quarkus seq 124/138/216). A call that never reaches a tool now seals a
  typed `refusal_record` that says which tool was asked, with which parameters
  (by digest, so a refusal and its retry compare), and why it was refused.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_native_loop_engine import _engine, _phase_turn
from test_terminal_claim_convergence import _engine as _repair_engine
from test_terminal_claim_convergence import _open_repair, _repair_call
from test_turn_records import _BillingClient, _events

from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    ControlEventSink,
    RefusalRecordPayload,
    canonical_sha256,
)
from sag.agent.invocation_contracts import clear_action_context
from sag.agent.loop_memory import LoopEvent, LoopMemory
from sag.agent.output_storage import OutputStorageManager
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.token_tracker import TokenTracker
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.trajectory.builder import build_trajectory
from sag.trajectory.reducer import DeltaAccumulator, TrajectoryReducer

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"


def _tool_turn(index, name, arguments):
    """One assistant turn asking for one tool by name."""
    return NativeTurn(
        text=f"Calling {name}.",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name=name,
                arguments=dict(arguments),
                raw_arguments=json.dumps(arguments),
            ),
        ),
        model_used="scripted-model",
    )


def _batch_breaking_turn(index):
    """One assistant turn whose FIRST call ends the batch the second was in."""
    arguments = {"action": "done", "outcome": "success"}
    return NativeTurn(
        text=f"Closing this phase, then asking again ({index}).",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}a",
                name="phase",
                arguments=dict(arguments),
                raw_arguments=json.dumps(arguments),
            ),
            NativeToolCall(
                id=f"call_{index}b",
                name="search",
                arguments={"pattern": "spotless"},
                raw_arguments=json.dumps({"pattern": "spotless"}),
            ),
        ),
        model_used="scripted-model",
    )


def _closure_engine(tmp_path, turns, *, pre_dispatch_control=False):
    """The native-loop harness with a real ledger, store, and loop memory.

    `pre_dispatch_control` wires the real intent-minting boundary in front of
    the tools, which is where a repair intent is accepted or refused.
    """
    engine = _engine(turns)
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine.loop_memory = LoopMemory()
    engine.llm_client = _BillingClient(turns, engine.token_tracker)
    if pre_dispatch_control:
        engine._pending_repair_context = None
        engine._last_invocation_contract_id = None
        orchestrator = ToolOrchestrator(
            tools=engine.tools,
            context_manager=engine.context_manager,
            recent_tool_executions=engine.recent_tool_executions,
            successful_states=engine.successful_states,
            repository_url=engine.repository_url,
            track_tool_execution=lambda *a, **k: None,
            update_successful_states=lambda *a, **k: None,
            add_system_guidance=lambda *a, **k: None,
            get_timestamp=lambda: "2026-08-15T00:00:00Z",
            output_storage=None,
            before_tool_execute=engine._prepare_control_action,
        )
        engine._get_tool_orchestrator = lambda: orchestrator
    return engine


@pytest.fixture
def closed_run(tmp_path):
    """Three model turns, one of them refused before any tool ran."""
    turns = [
        _phase_turn(1),
        _tool_turn(2, "nosuchtool", {"action": "guess"}),
        _phase_turn(3),
    ]
    engine = _closure_engine(tmp_path, turns)
    engine.run_setup_loop("set up the project", max_iterations=3)
    return engine


# ---------------------------------------------------------------------------
# Rule 1 — a phase call is no longer silent
# ---------------------------------------------------------------------------


def test_every_dispatched_call_states_the_control_layers_reading_of_it(closed_run):
    """The camel-quarkus hole: ten phase calls, not one `loop_decision`.

    The harness's own phase-entry advisor consult is in this list too — an
    envelope and a result and no decision is the same silence whether the
    model authored the call or the controller did.
    """
    decisions = _events(closed_run, "loop_decision")
    tools = [row["payload"]["event"]["tool_name"] for row in decisions]

    assert tools == ["phase", "nosuchtool", "phase", "advisor"]
    assert [row["payload"]["event"]["iteration"] for row in decisions] == [1, 2, 3, 3]
    assert [row["payload"]["event"]["phase"] for row in decisions] == [
        "provision",
        "analyze",
        "analyze",
        "build",
    ]


def test_a_claim_tool_is_recorded_by_the_ladder_and_never_counted_by_it(closed_run):
    """A phase call's decision is `continue`, and it is honest about why.

    The recurrence ladder bounds repeated FAILING ACTIONS. A repeated terminal
    claim is bounded by `completion_claim_decision` at its own cap, so counting
    it twice would be two instruments closing one phase for one reason.
    """
    decisions = _events(closed_run, "loop_decision")
    phase_decisions = [
        row["payload"] for row in decisions if row["payload"]["event"]["tool_name"] == "phase"
    ]

    assert [row["expected_decision"] for row in phase_decisions] == ["continue", "continue"]
    assert {row["expected_reason_code"] for row in phase_decisions} == {
        "tool_outside_recurrence_ladder"
    }


def test_a_claim_tool_leaves_the_ladders_state_exactly_as_it_found_it():
    """Parity in the ledger must not become interference in the loop breaker.

    A phase call between two identical failing builds may not disarm the break
    those builds armed, and may not enter the diversity census either — the
    ladder never saw claim tools before, and it still does not.
    """
    memory = LoopMemory()
    failing = LoopEvent(
        tool_name="build",
        args={"action": "compile"},
        operation_outcome="failed",
        error_code="MAVEN_BUILD_ERROR",
        failure_signature="MAVEN_BUILD_ERROR:8d576114",
        relevant_state={"artifacts": 0},
        relevant_scopes=("artifacts",),
        phase="build",
        attempt_id="build-1",
    )
    claiming = LoopEvent(
        tool_name="phase",
        args={"action": "done", "outcome": "success"},
        operation_outcome="failed",
        error_code="build_red",
        failure_signature="build_red:0001",
        relevant_state={"artifacts": 0},
        relevant_scopes=("artifacts",),
        phase="build",
        attempt_id="build-1",
    )
    for _ in range(4):
        memory.observe(failing)
    armed_before = memory._armed_key
    chains_before = dict(memory._chains)

    claim_decision = memory.observe(claiming)

    assert claim_decision.decision == "continue"
    assert claim_decision.close_phase is False
    assert memory._armed_key == armed_before
    assert memory._chains == chains_before
    assert memory.observe(failing).decision == "close_phase"


# ---------------------------------------------------------------------------
# Rule 4 — a refusal is a record, not an absence
# ---------------------------------------------------------------------------


def test_the_refusal_kind_is_appended_to_the_vocabulary_never_inserted():
    assert CONTROL_EVENT_KINDS[23] == "turn_record"
    assert CONTROL_EVENT_KINDS[24] == "refusal_record"
    assert len(CONTROL_EVENT_KINDS) == 25


def test_a_refusal_states_the_call_it_refused():
    event = ControlEvent(
        sequence=124,
        kind="refusal_record",
        payload={
            "tool": "search",
            "tool_call_id": "call_9",
            "refusal_code": "REPAIR_CONTEXT_NOT_ACTIVE",
            "exact_params_sha256": canonical_sha256({"pattern": "spotless"}),
        },
    )

    refusal = event.typed_payload
    assert isinstance(refusal, RefusalRecordPayload)
    assert refusal.tool == "search"
    assert refusal.refusal_code == "REPAIR_CONTEXT_NOT_ACTIVE"


def test_a_refusal_cannot_invent_a_field():
    with pytest.raises(ValidationError):
        RefusalRecordPayload.model_validate(
            {
                "tool": "search",
                "refusal_code": "REPAIR_CONTEXT_NOT_ACTIVE",
                "exact_params_sha256": canonical_sha256({}),
                "invented_field": 1,
            }
        )


def test_a_refusal_names_a_reason_and_a_digest_of_what_was_asked():
    with pytest.raises(ValidationError):
        RefusalRecordPayload.model_validate(
            {"tool": "search", "refusal_code": "", "exact_params_sha256": canonical_sha256({})}
        )
    with pytest.raises(ValidationError):
        RefusalRecordPayload.model_validate(
            {"tool": "search", "refusal_code": "X", "exact_params_sha256": "not-a-digest"}
        )


def test_a_refused_call_seals_a_record_instead_of_saying_nothing(closed_run):
    """The cassandra/tapestry-5/camel-quarkus shape: decision, and silence."""
    refusals = _events(closed_run, "refusal_record")

    assert len(refusals) == 1
    refusal = refusals[0]["payload"]
    assert refusal["tool"] == "nosuchtool"
    assert refusal["tool_call_id"] == "call_2"
    assert refusal["refusal_code"] == "UNKNOWN_TOOL"
    assert refusal["exact_params_sha256"] == canonical_sha256({"action": "guess"})


def test_a_refused_call_has_no_envelope_and_no_result(closed_run):
    """It never reached a tool: the refusal stands in for both."""
    envelopes = _events(closed_run, "action_envelope")
    results = _events(closed_run, "tool_result")

    assert "nosuchtool" not in [row["payload"]["tool"] for row in envelopes]
    assert "nosuchtool" not in [row["payload"]["tool"] for row in results]
    assert [row["payload"]["tool"] for row in envelopes].count("phase") == 2


def test_the_refusal_is_sealed_before_the_decision_that_reads_it(closed_run):
    """Order is the join: a refusal opens the turn its decision then closes."""
    rows = _events(closed_run)
    refusal = next(row for row in rows if row["kind"] == "refusal_record")
    decision = next(
        row
        for row in rows
        if row["kind"] == "loop_decision"
        and row["payload"]["event"]["tool_name"] == "nosuchtool"
    )

    assert refusal["sequence"] < decision["sequence"]


# ---------------------------------------------------------------------------
# The derived view stops calling a closed call a hole
# ---------------------------------------------------------------------------


def test_the_reducer_reads_a_refusal_as_an_annotation_not_a_hole(closed_run, tmp_path):
    snapshot = build_trajectory(tmp_path)
    refused = [turn for turn in snapshot.turns if turn.call and turn.call.tool == "nosuchtool"]

    assert len(refused) == 1
    annotations = [
        annotation
        for annotation in snapshot.annotations
        if annotation.kind == "refusal" and annotation.turn_id == refused[0].turn_id
    ]
    assert annotations and annotations[0].data["refusal_code"] == "UNKNOWN_TOOL"
    assert [warning for warning in snapshot.warnings if warning.turn_id == refused[0].turn_id] == []


# ---------------------------------------------------------------------------
# Rule 3 — repair intent survives from the model's hand to the sealed record
# ---------------------------------------------------------------------------


def _repair_intent():
    return {
        "blocking_fact_refs": ["asm-gate_1ab4d6d4-build_red-ea9e5b34"],
        "repair_hypothesis": "a formatting-only violation blocks the compile",
        "next_action_kind": "compile",
        "expected_observation": ["receipt_assessment"],
        "stop_condition": "stop after one terminal receipt",
    }


def test_a_dispatched_repair_action_seals_the_intent_the_model_submitted():
    """The accepted half, pinned: seatunnel seq 85 carries all five fields."""
    engine = _repair_engine()
    prepared = _open_repair(engine)
    engine._active_native_tool_call_id = "model-repair"
    call = _repair_call(prepared.context)

    engine._prepare_control_action(call, {"action": "deps"})

    envelope = [
        event for event in engine.control_event_sink.events if event.kind == "action_envelope"
    ][-1]
    submitted = call.repair_intent_submission
    assert envelope.payload["repair_hypothesis"] == submitted["repair_hypothesis"]
    assert envelope.payload["blocking_fact_refs"] == submitted["blocking_fact_refs"]
    assert envelope.payload["next_action_kind"] == submitted["next_action_kind"]
    assert envelope.payload["expected_observation"] == submitted["expected_observation"]
    assert envelope.payload["stop_condition"] == submitted["stop_condition"]
    clear_action_context()


def test_a_refused_repair_action_seals_the_intent_that_was_refused(tmp_path):
    """The measured half (seatunnel shape, six calls across the archives).

    A `repair_intent` submitted without an active judge context is refused
    before dispatch, so no envelope was ever going to carry it — and until the
    refusal became a record, the model's whole stated hypothesis lived in the
    branch history and nowhere in the authoritative layer.
    """
    submission = _repair_intent()
    turns = [
        _phase_turn(1),
        _tool_turn(2, "phase", {"action": "done", "outcome": "success"}),
        _phase_turn(3),
    ]
    turns[1].tool_calls[0].arguments["repair_intent"] = submission
    engine = _closure_engine(tmp_path, turns, pre_dispatch_control=True)
    engine.run_setup_loop("set up the project", max_iterations=3)

    refusals = _events(engine, "refusal_record")
    assert len(refusals) == 1
    refusal = refusals[0]["payload"]
    assert refusal["refusal_code"] == "REPAIR_CONTEXT_NOT_ACTIVE"
    assert refusal["repair_intent"] == submission
    # And the parameters it names are the tool's own, so the digest still
    # compares with the envelope of a retry that drops the intent.
    assert refusal["exact_params_sha256"] == canonical_sha256(
        {"action": "done", "outcome": "success", "key_results": ""}
    )


@pytest.fixture
def broken_batch(tmp_path):
    """Three assistant turns of two calls, each broken by its own first call."""
    turns = [_batch_breaking_turn(index) for index in range(1, 4)]
    engine = _closure_engine(tmp_path, turns)
    engine.run_setup_loop("set up the project", max_iterations=3)
    return engine, turns


def test_a_call_the_batch_never_reached_is_a_record_not_a_silence(broken_batch):
    """The measured hole with the fence's own name on it.

    When a batch break fires — a phase transition accepted, a loop-driven
    close, a live job barrier — every remaining call of that assistant turn is
    answered "[not executed: ...]" and used to get NOTHING else: no envelope,
    because nothing dispatched; no `tool_result`, because nothing ran; no
    `loop_decision`, no refusal record, no turn record. A delivered refusal with
    no record of it: exactly the silence spec §2.2 rule 4 forbids.

    It is a refusal, so it is a `refusal_record` — standing, as every refusal
    does, in both of the places its call never reached.
    """
    engine, turns = broken_batch
    refusals = [row["payload"] for row in _events(engine, "refusal_record")]

    assert [row["tool_call_id"] for row in refusals] == ["call_1b", "call_2b", "call_3b"]
    assert [row["tool"] for row in refusals] == ["search"] * 3
    assert {row["refusal_code"] for row in refusals} == {"CALL_NOT_EXECUTED"}
    assert {row["exact_params_sha256"] for row in refusals} == {
        canonical_sha256({"pattern": "spotless"})
    }


def test_the_fence_counts_the_calls_the_model_made_not_the_events_that_exist(broken_batch):
    """The counting trick, closed.

    `#loop_decision == #envelope == #(tool_result ∪ refusal)` equates only the
    events that EXIST, so a call emitting none of them balanced at zero on
    every side and was invisible to the fence and to the reducer alike: ten
    model calls, five in the ledger, and a green conservation check. A fence
    over a ledger cannot be the ledger's own oracle. The count that binds is
    the one the model made.

    A cancelled call has no `loop_decision` because it had no execution to
    read; fabricating one would feed the recurrence ladder an outcome that
    never happened. That is why the decision side is short by exactly the
    cancellations, and why it is stated here rather than balanced away.
    """
    engine, turns = broken_batch
    asked = [call.id for turn in turns for call in turn.tool_calls]
    opened = {
        row["payload"].get("tool_call_id")
        for kind in ("action_envelope", "refusal_record")
        for row in _events(engine, kind)
    }
    counted = _kinds(engine)
    cancelled = sum(
        1
        for row in _events(engine, "refusal_record")
        if row["payload"]["refusal_code"] == "CALL_NOT_EXECUTED"
    )

    assert len(asked) == 6
    assert set(asked) <= opened, "the model made a call the ledger never mentions"
    refusals = counted.get("refusal_record", 0)
    assert (
        counted.get("action_envelope", 0) + counted.get("forced_action", 0) + refusals
        == counted.get("tool_result", 0) + refusals
        == counted.get("loop_decision", 0) + cancelled
    )
    assert cancelled == 3


def test_a_cancelled_call_takes_a_turn_and_states_the_answer_it_delivered(broken_batch, tmp_path):
    """[C] of a turn that ran nothing is still the text the model read.

    The turn is the model's — it was in the same rendered window as the call
    beside it — and its observation ref resolves to the refusal the model was
    actually handed, reason and all. The reducer then reads a complete account
    of a refused call rather than a hole, which is what rule 4 asked for.
    """
    engine, _turns = broken_batch
    store = engine.output_storage
    sealed = [row["payload"] for row in _events(engine, "turn_record")]
    cancelled = [row for row in sealed if row["envelope_ref"] is None and row["actor"] == "model"]

    assert len(cancelled) == 3
    assert [store.retrieve_output(row["observation_ref"]) for row in cancelled] == [
        "[not executed: a done phase transition is being processed]"
    ] * 3
    snapshot = build_trajectory(tmp_path)
    assert snapshot.warnings == []
    assert [turn.turn_id for turn in snapshot.turns] == list(range(1, len(snapshot.turns) + 1))
    assert len([note for note in snapshot.annotations if note.kind == "refusal"]) == 3


def test_a_refused_intent_is_sealed_whole_or_it_is_not_what_was_submitted(tmp_path):
    """The model may state 4,096 chars a field; the record kept 512 of them.

    `refusal_record` was left out of the strict-lineage set, so its payload went
    through `compact_control_value` on the way to the sink — which clips every
    string at 512 characters and appends an ellipsis. A stated hypothesis longer
    than that was sealed as a paraphrase of itself, with no marker anywhere that
    the authoritative record was lossy, under a docstring promising what the
    model SUBMITTED. The payload already bounds itself through
    `bounded_exact_params`; a second, silent bound is the one that lies.
    """
    submission = _repair_intent()
    submission["repair_hypothesis"] = "the generator root shadows the compile classpath. " * 30
    submission["stop_condition"] = "stop after one terminal receipt for the module. " * 28
    assert len(submission["repair_hypothesis"]) > 512
    turns = [
        _phase_turn(1),
        _tool_turn(2, "phase", {"action": "done", "outcome": "success"}),
        _phase_turn(3),
    ]
    turns[1].tool_calls[0].arguments["repair_intent"] = submission
    engine = _closure_engine(tmp_path, turns, pre_dispatch_control=True)
    engine.run_setup_loop("set up the project", max_iterations=3)

    refusal = _events(engine, "refusal_record")[0]["payload"]

    assert refusal["repair_intent"] == submission


def test_a_refusal_that_carried_no_intent_claims_none(closed_run):
    refusal = _events(closed_run, "refusal_record")[0]["payload"]

    assert "repair_intent" not in refusal


# ---------------------------------------------------------------------------
# Rule 5 — the ledger balances
# ---------------------------------------------------------------------------


def _kinds(engine):
    counted = {}
    for row in _events(engine):
        counted[row["kind"]] = counted.get(row["kind"], 0) + 1
    return counted


def test_every_decision_has_its_envelope_and_its_answer(closed_run):
    """#loop_decision == #envelope == #(tool_result ∪ refusal_record).

    A refusal stands in both of the places its call never reached, so it is
    counted on both sides of the equation and on neither twice.
    """
    counted = _kinds(closed_run)
    decisions = counted.get("loop_decision", 0)
    refusals = counted.get("refusal_record", 0)
    opened = counted.get("action_envelope", 0) + counted.get("forced_action", 0) + refusals
    answered = counted.get("tool_result", 0) + refusals

    assert decisions == opened == answered
    assert decisions == 4 and refusals == 1


class _LosingSink:
    """A ledger that will not take one particular turn record.

    A full disk, a revoked handle, a payload the sink rejects — the engine
    already treats all three the same way, by logging and going on. What this
    stands in for is that the record does not reach the file.
    """

    def __init__(self, inner, *, drop_turn_id):
        self._inner = inner
        self._drop_turn_id = drop_turn_id
        self.dropped = 0

    def emit(self, kind, payload):
        if kind == "turn_record" and payload.get("turn_id") == self._drop_turn_id:
            self.dropped += 1
            raise OSError("the ledger would not take this record")
        return self._inner.emit(kind, payload)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_turn_record_that_never_persisted_leaves_the_hole_it_is_looked_for_by(tmp_path):
    """Rule 5's hole check must be falsifiable, or it is true by construction.

    The turn counter used to advance AFTER the emit and inside its try, so a
    record that failed to persist gave its id back to the next turn: the engine
    could not produce a hole, "the turn_id sequence has no holes" held by
    construction, and the reducer's `conservation_violation` was dead code for
    every engine-produced ledger. A run whose whole turn-record stream failed
    derived a clean, silent, one-turn trajectory.

    The id is spent when the turn is sealed. A record that does not arrive is
    then exactly what it is: a hole, in the place the fence looks.
    """
    turns = [_phase_turn(index) for index in range(1, 4)]
    engine = _closure_engine(tmp_path, turns)
    engine.control_event_sink = _LosingSink(engine.control_event_sink, drop_turn_id=2)
    engine.run_setup_loop("set up the project", max_iterations=3)

    stated = [row["payload"]["turn_id"] for row in _events(engine, "turn_record")]

    assert engine.control_event_sink.dropped == 1
    assert 2 not in stated
    assert stated == sorted(stated) and stated[0] == 1
    violations = [
        warning
        for warning in build_trajectory(tmp_path).warnings
        if warning.code == "conservation_violation"
    ]
    assert len(violations) == 1 and "3" in violations[0].detail


def test_the_turn_sequence_has_no_holes(closed_run):
    """Three model turns and the controller's consult, in one sequence."""
    records = _events(closed_run, "turn_record")
    stated = [row["payload"]["turn_id"] for row in records]

    assert stated == [1, 2, 3, 4]
    assert [row["payload"]["actor"] for row in records] == [
        "model",
        "model",
        "model",
        "controller",
    ]


def test_a_closed_session_replays_with_nothing_left_to_state(closed_run, tmp_path):
    """The whole point: a post-closure run derives without a single warning."""
    snapshot = build_trajectory(tmp_path)

    assert snapshot.warnings == []
    assert [turn.turn_id for turn in snapshot.turns] == list(range(1, len(snapshot.turns) + 1))


def test_the_reducer_takes_the_turn_from_the_record_instead_of_inferring_it(closed_run, tmp_path):
    """Exact turns: the phase, the iteration, the bill and the window are stated."""
    snapshot = build_trajectory(tmp_path)
    model_turns = [turn for turn in snapshot.turns if turn.actor == "model"]

    assert [turn.phase for turn in model_turns] == ["provision", "analyze", "analyze"]
    assert [turn.iteration for turn in model_turns] == [1, 2, 3]
    assert all(turn.tokens is not None for turn in model_turns)
    # One handle per window, and a different one per turn: the newest message
    # the model was shown, resolvable in the store the run wrote.
    windows = [turn.window_ref for turn in model_turns]
    assert all(windows) and len(set(windows)) == len(windows)
    # The controller was shown no window and claims none.
    assert [turn.window_ref for turn in snapshot.turns if turn.actor == "controller"] == [None]


def test_the_live_fold_and_the_replay_agree_on_a_closed_session(closed_run, tmp_path):
    """Idempotence, with records and a refusal in the stream (spec §1).

    Sealing a turn withdraws the statements the inference had made about it, so
    the accumulated deltas and the snapshot can only match if the retraction is
    shipped as carefully as the claim was.
    """
    reducer = TrajectoryReducer()
    accumulator = DeltaAccumulator()
    for line in (tmp_path / "control_events.jsonl").read_text(encoding="utf-8").splitlines():
        accumulator.feed(reducer.feed(line))

    assert accumulator.snapshot() == reducer.snapshot()


def test_a_controller_close_takes_a_turn_of_its_own(tmp_path):
    """An engine-generated gate seals a record while the model's turn is done.

    The open turn is the model's, already sealed, so the controller's record
    cannot be folded into it — it is a turn of its own, and a turn that made no
    call is missing neither an envelope nor an answer.
    """
    reducer = TrajectoryReducer()
    reducer.feed(
        json.dumps(
            {
                "sequence": 1,
                "kind": "turn_record",
                "payload": {
                    "turn_id": 1,
                    "phase": "test",
                    "actor": "model",
                    "window_digest": {"system_prompt_sha256": "a" * 64},
                    "t0": "2026-08-15T00:00:00Z",
                    "t1": "2026-08-15T00:00:01Z",
                },
            }
        )
    )
    reducer.feed(
        json.dumps(
            {
                "sequence": 2,
                "kind": "turn_record",
                "payload": {
                    "turn_id": 2,
                    "phase": "test",
                    "actor": "controller",
                    "gate_decision_id": "gate-1",
                    "window_digest": {"system_prompt_sha256": "a" * 64},
                    "t0": "2026-08-15T00:00:02Z",
                    "t1": "2026-08-15T00:00:02Z",
                },
            }
        )
    )
    snapshot = reducer.snapshot()

    assert [turn.actor for turn in snapshot.turns] == ["model", "controller"]
    assert snapshot.warnings == []


def test_a_turn_id_that_jumps_is_stated_as_a_conservation_violation():
    reducer = TrajectoryReducer()
    for turn_id in (1, 3):
        reducer.feed(
            json.dumps(
                {
                    "sequence": turn_id,
                    "kind": "turn_record",
                    "payload": {
                        "turn_id": turn_id,
                        "phase": "build",
                        "actor": "controller",
                        "window_digest": {"system_prompt_sha256": "a" * 64},
                        "t0": "2026-08-15T00:00:00Z",
                        "t1": "2026-08-15T00:00:01Z",
                    },
                }
            )
        )

    violations = [
        warning
        for warning in reducer.snapshot().warnings
        if warning.code == "conservation_violation"
    ]
    assert len(violations) == 1 and "3" in violations[0].detail


def test_the_archived_sessions_keep_the_holes_their_bytes_recorded():
    """New machinery never rewrites an old run: those calls WERE silent."""
    archived = build_trajectory(FIXTURES / "camel-quarkus-d2r3")
    silent = [
        warning
        for warning in archived.warnings
        if warning.code in ("missing_loop_decision", "missing_tool_result")
    ]

    assert len(silent) >= 10
