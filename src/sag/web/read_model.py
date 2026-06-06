"""Compose web read models for dashboard and session detail views."""

from __future__ import annotations

from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.models import DashboardResponse, DockerSummary, ExecutionSessionDetail
from sag.web.session_registry import SessionRegistry
from sag.web.workspace_registry import WorkspaceRegistry


class ReadModelBuilder:
    def __init__(
        self,
        workspace_registry: WorkspaceRegistry | None = None,
        session_registry: SessionRegistry | None = None,
        demo_mode: bool = False,
    ):
        self.workspace_registry = (
            workspace_registry if workspace_registry is not None else WorkspaceRegistry()
        )
        self.session_registry = (
            session_registry if session_registry is not None else SessionRegistry()
        )
        self.demo_mode = demo_mode

    def dashboard(self) -> DashboardResponse:
        if self.demo_mode:
            return build_demo_dashboard()

        try:
            workspaces = self.workspace_registry.list_workspaces()
        except Exception as exc:
            return DashboardResponse(
                docker=DockerSummary(status="unavailable", image=str(exc)),
                workspaces=[],
            )

        return DashboardResponse(
            docker=DockerSummary(status="connected"),
            workspaces=workspaces,
        )

    def session_detail(self, session_id: str) -> ExecutionSessionDetail:
        if self.demo_mode:
            return get_demo_session(session_id)

        raise KeyError(f"Session detail is not available yet for {session_id}")


__all__ = ["ReadModelBuilder"]
