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
