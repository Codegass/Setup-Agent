# ReAct Engine Prompt Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move stable `ReActEngine` prompt text into default YAML configuration, keep `react_engine.py` mostly structural Python, and clean up the misplaced Git utility under `src/sag/utils`.

**Architecture:** Add a narrow `PromptConfig` loader under `src/sag/config`, store default prompt text in `src/sag/config/prompts/react_engine.yaml`, and have `ReActEngine` assemble prompts from YAML blocks plus runtime data. Keep package-data verification explicit so the YAML asset works from installed wheels, not only from the source tree.

**Tech Stack:** Python 3.10+, PyYAML via `yaml.safe_load`, Hatchling package build, pytest, uv, existing SAG ReAct engine and config modules.

---

## Spec

Implement this plan from:

`docs/superpowers/specs/2026-06-03-react-engine-prompt-config-design.md`

Do not change the approved scope without user review. In particular:

- Do not rewrite prompt language for quality or style.
- Do not restructure `settings.py`, `logger.py`, or `models.py`.
- Do not add user-editable prompt overrides.
- Do not add Co-Authorship or authorship trailers to commit messages.

## File Map

- Create: `src/sag/utils/__init__.py`
  - Package marker for shared utility helpers.
- Move/Create: `src/sag/utils/git_utils.py`
  - New home for `extract_project_name_from_url`.
- Delete: `src/sag/config/git_utils.py`
  - Remove the misplaced config helper after import updates.
- Modify: `src/sag/main.py`
  - Import Git URL helper from `sag.utils.git_utils`.
- Create: `src/sag/config/prompts/react_engine.yaml`
  - Default ReAct prompt text asset.
- Create: `src/sag/config/prompt_loader.py`
  - Loads, validates, and formats prompt YAML.
- Modify: `src/sag/agent/react_engine.py`
  - Use `PromptConfig` instead of embedded long prompt strings.
  - Add `# Prompt:` comments pointing to YAML line/key references.
- Modify: `pyproject.toml`
  - Add `PyYAML` runtime dependency.
  - Add Hatchling package-data/artifact config if wheel inspection shows YAML is not included automatically.
- Modify: `uv.lock`
  - Update lockfile after adding `PyYAML`.
- Add tests:
  - `tests/test_git_utils.py`
  - `tests/test_prompt_loader.py`
  - `tests/test_react_engine_prompts.py`
  - `tests/test_prompt_reference_comments.py`
  - `tests/test_packaging_prompt_assets.py` or a packaging smoke command documented in the final verification.

## Required YAML Keys

The loader must validate this exact required dotted key set:

```python
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
```

---

## Task 1: Move Git URL Utility Out Of Config

**Files:**
- Create: `src/sag/utils/__init__.py`
- Create: `src/sag/utils/git_utils.py`
- Delete: `src/sag/config/git_utils.py`
- Modify: `src/sag/main.py`
- Test: `tests/test_git_utils.py`
- Test: `tests/test_import_smoke.py`

- [ ] **Step 1: Write the failing utility import test**

Create `tests/test_git_utils.py`:

```python
import pytest

from sag.utils.git_utils import extract_project_name_from_url


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        ("https://github.com/org/repo.git", "repo"),
        ("git@github.com:org/repo.git", "repo"),
        ("https://dev.azure.com/org/project/_git/service", "service"),
        ("/Users/example/projects/local-repo", "local-repo"),
        ("C:\\Users\\example\\repo-name", "repo-name"),
    ],
)
def test_extract_project_name_from_url(repo_url, expected):
    assert extract_project_name_from_url(repo_url) == expected


def test_extract_project_name_rejects_empty_url():
    with pytest.raises(ValueError, match="cannot be empty"):
        extract_project_name_from_url("")
```

- [ ] **Step 2: Run the new test to verify it fails**

Run:

```bash
uv run pytest tests/test_git_utils.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.utils'`.

- [ ] **Step 3: Move the implementation**

Create `src/sag/utils/__init__.py`:

```python
"""Shared utility helpers for SAG."""
```

Move the current contents of `src/sag/config/git_utils.py` into `src/sag/utils/git_utils.py` unchanged:

```python
"""Git URL utilities — shared between CLI and tools."""

import re
from urllib.parse import urlparse


def extract_project_name_from_url(repo_url: str) -> str:
    ...
```

Use the existing function body verbatim. Do not rewrite behavior.

- [ ] **Step 4: Update imports and remove old file**

In `src/sag/main.py`, replace:

```python
from sag.config.git_utils import extract_project_name_from_url
```

with:

```python
from sag.utils.git_utils import extract_project_name_from_url
```

Delete `src/sag/config/git_utils.py`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_git_utils.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/utils/__init__.py src/sag/utils/git_utils.py src/sag/main.py tests/test_git_utils.py
git add -u src/sag/config/git_utils.py
git commit -m "Move git utilities out of config"
```

---

## Task 2: Add Prompt Loader Contract And YAML Dependency

**Files:**
- Create: `src/sag/config/prompt_loader.py`
- Create: `src/sag/config/prompts/react_engine.yaml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_prompt_loader.py`

- [ ] **Step 1: Write failing loader tests**

Create `tests/test_prompt_loader.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_prompt_loader.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `sag.config.prompt_loader`.

- [ ] **Step 3: Add PyYAML dependency**

Run:

```bash
uv add PyYAML
```

Expected: `pyproject.toml` gains a `PyYAML` runtime dependency and `uv.lock` updates.

If `uv add` cannot run because of sandbox access to the uv cache, rerun with escalation. Do not hand-edit `uv.lock`.

- [ ] **Step 4: Implement prompt loader**

Create `src/sag/config/prompt_loader.py`:

```python
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
```

- [ ] **Step 5: Create initial YAML with all required keys**

Create `src/sag/config/prompts/react_engine.yaml` with the required key shape. For this task, use concise placeholder text with the correct semantic markers so loader tests pass. Full prompt extraction happens in Task 3.

```yaml
initial_system:
  identity: |
    You are SAG (Setup-Agent), an AI assistant specialized in setting up and configuring software projects.
  repository_url_notice: |
    Repository URL: {repository_url}
  context_management: |
    CRITICAL CONTEXT MANAGEMENT RULES:
  tool_clarification: |
    AVAILABLE TOOLS:
  intelligent_setup_workflow: |
    INTELLIGENT SETUP WORKFLOW
  maven_pom_recovery: |
    Handling Maven POM Parsing Errors
  maven_multimodule_testing: |
    Handling Multi-Module Maven Test Execution
  function_calling_response_format: |
    RESPONSE FORMAT:
  prompt_based_response_format: |
    RESPONSE FORMAT:
  repository_url_reminder: |
    REPOSITORY INFO: {repository_url}
  continuous_cycle_reminder: |
    REMEMBER THE CONTINUOUS CYCLE
next_prompt:
  conversation_header: |
    CONVERSATION HISTORY:
  omitted_steps_notice: |
    ... (earlier steps omitted for brevity) ...
  stuck_function_calling_guidance: |
    IMPORTANT: You have been thinking without taking action.
  stuck_repository_url_guidance: |
    The repository URL is already set: {repository_url}
  stuck_prompt_based_guidance: |
    IMPORTANT: You must take ACTION now.
  continuation: |
    Continue with your next THOUGHT and ACTION:
mode_prompts:
  thinking: |
    THINKING MODEL INSTRUCTIONS:
    CURRENT SITUATION TO ANALYZE:
  action: |
    ACTION MODEL INSTRUCTIONS:
    RESPONSE FORMAT (when function calling not supported):
```

Do not treat this placeholder YAML as final. Task 3 must replace these blocks with text extracted from `react_engine.py`.

- [ ] **Step 6: Run loader tests**

Run:

```bash
uv run pytest tests/test_prompt_loader.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add pyproject.toml uv.lock src/sag/config/prompt_loader.py src/sag/config/prompts/react_engine.yaml tests/test_prompt_loader.py
git commit -m "Add prompt config loader"
```

---

## Task 3: Extract ReActEngine Prompts Into YAML

**Files:**
- Modify: `src/sag/config/prompts/react_engine.yaml`
- Modify: `src/sag/agent/react_engine.py`
- Test: `tests/test_react_engine_prompts.py`

- [ ] **Step 1: Write failing prompt behavior tests**

Create `tests/test_react_engine_prompts.py`:

```python
from sag.agent.react_engine import ReActEngine, ReActStep, StepType
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


class DummyTool(BaseTool):
    def __init__(self):
        super().__init__("dummy", "Dummy tool for prompt tests")

    def execute(self) -> ToolResult:
        return ToolResult(success=True, output="ok")

    def get_usage_example(self):
        return "dummy()"


def make_engine(repository_url=None, supports_function_calling=True):
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_manager = DummyContextManager()
    engine.tools = {"dummy": DummyTool()}
    engine.repository_url = repository_url
    engine.supports_function_calling = supports_function_calling
    engine.prompts = load_react_engine_prompts()
    engine.steps = []
    return engine


def test_initial_system_prompt_preserves_core_markers_with_repository_url():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = ReActEngine._build_initial_system_prompt(engine)

    assert "You are SAG (Setup-Agent)" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "CRITICAL CONTEXT MANAGEMENT RULES" in prompt
    assert "AVAILABLE TOOLS" in prompt
    assert "dummy: Dummy tool for prompt tests" in prompt
    assert "Usage: dummy()" in prompt
    assert "Handling Maven POM Parsing Errors" in prompt
    assert "Handling Multi-Module Maven Test Execution" in prompt
    assert "RESPONSE FORMAT" in prompt
    assert "REMEMBER THE CONTINUOUS CYCLE" in prompt


def test_initial_system_prompt_uses_prompt_based_branch_when_function_calling_disabled():
    engine = make_engine(supports_function_calling=False)

    prompt = ReActEngine._build_initial_system_prompt(engine)

    assert "Always respond in this exact format" in prompt
    assert "ACTION: [tool_name]" in prompt


def test_next_prompt_preserves_history_and_stuck_guidance():
    engine = make_engine(repository_url="https://example.test/repo.git")
    engine.steps = [
        ReActStep(step_type=StepType.THOUGHT, content="thought 1", timestamp="t1"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 2", timestamp="t2"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 3", timestamp="t3"),
    ]

    prompt = ReActEngine._build_next_prompt(engine)

    assert "CONVERSATION HISTORY" in prompt
    assert "THOUGHT: thought 1" in prompt
    assert "IMPORTANT: You have been thinking without taking action" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "Continue with your next THOUGHT and ACTION" in prompt


def test_mode_prompts_preserve_markers_and_base_prompt():
    engine = make_engine()

    thinking_prompt = ReActEngine._build_thinking_model_prompt(engine, "base prompt")
    action_prompt = ReActEngine._build_action_model_prompt(engine, "base prompt")

    assert "THINKING MODEL INSTRUCTIONS" in thinking_prompt
    assert "CURRENT SITUATION TO ANALYZE" in thinking_prompt
    assert thinking_prompt.endswith("base prompt")
    assert "ACTION MODEL INSTRUCTIONS" in action_prompt
    assert "RESPONSE FORMAT (when function calling not supported)" in action_prompt
    assert action_prompt.endswith("base prompt")
```

- [ ] **Step 2: Run tests to verify current placeholder extraction is insufficient**

Run:

```bash
uv run pytest tests/test_react_engine_prompts.py -v
```

Expected: FAIL on missing full prompt markers or because `react_engine.py` still does not load `self.prompts` in normal initialization.

- [ ] **Step 3: Replace YAML placeholders with verbatim prompt blocks**

Extract stable text from `src/sag/agent/react_engine.py` into `src/sag/config/prompts/react_engine.yaml`.

Use this mapping:

- `_build_initial_system_prompt()` opening ReAct identity block → `initial_system.identity`
- repository URL warning block → `initial_system.repository_url_notice`
- context management rules → `initial_system.context_management`
- explicit tool name clarification and setup workflow text → split into:
  - `initial_system.tool_clarification`
  - `initial_system.intelligent_setup_workflow`
- Maven POM parsing section → `initial_system.maven_pom_recovery`
- multi-module Maven section → `initial_system.maven_multimodule_testing`
- function-calling response format branch → `initial_system.function_calling_response_format`
- prompt-based response format branch → `initial_system.prompt_based_response_format`
- repository URL reminder → `initial_system.repository_url_reminder`
- continuous cycle reminder → `initial_system.continuous_cycle_reminder`
- `_build_next_prompt()` static header/omission/stuck/continuation strings → `next_prompt.*`
- `_build_thinking_model_prompt()` `thinking_instructions` → `mode_prompts.thinking`
- `_build_action_model_prompt()` `action_instructions` → `mode_prompts.action`

Move text verbatim except for:

- Convert runtime values to explicit placeholders such as `{repository_url}`.
- Escape literal JSON braces in YAML blocks that will be formatted with `str.format`, for example `{{"param1": "value1"}}`.
- Keep code-generated tool descriptions and context fields in Python.

- [ ] **Step 4: Wire `ReActEngine` to prompt loader**

In `src/sag/agent/react_engine.py`, import:

```python
from sag.config.prompt_loader import load_react_engine_prompts
```

In `ReActEngine.__init__`, after `self.config = get_config()`:

```python
self.prompts = load_react_engine_prompts()
```

Update prompt-building methods to append YAML blocks in the same order as the old strings. Use helper code like:

```python
parts = []

# Prompt: src/sag/config/prompts/react_engine.yaml:<line> initial_system.identity
parts.append(self.prompts.get("initial_system.identity"))

if self.repository_url:
    # Prompt: src/sag/config/prompts/react_engine.yaml:<line> initial_system.repository_url_notice
    parts.append(self.prompts.format("initial_system.repository_url_notice", repository_url=self.repository_url))
```

Use `"\n\n".join(part.rstrip() for part in parts if part).rstrip() + "\n"` or a similar small join helper to avoid accidental section collapse.

- [ ] **Step 5: Keep dynamic Python assembly in Python**

Do not move these dynamic sections into YAML:

- The loop over `self.tools.values()` that adds tool descriptions and usage examples.
- Current context fields from `context_info`.
- Conversation history serialization in `_build_next_prompt()`.
- `_inject_memory_protection()` behavior.

- [ ] **Step 6: Run focused prompt tests**

Run:

```bash
uv run pytest tests/test_prompt_loader.py tests/test_react_engine_prompts.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/sag/agent/react_engine.py src/sag/config/prompts/react_engine.yaml tests/test_react_engine_prompts.py
git commit -m "Extract React engine prompts to YAML"
```

---

## Task 4: Validate Prompt Reference Comments

**Files:**
- Modify: `src/sag/agent/react_engine.py`
- Test: `tests/test_prompt_reference_comments.py`

- [ ] **Step 1: Write failing reference-comment test**

Create `tests/test_prompt_reference_comments.py`:

```python
import re
from pathlib import Path

from sag.config.prompt_loader import load_react_engine_prompts


REPO_ROOT = Path(__file__).resolve().parents[1]
REACT_ENGINE_PATH = REPO_ROOT / "src/sag/agent/react_engine.py"
PROMPT_REF_RE = re.compile(
    r"# Prompt: (?P<path>src/sag/config/prompts/react_engine\.yaml):(?P<line>\d+) (?P<key>[\w.]+)"
)


def test_react_engine_prompt_reference_comments_resolve():
    source = REACT_ENGINE_PATH.read_text()
    refs = list(PROMPT_REF_RE.finditer(source))
    assert refs

    prompts = load_react_engine_prompts()

    for ref in refs:
        prompt_path = REPO_ROOT / ref.group("path")
        line_number = int(ref.group("line"))
        key = ref.group("key")

        assert prompt_path.exists()
        assert prompts.get(key).strip()

        lines = prompt_path.read_text().splitlines()
        assert 1 <= line_number <= len(lines)
        nearby = "\n".join(lines[max(0, line_number - 4) : min(len(lines), line_number + 3)])
        assert key.split(".")[-1] in nearby
```

- [ ] **Step 2: Run test to verify it fails if references are missing or stale**

Run:

```bash
uv run pytest tests/test_prompt_reference_comments.py -v
```

Expected before adding comments: FAIL with `assert refs`.

- [ ] **Step 3: Add prompt reference comments**

In `src/sag/agent/react_engine.py`, add a `# Prompt:` comment near each YAML block retrieval:

```python
# Prompt: src/sag/config/prompts/react_engine.yaml:12 initial_system.identity
parts.append(self.prompts.get("initial_system.identity"))
```

Use actual line numbers from `src/sag/config/prompts/react_engine.yaml`.

- [ ] **Step 4: Run comment validation**

Run:

```bash
uv run pytest tests/test_prompt_reference_comments.py tests/test_react_engine_prompts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/sag/agent/react_engine.py tests/test_prompt_reference_comments.py
git commit -m "Validate React engine prompt references"
```

---

## Task 5: Verify Runtime Packaging Of Prompt YAML

**Files:**
- Modify: `pyproject.toml` if package-data config is needed
- Create or Modify: `tests/test_packaging_prompt_assets.py`

- [ ] **Step 1: Write packaging asset test**

Create `tests/test_packaging_prompt_assets.py`:

```python
import zipfile
from pathlib import Path


def test_built_wheel_contains_react_engine_prompt_yaml():
    dist_dir = Path("dist")
    wheels = sorted(dist_dir.glob("setup_agent-*.whl"))
    assert wheels, "Build a wheel before running this packaging asset test"

    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())

    assert "sag/config/prompts/react_engine.yaml" in names
```

This test intentionally requires a wheel to exist. Use it after `uv run python -m build --wheel`.

- [ ] **Step 2: Build a wheel and run the test**

Run:

```bash
uv run python -m build --wheel
uv run pytest tests/test_packaging_prompt_assets.py -v
```

Expected: If Hatchling includes YAML automatically, PASS. If it fails, update `pyproject.toml` package-data/artifact settings.

- [ ] **Step 3: If needed, add Hatchling artifact config**

Only if the wheel test fails, add an explicit Hatchling include setting. Prefer the smallest config that includes only the prompt YAML asset.

Example shape to evaluate against Hatchling behavior:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/sag"]
artifacts = [
    "src/sag/config/prompts/react_engine.yaml",
]
```

Rebuild the wheel after any `pyproject.toml` packaging change.

- [ ] **Step 4: Installed-package smoke check**

Run an isolated installed-package check from a temporary virtual environment:

```bash
uv run python -m build --wheel
python -m venv /tmp/sag-prompt-wheel-check
/tmp/sag-prompt-wheel-check/bin/python -m pip install dist/setup_agent-*.whl
/tmp/sag-prompt-wheel-check/bin/python -c "from sag.config.prompt_loader import load_react_engine_prompts; print(load_react_engine_prompts().get('initial_system.identity')[:20])"
```

Expected: command prints the beginning of the identity prompt and exits 0.

- [ ] **Step 5: Run focused package/import tests**

Run:

```bash
uv run pytest tests/test_packaging_prompt_assets.py tests/test_import_smoke.py tests/test_static_import_guard.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add pyproject.toml tests/test_packaging_prompt_assets.py
git commit -m "Verify packaged prompt assets"
```

If `pyproject.toml` did not need package-data changes, commit only the packaging test:

```bash
git add tests/test_packaging_prompt_assets.py
git commit -m "Verify packaged prompt assets"
```

---

## Task 6: Full Verification And Review

**Files:**
- No expected source changes unless verification finds issues.

- [ ] **Step 1: Run full tests**

Run:

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run formatting checks**

Run:

```bash
uv run black --check src tests
uv run isort --check-only src tests
```

Expected: both pass.

- [ ] **Step 3: Run import/static guards**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py -v
```

Expected: PASS.

- [ ] **Step 4: Run package asset verification**

Run:

```bash
uv run python -m build --wheel
uv run pytest tests/test_packaging_prompt_assets.py -v
python -m venv /tmp/sag-prompt-wheel-check
/tmp/sag-prompt-wheel-check/bin/python -m pip install dist/setup_agent-*.whl
/tmp/sag-prompt-wheel-check/bin/python -c "from sag.config.prompt_loader import load_react_engine_prompts; assert 'SAG' in load_react_engine_prompts().get('initial_system.identity')"
```

Expected: all commands exit 0.

- [ ] **Step 5: Run whitespace diff check**

Run:

```bash
git diff --check
```

Expected: clean.

- [ ] **Step 6: Request code review**

Dispatch a code-reviewer subagent with:

- Base SHA: commit before Task 1.
- Head SHA: current HEAD.
- Scope: prompt extraction to YAML, prompt loader, prompt reference comments, `git_utils` move, package-data verification.
- Ask specifically for:
  - Behavior drift risk in prompt assembly.
  - Missing required YAML keys.
  - Stale or misleading prompt reference comments.
  - Packaging/wheel asset gaps.
  - Any import compatibility issue from moving `git_utils`.

- [ ] **Step 7: Fix Critical/Important review issues**

Use `superpowers:receiving-code-review` before applying review feedback. Fix Critical and Important issues before completion. Re-run relevant tests after each fix.

- [ ] **Step 8: Final status**

Run:

```bash
git status --short --untracked-files=all
git log --oneline -5
```

Expected: working tree clean except intentionally ignored build artifacts. If `dist/` or temporary packaging outputs are untracked and ignored, mention them in the final summary only if visible.

---

## Notes For Implementers

- `docs/` is ignored by `.gitignore`; this plan file itself must be staged with `git add -f`.
- Some new files under `tests/` may also be ignored by the repository's `test_*.py` ignore rule. If `git status --untracked-files=all` does not show them, check `git check-ignore -v` and use `git add -f` for intentional test files.
- `uv run` may need sandbox escalation because uv reads from the user's cache directory.
- Do not hand-edit `uv.lock`; use `uv add PyYAML` or equivalent uv lock update.
- Keep commit messages concise and do not add authorship trailers.
