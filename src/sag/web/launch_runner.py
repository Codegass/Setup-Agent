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
