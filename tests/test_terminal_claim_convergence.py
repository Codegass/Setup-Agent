"""Integration law for judge-owned facts and model-owned repair actions."""

import json
from types import SimpleNamespace

import pytest
from test_container_io import FakeContainer

from sag.agent.action_intents import ActionIntent, EngineActionIntentFactory
from sag.agent.control_events import ControlEvent, ControlEventSink
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.invocation_contracts import clear_action_context, current_action_context
from sag.agent.loop_memory import LoopMemory
from sag.agent.phase_gates import (
    GateControlDisposition,
    ValidatorState,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import ReactLLMClient
from sag.agent.repair_contexts import REPAIR_CONTEXT_DIR
from sag.agent.replay import ReplayValidationError, recover_active_repair_context
from sag.agent.tool_orchestration import PreDispatchControlError, ToolCall, ToolExecution
from sag.tools.base import ToolResult


class _BuildAffordance:
    name = "build"

    @staticmethod
    def get_parameter_schema():
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["compile", "deps", "test"]},
                "working_directory": {"type": "string"},
            },
            "required": ["action"],
        }


class _MemorySink:
    def __init__(self):
        self.sequence = 0
        self.events = []

    def emit(self, kind, payload):
        sequence = self.sequence + 1
        event = ControlEvent(
            sequence=sequence,
            kind=kind,
            payload=payload,
            timestamp="2026-08-09T00:00:00Z",
            event_id=f"control-{sequence:06d}",
        )
        self.sequence = sequence
        self.events.append(event)
        return event


class _FailOpenedSink(_MemorySink):
    def emit(self, kind, payload):
        if kind == "repair_context_opened":
            raise OSError("disk full")
        return super().emit(kind, payload)


def _engine(*, sink=None):
    engine = ReActEngine.__new__(ReActEngine)
    engine.loop_memory = LoopMemory(completion_claim_cap=3)
    engine._pending_repair_context = None
    engine._last_invocation_contract_id = None
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.run_evidence_state = RunEvidenceState(run_id="convergence")
    engine.transition_policy = PhaseTransitionPolicy(repair_guard=engine.loop_memory)
    engine.orchestrator = FakeContainer()
    engine.tools = {"build": _BuildAffordance()}
    engine.successful_states = {"working_directory": "/workspace/demo"}
    engine.control_event_sink = sink if sink is not None else _MemorySink()
    engine.guidance = []
    engine._add_system_guidance = lambda text, priority=0: engine.guidance.append((priority, text))
    return engine


def _open_repair(engine, signal="done"):
    prepared = engine._prepare_rejected_completion(_rejected(signal))
    assert prepared is not None
    assert prepared.context is not None
    assert engine._pending_repair_context is None
    assert engine._apply_rejected_completion_control(prepared) is False
    return prepared


def _repair_call(context, *, action="deps", next_action_kind=None):
    return ToolCall(
        name="build",
        raw_params={"action": action},
        repair_intent_submission={
            "blocking_fact_refs": [context.trigger_assessment_id],
            "repair_hypothesis": "one bounded experiment may change the observed state",
            "next_action_kind": next_action_kind or action,
            "expected_observation": ["receipt_assessment"],
            "stop_condition": "stop after one terminal receipt",
        },
    )


def _rejected(signal="done"):
    claim = PhaseClaim(
        phase="build",
        signal=signal,
        claimed_outcome=PhaseOutcome.SUCCESS,
        reason="rewritten model prose",
        evidence_refs=("output_compile",),
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="compiler exited nonzero",
        evidence_refs=("output_compile",),
        code="compile_failed",
        validated_facts={"compiled_classes": 0, "fact_epoch": 7},
    )
    assert not gate.accepted
    assert gate.control_disposition.value == "repair_required"
    result = ToolResult.completed_failure(
        output="claim rejected",
        error="compiler exited nonzero",
        error_code="compile_failed",
        metadata={
            "phase_claim": claim.to_metadata(),
            "gate_result": gate.to_metadata(),
            "control_disposition": gate.control_disposition.value,
            "blocker_owner": gate.blocker_owner.value,
        },
    )
    call = ToolCall(name="phase", raw_params={"action": signal, "outcome": "success"})
    return ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        attempted_execution=True,
        observation_text="claim rejected",
    )


@pytest.mark.parametrize("has_identity", [True, False])
def test_build_phase_preserves_a_legacy_test_command_without_requiring_install(has_identity):
    engine = _engine()
    if has_identity:
        engine._active_native_tool_call_id = "call-ci-test"
    params = {
        "action": "test",
        "system": "maven",
        "working_directory": "/workspace/demo",
        "args": "-V --file pom.xml --no-transfer-progress",
        "source_command": "mvn -V test --file pom.xml --no-transfer-progress",
    }
    call = ToolCall(name="build", raw_params=params)

    if not has_identity:
        with pytest.raises(PreDispatchControlError) as exc:
            engine._prepare_control_action(call, params)
        assert exc.value.error_code == "ACTION_ENVELOPE_IDENTITY_MISSING"
        assert engine.control_event_sink.events == []
        return
    envelope = engine._prepare_control_action(call, params)

    assert envelope
    assert current_action_context().intent_exact_params == params
    assert engine.control_event_sink.events[-1].payload["exact_params"] == params
    assert engine.phase_machine.current_phase == "build"
    assert not engine.run_evidence_state.sealed


def test_rejected_gate_persists_facts_only_context_and_requires_model_fields():
    engine = _engine()

    prepared = engine._prepare_rejected_completion(_rejected())

    assert prepared is not None
    assert prepared[-1].recurrence_count == 1
    context = prepared.context
    assert context is not None
    assert engine._pending_repair_context is None
    assert engine.guidance == []
    assert engine._apply_rejected_completion_control(prepared) is False
    assert engine._pending_repair_context == context
    assert [event.kind for event in engine.control_event_sink.events[:3]] == [
        "validator_observation",
        "gate_decision",
        "repair_context_opened",
    ]
    assert context.blocker_owner == "project"
    assert context.typed_blocker == "compile_failed"
    assert {item.tool for item in context.allowed_tool_affordances} == {"build"}
    assert context.allowed_tool_affordances[0].action_parameter == "action"
    assert context.allowed_tool_affordances[0].action_kinds == ("compile", "deps", "test")
    persisted = engine.orchestrator.files[f"{REPAIR_CONTEXT_DIR}/{context.repair_context_id}.json"]
    assert "proposed_public_call" not in persisted
    assert '"argv"' not in persisted
    assert "repair_intent" in engine.guidance[-1][1]
    assessment_path = next(
        path for path in engine.orchestrator.files if path.startswith(f"{ASSESSMENT_DIR}/")
    )
    assessment = engine.orchestrator.files[assessment_path]
    assert '"observed_facts": {"compiled_classes": 0, "fact_epoch": 7}' in assessment
    source_gate = engine.control_event_sink.events[1].payload
    assert json.loads(assessment)["evidence_refs"] == [source_gate["decision_id"]]
    assert source_gate["evidence_refs"] == ["output_compile"]

    with pytest.raises(PreDispatchControlError) as exc:
        engine._mint_model_action_intent(
            ToolCall(name="build", raw_params={"action": "deps"}),
            {"action": "deps"},
        )
    assert exc.value.error_code == "REPAIR_INTENT_REQUIRED"


def test_rejected_completion_control_emits_exactly_one_gate_open_pair():
    engine = _engine()
    execution = _rejected()
    execution.result = execution.result.model_copy(
        update={
            "metadata": {
                **execution.result.metadata,
                "phase_signal": "done",
            }
        }
    )
    prepared = engine._prepare_rejected_completion(execution)

    assert prepared is not None
    assert engine._apply_rejected_completion_control(prepared) is False
    step = SimpleNamespace(tool_result=execution.result)
    assert engine._handle_phase_signals([step]) is None

    kinds = [event.kind for event in engine.control_event_sink.events]
    assert kinds.count("gate_decision") == 1
    assert kinds.count("repair_context_opened") == 1
    gate_index = kinds.index("gate_decision")
    assert kinds[gate_index + 1] == "repair_context_opened"
    assert recover_active_repair_context(engine.control_event_sink.events).context is not None


@pytest.mark.parametrize("tool_name", ["advisor", "manage_context", "report"])
def test_non_repair_tools_cannot_silently_bypass_an_active_context(tool_name):
    engine = _engine()
    _open_repair(engine)

    with pytest.raises(PreDispatchControlError) as exc:
        engine._mint_model_action_intent(
            ToolCall(name=tool_name, raw_params={}),
            {},
        )

    assert exc.value.error_code == "REPAIR_TOOL_NOT_ALLOWED"


def test_only_an_honest_terminal_phase_claim_bypasses_repair_affordances():
    engine = _engine()
    _open_repair(engine)

    intent = engine._mint_model_action_intent(
        ToolCall(name="phase", raw_params={"action": "blocked", "outcome": "failed"}),
        {"action": "blocked", "outcome": "failed"},
    )

    assert intent.tool == "phase"
    with pytest.raises(PreDispatchControlError) as exc:
        engine._mint_model_action_intent(
            ToolCall(name="phase", raw_params={"action": "note", "text": "still thinking"}),
            {"action": "note", "text": "still thinking"},
        )
    assert exc.value.error_code == "REPAIR_TOOL_NOT_ALLOWED"


def test_controller_terminal_action_cannot_use_the_model_terminal_exception():
    engine = _engine()
    prepared = _open_repair(engine)
    params = {"action": "done", "outcome": "failed"}
    intent = EngineActionIntentFactory.for_controller().from_submission(
        {
            "domain_id": prepared.context.domain_id,
            "tool": "phase",
            "params": params,
        },
        tool_call_id="controller-terminal",
    )
    call = ToolCall(name="phase", raw_params=params, action_intent=intent)

    with pytest.raises(PreDispatchControlError) as exc:
        engine._prepare_control_action(call, params)

    assert exc.value.error_code == "REPAIR_INTENT_LINEAGE_MISMATCH"
    assert exc.value.metadata["runner_dispatched"] is False


def test_complete_model_terminal_action_can_close_active_context_on_restart():
    engine = _engine()
    _open_repair(engine)
    engine._active_native_tool_call_id = "model-terminal"
    params = {"action": "blocked", "outcome": "failed"}
    call = ToolCall(name="phase", raw_params=params)
    envelope_id = engine._prepare_control_action(call, params)
    result = ToolResult.completed_success(output="terminal claim recorded")
    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-terminal",
        tool="phase",
        params=params,
        result=result,
    )
    with pytest.raises(ReplayValidationError, match="gate_decision"):
        recover_active_repair_context(engine.control_event_sink.events)
    claim = PhaseClaim(
        phase="build",
        signal="blocked",
        claimed_outcome=PhaseOutcome.FAILED,
        reason="the compiler remains red",
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="the compiler remains red",
        code="compile_failed",
    )
    assert gate.accepted
    engine._emit_control_gate(claim, gate)
    engine.control_event_sink.emit(
        "phase_transition",
        {
            "expected_kind": "flow_close",
            "expected_target": None,
            "expected_reason_code": "terminal_project_blocker",
            "repair_request": None,
        },
    )

    recovered = recover_active_repair_context(engine.control_event_sink.events)
    assert recovered.context is None
    assert recovered.consumed_context_ids == ()


def test_agent_no_progress_finalizer_gate_remains_linked_to_model_terminal_claim():
    engine = _engine()
    _open_repair(engine)

    def reject_again(call_id):
        execution = _rejected("done")
        engine._active_native_tool_call_id = call_id
        envelope_id = engine._prepare_control_action(
            execution.call,
            execution.call.raw_params,
        )
        prepared = engine._prepare_rejected_completion(execution)
        assert prepared is not None
        engine._emit_control_tool_result(
            envelope_id=envelope_id,
            execution_id=f"execution-{call_id}",
            tool="phase",
            params=execution.call.raw_params,
            result=execution.result,
        )
        return prepared

    second = reject_again("model-terminal-2")
    assert second.decision.recurrence_count == 2
    assert engine._apply_rejected_completion_control(second) is False
    assert recover_active_repair_context(engine.control_event_sink.events).context is not None

    third = reject_again("model-terminal-3")
    assert third.decision.decision == "agent_no_progress"
    engine._close_phase_for_agent_no_progress = lambda *_args, **_kwargs: False
    assert engine._apply_rejected_completion_control(third) is False
    with pytest.raises(ReplayValidationError, match="finalizer gate"):
        recover_active_repair_context(engine.control_event_sink.events)

    final_claim = PhaseClaim(
        phase="build",
        signal="done",
        claimed_outcome=third.gate.validated_outcome,
        reason="finalizer close after three no-op claims",
        evidence_refs=third.gate.evidence_refs,
    )
    final_gate = validate_phase_claim(
        final_claim,
        third.gate.validator_state,
        reason="agent_no_progress",
        evidence_refs=third.gate.evidence_refs,
        code="agent_no_progress",
        validated_facts=third.gate.validated_facts,
        control_disposition=GateControlDisposition.TERMINAL_CLAIMABLE,
        blocker_owner=third.gate.blocker_owner,
    )
    assert final_gate.accepted
    engine._emit_control_gate(final_claim, final_gate)
    engine.control_event_sink.emit(
        "phase_transition",
        {
            "expected_kind": "flow_close",
            "expected_target": None,
            "expected_reason_code": "agent_no_progress",
            "repair_request": None,
        },
    )

    recovered = recover_active_repair_context(engine.control_event_sink.events)
    assert recovered.context is None


def test_repair_next_action_kind_must_match_the_outer_public_action():
    engine = _engine()
    prepared = _open_repair(engine)

    with pytest.raises(PreDispatchControlError) as exc:
        engine._mint_model_action_intent(
            _repair_call(prepared.context, action="test", next_action_kind="deps"),
            {"action": "test"},
        )

    assert exc.value.error_code == "REPAIR_ACTION_AFFORDANCE_MISMATCH"


def test_live_repair_envelope_carries_exact_params_and_full_audit_lineage():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    long_arg = "x" * 700
    call = _repair_call(context, action="deps")
    params = {"action": "deps", "args": long_arg}
    engine._active_native_tool_call_id = "model-call-17"

    envelope_id = engine._prepare_control_action(call, params)

    event = engine.control_event_sink.events[-1]
    assert event.kind == "action_envelope"
    assert event.payload["envelope_id"] == envelope_id
    assert event.payload["exact_params"]["args"] == long_arg
    assert event.payload["repair_context_id"] == context.repair_context_id
    assert event.payload["repair_context_sha256"] == current_action_context().repair_context_sha256
    assert event.payload["next_action_kind"] == "deps"
    assert event.payload["repair_hypothesis"]
    clear_action_context()


def test_repair_action_without_a_durable_control_sink_fails_before_dispatch():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    engine.control_event_sink = None
    call = _repair_call(context)

    with pytest.raises(PreDispatchControlError) as exc:
        engine._prepare_control_action(call, {"action": "deps"})

    assert exc.value.error_code == "REPAIR_LINEAGE_RECORDING_REQUIRED"
    assert current_action_context().envelope_id is None


def test_done_blocked_and_rewritten_prose_share_the_three_claim_cap():
    engine = _engine()

    first = engine._prepare_rejected_completion(_rejected("done"))
    second = engine._prepare_rejected_completion(_rejected("blocked"))
    third = engine._prepare_rejected_completion(_rejected("done"))

    assert [item[-1].recurrence_count for item in (first, second, third)] == [1, 2, 3]
    assert third[-1].decision == "agent_no_progress"
    assert third[-1].close_phase is True


def test_different_dispatched_model_action_resets_completion_chain_even_when_it_fails():
    engine = _engine()
    first = _open_repair(engine)
    assert first[-1].recurrence_count == 1
    context = first.context
    assert context is not None

    call = ToolCall(
        name="build",
        raw_params={"action": "deps"},
        repair_intent_submission={
            "blocking_fact_refs": [context.trigger_assessment_id],
            "repair_hypothesis": "dependency resolution may change the compiler input",
            "next_action_kind": "deps",
            "expected_observation": ["receipt_assessment"],
            "stop_condition": "stop after one terminal dependency receipt",
        },
    )
    call.action_intent = engine._mint_model_action_intent(call, {"action": "deps"})
    failed = ToolResult.completed_failure(
        output="dependency resolution failed",
        error="repository unavailable",
        metadata={"runner_dispatched": True},
    )
    execution = ToolExecution(
        call=call,
        result=failed,
        status="failure",
        raw_params=call.raw_params,
        validated_params={"action": "deps"},
        attempted_execution=True,
        observation_text=failed.output,
    )

    assert engine._observe_action_intent_progress(execution) is True
    assert engine._pending_repair_context is None
    after_action = engine._prepare_rejected_completion(_rejected())
    assert after_action[-1].recurrence_count == 1


def test_predispatch_contract_refusal_keeps_the_active_context_even_with_wrapper_trace():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    call = _repair_call(context)
    call.action_intent = engine._mint_model_action_intent(call, {"action": "deps"})
    refused = ToolResult.completed_failure(
        output="no authority",
        error="no authority",
        error_code="CONTRACT_AUTHORITY_MISSING",
        metadata={"runner_dispatched": False},
    )
    execution = ToolExecution(
        call=call,
        result=refused,
        status="failure",
        raw_params=call.raw_params,
        validated_params={"action": "deps"},
        attempted_execution=True,
        actual_executions=[
            SimpleNamespace(
                tool_name="build",
                params={"action": "deps"},
                result=refused,
                execution_id="wrapper-refusal",
            )
        ],
        observation_text=refused.output,
    )

    assert engine._observe_action_intent_progress(execution) is False
    assert engine._pending_repair_context == context
    assert not getattr(engine, "_fatal_harness_control_failure", None)


def test_live_refusal_event_filters_wrapper_trace_and_replay_keeps_context_active():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    engine._active_native_tool_call_id = "model-call-refused"
    call = _repair_call(context)
    envelope_id = engine._prepare_control_action(call, {"action": "deps"})
    refused = ToolResult.completed_failure(
        output="no authority",
        error="no authority",
        error_code="CONTRACT_AUTHORITY_MISSING",
        metadata={"runner_dispatched": False},
    )
    wrapper = SimpleNamespace(
        tool_name="build",
        params={"action": "deps"},
        result=refused,
        execution_id="wrapper-refusal",
    )

    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-refused",
        tool="build",
        params={"action": "deps"},
        result=refused,
        actual_executions=[wrapper],
    )

    tool_event = engine.control_event_sink.events[-1]
    assert tool_event.payload["actual_executions"] == []
    assert current_action_context().envelope_id is None
    recovered = recover_active_repair_context(engine.control_event_sink.events)
    assert recovered.context == context


def test_unknown_repair_dispatch_telemetry_fails_closed_without_consuming_context():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    call = _repair_call(context)
    call.action_intent = engine._mint_model_action_intent(call, {"action": "deps"})
    unknown = ToolResult.completed_failure(output="unknown", error="unknown")
    execution = ToolExecution(
        call=call,
        result=unknown,
        status="failure",
        raw_params=call.raw_params,
        validated_params={"action": "deps"},
        attempted_execution=True,
        observation_text=unknown.output,
    )

    assert engine._observe_action_intent_progress(execution) is False
    assert engine._pending_repair_context == context
    assert engine._fatal_harness_control_failure == "repair_dispatch_evidence_unknown"


def test_unknown_build_facade_wrapper_is_not_physical_dispatch_evidence():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    call = _repair_call(context)
    call.action_intent = engine._mint_model_action_intent(call, {"action": "deps"})
    unknown = ToolResult.completed_failure(output="unknown", error="unknown")
    wrapper = SimpleNamespace(
        tool_name="build",
        params={"action": "deps"},
        result=unknown,
        execution_id="wrapper-unknown",
    )
    execution = ToolExecution(
        call=call,
        result=unknown,
        status="failure",
        raw_params=call.raw_params,
        validated_params={"action": "deps"},
        attempted_execution=True,
        actual_executions=[wrapper],
        observation_text=unknown.output,
    )

    assert engine._observe_action_intent_progress(execution) is False
    assert engine._pending_repair_context == context
    assert engine._fatal_harness_control_failure == "repair_dispatch_evidence_unknown"


def test_unknown_build_facade_wrapper_is_not_serialized_as_a_physical_leaf():
    engine = _engine()
    prepared = _open_repair(engine)
    context = prepared.context
    engine._active_native_tool_call_id = "model-call-unknown"
    call = _repair_call(context)
    envelope_id = engine._prepare_control_action(call, {"action": "deps"})
    unknown = ToolResult.completed_failure(output="unknown", error="unknown")
    wrapper = SimpleNamespace(
        tool_name="build",
        params={"action": "deps"},
        result=unknown,
        execution_id="wrapper-unknown",
    )

    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-unknown",
        tool="build",
        params={"action": "deps"},
        result=unknown,
        actual_executions=[wrapper],
    )

    tool_event = engine.control_event_sink.events[-1]
    assert tool_event.payload["actual_executions"] == []
    with pytest.raises(ReplayValidationError, match="no strict physical-dispatch"):
        recover_active_repair_context(engine.control_event_sink.events)


def test_identical_action_does_not_buy_another_completion_budget():
    engine = _engine()
    intent = EngineActionIntentFactory.for_model().from_submission(
        {"domain_id": "build:/workspace/demo", "tool": "build", "params": {"action": "deps"}}
    )
    assert engine.loop_memory.observe_material_action(intent)
    _open_repair(engine)

    call = ToolCall(name="build", raw_params={"action": "deps"}, action_intent=intent)
    result = ToolResult.completed_failure(
        output="same failure",
        error="same failure",
        metadata={"runner_dispatched": True},
    )
    execution = ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        attempted_execution=True,
        observation_text=result.output,
    )
    assert engine._observe_action_intent_progress(execution) is False
    second = engine._prepare_rejected_completion(_rejected())
    assert second[-1].recurrence_count == 2


def test_agent_no_progress_close_cannot_manufacture_success():
    engine = _engine()
    prepared = None
    for _ in range(3):
        prepared = engine._prepare_rejected_completion(_rejected())
    claim, gate, _, decision = prepared
    recorded = {}
    engine._emit_control_gate = lambda honest_claim, honest_gate, **_k: recorded.update(
        claim=honest_claim, gate=honest_gate
    )
    engine._record_gate_facts = lambda phase, honest_gate: None
    engine._apply_phase_decision = lambda record, route: recorded.update(record=record, route=route)

    assert engine._close_phase_for_agent_no_progress(claim, gate, decision)
    assert recorded["gate"].accepted
    assert recorded["gate"].validated_outcome is PhaseOutcome.FAILED
    assert recorded["record"].outcome is PhaseOutcome.FAILED
    assert all(
        blocker.error_code == "agent_no_progress" for blocker in engine.run_evidence_state.blockers
    )


def test_repair_context_changes_only_admissible_tool_schemas():
    engine = _engine()
    _open_repair(engine)
    client = ReactLLMClient.__new__(ReactLLMClient)
    client.repair_context_provider = lambda: engine._pending_repair_context

    build_schema = client._repair_aware_schema("build", _BuildAffordance.get_parameter_schema())
    phase_schema = client._repair_aware_schema(
        "phase", {"type": "object", "properties": {"action": {"type": "string"}}}
    )

    assert "repair_intent" in build_schema["required"]
    assert "repair_intent" in build_schema["properties"]
    repair_schema = build_schema["properties"]["repair_intent"]["properties"]
    assert repair_schema["next_action_kind"]["enum"] == ["compile", "deps", "test"]
    assert engine._pending_repair_context.trigger_assessment_id in (
        repair_schema["blocking_fact_refs"]["items"]["enum"]
    )
    assert repair_schema["expected_observation"]["items"]["enum"] == list(
        engine._pending_repair_context.admissible_observation_types
    )
    assert "repair_intent" not in phase_schema.get("properties", {})


def test_control_derived_gate_facts_receive_stable_replay_provenance():
    engine = _engine()
    events = []
    engine._emit_control_event = lambda kind, payload: events.append((kind, payload))
    claim = PhaseClaim(
        phase="build",
        signal="done",
        claimed_outcome=PhaseOutcome.FAILED,
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="no terminal build receipt",
        code="BUILD_ATTEMPT_REQUIRED",
        validated_facts={"terminal_build_receipts": 0},
    )

    engine._emit_control_gate(claim, gate)

    expected = "control:gate:build-1:BUILD_ATTEMPT_REQUIRED"
    assert [kind for kind, _ in events] == ["validator_observation", "gate_decision"]
    assert events[0][1]["evidence_refs"] == [expected]
    assert events[1][1]["evidence_refs"] == [expected]


def test_existing_action_intent_is_revalidated_before_dispatch():
    engine = _engine()
    valid = EngineActionIntentFactory.for_controller().from_submission(
        {
            "domain_id": "build:/workspace/demo",
            "tool": "build",
            "params": {"action": "deps"},
        }
    )
    object.__setattr__(valid, "action_fingerprint", "act-forged")
    forged = valid
    call = ToolCall(
        name="build",
        raw_params={"action": "deps"},
        action_intent=forged,
    )

    with pytest.raises(PreDispatchControlError) as exc:
        engine._prepare_control_action(call, {"action": "deps"})

    assert exc.value.error_code == "ACTION_INTENT_INVALID"
    assert exc.value.metadata["runner_dispatched"] is False


def test_existing_action_intent_rejects_a_forged_predecessor_before_dispatch():
    engine = _engine()
    intent = EngineActionIntentFactory.for_controller().from_submission(
        {
            "domain_id": "build:/workspace/demo",
            "tool": "build",
            "params": {"action": "deps"},
        },
        predecessor_contract_id="ic-000000000001",
    )
    object.__setattr__(intent, "predecessor_contract_id", "ic-forged")
    call = ToolCall(
        name="build",
        raw_params={"action": "deps"},
        action_intent=intent,
    )

    with pytest.raises(PreDispatchControlError) as exc:
        engine._prepare_control_action(call, {"action": "deps"})

    assert exc.value.error_code == "ACTION_INTENT_INVALID"
    assert exc.value.metadata["runner_dispatched"] is False


def test_existing_action_intent_must_bind_normalized_tool_params_and_domain():
    engine = _engine()
    intent = EngineActionIntentFactory.for_controller().from_submission(
        {
            "domain_id": "build:/workspace/other",
            "tool": "search",
            "params": {"action": "compile"},
        }
    )
    call = ToolCall(
        name="build",
        raw_params={"action": "deps"},
        action_intent=intent,
    )

    with pytest.raises(PreDispatchControlError) as exc:
        engine._prepare_control_action(call, {"action": "deps"})

    assert exc.value.error_code == "ACTION_INTENT_BINDING_MISMATCH"
    assert exc.value.metadata["runner_dispatched"] is False
    assert exc.value.metadata["mismatched_fields"] == [
        "tool",
        "domain_id",
        "canonical_params",
    ]


def test_repair_context_transport_failure_is_controller_owned_and_stops_the_batch():
    engine = _engine()
    engine._install_repair_context = lambda claim, gate: (
        None,
        "repair_context_persist_failed",
    )

    execution = _rejected()
    prepared = engine._prepare_rejected_completion(execution)

    assert prepared is not None
    _, gate, event, decision = prepared
    assert gate.control_disposition.value == "harness_recovery_required"
    assert gate.blocker_owner.value == "harness"
    assert gate.code == "repair_context_persist_failed"
    assert event.judge_disposition == "harness_recovery_required"
    assert decision.decision == "not_counted"
    assert execution.result.metadata["gate_result"]["blocker_owner"] == "harness"

    assert engine._apply_rejected_completion_control(prepared) is True
    assert engine._fatal_harness_control_failure == "repair_context_persist_failed"
    blocker = engine.run_evidence_state.blockers[-1]
    assert blocker.category == "harness_control"
    assert blocker.error_code == "repair_context_persist_failed"


def test_open_event_failure_never_activates_or_guides_the_model():
    engine = _engine(sink=_FailOpenedSink())
    prepared = engine._prepare_rejected_completion(_rejected())

    assert prepared.context is not None
    assert engine._apply_rejected_completion_control(prepared) is True
    assert engine._pending_repair_context is None
    assert engine.guidance == []
    assert engine._fatal_harness_control_failure == "repair_context_open_event_failed"


def test_restart_recovers_only_the_durable_open_context(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl", run_id="convergence")
    first = _engine(sink=sink)
    prepared = _open_repair(first)
    context = prepared.context

    restarted = _engine(sink=ControlEventSink(sink.path, run_id="convergence"))
    restarted._pending_repair_context = None
    restarted.guidance.clear()
    restarted._restore_active_repair_context()

    assert restarted._pending_repair_context == context
    assert context.repair_context_id in restarted.guidance[-1][1]


def test_engine_constructor_scopes_physical_validator_to_current_receipt_run(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class _Validator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("sag.agent.react_engine.PhysicalValidator", _Validator)
    context_manager = SimpleNamespace(
        contexts_dir=tmp_path,
        orchestrator=None,
        physical_validator=None,
    )

    engine = ReActEngine(
        context_manager,
        [],
        run_evidence_state=RunEvidenceState(run_id="receipt-run-current"),
        llm_client=SimpleNamespace(),
    )

    assert captured["receipt_run_id"] == "receipt-run-current"
    assert context_manager.physical_validator is engine.physical_validator


def test_no_op_convergence_in_the_test_phase_forces_the_floor_before_closing():
    """Live lp-commons-dbcp (2026-08-09): the TEST_ATTEMPT_REQUIRED gate told
    the model the controller would execute the registered phase-floor action,
    then the no-op cap closed the phase one second later with the promise
    unkept and zero tests executed. The convergence close must force the
    required attempt once; only its receipt or registered refusal may close."""

    from types import SimpleNamespace

    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="test")
    prepared = None
    for _ in range(3):
        prepared = engine._prepare_rejected_completion(_rejected_test())
    claim, gate, _, decision = prepared

    requirement = SimpleNamespace(action_text=lambda: "build(action='test')")
    forced = []
    engine._missing_required_test_attempt = lambda *_a, **_k: requirement
    engine._force_required_test_attempt = (
        lambda req, *, trigger: forced.append((req, trigger)) or True
    )
    engine._add_system_guidance = lambda *_a, **_k: None
    recorded = {}
    engine._emit_control_gate = lambda honest_claim, honest_gate, **_k: recorded.update(
        gate=honest_gate
    )
    engine._record_gate_facts = lambda phase, honest_gate: None
    engine._apply_phase_decision = lambda record, route: recorded.update(record=record)

    # First convergence: the floor runs, the phase does NOT close.
    assert engine._close_phase_for_agent_no_progress(claim, gate, decision) is False
    assert forced == [(requirement, "no_op_convergence")]
    assert "record" not in recorded

    # Second convergence with the same starved attempt: one shot spent —
    # the honest close proceeds instead of looping the promise forever.
    assert engine._close_phase_for_agent_no_progress(claim, gate, decision) is True
    assert len(forced) == 1
    assert recorded["gate"].accepted


def test_test_attempt_recovery_reaches_the_forced_floor_instead_of_aborting():
    """Live lp-dbcp-rates (2026-08-10): Maven verify had executed 1605
    tests, but the test phase still required its own runner receipt.  The
    rejected terminal claim is the handoff to the controller-owned forced
    action; it must not be promoted to a fatal harness failure before
    ``_execute_tool_step`` reaches that action."""

    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="test")
    execution = _rejected_test()
    claim = PhaseClaim.from_metadata(execution.result.metadata["phase_claim"])
    gate = validate_phase_claim(
        claim,
        ValidatorState.UNAVAILABLE,
        reason="one terminal test execution receipt is still required",
        evidence_refs=("output_verify",),
        code="TEST_ATTEMPT_REQUIRED",
        control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
    )
    execution.result = execution.result.model_copy(
        update={
            "error_code": "TEST_ATTEMPT_REQUIRED",
            "metadata": {
                **execution.result.metadata,
                "gate_result": gate.to_metadata(),
            },
        }
    )

    prepared = engine._prepare_rejected_completion(execution)

    assert prepared is not None
    assert prepared.event.judge_disposition == "harness_recovery_required"
    assert engine._apply_rejected_completion_control(prepared) is False
    assert not hasattr(engine, "_fatal_harness_control_failure")


def _rejected_test(signal="done"):
    claim = PhaseClaim(
        phase="test",
        signal=signal,
        claimed_outcome=PhaseOutcome.SUCCESS,
        reason="rewritten model prose",
        evidence_refs=("output_tests",),
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="no terminal test execution receipt",
        evidence_refs=("output_tests",),
        code="TEST_ATTEMPT_REQUIRED",
    )
    result = ToolResult.completed_failure(
        output=gate.reason,
        error=gate.reason,
        metadata={"phase_claim": claim.to_metadata(), "gate_result": gate.to_metadata()},
    )
    call = ToolCall(name="phase", raw_params={"action": signal})
    return ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        attempted_execution=True,
        observation_text=result.output,
    )
