# tests/test_native_messages.py
"""Steps→messages renderer: pairing invariant + tail-preserving clamps
(spec §3.1; anatomy map risks 5 and 7).

Adaptation vs. the plan sketch: the real ``ReActStep`` is a pydantic model
whose parameter field is ``tool_params`` (not ``tool_parameters``) and whose
``timestamp`` is required, so the helpers below pass it explicitly.
"""

import json

from sag.agent.native_messages import render_messages
from sag.agent.react_types import ReActStep, StepType


def _action(tool, params, call_id, text=""):
    step = ReActStep(
        step_type=StepType.ACTION,
        content=f"{tool}",
        tool_name=tool,
        timestamp="ts",
    )
    step.tool_params = params
    step.tool_call_id = call_id
    step.native_text = text
    return step


def _observation(content, call_id=None):
    step = ReActStep(step_type=StepType.OBSERVATION, content=content, timestamp="ts")
    step.tool_call_id = call_id
    return step


def _guidance(content):
    return ReActStep(step_type=StepType.SYSTEM_GUIDANCE, content=content, timestamp="ts")


def test_action_and_observation_render_as_paired_native_turns():
    steps = [
        _guidance("=== PHASE: BUILD ==="),
        _action("build", {"action": "compile"}, "call_1", text="Compiling now."),
        _observation("BUILD SUCCESS", call_id="call_1"),
    ]
    messages = render_messages("SYS", steps)
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1] == {"role": "user", "content": "=== PHASE: BUILD ==="}
    assistant = messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Compiling now."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["tool_calls"][0]["function"]["name"] == "build"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"action": "compile"}
    tool = messages[3]
    assert tool == {"role": "tool", "tool_call_id": "call_1", "content": "BUILD SUCCESS"}
    assert len(messages) == 4


def test_unanswered_call_gets_synthetic_cancellation():
    steps = [_action("bash", {"command": "ls"}, "call_9")]
    messages = render_messages("SYS", steps)
    replies = [m for m in messages if m["role"] == "tool"]
    assert len(replies) == 1
    assert replies[0]["tool_call_id"] == "call_9"
    assert "cancelled by harness" in replies[0]["content"]
    # the repair lands immediately after the assistant turn that opened the call
    assert messages[-2]["role"] == "assistant"
    assert messages[-1] is replies[0]


def test_failure_clamp_preserves_the_tail():
    body = "HEAD" + ("x" * 9000) + "FATAL: the real error"
    steps = [
        _action("build", {"action": "test"}, "call_2"),
        _observation(body, call_id="call_2"),
    ]
    messages = render_messages("SYS", steps)
    content = [m for m in messages if m["role"] == "tool"][0]["content"]
    assert len(content) < 5200
    assert content.startswith("HEAD")
    assert content.endswith("FATAL: the real error")
    assert "chars omitted" in content


def test_short_observation_is_not_clamped():
    steps = [
        _action("bash", {"command": "ls"}, "call_3"),
        _observation("ok", call_id="call_3"),
    ]
    messages = render_messages("SYS", steps)
    assert [m for m in messages if m["role"] == "tool"][0]["content"] == "ok"


def test_consecutive_calls_from_one_turn_render_as_one_assistant_message():
    steps = [
        _action("bash", {"command": "ls"}, "call_a", text="Two things."),
        _action("bash", {"command": "pwd"}, "call_b", text="Two things."),
        _observation("a", call_id="call_a"),
        _observation("b", call_id="call_b"),
    ]
    messages = render_messages("SYS", steps)
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "Two things."
    assert [c["id"] for c in assistants[0]["tool_calls"]] == ["call_a", "call_b"]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "call_a",
        "call_b",
    ]


def test_actions_from_different_turns_do_not_merge():
    steps = [
        _action("bash", {"command": "ls"}, "call_a", text="first turn"),
        _action("bash", {"command": "pwd"}, "call_b", text="second turn"),
    ]
    messages = render_messages("SYS", steps)
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert [m["content"] for m in assistants] == ["first turn", "second turn"]
    # both unanswered calls still get exactly one cancellation each
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "call_a",
        "call_b",
    ]


def test_action_without_tool_call_id_gets_a_synthetic_id():
    step = _action("bash", {"command": "ls"}, None)
    messages = render_messages("SYS", [step])
    assistant = [m for m in messages if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["id"] == "synthetic-1"
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["synthetic-1"]


def test_assistant_without_prose_carries_null_content():
    messages = render_messages("SYS", [_action("bash", {"command": "ls"}, "call_1")])
    assistant = [m for m in messages if m["role"] == "assistant"][0]
    assert assistant["content"] is None


def test_thought_and_guidance_steps_render_as_prose_turns():
    steps = [
        _guidance("phase intro"),
        ReActStep(step_type=StepType.THOUGHT, content="thinking", timestamp="ts"),
    ]
    messages = render_messages("SYS", steps)
    assert messages[1] == {"role": "user", "content": "phase intro"}
    assert messages[2] == {"role": "assistant", "content": "thinking"}


def test_legacy_observation_without_id_renders_as_a_user_message():
    steps = [_observation("legacy output")]
    messages = render_messages("SYS", steps)
    assert messages[1] == {"role": "user", "content": "[observation] legacy output"}
    assert not [m for m in messages if m["role"] == "tool"]


def test_orphan_tool_reply_is_dropped():
    """An observation whose id no assistant turn ever opened would 400 on
    Anthropic; the renderer drops it rather than emitting it."""
    steps = [_observation("stray", call_id="call_missing")]
    messages = render_messages("SYS", steps)
    assert [m["role"] for m in messages] == ["system"]


def test_duplicate_replies_for_one_call_collapse_to_one():
    steps = [
        _action("bash", {"command": "ls"}, "call_1"),
        _observation("first", call_id="call_1"),
        _observation("second", call_id="call_1"),
    ]
    messages = render_messages("SYS", steps)
    replies = [m for m in messages if m["role"] == "tool"]
    assert len(replies) == 1
    assert replies[0]["content"] == "first"


def test_empty_step_window_renders_only_the_system_prompt():
    assert render_messages("SYS", []) == [{"role": "system", "content": "SYS"}]


def test_every_emitted_call_id_has_exactly_one_reply():
    """The pairing invariant, asserted structurally over a mixed window."""
    steps = [
        _guidance("intro"),
        _action("phase", {"action": "note"}, "call_1", text="noting"),
        _observation("noted", call_id="call_1"),
        _action("bash", {"command": "ls"}, "call_2", text="listing"),
        _action("bash", {"command": "pwd"}, "call_3", text="listing"),
        _observation("files", call_id="call_2"),
        _guidance("a nudge arrived before call_3 answered"),
    ]
    messages = render_messages("SYS", steps)
    opened = [
        call["id"]
        for m in messages
        if m["role"] == "assistant"
        for call in m.get("tool_calls") or ()
    ]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert sorted(opened) == sorted(answered) == ["call_1", "call_2", "call_3"]
