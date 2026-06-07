# Batch Web Project Launch Design

## Context

SAG is often used to set up many repositories in one sitting. The Web UI should
support that workflow directly instead of forcing users to open many terminals
or launch projects one by one. At the same time, a Web-triggered setup must
behave like `sag project` from the CLI and must not mix project setup logs into
the long-running `sag ui` server process.

Existing facts:

- `sag project <repo_url>` is the authoritative setup entry point.
- `--name` changes the Docker label/container name only; it does not change the
  cloned project directory name.
- `--ref` selects a branch, tag, release tag, short commit, or full commit.
- The Web UI already submits follow-up workspace tasks with a background runner.
- The dashboard is the primary control surface for all SAG containers.

## Goals

- Let users launch many setup jobs from the Web UI in one batch.
- Default to a table/grid input model: one row per repository.
- Keep advanced row fields optional: `name`, `ref`, `goal`, and `record`.
- Persist queue state in `logs/launch_queue.sqlite3`.
- Run setup jobs through isolated CLI subprocesses, not in-process agent calls.
- Limit concurrent setup jobs with a CPU-aware default:
  `max(1, min(os.cpu_count() // 2, 4))`.
- Allow the user to override the batch concurrency for the submitted batch.
- Surface queued/running/completed/failed launch state in the dashboard.
- Give newly launched workspaces a short, restrained visual highlight.
- Keep current Web UI context-map work separate from this feature.

## Non-Goals

- No global SAG daemon.
- No multi-user authorization model.
- No remote worker fleet.
- No complex project-type-aware scheduler.
- No streaming of full setup logs into the Web UI in the first version.
- No automatic reuse of an existing workspace when a new project conflicts.

## API Design

### Submit Batch

`POST /api/project-launches/batch`

Request:

```json
{
  "concurrency": 3,
  "projects": [
    {
      "repo_url": "https://github.com/apache/commons-cli.git",
      "name": "commons-cli-111",
      "ref": "rel/commons-cli-1.11.0",
      "goal": "Setup and verify Apache Commons CLI",
      "record": true
    }
  ]
}
```

Rules:

- `repo_url` is required and whitespace-trimmed.
- `name`, `ref`, and `goal` are optional and whitespace-trimmed.
- `record` defaults to `false`.
- `concurrency` is optional. When omitted, use the CPU-aware default. When
  provided, validate it as an integer from `1` through `max(1, os.cpu_count())`.
- Invalid request shape returns `422`.
- Existing workspace conflicts are reported per row and are not enqueued.
- If at least one row is accepted, return `202`.
- If every row is rejected due to existing workspace conflicts, return `409`.

Response:

```json
{
  "batch_id": "BATCH-20260607-abcdef",
  "concurrency": 3,
  "accepted": [
    {
      "launch_id": "LAUNCH-12345678",
      "row_index": 0,
      "workspace_id": "sag-commons-cli-111",
      "status": "queued"
    }
  ],
  "rejected": [
    {
      "row_index": 1,
      "workspace_id": "sag-existing",
      "status": "conflict",
      "message": "Workspace already exists: sag-existing"
    }
  ]
}
```

### Read Queue

`GET /api/project-launches`

Returns compact queue state for the dashboard:

```json
{
  "default_concurrency": 4,
  "summary": {
    "queued": 3,
    "launching": 1,
    "running": 2,
    "completed": 7,
    "failed": 1
  },
  "batches": [
    {
      "id": "BATCH-20260607-abcdef",
      "status": "running",
      "concurrency": 3,
      "created": "2026-06-07T02:30:00",
      "items": [
        {
          "id": "LAUNCH-12345678",
          "row_index": 0,
          "repo_url": "https://github.com/apache/commons-cli.git",
          "workspace_id": "sag-commons-cli-111",
          "ref": "rel/commons-cli-1.11.0",
          "status": "running",
          "pid": 12345,
          "exit_code": null,
          "error": null,
          "process_log": "logs/project_launches/BATCH-.../LAUNCH-....log"
        }
      ]
    }
  ]
}
```

The queue API describes launch process state. Docker/read-model discovery
remains the source of truth for actual workspace status, sessions, evidence,
reports, build state, and test state.

## Queue Persistence

Use SQLite from the Python standard library.

Location:

`logs/launch_queue.sqlite3`

Tables:

- `launch_batches`
  - `id`
  - `created_at`
  - `concurrency`
  - `status`
  - `total`
  - `accepted`
  - `rejected`
- `launch_items`
  - `id`
  - `batch_id`
  - `row_index`
  - `repo_url`
  - `name`
  - `ref`
  - `goal`
  - `record`
  - `project_name`
  - `docker_label`
  - `workspace_id`
  - `status`
  - `pid`
  - `exit_code`
  - `error`
  - `command_json`
  - `process_log`
  - `created_at`
  - `started_at`
  - `finished_at`

Recommended SQLite settings:

- Enable WAL mode.
- Use short transactions for state transitions.
- Treat queued item claiming as an atomic update.

## CLI-Equivalent Launching

Each accepted row is launched as a separate subprocess that executes the CLI
project command.

The implementation should introduce a small command builder, for example:

```python
ProjectCliCommand(
    repo_url=...,
    name=...,
    ref=...,
    goal=...,
    record=...,
).argv()
```

It must produce the same project-command arguments a user would type manually:

```text
sag project <repo_url> [--name <name>] [--ref <ref>] [--goal <goal>] [--record]
```

The runner executes the current package's CLI command through the active Python
environment:

```text
<sys.executable> -m sag.main project ...
```

This subprocess enters the same Click command path as `sag project`, so default
goal generation, Docker setup, metadata writing, report generation, and exit
codes stay aligned with CLI behavior. Tests should assert the generated
project-command arguments are equivalent to `sag project ...`.

Subprocess output handling:

- Do not stream stdout/stderr into the `sag ui` server terminal.
- Redirect stdout/stderr to a per-launch process log under
  `logs/project_launches/<batch_id>/<launch_id>.log`.
- Let the CLI command create its own normal `logs/session_*` records.
- Store process `pid`, `exit_code`, and process-log path in SQLite.

## Conflict Handling

Before enqueueing an item, predict the Docker workspace id with the same naming
rules as CLI:

- `project_name = extract_project_name_from_url(repo_url)`
- `docker_label = name or project_name`
- `workspace_id = f"sag-{docker_label}"`

Then check whether the workspace already exists using the same Docker
orchestrator semantics as the CLI uses for conflict detection.

If a conflict exists:

- Do not enqueue that row.
- Return a row-level `conflict` error.
- In the UI, tell the user to open the existing workspace or choose a different
  `name`.

The CLI subprocess should still perform its own conflict check. The Web precheck
is for fast UX feedback, not the final authority.

## Worker Model

When `sag ui` starts:

1. Open or create `logs/launch_queue.sqlite3`.
2. Start a scheduler thread owned by the Web server lifespan.
3. Resume queued items from previous UI runs.
4. Reconcile stale `launching` or `running` rows whose process is no longer
   alive by marking them failed with a restart-recovery message unless Docker
   discovery clearly shows the workspace exists.

Worker loop:

1. Count active `launching` and `running` items.
2. For each batch, compare active items in that batch against the batch's stored
   `concurrency`.
3. Also enforce a global hard cap of `max(1, os.cpu_count())` active subprocesses
   across all batches.
4. If capacity is available, claim queued rows ordered by `created_at` and
   `row_index`.
5. Mark each claimed item `launching`.
6. Start its CLI subprocess with redirected logs.
7. Mark it `running` with `pid`.
8. A monitor waits for the process and marks `completed` or `failed`.

Status definitions:

- `queued`: persisted, waiting for capacity.
- `launching`: claimed and preparing subprocess.
- `running`: subprocess started and has a pid.
- `completed`: subprocess exited with code 0.
- `failed`: subprocess start failed or exited non-zero.

## Frontend Design

Dashboard adds a primary `Launch setups` action.

The launch dialog is table-first:

- Each row is one setup request.
- Required column: `repo URL`.
- Optional columns: `name`, `ref`, `goal`, `record`.
- Rows can be added/removed.
- Empty optional cells are allowed.
- `record` defaults off.
- Concurrency control appears once above or below the grid, defaulting to the
  server-provided CPU-aware value.

Paste behavior:

- Pasting multiple lines into the repo URL cell creates one row per line.
- Supported quick format:

```text
repo_url
repo_url ref
```

Examples:

```text
https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0
https://github.com/apache/dubbo.git dubbo-3.2.19
```

The quick parser only fills `repo_url` and `ref`. Users can edit optional fields
after paste.

After submit:

- Accepted rows close the dialog and refresh dashboard plus queue state.
- Rejected rows stay visible with row-level errors if no rows were accepted.
- If some rows were accepted and some rejected, show a compact result notice.
- Predicted accepted workspace ids receive a short restrained highlight in the
  dashboard. Use a low-saturation background or subtle border and clear it after
  roughly 6-10 seconds.

Dashboard queue panel:

- Show compact counts for queued/running/completed/failed.
- Show the current active batch and recent failed rows.
- Do not show full logs by default.
- Link or label process-log paths only as provenance; first version does not
  need a full log viewer.

## Testing Strategy

Backend:

- Request validation for blank repo rows and optional field trimming.
- Command builder tests for every optional flag:
  - bare repo
  - `--name`
  - `--ref`
  - `--goal`
  - `--record`
  - all options together
- Conflict precheck returns row-level conflict and does not enqueue.
- Batch submit returns `202` with accepted and rejected rows when mixed.
- Batch submit returns `409` when every row conflicts.
- SQLite queue persists items and reloads across queue-store instances.
- Worker respects concurrency limit and starts no more active subprocesses than
  allowed.
- Worker records pid, exit code, and process-log path.
- Worker marks non-zero subprocess exit as failed.

Frontend:

- Dashboard renders `Launch setups`.
- Dialog opens with one empty row and optional columns.
- User can paste multiple repo lines into the grid.
- `repo_url ref` paste fills the ref column.
- Submit sends `POST /api/project-launches/batch`.
- Successful submit refreshes dashboard and queue state.
- Accepted workspace rows receive temporary restrained highlight.
- Row-level conflict errors are shown without losing user input.
- Queue panel renders counts and recent failed launch rows.

Integration/smoke:

- `webui` test suite.
- `webui` build updates bundled static assets under `src/sag/web/static`.
- Python Web API tests.
- Packaging/import smoke tests.

## Open Implementation Notes

- Keep queue and runner modules deep: Web handlers should call a small service
  API and should not know SQLite or subprocess details.
- Keep the CLI subprocess command builder small and heavily tested.
- Avoid direct in-process calls to `SetupAgent.setup_project` from Web launch
  code.
- Do not stage or overwrite unrelated existing Web UI context-map changes while
  implementing this feature.
