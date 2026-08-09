from click.testing import CliRunner

from container_evidence_fakes import ContainerFS
import sag.config as config_module
import sag.config.logger as logger_module
import sag.main as main_module
from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
)
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    VERDICT_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
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
            raw=SnapshotTestCounts(
                executed=10,
                passed=10 if verdict == "success" else 8,
                failed=0 if verdict == "success" else 2,
            ),
        ),
    )


class FakeProjectOrchestrator:
    def __init__(self, project_name=None):
        self.project_name = project_name
        self.filesystem = ContainerFS()
        self.files = self.filesystem.files

    def container_exists(self):
        return False

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)


class _MemorySink:
    path = "/host/cli-verdict-control-events.jsonl"

    def emit(self, kind, payload, *, source=None):
        del kind, payload, source


class SnapshotSetupAgent:
    calls = []
    verdict = "success"
    delivery = ReportDeliveryStatus.DELIVERED

    def __init__(self, config, orchestrator):
        self.config = config
        self.orchestrator = orchestrator

    def setup_project(self, **kwargs):
        self.calls.append(kwargs)
        snapshot = snapshot_for(self.verdict)
        raw = snapshot.model_dump_json()
        self.orchestrator.files[VERDICT_PATH] = raw
        authority = EvidencePublicationAuthority(run_id=snapshot.run_id, sink=_MemorySink())
        token = install_evidence_publication_authority(
            authority,
            orchestrator=self.orchestrator,
        )
        reset_evidence_publication_authority(token)
        authority.publish_revision(
            record_kind="verdict",
            record_id=VERDICT_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
            raw=raw.encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
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


class UnpublishedSuccessfulAgent(SnapshotSetupAgent):
    verdict = "success"

    def setup_project(self, **kwargs):
        self.calls.append(kwargs)
        self.orchestrator.files[VERDICT_PATH] = snapshot_for(self.verdict).model_dump_json()
        return RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=ReportDeliveryStatus.DELIVERED,
        )


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
    assert "Claimed latest subjects: unavailable" in result.output
    assert "Unattributed observations (not verdict-bearing): 8/10 passed" in result.output


def test_project_command_returns_nonzero_for_partial_snapshot_in_ui(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, PartialSetupAgent, "--ui")

    assert result.exit_code == 1


def test_project_command_success_ignores_report_delivery_failure(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, ReportFailingSuccessfulAgent)

    assert result.exit_code == 0
    assert "Verdict: SUCCESS" in result.output
    assert "WARNING" in result.output
    assert "report delivery failed" in result.output.lower()


def test_project_command_never_promotes_unpublished_success_snapshot(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, UnpublishedSuccessfulAgent)

    assert result.exit_code == 1
    assert "Verdict: UNKNOWN" in result.output
    assert "setup completed" not in result.output.lower()


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
