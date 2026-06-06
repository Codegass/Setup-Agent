import json

from fastapi.testclient import TestClient

import sag.web.app as app_module
from sag.web.app import create_app
from sag.web.demo_data import build_demo_dashboard, get_demo_session
from sag.web.read_model import ReadModelBuilder


def test_dashboard_endpoint_returns_workspaces():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "sag-commons-cli"


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
