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
- `params: Dict[str, Any]`
- `raw_action_text: Optional[str]`
- `source_step_index: Optional[int]`
- `model_used: Optional[str]`

### `ToolExecution`

Represents the completed result of a tool call, including recovery and
observation data.

Fields:

- `call: ToolCall`
- `result: ToolResult`
- `status: Literal["success", "failure", "recovered", "skipped"]`
- `duration_ms: Optional[float]`
- `observation_text: str`
- `recovery_applied: bool`
- `recovery_strategy: Optional[str]`
- `metadata: Dict[str, Any]`

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
- `context_manager`
- `recent_tool_executions`
- `track_tool_execution: Callable[[str, bool], None]`
- `update_successful_states: Callable[[str, Dict[str, Any], ToolResult], None]`
- `add_system_guidance: Callable[[str, int], None]`
- `event_sink: Optional[Callable[[ToolLifecycleEvent], None]]`
- `logger`

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
- Apply the existing recovery logic, including Maven/Java/project setup/bash
  recovery paths.
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

## UI/Event Handling

The orchestrator must not import `UIManager`, Rich, or UI rendering objects.

Instead, it emits `ToolLifecycleEvent` through an optional callback. `ReActEngine`
or a thin adapter converts those lifecycle events into existing
`UIEventEmitter.emit(...)` calls.

This keeps future UI work flexible:

- CLI/Rich UI can subscribe to tool lifecycle events.
- Tests can capture events without constructing Rich UI state.
- Future telemetry/reporting can consume the same lifecycle stream.

## Recovery Handling

This phase intentionally moves recovery into the orchestration layer, not just
the happy-path execution.

Recovery migration includes:

- repeated tool execution detection
- Java configuration auto-fix
- Maven-focused recovery helpers
- project setup recovery helpers
- bash/working-directory recovery helpers
- system guidance generated as part of recovery

Recovery should still use existing helpers where practical during the
transition, but by the end of the phase the orchestration layer should own the
recovery entry point and decision model.

## Testing Strategy

Add characterization tests before moving behavior.

Required tests:

- `ToolCall` and `ToolExecution` model construction and metadata behavior.
- Successful tool execution emits start/result lifecycle events.
- Missing tool returns a failure `ToolExecution` with a useful `ToolResult`.
- Parameter validation/fix behavior remains compatible with current behavior.
- `ToolError` becomes a failed `ToolExecution` with suggestions and metadata
  preserved.
- Repetitive execution handling preserves the current loop-breaking behavior.
- Recovery path tests cover at least one Maven/Java or project setup recovery
  scenario with fake tools/context.
- `ReActEngine` delegates tool execution to `ToolOrchestrator` without changing
  recorded observation semantics.
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
