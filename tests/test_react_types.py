from sag.agent import agent_state_evaluator
from sag.agent.react_engine import ReActStep as EngineReActStep
from sag.agent.react_engine import StepType as EngineStepType
from sag.agent.react_types import (
    ReactModelCapabilities,
    ReactModelMode,
    ReActStep,
    StepType,
)


def test_react_engine_reexports_shared_step_types():
    assert EngineReActStep is ReActStep
    assert EngineStepType is StepType


def test_agent_state_evaluator_uses_shared_step_type():
    assert agent_state_evaluator.StepType is StepType


def test_react_model_capabilities_are_per_mode():
    capabilities = ReactModelCapabilities(
        mode=ReactModelMode.ACTION,
        model="anthropic/claude-4.6",
        supports_function_calling=True,
        supports_parallel_function_calling=False,
        tool_call_format="anthropic",
    )

    assert capabilities.mode == ReactModelMode.ACTION
    assert capabilities.tool_call_format == "anthropic"
