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
        return ToolResult(success=True, output=command)


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


def test_get_response_does_not_wrap_mode_prompt_again(monkeypatch):
    captured = {}

    def fake_completion(**params):
        captured.update(params)
        return make_response("THOUGHT: ok")

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.completion", fake_completion)
    client = make_client()

    response = client.get_response(
        "ACTION MODEL INSTRUCTIONS\nbase prompt",
        ReactModelMode.ACTION,
    )

    assert response == "THOUGHT: ok"
    assert captured["messages"][0]["content"].count("ACTION MODEL INSTRUCTIONS") == 1


def test_get_response_logs_trace_context_and_agent_response_length(monkeypatch):
    captured_verbose_logger = FakeVerboseLogger()
    agent_logger = FakeAgentLogger()

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.completion", lambda **params: make_response("THOUGHT: ok"))
    monkeypatch.setattr(
        "sag.agent.react_llm.create_verbose_logger",
        lambda name: captured_verbose_logger,
    )

    from sag.agent.react_llm import ReactLLMClient

    client = ReactLLMClient(
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

    response = client.get_response("wrapped prompt", ReactModelMode.ACTION)

    assert response == "THOUGHT: ok"
    assert agent_logger.messages == ["LLM Response from gpt-4o: 11 chars"]
    assert '"iteration": 7' in captured_verbose_logger.info_messages[0]
    assert '"timestamp": "2026-06-03 22:15:00"' in captured_verbose_logger.info_messages[0]


def test_json_function_call_content_fallback_preserves_tool_command_format(monkeypatch):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr(
        "litellm.completion",
        lambda **params: make_response('{"tool":"example","command":"pwd"}'),
    )
    client = make_client()

    response = client.get_response("wrapped prompt", ReactModelMode.ACTION)

    assert response == 'ACTION: example\nPARAMETERS: {"command": "pwd"}'


def test_prompt_tool_call_format_does_not_attach_native_tools(monkeypatch):
    captured = {}

    def fake_completion(**params):
        captured.update(params)
        return make_response('{"tool":"example","command":"pwd"}')

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.completion", fake_completion)
    client = make_client(
        make_config(
            action_provider="unknown",
            action_model="opaque-model",
        )
    )

    response = client.get_response("wrapped prompt", ReactModelMode.ACTION)
    capabilities = client.capabilities_for(ReactModelMode.ACTION)

    assert capabilities.tool_call_format == "prompt"
    assert capabilities.supports_function_calling is False
    assert "tools" not in captured
    assert "tool_choice" not in captured
    assert response == '{"tool":"example","command":"pwd"}'


def test_openai_tool_call_response_normalizes_to_react_and_strips_functions_prefix(
    monkeypatch,
):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="functions.example", arguments='{"command":"pwd"}')
    )
    monkeypatch.setattr(
        "litellm.completion",
        lambda **params: make_response("run command", [tool_call]),
    )
    client = make_client(
        make_config(action_model="gpt-5", action_provider="openai"),
    )

    response = client.get_response("wrapped prompt", ReactModelMode.ACTION)

    assert response == 'THOUGHT: run command\n\nACTION: example\n\nPARAMETERS: {"command": "pwd"}'


def test_claude_tool_call_response_normalizes_to_react_text(monkeypatch):
    monkeypatch.setattr("litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    tool_call = {"name": "example", "input": {"command": "pwd"}}
    monkeypatch.setattr(
        "litellm.completion",
        lambda **params: make_response("run command", [tool_call]),
    )
    client = make_client()

    response = client.get_response("wrapped prompt", ReactModelMode.ACTION)

    assert response == 'THOUGHT: run command\n\nACTION: example\n\nPARAMETERS: {"command": "pwd"}'


def test_gpt5_request_falls_back_to_traditional_params_with_drop_params(monkeypatch):
    calls = []

    def fake_completion(**params):
        calls.append(params)
        if len(calls) == 1:
            raise RuntimeError("reasoning params rejected")
        return make_response("THOUGHT: fallback")

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.completion", fake_completion)
    client = make_client(make_config(thinking_model="gpt-5", thinking_provider="openai"))

    response = client.get_response("wrapped prompt", ReactModelMode.THINKING)

    assert response == "THOUGHT: fallback"
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[0]["drop_params"] is True
    assert "temperature" not in calls[0]
    assert calls[1]["temperature"] == pytest.approx(0.1)
    assert calls[1]["max_tokens"] == 16000
    assert calls[1]["drop_params"] is True


def test_ollama_action_request_includes_api_base(monkeypatch):
    captured = {}

    def fake_completion(**params):
        captured.update(params)
        return make_response("THOUGHT: ok")

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.completion", fake_completion)
    client = make_client(
        make_config(
            action_provider="ollama",
            action_model="llama3.1",
            ollama_base_url="http://ollama.test:11434",
        )
    )

    client.get_response("wrapped prompt", ReactModelMode.ACTION)

    assert captured["model"] == "ollama/llama3.1"
    assert captured["api_base"] == "http://ollama.test:11434"


def test_deepseek_reasoner_uses_reasoning_config_without_budget_tokens(monkeypatch):
    captured = {}

    def fake_completion(**params):
        captured.update(params)
        return make_response("THOUGHT: ok")

    monkeypatch.setattr("litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("litellm.completion", fake_completion)
    client = make_client(
        make_config(
            thinking_provider="deepseek",
            thinking_model="deepseek-reasoner",
            reasoning_effort="high",
        )
    )

    client.get_response("wrapped prompt", ReactModelMode.THINKING)

    assert captured["model"] == "deepseek/deepseek-reasoner"
    assert captured["reasoning_effort"] == "high"
    assert "budget_tokens" not in captured
