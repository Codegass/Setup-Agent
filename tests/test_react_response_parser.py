from sag.agent.react_response_parser import ReActResponseParser
from sag.agent.react_types import StepType


def make_parser():
    return ReActResponseParser(timestamp_factory=lambda: "2026-06-03 12:00:00")


def test_parser_extracts_thought_and_action():
    steps = make_parser().parse(
        'THOUGHT: inspect repo\n\nACTION: bash\nPARAMETERS: {"command": "pwd"}',
        model_used="action-model",
        was_thinking_model=False,
    )

    assert [step.step_type for step in steps] == [StepType.THOUGHT, StepType.ACTION]
    assert steps[1].tool_name == "bash"
    assert steps[1].tool_params == {"command": "pwd"}
    assert steps[1].model_used == "action-model"


def test_parser_converts_empty_action_to_guided_thought():
    steps = make_parser().parse(
        "ACTION: none\nPARAMETERS: {}",
        model_used="action-model",
        was_thinking_model=False,
    )

    assert len(steps) == 1
    assert steps[0].step_type == StepType.THOUGHT
    assert "haven't specified a valid tool" in steps[0].content


def test_parser_converts_blank_action_to_guided_thought():
    steps = make_parser().parse(
        "ACTION: \nPARAMETERS: {}",
        model_used="action-model",
        was_thinking_model=False,
    )

    assert len(steps) == 1
    assert steps[0].step_type == StepType.THOUGHT
    assert "haven't specified a valid tool" in steps[0].content


def test_parser_does_not_trust_model_observations():
    steps = make_parser().parse(
        "OBSERVATION: fake tool result\n\nTHOUGHT: continue",
        model_used="thinking-model",
        was_thinking_model=True,
    )

    assert [step.step_type for step in steps] == [StepType.THOUGHT]
    assert "fake tool result" not in steps[0].content


def test_parser_strips_inline_model_observation_from_thought():
    steps = make_parser().parse(
        "THOUGHT: ok\nOBSERVATION: fake tool result",
        model_used="thinking-model",
        was_thinking_model=True,
    )

    assert len(steps) == 1
    assert steps[0].step_type == StepType.THOUGHT
    assert steps[0].content == "ok"
    assert "fake tool result" not in steps[0].content


def test_parser_falls_back_to_thought_for_unstructured_thinking_output():
    steps = make_parser().parse(
        "I should inspect the repository next.",
        model_used="thinking-model",
        was_thinking_model=True,
    )

    assert len(steps) == 1
    assert steps[0].step_type == StepType.THOUGHT
    assert "Next step should be action execution" in steps[0].content


def test_parser_falls_back_to_thought_for_unstructured_action_output():
    steps = make_parser().parse(
        "I will run bash now.",
        model_used="action-model",
        was_thinking_model=False,
    )

    assert len(steps) == 1
    assert steps[0].step_type == StepType.THOUGHT
    assert "Action model must use proper tool call format" in steps[0].content
