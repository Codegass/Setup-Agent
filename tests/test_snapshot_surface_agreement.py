import json
import re
from dataclasses import dataclass
from typing import get_args

import pytest
from container_evidence_fakes import ContainerFS, canonical_json, complete_run_pin
from result_card_fakes import (
    CLEAN_TEST_COUNTS,
    RUN_ID,
    evaluated_ci_comparison,
    module_metrics,
    phase_record,
    snapshot_dict,
)
from rich.console import Console

import sag.main as main_module
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    RUN_PIN_LOGICAL_ARTIFACT_ID,
    VERDICT_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.verdict_finalizer import (
    BuildEvidenceSnapshot,
    PhaseRecordSnapshot,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
    read_verdict_snapshot,
)
from sag.console.result_block import (
    _MAX_ITEMS,
    GUTTER,
    LABEL_WIDTH,
    STATUS_WIDTH,
    render_result_block,
)
from sag.evidence import EvidenceStatus, OperationOutcome
from sag.metrics.attainment import AttainmentVerdict
from sag.result_card.build import build_result_card
from sag.result_card.glosses import REASON_GLOSS
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import ROW_LABELS, ROW_ORDER, RunResultCard
from sag.result_card.rows import _CI_STATUS_WORD, _CI_TONE, NOT_COMPARED, NOT_SCORED
from sag.tools.report_tool import ReportTool
from sag.web.session_registry import _session_detail, _setup_artifact_item

VERDICT_PATH = "/workspace/.setup_agent/verdict.json"
RUN_PIN_PATH = "/workspace/.setup_agent/run-pin.json"


class _MemorySink:
    path = "/host/snapshot-surface-control-events.jsonl"

    def emit(self, kind, payload, *, source=None):
        del kind, payload, source


class SnapshotOrchestrator:
    def __init__(self, files=None, *, fail_report_writes=False, publish_verdict=True):
        self.filesystem = ContainerFS(files=files)
        self.files = self.filesystem.files
        self.fail_report_writes = fail_report_writes
        self.commands = self.filesystem.commands
        raw = self.files.get(VERDICT_PATH)
        try:
            snapshot = RunVerdictSnapshot.model_validate_json(raw) if raw is not None else None
        except (TypeError, ValueError):
            snapshot = None
        if publish_verdict and snapshot is not None and raw == snapshot.model_dump_json():
            pin_raw = canonical_json(complete_run_pin(snapshot.run_id, "a" * 40))
            self.files.setdefault(RUN_PIN_PATH, pin_raw)
            authority = EvidencePublicationAuthority(run_id=snapshot.run_id, sink=_MemorySink())
            token = install_evidence_publication_authority(authority, orchestrator=self)
            reset_evidence_publication_authority(token)
            authority.publish_revision(
                record_kind="verdict",
                record_id=VERDICT_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
                raw=raw.encode("utf-8"),
                expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
            )
            authority.publish_revision(
                record_kind="run_pin",
                record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
                raw=self.files[RUN_PIN_PATH].encode("utf-8"),
                expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
            )

    def execute_command(self, command, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in command or "SAG_JSON_RECORD_V1" in command:
            return self.filesystem(command, **kwargs)

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

        return self.filesystem(command, **kwargs)


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
        build_modules = {
            "success": {
                "rate": 100.0,
                "band": "fully",
                "numerator": 1,
                "denominator": 1,
            },
            "partial": {
                "rate": 90.0,
                "band": "most",
                "numerator": 9,
                "denominator": 10,
            },
            "failed": {
                "rate": 0.0,
                "band": "none",
                "numerator": 0,
                "denominator": 1,
            },
            "unknown": {
                "band": "unavailable",
                "reason": "fixture module scan unavailable",
            },
        }[verdict]
        return RunVerdictSnapshot(
            run_id="tvm-run",
            finalized_at="2026-07-17T12:00:00Z",
            verdict=verdict,
            build_evidence=BuildEvidenceSnapshot(
                judgment=verdict,
                source="physical",
                observed=True,
                green=verdict == "success",
                outcome=(
                    OperationOutcome.SUCCESS if verdict == "success" else OperationOutcome.PARTIAL
                ),
                evidence_status=EvidenceStatus.VERIFIED,
                refs=("build.log",),
            ),
            test_stats=SnapshotTestStats(
                # Success snapshots in this renderer fixture explicitly start
                # after the independent execution-completion gate has passed.
                judgment="success" if verdict == "success" else "unknown",
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
            rates={
                "build": {
                    "modules": build_modules,
                    "classes": {
                        "band": "unavailable",
                        "reason": "fixture class census unavailable",
                    },
                },
                "test": {
                    "cases": {
                        "rate": 100.0,
                        "band": "fully",
                        "numerator": unique_total,
                        "denominator": unique_total,
                    },
                    "modules": {
                        "band": "unavailable",
                        "reason": "fixture test survey unavailable",
                    },
                },
                "coverage": {
                    "status": "unavailable",
                    "reason": "coverage pass not run",
                },
            },
            conflicts=() if verdict == "success" else ("test_retry_evidence",),
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
    match = re.search(
        r"Verdict \(derived\):(?:\*\*)?\s*(success|partial|failed|unknown)",
        text,
        re.IGNORECASE,
    ) or re.search(r"\b(SUCCESS|PARTIAL|FAILED|UNKNOWN)\b", text)
    assert match, text
    return match.group(1).lower()


def _markdown_verdict_from_text(text):
    """The report states the run's verdict as the Result table's Setup row."""

    match = re.search(
        r"^\| \*\*Setup\*\* \| (success|partial|failed|unknown) \|",
        text,
        re.MULTILINE,
    )
    assert match, text
    return match.group(1)


def _markdown_tests(text):
    """The same layer the other surfaces read, under the report's own label."""

    match = re.search(
        r"Set aside: no module or test name recorded: \d+/(\d+) passed",
        text,
    )
    assert match, text
    return int(match.group(1))


def _condensed_tests(text):
    match = re.search(
        r"Unattributed observations \(not verdict-bearing\): \d+/(\d+) passed",
        text,
    )
    assert match, text
    return int(match.group(1))


def _cli_verdict_from_text(text):
    """The block states the run's verdict as the Setup row's own status word."""

    match = re.search(
        r"^ Setup\s+\[[a-z]+\](success|partial|failed|unknown)\[",
        text,
        re.MULTILINE,
    )
    assert match, text
    return match.group(1)


def _cli_tests(text):
    """The block names the raw execution grain; it prints it only when it differs."""

    match = re.search(r"([\d,]+) raw executions", text) or re.search(r"([\d,]+) executed", text)
    assert match, text
    return int(match.group(1).replace(",", ""))


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
                _markdown_verdict_from_text(markdown), _markdown_tests(markdown), markdown
            ),
            condensed=RenderedSurface(
                _verdict_from_text(condensed), _condensed_tests(condensed), condensed
            ),
            cli=RenderedSurface(_cli_verdict_from_text(cli_text), _cli_tests(cli_text), cli_text),
            web=RenderedSurface(
                detail.canonical_verdict,
                detail.test.evidence_layers.tests.unattributed_observations.executed,
                str(detail),
            ),
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


def test_report_states_its_own_delivery_and_the_counts_it_cannot_reach(
    tvm_snapshot, surface_harness
):
    """Neither row may say something untrue of the document holding it.

    The writer raises when the save fails, so a reader of this document is the
    proof it was delivered; and the run's counts do exist — this surface simply
    cannot reach them from inside the container.
    """

    markdown = surface_harness.render_all(tvm_snapshot).markdown.text

    assert "| **Report** | delivered | /workspace/setup-report-test.md |" in markdown
    assert "no report was recorded" not in markdown
    assert "| **Setup** | partial | run counts unavailable |" in markdown
    assert "no run counts were recorded" not in markdown


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
    } == {tvm_snapshot.test_stats.raw.executed}


def test_all_surfaces_keep_failures_and_errors_distinct(tvm_snapshot, surface_harness):
    rendered = surface_harness.render_all(tvm_snapshot)

    for surface in (rendered.markdown, rendered.condensed):
        assert "0 failed" in surface.text
        assert "987 errors" in surface.text
    # The block reports the run's own execution grain, and keeps the two counts
    # side by side there rather than summing them into one red number.
    assert "0 failed · 328 errors" in rendered.cli.text
    assert "987 raw executions" in rendered.cli.text
    assert "fail_count=0" in rendered.web.text
    assert "errors=328" in rendered.web.text


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


def test_setup_report_evidence_summary_keeps_failures_and_errors_distinct(tvm_snapshot):
    orchestrator = SnapshotOrchestrator({VERDICT_PATH: tvm_snapshot.model_dump_json()})
    tool = ReportTool(orchestrator, workflow_mode="setup")

    result = tool.execute(summary="TVM collection errors", status="failed")

    assert result.test_stats is not None
    assert result.test_stats.failed == 0
    assert result.test_stats.errors == 328
    assert "failed 0 · errors 328" in result.output
    assert "328 failed" not in result.output


def test_renderers_keep_observation_execution_grain_explicit(surface_harness, snapshot_factory):
    snapshot = snapshot_factory(unique_total=328, raw_executions=987)

    rendered = surface_harness.render_all(snapshot)

    assert {
        rendered.markdown.tests,
        rendered.condensed.tests,
        rendered.cli.tests,
        rendered.web.tests,
    } == {987}
    for surface in (
        rendered.markdown,
        rendered.condensed,
        rendered.cli,
        rendered.web,
    ):
        assert surface.primary_test_total != 328
    # Both surfaces name the wider grain as a layer held apart from the
    # verdict; only the words differ, the report saying it in the reader's.
    markdown_lines = [
        line
        for line in rendered.markdown.text.splitlines()
        if "Tests" in line or "Set aside" in line
    ]
    assert markdown_lines
    assert any("Set aside" in line and "987" in line for line in markdown_lines)
    condensed_lines = [
        line
        for line in rendered.condensed.text.splitlines()
        if "Tests" in line or "observations" in line
    ]
    assert condensed_lines
    assert any("not verdict-bearing" in line and "987" in line for line in condensed_lines)
    # The block keeps the grains apart by naming the larger one as raw, so 987
    # is never offered as the number of tests this run has.
    assert "328 executed" in rendered.cli.text
    assert "987 raw executions" in rendered.cli.text
    assert "987 executed" not in rendered.cli.text


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

    assert "3 flaky" in rendered.condensed.text
    assert "3 flaky" not in rendered.cli.text
    # The block does not drop the retry signal silently: it states the wider
    # execution grain instead of a flaky count it would have to name itself.
    assert "7 raw executions" in rendered.cli.text
    # The report prints the same result card, so it keeps the retry signal the
    # same way the block does rather than in a second, differently-worded line.
    assert "3 flaky" not in rendered.markdown.text
    assert "7 raw executions" in rendered.markdown.text
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


def test_report_delivery_failure_is_stated_without_changing_success_exit(snapshot_factory):
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
    assert "the setup report was not written" in text
    assert "WARNING" not in text
    assert _cli_verdict_from_text(text) == "success"


def test_web_valid_snapshot_owns_verdict_and_primary_counts(tvm_snapshot):
    files = {
        VERDICT_PATH: tvm_snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=987, passed=987),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == tvm_snapshot.verdict
    assert detail.rates == tvm_snapshot.rates
    assert detail.model_dump(mode="json", by_alias=True)["rates"] == tvm_snapshot.rates
    assert detail.snapshot_status == "valid"
    assert detail.test.total == 328
    assert detail.test.raw_executions == 987
    assert detail.result_card is not None
    assert detail.result_card.verdict == tvm_snapshot.verdict
    assert detail.result_card.row("tests").headline.startswith("328 executed")
    assert detail.report_delivery_status is None


def test_web_serves_a_result_for_a_seal_written_under_an_older_schema(tvm_snapshot):
    """184 of 681 archived records are v4, and every one read as "no result".

    `read_live_verdict_snapshot` answers `unknown` for any schema but the
    current one, so a v4 seal could never match its own forensic copy and the
    registry served no card — while `sag result <dir>`, reading the same bytes,
    printed all seven rows. The browser then stated that the run recorded no
    result, which is the one thing this layer exists not to do.
    """

    payload = json.loads(tvm_snapshot.model_dump_json())
    payload["schema_version"] = 4
    files = {
        VERDICT_PATH: json.dumps(payload),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.result_card is not None, "the card the terminal prints from these bytes"
    assert detail.result_card.verdict == tvm_snapshot.verdict
    assert detail.result_card.row("tests").headline.startswith("328 executed")
    # Served, but never as a reading: the seal reader could not re-read it.
    assert detail.result_card.verdict_source == "legacy"
    assert detail.snapshot_status == "legacy"
    assert detail.canonical_verdict == tvm_snapshot.verdict


def test_web_still_refuses_a_current_seal_it_cannot_authorize(tvm_snapshot):
    """Serving an older schema is not serving anything that fails to verify."""

    payload = json.loads(tvm_snapshot.model_dump_json())
    payload["verdict"] = "failed"
    files = {
        VERDICT_PATH: json.dumps(payload),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
    }

    item = _setup_artifact_item(
        SnapshotOrchestrator(files, publish_verdict=False), "sag-tvm"
    )
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.result_card is None
    assert detail.snapshot_status == "untrusted"


def test_web_card_keeps_the_mutable_module_rollup_out_of_the_build_word(snapshot_factory):
    """The rollup is a report diagnostic and the record is the result.

    Four failed modules beside a build the run recorded as successful must not
    turn the Build row's own word around; the count is still worth showing, so
    it is shown where a reader can see what it is."""

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
                # The shape module_metrics.json is actually written in.
                "module_summary": {
                    "modules_total": 4,
                    "modules_built": 0,
                    "modules_failed": 4,
                    "single_module": False,
                },
            }
        ),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    card = detail.result_card
    assert card is not None
    assert card.verdict == "success"
    build = card.row("build")
    assert build.status == "success"
    assert build.headline == "success"
    assert build.detail == ("4 modules failed · counts are diagnostic, CI defines scope")
    assert card.row("tests").headline == (
        "328 executed · 328 passed · 0 failed · 0 errors · 0 skipped"
    )


def test_web_terminal_snapshot_overrides_stale_trunk_and_marks_unreached_phases_not_run():
    unavailable = {"band": "unavailable", "reason": "phase was not run"}
    snapshot = RunVerdictSnapshot(
        run_id="tvm-run",
        finalized_at="2026-07-17T12:00:00Z",
        verdict="failed",
        build_evidence=BuildEvidenceSnapshot(
            observed=True,
            judgment="failed",
            outcome=OperationOutcome.FAILED,
            evidence_status=EvidenceStatus.VERIFIED,
            compiled_classes=0,
        ),
        test_stats=SnapshotTestStats(discovered=15085),
        rates={
            "build": {"modules": unavailable, "classes": unavailable},
            "test": {"cases": unavailable, "modules": unavailable},
            "coverage": {"status": "unavailable", "reason": "not collected"},
        },
        phase_records=(
            PhaseRecordSnapshot(
                phase="analyze",
                attempt_id="analyze-1",
                termination="aborted",
                outcome="failed",
                validated_outcome="failed",
                reason="iteration budget exhausted",
            ),
        ),
    )
    trunk = json.loads(_phase_trunk())
    trunk["todo_list"] = [
        {"id": "phase_analyze", "description": "Analyze", "status": "in_progress"},
        {"id": "phase_build", "description": "Build", "status": "pending"},
        {"id": "phase_test", "description": "Test", "status": "pending"},
    ]
    files = {
        VERDICT_PATH: snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": json.dumps(trunk),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.status == "completed"
    assert detail.finish == "2026-07-17T12:00:00+00:00"
    assert detail.duration == "—"
    assert detail.build.state == "not_attempted"
    assert detail.build.class_count is None
    assert detail.test.state == "not_attempted"
    assert detail.test.execution_rate is None
    card = detail.result_card
    assert card is not None
    assert card.row("setup").headline == "0/1 phases"
    assert card.row("setup").detail == "blocked at analyze: iteration budget exhausted"
    assert card.row("tests").headline == "no test results were recorded"
    assert card.row("tests").reason == "the run recorded no test outcomes"


def test_web_treats_policy_skipped_build_and_test_as_not_run():
    unavailable = {"band": "unavailable", "reason": "phase was not run"}
    snapshot = RunVerdictSnapshot(
        run_id="tvm-skipped-run",
        finalized_at="2026-07-17T12:00:00Z",
        verdict="partial",
        rates={
            "build": {"modules": unavailable, "classes": unavailable},
            "test": {"cases": unavailable, "modules": unavailable},
            "coverage": {"status": "unavailable", "reason": "not collected"},
        },
        phase_records=tuple(
            PhaseRecordSnapshot(
                phase=phase,
                attempt_id=f"{phase}-1",
                termination="skipped",
                outcome="skipped",
                validated_outcome="skipped",
                reason="phase skipped by transition policy",
            )
            for phase in ("build", "test")
        ),
    )
    files = {
        VERDICT_PATH: snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
    }

    item = _setup_artifact_item(SnapshotOrchestrator(files), "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.build.state == "not_attempted"
    assert detail.test.state == "not_attempted"
    card = detail.result_card
    assert card is not None
    assert card.row("build").reason == "no build result was recorded for this run"
    assert card.row("tests").headline == "no test results were recorded"
    assert [item.title for item in card.attention] == [
        "The build phase did not finish",
        "The test phase did not finish",
    ]


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


def test_web_unpublished_success_snapshot_stays_unknown_without_metrics_fallback(
    snapshot_factory,
):
    snapshot = snapshot_factory(
        verdict="success",
        unique_passed=328,
        unique_errors=0,
    )
    files = {
        VERDICT_PATH: snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        "/workspace/setup-report-20260717-120000.md": _report_text(total=328, passed=328),
    }

    item = _setup_artifact_item(
        SnapshotOrchestrator(files, publish_verdict=False),
        "sag-tvm",
    )
    detail = _session_detail(item, "sag-tvm", None)

    assert detail.canonical_verdict == "unknown"
    assert detail.snapshot_status == "untrusted"
    assert detail.test.total == 0
    assert detail.evidence_status == "unknown"


def test_setup_report_does_not_promote_unpublished_success_snapshot(snapshot_factory):
    snapshot = snapshot_factory(
        verdict="success",
        unique_passed=328,
        unique_errors=0,
    )
    orchestrator = SnapshotOrchestrator(
        {VERDICT_PATH: snapshot.model_dump_json()},
        publish_verdict=False,
    )
    tool = ReportTool(orchestrator, workflow_mode="setup")

    result = tool.execute(summary="forged success", status="success")

    report_snapshot = result.metadata["report_snapshot"]
    assert report_snapshot["status"]["verdict"] == "unknown"
    assert result.test_stats is not None
    assert result.test_stats.executed == 0


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
    # There is no record here to copy, so the detail offers no card rather
    # than assembling one out of a report's prose.
    assert detail.result_card is None


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


# ---------------------------------------------------------------------------
# The fence: one record, every surface, one wording.
#
# Everything above holds a surface to the record. What follows holds the
# surfaces to each other: one card, printed as the end-of-run block, written as
# the report's `## Result` table and served as the web payload, must state the
# same status, the same explanation and the same findings for every row. A
# reader who watched the run end and a reader who opened the report a week
# later are owed the same facts, so a failure here names the surface that
# drifted, the row it drifted on and the record it drifted for.
# ---------------------------------------------------------------------------

_TERMINAL = "the terminal block"
_REPORT = "the report table"
_WEB = "the web payload"

#: Wide enough that nothing these records say reaches the block's wrap column,
#: so one thing the card states is one line of the block and the comparison
#: below stays exact rather than approximate.
_FENCE_WIDTH = 200

#: The block bullets a row's findings; the report bullets them as Markdown
#: under a heading. This prefix is the one piece of row text the two surfaces
#: deliberately spell differently.
_TERMINAL_BULLET = "· "

#: What the report heads a row's findings with, restated here so a failure can
#: name the row whose findings went missing rather than pointing at a heading
#: (cf. `_ITEM_HEADINGS` in sag/result_card/markdown.py).
_REPORT_ITEM_HEADINGS = {"task": "Required task steps", "ci": "Official CI findings"}

#: The two trailing lists, under the name each surface gives them. The heading
#: differs by design; the lines under it must not.
_TRAILING_LISTS = {"Needs attention": "Needs attention", "Notes": "Data notes"}

#: What both surfaces label the line that says where the result came from. Each
#: renderer keeps its own copy of the sentence under it; the label is how this
#: file finds that sentence on either surface.
_PROVENANCE_LABEL = "Record"

_DELIVERED = RunTermination(
    termination=RunTerminationStatus.COMPLETED,
    report_delivery_status=ReportDeliveryStatus.DELIVERED,
)
_REPORT_FAILED = RunTermination(
    termination=RunTerminationStatus.COMPLETED,
    report_delivery_status=ReportDeliveryStatus.FAILED,
)

#: Named records with the arguments each one's card is built from. Between them
#: every row reaches both of its shapes: measured and absent, speaking for
#: itself and collapsed onto its own status word, with findings and without.
_FENCE_RECORDS: dict[str, tuple[dict, dict]] = {
    "a clean run": (
        snapshot_dict(),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_x/setup-report.md",
            "project": "commons-cli",
            "container": "sag-commons-cli",
            "session_dir": "logs/session_x",
            "turn_count": 42,
            "tool_calls": 180,
            "tool_failures": 3,
        },
    ),
    "a partial run that was interrupted": (
        snapshot_dict(
            verdict="partial",
            conflicts=["test_execution_interrupted"],
            phase_records=[
                phase_record(
                    "build",
                    termination="blocked",
                    outcome="failure",
                    reason="the maven wrapper was missing",
                )
            ],
        ),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_y/setup-report.md",
            "turn_count": 17,
        },
    ),
    "a failed build whose report was never written": (
        snapshot_dict(
            verdict="failed",
            build_evidence={
                "observed": True,
                "green": False,
                "judgment": "failed",
                "source": "physical",
                "outcome": "failed",
                "evidence_status": "verified",
                "refs": [],
            },
        ),
        {"termination": _REPORT_FAILED},
    ),
    "a run measured against official CI": (
        snapshot_dict(
            ci_comparison=evaluated_ci_comparison(
                verdict="not_met",
                clean=False,
                red_observed=3,
                unexpected_red_ids=["a.B#c", "a.B#d", "a.B#e"],
                reason_codes=["NEW_RED_BEYOND_TARGET"],
            )
        ),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_z/setup-report.md",
        },
    ),
    # The record's word for a comparison it could not make. 135 of the 342
    # evaluated comparisons under `logs/` hold it, every one of them with no
    # score and with blocker codes on the comparison beside the attainment's
    # own. No surface may spell it `invalid`, and no surface may invent a
    # score the record declined to give.
    "a comparison the run could not make": (
        snapshot_dict(
            ci_comparison=dict(
                evaluated_ci_comparison(
                    verdict="invalid",
                    valid=False,
                    built=False,
                    clean=False,
                    clean_form="counts",
                    alpha=None,
                    alpha_test=None,
                    alpha_build=None,
                    lifecycle_parity=None,
                    cell_id="maven-compile (ubuntu-latest, JDK-8)",
                    cell_grade="B",
                    executed_observed=1328,
                    executed_target=0,
                    red_observed=1,
                    modules_matched=2,
                    modules_target=4,
                    missing_module_ids=["rocketmq-broker 5.5.1", "rocketmq-store 5.5.1"],
                    reason_codes=[
                        "CERTIFICATE_AUTHORITY_UNAVAILABLE",
                        "TARGET_TEST_UNIVERSE_EMPTY",
                    ],
                ),
                reasons=["TEST_EXECUTION_NOT_COMPLETE", "BUILD_MODULE_SCOPE_UNAVAILABLE"],
            )
        ),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_i/setup-report.md",
        },
    ),
    # A pipe is the one character that can split a Markdown column, so one
    # record states a command containing one. The report escapes it in the cell
    # and the block prints it as it is; both surfaces still have to state the
    # same command, and the table still has to parse as a table.
    "a run whose required task piped its output": (
        snapshot_dict(
            task_completion={
                "run_id": RUN_ID,
                "task_sha256": "b" * 64,
                "status": "complete",
                "steps": [
                    {
                        "id": "piped-verify",
                        "command": "mvn test | tee out.log",
                        "status": "complete",
                        "receipt_id": "inv-maven-1-ee86ae186d94-0003",
                        "exit_code": 0,
                        "reason": None,
                    }
                ],
                "reasons": [],
            }
        ),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_p/setup-report.md",
        },
    ),
    # Nothing beside the record: every row that needs a sibling artifact states
    # its absence instead, and the surfaces must agree about that too.
    "a record with nothing beside it": (snapshot_dict(), {}),
    # A schema-v3 record: it predates the rates block, the CI comparison and
    # the required task, so the card marks it `verdict_source == "legacy"`.
    # That is the only shape in which either renderer prints its provenance
    # line, and each renderer spells that line from its own copy of the
    # sentence (`_OLDER_RECORD`, in sag/console/result_block.py and in
    # sag/result_card/markdown.py). Without a record of this shape the two
    # copies are never compared and can drift apart unseen.
    "a result reconstructed from an older record": (
        snapshot_dict(
            schema_version=3,
            verdict="partial",
            rates={},
            ci_comparison=None,
            task_completion=None,
            conflicts=["test_execution_interrupted"],
            build_evidence={
                "observed": True,
                "green": True,
                "judgment": "success",
                "source": "observations",
                "outcome": "success",
                "evidence_status": "verified",
                "refs": [],
            },
            test_stats={
                "discovered": 472,
                "denominator_basis": "complete",
                "unique": dict(CLEAN_TEST_COUNTS),
                "raw": dict(CLEAN_TEST_COUNTS),
                "flaky_count": 0,
                "judgment": "partial",
                "collection_errors": 0,
                "receipt_scoped": True,
            },
        ),
        {"termination": _DELIVERED, "report_path": "logs/session_old/setup-report.md"},
    ),
    # A schema-v4 record: the most common shape in the archive (184 of 681) and
    # the one no surface comparison had ever covered. It carries rates and a CI
    # block but predates the required task, so the task row reads `not supplied`
    # — spec §2.5 — and the card marks it `legacy`, because the seal reader this
    # system ships can no longer re-read it.
    "a result sealed under the previous schema": (
        snapshot_dict(
            schema_version=4,
            verdict="partial",
            task_completion=None,
            conflicts=["maven_reactor_unverified"],
            test_stats={
                "discovered": 472,
                "denominator_basis": "complete",
                "unique": dict(CLEAN_TEST_COUNTS),
                "raw": dict(CLEAN_TEST_COUNTS),
                "flaky_count": 0,
                "judgment": "partial",
                "collection_errors": 0,
                "receipt_scoped": True,
            },
        ),
        {
            "module_metrics": module_metrics(),
            "termination": _DELIVERED,
            "report_path": "logs/session_v4/setup-report.md",
            "project": "commons-cli",
            "container": "sag-commons-cli",
            "session_dir": "logs/session_v4",
            "turn_count": 29,
            "tool_calls": 28,
        },
    ),
}


@dataclass(frozen=True)
class _PrintedRow:
    """One row of the block, split back into the things it states."""

    status: str
    said: tuple[str, ...]
    items: tuple[str, ...]


def _fence_card(record: str):
    payload, arguments = _FENCE_RECORDS[record]
    return build_result_card(payload, **arguments)


def _fence_block(card) -> str:
    console = Console(width=_FENCE_WIDTH, force_terminal=False, no_color=True, soft_wrap=False)
    with console.capture() as capture:
        console.print(render_result_block(card, width=_FENCE_WIDTH), markup=True, highlight=False)
    return capture.get()


def _printed_rows(card) -> dict[str, _PrintedRow]:
    """Read the block back: every row it printed, in the order it printed them."""

    by_label = {ROW_LABELS[key]: key for key in ROW_ORDER}
    head = GUTTER + LABEL_WIDTH
    body = head + STATUS_WIDTH
    status: dict[str, str] = {}
    said: dict[str, list[str]] = {}
    items: dict[str, list[str]] = {}
    key = None
    for line in _fence_block(card).splitlines():
        label = line[GUTTER:head].strip() if line[:GUTTER].isspace() else ""
        if label in by_label:
            key = by_label[label]
            status[key] = line[head:body].strip()
            said[key], items[key] = [], []
            text = line[body:].strip()
        elif key is not None and line.startswith(" " * body):
            text = line.strip()
        else:
            key = None
            continue
        if not text:
            continue
        if text.startswith(_TERMINAL_BULLET):
            items[key].append(text[len(_TERMINAL_BULLET) :])
        else:
            said[key].append(text)
    return {key: _PrintedRow(status[key], tuple(said[key]), tuple(items[key])) for key in status}


def _printed_lists(card) -> dict[str, tuple[str, ...]]:
    """The block's trailing lists, keyed by the heading the block gives them."""

    lists: dict[str, list[str]] = {}
    heading = None
    for line in _fence_block(card).splitlines():
        text = line.strip()
        if text in _TRAILING_LISTS and line == f"{' ' * GUTTER}{text}":
            heading = text
            lists.setdefault(heading, [])
        elif heading is not None and line.startswith("   ") and text:
            lists[heading].append(text)
        elif text:
            heading = None
    return {heading: tuple(lines) for heading, lines in lists.items()}


def _report_cells(card) -> dict[str, tuple[str, str]]:
    """The table's body: label -> (status cell, detail cell), in printed order."""

    cells: dict[str, tuple[str, str]] = {}
    for line in render_result_card_markdown(card):
        match = re.match(
            r"^\| \*\*(?P<label>[^*]+)\*\* \| (?P<status>[^|]+) \| (?P<detail>.*) \|$", line
        )
        if match:
            cells[match.group("label").strip()] = (
                match.group("status").strip(),
                match.group("detail").strip(),
            )
    return cells


def _report_lists(card) -> dict[str, tuple[str, ...]]:
    """Every `###` list the section writes, keyed by its heading."""

    lists: dict[str, list[str]] = {}
    heading = None
    for line in render_result_card_markdown(card):
        if line.startswith("### "):
            heading = line[4:].strip()
            lists.setdefault(heading, [])
        elif heading is not None and line.startswith("- "):
            lists[heading].append(line[2:].strip())
    return {heading: tuple(lines) for heading, lines in lists.items()}


def _stated_parts(row) -> tuple[str, ...]:
    """What a row states beyond its status word, in the order every surface states it.

    A headline that only repeats the status word says the same thing twice, so
    both renderers drop it and lead with the explanation instead. The rule is
    written out here as well as in each renderer on purpose: this is the
    contract the surfaces are held to, and a renderer that quietly stops
    following it fails here by name instead of shipping.
    """

    parts = (
        None if row.headline == row.status else row.headline,
        row.detail,
        row.reason,
    )
    return tuple(part for part in parts if part)


def _as_report_text(text: str) -> str:
    """The report escapes a cell's pipes and flattens its newlines; nothing else."""

    return text.replace("|", r"\|").replace("\n", " ")


def _report_detail(parts: tuple[str, ...]) -> str:
    return _as_report_text(" · ".join(parts)) if parts else "—"


def _report_item_heading(row) -> str:
    return _REPORT_ITEM_HEADINGS.get(row.key, f"{row.label} details")


def _printed_items(items: tuple[str, ...]) -> tuple[str, ...]:
    """The block prints a budget of a row's findings and counts the rest."""

    shown = list(items[:_MAX_ITEMS])
    if len(items) > _MAX_ITEMS:
        shown.append(f"+{len(items) - _MAX_ITEMS} more")
    return tuple(shown)


def _disagrees(surface: str, record: str, key: str, saw, want, *, against: str = "the card") -> str:
    return (
        f"{surface} states {saw!r} for the {key} row of {record}; "
        f"{against} states {want!r}. One card, one wording: fix the renderer, not this test."
    )


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_every_surface_states_the_same_status_and_the_same_explanation(record):
    card = _fence_card(record)
    printed = _printed_rows(card)
    cells = _report_cells(card)

    assert list(printed) == list(
        ROW_ORDER
    ), f"{_TERMINAL} prints rows {list(printed)} for {record}; a card states {list(ROW_ORDER)}"
    assert list(cells) == [ROW_LABELS[key] for key in ROW_ORDER], (
        f"{_REPORT} tabulates rows {list(cells)} for {record}; "
        f"a card states {[ROW_LABELS[key] for key in ROW_ORDER]}"
    )

    for key in ROW_ORDER:
        row = card.row(key)
        status, detail = cells[ROW_LABELS[key]]
        said = _stated_parts(row)

        assert printed[key].status == row.status, _disagrees(
            _TERMINAL, record, key, printed[key].status, row.status
        )
        assert status == row.status, _disagrees(_REPORT, record, key, status, row.status)
        assert printed[key].said == said, _disagrees(
            _TERMINAL, record, key, printed[key].said, said
        )
        assert detail == _report_detail(said), _disagrees(
            _REPORT, record, key, detail, _report_detail(said)
        )


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_every_surface_files_a_rows_findings_under_that_row(record):
    card = _fence_card(record)
    printed = _printed_rows(card)
    written = _report_lists(card)
    expected_headings = set()

    for key in ROW_ORDER:
        row = card.row(key)
        heading = _report_item_heading(row)
        assert printed[key].items == _printed_items(row.items), _disagrees(
            _TERMINAL, record, key, printed[key].items, _printed_items(row.items)
        )
        if not row.items:
            assert heading not in written, (
                f"{_REPORT} heads a list {heading!r} for {record}, but the {key} row of the "
                "card has no findings to put under it"
            )
            continue
        expected_headings.add(heading)
        want = tuple(_as_report_text(item) for item in row.items)
        assert written.get(heading) == want, _disagrees(
            _REPORT, record, key, written.get(heading), want
        )

    # A finding under a heading no row accounts for is a bullet orphaned from
    # its measurement: the table above is the only thing that could have said
    # which row it belongs to.
    filed = set(written) - set(_TRAILING_LISTS.values())
    assert filed == expected_headings, (
        f"{_REPORT} files findings under {sorted(filed)} for {record}; "
        f"the card's rows account for {sorted(expected_headings)}"
    )


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_every_surface_lists_the_same_attention_and_the_same_notes(record):
    card = _fence_card(record)
    printed = _printed_lists(card)
    written = _report_lists(card)
    expected = {
        "Needs attention": tuple(
            item.title if not item.detail else f"{item.title} — {item.detail}"
            for item in card.attention
        ),
        "Notes": tuple(card.notes),
    }

    for block_heading, report_heading in _TRAILING_LISTS.items():
        want = expected[block_heading]
        assert printed.get(block_heading, ()) == want, _disagrees(
            _TERMINAL, record, block_heading, printed.get(block_heading, ()), want
        )
        escaped = tuple(_as_report_text(text) for text in want)
        assert written.get(report_heading, ()) == escaped, _disagrees(
            _REPORT, record, report_heading, written.get(report_heading, ()), escaped
        )


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_the_web_payload_carries_the_same_rows_as_the_terminal(record):
    card = _fence_card(record)
    printed = _printed_rows(card)
    cells = _report_cells(card)

    # The hop this leg reaches is the card's own: `model_dump(mode="json",
    # by_alias=True)` out to the JSON shape the API serves — camelCase, the
    # same convention as everything else on the wire, which is why the dump is
    # taken by alias and not by field name — and `model_validate` back in. It does
    # NOT run the registry — `_setup_artifact_item` and `_session_detail` are
    # imported above for the tests further up this file, not for this one, and
    # that the registry performs this same dump and validate is pinned in
    # tests/test_web_session_registry.py, not here.
    #
    # What that still buys is not circular: every row below is read off the far
    # side of the hop and held against `printed`, parsed back out of rendered
    # terminal text, and `cells`, parsed back out of rendered Markdown. A field
    # the JSON shape drops and a renderer that drifted from the card both fail
    # here, because neither of the two surfaces it is compared against went
    # through this hop.
    served = card.model_dump(mode="json", by_alias=True)
    delivered = RunResultCard.model_validate(served)

    assert [key for key in served if "_" in key] == [], (
        f"{_WEB} serves {[key for key in served if '_' in key]} for {record}; the card body "
        "is camelCase like the rest of the API"
    )

    assert [row["key"] for row in served["rows"]] == list(printed), (
        f"{_WEB} serves rows {[row['key'] for row in served['rows']]} for {record}; "
        f"{_TERMINAL} printed {list(printed)}"
    )
    for position, key in enumerate(ROW_ORDER):
        payload = served["rows"][position]
        for field in ("label", "status", "tone", "headline", "detail", "reason", "items"):
            assert field in payload, (
                f"{_WEB} serves no {field!r} for the {key} row of {record}; the block and the "
                "report both state what it holds"
            )
        row = delivered.row(key)
        said = _stated_parts(row)
        status, detail = cells[ROW_LABELS[key]]

        assert row.status == printed[key].status, _disagrees(
            _WEB, record, key, row.status, printed[key].status, against=_TERMINAL
        )
        assert row.status == status, _disagrees(
            _WEB, record, key, row.status, status, against=_REPORT
        )
        assert said == printed[key].said, _disagrees(
            _WEB, record, key, said, printed[key].said, against=_TERMINAL
        )
        assert _report_detail(said) == detail, _disagrees(
            _WEB, record, key, _report_detail(said), detail, against=_REPORT
        )
        assert _printed_items(row.items) == printed[key].items, _disagrees(
            _WEB, record, key, _printed_items(row.items), printed[key].items, against=_TERMINAL
        )


def _printed_provenance(card) -> str | None:
    """The block's `Record` line, or nothing when it printed none."""

    head = GUTTER + LABEL_WIDTH
    for line in _fence_block(card).splitlines():
        if line[:GUTTER].isspace() and line[GUTTER:head].strip() == _PROVENANCE_LABEL:
            return line[head:].strip()
    return None


def _written_provenance(card) -> str | None:
    """The report's `**Record**` line, or nothing when it wrote none."""

    prefix = f"**{_PROVENANCE_LABEL}** — "
    for line in render_result_card_markdown(card):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_every_surface_says_the_same_thing_about_where_the_result_came_from(record):
    """A result rebuilt from an older record is worth less, and says so identically.

    Each renderer keeps its own copy of that sentence, so nothing but a record
    of this shape reaching both of them stops the copies drifting apart: one
    surface would go on calling a reconstruction a reading, and only a reader
    holding a terminal beside a report would ever notice.
    """

    card = _fence_card(record)
    printed = _printed_provenance(card)
    written = _written_provenance(card)
    legacy = card.verdict_source == "legacy"

    assert (printed is not None) is legacy, (
        f"{_TERMINAL} {'omits' if printed is None else 'prints'} a Record line for {record}, "
        f"whose card is {card.verdict_source!r}; it states one for a legacy record and no other"
    )
    assert (written is not None) is legacy, (
        f"{_REPORT} {'omits' if written is None else 'writes'} a Record line for {record}, "
        f"whose card is {card.verdict_source!r}; it states one for a legacy record and no other"
    )
    assert printed == written, _disagrees(
        _TERMINAL, record, "record provenance", printed, written, against=_REPORT
    )


@pytest.mark.parametrize("record", list(_FENCE_RECORDS))
def test_no_surface_invents_a_verdict_the_run_did_not_record(record):
    payload, _ = _FENCE_RECORDS[record]
    card = _fence_card(record)
    recorded = payload["verdict"]

    assert card.verdict == recorded, _disagrees(
        "the card", record, "verdict", card.verdict, recorded
    )
    assert card.row("setup").status == recorded, _disagrees(
        "the card", record, "setup", card.row("setup").status, recorded
    )
    assert _printed_rows(card)["setup"].status == recorded, _disagrees(
        _TERMINAL, record, "setup", _printed_rows(card)["setup"].status, recorded
    )
    served = _report_cells(card)[ROW_LABELS["setup"]][0]
    assert served == recorded, _disagrees(_REPORT, record, "setup", served, recorded)


def test_every_ci_verdict_the_record_can_hold_has_a_word_a_reader_can_read():
    """No comparison verdict may reach a surface spelled the way the record spells it.

    `ci_row` spells the record's word for a reader through `_CI_STATUS_WORD`
    and falls back to the raw word for anything the map does not name. That
    fallback is right for the words a reader already reads (`met`, `partial`)
    and wrong for any word carrying an underscore, which would reach all three
    surfaces as `not_met` did before it was given an entry. This fails at the
    map, where the next verdict is added, rather than in a screenshot of the
    report weeks later.
    """

    for verdict in get_args(AttainmentVerdict):
        assert verdict in _CI_STATUS_WORD or "_" not in verdict, (
            f"AttainmentVerdict {verdict!r} has no reader's spelling: give it one in "
            "_CI_STATUS_WORD (sag/result_card/rows.py) or every surface prints the "
            "record's own spelling"
        )
        assert verdict in _CI_TONE, (
            f"AttainmentVerdict {verdict!r} has no tone: give it one in _CI_TONE "
            "(sag/result_card/rows.py) or the block colours it as attention by default"
        )

    unknown = set(_CI_STATUS_WORD) - set(get_args(AttainmentVerdict))
    assert (
        not unknown
    ), f"_CI_STATUS_WORD spells {sorted(unknown)}, which no AttainmentVerdict can hold"


# -- one reader, four surfaces ---------------------------------------------


def test_no_surface_spells_a_comparison_it_could_not_make_the_way_the_record_does():
    """`invalid` is the record's word; `not scored` is what happened.

    135 of the 342 evaluated comparisons under `logs/` are `invalid`, and on a
    screen the bare word reads as a judgment on the project — a reader sees a
    red row beside a green build and concludes the tests are broken. What the
    record means is that it reached a named CI job and could not put a score on
    it. One spelling in `_CI_STATUS_WORD` gives the terminal, the report and
    the web payload the same word; this asserts all three read it, so none can
    keep its own copy.
    """

    card = _fence_card("a comparison the run could not make")

    assert card.row("ci").status == NOT_SCORED
    assert _printed_rows(card)["ci"].status == NOT_SCORED
    assert _report_cells(card)[ROW_LABELS["ci"]][0] == NOT_SCORED

    # And it says it once. Every `invalid` record under `logs/` carries no
    # score, so a headline reporting the missing score beside the status word
    # states the same absence twice in one row.
    assert card.row("ci").headline == NOT_SCORED
    assert _printed_rows(card)["ci"].said == ('cell "maven-compile (ubuntu-latest, JDK-8)"',)

    block = _fence_block(card).lower()
    written = "\n".join(render_result_card_markdown(card)).lower()
    assert "invalid" not in block, "the block prints the record's own word"
    assert "invalid" not in written, "the report prints the record's own word"


def test_a_comparison_that_happened_does_not_read_as_one_that_did_not():
    """Two different facts had one phrase, and the phrase was wrong for one.

    `not compared` is true of a run with no CI job to compare against. It was
    also printed over a comparison that named a repository, a commit, a CI job
    and a command, and listed its findings under a heading reading WHAT WAS
    COMPARED. The rail inherited the collision and had to stop flagging the
    cell at all, because one word covered 269 of 373 archived rows.
    """

    made = _fence_card("a comparison the run could not make").row("ci")
    unmatched = _fence_card("a clean run").row("ci")

    assert made.status != unmatched.status
    assert made.status == NOT_SCORED
    assert unmatched.status == NOT_COMPARED
    assert made.detail and "cell " in made.detail, "it names the CI job it reached"



def test_a_reason_without_a_sentence_reaches_a_reader_once():
    """An unglossed code is one fact, printed once, on every surface.

    `_first_reason` used to render `f"{gloss(text)} ({text})"`, and `gloss`
    falls back to the code, so a code with no sentence arrived as
    `TEST_EXECUTION_NOT_COMPLETE (TEST_EXECUTION_NOT_COMPLETE)` — the same word
    twice, which reads as two different facts. A glossed code still carries its
    code, because the code is the handle a reader quotes.
    """

    unglossed = "A_CODE_THAT_SHIPPED_BEFORE_ITS_SENTENCE"
    assert unglossed not in REASON_GLOSS

    card = build_result_card(
        snapshot_dict(
            ci_comparison=dict(
                snapshot_dict()["ci_comparison"], status="unavailable", reasons=[unglossed]
            )
        ),
        termination=_DELIVERED,
    )
    assert card.row("ci").reason == unglossed
    assert _printed_rows(card)["ci"].said == (unglossed,)
    assert _report_cells(card)[ROW_LABELS["ci"]][1] == unglossed

    glossed = build_result_card(
        snapshot_dict(
            ci_comparison=dict(
                snapshot_dict()["ci_comparison"],
                status="unavailable",
                reasons=["official_ci_cell_not_matched"],
            )
        ),
        termination=_DELIVERED,
    )
    assert glossed.row("ci").reason == (
        "no CI job on this commit matches the run's JDK and OS (official_ci_cell_not_matched)"
    )


def test_the_run_counts_a_card_states_come_from_one_reader():
    """Four surfaces, one fold of one ledger, handed over as one group.

    The counts are named `read_run_counts` answers with, and every card site
    splats that answer whole. When two sites folded the ledger themselves and
    one of them learned to bill tokens, a single run reported 117068 tokens on
    the terminal and nothing at all through the web API — a contradiction a
    reader could see by opening both.
    """
    import inspect as inspection
    from pathlib import Path

    from sag.result_card.run_evidence import RUN_COUNT_KEYS, read_run_counts

    kafka = Path(__file__).parent / "fixtures" / "trajectory" / "kafka-d2r3"
    counts = read_run_counts(kafka)

    assert set(counts) == set(RUN_COUNT_KEYS)
    accepted = set(inspection.signature(build_result_card).parameters)
    assert set(counts) <= accepted

    card = build_result_card(snapshot_dict(), **counts)
    assert card.stats.turns == 24
    assert card.stats.tokens_in and card.stats.tokens_out
    assert counts["token_usage"], "the kafka session bills tokens; the reader must carry them"


def test_no_surface_names_the_run_counts_one_at_a_time():
    """The keywords may only reach `build_result_card` through the shared group.

    A site that names them one at a time can name four and forget the fifth,
    which is exactly how the surfaces came to disagree. Splatting one mapping
    makes that impossible rather than merely unlikely, so naming any of these
    keywords outside the reader that produces them is the defect itself.
    """
    from pathlib import Path

    from sag.result_card.run_evidence import RUN_COUNT_KEYS

    # Every module that builds a card FROM A RECORDED RUN. `build.py` declares
    # the keywords, and `web/demo_data.py` invents a run rather than reading
    # one, so neither has a ledger to disagree with anyone about.
    sites = (
        "src/sag/main.py",
        "src/sag/web/session_registry.py",
        "src/sag/tools/report_tool.py",
    )
    offenders: dict[str, list[str]] = {}
    for name in sites:
        source = Path(name).read_text(encoding="utf-8")
        named = [key for key in RUN_COUNT_KEYS if f"{key}=" in source]
        if named:
            offenders[name] = named

    assert offenders == {}, (
        "these files name a run count by keyword instead of splatting "
        f"read_run_counts(...): {offenders}"
    )
