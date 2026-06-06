import pytest

from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.read_model import ReadModelBuilder


class FakeWorkspaceRegistry:
    def list_workspaces(self):
        return build_demo_dashboard().workspaces


class FakeSessionRegistry:
    def read_index(self, workspace_root, workspace_id):
        return []


class RaisingWorkspaceRegistry:
    def list_workspaces(self):
        raise RuntimeError("docker socket unavailable")


def test_read_model_builder_uses_demo_fallback_when_requested():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=True,
    )

    dashboard = builder.dashboard()
    detail = builder.session_detail("CC-3")

    assert dashboard.workspaces[0].id == "sag-commons-cli"
    assert detail.id == get_demo_session("CC-3").id


def test_read_model_builder_uses_workspace_registry_when_not_demo():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=False,
    )

    dashboard = builder.dashboard()

    assert dashboard.workspaces == build_demo_dashboard().workspaces
    assert dashboard.docker.status == "connected"


def test_read_model_builder_marks_docker_unavailable_when_registry_raises():
    builder = ReadModelBuilder(
        workspace_registry=RaisingWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=False,
    )

    dashboard = builder.dashboard()

    assert dashboard.docker.status == "unavailable"
    assert dashboard.docker.image == "docker socket unavailable"
    assert dashboard.workspaces == []


def test_read_model_builder_session_detail_is_not_available_when_not_demo():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=False,
    )

    with pytest.raises(KeyError, match="Session detail is not available yet for x"):
        builder.session_detail("x")
