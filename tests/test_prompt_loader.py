import pytest

from sag.config.prompt_loader import (
    REACT_ENGINE_REQUIRED_PROMPT_KEYS,
    PromptConfig,
    PromptConfigError,
    load_react_engine_prompts,
)


def test_react_engine_prompts_load_required_keys():
    prompts = load_react_engine_prompts()

    for key in REACT_ENGINE_REQUIRED_PROMPT_KEYS:
        value = prompts.get(key)
        assert isinstance(value, str)
        assert value.strip()


def test_prompt_config_supports_dotted_keys():
    prompts = PromptConfig({"outer": {"inner": "hello"}})

    assert prompts.get("outer.inner") == "hello"


def test_prompt_config_formats_values():
    prompts = PromptConfig({"message": "Repository: {repository_url}"})

    assert prompts.format("message", repository_url="https://example.test/repo") == (
        "Repository: https://example.test/repo"
    )


def test_prompt_config_missing_key_error_is_clear():
    prompts = PromptConfig({"outer": {}})

    with pytest.raises(PromptConfigError, match="outer.inner"):
        prompts.get("outer.inner")


def test_prompt_config_non_string_value_error_is_clear():
    prompts = PromptConfig({"outer": {"inner": ["not", "a", "string"]}})

    with pytest.raises(PromptConfigError, match="outer.inner"):
        prompts.get("outer.inner")


def test_default_prompt_required_key_set_is_explicit():
    assert "initial_system.identity" in REACT_ENGINE_REQUIRED_PROMPT_KEYS
    assert "mode_prompts.action" in REACT_ENGINE_REQUIRED_PROMPT_KEYS
    assert len(REACT_ENGINE_REQUIRED_PROMPT_KEYS) == len(set(REACT_ENGINE_REQUIRED_PROMPT_KEYS))
