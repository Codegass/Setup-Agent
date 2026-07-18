import json
import re
from dataclasses import dataclass

import pytest
from rich.console import Console

import sag.main as main_module
from sag.agent.verdict_finalizer import (
    BuildEvidenceSnapshot,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
    read_verdict_snapshot,
)
from sag.evidence import EvidenceStatus, OperationOutcome
from sag.tools.report_tool import ReportTool
from sag.ui.ui_manager import UIManager
from sag.web.session_registry import _session_detail, _setup_artifact_item

VERDICT_PATH = "/workspace/.setup_agent/verdict.json"


class SnapshotOrchestrator:
    def __init__(self, files=None, *, fail_report_writes=False):
        self.files = dict(files or {})
        self.fail_report_writes = fail_report_writes
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)

        if command == f"test -f {VERDICT_PATH} && cat {VERDICT_PATH}":
            if VERDICT_PATH not in self.files:
                return {"exit_code": 1, "success": False, "output": ""}
            return {"exit_code": 0, "success": True, "output": self.files[VERDICT_PATH]}

        if command.startswith("cat ") and "<<" not in command:
            path = command.removeprefix("cat ").split(" ", 1)[0].strip("'")
            if path in self.files:
                return {"exit_code": 0, "success": True, "output": self.files[path]}
            return {"exit_code": 1, "success": False, "output": ""}

        if command.startswith("find /workspace/.setup_agent/contexts"):
            prefix = "/workspace/.setup_agent/contexts/"
            names = [
                path.removeprefix(prefix) for path in sorted(self.files) if path.startswith(prefix)
            ]
            return {"exit_code": 0, "success": True, "output": "\n".join(names)}

        if command.startswith("find /workspace -maxdepth 1 -name 'setup-report-*.md'"):
            paths = [
                path
                for path in sorted(self.files)
                if path.startswith("/workspace/setup-report-") and path.endswith(".md")
            ]
            return {"exit_code": 0, "success": True, "output": "\n".join(paths)}

        if command.startswith("find /workspace -maxdepth 1 -name '*report*.md'"):
            return {"exit_code": 0, "success": True, "output": ""}

        if command.startswith("cat > /workspace/setup-report-"):
            if self.fail_report_writes:
                return {"exit_code": 1, "success": False, "output": "write failed"}
            path = command.split("<<", 1)[0].removeprefix("cat > ").strip()
            body = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.files[path] = body
            return {"exit_code": 0, "success": True, "output": ""}

        if "base64 -d > /workspace/setup-report-" in command:
            if self.fail_report_writes:
                return {"exit_code": 1, "success": False, "output": "fallback failed"}
            return {"exit_code": 0, "success": True, "output": ""}

        return {"exit_code": 0, "success": True, "output": ""}


@pytest.fixture
def snapshot_factory():
    def factory(
        *,
        verdict="partial",
        unique_total=328,
        unique_passed=0,
        unique_failed=0,
        unique_errors=328,
        unique_skipped=0,
        raw_executions=987,
        flaky_count=0,
    ):
        return RunVerdictSnapshot(
            run_id="tvm-run",
            finalized_at="2026-07-17T12:00:00Z",
            verdict=verdict,
            build_evidence=BuildEvidenceSnapshot(
                observed=True,
                green=verdict == "success",
                outcome=(
                    OperationOutcome.SUCCESS if verdict == "success" else OperationOutcome.PARTIAL
                ),
                evidence_status=EvidenceStatus.VERIFIED,
                refs=("build.log",),
            ),
            test_stats=SnapshotTestStats(
                discovered=unique_total,
                executed=unique_total,
                passed=unique_passed,
                failed=unique_failed,
                errors=unique_errors,
                skipped=unique_skipped,
                raw=SnapshotTestCounts(
                    executed=raw_executions,
                    passed=unique_passed,
                    failed=unique_failed,
                    errors=max(raw_executions - unique_passed - unique_failed, 0),
                    skipped=unique_skipped,
                ),
                flaky_count=flaky_count,
            ),
            conflicts=("test_retry_evidence",),
        )

    return factory


@pytest.fixture
def tvm_snapshot(snapshot_factory):
    return snapshot_factory()


def _phase_trunk(*, legacy=False):
    task_id = "task_1" if legacy else "phase_report"
    return json.dumps(
        {
            "context_id": "trunk_tvm",
            "created_at": "2026-07-17 11:00:00",
            "last_updated": "2026-07-17 12:00:00",
            "goal": "Set up TVM",
            "legacy": legacy,
            "todo_list": [
                {"id": task_id, "description": "Generate setup report", "status": "completed"}
            ],
        }
    )


def _report_text(*, verdict="SUCCESS", total=987, passed=987):
    return (
        "# Project Setup Report\n\n"
        "**Generated:** 2026-07-17 12:00:00\n"
        f"**Result:** {verdict}\n\n"
        f"| **Tests Executed** | {total} |\n"
        f"| **Tests Passed** | {passed} |\n"
    )


@dataclass(frozen=True)
class RenderedSurface:
    verdict: str
    tests: int
    text: str

    @property
    def primary_test_total(self):
        return self.tests


@dataclass(frozen=True)
class RenderedSurfaces:
    markdown: RenderedSurface
    condensed: RenderedSurface
    cli: RenderedSurface
    web: RenderedSurface


def _verdict_from_text(text):
    match = re.search(r"\b(SUCCESS|PARTIAL|FAILED|UNKNOWN)\b", text)
    assert match, text
    return match.group(1).lower()


def _markdown_tests(text):
    match = re.search(r"\*\*Tests:\*\* \d+ / (\d+) passed", text)
    assert match, text
    return int(match.group(1))


def _condensed_tests(text):
    match = re.search(r"Tests:.*?\b(\d+) executed\b", text)
    assert match, text
    return int(match.group(1))


def _cli_tests(text):
    match = re.search(r"Tests: (\d+) unique", text)
    assert match, text
    return int(match.group(1))


class SurfaceHarness:
    def render_all(self, snapshot):
        orchestrator = SnapshotOrchestrator({VERDICT_PATH: snapshot.model_dump_json()})
        tool = ReportTool(orchestrator, workflow_mode="setup")
        report_snapshot = tool._build_report_snapshot(
            snapshot,
            report_filename="setup-report-test.md",
            project_info={"type": "Native Project", "build_system": "CMake"},
        )
        markdown = tool._generate_markdown_report(
            "TVM setup",
            snapshot.verdict,
            "",
            "2026-07-17 12:00:00",
            {"type": "Native Project", "build_system": "CMake"},
            {},
            {},
            report_snapshot,
        )
        condensed = tool._generate_condensed_log_output(
            snapshot.verdict,
            "setup-report-test.md",
            {},
            report_snapshot,
        )
        termination = RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=ReportDeliveryStatus.DELIVERED,
        )
        cli_text, _ = main_module._render_setup_cli_result(snapshot, termination, "tvm")

        files = {
            VERDICT_PATH: snapshot.model_dump_json(),
            "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        }
        item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
        detail = _session_detail(item, "sag-tvm", None)

        return RenderedSurfaces(
            markdown=RenderedSurface(
                _verdict_from_text(markdown), _markdown_tests(markdown), markdown
            ),
            condensed=RenderedSurface(
                _verdict_from_text(condensed), _condensed_tests(condensed), condensed
            ),
            cli=RenderedSurface(_verdict_from_text(cli_text), _cli_tests(cli_text), cli_text),
            web=RenderedSurface(detail.canonical_verdict, detail.test.total, str(detail)),
        )

    def fail_report_after_snapshot(self, snapshot):
        orchestrator = SnapshotOrchestrator(
            {VERDICT_PATH: snapshot.model_dump_json()},
            fail_report_writes=True,
        )
        tool = ReportTool(orchestrator, workflow_mode="setup")
        result = tool.execute(summary="TVM setup", status="success")
        self.failed_report_orchestrator = orchestrator
        return RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=(
                ReportDeliveryStatus.FAILED
                if result.operation_outcome is OperationOutcome.FAILED
                else ReportDeliveryStatus.DELIVERED
            ),
        )

    def read_snapshot(self):
        return read_verdict_snapshot(self.failed_report_orchestrator)


@pytest.fixture
def surface_harness():
    return SurfaceHarness()


def test_all_surfaces_render_the_same_snapshot(tvm_snapshot, surface_harness):
    rendered = surface_harness.render_all(tvm_snapshot)

    assert rendered.markdown.verdict == tvm_snapshot.verdict
    assert rendered.condensed.verdict == tvm_snapshot.verdict
    assert rendered.cli.verdict == tvm_snapshot.verdict
    assert rendered.web.verdict == tvm_snapshot.verdict
    assert {
        rendered.markdown.tests,
        rendered.condensed.tests,
        rendered.cli.tests,
        rendered.web.tests,
    } == {tvm_snapshot.test_stats.executed}


def test_report_failure_does_not_mutate_setup_verdict(tvm_snapshot, surface_harness):
    termination = surface_harness.fail_report_after_snapshot(tvm_snapshot)

    assert termination.snapshot_ref.endswith("verdict.json")
    assert termination.report_delivery_status is ReportDeliveryStatus.FAILED
    assert surface_harness.read_snapshot().verdict == tvm_snapshot.verdict


def test_setup_report_never_calls_legacy_verdict_or_scan_owners(monkeypatch, tvm_snapshot):
    orchestrator = SnapshotOrchestrator({VERDICT_PATH: tvm_snapshot.model_dump_json()})
    tool = ReportTool(orchestrator, workflow_mode="setup", physical_validator=object())

    def forbidden(*args, **kwargs):
        raise AssertionError("setup report entered a legacy verdict or scan owner")

    for name in (
        "_verify_execution_history",
        "_build_legacy_report_snapshot",
        "_legacy_snapshot_kernel_verdict",
        "_determine_actual_status",
        "_build_module_metrics",
        "_load_test_history",
    ):
        monkeypatch.setattr(tool, name, forbidden)

    result = tool.execute(summary="TVM setup", status="success")

    assert result.operation_outcome is OperationOutcome.SUCCESS
    assert result.metadata["report_snapshot"]["status"]["verdict"] == tvm_snapshot.verdict


def test_setup_report_overwrites_conflicting_nonzero_caller_evidence(tvm_snapshot):
    orchestrator = SnapshotOrchestrator({VERDICT_PATH: tvm_snapshot.model_dump_json()})
    tool = ReportTool(orchestrator, workflow_mode="setup")

    result = tool.execute(
        summary="Caller claims a different run",
        status="success",
        evidence_status="success",
        test_stats={
            "discovered": 987,
            "executed": 987,
            "passed": 987,
            "failed": 0,
            "skipped": 0,
        },
        conflicts=["caller_conflict"],
        evidence_refs=["caller.log"],
    )

    assert result.test_stats is not None
    assert result.test_stats.executed == tvm_snapshot.test_stats.executed
    assert result.test_stats.passed == tvm_snapshot.test_stats.passed
    assert result.conflicts == list(tvm_snapshot.conflicts)
    assert result.evidence_refs == ["build.log"]
    assert result.metadata["status"] == tvm_snapshot.verdict
    assert result.metadata["final_flow_status"] == tvm_snapshot.verdict
    assert result.metadata["test_stats"]["executed"] == tvm_snapshot.test_stats.executed
    assert "987 executed" not in result.output
    assert "caller_conflict" not in result.output


def test_setup_report_terminal_ui_uses_sealed_build_and_unique_test_stats(
    snapshot_factory,
):
    snapshot = snapshot_factory(
        verdict="success",
        unique_total=5,
        unique_passed=5,
        unique_failed=0,
        unique_errors=0,
        raw_executions=7,
    )
    orchestrator = SnapshotOrchestrator({VERDICT_PATH: snapshot.model_dump_json()})
    console = Console(record=True, width=100)
    manager = UIManager(project_name="tvm", console=console)
    tool = ReportTool(orchestrator, workflow_mode="setup")
    tool.set_ui_manager(manager)

    result = tool.execute(summary="TVM setup", status="success")
    console.print(manager._format_report_summary())
    rendered = console.export_text()

    assert result.succeeded is True
    assert manager.report_data["build_success"] is True
    assert manager.report_data["total_tests"] == 5
    assert manager.report_data["passed_tests"] == 5
    assert manager.report_data["test_pass_rate"] == 100.0
    assert "Build: SUCCESS" in rendered
    assert "Tests: 5/5 passed (100.0%)" in rendered


def test_renderers_use_unique_not_raw_retry_total(surface_harness, snapshot_factory):
    snapshot = snapshot_factory(unique_total=328, raw_executions=987)

    rendered = surface_harness.render_all(snapshot)

    assert {
        rendered.markdown.tests,
        rendered.condensed.tests,
        rendered.cli.tests,
        rendered.web.tests,
    } == {328}
    for surface in (
        rendered.markdown,
        rendered.condensed,
        rendered.cli,
        rendered.web,
    ):
        assert surface.primary_test_total != 987
    for surface in (rendered.markdown, rendered.condensed, rendered.cli):
        primary_test_lines = [line for line in surface.text.splitlines() if "Tests" in line]
        assert primary_test_lines
        assert all("987" not in line for line in primary_test_lines)


def test_all_surfaces_preserve_visible_flaky_count(surface_harness, snapshot_factory):
    snapshot = snapshot_factory(
        verdict="success",
        unique_total=5,
        unique_passed=5,
        unique_failed=0,
        unique_errors=0,
        raw_executions=7,
        flaky_count=3,
    )

    rendered = surface_harness.render_all(snapshot)

    assert "3 flaky" in rendered.markdown.text
    assert "3 flaky" in rendered.condensed.text
    assert "3 flaky" in rendered.cli.text
    assert "flaky_count=3" in rendered.web.text


@pytest.mark.parametrize(
    ("verdict", "expected_exit"),
    [("success", 0), ("partial", 1), ("failed", 1), ("unknown", 1)],
)
def test_cli_exit_code_comes_only_from_snapshot(verdict, expected_exit, snapshot_factory):
    snapshot = snapshot_factory(verdict=verdict)
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )

    _, exit_code = main_module._render_setup_cli_result(snapshot, termination, "tvm")

    assert exit_code == expected_exit


def test_report_delivery_failure_warns_without_changing_success_exit(snapshot_factory):
    snapshot = snapshot_factory(
        verdict="success",
        unique_passed=328,
        unique_errors=0,
    )
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.FAILED,
    )

    text, exit_code = main_module._render_setup_cli_result(snapshot, termination, "tvm")

    assert exit_code == 0
    assert "WARNING" in text
    assert "report delivery failed" in text.lower()
    assert "SUCCESS" in text


def test_web_valid_snapshot_owns_verdict_and_primary_counts(tvm_snapshot):
    files = {
        VERDICT_PATH: tvm_snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == tvm_snapshot.verdict
    assert detail.snapshot_status == "valid"
    assert detail.test.total == 328
    assert detail.test.raw_executions == 987
    assert detail.verdict.verdict == tvm_snapshot.verdict
    assert detail.verdict.source == "snapshot"
    assert detail.report_delivery_status is None


def test_web_valid_snapshot_headline_ignores_mutable_module_rollup(snapshot_factory):
    snapshot = snapshot_factory(
        verdict="success",
        unique_passed=328,
        unique_errors=0,
    )
    files = {
        VERDICT_PATH: snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        "/workspace/.setup_agent/module_metrics.json": json.dumps(
            {
                "modules": [],
                "module_summary": {
                    "modulesTotal": 4,
                    "modulesBuilt": 0,
                    "modulesFailed": 4,
                    "singleModule": False,
                },
            }
        ),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.verdict is not None
    assert detail.verdict.verdict == "success"
    assert detail.verdict.tone == "success"
    assert detail.verdict.headline == "Build passed. 328 tests passing"


def test_web_exposes_report_delivery_only_from_durable_flow_data(tvm_snapshot):
    trunk = json.loads(_phase_trunk())
    trunk["run_termination"] = {"report_delivery_status": "failed"}
    files = {
        VERDICT_PATH: tvm_snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": json.dumps(trunk),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == tvm_snapshot.verdict
    assert detail.report_delivery_status == "failed"


def test_web_missing_new_snapshot_is_unknown_without_reconstruction():
    files = {
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == "unknown"
    assert detail.snapshot_status == "missing"
    assert detail.test.total == 0
    assert detail.evidence_status == "unknown"
    assert detail.legacy is False


def test_web_corrupt_snapshot_never_falls_back_even_for_legacy_session():
    files = {
        VERDICT_PATH: "{not-json",
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(legacy=True),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == "unknown"
    assert detail.snapshot_status == "corrupt"
    assert detail.test.total == 0
    assert detail.legacy is False


def test_web_missing_legacy_snapshot_requires_labeled_legacy_fallback():
    files = {
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(legacy=True),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == "unknown"
    assert detail.snapshot_status == "legacy"
    assert detail.test.total == 987
    assert detail.evidence_status == "unknown"
    assert detail.legacy is True
    assert detail.verdict.verdict == "unknown"
    assert detail.verdict.source == "legacy"


def test_web_historical_shape_without_legacy_marker_stays_unknown():
    trunk = json.loads(_phase_trunk(legacy=True))
    trunk.pop("legacy")
    files = {
        "/workspace/.setup_agent/contexts/trunk_tvm.json": json.dumps(trunk),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == "unknown"
    assert detail.snapshot_status == "missing"
    assert detail.test.total == 0
    assert detail.legacy is False
