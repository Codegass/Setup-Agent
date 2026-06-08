import json

from fastapi.testclient import TestClient

import sag.web.app as app_module
from sag.web.app import create_app
from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.read_model import ReadModelBuilder


class FakeTaskRunner:
    def __init__(self):
        self.calls = []

    def submit(self, workspace_id, request):
        self.calls.append((workspace_id, request))
        return {
            "workspace_id": workspace_id,
            "session_id": "RUN-1",
            "source_session": request.source_session,
            "status": "queued",
        }


def test_dashboard_endpoint_returns_workspaces():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "sag-commons-cli"


def test_static_root_serves_web_ui_index(tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><div id=\"root\">SAG Workbench</div>",
        encoding="utf-8",
    )
    app = create_app(ReadModelBuilder(demo_mode=True), static_dir=tmp_path)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "SAG Workbench" in response.text


def test_static_assets_do_not_shadow_api_routes(tmp_path):
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('sag');", encoding="utf-8")
    app = create_app(ReadModelBuilder(demo_mode=True), static_dir=tmp_path)
    client = TestClient(app)

    asset_response = client.get("/assets/app.js")
    api_response = client.get("/api/workspaces")

    assert asset_response.status_code == 200
    assert "console.log('sag')" in asset_response.text
    assert api_response.status_code == 200
    assert api_response.json()["workspaces"][0]["id"] == "sag-commons-cli"


def test_submit_task_is_workspace_scoped():
    task_runner = FakeTaskRunner()
    app = create_app(ReadModelBuilder(demo_mode=True), task_runner=task_runner)
    client = TestClient(app)

    response = client.post(
        "/api/workspaces/sag-commons-cli/tasks",
        json={"task": "Run formatter tests", "source_session": "CC-3"},
    )

    assert response.status_code == 202
    assert response.json()["workspace_id"] == "sag-commons-cli"
    assert response.json()["source_session"] == "CC-3"
    assert task_runner.calls[0][0] == "sag-commons-cli"
    assert task_runner.calls[0][1].task == "Run formatter tests"


def test_submit_task_rejects_blank_task():
    app = create_app(ReadModelBuilder(demo_mode=True), task_runner=FakeTaskRunner())
    client = TestClient(app)

    response = client.post(
        "/api/workspaces/sag-commons-cli/tasks",
        json={"task": ""},
    )

    assert response.status_code == 422


def test_submit_task_rejects_whitespace_only_task():
    app = create_app(ReadModelBuilder(demo_mode=True), task_runner=FakeTaskRunner())
    client = TestClient(app)

    response = client.post(
        "/api/workspaces/sag-commons-cli/tasks",
        json={"task": "   "},
    )

    assert response.status_code == 422


def test_session_endpoint_returns_session_detail():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/sessions/CC-3")

    assert response.status_code == 200
    assert response.json()["id"] == "CC-3"
    assert response.json()["reportDoc"]["title"].startswith("setup-report")


def test_dashboard_stream_emits_sse_snapshot():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    with client.stream("GET", "/api/stream/dashboard") as response:
        lines = []
        for line in response.iter_lines():
            lines.append(line)
            if len(lines) >= 2:
                break

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert lines[0] == "event: snapshot"

    data_line = next(line for line in lines if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["workspaces"][0]["latestSession"] == "CC-3"


def test_unknown_session_returns_404_in_demo_mode():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/sessions/not-a-session")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found: not-a-session"


def test_create_app_uses_falsy_injected_read_model(monkeypatch):
    class FalsyReadModel:
        def __bool__(self):
            return False

        def dashboard(self):
            return build_demo_dashboard()

        def session_detail(self, session_id):
            return get_demo_session(session_id)

    def unexpected_default_builder():
        raise AssertionError("create_app should use the injected read model")

    monkeypatch.setattr(app_module, "ReadModelBuilder", unexpected_default_builder)

    app = create_app(FalsyReadModel())
    client = TestClient(app)

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "sag-commons-cli"


class FakeLaunchService:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.requests = []
        self.started = False
        self.stopped = False

    def submit_batch(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.outcome

    def queue_state(self):
        return {
            "default_concurrency": 3,
            "summary": {
                "queued": 1,
                "launching": 0,
                "running": 1,
                "completed": 2,
                "failed": 0,
            },
            "batches": [],
        }

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def launch_client(service):
    app = create_app(ReadModelBuilder(demo_mode=True), launch_service=service)
    return TestClient(app)


def test_batch_submit_returns_202_with_accepted_and_rejected_rows():
    service = FakeLaunchService(
        outcome={
            "batch_id": "BATCH-20260607-abcdef",
            "concurrency": 2,
            "accepted": [
                {
                    "launch_id": "LAUNCH-12345678",
                    "row_index": 0,
                    "workspace_id": "sag-commons-cli",
                    "status": "queued",
                }
            ],
            "rejected": [
                {
                    "row_index": 1,
                    "workspace_id": "sag-existing",
                    "status": "conflict",
                    "message": "Workspace already exists: sag-existing",
                }
            ],
        }
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={
            "concurrency": 2,
            "projects": [
                {"repo_url": "https://github.com/apache/commons-cli.git"},
                {"repo_url": "https://github.com/x/existing.git"},
            ],
        },
    )

    assert response.status_code == 202
    assert response.json()["batch_id"] == "BATCH-20260607-abcdef"
    assert len(service.requests) == 1
    assert service.requests[0].projects[0].repo_url == (
        "https://github.com/apache/commons-cli.git"
    )


def test_batch_submit_returns_409_when_every_row_conflicts():
    service = FakeLaunchService(
        outcome={
            "batch_id": None,
            "concurrency": 2,
            "accepted": [],
            "rejected": [
                {
                    "row_index": 0,
                    "workspace_id": "sag-existing",
                    "status": "conflict",
                    "message": "Workspace already exists: sag-existing",
                }
            ],
        }
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={"projects": [{"repo_url": "https://github.com/x/existing.git"}]},
    )

    assert response.status_code == 409
    assert response.json()["rejected"][0]["status"] == "conflict"


def test_batch_submit_returns_422_for_invalid_shape():
    client = launch_client(FakeLaunchService())

    no_projects = client.post("/api/project-launches/batch", json={"projects": []})
    blank_repo = client.post(
        "/api/project-launches/batch", json={"projects": [{"repo_url": "   "}]}
    )

    assert no_projects.status_code == 422
    assert blank_repo.status_code == 422


def test_batch_submit_returns_422_for_out_of_range_concurrency():
    from sag.web.launch_service import LaunchValidationError

    service = FakeLaunchService(
        error=LaunchValidationError("concurrency must be an integer between 1 and 8")
    )
    client = launch_client(service)

    response = client.post(
        "/api/project-launches/batch",
        json={
            "concurrency": 99,
            "projects": [{"repo_url": "https://github.com/apache/commons-cli.git"}],
        },
    )

    assert response.status_code == 422
    assert "concurrency" in response.json()["detail"]


def test_get_project_launches_returns_queue_state():
    client = launch_client(FakeLaunchService())

    response = client.get("/api/project-launches")

    assert response.status_code == 200
    body = response.json()
    assert body["default_concurrency"] == 3
    assert body["summary"]["completed"] == 2
    assert body["batches"] == []


def test_lifespan_starts_and_stops_launch_service():
    service = FakeLaunchService()

    with launch_client(service):
        assert service.started
        assert not service.stopped

    assert service.stopped


class FakeWorkspaceService:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def delete_workspace(self, workspace_id):
        self.calls.append(workspace_id)
        if self.error is not None:
            raise self.error
        return self.result


def test_delete_workspace_returns_200_with_result_body():
    service = FakeWorkspaceService(
        result={
            "workspace_id": "sag-commons-cli",
            "container_removed": True,
            "queue_items_removed": 2,
            "status": "deleted",
        }
    )
    app = create_app(ReadModelBuilder(demo_mode=True), workspace_service=service)
    client = TestClient(app)

    response = client.delete("/api/workspaces/sag-commons-cli")

    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": "sag-commons-cli",
        "container_removed": True,
        "queue_items_removed": 2,
        "status": "deleted",
    }
    assert service.calls == ["sag-commons-cli"]


def test_delete_workspace_returns_409_when_busy():
    from sag.web.launch_queue import WorkspaceBusyError

    service = FakeWorkspaceService(
        error=WorkspaceBusyError("Workspace has an active launch: sag-x")
    )
    app = create_app(ReadModelBuilder(demo_mode=True), workspace_service=service)
    client = TestClient(app)

    response = client.delete("/api/workspaces/sag-x")

    assert response.status_code == 409
    assert "active launch" in response.json()["detail"]
    assert service.calls == ["sag-x"]


def test_delete_workspace_returns_502_on_deletion_error():
    from sag.web.workspace_service import WorkspaceDeletionError

    service = FakeWorkspaceService(
        error=WorkspaceDeletionError(
            "Launch history for sag-x was cleared, but its Docker container "
            "could not be removed."
        )
    )
    app = create_app(ReadModelBuilder(demo_mode=True), workspace_service=service)
    client = TestClient(app)

    response = client.delete("/api/workspaces/sag-x")

    assert response.status_code == 502
    assert "could not be removed" in response.json()["detail"]
    assert service.calls == ["sag-x"]
