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
    """The camel-quarkus hole: ten phase calls, not one `loop_decision`."""
    decisions = _events(closed_run, "loop_decision")
    tools = [row["payload"]["event"]["tool_name"] for row in decisions]

    assert tools == ["phase", "nosuchtool", "phase"]
    assert [row["payload"]["event"]["iteration"] for row in decisions] == [1, 2, 3]
    assert [row["payload"]["event"]["phase"] for row in decisions] == [
        "provision",
        "analyze",
        "analyze",
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


def test_a_refusal_that_carried_no_intent_claims_none(closed_run):
    refusal = _events(closed_run, "refusal_record")[0]["payload"]

    assert "repair_intent" not in refusal


def test_the_archived_sessions_keep_the_holes_their_bytes_recorded():
    """New machinery never rewrites an old run: those calls WERE silent."""
    archived = build_trajectory(FIXTURES / "camel-quarkus-d2r3")
    silent = [
        warning
        for warning in archived.warnings
        if warning.code in ("missing_loop_decision", "missing_tool_result")
    ]

    assert len(silent) >= 10
