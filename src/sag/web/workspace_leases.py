"""Process-lifetime locks backing the web queue's task/delete occupancy rows."""

from __future__ import annotations

import errno
import os
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from loguru import logger

if TYPE_CHECKING:
    from sag.web.launch_queue import LaunchQueueStore


def canonical_workspace_id(workspace_id: str) -> str:
    """Use the exact Docker container name for every occupancy and UI record."""
    label = workspace_id.removeprefix("sag-")
    if not label:
        raise ValueError("Workspace identifier must have a nonempty project label")
    return f"sag-{label}"


def open_lease_lock(path: Path, *, create: bool = False) -> BinaryIO | None:
    """Try a lock without waiting; existing owners never lose their lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("x+b" if create else "r+b")
    try:
        if create:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            msvcrt = import_module("msvcrt")
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return None
        raise
    except BaseException:
        handle.close()
        raise
    return handle


class WorkspaceLease:
    """One task/delete occupancy, released after its final workspace write."""

    def __init__(
        self,
        store: LaunchQueueStore,
        workspace_id: str,
        owner_id: str,
        handle: BinaryIO,
    ):
        self.store = store
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self._handle = handle
        self._released = False

    def __enter__(self) -> WorkspaceLease:
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        removed = False
        try:
            self.store._release_workspace_lease(self.workspace_id, self.owner_id)
            removed = True
        except Exception:
            # Closing the handle still makes this row recoverable. Cleanup must
            # never replace the operation's original exception.
            logger.exception("Could not release workspace occupancy for {}", self.workspace_id)
        finally:
            try:
                self._handle.close()
            except OSError:
                logger.exception("Could not close workspace lease {}", self.owner_id)
        if removed:
            try:
                Path(self._handle.name).unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove released workspace lease {}", self.owner_id)


__all__ = ["WorkspaceLease", "canonical_workspace_id"]
