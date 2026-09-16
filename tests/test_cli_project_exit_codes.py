import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import click
from click.testing import CliRunner
from container_evidence_fakes import ContainerFS

import sag.config as config_module
import sag.config.logger as logger_module
import sag.main as main_module
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    VERDICT_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.verdict_finalizer import (
    BuildEvidenceSnapshot,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
)
from sag.tools.module_metrics import MODULE_METRICS_PATH

VERDICT_PATH = "/workspace/.setup_agent/verdict.json"


def reset_config_state(monkeypatch):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)


def snapshot_for(verdict):
    build_modules = {
        "success": {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1},
        "partial": {"rate": 90.0, "band": "most", "numerator": 9, "denominator": 10},
    }[verdict]
    return RunVerdictSnapshot(
        run_id=f"cli-{verdict}",
        finalized_at="2026-07-17T12:00:00Z",
        verdict=verdict,
        build_evidence=BuildEvidenceSnapshot(judgment=verdict, source="physical", observed=True),
        test_stats=SnapshotTestStats(
            # This CLI fixture starts after an independently completed test run;
            # report delivery and command options must not reclassify that run.
            judgment="success",
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
        rates={
            "build": {
                "modules": build_modules,
                "classes": {"band": "unavailable", "reason": "fixture class census unavailable"},
            },
            "test": {
                "cases": {"rate": 100.0, "band": "fully", "numerator": 10, "denominator": 10},
                "modules": {"band": "unavailable", "reason": "fixture test survey unavailable"},
            },
            "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
        },
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
    assert "Setup verdict: partial" in result.output
    assert "Claimed latest subjects" not in result.output
    assert "8 passed" in result.output
    assert "2 failed" in result.output


def test_project_command_returns_nonzero_for_partial_snapshot_in_ui(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, PartialSetupAgent, "--ui")

    assert result.exit_code == 1


def test_project_command_success_ignores_report_delivery_failure(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, ReportFailingSuccessfulAgent)

    assert result.exit_code == 0
    # Success still says the word: the block states it in the Setup row, and
    # only the closing trailer is what a successful run does without.
    assert " Setup         success" in result.output
    assert "Setup verdict" not in result.output
    assert "the setup report was not written" in result.output
    assert "WARNING" not in result.output


def test_project_command_never_promotes_unpublished_success_snapshot(monkeypatch, tmp_path):
    result = invoke_project(monkeypatch, tmp_path, UnpublishedSuccessfulAgent)

    assert result.exit_code == 1
    assert "Setup verdict: unknown" in result.output
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


def test_project_coverage_is_threaded_into_the_pre_finalize_hook(monkeypatch, tmp_path):
    RecordingSetupAgent.calls = []
    coverage_calls = []
    monkeypatch.setattr(
        main_module,
        "_run_coverage_evidence_pass",
        lambda orchestrator, project_name, validator=None: coverage_calls.append(project_name)
        or {"status": "collected", "line_rate": 88.0, "source": "jacoco-injected"},
    )

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent, "--coverage")

    assert result.exit_code == 0
    callback = RecordingSetupAgent.calls[0]["pre_finalize_evidence_callback"]
    assert callable(callback)
    assert coverage_calls == []
    assert callback() == {
        "status": "collected",
        "line_rate": 88.0,
        "source": "jacoco-injected",
    }
    assert coverage_calls == ["commons-cli"]


def test_project_command_initializes_agent_session_logs(monkeypatch, tmp_path):
    RecordingSetupAgent.calls = []

    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent)

    assert result.exit_code == 0
    assert list((tmp_path / "logs").glob("session_*"))


def test_block_states_the_verdict_once_and_exits_one(monkeypatch, tmp_path):
    """A partial run says its verdict in the block's closing line, nowhere else."""

    from result_card_fakes import snapshot_dict

    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict(verdict="partial"))
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    text, code = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert code == 1
    assert text.count("Setup verdict: partial") == 1
    assert "Claimed latest subjects" not in text
    assert "Verdict (derived)" not in text
    assert "Required task" in text


def test_success_exits_zero_and_adds_no_trailer():
    from result_card_fakes import snapshot_dict

    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    text, code = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert code == 0
    assert "Setup verdict" not in text
    assert "setup completed" not in text.lower()


def test_failed_report_delivery_is_an_attention_line_not_a_warning_banner():
    from result_card_fakes import snapshot_dict

    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.FAILED,
    )
    text, _ = _render_setup_cli_result(snapshot, termination, "commons-cli")
    assert "the setup report was not written" in text
    assert "WARNING" not in text


class ModuleMetricsOrchestrator:
    """Answers the one untruncated read the module-metrics reader performs."""

    def __init__(self, body=None):
        self.body = body

    def execute_command(self, command, **kwargs):
        del kwargs
        if self.body is not None and command == f"cat -- {MODULE_METRICS_PATH}":
            return {"exit_code": 0, "success": True, "output": self.body}
        return {"exit_code": 1, "success": False, "output": ""}


def test_module_metrics_are_read_when_present_and_absent_is_not_an_error():
    """The per-module file is a diagnostic: missing or malformed, the block still renders."""

    from result_card_fakes import module_metrics

    from sag.main import _read_module_metrics_for_cli

    payload = module_metrics()

    assert _read_module_metrics_for_cli(ModuleMetricsOrchestrator()) is None
    assert _read_module_metrics_for_cli(ModuleMetricsOrchestrator("{oops")) is None
    assert _read_module_metrics_for_cli(ModuleMetricsOrchestrator("[]")) is None
    assert _read_module_metrics_for_cli(ModuleMetricsOrchestrator(json.dumps(payload))) == payload


TRAJECTORY_FIXTURE = Path(__file__).parent / "fixtures" / "trajectory" / "kafka-d2r3"


def _session_with_ledger(tmp_path):
    session_dir = tmp_path / "session_20260814_072758"
    session_dir.mkdir(parents=True)
    shutil.copy(TRAJECTORY_FIXTURE / "control_events.jsonl", session_dir / "control_events.jsonl")
    return session_dir


def test_run_counts_are_folded_from_the_session_ledger(tmp_path):
    """The Setup row's turns and tool calls come from the run's own ledger."""

    from sag.main import _read_run_counts_for_cli

    counts = _read_run_counts_for_cli(str(_session_with_ledger(tmp_path)))

    assert counts["turn_count"] == 24
    assert counts["tool_calls"] == 24
    assert counts["tool_failures"] == 4
    assert counts["trajectory_session"]["wall_clock_seconds"] == 797.027746


def test_run_counts_state_absence_rather_than_zero_or_raising(tmp_path):
    """A missing ledger, directory or argument leaves every count absent."""

    from sag.main import _read_run_counts_for_cli

    absent = {
        "trajectory_session": None,
        "turn_count": None,
        "tool_calls": None,
        "tool_failures": None,
    }
    assert _read_run_counts_for_cli(None) == absent
    assert _read_run_counts_for_cli(str(tmp_path / "never-written")) == absent
    # A directory with no ledger still names the session; it counts no turns,
    # rather than counting zero of them.
    no_ledger = _read_run_counts_for_cli(str(tmp_path))
    assert no_ledger["turn_count"] is None
    assert no_ledger["tool_calls"] is None
    assert no_ledger["tool_failures"] is None


def test_run_counts_survive_this_module_shadowing_the_list_builtin(tmp_path):
    """`sag list` binds the name `list` in `sag.main`; the reader must not use it."""

    assert isinstance(main_module.list, click.Command)
    # A ledger with turns in it is what turns the shadowing into a crash, so
    # the guard is a real read rather than an inspection of the source.
    assert main_module._read_run_counts_for_cli(str(_session_with_ledger(tmp_path)))["turn_count"]


def test_the_run_pin_is_read_from_the_host_and_degrades_to_absent(tmp_path):
    """The block names the models the run used; a missing pin names none."""

    from sag.main import _read_run_pin_for_cli

    class _Logger:
        def __init__(self, path):
            self.run_pin_path = path

    pinned = _read_run_pin_for_cli(_Logger(TRAJECTORY_FIXTURE / "run-pin.json"))

    assert pinned["action_model"] == "gpt-5.4-mini"
    assert _read_run_pin_for_cli(None) is None
    assert _read_run_pin_for_cli(_Logger(tmp_path / "never-written.json")) is None
    (tmp_path / "not-json.json").write_text("{oops", encoding="utf-8")
    assert _read_run_pin_for_cli(_Logger(tmp_path / "not-json.json")) is None


def test_saving_artifacts_returns_the_report_it_copied(monkeypatch, tmp_path):
    """The block names a file on the host, so the copy has to report its path."""

    import subprocess

    class _Logger:
        session_log_dir = tmp_path

    class _Orchestrator:
        container_name = "sag-commons-cli"

        def execute_command(self, command, **kwargs):
            del kwargs
            if ".setup_agent" in command:
                return {"exit_code": 0, "output": "NOT_FOUND"}
            return {"exit_code": 0, "output": "/workspace/setup-report-20260914-211444.md"}

    monkeypatch.setattr(main_module, "get_session_logger", lambda: _Logger())
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="")
    )

    copied = main_module._save_setup_artifacts(_Orchestrator(), "commons-cli")

    assert copied == str(tmp_path / "setup-report-20260914-211444.md")

    monkeypatch.setattr(main_module, "get_session_logger", lambda: None)
    assert main_module._save_setup_artifacts(_Orchestrator(), "commons-cli") is None


def test_the_report_is_named_by_its_file_when_it_sits_in_the_session_dir():
    """A host path several times wider than the column would break its row."""

    from sag.main import _report_name_for_block

    session_dir = "logs/session_20260914_210609_965730_e39856b237f2_9183"

    assert (
        _report_name_for_block(f"{session_dir}/setup-report-20260914-211444.md", session_dir)
        == "setup-report-20260914-211444.md"
    )
    # A report from anywhere else keeps the only name that locates it.
    assert (
        _report_name_for_block("/elsewhere/setup-report-20260914-211444.md", session_dir)
        == "/elsewhere/setup-report-20260914-211444.md"
    )
    assert _report_name_for_block(None, session_dir) is None
    assert _report_name_for_block("setup-report.md", None) == "setup-report.md"


def test_the_block_states_the_run_counts_and_names_the_report_it_was_given():
    """The four trajectory counts and the report path reach the rendered block."""

    from result_card_fakes import snapshot_dict

    from sag.agent.verdict_finalizer import (
        ReportDeliveryStatus,
        RunTermination,
        RunTerminationStatus,
        RunVerdictSnapshot,
    )
    from sag.main import _render_setup_cli_result

    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    text, _ = _render_setup_cli_result(
        snapshot,
        termination,
        "commons-cli",
        trajectory_session={"wall_clock_seconds": 390.2},
        turn_count=12,
        tool_calls=20,
        tool_failures=3,
        run_pin={"action_model": "gpt-5.4-mini"},
        container="sag-commons-cli",
        session_dir="logs/session_20260914_210609",
        report_path="setup-report-20260914-211444.md",
    )

    assert "12 turns · 20 tool calls · 6m 30s" in text
    assert "setup-report-20260914-211444.md" in text
    assert "written inside the container" not in text
