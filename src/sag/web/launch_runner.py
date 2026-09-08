"""Background scheduler that runs queued project setups as CLI subprocesses."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from loguru import logger

from sag.web.launch_queue import LaunchItem, LaunchQueueStore
from sag.web.workspace_leases import open_lease_lock


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
    """Whether a launch subprocess with this pid is still running.

    Treats EPERM as dead: launches always run under the server's own UID, so a
    permission error means the pid was reused by another user's process. POSIX
    only — on Windows ``os.kill(pid, 0)`` would terminate the target.
    """

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
        self._recovered_ids: set[str] = set()
        self._recovery_complete = False
        self._owner_handle: BinaryIO | None = None
        self._owner_guard = threading.RLock()
        self._owned_children: set[str] = set()
        self._loop_active = False

    def _acquire_owner(self) -> None:
        """Called under the instance guard; the file is never unlinked."""
        if self._owner_handle is not None:
            return
        database = self.store.db_path.resolve()
        path = database.with_name(database.name + ".scheduler.lock")
        try:
            handle = open_lease_lock(path, create=True)
        except FileExistsError:
            handle = open_lease_lock(path)
        if handle is None:
            raise RuntimeError("Another launch scheduler owner is active for this queue")
        self._owner_handle = handle
        self._recovery_complete = False

    def _release_owner_if_idle(self) -> None:
        """Keep ownership through every live child's final database write."""
        if self._stop.is_set() and not self._loop_active and not self._owned_children:
            if self._owner_handle is not None:
                self._owner_handle.close()
                self._owner_handle = None

    def start(self) -> None:
        with self._owner_guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._acquire_owner()
            self._stop.clear()
            try:
                self.reconcile_stale()
            except Exception:
                logger.exception("Stale launch reconcile failed")
            self._loop_active = True
            try:
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="sag-launch-scheduler"
                )
                self._thread.start()
            except BaseException:
                self._thread = None
                self._loop_active = False
                self._stop.set()
                self._release_owner_if_idle()
                raise

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("Launch scheduler thread did not stop within 5s")
        # A blocked spawn/recovery still owns the guard. Its finally block
        # releases after it finishes; stop must not hand its queue to a rival.
        if self._owner_guard.acquire(blocking=False):
            try:
                self._release_owner_if_idle()
            finally:
                self._owner_guard.release()

    def wake(self) -> None:
        """Nudge the worker loop so new submissions start without polling delay."""

        self._wake.set()

    def launch_ready(self) -> None:
        """Claim work only after obtaining this queue's scheduler authority."""
        with self._owner_guard:
            self._acquire_owner()
            try:
                if not self._recovery_complete:
                    self.reconcile_stale()
                while not self._stop.is_set():
                    item = self.store.claim_next(self.global_cap, _now())
                    if item is None:
                        return
                    self._start_item(item)
            finally:
                self._release_owner_if_idle()

    def reconcile_stale(self) -> None:
        """Recover old children once per ownership, never infer their exit code."""
        with self._owner_guard:
            self._acquire_owner()
            try:
                if self._recovery_complete:
                    self._reconcile_recovered()
                    return
                self._recovered_ids.clear()
                for item in self.store.unfinished_items():
                    if item.id in self._owned_children:
                        continue
                    if item.pid is not None and _pid_alive(item.pid):
                        self._recovered_ids.add(item.id)
                    else:
                        self._mark_recovery_unavailable(item.id)
                self._recovery_complete = True
            finally:
                self._release_owner_if_idle()

    def _mark_recovery_unavailable(self, item_id: str) -> None:
        self.store.mark_failed(
            item_id,
            "Launch interrupted by UI restart; process is no longer running and its exit status is unavailable.",
            now=_now(),
        )

    def _reconcile_recovered(self) -> None:
        if not self._recovered_ids:
            return
        unfinished = {item.id: item for item in self.store.unfinished_items()}
        for item_id in tuple(self._recovered_ids):
            item = unfinished.get(item_id)
            if item is None:
                self._recovered_ids.discard(item_id)
            elif item.pid is None or not _pid_alive(item.pid):
                self._mark_recovery_unavailable(item_id)
                self._recovered_ids.discard(item_id)

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.reconcile_stale()
                    self.launch_ready()
                except Exception:
                    logger.exception("Launch scheduler iteration failed")
                self._wake.wait(self.poll_interval)
                self._wake.clear()
        finally:
            with self._owner_guard:
                self._loop_active = False
                self._release_owner_if_idle()

    def _start_item(self, item: LaunchItem) -> None:
        try:
            process = self.spawn(item.command, Path(item.process_log))
        except Exception as exc:
            self.store.mark_failed(item.id, f"Failed to start subprocess: {exc}", now=_now())
            return
        self._owned_children.add(item.id)
        try:
            self.store.mark_running(item.id, pid=process.pid, now=_now())
        finally:
            # Even a failed mark_running must not orphan a child we spawned.
            # If thread construction/start fails, retain ownership conservatively
            # until process exit releases the OS lock; never give a rival its row.
            threading.Thread(
                target=self._monitor,
                args=(item.id, process),
                daemon=True,
                name=f"sag-launch-monitor-{item.id}",
            ).start()

    def _monitor(self, item_id: str, process: Any) -> None:
        exited = False
        try:
            try:
                exit_code = process.wait()
                exited = True
            except Exception as exc:
                # Losing wait() does not prove the child exited. Keep both
                # workspace occupancy and scheduler ownership until recovery
                # can establish liveness after this parent process exits.
                logger.error("Launch {} wait failed; retaining active occupancy: {}", item_id, exc)
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
        finally:
            self._wake.set()
            with self._owner_guard:
                if exited:
                    self._owned_children.discard(item_id)
                self._release_owner_if_idle()
