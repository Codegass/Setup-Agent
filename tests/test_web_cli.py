from click.testing import CliRunner

from sag.main import cli


def test_ui_command_accepts_host_port_and_demo_flag(monkeypatch):
    calls = {}

    def fake_run_server(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("sag.main.run_web_server", fake_run_server)
    result = CliRunner().invoke(
        cli, ["ui", "--host", "127.0.0.1", "--port", "8765", "--demo"]
    )

    assert result.exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 8765, "demo": True}


def test_ui_command_uses_localhost_ephemeral_port_and_live_data_by_default(monkeypatch):
    calls = {}

    def fake_run_server(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("sag.main.run_web_server", fake_run_server)
    result = CliRunner().invoke(cli, ["ui"])

    assert result.exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 0, "demo": False}
