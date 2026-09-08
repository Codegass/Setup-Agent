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

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._recovery_complete = False
        try:
            self.reconcile_stale()
            self._recovery_complete = True
        except Exception:
            # A failed reconcile must not leave the UI without a scheduler.
            logger.exception("Stale launch reconcile failed")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sag-launch-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("Launch scheduler thread did not stop within 5s")
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

        A surviving process keeps its capacity while the scheduler watches it.
        Its exit status cannot be recovered by a new parent process, so losing
        it records an unavailable outcome, never inferred setup success.
        """

        self._recovered_ids.clear()
        for item in self.store.unfinished_items():
            if item.pid is not None and _pid_alive(item.pid):
                self._recovered_ids.add(item.id)
                continue
            self._mark_recovery_unavailable(item.id)

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
        while not self._stop.is_set():
            try:
                if not self._recovery_complete:
                    # Retry startup failures before creating children of this
                    # scheduler, so they cannot be mistaken for recovered PIDs.
                    self.reconcile_stale()
                    self._recovery_complete = True
                self._reconcile_recovered()
                self.launch_ready()
            except Exception:
                logger.exception("Launch scheduler iteration failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def _start_item(self, item: LaunchItem) -> None:
        try:
            process = self.spawn(item.command, Path(item.process_log))
        except Exception as exc:
            self.store.mark_failed(item.id, f"Failed to start subprocess: {exc}", now=_now())
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
            self._wake.set()
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
        # Freed capacity: nudge the scheduler instead of waiting out the poll.
        self._wake.set()
