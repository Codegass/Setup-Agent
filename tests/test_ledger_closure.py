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
from test_turn_records import _BillingClient, _events

from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    ControlEventSink,
    RefusalRecordPayload,
    canonical_sha256,
)
from sag.agent.loop_memory import LoopEvent, LoopMemory
from sag.agent.output_storage import OutputStorageManager
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.token_tracker import TokenTracker
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


def _closure_engine(tmp_path, turns):
    """The native-loop harness with a real ledger, store, and loop memory."""
    engine = _engine(turns)
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine.loop_memory = LoopMemory()
    engine.llm_client = _BillingClient(turns, engine.token_tracker)
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


def test_the_archived_sessions_keep_the_holes_their_bytes_recorded():
    """New machinery never rewrites an old run: those calls WERE silent."""
    archived = build_trajectory(FIXTURES / "camel-quarkus-d2r3")
    silent = [
        warning
        for warning in archived.warnings
        if warning.code in ("missing_loop_decision", "missing_tool_result")
    ]

    assert len(silent) >= 10
