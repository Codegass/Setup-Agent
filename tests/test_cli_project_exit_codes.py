from click.testing import CliRunner

import sag.main as main_module


class FakeProjectOrchestrator:
    def __init__(self, project_name=None):
        self.project_name = project_name

    def container_exists(self):
        return False


class FailingSetupAgent:
    def __init__(self, config, orchestrator):
        self.config = config
        self.orchestrator = orchestrator

    def setup_project(self, **kwargs):
        return False


def test_project_command_returns_nonzero_when_setup_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", FailingSetupAgent)

    result = CliRunner().invoke(
        main_module.cli,
        ["project", "https://github.com/apache/commons-cli.git"],
    )

    assert result.exit_code == 1
    assert "Project setup failed" in result.output


def test_project_command_returns_nonzero_when_ui_setup_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", FailingSetupAgent)

    result = CliRunner().invoke(
        main_module.cli,
        ["project", "https://github.com/apache/commons-cli.git", "--ui"],
    )

    assert result.exit_code == 1
