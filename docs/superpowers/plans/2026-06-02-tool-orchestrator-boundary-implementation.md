# Tool Orchestrator Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move SAG tool lookup, parameter normalization, execution, repetition handling, recovery, and tool lifecycle events behind a focused `ToolOrchestrator` while preserving existing ReAct behavior.

**Architecture:** Add internal orchestration contracts plus a `ToolOrchestrator` that returns a normalized `ToolExecution` for each action. Keep `ReActEngine` responsible for the LLM loop, prompt/history assembly, branch/output history, and ReAct step storage; use callbacks and returned metadata for loop-level effects. Keep recovery owned by the orchestration layer, with private recovery helpers split into `tool_recovery.py` so the orchestrator does not become another oversized class.

**Tech Stack:** Python 3.10+, dataclasses, typing `Literal`/`Protocol`, Pydantic `ToolResult`, pytest, uv, black, isort.

---

## Spec Reference

- Design spec: `docs/superpowers/specs/2026-06-01-tool-orchestrator-boundary-design.md`
- Prior review decision: full current recovery surface moves into orchestration; UI events use callback/sink; new runtime models stay internal.
- Constraint: commit messages must not include Co-Authorship or similar authorship trailers.

## File Structure

### Runtime

- Create: `src/sag/agent/tool_orchestration.py`
  - Internal dataclasses: `ParameterFix`, `ToolCall`, `ToolExecution`, `RecoveryDecision`, `ToolLifecycleEvent`, `ToolExecutionRecord`
  - Type aliases: `ToolExecutionStatus`, `ToolLifecycleEventType`, `GuidancePriority`
  - `ToolOrchestrator.execute(call: ToolCall) -> ToolExecution`
  - Tool-result formatting helper used to produce `ToolExecution.observation_text`
- Create: `src/sag/agent/tool_recovery.py`
  - Private `ToolRecoveryHandler`
  - Recovery methods migrated from current `_attempt_error_recovery` and `_recover_*_error` helpers
  - No public package export
- Modify: `src/sag/agent/react_engine.py`
  - Construct `ToolOrchestrator`
  - Convert `ReActStep` actions into `ToolCall`
  - Attach `ToolExecution.result` and `ToolExecution.observation_text` to existing ReAct steps/history
  - Remove delegated execution lifecycle helpers after behavior is covered by tests
- Do not modify: `src/sag/agent/__init__.py`
  - These models are internal runtime contracts, not public package API.

### Tests

- Create: `tests/test_tool_orchestration_models.py`
- Create: `tests/test_tool_orchestration_execution.py`
- Create: `tests/test_tool_orchestration_parameters.py`
- Create: `tests/test_tool_orchestration_repetition.py`
- Create: `tests/test_tool_orchestration_recovery.py`
- Create: `tests/test_react_engine_tool_orchestration.py`
- Modify if needed: existing smoke/contract tests only to update import expectations, not to weaken assertions.

## Recovery Coverage Map

Every current recovery branch must appear in this table before implementation
starts. If implementation discovers an extra branch, update the table and add a
test before moving that branch.

| Current behavior | Strategy name | Expected status | `executed_params` expectation | Required test |
| --- | --- | --- | --- | --- |
| Unknown tool feedback and ErrorLogger entry | `unknown_tool_feedback` | `missing_tool` | `None` | `test_orchestrator_returns_missing_tool_execution_with_existing_feedback` |
| `manage_context` no active task recovery | `manage_context_active_task` | `recovered` or `recovery_failed` | original validated params | `test_manage_context_recovery_uses_single_in_progress_task` |
| Project setup missing repository URL | `project_setup_repository_url` | `recovered` or `recovery_failed` | params with injected `repository_url` | `test_project_setup_recovery_injects_repository_url` |
| Maven Java version mismatch | `maven_java_version` | `recovered` or `recovery_failed` | original Maven params after system repair | `test_maven_java_version_recovery_installs_and_retries` |
| Maven known working directory retry | `maven_known_working_directory` | `recovered` or `recovery_failed` | params with known `working_directory` | `test_maven_working_directory_recovery_retries_known_directory` |
| Maven compile-before-test retry | `maven_compile_before_test` | `recovered` or `recovery_failed` | params with `command="compile"` | `test_maven_compile_before_test_recovery` |
| Maven `pom.xml` discovery retry | `maven_pom_discovery` | `recovered` or `recovery_failed` | params with `pom_file` and discovered `working_directory` | `test_maven_pom_discovery_recovery_targets_detected_pom` |
| Maven failed module/test exclusions | `maven_exclude_modules_or_tests` | `recovered` or `recovery_failed` | params with exclusion `properties` | `test_maven_module_and_test_exclusion_recovery_records_exclusions` |
| Maven timeout guidance | `maven_timeout_guidance` | `recovery_attempted` | original validated params | `test_maven_timeout_returns_guidance_without_retry` |
| Gradle timeout guidance | `gradle_timeout_guidance` | `recovery_attempted` | original validated params | `test_gradle_timeout_returns_guidance_without_retry` |
| Gradle known working directory retry | `gradle_known_working_directory` | `recovered` or `recovery_failed` | params with known `working_directory` | `test_gradle_working_directory_recovery_retries_known_directory` |
| Gradle compile fallback | `gradle_compile_before_test` | `recovered` or `recovery_failed` | params with `task="compileJava"` | `test_gradle_compile_fallback_recovery` |
| Bash timeout guidance | `bash_timeout_guidance` | `recovery_attempted` | original validated params | `test_bash_timeout_guidance_adds_system_guidance` |
| Bash workspace recreation retry | `bash_workspace_recreation` | `recovered` or `recovery_failed` | params with `working_directory="/workspace"` | `test_bash_workspace_recreation_retries_original_command` |
| Bash known working directory retry | `bash_known_working_directory` | `recovered` or `recovery_failed` | params with known `working_directory` | `test_bash_known_working_directory_recovery` |
| File I/O path repair | `file_io_known_working_directory` | `recovered` or `recovery_failed` | params with repaired absolute `path` | `test_file_io_path_recovery_uses_known_working_directory` |
| Generic fallback | `generic_no_strategy` | `failure` | original validated params | `test_generic_recovery_returns_failure_without_silent_success` |
| Repetition-triggered Java auto-fix | `java_configuration_auto_fix` | `recovered` or `recovery_failed` | auto-fix system params in metadata, original call not retried | `test_java_repetition_triggers_auto_fix` |
| Repetition hard break without Java auto-fix | `repetition_force_break` | `repetition_blocked` | `None` | `test_repetition_level_three_breaks_without_execution_and_forces_next_task` |

## Task 1: Add Internal Orchestration Models

**Files:**
- Create: `src/sag/agent/tool_orchestration.py`
- Create: `tests/test_tool_orchestration_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_tool_orchestration_models.py`:

```python
from sag.agent.tool_orchestration import (
    ParameterFix,
    ToolCall,
    ToolExecution,
    ToolLifecycleEvent,
)
from sag.tools.base import ToolResult


def test_tool_call_keeps_raw_and_validated_params_separate():
    call = ToolCall(
        name="bash",
        raw_params={"cmd": "pwd"},
        validated_params={"command": "pwd", "working_directory": "/workspace"},
        parameter_fixes=[
            ParameterFix(
                field="cmd",
                before="pwd",
                after={"command": "pwd"},
                reason="renamed to schema field",
                source="schema_alias",
            )
        ],
        execution_signature="bash:[('command', 'pwd'), ('working_directory', '/workspace')]",
        raw_action_text="ACTION: bash",
        source_step_index=3,
        model_used="action-model",
    )

    assert call.raw_params == {"cmd": "pwd"}
    assert call.validated_params["command"] == "pwd"
    assert call.parameter_fixes[0].source == "schema_alias"


def test_tool_execution_status_is_separate_from_tool_result_success():
    result = ToolResult(
        success=False,
        output="timeout guidance",
        error="timed out",
        error_code="TIMEOUT_HANDLED",
    )
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "mvn test"}),
        result=result,
        status="recovery_attempted",
        raw_params={"command": "mvn test"},
        validated_params={"command": "mvn test", "working_directory": "/workspace"},
        executed_params={"command": "mvn test", "working_directory": "/workspace"},
        duration_ms=12.5,
        observation_text="handled timeout",
        recovery_applied=True,
        recovery_strategy="bash_timeout_guidance",
        attempted_execution=True,
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.success is False
    assert execution.executed_params["working_directory"] == "/workspace"


def test_lifecycle_event_is_ui_agnostic_metadata_carrier():
    call = ToolCall(name="file_io", raw_params={"path": "README.md"})
    event = ToolLifecycleEvent(
        event_type="tool_start",
        call=call,
        message="Starting file_io",
        level="info",
        metadata={"raw_params": call.raw_params},
    )

    assert event.event_type == "tool_start"
    assert event.metadata["raw_params"] == {"path": "README.md"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.agent.tool_orchestration'`.

- [ ] **Step 3: Add model dataclasses and type aliases**

Create `src/sag/agent/tool_orchestration.py` with this initial structure:

```python
"""Internal tool orchestration contracts and execution boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, MutableSequence, Optional, Protocol

from sag.tools.base import BaseTool, ToolResult

ParameterFixSource = Literal["schema_alias", "default", "state_injection", "safety_fix"]
ToolExecutionStatus = Literal[
    "success",
    "failure",
    "missing_tool",
    "validation_failed",
    "repetition_blocked",
    "recovery_attempted",
    "recovered",
    "recovery_failed",
    "exception",
]
ToolLifecycleEventType = Literal[
    "tool_start",
    "tool_parameters_fixed",
    "tool_result",
    "tool_recovery",
    "tool_error",
]
ToolLifecycleLevel = Literal["debug", "info", "warning", "error", "success"]
GuidancePriority = int | str


@dataclass(slots=True)
class ParameterFix:
    field: str
    before: Any
    after: Any
    reason: str
    source: ParameterFixSource


@dataclass(slots=True)
class ToolCall:
    name: str
    raw_params: Dict[str, Any]
    validated_params: Optional[Dict[str, Any]] = None
    parameter_fixes: list[ParameterFix] = field(default_factory=list)
    execution_signature: Optional[str] = None
    raw_action_text: Optional[str] = None
    source_step_index: Optional[int] = None
    model_used: Optional[str] = None


@dataclass(slots=True)
class ToolExecutionRecord:
    signature: str
    success: bool
    timestamp: str


@dataclass(slots=True)
class RecoveryDecision:
    should_recover: bool
    strategy: Optional[str] = None
    guidance: Optional[str] = None
    replacement_result: Optional[ToolResult] = None
    replacement_params: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolExecution:
    call: ToolCall
    result: ToolResult
    status: ToolExecutionStatus
    raw_params: Dict[str, Any]
    validated_params: Optional[Dict[str, Any]] = None
    executed_params: Optional[Dict[str, Any]] = None
    duration_ms: Optional[float] = None
    observation_text: str = ""
    recovery_applied: bool = False
    recovery_strategy: Optional[str] = None
    attempted_execution: bool = False
    parameter_fixes: list[ParameterFix] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolLifecycleEvent:
    event_type: ToolLifecycleEventType
    call: ToolCall
    message: str
    level: ToolLifecycleLevel = "info"
    metadata: Dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run model tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/tool_orchestration.py tests/test_tool_orchestration_models.py
git commit -m "Add tool orchestration models"
```

Do not add Co-Authorship.

## Task 2: Add Minimal ToolOrchestrator Execution

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Create: `tests/test_tool_orchestration_execution.py`

- [ ] **Step 1: Write failing execution tests**

Create `tests/test_tool_orchestration_execution.py`:

```python
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolError, ToolResult


class EchoTool(BaseTool):
    def __init__(self):
        super().__init__("echo", "Echo test tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=f"ran {command}", metadata={"command": command})


def test_orchestrator_executes_successful_tool_and_emits_events():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"
    assert execution.attempted_execution is True
    assert execution.executed_params == {"command": "pwd"}
    assert "echo executed successfully" in execution.observation_text
    assert [event.event_type for event in events] == ["tool_start", "tool_result"]


def test_orchestrator_returns_missing_tool_execution_with_existing_feedback():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"bash": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="ls", raw_params={"path": "/workspace"}))

    assert execution.status == "missing_tool"
    assert execution.result.success is False
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert "Tool 'ls' does not exist" in execution.result.output
    assert "Did you mean: bash" in execution.result.output
    assert events[-1].event_type == "tool_error"


def test_tool_error_metadata_and_suggestions_are_preserved():
    class ErrorTool(BaseTool):
        def __init__(self):
            super().__init__("error_tool", "Error tool")

        def execute(self, command: str) -> ToolResult:
            raise ToolError(
                "bad input",
                category="validation",
                error_code="BAD_INPUT",
                suggestions=["try a better command"],
                details={"command": command},
                retryable=True,
            )

    orchestrator = ToolOrchestrator(
        tools={"error_tool": ErrorTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="error_tool", raw_params={"command": "bad"}))

    assert execution.status == "failure"
    assert execution.result.error_code == "BAD_INPUT"
    assert execution.result.suggestions == ["try a better command"]
    assert execution.result.metadata["failure_category"] == "validation"
    assert execution.result.metadata["retryable"] is True


def test_unexpected_safe_execute_exception_returns_exception_status():
    class ExplodingTool(BaseTool):
        def __init__(self):
            super().__init__("explode", "Exploding tool")

        def execute(self, command: str) -> ToolResult:
            return ToolResult(success=True, output="unused")

        def safe_execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("boom")

    orchestrator = ToolOrchestrator(
        tools={"explode": ExplodingTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="explode", raw_params={"command": "pwd"}))

    assert execution.status == "exception"
    assert execution.result.success is False
    assert execution.result.error_code == "TOOL_EXECUTION_EXCEPTION"
    assert execution.attempted_execution is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_execution.py -v
```

Expected: FAIL because `ToolOrchestrator` is not defined.

- [ ] **Step 3: Implement minimal orchestrator**

Add to `src/sag/agent/tool_orchestration.py`:

```python
class ToolOrchestrator:
    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        context_manager: Any,
        recent_tool_executions: MutableSequence[dict[str, Any] | ToolExecutionRecord],
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        track_tool_execution: Callable[[str, bool], None],
        update_successful_states: Callable[[str, Dict[str, Any], ToolResult], None],
        add_system_guidance: Callable[[str, GuidancePriority], None],
        get_timestamp: Callable[[], str],
        event_sink: Optional[Callable[[ToolLifecycleEvent], None]] = None,
        logger: Any = None,
    ):
        self.tools = tools
        self.context_manager = context_manager
        self.recent_tool_executions = recent_tool_executions
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.track_tool_execution = track_tool_execution
        self.update_successful_states = update_successful_states
        self.add_system_guidance = add_system_guidance
        self.get_timestamp = get_timestamp
        self.event_sink = event_sink
        self.logger = logger

    def execute(self, call: ToolCall) -> ToolExecution:
        self._emit(
            "tool_start",
            call,
            f"Starting {call.name}",
            metadata={"raw_params": call.raw_params, "source_step_index": call.source_step_index},
        )

        if call.name not in self.tools:
            feedback = self._generate_unknown_tool_feedback(call.name)
            self._log_unknown_tool_attempt(call, feedback)
            result = ToolResult(
                success=False,
                output=feedback,
                error=f"Unknown tool requested: {call.name}",
                error_code="UNKNOWN_TOOL",
                suggestions=["Use one of the available tools listed in the feedback"],
                metadata={"available_tools": sorted(self.tools)},
            )
            execution = ToolExecution(
                call=call,
                result=result,
                status="missing_tool",
                raw_params=call.raw_params,
                attempted_execution=False,
                observation_text=format_tool_result(call.name, result),
            )
            self._emit("tool_error", call, result.error or "", level="error", metadata=execution.metadata)
            return execution

        validated_params = dict(call.validated_params or call.raw_params)
        call.validated_params = validated_params
        signature = call.execution_signature or self._build_execution_signature(call.name, validated_params)
        call.execution_signature = signature

        try:
            result = self.tools[call.name].safe_execute(**validated_params)
        except Exception as exc:
            result = ToolResult(
                success=False,
                output="",
                error=f"Tool execution raised unexpectedly: {exc}",
                error_code="TOOL_EXECUTION_EXCEPTION",
                metadata={"exception_type": type(exc).__name__},
            )
            execution = ToolExecution(
                call=call,
                result=result,
                status="exception",
                raw_params=call.raw_params,
                validated_params=validated_params,
                executed_params=validated_params,
                observation_text=format_tool_result(call.name, result),
                attempted_execution=True,
            )
            self._emit("tool_error", call, result.error or "", level="error", metadata=execution.metadata)
            return execution

        self.track_tool_execution(signature, result.success)
        if result.success:
            self.update_successful_states(call.name, validated_params, result)

        execution = ToolExecution(
            call=call,
            result=result,
            status="success" if result.success else "failure",
            raw_params=call.raw_params,
            validated_params=validated_params,
            executed_params=validated_params,
            observation_text=format_tool_result(call.name, result),
            attempted_execution=True,
        )
        self._emit(
            "tool_result",
            call,
            f"{call.name} finished",
            level="success" if result.success else "error",
            metadata={
                "status": execution.status,
                "result_success": result.success,
                "error_code": result.error_code,
                "executed_params": validated_params,
                "recovery_applied": False,
            },
        )
        return execution

    def _build_execution_signature(self, tool_name: str, params: Dict[str, Any]) -> str:
        return f"{tool_name}:{str(sorted(params.items()))}"

    def _emit(
        self,
        event_type: ToolLifecycleEventType,
        call: ToolCall,
        message: str,
        *,
        level: ToolLifecycleLevel = "info",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.event_sink:
            self.event_sink(
                ToolLifecycleEvent(
                    event_type=event_type,
                    call=call,
                    message=message,
                    level=level,
                    metadata=metadata or {},
                )
            )
```

Also add:

- `format_tool_result(tool_name: str, result: ToolResult) -> str` by copying current `ReActEngine._format_tool_result` behavior without changing text.
- `_generate_unknown_tool_feedback(requested_tool: str) -> str` by copying current `ReActEngine._generate_unknown_tool_feedback` behavior without changing text.
- `_log_unknown_tool_attempt(call: ToolCall, feedback: str) -> None`, preserving the current `ErrorLogger.get_instance().log_unknown_tool(...)` behavior best-effort. This method must catch exceptions and never fail the tool execution.

- [ ] **Step 4: Run execution tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_execution.py -v
```

Expected: PASS.

- [ ] **Step 5: Run model and execution tests together**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py tests/test_tool_orchestration_execution.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sag/agent/tool_orchestration.py tests/test_tool_orchestration_execution.py
git commit -m "Add minimal tool orchestrator execution"
```

Do not add Co-Authorship.

## Task 3: Add ReActEngine Orchestrator Adapter Without Production Routing

**Files:**
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_react_engine_tool_orchestration.py`

- [ ] **Step 1: Write failing adapter tests**

Create `tests/test_react_engine_tool_orchestration.py`:

```python
from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine, ReActStep, StepType
from sag.agent.tool_orchestration import ToolCall, ToolExecution
from sag.tools.base import ToolResult


def make_engine():
    engine = ReActEngine.__new__(ReActEngine)
    engine.steps = []
    engine.current_iteration = 1
    engine.config = SimpleNamespace(verbose=False)
    engine.tools = {"example": object()}
    engine.repository_url = None
    engine.context_manager = SimpleNamespace(current_task_id=None)
    engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda name: None)
    engine.agent_logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    engine._force_thinking_after_success = False
    engine._force_thinking_next = False
    engine.recent_tool_executions = []
    engine.successful_states = {}
    engine.emit = lambda *args, **kwargs: None
    engine._log_react_step_verbose = lambda step: None
    engine._log_tool_result_verbose = lambda tool_name, result: None
    engine._add_observation_step = lambda content: engine.steps.append(
        ReActStep(step_type=StepType.OBSERVATION, content=content, timestamp="ts")
    )
    engine._get_timestamp = lambda: "ts"
    engine._invalidate_trunk_cache = lambda: None
    return engine


def test_react_engine_builds_tool_call_from_action_step():
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: example",
        tool_name="example",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="model",
    )
    engine = make_engine()

    call = engine._build_tool_call_from_step(step)

    assert call.name == "example"
    assert call.raw_params == {"command": "pwd"}
    assert call.raw_action_text == "ACTION: example"
    assert call.source_step_index == 1
    assert call.model_used == "model"


def test_react_engine_applies_tool_execution_loop_effects():
    engine = make_engine()
    execution = ToolExecution(
        call=ToolCall(name="example", raw_params={"command": "pwd"}),
        result=ToolResult(success=True, output="ok"),
        status="success",
        raw_params={"command": "pwd"},
        validated_params={"command": "pwd"},
        executed_params={"command": "pwd"},
        observation_text="formatted observation",
        attempted_execution=True,
        metadata={"force_thinking_next": True, "invalidate_trunk_cache": True},
    )

    invalidated = []
    engine._invalidate_trunk_cache = lambda: invalidated.append(True)
    engine._apply_tool_execution_loop_effects(execution)

    assert engine._force_thinking_next is True
    assert invalidated == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_react_engine_tool_orchestration.py -v
```

Expected: FAIL because the ReActEngine adapter methods do not exist yet.

- [ ] **Step 3: Add `_get_tool_orchestrator` factory**

Modify `src/sag/agent/react_engine.py` imports:

```python
from sag.agent.tool_orchestration import ToolCall, ToolExecution, ToolLifecycleEvent, ToolOrchestrator
```

Add method near other tool-execution helpers:

```python
def _get_tool_orchestrator(self) -> ToolOrchestrator:
    return ToolOrchestrator(
        tools=self.tools,
        context_manager=self.context_manager,
        recent_tool_executions=self.recent_tool_executions,
        successful_states=self.successful_states,
        repository_url=self.repository_url,
        track_tool_execution=self._track_tool_execution,
        update_successful_states=self._update_successful_states,
        add_system_guidance=self._add_system_guidance,
        get_timestamp=self._get_timestamp,
        event_sink=self._handle_tool_lifecycle_event,
        logger=logger,
    )
```

Add a temporary event adapter:

```python
def _handle_tool_lifecycle_event(self, event: ToolLifecycleEvent) -> None:
    # Detailed mapping is completed in Task 10. Keep this no-op for narrow routing.
    return None
```

- [ ] **Step 4: Add action-to-call and loop-effect helpers**

Add:

```python
def _build_tool_call_from_step(self, step: ReActStep) -> ToolCall:
    return ToolCall(
        name=step.tool_name or "",
        raw_params=step.tool_params or {},
        raw_action_text=step.content,
        source_step_index=self.current_iteration,
        model_used=step.model_used,
    )
```

Add:

```python
def _apply_tool_execution_loop_effects(self, execution: ToolExecution) -> None:
    if execution.metadata.get("force_thinking_next"):
        self._force_thinking_next = True
    if execution.metadata.get("invalidate_trunk_cache"):
        self._invalidate_trunk_cache()
    if execution.metadata.get("force_next_task") and hasattr(self.context_manager, "force_next_task"):
        self.context_manager.force_next_task()
```

Do not route `_execute_steps` through the orchestrator in this task. Production
execution must keep using the existing `ReActEngine` path until Tasks 4-9 have
migrated validation, repetition, and recovery; otherwise this refactor would
temporarily drop behavior the spec requires preserving.

- [ ] **Step 5: Run delegation tests**

Run:

```bash
uv run pytest tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 6: Run existing contract smoke tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_tool_contracts.py tests/test_result_state_contracts.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/react_engine.py tests/test_react_engine_tool_orchestration.py
git commit -m "Add React orchestrator adapter"
```

Do not add Co-Authorship.

## Task 4: Move Parameter Validation And ParameterFix Tracking

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Modify: `src/sag/agent/react_engine.py`
- Create: `tests/test_tool_orchestration_parameters.py`

- [ ] **Step 1: Write parameter characterization tests**

Create `tests/test_tool_orchestration_parameters.py`:

```python
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class CommandTool(BaseTool):
    def __init__(self):
        super().__init__("bash", "Command tool")

    def execute(self, command: str, working_directory: str = "/workspace", raw_output: bool = False) -> ToolResult:
        return ToolResult(success=True, output=f"{command}@{working_directory}:{raw_output}")


def make_orchestrator(successful_states=None, repository_url=None):
    return ToolOrchestrator(
        tools={"bash": CommandTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states=successful_states or {"working_directory": "/workspace/project"},
        repository_url=repository_url,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )


def test_parameter_alias_default_and_state_injection_are_recorded():
    execution = make_orchestrator().execute(
        ToolCall(name="bash", raw_params={"cmd": "pwd", "raw_output": "true"})
    )

    assert execution.status == "success"
    assert execution.raw_params == {"cmd": "pwd", "raw_output": "true"}
    assert execution.validated_params == {
        "command": "pwd",
        "working_directory": "/workspace/project",
        "raw_output": True,
    }
    assert execution.executed_params == execution.validated_params
    assert {fix.field for fix in execution.parameter_fixes} >= {"cmd", "working_directory", "raw_output"}


def test_validation_failed_status_when_fixing_raises(monkeypatch):
    orchestrator = make_orchestrator()
    monkeypatch.setattr(orchestrator, "_validate_and_fix_parameters", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "pwd"}))

    assert execution.status == "validation_failed"
    assert execution.result.success is False
    assert execution.attempted_execution is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_parameters.py -v
```

Expected: FAIL because orchestrator does not yet own validation/fix behavior or `ParameterFix` records.

- [ ] **Step 3: Move validation helpers**

Move these methods from `ReActEngine` to `ToolOrchestrator` without behavior changes:

```text
_validate_and_fix_parameters
_fix_parameters_against_schema
_get_smart_default
_get_tool_specific_action_default
_convert_parameter_type
_fix_parameter_names
_apply_basic_parameter_fixes
_apply_tool_specific_fixes
```

During the move:

- Use `self.tools`, `self.repository_url`, and `self.successful_states` from the orchestrator.
- Convert existing log-only changes into `ParameterFix` entries where the before/after value is known.
- Keep the old behavior of not deleting unexpected parameters unless current code already deletes them.
- On validation exceptions, return a `ToolExecution` with `status="validation_failed"`, `result.error_code="PARAMETER_VALIDATION_FAILED"`, and no tool execution.

- [ ] **Step 4: Add helper to record fixes**

Add to `ToolOrchestrator`:

```python
def _add_parameter_fix(
    self,
    fixes: list[ParameterFix],
    *,
    field: str,
    before: Any,
    after: Any,
    reason: str,
    source: ParameterFixSource,
) -> None:
    if before != after:
        fixes.append(ParameterFix(field=field, before=before, after=after, reason=reason, source=source))
```

Use this helper in schema aliasing, default injection, state injection, and safety fixes.

- [ ] **Step 5: Emit `tool_parameters_fixed` when params change**

When `parameter_fixes` is not empty, emit:

```python
self._emit(
    "tool_parameters_fixed",
    call,
    f"Parameters fixed for {call.name}",
    metadata={
        "raw_params": call.raw_params,
        "validated_params": validated_params,
        "parameter_fixes": parameter_fixes,
        "params_changed": True,
    },
)
```

- [ ] **Step 6: Run parameter and execution tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_parameters.py tests/test_tool_orchestration_execution.py -v
```

Expected: PASS.

- [ ] **Step 7: Run import/contract tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_tool_contracts.py tests/test_result_state_contracts.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/react_engine.py tests/test_tool_orchestration_parameters.py
git commit -m "Move tool parameter normalization to orchestrator"
```

Do not add Co-Authorship.

## Task 5: Move Execution Tracking And Successful-State Updates

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_tool_orchestration_execution.py`

- [ ] **Step 1: Add tracking and state tests**

Append to `tests/test_tool_orchestration_execution.py`:

```python
def test_successful_execution_tracks_signature_and_updates_state():
    tracked = []
    updated = []
    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracked.append((signature, success)),
        update_successful_states=lambda tool_name, params, result: updated.append((tool_name, params, result.output)),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert tracked == [(execution.call.execution_signature, True)]
    assert updated == [("echo", {"command": "pwd"}, "ran pwd")]


def test_failed_execution_tracks_failure_without_successful_state_update():
    class FailingTool(BaseTool):
        def __init__(self):
            super().__init__("fail", "Failing tool")

        def execute(self, command: str) -> ToolResult:
            return ToolResult(success=False, output="", error="nope", error_code="NOPE")

    tracked = []
    updated = []
    orchestrator = ToolOrchestrator(
        tools={"fail": FailingTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: tracked.append((signature, success)),
        update_successful_states=lambda tool_name, params, result: updated.append(tool_name),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="fail", raw_params={"command": "pwd"}))

    assert execution.status == "failure"
    assert tracked == [(execution.call.execution_signature, False)]
    assert updated == []
```

- [ ] **Step 2: Run tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_execution.py -v
```

Expected: PASS.

- [ ] **Step 3: Move cache invalidation signal**

`ReActEngine` currently invalidates trunk cache after successful context-changing `manage_context` actions. Keep actual cache mutation in `ReActEngine`, but have the orchestrator set metadata when it updates successful state:

```python
if result.success and call.name == "manage_context":
    action = validated_params.get("action", "")
    if action in {
        "start_task",
        "complete_task",
        "complete_with_results",
        "add_context",
        "compact_context",
        "create_branch",
        "switch_to_trunk",
        "switch_to_branch",
    }:
        execution.metadata["invalidate_trunk_cache"] = True
```

`ReActEngine._apply_tool_execution_loop_effects()` remains responsible for calling `_invalidate_trunk_cache()`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_execution.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/react_engine.py tests/test_tool_orchestration_execution.py tests/test_react_engine_tool_orchestration.py
git commit -m "Stabilize orchestrator execution tracking"
```

Do not add Co-Authorship.

## Task 6: Move Repetition Handling And Java Auto-Fix

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Modify: `src/sag/agent/react_engine.py`
- Create: `tests/test_tool_orchestration_repetition.py`

- [ ] **Step 1: Write repetition characterization tests**

Create `tests/test_tool_orchestration_repetition.py`:

```python
from types import SimpleNamespace

from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class EchoTool(BaseTool):
    def __init__(self, name="bash"):
        super().__init__(name, "Echo")

    def execute(self, command: str = "pwd", working_directory: str = "/workspace") -> ToolResult:
        return ToolResult(success=True, output="ok")


def make_orchestrator(recent, tools=None, guidance=None):
    return ToolOrchestrator(
        tools=tools or {"bash": EchoTool("bash")},
        context_manager=SimpleNamespace(get_current_context=lambda: "needs Java 17"),
        recent_tool_executions=recent,
        successful_states={"working_directory": "/workspace"},
        repository_url=None,
        track_tool_execution=lambda signature, success: recent.append({"signature": signature, "success": success, "timestamp": "ts"}),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=(guidance or (lambda message, priority=5: None)),
        get_timestamp=lambda: "ts",
    )


def test_repetition_level_one_executes_with_warning_output():
    recent = [
        {"signature": "bash:[('command', 'pwd'), ('working_directory', '/workspace')]", "success": False, "timestamp": "1"},
        {"signature": "bash:[('command', 'pwd'), ('working_directory', '/workspace')]", "success": False, "timestamp": "2"},
        {"signature": "bash:[('command', 'pwd'), ('working_directory', '/workspace')]", "success": False, "timestamp": "3"},
    ]

    execution = make_orchestrator(recent).execute(ToolCall(name="bash", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert "REPETITIVE EXECUTION WARNING" in execution.result.output
    assert execution.metadata["repetition_level"] == 1


def test_repetition_level_three_breaks_without_execution_and_forces_next_task():
    recent = [
        {"signature": f"bash:{i}", "success": False, "timestamp": str(i)}
        for i in range(8)
    ]
    execution = make_orchestrator(recent).execute(ToolCall(name="bash", raw_params={"command": "pwd"}))

    assert execution.status == "repetition_blocked"
    assert execution.result.error_code == "INFINITE_LOOP_BROKEN"
    assert execution.metadata["force_next_task"] is True


def test_java_repetition_triggers_auto_fix():
    class SystemTool(BaseTool):
        def __init__(self):
            super().__init__("system", "System")

        def execute(self, action: str, java_version: str | None = None) -> ToolResult:
            if action == "install_java":
                return ToolResult(success=True, output="installed")
            return ToolResult(success=False, output="needs install")

    recent = [
        {"signature": "bash:[('command', 'update-alternatives')]", "success": False, "timestamp": str(i)}
        for i in range(8)
    ]
    execution = make_orchestrator(
        recent,
        tools={"bash": EchoTool("bash"), "system": SystemTool()},
    ).execute(ToolCall(name="bash", raw_params={"command": "update-alternatives"}))

    assert execution.status == "recovered"
    assert execution.result.metadata["auto_fixed"] is True
    assert execution.recovery_strategy == "java_configuration_auto_fix"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_repetition.py -v
```

Expected: FAIL because repetition behavior still lives in `ReActEngine` or has incomplete status metadata.

- [ ] **Step 3: Move repetition helpers**

Move these methods to `ToolOrchestrator`:

```text
_get_repetition_level
_generate_alternative_suggestions
_auto_fix_java_configuration
```

Keep `_track_tool_execution` as an injected callback for now so the existing bounded-history behavior remains centralized until cleanup.

- [ ] **Step 4: Preserve loop-level effects through metadata**

When repetition level 2 occurs:

```python
execution.metadata["force_thinking_next"] = True
```

When level 3 blocks:

```python
execution.status = "repetition_blocked"
execution.metadata["force_next_task"] = True
```

Update `ReActEngine._apply_tool_execution_loop_effects()`:

```python
if execution.metadata.get("force_next_task") and hasattr(self.context_manager, "force_next_task"):
    self.context_manager.force_next_task()
```

- [ ] **Step 5: Run repetition tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_repetition.py -v
```

Expected: PASS.

- [ ] **Step 6: Run focused ReAct delegation tests**

Run:

```bash
uv run pytest tests/test_react_engine_tool_orchestration.py tests/test_tool_orchestration_execution.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/react_engine.py tests/test_tool_orchestration_repetition.py
git commit -m "Move repetitive execution handling to orchestrator"
```

Do not add Co-Authorship.

## Task 7: Add Recovery Handler Shell And Simple Recovery Categories

**Files:**
- Create: `src/sag/agent/tool_recovery.py`
- Modify: `src/sag/agent/tool_orchestration.py`
- Create: `tests/test_tool_orchestration_recovery.py`

- [ ] **Step 1: Write recovery shell tests for simple categories**

Create `tests/test_tool_orchestration_recovery.py` with fake tools and focused tests:

```python
from types import SimpleNamespace

from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class ResultTool(BaseTool):
    def __init__(self, name, results):
        super().__init__(name, name)
        self.results = list(results)
        self.calls = []

    def execute(self) -> ToolResult:
        raise AssertionError("Tests should call safe_execute on ResultTool")

    def safe_execute(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return self.results.pop(0)


def make_orchestrator(
    tools,
    context_manager=None,
    successful_states=None,
    repository_url=None,
    guidance=None,
    event_sink=None,
):
    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=[],
        successful_states=successful_states or {"working_directory": "/workspace/project"},
        repository_url=repository_url,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=(guidance or (lambda message, priority=5: None)),
        get_timestamp=lambda: "ts",
        event_sink=event_sink,
    )


def test_project_setup_recovery_injects_repository_url():
    tool = ResultTool(
        "project_setup",
        [
            ToolResult(success=False, output="", error="missing url"),
            ToolResult(success=True, output="cloned"),
        ],
    )
    execution = make_orchestrator({"project_setup": tool}, repository_url="https://example/repo.git").execute(
        ToolCall(name="project_setup", raw_params={"action": "clone"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "project_setup_repository_url"
    assert tool.calls[-1]["repository_url"] == "https://example/repo.git"
    assert execution.executed_params["repository_url"] == "https://example/repo.git"


def test_manage_context_recovery_uses_single_in_progress_task():
    class FakeTask:
        id = "task_2"
        status = SimpleNamespace(value="in_progress")

    context_manager = SimpleNamespace(
        current_task_id=None,
        load_trunk_context=lambda: SimpleNamespace(todo_list=[FakeTask()]),
    )
    tool = ResultTool(
        "manage_context",
        [
            ToolResult(success=False, output="", error="no active", error_code="NO_ACTIVE_TASK"),
            ToolResult(success=True, output="completed"),
        ],
    )

    execution = make_orchestrator({"manage_context": tool}, context_manager=context_manager).execute(
        ToolCall(name="manage_context", raw_params={"action": "complete_task", "summary": "done"})
    )

    assert execution.status == "recovered"
    assert context_manager.current_task_id == "task_2"
    assert execution.executed_params == {"action": "complete_task", "summary": "done"}


def test_generic_recovery_returns_failure_without_silent_success():
    tool = ResultTool("other", [ToolResult(success=False, output="", error="bad")])

    execution = make_orchestrator({"other": tool}).execute(ToolCall(name="other", raw_params={}))

    assert execution.status == "failure"
    assert execution.recovery_applied is False
    assert execution.metadata["recovery"]["message"] == "No generic recovery strategy available"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: FAIL because recovery handler does not exist.

- [ ] **Step 3: Create `ToolRecoveryHandler`**

Create `src/sag/agent/tool_recovery.py`:

```python
"""Private recovery strategies for tool orchestration."""

from __future__ import annotations

from typing import Any, Dict, Optional

from sag.agent.tool_orchestration import RecoveryDecision
from sag.tools.base import ToolResult


class ToolRecoveryHandler:
    def __init__(
        self,
        *,
        tools: Dict[str, Any],
        context_manager: Any,
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        add_system_guidance,
        logger: Any = None,
    ):
        self.tools = tools
        self.context_manager = context_manager
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.add_system_guidance = add_system_guidance
        self.logger = logger

    def recover(self, tool_name: str, params: Dict[str, Any], failed_result: ToolResult) -> RecoveryDecision:
        if tool_name == "manage_context":
            return self._recover_context_management_error(params, failed_result)
        if tool_name == "project_setup":
            return self._recover_project_setup_error(params, failed_result)
        return self._recover_generic_error(tool_name, params, failed_result)
```

Copy current logic for:

```text
_recover_context_management_error
_recover_project_setup_error
_recover_generic_error
```

Convert return dictionaries into `RecoveryDecision`.

- [ ] **Step 4: Wire recovery into orchestrator**

In `ToolOrchestrator.__init__`, instantiate:

```python
from sag.agent.tool_recovery import ToolRecoveryHandler

self.recovery_handler = ToolRecoveryHandler(...)
```

After a failed tool result, call:

```python
decision = self.recovery_handler.recover(call.name, validated_params, result)
execution.metadata["recovery"] = decision.metadata | {"message": decision.guidance or ""}
```

Set final status:

```python
if decision.should_recover and decision.replacement_result:
    result = decision.replacement_result
    status = "recovered" if result.success else "recovery_failed"
    executed_params = decision.replacement_params or validated_params
elif decision.should_recover:
    status = "recovery_attempted"
    executed_params = validated_params
else:
    status = "failure"
    executed_params = validated_params
```

When building the returned `ToolExecution`, set:

```python
executed_params=executed_params
```

For replacement retries, this must be the actual parameter dict passed to the
replacement `safe_execute` call. Timeout/guidance-only recovery keeps the
original validated params because no replacement call occurs.

- [ ] **Step 5: Emit recovery events**

Emit `tool_recovery` when a strategy attempts recovery:

```python
self._emit(
    "tool_recovery",
    call,
    decision.guidance or f"Recovery attempted for {call.name}",
    level="success" if decision.replacement_result and decision.replacement_result.success else "warning",
    metadata={
        "recovery_strategy": decision.strategy,
        "attempted": decision.should_recover,
        "success": bool(decision.replacement_result and decision.replacement_result.success),
        "guidance": decision.guidance,
        "replacement_result_success": (
            decision.replacement_result.success if decision.replacement_result else None
        ),
        "recovery_params": decision.replacement_params,
    },
)
```

- [ ] **Step 6: Run simple recovery tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: PASS for simple categories.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/tool_recovery.py tests/test_tool_orchestration_recovery.py
git commit -m "Add orchestrator recovery handler"
```

Do not add Co-Authorship.

## Task 8: Migrate Maven And Gradle Recovery

**Files:**
- Modify: `src/sag/agent/tool_recovery.py`
- Modify: `tests/test_tool_orchestration_recovery.py`

- [ ] **Step 1: Add Maven/Gradle characterization tests**

Append concrete Maven/Gradle tests:

```python
def test_maven_java_version_recovery_installs_and_retries():
    maven = ResultTool(
        "maven",
        [
            ToolResult(
                success=False,
                output="",
                error="Java mismatch",
                error_code="JAVA_VERSION_MISMATCH",
                metadata={
                    "analysis": {
                        "java_version_error": {"required": "17", "current": "11"}
                    }
                },
            ),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )
    system = ResultTool(
        "system",
        [
            ToolResult(success=False, output="not installed"),
            ToolResult(success=True, output="installed Java 17"),
        ],
    )

    execution = make_orchestrator({"maven": maven, "system": system}).execute(
        ToolCall(name="maven", raw_params={"command": "test"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_java_version"
    assert system.calls == [
        {"action": "verify_java", "java_version": "17"},
        {"action": "install_java", "java_version": "17"},
    ]
    assert maven.calls[-1]["command"] == "test"
    assert execution.executed_params == {"command": "test"}

def test_maven_working_directory_recovery_retries_known_directory():
    maven = ResultTool(
        "maven",
        [
            ToolResult(success=False, output="", error="pom not found"),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )

    execution = make_orchestrator(
        {"maven": maven},
        successful_states={"working_directory": "/workspace/project"},
    ).execute(ToolCall(name="maven", raw_params={"command": "test"}))

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_known_working_directory"
    assert maven.calls[-1]["working_directory"] == "/workspace/project"
    assert execution.executed_params["working_directory"] == "/workspace/project"


def test_maven_compile_before_test_recovery():
    maven = ResultTool(
        "maven",
        [
            ToolResult(success=False, output="", error="test compilation failed"),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )

    execution = make_orchestrator({"maven": maven}).execute(
        ToolCall(name="maven", raw_params={"command": "test"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_compile_before_test"
    assert maven.calls[-1]["command"] == "compile"
    assert execution.executed_params["command"] == "compile"


def test_maven_pom_discovery_recovery_targets_detected_pom():
    class FakeDockerOrchestrator:
        project_name = "project"

        def execute_command(self, command):
            return {"success": True, "output": "/workspace/project/pom.xml\n"}

    context_manager = SimpleNamespace(orchestrator=FakeDockerOrchestrator())
    maven = ResultTool(
        "maven",
        [
            ToolResult(
                success=False,
                output="",
                error="missing pom",
                metadata={"analysis": {"error_type": "MISSING_PROJECT"}},
            ),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )

    execution = make_orchestrator({"maven": maven}, context_manager=context_manager).execute(
        ToolCall(name="maven", raw_params={"command": "test"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_pom_discovery"
    assert maven.calls[-1]["pom_file"] == "/workspace/project/pom.xml"
    assert maven.calls[-1]["working_directory"] == "/workspace/project"
    assert execution.executed_params["pom_file"] == "/workspace/project/pom.xml"
    assert execution.executed_params["working_directory"] == "/workspace/project"


def test_maven_module_and_test_exclusion_recovery_records_exclusions():
    maven = ResultTool(
        "maven",
        [
            ToolResult(
                success=False,
                output="",
                error="test failures",
                metadata={
                    "analysis": {
                        "failed_modules": [{"artifact_id": "bad-module"}],
                        "failed_tests": ["com.example.Foo.testBar"],
                    }
                },
            ),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )
    states = {"working_directory": "/workspace/project", "excluded_modules": set(), "excluded_tests": set()}

    execution = make_orchestrator({"maven": maven}, successful_states=states).execute(
        ToolCall(name="maven", raw_params={"command": "test"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_exclude_modules_or_tests"
    assert "-pl !bad-module" in maven.calls[-1]["properties"]
    assert "-am" in maven.calls[-1]["properties"]
    assert "test=!com.example.Foo#testBar" in maven.calls[-1]["properties"]
    assert execution.executed_params["properties"] == maven.calls[-1]["properties"]


def test_maven_timeout_returns_guidance_without_retry():
    guidance = []
    maven = ResultTool(
        "maven",
        [
            ToolResult(
                success=False,
                output="",
                error="timeout",
                error_code="TIMEOUT_IDLE",
                metadata={"termination_reason": "idle", "execution_time": 42.0},
            )
        ],
    )

    execution = make_orchestrator(
        {"maven": maven},
        guidance=lambda message, priority=5: guidance.append((message, priority)),
    ).execute(ToolCall(name="maven", raw_params={"command": "test"}))

    assert execution.status == "recovery_attempted"
    assert execution.result.error_code == "MAVEN_TIMEOUT_HANDLED"
    assert execution.recovery_strategy == "maven_timeout_guidance"
    assert len(maven.calls) == 1
    assert guidance[-1][1] == "high"


def test_gradle_timeout_returns_guidance_without_retry():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult(
                success=False,
                output="",
                error="timeout",
                error_code="TIMEOUT_IDLE",
                metadata={"termination_reason": "idle", "execution_time": 12.0},
            )
        ],
    )

    execution = make_orchestrator({"gradle": gradle}).execute(
        ToolCall(name="gradle", raw_params={"task": "test"})
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.error_code == "GRADLE_TIMEOUT_HANDLED"
    assert execution.recovery_strategy == "gradle_timeout_guidance"
    assert len(gradle.calls) == 1


def test_gradle_working_directory_recovery_retries_known_directory():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult(success=False, output="", error="build file not found"),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )

    execution = make_orchestrator(
        {"gradle": gradle},
        successful_states={"working_directory": "/workspace/project"},
    ).execute(ToolCall(name="gradle", raw_params={"task": "test"}))

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_known_working_directory"
    assert gradle.calls[-1]["working_directory"] == "/workspace/project"
    assert execution.executed_params["working_directory"] == "/workspace/project"


def test_gradle_compile_fallback_recovery():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult(success=False, output="", error="test compilation failed"),
            ToolResult(success=True, output="BUILD SUCCESS"),
        ],
    )

    execution = make_orchestrator({"gradle": gradle}).execute(
        ToolCall(name="gradle", raw_params={"task": "test"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_compile_before_test"
    assert gradle.calls[-1]["task"] == "compileJava"
    assert execution.executed_params["task"] == "compileJava"
```

Use `ResultTool` and `make_orchestrator()` from Task 7. Keep each test deterministic and no Docker.

- [ ] **Step 2: Run new tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: FAIL for Maven/Gradle cases.

- [ ] **Step 3: Move Maven recovery logic**

Copy current `ReActEngine._recover_maven_error` into `ToolRecoveryHandler._recover_maven_error` and convert dict returns to `RecoveryDecision`.
Every retry strategy must set `replacement_params` to the exact params passed
to the replacement `safe_execute` call.

Preserve these strategy names in decisions:

```text
maven_java_version
maven_known_working_directory
maven_compile_before_test
maven_pom_discovery
maven_exclude_modules_or_tests
maven_timeout_guidance
maven_no_strategy
```

- [ ] **Step 4: Move Gradle recovery logic**

Copy current `ReActEngine._recover_gradle_error` into `ToolRecoveryHandler._recover_gradle_error`.
Every retry strategy must set `replacement_params` to the exact params passed
to the replacement `safe_execute` call.

Preserve these strategy names:

```text
gradle_timeout_guidance
gradle_known_working_directory
gradle_compile_before_test
gradle_no_strategy
```

- [ ] **Step 5: Add dispatch branches**

In `ToolRecoveryHandler.recover()`:

```python
if tool_name == "maven":
    return self._recover_maven_error(params, failed_result)
if tool_name == "gradle":
    return self._recover_gradle_error(params, failed_result)
```

- [ ] **Step 6: Run recovery tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/tool_recovery.py tests/test_tool_orchestration_recovery.py
git commit -m "Move Maven and Gradle recovery to orchestrator"
```

Do not add Co-Authorship.

## Task 9: Migrate Bash, File I/O, Generic Recovery, And System Guidance

**Files:**
- Modify: `src/sag/agent/tool_recovery.py`
- Modify: `tests/test_tool_orchestration_recovery.py`

- [ ] **Step 1: Add bash/file_io/system guidance tests**

Append concrete bash/file I/O tests:

```python
def test_bash_timeout_guidance_adds_system_guidance():
    guidance = []
    bash = ResultTool(
        "bash",
        [
            ToolResult(
                success=False,
                output="",
                error="timeout",
                error_code="TIMEOUT_IDLE",
                metadata={
                    "termination_reason": "idle",
                    "monitoring_info": {"execution_time": 33.0},
                },
            )
        ],
    )

    execution = make_orchestrator(
        {"bash": bash},
        guidance=lambda message, priority=5: guidance.append((message, priority)),
    ).execute(ToolCall(name="bash", raw_params={"command": "mvn test"}))

    assert execution.status == "recovery_attempted"
    assert execution.result.error_code == "TIMEOUT_HANDLED"
    assert execution.recovery_strategy == "bash_timeout_guidance"
    assert guidance[-1][1] == "high"


def test_bash_workspace_recreation_retries_original_command():
    class FakeDockerOrchestrator:
        def __init__(self):
            self.commands = []

        def execute_command(self, command, workdir=None):
            self.commands.append((command, workdir))
            return {"success": True, "output": "ok"}

    docker = FakeDockerOrchestrator()
    context_manager = SimpleNamespace(orchestrator=docker)
    bash = ResultTool(
        "bash",
        [
            ToolResult(
                success=False,
                output="",
                error="OCI runtime exec failed",
                metadata={"exit_code": 127},
            ),
            ToolResult(success=True, output="ok"),
        ],
    )

    execution = make_orchestrator({"bash": bash}, context_manager=context_manager).execute(
        ToolCall(name="bash", raw_params={"command": "pwd"})
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "bash_workspace_recreation"
    assert bash.calls[-1]["working_directory"] == "/workspace"
    assert execution.executed_params["working_directory"] == "/workspace"
    assert [command for command, _ in docker.commands] == [
        "mkdir -p /workspace",
        "chmod 755 /workspace",
        "touch /workspace/.sag_workspace_marker",
    ]


def test_bash_known_working_directory_recovery():
    bash = ResultTool(
        "bash",
        [
            ToolResult(success=False, output="", error="command failed"),
            ToolResult(success=True, output="ok"),
        ],
    )

    execution = make_orchestrator(
        {"bash": bash},
        successful_states={"working_directory": "/workspace/project"},
    ).execute(ToolCall(name="bash", raw_params={"command": "pwd"}))

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "bash_known_working_directory"
    assert bash.calls[-1]["working_directory"] == "/workspace/project"
    assert execution.executed_params["working_directory"] == "/workspace/project"


def test_file_io_path_recovery_uses_known_working_directory():
    file_io = ResultTool(
        "file_io",
        [
            ToolResult(success=False, output="", error="not found"),
            ToolResult(success=True, output="README"),
        ],
    )

    execution = make_orchestrator(
        {"file_io": file_io},
        successful_states={"working_directory": "/workspace/project"},
    ).execute(ToolCall(name="file_io", raw_params={"action": "read", "path": "README.md"}))

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "file_io_known_working_directory"
    assert file_io.calls[-1]["path"] == "/workspace/project/README.md"
    assert execution.executed_params["path"] == "/workspace/project/README.md"


def test_recovery_failed_status_when_replacement_result_fails():
    project_setup = ResultTool(
        "project_setup",
        [
            ToolResult(success=False, output="", error="missing url"),
            ToolResult(success=False, output="", error="clone failed"),
        ],
    )

    execution = make_orchestrator(
        {"project_setup": project_setup},
        repository_url="https://example/repo.git",
    ).execute(ToolCall(name="project_setup", raw_params={"action": "clone"}))

    assert execution.status == "recovery_failed"
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "project_setup_repository_url"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: FAIL for bash/file_io categories.

- [ ] **Step 3: Move bash recovery**

Copy current `ReActEngine._recover_bash_error` into `ToolRecoveryHandler._recover_bash_error`.
Every retry strategy must set `replacement_params` to the exact params passed
to the replacement `safe_execute` call.

Preserve:

- timeout guidance without automatic retry
- Maven/Gradle-specific timeout suggestions
- `_add_system_guidance(..., priority="high")`
- workspace recreation through `context_manager.orchestrator.execute_command`
- fallback to bash tool when orchestrator command execution is unavailable
- known working directory retry

- [ ] **Step 4: Move file_io recovery**

Copy current `ReActEngine._recover_file_io_error` into `ToolRecoveryHandler._recover_file_io_error`.
Every retry strategy must set `replacement_params` to the exact params passed
to the replacement `safe_execute` call.

Preserve path behavior exactly, including the current use of `params.get("path", "")`.

- [ ] **Step 5: Ensure generic fallback metadata is always present**

For tools without specialized strategy, return:

```python
RecoveryDecision(
    should_recover=False,
    strategy="generic_no_strategy",
    guidance="No generic recovery strategy available",
    metadata={"message": "No generic recovery strategy available"},
)
```

- [ ] **Step 6: Run full recovery tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_recovery.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/tool_recovery.py tests/test_tool_orchestration_recovery.py
git commit -m "Move bash and file recovery to orchestrator"
```

Do not add Co-Authorship.

## Task 10: Route Production Actions, Complete Events, And Cleanup Delegated Helpers

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_tool_orchestration_execution.py`
- Modify: `tests/test_tool_orchestration_recovery.py`
- Modify: `tests/test_react_engine_tool_orchestration.py`

- [ ] **Step 1: Add event metadata tests**

Add the successful-result metadata test to `tests/test_tool_orchestration_execution.py`:

```python
def test_lifecycle_events_include_required_metadata():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )
    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    start = events[0]
    result = events[-1]
    assert start.metadata["raw_params"] == {"command": "pwd"}
    assert "execution_signature" in start.metadata
    assert result.metadata["status"] == execution.status
    assert result.metadata["duration_ms"] is not None
    assert result.metadata["result_success"] is True
    assert result.metadata["executed_params"] == {"command": "pwd"}
```

Add the recovery/error metadata test to `tests/test_tool_orchestration_recovery.py`:

```python
def test_recovery_and_error_events_include_required_metadata():
    events = []
    project_setup = ResultTool(
        "project_setup",
        [
            ToolResult(success=False, output="", error="missing url"),
            ToolResult(success=True, output="cloned"),
        ],
    )
    orchestrator = make_orchestrator(
        {"project_setup": project_setup},
        repository_url="https://example/repo.git",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="project_setup", raw_params={"action": "clone"}))

    recovery_event = next(event for event in events if event.event_type == "tool_recovery")
    result_event = events[-1]
    assert recovery_event.metadata["recovery_strategy"] == "project_setup_repository_url"
    assert recovery_event.metadata["attempted"] is True
    assert recovery_event.metadata["success"] is True
    assert recovery_event.metadata["replacement_result_success"] is True
    assert recovery_event.metadata["recovery_params"]["repository_url"] == "https://example/repo.git"
    assert result_event.metadata["recovery_applied"] is True
    assert result_event.metadata["status"] == execution.status

    events = []
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )
    execution = orchestrator.execute(ToolCall(name="missing", raw_params={}))

    error_event = events[-1]
    assert error_event.event_type == "tool_error"
    assert error_event.metadata["error_code"] == "UNKNOWN_TOOL"
    assert error_event.metadata["suggestions"]
    assert error_event.metadata["original_error"] == execution.result.error
    assert error_event.metadata["recovery_attempted"] is False
```

Add ReAct adapter test to `tests/test_react_engine_tool_orchestration.py`:

```python
from sag.agent.tool_orchestration import ToolCall, ToolExecution, ToolLifecycleEvent
from sag.ui.events import EventType


def test_react_engine_tool_event_adapter_emits_existing_ui_events():
    engine = ReActEngine.__new__(ReActEngine)
    engine.current_iteration = 7
    emitted = []
    engine.emit = lambda *args, **kwargs: emitted.append((args, kwargs))

    event = ToolLifecycleEvent(
        event_type="tool_result",
        call=ToolCall(name="echo", raw_params={"command": "pwd"}),
        message="echo finished",
        metadata={"status": "success", "result_success": True},
    )

    engine._handle_tool_lifecycle_event(event)

    assert emitted[0][0][0] == EventType.AGENT_OBSERVATION
    assert emitted[0][1]["message"] == "echo finished"
    assert emitted[0][1]["step_num"] == 7
    assert emitted[0][1]["status"] == "success"


def test_execute_steps_delegates_action_to_orchestrator_after_migration(monkeypatch):
    result = ToolResult(success=True, output="ok")
    step = ReActStep(
        step_type=StepType.ACTION,
        content="ACTION: example",
        tool_name="example",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="model",
    )
    execution = ToolExecution(
        call=ToolCall(name="example", raw_params={"command": "pwd"}),
        result=result,
        status="success",
        raw_params={"command": "pwd"},
        validated_params={"command": "pwd"},
        executed_params={"command": "pwd"},
        observation_text="formatted observation",
        attempted_execution=True,
    )
    engine = make_engine()

    class FakeOrchestrator:
        def execute(self, call):
            engine.seen_call = call
            return execution

    monkeypatch.setattr(engine, "_get_tool_orchestrator", lambda: FakeOrchestrator())

    assert engine._execute_steps([step]) is True
    assert engine.seen_call.name == "example"
    assert engine.seen_call.raw_params == {"command": "pwd"}
    assert step.tool_result is result
    assert any(
        s.step_type == StepType.OBSERVATION and s.content == "formatted observation"
        for s in engine.steps
    )
    assert engine._force_thinking_after_success is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_orchestration_execution.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: FAIL until event metadata and adapter are complete.

- [ ] **Step 3: Fill event metadata**

Ensure events include exactly what the spec requires:

```text
tool_start: tool name, source step index, raw params, execution signature
tool_parameters_fixed: raw params, validated params, ParameterFix entries, params_changed
tool_result: status, duration, result success, error code, executed params, recovery_applied
tool_recovery: strategy, attempted, success, guidance, replacement result success, recovery params, parameter diff
tool_error: error code, category, suggestions, original error, recovery_attempted
```

Use `time.perf_counter()` in `ToolOrchestrator.execute()` to populate `duration_ms`.

- [ ] **Step 4: Implement ReActEngine event adapter**

In `_handle_tool_lifecycle_event`, map tool lifecycle events into existing `EventType` values without importing UI objects into the orchestrator:

```python
if event.event_type == "tool_error":
    self.emit(EventType.ERROR, message=event.message, level="error", **event.metadata)
elif event.event_type == "tool_recovery":
    self.emit(EventType.WARNING, message=event.message, level=event.level, **event.metadata)
elif event.event_type == "tool_result":
    self.emit(
        EventType.AGENT_OBSERVATION,
        message=event.message,
        step_num=self.current_iteration,
        **event.metadata,
    )
```

Do not emit duplicate thought/action events; those remain in `ReActEngine`.

- [ ] **Step 5: Route `_execute_steps` ACTION execution through orchestrator**

Only do this after Tasks 4-9 are complete and passing.

In `_execute_steps`, keep thought handling and `EventType.AGENT_ACTION` emission
in `ReActEngine`. Replace the unknown-tool, validation, repetition,
`safe_execute`, recovery, tracking, successful-state, formatting block with:

```python
call = self._build_tool_call_from_step(step)
execution = self._get_tool_orchestrator().execute(call)
result = execution.result
step.tool_result = result
self._apply_tool_execution_loop_effects(execution)
```

Then keep the existing verbose result logging, observation insertion, and
force-thinking-after-success behavior:

```python
if self.config.verbose:
    self._log_tool_result_verbose(step.tool_name, result)
self._add_observation_step(execution.observation_text)
if result.success:
    self._force_thinking_after_success = True
```

This is the first task where production ReAct action execution is fully routed
through the orchestrator. The earlier adapter task intentionally did not change
production routing.

- [ ] **Step 6: Remove delegated helpers from ReActEngine**

After all focused tests pass, remove from `src/sag/agent/react_engine.py`:

```text
_validate_and_fix_parameters
_fix_parameters_against_schema
_get_smart_default
_get_tool_specific_action_default
_convert_parameter_type
_fix_parameter_names
_apply_basic_parameter_fixes
_apply_tool_specific_fixes
_get_repetition_level
_is_repetitive_execution
_generate_alternative_suggestions
_auto_fix_java_configuration
_attempt_error_recovery
_recover_context_management_error
_recover_maven_error
_recover_gradle_error
_recover_project_setup_error
_recover_bash_error
_recover_file_io_error
_recover_generic_error
```

Keep in `ReActEngine`:

```text
_format_context_for_prompt
_add_observation_step
_update_successful_states
_propagate_working_directory_change
_track_tool_execution
_format_tool_result only if it delegates to tool_orchestration.format_tool_result, otherwise remove it
```

- [ ] **Step 7: Run cleanup guards**

Run:

```bash
rg -n "def _validate_and_fix_parameters|def _attempt_error_recovery|def _recover_.*_error|def _auto_fix_java_configuration|def _get_repetition_level|def _is_repetitive_execution" src/sag/agent/react_engine.py
```

Expected: no output.

Run:

```bash
rg -n "safe_execute\\(" src/sag/agent/react_engine.py
```

Expected: no output.

Run:

```bash
rg -n "UIManager|rich|from sag.ui" src/sag/agent/tool_orchestration.py src/sag/agent/tool_recovery.py
```

Expected: no output, except `tool_orchestration.py` may contain no UI imports at all.

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py tests/test_tool_orchestration_execution.py tests/test_tool_orchestration_parameters.py tests/test_tool_orchestration_repetition.py tests/test_tool_orchestration_recovery.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/tool_recovery.py src/sag/agent/react_engine.py tests/test_tool_orchestration_execution.py tests/test_react_engine_tool_orchestration.py
git commit -m "Complete tool lifecycle event orchestration"
```

Do not add Co-Authorship.

## Task 11: Full Verification And Independent Review

**Files:**
- Modify only if verification exposes issues.

- [ ] **Step 1: Run formatting checks**

Run:

```bash
uv run black --check src tests
```

Expected: PASS.

Run:

```bash
uv run isort --check-only src tests
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest -v
```

Expected: PASS. Current baseline before this plan was `14 passed, 17 warnings`; after this implementation the pass count should increase by the new orchestration tests.

- [ ] **Step 3: Run architecture guards**

Run:

```bash
rg -n "skill_engine|SkillRegistry|SkillTool|config/skills|AVAILABLE SKILLS|\\.sag/skills" src tests docs
```

Expected: no output. This confirms the intentionally excluded skill-engine feature has not been reintroduced.

Run:

```bash
rg -n "def _validate_and_fix_parameters|def _attempt_error_recovery|def _recover_.*_error|def _auto_fix_java_configuration|def _get_repetition_level|def _is_repetitive_execution" src/sag/agent/react_engine.py
```

Expected: no output.

Run:

```bash
rg -n "safe_execute\\(" src/sag/agent/react_engine.py
```

Expected: no output.

- [ ] **Step 4: Request independent correctness review**

Use a fresh review agent. Provide:

```text
Spec: docs/superpowers/specs/2026-06-01-tool-orchestrator-boundary-design.md
Plan: docs/superpowers/plans/2026-06-02-tool-orchestrator-boundary-implementation.md
Git range: from the commit before Task 1 to HEAD
Focus: behavior preservation, recovery coverage, parameter lifecycle correctness, UI decoupling, no skill-engine reintroduction.
```

- [ ] **Step 5: Fix Critical/Important review issues**

If review finds Critical or Important issues, fix them before proceeding. Minor issues can be deferred only if they do not affect correctness, maintainability, or the spec contract.

- [ ] **Step 6: Final commit if review fixes were needed**

```bash
git add <changed-files>
git commit -m "Address tool orchestrator review feedback"
```

Do not add Co-Authorship.

## Success Criteria

- `src/sag/agent/react_engine.py` no longer owns tool lookup, parameter validation/fix, direct `safe_execute`, repetition handling, recovery helpers, or tool lifecycle event emission.
- `ToolOrchestrator.execute()` returns `ToolExecution` with correct `raw_params`, `validated_params`, `executed_params`, status, recovery metadata, and observation text.
- Recovery tests cover `manage_context`, Java/Maven repair, Maven working-directory repair, Maven compile-before-test, Maven `pom.xml` discovery, Maven module/test exclusion, Maven timeout guidance, Gradle timeout guidance, Gradle working-directory repair, Gradle compile fallback, project setup URL injection, bash timeout guidance, bash workspace recreation, bash working-directory repair, `file_io` path repair, generic fallback, and system guidance.
- `ToolOrchestrator` and `ToolRecoveryHandler` have no Rich/UIManager dependency.
- Existing import, schema, result/state, report, packaging, formatting, and full pytest verification pass.
- Independent review finds no blocking correctness issues.
