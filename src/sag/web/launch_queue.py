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
        except BaseException:
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
