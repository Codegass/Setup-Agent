"""SQLite persistence for Web-triggered project launch batches."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sag.web.workspace_leases import (
    WorkspaceLease,
    canonical_workspace_id,
    open_lease_lock,
)

ALL_STATUSES = ("queued", "launching", "running", "completed", "failed")


class WorkspaceBusyError(RuntimeError):
    """Raised when another launch, task, or delete owns the workspace.

    Deleting such a workspace would orphan an in-flight setup, so the store
    refuses and changes nothing.
    """


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
CREATE TABLE IF NOT EXISTS workspace_leases (
    workspace_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('task', 'delete'))
);
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
        # SQLite aliases must also share the same owner-lock directory.
        self.db_path = Path(db_path).resolve()

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
            self._reap_abandoned_workspace_leases(conn)
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _lease_path(self, owner_id: str) -> Path:
        # Never reuse an owner's file: unlinking/recreating a live lock path
        # could create two independent locks for the same occupancy.
        key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        return self.db_path.parent / f"{self.db_path.name}.leases" / f"{key}.lock"

    def _reap_abandoned_workspace_leases(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT workspace_id, owner_id FROM workspace_leases").fetchall():
            path = self._lease_path(row["owner_id"])
            try:
                handle = open_lease_lock(path)
            except FileNotFoundError:
                handle = None
            else:
                if handle is None:
                    continue  # A task/delete still owns the OS lock.
            try:
                conn.execute(
                    "DELETE FROM workspace_leases WHERE workspace_id = ? AND owner_id = ?",
                    (row["workspace_id"], row["owner_id"]),
                )
            finally:
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.close()
            path.unlink(missing_ok=True)

    @staticmethod
    def _occupied_workspaces(
        conn: sqlite3.Connection, *, include_queued: bool = True, lease_owner: str | None = None
    ) -> set[str]:
        statuses = (
            "'queued', 'launching', 'running'" if include_queued else "'launching', 'running'"
        )
        return {
            canonical_workspace_id(row[0])
            for row in conn.execute(
                f"SELECT workspace_id FROM launch_items WHERE status IN ({statuses})"
            )
        } | {
            canonical_workspace_id(row[0])
            for row in conn.execute(
                "SELECT workspace_id FROM workspace_leases WHERE owner_id != ?",
                (lease_owner or "",),
            )
        }

    def acquire_workspace_lease(
        self, workspace_id: str, operation: Literal["task", "delete"]
    ) -> WorkspaceLease:
        if operation not in {"task", "delete"}:
            raise ValueError("unsupported workspace occupancy operation")
        workspace_id = canonical_workspace_id(workspace_id)
        owner_id = uuid4().hex
        path = self._lease_path(owner_id)
        handle = open_lease_lock(path, create=True)
        if handle is None:
            raise RuntimeError("new workspace lease could not be locked")
        try:
            with contextlib.closing(self._connect()) as conn:
                with self._transaction(conn):
                    if workspace_id in self._occupied_workspaces(
                        conn, include_queued=operation == "task"
                    ):
                        raise WorkspaceBusyError(f"Workspace is in use: {workspace_id}")
                    conn.execute(
                        "INSERT INTO workspace_leases (workspace_id, owner_id, operation) VALUES (?, ?, ?)",
                        (workspace_id, owner_id, operation),
                    )
        except BaseException:
            with contextlib.suppress(OSError):
                handle.close()
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            raise
        return WorkspaceLease(self, workspace_id, owner_id, handle)

    def _release_workspace_lease(self, workspace_id: str, owner_id: str) -> None:
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                conn.execute(
                    "DELETE FROM workspace_leases WHERE workspace_id = ? AND owner_id = ?",
                    (workspace_id, owner_id),
                )

    def enqueue_batch(self, batch: LaunchBatch, items: list[LaunchItem]) -> list[LaunchItem]:
        """Admit distinct active workspaces atomically; return conflicting rows."""
        items = [
            replace(item, workspace_id=canonical_workspace_id(item.workspace_id)) for item in items
        ]
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                active = self._occupied_workspaces(conn)
                admitted: list[LaunchItem] = []
                rejected: list[LaunchItem] = []
                for item in items:
                    is_active = item.status in {"queued", "launching", "running"}
                    if is_active and item.workspace_id in active:
                        rejected.append(item)
                    else:
                        admitted.append(item)
                        if is_active:
                            active.add(item.workspace_id)
                if not admitted:
                    return rejected
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
                        len(admitted),
                        batch.rejected + len(rejected),
                    ),
                )
                for item in admitted:
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
        return rejected

    def summary_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ALL_STATUSES}
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM launch_items GROUP BY status")
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
                    "SELECT COUNT(*) FROM launch_items" " WHERE status IN ('launching', 'running')"
                ).fetchone()[0]
                if active < global_cap:
                    row = conn.execute(
                        "SELECT i.* FROM launch_items i"
                        " JOIN launch_batches b ON b.id = i.batch_id"
                        " WHERE i.status = 'queued'"
                        "   AND NOT EXISTS (SELECT 1 FROM workspace_leases w WHERE w.workspace_id = i.workspace_id)"
                        "   AND ("
                        "     SELECT COUNT(*) FROM launch_items a"
                        "     WHERE a.batch_id = i.batch_id"
                        "       AND a.status IN ('launching', 'running')"
                        "   ) < b.concurrency"
                        " ORDER BY i.created_at, i.row_index, i.id"
                        " LIMIT 1"
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            "UPDATE launch_items"
                            " SET status = 'launching', started_at = ?"
                            " WHERE id = ?",
                            (now, row["id"]),
                        )
                        claimed = replace(_item_from_row(row), status="launching", started_at=now)
            return claimed

    def mark_running(self, item_id: str, pid: int, now: str) -> None:
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                conn.execute(
                    "UPDATE launch_items"
                    " SET status = 'running', pid = ?, started_at = COALESCE(started_at, ?)"
                    " WHERE id = ? AND status = 'launching'",
                    (pid, now, item_id),
                )

    def mark_completed(self, item_id: str, exit_code: int, now: str) -> None:
        self._finish(item_id, "completed", exit_code=exit_code, error=None, now=now)

    def mark_failed(self, item_id: str, error: str, now: str, exit_code: int | None = None) -> None:
        self._finish(item_id, "failed", exit_code=exit_code, error=error, now=now)

    def unfinished_items(self) -> list[LaunchItem]:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM launch_items"
                " WHERE status IN ('launching', 'running')"
                " ORDER BY created_at, row_index"
            ).fetchall()
            return [_item_from_row(row) for row in rows]

    def active_workspace_ids(self) -> set[str]:
        """Workspace ids reserved by pending launches or task/delete operations."""

        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                return self._occupied_workspaces(conn)

    def is_workspace_busy(self, workspace_id: str) -> bool:
        """Return True for executing launches and task/delete reservations.

        Counterpart to the atomic busy-guard in
        ``delete_workspace_items``. Lets a caller reject a busy workspace before
        touching Docker, so busy rejection works even when the daemon is down.
        Dead task/delete reservations are reclaimed before the query.
        """

        workspace_id = canonical_workspace_id(workspace_id)
        with contextlib.closing(self._connect()) as conn:
            with self._transaction(conn):
                return workspace_id in self._occupied_workspaces(conn, include_queued=False)

    def delete_workspace_items(
        self, workspace_id: str, *, lease_owner: str | None = None
    ) -> tuple[int, list[str]]:
        """Atomically delete every launch_item for ``workspace_id``.

        Inside one ``BEGIN IMMEDIATE`` transaction: refuse active launches and
        task/delete leases other than the caller's ``lease_owner``. Otherwise
        delete the matching items, drop empty batches, and return the deleted
        count with their ``process_log`` paths. The caller keeps its delete
        lease until container removal ends. Zero matching items returns ``(0, [])``.
        """

        workspace_id = canonical_workspace_id(workspace_id)
        with contextlib.closing(self._connect()) as conn:
            deleted = 0
            process_logs: list[str] = []
            with self._transaction(conn):
                if workspace_id in self._occupied_workspaces(
                    conn, include_queued=False, lease_owner=lease_owner
                ):
                    raise WorkspaceBusyError(f"Workspace has an active launch: {workspace_id}")

                rows = conn.execute(
                    "SELECT batch_id, process_log FROM launch_items" " WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchall()
                process_logs = [row["process_log"] for row in rows]
                deleted = len(rows)
                batch_ids = {row["batch_id"] for row in rows}

                conn.execute(
                    "DELETE FROM launch_items WHERE workspace_id = ?",
                    (workspace_id,),
                )

                for batch_id in batch_ids:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM launch_items WHERE batch_id = ?",
                        (batch_id,),
                    ).fetchone()[0]
                    if remaining == 0:
                        conn.execute("DELETE FROM launch_batches WHERE id = ?", (batch_id,))
                    else:
                        # A surviving batch may now have a different makeup (e.g.
                        # its only failed item was removed): recompute its status
                        # so list_batches does not report a stale 'failed'.
                        self._refresh_batch_status(conn, batch_id)
            return deleted, process_logs

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
                "SELECT COUNT(*) FROM launch_items" " WHERE batch_id = ? AND status = 'failed'",
                (batch_id,),
            ).fetchone()[0]
            status = "failed" if failed else "completed"
        conn.execute("UPDATE launch_batches SET status = ? WHERE id = ?", (status, batch_id))


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
