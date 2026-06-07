from click.testing import CliRunner

from sag.main import cli
from sag.web.server import STATIC_DIR, run_web_server


def test_ui_command_accepts_host_port_and_demo_flag(monkeypatch, tmp_path):
    calls = {}

    def fake_run_server(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("sag.main.run_web_server", fake_run_server)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli, ["ui", "--host", "127.0.0.1", "--port", "8765", "--demo"]
    )

    assert result.exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 8765, "demo": True}


def test_ui_command_uses_localhost_ephemeral_port_and_live_data_by_default(monkeypatch, tmp_path):
    calls = {}

    def fake_run_server(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("sag.main.run_web_server", fake_run_server)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ui"])

    assert result.exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 0, "demo": False}


def test_run_web_server_mounts_bundled_static_dir(monkeypatch):
    calls = {}
    sentinel_app = object()

    def fake_create_app(read_model, *, static_dir=None):
        calls["static_dir"] = static_dir
        return sentinel_app

    def fake_uvicorn_run(app, **kwargs):
        calls["app"] = app
        calls["uvicorn"] = kwargs

    monkeypatch.setattr("sag.web.server.create_app", fake_create_app)
    monkeypatch.setattr("sag.web.server.uvicorn.run", fake_uvicorn_run)

    run_web_server(host="127.0.0.1", port=8765, demo=True)

    assert calls["static_dir"] == STATIC_DIR
    assert calls["app"] is sentinel_app
    assert calls["uvicorn"] == {
        "host": "127.0.0.1",
        "port": 8765,
        "log_level": "info",
    }
