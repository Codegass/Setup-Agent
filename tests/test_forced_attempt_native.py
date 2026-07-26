# tests/test_forced_attempt_native.py
"""A harness-forced test attempt must be a well-formed native turn: the
synthetic ACTION step carries a forced tool_call_id and its observation
answers it (pairing invariant for harness-authored calls).

Anatomy map risk 4 — `_force_required_test_attempt` writes an ACTION step and
its observation straight into `self.steps` from five different triggers. In
the native protocol an unanswered assistant tool_use is a provider 400, so the
harness's own calls need ids exactly like the model's, without losing the
`forced_action` control event."""

from types import SimpleNamespace

import pytest

# Aliased away from the "Test" prefix so pytest does not try to collect the
# dataclasses as test classes.
from sag.agent.attempt_policy import TestAttemptRequirement as AttemptRequirement
from sag.agent.attempt_policy import TestCandidateResolution as CandidateResolution
from sag.agent.native_messages import render_messages
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import ToolExecution
from sag.tools.base import ToolResult


def _requirement():
    return AttemptRequirement(
        root="/workspace/demo",
        system="gradle",
        required_action={
            "tool": "build",
            "params": {"action": "test", "working_directory": "/workspace/demo"},
        },
    )


def _resolution(requirement):
    return CandidateResolution(
        status="available",
        candidates=(requirement,),
        project_root="/workspace/demo",
        workspace_root="/workspace",
        primary=requirement,
    )


@pytest.fixture
def forced_engine():
    def _build():
        requirement = _requirement()
        engine = ReActEngine.__new__(ReActEngine)
        engine.phase_machine = PhaseMachine(start_phase="test")
        engine.steps = []
        engine.tools = {}
        engine.current_iteration = 7
        engine.config = SimpleNamespace(verbose=False)
        engine.orchestrator = None
        engine._last_test_candidate_resolution = _resolution(requirement)
        engine._get_timestamp = lambda: "2026-07-26T00:00:00Z"
        engine.control_event_sink = SimpleNamespace(sequence=0)
        engine.control_events = []
        engine.executed_calls = []

        def emit_control_event(kind, payload):
            engine.control_events.append((kind, payload))
            engine.control_event_sink.sequence += 1
            return f"event-{engine.control_event_sink.sequence}"

        def execute(call):
            engine.executed_calls.append(call)
            result = ToolResult.completed_success(
                output="50 tests passed",
                metadata={
                    "command": "./gradlew test",
                    "runner_dispatched": True,
                    "exit_code": 0,
                },
            )
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
                timestamp="2026-07-26T00:00:00Z",
            )
            engine.steps.append(step)
            return step

        engine._emit_control_event = emit_control_event
        engine._execute_tool_call = execute
        # Receipt marking has its own suite; this one is about call identity.
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

    return _build


def test_forced_attempt_pairs_a_synthetic_call_with_its_observation(forced_engine):
    engine, requirement = forced_engine()

    assert engine._force_required_test_attempt(requirement, trigger="floor") is True

    action, observation = engine.steps
    assert action.step_type == StepType.ACTION
    assert action.tool_call_id == "forced-1"
    assert action.native_text == "[harness] executing the mandatory test attempt"
    assert observation.step_type == StepType.OBSERVATION
    assert observation.tool_call_id == "forced-1"


def test_forced_attempt_still_emits_the_forced_action_event(forced_engine):
    engine, requirement = forced_engine()

    engine._force_required_test_attempt(requirement, trigger="terminal_metadata")

    kinds = [kind for kind, _ in engine.control_events]
    assert "forced_action" in kinds
    payload = next(payload for kind, payload in engine.control_events if kind == "forced_action")
    assert payload["policy"] == "test_attempt_required"
    assert payload["trigger"] == "terminal_metadata"
    assert payload["tool"] == "build"
    assert payload["action_sha256"]


def test_forced_call_ids_are_unique_per_attempt(forced_engine):
    engine, requirement = forced_engine()

    engine._force_required_test_attempt(requirement, trigger="floor")
    engine._force_required_test_attempt(requirement, trigger="loop_close")

    actions = [step for step in engine.steps if step.step_type == StepType.ACTION]
    observations = [step for step in engine.steps if step.step_type == StepType.OBSERVATION]
    assert [step.tool_call_id for step in actions] == ["forced-1", "forced-2"]
    assert [step.tool_call_id for step in observations] == ["forced-1", "forced-2"]


def test_forced_attempt_renders_as_a_valid_native_exchange(forced_engine):
    engine, requirement = forced_engine()
    engine._force_required_test_attempt(requirement, trigger="floor")

    messages = render_messages("SYS", engine.steps)

    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "[harness] executing the mandatory test attempt"
    assert assistant["tool_calls"][0]["id"] == "forced-1"
    assert assistant["tool_calls"][0]["function"]["name"] == "build"
    reply = messages[2]
    assert reply == {
        "role": "tool",
        "tool_call_id": "forced-1",
        "content": "50 tests passed",
    }
    assert "cancelled by harness" not in str(messages)
