from types import SimpleNamespace

import pytest

from sag.agent.react_types import ReactModelMode
from sag.config.models import LogLevel
from sag.config.settings import Config
from sag.tools.base import BaseTool, ToolResult


class ExampleTool(BaseTool):
    def __init__(self):
        super().__init__("example", "Example tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult.completed_success(output=command)


class FakeTokenTracker:
    def __init__(self):
        self.calls = []

    def track_token_usage(self, response, model, step_type):
        self.calls.append((response, model, step_type))


class FakeAgentLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class FakeVerboseLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


def make_config(**overrides):
    values = {
        "thinking_model": "gpt-5",
        "thinking_provider": "openai",
        "action_model": "claude-sonnet-4-6",
        "action_provider": "anthropic",
        "log_level": LogLevel.INFO,
    }
    values.update(overrides)
    return Config(**values)


def make_client(config=None, tools=None):
    from sag.agent.react_llm import ReactLLMClient

    return ReactLLMClient(
        config=config or make_config(),
        tools=tools or {"example": ExampleTool()},
        token_tracker=FakeTokenTracker(),
    )


def make_response(content="", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))
        ]
    )


def test_capabilities_are_resolved_per_mode_with_gpt5_thinking_and_claude_action(
    monkeypatch,
):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: "claude" in model)
    monkeypatch.setattr(
        "litellm.supports_parallel_function_calling", lambda model: "claude" not in model
    )
    client = make_client()

    thinking = client.capabilities_for(ReactModelMode.THINKING)
    action = client.capabilities_for(ReactModelMode.ACTION)

    assert thinking.model == "gpt-5"
    assert thinking.tool_call_format == "openai"
    assert thinking.supports_function_calling is False
    assert action.model == "anthropic/claude-sonnet-4-6"
    assert action.tool_call_format == "anthropic"
    assert action.supports_function_calling is True


def test_reverse_roles_do_not_hardcode_provider_to_role(monkeypatch):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: "gpt" in model)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    client = make_client(
        make_config(
            thinking_model="claude-sonnet-4-6",
            thinking_provider="anthropic",
            action_model="gpt-5",
            action_provider="openai",
        )
    )

    thinking = client.capabilities_for(ReactModelMode.THINKING)
    action = client.capabilities_for(ReactModelMode.ACTION)

    assert thinking.model == "anthropic/claude-sonnet-4-6"
    assert thinking.tool_call_format == "anthropic"
    assert thinking.supports_function_calling is False
    assert action.model == "gpt-5"
    assert action.tool_call_format == "openai"
    assert action.supports_function_calling is True


def test_setup_caches_capabilities_for_both_modes(monkeypatch):
    function_calling_checks = []
    parallel_checks = []

    def fake_supports_function_calling(model):
        function_calling_checks.append(model)
        return "claude" in model

    def fake_supports_parallel_function_calling(model):
        parallel_checks.append(model)
        return "gpt" in model

    monkeypatch.setattr("litellm.supports_function_calling", fake_supports_function_calling)
    monkeypatch.setattr(
        "litellm.supports_parallel_function_calling",
        fake_supports_parallel_function_calling,
    )
    client = make_client()

    client.setup()

    for _ in range(3):
        assert client.capabilities_for(ReactModelMode.THINKING).model == "gpt-5"
        assert client.capabilities_for(ReactModelMode.ACTION).model == "anthropic/claude-sonnet-4-6"

    assert function_calling_checks == ["gpt-5", "anthropic/claude-sonnet-4-6"]
    assert parallel_checks == ["gpt-5", "anthropic/claude-sonnet-4-6"]


def test_build_tools_schema_action_uses_action_model_format(monkeypatch):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    client = make_client(
        make_config(
            thinking_model="claude-sonnet-4-6",
            thinking_provider="anthropic",
            action_model="gpt-5",
            action_provider="openai",
        )
    )

    schema = client.build_tools_schema(ReactModelMode.ACTION)

    assert schema[0]["type"] == "function"
    assert schema[0]["function"]["name"] == "example"
    assert "command" in schema[0]["function"]["parameters"]["properties"]


def _verbose_client(monkeypatch, verbose_logger, agent_logger):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr(
        "sag.agent.react_llm.create_verbose_logger",
        lambda name: verbose_logger,
    )

    from sag.agent.react_llm import ReactLLMClient

    return ReactLLMClient(
        config=make_config(
            action_model="gpt-4o",
            action_provider="openai",
            verbose=True,
        ),
        tools={"example": ExampleTool()},
        token_tracker=FakeTokenTracker(),
        trace_context=lambda: {
            "iteration": 7,
            "timestamp": "2026-06-03 22:15:00",
            "agent_logger": agent_logger,
        },
    )


def test_native_turn_logs_trace_context_and_agent_response_length(monkeypatch):
    captured_verbose_logger = FakeVerboseLogger()
    agent_logger = FakeAgentLogger()
    monkeypatch.setattr("litellm.completion", lambda **params: make_response("all good"))
    client = _verbose_client(monkeypatch, captured_verbose_logger, agent_logger)

    turn = client.get_native_turn([{"role": "user", "content": "go"}])

    assert turn.text == "all good"
    assert agent_logger.messages == ["LLM Response from gpt-4o: 8 chars"]
    assert '"iteration": 7' in captured_verbose_logger.info_messages[0]
    assert '"timestamp": "2026-06-03 22:15:00"' in captured_verbose_logger.info_messages[0]


def test_native_turn_logs_then_propagates_a_provider_failure(monkeypatch):
    """The loop turns the raise into a typed abort naming the cause, so the
    client must not swallow it the way `get_response` used to."""
    captured_verbose_logger = FakeVerboseLogger()
    agent_logger = FakeAgentLogger()

    def explode(**params):
        raise RuntimeError("transport failed")

    monkeypatch.setattr("litellm.completion", explode)
    client = _verbose_client(monkeypatch, captured_verbose_logger, agent_logger)

    with pytest.raises(RuntimeError, match="transport failed"):
        client.get_native_turn([{"role": "user", "content": "go"}])

    assert any("llm_error" in message for message in captured_verbose_logger.error_messages)


