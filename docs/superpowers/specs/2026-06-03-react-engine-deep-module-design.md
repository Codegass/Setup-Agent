# ReAct Engine Deep Module Refactor Design

Date: 2026-06-03
Status: Draft for spec review

## Goal

Slim `src/sag/agent/react_engine.py` by moving high-complexity, low-level
ReAct details behind a small number of deep internal modules. The refactor
should preserve runtime behavior while making `ReActEngine` readable as the
main loop coordinator instead of the place where every provider, parser,
prompt, and fallback rule lives.

The first phase intentionally targets the highest leverage boundaries:

- LLM request and function-call response normalization.
- ReAct response parsing.
- ReAct prompt assembly and prompt-memory protection.

## Non-Goals

- Do not redesign the ReAct loop or change model selection semantics.
- Do not rewrite prompt content or change the YAML prompt contract.
- Do not refactor tool execution beyond the existing `ToolOrchestrator`
  adapter calls.
- Do not extract successful-state tracking, physical observation enrichment, or
  general tracing in this phase. LLM-specific verbose request/response/error
  logging may move with the LLM client because raw response objects live at that
  boundary after extraction.
- Do not add a user-facing prompt override system.
- Do not change CLI, UI, Docker, or report behavior.
- Do not introduce broad framework abstractions, inheritance hierarchies, or a
  general-purpose agent runtime API.

## Current Problem

`react_engine.py` is currently 2348 lines. `ReActEngine` has more than 40
methods and mixes several layers of responsibility:

- Main ReAct loop orchestration.
- LiteLLM setup, provider capability checks, request parameter construction,
  GPT-5 fallback handling, and token tracking.
- OpenAI and Claude function-call response normalization.
- JSON function-call fallback parsing for models that return tool calls as
  content.
- `THOUGHT` / `ACTION` / `PARAMETERS` ReAct text parsing.
- Initial, next-iteration, thinking-mode, and action-mode prompt assembly.
- Critical-memory prompt injection and trunk context caching.
- Tool execution adapter methods and loop side effects.
- Successful-state mutation after tool execution.
- Physical validation enrichment for observations.
- Legacy completion checks that have been superseded by `AgentStateEvaluator`.
- Verbose tracing and token usage export.

This makes maintenance hard because a reader must scan low-level provider,
parser, and prompt logic to understand the top-level control loop.

## Design Principle

The target is a deep-module shape, not a large number of shallow helpers.

`ReActEngine` should keep the small orchestration surface:

```text
initialize collaborators
build initial prompt
for each iteration:
  choose thinking or action mode
  request model output
  parse model output into ReActStep objects
  execute steps
  evaluate state
  build the next prompt
export token usage / return result
```

Details such as provider-specific request parameters, response normalization,
prompt-memory injection, and ReAct text parsing should be hidden behind focused
interfaces.

## Proposed Modules

### `src/sag/agent/react_types.py`

Move the shared ReAct data contracts out of `react_engine.py`:

```python
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
```

`react_engine.py` should import these types and continue exposing them through
the existing module import path. This keeps current imports such as:

```python
from sag.agent.react_engine import ReActStep, StepType
```

working while avoiding circular imports when parser and prompt modules need the
same types. `AgentStateEvaluator` should also import `StepType` from this module
instead of keeping a local duplicate enum.

### `src/sag/agent/react_llm.py`

Add a `ReactLLMClient` responsible for provider and model-call details.

Primary responsibilities:

- Configure LiteLLM cache and debug verbosity.
- Resolve capabilities for each configured role/model, including thinking and
  action roles.
- Track function-calling support, parallel function-calling support, and tool
  call schema format per role/model rather than through one global
  provider-format flag.
- Build tool schemas for OpenAI and Anthropic/Claude formats.
- Build thinking/action request parameters from SAG config.
- Handle GPT-5 reasoning-parameter fallback without leaking that logic into
  `ReActEngine`.
- Attach function-calling tool schemas to action requests when supported.
- Normalize OpenAI and Claude tool calls into the existing ReAct text format.
- Parse JSON function-call content fallback into ReAct text when function
  calling was expected but content was returned.
- Track token usage via the injected `TokenTracker`.
- Preserve LLM-specific verbose logging for request, response, and error
  details either inside `ReactLLMClient` or through a narrow injected callback.

Suggested interface:

```python
@dataclass(frozen=True)
class ReactModelCapabilities:
    mode: ReactModelMode
    model: str
    supports_function_calling: bool
    supports_parallel_function_calling: bool
    tool_call_format: Literal["openai", "anthropic", "prompt"]


class ReactLLMClient:
    def __init__(
        self,
        *,
        config: Any,
        tools: Mapping[str, BaseTool],
        token_tracker: TokenTracker,
        logger: Any = logger,
    ) -> None:
        ...

    def capabilities_for(self, mode: ReactModelMode) -> ReactModelCapabilities:
        ...

    def setup(self) -> None:
        ...

    def get_response(self, prompt: str, mode: ReactModelMode) -> Optional[str]:
        ...
```

`get_response()` receives the already mode-wrapped prompt. It should not know
about YAML prompt keys or prompt-memory logic, and it must not wrap the prompt
again. The implementation plan should include a characterization test that a
thinking/action mode prompt marker appears exactly once in the final message
sent to `litellm.completion()`.

Thinking and action are roles, not provider assumptions. Either role may be
configured with GPT-5, Claude 4.6, or another LiteLLM-supported model. The
client should prioritize first-class support for GPT-5 and Claude 4.6 by using
model/provider capabilities for the selected mode instead of assuming a fixed
provider-to-role mapping. Tool schemas are attached only when the request mode
should execute tools, but the schema format must come from that mode's
configured model.

The role/model capability design should also stay compatible with future
DeepSeek and Ollama usage. LiteLLM's official provider docs require DeepSeek
models to use the `deepseek/` prefix, and DeepSeek reasoner thinking should use
`thinking={"type": "enabled"}` or `reasoning_effort` rather than Anthropic
`budget_tokens`. Ollama chat/tool-calling support should allow
`ollama_chat/<model>` and should pass the configured `api_base` when available.
Use LiteLLM capability helpers such as `supports_function_calling()` and
`get_supported_openai_params()` where useful instead of maintaining a broad
provider matrix in SAG.

Behavior preservation details:

- Return `None` on request failure, matching current `_get_llm_response()`.
- Keep GPT-5 fallback behavior for thinking and action roles.
- Keep the current `temperature=1.0` special case for `o1` / `o4` model names.
- Keep Anthropic/Claude tool schema handling available to whichever role/model
  is executing tool calls.
- Keep current function-call normalization output:

```text
THOUGHT: ...

ACTION: tool_name
PARAMETERS: {...}
```

- Preserve current JSON fallback formats, including single-key tool objects and
  inferred `file_io`, `manage_context`, `project_setup`, `maven`, `bash`, and
  `web_search` mappings.

### `src/sag/agent/react_response_parser.py`

Add a parser module responsible only for converting normalized ReAct text into
typed thought/action steps.

Suggested interface:

```python
class ReActResponseParser:
    def __init__(self, *, timestamp_factory: Callable[[], str]) -> None:
        ...

    def parse(
        self,
        response: str,
        *,
        model_used: str,
        was_thinking_model: bool,
    ) -> list[ReActStep]:
        ...
```

Responsibilities:

- Split response sections on `THOUGHT:` and `ACTION:` markers. If the model
  includes `OBSERVATION:` text, keep the current trust boundary: do not create
  an observation step from model output. Observations are trusted tool outputs
  created only by the tool execution path.
- Parse action tool names and JSON parameters.
- Convert invalid empty action names into a thought with current guidance.
- Preserve the fallback that treats unparseable content as a thought.
- Preserve the extra guidance currently appended when a thinking-role or
  action-role model returns unstructured content.

This parser should not know about LiteLLM, tools, context manager, UI events, or
prompt YAML.

### `src/sag/agent/react_prompt_builder.py`

Add a prompt builder that owns all prompt assembly and critical-memory prompt
injection.

Primary responsibilities:

- Build the initial system prompt.
- Build the next-iteration prompt from recent `ReActStep` history.
- Wrap base prompts with thinking/action mode instructions.
- Inject critical memory into next prompts.
- Own the trunk context cache used by critical-memory construction.
- Expose `invalidate_trunk_cache()` for tool-execution side effects.

Suggested interface:

```python
class ReActPromptBuilder:
    def __init__(
        self,
        *,
        prompts: PromptConfig,
        context_manager: ContextManager,
        tools: Mapping[str, BaseTool],
    ) -> None:
        ...

    def build_initial_system_prompt(
        self,
        *,
        repository_url: Optional[str],
        tool_calling_enabled: bool,
    ) -> str:
        ...

    def build_next_prompt(
        self,
        *,
        steps: Sequence[ReActStep],
        repository_url: Optional[str],
        tool_calling_enabled: bool,
        successful_states: Mapping[str, Any],
    ) -> str:
        ...

    def build_mode_prompt(self, base_prompt: str, mode: ReactModelMode) -> str:
        ...

    def invalidate_trunk_cache(self) -> None:
        ...
```

Prompt reference comments should move with prompt lookups into
`react_prompt_builder.py`:

```text
# Prompt: src/sag/config/prompts/react_engine.yaml:<line> <dotted.key>
```

Tests should update from scanning only `react_engine.py` to scanning all Python
files that use `load_react_engine_prompts()` or `PromptConfig.get/format()`.

Behavior preservation details:

- Preserve current tool description and usage-example rendering.
- Preserve current context fields for trunk and branch contexts.
- Preserve the current history truncation rule.
- Preserve stuck-thinking guidance after three thoughts without an action.
- Preserve repository URL stuck guidance.
- Preserve critical-memory content and insertion behavior.
- Preserve defensive behavior when trunk context cannot be loaded.

## `ReActEngine` After Phase One

`ReActEngine` remains the owner of:

- Public construction and repository URL mutation.
- ReAct loop iteration and early-return behavior.
- Step list and iteration counters.
- Thinking/action mode selection policy.
- Tool orchestration adapter methods.
- UI event emission for thoughts, actions, observations, and tool lifecycle
  events.
- State evaluation through `AgentStateEvaluator`.
- Successful-state mutation and physical observation enrichment for now.

`ReActEngine` delegates:

```python
action_capabilities = self.llm_client.capabilities_for(ReactModelMode.ACTION)
self.prompt_builder.build_initial_system_prompt(
    ..., tool_calling_enabled=action_capabilities.supports_function_calling
)
self.prompt_builder.build_next_prompt(
    ..., tool_calling_enabled=action_capabilities.supports_function_calling
)
self.prompt_builder.build_mode_prompt(...)
self.llm_client.get_response(...)
self.response_parser.parse(...)
```

The main loop should read as a sequence of domain operations, not provider,
parser, or prompt mechanics.

## Legacy Method Cleanup

The following methods appear unused in the repository and are candidates for
deletion in this phase:

- `_format_tool_result`
- `_log_llm_request`
- `_log_tool_execution_verbose`
- `test_state_evaluator_integration`

The following legacy completion cluster is also unused by `ReActEngine` because
completion is evaluated through `AgentStateEvaluator`. It should be removed
only after tests confirm behavior still relies on `AgentStateEvaluator`:

- `_is_task_complete`
- `_check_maven_completion`
- `_add_completion_guidance`
- `_check_completion_suggestion`
- `_has_report_been_generated`

Unused imports exposed by this cleanup should be removed. In particular,
`AgentStateAnalysis`, `AgentStatus`, `BranchContext`, `BranchContextHistory`,
and `TrunkContext` should not stay imported in `react_engine.py` if they remain
unused.

## Error Handling

- LLM request failures continue to log and return `None`; the main loop keeps
  the current early-failure path.
- GPT-5 request fallback keeps the current two-attempt behavior.
- Function-call normalization errors fall back to message content when
  available.
- Parser errors should not raise into the main loop for malformed model output;
  malformed output should produce either no steps or the current fallback
  thought.
- Prompt-builder trunk context failures should append the existing "task plan
  unavailable" guidance rather than failing the loop.
- Prompt YAML missing-key validation remains owned by `PromptConfig`.

## Testing Strategy

Add and migrate focused tests before removing old engine methods:

- `tests/test_react_types.py`
  - `ReActStep` / `StepType` import compatibility from `react_engine.py`.
  - `ReactModelMode` lives in `react_types.py`.
  - `AgentStateEvaluator` uses the shared `StepType` rather than a duplicate
    local enum.

- `tests/test_react_response_parser.py`
  - Parses thoughts and actions.
  - Parses action parameters.
  - Handles invalid empty action names.
  - Does not turn model-generated `OBSERVATION:` text into observation steps.
  - Preserves unstructured response fallback for thinking-role and action-role
    models.

- `tests/test_react_prompt_builder.py`
  - Covers initial prompt behavior currently tested through private
    `ReActEngine` methods.
  - Covers next prompt history, stuck guidance, repository URL guidance, and
    mode wrappers.
  - Covers prompt reference comments after they move out of `react_engine.py`.

- `tests/test_react_llm.py` or migrated contract tests
  - Tool schema generation preserves public tool schemas and bash timeout.
  - Claude and OpenAI function-call responses normalize to ReAct text.
  - JSON fallback parser preserves currently supported formats.
  - GPT-5 fallback parameter behavior is characterized with mocked
    `litellm.completion`.
  - GPT-5 and Claude 4.6 can be assigned to either thinking or action roles;
    capabilities are resolved per mode instead of through a global provider
    flag.
  - Thinking/action prompt wrappers are not applied inside the LLM client.

Keep or adjust existing orchestration tests so they continue to exercise
`ReActEngine` as the loop adapter, not as the owner of parser/prompt/provider
details.

Full verification should include:

```text
uv run pytest
uv run black --check src tests
uv run isort --check-only src tests
git diff --check
```

## Implementation Sequence

1. Add `react_types.py`, import the types from `react_engine.py`, and keep
   compatibility imports working.
2. Add `ReActResponseParser` with tests, then delegate `_parse_llm_response()`
   through it or replace direct calls with `self.response_parser.parse(...)`.
3. Add `ReActPromptBuilder` with tests, migrate prompt reference comments, and
   delegate prompt-building calls.
4. Add `ReactLLMClient` with tests, then move LiteLLM setup, capability checks,
   tool schema generation, response normalization, and JSON fallback parsing
   into the client.
5. Update `AgentStateEvaluator` to use the shared `StepType` from
   `react_types.py`.
6. Delete confirmed unused non-completion legacy methods and unused imports.
7. Treat completion-cluster deletion as a separate cleanup step after the core
   extraction passes tests; keep it out of the same risky movement if review or
   tests show uncertainty.
8. Run the full verification suite.
9. Request independent review focused on correctness risk and whether the new
   modules are deep enough to justify their existence.

## Review Checklist

- Does `ReActEngine` read primarily as a loop coordinator after the refactor?
- Can each new module be understood through its public interface without
  reading `ReActEngine` internals?
- Did prompt reference comments stay next to the prompt lookups they describe?
- Did behavior-sensitive fallbacks remain covered by tests?
- Did we avoid extracting runtime state, tracing, and observation enrichment
  before their boundaries are stable?
- Did we avoid adding Co-Authorship or authorship trailers to commits?
