"""Prompt configuration loading for bundled SAG prompt assets."""

from __future__ import annotations

from importlib import resources
from typing import Any, Mapping

import yaml

REACT_ENGINE_REQUIRED_PROMPT_KEYS = (
    "initial_system.identity",
    "initial_system.repository_url_notice",
    "initial_system.context_management",
    "initial_system.tool_clarification",
    "initial_system.intelligent_setup_workflow",
    "initial_system.maven_pom_recovery",
    "initial_system.maven_multimodule_testing",
    "initial_system.function_calling_response_format",
    "initial_system.prompt_based_response_format",
    "initial_system.repository_url_reminder",
    "initial_system.continuous_cycle_reminder",
    "next_prompt.conversation_header",
    "next_prompt.omitted_steps_notice",
    "next_prompt.stuck_function_calling_guidance",
    "next_prompt.stuck_repository_url_guidance",
    "next_prompt.stuck_prompt_based_guidance",
    "next_prompt.continuation",
    "mode_prompts.thinking",
    "mode_prompts.action",
)


class PromptConfigError(RuntimeError):
    """Raised when prompt configuration cannot be loaded or resolved."""


class PromptConfig:
    """Read prompt text by dotted keys from a nested prompt mapping."""

    def __init__(self, data: Mapping[str, Any]):
        self._data = data

    def get(self, key: str) -> str:
        value: Any = self._data
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                raise PromptConfigError(f"Missing prompt key: {key}")
            value = value[part]

        if not isinstance(value, str):
            raise PromptConfigError(f"Prompt key {key} must resolve to a string")

        return value

    def format(self, key: str, **values: object) -> str:
        try:
            return self.get(key).format(**values)
        except KeyError as exc:
            raise PromptConfigError(f"Missing format value for prompt key {key}: {exc}") from exc

    def validate_required(self, required_keys: tuple[str, ...]) -> None:
        for key in required_keys:
            if not self.get(key).strip():
                raise PromptConfigError(f"Prompt key {key} must not be empty")


def load_react_engine_prompts() -> PromptConfig:
    try:
        prompt_text = (
            resources.files("sag.config.prompts").joinpath("react_engine.yaml").read_text()
        )
    except FileNotFoundError as exc:
        raise PromptConfigError("Missing react_engine prompt YAML asset") from exc

    try:
        data = yaml.safe_load(prompt_text)
    except yaml.YAMLError as exc:
        raise PromptConfigError("Invalid react_engine prompt YAML") from exc

    if not isinstance(data, Mapping):
        raise PromptConfigError("react_engine prompt YAML must contain a mapping")

    prompts = PromptConfig(data)
    prompts.validate_required(REACT_ENGINE_REQUIRED_PROMPT_KEYS)
    return prompts
