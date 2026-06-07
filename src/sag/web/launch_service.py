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
