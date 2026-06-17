# Plan 3 — Build Time / Command / Artifact Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans or subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Backend (Python) — verify with pytest.

**Goal:** Populate the build facet's **Time**, **Command**, and **Artifact** (today the read model hardcodes `time: "—"` and omits command/artifact). Capture the build command's wall-clock duration, thread it through the command tracker → physical validator build evidence → `report_metrics` → read model, and surface the already-known command + primary artifact.

**Architecture:** No API/contract change — `BuildSummary` already declares `time`/`note`/`artifact`; this fills them. Five small layers: (1) `maven_tool` times the build command, (2) `CommandTracker` stores `duration`, (3) the physical validator's build-status evidence includes `build_time`/`build_command`/`artifact`, (4) `report_metrics.assemble_report_metrics` surfaces them in the `build` dict, (5) the web read model returns them instead of `"—"`. Artifact is essentially free (`artifact_samples[0]`); command needs only the tracked string; time needs the full duration chain.

**Tech Stack:** Python (sag backend), pytest. Frontend unchanged (Plan 2's KV rows light up automatically).

**Branch:** `feature/webui-workbench-redesign` (even with main).

**Project policy:** stage exact paths; force-add docs; no `Co-Authored-By` trailer. No `npm run build` / `static/`.

**Per-task verification:** `uv run pytest <test path> -q`.

---

## Data flow (today → target)

- `maven_tool.py` runs the Maven command (`result = orchestrator.execute_command(maven_cmd, …)`) then calls `command_tracker.track_build_command(command, tool, working_dir, exit_code, output)` — **no timing**.
- `CommandTracker.track_build_command` stores `{command, tool, working_dir, timestamp, exit_code, output_snippet, build_success}` — **no `duration`**.
- `physical_validator.validate_build_status` builds `build_status["evidence"]` (has `build_system`/`tool`/`class_files`/`jar_files`/`artifact_samples`…) and already calls `command_tracker.get_last_build_command()` — does **not** surface time/command/artifact.
- `report_metrics.assemble_report_metrics` build dict (report_metrics.py:44-51) maps evidence → `{state, system, tool, class_count, jar_count, module_output_count, artifact_samples, warnings}` — **no `time`/`note`/`artifact`**.
- `session_registry._build_payload_from_metrics` returns `time: "—"`, omits `note`/`artifact`.

---

## Task 1: CommandTracker stores build duration

**Files:** `src/sag/tools/internal/command_tracker.py`, `tests/test_command_tracker.py` (create if absent)

- [ ] **Step 1: Write the failing test**

In `tests/test_command_tracker.py` (new or appended):

```python
from sag.tools.internal.command_tracker import CommandTracker


def test_track_build_command_records_duration():
    tracker = CommandTracker()
    tracker.track_build_command(
        command="mvn -q clean package",
        tool="maven",
        working_dir="/workspace",
        exit_code=0,
        output="BUILD SUCCESS",
        duration=47.2,
    )
    last = tracker.get_last_build_command()
    assert last is not None
    assert last["duration"] == 47.2
    assert last["command"] == "mvn -q clean package"


def test_track_build_command_duration_optional():
    tracker = CommandTracker()
    tracker.track_build_command(command="mvn install", tool="maven", output="BUILD SUCCESS")
    last = tracker.get_last_build_command()
    assert last is not None
    assert last.get("duration") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_command_tracker.py -q` — FAIL (`track_build_command` has no `duration` param / entry lacks it).

- [ ] **Step 3: Implement**

In `src/sag/tools/internal/command_tracker.py`, add `duration: float | None = None` to `track_build_command`'s signature and `"duration": duration` to the `entry` dict. (Optionally mirror onto `track_test_command` for symmetry — not required for this plan.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_command_tracker.py -q` — PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/command_tracker.py tests/test_command_tracker.py
git commit -m "feat(backend): record build command duration in CommandTracker"
```

---

## Task 2: maven_tool times the build command

**Files:** `src/sag/tools/internal/maven_tool.py`

- [ ] **Step 1: Implement timing**

In `src/sag/tools/internal/maven_tool.py`, locate the line that executes the Maven command (`result = self.orchestrator.execute_command(maven_cmd, …)` — a few lines above the `track_build_command` call near line 419). Wrap it with a monotonic clock:

```python
import time  # at top of file if not already imported
…
_build_t0 = time.monotonic()
result = self.orchestrator.execute_command(maven_cmd, …)   # existing call, unchanged args
_build_elapsed = time.monotonic() - _build_t0
```

Then pass it to the build-command tracking call:

```python
                elif is_build_command:
                    self.command_tracker.track_build_command(
                        command=maven_cmd,
                        tool="maven",
                        working_dir=working_directory,
                        exit_code=result["exit_code"],
                        output=result["output"],
                        duration=_build_elapsed,
                    )
```

Only the build branch needs `duration`. Keep the test branch unchanged. If `time` is already imported, don't re-import.

- [ ] **Step 2: Type/run sanity**

Run: `uv run pytest tests/ -q -k "maven" ` (and the full backend suite later). No new unit test here (it's wall-clock + orchestrator I/O); Task 4/5 tests assert the surfaced value, and the live check confirms a real run.

- [ ] **Step 3: Commit**

```bash
git add src/sag/tools/internal/maven_tool.py
git commit -m "feat(backend): time the Maven build command for duration capture"
```

---

## Task 3: Validator surfaces build_time / build_command / artifact in evidence

**Files:** `src/sag/agent/physical_validator.py`, `tests/test_physical_validator*.py` (extend existing)

- [ ] **Step 1: Locate the build-status evidence assembly**

In `validate_build_status` (≈ line 1832), find where `evidence` (the dict carrying `build_system`/`tool`/`class_files`/`jar_files`/`artifact_samples`) is assembled, and the existing `command_tracker.get_last_build_command()` usage (≈ line 3275 region; the validator already reads the last build command).

- [ ] **Step 2: Add the three fields to evidence**

When a last build command is available, add to the build-status `evidence` dict:

```python
last_build = self.command_tracker.get_last_build_command() if self.command_tracker else None
if last_build:
    evidence["build_command"] = last_build.get("command")
    dur = last_build.get("duration")
    if isinstance(dur, (int, float)):
        evidence["build_time"] = _format_build_duration(dur)
# Primary artifact: first of the expected/known artifact samples.
samples = evidence.get("artifact_samples") or []
if samples:
    evidence.setdefault("artifact", samples[0])
```

Add a small formatter (module-level or static):

```python
def _format_build_duration(seconds: float) -> str:
    # "47.2s" under a minute; "3m 12s" otherwise.
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"
```

(Adapt key names to the actual evidence dict; the goal is that `build_evidence` carries `build_command`, `build_time`, `artifact`.)

- [ ] **Step 3: Test the formatter + evidence threading**

Add a focused test (in the existing physical-validator test module) for `_format_build_duration` (e.g. `47.2 -> "47.2s"`, `192 -> "3m 12s"`). For the evidence threading, if the validator is unit-testable with a stub `command_tracker` returning a `duration`, assert `evidence["build_time"]`/`build_command` appear; otherwise rely on Task 4/5 + the live run (note this in the commit).

Run: `uv run pytest tests/ -q -k "validator or build_duration"` — PASS.

- [ ] **Step 4: Commit**

```bash
git add src/sag/agent/physical_validator.py tests/
git commit -m "feat(backend): surface build time/command/artifact in build-status evidence"
```

---

## Task 4: report_metrics build dict carries time / note / artifact

**Files:** `src/sag/tools/report_metrics.py`, `tests/test_report_metrics.py` (extend)

- [ ] **Step 1: Write the failing test**

In `tests/test_report_metrics.py`:

```python
from sag.tools.report_metrics import assemble_report_metrics


def test_build_dict_surfaces_time_command_artifact():
    metrics = assemble_report_metrics(
        snapshot={"phases": {"build": True}, "status": {"overall": "success"}},
        build_evidence={
            "build_system": "maven", "tool": "Maven 3.9.6",
            "class_files": 115, "jar_files": 1,
            "build_time": "47.2s", "build_command": "clean package",
            "artifact": "target/commons-cli-1.6.0.jar",
            "artifact_samples": ["target/commons-cli-1.6.0.jar"],
        },
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-06-17T00:00:00",
    )
    build = metrics["build"]
    assert build["time"] == "47.2s"
    assert build["note"] == "clean package"
    assert build["artifact"] == "target/commons-cli-1.6.0.jar"
```

> Adapt the `snapshot`/`status` keys to whatever `assemble_report_metrics` reads for `state` (it reads `status.get("overall")`); the assertions on `time`/`note`/`artifact` are the point.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report_metrics.py -q` — FAIL (build dict has no `time`/`note`/`artifact`).

- [ ] **Step 3: Implement**

In `src/sag/tools/report_metrics.py`, in the `build` dict (≈ lines 44-51), add:

```python
        "time": build_evidence.get("build_time"),
        "note": build_evidence.get("build_command"),
        "artifact": build_evidence.get("artifact")
            or (_str_list(build_evidence.get("artifact_samples"), 1) or [None])[0],
```

(Keep the existing keys. `artifact` falls back to the first artifact sample when no explicit artifact.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_report_metrics.py -q` — PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/report_metrics.py tests/test_report_metrics.py
git commit -m "feat(backend): report_metrics build dict carries time/note/artifact"
```

---

## Task 5: Read model surfaces time / note / artifact

**Files:** `src/sag/web/session_registry.py`, `tests/test_web_session_registry.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_web_session_registry.py`:

```python
from sag.web.session_registry import _build_payload_from_metrics


def test_build_payload_surfaces_time_note_artifact():
    payload = _build_payload_from_metrics({
        "build": {
            "state": "success", "system": "maven", "tool": "Maven 3.9.6",
            "class_count": 115, "jar_count": 1,
            "time": "47.2s", "note": "clean package", "artifact": "target/x.jar",
        }
    })
    assert payload is not None
    assert payload["time"] == "47.2s"
    assert payload["note"] == "clean package"
    assert payload["artifact"] == "target/x.jar"


def test_build_payload_time_falls_back_to_dash():
    payload = _build_payload_from_metrics({"build": {"state": "success", "tool": "maven"}})
    assert payload is not None
    assert payload["time"] == "—"
    assert payload.get("note") in (None, "", "—")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web_session_registry.py -q -k build_payload` — FAIL (time hardcoded `"—"`; note/artifact absent).

- [ ] **Step 3: Implement**

In `src/sag/web/session_registry.py` `_build_payload_from_metrics`, replace the hardcoded time and add note/artifact:

```python
        "time": _text(build.get("time"), default="—") if build.get("time") else "—",
        "note": build.get("note"),
        "artifact": build.get("artifact"),
```

(Also add `"note"`/`"artifact"` to `_build_payload_from_report` if trivially available; otherwise leave the report fallback — the metrics path is primary.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web_session_registry.py -q` — PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/session_registry.py tests/test_web_session_registry.py
git commit -m "feat(web): surface build time/note/artifact from metrics (was hardcoded —)"
```

---

## Task 6: Full verification + live check

- [ ] **Step 1: Backend suite** — `uv run pytest tests/ -q` — all green.
- [ ] **Step 2: Live (real run)** — set up a real Maven project via `sag project …` so a build runs and times; then `sag ui` and open the Build facet: **Time** shows a real duration (e.g. `47.2s`), **Command** shows the build command, **Artifact** shows the built jar — the two-card layout (Plan 2) now fully populated. (Demo data won't have these; a real run is required to see Time.)
- [ ] **Step 3: Frontend regression** — `cd webui && npm test && npx tsc -p tsconfig.app.json --noEmit` — green (no frontend change, but confirm nothing drifted).

---

## Self-Review

**Spec coverage (B3):** duration capture (Tasks 1-2) → validator evidence (Task 3) → report_metrics (Task 4) → read model (Task 5). Command + artifact ride the same chain (command from the tracker, artifact from samples).

**Risk notes:**
- The integration layers (maven_tool timing, validator evidence) are wall-clock/I-O bound and hard to unit-test in isolation; their effect is asserted at the report_metrics + read-model boundaries (Tasks 4-5) and confirmed by the live run (Task 6). The pure pieces (`duration` storage, `_format_build_duration`, the two metrics surfacings) are unit-tested.
- Gradle: only `maven_tool` is timed here. A follow-up can time the Gradle build path the same way (its tracker call mirrors maven's); out of scope for this plan, which targets the reported Maven case.
- `time` fallback to `"—"` preserved when duration is absent (older runs, Gradle) — no regression.
