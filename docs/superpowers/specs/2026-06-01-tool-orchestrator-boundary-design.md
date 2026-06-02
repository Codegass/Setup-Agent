# Tool Orchestrator Boundary Refactor Design

Date: 2026-06-01
Status: Draft for spec review

## Goal

Create a stable tool orchestration boundary inside SAG so future UI, recovery,
tool execution, and evidence/reporting work can evolve without further growing
`ReActEngine`.

This phase should move the full tool execution lifecycle out of
`src/sag/agent/react_engine.py` while preserving runtime behavior. The main
loop, prompt construction, LLM calls, and `ReActStep` history remain owned by
`ReActEngine`; tool lookup, parameter validation, execution, recovery, and
lifecycle events move behind a new `ToolOrchestrator`.

## Non-Goals

- Do not redesign the ReAct reasoning loop.
- Do not rewrite prompt content or LLM request/response handling.
- Do not split `ReportTool` or `PhysicalValidator` in this phase.
- Do not change public CLI behavior.
- Do not introduce a new UI renderer or change Rich `UIManager` rendering.
- Do not reintroduce the skill-engine feature that was intentionally excluded
  during the `origin/ui` merge resolution.

## Constraints

- Preserve existing behavior first; this is a boundary refactor, not a feature
  release.
- Keep commit messages free of Co-Authorship or similar authorship trailers.
- Do not include unrelated worktree changes.
- Add characterization tests before moving complex execution/recovery logic.
- Prefer internal typed contracts over loosely shared dictionaries.

## Current Problem

`ReActEngine` currently owns too many responsibilities:

- LLM loop and prompt/session management
- action parsing and function-calling response handling
- tool schema construction
- tool lookup and parameter validation/fixing
- tool execution and `ToolError` conversion
- repeated execution detection and loop breaking
- successful-state updates
- UI event emission for thought/action/observation flow
- domain-specific error recovery and auto-fix behavior
- observation formatting and execution summaries

This makes later changes risky. UI, recovery, and reporting all depend on tool
execution, but the execution lifecycle is embedded inside a large class rather
than exposed through a focused interface.

## Proposed Architecture

Add a new module:

```text
src/sag/agent/tool_orchestration.py
```

This module owns the tool execution lifecycle through a `ToolOrchestrator`.
`ReActEngine` remains the owner of the reasoning loop and delegates each tool
call to the orchestrator.

High-level flow:

```text
ReActEngine
  parses model response into ReActStep/action intent
  creates ToolCall
  calls ToolOrchestrator.execute(call)
  receives ToolExecution
  records ReActStep observation
  appends prompt context / continues loop

ToolOrchestrator
  finds tool
  validates/fixes parameters
  detects repetition
  executes safe_execute
  applies recovery if needed
  updates execution tracking
  emits lifecycle callbacks
  returns ToolExecution
```

## Data Models

The following models are internal runtime contracts. They should be documented
and tested, but not promised as public package API yet.

### `ToolCall`

Represents a normalized request to invoke a tool.

Fields:

- `name: str`
- `raw_params: Dict[str, Any]`
- `validated_params: Optional[Dict[str, Any]]`
- `parameter_fixes: List[ParameterFix]`
- `execution_signature: Optional[str]`
- `raw_action_text: Optional[str]`
- `source_step_index: Optional[int]`
- `model_used: Optional[str]`

`raw_params` are the parameters exactly parsed from the model response.
`validated_params` are the parameters after schema aliasing, default injection,
and existing `_validate_and_fix_parameters` behavior. `execution_signature` is
computed from the validated parameters when available, so repetition detection
does not accidentally key off unnormalized model output.

### `ParameterFix`

Represents a validation or normalization change made before execution.

Fields:

- `field: str`
- `before: Any`
- `after: Any`
- `reason: str`
- `source: Literal["schema_alias", "default", "state_injection", "safety_fix"]`

### `ToolExecution`

Represents the completed result of a tool call, including recovery and
observation data.

Fields:

- `call: ToolCall`
- `result: ToolResult`
- `status: Literal["success", "failure", "missing_tool", "validation_failed", "repetition_blocked", "recovery_attempted", "recovered", "recovery_failed", "exception"]`
- `raw_params: Dict[str, Any]`
- `validated_params: Optional[Dict[str, Any]]`
- `executed_params: Optional[Dict[str, Any]]`
- `duration_ms: Optional[float]`
- `observation_text: str`
- `recovery_applied: bool`
- `recovery_strategy: Optional[str]`
- `attempted_execution: bool`
- `parameter_fixes: List[ParameterFix]`
- `metadata: Dict[str, Any]`

`status` describes the lifecycle outcome. `result.success` still describes the
success flag on the returned `ToolResult`. These values normally align, but
they are intentionally separate so handled failures such as timeout guidance can
be represented without pretending that the original tool operation succeeded.

Status transitions:

| Condition | Tool execution | Recovery | Final status |
| --- | --- | --- | --- |
| Tool name is unknown | No | No | `missing_tool` |
| Parameters cannot be validated or safely fixed | No | No | `validation_failed` |
| Repetition policy blocks the call before execution | No | May add guidance | `repetition_blocked` |
| Tool executes and returns success | Yes | No | `success` |
| Tool executes and returns failure, no strategy applies | Yes | No | `failure` |
| Tool executes and returns failure, strategy returns handled guidance without retry | Yes | Guidance only | `recovery_attempted` |
| Tool executes and returns failure, retry or repair succeeds | Yes | Yes | `recovered` |
| Tool executes and returns failure, retry or repair fails | Yes | Yes | `recovery_failed` |
| Tool lookup, validation, execution, or recovery raises unexpectedly | Partial | Maybe | `exception` |

When recovery changes parameters, `executed_params` contains the final
parameters used for the successful or failed replacement execution. If no tool
was called, `executed_params` is `None`.

### `RecoveryDecision`

Represents whether and how recovery should run after a failed tool execution.

Fields:

- `should_recover: bool`
- `strategy: Optional[str]`
- `guidance: Optional[str]`
- `replacement_result: Optional[ToolResult]`
- `metadata: Dict[str, Any]`

### `ToolLifecycleEvent`

Represents a tool lifecycle notification. The orchestrator emits these through
an injected callback/sink instead of importing or depending on `UIManager`.

Fields:

- `event_type: Literal["tool_start", "tool_parameters_fixed", "tool_result", "tool_recovery", "tool_error"]`
- `call: ToolCall`
- `message: str`
- `level: Literal["debug", "info", "warning", "error", "success"]`
- `metadata: Dict[str, Any]`

## `ToolOrchestrator`

Constructor dependencies:

- `tools: Dict[str, BaseTool]`
- `context_manager: ContextManagerProtocol`
- `recent_tool_executions: MutableSequence[ToolExecutionRecord]`
- `track_tool_execution: Callable[[str, bool], None]`
- `update_successful_states: Callable[[str, Dict[str, Any], ToolResult], None]`
- `add_system_guidance: Callable[[str, GuidancePriority], None]`
- `event_sink: Optional[Callable[[ToolLifecycleEvent], None]]`
- `logger`

The first implementation can keep these protocols narrow. They should expose
only the methods the orchestrator actually needs, such as context loading,
orchestrator command execution for recovery, and successful-state mutation.
`GuidancePriority` should preserve current behavior, including numeric
priorities and existing string priorities such as `"high"`.

Primary method:

```python
def execute(self, call: ToolCall) -> ToolExecution:
    ...
```

Responsibilities:

- Validate that the requested tool exists.
- Apply the existing parameter validation/fix behavior.
- Detect repetitive executions and preserve existing loop-breaking behavior.
- Execute `tool.safe_execute(**validated_params)`.
- Convert `ToolError` and unexpected exceptions into `ToolResult`.
- Update tool execution tracking.
- Update successful state through the injected callback.
- Apply the existing recovery logic across the full current recovery surface.
- Return a normalized `ToolExecution`.

## ReActEngine Changes

`ReActEngine` should keep these responsibilities:

- LLM setup and model capability checks
- prompt construction
- function-calling response parsing
- action/thought/observation step storage
- context switching
- deciding when the overall ReAct loop continues or stops
- execution summary generation

`ReActEngine` should delegate these responsibilities:

- `_validate_and_fix_parameters`
- direct `tool.safe_execute` calls inside `_execute_steps`
- repetitive execution loop-breaking mechanics
- `_attempt_error_recovery` and related recovery helpers
- auto-fix methods that are part of failed tool execution handling
- execution lifecycle event emission

The migration should be incremental. The first implementation step may create
the orchestrator and route only a narrow execution path through it, but the
phase is complete only when the full recovery surface is owned by the
orchestrator.

Boundary decisions for this phase:

- `ReActEngine` keeps full ReAct step storage, prompt context assembly, branch
  history, output-history storage, and execution summary generation.
- `ToolOrchestrator` may return `observation_text` for the tool-centric
  observation, but `ReActEngine` still decides how that text is attached to the
  current `ReActStep` and prompt history.
- Existing physical validation and report evidence enrichment remain in their
  current tools/report paths. The orchestrator can pass metadata through, but it
  does not own the evidence model in this phase.
- The implementation plan must explicitly list any helper that remains on
  `ReActEngine` after the phase and why it is still loop-level rather than
  execution-lifecycle behavior.

## UI/Event Handling

The orchestrator must not import `UIManager`, Rich, or UI rendering objects.

Instead, it emits `ToolLifecycleEvent` through an optional callback. `ReActEngine`
or a thin adapter converts those lifecycle events into existing
`UIEventEmitter.emit(...)` calls.

This keeps future UI work flexible:

- CLI/Rich UI can subscribe to tool lifecycle events.
- Tests can capture events without constructing Rich UI state.
- Future telemetry/reporting can consume the same lifecycle stream.

Required event metadata:

- `tool_start`: tool name, source step index, raw params, and execution
  signature when available.
- `tool_parameters_fixed`: raw params, validated params, `ParameterFix` entries,
  and a boolean `params_changed`.
- `tool_result`: status, duration, result success flag, error code, executed
  params, and whether recovery was applied.
- `tool_recovery`: recovery strategy, attempted flag, success flag, guidance,
  replacement result success, recovery params, and parameter diff.
- `tool_error`: error code, error category, suggestions, original error message,
  and whether recovery was attempted.

## Recovery Handling

This phase intentionally moves recovery into the orchestration layer, not just
the happy-path execution.

Recovery migration includes:

- repeated tool execution detection and loop-breaking guidance
- Java configuration auto-fix triggered by repeated execution loop breaking
- `manage_context` recovery for missing active tasks and invalid task IDs
- Java version mismatch recovery through the system tool and Maven retry
- Maven recovery for known working directory reuse, compile-before-test,
  automatic `pom.xml` discovery, failed module/test exclusions, and timeout
  guidance
- Gradle recovery for timeout guidance, known working directory reuse, and
  compile-before-test fallback
- project setup recovery for repository URL injection
- bash recovery for timeout guidance, workspace recreation, and known working
  directory retry
- `file_io` recovery for read-path repair using the known working directory
- generic fallback recovery for tools without a specialized strategy
- system guidance generated as part of recovery

Recovery should still use existing helpers where practical during the
transition, but by the end of the phase the orchestration layer should own the
recovery entry point and decision model.

The implementation plan must audit the current `_attempt_error_recovery` entry
point, every `_recover_*_error` helper, and loop-breaking auto-fix helpers such
as `_auto_fix_java_configuration`. Each current branch must be mapped to one of
these outcomes:

- migrated into a named orchestrator recovery strategy
- retained outside the orchestrator because it is loop-level behavior
- removed with an explicit behavior-preservation justification

Silent omission is not acceptable for this phase.

## Testing Strategy

Add characterization tests before moving behavior.

Required tests:

- `ToolCall` and `ToolExecution` model construction and metadata behavior.
- Raw, validated, and executed parameter lifecycle behavior, including
  `ParameterFix` entries and parameter diffs.
- Every `ToolExecution.status` value, including missing tool, validation
  failure, repetition blocked, guidance-only recovery, recovered success,
  recovery failure, and unexpected exception.
- Successful tool execution emits start/result lifecycle events.
- Lifecycle event payloads include the required metadata for each event type.
- Missing tool returns a failure `ToolExecution` with a useful `ToolResult`.
- Parameter validation/fix behavior remains compatible with current behavior.
- `ToolError` becomes a failed `ToolExecution` with suggestions and metadata
  preserved.
- Repetitive execution handling preserves the current loop-breaking behavior.
- Repetition-triggered Java configuration auto-fix remains covered when the
  loop-breaking path moves to the orchestrator.
- Recovery characterization tests cover every current recovery category:
  `manage_context`, Java/Maven version repair, Maven working-directory repair,
  Maven compile-before-test, Maven `pom.xml` discovery, Maven module/test
  exclusion, Maven timeout guidance, Gradle timeout guidance, Gradle
  working-directory repair, Gradle compile fallback, project setup repository
  URL injection, bash timeout guidance, bash workspace recreation, bash
  working-directory repair, `file_io` path repair, generic fallback, and system
  guidance emission.
- `ReActEngine` delegates tool execution to `ToolOrchestrator` without changing
  recorded observation semantics, branch/output history storage, or prompt
  context assembly.
- Existing import, static import, schema, result/state, report, and packaging
  tests remain passing.

Testing should avoid Docker and LLM integration in this phase. Use fake tools,
fake context managers, and direct method calls to keep the suite deterministic.

## Migration Plan Shape

The implementation plan should break this into guarded steps:

1. Add tool orchestration models and tests.
2. Add a minimal `ToolOrchestrator` for successful execution.
3. Route a narrow `ReActEngine` execution path through the orchestrator.
4. Move parameter validation/fix behavior.
5. Move repetitive execution handling.
6. Move recovery entry point and recovery helpers.
7. Add lifecycle event sink and adapter to existing UI emission.
8. Run full verification and independent correctness review.

Each step should keep tests green and avoid broad behavior changes.

## Success Criteria

- `ReActEngine` no longer directly owns the tool execution lifecycle.
- `ToolOrchestrator` owns tool lookup, validation/fix, execution, recovery, and
  lifecycle events.
- New typed orchestration models are covered by focused tests.
- UI lifecycle events flow through a callback/sink, not direct UI dependency.
- Existing contract and packaging tests still pass.
- No skill-engine functionality is reintroduced.
- Independent review finds no blocking correctness issues.

## Risks And Mitigations

Risk: Moving recovery logic creates subtle behavior regressions.
Mitigation: Add characterization tests first and migrate recovery in small
reviewed steps.

Risk: The orchestrator becomes another large class.
Mitigation: Keep its public surface small. If helper complexity grows, split
private recovery helpers into a second internal module during implementation
planning.

Risk: UI event callbacks become too generic or too UI-shaped.
Mitigation: Use tool lifecycle terminology and keep Rich/UI concepts outside
the event model.

Risk: Data models become prematurely public.
Mitigation: Document them as internal runtime contracts and do not export them
from package `__init__` files in this phase.
