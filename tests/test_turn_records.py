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

from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    ControlEventSink,
    TurnRecordPayload,
    WindowDigestPayload,
)
from sag.agent.output_storage import OutputStorageManager
from sag.agent.token_tracker import TokenTracker

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
    assert len(CONTROL_EVENT_KINDS) == 24


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


def test_every_model_turn_seals_exactly_one_record(sealed_run):
    """One call, one sealed turn — and the numbering has no holes."""
    records = _events(sealed_run, "turn_record")
    envelopes = _events(sealed_run, "action_envelope")

    model_calls = [
        row["payload"]["envelope_id"]
        for row in envelopes
        if str(row["payload"].get("tool_call_id", "")).startswith("call_")
    ]

    assert len(records) == len(model_calls) == 5
    assert [row["payload"]["turn_id"] for row in records] == [1, 2, 3, 4, 5]
    assert {row["payload"]["actor"] for row in records} == {"model"}
    assert [row["payload"]["iteration"] for row in records] == [1, 2, 3, 4, 5]
    # The phase the call was MADE in, which is the phase it closed.
    assert [row["payload"]["phase"] for row in records] == [
        "provision",
        "analyze",
        "build",
        "test",
        "report",
    ]
    assert [row["payload"]["envelope_ref"] for row in records] == model_calls
    assert all(row["payload"]["t1"] >= row["payload"]["t0"] for row in records)


def test_a_sealed_window_resolves_to_the_bytes_the_model_saw(sealed_run):
    """[A] becomes a lookup: the refs, in the recorded order, ARE the array.

    This is the fence the whole digest exists for. Nothing is reconstructed
    from branch history, nothing is approximated by compaction shape — the
    components are resolved from the output store and concatenated in list
    order, and what comes out is the exact messages array the client received.
    """
    storage = sealed_run.output_storage
    records = _events(sealed_run, "turn_record")

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
    records = _events(sealed_run, "turn_record")
    first_components = [row["payload"]["window_digest"]["component_refs"][0] for row in records]
    referenced = [ref for row in records for ref in row["payload"]["window_digest"]["component_refs"]]

    assert len(set(first_components)) == 1, "the system prompt was stored more than once"
    assert len(referenced) > len(set(referenced)), "no component was reused across turns"


def test_the_bill_joins_in_process_never_from_the_csv(sealed_run, tmp_path):
    """Tokens come from the accounting that writes the CSV, not from the file.

    `token_usage.csv` is exported when the loop EXITS, so a record that waited
    for it would seal blank for the whole run — which is precisely why the join
    is in-process.
    """
    records = _events(sealed_run, "turn_record")

    assert [row["payload"]["tokens_in"] for row in records] == [PROMPT_TOKENS] * 5
    assert [row["payload"]["tokens_out"] for row in records] == [COMPLETION_TOKENS] * 5
    assert not list(tmp_path.rglob("token_usage.csv"))


def test_a_sealed_turn_states_the_observation_it_delivered(sealed_run):
    """[C] is resolvable too: the ref names the text the model actually read."""
    storage = sealed_run.output_storage
    records = _events(sealed_run, "turn_record")
    delivered = [
        step.content for step in sealed_run.steps if getattr(step, "tool_call_id", None) == "call_5"
    ]

    refs = [row["payload"]["observation_ref"] for row in records]
    assert all(ref for ref in refs)
    assert storage.retrieve_output(refs[-1]) in delivered
