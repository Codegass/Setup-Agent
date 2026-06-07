import queue
import time

from fastapi.testclient import TestClient

import sag.web.app as app_module
from sag.web.app import create_app
from sag.web.demo_data import build_demo_dashboard


class FalsyReadModel:
    def __bool__(self):
        return False

    def dashboard(self):
        return build_demo_dashboard()

    def session_detail(self, session_id):
        raise KeyError(session_id)


def test_build_exec_options_request_interactive_tty():
    from sag.web.terminal import build_exec_options

    assert build_exec_options() == {"cmd": "/bin/bash", "stdin": True, "tty": True}
    assert build_exec_options("/bin/sh") == {"cmd": "/bin/sh", "stdin": True, "tty": True}


def test_terminal_adapter_uses_injected_docker_client_for_exec_socket():
    from sag.web.terminal import TerminalAdapter

    socket = object()

    class FakeDockerApi:
        def __init__(self):
            self.created = []
            self.started = []

        def exec_create(self, container, **options):
            self.created.append((container, options))
            return {"Id": "exec-123"}

        def exec_start(self, exec_id, **options):
            self.started.append((exec_id, options))
            return socket

    class FakeDockerClient:
        def __init__(self):
            self.api = FakeDockerApi()

    docker_client = FakeDockerClient()

    adapter = TerminalAdapter(docker_client=docker_client)

    assert adapter.open_socket("sag-demo", shell="/bin/sh") is socket
    assert docker_client.api.created == [
        ("sag-demo", {"cmd": "/bin/sh", "stdin": True, "tty": True})
    ]
    assert docker_client.api.started == [("exec-123", {"tty": True, "socket": True})]


def test_rest_api_does_not_instantiate_terminal_adapter_until_terminal_route(monkeypatch):
    class ExplodingTerminalAdapter:
        def __init__(self):
            raise AssertionError("terminal adapter should be lazy")

    monkeypatch.setattr(app_module, "TerminalAdapter", ExplodingTerminalAdapter)

    app = create_app(FalsyReadModel())
    client = TestClient(app)

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json()["workspaces"][0]["id"] == "sag-commons-cli"


def test_terminal_websocket_opens_workspace_and_forwards_bytes():
    class FakeTerminalSocket:
        def __init__(self):
            self.outputs = queue.Queue()
            self.outputs.put(b"ready\r\n")
            self.sent = queue.Queue()
            self.closed = False

        def recv(self, _size):
            try:
                return self.outputs.get(timeout=1)
            except queue.Empty:
                return b""

        def send(self, data):
            self.sent.put(data)
            return len(data)

        def close(self):
            self.closed = True

    class FakeTerminalAdapter:
        def __init__(self):
            self.socket = FakeTerminalSocket()
            self.opened = []

        def open_socket(self, container, shell="/bin/bash"):
            self.opened.append((container, shell))
            return self.socket

    adapter = FakeTerminalAdapter()
    app = create_app(FalsyReadModel(), terminal_adapter=adapter)
    client = TestClient(app)

    with client.websocket_connect("/api/workspaces/sag-commons-cli/terminal") as websocket:
        assert websocket.receive_bytes() == b"ready\r\n"

        websocket.send_bytes(b"pwd\n")

        deadline = time.monotonic() + 1
        while adapter.socket.sent.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    assert adapter.opened == [("sag-commons-cli", "/bin/bash")]
    assert adapter.socket.sent.get_nowait() == b"pwd\n"
    assert adapter.socket.closed is True
