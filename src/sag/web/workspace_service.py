"""Service facade for deleting Docker-based workspaces.

Deleting a workspace means, in this order:
  1. reject a busy workspace up front (an active launch must not be orphaned),
  2. build the Docker orchestrator *before* any DB write, so an unreachable
     daemon leaves the launch history intact and the delete stays retryable,
  3. atomically remove its launch-queue rows (a final atomic busy re-check),
  4. remove its Docker container, surfacing a partial failure if it persists,
  5. best-effort delete the launch log files left behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from loguru import logger

from sag.web.launch_queue import LaunchQueueStore, WorkspaceBusyError
from sag.web.launch_service import DEFAULT_DB_PATH, PROCESS_LOG_ROOT


class WorkspaceDeletionError(RuntimeError):
    """Raised when a workspace's container could not be fully removed.

    Covers both an unreachable Docker daemon (raised before any DB row is
    deleted, so the operation is retryable without data loss) and a container
    that survives ``remove_project`` (raised after the queue rows are cleared,
    so the caller learns the workspace was not actually removed instead of
    seeing a false success).
    """


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

        Ordering is chosen so a partial failure never silently loses data:

        * Reject a busy workspace up front (``WorkspaceBusyError``) without
          constructing a Docker client, so an active launch is never orphaned
          and busy rejection still works when the daemon is down.
        * Build the orchestrator *before* deleting any DB rows. Construction
          pings the Docker daemon, so an unreachable daemon raises
          ``WorkspaceDeletionError`` while the launch history is still intact
          and the delete stays retryable.
        * After removing the container, re-probe its existence:
          ``remove_project`` swallows Docker errors and merely returns
          ``False``. If the container genuinely persists we raise
          ``WorkspaceDeletionError`` instead of reporting success, so the
          workspace does not silently reappear with its history wiped.
        """

        if self._store.is_workspace_busy(workspace_id):
            raise WorkspaceBusyError(f"Workspace has an active launch: {workspace_id}")

        try:
            orchestrator = self._orchestrator_factory(workspace_id)
        except Exception as exc:  # unreachable daemon, bad config, ...
            # No DB row has been touched yet: leave the queue history intact so
            # the user can retry once Docker is back.
            raise WorkspaceDeletionError(
                f"Could not reach Docker to delete {workspace_id}; the launch "
                "history was left intact. Retry once Docker is available."
            ) from exc

        # Atomic busy re-check + row deletion. Guards the (tiny) window between
        # the pre-check above and here; on busy it raises and deletes nothing.
        deleted, process_logs = self._store.delete_workspace_items(workspace_id)

        # remove_project is idempotent for an already-gone container (returns
        # True), but only logs-and-returns-False on a real Docker error. Re-probe
        # so we distinguish "actually gone" from "stuck container still present".
        container_removed = bool(orchestrator.remove_project())
        if not container_removed and self._container_persists(orchestrator):
            # The rows are already gone; clean their logs, then surface the
            # partial failure rather than reporting a success the container
            # never honored.
            self._cleanup_logs(process_logs)
            raise WorkspaceDeletionError(
                f"Launch history for {workspace_id} was cleared, but its Docker "
                "container could not be removed. Retry the delete or remove the "
                "container manually."
            )

        self._cleanup_logs(process_logs)

        return {
            "workspace_id": workspace_id,
            # Reaching here means the container is confirmed gone (removed, or
            # re-probed as absent despite a swallowed error).
            "container_removed": True,
            "queue_items_removed": deleted,
            "status": "deleted",
        }

    @staticmethod
    def _container_persists(orchestrator: Any) -> bool:
        """Return True when the container survived a failed ``remove_project``.

        ``remove_project`` returning ``False`` only means it swallowed an error,
        not that the container survived, so re-probe to avoid both false success
        (silently keeping a stuck container) and false failure. If the probe is
        unavailable or itself errors, assume the container persists so the
        failure is surfaced rather than masked.
        """

        probe = getattr(orchestrator, "container_exists", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            logger.exception("Failed to re-check container existence after removal")
            return True

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


__all__ = ["WorkspaceService", "WorkspaceBusyError", "WorkspaceDeletionError"]
