from fastapi.testclient import TestClient

from sag.web.app import create_app
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
        first = next(response.iter_lines())

    assert response.status_code == 200
    assert first.startswith("event: snapshot")


def test_unknown_session_returns_404_in_demo_mode():
    app = create_app(ReadModelBuilder(demo_mode=True))
    client = TestClient(app)

    response = client.get("/api/sessions/not-a-session")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found: not-a-session"
