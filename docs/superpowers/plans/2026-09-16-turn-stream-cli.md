# Turn Stream and CLI Surfaces Implementation Plan (phase 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the log firehose during a run with one readable line per turn, derived from the authoritative control ledger, and bring the rest of the CLI's commands up to the current result model.

**Architecture:** The trajectory reducer gains three human-readable fields it derives from payloads the ledger already carries: a call summary, an observation outcome and an observation summary. A new `sag.console.turn_stream` renderer folds control-event lines through that reducer and writes one line per turn to the terminal; it is attached to the session's control-event sink as a second observer, so console output and the Workbench read the same derivation. Loguru's console sink drops to WARNING, the disconnected `--ui` mode is deleted, and the remaining commands (`result`, `trajectory`, `list`, `run`, `inspect`) are brought onto the result card and the turn vocabulary.

**Tech Stack:** Python 3.10+, pydantic v2, Rich (markup and terminal width only), pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-result-card-and-trajectory-surfaces-design.md` — read §3 and §4 before starting.

**Depends on:** `docs/superpowers/plans/2026-09-16-result-card.md` (phase 1) for `sag.result_card` and `sag.console.result_block`. Tasks 1-4 of this plan do not need phase 1; Tasks 9 and 11 do.

## Global Constraints

- **Console logs are never an input.** Every rendered turn derives from `control_events.jsonl` through `TrajectoryReducer`. Rendering a log line as evidence is the exact failure this layer exists to end.
- **The reducer never writes, reorders or mutates a source file.** It opens files read-only.
- **`schema_version` stays `1`.** The new trajectory fields are optional; a document without them stays valid, and `extra="forbid"` models accept them because they are declared.
- **Absence is stated, never implied.** A turn with no call says so; a summary that cannot be derived is `None`, never an empty string.
- **Batch and follow must agree.** `build_trajectory(dir)` must equal `DeltaAccumulator(follow_trajectory(dir))` for every fixture, including the new fields. `tests/test_trajectory_golden.py` is the fence.
- **Forbidden in any user-visible string:** `sealed`, `canonical`, `claimed`, `quarantined`, `subject`, `snapshot`, `metrics-v2`, `verdict-bearing`, `promoting`.
- **No judgment changes.** Nothing in this plan alters a verdict, a gate, a receipt or a rate.
- **Commit messages carry no `Co-Authored-By` trailer.**
- **Test command:** `PYTHONPATH=.:tests uv run pytest <path> -v`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/sag/trajectory/schema.py` | +3 optional field groups on `CallInfo`, `ObservationInfo`, `PhaseInfo` |
| `src/sag/trajectory/summaries.py` | **New.** `call_summary(tool, params)` and `observation_outcome(result)` / `observation_summary(tool, result)` — pure functions over payload dicts |
| `src/sag/trajectory/reducer.py` | Calls the summarisers at the three handlers that already hold the payloads |
| `src/sag/console/turn_stream.py` | **New.** `TurnStreamRenderer` — control-event lines in, terminal lines out |
| `src/sag/agent/control_events.py` | `ControlEventSink` gains an `observers` tuple beside `mirror` |
| `src/sag/config/logger.py` | One console implementation; WARNING default; `ui_mode` and `suppress_console_logging` removed |
| `src/sag/main.py` | Turn stream wiring; `--ui` removal; `result`, `trajectory --format`, `list`, `run`, `inspect --turn` |
| `README.md` | `sag trajectory` now needs `--format json` to pipe to `jq` |

Deleted: `src/sag/ui/` (6 files), `tests/test_ui_state_aggregator.py`, `tests/test_ui_manager_observability.py`, `tests/test_ui_diagnosis.py`, `tests/test_ui_state_models.py`.

---

### Task 1: Trajectory summary fields

**Files:**
- Modify: `src/sag/trajectory/schema.py:54-63` (`CallInfo`), `:65-80` (`ObservationInfo`), `:129-134` (`PhaseInfo`)
- Test: `tests/test_trajectory_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CallInfo.summary: str | None`; `ObservationInfo.outcome: Literal["ok","failed","refused","pending","cancelled"] | None` and `ObservationInfo.summary: str | None`; `PhaseInfo.validator_state: Literal["green","partial","red","unavailable"] | None`, `PhaseInfo.reason: str | None`, `PhaseInfo.key_results: str | None`; `SUMMARY_MAX_CHARS = 80`, `KEY_RESULTS_MAX_CHARS = 400`, `ObservationOutcome` type alias.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_trajectory_schema.py`:

```python
def test_call_carries_an_optional_human_summary():
    from sag.trajectory.schema import CallInfo

    assert CallInfo(tool="build").summary is None
    assert CallInfo(tool="build", summary="verify mvn clean verify").summary == (
        "verify mvn clean verify"
    )


def test_a_summary_longer_than_the_cap_is_rejected():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import SUMMARY_MAX_CHARS, CallInfo

    with pytest.raises(ValidationError):
        CallInfo(tool="bash", summary="x" * (SUMMARY_MAX_CHARS + 1))


def test_observation_states_how_the_call_came_out():
    from sag.trajectory.schema import ObservationInfo

    observation = ObservationInfo(outcome="failed", summary="exit 1 · enforcer")
    assert observation.outcome == "failed"
    assert observation.summary == "exit 1 · enforcer"
    assert ObservationInfo().outcome is None


def test_observation_outcome_is_a_closed_vocabulary():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import ObservationInfo

    with pytest.raises(ValidationError):
        ObservationInfo(outcome="probably fine")


def test_phase_carries_what_its_gate_decided():
    from sag.trajectory.schema import PhaseInfo

    phase = PhaseInfo(
        name="build",
        termination="advance",
        validator_state="green",
        reason="JVM build execution validated",
        key_results="Executed mvn clean verify",
    )
    assert phase.validator_state == "green"
    assert PhaseInfo(name="build").reason is None


def test_a_document_without_the_new_fields_still_validates():
    from sag.trajectory.schema import Trajectory

    document = Trajectory.model_validate(
        {
            "schema_version": 1,
            "session": {"run_id": "r"},
            "phases": [{"name": "build"}],
            "turns": [
                {
                    "turn_id": 1,
                    "phase": "build",
                    "actor": "model",
                    "call": {"tool": "build"},
                    "observation": {"ref": "output_a"},
                    "control_seq": [1],
                }
            ],
        }
    )
    assert document.turns[0].call.summary is None
    assert document.turns[0].observation.outcome is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_schema.py -v -k "summary or outcome or gate_decided"`
Expected: FAIL with `ValidationError: Extra inputs are not permitted [type=extra_forbidden]` for `summary`.

- [ ] **Step 3: Add the fields**

In `src/sag/trajectory/schema.py`, add near `DETAIL_TIERS`:

```python
#: How long a one-line summary may be. Long enough for a Maven command, short
#: enough that a turn stays one terminal row at 80 columns.
SUMMARY_MAX_CHARS = 80
#: A phase's key results are a paragraph the gate wrote, not a line.
KEY_RESULTS_MAX_CHARS = 400

ObservationOutcome = Literal["ok", "failed", "refused", "pending", "cancelled"]
ValidatorState = Literal["green", "partial", "red", "unavailable"]
```

Extend `CallInfo` (`:54`):

```python
class CallInfo(_TrajectoryModel):
    tool: str
    params_ref: str | None = None
    #: What this call asked for, in one line. Derived from the envelope's exact
    #: params; `None` when the tool's shape is unknown to the summariser, never
    #: an empty string, so "no summary" and "an empty summary" stay distinct.
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
```

Extend `ObservationInfo` (`:65`):

```python
class ObservationInfo(_TrajectoryModel):
    ref: str | None = None
    evidence_ref: str | None = None
    error_code: str | None = None
    failure_signature: str | None = None
    #: How the call came out, as one of five words. `pending` is a dispatched
    #: job with no terminal exit yet; `cancelled` is a call a batch break
    #: stopped before it dispatched anything.
    outcome: ObservationOutcome | None = None
    #: What came back, in one line.
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
```

Extend `PhaseInfo` (`:129`):

```python
class PhaseInfo(_TrajectoryModel):
    name: str
    termination: str | None = None
    gates: list[GateInfo] = Field(default_factory=list)
    #: What the validator observed when the phase closed.
    validator_state: ValidatorState | None = None
    #: Why the gate decided what it decided.
    reason: str | None = None
    #: What the phase reported it achieved, as the gate recorded it.
    key_results: str | None = Field(default=None, max_length=KEY_RESULTS_MAX_CHARS)
```

Add `Field` to the module's pydantic import if it is not already there, and add the new names to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_schema.py -v`
Expected: PASS, existing tests plus 6 new.

- [ ] **Step 5: Commit**

```bash
git add src/sag/trajectory/schema.py tests/test_trajectory_schema.py
git commit -m "feat(trajectory): a turn can carry a human summary of its call and result"
```

---

### Task 2: Derive the summaries

**Files:**
- Create: `src/sag/trajectory/summaries.py`
- Test: `tests/test_trajectory_summaries.py`

**Interfaces:**
- Consumes: Task 1's `SUMMARY_MAX_CHARS`.
- Produces: `call_summary(tool: str, params: Mapping[str, Any] | None) -> str | None`; `observation_outcome(result: Mapping[str, Any] | None) -> ObservationOutcome | None`; `observation_summary(tool: str, result: Mapping[str, Any] | None) -> str | None`; `refusal_summary(payload: Mapping[str, Any]) -> tuple[ObservationOutcome, str | None]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_trajectory_summaries.py`:

```python
"""A turn reads as a line: what was asked, and what came back."""

from sag.trajectory.schema import SUMMARY_MAX_CHARS
from sag.trajectory.summaries import (
    call_summary,
    observation_outcome,
    observation_summary,
    refusal_summary,
)


def test_build_call_names_its_action_and_command():
    assert (
        call_summary(
            "build",
            {
                "command": "mvn clean verify",
                "system": "maven",
                "working_directory": "/workspace/commons-cli",
            },
        )
        == "verify mvn clean verify"
    )


def test_build_call_without_a_recognisable_action_shows_the_command():
    assert call_summary("build", {"command": "./gradlew check"}) == "./gradlew check"


def test_bash_call_collapses_the_command_to_its_first_line():
    assert call_summary("bash", {"command": "cat <<'EOF'\nline two\nEOF"}) == "cat <<'EOF'"


def test_project_clone_names_the_repository_and_ref():
    assert (
        call_summary(
            "project",
            {
                "action": "clone",
                "repo_url": "https://github.com/apache/commons-cli.git",
                "ref": "e17111798da51037659b3594d9c0b3b525040081",
            },
        )
        == "clone apache/commons-cli@e171117"
    )


def test_project_provision_names_the_toolchain():
    assert (
        call_summary(
            "project", {"action": "provision", "java_distribution": "openjdk", "java_version": "17"}
        )
        == "provision openjdk 17"
    )
    assert (
        call_summary("project", {"action": "provision", "maven_version": "3.9"})
        == "provision maven 3.9"
    )


def test_files_and_search_and_phase_and_advisor_and_report():
    assert call_summary("files", {"action": "write", "path": "/workspace/x/pom.xml"}) == (
        "write /workspace/x/pom.xml"
    )
    assert call_summary("search", {"target": "output_abc", "pattern": "ERROR"}) == (
        "output_abc /ERROR/"
    )
    assert call_summary("search", {"query": "maven enforcer minimum"}) == (
        "maven enforcer minimum"
    )
    assert call_summary("phase", {"signal": "done", "phase": "build"}) == "done build"
    assert call_summary("advisor", {"question": "what now"}) == "consult"
    assert call_summary("report", {}) == "generate"


def test_an_unknown_tool_shows_its_first_three_scalar_parameters():
    summary = call_summary("mystery", {"a": 1, "b": "two", "c": True, "d": [1, 2], "e": "five"})
    assert summary == "a=1 b=two c=True"


def test_a_summary_is_truncated_with_an_ellipsis():
    summary = call_summary("bash", {"command": "echo " + "x" * 200})
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_no_params_yields_no_summary():
    assert call_summary("build", None) is None
    assert call_summary("build", {}) is None


def test_outcome_reads_success_as_ok():
    assert observation_outcome({"operation_outcome": "success"}) == "ok"


def test_outcome_reads_a_running_dispatch_as_pending():
    assert observation_outcome({"invocation_status": "dispatched"}) == "pending"
    assert observation_outcome({"invocation_status": "running"}) == "pending"


def test_outcome_falls_back_to_failed():
    assert observation_outcome({"operation_outcome": "failed"}) == "failed"
    assert observation_outcome({}) == "failed"


def test_no_result_yields_no_outcome():
    assert observation_outcome(None) is None


def test_build_result_summarises_exit_tests_and_artifacts():
    result = {
        "operation_outcome": "success",
        "facts": {"executed": 994, "passed": 933, "failed": 0, "skipped": 61},
        "metadata": {
            "analysis": {
                "exit_code": 0,
                "artifacts_created": ["a.jar", "b.jar"],
                "log_tests_run": {"errors": 0},
            }
        },
    }
    assert observation_summary("build", result) == "exit 0 · 994 tests · 0 F · 0 E · 61 S · 2 jars"


def test_failed_build_result_names_its_error():
    result = {
        "operation_outcome": "failed",
        "error_code": "MAVEN_VERSION_BELOW_MINIMUM",
        "metadata": {"analysis": {"exit_code": 1}},
    }
    assert observation_summary("build", result) == "exit 1 · MAVEN_VERSION_BELOW_MINIMUM"


def test_provision_result_names_the_version_it_verified():
    result = {
        "operation_outcome": "success",
        "metadata": {"verified_java_version": "17.0.20", "java_version": "17"},
    }
    assert observation_summary("project", result) == "java 17.0.20"


def test_clone_result_names_the_commit_and_path():
    result = {
        "operation_outcome": "success",
        "metadata": {
            "resolved_commit": "e17111798da51037659b3594d9c0b3b525040081",
            "clone_path": "/workspace/commons-cli",
        },
    }
    assert observation_summary("project", result) == "e171117 → /workspace/commons-cli"


def test_phase_result_names_the_gate_word_and_reason():
    result = {
        "operation_outcome": "success",
        "facts": {"gate": "success", "reason": "workspace /workspace/commons-cli exists"},
    }
    assert observation_summary("phase", result) == (
        "gate success · workspace /workspace/commons-cli exists"
    )


def test_advisor_result_says_advice_was_delivered():
    assert observation_summary("advisor", {"operation_outcome": "success"}) == "advice delivered"


def test_a_pending_job_names_its_handle():
    result = {"invocation_status": "dispatched", "metadata": {"job_id": "c523e63040aa"}}
    assert observation_summary("build", result) == "running · job c523e63040aa"


def test_a_refusal_states_its_code():
    outcome, summary = refusal_summary(
        {"refusal_code": "CALL_NOT_EXECUTED", "reason": "phase transition applied"}
    )
    assert outcome == "cancelled"
    assert summary == "cancelled: phase transition applied"


def test_a_non_cancellation_refusal_is_a_refusal():
    outcome, summary = refusal_summary({"refusal_code": "TOOL_PARAMETERS_INVALID"})
    assert outcome == "refused"
    assert summary == "TOOL_PARAMETERS_INVALID"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_summaries.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.trajectory.summaries'`.

- [ ] **Step 3: Write the summarisers**

Create `src/sag/trajectory/summaries.py`:

```python
"""One line for what a call asked, one for what it answered.

Pure functions over the payload dicts the ledger already carries. They add no
facts: every value here is copied out of an `action_envelope`'s exact params or
a `tool_result`'s projected result. Where a shape is not recognised the answer
is `None`, so a reader is never shown a confident-looking blank.
"""

from __future__ import annotations

from typing import Any, Mapping

from sag.trajectory.schema import SUMMARY_MAX_CHARS, ObservationOutcome

_SHA_CHARS = 7
_MAX_SCALAR_PARAMS = 3
_PENDING_STATUSES = frozenset({"dispatched", "running", "polling"})
_CANCELLED_REFUSAL_CODES = frozenset({"CALL_NOT_EXECUTED"})


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    if len(collapsed) <= SUMMARY_MAX_CHARS:
        return collapsed
    return collapsed[: SUMMARY_MAX_CHARS - 1] + "…"


def _first_line(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.splitlines()[0].split()) or None


def _join(*parts: str | None) -> str | None:
    kept = [part for part in parts if part]
    return " · ".join(kept) if kept else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _repo_slug(url: Any) -> str | None:
    if not isinstance(url, str) or "/" not in url:
        return None
    trimmed = url.rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    parts = [part for part in trimmed.split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def _short_sha(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < _SHA_CHARS:
        return None
    return value[:_SHA_CHARS]


def _build_call(params: dict[str, Any]) -> str | None:
    command = _first_line(params.get("command")) or _first_line(params.get("source_command"))
    action = params.get("action") or params.get("effective_action")
    if command and isinstance(action, str) and action:
        return f"{action} {command}"
    return command


def _project_call(params: dict[str, Any]) -> str | None:
    action = params.get("action")
    if action == "clone":
        slug = _repo_slug(params.get("repo_url")) or "repository"
        sha = _short_sha(params.get("ref"))
        return f"clone {slug}@{sha}" if sha else f"clone {slug}"
    if action == "provision":
        maven = params.get("maven_version")
        if maven:
            return f"provision maven {maven}"
        java = params.get("java_version")
        if java:
            distribution = params.get("java_distribution") or "jdk"
            return f"provision {distribution} {java}"
        return "provision"
    if action == "env":
        keys = params.get("keys") or params.get("variables")
        if isinstance(keys, (list, tuple)) and keys:
            return "env " + ", ".join(str(key) for key in keys[:3])
        return "env"
    return str(action) if action else None


def _search_call(params: dict[str, Any]) -> str | None:
    target = params.get("target")
    pattern = params.get("pattern")
    if target:
        return f"{target} /{pattern}/" if pattern else str(target)
    return _first_line(params.get("query"))


def _scalar_params(params: dict[str, Any]) -> str | None:
    pairs = [
        f"{key}={value}"
        for key, value in params.items()
        if isinstance(value, (str, int, float, bool))
    ]
    return " ".join(pairs[:_MAX_SCALAR_PARAMS]) or None


def call_summary(tool: str, params: Mapping[str, Any] | None) -> str | None:
    """What this call asked for, in one line."""

    values = _mapping(params)
    if not values:
        return None
    if tool == "build":
        return _clip(_build_call(values))
    if tool == "bash":
        return _clip(_first_line(values.get("command")))
    if tool == "project":
        return _clip(_project_call(values))
    if tool == "files":
        action = values.get("action")
        path = values.get("path") or values.get("file_path")
        return _clip(f"{action} {path}" if action and path else (action or path))
    if tool == "search":
        return _clip(_search_call(values))
    if tool == "phase":
        signal = values.get("signal")
        phase = values.get("phase")
        return _clip(f"{signal} {phase}" if signal and phase else (signal or phase))
    if tool == "advisor":
        return "consult"
    if tool == "report":
        return "generate"
    return _clip(_scalar_params(values))


def observation_outcome(result: Mapping[str, Any] | None) -> ObservationOutcome | None:
    """How the call came out, as one of five words."""

    if result is None:
        return None
    values = _mapping(result)
    status = str(values.get("invocation_status") or "").strip().lower()
    if status in _PENDING_STATUSES:
        return "pending"
    if str(values.get("operation_outcome") or "").strip().lower() == "success":
        return "ok"
    return "failed"


def _analysis(result: dict[str, Any]) -> dict[str, Any]:
    return _mapping(_mapping(result.get("metadata")).get("analysis"))


def _build_observation(result: dict[str, Any], outcome: ObservationOutcome) -> str | None:
    analysis = _analysis(result)
    facts = _mapping(result.get("facts"))
    exit_code = analysis.get("exit_code")
    exit_text = f"exit {exit_code}" if isinstance(exit_code, int) else None
    if outcome != "ok":
        reason = result.get("error_code") or _first_line(result.get("failure_signature"))
        return _join(exit_text, str(reason) if reason else None)
    executed = facts.get("executed")
    counts = None
    if isinstance(executed, int):
        errors = facts.get("errors")
        if not isinstance(errors, int):
            errors = _mapping(analysis.get("log_tests_run")).get("errors")
        counts = (
            f"{executed:,} tests · {facts.get('failed', 0)} F · "
            f"{errors if isinstance(errors, int) else 0} E · {facts.get('skipped', 0)} S"
        )
    artifacts = analysis.get("artifacts_created")
    jars = f"{len(artifacts)} jars" if isinstance(artifacts, list) and artifacts else None
    return _join(exit_text, counts, jars)


def _project_observation(result: dict[str, Any]) -> str | None:
    metadata = _mapping(result.get("metadata"))
    commit = _short_sha(metadata.get("resolved_commit"))
    if commit:
        path = metadata.get("clone_path")
        return f"{commit} → {path}" if path else commit
    java = metadata.get("verified_java_version")
    if java:
        return f"java {java}"
    maven = metadata.get("verified_maven_version") or metadata.get("maven_version")
    if maven:
        return f"maven {maven}"
    return None


def observation_summary(tool: str, result: Mapping[str, Any] | None) -> str | None:
    """What came back, in one line."""

    if result is None:
        return None
    values = _mapping(result)
    outcome = observation_outcome(values)
    if outcome == "pending":
        job = _mapping(values.get("metadata")).get("job_id")
        return _clip(f"running · job {job}" if job else "running")
    if tool == "build":
        return _clip(_build_observation(values, outcome))
    if tool == "project":
        return _clip(_project_observation(values) or (None if outcome == "ok" else
                     str(values.get("error_code") or "")))
    if tool == "phase":
        facts = _mapping(values.get("facts"))
        gate = facts.get("gate") or facts.get("signal")
        reason = _first_line(facts.get("reason"))
        return _clip(_join(f"gate {gate}" if gate else None, reason))
    if tool == "advisor":
        return "advice delivered" if outcome == "ok" else _clip(str(values.get("error_code") or ""))
    if outcome == "ok":
        return None
    return _clip(
        str(values.get("error_code") or "")
        or _first_line(values.get("failure_signature"))
        or "failed"
    )


def refusal_summary(payload: Mapping[str, Any]) -> tuple[ObservationOutcome, str | None]:
    """How a refused call reads: cancelled before dispatch, or refused outright."""

    values = _mapping(payload)
    code = str(values.get("refusal_code") or "").strip()
    reason = _first_line(values.get("reason"))
    if code in _CANCELLED_REFUSAL_CODES:
        return "cancelled", _clip(f"cancelled: {reason}" if reason else "cancelled")
    return "refused", _clip(_join(code or None, reason))


__all__ = [
    "call_summary",
    "observation_outcome",
    "observation_summary",
    "refusal_summary",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_summaries.py -v`
Expected: PASS, 22 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/trajectory/summaries.py tests/test_trajectory_summaries.py
git commit -m "feat(trajectory): derive a one-line summary for every call and result"
```

---

### Task 3: Wire the summaries into the reducer

**Files:**
- Modify: `src/sag/trajectory/reducer.py:687-716` (`_on_action_envelope`), `:717-747` (`_on_forced_action`), `:840-877` (`_on_refusal_record`), `:878-910` (`_on_tool_result`), `:966-1065` (`_on_gate_decision`)
- Test: `tests/test_trajectory_reducer.py`, `tests/test_trajectory_golden.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: turns whose `call.summary`, `observation.outcome` and `observation.summary` are populated, and phases whose `validator_state`, `reason` and `key_results` are populated.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_trajectory_reducer.py`:

```python
def test_reducer_summarises_a_build_call_and_its_result():
    from sag.trajectory.reducer import TrajectoryReducer

    reducer = TrajectoryReducer()
    reducer.feed(
        _event(
            1,
            "action_envelope",
            {
                "envelope_id": "envelope-000001",
                "tool_call_id": "call-1",
                "tool": "build",
                "exact_params": {"command": "mvn clean verify", "action": "verify"},
                "envelope_sha256": "a" * 64,
            },
        )
    )
    reducer.feed(
        _event(
            2,
            "tool_result",
            {
                "envelope_id": "envelope-000001",
                "execution_id": "x1",
                "tool": "build",
                "params": {"command": "mvn clean verify"},
                "scope": "test_runtime",
                "result": {
                    "operation_outcome": "success",
                    "invocation_status": "completed",
                    "facts": {"executed": 994, "failed": 0, "skipped": 61, "errors": 0},
                    "metadata": {"analysis": {"exit_code": 0, "artifacts_created": ["a.jar"]}},
                },
            },
        )
    )
    turn = reducer.snapshot().turns[0]
    assert turn.call.summary == "verify mvn clean verify"
    assert turn.observation.outcome == "ok"
    assert turn.observation.summary == "exit 0 · 994 tests · 0 F · 0 E · 61 S · 1 jars"


def test_reducer_marks_a_refused_call():
    from sag.trajectory.reducer import TrajectoryReducer

    reducer = TrajectoryReducer()
    reducer.feed(
        _event(
            1,
            "refusal_record",
            {
                "envelope_id": None,
                "tool": "build",
                "refusal_code": "CALL_NOT_EXECUTED",
                "reason": "phase transition applied",
                "observation_ref": "output_a",
            },
        )
    )
    turn = reducer.snapshot().turns[0]
    assert turn.observation.outcome == "cancelled"
    assert turn.observation.summary == "cancelled: phase transition applied"


def test_phase_carries_the_gates_validator_state_and_reason():
    from sag.trajectory.reducer import TrajectoryReducer

    reducer = TrajectoryReducer()
    reducer.feed(
        _event(
            1,
            "gate_decision",
            {
                "phase": "build",
                "signal": "done",
                "claimed_outcome": "success",
                "validator_state": "green",
                "expected_accepted": True,
                "expected_outcome": "success",
                "reason": "JVM build execution validated",
                "key_results": "Executed mvn clean verify in /workspace/commons-cli",
                "evidence_refs": [],
                "decision_id": "gate-1",
            },
        )
    )
    reducer.feed(
        _event(
            2,
            "phase_transition",
            {
                "expected_kind": "advance",
                "expected_target": "test",
                "expected_reason_code": "build_complete",
            },
        )
    )
    phase = reducer.snapshot().phases[0]
    assert phase.validator_state == "green"
    assert phase.reason == "JVM build execution validated"
    assert phase.key_results.startswith("Executed mvn clean verify")
```

If `tests/test_trajectory_reducer.py` has no `_event` helper, add one at the top of the file:

```python
def _event(sequence: int, kind: str, payload: dict) -> str:
    import json

    return json.dumps(
        {
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
            "source": None,
            "timestamp": f"2026-09-15T01:0{sequence % 10}:00Z",
            "event_id": f"control-{sequence:06d}",
            "run_id": "run-1",
        }
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_reducer.py -v -k "summarises or refused or validator_state"`
Expected: FAIL — `turn.call.summary` is `None`.

- [ ] **Step 3: Populate the call summary**

In `_on_action_envelope` (`reducer.py:709`), replace

```python
        turn.call = CallInfo(tool=tool, params_ref=envelope_id)
```

with

```python
        turn.call = CallInfo(
            tool=tool,
            params_ref=envelope_id,
            summary=call_summary(tool, payload.get("exact_params")),
        )
```

Apply the identical change in `_on_forced_action` (`:727`). Add the import at the top of `reducer.py`:

```python
from sag.trajectory.summaries import (
    call_summary,
    observation_outcome,
    observation_summary,
    refusal_summary,
)
```

- [ ] **Step 4: Populate the observation outcome and summary**

In `_on_tool_result` (`:891`), after the existing `_merge_observation` call, add:

```python
        tool_name = turn.call.tool if turn.call else str(payload.get("tool") or "")
        turn.observation = turn.observation.model_copy(
            update={
                "outcome": observation_outcome(result),
                "summary": observation_summary(tool_name, result),
            }
        )
```

In `_on_refusal_record`, after the turn's observation is first set, add:

```python
        outcome, summary = refusal_summary(payload)
        turn.observation = (turn.observation or ObservationInfo()).model_copy(
            update={"outcome": outcome, "summary": summary}
        )
```

Import `ObservationInfo` in `reducer.py` if it is not already imported.

- [ ] **Step 5: Populate the phase fields**

In `_on_gate_decision`, where the gate is attached to the phase state, also record the validator's reading on the phase band. Find the `_PhaseState` the gate belongs to (the reducer already resolves it to append `gates`) and set:

```python
            state.validator_state = _text(payload.get("validator_state"))
            state.reason = _text(payload.get("reason"))
            key_results = _text(payload.get("key_results"))
            if key_results and len(key_results) > KEY_RESULTS_MAX_CHARS:
                key_results = key_results[: KEY_RESULTS_MAX_CHARS - 1] + "…"
            state.key_results = key_results
```

Add the three fields to `_PhaseState` and carry them through `_PhaseState.render()`:

```python
@dataclass
class _PhaseState:
    name: str
    termination: str | None = None
    gates: list[GateInfo] = field(default_factory=list)
    validator_state: str | None = None
    reason: str | None = None
    key_results: str | None = None

    def render(self) -> PhaseInfo:
        return PhaseInfo(
            name=self.name,
            termination=self.termination,
            gates=list(self.gates),
            validator_state=self.validator_state,
            reason=self.reason,
            key_results=self.key_results,
        )
```

Import `KEY_RESULTS_MAX_CHARS` from `sag.trajectory.schema`.

- [ ] **Step 6: Run the reducer tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_reducer.py -v`
Expected: PASS.

- [ ] **Step 7: Re-pin the golden fixtures**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_golden.py -v`
Expected: PASS without changes — the golden tests pin counts and identities, not the new fields. If a test compares whole `Turn` objects against a stored JSON file, regenerate that file with:

```bash
PYTHONPATH=. uv run python -c "
from pathlib import Path
from sag.trajectory.builder import build_trajectory
for name in ('kafka-d2r3', 'camel-quarkus-d2r3', 'ignite-d2r3'):
    directory = Path('tests/fixtures/trajectory') / name
    expected = directory.parent / f'{name}.expected.json'
    if expected.exists():
        expected.write_text(build_trajectory(directory).model_dump_json(indent=2) + '\n')
        print('repinned', expected)
"
```

Then re-run the golden suite and read the diff: every change must be an added `summary`, `outcome`, `validator_state`, `reason` or `key_results` field. A changed turn id, count, warning or ref is a regression, not a re-pin.

- [ ] **Step 8: Verify batch and follow still agree**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_golden.py tests/test_trajectory_builder.py tests/test_trajectory_api.py tests/test_trajectory_cli.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/sag/trajectory tests/test_trajectory_reducer.py tests/fixtures/trajectory
git commit -m "feat(trajectory): the reducer fills in call, result and phase summaries"
```

---

### Task 4: The turn stream renderer

**Files:**
- Create: `src/sag/console/turn_stream.py`
- Test: `tests/test_turn_stream.py`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `TurnStreamRenderer(write: Callable[[str], None], *, width: int = 100, tty: bool = False)` with methods `feed(raw_line: str) -> None` and `close() -> None`; module constant `TOOL_WIDTH = 9`.

The renderer owns a `TrajectoryReducer`. `feed` is given one raw control-event JSON line; it folds it and writes whatever new text that line produced. Nothing is buffered across a `close()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_turn_stream.py`:

```python
"""One readable line per turn, from the ledger, never from a log."""

import json

from sag.console.turn_stream import TurnStreamRenderer


class _Sink:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def __call__(self, text: str) -> None:
        self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line.strip()]


def _event(sequence: int, kind: str, payload: dict) -> str:
    return json.dumps(
        {
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
            "source": None,
            "timestamp": f"2026-09-15T01:00:{sequence:02d}Z",
            "event_id": f"control-{sequence:06d}",
            "run_id": "run-1",
        }
    )


def _envelope(sequence: int, tool: str, params: dict, envelope_id: str) -> str:
    return _event(
        sequence,
        "action_envelope",
        {
            "envelope_id": envelope_id,
            "tool_call_id": f"call-{sequence}",
            "tool": tool,
            "exact_params": params,
            "envelope_sha256": "a" * 64,
        },
    )


def _result(sequence: int, tool: str, envelope_id: str, result: dict) -> str:
    return _event(
        sequence,
        "tool_result",
        {
            "envelope_id": envelope_id,
            "execution_id": f"x{sequence}",
            "tool": tool,
            "params": {},
            "scope": "environment",
            "result": result,
        },
    )


def _build_ok() -> dict:
    return {
        "operation_outcome": "success",
        "invocation_status": "completed",
        "facts": {"executed": 994, "failed": 0, "skipped": 61, "errors": 0},
        "metadata": {"analysis": {"exit_code": 0, "artifacts_created": ["a.jar", "b.jar"]}},
    }


def test_one_turn_is_one_line_with_tool_summary_and_outcome():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn clean verify", "action": "verify"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    line = sink.lines[-1]
    assert line.lstrip().startswith("#1")
    assert "build" in line
    assert "verify mvn clean verify" in line
    assert "exit 0 · 994 tests" in line


def test_a_phase_opens_a_band_and_a_transition_closes_it():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "project", {"action": "clone", "repo_url": "x/y"}, "e1"))
    stream.feed(_result(2, "project", "e1", {"operation_outcome": "success"}))
    stream.feed(
        _event(
            3,
            "loop_decision",
            {
                "event": {"tool_name": "project", "phase": "provision", "iteration": 1},
                "expected_decision": "continue",
                "expected_reason_code": "ok",
            },
        )
    )
    stream.feed(
        _event(
            4,
            "phase_transition",
            {
                "expected_kind": "advance",
                "expected_target": "analyze",
                "expected_reason_code": "workspace_ready",
            },
        )
    )
    stream.close()
    assert any(line.startswith("▸ provision") for line in sink.lines)
    assert any(line.startswith("✓ provision advanced") for line in sink.lines)


def test_a_blocked_phase_says_why():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "phase_transition",
            {
                "expected_kind": "repair",
                "expected_target": "build",
                "expected_reason_code": "maven_version_below_minimum",
            },
        )
    )
    stream.close()
    assert any("build repair" in line and "maven_version_below_minimum" in line for line in sink.lines)


def test_a_controller_turn_is_marked_as_the_engine():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "forced_action",
            {
                "envelope_id": "e1",
                "tool": "phase",
                "phase": "report",
                "exact_params": {"signal": "done", "phase": "report"},
                "policy": "p",
                "trigger": "t",
                "reason_code": "r",
            },
        )
    )
    stream.close()
    assert any("engine" in line for line in sink.lines)


def test_the_outcome_lands_on_the_same_line_when_nothing_intervened():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    assert len([line for line in sink.lines if line.lstrip().startswith("#1")]) == 1
    assert "↳" not in sink.text


def test_the_outcome_gets_its_own_line_when_something_was_printed_between():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.feed(
        _event(
            2,
            "job_barrier_wait",
            {
                "job_id": "c523e63040aa",
                "obligation_ref": "o1",
                "waits": 1,
                "waited_seconds": 300.0,
                "remaining_seconds": 900.0,
                "process_state": "running",
                "log_size": 1_258_291,
                "progressing": True,
            },
        )
    )
    stream.feed(_result(3, "build", "e1", _build_ok()))
    stream.close()
    assert "still running" in sink.text
    assert "↳ exit 0" in sink.text


def test_job_progress_is_throttled_to_once_a_minute_per_job():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100, clock=iter([0.0, 10.0, 70.0]).__next__)
    payload = {
        "job_id": "j1",
        "obligation_ref": "o1",
        "waits": 1,
        "waited_seconds": 10.0,
        "remaining_seconds": 10.0,
        "process_state": "running",
        "progressing": True,
    }
    for sequence in (1, 2, 3):
        stream.feed(_event(sequence, "job_barrier_wait", dict(payload)))
    stream.close()
    assert sink.text.count("still running") == 2


def test_a_ledger_hole_is_stated_once():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.close()
    warnings = [line for line in sink.lines if line.lstrip().startswith("!")]
    assert len(warnings) == 1
    assert "missing_tool_result" in warnings[0]


def test_no_line_exceeds_the_configured_width():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=80)
    stream.feed(
        _envelope(1, "bash", {"command": "echo " + "x" * 300}, "e1")
    )
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "failed", "error_code": "E" * 90}))
    stream.close()
    for line in sink.lines:
        assert len(line) <= 80, line


def test_the_stream_never_reads_a_log_line():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed("2026-09-14 21:06:09 | INFO | Executing command in container: ls")
    stream.close()
    assert sink.lines == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_turn_stream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.console.turn_stream'`.

- [ ] **Step 3: Write the renderer**

Create `src/sag/console/turn_stream.py`:

```python
"""The run, as it happens, one line per turn.

The input is the control ledger and nothing else: this renderer folds raw
event lines through the same `TrajectoryReducer` the Workbench timeline reads,
so the terminal and the browser cannot describe the same run differently.

A turn's dispatch line is written without a newline and completed in place when
the turn seals. If anything else reached the terminal in between — a job
progress note, a ledger warning — the outcome takes a fresh indented line
instead, so no outcome is ever lost to an interleaving.
"""

from __future__ import annotations

import time
from typing import Callable

from sag.trajectory.reducer import TrajectoryReducer
from sag.trajectory.schema import Turn

TOOL_WIDTH = 9
_ID_WIDTH = 5
_SUMMARY_WIDTH = 44
_DURATION_WIDTH = 8
_MIN_WIDTH = 60
_JOB_NOTE_INTERVAL_SECONDS = 60.0
_ENGINE_ACTOR = "⚙ engine"

_OUTCOME_STYLE = {
    "ok": "green",
    "failed": "red",
    "refused": "yellow",
    "cancelled": "yellow",
    "pending": "cyan",
}

_TRANSITION_GLYPH = {
    "advance": "✓",
    "evidence_close": "✓",
    "report": "✓",
    "flow_close": "✓",
    "repair": "→",
}


def _duration(turn: Turn) -> str | None:
    from sag.trajectory.reducer import _elapsed  # one implementation of turn timing

    seconds = _elapsed(turn.t0, turn.t1) if turn.t0 and turn.t1 else None
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _clip(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


class TurnStreamRenderer:
    """Folds control-event lines into terminal lines. Writes; never reads files."""

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        width: int = 100,
        tty: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._write = write
        self._width = max(_MIN_WIDTH, width)
        self._tty = tty
        self._clock = clock or time.monotonic
        self._reducer = TrajectoryReducer()
        self._phase: str | None = None
        self._printed: set[int] = set()
        self._open_turn: int | None = None
        self._line_open = False
        self._warned: set[tuple[str, int | None]] = set()
        self._job_notes: dict[str, float] = {}

    # -- writing -------------------------------------------------------

    def _emit(self, text: str) -> None:
        self._close_line()
        self._write(_clip(text, self._width) + "\n")

    def _open_line(self, text: str) -> None:
        self._close_line()
        self._write(_clip(text, self._width))
        self._line_open = True

    def _close_line(self) -> None:
        if self._line_open:
            self._write("\n")
            self._line_open = False

    def _style(self, text: str, style: str | None) -> str:
        if not style or not self._tty:
            return text
        return f"[{style}]{text}[/{style}]"

    # -- turns ---------------------------------------------------------

    def _dispatch_line(self, turn: Turn) -> str:
        actor = _ENGINE_ACTOR if turn.actor == "controller" else (
            turn.call.tool if turn.call else "—"
        )
        summary = (turn.call.summary if turn.call else None) or (
            "no call" if turn.call is None else turn.call.tool
        )
        return (
            f"  {('#' + str(turn.turn_id)):<{_ID_WIDTH}}"
            f"{_clip(actor, TOOL_WIDTH):<{TOOL_WIDTH + 1}}"
            f"{_clip(summary, _SUMMARY_WIDTH):<{_SUMMARY_WIDTH + 1}}"
        )

    def _outcome_text(self, turn: Turn) -> str | None:
        observation = turn.observation
        if observation is None or observation.outcome is None:
            return None
        parts = [self._style(observation.outcome, _OUTCOME_STYLE.get(observation.outcome))]
        if observation.summary:
            parts.append(observation.summary)
        gate = turn.gate
        if gate is not None:
            parts.append(f"gate {gate.word}")
        return " · ".join(parts)

    def _render_turn(self, turn: Turn) -> None:
        if turn.phase and turn.phase != self._phase:
            self._phase = turn.phase
            self._emit(f"▸ {turn.phase}")
        if turn.turn_id not in self._printed:
            duration = _duration(turn) or ""
            head = self._dispatch_line(turn) + f"{duration:>{_DURATION_WIDTH}}  "
            self._printed.add(turn.turn_id)
            outcome = self._outcome_text(turn)
            if outcome is None:
                self._open_line(head)
                self._open_turn = turn.turn_id
            else:
                self._emit(head + outcome)
                self._open_turn = None
            return
        outcome = self._outcome_text(turn)
        if outcome is None:
            return
        if self._open_turn == turn.turn_id and self._line_open:
            self._write(_clip(outcome, self._width))
            self._close_line()
            self._open_turn = None
        else:
            self._emit(f"      ↳ {outcome}")

    # -- events --------------------------------------------------------

    def _note_transition(self, payload: dict) -> None:
        kind = str(payload.get("expected_kind") or "")
        target = payload.get("expected_target")
        reason = payload.get("expected_reason_code")
        glyph = _TRANSITION_GLYPH.get(kind, "·")
        name = self._phase or target or "phase"
        if kind == "repair":
            text = f"{glyph} {target or name} repair"
        elif kind == "advance":
            text = f"{glyph} {name} advanced"
        else:
            text = f"{glyph} {name} {kind}"
        if reason:
            text = f"{text} · {reason}"
        self._emit(text)
        if kind == "advance" and isinstance(target, str):
            self._phase = None

    def _note_job(self, payload: dict) -> None:
        job = str(payload.get("job_id") or "")
        now = self._clock()
        last = self._job_notes.get(job)
        if last is not None and now - last < _JOB_NOTE_INTERVAL_SECONDS:
            return
        self._job_notes[job] = now
        waited = payload.get("waited_seconds")
        parts = ["still running"]
        if isinstance(waited, (int, float)):
            parts.append(f"{int(waited) // 60}m{int(waited) % 60:02d}s")
        size = payload.get("log_size")
        if isinstance(size, int) and size > 0:
            parts.append(f"log {size / 1_048_576:.1f} MB")
        if payload.get("progressing") is True:
            parts.append("progressing")
        self._emit("      … " + " · ".join(parts))

    def _note_warnings(self, warnings) -> None:
        for warning in warnings:
            key = (warning.code, warning.turn_id)
            if key in self._warned:
                continue
            self._warned.add(key)
            self._emit(f"  ! {warning.code}: {warning.detail}")

    def feed(self, raw_line: str) -> None:
        """Fold one control-event line and write whatever it produced."""

        delta = self._reducer.feed(raw_line)
        payload = self._payload(raw_line)
        if payload is not None:
            kind, body = payload
            if kind == "phase_transition":
                self._note_transition(body)
            elif kind == "job_barrier_wait":
                self._note_job(body)
            elif kind == "job_live_at_close":
                self._emit(f"      ! job {body.get('job_id')} still live at close")
        for turn in delta.turns:
            self._render_turn(turn)
        self._note_warnings(delta.warnings)

    @staticmethod
    def _payload(raw_line: str) -> tuple[str, dict] | None:
        import json

        try:
            event = json.loads(raw_line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("kind")
        body = event.get("payload")
        if not isinstance(kind, str) or not isinstance(body, dict):
            return None
        return kind, body

    def close(self) -> None:
        """Flush the open line and state any hole the ledger left."""

        self._close_line()
        snapshot = self._reducer.snapshot()
        self._note_warnings(snapshot.warnings)
        self._close_line()


__all__ = ["TurnStreamRenderer", "TOOL_WIDTH"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_turn_stream.py -v`
Expected: PASS, 10 tests. If `_elapsed` is private and importing it feels wrong, promote it in `reducer.py` by adding `elapsed = _elapsed` at module scope and import that instead — one implementation of turn timing either way.

- [ ] **Step 5: Commit**

```bash
git add src/sag/console/turn_stream.py tests/test_turn_stream.py
git commit -m "feat(cli): render the run as one line per turn, from the control ledger"
```

---

### Task 5: Attach the stream to the run

**Files:**
- Modify: `src/sag/agent/control_events.py` (`ControlEventSink.__init__` and `emit`), `src/sag/config/logger.py:190-210` (`get_control_event_sink`), `src/sag/main.py` (`project` and `run` commands)
- Test: `tests/test_control_event_sink_observers.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `ControlEventSink(..., observers: tuple[Callable[[str], None], ...] = ())` and `ControlEventSink.add_observer(callable) -> None`; `SessionLogger.get_control_event_sink(..., observers=...)` passes it through.

An observer receives the same JSON line the mirror receives, after the host file is written and fsynced. An observer that raises is logged and dropped, exactly as the mirror is — the host ledger's append-only guarantee outranks any renderer.

- [ ] **Step 1: Write the failing test**

Create `tests/test_control_event_sink_observers.py`:

```python
"""A second reader may watch the ledger without becoming part of it."""

import json

import pytest

from sag.agent.control_events import ControlEventSink


def _sink(tmp_path, **kwargs) -> ControlEventSink:
    return ControlEventSink(tmp_path / "control_events.jsonl", run_id="run-1", **kwargs)


def test_an_observer_sees_every_emitted_line(tmp_path):
    seen: list[str] = []
    sink = _sink(tmp_path, observers=(seen.append,))
    sink.emit("evidence_close", {"reason": "test_terminated"})
    assert len(seen) == 1
    assert json.loads(seen[0])["kind"] == "evidence_close"


def test_an_observer_added_later_sees_later_lines(tmp_path):
    seen: list[str] = []
    sink = _sink(tmp_path)
    sink.emit("evidence_close", {"reason": "aborted"})
    sink.add_observer(seen.append)
    sink.emit("evidence_close", {"reason": "cancelled"})
    assert len(seen) == 1
    assert json.loads(seen[0])["payload"]["reason"] == "cancelled"


def test_a_failing_observer_never_breaks_the_ledger(tmp_path):
    def explode(_: str) -> None:
        raise RuntimeError("renderer is down")

    sink = _sink(tmp_path, observers=(explode,))
    event = sink.emit("evidence_close", {"reason": "aborted"})
    assert event.sequence == 1
    written = (tmp_path / "control_events.jsonl").read_text().strip().splitlines()
    assert len(written) == 1


def test_observers_run_after_the_line_is_on_disk(tmp_path):
    path = tmp_path / "control_events.jsonl"
    observed: list[int] = []

    def count_lines(_: str) -> None:
        observed.append(len(path.read_text().strip().splitlines()))

    sink = _sink(tmp_path, observers=(count_lines,))
    sink.emit("evidence_close", {"reason": "aborted"})
    assert observed == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_control_event_sink_observers.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'observers'`.

- [ ] **Step 3: Add observers to the sink**

In `src/sag/agent/control_events.py`, add the parameter to `ControlEventSink.__init__`:

```python
        observers: tuple[Callable[[str], None], ...] = (),
```

and store `self._observers = list(observers)`. Add:

```python
    def add_observer(self, observer: Callable[[str], None]) -> None:
        """Watch every line from here on. Observers never gate the append."""

        with self._lock:
            self._observers.append(observer)
```

In `emit`, after the existing mirror block, add:

```python
            for observer in tuple(self._observers):
                try:
                    observer(line)
                except Exception as exc:  # a renderer may fail; the ledger may not
                    logging.getLogger(__name__).warning(
                        "control-event observer failed at sequence %s: %s", sequence, exc
                    )
```

In `src/sag/config/logger.py`, add `observers: tuple = ()` to `get_control_event_sink`'s signature and pass it to the constructor.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_control_event_sink_observers.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Attach the renderer in the CLI**

In `src/sag/main.py`, add a helper beside `_start_agent_session_logging`:

```python
def _attach_turn_stream(config: Config) -> Optional[TurnStreamRenderer]:
    """Show the run as turns. Returns the renderer so the caller can close it."""

    session_logger = get_session_logger()
    if session_logger is None:
        return None
    renderer = TurnStreamRenderer(
        lambda text: console.file.write(text),
        width=console.width,
        tty=console.is_terminal,
    )
    session_logger.get_control_event_sink().add_observer(renderer.feed)
    return renderer
```

Import `from sag.console.turn_stream import TurnStreamRenderer`.

Call it right after `_start_agent_session_logging(config)` in both `project` and `run`, and call `renderer.close()` in a `finally` around the agent call so a crashing run still flushes its last line.

- [ ] **Step 6: Verify against a real recorded ledger**

Run:

```bash
PYTHONPATH=. uv run python -c "
import sys
from pathlib import Path
from sag.console.turn_stream import TurnStreamRenderer
ledger = sorted(Path('logs').glob('*/runs/*/container-evidence/.setup_agent/control_events.jsonl'))[0]
stream = TurnStreamRenderer(sys.stdout.write, width=100)
for line in ledger.open():
    stream.feed(line)
stream.close()
"
```

Expected: phase bands and one line per turn, similar to the spec's §4.2 example. Read it: every tool call in that run should appear exactly once.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/control_events.py src/sag/config/logger.py src/sag/main.py tests/test_control_event_sink_observers.py
git commit -m "feat(cli): show the turn stream live by observing the control ledger"
```

---

### Task 6: Quiet the console logger

**Files:**
- Modify: `src/sag/config/logger.py:50-125` (`_setup_loggers`), `:127-157` (formats and filter), `:255-300` (module-level twins), `:331-417` (`suppress_console_logging`), `src/sag/agent/react_engine.py:8716-8717`, `:9581`
- Test: `tests/test_logger_console_defaults.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SessionLogger._setup_loggers` delegates its console sink to the module-level `setup_console_logging`; `suppress_console_logging` is deleted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_logger_console_defaults.py`:

```python
"""The console shows turns; the files keep everything."""

import inspect

from sag.config import logger as logger_module


def test_there_is_one_console_implementation():
    source = inspect.getsource(logger_module.SessionLogger._setup_loggers)
    assert "sys.stderr" not in source, (
        "the session logger must delegate its console sink, not add a second one"
    )
    assert "setup_console_logging" in source


def test_the_console_default_is_warning():
    class _Config:
        verbose = False
        log_level = type("L", (), {"value": "INFO"})()

    assert logger_module.console_level(_Config()) == "WARNING"


def test_verbose_restores_debug():
    class _Config:
        verbose = True
        log_level = type("L", (), {"value": "INFO"})()

    assert logger_module.console_level(_Config()) == "DEBUG"


def test_an_explicit_log_level_still_wins():
    class _Config:
        verbose = False
        log_level = type("L", (), {"value": "ERROR"})()

    assert logger_module.console_level(_Config()) == "ERROR"


def test_suppress_console_logging_is_gone():
    assert not hasattr(logger_module, "suppress_console_logging")


def test_the_action_line_is_logged_once():
    from sag.agent import react_engine

    source = inspect.getsource(react_engine)
    assert source.count('"🔧 ACTION: ') <= 1, "the action line was reaching the console twice"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_logger_console_defaults.py -v`
Expected: FAIL — `console_level` does not exist.

- [ ] **Step 3: Unify the console sink**

In `src/sag/config/logger.py`, add:

```python
# The console belongs to the turn stream now. Loguru keeps the files; on screen
# it speaks only when something went wrong, unless the user asked for more.
DEFAULT_CONSOLE_LEVEL = "WARNING"


def console_level(config) -> str:
    """The one rule for how loud the console is."""

    if config.verbose:
        return "DEBUG"
    level = str(getattr(config.log_level, "value", config.log_level) or "").upper()
    if level in {"", "DEBUG", "INFO"}:
        return DEFAULT_CONSOLE_LEVEL
    return level
```

Rewrite `setup_console_logging` to use it and drop the `ui_mode` early return and the `quiet_default` parameter:

```python
def setup_console_logging(config) -> None:
    """Configure the console sink. One implementation, used by both callers."""

    logger.add(
        sys.stderr,
        level=console_level(config),
        format=_get_console_format(config),
        colorize=True,
        filter=lambda record: _console_filter(config, record),
    )
```

In `SessionLogger._setup_loggers`, replace the whole `if not self.config.ui_mode:` console block with `setup_console_logging(self.config)` and delete `SessionLogger._get_console_format` and `SessionLogger._console_filter` in favour of the module-level twins.

Delete `suppress_console_logging` entirely. Remove `ui_mode` from the `Config` model in `src/sag/config/settings.py` and from `set_config` in `src/sag/config/__init__.py`, plus every reference (`grep -rn "ui_mode" src tests`).

- [ ] **Step 4: Stop double-logging the action line**

In `src/sag/agent/react_engine.py`, delete line 8717 (the second `🔧 ACTION:` emit) and change the remaining one at 8716 to bind `AGENT_TRACE` so it lands in `agent_execution.log` only. Change the `👁️ OBSERVATION:` emit at 9581 from `info` to `debug` for the same reason: the full observation text belongs in a file, not on a terminal.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_logger_console_defaults.py -v`
Expected: PASS, 6 tests.

Run: `PYTHONPATH=.:tests uv run pytest -q -k "logger or config" `
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sag/config src/sag/agent/react_engine.py tests/test_logger_console_defaults.py
git commit -m "feat(cli): the console speaks in turns; loguru keeps the detail in files"
```

---

### Task 7: Retire `--ui`

**Files:**
- Delete: `src/sag/ui/` (all 6 files), `tests/test_ui_state_aggregator.py`, `tests/test_ui_manager_observability.py`, `tests/test_ui_diagnosis.py`, `tests/test_ui_state_models.py`
- Modify: `src/sag/main.py` (the `--ui` options on `cli`, `project`, `run` and their guards at `:419-427`, `:534`, `:610-612`, `:737`), `src/sag/agent/agent.py` and `src/sag/agent/react_engine.py` (UI event emission)
- Test: `tests/test_cli_project_exit_codes.py`

**Interfaces:**
- Consumes: Task 6 (the `ui_mode` config flag is already gone).
- Produces: no `--ui` flag anywhere; `sag.ui` no longer importable.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_project_exit_codes.py`:

```python
def test_the_ui_flag_is_gone():
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(cli, ["project", "--help"])
    assert "--ui" not in result.output


def test_the_ui_package_is_gone():
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sag.ui")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_cli_project_exit_codes.py -v -k "ui_flag or ui_package"`
Expected: FAIL — the flag is in the help text and the module imports.

- [ ] **Step 3: Delete the package and its tests**

```bash
git rm -r src/sag/ui tests/test_ui_state_aggregator.py tests/test_ui_manager_observability.py tests/test_ui_diagnosis.py tests/test_ui_state_models.py
```

- [ ] **Step 4: Remove the flag and its plumbing**

In `src/sag/main.py`: delete the `--ui` option from `cli`, `project` and `run`; delete the mutual-exclusion guard at `:419-427` and its twin at `:610`; delete the `if not config.ui_mode:` guards (the bodies stay, unindented); delete the `UIManager` import and every use.

Then `grep -rn "from sag.ui\|import sag.ui\|UIManager\|EventType\|PhaseType\|ui_manager\|state_aggregator" src tests` and remove each hit. In `react_engine.py` and `agent.py` this means deleting the event-emission calls that fed the UI (`AGENT_ACTION`, `AGENT_OBSERVATION`, `REPORT_GENERATED`, `STEP_*`, `PHASE_*`) — the ledger already records all of it, which is what the turn stream reads.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_cli_project_exit_codes.py -v`
Expected: PASS.

Run: `PYTHONPATH=.:tests uv run pytest -q`
Expected: PASS. Any remaining failure naming `sag.ui` is a leftover import to delete.

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "refactor(cli): retire the disconnected --ui mode"
```

---

### Task 8: `sag trajectory --format`

**Files:**
- Modify: `src/sag/main.py:1493-1545` (`trajectory`)
- Test: `tests/test_trajectory_cli.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `sag trajectory <dir> [--format table|json] [--follow] [--detail summary|full]`; `--format` defaults to `table`; `--detail` with `--format table` exits 2.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_trajectory_cli.py`:

```python
def test_table_is_the_default_format(kafka_session):
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(cli, ["trajectory", str(kafka_session)])
    assert result.exit_code == 0
    assert not result.output.lstrip().startswith("{")
    assert "▸ " in result.output
    assert result.output.splitlines()[0].startswith("kafka") or "run" in result.output.splitlines()[0]


def test_json_format_still_prints_one_document(kafka_session):
    import json

    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(cli, ["trajectory", str(kafka_session), "--format", "json"])
    assert result.exit_code == 0
    document = json.loads(result.output.strip())
    assert document["schema_version"] == 1


def test_detail_with_table_is_a_usage_error(kafka_session):
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(
        cli, ["trajectory", str(kafka_session), "--format", "table", "--detail", "full"]
    )
    assert result.exit_code == 2
    assert "--detail" in result.output
```

`kafka_session` is the existing fixture in that file pointing at `tests/fixtures/trajectory/kafka-d2r3`; if it is a plain path variable, use it directly instead of a fixture argument.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_cli.py -v -k "table or json_format or usage_error"`
Expected: FAIL — `--format` is not an option.

- [ ] **Step 3: Add the format option**

Replace the `trajectory` command body in `src/sag/main.py`:

```python
@cli.command()
@click.argument("session_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("table", "json")),
    default="table",
    help="table: one line per turn, as the run happened. json: the trajectory document.",
)
@click.option("--follow", is_flag=True, help="Tail a running session instead of replaying it")
@click.option(
    "--detail",
    type=click.Choice(DETAIL_TIERS),
    default=None,
    help="json only — summary: names, codes, timing, tokens. full: also the bytes each ref names",
)
def trajectory(session_dir, output_format, follow, detail):
    """Print a session's turns, as a table or as the trajectory document."""

    if output_format == "table" and detail is not None:
        raise click.UsageError("--detail applies to --format json; the table has one detail tier")
    try:
        if output_format == "json":
            _trajectory_json(session_dir, follow=follow, detail=detail or DETAIL_TIERS[0])
        else:
            _trajectory_table(session_dir, follow=follow)
    except (OSError, ValueError) as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        return
```

Move the existing JSON body into `_trajectory_json(session_dir, *, follow, detail)` unchanged, and add:

```python
def _trajectory_table(session_dir: Path, *, follow: bool) -> None:
    """Replay or tail the session as the turn stream the terminal shows live."""

    document = build_trajectory(session_dir)
    session = document.session
    header = " · ".join(
        part
        for part in (
            session.project,
            session.run_id,
            f"verdict {session.verdict}" if session.verdict else None,
            f"{len(document.turns):,} turns" if document.turns else None,
        )
        if part
    )
    console.print(header)
    stream = TurnStreamRenderer(
        lambda text: console.file.write(text), width=console.width, tty=console.is_terminal
    )
    ledger = Path(session_dir) / "control_events.jsonl"
    if ledger.exists():
        with ledger.open("r", encoding="utf-8") as handle:
            for line in handle:
                stream.feed(line)
    stream.close()
    if follow:
        for delta in follow_trajectory(session_dir):
            for turn in delta.turns:
                stream._render_turn(turn)  # same renderer, same rows
```

If reaching into `_render_turn` is unacceptable to the reviewer, add a public `TurnStreamRenderer.render_turn(turn)` in Task 4's module that `_render_turn` delegates to, and call that.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_trajectory_cli.py -v`
Expected: PASS. Existing tests that assert stdout is exactly one JSON line must gain `--format json`.

- [ ] **Step 5: Update the README**

In `README.md`, change the `uv run sag trajectory logs/session_X --detail full` example to `uv run sag trajectory logs/session_X` and add one line: piping to `jq` needs `--format json`.

- [ ] **Step 6: Commit**

```bash
git add src/sag/main.py tests/test_trajectory_cli.py README.md
git commit -m "feat(cli): sag trajectory reads as turns by default, json on request"
```

---

### Task 9: `sag result`

**Files:**
- Modify: `src/sag/main.py` (new command)
- Test: `tests/test_result_command.py`

**Interfaces:**
- Consumes: phase 1's `build_result_card` and `render_result_block`; Task 4's reducer-derived counts.
- Produces: `sag result <container|session_dir> [--json]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_result_command.py`:

```python
"""`sag result` reads a finished run back, live or recorded."""

import json

from click.testing import CliRunner

from sag.main import cli

from result_card_fakes import snapshot_dict


def _recorded(tmp_path, **overrides):
    directory = tmp_path / "session_1"
    (directory / ".setup_agent").mkdir(parents=True)
    (directory / ".setup_agent" / "verdict.json").write_text(
        json.dumps(snapshot_dict(**overrides)), encoding="utf-8"
    )
    return directory


def test_prints_the_block_for_a_recorded_session(tmp_path):
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path))])
    assert result.exit_code == 0
    assert "Required task" in result.output
    assert "Official CI" in result.output


def test_json_prints_the_card(tmp_path):
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path)), "--json"])
    assert result.exit_code == 0
    card = json.loads(result.output)
    assert card["schema_version"] == 1
    assert [row["key"] for row in card["rows"]][0] == "setup"


def test_finds_a_verdict_at_the_directory_root(tmp_path):
    directory = tmp_path / "flat"
    directory.mkdir()
    (directory / "verdict.json").write_text(json.dumps(snapshot_dict()), encoding="utf-8")
    assert CliRunner().invoke(cli, ["result", str(directory)]).exit_code == 0


def test_finds_a_verdict_in_a_campaign_archive(tmp_path):
    directory = tmp_path / "archived"
    (directory / "container-evidence" / ".setup_agent").mkdir(parents=True)
    (directory / "container-evidence" / ".setup_agent" / "verdict.json").write_text(
        json.dumps(snapshot_dict()), encoding="utf-8"
    )
    assert CliRunner().invoke(cli, ["result", str(directory)]).exit_code == 0


def test_a_partial_run_exits_zero_because_reading_is_not_running(tmp_path):
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path, verdict="partial"))])
    assert result.exit_code == 0
    assert "partial" in result.output


def test_nothing_readable_exits_one(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = CliRunner().invoke(cli, ["result", str(empty)])
    assert result.exit_code == 1
    assert "no result" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_command.py -v`
Expected: FAIL with `Error: No such command 'result'`.

- [ ] **Step 3: Write the command**

Add to `src/sag/main.py`:

```python
_VERDICT_CANDIDATES = (
    Path(".setup_agent") / "verdict.json",
    Path("verdict.json"),
    Path("container-evidence") / ".setup_agent" / "verdict.json",
)


def _recorded_verdict(directory: Path) -> dict | None:
    """Read a recorded run's verdict from the three layouts we write."""

    for candidate in _VERDICT_CANDIDATES:
        path = directory / candidate
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _recorded_sibling(directory: Path, name: str) -> dict | None:
    for candidate in _VERDICT_CANDIDATES:
        path = directory / candidate.parent / name
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                return payload
    return None


@cli.command()
@click.argument("target")
@click.option("--json", "as_json", is_flag=True, help="Print the result card as JSON")
def result(target, as_json):
    """Print the result of a finished run, from a container or a session directory."""

    directory = Path(target)
    payload = _recorded_verdict(directory) if directory.is_dir() else None
    container = None
    session_dir = str(directory) if payload is not None else None
    module_metrics = report_metrics = run_pin = None

    if payload is None and not directory.is_dir():
        try:
            orchestrator = DockerOrchestrator(target)
            snapshot = read_live_verdict_snapshot(orchestrator)
        except Exception:
            snapshot = None
        if snapshot is None or snapshot.verdict == "unknown":
            console.print(f"[red]❌ no result could be read from {target}[/red]")
            sys.exit(1)
        payload = snapshot.model_dump(mode="json")
        container = target
        module_metrics = _read_module_metrics_for_cli(orchestrator)
        report_metrics = _read_metrics_v2_for_cli(orchestrator)
    elif payload is not None:
        module_metrics = _recorded_sibling(directory, "module_metrics.json")
        report_metrics = _recorded_sibling(directory, "report_metrics.json")
        pin = directory / "run-pin.json"
        if pin.is_file():
            try:
                run_pin = json.loads(pin.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                run_pin = None

    if payload is None:
        console.print(f"[red]❌ no result could be read from {target}[/red]")
        sys.exit(1)

    turns = tool_calls = tool_failures = None
    session = None
    ledger = directory / "control_events.jsonl"
    if ledger.is_file():
        document = build_trajectory(directory)
        turns = len(document.turns)
        tool_calls = sum(1 for turn in document.turns if turn.call is not None)
        tool_failures = sum(
            1
            for turn in document.turns
            if turn.observation is not None and turn.observation.outcome == "failed"
        )
        session = document.session.model_dump(mode="json")

    card = build_result_card(
        payload,
        module_metrics=module_metrics,
        report_metrics=report_metrics,
        run_pin=run_pin,
        trajectory_session=session,
        turn_count=turns,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        container=container,
        session_dir=session_dir,
    )
    if as_json:
        click.echo(json.dumps(card.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    console.print(render_result_block(card, width=console.width))
```

Note the `result` command reads only; it never exits non-zero for a partial run, because reading a result is not running one.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_result_command.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sag/main.py tests/test_result_command.py
git commit -m "feat(cli): sag result reads a finished run back from a container or a directory"
```

---

### Task 10: `sag list` and `sag run`'s exit code

**Files:**
- Modify: `src/sag/main.py:466-517` (`list`), `:739-836` (`run`), `src/sag/agent/agent.py` (`_provide_task_summary`)
- Test: `tests/test_list_command.py`, `tests/test_cli_project_exit_codes.py`

**Interfaces:**
- Consumes: phase 1's card; Task 9's helpers.
- Produces: `sag list` with the columns Project, Container, State, Setup, Required task, Tests, Updated; `sag run` exits 1 when the task is reported incomplete.

- [ ] **Step 1: Write the failing test**

Create `tests/test_list_command.py`:

```python
"""`sag list` and the Workbench rail agree about every workspace."""

from click.testing import CliRunner

from sag.main import cli


def test_columns_name_the_result_not_a_free_text_comment(monkeypatch):
    from sag.web.models import DashboardResponse, DockerSummary

    monkeypatch.setattr(
        "sag.web.read_model.ReadModelBuilder.dashboard",
        lambda self: DashboardResponse(docker=DockerSummary(status="connected"), workspaces=[]),
    )
    output = CliRunner().invoke(cli, ["list"]).output
    assert "Last Comment" not in output


def test_an_empty_dashboard_teaches_the_first_command(monkeypatch):
    from sag.web.models import DashboardResponse, DockerSummary

    monkeypatch.setattr(
        "sag.web.read_model.ReadModelBuilder.dashboard",
        lambda self: DashboardResponse(docker=DockerSummary(status="connected"), workspaces=[]),
    )
    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "sag project" in result.output
```

Add to `tests/test_cli_project_exit_codes.py`:

```python
def test_sag_run_exits_one_when_the_task_did_not_finish(monkeypatch):
    from click.testing import CliRunner

    from sag.main import cli

    monkeypatch.setattr("sag.agent.agent.SetupAgent.run_task", lambda *a, **k: False)
    monkeypatch.setattr(
        "sag.main.DockerOrchestrator", lambda *a, **k: _orchestrator_double()
    )
    result = CliRunner().invoke(cli, ["run", "sag-x", "--task", "do a thing"])
    assert result.exit_code == 1
```

`_orchestrator_double()` is whatever double the surrounding file already uses for a live container; reuse it rather than writing a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_list_command.py tests/test_cli_project_exit_codes.py -v -k "columns or teaches or exits_one"`
Expected: FAIL — `Last Comment` is still a column and `sag run` exits 0.

- [ ] **Step 3: Rewrite `sag list`**

Replace the body of `list` in `src/sag/main.py`:

```python
@cli.command()
def list():
    """List SAG workspaces and what each run produced."""

    from sag.web.read_model import ReadModelBuilder

    try:
        dashboard = ReadModelBuilder().dashboard()
    except Exception as exc:
        console.print(f"[red]❌ Failed to list projects: {exc}[/red]")
        return

    if not dashboard.workspaces:
        console.print("No SAG workspaces found.")
        console.print("Use 'sag project <repo_url>' to create one.")
        return

    table = Table(title="SAG Workspaces", show_header=True, header_style="bold")
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Container", style="blue", no_wrap=True)
    table.add_column("State")
    table.add_column("Setup")
    table.add_column("Required task")
    table.add_column("Tests")
    table.add_column("Updated", style="dim")

    for workspace in dashboard.workspaces:
        outcome = getattr(workspace, "result", None)
        task = getattr(outcome, "task", None) if outcome else None
        tests = getattr(outcome, "tests", None) if outcome else None
        task_text = f"{task.completed}/{task.required}" if task else "—"
        if tests and tests.executed:
            red = (tests.failed or 0) + (tests.errors or 0)
            tests_text = f"{tests.passed:,}/{tests.executed:,}"
            if red:
                tests_text = f"{tests_text} +{red} red"
        else:
            tests_text = "—"
        table.add_row(
            workspace.project or "—",
            workspace.id,
            workspace.status or "—",
            (getattr(outcome, "verdict", None) or "—") if outcome else "—",
            task_text,
            tests_text,
            workspace.updated or "—",
        )
    console.print(table)
```

`WorkspaceSummary.result` is added by phase 3 Task 2. Until then the `getattr` chain yields `"—"` in every result column, which is correct and testable — the columns exist and say nothing is known.

- [ ] **Step 4: Make `sag run` exit honestly**

In `src/sag/main.py`'s `run` command, replace the trailing `if not config.ui_mode:` block with:

```python
        if success:
            console.print("[green]Task completed.[/green]")
        else:
            console.print("[yellow]Task did not finish. Run another task to continue.[/yellow]")
            sys.exit(1)
```

In `src/sag/agent/agent.py`, delete the `Final TODO List Status` block from `_provide_task_summary` (it renders a retired task list) and replace `setup-agent connect` / `setup-agent continue` in that method with `sag shell {docker_name}` / `sag run {docker_name} --task "..."`.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_list_command.py tests/test_cli_project_exit_codes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sag/main.py src/sag/agent/agent.py tests/test_list_command.py tests/test_cli_project_exit_codes.py
git commit -m "feat(cli): sag list shows results, sag run exits on an unfinished task"
```

---

### Task 11: `sag inspect --turn` and the welcome panel

**Files:**
- Modify: `src/sag/main.py:450-463` (welcome panel), `:1423-1491` (`inspect`)
- Test: `tests/test_inspect_command.py`

**Interfaces:**
- Consumes: Tasks 1-4; `read_call_envelope` from `sag.trajectory.builder`.
- Produces: `sag inspect <target> --turn N [--session DIR]`; the welcome panel prints only for `project` and `run`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_inspect_command.py`:

```python
def test_inspect_does_not_print_the_welcome_panel(kafka_session):
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(cli, ["inspect", "x", "--session", str(kafka_session)])
    assert "Automated project setup with AI" not in result.output


def test_turn_shows_one_turns_call_and_result(kafka_session):
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(
        cli, ["inspect", "x", "--session", str(kafka_session), "--turn", "1"]
    )
    assert result.exit_code == 0
    assert "Turn 1" in result.output
    assert "Call" in result.output
    assert "Result" in result.output


def test_an_unknown_turn_names_the_range_that_exists(kafka_session):
    from click.testing import CliRunner

    from sag.main import cli

    result = CliRunner().invoke(
        cli, ["inspect", "x", "--session", str(kafka_session), "--turn", "9999"]
    )
    assert result.exit_code == 1
    assert "recorded turns" in result.output
```

Use the same session fixture path this file already uses for `--session` tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_inspect_command.py -v -k "welcome or turn"`
Expected: FAIL — no `--turn` option.

- [ ] **Step 3: Narrow the welcome panel**

In `src/sag/main.py:452`, change the skip list so the panel prints only for the two commands that start agent work:

```python
    if ctx.invoked_subcommand not in {"project", "run"}:
        return
```

(keeping whatever surrounding structure the function has — the change is from "skip these two" to "print for these two").

- [ ] **Step 4: Add `--turn`**

Add the option to `inspect` and a renderer:

```python
@click.option("--turn", "turn_id", default=None, type=int, help="Show one turn end to end")
```

```python
def _inspect_render_turn(session_dir: Path, turn_id: int) -> str:
    """One turn: what was asked, what came back, and what the gate decided."""

    document = build_trajectory(session_dir, detail="full")
    turn = next((item for item in document.turns if item.turn_id == turn_id), None)
    if turn is None:
        ids = [item.turn_id for item in document.turns]
        span = f"{min(ids)}..{max(ids)}" if ids else "none"
        raise _InspectError(f"No turn {turn_id} (recorded turns: {span})")

    lines = [f"=== Turn {turn.turn_id} ({turn.phase}, {turn.actor}) ==="]
    if turn.iteration is not None:
        lines.append(f"Iteration: {turn.iteration}")
    lines.append(f"Control events: {', '.join(str(seq) for seq in turn.control_seq)}")
    lines.append("")

    lines.append("Call:")
    if turn.call is None:
        lines.append("  this turn called no tool")
    else:
        lines.append(f"  tool: {turn.call.tool}")
        if turn.call.summary:
            lines.append(f"  summary: {turn.call.summary}")
        if turn.call.params_ref:
            envelope = read_call_envelope(session_dir, turn.call.params_ref)
            if envelope is not None:
                params = json.dumps(envelope.get("exact_params"), indent=2, sort_keys=True)
                lines.append(textwrap.indent(params, "  "))
    lines.append("")

    lines.append("Result:")
    observation = turn.observation
    if observation is None:
        lines.append("  no result is recorded for this turn")
    else:
        if observation.outcome:
            lines.append(f"  outcome: {observation.outcome}")
        if observation.summary:
            lines.append(f"  summary: {observation.summary}")
        if observation.error_code:
            lines.append(f"  error code: {observation.error_code}")
        for label, ref in (("model-visible", observation.ref), ("evidence", observation.evidence_ref)):
            if not ref:
                continue
            body = (document.outputs or {}).get(ref)
            lines.append(f"  {label} ref {ref}:")
            lines.append(textwrap.indent(body if body else "(bytes not in this session's store)", "    "))
    if turn.gate is not None:
        lines.extend(["", f"Gate: {turn.gate.word} ({turn.gate.decision_id})"])
    return "\n".join(lines)
```

Wire it into `inspect`'s dispatch before the `--iter` branch: when `turn_id` is not None, require `--session` (a container's ledger is reachable through its recorded session) and print `_inspect_render_turn`.

Import `read_call_envelope` from `sag.trajectory.builder` at the top of `main.py`.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=.:tests uv run pytest tests/test_inspect_command.py -v`
Expected: PASS.

- [ ] **Step 6: Run the whole suite**

Run: `PYTHONPATH=.:tests uv run pytest -q --ignore=tests/test_packaging_smoke.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/main.py tests/test_inspect_command.py
git commit -m "feat(cli): inspect one turn end to end, and stop greeting non-agent commands"
```

---

## Self-Review

**Spec coverage.** §3 schema additions → Task 1; derivation rules → Task 2; reducer wiring and golden re-pin → Task 3. §4.2 turn stream grammar → Task 4; live attachment → Task 5; logger defaults → Task 6. §4.3's command table: `--ui` removal → Task 7; `sag trajectory --format` → Task 8; `sag result` → Task 9; `sag list` and `sag run`'s exit code → Task 10; `inspect --turn` and the welcome panel → Task 11; the README note → Task 8. §4.1's block is phase 1's Task 9 and is only consumed here.

Two deliberate deviations from the spec, both widenings:

1. `sag result` also looks in `container-evidence/.setup_agent/verdict.json`, the layout the campaign driver archives into. The spec names two candidate paths; this adds the third real one at the cost of one tuple entry.
2. `sag trajectory --format table --detail X` exits 2 as a Click usage error. The spec says "combining it with table is a usage error (exit 2)"; this plan states the message.

**Placeholder scan.** No "TBD", no "handle edge cases", no "similar to Task N". Task 3 Step 7 and Task 8 Step 3 each carry a conditional ("if a test compares whole `Turn` objects", "if reaching into `_render_turn` is unacceptable") with the exact alternative spelled out, not a deferred decision. Task 10's `sag list` depends on a field phase 3 adds; the plan states what the column shows until then and why that is correct.

**Type consistency.** `call_summary(tool, params)`, `observation_outcome(result)`, `observation_summary(tool, result)` and `refusal_summary(payload)` are defined in Task 2 and called with those exact names and argument orders in Task 3. `TurnStreamRenderer(write, *, width, tty, clock)` matches between Task 4's definition and its uses in Tasks 5, 8 and the verification command in Task 5 Step 6. `ObservationOutcome`, `SUMMARY_MAX_CHARS` and `KEY_RESULTS_MAX_CHARS` come from Task 1 and are imported under those names in Tasks 2, 3 and 4. `build_result_card`'s keywords in Task 9 match phase 1 Task 6's signature exactly, including `trajectory_session`, `turn_count`, `tool_calls` and `tool_failures`.
