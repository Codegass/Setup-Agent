"""Strict bounded foundations for repair-context action lineage."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sag.agent.action_intents import (
    ACTION_INTENT_MAX_CANONICAL_BYTES,
    ACTION_INTENT_MAX_RAW_BYTES,
    ActionIntent,
    ActionIntentSubmission,
    EngineActionIntentFactory,
    action_fingerprint,
    bounded_exact_params,
    validate_repair_action_affordance,
)
from sag.agent.control_events import (
    CONTROL_EVENT_SCHEMA_VERSION,
    CONTROL_EVENT_MAX_RAW_BYTES,
    ActionEnvelopePayload,
    ControlEvent,
    ControlEventSink,
    action_envelope_sha256,
    canonical_sha256,
)
from sag.agent.evidence_assessments import assessment_id
from sag.agent.replay import (
    ControlReplayRunner,
    ReplayValidationError,
    recover_active_repair_context,
    recover_active_repair_context_from_path,
)
from sag.agent.repair_contexts import (
    REPAIR_CONTEXT_MAX_CANONICAL_BYTES,
    REPAIR_CONTEXT_MAX_RAW_BYTES,
    RepairContext,
    ToolSemanticAffordance,
    repair_context_identity,
    repair_context_sha256,
)


def _context_payload(**overrides):
    payload = {
        "repair_context_id": repair_context_identity("asm-gate-1"),
        "trigger_assessment_id": "asm-gate-1",
        "domain_id": "build:/workspace/repo",
        "typed_failure_or_capability": "tests_not_fully_executed",
        "blocker_owner": "project",
        "observed_fact_refs": ["asm-gate-1", "receipt-1"],
        "allowed_tool_affordances": [
            {
                "tool": "build",
                "action_parameter": "action",
                "action_kinds": ["build", "deps", "test"],
            }
        ],
        "admissible_observation_types": ["report_delta"],
    }
    payload.update(overrides)
    return payload


def _repair_submission(**overrides):
    payload = {
        "domain_id": "build:/workspace/repo",
        "tool": "build",
        "params": {"action": "test", "working_directory": "/workspace/repo"},
        "blocking_fact_refs": ["asm-gate-1"],
        "repair_hypothesis": "a bounded test can answer the blocker",
        "next_action_kind": "test",
        "expected_observation": ["report_delta"],
        "stop_condition": "stop after one terminal receipt",
    }
    payload.update(overrides)
    return payload


def test_repair_context_is_exactly_bounded_and_construct_cannot_bypass_validation():
    payload = _context_payload(typed_failure_or_capability="x" * 40_000)

    with pytest.raises(ValidationError):
        RepairContext.model_validate(payload)
    with pytest.raises(ValidationError):
        RepairContext.model_construct(**payload)

    valid = RepairContext.model_validate(_context_payload())
    canonical = json.dumps(
        valid.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert len(canonical) <= REPAIR_CONTEXT_MAX_CANONICAL_BYTES
    assert len(repair_context_sha256(valid)) == 64


def test_public_json_boundaries_run_before_duplicate_keys_can_collapse():
    context_json = json.dumps(_context_payload(), separators=(",", ":"))
    exact_context_json = context_json + " " * (
        REPAIR_CONTEXT_MAX_RAW_BYTES - len(context_json.encode("utf-8"))
    )
    assert RepairContext.model_validate_json(exact_context_json).repair_context_id
    with pytest.raises(ValueError, match="raw byte"):
        RepairContext.model_validate_json(exact_context_json + " ")
    duplicate_context_json = (
        context_json[:-1]
        + ',"observed_fact_refs":[]' * 4_000
        + "}"
    )
    assert len(duplicate_context_json.encode("utf-8")) > REPAIR_CONTEXT_MAX_RAW_BYTES
    with pytest.raises(ValueError, match="raw byte"):
        RepairContext.model_validate_json(duplicate_context_json)
    small_duplicate_context_json = context_json[:-1] + ',"blocker_owner":"harness"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        RepairContext.model_validate_json(small_duplicate_context_json)

    submission = _repair_submission()
    submission_json = json.dumps(submission, separators=(",", ":"))
    exact_submission_json = submission_json + " " * (
        ACTION_INTENT_MAX_RAW_BYTES - len(submission_json.encode("utf-8"))
    )
    assert ActionIntentSubmission.model_validate_json(exact_submission_json).tool == "build"
    with pytest.raises(ValueError, match="raw byte"):
        ActionIntentSubmission.model_validate_json(exact_submission_json + " ")
    duplicate_submission_json = submission_json[:-1] + ',"params":{}' * 6_000 + "}"
    assert len(duplicate_submission_json.encode("utf-8")) > ACTION_INTENT_MAX_RAW_BYTES
    with pytest.raises(ValueError, match="raw byte"):
        ActionIntentSubmission.model_validate_json(duplicate_submission_json)
    small_duplicate_submission_json = submission_json[:-1] + ',"tool":"search"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ActionIntentSubmission.model_validate_json(small_duplicate_submission_json)

    intent = EngineActionIntentFactory.for_model().from_submission(submission)
    intent_json = intent.model_dump_json()
    duplicate_intent_json = intent_json[:-1] + ',"next_action_kind":"test"' * 3_000 + "}"
    assert len(duplicate_intent_json.encode("utf-8")) > ACTION_INTENT_MAX_RAW_BYTES
    with pytest.raises(ValueError, match="raw byte"):
        ActionIntent.model_validate_json(duplicate_intent_json)
    small_duplicate_intent_json = intent_json[:-1] + ',"next_action_kind":"deps"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ActionIntent.model_validate_json(small_duplicate_intent_json)


def test_semantic_affordance_has_one_selector_vocabulary_and_normalizes_only_audit_kind():
    with pytest.raises(ValidationError):
        ToolSemanticAffordance.model_validate(
            {"tool": "maven", "action_parameter": "command", "action_kinds": []}
        )

    context = RepairContext.model_validate(_context_payload())
    intent = EngineActionIntentFactory.for_model().from_submission(
        _repair_submission(
            params={"action": " Test ", "working_directory": "/workspace/repo"},
            next_action_kind="test",
        ),
        trigger_assessment_id=context.trigger_assessment_id,
        repair_context_id=context.repair_context_id,
        repair_context_sha256=repair_context_sha256(context),
    )
    assert intent.next_action_kind == "test"
    assert intent.canonical_params["action"] == " Test "
    validate_repair_action_affordance(
        tool=intent.tool,
        params=intent.canonical_params,
        next_action_kind=intent.next_action_kind,
        context=context,
    )

    tool_kind_context = RepairContext.model_validate(
        _context_payload(
            allowed_tool_affordances=[
                {"tool": "search", "action_parameter": None, "action_kinds": []}
            ]
        )
    )
    validate_repair_action_affordance(
        tool="search",
        params={"path": "/workspace/repo"},
        next_action_kind=" SEARCH ",
        context=tool_kind_context,
    )
    with pytest.raises(ValueError, match="next_action_kind"):
        validate_repair_action_affordance(
            tool="search",
            params={"action": "test"},
            next_action_kind="test",
            context=tool_kind_context,
        )

    open_selector_context = RepairContext.model_validate(
        _context_payload(
            allowed_tool_affordances=[
                {"tool": "file_io", "action_parameter": "action", "action_kinds": []}
            ]
        )
    )
    with pytest.raises(ValueError, match="omits"):
        validate_repair_action_affordance(
            tool="file_io",
            params={"path": "/workspace/repo/file"},
            next_action_kind="write",
            context=open_selector_context,
        )
    validate_repair_action_affordance(
        tool="file_io",
        params={"action": " Write ", "path": "/workspace/repo/file"},
        next_action_kind="write",
        context=open_selector_context,
    )


def test_complete_intent_and_envelope_share_the_32k_canonical_ceiling():
    params = {
        "action": "test",
        "first": "x" * 14_000,
        "second": "y" * 14_000,
    }
    fingerprint = action_fingerprint(
        domain_id="build:/workspace/repo",
        tool="build",
        params=params,
    )
    intent_payload = {
        "intent_id": "intent-oversized",
        "source": "model",
        "domain_id": "build:/workspace/repo",
        "tool": "build",
        "canonical_params": params,
        "action_fingerprint": fingerprint,
        "trigger_assessment_id": "asm-gate-1",
        "repair_context_id": repair_context_identity("asm-gate-1"),
        "repair_context_sha256": "a" * 64,
        "blocking_fact_refs": ["asm-gate-1"],
        "repair_hypothesis": "h" * 2_500,
        "next_action_kind": "test",
        "expected_observation": ["report_delta"],
        "stop_condition": "s" * 2_500,
    }
    assert len(json.dumps(params, separators=(",", ":")).encode("utf-8")) < (
        ACTION_INTENT_MAX_CANONICAL_BYTES
    )
    with pytest.raises(ValidationError, match="canonical byte"):
        ActionIntent.model_validate(intent_payload)

    envelope_payload = {
        "envelope_id": "envelope-oversized",
        "tool_call_id": "call-oversized",
        "tool": "build",
        "exact_params": params,
        **{
            key: value
            for key, value in intent_payload.items()
            if key
            in {
                "intent_id",
                "source",
                "action_fingerprint",
                "trigger_assessment_id",
                "repair_context_id",
                "repair_context_sha256",
                "domain_id",
                "blocking_fact_refs",
                "repair_hypothesis",
                "next_action_kind",
                "expected_observation",
                "stop_condition",
            }
        },
    }
    envelope_payload["intent_source"] = envelope_payload.pop("source")
    envelope_payload["envelope_sha256"] = action_envelope_sha256(
        tool_call_id="call-oversized",
        tool="build",
        exact_params=params,
        **{
            key: envelope_payload[key]
            for key in (
                "intent_id",
                "intent_source",
                "action_fingerprint",
                "trigger_assessment_id",
                "repair_context_id",
                "repair_context_sha256",
                "domain_id",
                "blocking_fact_refs",
                "repair_hypothesis",
                "next_action_kind",
                "expected_observation",
                "stop_condition",
            )
        },
    )
    with pytest.raises(ValidationError, match="canonical byte"):
        ControlEvent(sequence=1, kind="action_envelope", payload=envelope_payload)


def test_repair_action_kind_is_explicit_and_matches_the_outer_canonical_call():
    context = RepairContext.model_validate(_context_payload())
    digest = repair_context_sha256(context)
    factory = EngineActionIntentFactory.for_model()

    with pytest.raises(ValueError, match="next_action_kind"):
        factory.from_submission(
            _repair_submission(next_action_kind="deps"),
            trigger_assessment_id=context.trigger_assessment_id,
            repair_context_id=context.repair_context_id,
            repair_context_sha256=digest,
        )
    with pytest.raises(ValueError, match="next_action_kind"):
        factory.from_submission(
            _repair_submission(next_action_kind=""),
            trigger_assessment_id=context.trigger_assessment_id,
            repair_context_id=context.repair_context_id,
            repair_context_sha256=digest,
        )

    intent = factory.from_submission(
        _repair_submission(),
        trigger_assessment_id=context.trigger_assessment_id,
        repair_context_id=context.repair_context_id,
        repair_context_sha256=digest,
    )
    assert intent.repair_context_sha256 == digest


def test_action_envelope_requires_a_complete_hash_bound_repair_audit():
    context = RepairContext.model_validate(_context_payload())
    digest = repair_context_sha256(context)
    intent = EngineActionIntentFactory.for_model().from_submission(
        _repair_submission(),
        tool_call_id="call-1",
        trigger_assessment_id=context.trigger_assessment_id,
        repair_context_id=context.repair_context_id,
        repair_context_sha256=digest,
    )
    params = dict(intent.canonical_params)
    audit = {
        "intent_id": intent.intent_id,
        "intent_source": intent.source,
        "action_fingerprint": intent.action_fingerprint,
        "domain_id": intent.domain_id,
        "trigger_assessment_id": intent.trigger_assessment_id,
        "repair_context_id": intent.repair_context_id,
        "repair_context_sha256": intent.repair_context_sha256,
        "blocking_fact_refs": list(intent.blocking_fact_refs),
        "repair_hypothesis": intent.repair_hypothesis,
        "next_action_kind": intent.next_action_kind,
        "expected_observation": list(intent.expected_observation),
        "stop_condition": intent.stop_condition,
    }
    payload = {
        "envelope_id": "envelope-1",
        "tool_call_id": "call-1",
        "tool": "build",
        "exact_params": params,
        **audit,
    }
    payload["envelope_sha256"] = action_envelope_sha256(
        tool_call_id="call-1",
        tool="build",
        exact_params=params,
        **audit,
    )

    assert CONTROL_EVENT_SCHEMA_VERSION == 5
    event = ControlEvent(sequence=1, kind="action_envelope", payload=payload)
    assert event.payload["repair_context_sha256"] == digest

    forged = dict(payload)
    forged.pop("blocking_fact_refs")
    forged["envelope_sha256"] = action_envelope_sha256(
        tool_call_id="call-1",
        tool="build",
        exact_params=params,
        **{key: value for key, value in audit.items() if key != "blocking_fact_refs"},
    )
    with pytest.raises(ValidationError, match="repair"):
        ControlEvent(sequence=1, kind="action_envelope", payload=forged)


def test_action_envelope_sink_preserves_exact_params_and_bounds_raw_event_json(tmp_path):
    params = {"command": "x" * 1_024}
    payload = {
        "envelope_id": "envelope-exact-1",
        "plan_index": 0,
        "tool": "maven",
        "exact_params": params,
        "envelope_sha256": action_envelope_sha256(
            plan_index=0,
            tool="maven",
            exact_params=params,
        ),
    }
    path = tmp_path / "control.jsonl"
    emitted = ControlEventSink(path).emit("action_envelope", payload)
    assert emitted.payload["exact_params"] == params
    recorded = ControlEvent.model_validate_json(path.read_text(encoding="utf-8"))
    assert recorded.payload["exact_params"]["command"] == "x" * 1_024

    event_json = emitted.model_dump_json()
    duplicate_event_json = event_json[:-1] + ',"sequence":1' * 21_000 + "}"
    assert len(duplicate_event_json.encode("utf-8")) > CONTROL_EVENT_MAX_RAW_BYTES
    with pytest.raises(ValueError, match="raw byte"):
        ControlEvent.model_validate_json(duplicate_event_json)


def test_exact_params_reject_key_normalization_collisions_and_nested_duplicate_json():
    with pytest.raises(ValueError, match="whitespace-canonical"):
        bounded_exact_params({"a": 1, " a": 2})

    params = {"outer": {"a": 1}, "ordered": ["second", "first"]}
    payload = {
        "envelope_id": "envelope-exact-roundtrip",
        "plan_index": 0,
        "tool": "maven",
        "exact_params": params,
        "envelope_sha256": action_envelope_sha256(
            plan_index=0,
            tool="maven",
            exact_params=params,
        ),
    }
    envelope = ActionEnvelopePayload.model_validate(payload)
    wire = envelope.model_dump_json()
    restored = ActionEnvelopePayload.model_validate_json(wire)
    assert restored.exact_params == params
    assert restored.envelope_sha256 == envelope.envelope_sha256

    duplicate_nested = wire.replace(
        '"outer":{"a":1}',
        '"outer":{"a":1,"a":2}',
    )
    assert duplicate_nested != wire
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ActionEnvelopePayload.model_validate_json(duplicate_nested)

    event = ControlEvent(sequence=1, kind="action_envelope", payload=payload)
    duplicate_event = event.model_dump_json().replace(
        '"outer":{"a":1}',
        '"outer":{"a":1,"a":2}',
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ControlEvent.model_validate_json(duplicate_event)


def test_tool_result_outer_and_actual_params_share_the_exact_32k_boundary():
    base = {
        "envelope_id": "envelope-exact-result",
        "execution_id": "execution-1",
        "tool": "build",
        "params": {"action": "test"},
        "scope": "test_runtime",
        "result": {},
        "actual_executions": [],
    }
    with pytest.raises(ValidationError, match="whitespace-canonical"):
        ControlEvent(sequence=2, kind="tool_result", payload={**base, "params": {" a": 1}})

    oversized = {f"arg_{index}": "x" * 4_000 for index in range(9)}
    with pytest.raises(ValidationError, match="canonical byte limit"):
        ControlEvent(
            sequence=2,
            kind="tool_result",
            payload={**base, "params": oversized},
        )

    nested = {
        **base,
        "actual_executions": [
            {
                "execution_id": "actual-1",
                "tool": "maven",
                "params": {" command": "verify"},
                "scope": "test_runtime",
                "result": {},
            }
        ],
    }
    with pytest.raises(ValidationError, match="whitespace-canonical"):
        ControlEvent(sequence=2, kind="tool_result", payload=nested)


def test_repair_context_opened_event_carries_the_full_bounded_projection():
    context = RepairContext.model_validate(_context_payload())
    event = ControlEvent(
        sequence=2,
        kind="repair_context_opened",
        payload={
            "source_gate_sequence": 1,
            "source_phase_attempt_id": "build-1",
            "context": context.model_dump(mode="json"),
            "context_sha256": repair_context_sha256(context),
        },
    )

    assert event.payload["context"]["repair_context_id"] == context.repair_context_id
    forged = dict(event.payload)
    forged["context_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="hash"):
        ControlEvent(sequence=2, kind="repair_context_opened", payload=forged)


def _v3_repair_rows():
    fixture = Path(__file__).parent / "fixtures" / "control_layer" / "paramiko.jsonl"
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    rows[0]["schema_version"] = 3
    for row in rows[1:]:
        if row.get("kind") == "gate_decision":
            row["payload"]["code"] = "fixture_gate"
            row["payload"]["source_attempt_id"] = (
                "test-1" if row["payload"]["phase"] == "test" else "build-1"
            )
            if row["payload"]["phase"] == "test":
                row["payload"]["test_candidate_resolution"] = {
                    "status": "available",
                    "workspace_root": "/workspace",
                    "project_root": "/workspace/paramiko",
                    "candidates": [
                        {"root": "/workspace/paramiko", "system": "python"}
                    ],
                }
        if (
            row.get("kind") == "tool_result"
            and row["payload"].get("source_attempt_id") == "test-1"
            and row["payload"].get("tool") == "build"
            and row["payload"].get("params", {}).get("action") == "test"
        ):
            row["payload"]["result"].setdefault("facts", {})["system"] = "python"
            row["payload"]["result"].setdefault("metadata", {}).update(
                {"runner_dispatched": True, "command": "pytest"}
            )

    gate = {
        "kind": "gate_decision",
        "payload": {
            "phase": "build",
            "signal": "done",
            "claimed_outcome": "success",
            "validator_state": "red",
            "expected_accepted": False,
            "expected_outcome": "failed",
            "control_disposition": "repair_required",
            "blocker_owner": "project",
            "code": "build_failed",
            "reason": "the build receipt is red",
            "key_results": "claimed success",
            "evidence_refs": ["receipt-1"],
            "validated_facts": {},
            "source_attempt_id": "build-1",
        },
    }
    subject = {
        "phase_attempt_id": "build-1",
        "phase": "build",
        "validator_state": "red",
        "validated_outcome": "failed",
        "control_disposition": "repair_required",
        "blocker_owner": "project",
        "code": "build_failed",
        "validated_facts": {},
        "evidence_refs": ["receipt-1"],
    }
    trigger = assessment_id("gate-" + canonical_sha256(subject)[:16], "build_failed")
    context = RepairContext.model_validate(
        _context_payload(
            repair_context_id=repair_context_identity(trigger),
            trigger_assessment_id=trigger,
            domain_id="build:/workspace/paramiko",
            typed_failure_or_capability="build_failed",
            observed_fact_refs=[trigger, "receipt-1"],
        )
    )
    digest = repair_context_sha256(context)
    opened = {
        "kind": "repair_context_opened",
        "payload": {
            # Filled after insertion/renumbering below.
            "source_gate_sequence": 0,
            "source_phase_attempt_id": "build-1",
            "context": context.model_dump(mode="json"),
            "context_sha256": digest,
        },
    }
    first_envelope_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "action_envelope"
    )
    gate["source"] = dict(rows[first_envelope_index]["source"])
    opened["source"] = dict(rows[first_envelope_index]["source"])
    rows[first_envelope_index:first_envelope_index] = [gate, opened]
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    opened["payload"]["source_gate_sequence"] = gate["sequence"]

    envelope = next(
        row for row in rows if row.get("kind") == "action_envelope" and row["sequence"] > opened["sequence"]
    )
    params = envelope["payload"]["exact_params"]
    intent = EngineActionIntentFactory.for_model().from_submission(
        {
            "domain_id": context.domain_id,
            "tool": envelope["payload"]["tool"],
            "params": params,
            "blocking_fact_refs": [trigger],
            "repair_hypothesis": "dependency resolution may unblock the build",
            "next_action_kind": params["action"],
            "expected_observation": ["report_delta"],
            "stop_condition": "stop after this terminal tool result",
        },
        tool_call_id=str(envelope["payload"].get("tool_call_id") or ""),
        trigger_assessment_id=trigger,
        repair_context_id=context.repair_context_id,
        repair_context_sha256=digest,
    )
    audit = {
        "intent_id": intent.intent_id,
        "intent_source": intent.source,
        "action_fingerprint": intent.action_fingerprint,
        "domain_id": intent.domain_id,
        "trigger_assessment_id": intent.trigger_assessment_id,
        "repair_context_id": intent.repair_context_id,
        "repair_context_sha256": intent.repair_context_sha256,
        "blocking_fact_refs": list(intent.blocking_fact_refs),
        "repair_hypothesis": intent.repair_hypothesis,
        "next_action_kind": intent.next_action_kind,
        "expected_observation": list(intent.expected_observation),
        "stop_condition": intent.stop_condition,
    }
    envelope["payload"].update(audit)
    envelope["payload"]["envelope_sha256"] = action_envelope_sha256(
        plan_index=envelope["payload"].get("plan_index"),
        tool_call_id=envelope["payload"].get("tool_call_id"),
        tool=envelope["payload"]["tool"],
        exact_params=params,
        **audit,
    )
    linked_result = next(
        row
        for row in rows
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == envelope["payload"]["envelope_id"]
    )
    linked_result["payload"]["result"].setdefault("metadata", {})[
        "runner_dispatched"
    ] = True
    return rows


def _write_rows(path, rows):
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_v3_offline_replay_consumes_one_hash_bound_repair_context(tmp_path):
    transcript = tmp_path / "repair-v3.jsonl"
    _write_rows(transcript, _v3_repair_rows())

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.unconsumed_events == ()


def _strict_events(rows):
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    return [ControlEvent.model_validate(row) for row in rows[1:]]


def test_restart_projection_consumes_once_but_keeps_predispatch_refusal_active(tmp_path):
    rows = _v3_repair_rows()
    linked_envelope = next(
        row
        for row in rows
        if row.get("kind") == "action_envelope"
        and row["payload"].get("repair_context_id")
    )
    result_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == linked_envelope["payload"]["envelope_id"]
    )
    refused_rows = rows[: result_index + 1]
    refused_result = refused_rows[-1]["payload"]["result"]
    refused_result.setdefault("metadata", {})["runner_dispatched"] = False

    state = recover_active_repair_context(_strict_events(refused_rows))
    assert state.context is not None
    assert state.context.repair_context_id == linked_envelope["payload"]["repair_context_id"]
    assert state.consumed_context_ids == ()

    path = tmp_path / "live-control.jsonl"
    path.write_text(
        "".join(event.model_dump_json() + "\n" for event in _strict_events(refused_rows)),
        encoding="utf-8",
    )
    restored = recover_active_repair_context_from_path(path)
    assert restored.context_sha256 == state.context_sha256

    consumed = recover_active_repair_context(_strict_events(_v3_repair_rows()))
    assert consumed.context is None
    assert consumed.consumed_context_ids == (linked_envelope["payload"]["repair_context_id"],)


def test_restart_projection_cannot_clear_an_active_context_on_a_bare_transition():
    rows = _v3_repair_rows()
    opened_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "repair_context_opened"
    )
    transition = next(row for row in rows if row.get("kind") == "phase_transition")

    with pytest.raises(ReplayValidationError, match="active repair context"):
        recover_active_repair_context(
            _strict_events(rows[: opened_index + 1] + [transition])
        )


@pytest.mark.parametrize("intent_source", [None, "controller"])
def test_restart_projection_terminal_exception_requires_a_complete_model_intent(
    intent_source,
    tmp_path,
):
    rows = _v3_repair_rows()
    opened_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "repair_context_opened"
    )
    template_envelope = next(row for row in rows if row.get("kind") == "action_envelope")
    template_result = next(row for row in rows if row.get("kind") == "tool_result")
    envelope = json.loads(json.dumps(template_envelope))
    result = json.loads(json.dumps(template_result))
    payload = envelope["payload"]
    params = {"action": "done", "outcome": "failed"}
    payload.update(
        {
            "envelope_id": "terminal-exception",
            "tool_call_id": "terminal-exception-call",
            "tool": "phase",
            "exact_params": params,
        }
    )
    for key in (
        "intent_id",
        "intent_source",
        "action_fingerprint",
        "trigger_assessment_id",
        "repair_context_id",
        "repair_context_sha256",
        "domain_id",
        "blocking_fact_refs",
        "repair_hypothesis",
        "next_action_kind",
        "expected_observation",
        "stop_condition",
    ):
        payload.pop(key, None)
    if intent_source is not None:
        payload.update(
            {
                "intent_id": "intent-controller-terminal",
                "intent_source": intent_source,
                "action_fingerprint": action_fingerprint(
                    domain_id="phase:/workspace/repo",
                    tool="phase",
                    params=params,
                ),
            }
        )
    payload["envelope_sha256"] = action_envelope_sha256(
        plan_index=payload.get("plan_index"),
        tool_call_id=payload["tool_call_id"],
        tool="phase",
        exact_params=params,
        intent_id=payload.get("intent_id"),
        intent_source=payload.get("intent_source"),
        action_fingerprint=payload.get("action_fingerprint"),
    )
    result["payload"].update(
        {
            "envelope_id": payload["envelope_id"],
            "tool": "phase",
            "params": params,
        }
    )

    attempted = rows[: opened_index + 1] + [envelope, result]
    with pytest.raises(ReplayValidationError, match="terminal|repair action"):
        recover_active_repair_context(_strict_events(attempted))

    transcript = tmp_path / f"terminal-exception-{intent_source or 'legacy'}.jsonl"
    _write_rows(transcript, attempted)
    with pytest.raises(ReplayValidationError, match="terminal|repair action"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_restart_and_offline_replay_accept_the_same_complete_model_terminal_chain(
    tmp_path,
):
    rows = _v3_repair_rows()
    opened_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "repair_context_opened"
    )
    validator_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "validator_observation"
        and row["payload"].get("phase") == "build"
    )
    template_envelope = next(row for row in rows if row.get("kind") == "action_envelope")
    template_result = next(row for row in rows if row.get("kind") == "tool_result")
    envelope = json.loads(json.dumps(template_envelope))
    result = json.loads(json.dumps(template_result))
    params = {"action": "done", "outcome": "success"}
    intent = EngineActionIntentFactory.for_model().from_submission(
        {
            "domain_id": "phase:/workspace/paramiko",
            "tool": "phase",
            "params": params,
        },
        tool_call_id="model-terminal-chain",
    )
    payload = envelope["payload"]
    payload.update(
        {
            "envelope_id": "terminal-model-chain",
            "tool_call_id": "model-terminal-chain",
            "tool": "phase",
            "exact_params": params,
            "intent_id": intent.intent_id,
            "intent_source": intent.source,
            "action_fingerprint": intent.action_fingerprint,
        }
    )
    for key in (
        "trigger_assessment_id",
        "repair_context_id",
        "repair_context_sha256",
        "domain_id",
        "blocking_fact_refs",
        "repair_hypothesis",
        "next_action_kind",
        "expected_observation",
        "stop_condition",
    ):
        payload.pop(key, None)
    payload["envelope_sha256"] = action_envelope_sha256(
        plan_index=payload.get("plan_index"),
        tool_call_id=payload["tool_call_id"],
        tool="phase",
        exact_params=params,
        intent_id=intent.intent_id,
        intent_source=intent.source,
        action_fingerprint=intent.action_fingerprint,
    )
    result["payload"].update(
        {
            "envelope_id": payload["envelope_id"],
            "tool": "phase",
            "params": params,
        }
    )
    attempted = (
        rows[: opened_index + 1]
        + [envelope, result]
        + rows[validator_index:]
    )

    state = recover_active_repair_context(_strict_events(attempted))
    assert state.context is None
    assert state.consumed_context_ids == ()

    transcript = tmp_path / "terminal-model-chain.jsonl"
    _write_rows(transcript, attempted)
    replayed = ControlReplayRunner.offline(verify_expected=False).run(transcript)
    assert replayed.unconsumed_events == ()


def test_restart_projection_enforces_monotone_sequences_and_single_answer_identity():
    rows = _v3_repair_rows()
    events = _strict_events(rows)
    duplicate_sequence = events[1].model_copy(update={"sequence": events[0].sequence})
    with pytest.raises(ReplayValidationError, match="monotonic"):
        recover_active_repair_context([events[0], duplicate_sequence])

    rows = _v3_repair_rows()
    envelope_indices = [
        index for index, row in enumerate(rows) if row.get("kind") == "action_envelope"
    ]
    first = rows[envelope_indices[0]]["payload"]
    second = rows[envelope_indices[1]]["payload"]
    old_second_id = second["envelope_id"]
    second["envelope_id"] = first["envelope_id"]
    second_result = next(
        row
        for row in rows
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == old_second_id
    )
    second_result["payload"]["envelope_id"] = first["envelope_id"]

    with pytest.raises(ReplayValidationError, match="already"):
        recover_active_repair_context(_strict_events(rows))


def test_restart_projection_keeps_typed_contract_refusal_active_without_guessing():
    rows = _v3_repair_rows()
    linked_envelope = next(
        row
        for row in rows
        if row.get("kind") == "action_envelope"
        and row["payload"].get("repair_context_id")
    )
    result_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == linked_envelope["payload"]["envelope_id"]
    )
    result = rows[result_index]["payload"]["result"]
    result.get("metadata", {}).pop("runner_dispatched", None)
    result["error_code"] = "CONTRACT_AUTHORITY_MISSING"

    state = recover_active_repair_context(_strict_events(rows[: result_index + 1]))
    assert state.context is not None
    assert state.consumed_context_ids == ()


def test_restart_projection_rejects_missing_open_after_repair_gate_at_action_or_eof():
    rows = _v3_repair_rows()
    opened_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "repair_context_opened"
    )
    without_opened = rows[:opened_index] + rows[opened_index + 1 :]
    action_index = next(
        index
        for index, row in enumerate(without_opened)
        if row.get("kind") == "action_envelope"
    )

    with pytest.raises(ReplayValidationError, match="no repair_context_opened"):
        recover_active_repair_context(_strict_events(without_opened[: action_index + 1]))
    with pytest.raises(ReplayValidationError, match="ended before repair_context_opened"):
        recover_active_repair_context(_strict_events(rows[:opened_index]))


@pytest.mark.parametrize("next_kind", ["gate_decision", "phase_transition", "evidence_close"])
def test_restart_projection_rejects_control_progress_before_pending_context_opens(
    next_kind,
):
    rows = _v3_repair_rows()
    gate_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "gate_decision"
        and row["payload"].get("control_disposition") == "repair_required"
    )
    # Copy because the gate case intentionally reuses the same source row; the
    # restart projector now also verifies sequence monotonicity, so aliasing one
    # dict into two positions would mutate both sequence fields at once.
    candidate = json.loads(
        json.dumps(next(row for row in rows if row.get("kind") == next_kind))
    )
    attempted = [rows[0], rows[gate_index], candidate]

    with pytest.raises(ReplayValidationError, match="no repair_context_opened"):
        recover_active_repair_context(_strict_events(attempted))


def test_offline_replay_rejects_eof_with_a_pending_repair_gate(tmp_path):
    rows = _v3_repair_rows()
    opened_index = next(
        index for index, row in enumerate(rows) if row.get("kind") == "repair_context_opened"
    )
    transcript = tmp_path / "repair-v3-pending-gate-eof.jsonl"
    _write_rows(transcript, rows[:opened_index])

    with pytest.raises(ReplayValidationError, match="ended before repair_context_opened"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_restart_and_offline_replay_reject_unknown_repair_dispatch_state(tmp_path):
    rows = _v3_repair_rows()
    linked_envelope = next(
        row
        for row in rows
        if row.get("kind") == "action_envelope"
        and row["payload"].get("repair_context_id")
    )
    result_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == linked_envelope["payload"]["envelope_id"]
    )
    result = rows[result_index]["payload"]["result"]
    result.get("metadata", {}).pop("runner_dispatched", None)

    with pytest.raises(ReplayValidationError, match="no strict physical-dispatch"):
        recover_active_repair_context(_strict_events(rows[: result_index + 1]))

    transcript = tmp_path / "repair-v3-unknown-dispatch.jsonl"
    _write_rows(transcript, rows)
    with pytest.raises(ReplayValidationError, match="no strict physical-dispatch"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize("external_ref", [False, True])
def test_repair_refs_include_constraint_and_selected_affordance_provenance(external_ref):
    rows = _v3_repair_rows()
    opened = next(row for row in rows if row.get("kind") == "repair_context_opened")
    context_payload = opened["payload"]["context"]
    context_payload["constraint_set"] = {
        "source_refs": ["constraint-set-ref"],
        "constraints": [
            {
                "constraint_id": "build-scope",
                "kind": "scope",
                "subject": "project",
                "relation": "within",
                "value": "/workspace/paramiko",
                "source_refs": ["constraint-ref"],
            }
        ],
    }
    context_payload["allowed_tool_affordances"][0]["constraint_refs"] = [
        "affordance-ref"
    ]
    context = RepairContext.model_validate(context_payload)
    digest = repair_context_sha256(context)
    opened["payload"]["context"] = context.model_dump(mode="json")
    opened["payload"]["context_sha256"] = digest

    envelope = next(
        row
        for row in rows
        if row.get("kind") == "action_envelope"
        and row["payload"].get("repair_context_id")
    )["payload"]
    envelope["repair_context_sha256"] = digest
    envelope["blocking_fact_refs"] = [
        "constraint-set-ref",
        "constraint-ref",
        "affordance-ref",
    ]
    if external_ref:
        envelope["blocking_fact_refs"].append("outside-ref")
    envelope["envelope_sha256"] = action_envelope_sha256(
        plan_index=envelope.get("plan_index"),
        tool_call_id=envelope.get("tool_call_id"),
        tool=envelope["tool"],
        exact_params=envelope["exact_params"],
        **{
            key: envelope[key]
            for key in (
                "intent_id",
                "intent_source",
                "action_fingerprint",
                "trigger_assessment_id",
                "repair_context_id",
                "repair_context_sha256",
                "domain_id",
                "blocking_fact_refs",
                "repair_hypothesis",
                "next_action_kind",
                "expected_observation",
                "stop_condition",
            )
        },
    )

    events = _strict_events(rows)
    if external_ref:
        with pytest.raises(ReplayValidationError, match="outside the opened context"):
            recover_active_repair_context(events)
    else:
        state = recover_active_repair_context(events)
        assert state.context is None


def test_restart_projection_rejects_reuse_after_a_context_was_consumed():
    rows = _v3_repair_rows()
    linked_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "action_envelope"
        and row["payload"].get("repair_context_id")
    )
    result_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == rows[linked_index]["payload"]["envelope_id"]
    )
    second_index = next(
        index
        for index, row in enumerate(rows[result_index + 1 :], result_index + 1)
        if row.get("kind") == "action_envelope"
    )
    first = rows[linked_index]["payload"]
    second = rows[second_index]["payload"]
    for key in (
        "intent_id",
        "intent_source",
        "trigger_assessment_id",
        "repair_context_id",
        "repair_context_sha256",
        "domain_id",
        "blocking_fact_refs",
        "repair_hypothesis",
        "expected_observation",
        "stop_condition",
    ):
        second[key] = first[key]
    second["next_action_kind"] = second["exact_params"]["action"]
    second["action_fingerprint"] = action_fingerprint(
        domain_id=second["domain_id"],
        tool=second["tool"],
        params=second["exact_params"],
    )
    second["envelope_sha256"] = action_envelope_sha256(
        plan_index=second.get("plan_index"),
        tool_call_id=second.get("tool_call_id"),
        tool=second["tool"],
        exact_params=second["exact_params"],
        **{
            key: second[key]
            for key in (
                "intent_id",
                "intent_source",
                "action_fingerprint",
                "trigger_assessment_id",
                "repair_context_id",
                "repair_context_sha256",
                "domain_id",
                "blocking_fact_refs",
                "repair_hypothesis",
                "next_action_kind",
                "expected_observation",
                "stop_condition",
            )
        },
    )

    with pytest.raises(ReplayValidationError, match="no active"):
        recover_active_repair_context(_strict_events(rows[: second_index + 1]))


@pytest.mark.parametrize("mutation", ["missing_context", "forged_affordance"])
def test_v3_offline_replay_rejects_missing_or_forged_repair_context(tmp_path, mutation):
    rows = _v3_repair_rows()
    if mutation == "missing_context":
        rows = [row for row in rows if row.get("kind") != "repair_context_opened"]
    else:
        opened = next(row for row in rows if row.get("kind") == "repair_context_opened")
        opened["payload"]["context"]["allowed_tool_affordances"] = [
            {"tool": "search", "action_parameter": None, "action_kinds": []}
        ]
        forged_context = RepairContext.model_validate(opened["payload"]["context"])
        opened["payload"]["context_sha256"] = repair_context_sha256(forged_context)
    transcript = tmp_path / f"repair-v3-{mutation}.jsonl"
    _write_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match="repair"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_legacy_replay_is_accepted_only_when_it_contains_no_repair_lineage(tmp_path):
    rows = _v3_repair_rows()
    rows[0]["schema_version"] = 2
    rows = [row for row in rows if row.get("kind") != "repair_context_opened"]
    transcript = tmp_path / "repair-v2-unverifiable.jsonl"
    _write_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match="explicitly unverifiable"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)
