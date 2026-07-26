"""LiteLLM client for the native executor loop.

One request shape: a full OpenAI-normalized `messages` array in,
`NativeTurn(text, tool_calls)` out, tool-call ids preserved. The ReAct-text
protocol (single-user-message requests, the tool_call -> "ACTION:" flattener,
and the JSON salvage heuristics that repaired it) was deleted with the
scheduler in Plan 2 Task 8.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

import litellm
from loguru import logger

from sag.config import create_verbose_logger
from sag.tools.base import BaseTool

from .react_types import ReactModelCapabilities, ReactModelMode


@dataclass(frozen=True)
class NativeToolCall:
    """One tool call requested by the executor, with its provider id intact.

    The id is what lets a later ``{"role": "tool", "tool_call_id": ...}`` message
    be correlated back to this call, so it is never empty: a synthetic
    ``call_<index>`` stands in when the provider omits one.
    """

    id: str
    name: str
    arguments: dict  # parsed JSON object; {} when unparseable (see raw_arguments)
    raw_arguments: str


@dataclass(frozen=True)
class NativeTurn:
    """One assistant turn: prose plus the tool calls it requested."""

    text: str  # "" when the model emitted no prose
    tool_calls: tuple  # tuple[NativeToolCall, ...]
    model_used: str


class ReactLLMClient:
    """Own model capabilities, LiteLLM request construction, and response normalization."""

    def __init__(
        self,
        *,
        config: Any,
        tools: dict[str, BaseTool],
        token_tracker: Any,
        logger=logger,
        trace_context: Optional[Callable[[], dict[str, Any]]] = None,
    ):
        self.config = config
        self.tools = tools
        self.token_tracker = token_tracker
        self.logger = logger
        self.trace_context = trace_context
        self._capability_cache: dict[ReactModelMode, ReactModelCapabilities] = {}

    def setup(self) -> None:
        """Setup LiteLLM configuration."""
        litellm.cache = None

        if self.config.log_level.value == "DEBUG":
            litellm.set_verbose = True

        for mode in ReactModelMode:
            self._capability_cache[mode] = self._resolve_capabilities(mode)

        action_capabilities = self.capabilities_for(ReactModelMode.ACTION)
        if action_capabilities.supports_function_calling:
            self.logger.info(
                f"Action model {action_capabilities.model} supports "
                f"{action_capabilities.tool_call_format} function calling"
            )
        else:
            self.logger.warning(
                f"Action model {action_capabilities.model} does not support function calling, "
                "falling back to prompt-based approach"
            )
            litellm.add_function_to_prompt = True

        if action_capabilities.supports_parallel_function_calling:
            self.logger.info(
                f"Action model {action_capabilities.model} supports parallel function calling"
            )
        else:
            self.logger.info(
                f"Action model {action_capabilities.model} does not support parallel function calling"
            )

        self.logger.info("LiteLLM configured")

    def capabilities_for(self, mode: ReactModelMode) -> ReactModelCapabilities:
        """Resolve model capabilities for a ReAct model mode."""
        if mode not in self._capability_cache:
            self._capability_cache[mode] = self._resolve_capabilities(mode)

        return self._capability_cache[mode]

    def _resolve_capabilities(self, mode: ReactModelMode) -> ReactModelCapabilities:
        """Compute model capabilities for a ReAct model mode."""
        model_type = self._model_type_for(mode)
        model = self.config.get_litellm_model_name(model_type)
        tool_call_format = self._tool_call_format_for_model(model)
        supports_function_calling = (
            False if tool_call_format == "prompt" else self._supports_function_calling(model)
        )
        supports_parallel_function_calling = (
            False
            if tool_call_format == "prompt"
            else self._supports_parallel_function_calling(model)
        )

        return ReactModelCapabilities(
            mode=mode,
            model=model,
            supports_function_calling=supports_function_calling,
            supports_parallel_function_calling=supports_parallel_function_calling,
            tool_call_format=tool_call_format,
        )

    def build_tools_schema(self, mode: ReactModelMode) -> list[dict[str, Any]]:
        """Build function calling schema from tools for the selected mode model."""
        capabilities = self.capabilities_for(mode)
        tools_schema = []

        for tool in self.tools.values():
            schema = tool.get_parameter_schema()

            if capabilities.tool_call_format == "anthropic":
                tool_def = {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": schema,
                }
            else:
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": schema,
                    },
                }

            tools_schema.append(tool_def)

        return tools_schema

    def get_native_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        include_tools: bool = True,
    ) -> NativeTurn:
        """One native multi-turn executor call: full message history in,
        structured (text, tool_calls) out.

        Nothing is flattened into text and no tool-call id is discarded, so each
        `{"role": "tool", "tool_call_id": ...}` reply can be correlated back to
        the call that produced it (spec §3.1).
        """
        capabilities = self.capabilities_for(ReactModelMode.ACTION)
        params = self._build_native_request_params(messages, capabilities, include_tools)
        try:
            response = litellm.completion(**params)
        except Exception as exc:
            # Logged, never swallowed: the loop turns a provider failure into a
            # typed abort, which a None return could not express.
            self.logger.error(f"Native executor request failed: {exc}")
            if self.config.verbose:
                self._log_llm_error(exc)
            raise
        self._track_native_usage(response, capabilities.model)
        turn = self._native_turn_from_response(response, capabilities)
        if self.config.verbose:
            self._log_llm_response(capabilities.model, turn.text, response)
        self._log_agent_response_length(capabilities.model, turn.text)
        return turn

    def get_advisor_response(
        self,
        messages: list[dict],
        *,
        model: str,
        max_tokens: int,
    ) -> str:
        """One fresh-context advisor consult: plain completion text out.

        No tools, no thinking config, a hard output cap — the advisor reviews
        the transcript, it does not act. Provider errors propagate: the ONLY
        caller (`ReActEngine.consult_advisor`) turns them into a success-shaped
        "proceed with your best judgment" result, because a broken advisor must
        degrade to Plan-2 behavior rather than abort the run.
        """
        params: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            # Providers that reject `max_tokens` (or any other field here) drop
            # it instead of 400-ing the consult.
            "drop_params": True,
        }
        self._add_ollama_api_base(params, model)
        response = litellm.completion(**params)
        self._track_advisor_usage(response, model)
        message = response.choices[0].message
        return getattr(message, "content", None) or ""

    def _track_advisor_usage(self, response: Any, model: str) -> None:
        if self.token_tracker is None:
            return

        try:
            self.token_tracker.track_token_usage(response, model, "advisor")
        except Exception as exc:  # pragma: no cover - defensive accounting path
            self.logger.debug(f"Could not track advisor token usage: {exc}")

    def _build_native_request_params(
        self,
        messages: list[dict[str, Any]],
        capabilities: ReactModelCapabilities,
        include_tools: bool,
    ) -> dict[str, Any]:
        model_type = self._model_type_for(ReactModelMode.ACTION)
        params: dict[str, Any] = {
            "model": capabilities.model,
            "messages": list(messages),
        }

        tools_schema: list[dict[str, Any]] = []
        if include_tools and capabilities.supports_function_calling:
            tools_schema = self.build_tools_schema(ReactModelMode.ACTION)

        is_gpt5 = self.config.is_gpt5_model(model_type)
        use_traditional_tool_params = is_gpt5 and bool(tools_schema)

        if is_gpt5 and not use_traditional_tool_params:
            params["reasoning_effort"] = self.config.gpt5_reasoning_effort
            params["drop_params"] = True
        else:
            params["temperature"] = self._temperature_for(ReactModelMode.ACTION)
            params["max_tokens"] = self._max_tokens_for(ReactModelMode.ACTION)
            if use_traditional_tool_params:
                params["drop_params"] = True

        self._add_ollama_api_base(params, capabilities.model)

        if tools_schema:
            params["tools"] = tools_schema
            params["tool_choice"] = (
                {"type": "auto"} if capabilities.tool_call_format == "anthropic" else "auto"
            )

        return params

    def _track_native_usage(self, response: Any, model: str) -> None:
        if self.token_tracker is None:
            return

        try:
            self.token_tracker.track_token_usage(response, model, "executor")
        except Exception as exc:  # pragma: no cover - defensive accounting path
            self.logger.debug(f"Could not track executor token usage: {exc}")

    def _native_turn_from_response(
        self,
        response: Any,
        capabilities: ReactModelCapabilities,
    ) -> NativeTurn:
        message = response.choices[0].message
        raw_calls = getattr(message, "tool_calls", None) or ()

        return NativeTurn(
            text=getattr(message, "content", None) or "",
            tool_calls=tuple(
                self._native_tool_call(raw_call, index) for index, raw_call in enumerate(raw_calls)
            ),
            model_used=getattr(response, "model", None) or capabilities.model,
        )

    def _native_tool_call(self, raw_call: Any, index: int) -> NativeToolCall:
        """Read one wire tool call. litellm normalizes both providers to the
        OpenAI `function` shape; the anthropic-native `name`/`input` pair is
        still accepted as a fallback."""
        function = self._get_tool_call_value(raw_call, "function")
        if function is None:
            name = self._get_tool_call_value(raw_call, "name")
            arguments = self._get_tool_call_value(raw_call, "input")
        else:
            name = self._get_tool_call_value(function, "name")
            arguments = self._get_tool_call_value(function, "arguments")

        name = name if isinstance(name, str) else ""
        if name.startswith("functions."):
            name = name[len("functions.") :]

        parsed_arguments, raw_arguments = self._normalize_tool_arguments(arguments)

        return NativeToolCall(
            id=self._get_tool_call_value(raw_call, "id") or f"call_{index}",
            name=name,
            arguments=parsed_arguments,
            raw_arguments=raw_arguments,
        )

    def _normalize_tool_arguments(self, arguments: Any) -> tuple[dict[str, Any], str]:
        """Return the parsed argument object plus the exact wire text.

        Never raises: a malformed call still has to round-trip so the paired
        tool reply can quote back what the model actually sent.
        """
        if isinstance(arguments, dict):
            return arguments, json.dumps(arguments)
        if not isinstance(arguments, str):
            return {}, ""

        try:
            parsed = json.loads(arguments) if arguments else {}
        except (ValueError, TypeError):
            self.logger.warning(f"Failed to parse tool call arguments: {arguments}")
            return {}, arguments

        return (parsed if isinstance(parsed, dict) else {}), arguments

    def _model_type_for(self, mode: ReactModelMode) -> str:
        return "thinking" if mode == ReactModelMode.THINKING else "action"

    def _get_trace_context(self) -> dict[str, Any]:
        if self.trace_context is None:
            return {}

        try:
            return self.trace_context() or {}
        except Exception as exc:  # pragma: no cover - defensive observability path
            self.logger.debug(f"Could not read LLM trace context: {exc}")
            return {}

    def _log_agent_response_length(self, model: str, content: str) -> None:
        agent_logger = self._get_trace_context().get("agent_logger")
        if agent_logger is None:
            return

        agent_logger.info(f"LLM Response from {model}: {len(content)} chars")

    def _supports_function_calling(self, model: str) -> bool:
        try:
            return bool(litellm.supports_function_calling(model))
        except Exception as exc:  # pragma: no cover - defensive LiteLLM compatibility
            self.logger.debug(f"Could not check function calling support for {model}: {exc}")
            return False

    def _supports_parallel_function_calling(self, model: str) -> bool:
        try:
            return bool(litellm.supports_parallel_function_calling(model))
        except Exception as exc:  # pragma: no cover - defensive LiteLLM compatibility
            self.logger.debug(
                f"Could not check parallel function calling support for {model}: {exc}"
            )
            return False

    def _tool_call_format_for_model(self, model: str) -> str:
        model_lower = model.lower()
        if "anthropic/" in model_lower or "claude" in model_lower:
            return "anthropic"
        if (
            model_lower.startswith("openai/")
            or model_lower.startswith("azure/")
            or model_lower.startswith("deepseek/")
            or model_lower.startswith("ollama/")
            or model_lower.startswith("ollama_chat/")
            or model_lower.startswith("groq/")
            or "gpt" in model_lower
            or "o1" in model_lower
            or "o4" in model_lower
        ):
            return "openai"
        return "prompt"

    def _temperature_for(self, mode: ReactModelMode) -> float:
        model_type = self._model_type_for(mode)
        model_name = getattr(self.config, f"{model_type}_model").lower()
        if "o4" in model_name or "o1" in model_name:
            return 1.0
        return (
            self.config.thinking_temperature
            if mode == ReactModelMode.THINKING
            else self.config.action_temperature
        )

    def _max_tokens_for(self, mode: ReactModelMode) -> int:
        return (
            self.config.thinking_max_tokens
            if mode == ReactModelMode.THINKING
            else self.config.action_max_tokens
        )

    def _add_ollama_api_base(self, params: dict[str, Any], model: str) -> None:
        if model.startswith(("ollama/", "ollama_chat/")) and self.config.ollama_base_url:
            params["api_base"] = self.config.ollama_base_url

    def _get_tool_call_value(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _log_llm_response(self, model: str, content: str, response: Any) -> None:
        verbose_logger = create_verbose_logger("react_llm")
        trace_context = self._get_trace_context()
        usage_info = {}
        if hasattr(response, "usage") and response.usage:
            usage_info = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }

        log_entry = {
            "event": "llm_response",
            "model": model,
            "iteration": trace_context.get("iteration"),
            "response_length": len(content),
            "full_response": content,
            "usage": usage_info,
            "timestamp": trace_context.get("timestamp"),
        }

        verbose_logger.info(f"LLM RESPONSE: {json.dumps(log_entry, indent=2)}")

    def _log_llm_error(self, error: Exception) -> None:
        verbose_logger = create_verbose_logger("react_llm")
        trace_context = self._get_trace_context()
        error_entry = {
            "event": "llm_error",
            "iteration": trace_context.get("iteration"),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "timestamp": trace_context.get("timestamp"),
        }
        verbose_logger.error(f"LLM ERROR: {json.dumps(error_entry, indent=2)}")
