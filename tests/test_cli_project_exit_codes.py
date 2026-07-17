from click.testing import CliRunner

import sag.config as config_module
import sag.config.logger as logger_module
import sag.main as main_module
from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestStats,
)

VERDICT_PATH = "/workspace/.setup_agent/verdict.json"


def reset_config_state(monkeypatch):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)


def snapshot_for(verdict):
    return RunVerdictSnapshot(
        run_id=f"cli-{verdict}",
        finalized_at="2026-07-17T12:00:00Z",
        verdict=verdict,
        test_stats=SnapshotTestStats(
            discovered=10,
            executed=10,
            passed=10 if verdict == "success" else 8,
            failed=0 if verdict == "success" else 2,
        ),
    )


class FakeProjectOrchestrator:
    def __init__(self, project_name=None):
        self.project_name = project_name
        self.files = {}

    def container_exists(self):
        return False

    def execute_command(self, command, **kwargs):
        if command == f"test -f {VERDICT_PATH} && cat {VERDICT_PATH}":
            if VERDICT_PATH in self.files:
                return {"exit_code": 0, "success": True, "output": self.files[VERDICT_PATH]}
            return {"exit_code": 1, "success": False, "output": ""}
        return {"exit_code": 0, "success": True, "output": ""}


class SnapshotSetupAgent:
    calls = []
    verdict = "success"
    delivery = ReportDeliveryStatus.DELIVERED

    def __init__(self, config, orchestrator):
        self.config = config
        self.orchestrator = orchestrator

    def setup_project(self, **kwargs):
        self.calls.append(kwargs)
        self.orchestrator.files[VERDICT_PATH] = snapshot_for(self.verdict).model_dump_json()
        return RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=self.delivery,
        )


class PartialSetupAgent(SnapshotSetupAgent):
    verdict = "partial"


class RecordingSetupAgent(SnapshotSetupAgent):
    verdict = "success"


class ReportFailingSuccessfulAgent(SnapshotSetupAgent):
    verdict = "success"
    delivery = ReportDeliveryStatus.FAILED


def invoke_project(monkeypatch, tmp_path, agent_type, *extra_args):
    reset_config_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", agent_type)
    return CliRunner().invoke(
        main_module.cli,
        ["project", "https://github.com/apache/commons-cli.git", *extra_args],
    )


def test_project_command_returns_nonzero_for_partial_snapshot(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, PartialSetupAgent)

    assert result.exit_code == 1
    assert "Verdict: PARTIAL" in result.output
    assert "Tests: 10 unique" in result.output


def test_project_command_returns_nonzero_for_partial_snapshot_in_ui(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, PartialSetupAgent, "--ui")

    assert result.exit_code == 1


def test_project_command_success_ignores_report_delivery_failure(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, ReportFailingSuccessfulAgent)

    assert result.exit_code == 0
    assert "Verdict: SUCCESS" in result.output
    assert "WARNING" in result.output
    assert "report delivery failed" in result.output.lower()


def test_project_command_passes_ref_to_setup_agent(monkeypatch, tmp_path):
    RecordingSetupAgent.calls = []

    result = invoke_project(
        monkeypatch,
        tmp_path,
        RecordingSetupAgent,
        "--ref",
        "rel/commons-cli-1.11.0",
    )

    assert result.exit_code == 0
    assert RecordingSetupAgent.calls[0]["project_ref"] == "rel/commons-cli-1.11.0"


def test_project_command_initializes_agent_session_logs(monkeypatch, tmp_path):
    RecordingSetupAgent.calls = []

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent)

    assert result.exit_code == 0
    assert list((tmp_path / "logs").glob("session_*"))
