import json

import pytest

import sag.web.read_model as read_model_module
from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.models import (
    BuildSummary,
    ExecutionSessionDetail,
    ExecutionSessionSummary,
    TestSummary,
)
from sag.web.read_model import ReadModelBuilder
from sag.web.session_registry import _setup_evidence_status, parse_session_index


class FakeWorkspaceRegistry:
    def list_workspaces(self):
        return build_demo_dashboard().workspaces


class FakeSessionRegistry:
    def list_workspace_sessions(self, workspace):
        return []


class FakeLiveSessionRegistry:
    def __init__(self):
        self.summary = ExecutionSessionSummary(
            id="UI-12345678",
            workspace="sag-commons-cli",
            title="Give me a report of all tests",
            status="completed",
            entry="Web UI",
            start="2026-06-06T21:13:38",
            finish="2026-06-06T21:14:09",
            duration="31s",
            build="none",
            test=TestSummary(state="none"),
            report="none",
            files=0,
            evidence=1,
        )
        self.detail = ExecutionSessionDetail(
            id=self.summary.id,
            workspace=self.summary.workspace,
            title=self.summary.title,
            status=self.summary.status,
            entry=self.summary.entry,
            start=self.summary.start,
            duration=self.summary.duration,
            outcome="Task completed: give me a report of all the test in the workspace",
            build=BuildSummary(state="none"),
            test=self.summary.test,
            report="none",
            evidence=[],
            files=None,
            context=None,
            logs=[],
        )

    def list_workspace_sessions(self, workspace):
        if workspace.id == "sag-commons-cli":
            return [self.summary]
        return []

    def get_session_detail(self, session_id):
        if session_id != self.summary.id:
            raise KeyError(session_id)
        return self.detail


class FakeMixedSessionRegistry:
    def __init__(self):
        self.setup = ExecutionSessionSummary(
            id="SETUP-20260606-213241",
            workspace="sag-commons-cli",
            title="Setup and configure the commons-cli project to be runnable",
            status="completed",
            entry="CLI",
            start="2026-06-06T21:32:41",
            finish="2026-06-06T21:35:09",
            duration="2m 28s",
            build="success",
            test=TestSummary(state="success", pass_count=420, total=430),
            report="ready",
            files=6,
            evidence=7,
        )
        self.ui = ExecutionSessionSummary(
            id="UI-12345678",
            workspace="sag-commons-cli",
            title="Run formatter tests",
            status="running",
            entry="Web UI",
            start="2026-06-06T21:48:30",
            finish=None,
            duration="running",
            build="none",
            test=TestSummary(state="none"),
            report="none",
            files=0,
            evidence=1,
        )

    def list_workspace_sessions(self, workspace):
        if workspace.id == "sag-commons-cli":
            return [self.setup, self.ui]
        return []


class RaisingWorkspaceRegistry:
    def list_workspaces(self):
        raise RuntimeError("docker socket unavailable")


def test_demo_read_model_does_not_construct_registries_without_injection(monkeypatch):
    constructed = {"workspace": 0, "session": 0}

    class CountingWorkspaceRegistry:
        def __init__(self):
            constructed["workspace"] += 1

    class CountingSessionRegistry:
        def __init__(self):
            constructed["session"] += 1

    monkeypatch.setattr(read_model_module, "WorkspaceRegistry", CountingWorkspaceRegistry)
    monkeypatch.setattr(read_model_module, "ContainerSessionRegistry", CountingSessionRegistry)

    builder = ReadModelBuilder(demo_mode=True)
    dashboard = builder.dashboard()
    detail = builder.session_detail("CC-3")

    assert dashboard.workspaces[0].id == "sag-commons-cli"
    assert detail.id == get_demo_session("CC-3").id
    assert constructed == {"workspace": 0, "session": 0}


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


def test_read_model_builder_enriches_workspaces_from_live_sessions():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeLiveSessionRegistry(),
        demo_mode=False,
    )

    dashboard = builder.dashboard()
    workspace = dashboard.workspaces[0]

    assert workspace.latest_session == "UI-12345678"
    assert workspace.active_session is None
    assert workspace.task == "Give me a report of all tests"
    assert workspace.updated == "2026-06-06T21:14:09"


def test_parse_session_index_preserves_completed_flow_partial_evidence_and_test_rates():
    rows = parse_session_index(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "SETUP-20260606-213241",
                        "workspace": "sag-commons-cli",
                        "title": "Set up project",
                        "status": "completed",
                        "evidenceStatus": "partial",
                        "entry": "CLI",
                        "start": "2026-06-06T21:32:41",
                        "finish": "2026-06-06T21:35:09",
                        "duration": "2m 28s",
                        "build": "success",
                        "test": {
                            "state": "partial",
                            "pass": 312,
                            "fail": 8,
                            "skip": 0,
                            "total": 320,
                            "passRate": 97.5,
                            "executionRate": 100.0,
                        },
                        "report": "ready",
                        "files": 6,
                        "evidence": 7,
                    },
                    {
                        "id": "SETUP-unknown-evidence",
                        "title": "Completed without evidence metadata",
                        "status": "completed",
                        "entry": "CLI",
                        "start": "2026-06-06T21:32:41",
                        "duration": "2m 28s",
                        "build": "success",
                        "test": {"state": "none"},
                        "report": "ready",
                        "files": 0,
                        "evidence": 0,
                    },
                ]
            }
        ),
        "sag-commons-cli",
    )

    assert rows[0].status == "completed"
    assert rows[0].evidence_status == "partial"
    assert rows[0].test.pass_rate == 97.5
    assert rows[0].test.execution_rate == 100.0
    assert rows[1].status == "completed"
    assert rows[1].evidence_status == "unknown"


def test_setup_artifact_evidence_status_prefers_report_result_and_task_evidence():
    tasks = [
        {
            "id": "T1",
            "status": "completed",
            "evidence_status": "success",
        },
        {
            "id": "T2",
            "status": "completed",
            "evidence_status": "conflict",
        },
    ]

    assert _setup_evidence_status({}, tasks, "**Result:** ⚠️ PARTIAL\n") == "partial"
    assert _setup_evidence_status({}, tasks, None) == "conflict"
    assert _setup_evidence_status({}, [{"status": "completed"}], None) == "unknown"


def test_read_model_builder_marks_running_live_session_active():
    registry = FakeLiveSessionRegistry()
    registry.summary.status = "running"
    registry.summary.finish = None
    registry.summary.duration = "running"
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=registry,
        demo_mode=False,
    )

    workspace = builder.dashboard().workspaces[0]

    assert workspace.active_session == "UI-12345678"
    assert workspace.latest_session == "UI-12345678"


def test_read_model_builder_includes_all_workspace_sessions():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeMixedSessionRegistry(),
        demo_mode=False,
    )

    workspace = builder.dashboard().workspaces[0]

    assert [session.id for session in workspace.sessions] == [
        "SETUP-20260606-213241",
        "UI-12345678",
    ]
    assert workspace.active_session == "UI-12345678"
    assert workspace.latest_session == "UI-12345678"


def test_read_model_builder_marks_docker_unavailable_when_registry_raises():
    builder = ReadModelBuilder(
        workspace_registry=RaisingWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=False,
    )

    dashboard = builder.dashboard()

    assert dashboard.docker.status == "unavailable"
    assert dashboard.docker.image is None
    assert dashboard.workspaces == []


def test_non_demo_session_detail_uses_session_registry_without_workspace_registry(monkeypatch):
    constructed = {"workspace": 0, "session": 0}

    class CountingWorkspaceRegistry:
        def __init__(self):
            constructed["workspace"] += 1

    class CountingSessionRegistry:
        def __init__(self):
            constructed["session"] += 1

    monkeypatch.setattr(read_model_module, "WorkspaceRegistry", CountingWorkspaceRegistry)
    monkeypatch.setattr(read_model_module, "ContainerSessionRegistry", CountingSessionRegistry)

    builder = ReadModelBuilder(demo_mode=False)

    with pytest.raises(KeyError, match="Session detail is not available yet for x"):
        builder.session_detail("x")

    assert constructed == {"workspace": 0, "session": 1}


def test_read_model_builder_session_detail_is_not_available_when_not_demo():
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=FakeSessionRegistry(),
        demo_mode=False,
    )

    with pytest.raises(KeyError, match="Session detail is not available yet for x"):
        builder.session_detail("x")


def test_read_model_builder_returns_live_session_detail_when_available():
    registry = FakeLiveSessionRegistry()
    builder = ReadModelBuilder(
        workspace_registry=FakeWorkspaceRegistry(),
        session_registry=registry,
        demo_mode=False,
    )

    detail = builder.session_detail("UI-12345678")

    assert detail.id == "UI-12345678"
    assert detail.outcome.startswith("Task completed")
