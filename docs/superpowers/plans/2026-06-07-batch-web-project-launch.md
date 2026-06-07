# Batch Web Project Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users launch many `sag project` setup jobs from the Web UI in one batch, with a SQLite-persisted queue, CPU-aware concurrency, isolated CLI subprocesses, and dashboard queue visibility.

**Architecture:** A new backend stack of four deep modules under `src/sag/web/` — `project_cli.py` (command builder), `launch_queue.py` (SQLite store), `launch_runner.py` (scheduler thread + subprocess monitors), `launch_service.py` (facade the FastAPI handlers call). Two new endpoints (`POST /api/project-launches/batch`, `GET /api/project-launches`) wired into `app.py` with scheduler start/stop in the lifespan. The React frontend adds a table-first "Launch setups" dialog, a queue panel on the dashboard, and a temporary highlight for newly launched workspaces.

**Tech Stack:** Python 3.10+, FastAPI + Pydantic v2, stdlib `sqlite3`/`subprocess`/`threading`, pytest; React 18 + TypeScript + Tailwind 4 + Radix dialog, Vite, vitest + React Testing Library.

**Spec:** `docs/superpowers/specs/2026-06-07-batch-web-project-launch-design.md`

---

## Context for the implementer (read first)

Facts about this codebase you need — all verified:

- **Run backend tests:** `uv run pytest tests/<file>.py -v` from the repo root (the project uses `uv`; `[tool.pytest.ini_options] pythonpath = ["src"]` is configured).
- **Run frontend tests:** `cd webui && npx vitest run <path>` (or `npm test` for the whole suite). Build with `cd webui && npm run build`, which emits bundled assets into `src/sag/web/static` (these built assets are **git-tracked** and must be committed when the frontend changes).
- **CLI entry:** `src/sag/main.py` defines a Click group `cli`; `pyproject.toml` has `sag = "sag.main:cli"`. `main.py` ends with `if __name__ == "__main__": cli()`, so **`<sys.executable> -m sag.main project ...` works** and enters the same Click path as `sag project`.
- **`sag project` options** (`src/sag/main.py:320-337`): argument `repo_url`; `--name` (str), `--goal` (str), `--record` (flag), `--ui` (flag), `--ref` (str, param name `project_ref`). Exit code 0 on success, 1 on failure (`sys.exit(1)` at `main.py:430-437`). Note: when the container already exists the CLI prints a warning and returns **exit 0** — that is why the Web precheck matters for UX, and why the spec says the CLI re-check is the final authority.
- **Workspace naming** (`src/sag/main.py:357-369`): `project_name = extract_project_name_from_url(repo_url)` (from `src/sag/utils/git_utils.py:7`, raises `ValueError` on un-derivable URLs); `docker_label = name or project_name`; container name = `f"sag-{docker_label}"`.
- **Conflict check the CLI uses** (`src/sag/main.py:387-397`): `DockerOrchestrator(project_name=docker_label).container_exists()` (`src/sag/docker_orch/orch.py:205-215`). **Warning:** `DockerOrchestrator.__init__` calls `docker.from_env()` and `client.ping()` and **raises** if Docker is unreachable — the Web precheck must wrap this in try/except and fail open (treat as "no conflict") because the CLI subprocess re-checks anyway.
- **Web app factory:** `src/sag/web/app.py` — `create_app(read_model, task_runner, terminal_adapter, static_dir)` returns a FastAPI app; dependencies are injected as keyword args with `None` defaults and real defaults constructed inside (follow this pattern for the new `launch_service` param). Lifespan is an `@contextlib.asynccontextmanager` that cleans up the terminal bridge with `asyncio.to_thread`.
- **Existing request-validation pattern:** `TaskRequest` in `src/sag/web/task_runner.py:15-17` uses `Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]`; FastAPI turns Pydantic violations into HTTP 422 automatically.
- **Backend web tests:** `tests/test_web_api.py` builds apps via `create_app(ReadModelBuilder(demo_mode=True), ...)` + `fastapi.testclient.TestClient` and injects fakes (see `FakeTaskRunner` there). `TestClient(app)` used without a `with` block does **not** run the lifespan — so route tests never start the real scheduler.
- **Logs convention:** everything lives under a `logs/` directory relative to the CWD where `sag` was started (`src/sag/config/logger.py:11-32` uses `Path("logs")`). The queue DB goes to `logs/launch_queue.sqlite3` and process logs to `logs/project_launches/<batch_id>/<launch_id>.log`, both relative paths, same convention.
- **Frontend layout:** source in `webui/src/`; API client `webui/src/api/client.ts` (plain `fetch` wrappers); types in `webui/src/api/types.ts` (backend JSON for the new endpoints is snake_case — keep snake_case in these types, like the existing `SubmitTaskResponse`); dashboard page `webui/src/pages/Dashboard.tsx`; app shell + polling `webui/src/App.tsx` (5s `setInterval` calling `loadDashboard`); reusable `Dialog*` components in `webui/src/components/ui/dialog.tsx`; `Button`/`Badge`/`StatusBadge`/`Card`/`CardHead` in `webui/src/components/common/`; icons from `lucide-react`; styling is Tailwind utility classes (low-saturation tones like `border-blue-200 bg-blue-50` are the house style for highlights).
- **Do not touch** the context-map work: `src/sag/web/context_map.py`, `src/sag/web/session_registry.py`, `webui/src/pages/SessionDetail.tsx`, `webui/src/components/session/ContextMap.tsx`, and their tests.
- **Commits:** short imperative subject lines matching repo history (e.g. "Add project CLI command builder"). Do **not** add a `Co-Authored-By` trailer.

## File structure

New files:

| File | Responsibility |
|---|---|
| `src/sag/web/project_cli.py` | `ProjectCliCommand` — build the exact `sag project ...` argv. Pure, no I/O. |
| `src/sag/web/launch_queue.py` | `LaunchQueueStore` + `LaunchBatch`/`LaunchItem` records — all SQLite persistence, atomic claiming, status transitions, queue reads. Nothing else knows SQL. |
| `src/sag/web/launch_runner.py` | `LaunchScheduler` — worker thread, subprocess spawning with redirected logs, exit monitoring, stale-row reconcile. Nothing else knows `subprocess`. |
| `src/sag/web/launch_service.py` | `LaunchService` + Pydantic request models — validation, conflict precheck, batch submission, queue-state reads. The only module `app.py` imports. |
| `tests/test_web_project_cli.py` | Command-builder tests. |
| `tests/test_web_launch_queue.py` | Store tests. |
| `tests/test_web_launch_runner.py` | Scheduler tests (fake spawn). |
| `tests/test_web_launch_service.py` | Service tests (fake store/scheduler/docker). |
| `webui/src/components/launch/launchRows.ts` | Row draft type + multi-line paste parser. Pure. |
| `webui/src/components/launch/launchRows.test.ts` | Paste parser tests. |
| `webui/src/components/launch/LaunchSetupsDialog.tsx` | The table-first launch dialog. |
| `webui/src/components/launch/LaunchSetupsDialog.test.tsx` | Dialog tests. |
| `webui/src/components/launch/LaunchQueuePanel.tsx` | Dashboard queue panel (counts, active batch, recent failures). |
| `webui/src/components/launch/LaunchQueuePanel.test.tsx` | Panel tests. |

Modified files:

| File | Change |
|---|---|
| `src/sag/web/app.py` | Two new routes, `launch_service` param, lifespan start/stop. |
| `tests/test_web_api.py` | Endpoint tests with a `FakeLaunchService`. |
| `webui/src/api/types.ts` | Launch request/response/queue types. |
| `webui/src/api/client.ts` | `submitProjectBatch`, `fetchLaunchQueue`. |
| `webui/src/api/client.test.ts` | Tests for the two new client functions. |
| `webui/src/pages/Dashboard.tsx` | "Launch setups" button, queue panel slot, row/card highlight prop. |
| `webui/src/pages/Dashboard.test.tsx` | Tests for button, panel, highlight. |
| `webui/src/App.tsx` | Dialog state, queue polling, highlight timers, result notice. |
| `src/sag/web/static/*` | Rebuilt bundle (final task). |

Status model (used everywhere, defined by the spec): items are `queued → launching → running → completed | failed`; batches are `running → completed | failed`.

---

### Task 1: `ProjectCliCommand` builder

**Files:**
- Create: `src/sag/web/project_cli.py`
- Test: `tests/test_web_project_cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_project_cli.py`:

```python
"""Tests for the CLI-equivalent project command builder."""

import sys

from sag.web.project_cli import ProjectCliCommand

REPO = "https://github.com/apache/commons-cli.git"


def test_bare_repo_builds_minimal_project_command():
    command = ProjectCliCommand(repo_url=REPO)

    assert command.project_args() == ["project", REPO]


def test_name_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, name="commons-cli-111")

    assert command.project_args() == ["project", REPO, "--name", "commons-cli-111"]


def test_ref_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, ref="rel/commons-cli-1.11.0")

    assert command.project_args() == ["project", REPO, "--ref", "rel/commons-cli-1.11.0"]


def test_goal_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, goal="Setup and verify Apache Commons CLI")

    assert command.project_args() == [
        "project",
        REPO,
        "--goal",
        "Setup and verify Apache Commons CLI",
    ]


def test_record_flag_is_included():
    command = ProjectCliCommand(repo_url=REPO, record=True)

    assert command.project_args() == ["project", REPO, "--record"]


def test_all_options_together_match_manual_sag_project_invocation():
    command = ProjectCliCommand(
        repo_url=REPO,
        name="commons-cli-111",
        ref="rel/commons-cli-1.11.0",
        goal="Setup and verify Apache Commons CLI",
        record=True,
    )

    # Equivalent to:
    # sag project <repo> --name commons-cli-111 --ref rel/commons-cli-1.11.0 \
    #   --goal "Setup and verify Apache Commons CLI" --record
    assert command.project_args() == [
        "project",
        REPO,
        "--name",
        "commons-cli-111",
        "--ref",
        "rel/commons-cli-1.11.0",
        "--goal",
        "Setup and verify Apache Commons CLI",
        "--record",
    ]


def test_argv_runs_the_cli_module_through_the_active_python():
    command = ProjectCliCommand(repo_url=REPO, ref="v1.0")

    assert command.argv() == [
        sys.executable,
        "-m",
        "sag.main",
        "project",
        REPO,
        "--ref",
        "v1.0",
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_project_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sag.web.project_cli'`

- [ ] **Step 3: Write the implementation**

Create `src/sag/web/project_cli.py`:

```python
"""Build CLI-equivalent ``sag project`` commands for Web-triggered launches."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectCliCommand:
    """The exact ``sag project ...`` invocation for one launch row."""

    repo_url: str
    name: str | None = None
    ref: str | None = None
    goal: str | None = None
    record: bool = False

    def project_args(self) -> list[str]:
        """Arguments exactly as a user would type them after ``sag``."""

        args = ["project", self.repo_url]
        if self.name:
            args.extend(["--name", self.name])
        if self.ref:
            args.extend(["--ref", self.ref])
        if self.goal:
            args.extend(["--goal", self.goal])
        if self.record:
            args.append("--record")
        return args

    def argv(self) -> list[str]:
        """Full subprocess argv through the active Python environment."""

        return [sys.executable, "-m", "sag.main", *self.project_args()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_project_cli.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/project_cli.py tests/test_web_project_cli.py
git commit -m "Add project CLI command builder"
```

### Task 2: `LaunchQueueStore` — schema, records, enqueue, persistence

**Files:**
- Create: `src/sag/web/launch_queue.py`
- Test: `tests/test_web_launch_queue.py`

The store opens a short-lived SQLite connection per operation (WAL mode, `isolation_level=None` for explicit transaction control). That makes it safe to call from FastAPI handler threads and the scheduler thread without shared-connection locking. The DB file and parent dir are created lazily on first use, so constructing a store (e.g. inside `create_app`) touches nothing on disk.

Timestamps are passed in as ISO strings by callers (never generated inside the store) so tests are deterministic.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_launch_queue.py`:

```python
"""Tests for the SQLite-backed launch queue store."""

from sag.web.launch_queue import LaunchBatch, LaunchItem, LaunchQueueStore

NOW = "2026-06-07T10:00:00"
LATER = "2026-06-07T10:05:00"


def make_store(tmp_path):
    return LaunchQueueStore(tmp_path / "launch_queue.sqlite3")


def make_batch(batch_id="BATCH-20260607-abcdef", concurrency=2, total=1, accepted=1):
    return LaunchBatch(
        id=batch_id,
        created_at=NOW,
        concurrency=concurrency,
        status="running",
        total=total,
        accepted=accepted,
        rejected=total - accepted,
    )


def make_item(item_id, batch_id="BATCH-20260607-abcdef", row_index=0, **overrides):
    fields = dict(
        id=item_id,
        batch_id=batch_id,
        row_index=row_index,
        repo_url="https://github.com/apache/commons-cli.git",
        name=None,
        ref=None,
        goal=None,
        record=False,
        project_name="commons-cli",
        docker_label="commons-cli",
        workspace_id="sag-commons-cli",
        command=["python", "-m", "sag.main", "project", "https://github.com/apache/commons-cli.git"],
        process_log=f"logs/project_launches/{batch_id}/{item_id}.log",
        created_at=NOW,
    )
    fields.update(overrides)
    return LaunchItem(**fields)


def test_enqueued_items_persist_across_store_instances(tmp_path):
    db_path = tmp_path / "launch_queue.sqlite3"
    first = LaunchQueueStore(db_path)
    first.enqueue_batch(
        make_batch(),
        [make_item("LAUNCH-11111111", ref="rel/commons-cli-1.11.0")],
    )

    second = LaunchQueueStore(db_path)
    batches = second.list_batches()

    assert len(batches) == 1
    assert batches[0]["id"] == "BATCH-20260607-abcdef"
    assert batches[0]["status"] == "running"
    assert batches[0]["concurrency"] == 2
    assert batches[0]["created"] == NOW
    item = batches[0]["items"][0]
    assert item["id"] == "LAUNCH-11111111"
    assert item["row_index"] == 0
    assert item["repo_url"] == "https://github.com/apache/commons-cli.git"
    assert item["workspace_id"] == "sag-commons-cli"
    assert item["ref"] == "rel/commons-cli-1.11.0"
    assert item["status"] == "queued"
    assert item["pid"] is None
    assert item["exit_code"] is None
    assert item["error"] is None
    assert item["process_log"] == "logs/project_launches/BATCH-20260607-abcdef/LAUNCH-11111111.log"


def test_list_batches_orders_newest_first_and_items_by_row_index(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=2, accepted=2),
        [
            make_item("LAUNCH-00000002", "BATCH-20260607-aaaaaa", row_index=1),
            make_item("LAUNCH-00000001", "BATCH-20260607-aaaaaa", row_index=0),
        ],
    )
    later_batch = LaunchBatch(
        id="BATCH-20260607-bbbbbb",
        created_at=LATER,
        concurrency=1,
        status="running",
        total=1,
        accepted=1,
        rejected=0,
    )
    store.enqueue_batch(later_batch, [make_item("LAUNCH-00000003", "BATCH-20260607-bbbbbb")])

    batches = store.list_batches()

    assert [batch["id"] for batch in batches] == [
        "BATCH-20260607-bbbbbb",
        "BATCH-20260607-aaaaaa",
    ]
    assert [item["id"] for item in batches[1]["items"]] == [
        "LAUNCH-00000001",
        "LAUNCH-00000002",
    ]


def test_summary_counts_zero_for_empty_store(tmp_path):
    store = make_store(tmp_path)

    assert store.summary_counts() == {
        "queued": 0,
        "launching": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_launch_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sag.web.launch_queue'`

- [ ] **Step 3: Write the implementation**

Create `src/sag/web/launch_queue.py`:

```python
"""SQLite persistence for Web-triggered project launch batches."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

ACTIVE_STATUSES = ("launching", "running")
ALL_STATUSES = ("queued", "launching", "running", "completed", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS launch_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    rejected INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS launch_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES launch_batches(id),
    row_index INTEGER NOT NULL,
    repo_url TEXT NOT NULL,
    name TEXT,
    ref TEXT,
    goal TEXT,
    record INTEGER NOT NULL DEFAULT 0,
    project_name TEXT NOT NULL,
    docker_label TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    exit_code INTEGER,
    error TEXT,
    command_json TEXT NOT NULL,
    process_log TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_launch_items_status ON launch_items(status);
CREATE INDEX IF NOT EXISTS idx_launch_items_batch ON launch_items(batch_id);
"""


@dataclass(frozen=True)
class LaunchBatch:
    id: str
    created_at: str
    concurrency: int
    status: str = "running"
    total: int = 0
    accepted: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class LaunchItem:
    id: str
    batch_id: str
    row_index: int
    repo_url: str
    project_name: str
    docker_label: str
    workspace_id: str
    command: list[str]
    process_log: str
    created_at: str
    name: str | None = None
    ref: str | None = None
    goal: str | None = None
    record: bool = False
    status: str = "queued"
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class LaunchQueueStore:
    """All SQLite access for the launch queue lives here."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        return conn

    @contextlib.contextmanager
    def _transaction(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def enqueue_batch(self, batch: LaunchBatch, items: list[LaunchItem]) -> None:
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                conn.execute(
                    "INSERT INTO launch_batches"
                    " (id, created_at, concurrency, status, total, accepted, rejected)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch.id,
                        batch.created_at,
                        batch.concurrency,
                        batch.status,
                        batch.total,
                        batch.accepted,
                        batch.rejected,
                    ),
                )
                for item in items:
                    conn.execute(
                        "INSERT INTO launch_items ("
                        " id, batch_id, row_index, repo_url, name, ref, goal, record,"
                        " project_name, docker_label, workspace_id, status, pid, exit_code,"
                        " error, command_json, process_log, created_at, started_at, finished_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.id,
                            item.batch_id,
                            item.row_index,
                            item.repo_url,
                            item.name,
                            item.ref,
                            item.goal,
                            int(item.record),
                            item.project_name,
                            item.docker_label,
                            item.workspace_id,
                            item.status,
                            item.pid,
                            item.exit_code,
                            item.error,
                            json.dumps(item.command),
                            item.process_log,
                            item.created_at,
                            item.started_at,
                            item.finished_at,
                        ),
                    )

    def summary_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ALL_STATUSES}
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM launch_items GROUP BY status"
            )
            for row in rows:
                if row["status"] in counts:
                    counts[row["status"]] = row["n"]
        return counts

    def list_batches(self) -> list[dict]:
        with contextlib.closing(self._connect()) as conn:
            batches: list[dict] = []
            batch_rows = conn.execute(
                "SELECT * FROM launch_batches ORDER BY created_at DESC, id DESC"
            ).fetchall()
            for batch in batch_rows:
                item_rows = conn.execute(
                    "SELECT * FROM launch_items WHERE batch_id = ? ORDER BY row_index",
                    (batch["id"],),
                ).fetchall()
                batches.append(
                    {
                        "id": batch["id"],
                        "status": batch["status"],
                        "concurrency": batch["concurrency"],
                        "created": batch["created_at"],
                        "items": [_item_payload(row) for row in item_rows],
                    }
                )
            return batches


def _item_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "row_index": row["row_index"],
        "repo_url": row["repo_url"],
        "workspace_id": row["workspace_id"],
        "ref": row["ref"],
        "status": row["status"],
        "pid": row["pid"],
        "exit_code": row["exit_code"],
        "error": row["error"],
        "process_log": row["process_log"],
    }


def _item_from_row(row: sqlite3.Row) -> LaunchItem:
    return LaunchItem(
        id=row["id"],
        batch_id=row["batch_id"],
        row_index=row["row_index"],
        repo_url=row["repo_url"],
        name=row["name"],
        ref=row["ref"],
        goal=row["goal"],
        record=bool(row["record"]),
        project_name=row["project_name"],
        docker_label=row["docker_label"],
        workspace_id=row["workspace_id"],
        command=json.loads(row["command_json"]),
        process_log=row["process_log"],
        created_at=row["created_at"],
        status=row["status"],
        pid=row["pid"],
        exit_code=row["exit_code"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
```

(`replace` and `_item_from_row` are used by Task 3 — `replace` stays unused for now; if your linter complains, leave it, Task 3 needs it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_launch_queue.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/launch_queue.py tests/test_web_launch_queue.py
git commit -m "Add SQLite launch queue store with batch persistence"
```

### Task 3: `LaunchQueueStore` — atomic claiming and status transitions

**Files:**
- Modify: `src/sag/web/launch_queue.py` (append methods to `LaunchQueueStore`)
- Test: `tests/test_web_launch_queue.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_launch_queue.py`:

```python
def enqueue_three_queued_items(store, concurrency=2):
    store.enqueue_batch(
        make_batch(concurrency=concurrency, total=3, accepted=3),
        [
            make_item("LAUNCH-00000001", row_index=0, workspace_id="sag-a", docker_label="a"),
            make_item("LAUNCH-00000002", row_index=1, workspace_id="sag-b", docker_label="b"),
            make_item("LAUNCH-00000003", row_index=2, workspace_id="sag-c", docker_label="c"),
        ],
    )


def test_claim_next_takes_oldest_row_and_marks_it_launching(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)

    claimed = store.claim_next(global_cap=8, now=LATER)

    assert claimed is not None
    assert claimed.id == "LAUNCH-00000001"
    assert claimed.status == "launching"
    assert claimed.started_at == LATER
    statuses = {
        item["id"]: item["status"] for item in store.list_batches()[0]["items"]
    }
    assert statuses["LAUNCH-00000001"] == "launching"
    assert statuses["LAUNCH-00000002"] == "queued"


def test_claim_next_orders_across_batches_by_created_at_then_row_index(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        LaunchBatch(id="BATCH-20260607-bbbbbb", created_at=LATER, concurrency=2),
        [make_item("LAUNCH-00000009", "BATCH-20260607-bbbbbb", created_at=LATER)],
    )
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=2, accepted=2),
        [
            make_item("LAUNCH-00000002", "BATCH-20260607-aaaaaa", row_index=1),
            make_item("LAUNCH-00000001", "BATCH-20260607-aaaaaa", row_index=0),
        ],
    )

    first = store.claim_next(global_cap=8, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)

    assert first.id == "LAUNCH-00000001"
    assert second.id == "LAUNCH-00000002"


def test_claim_next_respects_batch_concurrency(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=2)

    assert store.claim_next(global_cap=8, now=LATER) is not None
    assert store.claim_next(global_cap=8, now=LATER) is not None
    # Two active items in the batch == batch concurrency: nothing claimable.
    assert store.claim_next(global_cap=8, now=LATER) is None


def test_claim_next_respects_global_cap(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)

    assert store.claim_next(global_cap=1, now=LATER) is not None
    assert store.claim_next(global_cap=1, now=LATER) is None


def test_mark_running_records_pid(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)
    claimed = store.claim_next(global_cap=8, now=LATER)

    store.mark_running(claimed.id, pid=12345, now=LATER)

    item = store.list_batches()[0]["items"][0]
    assert item["status"] == "running"
    assert item["pid"] == 12345


def test_mark_completed_finishes_item_and_batch(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(make_batch(), [make_item("LAUNCH-00000001")])
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)

    store.mark_completed(claimed.id, exit_code=0, now=LATER)

    batch = store.list_batches()[0]
    assert batch["status"] == "completed"
    assert batch["items"][0]["status"] == "completed"
    assert batch["items"][0]["exit_code"] == 0


def test_mark_failed_records_error_and_fails_batch_when_done(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(make_batch(), [make_item("LAUNCH-00000001")])
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)

    store.mark_failed(claimed.id, "sag project exited with code 1", now=LATER, exit_code=1)

    batch = store.list_batches()[0]
    assert batch["status"] == "failed"
    assert batch["items"][0]["status"] == "failed"
    assert batch["items"][0]["exit_code"] == 1
    assert batch["items"][0]["error"] == "sag project exited with code 1"


def test_batch_stays_running_while_items_remain(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)
    claimed = store.claim_next(global_cap=8, now=LATER)

    store.mark_completed(claimed.id, exit_code=0, now=LATER)

    assert store.list_batches()[0]["status"] == "running"


def test_unfinished_items_returns_launching_and_running_rows(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    first = store.claim_next(global_cap=8, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(second.id, pid=77, now=LATER)

    unfinished = store.unfinished_items()

    assert {item.id for item in unfinished} == {first.id, second.id}
    assert {item.status for item in unfinished} == {"launching", "running"}


def test_summary_counts_tracks_statuses(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    first = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(first.id, pid=1, now=LATER)
    store.mark_completed(first.id, exit_code=0, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)
    store.mark_failed(second.id, "boom", now=LATER)

    assert store.summary_counts() == {
        "queued": 1,
        "launching": 0,
        "running": 0,
        "completed": 1,
        "failed": 1,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_launch_queue.py -v`
Expected: the 3 Task-2 tests pass; the new tests FAIL with `AttributeError: 'LaunchQueueStore' object has no attribute 'claim_next'`

- [ ] **Step 3: Implement claiming and transitions**

Append these methods to `LaunchQueueStore` in `src/sag/web/launch_queue.py` (before the module-level helper functions):

```python
    def claim_next(self, global_cap: int, now: str) -> LaunchItem | None:
        """Atomically claim the oldest queued item that has capacity.

        Honors each batch's stored concurrency and the global hard cap.
        Returns the claimed item already marked ``launching``, or ``None``.
        """

        with contextlib.closing(self._connect()) as conn:
            claimed: LaunchItem | None = None
            with self._transaction(conn):
                active = conn.execute(
                    "SELECT COUNT(*) FROM launch_items"
                    " WHERE status IN ('launching', 'running')"
                ).fetchone()[0]
                if active < global_cap:
                    row = conn.execute(
                        "SELECT i.* FROM launch_items i"
                        " JOIN launch_batches b ON b.id = i.batch_id"
                        " WHERE i.status = 'queued'"
                        "   AND ("
                        "     SELECT COUNT(*) FROM launch_items a"
                        "     WHERE a.batch_id = i.batch_id"
                        "       AND a.status IN ('launching', 'running')"
                        "   ) < b.concurrency"
                        " ORDER BY i.created_at, i.row_index"
                        " LIMIT 1"
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            "UPDATE launch_items"
                            " SET status = 'launching', started_at = ?"
                            " WHERE id = ?",
                            (now, row["id"]),
                        )
                        claimed = replace(
                            _item_from_row(row), status="launching", started_at=now
                        )
            return claimed

    def mark_running(self, item_id: str, pid: int, now: str) -> None:
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                conn.execute(
                    "UPDATE launch_items"
                    " SET status = 'running', pid = ?, started_at = COALESCE(started_at, ?)"
                    " WHERE id = ?",
                    (pid, now, item_id),
                )

    def mark_completed(self, item_id: str, exit_code: int, now: str) -> None:
        self._finish(item_id, "completed", exit_code=exit_code, error=None, now=now)

    def mark_failed(
        self, item_id: str, error: str, now: str, exit_code: int | None = None
    ) -> None:
        self._finish(item_id, "failed", exit_code=exit_code, error=error, now=now)

    def unfinished_items(self) -> list[LaunchItem]:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM launch_items"
                " WHERE status IN ('launching', 'running')"
                " ORDER BY created_at, row_index"
            ).fetchall()
            return [_item_from_row(row) for row in rows]

    def _finish(
        self,
        item_id: str,
        status: str,
        exit_code: int | None,
        error: str | None,
        now: str,
    ) -> None:
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                conn.execute(
                    "UPDATE launch_items"
                    " SET status = ?, exit_code = ?, error = ?, finished_at = ?"
                    " WHERE id = ?",
                    (status, exit_code, error, now, item_id),
                )
                batch_row = conn.execute(
                    "SELECT batch_id FROM launch_items WHERE id = ?", (item_id,)
                ).fetchone()
                if batch_row is not None:
                    self._refresh_batch_status(conn, batch_row["batch_id"])

    def _refresh_batch_status(self, conn: sqlite3.Connection, batch_id: str) -> None:
        pending = conn.execute(
            "SELECT COUNT(*) FROM launch_items"
            " WHERE batch_id = ? AND status IN ('queued', 'launching', 'running')",
            (batch_id,),
        ).fetchone()[0]
        if pending:
            status = "running"
        else:
            failed = conn.execute(
                "SELECT COUNT(*) FROM launch_items"
                " WHERE batch_id = ? AND status = 'failed'",
                (batch_id,),
            ).fetchone()[0]
            status = "failed" if failed else "completed"
        conn.execute(
            "UPDATE launch_batches SET status = ? WHERE id = ?", (status, batch_id)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_launch_queue.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/launch_queue.py tests/test_web_launch_queue.py
git commit -m "Add atomic claiming and status transitions to launch queue"
```

### Task 4: `LaunchScheduler` — spawn, monitor, concurrency

**Files:**
- Create: `src/sag/web/launch_runner.py`
- Test: `tests/test_web_launch_runner.py`

Design notes:
- The scheduler thread loop just calls `launch_ready()`; tests call `launch_ready()` directly (no thread) so claiming is deterministic. Only monitor threads (one per subprocess) are exercised in tests, via a `FakeProcess` whose `wait()` blocks on an event.
- `spawn` is injected: `Callable[[list[str], Path], ProcessHandle]`. The default opens the per-launch log file, redirects stdout+stderr there, and uses `start_new_session=True` so launches survive UI shutdown and never write to the `sag ui` terminal.
- Subprocesses inherit the server's CWD, so the CLI's own `logs/session_*` records land in the same `logs/` tree as usual.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_launch_runner.py`:

```python
"""Tests for the launch scheduler worker."""

import threading
import time

from sag.web.launch_queue import LaunchBatch, LaunchItem, LaunchQueueStore
from sag.web.launch_runner import LaunchScheduler

NOW = "2026-06-07T10:00:00"


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self._exited = threading.Event()
        self._exit_code = 0

    def finish(self, exit_code=0):
        self._exit_code = exit_code
        self._exited.set()

    def wait(self):
        self._exited.wait(timeout=5)
        return self._exit_code


class FakeSpawner:
    def __init__(self, fail_with=None):
        self.calls = []
        self.processes = []
        self.fail_with = fail_with

    def __call__(self, argv, log_path):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((argv, log_path))
        process = FakeProcess(pid=1000 + len(self.processes))
        self.processes.append(process)
        return process


def wait_for(condition, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_store(tmp_path):
    return LaunchQueueStore(tmp_path / "launch_queue.sqlite3")


def make_item(item_id, batch_id="BATCH-20260607-abcdef", row_index=0, **overrides):
    fields = dict(
        id=item_id,
        batch_id=batch_id,
        row_index=row_index,
        repo_url="https://github.com/apache/commons-cli.git",
        project_name="commons-cli",
        docker_label="commons-cli",
        workspace_id="sag-commons-cli",
        command=["python", "-m", "sag.main", "project", "x"],
        process_log=f"logs/project_launches/{batch_id}/{item_id}.log",
        created_at=NOW,
    )
    fields.update(overrides)
    return LaunchItem(**fields)


def enqueue(store, items, concurrency=2):
    store.enqueue_batch(
        LaunchBatch(
            id="BATCH-20260607-abcdef",
            created_at=NOW,
            concurrency=concurrency,
            total=len(items),
            accepted=len(items),
        ),
        items,
    )


def item_states(store):
    return {
        item["id"]: item for item in store.list_batches()[0]["items"]
    }


def test_launch_ready_starts_no_more_than_batch_concurrency(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [
            make_item("LAUNCH-00000001", row_index=0),
            make_item("LAUNCH-00000002", row_index=1),
            make_item("LAUNCH-00000003", row_index=2),
        ],
        concurrency=2,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    assert len(spawner.calls) == 2
    states = item_states(store)
    assert states["LAUNCH-00000003"]["status"] == "queued"


def test_launch_ready_respects_global_cap_across_batches(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [make_item("LAUNCH-00000001"), make_item("LAUNCH-00000002", row_index=1)],
        concurrency=2,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=1)

    scheduler.launch_ready()

    assert len(spawner.calls) == 1


def test_started_item_records_pid_and_redirected_log_path(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "running")
    item = item_states(store)["LAUNCH-00000001"]
    assert item["pid"] == 1000
    assert item["process_log"].endswith("LAUNCH-00000001.log")
    argv, log_path = spawner.calls[0]
    assert argv == ["python", "-m", "sag.main", "project", "x"]
    assert str(log_path).endswith("LAUNCH-00000001.log")


def test_zero_exit_marks_completed(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()

    spawner.processes[0].finish(exit_code=0)

    assert wait_for(
        lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed"
    )
    assert item_states(store)["LAUNCH-00000001"]["exit_code"] == 0


def test_nonzero_exit_marks_failed_with_exit_code(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()

    spawner.processes[0].finish(exit_code=1)

    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "failed")
    item = item_states(store)["LAUNCH-00000001"]
    assert item["exit_code"] == 1
    assert "exited with code 1" in item["error"]


def test_spawn_failure_marks_failed(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner(fail_with=OSError("no such file"))
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    item = item_states(store)["LAUNCH-00000001"]
    assert item["status"] == "failed"
    assert "Failed to start subprocess" in item["error"]


def test_capacity_freed_by_completion_lets_next_item_start(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [make_item("LAUNCH-00000001"), make_item("LAUNCH-00000002", row_index=1)],
        concurrency=1,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()
    assert len(spawner.calls) == 1

    spawner.processes[0].finish(exit_code=0)
    assert wait_for(
        lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed"
    )
    scheduler.launch_ready()

    assert len(spawner.calls) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_launch_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sag.web.launch_runner'`

- [ ] **Step 3: Write the implementation**

Create `src/sag/web/launch_runner.py`:

```python
"""Background scheduler that runs queued project setups as CLI subprocesses."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from sag.web.launch_queue import LaunchItem, LaunchQueueStore


def default_global_cap() -> int:
    """Hard cap of active setup subprocesses across all batches."""

    return max(1, os.cpu_count() or 1)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _spawn_subprocess(argv: list[str], log_path: Path) -> Any:
    """Start a launch subprocess with stdout/stderr redirected to its log file."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LaunchScheduler:
    """Claims queued launch items and runs them as monitored subprocesses."""

    def __init__(
        self,
        store: LaunchQueueStore,
        spawn: Callable[[list[str], Path], Any] = _spawn_subprocess,
        workspace_exists: Callable[[str], bool] | None = None,
        global_cap: int | None = None,
        poll_interval: float = 0.5,
    ):
        self.store = store
        self.spawn = spawn
        self.workspace_exists = workspace_exists or (lambda docker_label: False)
        self.global_cap = global_cap if global_cap is not None else default_global_cap()
        self.poll_interval = poll_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.reconcile_stale()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sag-launch-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def wake(self) -> None:
        """Nudge the worker loop so new submissions start without polling delay."""

        self._wake.set()

    def launch_ready(self) -> None:
        """Start subprocesses for every queued item that has capacity right now."""

        while True:
            item = self.store.claim_next(self.global_cap, _now())
            if item is None:
                return
            self._start_item(item)

    def reconcile_stale(self) -> None:
        """Resolve launching/running rows left over from a previous UI run.

        Rows whose process is gone are failed with a restart-recovery message,
        unless Docker discovery clearly shows the workspace exists (then the
        setup evidently got far enough to create it, so mark completed). Rows
        whose process is still alive are left untouched; they are counted
        against capacity and re-checked on the next UI restart.
        """

        for item in self.store.unfinished_items():
            if item.pid is not None and _pid_alive(item.pid):
                continue
            if self.workspace_exists(item.docker_label):
                self.store.mark_completed(item.id, exit_code=0, now=_now())
            else:
                self.store.mark_failed(
                    item.id,
                    "Launch interrupted by UI restart; process is no longer running.",
                    now=_now(),
                    exit_code=item.exit_code,
                )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.launch_ready()
            except Exception:
                logger.exception("Launch scheduler iteration failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def _start_item(self, item: LaunchItem) -> None:
        try:
            process = self.spawn(item.command, Path(item.process_log))
        except Exception as exc:
            self.store.mark_failed(
                item.id, f"Failed to start subprocess: {exc}", now=_now()
            )
            return
        self.store.mark_running(item.id, pid=process.pid, now=_now())
        threading.Thread(
            target=self._monitor,
            args=(item.id, process),
            daemon=True,
            name=f"sag-launch-monitor-{item.id}",
        ).start()

    def _monitor(self, item_id: str, process: Any) -> None:
        try:
            exit_code = process.wait()
        except Exception as exc:
            self.store.mark_failed(item_id, f"Lost launch process: {exc}", now=_now())
            return
        if exit_code == 0:
            self.store.mark_completed(item_id, exit_code=0, now=_now())
        else:
            self.store.mark_failed(
                item_id,
                f"sag project exited with code {exit_code}",
                now=_now(),
                exit_code=exit_code,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_launch_runner.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/launch_runner.py tests/test_web_launch_runner.py
git commit -m "Add launch scheduler with monitored CLI subprocesses"
```

### Task 5: `LaunchScheduler` — stale-row reconcile on startup

**Files:**
- Modify: `src/sag/web/launch_runner.py` (already implemented `reconcile_stale` in Task 4 — this task proves it)
- Test: `tests/test_web_launch_runner.py` (append tests)

- [ ] **Step 1: Write the failing-or-passing tests**

Append to `tests/test_web_launch_runner.py`:

```python
def test_reconcile_fails_dead_process_rows_with_restart_message(tmp_path):
    store = make_store(tmp_path)
    enqueue(
        store,
        [make_item("LAUNCH-00000001", status="running", pid=2_000_000_000)],
    )
    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=lambda label: False, global_cap=8
    )

    scheduler.reconcile_stale()

    item = item_states(store)["LAUNCH-00000001"]
    assert item["status"] == "failed"
    assert "UI restart" in item["error"]


def test_reconcile_marks_completed_when_workspace_exists(tmp_path):
    store = make_store(tmp_path)
    enqueue(
        store,
        [make_item("LAUNCH-00000001", status="launching", pid=2_000_000_000)],
    )
    checked = []

    def workspace_exists(docker_label):
        checked.append(docker_label)
        return True

    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=workspace_exists, global_cap=8
    )

    scheduler.reconcile_stale()

    assert checked == ["commons-cli"]
    assert item_states(store)["LAUNCH-00000001"]["status"] == "completed"


def test_reconcile_leaves_alive_process_rows_untouched(tmp_path):
    store = make_store(tmp_path)
    enqueue(store, [make_item("LAUNCH-00000001", status="running", pid=os.getpid())])
    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=lambda label: False, global_cap=8
    )

    scheduler.reconcile_stale()

    assert item_states(store)["LAUNCH-00000001"]["status"] == "running"


def test_queued_items_from_previous_run_resume(tmp_path):
    db_path = tmp_path / "launch_queue.sqlite3"
    previous = LaunchQueueStore(db_path)
    enqueue(previous, [make_item("LAUNCH-00000001")])

    spawner = FakeSpawner()
    scheduler = LaunchScheduler(LaunchQueueStore(db_path), spawn=spawner, global_cap=8)
    scheduler.reconcile_stale()
    scheduler.launch_ready()

    assert len(spawner.calls) == 1
```

Add `import os` to the imports at the top of `tests/test_web_launch_runner.py` (pid 2,000,000,000 exceeds any real pid on macOS/Linux, so `os.kill(pid, 0)` raises and the row counts as dead; `os.getpid()` is guaranteed alive).

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_web_launch_runner.py -v`
Expected: 11 passed (reconcile was implemented in Task 4; these tests pin its behavior). If any fail, fix `reconcile_stale` — the contract is the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_launch_runner.py
git commit -m "Cover scheduler restart reconciliation"
```

### Task 6: `LaunchService` — validation, conflict precheck, batch submission

**Files:**
- Create: `src/sag/web/launch_service.py`
- Test: `tests/test_web_launch_service.py`

Design notes:
- `workspace_exists: Callable[[str], bool]` is injected; the default wraps `DockerOrchestrator(project_name=docker_label).container_exists()` in try/except and returns `False` on any error (Docker down must not block submission — the CLI subprocess is the final authority per the spec).
- The same `workspace_exists` is handed to the default scheduler for restart reconcile.
- Two rows in one batch that predict the same `workspace_id`: the first is accepted, the second rejected as a conflict (they would race for the same container).
- A `repo_url` that survives Pydantic but can't yield a project name (`extract_project_name_from_url` raises `ValueError`, e.g. `"/"`) is rejected row-level with status `invalid`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_launch_service.py`:

```python
"""Tests for the batch launch service facade."""

import re
import sys

import pytest

from sag.web.launch_queue import LaunchQueueStore
from sag.web.launch_service import (
    LaunchBatchRequest,
    LaunchService,
    LaunchValidationError,
    default_concurrency,
)

REPO = "https://github.com/apache/commons-cli.git"


class FakeScheduler:
    def __init__(self):
        self.woken = 0
        self.started = False
        self.stopped = False

    def wake(self):
        self.woken += 1

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_service(tmp_path, existing=(), cpu_count=8, monkeypatch=None):
    store = LaunchQueueStore(tmp_path / "launch_queue.sqlite3")
    scheduler = FakeScheduler()
    service = LaunchService(
        store=store,
        scheduler=scheduler,
        workspace_exists=lambda label: label in existing,
    )
    if monkeypatch is not None:
        monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: cpu_count)
    return service, store, scheduler


def request_for(*rows, concurrency=None):
    return LaunchBatchRequest(concurrency=concurrency, projects=list(rows))


def test_accepted_rows_are_enqueued_with_cli_equivalent_command(tmp_path):
    service, store, scheduler = make_service(tmp_path)

    outcome = service.submit_batch(
        request_for(
            {
                "repo_url": REPO,
                "name": "commons-cli-111",
                "ref": "rel/commons-cli-1.11.0",
                "goal": "Setup and verify Apache Commons CLI",
                "record": True,
            }
        )
    )

    assert len(outcome["accepted"]) == 1
    accepted = outcome["accepted"][0]
    assert accepted["row_index"] == 0
    assert accepted["workspace_id"] == "sag-commons-cli-111"
    assert accepted["status"] == "queued"
    assert re.fullmatch(r"LAUNCH-[0-9a-f]{8}", accepted["launch_id"])
    assert re.fullmatch(r"BATCH-\d{8}-[0-9a-f]{6}", outcome["batch_id"])
    assert scheduler.woken == 1

    queued = store.list_batches()[0]["items"][0]
    assert queued["status"] == "queued"
    assert queued["process_log"] == (
        f"logs/project_launches/{outcome['batch_id']}/{accepted['launch_id']}.log"
    )


def test_stored_command_matches_manual_sag_project_invocation(tmp_path):
    service, store, _ = make_service(tmp_path)

    service.submit_batch(
        request_for({"repo_url": REPO, "ref": "v1.0", "record": True})
    )

    claimed = store.claim_next(global_cap=8, now="2026-06-07T10:00:00")
    assert claimed.command == [
        sys.executable,
        "-m",
        "sag.main",
        "project",
        REPO,
        "--ref",
        "v1.0",
        "--record",
    ]


def test_optional_fields_are_trimmed_and_blank_becomes_none(tmp_path):
    service, store, _ = make_service(tmp_path)

    service.submit_batch(
        request_for(
            {"repo_url": f"  {REPO}  ", "name": "  ", "ref": " v1.0 ", "goal": ""}
        )
    )

    claimed = store.claim_next(global_cap=8, now="2026-06-07T10:00:00")
    assert claimed.repo_url == REPO
    assert claimed.name is None
    assert claimed.ref == "v1.0"
    assert claimed.goal is None
    assert claimed.docker_label == "commons-cli"
    assert claimed.workspace_id == "sag-commons-cli"


def test_existing_workspace_conflicts_are_rejected_and_not_enqueued(tmp_path):
    service, store, scheduler = make_service(tmp_path, existing={"existing"})

    outcome = service.submit_batch(
        request_for(
            {"repo_url": REPO},
            {"repo_url": "https://github.com/x/existing.git"},
        )
    )

    assert len(outcome["accepted"]) == 1
    assert outcome["rejected"] == [
        {
            "row_index": 1,
            "workspace_id": "sag-existing",
            "status": "conflict",
            "message": "Workspace already exists: sag-existing",
        }
    ]
    assert len(store.list_batches()[0]["items"]) == 1


def test_all_conflicts_creates_no_batch(tmp_path):
    service, store, scheduler = make_service(tmp_path, existing={"commons-cli"})

    outcome = service.submit_batch(request_for({"repo_url": REPO}))

    assert outcome["accepted"] == []
    assert outcome["batch_id"] is None
    assert outcome["rejected"][0]["status"] == "conflict"
    assert store.list_batches() == []
    assert scheduler.woken == 0


def test_duplicate_workspace_within_batch_is_a_conflict(tmp_path):
    service, _, _ = make_service(tmp_path)

    outcome = service.submit_batch(
        request_for({"repo_url": REPO}, {"repo_url": REPO})
    )

    assert len(outcome["accepted"]) == 1
    assert outcome["rejected"][0]["status"] == "conflict"
    assert "Duplicate workspace in batch" in outcome["rejected"][0]["message"]


def test_underivable_repo_url_is_rejected_as_invalid(tmp_path):
    service, store, _ = make_service(tmp_path)

    outcome = service.submit_batch(request_for({"repo_url": "/"}))

    assert outcome["accepted"] == []
    assert outcome["rejected"][0]["status"] == "invalid"
    assert store.list_batches() == []


def test_omitted_concurrency_uses_cpu_aware_default(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=6, monkeypatch=monkeypatch)

    outcome = service.submit_batch(request_for({"repo_url": REPO}))

    assert outcome["concurrency"] == 3  # max(1, min(6 // 2, 4))


def test_default_concurrency_formula(monkeypatch):
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: 16)
    assert default_concurrency() == 4
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: 1)
    assert default_concurrency() == 1
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: None)
    assert default_concurrency() == 1


def test_concurrency_above_cpu_count_is_rejected(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=4, monkeypatch=monkeypatch)

    with pytest.raises(LaunchValidationError):
        service.submit_batch(request_for({"repo_url": REPO}, concurrency=5))


def test_concurrency_below_one_is_rejected(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=4, monkeypatch=monkeypatch)

    with pytest.raises(LaunchValidationError):
        service.submit_batch(request_for({"repo_url": REPO}, concurrency=0))


def test_blank_repo_url_fails_request_validation():
    with pytest.raises(ValueError):
        LaunchBatchRequest(concurrency=None, projects=[{"repo_url": "   "}])


def test_empty_projects_list_fails_request_validation():
    with pytest.raises(ValueError):
        LaunchBatchRequest(concurrency=None, projects=[])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_launch_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sag.web.launch_service'`

- [ ] **Step 3: Write the implementation**

Create `src/sag/web/launch_service.py`:

```python
"""Service facade for Web-triggered batch project launches."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Callable
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, Field, StringConstraints, field_validator

from sag.utils.git_utils import extract_project_name_from_url
from sag.web.launch_queue import LaunchBatch, LaunchItem, LaunchQueueStore
from sag.web.launch_runner import LaunchScheduler
from sag.web.project_cli import ProjectCliCommand

DEFAULT_DB_PATH = Path("logs/launch_queue.sqlite3")
PROCESS_LOG_ROOT = Path("logs/project_launches")


class LaunchValidationError(ValueError):
    """Raised when a launch request fails semantic validation."""


class LaunchProjectRow(BaseModel):
    repo_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: str | None = None
    ref: str | None = None
    goal: str | None = None
    record: bool = False

    @field_validator("name", "ref", "goal")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LaunchBatchRequest(BaseModel):
    concurrency: int | None = None
    projects: list[LaunchProjectRow] = Field(min_length=1)


def default_concurrency() -> int:
    """CPU-aware default batch concurrency."""

    return max(1, min((os.cpu_count() or 1) // 2, 4))


def max_concurrency() -> int:
    """Upper bound a user may request for one batch."""

    return max(1, os.cpu_count() or 1)


def _workspace_exists_via_docker(docker_label: str) -> bool:
    """Fast UX precheck mirroring the CLI conflict check.

    Fails open: the CLI subprocess performs its own authoritative check.
    """

    try:
        from sag.docker_orch.orch import DockerOrchestrator

        return DockerOrchestrator(project_name=docker_label).container_exists()
    except Exception:
        logger.exception("Workspace conflict precheck failed; allowing launch")
        return False


class LaunchService:
    """The only API the web handlers use for batch launches."""

    def __init__(
        self,
        store: LaunchQueueStore | None = None,
        scheduler: LaunchScheduler | None = None,
        workspace_exists: Callable[[str], bool] | None = None,
    ):
        self.store = store if store is not None else LaunchQueueStore(DEFAULT_DB_PATH)
        self.workspace_exists = (
            workspace_exists if workspace_exists is not None else _workspace_exists_via_docker
        )
        self.scheduler = (
            scheduler
            if scheduler is not None
            else LaunchScheduler(self.store, workspace_exists=self.workspace_exists)
        )

    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()

    def submit_batch(self, request: LaunchBatchRequest) -> dict:
        concurrency = self._validate_concurrency(request.concurrency)
        now = datetime.now()
        created_at = now.isoformat(timespec="seconds")
        batch_id = f"BATCH-{now.strftime('%Y%m%d')}-{uuid4().hex[:6]}"

        accepted: list[dict] = []
        rejected: list[dict] = []
        items: list[LaunchItem] = []
        seen_workspaces: set[str] = set()

        for row_index, row in enumerate(request.projects):
            try:
                project_name = extract_project_name_from_url(row.repo_url)
            except ValueError as exc:
                rejected.append(
                    {
                        "row_index": row_index,
                        "workspace_id": None,
                        "status": "invalid",
                        "message": str(exc),
                    }
                )
                continue

            docker_label = row.name or project_name
            workspace_id = f"sag-{docker_label}"

            if workspace_id in seen_workspaces:
                rejected.append(
                    {
                        "row_index": row_index,
                        "workspace_id": workspace_id,
                        "status": "conflict",
                        "message": f"Duplicate workspace in batch: {workspace_id}",
                    }
                )
                continue

            if self.workspace_exists(docker_label):
                rejected.append(
                    {
                        "row_index": row_index,
                        "workspace_id": workspace_id,
                        "status": "conflict",
                        "message": f"Workspace already exists: {workspace_id}",
                    }
                )
                continue

            seen_workspaces.add(workspace_id)
            launch_id = f"LAUNCH-{uuid4().hex[:8]}"
            command = ProjectCliCommand(
                repo_url=row.repo_url,
                name=row.name,
                ref=row.ref,
                goal=row.goal,
                record=row.record,
            ).argv()
            process_log = PROCESS_LOG_ROOT / batch_id / f"{launch_id}.log"
            items.append(
                LaunchItem(
                    id=launch_id,
                    batch_id=batch_id,
                    row_index=row_index,
                    repo_url=row.repo_url,
                    name=row.name,
                    ref=row.ref,
                    goal=row.goal,
                    record=row.record,
                    project_name=project_name,
                    docker_label=docker_label,
                    workspace_id=workspace_id,
                    command=command,
                    process_log=str(process_log),
                    created_at=created_at,
                )
            )
            accepted.append(
                {
                    "launch_id": launch_id,
                    "row_index": row_index,
                    "workspace_id": workspace_id,
                    "status": "queued",
                }
            )

        if items:
            self.store.enqueue_batch(
                LaunchBatch(
                    id=batch_id,
                    created_at=created_at,
                    concurrency=concurrency,
                    status="running",
                    total=len(request.projects),
                    accepted=len(accepted),
                    rejected=len(rejected),
                ),
                items,
            )
            self.scheduler.wake()

        return {
            "batch_id": batch_id if items else None,
            "concurrency": concurrency,
            "accepted": accepted,
            "rejected": rejected,
        }

    def queue_state(self) -> dict:
        return {
            "default_concurrency": default_concurrency(),
            "summary": self.store.summary_counts(),
            "batches": self.store.list_batches(),
        }

    def _validate_concurrency(self, value: int | None) -> int:
        if value is None:
            return default_concurrency()
        limit = max_concurrency()
        if value < 1 or value > limit:
            raise LaunchValidationError(
                f"concurrency must be an integer between 1 and {limit}"
            )
        return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_launch_service.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/launch_service.py tests/test_web_launch_service.py
git commit -m "Add launch service with conflict precheck and batch submit"
```

### Task 7: `LaunchService.queue_state` shape

**Files:**
- Modify: `src/sag/web/launch_service.py` (already implemented in Task 6 — this task pins the contract)
- Test: `tests/test_web_launch_service.py` (append tests)

- [ ] **Step 1: Write the tests**

Append to `tests/test_web_launch_service.py`:

```python
def test_queue_state_reports_defaults_summary_and_batches(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=8, monkeypatch=monkeypatch)
    outcome = service.submit_batch(
        request_for({"repo_url": REPO, "ref": "v1.0"}, concurrency=2)
    )

    state = service.queue_state()

    assert state["default_concurrency"] == 4  # max(1, min(8 // 2, 4))
    assert state["summary"] == {
        "queued": 1,
        "launching": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
    assert len(state["batches"]) == 1
    batch = state["batches"][0]
    assert batch["id"] == outcome["batch_id"]
    assert batch["status"] == "running"
    assert batch["concurrency"] == 2
    item = batch["items"][0]
    assert item["id"] == outcome["accepted"][0]["launch_id"]
    assert item["repo_url"] == REPO
    assert item["ref"] == "v1.0"
    assert item["status"] == "queued"
    assert item["pid"] is None
    assert item["exit_code"] is None
    assert item["error"] is None
    assert item["process_log"].startswith("logs/project_launches/")


def test_queue_state_on_empty_store(tmp_path):
    service, _, _ = make_service(tmp_path)

    state = service.queue_state()

    assert state["batches"] == []
    assert sum(state["summary"].values()) == 0
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_web_launch_service.py -v`
Expected: 15 passed (queue_state was implemented in Task 6). If anything fails, fix `queue_state` — the contract is the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_launch_service.py
git commit -m "Pin launch queue state contract"
```

### Task 8: API endpoints and lifespan wiring

**Files:**
- Modify: `src/sag/web/app.py`
- Test: `tests/test_web_api.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_api.py` (it already imports `create_app`, `ReadModelBuilder`, and `TestClient` at the top — reuse those):

```python
class FakeLaunchService:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.requests = []

    def submit_batch(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.outcome

    def queue_state(self):
        return {
            "default_concurrency": 3,
            "summary": {
                "queued": 1,
                "launching": 0,
                "running": 1,
                "completed": 2,
                "failed": 0,
            },
            "batches": [],
        }

    def start(self):
        pass

    def stop(self):
        pass


def launch_client(service):
    app = create_app(ReadModelBuilder(demo_mode=True), launch_service=service)
    return TestClient(app)


def test_batch_submit_returns_202_with_accepted_and_rejected_rows():
    service = FakeLaunchService(
        outcome={
            "batch_id": "BATCH-20260607-abcdef",
            "concurrency": 2,
            "accepted": [
                {
                    "launch_id": "LAUNCH-12345678",
                    "row_index": 0,
                    "workspace_id": "sag-commons-cli",
                    "status": "queued",
                }
            ],
            "rejected": [
                {
                    "row_index": 1,
                    "workspace_id": "sag-existing",
                    "status": "conflict",
                    "message": "Workspace already exists: sag-existing",
                }
            ],
        }
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={
            "concurrency": 2,
            "projects": [
                {"repo_url": "https://github.com/apache/commons-cli.git"},
                {"repo_url": "https://github.com/x/existing.git"},
            ],
        },
    )

    assert response.status_code == 202
    assert response.json()["batch_id"] == "BATCH-20260607-abcdef"
    assert len(service.requests) == 1
    assert service.requests[0].projects[0].repo_url == (
        "https://github.com/apache/commons-cli.git"
    )


def test_batch_submit_returns_409_when_every_row_conflicts():
    service = FakeLaunchService(
        outcome={
            "batch_id": None,
            "concurrency": 2,
            "accepted": [],
            "rejected": [
                {
                    "row_index": 0,
                    "workspace_id": "sag-existing",
                    "status": "conflict",
                    "message": "Workspace already exists: sag-existing",
                }
            ],
        }
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={"projects": [{"repo_url": "https://github.com/x/existing.git"}]},
    )

    assert response.status_code == 409
    assert response.json()["rejected"][0]["status"] == "conflict"


def test_batch_submit_returns_422_for_invalid_shape():
    client = launch_client(FakeLaunchService())

    no_projects = client.post("/api/project-launches/batch", json={"projects": []})
    blank_repo = client.post(
        "/api/project-launches/batch", json={"projects": [{"repo_url": "   "}]}
    )

    assert no_projects.status_code == 422
    assert blank_repo.status_code == 422


def test_batch_submit_returns_422_for_out_of_range_concurrency():
    from sag.web.launch_service import LaunchValidationError

    service = FakeLaunchService(
        error=LaunchValidationError("concurrency must be an integer between 1 and 8")
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={
            "concurrency": 99,
            "projects": [{"repo_url": "https://github.com/apache/commons-cli.git"}],
        },
    )

    assert response.status_code == 422
    assert "concurrency" in response.json()["detail"]


def test_get_project_launches_returns_queue_state():
    client = launch_client(FakeLaunchService())

    response = client.get("/api/project-launches")

    assert response.status_code == 200
    body = response.json()
    assert body["default_concurrency"] == 3
    assert body["summary"]["completed"] == 2
    assert body["batches"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_api.py -v`
Expected: existing tests pass; new tests FAIL with `TypeError: create_app() got an unexpected keyword argument 'launch_service'`

- [ ] **Step 3: Wire the service into `app.py`**

In `src/sag/web/app.py`, make these exact edits:

1. Add imports (after the existing `fastapi` imports):

```python
from fastapi.responses import JSONResponse, StreamingResponse
```

(replacing the existing `from fastapi.responses import StreamingResponse` line), and after the `sag.web.read_model` import:

```python
from loguru import logger

from sag.web.launch_service import LaunchBatchRequest, LaunchService, LaunchValidationError
```

2. Add the `launch_service` parameter to `create_app` and construct the default:

```python
def create_app(
    read_model: ReadModelBuilder | None = None,
    task_runner: TaskRunner | None = None,
    terminal_adapter: TerminalAdapter | None = None,
    static_dir: Path | None = None,
    launch_service: LaunchService | None = None,
) -> FastAPI:
    builder = read_model if read_model is not None else ReadModelBuilder()
    runner = task_runner if task_runner is not None else TaskRunner()
    terminal_bridge = terminal_adapter if terminal_adapter is not None else TerminalAdapter()
    owns_terminal_bridge = terminal_adapter is None
    launches = launch_service if launch_service is not None else LaunchService()
```

(Constructing the default `LaunchService` does not touch the filesystem or Docker — the store connects lazily and the scheduler thread only starts in the lifespan.)

3. Replace the lifespan body so the scheduler starts on startup and stops on shutdown:

```python
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await asyncio.to_thread(launches.start)
        except Exception:
            logger.exception("Failed to start launch scheduler")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(launches.stop)
            if owns_terminal_bridge:
                close = getattr(terminal_bridge, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(close)
```

4. Add the two routes after the existing `submit_task` route:

```python
    @app.post("/api/project-launches/batch")
    def submit_project_batch(request: LaunchBatchRequest) -> JSONResponse:
        try:
            outcome = launches.submit_batch(request)
        except LaunchValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        status_code = 202 if outcome["accepted"] else 409
        return JSONResponse(status_code=status_code, content=outcome)

    @app.get("/api/project-launches")
    def get_project_launches() -> dict:
        return launches.queue_state()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_api.py -v`
Expected: all pass (existing + 5 new)

- [ ] **Step 5: Run the whole backend suite plus packaging/import smoke**

Run: `uv run pytest`
Expected: all pass (this covers `tests/test_import_smoke.py`, `tests/test_packaging_smoke.py`, and `tests/test_static_import_guard.py`, which must accept the new modules)

- [ ] **Step 6: Commit**

```bash
git add src/sag/web/app.py tests/test_web_api.py
git commit -m "Add batch project launch API endpoints"
```

### Task 9: Frontend API types and client functions

**Files:**
- Modify: `webui/src/api/types.ts`
- Modify: `webui/src/api/client.ts`
- Test: `webui/src/api/client.test.ts`

- [ ] **Step 1: Write the failing tests**

Append to `webui/src/api/client.test.ts` (inside the existing `describe("api client", ...)` block; it already defines the `jsonResponse` helper and uses `vi.spyOn(globalThis, "fetch")`):

```typescript
  it("submits a project batch and returns the body with http status", async () => {
    const body = {
      batch_id: "BATCH-20260607-abcdef",
      concurrency: 2,
      accepted: [
        {
          launch_id: "LAUNCH-12345678",
          row_index: 0,
          workspace_id: "sag-commons-cli",
          status: "queued",
        },
      ],
      rejected: [],
    }
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(body, { status: 202 }))

    const result = await submitProjectBatch({
      concurrency: 2,
      projects: [{ repo_url: "https://github.com/apache/commons-cli.git" }],
    })

    expect(fetchMock).toHaveBeenCalledWith("/api/project-launches/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        concurrency: 2,
        projects: [{ repo_url: "https://github.com/apache/commons-cli.git" }],
      }),
    })
    expect(result).toEqual({ status: 202, ...body })
  })

  it("returns conflict batch responses instead of throwing on 409", async () => {
    const body = {
      batch_id: null,
      concurrency: 2,
      accepted: [],
      rejected: [
        {
          row_index: 0,
          workspace_id: "sag-existing",
          status: "conflict",
          message: "Workspace already exists: sag-existing",
        },
      ],
    }
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(body, { status: 409 }),
    )

    const result = await submitProjectBatch({
      projects: [{ repo_url: "https://github.com/x/existing.git" }],
    })

    expect(result.status).toBe(409)
    expect(result.rejected[0].message).toContain("already exists")
  })

  it("throws on unexpected batch submit failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("boom", { status: 500, statusText: "Internal Server Error" }),
    )

    await expect(
      submitProjectBatch({ projects: [{ repo_url: "x" }] }),
    ).rejects.toThrow("500")
  })

  it("fetches the launch queue", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        default_concurrency: 4,
        summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
        batches: [],
      }),
    )

    const queue = await fetchLaunchQueue()

    expect(fetchMock).toHaveBeenCalledWith("/api/project-launches")
    expect(queue.default_concurrency).toBe(4)
  })
```

Update the import at the top of `webui/src/api/client.test.ts` to:

```typescript
import {
  fetchDashboard,
  fetchLaunchQueue,
  fetchSession,
  submitProjectBatch,
  submitTask,
} from "./client"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd webui && npx vitest run src/api/client.test.ts`
Expected: FAIL — `"./client"` has no exported member `submitProjectBatch` / `fetchLaunchQueue`

- [ ] **Step 3: Add the types**

Append to `webui/src/api/types.ts` (backend payloads are snake_case, matching `SubmitTaskResponse`):

```typescript
export interface LaunchProjectRowInput {
  repo_url: string
  name?: string | null
  ref?: string | null
  goal?: string | null
  record?: boolean
}

export interface LaunchBatchRequestBody {
  concurrency?: number | null
  projects: LaunchProjectRowInput[]
}

export interface LaunchAcceptedRow {
  launch_id: string
  row_index: number
  workspace_id: string
  status: string
}

export interface LaunchRejectedRow {
  row_index: number
  workspace_id: string | null
  status: string
  message: string
}

export interface LaunchBatchResponse {
  batch_id: string | null
  concurrency: number
  accepted: LaunchAcceptedRow[]
  rejected: LaunchRejectedRow[]
}

export interface LaunchBatchResult extends LaunchBatchResponse {
  status: number
}

export interface LaunchQueueSummary {
  queued: number
  launching: number
  running: number
  completed: number
  failed: number
}

export interface LaunchQueueItem {
  id: string
  row_index: number
  repo_url: string
  workspace_id: string
  ref: string | null
  status: string
  pid: number | null
  exit_code: number | null
  error: string | null
  process_log: string
}

export interface LaunchQueueBatch {
  id: string
  status: string
  concurrency: number
  created: string
  items: LaunchQueueItem[]
}

export interface LaunchQueueState {
  default_concurrency: number
  summary: LaunchQueueSummary
  batches: LaunchQueueBatch[]
}
```

- [ ] **Step 4: Add the client functions**

In `webui/src/api/client.ts`, extend the type import and append the functions:

```typescript
import type {
  DashboardResponse,
  ExecutionSessionDetail,
  LaunchBatchRequestBody,
  LaunchBatchResponse,
  LaunchBatchResult,
  LaunchQueueState,
  SubmitTaskResponse,
} from "./types"
```

```typescript
export function fetchLaunchQueue(): Promise<LaunchQueueState> {
  return getJson<LaunchQueueState>("/api/project-launches")
}

export async function submitProjectBatch(
  payload: LaunchBatchRequestBody,
): Promise<LaunchBatchResult> {
  const response = await fetch("/api/project-launches/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })

  if (response.status === 202 || response.status === 409) {
    const body = (await response.json()) as LaunchBatchResponse
    return { status: response.status, ...body }
  }

  throw new Error(`${response.status} ${response.statusText}`)
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd webui && npx vitest run src/api/client.test.ts`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add webui/src/api/types.ts webui/src/api/client.ts webui/src/api/client.test.ts
git commit -m "Add launch batch API client"
```

### Task 10: Paste parser and row drafts

**Files:**
- Create: `webui/src/components/launch/launchRows.ts`
- Test: `webui/src/components/launch/launchRows.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `webui/src/components/launch/launchRows.test.ts`:

```typescript
import { describe, expect, it } from "vitest"

import { emptyLaunchRow, parsePastedRepoLines } from "./launchRows"

describe("emptyLaunchRow", () => {
  it("creates a blank row with record off", () => {
    expect(emptyLaunchRow()).toEqual({
      repoUrl: "",
      name: "",
      ref: "",
      goal: "",
      record: false,
    })
  })
})

describe("parsePastedRepoLines", () => {
  it("parses one repo url per line", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git\nhttps://github.com/apache/dubbo.git",
    )

    expect(parsed).toEqual([
      { repoUrl: "https://github.com/apache/commons-cli.git", ref: "" },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "" },
    ])
  })

  it("parses the quick repo_url ref format", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0\n" +
        "https://github.com/apache/dubbo.git dubbo-3.2.19",
    )

    expect(parsed).toEqual([
      {
        repoUrl: "https://github.com/apache/commons-cli.git",
        ref: "rel/commons-cli-1.11.0",
      },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "dubbo-3.2.19" },
    ])
  })

  it("ignores blank lines and trims whitespace", () => {
    const parsed = parsePastedRepoLines(
      "\n  https://github.com/a/b.git   v1.0  \r\n\n",
    )

    expect(parsed).toEqual([{ repoUrl: "https://github.com/a/b.git", ref: "v1.0" }])
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd webui && npx vitest run src/components/launch/launchRows.test.ts`
Expected: FAIL — cannot resolve `./launchRows`

- [ ] **Step 3: Write the implementation**

Create `webui/src/components/launch/launchRows.ts`:

```typescript
export interface LaunchRowDraft {
  repoUrl: string
  name: string
  ref: string
  goal: string
  record: boolean
}

export function emptyLaunchRow(): LaunchRowDraft {
  return { repoUrl: "", name: "", ref: "", goal: "", record: false }
}

/**
 * Parse multi-line paste input. Supported quick format per line:
 *   repo_url
 *   repo_url ref
 */
export function parsePastedRepoLines(
  text: string,
): Array<{ repoUrl: string; ref: string }> {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [repoUrl, ref = ""] = line.split(/\s+/)
      return { repoUrl, ref }
    })
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd webui && npx vitest run src/components/launch/launchRows.test.ts`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/launch/launchRows.ts webui/src/components/launch/launchRows.test.ts
git commit -m "Add launch row paste parser"
```

### Task 11: `LaunchSetupsDialog` component

**Files:**
- Create: `webui/src/components/launch/LaunchSetupsDialog.tsx`
- Test: `webui/src/components/launch/LaunchSetupsDialog.test.tsx`

Behavior contract:
- Opens with one empty row; required column repo URL; optional `name`, `ref`, `goal`, `record` (checkbox, default off); rows can be added/removed.
- Pasting multi-line text into a repo URL cell fills that row and appends one row per extra line (`repo_url` or `repo_url ref` format).
- Concurrency input appears once above the grid, prefilled with the server default.
- Submit: fully-empty rows are dropped; a non-empty row without a repo URL is a client-side row error; the payload maps trimmed values, blanks omitted as `null`.
- On 202 the dialog calls `onSubmitted(result)` (parent closes it). On 409 the dialog stays open, maps `rejected[].row_index` back to visible rows, and keeps all user input.

- [ ] **Step 1: Write the failing tests**

Create `webui/src/components/launch/LaunchSetupsDialog.test.tsx`:

```typescript
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { LaunchBatchResult } from "@/api/types"

import { LaunchSetupsDialog } from "./LaunchSetupsDialog"

const accepted = (workspaceId: string, rowIndex: number) => ({
  launch_id: `LAUNCH-0000000${rowIndex}`,
  row_index: rowIndex,
  workspace_id: workspaceId,
  status: "queued",
})

function renderDialog(overrides?: {
  onSubmit?: (payload: unknown) => Promise<LaunchBatchResult>
  onSubmitted?: (result: LaunchBatchResult) => void
  onClose?: () => void
}) {
  const onSubmit =
    overrides?.onSubmit ??
    vi.fn().mockResolvedValue({
      status: 202,
      batch_id: "BATCH-20260607-abcdef",
      concurrency: 2,
      accepted: [accepted("sag-commons-cli", 0)],
      rejected: [],
    })
  const onSubmitted = overrides?.onSubmitted ?? vi.fn()
  const onClose = overrides?.onClose ?? vi.fn()

  render(
    <LaunchSetupsDialog
      defaultConcurrency={2}
      onClose={onClose}
      onSubmit={onSubmit}
      onSubmitted={onSubmitted}
    />,
  )

  return { onSubmit, onSubmitted, onClose }
}

describe("LaunchSetupsDialog", () => {
  afterEach(() => {
    cleanup()
  })

  it("opens with one empty row and the optional columns", () => {
    renderDialog()

    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue("")
    expect(screen.getByLabelText("Name row 1")).toHaveValue("")
    expect(screen.getByLabelText("Ref row 1")).toHaveValue("")
    expect(screen.getByLabelText("Goal row 1")).toHaveValue("")
    expect(screen.getByLabelText("Record row 1")).not.toBeChecked()
    expect(screen.getByLabelText("Concurrency")).toHaveValue(2)
  })

  it("adds and removes rows", () => {
    renderDialog()

    fireEvent.click(screen.getByRole("button", { name: "Add row" }))
    expect(screen.getByLabelText("Repository URL row 2")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Remove row 2" }))
    expect(screen.queryByLabelText("Repository URL row 2")).not.toBeInTheDocument()
  })

  it("creates one row per pasted line and fills refs", () => {
    renderDialog()

    fireEvent.paste(screen.getByLabelText("Repository URL row 1"), {
      clipboardData: {
        getData: () =>
          "https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0\n" +
          "https://github.com/apache/dubbo.git dubbo-3.2.19",
      },
    })

    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue(
      "https://github.com/apache/commons-cli.git",
    )
    expect(screen.getByLabelText("Ref row 1")).toHaveValue("rel/commons-cli-1.11.0")
    expect(screen.getByLabelText("Repository URL row 2")).toHaveValue(
      "https://github.com/apache/dubbo.git",
    )
    expect(screen.getByLabelText("Ref row 2")).toHaveValue("dubbo-3.2.19")
  })

  it("submits trimmed rows and reports the result", async () => {
    const { onSubmit, onSubmitted } = renderDialog()

    fireEvent.change(screen.getByLabelText("Repository URL row 1"), {
      target: { value: " https://github.com/apache/commons-cli.git " },
    })
    fireEvent.change(screen.getByLabelText("Ref row 1"), {
      target: { value: "v1.0" },
    })
    fireEvent.click(screen.getByLabelText("Record row 1"))
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    await waitFor(() => expect(onSubmitted).toHaveBeenCalled())
    expect(onSubmit).toHaveBeenCalledWith({
      concurrency: 2,
      projects: [
        {
          repo_url: "https://github.com/apache/commons-cli.git",
          name: null,
          ref: "v1.0",
          goal: null,
          record: true,
        },
      ],
    })
  })

  it("keeps input and shows row-level errors when every row conflicts", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      status: 409,
      batch_id: null,
      concurrency: 2,
      accepted: [],
      rejected: [
        {
          row_index: 0,
          workspace_id: "sag-existing",
          status: "conflict",
          message: "Workspace already exists: sag-existing",
        },
      ],
    } satisfies LaunchBatchResult)
    const { onSubmitted } = renderDialog({ onSubmit })

    fireEvent.change(screen.getByLabelText("Repository URL row 1"), {
      target: { value: "https://github.com/x/existing.git" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    await waitFor(() =>
      expect(
        screen.getByText(/Workspace already exists: sag-existing/),
      ).toBeInTheDocument(),
    )
    expect(onSubmitted).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue(
      "https://github.com/x/existing.git",
    )
  })

  it("flags non-empty rows that are missing a repo url", async () => {
    const { onSubmit } = renderDialog()

    fireEvent.change(screen.getByLabelText("Ref row 1"), {
      target: { value: "v1.0" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    expect(await screen.findByText(/Repository URL is required/)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd webui && npx vitest run src/components/launch/LaunchSetupsDialog.test.tsx`
Expected: FAIL — cannot resolve `./LaunchSetupsDialog`

- [ ] **Step 3: Write the component**

Create `webui/src/components/launch/LaunchSetupsDialog.tsx`:

```tsx
import { useState } from "react"
import type { ClipboardEvent } from "react"
import { Plus, Rocket, X } from "lucide-react"

import type { LaunchBatchRequestBody, LaunchBatchResult } from "@/api/types"
import { Button } from "@/components/common/Button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

import { emptyLaunchRow, parsePastedRepoLines, type LaunchRowDraft } from "./launchRows"

interface LaunchSetupsDialogProps {
  defaultConcurrency: number
  onClose: () => void
  onSubmit: (payload: LaunchBatchRequestBody) => Promise<LaunchBatchResult>
  onSubmitted: (result: LaunchBatchResult) => void
}

const cellClass =
  "w-full rounded-md border border-slate-200 px-2 py-1.5 font-mono text-[12px] text-slate-700 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"

function isRowEmpty(row: LaunchRowDraft): boolean {
  return (
    !row.repoUrl.trim() &&
    !row.name.trim() &&
    !row.ref.trim() &&
    !row.goal.trim() &&
    !row.record
  )
}

export function LaunchSetupsDialog({
  defaultConcurrency,
  onClose,
  onSubmit,
  onSubmitted,
}: LaunchSetupsDialogProps) {
  const [rows, setRows] = useState<LaunchRowDraft[]>([emptyLaunchRow()])
  const [concurrency, setConcurrency] = useState(String(defaultConcurrency))
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const updateRow = (index: number, patch: Partial<LaunchRowDraft>) => {
    setRows((current) =>
      current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)),
    )
  }

  const addRow = () => setRows((current) => [...current, emptyLaunchRow()])

  const removeRow = (index: number) => {
    setRows((current) => {
      const next = current.filter((_, rowIndex) => rowIndex !== index)
      return next.length ? next : [emptyLaunchRow()]
    })
    setRowErrors({})
  }

  const handleRepoPaste = (index: number, event: ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData("text")
    if (!text.includes("\n")) {
      return
    }
    event.preventDefault()
    const parsed = parsePastedRepoLines(text)
    if (!parsed.length) {
      return
    }
    setRows((current) => {
      const next = [...current]
      const [first, ...rest] = parsed
      next[index] = { ...next[index], repoUrl: first.repoUrl, ref: first.ref || next[index].ref }
      const extra = rest.map((line) => ({
        ...emptyLaunchRow(),
        repoUrl: line.repoUrl,
        ref: line.ref,
      }))
      next.splice(index + 1, 0, ...extra)
      return next
    })
  }

  const handleSubmit = async () => {
    setFormError(null)
    setRowErrors({})

    const parsedConcurrency = Number(concurrency)
    if (!Number.isInteger(parsedConcurrency) || parsedConcurrency < 1) {
      setFormError("Concurrency must be a whole number of 1 or more.")
      return
    }

    const submittedIndexes: number[] = []
    const errors: Record<number, string> = {}
    rows.forEach((row, index) => {
      if (isRowEmpty(row)) {
        return
      }
      if (!row.repoUrl.trim()) {
        errors[index] = "Repository URL is required."
        return
      }
      submittedIndexes.push(index)
    })

    if (Object.keys(errors).length) {
      setRowErrors(errors)
      return
    }
    if (!submittedIndexes.length) {
      setFormError("Add at least one repository URL.")
      return
    }

    const payload: LaunchBatchRequestBody = {
      concurrency: parsedConcurrency,
      projects: submittedIndexes.map((index) => {
        const row = rows[index]
        return {
          repo_url: row.repoUrl.trim(),
          name: row.name.trim() || null,
          ref: row.ref.trim() || null,
          goal: row.goal.trim() || null,
          record: row.record,
        }
      }),
    }

    setSubmitting(true)
    try {
      const result = await onSubmit(payload)
      if (result.status === 409) {
        const conflictErrors: Record<number, string> = {}
        for (const rejection of result.rejected) {
          const rowIndex = submittedIndexes[rejection.row_index]
          if (rowIndex !== undefined) {
            conflictErrors[rowIndex] = rejection.message
          }
        }
        setRowErrors(conflictErrors)
        setFormError("No rows were launched.")
        return
      }
      onSubmitted(result)
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose()
        }
      }}
    >
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[920px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle>Launch setups</DialogTitle>
          <DialogDescription>
            One row per repository. Each accepted row runs `sag project` in its own
            process. Paste multiple lines (repo URL, optionally followed by a ref)
            into a repository cell to fill the grid.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto p-4">
          <div className="flex items-center gap-2">
            <label
              className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400"
              htmlFor="launch-concurrency"
            >
              Concurrency
            </label>
            <input
              aria-label="Concurrency"
              className={`${cellClass} w-20`}
              id="launch-concurrency"
              min={1}
              onChange={(event) => setConcurrency(event.target.value)}
              type="number"
              value={concurrency}
            />
            <span className="text-[11px] text-slate-400">
              parallel setups for this batch
            </span>
          </div>

          <div className="mt-3 grid grid-cols-[2.2fr_1fr_1.2fr_1.6fr_56px_36px] items-center gap-2">
            {["Repo URL", "Name", "Ref", "Goal", "Record", ""].map((header) => (
              <div
                key={header || "actions"}
                className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400"
              >
                {header}
              </div>
            ))}
            {rows.map((row, index) => (
              <RowCells
                key={index}
                error={rowErrors[index]}
                index={index}
                onChange={(patch) => updateRow(index, patch)}
                onRemove={() => removeRow(index)}
                onRepoPaste={(event) => handleRepoPaste(index, event)}
                row={row}
              />
            ))}
          </div>

          <Button
            className="mt-3"
            onClick={addRow}
            size="sm"
            type="button"
            variant="outline"
          >
            <Plus size={13} />
            Add row
          </Button>

          {formError ? (
            <div className="mt-3 text-[12px] text-red-600">{formError}</div>
          ) : null}
        </div>

        <DialogFooter className="gap-2 border-t border-slate-100 px-4 py-3 sm:space-x-0">
          <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
            Cancel
          </Button>
          <Button disabled={submitting} onClick={() => void handleSubmit()} type="button">
            <Rocket size={13} />
            {submitting ? "Launching" : "Launch setups"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RowCells({
  row,
  index,
  error,
  onChange,
  onRemove,
  onRepoPaste,
}: {
  row: LaunchRowDraft
  index: number
  error?: string
  onChange: (patch: Partial<LaunchRowDraft>) => void
  onRemove: () => void
  onRepoPaste: (event: ClipboardEvent<HTMLInputElement>) => void
}) {
  const rowLabel = index + 1

  return (
    <>
      <input
        aria-label={`Repository URL row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ repoUrl: event.target.value })}
        onPaste={onRepoPaste}
        placeholder="https://github.com/owner/repo.git"
        value={row.repoUrl}
      />
      <input
        aria-label={`Name row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ name: event.target.value })}
        placeholder="optional"
        value={row.name}
      />
      <input
        aria-label={`Ref row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ ref: event.target.value })}
        placeholder="optional"
        value={row.ref}
      />
      <input
        aria-label={`Goal row ${rowLabel}`}
        className={cellClass}
        onChange={(event) => onChange({ goal: event.target.value })}
        placeholder="optional"
        value={row.goal}
      />
      <div className="flex justify-center">
        <input
          aria-label={`Record row ${rowLabel}`}
          checked={row.record}
          className="h-4 w-4 accent-blue-600"
          onChange={(event) => onChange({ record: event.target.checked })}
          type="checkbox"
        />
      </div>
      <button
        aria-label={`Remove row ${rowLabel}`}
        className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
        onClick={onRemove}
        type="button"
      >
        <X size={14} />
      </button>
      {error ? (
        <div className="col-span-6 -mt-1 text-[12px] text-red-600">{error}</div>
      ) : null}
    </>
  )
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd webui && npx vitest run src/components/launch/LaunchSetupsDialog.test.tsx`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/launch/LaunchSetupsDialog.tsx webui/src/components/launch/LaunchSetupsDialog.test.tsx
git commit -m "Add table-first launch setups dialog"
```

### Task 12: `LaunchQueuePanel` component

**Files:**
- Create: `webui/src/components/launch/LaunchQueuePanel.tsx`
- Test: `webui/src/components/launch/LaunchQueuePanel.test.tsx`

Behavior contract (per spec): compact counts for queued/running/completed/failed (queued count includes `launching` — it is a sub-state of "not running yet"), the current active batch with per-item statuses, recent failed rows with their error, and process-log paths shown only as mono provenance text (no log viewer).

- [ ] **Step 1: Write the failing tests**

Create `webui/src/components/launch/LaunchQueuePanel.test.tsx`:

```typescript
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { LaunchQueueState } from "@/api/types"

import { LaunchQueuePanel } from "./LaunchQueuePanel"

const queue: LaunchQueueState = {
  default_concurrency: 4,
  summary: { queued: 2, launching: 1, running: 1, completed: 7, failed: 1 },
  batches: [
    {
      id: "BATCH-20260607-abcdef",
      status: "running",
      concurrency: 3,
      created: "2026-06-07T02:30:00",
      items: [
        {
          id: "LAUNCH-12345678",
          row_index: 0,
          repo_url: "https://github.com/apache/commons-cli.git",
          workspace_id: "sag-commons-cli-111",
          ref: "rel/commons-cli-1.11.0",
          status: "running",
          pid: 12345,
          exit_code: null,
          error: null,
          process_log:
            "logs/project_launches/BATCH-20260607-abcdef/LAUNCH-12345678.log",
        },
      ],
    },
    {
      id: "BATCH-20260606-ffffff",
      status: "failed",
      concurrency: 2,
      created: "2026-06-06T01:00:00",
      items: [
        {
          id: "LAUNCH-87654321",
          row_index: 0,
          repo_url: "https://github.com/x/broken.git",
          workspace_id: "sag-broken",
          ref: null,
          status: "failed",
          pid: 222,
          exit_code: 1,
          error: "sag project exited with code 1",
          process_log:
            "logs/project_launches/BATCH-20260606-ffffff/LAUNCH-87654321.log",
        },
      ],
    },
  ],
}

describe("LaunchQueuePanel", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders compact status counts", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("3 queued")).toBeInTheDocument()
    expect(screen.getByText("1 running")).toBeInTheDocument()
    expect(screen.getByText("7 completed")).toBeInTheDocument()
    expect(screen.getByText("1 failed")).toBeInTheDocument()
  })

  it("shows the active batch with its items", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("BATCH-20260607-abcdef")).toBeInTheDocument()
    expect(screen.getByText("sag-commons-cli-111")).toBeInTheDocument()
  })

  it("lists recent failed launches with error and process log provenance", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("sag-broken")).toBeInTheDocument()
    expect(screen.getByText(/exited with code 1/)).toBeInTheDocument()
    expect(
      screen.getByText(
        "logs/project_launches/BATCH-20260606-ffffff/LAUNCH-87654321.log",
      ),
    ).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd webui && npx vitest run src/components/launch/LaunchQueuePanel.test.tsx`
Expected: FAIL — cannot resolve `./LaunchQueuePanel`

- [ ] **Step 3: Write the component**

Create `webui/src/components/launch/LaunchQueuePanel.tsx`:

```tsx
import { Rocket } from "lucide-react"

import type { LaunchQueueBatch, LaunchQueueItem, LaunchQueueState } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card, CardHead } from "@/components/common/Card"

interface LaunchQueuePanelProps {
  queue: LaunchQueueState
}

const MAX_FAILED_ROWS = 5

export function LaunchQueuePanel({ queue }: LaunchQueuePanelProps) {
  const { summary } = queue
  const activeBatch = queue.batches.find((batch) => batch.status === "running") ?? null
  const failedItems = queue.batches
    .flatMap((batch) => batch.items.filter((item) => item.status === "failed"))
    .slice(0, MAX_FAILED_ROWS)

  return (
    <Card className="mt-5">
      <CardHead
        icon={<Rocket size={14} className="text-slate-400" />}
        title="Launch queue"
        sub="Web-triggered sag project setups"
        right={
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={summary.queued + summary.launching ? "blue" : "neutral"}>
              {summary.queued + summary.launching} queued
            </Badge>
            <Badge tone={summary.running ? "blue" : "neutral"}>
              {summary.running} running
            </Badge>
            <Badge tone={summary.completed ? "green" : "neutral"}>
              {summary.completed} completed
            </Badge>
            <Badge tone={summary.failed ? "red" : "neutral"}>
              {summary.failed} failed
            </Badge>
          </div>
        }
      />
      <div className="px-4 py-3">
        {activeBatch ? (
          <ActiveBatch batch={activeBatch} />
        ) : (
          <div className="text-[12px] text-slate-400">No batch is currently running.</div>
        )}
        {failedItems.length ? <FailedRows items={failedItems} /> : null}
      </div>
    </Card>
  )
}

function ActiveBatch({ batch }: { batch: LaunchQueueBatch }) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-slate-600">{batch.id}</span>
        <StatusBadge status={batch.status} />
        <span className="font-mono text-[10px] text-slate-400">
          concurrency {batch.concurrency}
        </span>
      </div>
      <div className="mt-2 grid gap-1.5">
        {batch.items.map((item) => (
          <div key={item.id} className="flex min-w-0 items-center gap-2">
            <StatusBadge status={item.status} />
            <span className="truncate font-mono text-[11px] text-slate-600">
              {item.workspace_id}
            </span>
            {item.ref ? (
              <span className="truncate font-mono text-[10px] text-slate-400">
                {item.ref}
              </span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}

function FailedRows({ items }: { items: LaunchQueueItem[] }) {
  return (
    <div className="mt-3 border-t border-slate-100 pt-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
        Recent failures
      </div>
      <div className="mt-1.5 grid gap-2">
        {items.map((item) => (
          <div key={item.id} className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate font-mono text-[11px] text-slate-600">
                {item.workspace_id}
              </span>
              <span className="truncate text-[12px] text-red-600">{item.error}</span>
            </div>
            <div className="truncate font-mono text-[10px] text-slate-400">
              {item.process_log}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd webui && npx vitest run src/components/launch/LaunchQueuePanel.test.tsx`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/launch/LaunchQueuePanel.tsx webui/src/components/launch/LaunchQueuePanel.test.tsx
git commit -m "Add dashboard launch queue panel"
```

### Task 13: Dashboard integration — button, panel slot, highlight

**Files:**
- Modify: `webui/src/pages/Dashboard.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`

All new props are optional, so the existing Dashboard tests keep passing unchanged.

- [ ] **Step 1: Write the failing tests**

Read `webui/src/pages/Dashboard.test.tsx` first; it has a `dashboard: DashboardResponse` fixture whose first workspace id is `sag-commons-cli`. Append inside the existing `describe("Dashboard", ...)` block:

```typescript
  it("renders the launch setups action and reports clicks", () => {
    const onLaunchSetups = vi.fn()
    render(
      <Dashboard
        data={dashboard}
        onLaunchSetups={onLaunchSetups}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    expect(onLaunchSetups).toHaveBeenCalled()
  })

  it("renders the launch queue panel when queue data has batches", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={{
          default_concurrency: 4,
          summary: { queued: 1, launching: 0, running: 0, completed: 0, failed: 0 },
          batches: [
            {
              id: "BATCH-20260607-abcdef",
              status: "running",
              concurrency: 2,
              created: "2026-06-07T02:30:00",
              items: [],
            },
          ],
        }}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    expect(screen.getByText("Launch queue")).toBeInTheDocument()
  })

  it("highlights newly launched workspaces", () => {
    render(
      <Dashboard
        data={dashboard}
        highlightedWorkspaces={["sag-commons-cli"]}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    const rows = screen.getAllByLabelText(/open workspace/i)
    const highlighted = rows.filter((row) => row.className.includes("bg-blue-50"))
    expect(highlighted.length).toBeGreaterThan(0)
  })
```

If the test file does not already import `fireEvent`, extend its `@testing-library/react` import to include it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd webui && npx vitest run src/pages/Dashboard.test.tsx`
Expected: new tests FAIL (no "Launch setups" button, no panel, no highlight); existing tests still pass

- [ ] **Step 3: Modify `Dashboard.tsx`**

Make these exact edits to `webui/src/pages/Dashboard.tsx`:

1. Extend imports: add `Rocket` to the `lucide-react` import list; add the queue type and panel:

```typescript
import type { DashboardResponse, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { LaunchQueuePanel } from "@/components/launch/LaunchQueuePanel"
```

2. Extend `DashboardProps`:

```typescript
interface DashboardProps {
  data: DashboardResponse
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onRefresh?: () => void
  refreshing?: boolean
  onLaunchSetups?: () => void
  launchQueue?: LaunchQueueState | null
  highlightedWorkspaces?: string[]
}
```

3. Update the `Dashboard` function signature and body. Destructure the new props with defaults:

```typescript
export function Dashboard({
  data,
  onOpenWorkspace,
  onOpenSession,
  onRefresh,
  refreshing = false,
  onLaunchSetups,
  launchQueue = null,
  highlightedWorkspaces = [],
}: DashboardProps) {
```

In the header actions `div` (the one holding the Docker badge and Refresh button), add the primary action **before** the Refresh button:

```tsx
          {onLaunchSetups ? (
            <Button onClick={onLaunchSetups} type="button">
              <Rocket size={14} />
              Launch setups
            </Button>
          ) : null}
```

After the summary-cards `div` (`<div className="mt-5 grid gap-3 sm:grid-cols-3">...</div>`) and before the table `Card`, add:

```tsx
      {launchQueue && launchQueue.batches.length > 0 ? (
        <LaunchQueuePanel queue={launchQueue} />
      ) : null}
```

Pass the highlight flag into both workspace lists by changing the two `.map` calls:

```tsx
        {workspaces.map((workspace) => (
          <WorkspaceRow
            key={workspace.id}
            highlighted={highlightedWorkspaces.includes(workspace.id)}
            onOpenSession={onOpenSession}
            onOpenWorkspace={onOpenWorkspace}
            workspace={workspace}
          />
        ))}
```

(and the same `highlighted={...}` prop on `WorkspaceCard` in the mobile list).

4. Add the `highlighted` prop to `WorkspaceRow` — restrained low-saturation background, cleared by App after ~8s:

```typescript
function WorkspaceRow({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  highlighted = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  highlighted?: boolean
}) {
```

and change its root `className` template to append the highlight:

```tsx
      className={`group grid ${tableColumns} cursor-pointer items-center gap-3 border-b border-slate-100 px-4 py-3 text-left transition-colors duration-700 last:border-b-0 hover:bg-slate-50/70 focus-visible:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30 ${
        highlighted ? "bg-blue-50/60" : ""
      }`}
```

5. Add the same prop to `WorkspaceCard`:

```typescript
function WorkspaceCard({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  highlighted = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  highlighted?: boolean
}) {
```

and change its `Card` className to:

```tsx
      className={`cursor-pointer p-4 transition-colors duration-700 hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30 ${
        highlighted ? "border-blue-200 bg-blue-50/60" : ""
      }`}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd webui && npx vitest run src/pages/Dashboard.test.tsx`
Expected: all pass (existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx
git commit -m "Add launch action, queue panel, and highlight to dashboard"
```

### Task 14: App integration — dialog state, queue polling, highlights, notice

**Files:**
- Modify: `webui/src/App.tsx`

This is wiring of already-tested pieces; behavior is covered by the component tests above plus the manual smoke in Task 15.

- [ ] **Step 1: Make these exact edits to `webui/src/App.tsx`**

1. Extend the react import and API imports:

```typescript
import { useCallback, useEffect, useRef, useState } from "react"

import {
  fetchDashboard,
  fetchLaunchQueue,
  fetchSession,
  submitProjectBatch,
  submitTask,
} from "@/api/client"
```

and add to the type import list: `LaunchBatchResult` and `LaunchQueueState`. Add the dialog import next to the other component imports:

```typescript
import { LaunchSetupsDialog } from "@/components/launch/LaunchSetupsDialog"
```

2. Add a highlight duration constant next to the poll constants:

```typescript
const LAUNCH_HIGHLIGHT_MS = 8000
```

3. Inside `App()`, after the existing `loading` state, add:

```typescript
  const [launchQueue, setLaunchQueue] = useState<LaunchQueueState | null>(null)
  const [launchDialogOpen, setLaunchDialogOpen] = useState(false)
  const [launchNotice, setLaunchNotice] = useState<string | null>(null)
  const [highlightedWorkspaces, setHighlightedWorkspaces] = useState<string[]>([])
  const highlightTimers = useRef<number[]>([])

  const loadLaunchQueue = useCallback(async () => {
    try {
      setLaunchQueue(await fetchLaunchQueue())
    } catch {
      // Queue state is auxiliary; dashboard errors are reported separately.
    }
  }, [])

  useEffect(() => {
    return () => {
      highlightTimers.current.forEach((timer) => window.clearTimeout(timer))
    }
  }, [])
```

4. Fold queue polling into the existing dashboard effects — change the initial-load effect and the interval effect to:

```typescript
  useEffect(() => {
    void loadDashboard()
    void loadLaunchQueue()
  }, [loadDashboard, loadLaunchQueue])

  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadDashboard({ silent: true })
      void loadLaunchQueue()
    }, DASHBOARD_POLL_MS)

    return () => window.clearInterval(interval)
  }, [loadDashboard, loadLaunchQueue])
```

5. Add the submit-handled callback after `submitWorkspaceTask`:

```typescript
  const handleBatchSubmitted = (result: LaunchBatchResult) => {
    setLaunchDialogOpen(false)
    setLaunchNotice(
      result.rejected.length
        ? `${result.accepted.length} setup${result.accepted.length === 1 ? "" : "s"} launched, ` +
            `${result.rejected.length} rejected: ` +
            result.rejected
              .map((row) => row.message)
              .join("; ")
        : null,
    )

    const ids = result.accepted.map((row) => row.workspace_id)
    if (ids.length) {
      setHighlightedWorkspaces((current) => [...new Set([...current, ...ids])])
      const timer = window.setTimeout(() => {
        setHighlightedWorkspaces((current) => current.filter((id) => !ids.includes(id)))
      }, LAUNCH_HIGHLIGHT_MS)
      highlightTimers.current.push(timer)
    }

    void loadDashboard({ silent: true })
    void loadLaunchQueue()
  }
```

6. Render the notice between the `routeError` card block and the `route.view === "dashboard"` block:

```tsx
      {launchNotice ? (
        <div className="mx-auto max-w-[1180px] px-4 pt-5 sm:px-6 lg:px-8">
          <Card className="flex flex-col gap-3 border-blue-100 bg-blue-50/50 px-4 py-3 text-[13px] sm:flex-row sm:items-center sm:justify-between">
            <div className="text-blue-700">{launchNotice}</div>
            <Button onClick={() => setLaunchNotice(null)} type="button" variant="outline">
              Dismiss
            </Button>
          </Card>
        </div>
      ) : null}
```

7. Pass the new props to `<Dashboard ...>`:

```tsx
        <Dashboard
          data={dashboard}
          highlightedWorkspaces={highlightedWorkspaces}
          launchQueue={launchQueue}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={() => void loadDashboard()}
          refreshing={loading}
        />
```

8. Render the dialog just before the closing `</div>` of the App root:

```tsx
      {launchDialogOpen ? (
        <LaunchSetupsDialog
          defaultConcurrency={launchQueue?.default_concurrency ?? 1}
          onClose={() => setLaunchDialogOpen(false)}
          onSubmit={submitProjectBatch}
          onSubmitted={handleBatchSubmitted}
        />
      ) : null}
```

- [ ] **Step 2: Run the full frontend suite and typecheck**

Run: `cd webui && npm test && npx tsc -b`
Expected: all tests pass, no type errors

- [ ] **Step 3: Commit**

```bash
git add webui/src/App.tsx
git commit -m "Wire batch launch dialog, queue polling, and highlights into app"
```

### Task 15: Build, full verification, smoke

**Files:**
- Modify: `src/sag/web/static/*` (regenerated bundle)

- [ ] **Step 1: Run the complete backend suite**

Run: `uv run pytest`
Expected: all pass

- [ ] **Step 2: Run the complete frontend suite and rebuild the bundle**

Run: `cd webui && npm test && npm run build`
Expected: tests pass; build succeeds and rewrites `src/sag/web/static/` (new hashed asset names; `git status` shows changes under `src/sag/web/static/`)

- [ ] **Step 3: Manual smoke (requires Docker running)**

```bash
uv run sag ui --port 8123
```

Open `http://127.0.0.1:8123`, then:
1. Dashboard shows the `Launch setups` button.
2. Open the dialog, paste two `repo_url ref` lines into the repo cell — two rows appear with refs filled.
3. Submit a row for a repo whose `sag-<name>` container already exists — the row shows a conflict error and is not enqueued.
4. Submit a valid row — dialog closes, queue panel appears with the batch, the predicted workspace row gets a soft blue highlight that fades after ~8s, and a process log appears under `logs/project_launches/<batch_id>/`.
5. Confirm no setup output appears in the `sag ui` terminal, and `logs/launch_queue.sqlite3` exists.
6. Stop and restart `sag ui` mid-setup — the interrupted row is reconciled (failed with a restart message, or completed if the workspace exists).

If Docker isn't available, verify steps 1-2 plus the 422/409 behaviors against the demo server (`uv run sag ui --demo`) and note the skipped checks in the final report.

- [ ] **Step 4: Commit the rebuilt assets**

```bash
git add src/sag/web/static
git commit -m "Rebuild web UI bundle with batch launch feature"
```

---

## Self-review notes (already applied)

- **Spec coverage check:** submit/read API shapes and status codes (Tasks 6-8), SQLite schema + WAL + atomic claim (Tasks 2-3), CLI-equivalent argv via `python -m sag.main` (Task 1, asserted equivalent to `sag project ...`), per-launch process logs + pid/exit persistence (Task 4), conflict precheck with CLI naming rules (Task 6), worker resume + stale reconcile (Task 5), CPU-aware default + per-batch override + global cap (Tasks 3, 6), table-first dialog with paste quick-format (Tasks 10-11), queue panel counts/failures/provenance (Task 12), restrained 8s highlight (Tasks 13-14), webui build into tracked static assets (Task 15). Non-goals respected: no log streaming, no daemon, no scheduler smartness.
- **Type consistency:** `LaunchItem`/`LaunchBatch` field names match the SQL schema and the test factories; `claim_next(global_cap, now)`, `mark_running(item_id, pid, now)`, `mark_completed(item_id, exit_code, now)`, `mark_failed(item_id, error, now, exit_code=None)` are used with identical signatures in Tasks 3, 4, 6, 7. Frontend `LaunchBatchResult = LaunchBatchResponse + status` is what both the client (Task 9) and dialog (Task 11) use.
- **Known judgment calls** (flag to the user if they disagree): rows whose predicted workspace duplicates an earlier row in the same batch are rejected as conflicts; un-derivable repo URLs get row-level status `invalid` rather than failing the whole request; a stale row whose process is still alive after restart is left `running` rather than adopted by a new monitor.
- **Known v1 limitations** (accepted during code review): an orphaned `running` row whose still-alive process outlives a UI restart is never resolved by the current run — once that process dies, the row keeps consuming batch/global capacity until the next `sag ui` restart reconciles it (a safe in-run reconcile would need run-ownership tracking; deliberately out of scope). Reconcile's "workspace exists" path records a synthetic `exit_code=0` the process never reported; the UI does not display exit codes, so this affects the API payload only. The conflict precheck constructs a fresh Docker client (connect + ping) per row — spec-mandated reuse of `DockerOrchestrator.container_exists` semantics; negligible for typical batch sizes over the local socket, revisit if very large batches become common.

