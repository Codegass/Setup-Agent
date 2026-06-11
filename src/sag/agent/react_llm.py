"""LiteLLM client for the ReAct engine."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import litellm
from loguru import logger

from sag.config import create_verbose_logger
from sag.tools.base import BaseTool

from .react_types import ReactModelCapabilities, ReactModelMode


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

    def get_response(self, prompt: str, mode: ReactModelMode) -> Optional[str]:
        """Get a normalized ReAct response for an already mode-wrapped prompt."""
        capabilities = self.capabilities_for(mode)
        try:
            request_params = self._build_request_params(prompt, mode, capabilities)
            response = self._completion_with_gpt5_fallback(request_params, prompt, mode)
            self.token_tracker.track_token_usage(
                response,
                capabilities.model,
                "thought" if mode == ReactModelMode.THINKING else "action",
            )

            message = response.choices[0].message
            self.logger.debug(f"Response message attributes: {dir(message)}")
            self.logger.debug(f"Has tool_calls: {hasattr(message, 'tool_calls')}")
            if hasattr(message, "tool_calls"):
                self.logger.debug(f"Tool calls value: {message.tool_calls}")

            if getattr(message, "tool_calls", None):
                self.logger.debug("Using function calling response handler")
                return self._handle_function_calling_response(response, capabilities)

            content = message.content
            content_str = content if content is not None else ""

            if self.config.verbose:
                self._log_llm_response(capabilities.model, content_str, response)

            self._log_agent_response_length(capabilities.model, content_str)
            self.logger.info(f"Full LLM Response from {capabilities.model}:")
            self.logger.info(content_str)
            self.logger.debug(
                f"Model used: {capabilities.model}, Response length: {len(content_str)}"
            )

            if (
                mode == ReactModelMode.ACTION
                and capabilities.supports_function_calling
                and content_str.strip()
            ):
                parsed_content = self._try_parse_json_function_calls(content_str)
                if parsed_content:
                    self.logger.debug("Successfully parsed JSON function calls from content")
                    return parsed_content

            return content_str

        except Exception as exc:
            self.logger.error(f"LLM request failed: {exc}")
            if self.config.verbose:
                self._log_llm_error(exc)
            return None

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

    def _build_request_params(
        self,
        prompt: str,
        mode: ReactModelMode,
        capabilities: ReactModelCapabilities,
    ) -> dict[str, Any]:
        model_type = self._model_type_for(mode)
        params: dict[str, Any] = {
            "model": capabilities.model,
            "messages": [{"role": "user", "content": prompt}],
        }

        tools_schema: list[dict[str, Any]] = []
        if mode == ReactModelMode.ACTION and capabilities.supports_function_calling:
            tools_schema = self.build_tools_schema(ReactModelMode.ACTION)

        use_traditional_tool_params = (
            mode == ReactModelMode.ACTION
            and self.config.is_gpt5_model(model_type)
            and bool(tools_schema)
        )

        if self.config.is_gpt5_model(model_type) and not use_traditional_tool_params:
            params["reasoning_effort"] = self.config.gpt5_reasoning_effort
            params["drop_params"] = True
            self.logger.info(
                f"Using GPT-5 parameters for {model_type} model: "
                f"reasoning_effort={self.config.gpt5_reasoning_effort}"
            )
        else:
            params["temperature"] = self._temperature_for(mode)
            params["max_tokens"] = self._max_tokens_for(mode)
            if use_traditional_tool_params:
                params["drop_params"] = True
                self.logger.info("Using GPT-5 action tool-call parameters without reasoning_effort")
            if mode == ReactModelMode.THINKING:
                params.update(self._thinking_config_for_mode())

        self._add_ollama_api_base(params, capabilities.model)

        if tools_schema:
            params["tools"] = tools_schema
            params["tool_choice"] = (
                {"type": "auto"} if capabilities.tool_call_format == "anthropic" else "auto"
            )
            self.logger.debug(
                f"Using {capabilities.tool_call_format} function calling with "
                f"{len(tools_schema)} tools"
            )
            if self.config.verbose:
                self.logger.debug(f"First tool schema: {json.dumps(tools_schema[0], indent=2)}")

        if self.config.verbose:
            self.logger.debug(f"{mode.value.title()} model request params: {params}")

        return params

    def _completion_with_gpt5_fallback(
        self,
        request_params: dict[str, Any],
        prompt: str,
        mode: ReactModelMode,
    ):
        model_type = self._model_type_for(mode)
        try:
            return litellm.completion(**request_params)
        except Exception as exc:
            if not self.config.is_gpt5_model(model_type):
                raise

            self.logger.warning(f"GPT-5 {model_type} model call failed: {exc}")
            self.logger.info(f"Falling back to traditional parameters for {model_type} model")

            fallback_params = {
                "model": request_params["model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self._temperature_for(mode),
                "max_tokens": self._max_tokens_for(mode),
                "drop_params": True,
            }
            self._add_ollama_api_base(fallback_params, request_params["model"])

            for key in ("tools", "tool_choice"):
                if key in request_params:
                    fallback_params[key] = request_params[key]

            return litellm.completion(**fallback_params)

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

    def _thinking_config_for_mode(self) -> dict[str, Any]:
        provider = self.config.thinking_provider
        model = self.config.thinking_model.lower()
        if provider == "deepseek" and "reasoner" in model:
            return {"reasoning_effort": self.config.reasoning_effort}
        return self.config.get_thinking_config()

    def _add_ollama_api_base(self, params: dict[str, Any], model: str) -> None:
        if model.startswith(("ollama/", "ollama_chat/")) and self.config.ollama_base_url:
            params["api_base"] = self.config.ollama_base_url

    def _handle_function_calling_response(
        self,
        response: Any,
        capabilities: ReactModelCapabilities,
    ) -> str:
        """Convert OpenAI and Anthropic tool-call responses to ReAct text."""
        try:
            message = response.choices[0].message
            content_parts = []
            if message.content:
                content_parts.append(f"THOUGHT: {message.content}")

            for tool_call in getattr(message, "tool_calls", None) or []:
                function_name, function_args = self._extract_tool_call(tool_call, capabilities)
                if not function_name:
                    continue

                if function_name.startswith("functions."):
                    function_name = function_name[10:]

                content_parts.append(f"ACTION: {function_name}")
                content_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
                self.logger.debug(
                    f"{capabilities.tool_call_format} function call: {function_name} "
                    f"with args: {function_args}"
                )

            result = "\n\n".join(content_parts)

            if self.config.verbose:
                tool_count = len(getattr(message, "tool_calls", None) or [])
                self.logger.info(
                    f"{capabilities.tool_call_format} function calling response from "
                    f"{capabilities.model}: {tool_count} tool calls"
                )

            return result

        except Exception as exc:
            self.logger.error(f"Failed to handle function calling response: {exc}")
            content = response.choices[0].message.content
            return content if content is not None else ""

    def _extract_tool_call(
        self,
        tool_call: Any,
        capabilities: ReactModelCapabilities,
    ) -> tuple[Optional[str], dict[str, Any]]:
        if capabilities.tool_call_format == "anthropic":
            function_name = self._get_tool_call_value(tool_call, "name")
            function_args = self._get_tool_call_value(tool_call, "input")
            if function_name is None:
                function = self._get_tool_call_value(tool_call, "function") or {}
                function_name = self._get_tool_call_value(function, "name")
                function_args = self._get_tool_call_value(function, "arguments")
        else:
            function = self._get_tool_call_value(tool_call, "function") or {}
            function_name = self._get_tool_call_value(function, "name")
            function_args = self._get_tool_call_value(function, "arguments")

        if isinstance(function_args, str):
            try:
                function_args = json.loads(function_args)
            except json.JSONDecodeError:
                self.logger.warning(f"Failed to parse function arguments: {function_args}")
                function_args = {}

        return function_name, function_args if isinstance(function_args, dict) else {}

    def _get_tool_call_value(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _try_parse_json_function_calls(self, content: str) -> Optional[str]:
        """Try to parse JSON function calls from content when tool calls are not used."""
        try:
            lines = content.strip().split("\n")
            parsed_parts = []
            i = 0

            while i < len(lines):
                stripped = lines[i].strip()

                if stripped.startswith("{") and stripped.endswith("}"):
                    parsed = self._parse_json_tool_object(stripped)
                    if parsed:
                        parsed_parts.extend(parsed)
                        i += 1
                        continue

                if stripped == "ACTION:" and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.startswith("{") and next_line.endswith("}"):
                        parsed = self._parse_json_tool_object(next_line)
                        if parsed:
                            parsed_parts.extend(parsed)
                            i += 2
                            continue

                if stripped.startswith("ACTION:"):
                    action_part = stripped[7:].strip()
                    if action_part.startswith("{") and action_part.endswith("}"):
                        parsed = self._parse_json_tool_object(action_part)
                        if parsed:
                            parsed_parts.extend(parsed)
                            i += 1
                            continue

                if stripped.startswith("```json") and i + 1 < len(lines):
                    json_lines = []
                    j = i + 1
                    while j < len(lines) and not lines[j].strip().startswith("```"):
                        json_lines.append(lines[j])
                        j += 1
                    if json_lines:
                        parsed = self._parse_json_tool_object("\n".join(json_lines).strip())
                        if parsed:
                            parsed_parts.extend(parsed)
                            i = j + 1
                            continue

                if stripped and not stripped.startswith("```"):
                    parsed_parts.append(stripped)

                i += 1

            if parsed_parts:
                return "\n".join(parsed_parts)

        except Exception as exc:
            self.logger.warning(f"Failed to parse JSON function calls: {exc}")

        return None

    def _parse_json_tool_object(self, content: str) -> Optional[list[str]]:
        try:
            json_obj = json.loads(content)
        except json.JSONDecodeError:
            return None

        function_name = None
        function_args: dict[str, Any] = {}
        parsed_parts: list[str] = []

        if "tool" in json_obj:
            function_name = json_obj["tool"]
            function_args = {key: value for key, value in json_obj.items() if key != "tool"}
        elif "name" in json_obj and "arguments" in json_obj:
            function_name = json_obj["name"]
            function_args = json_obj["arguments"]
        elif "action" in json_obj and "action_args" in json_obj:
            function_name = json_obj["action"]
            function_args = json_obj["action_args"]
            if "thought" in json_obj:
                parsed_parts.append(f"THOUGHT: {json_obj['thought']}")
        elif "action" in json_obj and "args" in json_obj:
            function_name = json_obj["action"]
            function_args = json_obj["args"]
        elif len(json_obj) == 1:
            tool_name = list(json_obj.keys())[0]
            if tool_name in self.tools:
                function_name = tool_name
                value = json_obj[tool_name]
                function_args = value if isinstance(value, dict) else {}
        elif any(key in json_obj for key in ["action", "command", "path", "query"]):
            function_name, function_args = self._infer_tool_from_parameters(json_obj)

        if not function_name:
            return None

        parsed_parts.append(f"ACTION: {function_name}")
        parsed_parts.append(f"PARAMETERS: {json.dumps(function_args)}")
        self.logger.debug(f"Parsed JSON function call: {function_name} with args: {function_args}")
        return parsed_parts

    def _infer_tool_from_parameters(
        self,
        json_obj: dict[str, Any],
    ) -> tuple[Optional[str], dict[str, Any]]:
        if "action" in json_obj:
            if json_obj.get("action") in ["read", "write", "append"]:
                return "file_io", json_obj
            if json_obj.get("action") in [
                "get_info",
                "switch_to_trunk",
                "create_branch",
            ]:
                return "manage_context", json_obj
            if json_obj.get("action") in ["clone", "detect_project_type"]:
                return "project_setup", json_obj
        elif "command" in json_obj:
            command = json_obj.get("command", "").lower()
            if any(cmd in command for cmd in ["compile", "test", "package", "clean", "install"]):
                return "maven", json_obj
            return "bash", json_obj
        elif "query" in json_obj:
            return "search", {"target": f"web:{json_obj.get('query', '')}"}

        return None, {}

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
