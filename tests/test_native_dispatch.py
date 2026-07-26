# tests/test_native_dispatch.py
"""The dispatcher owns the pairing invariant: every tool_call id that enters
`self.steps` gets exactly one observation, even when a phase signal, a
refusal, or a loop force-break interrupts the batch (Plan 2 Task 4; anatomy
map risk 5 — Anthropic hard-400s on an unanswered tool_use)."""

from types import SimpleNamespace

import pytest

from sag.agent.native_messages import render_messages
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import ReActStep, StepType


def _turn(*calls, text="working"):
    return NativeTurn(text=text, tool_calls=tuple(calls), model_used="scripted-model")


def _call(index, name="bash", args=None):
    return NativeToolCall(
        id=f"call_{index}",
        name=name,
        arguments={"command": "ls"} if args is None else args,
        raw_arguments="{}",
    )


def _scripted_result(metadata):
    """A plain stand-in for ToolResult carrying only what the ACTION path reads."""
    return SimpleNamespace(
        succeeded=True,
        error_code=None,
        error=None,
        output="ok",
        output_ref=None,
        evidence_refs=[],
        refs=[],
        metadata=dict(metadata or {}),
        invocation_status=SimpleNamespace(value="completed"),
        operation_outcome=SimpleNamespace(value="succeeded"),
        evidence_status=SimpleNamespace(value="present"),
        evidence_assessment=SimpleNamespace(value="supported"),
        failure_signature=None,
        error_tail_preview=None,
    )


def _assert_pairing(messages):
    """Every emitted tool_call is answered by the message right after it."""
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        for call in message.get("tool_calls") or ():
            reply = messages[index]
            assert reply["role"] == "tool", f"call {call['id']} is unanswered"
            assert reply["tool_call_id"] == call["id"]
            index += 1


@pytest.fixture
def native_engine():
    """A `ReActEngine` with the tool-execution collaborators scripted.

    `results` maps a tool name to the metadata its result should carry;
    `loop_decisions` maps a tool name to the LoopDecision stand-in that
    `_apply_tool_execution_loop_effects` should hand back.
    """

    def _build(*, results=None, loop_decisions=None):
        engine = ReActEngine.__new__(ReActEngine)
        engine.steps = []
        engine.current_iteration = 3
        engine.config = SimpleNamespace(verbose=False)
        engine.agent_logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        engine.context_manager = SimpleNamespace(current_task_id=None)
        engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda name: None)
        engine.emit = lambda *a, **k: None
        # No scheduler and no sealed evidence: the real refusal guards run and
        # decline to refuse, so calls reach the scripted executor.
        engine._scheduler_active = False
        engine.reasoning_scheduler = None
        engine.run_evidence_state = None
        engine.phase_machine = None
        engine.loop_memory = None
        engine.output_storage = None
        engine.executed_calls = []
        engine.emitted_tool_results = []
        engine.closed_for_loop = []

        def fake_execute_tool_call(call):
            engine.executed_calls.append(call)
            return SimpleNamespace(
                call=call,
                result=_scripted_result((results or {}).get(call.name)),
                status="success",
                raw_params=call.raw_params,
                validated_params=dict(call.raw_params),
                observation_text=f"[{call.name or 'unnamed'}] observed",
                attempted_execution=True,
                metadata={},
                actual_executions=[],
            )

        def fake_add_observation_step(observation):
            step = ReActStep(
                step_type=StepType.OBSERVATION,
                content=observation,
                timestamp="scripted",
            )
            engine.steps.append(step)
            return step

        def fake_close_phase_for_loop(decision, execution):
            engine.closed_for_loop.append(execution.call.name)
            return True

        engine._execute_tool_call = fake_execute_tool_call
        engine._record_execution_bundle = lambda execution, call: (
            execution.result,
            "execution-1",
            [],
        )
        engine._emit_control_action_envelope = lambda tool, params: None
        engine._emit_control_tool_result = lambda **kwargs: engine.emitted_tool_results.append(
            kwargs
        )
        engine._apply_tool_execution_loop_effects = lambda execution: (loop_decisions or {}).get(
            execution.call.name
        )
        engine._close_phase_for_loop = fake_close_phase_for_loop
        engine._missing_required_test_attempt = lambda: None
        engine._add_observation_step = fake_add_observation_step
        return engine

    return _build


def _observations(engine):
    return [step for step in engine.steps if step.step_type == StepType.OBSERVATION]


def test_phase_signal_cancels_remaining_calls_with_observations(native_engine):
    engine = native_engine(results={"phase": {"phase_signal": "done"}})

    engine._execute_native_calls(_turn(_call(1, "phase"), _call(2, "bash")))

    observations = _observations(engine)
    assert [o.tool_call_id for o in observations] == ["call_1", "call_2"]
    assert "not executed" in observations[1].content
    assert [call.name for call in engine.executed_calls] == ["phase"]


def test_loop_force_break_cancels_the_rest_of_the_batch(native_engine):
    engine = native_engine(
        loop_decisions={
            "build": SimpleNamespace(
                close_phase=True, request_thinking=False, decision="force_break"
            )
        }
    )

    engine._execute_native_calls(_turn(_call(1, "build"), _call(2), _call(3)))

    observations = _observations(engine)
    assert [o.tool_call_id for o in observations] == ["call_1", "call_2", "call_3"]
    assert engine.closed_for_loop == ["build"]
    assert [call.name for call in engine.executed_calls] == ["build"]


def test_unnamed_tool_call_is_delivered_not_dropped(native_engine):
    engine = native_engine()
    unnamed = NativeToolCall(id="call_2", name="", arguments={}, raw_arguments="")

    executed = engine._execute_native_calls(_turn(_call(1), unnamed))

    # An empty name still reaches the orchestrator, which owns unknown-tool
    # feedback; dropping it would leave `call_2` forever unanswered.
    assert [call.name for call in engine.executed_calls] == ["bash", ""]
    assert [step.tool_call_id for step in executed] == ["call_1", "call_2"]
    assert [o.tool_call_id for o in _observations(engine)] == ["call_1", "call_2"]


def test_executed_batch_renders_as_paired_native_messages(native_engine):
    engine = native_engine(results={"phase": {"phase_signal": "done"}})

    engine._execute_native_calls(_turn(_call(1), _call(2, "phase"), _call(3), text="three moves"))

    messages = render_messages("SYS", engine.steps)
    _assert_pairing(messages)
    assistants = [m for m in messages if m["role"] == "assistant"]
    # Every call of the turn carries the same assistant prose provenance.
    assert {m["content"] for m in assistants} == {"three moves"}
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "call_1",
        "call_2",
        "call_3",
    ]
