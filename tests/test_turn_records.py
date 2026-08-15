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

import pytest
from pydantic import ValidationError

from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    TurnRecordPayload,
    WindowDigestPayload,
)

SHA = "a" * 64


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
