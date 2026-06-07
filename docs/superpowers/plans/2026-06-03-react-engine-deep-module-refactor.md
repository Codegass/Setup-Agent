# ReAct Engine Deep Module Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move ReAct model-call, response parsing, prompt-building, and shared type logic out of `react_engine.py` while preserving current behavior.

**Architecture:** Add focused internal modules under `src/sag/agent`: `react_types.py`, `react_response_parser.py`, `react_prompt_builder.py`, and `react_llm.py`. Keep `ReActEngine` as the loop coordinator, with compatibility imports for `ReActStep` and `StepType`, and migrate tests from private engine methods to the new module interfaces.

**Tech Stack:** Python 3.10+, Pydantic, dataclasses, LiteLLM, pytest, uv, black, isort, existing SAG agent/config/tool modules.

---

## Spec Reference

- Design spec: `docs/superpowers/specs/2026-06-03-react-engine-deep-module-design.md`
- Constraint: preserve runtime behavior first; this is a boundary refactor.
- Constraint: commit messages must not include Co-Authorship or authorship trailers.
- Constraint: do not rewrite prompt text or change the prompt YAML contract.
- Constraint: GPT-5 and Claude 4.6 must be first-class options for either thinking or action roles.

## LiteLLM Provider Compatibility Notes

Official LiteLLM docs checked for this plan:

- DeepSeek provider docs: `https://docs.litellm.ai/docs/providers/deepseek`
- Ollama provider docs: `https://docs.litellm.ai/docs/providers/ollama`
- Function calling docs: `https://docs.litellm.ai/docs/completion/function_call`
- Input params docs: `https://docs.litellm.ai/docs/completion/input`

Implementation guardrails for `ReactLLMClient`:

- Do not hard-code provider behavior from the current OpenAI/Anthropic pair.
- DeepSeek models use the `deepseek/` prefix and `DEEPSEEK_API_KEY`; keep model
  resolution compatible with `deepseek/deepseek-chat` and
  `deepseek/deepseek-reasoner`.
- DeepSeek reasoner thinking mode supports `thinking={"type": "enabled"}` or
  `reasoning_effort`; do not send Anthropic-only `budget_tokens` to DeepSeek.
- Ollama chat/tool-calling requests should support `ollama_chat/<model>` when
  configured. Plain `ollama/<model>` is still valid for normal chat/generate
  paths, but LiteLLM recommends `ollama_chat` for better chat responses.
- Ollama requests should pass `api_base` from `config.ollama_base_url` when the
  selected model/provider is Ollama.
- Use LiteLLM capability helpers such as `supports_function_calling()` and,
  where useful, `get_supported_openai_params()` instead of maintaining a broad
  provider matrix in SAG.
- `drop_params=True` only drops unsupported OpenAI params; provider-specific
  kwargs still pass through. Do not rely on it to sanitize arbitrary
  provider-specific values.

## File Structure

### Runtime Files

- Create: `src/sag/agent/react_types.py`
  - Shared `ReactModelMode`, `StepType`, `ReActStep`, and `ReactModelCapabilities`.
  - No import from `react_engine.py`.
- Create: `src/sag/agent/react_response_parser.py`
  - `ReActResponseParser.parse(response, model_used, was_thinking_model) -> list[ReActStep]`.
  - Parses model text into trusted thought/action steps only.
- Create: `src/sag/agent/react_prompt_builder.py`
  - `ReActPromptBuilder` owns initial/next/mode prompt construction, prompt reference comments, critical-memory injection, and trunk context cache.
- Create: `src/sag/agent/react_llm.py`
  - `ReactLLMClient` owns LiteLLM setup, per-role capabilities, request parameters, function-call normalization, JSON fallback parsing, token tracking, and LLM-specific verbose logging.
- Modify: `src/sag/agent/react_engine.py`
  - Import shared types from `react_types.py`.
  - Instantiate and delegate to `ReActResponseParser`, `ReActPromptBuilder`, and `ReactLLMClient`.
  - Keep loop orchestration, step storage, UI events, tool orchestration, state evaluation, successful-state tracking, physical observation enrichment, and token usage export.
- Modify: `src/sag/agent/agent_state_evaluator.py`
  - Import shared `StepType` from `react_types.py`; remove duplicate enum.
- Modify: `src/sag/agent/__init__.py` only if needed for import smoke tests.
  - Do not expose new modules as public API unless tests require it.

### Test Files

- Create: `tests/test_react_types.py`
- Create: `tests/test_react_response_parser.py`
- Create: `tests/test_react_prompt_builder.py`
- Create: `tests/test_react_llm.py`
- Modify: `tests/test_react_engine_prompts.py`
- Modify: `tests/test_prompt_reference_comments.py`
- Modify: `tests/test_tool_contracts.py`
- Modify: `tests/test_react_engine_tool_orchestration.py` only if the engine constructor/wiring affects mocks.

## Task 1: Add Shared ReAct Types

**Files:**
- Create: `src/sag/agent/react_types.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `src/sag/agent/agent_state_evaluator.py`
- Test: `tests/test_react_types.py`

- [ ] **Step 1: Write failing shared-type tests**

Create `tests/test_react_types.py`:

```python
from sag.agent import agent_state_evaluator
from sag.agent.react_engine import ReActStep as EngineReActStep
from sag.agent.react_engine import StepType as EngineStepType
from sag.agent.react_types import (
    ReActStep,
    ReactModelCapabilities,
    ReactModelMode,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_react_types.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.agent.react_types'`.

- [ ] **Step 3: Add `react_types.py`**

Create `src/sag/agent/react_types.py`:

```python
"""Shared ReAct runtime types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

from sag.tools.base import ToolResult


class ReactModelMode(str, Enum):
    THINKING = "thinking"
    ACTION = "action"


class StepType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    SYSTEM_GUIDANCE = "system_guidance"


class ReActStep(BaseModel):
    step_type: StepType
    content: str
    tool_name: Optional[str] = None
    tool_params: Optional[Dict[str, Any]] = None
    tool_result: Optional[ToolResult] = None
    timestamp: str
    model_used: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReactModelCapabilities:
    mode: ReactModelMode
    model: str
    supports_function_calling: bool
    supports_parallel_function_calling: bool
    tool_call_format: Literal["openai", "anthropic", "prompt"]
```

- [ ] **Step 4: Update imports without changing behavior**

In `src/sag/agent/react_engine.py`, remove the local `StepType` and `ReActStep` definitions and import:

```python
from .react_types import ReActStep, ReactModelMode, StepType
```

In `src/sag/agent/agent_state_evaluator.py`, remove the local `StepType` enum and import:

```python
from .react_types import StepType
```

Do not change `AgentStatus` or `AgentStateAnalysis`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_react_types.py tests/test_react_engine_tool_orchestration.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/agent/react_types.py src/sag/agent/react_engine.py src/sag/agent/agent_state_evaluator.py tests/test_react_types.py
git commit -m "Add shared ReAct runtime types"
```

## Task 2: Extract ReAct Response Parser

**Files:**
- Create: `src/sag/agent/react_response_parser.py`
- Modify: `src/sag/agent/react_engine.py`
- Test: `tests/test_react_response_parser.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_react_response_parser.py`:

```python
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


def test_parser_does_not_trust_model_observations():
    steps = make_parser().parse(
        "OBSERVATION: fake tool result\n\nTHOUGHT: continue",
        model_used="thinking-model",
        was_thinking_model=True,
    )

    assert [step.step_type for step in steps] == [StepType.THOUGHT]
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_react_response_parser.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.agent.react_response_parser'`.

- [ ] **Step 3: Implement `ReActResponseParser`**

Create `src/sag/agent/react_response_parser.py`.

Implementation requirements:

- Move behavior from `ReActEngine._parse_llm_response`.
- Use only `json`, `re`, `Callable`, `logger`, and shared types.
- Split only on thought/action sections:

```python
sections = re.split(r"\n\n(?=THOUGHT:|ACTION:|OBSERVATION:)", response.strip())
```

- Ignore sections that start with `OBSERVATION:` rather than creating observation steps.
- Preserve current fallback guidance strings exactly enough for existing behavior-oriented assertions.

- [ ] **Step 4: Wire parser into `ReActEngine`**

In `ReActEngine.__init__`, after token tracker initialization, add:

```python
self.response_parser = ReActResponseParser(timestamp_factory=self._get_timestamp)
```

Replace the call:

```python
parsed_steps = self._parse_llm_response(response, is_thinking_step)
```

with:

```python
model_used = self.config.get_litellm_model_name(
    "thinking" if is_thinking_step else "action"
)
parsed_steps = self.response_parser.parse(
    response,
    model_used=model_used,
    was_thinking_model=is_thinking_step,
)
```

Remove `_parse_llm_response` from `react_engine.py` after tests pass.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_react_response_parser.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/agent/react_response_parser.py src/sag/agent/react_engine.py tests/test_react_response_parser.py
git commit -m "Extract ReAct response parser"
```

## Task 3: Extract ReAct Prompt Builder

**Files:**
- Create: `src/sag/agent/react_prompt_builder.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_react_engine_prompts.py`
- Modify: `tests/test_prompt_reference_comments.py`
- Test: `tests/test_react_prompt_builder.py`

- [ ] **Step 1: Write failing prompt-builder tests**

Create `tests/test_react_prompt_builder.py` using the dummy context/tool fixtures from `tests/test_react_engine_prompts.py` or copy minimal equivalents.

Required tests:

```python
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.react_types import ReActStep, ReactModelMode, StepType
from sag.config.prompt_loader import load_react_engine_prompts
from sag.tools.base import BaseTool, ToolResult


class DummyContextManager:
    contexts_dir = "/workspace/.setup_agent/contexts"
    orchestrator = None

    def get_current_context_info(self):
        return {
            "context_type": "trunk",
            "context_id": "trunk",
            "goal": "Set up the repository",
            "progress": "0/1",
            "next_task": "task_1",
        }

    def load_trunk_context(self):
        return None


class DummyTool(BaseTool):
    def __init__(self):
        super().__init__("dummy", "Dummy tool for prompt tests")

    def execute(self) -> ToolResult:
        return ToolResult(success=True, output="ok")

    def get_usage_example(self):
        return "dummy()"


def make_builder():
    return ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=DummyContextManager(),
        tools={"dummy": DummyTool()},
    )


def test_initial_prompt_preserves_repository_and_tool_markers():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        tool_calling_enabled=True,
    )

    assert "You are SAG (Setup-Agent)" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "dummy: Dummy tool for prompt tests" in prompt
    assert "Usage: dummy()" in prompt
    assert "RESPONSE FORMAT" in prompt


def test_next_prompt_preserves_stuck_guidance_and_repository_url():
    steps = [
        ReActStep(step_type=StepType.THOUGHT, content="thought 1", timestamp="t1"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 2", timestamp="t2"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 3", timestamp="t3"),
    ]

    prompt = make_builder().build_next_prompt(
        steps=steps,
        repository_url="https://example.test/repo.git",
        tool_calling_enabled=True,
        successful_states={"maven_success": False, "cloned_repos": set()},
    )

    assert "CONVERSATION HISTORY" in prompt
    assert "IMPORTANT: You have been thinking without taking action" in prompt
    assert "https://example.test/repo.git" in prompt


def test_mode_prompt_wraps_base_once():
    builder = make_builder()

    thinking = builder.build_mode_prompt("base prompt", ReactModelMode.THINKING)
    action = builder.build_mode_prompt("base prompt", ReactModelMode.ACTION)

    assert thinking.count("THINKING MODEL INSTRUCTIONS") == 1
    assert action.count("ACTION MODEL INSTRUCTIONS") == 1
    assert thinking.endswith("base prompt")
    assert action.endswith("base prompt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_react_prompt_builder.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.agent.react_prompt_builder'`.

- [ ] **Step 3: Implement `ReActPromptBuilder`**

Create `src/sag/agent/react_prompt_builder.py`.

Move these behaviors from `react_engine.py`:

- `_build_initial_system_prompt`
- `_build_next_prompt`
- `_build_thinking_model_prompt`
- `_build_action_model_prompt`
- `_preserve_critical_info`
- `_inject_memory_protection`
- `_get_cached_trunk_context`
- `_invalidate_trunk_cache`

Implementation notes:

- Rename mode prompt wrappers to `build_mode_prompt(base_prompt, mode)`.
- Keep prompt lookup comments next to `PromptConfig.get/format()` calls.
- Accept `successful_states` as an argument to `build_next_prompt` so prompt builder can remain state-light.
- Keep trunk cache inside the builder, not the engine.
- Preserve prompt text and history truncation behavior.

- [ ] **Step 4: Update prompt reference comment test**

Modify `tests/test_prompt_reference_comments.py` so it scans both:

```python
REACT_PROMPT_SOURCE_PATHS = (
    REPO_ROOT / "src/sag/agent/react_engine.py",
    REPO_ROOT / "src/sag/agent/react_prompt_builder.py",
)
```

The test should collect prompt lookups from all listed files and assert every lookup has a nearby `# Prompt:` reference.

- [ ] **Step 5: Wire prompt builder into `ReActEngine`**

In `ReActEngine.__init__`, add:

```python
self.prompt_builder = ReActPromptBuilder(
    prompts=self.prompts,
    context_manager=self.context_manager,
    tools=self.tools,
)
```

Replace:

```python
self._invalidate_trunk_cache()
self._get_cached_trunk_context()
current_prompt = self._build_initial_system_prompt() + "\n\n" + initial_prompt
```

with:

```python
self.prompt_builder.invalidate_trunk_cache()
current_prompt = (
    self.prompt_builder.build_initial_system_prompt(
        repository_url=self.repository_url,
        tool_calling_enabled=self.llm_client.capabilities_for(
            ReactModelMode.ACTION
        ).supports_function_calling,
    )
    + "\n\n"
    + initial_prompt
)
```

If `ReactLLMClient` is not implemented yet, temporarily use the current `self.supports_function_calling` value and replace it in Task 4.

Replace `_build_next_prompt()` calls with:

```python
action_capabilities = self.llm_client.capabilities_for(ReactModelMode.ACTION)
current_prompt = self.prompt_builder.build_next_prompt(
    steps=self.steps,
    repository_url=self.repository_url,
    tool_calling_enabled=action_capabilities.supports_function_calling,
    successful_states=self.successful_states,
)
```

Replace `_build_thinking_model_prompt` / `_build_action_model_prompt` callers with `build_mode_prompt`.

Update `_apply_tool_execution_loop_effects()` so cache invalidation goes through the new owner:

```python
if metadata.get("invalidate_trunk_cache"):
    self.prompt_builder.invalidate_trunk_cache()
```

Do not leave this call pointing at `self._invalidate_trunk_cache()`, because that would split cache ownership after prompt-builder extraction.

Remove delegated prompt methods from `react_engine.py` after tests pass.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/test_react_prompt_builder.py tests/test_react_engine_prompts.py tests/test_prompt_reference_comments.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/sag/agent/react_prompt_builder.py src/sag/agent/react_engine.py tests/test_react_prompt_builder.py tests/test_react_engine_prompts.py tests/test_prompt_reference_comments.py
git commit -m "Extract ReAct prompt builder"
```

## Task 4: Extract React LLM Client

**Files:**
- Create: `src/sag/agent/react_llm.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_tool_contracts.py`
- Test: `tests/test_react_llm.py`

- [ ] **Step 1: Write failing LLM capability and schema tests**

Create `tests/test_react_llm.py`.

Use lightweight dummy config/tool objects and monkeypatch `litellm` calls:

```python
from types import SimpleNamespace

import pytest

from sag.agent.react_llm import ReactLLMClient
from sag.agent.react_types import ReactModelMode
from sag.agent.token_tracker import TokenTracker
from sag.tools.base import BaseTool, ToolResult


class ExampleTool(BaseTool):
    def __init__(self):
        super().__init__("example", "Example tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=command)


class DummyConfig:
    verbose = False
    log_level = SimpleNamespace(value="INFO")
    thinking_model = "gpt-5"
    thinking_provider = "openai"
    thinking_temperature = 0.1
    thinking_max_tokens = 16000
    action_model = "claude-4.6"
    action_provider = "anthropic"
    action_temperature = 0.3
    action_max_tokens = 10000
    gpt5_reasoning_effort = "medium"
    ollama_base_url = "http://localhost:11434"

    def get_litellm_model_name(self, model_type="action"):
        if model_type == "thinking":
            return "gpt-5"
        return "anthropic/claude-4.6"

    def is_gpt5_model(self, model_type="action"):
        return model_type == "thinking"

    def get_thinking_config(self):
        return {"reasoning_effort": "medium"}
```

Add tests:

```python
def test_capabilities_are_resolved_per_mode(monkeypatch):
    def supports_function_calling(model):
        return model == "anthropic/claude-4.6"

    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", supports_function_calling)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    client = ReactLLMClient(
        config=DummyConfig(),
        tools={"example": ExampleTool()},
        token_tracker=TokenTracker(),
    )
    client.setup()

    thinking = client.capabilities_for(ReactModelMode.THINKING)
    action = client.capabilities_for(ReactModelMode.ACTION)

    assert thinking.model == "gpt-5"
    assert thinking.tool_call_format == "openai"
    assert action.model == "anthropic/claude-4.6"
    assert action.supports_function_calling is True
    assert action.tool_call_format == "anthropic"


def test_tool_schema_uses_action_model_format(monkeypatch):
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    client = ReactLLMClient(
        config=DummyConfig(),
        tools={"example": ExampleTool()},
        token_tracker=TokenTracker(),
    )
    client.setup()

    schema = client.build_tools_schema(ReactModelMode.ACTION)

    assert schema[0]["name"] == "example"
    assert "input_schema" in schema[0]
```

Also add a reverse-role capability test so implementation does not bake in provider-role assumptions:

```python
class ReverseRoleConfig(DummyConfig):
    thinking_model = "claude-4.6"
    thinking_provider = "anthropic"
    action_model = "gpt-5"
    action_provider = "openai"

    def get_litellm_model_name(self, model_type="action"):
        if model_type == "thinking":
            return "anthropic/claude-4.6"
        return "gpt-5"

    def is_gpt5_model(self, model_type="action"):
        return model_type == "action"


def test_capabilities_support_claude_thinking_and_gpt5_action(monkeypatch):
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    client = ReactLLMClient(
        config=ReverseRoleConfig(),
        tools={"example": ExampleTool()},
        token_tracker=TokenTracker(),
    )
    client.setup()

    thinking = client.capabilities_for(ReactModelMode.THINKING)
    action = client.capabilities_for(ReactModelMode.ACTION)

    assert thinking.tool_call_format == "anthropic"
    assert action.tool_call_format == "openai"
```

- [ ] **Step 2: Add failing prompt-wrapper test**

In `tests/test_react_llm.py`, add:

```python
def test_get_response_does_not_wrap_mode_prompt(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="THOUGHT: ok"))],
            usage=None,
        )

    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.completion", fake_completion)

    client = ReactLLMClient(
        config=DummyConfig(),
        tools={},
        token_tracker=TokenTracker(),
    )
    client.setup()

    result = client.get_response(
        "THINKING MODEL INSTRUCTIONS\nbase prompt",
        ReactModelMode.THINKING,
    )

    assert result == "THOUGHT: ok"
    content = captured["messages"][0]["content"]
    assert content.count("THINKING MODEL INSTRUCTIONS") == 1
```

- [ ] **Step 3: Add failing response-normalization and fallback tests**

In `tests/test_react_llm.py`, add tests that characterize behavior moved from `react_engine.py`:

```python
def test_json_function_call_content_fallback(monkeypatch):
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    client = ReactLLMClient(
        config=DummyConfig(),
        tools={"example": ExampleTool()},
        token_tracker=TokenTracker(),
    )
    client.setup()

    parsed = client.try_parse_json_function_calls('{"tool": "example", "command": "pwd"}')

    assert "ACTION: example" in parsed
    assert 'PARAMETERS: {"command": "pwd"}' in parsed


def test_openai_tool_call_normalizes_to_react_text(monkeypatch):
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="functions.example", arguments='{"command": "pwd"}')
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="run pwd", tool_calls=[tool_call]))]
    )
    config = DummyConfig()
    config.action_provider = "openai"
    config.action_model = "gpt-5"

    client = ReactLLMClient(config=config, tools={"example": ExampleTool()}, token_tracker=TokenTracker())
    client.setup()

    text = client.normalize_function_calling_response(response, ReactModelMode.ACTION)

    assert "THOUGHT: run pwd" in text
    assert "ACTION: example" in text
    assert 'PARAMETERS: {"command": "pwd"}' in text
```

Add the matching Anthropic/Claude normalization test:

```python
def test_claude_tool_call_normalizes_to_react_text(monkeypatch):
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: True)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="run pwd",
                    tool_calls=[{"name": "example", "input": {"command": "pwd"}}],
                )
            )
        ]
    )

    client = ReactLLMClient(
        config=DummyConfig(),
        tools={"example": ExampleTool()},
        token_tracker=TokenTracker(),
    )
    client.setup()

    text = client.normalize_function_calling_response(response, ReactModelMode.ACTION)

    assert "THOUGHT: run pwd" in text
    assert "ACTION: example" in text
    assert 'PARAMETERS: {"command": "pwd"}' in text
```

Add a GPT-5 fallback characterization test:

```python
def test_gpt5_request_falls_back_to_traditional_params(monkeypatch):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("unsupported reasoning params")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="THOUGHT: fallback ok"))],
            usage=None,
        )

    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.completion", fake_completion)

    client = ReactLLMClient(
        config=ReverseRoleConfig(),
        tools={},
        token_tracker=TokenTracker(),
    )
    client.setup()

    result = client.get_response("ACTION MODEL INSTRUCTIONS\nbase prompt", ReactModelMode.ACTION)

    assert result == "THOUGHT: fallback ok"
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[0]["drop_params"] is True
    assert calls[1]["temperature"] == ReverseRoleConfig.action_temperature
    assert calls[1]["max_tokens"] == ReverseRoleConfig.action_max_tokens
    assert calls[1]["drop_params"] is True
```

Add an Ollama `api_base` characterization test:

```python
class OllamaActionConfig(DummyConfig):
    action_model = "llama3.1"
    action_provider = "ollama_chat"

    def get_litellm_model_name(self, model_type="action"):
        if model_type == "thinking":
            return "gpt-5"
        return "ollama_chat/llama3.1"

    def is_gpt5_model(self, model_type="action"):
        return model_type == "thinking"


def test_ollama_action_request_includes_api_base(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="THOUGHT: ok"))],
            usage=None,
        )

    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.completion", fake_completion)

    client = ReactLLMClient(
        config=OllamaActionConfig(),
        tools={},
        token_tracker=TokenTracker(),
    )
    client.setup()

    result = client.get_response("ACTION MODEL INSTRUCTIONS\nbase prompt", ReactModelMode.ACTION)

    assert result == "THOUGHT: ok"
    assert captured["model"] == "ollama_chat/llama3.1"
    assert captured["api_base"] == "http://localhost:11434"
```

Add a DeepSeek reasoner guardrail test:

```python
class DeepSeekThinkingConfig(DummyConfig):
    thinking_model = "deepseek-reasoner"
    thinking_provider = "deepseek"

    def get_litellm_model_name(self, model_type="action"):
        if model_type == "thinking":
            return "deepseek/deepseek-reasoner"
        return "anthropic/claude-4.6"

    def is_gpt5_model(self, model_type="action"):
        return False

    def get_thinking_config(self):
        return {"reasoning_effort": "medium"}


def test_deepseek_reasoner_uses_reasoning_effort_not_anthropic_budget(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="THOUGHT: ok"))],
            usage=None,
        )

    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.supports_parallel_function_calling", lambda model: False)
    monkeypatch.setattr("sag.agent.react_llm.litellm.completion", fake_completion)

    client = ReactLLMClient(
        config=DeepSeekThinkingConfig(),
        tools={},
        token_tracker=TokenTracker(),
    )
    client.setup()

    result = client.get_response("THINKING MODEL INSTRUCTIONS\nbase prompt", ReactModelMode.THINKING)

    assert result == "THOUGHT: ok"
    assert captured["model"] == "deepseek/deepseek-reasoner"
    assert captured["reasoning_effort"] == "medium"
    assert "budget_tokens" not in captured
```

If these helper methods are private in the implementation, keep the tests on `get_response()` with a mocked `litellm.completion()` response instead of exposing unnecessary public API.

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_react_llm.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.agent.react_llm'`.

- [ ] **Step 5: Implement `ReactLLMClient`**

Create `src/sag/agent/react_llm.py`.

Move these behaviors from `react_engine.py`:

- `_setup_litellm`
- `_check_function_calling_support`
- `_build_tools_schema`
- `_handle_function_calling_response`
- `_get_llm_response`
- `_try_parse_json_function_calls`
- LLM-specific verbose response/error logging if the raw `response` object is required.

Implementation requirements:

- `ReactLLMClient.setup()` must populate a per-mode capability cache.
- `capabilities_for(mode)` must return `ReactModelCapabilities`.
- `build_tools_schema(mode)` should be testable and use that mode's `tool_call_format`.
- Attach tools only for `ReactModelMode.ACTION`.
- Do not wrap prompts in `get_response`; prompt builder owns wrappers.
- Keep GPT-5 fallback behavior for each role.
- Keep O-series `temperature=1.0` behavior.
- Pass `api_base=config.ollama_base_url` for `ollama/` and `ollama_chat/`
  models when the config exposes that value.
- Keep DeepSeek reasoning params compatible with LiteLLM docs; use
  `reasoning_effort` or `thinking={"type": "enabled"}`, not Anthropic
  `budget_tokens`.
- Preserve JSON fallback parsing and function-call normalization behavior.

- [ ] **Step 6: Wire LLM client into `ReActEngine`**

In `ReActEngine.__init__`, after token tracker creation, instantiate:

```python
self.llm_client = ReactLLMClient(
    config=self.config,
    tools=self.tools,
    token_tracker=self.token_tracker,
)
self.llm_client.setup()
```

Remove direct `self._setup_litellm()` and `self._check_function_calling_support()` calls.

Replace every temporary `self.supports_function_calling` usage in initial and next prompt builder calls with:

```python
self.llm_client.capabilities_for(ReactModelMode.ACTION).supports_function_calling
```

Replace `_get_llm_response(current_prompt, is_thinking_step)` with:

```python
mode = ReactModelMode.THINKING if is_thinking_step else ReactModelMode.ACTION
wrapped_prompt = self.prompt_builder.build_mode_prompt(current_prompt, mode)
response = self.llm_client.get_response(wrapped_prompt, mode)
```

Remove delegated LLM methods from `react_engine.py` after tests pass.

- [ ] **Step 7: Update contract tests**

Modify `tests/test_tool_contracts.py` so schema tests use `ReactLLMClient.build_tools_schema(ReactModelMode.ACTION)` instead of `ReActEngine._build_tools_schema`.

Keep assertions for bash `timeout`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run pytest tests/test_react_llm.py tests/test_tool_contracts.py tests/test_react_engine_prompts.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add src/sag/agent/react_llm.py src/sag/agent/react_engine.py tests/test_react_llm.py tests/test_tool_contracts.py tests/test_react_engine_prompts.py
git commit -m "Extract React LLM client"
```

## Task 5: Engine Wiring Cleanup And Legacy Method Removal

**Files:**
- Modify: `src/sag/agent/react_engine.py`
- Modify: tests as needed from earlier tasks

- [ ] **Step 1: Scan for obsolete methods and imports**

Run:

```bash
rg -n "_parse_llm_response|_build_initial_system_prompt|_build_next_prompt|_build_thinking_model_prompt|_build_action_model_prompt|_preserve_critical_info|_inject_memory_protection|_get_cached_trunk_context|_invalidate_trunk_cache|_get_llm_response|_try_parse_json_function_calls|_handle_function_calling_response|_build_tools_schema|_setup_litellm|_check_function_calling_support|_format_tool_result|_log_llm_request|_log_tool_execution_verbose|test_state_evaluator_integration" src/sag/agent/react_engine.py tests
```

Expected: only compatibility references or no references for methods moved out.

- [ ] **Step 2: Delete confirmed unused non-completion legacy methods**

Remove from `react_engine.py` if no tests or runtime code call them:

- `_format_tool_result`
- `_log_llm_request`
- `_log_tool_execution_verbose`
- `test_state_evaluator_integration`
- `_preserve_critical_info`
- `_inject_memory_protection`
- `_get_cached_trunk_context`
- `_invalidate_trunk_cache`

Do not delete the completion cluster in this task:

- `_is_task_complete`
- `_check_maven_completion`
- `_add_completion_guidance`
- `_check_completion_suggestion`
- `_has_report_been_generated`

The spec says completion-cluster deletion should be a separate cleanup after core extraction passes.

- [ ] **Step 3: Remove unused imports**

Run:

```bash
uv run python -m compileall src/sag/agent
```

Then remove imports flagged by lint or obvious unused references, especially:

- `json` only if no remaining engine code needs it.
- `re` only if no remaining engine code needs it.
- `litellm` should leave `react_engine.py`.
- `AgentStateAnalysis`, `AgentStatus`, `BranchContext`, `BranchContextHistory`, `TrunkContext` if still unused.

- [ ] **Step 4: Run focused import and orchestration tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/sag/agent/react_engine.py
git commit -m "Clean up React engine delegated methods"
```

## Task 6: Full Verification And Review

**Files:**
- No planned source edits unless verification exposes failures.

- [ ] **Step 1: Run full test suite**

Run:

```bash
uv run pytest
```

Expected: PASS.

- [ ] **Step 2: Run format checks**

Run:

```bash
uv run black --check src tests
uv run isort --check-only src tests
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 3: Run targeted regression tests**

Run:

```bash
uv run pytest \
  tests/test_react_types.py \
  tests/test_react_response_parser.py \
  tests/test_react_prompt_builder.py \
  tests/test_react_llm.py \
  tests/test_react_engine_prompts.py \
  tests/test_prompt_reference_comments.py \
  tests/test_tool_contracts.py \
  tests/test_react_engine_tool_orchestration.py \
  -v
```

Expected: PASS.

- [ ] **Step 4: Inspect final method inventory**

Run:

```bash
python3 -c 'import ast, pathlib; p=pathlib.Path("src/sag/agent/react_engine.py"); tree=ast.parse(p.read_text()); cls=next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=="ReActEngine"); print(len([m for m in cls.body if isinstance(m, ast.FunctionDef)])); [print(m.name) for m in cls.body if isinstance(m, ast.FunctionDef)]'
```

Expected: method count is materially lower and remaining methods are loop/UI/tool-state oriented.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review`.

Ask the review agent to focus on:

- Behavior regressions in prompt construction, model call wrapping, parser trust boundaries, and tool schema format.
- Whether `ReactLLMClient`, `ReActPromptBuilder`, and `ReActResponseParser` are deep enough to justify their modules.
- Whether GPT-5 and Claude 4.6 can be configured in either thinking or action role without provider-role assumptions.

- [ ] **Step 6: Fix review findings if needed**

If review finds issues, use `superpowers:receiving-code-review`, fix one issue at a time, and rerun the relevant focused tests plus the full verification checks.

- [ ] **Step 7: Final commit if review fixes changed files**

Run:

```bash
git status --short
git add src/sag/agent tests
git commit -m "Address React engine refactor review"
```

Only run this if review fixes create new changes.

## Completion Criteria

- `react_engine.py` no longer owns LLM request construction, function-call normalization, JSON function-call fallback parsing, ReAct response parsing, prompt assembly, or critical-memory prompt injection.
- `ReActEngine` still owns loop orchestration, UI events, tool orchestration adapter logic, state evaluation, successful-state tracking, and physical observation enrichment.
- Prompt reference comments live beside prompt lookups in `react_prompt_builder.py` and pass tests.
- Model-generated `OBSERVATION:` text cannot create trusted observation steps.
- GPT-5 and Claude 4.6 capabilities are resolved per role/model.
- Existing imports from `sag.agent.react_engine import ReActStep, StepType` still work.
- Full verification passes.
