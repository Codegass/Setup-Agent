"""Native multi-turn tool-calling API (spec §3.1): messages array in,
structured turn out, tool_call ids preserved (the old path discarded them —
anatomy map §4.2)."""

import json
from types import SimpleNamespace

import pytest

from sag.agent.react_llm import NativeToolCall, NativeTurn, ReactLLMClient
from sag.config.models import LogLevel
from sag.config.settings import Config
from sag.tools.base import BaseTool, ToolResult


class ProjectTool(BaseTool):
    def __init__(self):
        super().__init__("project", "Project tool")

    def execute(self, action: str, repo_url: str = "") -> ToolResult:
        return ToolResult.completed_success(output=f"{action}:{repo_url}")


class FakeTokenTracker:
    def __init__(self):
        self.calls = []

    def track_token_usage(self, response, model, step_type):
        self.calls.append((response, model, step_type))


def make_config(**overrides):
    values = {
        "thinking_model": "gpt-5",
        "thinking_provider": "openai",
        "action_model": "gpt-4o-mini",
        "action_provider": "openai",
        "log_level": LogLevel.INFO,
    }
    values.update(overrides)
    return Config(**values)


def make_client(config=None, tools=None, token_tracker=None):
    return ReactLLMClient(
        config=config or make_config(),
        tools={"project": ProjectTool()} if tools is None else tools,
        token_tracker=FakeTokenTracker() if token_tracker is None else token_tracker,
    )


@pytest.fixture(autouse=True)
def _native_capability_probes(monkeypatch):
    """Every model in this file natively supports function calling."""
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: True)


def fake_completion(monkeypatch, message, model="gpt-4o-mini"):
    captured = {}

    def _completion(**params):
        captured.update(params)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model=params.get("model", model),
        )

    monkeypatch.setattr("litellm.completion", _completion)
    return captured


def openai_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_native_turn_preserves_tool_call_ids(monkeypatch):
    arguments = json.dumps({"action": "clone", "repo_url": "https://x"})
    message = SimpleNamespace(
        content="Cloning now.",
        tool_calls=[openai_tool_call("call_abc123", "project", arguments)],
    )
    captured = fake_completion(monkeypatch, message)
    client = make_client()

    turn = client.get_native_turn(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    )

    assert isinstance(turn, NativeTurn)
    assert turn.text == "Cloning now."
    assert turn.tool_calls == (
        NativeToolCall(
            id="call_abc123",
            name="project",
            arguments={"action": "clone", "repo_url": "https://x"},
            raw_arguments=arguments,
        ),
    )
    assert turn.model_used == "gpt-4o-mini"
    # the full messages array went out unmodified, with tools attached
    assert captured["messages"][0]["role"] == "system"
    assert len(captured["messages"]) == 2
    assert captured["tools"], "tools schema must be attached"
    assert captured["tool_choice"] == "auto"


def test_textless_toolless_turn_is_normalized(monkeypatch):
    fake_completion(monkeypatch, SimpleNamespace(content=None, tool_calls=None))

    turn = make_client().get_native_turn([{"role": "user", "content": "go"}])

    assert turn.text == ""
    assert turn.tool_calls == ()


def test_malformed_arguments_yield_empty_dict_with_raw(monkeypatch):
    message = SimpleNamespace(
        content="",
        tool_calls=[openai_tool_call("", "bash", "{not json")],
    )
    fake_completion(monkeypatch, message)

    turn = make_client().get_native_turn([{"role": "user", "content": "go"}])

    call = turn.tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == "{not json"
    assert call.id.startswith("call_")  # synthesized id — never empty


def test_non_object_arguments_are_rejected_but_raw_is_kept(monkeypatch):
    message = SimpleNamespace(
        content="",
        tool_calls=[openai_tool_call("call_1", "bash", '"just a string"')],
    )
    fake_completion(monkeypatch, message)

    call = make_client().get_native_turn([{"role": "user", "content": "go"}]).tool_calls[0]

    assert call.arguments == {}
    assert call.raw_arguments == '"just a string"'


def test_messages_array_is_copied_not_aliased(monkeypatch):
    captured = fake_completion(monkeypatch, SimpleNamespace(content="ok", tool_calls=None))
    messages = [{"role": "user", "content": "go"}]

    make_client().get_native_turn(messages)

    assert captured["messages"] == messages
    assert captured["messages"] is not messages


def test_functions_namespace_prefix_is_stripped_from_tool_name(monkeypatch):
    """Parity with the retired flattener (react_llm.py:368) — some models emit
    `functions.<tool>` and the dispatcher looks the bare name up in self.tools."""
    message = SimpleNamespace(
        content=None,
        tool_calls=[openai_tool_call("call_1", "functions.project", "{}")],
    )
    fake_completion(monkeypatch, message)

    turn = make_client().get_native_turn([{"role": "user", "content": "go"}])

    assert turn.tool_calls[0].name == "project"


def test_anthropic_target_uses_object_tool_choice_and_native_call_shape(monkeypatch):
    """litellm normalizes anthropic to the OpenAI wire shape, but the retired
    extractor also read the native `name`/`input` pair — keep that fallback."""
    message = SimpleNamespace(
        content="Cloning.",
        tool_calls=[
            SimpleNamespace(
                id="toolu_01",
                type="tool_use",
                name="project",
                input={"action": "clone"},
            )
        ],
    )
    captured = fake_completion(monkeypatch, message)
    client = make_client(make_config(action_model="claude-sonnet-4-6", action_provider="anthropic"))

    turn = client.get_native_turn([{"role": "user", "content": "go"}])

    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["tool_choice"] == {"type": "auto"}
    assert captured["tools"][0]["name"] == "project"  # anthropic tool_def shape
    assert turn.tool_calls[0] == NativeToolCall(
        id="toolu_01",
        name="project",
        arguments={"action": "clone"},
        raw_arguments=json.dumps({"action": "clone"}),
    )


def test_include_tools_false_sends_a_toolless_request(monkeypatch):
    captured = fake_completion(monkeypatch, SimpleNamespace(content="thinking", tool_calls=None))

    make_client().get_native_turn([{"role": "user", "content": "go"}], include_tools=False)

    assert "tools" not in captured
    assert "tool_choice" not in captured


def test_prompt_format_models_get_no_native_tools(monkeypatch):
    captured = fake_completion(monkeypatch, SimpleNamespace(content="hi", tool_calls=None))
    client = make_client(make_config(action_provider="unknown", action_model="opaque-model"))

    client.get_native_turn([{"role": "user", "content": "go"}])

    assert "tools" not in captured


def test_token_usage_is_tracked_under_the_executor_label(monkeypatch):
    fake_completion(monkeypatch, SimpleNamespace(content="ok", tool_calls=None))
    tracker = FakeTokenTracker()

    make_client(token_tracker=tracker).get_native_turn([{"role": "user", "content": "go"}])

    assert [(model, label) for _, model, label in tracker.calls] == [("gpt-4o-mini", "executor")]


def test_gpt5_action_model_with_tools_keeps_traditional_params(monkeypatch):
    """Mirrors _build_request_params: a GPT-5 action model carrying a tools
    schema drops reasoning_effort and uses temperature/max_tokens."""
    captured = fake_completion(monkeypatch, SimpleNamespace(content="ok", tool_calls=None))
    client = make_client(make_config(action_model="gpt-5", action_provider="openai"))

    client.get_native_turn([{"role": "user", "content": "go"}])

    assert "reasoning_effort" not in captured
    assert captured["drop_params"] is True
    assert captured["temperature"] == client.config.action_temperature
    assert captured["max_tokens"] == client.config.action_max_tokens


def test_gpt5_action_model_without_tools_uses_reasoning_effort(monkeypatch):
    captured = fake_completion(monkeypatch, SimpleNamespace(content="ok", tool_calls=None))
    client = make_client(make_config(action_model="gpt-5", action_provider="openai"))

    client.get_native_turn([{"role": "user", "content": "go"}], include_tools=False)

    assert captured["reasoning_effort"] == client.config.gpt5_reasoning_effort
    assert captured["drop_params"] is True
    assert "temperature" not in captured


def test_ollama_targets_carry_the_configured_api_base(monkeypatch):
    captured = fake_completion(monkeypatch, SimpleNamespace(content="ok", tool_calls=None))
    client = make_client(
        make_config(
            action_provider="ollama",
            action_model="qwen3",
            ollama_base_url="http://localhost:11434",
        )
    )

    client.get_native_turn([{"role": "user", "content": "go"}])

    assert captured["api_base"] == "http://localhost:11434"
