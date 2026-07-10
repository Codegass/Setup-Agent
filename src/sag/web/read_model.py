"""Compose web read models for dashboard and session detail views."""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.models import (
    DashboardResponse,
    DockerSummary,
    ExecutionSessionDetail,
    SystemSummary,
    WorkspaceSummary,
)
from sag.web.session_registry import ContainerSessionRegistry
from sag.web.workspace_registry import WorkspaceRegistry


class ReadModelBuilder:
    def __init__(
        self,
        workspace_registry: WorkspaceRegistry | None = None,
        session_registry: object | None = None,
        demo_mode: bool = False,
    ):
        self.workspace_registry: WorkspaceRegistry | None = workspace_registry
        self.session_registry: object | None = session_registry
        self.demo_mode = demo_mode

    def dashboard(self) -> DashboardResponse:
        if self.demo_mode:
            return build_demo_dashboard()

        try:
            if self.workspace_registry is None:
                self.workspace_registry = WorkspaceRegistry()

            workspaces = [
                self._with_session_state(workspace)
                for workspace in self.workspace_registry.list_workspaces()
            ]
        except Exception:
            logger.exception("Failed to build SAG Workbench dashboard")
            return DashboardResponse(
                docker=DockerSummary(status="unavailable"),
                workspaces=[],
            )

        return DashboardResponse(
            docker=DockerSummary(status="connected"),
            workspaces=workspaces,
        )

    def system(self) -> SystemSummary:
        """Host + docker resource usage for the nav bar. Each source is best-effort:
        a failure leaves that field None rather than erroring the whole endpoint."""
        summary = SystemSummary()
        try:
            if self.workspace_registry is None:
                self.workspace_registry = WorkspaceRegistry()
            df = self.workspace_registry.client.df()
            used = int(df.get("LayersSize", 0) or 0)  # total image layer bytes
            for vol in df.get("Volumes") or []:
                used += max(0, int((vol.get("UsageData") or {}).get("Size", 0) or 0))
            for container in df.get("Containers") or []:
                used += max(0, int(container.get("SizeRw", 0) or 0))
            summary.docker_disk_used = used
            summary.docker_reclaimable = (
                sum(int(b.get("Size", 0) or 0) for b in (df.get("BuildCache") or [])) or None
            )
        except Exception:
            logger.debug("docker df unavailable")

        try:
            meminfo = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                field, _, rest = line.partition(":")
                meminfo[field] = int(rest.strip().split()[0]) * 1024  # kB -> bytes
            total = meminfo.get("MemTotal")
            available = meminfo.get("MemAvailable")
            if total is not None:
                summary.mem_total = total
                if available is not None:
                    summary.mem_used = total - available
        except Exception:
            logger.debug("/proc/meminfo unavailable")

        try:
            summary.cpu_load = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        return summary

    def session_detail(self, session_id: str) -> ExecutionSessionDetail:
        if self.demo_mode:
            return get_demo_session(session_id)

        registry = self._session_registry()
        get_session_detail = getattr(registry, "get_session_detail", None)
        if get_session_detail is not None:
            try:
                return get_session_detail(session_id)
            except KeyError:
                pass

        raise KeyError(f"Session detail is not available yet for {session_id}")

    def _session_registry(self) -> object:
        if self.session_registry is None:
            self.session_registry = ContainerSessionRegistry()
        return self.session_registry

    def _with_session_state(self, workspace: WorkspaceSummary) -> WorkspaceSummary:
        registry = self._session_registry()
        list_workspace_sessions = getattr(registry, "list_workspace_sessions", None)
        if list_workspace_sessions is None:
            return workspace

        try:
            sessions = list_workspace_sessions(workspace)
        except Exception:
            logger.debug("Failed to enrich workspace sessions for {}", workspace.id)
            return workspace

        if not sessions:
            return workspace

        latest = sessions[-1]
        active = next(
            (
                session
                for session in reversed(sessions)
                if session.status.strip().lower() in {"active", "running", "in_progress", "queued"}
            ),
            None,
        )

        return workspace.model_copy(
            update={
                "task": (active or latest).title,
                "evidence_status": latest.evidence_status,
                "build": latest.build,
                "test": latest.test,
                "report": latest.report,
                "changed": latest.files,
                "active_session": active.id if active is not None else None,
                "latest_session": latest.id,
                "sessions": sessions,
                "updated": latest.finish or latest.start,
            }
        )


__all__ = ["ReadModelBuilder"]
