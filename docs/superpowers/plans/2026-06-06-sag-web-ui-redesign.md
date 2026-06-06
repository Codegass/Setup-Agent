# SAG Web UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first real `sag ui` web workbench from the approved redesign spec and the local UI demo in `docs/Setup Agent Web UI/`.

**Architecture:** Add a Python web backend under `src/sag/web/` that owns SAG workspace/session read models, Docker/session discovery, file change digests, SSE streams, task submission, and terminal WebSocket bridging. Add a Vite React TypeScript frontend under `webui/` that ports the demo's Dashboard, Workspace, Session Detail, Evidence Timeline, Context Map, File Digest, Report, Settings, and Terminal views to real API data.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, Pydantic v2, Docker SDK, React + Vite + TypeScript, shadcn-ui/Radix primitives, Tailwind CSS, lucide-react, `@xterm/xterm`, `@xterm/addon-fit`, pytest, Vitest, Playwright.

---

## Scope Check

The full redesign touches several subsystems. This plan implements one vertical
MVP that can run locally and show real or partially discovered SAG state:

- Backend read models and discovery
- File change tracker
- REST + SSE API
- Workspace-level task submission
- Static frontend matching the demo
- Terminal WebSocket bridge
- Packaging through `uv run sag ui`

The implementation must preserve these product boundaries:

- Workspace is the task entry point.
- Session is an execution record.
- Terminal is independent from sessions.
- FileChangeDigest connects manual user work to the next execution.
- Raw logs and raw context JSON stay behind explicit detail views.

## References

- Approved spec: `docs/superpowers/specs/2026-06-06-sag-web-ui-redesign-design.md`
- Local UI demo: `docs/Setup Agent Web UI/`
- shadcn Vite setup: https://ui.shadcn.com/docs/installation/vite
- xterm.js addon usage: https://xtermjs.org/docs/guides/using-addons/
- FastAPI WebSocket guide: https://fastapi.tiangolo.com/advanced/websockets/

## File Structure

Create backend package:

- `src/sag/web/__init__.py`: package exports.
- `src/sag/web/paths.py`: package-local static asset paths.
- `src/sag/web/models.py`: Pydantic read models shared by API and tests.
- `src/sag/web/status.py`: status normalization and tone mapping.
- `src/sag/web/workspace_registry.py`: SAG-managed Docker workspace discovery.
- `src/sag/web/session_registry.py`: execution/session discovery from live runs, logs, and `.setup_agent` artifacts.
- `src/sag/web/read_model.py`: builds `WorkspaceSummary` and `ExecutionSessionDetail`.
- `src/sag/web/evidence.py`: groups trusted evidence records.
- `src/sag/web/context_map.py`: converts trunk/branch JSON into the abstract Context Map.
- `src/sag/web/file_tracker.py`: metadata snapshots and file change digests.
- `src/sag/web/task_runner.py`: workspace-level task submission and background execution.
- `src/sag/web/terminal.py`: Docker exec TTY adapter for WebSocket terminal.
- `src/sag/web/app.py`: FastAPI app factory, REST routes, SSE routes, WebSocket route.
- `src/sag/web/server.py`: `sag ui` server entry point and static frontend mounting.
- `src/sag/web/demo_data.py`: deterministic read-model fixture derived from the UI demo for offline tests and Docker-unavailable fallback.
- `src/sag/web/static/`: built frontend assets served by Python.

Create frontend app:

- `webui/package.json`: Vite app scripts and frontend dependencies.
- `webui/vite.config.ts`: Vite config with `@` alias and build output to `src/sag/web/static`.
- `webui/tsconfig.json`, `webui/tsconfig.app.json`, `webui/tsconfig.node.json`: TypeScript configs.
- `webui/components.json`: shadcn config.
- `webui/src/main.tsx`: React entry point.
- `webui/src/App.tsx`: route shell and breadcrumb navigation.
- `webui/src/api/client.ts`: REST/SSE/WebSocket client helpers.
- `webui/src/api/types.ts`: frontend types mirroring backend JSON.
- `webui/src/components/ui/*.tsx`: shadcn primitives needed by the demo.
- `webui/src/components/common/*.tsx`: badges, cards, tabs, metadata labels, status/test bars.
- `webui/src/pages/Dashboard.tsx`: workspace list.
- `webui/src/pages/Workspace.tsx`: Overview/Sessions/Terminal/Settings workspace shell.
- `webui/src/pages/SessionDetail.tsx`: Status/Evidence/Context/Files/Report/Logs.
- `webui/src/components/session/*.tsx`: EvidenceTimeline, ContextMap, FilesDigest, ReportDoc, LogsView, BuildCard, TestCard.
- `webui/src/components/terminal/TerminalPanel.tsx`: xterm.js terminal.
- `webui/src/styles.css`: Tailwind and SAG theme tokens.

Create tests:

- `tests/test_web_status.py`
- `tests/test_web_models.py`
- `tests/test_web_file_tracker.py`
- `tests/test_web_context_map.py`
- `tests/test_web_evidence.py`
- `tests/test_web_workspace_registry.py`
- `tests/test_web_session_registry.py`
- `tests/test_web_read_model.py`
- `tests/test_web_api.py`
- `tests/test_web_task_runner.py`
- `tests/test_web_terminal.py`
- `webui/src/**/*.test.tsx`
- `webui/e2e/sag-workbench.spec.ts`

Modify existing files:

- `pyproject.toml`: add backend web dependencies.
- `src/sag/main.py`: add `sag ui` command.
- `.gitignore`: ignore `webui/node_modules`, keep `src/sag/web/static` tracked.

---

### Task 1: Backend Web Dependencies And Package Skeleton

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Create: `src/sag/web/__init__.py`
- Create: `src/sag/web/paths.py`
- Create: `src/sag/web/server.py`
- Test: `tests/test_web_import_smoke.py`

- [ ] **Step 1: Write the failing import smoke test**

Create `tests/test_web_import_smoke.py`:

```python
def test_web_package_imports():
    import sag.web
    from sag.web.paths import STATIC_DIR

    assert sag.web.__all__ == ["STATIC_DIR"]
    assert STATIC_DIR.name == "static"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/test_web_import_smoke.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sag.web'`.

- [ ] **Step 3: Add backend dependencies**

Modify `pyproject.toml` dependencies:

```toml
dependencies = [
    "click>=8.1.8",
    "docker>=7.1.0",
    "litellm==1.83.7",
    "requests>=2.32.5",
    "pydantic>=2.11.9",
    "rich>=14.1.0",
    "loguru>=0.7.3",
    "python-dotenv>=1.0.1",
    "openai>=2.1.0",
    "pyyaml>=6.0.3",
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
]
```

- [ ] **Step 4: Add web package skeleton**

Create `src/sag/web/paths.py`:

```python
"""Filesystem paths used by the SAG web package."""

from pathlib import Path


STATIC_DIR = Path(__file__).with_name("static")
```

Create `src/sag/web/server.py`:

```python
"""Local web server entry points for SAG Workbench."""
```

Create `src/sag/web/__init__.py`:

```python
"""Web UI backend for SAG Workbench."""

from sag.web.paths import STATIC_DIR

__all__ = ["STATIC_DIR"]
```

- [ ] **Step 5: Ignore frontend dependencies without hiding built assets**

Append to `.gitignore`:

```gitignore

# SAG web UI frontend
webui/node_modules/
webui/dist/
```

Do not add `src/sag/web/static/` to `.gitignore`; built assets must be package-visible.

- [ ] **Step 6: Run the smoke test**

Run:

```bash
uv run pytest tests/test_web_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore src/sag/web/__init__.py src/sag/web/paths.py src/sag/web/server.py tests/test_web_import_smoke.py
git commit -m "Add web backend package skeleton"
```

---

### Task 2: Backend Read Models And Status Semantics

**Files:**
- Create: `src/sag/web/status.py`
- Create: `src/sag/web/models.py`
- Test: `tests/test_web_status.py`
- Test: `tests/test_web_models.py`

- [ ] **Step 1: Write status normalization tests**

Create `tests/test_web_status.py`:

```python
from sag.web.status import StatusTone, normalize_status, status_tone


def test_normalize_status_keeps_known_values():
    assert normalize_status("BUILD SUCCESS") == "success"
    assert normalize_status("running") == "running"
    assert normalize_status("exited") == "exited"
    assert normalize_status(None) == "none"


def test_status_tone_matches_demo_semantics():
    assert status_tone("success") == StatusTone.GREEN
    assert status_tone("partial") == StatusTone.AMBER
    assert status_tone("running") == StatusTone.BLUE
    assert status_tone("failed") == StatusTone.RED
    assert status_tone("unknown") == StatusTone.NEUTRAL
```

- [ ] **Step 2: Write read-model serialization tests**

Create `tests/test_web_models.py`:

```python
from sag.web.models import (
    BuildSummary,
    DockerSummary,
    ExecutionSessionSummary,
    TestSummary,
    WorkspaceSummary,
)


def test_workspace_summary_serializes_demo_shape():
    summary = WorkspaceSummary(
        id="sag-commons-cli",
        project="apache/commons-cli",
        container="sag-commons-cli",
        stack="Java · Maven",
        docker=DockerSummary(status="running", image="sag/base:24.04"),
        task="Build project and run full test suite",
        build=BuildSummary(state="success", tool="Maven", time="47.2s"),
        test=TestSummary(state="partial", pass_count=312, fail_count=8, skip_count=0, total=320),
        report="ready",
        changed=7,
        active_session="CC-3",
        latest_session="CC-3",
        updated="just now",
    )

    data = summary.model_dump(mode="json")
    assert data["id"] == "sag-commons-cli"
    assert data["docker"]["status"] == "running"
    assert data["test"]["pass_count"] == 312
    assert data["latest_session"] == "CC-3"


def test_session_summary_uses_workspace_task_entry_semantics():
    session = ExecutionSessionSummary(
        id="CC-3",
        workspace="sag-commons-cli",
        title="Build project and execute full test suite",
        status="running",
        entry="CLI",
        start="02:14:08",
        duration="running · 2m 11s",
        build="success",
        test=TestSummary(state="partial", pass_count=312, fail_count=8, skip_count=0, total=320),
        report="ready",
        files=7,
        evidence=18,
    )

    assert session.workspace == "sag-commons-cli"
    assert session.entry == "CLI"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_status.py tests/test_web_models.py -v
```

Expected: FAIL because `sag.web.status` and `sag.web.models` do not exist.

- [ ] **Step 4: Implement status semantics**

Create `src/sag/web/status.py`:

```python
"""Status normalization for SAG web read models."""

from enum import StrEnum
from typing import Any


class StatusTone(StrEnum):
    NEUTRAL = "neutral"
    BLUE = "blue"
    GREEN = "green"
    RED = "red"
    AMBER = "amber"


_ALIASES = {
    "build success": "success",
    "build failure": "failure",
    "passed": "pass",
    "failed": "failed",
    "fail": "failed",
    "available": "available",
    "connected": "connected",
}


_TONES = {
    "success": StatusTone.GREEN,
    "pass": StatusTone.GREEN,
    "completed": StatusTone.GREEN,
    "ready": StatusTone.GREEN,
    "available": StatusTone.GREEN,
    "running": StatusTone.BLUE,
    "connected": StatusTone.BLUE,
    "active": StatusTone.BLUE,
    "partial": StatusTone.AMBER,
    "stopped": StatusTone.AMBER,
    "exited": StatusTone.RED,
    "failure": StatusTone.RED,
    "failed": StatusTone.RED,
    "blocked": StatusTone.RED,
}


def normalize_status(value: Any) -> str:
    if value is None:
        return "none"
    status = str(value).strip().lower().replace("_", " ")
    return _ALIASES.get(status, status.replace(" ", "-") if " " in status else status)


def status_tone(value: Any) -> StatusTone:
    return _TONES.get(normalize_status(value), StatusTone.NEUTRAL)
```

- [ ] **Step 5: Implement read models**

Create `src/sag/web/models.py`:

```python
"""Pydantic read models consumed by the SAG Workbench frontend."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DockerSummary(BaseModel):
    status: str
    image: str | None = None
    version: str | None = None
    endpoint: str | None = None


class BuildSummary(BaseModel):
    state: str = "none"
    tool: str = "—"
    time: str = "—"
    artifact: str | None = None
    note: str = ""


class TestSummary(BaseModel):
    state: str = "none"
    pass_count: int = Field(default=0, serialization_alias="pass")
    fail_count: int = Field(default=0, serialization_alias="fail")
    skip_count: int = Field(default=0, serialization_alias="skip")
    total: int = 0
    note: str = ""


class EvidenceRecord(BaseModel):
    time: str
    status: str
    title: str
    detail: str
    ref: str


class EvidenceGroup(BaseModel):
    source: str
    status: str
    counts: str
    time: str
    summary: str
    records: list[EvidenceRecord] = Field(default_factory=list)


class FileChangeItem(BaseModel):
    path: str
    change: Literal["added", "modified", "deleted", "renamed"]
    type: Literal["file", "dir", "other"] = "file"
    size: str = "—"
    mtime: str = "—"
    note: str = ""


class FileChangeCounts(BaseModel):
    modified: int = 0
    added: int = 0
    deleted: int = 0
    renamed: int = 0


class FileSnapshotRef(BaseModel):
    base: str
    head: str
    mode: str


class FileChangeDigest(BaseModel):
    snapshot: FileSnapshotRef
    counts: FileChangeCounts
    items: list[FileChangeItem] = Field(default_factory=list)


class ContextTask(BaseModel):
    id: str
    title: str
    status: str
    summary: str = ""
    refs: list[str] = Field(default_factory=list)
    recovered: bool = False


class TrunkSummary(BaseModel):
    goal: str
    state: str
    progress: dict[str, int]
    summary: str = ""


class ActiveBranchSummary(BaseModel):
    task: str = ""
    why: str = ""
    memory: list[str] = Field(default_factory=list)
    last_refs: list[dict[str, str]] = Field(default_factory=list, serialization_alias="lastRefs")
    pressure: float = 0.0


class ContextMap(BaseModel):
    trunk: TrunkSummary
    tasks: list[ContextTask] = Field(default_factory=list)
    active_branch: ActiveBranchSummary = Field(
        default_factory=ActiveBranchSummary, serialization_alias="activeBranch"
    )
    debug: dict[str, Any] = Field(default_factory=dict)


class ReportDocument(BaseModel):
    title: str
    path: str | None = None
    generated: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceSummary(BaseModel):
    id: str
    project: str
    container: str
    stack: str = "Unknown"
    tag: str | None = None
    release: str | None = None
    commit: str | None = None
    docker: DockerSummary
    task: str = "No current task"
    build: BuildSummary | str = "none"
    test: TestSummary = Field(default_factory=TestSummary)
    report: str = "none"
    changed: int = 0
    active_session: str | None = Field(default=None, serialization_alias="activeSession")
    latest_session: str | None = Field(default=None, serialization_alias="latestSession")
    updated: str = "unknown"


class ExecutionSessionSummary(BaseModel):
    id: str
    workspace: str
    title: str
    status: str
    entry: str
    start: str
    finish: str | None = None
    duration: str
    build: str
    test: TestSummary
    report: str
    files: int
    evidence: int


class BlockerSummary(BaseModel):
    code: str
    title: str
    detail: str
    hint: str


class ExecutionSessionDetail(BaseModel):
    id: str
    workspace: str
    title: str
    status: str
    entry: str
    start: str
    duration: str
    outcome: str
    build: BuildSummary
    test: TestSummary
    report: str
    report_doc: ReportDocument | None = Field(default=None, serialization_alias="reportDoc")
    blocker: BlockerSummary | None = None
    evidence: list[EvidenceGroup] = Field(default_factory=list)
    files: FileChangeDigest | None = None
    context: ContextMap | None = None
    logs: list[str] = Field(default_factory=list)
    partial: bool = False


class TerminalConnectionState(BaseModel):
    container: str
    cwd: str = "/workspace"
    status: str
    tty: str = "120 × 32"
    lines: list[dict[str, str]] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    docker: DockerSummary
    workspaces: list[WorkspaceSummary]
```

- [ ] **Step 6: Run model tests**

Run:

```bash
uv run pytest tests/test_web_status.py tests/test_web_models.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/web/status.py src/sag/web/models.py tests/test_web_status.py tests/test_web_models.py
git commit -m "Add web UI read models"
```

---

### Task 3: Demo Fixture Adapter

**Files:**
- Create: `src/sag/web/demo_data.py`
- Test: `tests/test_web_demo_data.py`

- [ ] **Step 1: Write fixture tests**

Create `tests/test_web_demo_data.py`:

```python
from sag.web.demo_data import build_demo_dashboard, get_demo_session


def test_demo_dashboard_matches_local_ui_demo_shape():
    dashboard = build_demo_dashboard()

    assert dashboard.docker.status == "connected"
    assert dashboard.workspaces[0].id == "sag-commons-cli"
    assert dashboard.workspaces[0].latest_session == "CC-3"
    assert dashboard.workspaces[0].test.pass_count == 312


def test_demo_session_contains_evidence_context_files_and_report():
    detail = get_demo_session("CC-3")

    assert detail.id == "CC-3"
    assert detail.evidence[0].source == "Project analyzer"
    assert detail.context is not None
    assert detail.files is not None
    assert detail.report_doc is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_demo_data.py -v
```

Expected: FAIL because `sag.web.demo_data` does not exist.

- [ ] **Step 3: Implement deterministic fixture**

Create `src/sag/web/demo_data.py` with a compact typed version of the local demo data. Keep the first workspace/session faithful to `docs/Setup Agent Web UI/src/data.js`:

```python
"""Deterministic SAG Workbench fixture derived from docs/Setup Agent Web UI."""

from sag.web.models import (
    ActiveBranchSummary,
    BuildSummary,
    ContextMap,
    ContextTask,
    DashboardResponse,
    DockerSummary,
    EvidenceGroup,
    EvidenceRecord,
    ExecutionSessionDetail,
    FileChangeCounts,
    FileChangeDigest,
    FileChangeItem,
    FileSnapshotRef,
    ReportDocument,
    TerminalConnectionState,
    TestSummary,
    TrunkSummary,
    WorkspaceSummary,
)


def _commons_test_summary() -> TestSummary:
    return TestSummary(state="partial", pass_count=312, fail_count=8, skip_count=0, total=320)


def _commons_evidence() -> list[EvidenceGroup]:
    return [
        EvidenceGroup(
            source="Project analyzer",
            status="success",
            counts="1 scan",
            time="01:50",
            summary="Detected Maven project, Java 8 source target, 38 test classes.",
            records=[
                EvidenceRecord(
                    time="01:50:51",
                    status="success",
                    title="Project type resolved",
                    detail="Build tool: Maven 3.9.6 · packaging: jar · 38 test classes.",
                    ref=".setup_agent/analysis/project_scan.json",
                )
            ],
        ),
        EvidenceGroup(
            source="Test validator",
            status="partial",
            counts="312 / 320",
            time="02:16",
            summary="312 passed, 8 failed across 38 classes.",
            records=[
                EvidenceRecord(
                    time="02:16:30",
                    status="fail",
                    title="HelpFormatter line wrapping",
                    detail="Expected wrapped width 74 but was 80.",
                    ref="target/surefire-reports/HelpFormatterTest.xml",
                )
            ],
        ),
    ]


def _commons_context() -> ContextMap:
    return ContextMap(
        trunk=TrunkSummary(
            goal="Set up apache/commons-cli so it builds cleanly and its full test suite runs.",
            state="In progress",
            progress={"done": 4, "total": 6},
            summary="Toolchain provisioned and Maven build succeeds on JDK 11.",
        ),
        tasks=[
            ContextTask(id="T1", title="Clone repository", status="completed", refs=["task_T1.json"]),
            ContextTask(id="T5", title="Run full test suite", status="active", refs=["task_T5.json"]),
        ],
        active_branch=ActiveBranchSummary(
            task="Run full test suite",
            why="Build succeeds; passing tests are the remaining gate.",
            memory=["mvn test -> 312 passed, 8 failed in HelpFormatterTest."],
            last_refs=[{"label": "maven_test.log", "ref": "logs/session_021408/maven_test.log"}],
            pressure=0.61,
        ),
        debug={"trunk": "/workspace/.setup_agent/contexts/trunk_commons-cli.json", "branches": []},
    )


def _commons_files() -> FileChangeDigest:
    return FileChangeDigest(
        snapshot=FileSnapshotRef(base="021408 (startup)", head="live", mode="metadata"),
        counts=FileChangeCounts(modified=4, added=2, deleted=0, renamed=0),
        items=[
            FileChangeItem(path="pom.xml", change="modified", size="14.2 KB", mtime="02:11:30"),
            FileChangeItem(
                path=".setup_agent/env_overlay.json",
                change="modified",
                size="1.1 KB",
                mtime="02:02:44",
                note="JAVA_HOME override persisted.",
            ),
        ],
    )


def _commons_report() -> ReportDocument:
    return ReportDocument(
        title="setup-report-2026-06-06T0216.md",
        path="/workspace/setup-report-2026-06-06T0216.md",
        generated="2026-06-06 02:16:40 UTC",
        blocks=[
            {"type": "h1", "text": "Setup Report — apache/commons-cli"},
            {"type": "p", "text": "The project builds cleanly and is runnable."},
        ],
    )


def build_demo_dashboard() -> DashboardResponse:
    return DashboardResponse(
        docker=DockerSummary(status="connected", version="27.1.1", endpoint="unix:///var/run/docker.sock"),
        workspaces=[
            WorkspaceSummary(
                id="sag-commons-cli",
                project="apache/commons-cli",
                container="sag-commons-cli",
                stack="Java · Maven",
                tag="rel/commons-cli-1.6.0",
                release="1.6.0",
                commit="b7c8f2a",
                docker=DockerSummary(status="running", image="sag/base:24.04"),
                task="Build project and run full test suite",
                build=BuildSummary(state="success", tool="Maven 3.9.6", time="47.2s"),
                test=_commons_test_summary(),
                report="ready",
                changed=7,
                active_session="CC-3",
                latest_session="CC-3",
                updated="just now",
            )
        ],
    )


def get_demo_session(session_id: str) -> ExecutionSessionDetail:
    if session_id != "CC-3":
        raise KeyError(session_id)
    return ExecutionSessionDetail(
        id="CC-3",
        workspace="sag-commons-cli",
        title="Build project and execute full test suite",
        status="running",
        entry="CLI",
        start="02:14:08",
        duration="running · 2m 11s",
        outcome="Build succeeds and the test suite is running. 312 of 320 cases pass.",
        build=BuildSummary(
            state="success",
            tool="Maven 3.9.6",
            time="47.2s",
            artifact="target/commons-cli-1.6.0.jar",
            note="clean package -DskipTests on JDK 11",
        ),
        test=_commons_test_summary(),
        report="ready",
        report_doc=_commons_report(),
        evidence=_commons_evidence(),
        files=_commons_files(),
        context=_commons_context(),
        logs=["[02:16:40] INFO sag report_tool: wrote setup report"],
    )


def get_demo_terminal(workspace_id: str) -> TerminalConnectionState:
    return TerminalConnectionState(
        container=workspace_id,
        cwd="/workspace/commons-cli",
        status="connected",
        lines=[
            {"kind": "prompt", "text": "root@sag-commons-cli:/workspace/commons-cli# java -version"},
            {"kind": "out", "text": 'openjdk version "11.0.22"'},
        ],
    )
```

- [ ] **Step 4: Run fixture tests**

Run:

```bash
uv run pytest tests/test_web_demo_data.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/demo_data.py tests/test_web_demo_data.py
git commit -m "Add web UI demo read model fixture"
```

---

### Task 4: File Change Tracker

**Files:**
- Create: `src/sag/web/file_tracker.py`
- Test: `tests/test_web_file_tracker.py`

- [ ] **Step 1: Write metadata diff tests**

Create `tests/test_web_file_tracker.py`:

```python
from pathlib import Path

from sag.web.file_tracker import FileChangeTracker


def test_file_tracker_detects_added_modified_and_deleted(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    keep = root / "keep.txt"
    gone = root / "gone.txt"
    keep.write_text("one", encoding="utf-8")
    gone.write_text("old", encoding="utf-8")

    tracker = FileChangeTracker(root)
    base = tracker.snapshot("base")

    keep.write_text("two", encoding="utf-8")
    gone.unlink()
    (root / "new.txt").write_text("new", encoding="utf-8")

    head = tracker.snapshot("head")
    digest = tracker.diff(base, head)

    changes = {item.path: item.change for item in digest.items}
    assert changes["keep.txt"] == "modified"
    assert changes["gone.txt"] == "deleted"
    assert changes["new.txt"] == "added"
    assert digest.counts.modified == 1
    assert digest.counts.deleted == 1
    assert digest.counts.added == 1


def test_file_tracker_ignores_heavy_and_hidden_generated_dirs(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "index").write_text("ignored", encoding="utf-8")
    (root / "target").mkdir()
    (root / "target" / "app.jar").write_text("ignored", encoding="utf-8")
    (root / "src.py").write_text("tracked", encoding="utf-8")

    tracker = FileChangeTracker(root)
    snap = tracker.snapshot("base")

    assert "src.py" in snap.files
    assert ".git/index" not in snap.files
    assert "target/app.jar" not in snap.files
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_file_tracker.py -v
```

Expected: FAIL because `sag.web.file_tracker` does not exist.

- [ ] **Step 3: Implement metadata snapshot and digest**

Create `src/sag/web/file_tracker.py`:

```python
"""Workspace file change snapshots for SAG Workbench."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sag.web.models import FileChangeCounts, FileChangeDigest, FileChangeItem, FileSnapshotRef


DEFAULT_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "target", "build", "dist"}


@dataclass(frozen=True)
class FileMeta:
    path: str
    type: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    id: str
    root: Path
    mode: str
    files: dict[str, FileMeta]


class FileChangeTracker:
    def __init__(self, root: Path, ignore_dirs: set[str] | None = None):
        self.root = root
        self.ignore_dirs = ignore_dirs or DEFAULT_IGNORE_DIRS

    def snapshot(self, snapshot_id: str) -> FileSnapshot:
        files: dict[str, FileMeta] = {}
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root).as_posix()
            if self._ignored(path):
                continue
            stat = path.stat()
            kind = "dir" if path.is_dir() else "file" if path.is_file() else "other"
            files[rel] = FileMeta(path=rel, type=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        return FileSnapshot(id=snapshot_id, root=self.root, mode="metadata", files=files)

    def diff(self, base: FileSnapshot, head: FileSnapshot) -> FileChangeDigest:
        items: list[FileChangeItem] = []
        base_paths = set(base.files)
        head_paths = set(head.files)

        for rel in sorted(head_paths - base_paths):
            meta = head.files[rel]
            items.append(self._item(meta, "added"))
        for rel in sorted(base_paths - head_paths):
            meta = base.files[rel]
            items.append(self._item(meta, "deleted"))
        for rel in sorted(base_paths & head_paths):
            before = base.files[rel]
            after = head.files[rel]
            if before.size != after.size or before.mtime_ns != after.mtime_ns or before.type != after.type:
                items.append(self._item(after, "modified"))

        counts = FileChangeCounts(
            added=sum(1 for item in items if item.change == "added"),
            modified=sum(1 for item in items if item.change == "modified"),
            deleted=sum(1 for item in items if item.change == "deleted"),
            renamed=0,
        )
        return FileChangeDigest(
            snapshot=FileSnapshotRef(base=base.id, head=head.id, mode=head.mode),
            counts=counts,
            items=items,
        )

    def _ignored(self, path: Path) -> bool:
        rel_parts = path.relative_to(self.root).parts
        return any(part in self.ignore_dirs for part in rel_parts)

    def _item(self, meta: FileMeta, change: str) -> FileChangeItem:
        return FileChangeItem(
            path=meta.path,
            change=change,
            type=meta.type,  # type: ignore[arg-type]
            size=_format_size(meta.size),
            mtime=str(meta.mtime_ns),
        )


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
```

- [ ] **Step 4: Run tracker tests**

Run:

```bash
uv run pytest tests/test_web_file_tracker.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/file_tracker.py tests/test_web_file_tracker.py
git commit -m "Add workspace file change tracker"
```

---

### Task 5: Context Map Builder

**Files:**
- Create: `src/sag/web/context_map.py`
- Test: `tests/test_web_context_map.py`

- [ ] **Step 1: Write trunk/branch mapping tests**

Create `tests/test_web_context_map.py`:

```python
import json
from pathlib import Path

from sag.web.context_map import ContextMapBuilder


def test_context_map_builder_creates_trunk_branch_abstraction(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-cli",
                "overall_status": "In progress",
                "summary": "Build succeeds; tests are partial.",
                "todo_list": [
                    {"id": "T1", "task": "Clone repository", "status": "completed", "summary": "Cloned."},
                    {"id": "T2", "task": "Run tests", "status": "active", "summary": "312/320 passing."},
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_T2.json").write_text(
        json.dumps(
            {
                "task": "Run tests",
                "why": "Build is green.",
                "memory": ["mvn test -> 312/320"],
                "last_refs": [{"label": "maven_test.log", "ref": "logs/maven_test.log"}],
                "context_pressure": 0.42,
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert ctx.trunk.goal == "Set up commons-cli"
    assert ctx.trunk.progress == {"done": 1, "total": 2}
    assert ctx.tasks[1].status == "active"
    assert ctx.active_branch.task == "Run tests"
    assert ctx.debug["trunk"].endswith("trunk_commons.json")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_context_map.py -v
```

Expected: FAIL because `sag.web.context_map` does not exist.

- [ ] **Step 3: Implement context map builder**

Create `src/sag/web/context_map.py`:

```python
"""Build abstract trunk/branch context maps from SAG context files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sag.web.models import ActiveBranchSummary, ContextMap, ContextTask, TrunkSummary


class ContextMapBuilder:
    def __init__(self, contexts_dir: Path):
        self.contexts_dir = contexts_dir

    def build(self) -> ContextMap | None:
        trunk_path = self._find_trunk()
        if trunk_path is None:
            return None
        trunk_data = self._read_json(trunk_path)
        raw_tasks = trunk_data.get("todo_list") or trunk_data.get("tasks") or []
        tasks = [self._task(item, index) for index, item in enumerate(raw_tasks, start=1)]
        active = next((task for task in tasks if task.status == "active"), None)
        active_branch = self._active_branch(active.id if active else None)
        done = sum(1 for task in tasks if task.status == "completed")

        return ContextMap(
            trunk=TrunkSummary(
                goal=str(trunk_data.get("goal") or trunk_data.get("project_goal") or "Unknown goal"),
                state=str(trunk_data.get("overall_status") or trunk_data.get("state") or "Unknown"),
                progress={"done": done, "total": len(tasks)},
                summary=str(trunk_data.get("summary") or trunk_data.get("latest_summary") or ""),
            ),
            tasks=tasks,
            active_branch=active_branch,
            debug={
                "trunk": str(trunk_path),
                "branches": [str(path) for path in sorted(self.contexts_dir.glob("task_*.json"))],
            },
        )

    def _find_trunk(self) -> Path | None:
        candidates = sorted(self.contexts_dir.glob("trunk*.json"))
        return candidates[0] if candidates else None

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _task(self, item: dict[str, Any], index: int) -> ContextTask:
        task_id = str(item.get("id") or item.get("task_id") or f"T{index}")
        return ContextTask(
            id=task_id,
            title=str(item.get("task") or item.get("title") or "Untitled task"),
            status=str(item.get("status") or "pending"),
            summary=str(item.get("summary") or ""),
            refs=[str(ref) for ref in item.get("refs", [])],
            recovered=bool(item.get("recovered", False)),
        )

    def _active_branch(self, task_id: str | None) -> ActiveBranchSummary:
        if task_id is None:
            return ActiveBranchSummary()
        branch_path = self.contexts_dir / f"task_{task_id}.json"
        data = self._read_json(branch_path)
        return ActiveBranchSummary(
            task=str(data.get("task") or ""),
            why=str(data.get("why") or ""),
            memory=[str(item) for item in data.get("memory", [])],
            last_refs=[dict(item) for item in data.get("last_refs", [])],
            pressure=float(data.get("context_pressure") or data.get("pressure") or 0.0),
        )
```

- [ ] **Step 4: Run context tests**

Run:

```bash
uv run pytest tests/test_web_context_map.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/context_map.py tests/test_web_context_map.py
git commit -m "Add trunk branch context map builder"
```

---

### Task 6: Evidence Index

**Files:**
- Create: `src/sag/web/evidence.py`
- Test: `tests/test_web_evidence.py`

- [ ] **Step 1: Write evidence grouping tests**

Create `tests/test_web_evidence.py`:

```python
from datetime import datetime, timezone

from sag.ui.state import UIEvidenceRecord
from sag.web.evidence import EvidenceIndex


def test_evidence_index_groups_runtime_records_by_source():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="command",
            summary="maven clean package passed",
            metadata={"tool_name": "maven", "status": "success", "ref": "logs/maven.log"},
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 16, tzinfo=timezone.utc),
            kind="validation",
            summary="312/320 tests passed",
            metadata={"source": "Test validator", "status": "partial", "ref": "target/surefire-reports"},
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert groups[0].source == "Build tool · Maven"
    assert groups[0].status == "success"
    assert groups[1].source == "Test validator"
    assert groups[1].records[0].ref == "target/surefire-reports"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_evidence.py -v
```

Expected: FAIL because `sag.web.evidence` does not exist.

- [ ] **Step 3: Implement evidence grouping**

Create `src/sag/web/evidence.py`:

```python
"""Trusted evidence grouping for SAG Workbench."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Iterable

from sag.ui.state import UIEvidenceRecord
from sag.web.models import EvidenceGroup, EvidenceRecord


class EvidenceIndex:
    def from_ui_records(self, records: Iterable[UIEvidenceRecord]) -> list[EvidenceGroup]:
        grouped: OrderedDict[str, list[EvidenceRecord]] = OrderedDict()
        statuses: dict[str, str] = {}

        for record in records:
            metadata = record.metadata or {}
            source = self._source(record.kind, metadata)
            status = str(metadata.get("status") or "info")
            ref = str(metadata.get("ref") or metadata.get("output_ref") or "runtime")
            grouped.setdefault(source, []).append(
                EvidenceRecord(
                    time=self._time(record.timestamp),
                    status=status,
                    title=record.kind.title(),
                    detail=record.summary,
                    ref=ref,
                )
            )
            statuses[source] = self._merge_status(statuses.get(source), status)

        return [
            EvidenceGroup(
                source=source,
                status=statuses[source],
                counts=f"{len(items)} records",
                time=items[-1].time,
                summary=items[-1].detail,
                records=items,
            )
            for source, items in grouped.items()
        ]

    def _source(self, kind: str, metadata: dict[str, object]) -> str:
        if metadata.get("source"):
            return str(metadata["source"])
        tool = str(metadata.get("tool_name") or "")
        if tool == "maven":
            return "Build tool · Maven"
        if tool == "gradle":
            return "Build tool · Gradle"
        if tool:
            return tool.title()
        return kind.title()

    def _merge_status(self, current: str | None, next_status: str) -> str:
        order = {"failure": 4, "failed": 4, "partial": 3, "success": 2, "info": 1}
        if current is None:
            return next_status
        return next_status if order.get(next_status, 1) > order.get(current, 1) else current

    def _time(self, value: datetime) -> str:
        return value.strftime("%H:%M")
```

- [ ] **Step 4: Run evidence tests**

Run:

```bash
uv run pytest tests/test_web_evidence.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/evidence.py tests/test_web_evidence.py
git commit -m "Add web evidence grouping"
```

---

### Task 7: Workspace And Session Discovery

**Files:**
- Create: `src/sag/web/workspace_registry.py`
- Create: `src/sag/web/session_registry.py`
- Test: `tests/test_web_workspace_registry.py`
- Test: `tests/test_web_session_registry.py`

- [ ] **Step 1: Write workspace discovery tests with a fake Docker client**

Create `tests/test_web_workspace_registry.py`:

```python
from sag.web.workspace_registry import WorkspaceRegistry


class FakeImage:
    tags = ["sag/base:24.04"]


class FakeContainer:
    def __init__(self):
        self.name = "sag-commons-cli"
        self.status = "running"
        self.image = FakeImage()
        self.attrs = {
            "Created": "2026-06-06T02:00:00Z",
            "Config": {"Labels": {"setup-agent.project": "commons-cli"}},
        }


class FakeContainers:
    def list(self, all=True):
        return [FakeContainer()]


class FakeClient:
    containers = FakeContainers()


def test_workspace_registry_lists_sag_containers_only():
    registry = WorkspaceRegistry(client=FakeClient())
    workspaces = registry.list_workspaces()

    assert len(workspaces) == 1
    assert workspaces[0].id == "sag-commons-cli"
    assert workspaces[0].container == "sag-commons-cli"
    assert workspaces[0].docker.status == "running"
```

- [ ] **Step 2: Write session discovery tests from local artifacts**

Create `tests/test_web_session_registry.py`:

```python
import json
from pathlib import Path

from sag.web.session_registry import SessionRegistry


def test_session_registry_reads_local_session_index(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "CC-3",
                        "workspace": "sag-commons-cli",
                        "title": "Build project",
                        "status": "running",
                        "entry": "CLI",
                        "start": "02:14:08",
                        "duration": "running · 2m 11s",
                        "build": "success",
                        "test": {"state": "partial", "pass": 312, "fail": 8, "skip": 0, "total": 320},
                        "report": "ready",
                        "files": 7,
                        "evidence": 18,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert rows[0].id == "CC-3"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].test.pass_count == 312
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_workspace_registry.py tests/test_web_session_registry.py -v
```

Expected: FAIL because registry modules do not exist.

- [ ] **Step 4: Implement workspace registry**

Create `src/sag/web/workspace_registry.py`:

```python
"""Discover SAG-managed Docker workspaces for the web UI."""

from __future__ import annotations

from typing import Any

import docker

from sag.web.models import BuildSummary, DockerSummary, TestSummary, WorkspaceSummary


class WorkspaceRegistry:
    def __init__(self, client: Any | None = None):
        self.client = client or docker.from_env()

    def list_workspaces(self) -> list[WorkspaceSummary]:
        workspaces: list[WorkspaceSummary] = []
        for container in self.client.containers.list(all=True):
            if not container.name.startswith("sag-"):
                continue
            labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
            project_name = labels.get("setup-agent.project") or container.name.removeprefix("sag-")
            image = container.image.tags[0] if getattr(container.image, "tags", None) else None
            workspaces.append(
                WorkspaceSummary(
                    id=container.name,
                    project=str(project_name),
                    container=container.name,
                    docker=DockerSummary(status=container.status, image=image),
                    build=BuildSummary(),
                    test=TestSummary(),
                    updated=_created(container.attrs.get("Created")),
                )
            )
        return sorted(workspaces, key=lambda item: item.container)


def _created(value: str | None) -> str:
    return value or "unknown"
```

- [ ] **Step 5: Implement session registry**

Create `src/sag/web/session_registry.py`:

```python
"""Discover execution sessions from SAG artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sag.web.models import ExecutionSessionSummary, TestSummary


class SessionRegistry:
    def read_index(self, workspace_root: Path, workspace_id: str) -> list[ExecutionSessionSummary]:
        path = workspace_root / ".setup_agent" / "sessions" / "index.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        rows = data.get("sessions", [])
        return [self._summary(row, workspace_id) for row in rows]

    def _summary(self, row: dict[str, Any], workspace_id: str) -> ExecutionSessionSummary:
        raw_test = row.get("test") or {}
        return ExecutionSessionSummary(
            id=str(row.get("id")),
            workspace=str(row.get("workspace") or workspace_id),
            title=str(row.get("title") or "Untitled task"),
            status=str(row.get("status") or "unknown"),
            entry=str(row.get("entry") or "external"),
            start=str(row.get("start") or "—"),
            finish=row.get("finish"),
            duration=str(row.get("duration") or "—"),
            build=str(row.get("build") or "none"),
            test=TestSummary(
                state=str(raw_test.get("state") or "none"),
                pass_count=int(raw_test.get("pass") or 0),
                fail_count=int(raw_test.get("fail") or 0),
                skip_count=int(raw_test.get("skip") or 0),
                total=int(raw_test.get("total") or 0),
            ),
            report=str(row.get("report") or "none"),
            files=int(row.get("files") or 0),
            evidence=int(row.get("evidence") or 0),
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
```

- [ ] **Step 6: Run registry tests**

Run:

```bash
uv run pytest tests/test_web_workspace_registry.py tests/test_web_session_registry.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/web/workspace_registry.py src/sag/web/session_registry.py tests/test_web_workspace_registry.py tests/test_web_session_registry.py
git commit -m "Add web workspace and session discovery"
```

---

### Task 8: Read Model Builder

**Files:**
- Create: `src/sag/web/read_model.py`
- Test: `tests/test_web_read_model.py`

- [ ] **Step 1: Write read-model builder tests**

Create `tests/test_web_read_model.py`:

```python
from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.read_model import ReadModelBuilder


class FakeWorkspaceRegistry:
    def list_workspaces(self):
        return build_demo_dashboard().workspaces


class FakeSessionRegistry:
    def read_index(self, workspace_root, workspace_id):
        return []


def test_read_model_builder_uses_demo_fallback_when_requested():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=True,
    )

    dashboard = builder.dashboard()
    detail = builder.session_detail("CC-3")

    assert dashboard.workspaces[0].id == "sag-commons-cli"
    assert detail.id == get_demo_session("CC-3").id
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_web_read_model.py -v
```

Expected: FAIL because `sag.web.read_model` does not exist.

- [ ] **Step 3: Implement builder with explicit demo fallback**

Create `src/sag/web/read_model.py`:

```python
"""Compose web read models from registries and artifacts."""

from __future__ import annotations

from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.models import DashboardResponse, DockerSummary, ExecutionSessionDetail
from sag.web.session_registry import SessionRegistry
from sag.web.workspace_registry import WorkspaceRegistry


class ReadModelBuilder:
    def __init__(
        self,
        workspace_registry: WorkspaceRegistry | None = None,
        session_registry: SessionRegistry | None = None,
        demo_mode: bool = False,
    ):
        self.workspace_registry = workspace_registry or WorkspaceRegistry()
        self.session_registry = session_registry or SessionRegistry()
        self.demo_mode = demo_mode

    def dashboard(self) -> DashboardResponse:
        if self.demo_mode:
            return build_demo_dashboard()
        try:
            return DashboardResponse(
                docker=DockerSummary(status="connected"),
                workspaces=self.workspace_registry.list_workspaces(),
            )
        except Exception as exc:
            return DashboardResponse(
                docker=DockerSummary(status="unavailable", image=str(exc)),
                workspaces=[],
            )

    def session_detail(self, session_id: str) -> ExecutionSessionDetail:
        if self.demo_mode:
            return get_demo_session(session_id)
        raise KeyError(f"Session detail is not available yet for {session_id}")
```

- [ ] **Step 4: Run builder tests**

Run:

```bash
uv run pytest tests/test_web_read_model.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/read_model.py tests/test_web_read_model.py
git commit -m "Add web read model builder"
```

---

### Task 9: REST And SSE API

**Files:**
- Create: `src/sag/web/app.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write API tests**

Create `tests/test_web_api.py`:

```python
from fastapi.testclient import TestClient

from sag.web.app import create_app
from sag.web.read_model import ReadModelBuilder


def test_dashboard_endpoint_returns_workspaces():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "sag-commons-cli"


def test_session_endpoint_returns_session_detail():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/sessions/CC-3")

    assert response.status_code == 200
    assert response.json()["id"] == "CC-3"
    assert response.json()["reportDoc"]["title"].startswith("setup-report")


def test_dashboard_stream_emits_sse_snapshot():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    with client.stream("GET", "/api/stream/dashboard") as response:
        first = next(response.iter_lines())

    assert response.status_code == 200
    assert first.startswith("event: snapshot")
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_api.py -v
```

Expected: FAIL because `sag.web.app` does not exist.

- [ ] **Step 3: Implement FastAPI app factory**

Create `src/sag/web/app.py`:

```python
"""FastAPI application for SAG Workbench."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from sag.web.read_model import ReadModelBuilder


def create_app(read_model: ReadModelBuilder | None = None) -> FastAPI:
    builder = read_model or ReadModelBuilder()
    app = FastAPI(title="SAG Workbench", version="0.1.0")

    @app.get("/api/workspaces")
    def list_workspaces():
        return builder.dashboard().model_dump(mode="json", by_alias=True)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            return builder.session_detail(session_id).model_dump(mode="json", by_alias=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/stream/dashboard")
    def stream_dashboard():
        return StreamingResponse(
            _single_snapshot(builder.dashboard().model_dump(mode="json", by_alias=True)),
            media_type="text/event-stream",
        )

    return app


def _single_snapshot(payload: dict) -> Iterator[str]:
    yield "event: snapshot\n"
    yield f"data: {json.dumps(payload)}\n\n"
```

- [ ] **Step 4: Run API tests**

Run:

```bash
uv run pytest tests/test_web_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/app.py tests/test_web_api.py
git commit -m "Add web UI REST and SSE API"
```

---

### Task 10: `sag ui` CLI Command

**Files:**
- Modify: `src/sag/main.py`
- Modify: `src/sag/web/server.py`
- Test: `tests/test_web_cli.py`

- [ ] **Step 1: Write CLI test**

Create `tests/test_web_cli.py`:

```python
from click.testing import CliRunner

from sag.main import cli


def test_ui_command_accepts_host_port_and_demo_flag(monkeypatch):
    calls = {}

    def fake_run_server(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("sag.main.run_web_server", fake_run_server)
    result = CliRunner().invoke(cli, ["ui", "--host", "127.0.0.1", "--port", "8765", "--demo"])

    assert result.exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 8765, "demo": True}
```

- [ ] **Step 2: Run CLI test to verify failure**

Run:

```bash
uv run pytest tests/test_web_cli.py -v
```

Expected: FAIL because `ui` command and `run_web_server` do not exist.

- [ ] **Step 3: Implement server runner**

Modify `src/sag/web/server.py`:

```python
"""Local web server entry points for SAG Workbench."""

from pathlib import Path

import uvicorn

from sag.web.app import create_app
from sag.web.read_model import ReadModelBuilder


STATIC_DIR = Path(__file__).with_name("static")


def run_web_server(host: str = "127.0.0.1", port: int = 0, demo: bool = False) -> None:
    app = create_app(ReadModelBuilder(demo_mode=demo))
    uvicorn.run(app, host=host, port=port, log_level="info")
```

- [ ] **Step 4: Add CLI command**

Modify imports in `src/sag/main.py`:

```python
from sag.web.server import run_web_server
```

Add command near `version()`:

```python
@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for the local web UI")
@click.option("--port", default=0, show_default=True, type=int, help="Port for the local web UI")
@click.option("--demo", is_flag=True, help="Use deterministic demo data instead of Docker discovery")
def ui(host, port, demo):
    """Start the local SAG Workbench web UI."""
    console.print(f"[bold blue]Starting SAG Workbench[/bold blue] on {host}:{port or 'auto'}")
    run_web_server(host=host, port=port, demo=demo)
```

- [ ] **Step 5: Run CLI test**

Run:

```bash
uv run pytest tests/test_web_cli.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sag/main.py src/sag/web/server.py tests/test_web_cli.py
git commit -m "Add sag ui command"
```

---

### Task 11: Workspace-Level Task Submission

**Files:**
- Create: `src/sag/web/task_runner.py`
- Modify: `src/sag/web/app.py`
- Test: `tests/test_web_task_runner.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write task runner tests**

Create `tests/test_web_task_runner.py`:

```python
from sag.web.task_runner import TaskRequest, TaskRunner


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def run(self, workspace_id: str, task: str, source_session: str | None):
        self.calls.append((workspace_id, task, source_session))
        return "RUN-1"


def test_task_runner_creates_new_session_from_workspace_task():
    launcher = FakeLauncher()
    runner = TaskRunner(launcher=launcher)

    response = runner.submit(
        "sag-commons-cli",
        TaskRequest(task="Run formatter tests", source_session="CC-3"),
    )

    assert response["session_id"] == "RUN-1"
    assert launcher.calls == [("sag-commons-cli", "Run formatter tests", "CC-3")]
```

- [ ] **Step 2: Extend API test for task submission**

Append to `tests/test_web_api.py`:

```python
def test_submit_task_is_workspace_scoped():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.post(
        "/api/workspaces/sag-commons-cli/tasks",
        json={"task": "Run formatter tests", "source_session": "CC-3"},
    )

    assert response.status_code == 202
    assert response.json()["workspace_id"] == "sag-commons-cli"
    assert response.json()["source_session"] == "CC-3"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_web_task_runner.py tests/test_web_api.py -v
```

Expected: FAIL because task runner and POST route do not exist.

- [ ] **Step 4: Implement task runner**

Create `src/sag/web/task_runner.py`:

```python
"""Workspace-level task submission for SAG Workbench."""

from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Thread
from uuid import uuid4

from pydantic import BaseModel, Field

from sag.agent.agent import SetupAgent
from sag.config import get_config
from sag.docker_orch.orch import DockerOrchestrator


class TaskRequest(BaseModel):
    task: str = Field(min_length=1)
    source_session: str | None = None


@dataclass
class AgentTaskLauncher:
    def run(self, workspace_id: str, task: str, source_session: str | None) -> str:
        session_id = f"UI-{uuid4().hex[:8]}"
        thread = Thread(
            target=self._run_agent,
            args=(workspace_id, task, source_session, session_id),
            daemon=True,
            name=f"sag-ui-task-{session_id}",
        )
        thread.start()
        return session_id

    def _run_agent(
        self,
        workspace_id: str,
        task: str,
        source_session: str | None,
        session_id: str,
    ) -> None:
        docker_label = workspace_id.removeprefix("sag-")
        orchestrator = DockerOrchestrator(project_name=docker_label)
        if not orchestrator.is_container_running():
            orchestrator.start_container()
        project_name = _read_project_name(orchestrator, docker_label)
        task_text = task
        if source_session:
            task_text = f"{task}\n\nReference prior SAG session: {source_session}"
        agent = SetupAgent(config=get_config(), orchestrator=orchestrator)
        agent.run_task(project_name=project_name, task_description=task_text)


class TaskRunner:
    def __init__(self, launcher: AgentTaskLauncher | None = None):
        self.launcher = launcher or AgentTaskLauncher()

    def submit(self, workspace_id: str, request: TaskRequest) -> dict[str, str | None]:
        session_id = self.launcher.run(workspace_id, request.task, request.source_session)
        return {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "source_session": request.source_session,
            "status": "queued",
        }


def _read_project_name(orchestrator: DockerOrchestrator, fallback: str) -> str:
    result = orchestrator.execute_command("cat /workspace/.setup_agent/project_meta.json 2>/dev/null")
    if result.get("exit_code") != 0:
        return fallback
    try:
        data = json.loads(result.get("output") or "{}")
    except json.JSONDecodeError:
        return fallback
    return str(data.get("project_name") or fallback)
```

- [ ] **Step 5: Add POST route**

Modify `src/sag/web/app.py`:

```python
from sag.web.task_runner import TaskRequest, TaskRunner
```

Inside `create_app` before `return app`:

```python
    task_runner = TaskRunner()

    @app.post("/api/workspaces/{workspace_id}/tasks", status_code=202)
    def submit_task(workspace_id: str, request: TaskRequest):
        return task_runner.submit(workspace_id, request)
```

- [ ] **Step 6: Run task/API tests**

Run:

```bash
uv run pytest tests/test_web_task_runner.py tests/test_web_api.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/sag/web/task_runner.py src/sag/web/app.py tests/test_web_task_runner.py tests/test_web_api.py
git commit -m "Add workspace task submission API"
```

---

### Task 12: Frontend Scaffold With shadcn/Vite

**Files:**
- Create: `webui/package.json`
- Create: `webui/index.html`
- Create: `webui/vite.config.ts`
- Create: `webui/tsconfig.json`
- Create: `webui/tsconfig.app.json`
- Create: `webui/tsconfig.node.json`
- Create: `webui/components.json`
- Create: `webui/src/main.tsx`
- Create: `webui/src/App.tsx`
- Create: `webui/src/styles.css`

- [ ] **Step 1: Create package manifest**

Create `webui/package.json`:

```json
{
  "name": "sag-workbench",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "preview": "vite preview --host 127.0.0.1"
  },
  "dependencies": {
    "@radix-ui/react-dialog": "^1.1.15",
    "@radix-ui/react-tabs": "^1.1.13",
    "@xterm/addon-fit": "^0.10.0",
    "@xterm/xterm": "^5.5.0",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "lucide-react": "^0.468.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "tailwind-merge": "^2.5.5"
  },
  "devDependencies": {
    "@tailwindcss/vite": "^4.0.0",
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@types/node": "^22.10.0",
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "jsdom": "^25.0.1",
    "tailwindcss": "^4.0.0",
    "typescript": "^5.7.2",
    "vite": "^6.0.3",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: Create Vite config**

Create `webui/vite.config.ts`:

```ts
import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/sag/web/static",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
})
```

- [ ] **Step 3: Create TypeScript configs**

Create `webui/tsconfig.json`:

```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ],
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  }
}
```

Create `webui/tsconfig.app.json`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src"]
}
```

Create `webui/tsconfig.node.json`:

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 4: Create shadcn config and entry files**

Create `webui/components.json`:

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "new-york",
  "rsc": false,
  "tsx": true,
  "tailwind": {
    "config": "tailwind.config.ts",
    "css": "src/styles.css",
    "baseColor": "slate",
    "cssVariables": true,
    "prefix": ""
  },
  "aliases": {
    "components": "@/components",
    "utils": "@/lib/utils",
    "ui": "@/components/ui",
    "lib": "@/lib",
    "hooks": "@/hooks"
  },
  "iconLibrary": "lucide"
}
```

Create `webui/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>SAG Workbench</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

Create `webui/src/main.tsx`:

```tsx
import React from "react"
import ReactDOM from "react-dom/client"
import { App } from "./App"
import "./styles.css"

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
```

Create `webui/src/App.tsx`:

```tsx
export function App() {
  return (
    <div className="min-h-screen bg-[#fbfbfc] text-slate-900">
      <main className="mx-auto max-w-[1180px] px-8 py-7">
        <h1 className="text-[22px] font-semibold tracking-tight">SAG Workbench</h1>
      </main>
    </div>
  )
}
```

Create `webui/src/styles.css`:

```css
@import "tailwindcss";

:root {
  --primary: 217 91% 53%;
  --ring: 217 91% 53%;
  --radius: 0.5rem;
}

html {
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
}
```

- [ ] **Step 5: Install shadcn components and build frontend**

Run:

```bash
cd webui
npm install
npx shadcn@latest add button card tabs dialog textarea badge
npm run build
```

Expected: shadcn creates files under `webui/src/components/ui/`, and `npm run build` produces `src/sag/web/static/index.html` from the repo root.

- [ ] **Step 6: Commit**

```bash
git add webui/package.json webui/package-lock.json webui/index.html webui/vite.config.ts webui/tsconfig.json webui/tsconfig.app.json webui/tsconfig.node.json webui/components.json webui/src/main.tsx webui/src/App.tsx webui/src/styles.css webui/src/components/ui src/sag/web/static
git commit -m "Scaffold SAG Workbench frontend"
```

---

### Task 13: Frontend API Client And Common Components

**Files:**
- Create: `webui/src/api/types.ts`
- Create: `webui/src/api/client.ts`
- Create: `webui/src/lib/utils.ts`
- Create: `webui/src/components/common/status.ts`
- Create: `webui/src/components/common/Badge.tsx`
- Create: `webui/src/components/common/Card.tsx`
- Create: `webui/src/components/common/Button.tsx`
- Create: `webui/src/components/common/Tabs.tsx`
- Create: `webui/src/components/common/TestBar.tsx`
- Test: `webui/src/components/common/status.test.ts`

- [ ] **Step 1: Write status test**

Create `webui/src/components/common/status.test.ts`:

```ts
import { describe, expect, it } from "vitest"
import { statusMeta } from "./status"

describe("statusMeta", () => {
  it("matches the SAG demo status tones", () => {
    expect(statusMeta("success").tone).toBe("green")
    expect(statusMeta("partial").tone).toBe("amber")
    expect(statusMeta("running").tone).toBe("blue")
    expect(statusMeta("failed").tone).toBe("red")
    expect(statusMeta("unknown").tone).toBe("neutral")
  })
})
```

- [ ] **Step 2: Add frontend types and API client**

Create `webui/src/api/types.ts`:

```ts
export type Tone = "neutral" | "blue" | "green" | "red" | "amber"

export interface DockerSummary {
  status: string
  image?: string | null
  version?: string | null
  endpoint?: string | null
}

export interface TestSummary {
  state: string
  pass: number
  fail: number
  skip: number
  total: number
  note?: string
}

export interface BuildSummary {
  state: string
  tool: string
  time: string
  artifact?: string | null
  note: string
}

export interface WorkspaceSummary {
  id: string
  project: string
  container: string
  stack: string
  tag?: string | null
  release?: string | null
  commit?: string | null
  docker: DockerSummary
  task: string
  build: BuildSummary | string
  test: TestSummary
  report: string
  changed: number
  activeSession?: string | null
  latestSession?: string | null
  updated: string
}

export interface DashboardResponse {
  docker: DockerSummary
  workspaces: WorkspaceSummary[]
}

export interface EvidenceRecord {
  time: string
  status: string
  title: string
  detail: string
  ref: string
}

export interface EvidenceGroup {
  source: string
  status: string
  counts: string
  time: string
  summary: string
  records: EvidenceRecord[]
}

export interface ExecutionSessionDetail {
  id: string
  workspace: string
  title: string
  status: string
  entry: string
  start: string
  duration: string
  outcome: string
  build: BuildSummary
  test: TestSummary
  report: string
  reportDoc?: ReportDocument | null
  blocker?: { code: string; title: string; detail: string; hint: string } | null
  evidence: EvidenceGroup[]
  files?: FileChangeDigest | null
  context?: ContextMap | null
  logs: string[]
  partial?: boolean
}

export interface FileChangeDigest {
  snapshot: { base: string; head: string; mode: string }
  counts: { modified: number; added: number; deleted: number; renamed: number }
  items: Array<{ path: string; change: string; type: string; size: string; mtime: string; note: string }>
}

export interface ContextMap {
  trunk: { goal: string; state: string; progress: { done: number; total: number }; summary: string }
  tasks: Array<{ id: string; title: string; status: string; summary: string; refs: string[]; recovered: boolean }>
  activeBranch: { task: string; why: string; memory: string[]; lastRefs: Array<{ label: string; ref: string }>; pressure: number }
  debug: Record<string, unknown>
}

export interface ReportDocument {
  title: string
  path?: string | null
  generated: string
  blocks: Array<Record<string, unknown>>
}
```

Create `webui/src/api/client.ts`:

```ts
import type { DashboardResponse, ExecutionSessionDetail } from "./types"

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export function fetchDashboard(): Promise<DashboardResponse> {
  return getJson<DashboardResponse>("/api/workspaces")
}

export function fetchSession(sessionId: string): Promise<ExecutionSessionDetail> {
  return getJson<ExecutionSessionDetail>(`/api/sessions/${sessionId}`)
}

export async function submitTask(workspaceId: string, task: string, sourceSession?: string) {
  const response = await fetch(`/api/workspaces/${workspaceId}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, source_session: sourceSession ?? null }),
  })
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<{ workspace_id: string; session_id: string; status: string }>
}
```

- [ ] **Step 3: Add common component primitives**

Create `webui/src/components/common/status.ts`:

```ts
import type { Tone } from "@/api/types"

const toneByStatus: Record<string, Tone> = {
  success: "green",
  pass: "green",
  completed: "green",
  ready: "green",
  available: "green",
  running: "blue",
  connected: "blue",
  active: "blue",
  partial: "amber",
  stopped: "amber",
  exited: "red",
  failure: "red",
  failed: "red",
  fail: "red",
  blocked: "red",
}

export function statusMeta(status: string): { label: string; tone: Tone } {
  const normalized = status.toLowerCase()
  const label = normalized === "none" ? "—" : normalized.charAt(0).toUpperCase() + normalized.slice(1)
  return { label, tone: toneByStatus[normalized] ?? "neutral" }
}
```

Create `webui/src/lib/utils.ts`:

```ts
import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

Create `webui/src/components/common/Badge.tsx`, `Card.tsx`, `Button.tsx`, `Tabs.tsx`, and `TestBar.tsx` by wrapping the shadcn files generated in `webui/src/components/ui/` where a matching primitive exists, then porting the remaining demo-specific presentation from `docs/Setup Agent Web UI/src/ui.jsx` to TypeScript. Keep the demo's visual vocabulary: thin borders, `rounded-md` buttons, mono metadata, restrained blue accent, and non-nested cards.

- [ ] **Step 4: Run frontend tests**

Run:

```bash
cd webui
npm test
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/api/types.ts webui/src/api/client.ts webui/src/lib/utils.ts webui/src/components/common webui/src/components/common/status.test.ts
git commit -m "Add SAG Workbench frontend primitives"
```

---

### Task 14: Dashboard And App Shell

**Files:**
- Modify: `webui/src/App.tsx`
- Create: `webui/src/pages/Dashboard.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`

- [ ] **Step 1: Write Dashboard render test**

Create `webui/src/pages/Dashboard.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { Dashboard } from "./Dashboard"

const dashboard = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    {
      id: "sag-commons-cli",
      project: "apache/commons-cli",
      container: "sag-commons-cli",
      stack: "Java · Maven",
      docker: { status: "running" },
      task: "Build project and run full test suite",
      build: { state: "success", tool: "Maven", time: "47.2s", note: "" },
      test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
      report: "ready",
      changed: 7,
      activeSession: "CC-3",
      latestSession: "CC-3",
      updated: "just now",
    },
  ],
}

describe("Dashboard", () => {
  it("renders workspace status and task summary", () => {
    render(<Dashboard data={dashboard} onOpenWorkspace={() => {}} onOpenSession={() => {}} />)

    expect(screen.getByText("Workspaces")).toBeInTheDocument()
    expect(screen.getByText("apache/commons-cli")).toBeInTheDocument()
    expect(screen.getByText("Build project and run full test suite")).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Implement Dashboard by porting the demo component**

Create `webui/src/pages/Dashboard.tsx`:

```tsx
import { Activity, ArrowRight, Check, Clock, FileText, GitBranch, RefreshCw, X } from "lucide-react"
import type { DashboardResponse } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"

interface Props {
  data: DashboardResponse
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
}

export function Dashboard({ data, onOpenWorkspace, onOpenSession }: Props) {
  const running = data.workspaces.filter((w) => w.docker.status === "running").length
  const attention = data.workspaces.filter((w) => w.build === "failure" || w.test.state === "fail" || w.docker.status === "exited").length

  return (
    <div className="mx-auto max-w-[1180px] px-8 py-7">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">sag ui · local workbench</div>
          <h1 className="mt-1.5 text-[22px] font-semibold tracking-tight text-slate-900">Workspaces</h1>
          <p className="mt-1 text-[13px] text-slate-500">SAG-managed containers and their latest setup state.</p>
        </div>
        <Button variant="outline"><RefreshCw size={14} /> Refresh</Button>
      </div>

      <div className="mt-5 grid grid-cols-3 gap-3">
        <SummaryCard label="Workspaces" value={data.workspaces.length} sub="managed by SAG" />
        <SummaryCard label="Running" value={running} sub="active containers" icon={<Activity size={14} className="text-blue-500" />} />
        <SummaryCard label="Need attention" value={attention} sub="failed or exited" />
      </div>

      <Card className="mt-5 overflow-hidden">
        <div className="grid grid-cols-[1.6fr_0.9fr_1.5fr_0.9fr_1.1fr_0.7fr_0.6fr_72px] items-center gap-3 border-b border-slate-100 bg-slate-50/60 px-4 py-2.5">
          {["Project", "Container", "Current task", "Build", "Test", "Report", "Changed", ""].map((h) => (
            <div key={h} className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">{h}</div>
          ))}
        </div>
        {data.workspaces.map((w) => (
          <button key={w.id} onClick={() => onOpenWorkspace(w.id)}
            className="group grid w-full cursor-pointer grid-cols-[1.6fr_0.9fr_1.5fr_0.9fr_1.1fr_0.7fr_0.6fr_72px] items-center gap-3 border-b border-slate-100 px-4 py-3 text-left last:border-b-0 hover:bg-slate-50/70">
            <div className="flex min-w-0 items-center gap-2.5">
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-slate-50 text-slate-500"><GitBranch size={14} /></div>
              <div className="min-w-0">
                <div className="truncate text-[13px] font-medium text-slate-800 group-hover:text-[hsl(var(--primary))]">{w.project}</div>
                <div className="font-mono text-[10px] text-slate-400">{w.stack} · {w.commit ?? "unknown"} · {w.updated}</div>
              </div>
            </div>
            <StatusBadge status={w.docker.status} />
            <div className="min-w-0 truncate text-[12.5px] text-slate-600">{w.task}</div>
            <BuildCell build={typeof w.build === "string" ? w.build : w.build.state} />
            <TestBar pass={w.test.pass} fail={w.test.fail} total={w.test.total} />
            {w.report === "ready" ? <Badge tone="green">Ready</Badge> : <span className="text-[12px] text-slate-300">—</span>}
            <div className="font-mono text-[12px] text-slate-500">{w.changed}</div>
            <span className="flex justify-end text-slate-400"><ArrowRight size={15} /></span>
          </button>
        ))}
      </Card>
    </div>
  )
}

function SummaryCard({ label, value, sub, icon }: { label: string; value: number; sub: string; icon?: React.ReactNode }) {
  return (
    <Card className="px-4 py-3.5">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">{label}</div>
        {icon}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <span className="text-[26px] font-semibold tabular-nums text-slate-900">{value}</span>
        <span className="text-[12px] text-slate-400">{sub}</span>
      </div>
    </Card>
  )
}

function BuildCell({ build }: { build: string }) {
  if (build === "success") return <span className="inline-flex items-center gap-1.5 text-[12px] text-emerald-600"><Check size={14} /> Success</span>
  if (build === "failure") return <span className="inline-flex items-center gap-1.5 text-[12px] text-red-600"><X size={14} /> Failure</span>
  if (build === "pending") return <span className="inline-flex items-center gap-1.5 text-[12px] text-slate-400"><Clock size={13} /> Pending</span>
  return <span className="text-slate-300">—</span>
}
```

- [ ] **Step 3: Connect App shell to API**

Modify `webui/src/App.tsx`:

```tsx
import { useEffect, useState } from "react"
import { fetchDashboard } from "@/api/client"
import type { DashboardResponse } from "@/api/types"
import { Dashboard } from "@/pages/Dashboard"

type Route = { view: "dashboard" } | { view: "workspace"; workspaceId: string } | { view: "session"; sessionId: string }

export function App() {
  const [route, setRoute] = useState<Route>({ view: "dashboard" })
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchDashboard().then(setDashboard).catch((err) => setError(String(err)))
  }, [])

  return (
    <div className="min-h-screen bg-[#fbfbfc] text-slate-900">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/85 backdrop-blur">
        <div className="mx-auto flex h-12 max-w-[1180px] items-center gap-3 px-8">
          <button onClick={() => setRoute({ view: "dashboard" })} className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded bg-[hsl(var(--primary))] font-mono text-[11px] font-bold text-white">S</span>
            <span className="font-mono text-[12px] font-semibold tracking-tight text-slate-800">sag</span>
          </button>
        </div>
      </header>
      {error && <main className="mx-auto max-w-[1180px] px-8 py-7 text-sm text-red-600">{error}</main>}
      {!error && !dashboard && <main className="mx-auto max-w-[1180px] px-8 py-7 text-sm text-slate-500">Loading workspaces...</main>}
      {!error && dashboard && route.view === "dashboard" && (
        <Dashboard data={dashboard} onOpenWorkspace={(workspaceId) => setRoute({ view: "workspace", workspaceId })} onOpenSession={(_, sessionId) => setRoute({ view: "session", sessionId })} />
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run frontend tests and build**

Run:

```bash
cd webui
npm test
npm run build
```

Expected: PASS and built assets update `src/sag/web/static`.

- [ ] **Step 5: Commit**

```bash
git add webui/src/App.tsx webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx src/sag/web/static
git commit -m "Add SAG Workbench dashboard"
```

---

### Task 15: Workspace And Session Pages

**Files:**
- Create: `webui/src/pages/Workspace.tsx`
- Create: `webui/src/pages/SessionDetail.tsx`
- Create: `webui/src/components/session/BuildCard.tsx`
- Create: `webui/src/components/session/TestCard.tsx`
- Create: `webui/src/components/session/EvidenceTimeline.tsx`
- Create: `webui/src/components/session/ContextMap.tsx`
- Create: `webui/src/components/session/FilesDigest.tsx`
- Create: `webui/src/components/session/ReportDoc.tsx`
- Create: `webui/src/components/session/LogsView.tsx`
- Modify: `webui/src/App.tsx`
- Test: `webui/src/pages/SessionDetail.test.tsx`

- [ ] **Step 1: Write Session Detail render test**

Create `webui/src/pages/SessionDetail.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { SessionDetail } from "./SessionDetail"

const detail = {
  id: "CC-3",
  workspace: "sag-commons-cli",
  title: "Build project and execute full test suite",
  status: "running",
  entry: "CLI",
  start: "02:14:08",
  duration: "running · 2m 11s",
  outcome: "Build succeeds and tests are partial.",
  build: { state: "success", tool: "Maven 3.9.6", time: "47.2s", artifact: "target/app.jar", note: "clean package" },
  test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320, note: "HelpFormatter failures" },
  report: "ready",
  reportDoc: { title: "setup-report.md", generated: "now", blocks: [{ type: "p", text: "Project builds." }] },
  evidence: [{ source: "Test validator", status: "partial", counts: "312 / 320", time: "02:16", summary: "8 failed", records: [] }],
  files: null,
  context: null,
  logs: [],
}

describe("SessionDetail", () => {
  it("renders result-first status", () => {
    render(<SessionDetail detail={detail} onBack={() => {}} onNewTask={() => {}} />)

    expect(screen.getByText("Outcome")).toBeInTheDocument()
    expect(screen.getByText("Build project and execute full test suite")).toBeInTheDocument()
    expect(screen.getByText("312")).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Port session components from the demo**

Create the session components by converting `docs/Setup Agent Web UI/src/SessionDetail.jsx` into focused TypeScript modules:

- `BuildCard.tsx`: accepts `{ build: BuildSummary }`.
- `TestCard.tsx`: accepts `{ test: TestSummary }`.
- `EvidenceTimeline.tsx`: accepts `{ groups: EvidenceGroup[]; preview?: boolean }`.
- `ContextMap.tsx`: accepts `{ ctx: ContextMap; preview?: boolean }`.
- `FilesDigest.tsx`: accepts `{ digest?: FileChangeDigest | null; preview?: boolean }`.
- `ReportDoc.tsx`: accepts `{ doc?: ReportDocument | null }`.
- `LogsView.tsx`: accepts `{ logs: string[] }`.

Use this shared import style in every file:

```tsx
import type { BuildSummary, ContextMap, EvidenceGroup, FileChangeDigest, ReportDocument, TestSummary } from "@/api/types"
import { Card, CardHead } from "@/components/common/Card"
import { StatusBadge } from "@/components/common/Badge"
```

- [ ] **Step 3: Implement Session Detail**

Create `webui/src/pages/SessionDetail.tsx` by porting the demo's tab layout. Preserve default tab `Status`, and keep `New task from this` as a button that calls `onNewTask(detail.id)` instead of mutating the session.

Use this component signature:

```tsx
import { useState } from "react"
import type { ExecutionSessionDetail } from "@/api/types"

interface Props {
  detail: ExecutionSessionDetail
  onBack: () => void
  onNewTask: (sourceSession: string) => void
}

export function SessionDetail({ detail, onBack, onNewTask }: Props) {
  const [tab, setTab] = useState("Status")
  // Port tab content from the demo here.
}
```

- [ ] **Step 4: Implement Workspace shell**

Create `webui/src/pages/Workspace.tsx` by porting `docs/Setup Agent Web UI/src/Workspace.jsx`. The first version should:

- receive `workspace`, `latest`, `sessions`, and callbacks as props;
- show tabs `Overview`, `Sessions`, `Terminal`, `Settings`;
- show `New task` as a workspace-level modal;
- render a static inactive terminal panel until Task 16 adds xterm;
- route `Open session detail` through `onOpenSession(latest.id)`.

- [ ] **Step 5: Connect routes in App**

Modify `webui/src/App.tsx` so workspace and session routes fetch detail:

```tsx
// Add imports:
import { fetchSession } from "@/api/client"
import type { ExecutionSessionDetail } from "@/api/types"
import { Workspace } from "@/pages/Workspace"
import { SessionDetail } from "@/pages/SessionDetail"

// Add state:
const [sessionDetail, setSessionDetail] = useState<ExecutionSessionDetail | null>(null)

// When opening a session:
function openSession(sessionId: string) {
  setRoute({ view: "session", sessionId })
  fetchSession(sessionId).then(setSessionDetail).catch((err) => setError(String(err)))
}
```

- [ ] **Step 6: Run frontend tests and build**

Run:

```bash
cd webui
npm test
npm run build
```

Expected: PASS and built assets update `src/sag/web/static`.

- [ ] **Step 7: Commit**

```bash
git add webui/src/pages/Workspace.tsx webui/src/pages/SessionDetail.tsx webui/src/components/session webui/src/App.tsx webui/src/pages/SessionDetail.test.tsx src/sag/web/static
git commit -m "Add workspace and session detail views"
```

---

### Task 16: Terminal WebSocket And xterm Panel

**Files:**
- Create: `src/sag/web/terminal.py`
- Modify: `src/sag/web/app.py`
- Create: `webui/src/components/terminal/TerminalPanel.tsx`
- Modify: `webui/src/pages/Workspace.tsx`
- Test: `tests/test_web_terminal.py`

- [ ] **Step 1: Write terminal adapter unit test**

Create `tests/test_web_terminal.py`:

```python
from sag.web.terminal import build_exec_options


def test_build_exec_options_request_interactive_tty():
    assert build_exec_options("/bin/bash") == {
        "cmd": "/bin/bash",
        "stdin": True,
        "tty": True,
    }
```

- [ ] **Step 2: Run backend terminal test to verify failure**

Run:

```bash
uv run pytest tests/test_web_terminal.py -v
```

Expected: FAIL because `sag.web.terminal` does not exist.

- [ ] **Step 3: Implement terminal adapter skeleton**

Create `src/sag/web/terminal.py`:

```python
"""Docker exec terminal bridge for SAG Workbench."""

from __future__ import annotations

import asyncio

import docker


def build_exec_options(shell: str = "/bin/bash") -> dict[str, object]:
    return {"cmd": shell, "stdin": True, "tty": True}


class TerminalAdapter:
    def __init__(self, client=None):
        self.client = client or docker.from_env()

    def open_socket(self, container: str, shell: str = "/bin/bash"):
        exec_info = self.client.api.exec_create(container, **build_exec_options(shell))
        return self.client.api.exec_start(exec_info["Id"], tty=True, socket=True)

    async def recv(self, socket, size: int = 4096) -> bytes:
        return await asyncio.to_thread(socket.recv, size)

    async def send(self, socket, data: bytes) -> None:
        await asyncio.to_thread(socket.send, data)
```

- [ ] **Step 4: Add WebSocket route**

Modify `src/sag/web/app.py`:

```python
from fastapi import WebSocket, WebSocketDisconnect
from sag.web.terminal import TerminalAdapter
```

Inside `create_app`:

```python
    terminal_adapter = TerminalAdapter()

    @app.websocket("/api/workspaces/{workspace_id}/terminal")
    async def terminal_socket(websocket: WebSocket, workspace_id: str):
        await websocket.accept()
        socket = terminal_adapter.open_socket(workspace_id)

        async def pump_output():
            while True:
                chunk = await terminal_adapter.recv(socket)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)

        output_task = None
        try:
            output_task = __import__("asyncio").create_task(pump_output())
            while True:
                data = await websocket.receive_bytes()
                await terminal_adapter.send(socket, data)
        except WebSocketDisconnect:
            pass
        finally:
            if output_task:
                output_task.cancel()
            socket.close()
```

- [ ] **Step 5: Build xterm panel**

Create `webui/src/components/terminal/TerminalPanel.tsx`:

```tsx
import { useEffect, useRef } from "react"
import { Terminal } from "@xterm/xterm"
import { FitAddon } from "@xterm/addon-fit"
import "@xterm/xterm/css/xterm.css"

export function TerminalPanel({ workspaceId }: { workspaceId: string }) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const term = new Terminal({ cursorBlink: true, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13 })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(ref.current)
    fit.fit()

    const scheme = window.location.protocol === "https:" ? "wss" : "ws"
    const socket = new WebSocket(`${scheme}://${window.location.host}/api/workspaces/${workspaceId}/terminal`)
    socket.binaryType = "arraybuffer"
    socket.onmessage = (event) => {
      const bytes = event.data instanceof ArrayBuffer ? new Uint8Array(event.data) : new TextEncoder().encode(String(event.data))
      term.write(bytes)
    }
    const disposable = term.onData((data) => socket.readyState === WebSocket.OPEN && socket.send(new TextEncoder().encode(data)))

    const resize = () => fit.fit()
    window.addEventListener("resize", resize)
    return () => {
      window.removeEventListener("resize", resize)
      disposable.dispose()
      socket.close()
      term.dispose()
    }
  }, [workspaceId])

  return <div className="h-[520px] overflow-hidden rounded-lg border border-slate-800 bg-[#0d1117] p-2" ref={ref} />
}
```

- [ ] **Step 6: Replace Workspace static terminal panel**

Modify `webui/src/pages/Workspace.tsx`:

```tsx
import { TerminalPanel } from "@/components/terminal/TerminalPanel"
```

Render:

```tsx
{tab === "Terminal" && <TerminalPanel workspaceId={workspace.id} />}
```

- [ ] **Step 7: Run backend tests and frontend build**

Run:

```bash
uv run pytest tests/test_web_terminal.py tests/test_web_api.py -v
cd webui
npm run build
```

Expected: PASS and built assets update `src/sag/web/static`.

- [ ] **Step 8: Commit**

```bash
git add src/sag/web/terminal.py src/sag/web/app.py tests/test_web_terminal.py webui/src/components/terminal/TerminalPanel.tsx webui/src/pages/Workspace.tsx src/sag/web/static
git commit -m "Add web terminal bridge"
```

---

### Task 17: Static Asset Serving

**Files:**
- Modify: `src/sag/web/server.py`
- Modify: `src/sag/web/app.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Add static serving test**

Append to `tests/test_web_api.py`:

```python
def test_static_index_served_when_static_dir_exists(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    monkeypatch.setattr("sag.web.app.STATIC_DIR", static_dir)

    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "root" in response.text
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_web_api.py::test_static_index_served_when_static_dir_exists -v
```

Expected: FAIL because static frontend is not mounted.

- [ ] **Step 3: Mount static files and index fallback**

Modify `src/sag/web/app.py`:

```python
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sag.web.paths import STATIC_DIR
```

Inside `create_app`, before `return app`:

```python
    if STATIC_DIR.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")
```

- [ ] **Step 4: Run static serving test**

Run:

```bash
uv run pytest tests/test_web_api.py::test_static_index_served_when_static_dir_exists -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/app.py tests/test_web_api.py
git commit -m "Serve SAG Workbench static assets"
```

---

### Task 18: End-To-End Verification

**Files:**
- Create: `webui/e2e/sag-workbench.spec.ts`
- Modify: `webui/package.json`

- [ ] **Step 1: Add Playwright smoke test script**

Modify `webui/package.json` scripts:

```json
{
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "e2e": "playwright test",
    "preview": "vite preview --host 127.0.0.1"
  }
}
```

Add dev dependency:

```json
"@playwright/test": "^1.49.0"
```

- [ ] **Step 2: Create browser smoke test**

Create `webui/e2e/sag-workbench.spec.ts`:

```ts
import { expect, test } from "@playwright/test"

test("dashboard shows demo workspace", async ({ page }) => {
  await page.goto("http://127.0.0.1:8765")
  await expect(page.getByText("Workspaces")).toBeVisible()
  await expect(page.getByText("apache/commons-cli")).toBeVisible()
})
```

- [ ] **Step 3: Run full backend and frontend verification**

Run:

```bash
uv run pytest tests/test_web_*.py -v
cd webui
npm test
npm run build
cd ..
uv run sag ui --host 127.0.0.1 --port 8765 --demo
```

In a second terminal, run:

```bash
cd webui
npm run e2e
```

Expected: pytest PASS, Vitest PASS, frontend build PASS, Playwright smoke PASS.

- [ ] **Step 4: Commit**

```bash
git add webui/package.json webui/package-lock.json webui/e2e/sag-workbench.spec.ts src/sag/web/static
git commit -m "Add SAG Workbench browser smoke test"
```

---

## Final Verification

Run from the repository root:

```bash
uv run pytest tests/test_web_*.py tests/test_import_smoke.py -v
cd webui
npm test
npm run build
cd ..
uv run sag ui --host 127.0.0.1 --port 8765 --demo
```

Expected results:

- Backend web tests pass.
- Existing import smoke test still passes.
- Frontend unit tests pass.
- Frontend build refreshes `src/sag/web/static`.
- `uv run sag ui --host 127.0.0.1 --port 8765 --demo` serves the SAG Workbench.
- Dashboard shows `apache/commons-cli`.
- Workspace Overview shows build/test/report/evidence/file/context previews.
- Session Detail shows Status/Evidence/Context/Files/Report/Logs tabs.
- Terminal tab attempts WebSocket connection and isolates terminal failure from session state if Docker is unavailable.

## Review Checklist

- The UI is not chat-first.
- Workspace-level New Task creates a new execution/session.
- Session Detail does not mutate the current session into a continuation.
- Terminal is independent from sessions.
- File changes are represented through FileChangeDigest.
- Evidence Timeline groups trusted evidence by source.
- Context Map shows trunk/branch semantics instead of a file tree.
- Frontend renders backend read models and does not parse logs.
- Commit messages contain no Co-Authorship or authorship trailer.
