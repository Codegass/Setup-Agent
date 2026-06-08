"""Service facade for deleting Docker-based workspaces.

Deleting a workspace means three things, in this order:
  1. atomically remove its launch-queue rows (refusing if a launch is active),
  2. remove its Docker container (idempotent: already-gone counts as removed),
  3. best-effort delete the launch log files left behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from loguru import logger

from sag.web.launch_queue import LaunchQueueStore, WorkspaceBusyError
from sag.web.launch_service import DEFAULT_DB_PATH, PROCESS_LOG_ROOT


def _default_orchestrator(workspace_id: str) -> Any:
    # Deferred import mirrors session_registry: importing the orchestrator at
    # module load would drag Docker client setup into every web test.
    from sag.docker_orch.orch import DockerOrchestrator

    return DockerOrchestrator(project_name=workspace_id.removeprefix("sag-"))


class WorkspaceService:
    """The only API the web handlers use to delete a workspace."""

    def __init__(
        self,
        store: LaunchQueueStore | None = None,
        orchestrator_factory: Callable[[str], Any] | None = None,
        launches_root: Path | None = None,
    ):
        self._store = store if store is not None else LaunchQueueStore(DEFAULT_DB_PATH)
        self._orchestrator_factory = (
            orchestrator_factory
            if orchestrator_factory is not None
            else _default_orchestrator
        )
        self._launches_root = (
            Path(launches_root) if launches_root is not None else PROCESS_LOG_ROOT
        )

    def delete_workspace(self, workspace_id: str) -> dict:
        """Delete a workspace's queue rows, container, and launch logs.

        DB cleanup runs first so the atomic busy-guard can reject (via
        ``WorkspaceBusyError``, which propagates) before we touch the container.
        """

        deleted, process_logs = self._store.delete_workspace_items(workspace_id)

        # remove_project is idempotent: an already-gone container still returns
        # True, so deleting a failed/stopped workspace never errors here.
        container_removed = bool(self._orchestrator_factory(workspace_id).remove_project())

        self._cleanup_logs(process_logs)

        return {
            "workspace_id": workspace_id,
            "container_removed": container_removed,
            "queue_items_removed": deleted,
            "status": "deleted",
        }

    def _cleanup_logs(self, process_logs: list[str]) -> None:
        for raw_path in process_logs:
            log_path = Path(raw_path)
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove launch log: {}", raw_path)
                continue
            self._prune_empty_batch_dir(log_path.parent)

    def _prune_empty_batch_dir(self, batch_dir: Path) -> None:
        if not self._is_under_launches_root(batch_dir):
            return
        try:
            if batch_dir.is_dir() and not any(batch_dir.iterdir()):
                batch_dir.rmdir()
        except OSError:
            logger.exception("Failed to prune launch batch dir: {}", batch_dir)

    def _is_under_launches_root(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            root = self._launches_root.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return False
        # Never treat the launches root itself as a prunable batch dir.
        return resolved != root


__all__ = ["WorkspaceService", "WorkspaceBusyError"]
