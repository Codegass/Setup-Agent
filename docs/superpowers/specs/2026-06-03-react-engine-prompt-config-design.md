# ReAct Engine Prompt Config Design

## Goal

Refactor `src/sag/agent/react_engine.py` so long-lived prompt text lives in default YAML configuration instead of large Python string literals, while preserving current ReAct behavior and making future prompt review easier.

## Scope

This change is intentionally narrow. It does:

- Extract stable ReAct prompt text from `react_engine.py` into `src/sag/config/prompts/react_engine.yaml`.
- Add a small prompt loader in `src/sag/config/prompt_loader.py`.
- Keep dynamic runtime assembly in `react_engine.py`, including repository URL, current context, tool descriptions, conversation history, and function-calling branches.
- Move the misplaced Git helper from `src/sag/config/git_utils.py` to `src/sag/utils/git_utils.py`.
- Add `src/sag/utils/__init__.py` as the home for shared utility helpers.

It does not:

- Rewrite prompt content for style or behavior improvements.
- Restructure `settings.py`, `logger.py`, or `models.py`.
- Add a user-editable prompt override system.
- Change ReAct loop semantics, tool orchestration, context management, or provider behavior.

## Config Boundary

`src/sag/config` currently contains both true configuration (`settings.py`, `models.py`) and runtime bootstrap helpers (`logger.py`). Prompt defaults fit this directory better than `src/sag/agent` because the extracted YAML is a default configuration asset, not agent control flow.

The config boundary should be tightened without turning this into a large architecture migration:

- `src/sag/config/prompts/react_engine.yaml` stores default prompt text assets.
- `src/sag/config/prompt_loader.py` loads and validates prompt assets.
- `src/sag/config/settings.py`, `logger.py`, and `models.py` remain unchanged structurally.
- `src/sag/config/git_utils.py` moves to `src/sag/utils/git_utils.py` because Git URL parsing is a general utility, not configuration.

The import in `src/sag/main.py` should change from:

```python
from sag.config.git_utils import extract_project_name_from_url
```

to:

```python
from sag.utils.git_utils import extract_project_name_from_url
```

No compatibility shim is required unless tests reveal hidden consumers, because current repository usage only imports the helper from `src/sag/main.py`.

## Prompt YAML Shape

The YAML is a required contract, not a loose recommendation. It should be structured by prompt purpose, not by Python method name alone. The first implementation must include these required keys:

```yaml
initial_system:
  identity: |
    ...
  repository_url_notice: |
    ...
  context_management: |
    ...
  tool_clarification: |
    ...
  intelligent_setup_workflow: |
    ...
  maven_pom_recovery: |
    ...
  maven_multimodule_testing: |
    ...
  function_calling_response_format: |
    ...
  prompt_based_response_format: |
    ...
  repository_url_reminder: |
    ...
  continuous_cycle_reminder: |
    ...

next_prompt:
  conversation_header: |
    ...
  omitted_steps_notice: |
    ...
  stuck_function_calling_guidance: |
    ...
  stuck_repository_url_guidance: |
    ...
  stuck_prompt_based_guidance: |
    ...
  continuation: |
    ...

mode_prompts:
  thinking: |
    ...
  action: |
    ...
```

Required dotted key list:

- `initial_system.identity`
- `initial_system.repository_url_notice`
- `initial_system.context_management`
- `initial_system.tool_clarification`
- `initial_system.intelligent_setup_workflow`
- `initial_system.maven_pom_recovery`
- `initial_system.maven_multimodule_testing`
- `initial_system.function_calling_response_format`
- `initial_system.prompt_based_response_format`
- `initial_system.repository_url_reminder`
- `initial_system.continuous_cycle_reminder`
- `next_prompt.conversation_header`
- `next_prompt.omitted_steps_notice`
- `next_prompt.stuck_function_calling_guidance`
- `next_prompt.stuck_repository_url_guidance`
- `next_prompt.stuck_prompt_based_guidance`
- `next_prompt.continuation`
- `mode_prompts.thinking`
- `mode_prompts.action`

Stable text moves into YAML. Runtime values remain in Python and are rendered into placeholders when needed:

- `{repository_url}`
- `{context_type}`
- `{context_id}`
- `{goal}`
- `{progress}`
- `{next_task}`
- `{task}`
- `{focus}`

The first implementation should keep rendering simple. `str.format(**values)` is enough if the YAML uses explicit placeholders and literal braces in examples are escaped. The loader should raise a clear error if a requested key is missing.

## Prompt Loader

`src/sag/config/prompt_loader.py` should provide a narrow API:

```python
class PromptConfigError(RuntimeError):
    pass


class PromptConfig:
    def get(self, key: str) -> str:
        ...

    def format(self, key: str, **values: object) -> str:
        ...


def load_react_engine_prompts() -> PromptConfig:
    ...
```

Loader responsibilities:

- Read `src/sag/config/prompts/react_engine.yaml` via package resources or a path relative to the module.
- Parse YAML with `yaml.safe_load`.
- Validate that every required dotted key listed in this spec exists and resolves to a string.
- Support dotted keys such as `initial_system.identity`.
- Return clear `PromptConfigError` messages for missing files, invalid YAML, missing keys, and non-string prompt values.
- Keep a module-level required-key list so tests and loader validation share one contract.

This keeps YAML parsing out of `react_engine.py`.

## Packaging Requirements

The YAML file is a runtime asset. Source-tree tests are not enough; the installed package must be able to load it.

Implementation planning must include:

- Add `PyYAML` to `pyproject.toml` runtime dependencies.
- Update `uv.lock`.
- Ensure `src/sag/config/prompts/react_engine.yaml` is included in built wheels. If Hatchling does not include it automatically, add explicit package-data/artifact configuration to `pyproject.toml`.
- Add a packaging smoke test or command that builds a wheel, installs it into an isolated environment, and verifies `load_react_engine_prompts().get("initial_system.identity")` works from the installed package.

This prevents a failure mode where `pytest` passes from the source tree but `sag` fails after installation because the YAML asset was not packaged.

## ReActEngine Changes

`ReActEngine.__init__` should load prompts once:

```python
self.prompts = load_react_engine_prompts()
```

Prompt-building methods should become mostly structural Python:

- `_build_initial_system_prompt()` gets dynamic context and tool data, then appends YAML blocks.
- `_build_next_prompt()` still serializes recent `ReActStep` history in Python, but static warning/reminder blocks come from YAML.
- `_build_thinking_model_prompt()` prepends `mode_prompts.thinking`.
- `_build_action_model_prompt()` prepends `mode_prompts.action`.

The code should keep small review comments pointing to YAML locations:

```python
# Prompt: src/sag/config/prompts/react_engine.yaml:12 initial_system.identity
prompt = self.prompts.get("initial_system.identity")
```

These comments are intentionally not documentation prose; they are review navigation hints.

## YAML Reference Comments

Each extracted prompt block used by `react_engine.py` should have a nearby comment in `react_engine.py` with this format:

```text
# Prompt: src/sag/config/prompts/react_engine.yaml:<line> <dotted.key>
```

The line number should point to the YAML key or the first line of that prompt block. Because YAML line numbers can drift during prompt edits, tests should validate references:

- Parse all `# Prompt:` comments in `react_engine.py`.
- Confirm the referenced YAML file exists.
- Confirm the dotted key exists via the loader.
- Confirm the referenced line is near the key name or the final key segment.

This gives reviewers fast jump targets and makes stale line references visible in CI.

## Behavior Preservation

The refactor should preserve current prompt behavior as much as possible:

- Do not rewrite prompt language during extraction.
- Preserve current section ordering.
- Preserve current function-calling vs prompt-based response format branches.
- Preserve repository URL reminders.
- Preserve context management and Maven recovery guidance.
- Preserve thinking/action mode instruction content.

Tests should assert stable semantic markers rather than byte-for-byte full prompts. Exact full-prompt snapshots would be too brittle because dynamic context and tool ordering can vary.

## Testing Strategy

Add focused tests before implementation:

- Loader loads the default YAML and returns required keys.
- Loader exposes or uses the exact required dotted key set listed in this spec.
- Loader raises `PromptConfigError` for missing keys and non-string values.
- `ReActEngine._build_thinking_model_prompt()` still contains `THINKING MODEL INSTRUCTIONS`, `CURRENT SITUATION TO ANALYZE`, and the supplied base prompt.
- `ReActEngine._build_action_model_prompt()` still contains `ACTION MODEL INSTRUCTIONS`, action format guidance, and the supplied base prompt.
- `_build_initial_system_prompt()` still contains core markers: SAG identity, context management rules, available tools, Maven recovery guidance, response format, and repository URL reminder when configured.
- `_build_next_prompt()` still contains conversation history, stuck-action guidance when appropriate, and continuation guidance.
- Prompt reference comments in `react_engine.py` resolve to existing YAML keys and nearby line numbers.
- `extract_project_name_from_url` still works from `sag.utils.git_utils`.
- `src/sag/main.py` imports Git URL utilities from `sag.utils.git_utils`.
- Built wheel includes `src/sag/config/prompts/react_engine.yaml`, and an installed-package smoke check can load `load_react_engine_prompts()`.

Existing full-suite verification should still pass:

```bash
uv run pytest
uv run black --check src tests
uv run isort --check-only src tests
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py -v
git diff --check
```

## Dependency Decision

The project does not currently include a YAML parser. This design allows adding `PyYAML` as a small runtime dependency because:

- The requested artifact is YAML, not JSON or TOML.
- Hand-rolled YAML parsing would be fragile and less maintainable.
- `yaml.safe_load` keeps parsing simple and avoids executable YAML behavior.

If dependency minimization becomes more important than YAML specifically, the alternative is TOML or JSON. For this request, YAML is the intended format.

The implementation must update both `pyproject.toml` and `uv.lock` when adding `PyYAML`.

## Risks

- Prompt behavior could accidentally change during extraction. Mitigation: move text verbatim and assert core markers.
- YAML line comments can drift. Mitigation: reference validation test.
- `str.format` placeholders can conflict with literal JSON braces in prompt examples. Mitigation: escape literal braces in YAML examples or avoid formatting blocks that contain JSON examples.
- Moving `git_utils.py` could break hidden imports. Mitigation: search repository imports before moving and rely on import smoke tests.

## Acceptance Criteria

- `react_engine.py` no longer contains the large stable prompt bodies currently embedded in `_build_initial_system_prompt`, `_build_next_prompt`, `_build_thinking_model_prompt`, and `_build_action_model_prompt`.
- Default prompt text lives in `src/sag/config/prompts/react_engine.yaml`.
- `react_engine.py` includes prompt reference comments with YAML path, line number, and dotted key.
- Prompt loader errors are explicit and covered by tests.
- Prompt YAML required keys are explicit and validated.
- The prompt YAML asset is included in the built wheel and verified from an installed package.
- `git_utils.py` lives under `src/sag/utils`.
- All focused and full verification commands pass.
