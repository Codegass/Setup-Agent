# CLI Observability And Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a maintainable CLI/TUI observability layer that shows live run state clearly and generates final diagnostics from the same state model.

**Architecture:** Add typed UI run-state contracts and a `UIStateAggregator` between `UIEvent` producers and Rich rendering. Keep `UIManager` as the lifecycle coordinator, move event-to-state logic into the aggregator, render the live TUI from snapshots, and generate final diagnosis from the same snapshot/evidence records. Web UI is out of scope; the state model should simply avoid terminal-only assumptions.

**Tech Stack:** Python 3.10+, dataclasses, Rich, Click CLI, pytest, uv, black, isort.

---

## Spec Reference

- Design spec: `docs/superpowers/specs/2026-06-02-cli-observability-design.md`
- Current UI entry points: `src/sag/ui/events.py`, `src/sag/ui/ui_manager.py`, `src/sag/ui/components.py`
- Current orchestrator lifecycle source: `src/sag/agent/tool_orchestration.py`
- Current ReAct UI mapping: `src/sag/agent/react_engine.py`
- Constraint: commit messages must not include Co-Authorship or similar authorship trailers.

## Scope Boundaries

This plan builds only CLI/TUI observability and final diagnosis. Do not build a web dashboard, browser UI, interactive pause/retry controls, or a new command mode.

Do not redesign the ReAct loop or tool orchestration contracts. Only extend UI-facing events when needed to expose already-available lifecycle metadata.

Do not parse Rich-rendered terminal output. The final diagnosis must read typed state, timeline, and evidence records.

## File Structure

### Runtime

- Create: `src/sag/ui/state.py`
  - Typed read-model dataclasses: `PhaseSnapshot`, `ActiveOperation`, `UITimelineEntry`, `UIEvidenceRecord`, `RecoverySnapshot`, `UIRunState`
  - Pure helper `initial_run_state(project_name, start_time)`
- Create: `src/sag/ui/state_aggregator.py`
  - `UIStateAggregator`
  - Event-to-state handlers
  - Pure helpers migrated from `UIManager` where they are state concerns: tool parameter display, phase detection, thought/observation summaries
- Create: `src/sag/ui/diagnosis.py`
  - `FinalDiagnosis`
  - `build_final_diagnosis(state: UIRunState) -> FinalDiagnosis`
- Modify: `src/sag/ui/events.py`
  - Add tool lifecycle/evidence event types used by the aggregator
- Modify: `src/sag/ui/components.py`
  - Add snapshot-based Rich builders for status header, timeline, active operation, recovery/evidence, and final diagnosis
  - Keep current helper functions during migration if callers still need them
- Modify: `src/sag/ui/ui_manager.py`
  - Instantiate and update `UIStateAggregator`
  - Render from `UIRunState`
  - Delegate final summary content to `diagnosis.py` and snapshot-based components
  - Remove or deprecate duplicated mutable event-handling state after snapshot rendering is in place
- Modify: `src/sag/agent/react_engine.py`
  - Map `ToolLifecycleEvent` values to UI lifecycle events instead of dropping non-error lifecycle events

### Tests

- Create: `tests/test_ui_state_models.py`
- Create: `tests/test_ui_state_aggregator.py`
- Create: `tests/test_ui_diagnosis.py`
- Create: `tests/test_ui_manager_observability.py`
- Modify: `tests/test_import_smoke.py`
- Modify: `tests/test_react_engine_tool_orchestration.py`

## Task 1: Add UI Run-State Models

**Files:**
- Create: `src/sag/ui/state.py`
- Create: `tests/test_ui_state_models.py`
- Modify: `tests/test_import_smoke.py`

- [ ] **Step 1: Write failing state model tests**

Create `tests/test_ui_state_models.py`:

```python
from datetime import datetime, timezone

from sag.ui.events import PhaseType
from sag.ui.state import (
    ActiveOperation,
    RecoverySnapshot,
    UIEvidenceRecord,
    UIRunState,
    UITimelineEntry,
    initial_run_state,
)


def test_initial_run_state_has_all_phases_pending():
    state = initial_run_state(
        project_name="commons-cli",
        start_time=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert state.project_name == "commons-cli"
    assert state.current_phase is None
    assert [phase.phase for phase in state.phases] == [
        PhaseType.SETUP,
        PhaseType.BUILD,
        PhaseType.TEST,
        PhaseType.VERIFICATION,
    ]
    assert [phase.status for phase in state.phases] == ["pending"] * 4
    assert state.timeline == ()
    assert state.evidence == ()


def test_run_state_is_a_read_model_with_tuple_history():
    entry = UITimelineEntry(
        timestamp=datetime(2026, 6, 2, 12, 1, tzinfo=timezone.utc),
        kind="tool",
        message="maven compile",
        level="info",
    )
    evidence = UIEvidenceRecord(
        timestamp=datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc),
        kind="command",
        summary="maven compile passed",
    )
    state = UIRunState(
        project_name="commons-cli",
        start_time=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
        current_phase=PhaseType.BUILD,
        phases=initial_run_state("commons-cli", datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)).phases,
        current_status="Using maven",
        active_operation=ActiveOperation(tool_name="maven", action="compile"),
        recovery=RecoverySnapshot(active=False),
        timeline=(entry,),
        evidence=(evidence,),
    )

    assert state.timeline[0].message == "maven compile"
    assert state.evidence[0].summary == "maven compile passed"
    assert state.active_operation.tool_name == "maven"


def test_timeline_entries_can_carry_failure_classification():
    entry = UITimelineEntry(
        timestamp=datetime(2026, 6, 2, 12, 3, tzinfo=timezone.utc),
        kind="error",
        message="Command timed out",
        level="error",
        failure_classification="command_timeout",
    )

    assert entry.failure_classification == "command_timeout"
```

Modify `tests/test_import_smoke.py` to include:

```python
"sag.ui.state",
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_import_smoke.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.ui.state'`.

- [ ] **Step 3: Add minimal state dataclasses**

Create `src/sag/ui/state.py`:

```python
"""Typed read models for CLI/TUI run state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Optional

from sag.ui.events import PhaseType

PhaseStatus = Literal["pending", "running", "success", "error", "skipped"]
FailureClassification = Literal[
    "tool_failure",
    "command_timeout",
    "parameter_normalization",
    "recovery_attempt",
    "verification_failure",
    "warning",
    "final_failure",
]
TimelineKind = Literal[
    "phase",
    "step",
    "status",
    "thought",
    "tool",
    "observation",
    "stream",
    "recovery",
    "warning",
    "error",
    "evidence",
    "report",
    "completion",
]


@dataclass(frozen=True, slots=True)
class PhaseSnapshot:
    phase: PhaseType
    status: PhaseStatus = "pending"
    steps: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    tool_name: Optional[str] = None
    action: Optional[str] = None
    workdir: Optional[str] = None
    visible_params: str = ""
    started_at: Optional[datetime] = None
    detail: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UITimelineEntry:
    timestamp: datetime
    kind: TimelineKind | str
    message: str
    level: str = "info"
    details: Optional[str] = None
    failure_classification: Optional[FailureClassification | str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UIEvidenceRecord:
    timestamp: datetime
    kind: str
    summary: str
    details: Optional[str] = None
    path: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    active: bool = False
    strategy: Optional[str] = None
    retry_count: int = 0
    message: Optional[str] = None
    unresolved_risk: Optional[str] = None


@dataclass(frozen=True, slots=True)
class UIRunState:
    project_name: str
    start_time: datetime
    current_phase: Optional[PhaseType]
    phases: tuple[PhaseSnapshot, ...]
    current_status: str = "Initializing"
    active_operation: ActiveOperation = field(default_factory=ActiveOperation)
    recovery: RecoverySnapshot = field(default_factory=RecoverySnapshot)
    timeline: tuple[UITimelineEntry, ...] = ()
    evidence: tuple[UIEvidenceRecord, ...] = ()
    latest_error: Optional[UITimelineEntry] = None
    latest_warning: Optional[UITimelineEntry] = None
    is_complete: bool = False
    final_status: Optional[str] = None
    report_data: Optional[dict[str, Any]] = None

    def with_phase(self, phase: PhaseType, status: PhaseStatus) -> "UIRunState":
        phases = tuple(
            replace(item, status=status) if item.phase == phase else item for item in self.phases
        )
        return replace(self, phases=phases, current_phase=phase)


def initial_run_state(project_name: str, start_time: datetime) -> UIRunState:
    return UIRunState(
        project_name=project_name,
        start_time=start_time,
        current_phase=None,
        phases=tuple(PhaseSnapshot(phase=phase) for phase in PhaseType),
    )
```

Renderers should compute elapsed time from `UIRunState.start_time`. Active
operation elapsed time should be computed from `ActiveOperation.started_at` when
present. Do not store mutable elapsed counters in the snapshot.

- [ ] **Step 4: Run state tests to verify they pass**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 5: Format and commit**

Run:

```bash
uv run black src/sag/ui/state.py tests/test_ui_state_models.py tests/test_import_smoke.py
uv run isort src/sag/ui/state.py tests/test_ui_state_models.py tests/test_import_smoke.py
git diff --check
git status --short
```

Expected: only Task 1 files are changed.

Commit:

```bash
git add src/sag/ui/state.py tests/test_ui_state_models.py tests/test_import_smoke.py
git commit -m "Add CLI UI run state models"
```

## Task 2: Add UI State Aggregator For Existing Events

**Files:**
- Create: `src/sag/ui/state_aggregator.py`
- Create: `tests/test_ui_state_aggregator.py`

- [ ] **Step 1: Write failing aggregator tests**

Create `tests/test_ui_state_aggregator.py`:

```python
from datetime import datetime, timezone

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state_aggregator import UIStateAggregator


def fixed_now():
    return datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def test_aggregator_tracks_phase_step_and_status_events():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    state = aggregator.handle(
        UIEvent(EventType.PHASE_START, "Setting up", phase=PhaseType.SETUP)
    )
    assert state.current_phase == PhaseType.SETUP
    assert state.current_status == "Setting up"
    assert state.phases[0].status == "running"

    state = aggregator.handle(
        UIEvent(
            EventType.STEP_START,
            "Create container",
            phase=PhaseType.SETUP,
            details="docker",
        )
    )
    assert state.phases[0].steps[-1]["name"] == "Create container"
    assert state.phases[0].steps[-1]["status"] == "running"

    state = aggregator.handle(
        UIEvent(EventType.STEP_COMPLETE, "Create container", phase=PhaseType.SETUP)
    )
    assert state.phases[0].steps[-1]["status"] == "success"


def test_aggregator_tracks_agent_action_as_active_operation():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    aggregator.handle(
        UIEvent(
            EventType.AGENT_THOUGHT,
            "I need to compile the Maven project before testing.",
            metadata={"step_num": 4},
        )
    )
    state = aggregator.handle(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using maven",
            metadata={
                "step_num": 4,
                "tool_name": "maven",
                "tool_params": {"goal": "compile", "working_directory": "/workspace/app"},
            },
        )
    )

    assert state.current_phase == PhaseType.BUILD
    assert state.active_operation.tool_name == "maven"
    assert state.active_operation.action == "goal='compile'"
    assert "/workspace/app" in state.active_operation.workdir
    assert state.current_status.startswith("Using maven")


def test_aggregator_records_errors_warnings_completion_and_reports():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    warning_state = aggregator.handle(UIEvent(EventType.WARNING, "Retrying", level="warning"))
    assert warning_state.latest_warning.message == "Retrying"

    error_state = aggregator.handle(UIEvent(EventType.ERROR, "Build failed", level="error"))
    assert error_state.latest_error.message == "Build failed"

    report_state = aggregator.handle(
        UIEvent(
            EventType.REPORT_GENERATED,
            "Report generated",
            metadata={"report_path": "reports/setup.md", "status": "failure"},
        )
    )
    assert report_state.report_data["report_path"] == "reports/setup.md"
    assert report_state.evidence[-1].kind == "report"

    final_state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))
    assert final_state.is_complete is True
    assert final_state.final_status == "failure"
    assert final_state.timeline[-1].failure_classification == "final_failure"


def test_aggregator_records_validation_evidence_and_failure_classification():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    validation_state = aggregator.handle(
        UIEvent(
            EventType.VALIDATION_COMPLETE,
            "Validation failed",
            phase=PhaseType.VERIFICATION,
            level="error",
            metadata={"summary": "2 checks failed", "path": "reports/validation.json"},
        )
    )

    assert validation_state.evidence[-1].kind == "validation"
    assert validation_state.evidence[-1].summary == "2 checks failed"
    assert validation_state.latest_error.failure_classification == "verification_failure"


def test_aggregator_degrades_unknown_event_to_warning_timeline_entry():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    event = UIEvent(EventType.STATUS_UPDATE, "Known status")
    event.event_type = "unknown_event"

    state = aggregator.handle(event)

    assert state.latest_warning is not None
    assert "unknown_event" in state.latest_warning.message
    assert state.current_status == "Initializing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_state_aggregator.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.ui.state_aggregator'`.

- [ ] **Step 3: Implement minimal aggregator**

Create `src/sag/ui/state_aggregator.py` with:

```python
"""Aggregate UI events into typed CLI/TUI run state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state import (
    ActiveOperation,
    PhaseSnapshot,
    RecoverySnapshot,
    UIEvidenceRecord,
    UIRunState,
    UITimelineEntry,
    initial_run_state,
)


class UIStateAggregator:
    def __init__(self, project_name: str, clock: Callable[[], datetime] | None = None):
        self._clock = clock or datetime.now
        self._state = initial_run_state(project_name, self._clock())

    def snapshot(self) -> UIRunState:
        return self._state

    def handle(self, event: UIEvent) -> UIRunState:
        if not isinstance(event.event_type, EventType):
            self._state = self._append_warning(
                f"Unknown UI event ignored: {event.event_type}", details=event.details
            )
            return self._state

        handler = {
            EventType.PHASE_START: self._handle_phase_start,
            EventType.PHASE_COMPLETE: self._handle_phase_complete,
            EventType.PHASE_ERROR: self._handle_phase_error,
            EventType.STEP_START: self._handle_step_start,
            EventType.STEP_COMPLETE: self._handle_step_complete,
            EventType.STEP_ERROR: self._handle_step_error,
            EventType.STATUS_UPDATE: self._handle_status_update,
            EventType.AGENT_THOUGHT: self._handle_agent_thought,
            EventType.AGENT_ACTION: self._handle_agent_action,
            EventType.AGENT_OBSERVATION: self._handle_agent_observation,
            EventType.VALIDATION_START: self._handle_validation_event,
            EventType.VALIDATION_CHECK: self._handle_validation_event,
            EventType.VALIDATION_COMPLETE: self._handle_validation_event,
            EventType.WARNING: self._handle_warning,
            EventType.ERROR: self._handle_error,
            EventType.REPORT_GENERATED: self._handle_report_generated,
            EventType.SUCCESS: self._handle_success,
            EventType.FAILURE: self._handle_failure,
        }.get(event.event_type)

        if handler is None:
            self._state = self._append_timeline(event, kind="status")
            return self._state

        handler(event)
        return self._state
```

Required aggregator behavior:

| Event | State updates |
| --- | --- |
| `PHASE_START` | Set `current_phase`, set that phase `running`, set `current_status`, append timeline kind `phase`. |
| `PHASE_COMPLETE` | Set that phase `success`, set `current_status`, append timeline kind `phase`. |
| `PHASE_ERROR` | Set that phase `error`, set latest error with classification `verification_failure` when phase is `VERIFICATION`, otherwise `tool_failure`, append timeline kind `error`. |
| `STEP_START` | Append a running step to that phase, set `current_status`, append timeline kind `step`. |
| `STEP_COMPLETE` | Mark the named or first running step `success`, append timeline kind `step`. |
| `STEP_ERROR` | Mark the named or first running step `error`, set latest error, append timeline kind `error`. |
| `STATUS_UPDATE` | Set `current_status`, append timeline kind `status`. |
| `AGENT_THOUGHT` | Append timeline kind `thought`, set concise current status from thought summary. |
| `AGENT_ACTION` | Set `active_operation` from `tool_name` and `tool_params`, set detected phase when the tool implies build/test/verification, append timeline kind `tool`. |
| `AGENT_OBSERVATION` | Append timeline kind `observation`, clear or mark active operation detail as observation summary. |
| `VALIDATION_START` / `VALIDATION_CHECK` / `VALIDATION_COMPLETE` | Append timeline kind `evidence` or `error`; record `UIEvidenceRecord(kind=\"validation\")` when metadata has a summary/result/path; classify error-level validation as `verification_failure`. |
| `REPORT_GENERATED` | Store `report_data`, append `UIEvidenceRecord(kind=\"report\")`, append timeline kind `report`. |
| `WARNING` | Set latest warning, append timeline kind `warning`, classification `warning`. |
| `ERROR` | Set latest error, append timeline kind `error`, classification from metadata `failure_type` or `_classify_failure(event)`. |
| `SUCCESS` | Mark complete, set `final_status=\"success\"`, append timeline kind `completion`. |
| `FAILURE` | Mark complete, set `final_status=\"failure\"`, append timeline kind `completion`, classification `final_failure`. |
| Unknown event | Do not crash; append latest warning explaining the ignored event and keep the prior status. |

Required private helpers:

- `_append_timeline(event, kind, message=None, level=None)`
- `_append_warning(message, details=None)`
- `_classify_failure(event) -> str | None`
- `_append_evidence(kind, summary, details=None, path=None, metadata=None)`
- `_replace_phase(phase, status=None, steps=None)`
- `_update_running_or_named_step(phase, name, status, details)`
- `_format_tool_params(tool_name, params)`
- `_detect_phase_from_action(tool_name, params)`
- `_extract_workdir(params)`
- `_summarize_thought(text)`
- `_summarize_observation(text)`

`_classify_failure(event)` must implement these rules:

- return `event.metadata["failure_type"]` when present
- return `command_timeout` when message, details, or metadata error code contains `timeout`
- return `parameter_normalization` for parameter-fix events added in Task 5
- return `recovery_attempt` for recovery events added in Task 5
- return `verification_failure` for validation events or `PhaseType.VERIFICATION` errors
- return `warning` for warning events
- return `final_failure` for `EventType.FAILURE`
- otherwise return `tool_failure`

Keep helper functions pure and small. They can be migrated from the existing
`UIManager` helper behavior, but do not import `UIManager`.

- [ ] **Step 4: Run aggregator tests**

Run:

```bash
uv run pytest tests/test_ui_state_aggregator.py -v
```

Expected: PASS.

- [ ] **Step 5: Run UI state tests together**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_ui_state_aggregator.py -v
```

Expected: PASS.

- [ ] **Step 6: Format and commit**

Run:

```bash
uv run black src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py
uv run isort src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py
git diff --check
git status --short
```

Commit:

```bash
git add src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py
git commit -m "Add CLI UI state aggregator"
```

## Task 3: Add Final Diagnosis From Run State

**Files:**
- Create: `src/sag/ui/diagnosis.py`
- Create: `tests/test_ui_diagnosis.py`
- Modify: `tests/test_import_smoke.py`

- [ ] **Step 1: Write failing diagnosis tests**

Create `tests/test_ui_diagnosis.py`:

```python
from datetime import datetime, timezone

from sag.ui.diagnosis import build_final_diagnosis
from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state_aggregator import UIStateAggregator


def fixed_now():
    return datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def test_success_diagnosis_summarizes_completed_phases_and_evidence():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(UIEvent(EventType.PHASE_START, "Build", phase=PhaseType.BUILD))
    aggregator.handle(UIEvent(EventType.PHASE_COMPLETE, "Build passed", phase=PhaseType.BUILD))
    aggregator.handle(
        UIEvent(
            EventType.REPORT_GENERATED,
            "Report generated",
            metadata={"report_path": "reports/setup.md", "status": "success"},
        )
    )
    state = aggregator.handle(UIEvent(EventType.SUCCESS, "Project setup completed"))

    diagnosis = build_final_diagnosis(state)

    assert diagnosis.status == "success"
    assert "Build" in diagnosis.outcome
    assert any("reports/setup.md" in item for item in diagnosis.evidence)
    assert diagnosis.next_actions == ()


def test_failure_diagnosis_includes_error_recovery_and_next_action():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(UIEvent(EventType.PHASE_START, "Build", phase=PhaseType.BUILD))
    aggregator.handle(UIEvent(EventType.WARNING, "Retrying with fallback", level="warning"))
    aggregator.handle(
        UIEvent(
            EventType.ERROR,
            "Maven compile failed",
            phase=PhaseType.BUILD,
            details="Missing dependency",
            level="error",
        )
    )
    state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    diagnosis = build_final_diagnosis(state)

    assert diagnosis.status == "failure"
    assert any("Maven compile failed" in item for item in diagnosis.failures)
    assert any("Retrying with fallback" in item for item in diagnosis.recovery)
    assert "tool_failure" in diagnosis.failure_classifications
    assert "warning" in diagnosis.failure_classifications
    assert diagnosis.next_actions

```

Modify `tests/test_import_smoke.py` to include:

```python
"sag.ui.diagnosis",
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_diagnosis.py tests/test_import_smoke.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.ui.diagnosis'`.

- [ ] **Step 3: Implement diagnosis builder**

Create `src/sag/ui/diagnosis.py`:

```python
"""Build final CLI diagnosis from typed run state."""

from __future__ import annotations

from dataclasses import dataclass

from sag.ui.state import UIRunState


@dataclass(frozen=True, slots=True)
class FinalDiagnosis:
    status: str
    outcome: str
    completed_phases: tuple[str, ...] = ()
    failed_phases: tuple[str, ...] = ()
    skipped_phases: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recovery: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    failure_classifications: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


def build_final_diagnosis(state: UIRunState) -> FinalDiagnosis:
    status = state.final_status or ("success" if state.is_complete else "unknown")
    completed = tuple(phase.phase.value.title() for phase in state.phases if phase.status == "success")
    failed = tuple(phase.phase.value.title() for phase in state.phases if phase.status == "error")
    skipped = tuple(phase.phase.value.title() for phase in state.phases if phase.status == "skipped")

    failures = tuple(
        entry.message for entry in state.timeline if entry.kind == "error" or entry.level == "error"
    )
    warnings = tuple(
        entry.message for entry in state.timeline if entry.kind == "warning" or entry.level == "warning"
    )
    recovery = tuple(
        _format_recovery_entry(entry) for entry in state.timeline if entry.kind == "recovery"
    )
    evidence = tuple(
        " - ".join(part for part in [record.summary, record.path] if part)
        for record in state.evidence
    )
    classifications = tuple(
        dict.fromkeys(
            entry.failure_classification
            for entry in state.timeline
            if entry.failure_classification
        )
    )

    if status == "success":
        outcome = f"{state.project_name} completed successfully"
        next_actions = ()
    else:
        outcome = f"{state.project_name} did not complete successfully"
        next_actions = (
            "Review the latest failure and rerun the failed phase after addressing it.",
        )

    if completed:
        outcome = f"{outcome}. Completed phases: {', '.join(completed)}."

    return FinalDiagnosis(
        status=status,
        outcome=outcome,
        completed_phases=completed,
        failed_phases=failed,
        skipped_phases=skipped,
        failures=failures,
        warnings=warnings,
        recovery=recovery or warnings,
        evidence=evidence,
        failure_classifications=classifications,
        next_actions=next_actions,
    )


def _format_recovery_entry(entry) -> str:
    metadata = entry.metadata
    strategy = metadata.get("recovery_strategy") or metadata.get("strategy")
    guidance = metadata.get("guidance") or entry.message
    recovery_params = metadata.get("recovery_params")
    parameter_diff = metadata.get("parameter_diff")
    recovery_status = metadata.get("recovery")
    parts = [part for part in [strategy, guidance] if part]
    if recovery_params:
        parts.append(f"params={recovery_params}")
    if parameter_diff:
        parts.append(f"changed={parameter_diff}")
    if recovery_status:
        parts.append(f"recovery={recovery_status}")
    return " | ".join(parts)
```

Keep the first implementation concise. Do not infer complex root causes yet:
the diagnosis should report classifications that were already attached by the
aggregator, not invent new classification rules. Recovery strings should expose
the fallback strategy, guidance, changed parameters, recovery params, and nested
recovery status when the aggregator captured them.

- [ ] **Step 4: Run diagnosis tests**

Run:

```bash
uv run pytest tests/test_ui_diagnosis.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 5: Format and commit**

Run:

```bash
uv run black src/sag/ui/diagnosis.py tests/test_ui_diagnosis.py tests/test_import_smoke.py
uv run isort src/sag/ui/diagnosis.py tests/test_ui_diagnosis.py tests/test_import_smoke.py
git diff --check
git status --short
```

Commit:

```bash
git add src/sag/ui/diagnosis.py tests/test_ui_diagnosis.py tests/test_import_smoke.py
git commit -m "Add CLI final diagnosis builder"
```

## Task 4: Wire UIManager To Snapshot State Without Changing CLI Semantics

**Files:**
- Modify: `src/sag/ui/ui_manager.py`
- Create: `tests/test_ui_manager_observability.py`

- [ ] **Step 1: Write failing UIManager smoke tests**

Create `tests/test_ui_manager_observability.py`:

```python
from rich.console import Console

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.ui_manager import UIManager


def make_manager():
    console = Console(record=True, width=100)
    return UIManager(project_name="commons-cli", console=console)


def test_ui_manager_updates_snapshot_when_handling_events():
    manager = make_manager()

    manager.handle_event(UIEvent(EventType.PHASE_START, "Building", phase=PhaseType.BUILD))
    manager.handle_event(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.current_phase == PhaseType.BUILD
    assert snapshot.active_operation.tool_name == "maven"


def test_ui_manager_handles_unknown_event_without_crashing():
    manager = make_manager()
    event = UIEvent(EventType.STATUS_UPDATE, "Known")
    event.event_type = "unknown_event"

    manager.handle_event(event)

    assert manager.snapshot().latest_warning is not None


def test_ui_manager_render_failure_does_not_abort_event_handling(monkeypatch):
    manager = make_manager()

    def broken_render():
        raise RuntimeError("render exploded")

    monkeypatch.setattr(manager, "_render_display", broken_render)

    manager.handle_event(UIEvent(EventType.STATUS_UPDATE, "Still running"))

    assert manager.snapshot().current_status == "Still running"
    assert manager.snapshot().latest_warning is not None
    assert "render" in manager.snapshot().latest_warning.message.lower()


def test_ui_manager_start_render_failure_does_not_abort_ui_mode(monkeypatch):
    manager = make_manager()

    def broken_render():
        raise RuntimeError("initial render exploded")

    monkeypatch.setattr(manager, "_render_display", broken_render)

    manager.start()

    assert manager.live is None
    assert manager.snapshot().latest_warning is not None
    assert "initial render" in manager.snapshot().latest_warning.message.lower()


def test_display_final_summary_is_idempotent_with_snapshot_diagnosis():
    manager = make_manager()
    manager.handle_event(UIEvent(EventType.PHASE_START, "Building", phase=PhaseType.BUILD))
    manager.handle_event(UIEvent(EventType.PHASE_ERROR, "Build failed", phase=PhaseType.BUILD))
    manager.handle_event(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    manager.display_final_summary()
    first = manager.console.export_text()
    manager.display_final_summary()
    second = manager.console.export_text()

    assert first == second
    assert "Project setup incomplete" in first or "did not complete" in first
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_manager_observability.py -v
```

Expected: FAIL because `UIManager.snapshot()` does not exist and the manager does not use the aggregator.

- [ ] **Step 3: Add aggregator ownership to UIManager**

Modify `src/sag/ui/ui_manager.py`:

- Import `UIStateAggregator` and `build_final_diagnosis`. Do not import Task 6 snapshot-based component builders yet; Task 4 can keep the existing render helpers while it adds state ownership.
- In `__init__`, create `self._aggregator = UIStateAggregator(project_name)`.
- Add:

```python
def snapshot(self):
    """Return the current typed UI run state."""
    return self._aggregator.snapshot()
```

- Change `handle_event()` so it first calls `self._aggregator.handle(event)` inside a small `try` block. If aggregation raises unexpectedly, append a warning event to the aggregator and continue rendering from the last known state.
- Wrap `_update_display()` in a containment helper such as `_safe_update_display()`. If Rich rendering or any component builder raises, record a warning in the aggregator and return without re-raising. UI rendering failures must not abort the agent run.
- Wrap the initial `start()` render as well. If `_render_display()` fails before `Live` is constructed, record a warning, leave `self.live` as `None`, and return so `--ui` mode degrades instead of aborting the agent run.
- Preserve existing public methods: `start()`, `stop()`, `abort_running_phases()`, `display_final_summary()`.

Implementation can temporarily keep legacy mutable fields to reduce risk, but the render path in Task 6 must move to snapshot consumption. Do not add another independent state container.

- [ ] **Step 4: Update final summary to use diagnosis data**

In `display_final_summary()`:

- Stop the live display as before.
- Build `diagnosis = build_final_diagnosis(self.snapshot())`.
- Print a success or error panel based on `diagnosis.status`.
- Keep the detailed phase tree output for continuity.
- Keep idempotence through `_summary_shown`.

Do not remove existing report display until Task 6 adds an evidence/diagnosis panel that covers it.

- [ ] **Step 5: Run UIManager smoke tests**

Run:

```bash
uv run pytest tests/test_ui_manager_observability.py -v
```

Expected: PASS.

- [ ] **Step 6: Run focused UI tests**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_ui_manager_observability.py -v
```

Expected: PASS.

- [ ] **Step 7: Format and commit**

Run:

```bash
uv run black src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
uv run isort src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
git diff --check
git status --short
```

Commit:

```bash
git add src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
git commit -m "Connect UI manager to run state snapshots"
```

## Task 5: Add Tool Lifecycle And Evidence Events

**Files:**
- Modify: `src/sag/ui/events.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `src/sag/ui/state_aggregator.py`
- Modify: `tests/test_react_engine_tool_orchestration.py`
- Modify: `tests/test_ui_state_aggregator.py`
- Modify: `tests/test_ui_diagnosis.py`

- [ ] **Step 1: Write failing event coverage tests**

Extend `tests/test_ui_state_aggregator.py`:

```python
def test_aggregator_tracks_tool_lifecycle_and_evidence_events():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    start_state = aggregator.handle(
        UIEvent(
            EventType.TOOL_START,
            "Starting maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )
    assert start_state.active_operation.tool_name == "maven"
    assert start_state.timeline[-1].kind == "tool"

    recovery_state = aggregator.handle(
        UIEvent(
            EventType.TOOL_RECOVERY,
            "Retrying maven with known working directory",
            level="warning",
            metadata={
                "recovery_strategy": "maven_known_working_directory",
                "retry_count": 1,
                "recovery_params": {"goal": "compile", "working_directory": "/workspace/app"},
                "parameter_diff": {"working_directory": [None, "/workspace/app"]},
                "guidance": "Retrying in discovered project directory",
                "recovery": {"attempted": True, "success": True},
            },
        )
    )
    assert recovery_state.recovery.active is True
    assert recovery_state.recovery.strategy == "maven_known_working_directory"
    assert recovery_state.timeline[-1].failure_classification == "recovery_attempt"
    recovery_metadata = recovery_state.timeline[-1].metadata
    assert recovery_metadata["recovery_params"]["working_directory"] == "/workspace/app"
    assert recovery_metadata["parameter_diff"]
    assert recovery_metadata["recovery"]["success"] is True

    result_state = aggregator.handle(
        UIEvent(
            EventType.TOOL_RESULT,
            "maven compile completed",
            metadata={
                "tool_name": "maven",
                "tool_params": {"goal": "compile"},
                "executed_params": {"goal": "compile", "working_directory": "/workspace/app"},
                "status": "success",
                "duration_ms": 125.0,
            },
        )
    )
    assert result_state.evidence[-1].kind == "command"
    assert "maven" in result_state.evidence[-1].summary
    assert "compile" in result_state.evidence[-1].summary
    assert result_state.evidence[-1].metadata["status"] == "success"
    assert result_state.evidence[-1].metadata["tool_message"] == "maven compile completed"

    evidence_state = aggregator.handle(
        UIEvent(
            EventType.EVIDENCE_RECORDED,
            "review checkpoint completed",
            metadata={
                "kind": "review_checkpoint",
                "summary": "review agent approved implementation",
                "path": "reviews/goodall.md",
            },
        )
    )
    assert evidence_state.evidence[-1].kind == "review_checkpoint"
    assert evidence_state.evidence[-1].path == "reviews/goodall.md"


def test_aggregator_classifies_timeout_and_parameter_normalization_events():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    fixed_state = aggregator.handle(
        UIEvent(
            EventType.TOOL_PARAMETERS_FIXED,
            "Normalized maven parameters",
            level="warning",
            metadata={"tool_name": "maven", "field": "goal"},
        )
    )
    assert fixed_state.timeline[-1].failure_classification == "parameter_normalization"

    timeout_state = aggregator.handle(
        UIEvent(
            EventType.TOOL_ERROR,
            "maven command timeout",
            level="error",
            metadata={"tool_name": "maven", "error_code": "TIMEOUT"},
        )
    )
    assert timeout_state.latest_error.failure_classification == "command_timeout"
```

Extend `tests/test_ui_diagnosis.py`:

```python
def test_diagnosis_surfaces_tool_recovery_fallback_decision():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(
        UIEvent(
            EventType.TOOL_RECOVERY,
            "Retrying maven in discovered project directory",
            level="warning",
            metadata={
                "recovery_strategy": "maven_known_working_directory",
                "guidance": "Retrying in discovered project directory",
                "recovery_params": {"goal": "compile", "working_directory": "/workspace/app"},
                "parameter_diff": {"working_directory": [None, "/workspace/app"]},
                "recovery": {"attempted": True, "success": True},
            },
        )
    )
    state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    diagnosis = build_final_diagnosis(state)

    assert any("maven_known_working_directory" in item for item in diagnosis.recovery)
    assert any("/workspace/app" in item for item in diagnosis.recovery)
    assert any("working_directory" in item for item in diagnosis.recovery)
    assert any("success" in item for item in diagnosis.recovery)
```

Extend `tests/test_react_engine_tool_orchestration.py` with a narrow test around `_handle_tool_lifecycle_event`:

```python
def test_react_engine_maps_tool_lifecycle_events_to_ui_events(fake_engine):
    emitted = []
    fake_engine.emit = lambda *args, **kwargs: emitted.append((args, kwargs))

    call = ToolCall(name="maven", raw_params={"goal": "compile"})
    fake_engine._handle_tool_lifecycle_event(
        ToolLifecycleEvent(
            event_type="tool_start",
            call=call,
            message="Starting maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )

    assert emitted[0][0][0] == EventType.TOOL_START


def test_react_engine_preserves_real_tool_result_lifecycle_metadata(fake_engine):
    emitted = []
    fake_engine.emit = lambda *args, **kwargs: emitted.append((args, kwargs))

    call = ToolCall(
        name="maven",
        raw_params={"goal": "compile"},
        validated_params={"goal": "compile", "working_directory": "/workspace/app"},
    )
    fake_engine._handle_tool_lifecycle_event(
        ToolLifecycleEvent(
            event_type="tool_result",
            call=call,
            message="maven compile completed",
            level="success",
            metadata={
                "status": "success",
                "duration_ms": 125.0,
                "result_success": True,
                "error_code": None,
                "executed_params": {"goal": "compile", "working_directory": "/workspace/app"},
                "recovery_applied": False,
                "execution_signature": "maven:[('goal', 'compile')]",
            },
        )
    )

    event_type = emitted[0][0][0]
    metadata = emitted[0][1]
    assert event_type == EventType.TOOL_RESULT
    assert metadata["tool_name"] == "maven"
    assert metadata["tool_params"]["goal"] == "compile"
    assert metadata["executed_params"]["working_directory"] == "/workspace/app"
```

If the existing test file does not have a `fake_engine` fixture, create a local minimal object or use the existing helper pattern in that file. Do not instantiate the full CLI.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: FAIL because lifecycle event enum values and mapping do not exist.

- [ ] **Step 3: Extend EventType**

Modify `src/sag/ui/events.py`:

```python
    # Tool lifecycle events
    TOOL_START = "tool_start"
    TOOL_PARAMETERS_FIXED = "tool_parameters_fixed"
    TOOL_RESULT = "tool_result"
    TOOL_RECOVERY = "tool_recovery"
    TOOL_ERROR = "tool_error"

    # Evidence events
    EVIDENCE_RECORDED = "evidence_recorded"
```

- [ ] **Step 4: Map orchestrator lifecycle events in ReActEngine**

Modify `_handle_tool_lifecycle_event()` in `src/sag/agent/react_engine.py`:

```python
lifecycle_event_map = {
    "tool_start": EventType.TOOL_START,
    "tool_parameters_fixed": EventType.TOOL_PARAMETERS_FIXED,
    "tool_result": EventType.TOOL_RESULT,
    "tool_recovery": EventType.TOOL_RECOVERY,
    "tool_error": EventType.TOOL_ERROR,
}
event_type = lifecycle_event_map.get(event.event_type)
if event_type is None:
    return None

metadata = dict(event.metadata)
metadata.setdefault("tool_name", event.call.name)
metadata.setdefault("tool_params", event.call.validated_params or event.call.raw_params)
metadata.setdefault("tool_message", event.message)

self.emit(
    event_type,
    message=event.message,
    level=event.level,
    **metadata,
)
```

If a current test requires `tool_result` to stay telemetry-only, update that test to assert the new design: `tool_result` becomes UI telemetry, but the ReAct observation still owns the main observation text.

- [ ] **Step 5: Teach aggregator lifecycle handling**

Update `src/sag/ui/state_aggregator.py`:

- `TOOL_START`: update active operation and append timeline kind `tool`.
- `TOOL_PARAMETERS_FIXED`: append timeline kind `warning`, classification `parameter_normalization`, and include field/fix metadata.
- `TOOL_RESULT`: append timeline kind `observation`; append `UIEvidenceRecord(kind="command")` for every result using `tool_name`, `tool_params`, `executed_params`, `status`, `duration_ms`, and the event message. If metadata includes `summary`, use it; otherwise build a summary like `<tool_name> <visible action> -> <status>`. Preserve the raw metadata on the evidence record.
- `TOOL_RECOVERY`: update `RecoverySnapshot(active=True, strategy=..., retry_count=...)`, append timeline kind `recovery`, classification `recovery_attempt`. Preserve `recovery_strategy`, `recovery_params`, `parameter_diff`, `guidance`, and nested `recovery` metadata in the timeline metadata so final diagnosis can report the fallback decision.
- `TOOL_ERROR`: update latest error and append timeline kind `error`, classification from metadata or timeout/tool-failure rules.
- `EVIDENCE_RECORDED`: append `UIEvidenceRecord` with metadata `kind`, `summary`, `details`, and `path`; support `command`, `report`, `validation`, and `review_checkpoint` kinds.

Evidence coverage required by the spec:

- command summaries: every `TOOL_RESULT`, using `tool_message`, `tool_name`, `tool_params`, `executed_params`, `status`, `duration_ms`, and optional `path`
- report paths: `REPORT_GENERATED` from Task 2
- validation results: `VALIDATION_*` handling from Task 2
- review checkpoints: `EVIDENCE_RECORDED` with `kind="review_checkpoint"`

- [ ] **Step 6: Run lifecycle tests**

Run:

```bash
uv run pytest tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_react_engine_tool_orchestration.py -v
```

Expected: PASS.

- [ ] **Step 7: Format and commit**

Run:

```bash
uv run black src/sag/ui/events.py src/sag/agent/react_engine.py src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_react_engine_tool_orchestration.py
uv run isort src/sag/ui/events.py src/sag/agent/react_engine.py src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_react_engine_tool_orchestration.py
git diff --check
git status --short
```

Commit:

```bash
git add src/sag/ui/events.py src/sag/agent/react_engine.py src/sag/ui/state_aggregator.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_react_engine_tool_orchestration.py
git commit -m "Surface tool lifecycle events in CLI state"
```

## Task 6: Render Live Timeline, Active Operation, Recovery, Evidence, And Diagnosis Panels

**Files:**
- Modify: `src/sag/ui/components.py`
- Modify: `src/sag/ui/ui_manager.py`
- Modify: `tests/test_ui_manager_observability.py`

- [ ] **Step 1: Write failing render smoke tests**

Extend `tests/test_ui_manager_observability.py`:

```python
def test_render_display_includes_live_timeline_and_active_operation():
    manager = make_manager()
    manager.handle_event(
        UIEvent(
            EventType.TOOL_START,
            "Starting maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )
    manager.handle_event(UIEvent(EventType.AGENT_OBSERVATION, "maven compile passed"))

    manager.console.print(manager._render_display())
    output = manager.console.export_text()

    assert "maven" in output
    assert "compile" in output
    assert "Timeline" in output


def test_render_display_includes_recovery_and_evidence_when_present():
    manager = make_manager()
    manager.handle_event(
        UIEvent(
            EventType.TOOL_RECOVERY,
            "Retrying with fallback",
            level="warning",
            metadata={"strategy": "fallback", "retry_count": 1},
        )
    )
    manager.handle_event(
        UIEvent(
            EventType.EVIDENCE_RECORDED,
            "compile log captured",
            metadata={"kind": "command", "summary": "maven compile", "path": "logs/maven.log"},
        )
    )

    manager.console.print(manager._render_display())
    output = manager.console.export_text()

    assert "Recovery" in output
    assert "Evidence" in output
    assert "logs/maven.log" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_ui_manager_observability.py -v
```

Expected: FAIL because snapshot-specific panels are not rendered yet.

- [ ] **Step 3: Add snapshot-based component builders**

Modify `src/sag/ui/components.py` with small builders:

- `create_status_header(state: UIRunState, elapsed_time: str) -> Panel`
- `create_phase_timeline(state: UIRunState) -> Tree`
- `create_active_operation_panel(state: UIRunState) -> Panel | None`
- `create_recent_timeline_panel(state: UIRunState, limit: int = 6) -> Panel | None`
- `create_recovery_panel(state: UIRunState) -> Panel | None`
- `create_evidence_panel(state: UIRunState, limit: int = 5) -> Panel | None`
- `create_final_diagnosis_panel(diagnosis: FinalDiagnosis) -> Panel`

Keep display text concise. The live screen should show current state and recent
events, not a full log dump.

- [ ] **Step 4: Render UIManager from snapshot**

Modify `_render_display()` in `src/sag/ui/ui_manager.py`:

- Get `state = self.snapshot()`.
- Build status header from `state`.
- Render phase timeline from `state`.
- Render active operation, recovery, evidence, and recent timeline panels only when data exists.
- Render latest error or warning from `state.latest_error` / `state.latest_warning`.
- Keep `display_final_summary()` idempotent and diagnosis-based.

After this step, remove event-handler mutable fields from `UIManager` when they are no longer needed by rendering or final summary. If removing all legacy fields is too risky, leave them only as temporary compatibility shims and add a comment naming the follow-up.

- [ ] **Step 5: Run render smoke tests**

Run:

```bash
uv run pytest tests/test_ui_manager_observability.py -v
```

Expected: PASS.

- [ ] **Step 6: Run focused UI suite**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_ui_manager_observability.py -v
```

Expected: PASS.

- [ ] **Step 7: Format and commit**

Run:

```bash
uv run black src/sag/ui/components.py src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
uv run isort src/sag/ui/components.py src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
git diff --check
git status --short
```

Commit:

```bash
git add src/sag/ui/components.py src/sag/ui/ui_manager.py tests/test_ui_manager_observability.py
git commit -m "Render CLI observability panels from snapshots"
```

## Task 7: Final Verification And Documentation Check

**Files:**
- Modify if needed: `README.md`
- Modify if needed: `docs/superpowers/specs/2026-06-02-cli-observability-design.md`

- [ ] **Step 1: Check whether README needs a `--ui` update**

Run:

```bash
rg -n -- "--ui|UI mode|Rich|sag run" README.md
```

If README already documents the improved `--ui` mode accurately, do not edit it.
If it omits `--ui`, add a short CLI option note only. Do not add a marketing section or web UI promise.

- [ ] **Step 2: Run focused and full tests**

Run:

```bash
uv run pytest tests/test_ui_state_models.py tests/test_ui_state_aggregator.py tests/test_ui_diagnosis.py tests/test_ui_manager_observability.py tests/test_react_engine_tool_orchestration.py -v
uv run pytest
```

Expected: PASS. If `tests/test_packaging_smoke.py` needs network to download build dependencies, request escalation instead of skipping it.

- [ ] **Step 3: Run format/import guards**

Run:

```bash
uv run black --check src tests
uv run isort --check-only src tests
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py -v
git diff --check
git status --short
```

Expected: PASS, no whitespace issues, and only intentional files changed.

- [ ] **Step 4: Request implementation review agent**

Use a review agent to check correctness before finalizing. Provide:

- Spec path: `docs/superpowers/specs/2026-06-02-cli-observability-design.md`
- Plan path: `docs/superpowers/plans/2026-06-02-cli-observability-implementation.md`
- Commit range for the implementation branch
- Focus areas: state aggregation correctness, UIManager responsibility creep, lifecycle event mapping, final diagnosis source of truth, and test coverage

Do not merge or push until review issues are addressed or explicitly deferred.

- [ ] **Step 5: Final commit if README/spec changed**

If Task 7 changed docs after previous commits:

```bash
git add README.md docs/superpowers/specs/2026-06-02-cli-observability-design.md
git commit -m "Document CLI observability UI"
```

If no docs changed, skip this commit.

## Final Acceptance Checklist

- [ ] `UIStateAggregator` owns event-to-state logic.
- [ ] `UIRunState` is the snapshot consumed by rendering and diagnosis.
- [ ] `UIManager` coordinates lifecycle and no longer grows broad event-state responsibilities.
- [ ] Live TUI shows current operation, recent timeline, recovery, evidence, and phase state.
- [ ] Final diagnosis is generated from typed state/evidence, not rendered terminal text.
- [ ] Tool lifecycle events are surfaced to UI state where useful.
- [ ] Existing non-UI CLI behavior remains unchanged.
- [ ] Focused UI tests and full pytest pass.
- [ ] Commit messages contain no Co-Authorship or authorship trailers.
