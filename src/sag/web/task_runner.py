"""Workspace-scoped task submission for the SAG Workbench API."""

from __future__ import annotations

import uuid
from threading import Lock, Thread
from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints

from sag.web.session_registry import ContainerSessionStore


class TaskRequest(BaseModel):
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    source_session: str | None = None


class AgentTaskLauncher:
    _agent_run_lock = Lock()

    def __init__(self, session_store: ContainerSessionStore | None = None):
        self.session_store = session_store if session_store is not None else ContainerSessionStore()

    def run(self, workspace_id: str, task: str, source_session: str | None) -> str:
        session_id = f"UI-{uuid.uuid4().hex[:8]}"
        self.session_store.mark_started(
            workspace_id=workspace_id,
            session_id=session_id,
            task=task,
            source_session=source_session,
        )
        thread = Thread(
            target=self._run_agent,
            args=(session_id, workspace_id, task, source_session),
            daemon=True,
            name=f"sag-ui-task-{session_id}",
        )
        thread.start()
        return session_id

    def _run_agent(
        self,
        session_id: str,
        workspace_id: str,
        task: str,
        source_session: str | None,
    ) -> None:
        from loguru import logger

        success = False
        outcome = f"Task failed: {task}"
        try:
            from sag.agent.agent import SetupAgent
            from sag.config import ensure_session_logging, get_config
            from sag.docker_orch.orch import DockerOrchestrator

            docker_label = workspace_id.removeprefix("sag-")
            orchestrator = DockerOrchestrator(project_name=docker_label)

            if not orchestrator.container_exists():
                raise RuntimeError(f"Workspace container not found: {workspace_id}")

            if not orchestrator.is_container_running() and not orchestrator.start_container():
                raise RuntimeError(f"Workspace container failed to start: {workspace_id}")

            project_name = self._read_project_name(orchestrator, fallback=docker_label)
            task_text = self._task_with_source_session(task, source_session)
            with self._agent_run_lock:
                config = get_config()
                ensure_session_logging(config, force_new=True)
                agent = SetupAgent(config=config, orchestrator=orchestrator)
                success = agent.run_task(project_name=project_name, task_description=task_text)
            outcome = f"Task completed: {task}" if success else f"Task incomplete: {task}"
        except Exception:
            outcome = f"Task failed: {task}"
            logger.exception(
                "Failed to run workspace task {} for session {}",
                workspace_id,
                session_id,
            )
        finally:
            try:
                self.session_store.mark_finished(
                    workspace_id=workspace_id,
                    session_id=session_id,
                    success=success,
                    outcome=outcome,
                )
            except Exception:
                logger.exception(
                    "Failed to update workspace task session {} for {}",
                    session_id,
                    workspace_id,
                )

    def _read_project_name(self, orchestrator: Any, fallback: str) -> str:
        """Mechanically resolve a workspace root; ignore project_meta.json."""
        try:
            from sag.main import detect_project_directory_in_container

            project_name = detect_project_directory_in_container(orchestrator)
        except Exception:
            return fallback
        return project_name or fallback

    def _task_with_source_session(self, task: str, source_session: str | None) -> str:
        if not source_session:
            return task
        return f"{task}\n\nReference prior SAG session: {source_session}"


class TaskRunner:
    def __init__(self, launcher: AgentTaskLauncher | None = None):
        self.launcher = launcher if launcher is not None else AgentTaskLauncher()

    def submit(self, workspace_id: str, request: TaskRequest) -> dict:
        session_id = self.launcher.run(
            workspace_id,
            request.task,
            request.source_session,
        )
        return {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "source_session": request.source_session,
            "status": "queued",
        }


__all__ = ["AgentTaskLauncher", "TaskRequest", "TaskRunner"]
