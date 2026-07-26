# tests/test_action_envelope_rekey.py
"""Action envelopes must survive the scheduler's removal: a step carrying a
native tool_call_id gets an envelope without any plan machinery (anatomy
map risk 1 — otherwise every tool_result control event vanishes)."""

from types import SimpleNamespace

import pytest

from sag.agent.control_events import (
    ActionEnvelopePayload,
    ControlEvent,
    ControlEventSink,
    action_envelope_sha256,
)
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import StepType
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult

# One recorded envelope from tests/fixtures/control_layer/paramiko.jsonl.
# Its digest is the byte-identity contract for every transcript already on disk.
_RECORDED_PARAMS = {
    "action": "deps",
    "working_directory": "/workspace/paramiko",
    "timeout": 120,
}
_RECORDED_SHA = "c2854c116bd27743c3b101d2cb87ceb39452ac3a08764c87186df92a7327a200"


def _sink(tmp_path):
    return ControlEventSink(
        tmp_path / "control_events.jsonl",
        clock=lambda: "2026-07-26T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )


def _events(tmp_path):
    text = (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    return [ControlEvent.model_validate_json(line) for line in text.splitlines()]


def _engine(sink, **attributes):
    """A schedulerless engine — no plan, no scheduler, no scheduled turn."""
    engine = object.__new__(ReActEngine)
    engine.control_event_sink = sink
    engine._active_control_envelope_id = None
    engine.steps = []
    for name, value in attributes.items():
        setattr(engine, name, value)
    return engine


def _action_step(tool_call_id):
    # ACTION steps gain `tool_call_id` in Plan 2 Task 2; the envelope only
    # duck-types on it, so the identity seam is exercised independently.
    return SimpleNamespace(step_type=StepType.ACTION, tool_call_id=tool_call_id)


def _result():
    return ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.SUCCESS,
        evidence_status=EvidenceStatus.VERIFIED,
        output_ref="output_native_build",
        output="compiled",
        refs=["output_native_build"],
        evidence_refs=["output_native_build"],
    )


def test_payload_accepts_tool_call_identity():
    payload = ActionEnvelopePayload(
        envelope_id="envelope-000001",
        tool="build",
        exact_params={"action": "compile"},
        tool_call_id="call_7",
        plan_index=None,
        envelope_sha256=action_envelope_sha256(
            tool="build", exact_params={"action": "compile"}, tool_call_id="call_7"
        ),
    )

    assert payload.tool_call_id == "call_7"
    assert payload.plan_index is None


def test_payload_still_accepts_plan_index_identity():
    payload = ActionEnvelopePayload(
        envelope_id="envelope-000001",
        tool="build",
        exact_params=_RECORDED_PARAMS,
        plan_index=0,
        envelope_sha256=_RECORDED_SHA,
    )

    assert payload.plan_index == 0
    assert payload.tool_call_id is None


def test_payload_rejects_an_identityless_envelope():
    with pytest.raises(ValueError):
        ActionEnvelopePayload(
            envelope_id="envelope-000001",
            tool="build",
            exact_params={"action": "compile"},
            envelope_sha256="0" * 64,
        )


def test_hash_is_stable_for_old_transcripts():
    recomputed = action_envelope_sha256(
        plan_index=0, tool="build", exact_params=_RECORDED_PARAMS
    )

    assert recomputed == _RECORDED_SHA


def test_hash_prefers_plan_index_when_both_identities_are_present():
    assert (
        action_envelope_sha256(
            plan_index=0,
            tool="build",
            exact_params=_RECORDED_PARAMS,
            tool_call_id="call_7",
        )
        == _RECORDED_SHA
    )


def test_hash_accepts_tool_call_identity():
    a = action_envelope_sha256(
        tool="build", exact_params={"action": "compile"}, tool_call_id="call_7"
    )
    b = action_envelope_sha256(
        tool="build", exact_params={"action": "compile"}, tool_call_id="call_8"
    )

    assert a != b


def test_hash_rejects_a_missing_identity():
    with pytest.raises(ValueError):
        action_envelope_sha256(tool="build", exact_params={"action": "compile"})


def test_native_tool_call_id_emits_an_envelope_without_a_scheduler(tmp_path):
    sink = _sink(tmp_path)
    engine = _engine(sink, _active_native_tool_call_id="call_7")
    params = {"action": "build", "working_directory": "/workspace/demo"}

    envelope_id = engine._emit_control_action_envelope("build", params)

    assert envelope_id == "envelope-000001"
    envelope = _events(tmp_path)[0]
    assert envelope.kind == "action_envelope"
    assert envelope.payload["tool_call_id"] == "call_7"
    assert "plan_index" not in envelope.payload
    assert envelope.payload["envelope_sha256"] == action_envelope_sha256(
        tool="build", exact_params=params, tool_call_id="call_7"
    )


def test_native_envelope_keeps_the_tool_result_event_alive(tmp_path):
    sink = _sink(tmp_path)
    engine = _engine(sink, _active_native_tool_call_id="call_7")
    params = {"action": "build", "working_directory": "/workspace/demo"}

    envelope_id = engine._emit_control_action_envelope("build", params)
    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-native-1",
        tool="build",
        params=params,
        result=_result(),
    )

    kinds = [event.kind for event in _events(tmp_path)]
    assert kinds == ["action_envelope", "tool_result"]


def test_envelope_identity_falls_back_to_the_active_action_step(tmp_path):
    sink = _sink(tmp_path)
    engine = _engine(sink)
    engine.steps = [
        SimpleNamespace(step_type=StepType.OBSERVATION, tool_call_id="call_1"),
        _action_step("call_1"),
        _action_step("call_2"),
    ]

    engine._emit_control_action_envelope("bash", {"command": "ls"})

    assert _events(tmp_path)[0].payload["tool_call_id"] == "call_2"


def test_envelope_is_omitted_without_any_action_identity(tmp_path):
    sink = _sink(tmp_path)
    engine = _engine(sink)
    engine.steps = [_action_step(None)]

    assert engine._emit_control_action_envelope("bash", {"command": "ls"}) is None
    assert not (tmp_path / "control_events.jsonl").exists()


def test_suppression_still_wins_over_a_native_identity(tmp_path):
    sink = _sink(tmp_path)
    engine = _engine(
        sink,
        _active_native_tool_call_id="call_7",
        _suppress_control_action_envelope=True,
    )

    assert engine._emit_control_action_envelope("build", {"action": "test"}) is None
    assert not (tmp_path / "control_events.jsonl").exists()
