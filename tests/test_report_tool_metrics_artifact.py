import base64
import hashlib
import json
import shlex
from types import SimpleNamespace

import pytest

from sag.agent.control_events import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    RunPin,
    canonical_json,
)
from sag.agent.evidence_records import (
    frame_json_record_stream,
    frame_named_json_record_stream,
)
from sag.agent.invocation_receipts import build_receipt
from sag.agent.receipt_test_rows import testcase_execution_id as _testcase_execution_id
from sag.main import _read_metrics_v2_for_cli
from sag.tools.report_metrics import (
    METRICS_PATH,
    REPORT_METRICS_LOGICAL_ARTIFACT_ID,
    _receipt_row_projection,
    assemble_report_metrics,
    read_live_report_metrics,
    validate_report_metrics_v2,
)
from sag.tools.report_tool import ReportTool
from sag.web.session_registry import _read_report_metrics as _read_web_report_metrics


class CapturingOrchestrator:
    def __init__(self):
        self.writes = {}
        # ``read_container_text(..., exact_bytes=True)`` treats an in-memory
        # ``files`` map as a lossless container transport.
        self.files = self.writes

    def execute_command(self, command, **kwargs):
        if command.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in command:
            records = (
                [(METRICS_PATH.rsplit("/", 1)[-1], self.writes[METRICS_PATH])]
                if METRICS_PATH in self.writes
                else []
            )
            return {"exit_code": 0, "output": frame_named_json_record_stream(records)}
        if command.startswith(f"cat {METRICS_PATH} "):
            if METRICS_PATH in self.writes:
                return {"exit_code": 0, "output": self.writes[METRICS_PATH]}
            return {"exit_code": 1, "output": ""}
        tokens = shlex.split(command)
        if tokens[:3] == ["mkdir", "-p", "--"]:
            return {"exit_code": 0, "output": ""}
        if len(tokens) == 3 and tokens[:2] == [":", ">"]:
            self.writes[tokens[2]] = ""
        elif len(tokens) == 5 and tokens[:2] == ["printf", "%s"] and tokens[3] == ">>":
            self.writes[tokens[4]] = self.writes.get(tokens[4], "") + tokens[2]
        elif tokens[:2] == ["base64", "--decode"] and tokens[-2:-1] == [">"]:
            self.writes[tokens[-1]] = base64.b64decode(self.writes.get(tokens[2], "")).decode()
        elif (
            tokens[:2] == ["python3", "-c"]
            and "hashlib.sha256" in tokens[2]
            and "fcntl.flock" not in tokens[2]
        ):
            payload = self.writes.get(tokens[3], "").encode()
            valid = (
                len(payload) == int(tokens[4]) and hashlib.sha256(payload).hexdigest() == tokens[5]
            )
            return {"exit_code": 0 if valid else 1, "output": ""}
        elif tokens[:2] == ["python3", "-c"] and "json.load" in tokens[2]:
            json.loads(self.writes[tokens[3]])
        elif tokens[:2] == ["python3", "-c"] and "SAG_QUARANTINE_CONFLICT" in tokens[2]:
            target, rejected, _lock_path, expected_bytes, expected_sha = tokens[3:8]
            current = self.writes.get(target)
            if current is None:
                return {"exit_code": 75, "output": "SAG_QUARANTINE_ABSENT\n"}
            current_bytes = current.encode()
            if (
                len(current_bytes) != int(expected_bytes)
                or hashlib.sha256(current_bytes).hexdigest() != expected_sha
            ):
                return {"exit_code": 75, "output": "SAG_QUARANTINE_CONFLICT\n"}
            self.writes[rejected] = self.writes.pop(target)
            return {"exit_code": 0, "output": ""}
        elif tokens[:2] == ["python3", "-c"] and "fcntl.flock" in tokens[2]:
            target, candidate, _lock_path, expected, expected_bytes, expected_sha = tokens[3:9]
            candidate_bytes = self.writes.get(candidate, "").encode()
            if (
                len(candidate_bytes) != int(expected_bytes)
                or hashlib.sha256(candidate_bytes).hexdigest() != expected_sha
            ):
                return {"exit_code": 76, "output": "SAG_CAS_CANDIDATE_INVALID\n"}
            current = self.writes.get(target)
            actual = (
                "absent"
                if current is None
                else "sha256:" + hashlib.sha256(current.encode()).hexdigest()
            )
            if actual != expected:
                return {"exit_code": 75, "output": "SAG_CAS_CONFLICT\n"}
            self.writes[target] = self.writes.pop(candidate)
        elif tokens[:3] == ["mv", "-f", "--"]:
            self.writes[tokens[4]] = self.writes.pop(tokens[3])
        elif tokens[:2] == ["rm", "-f"]:
            targets = tokens[3:] if tokens[2:3] == ["--"] else tokens[2:]
            for target in targets:
                self.writes.pop(target, None)
        return {"exit_code": 0, "output": ""}


class ProbeOrchestrator:
    def __init__(self, *, receipts="", obligations="", control="", fail=False):
        self.receipts = frame_json_record_stream(receipts.splitlines() if receipts else [])
        self.obligations = frame_json_record_stream(obligations.splitlines() if obligations else [])
        self.control = control
        self.fail = fail
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if self.fail:
            return {"exit_code": -1, "dispatch_status": "failed", "output": ""}
        if "invocation_receipts" in command:
            return {"exit_code": 0, "output": self.receipts}
        if "job_obligations" in command:
            return {"exit_code": 0, "output": self.obligations}
        if "control_events.jsonl" in command:
            return {"exit_code": 0, "output": self.control}
        return {"exit_code": 0, "output": ""}


class FailingReceiptProbeOrchestrator(CapturingOrchestrator):
    def execute_command(self, command, **kwargs):
        if "invocation_receipts" in command:
            return {"exit_code": -1, "dispatch_status": "failed", "output": ""}
        return super().execute_command(command, **kwargs)


class IdentityMetricsOrchestrator(CapturingOrchestrator):
    def __init__(self, receipt):
        super().__init__()
        self.receipt = receipt
        self.receipt_raw = json.dumps(receipt, sort_keys=True)
        self.run_pin_raw = canonical_json(
            RunPin(
                run_id="run-pytest",
                target_repo_sha="a" * 40,
                container_image_digest="sha256:" + "b" * 64,
                sag_git_sha="c" * 40,
                thinking_model="think",
                action_model="act",
                sanitized_config={},
                prompt_bundle_sha256="d" * 64,
                feature_flags={},
                run_order_index=1,
                random_seed_or_null=None,
                dependency_cache_state="cold",
                host_arch="arm64",
            )
        )
        self.receipt_reads = 0

    def execute_command(self, command, **kwargs):
        if command.startswith("file=") and "run-pin.json" in command:
            return {
                "exit_code": 0,
                "output": frame_named_json_record_stream([("run-pin.json", self.run_pin_raw)]),
            }
        if "SAG_NAMED_JSON_RECORD_V1" in command and "invocation_receipts" in command:
            self.receipt_reads += 1
            return {
                "exit_code": 0,
                "output": frame_named_json_record_stream(
                    [(f"{self.receipt['receipt_id']}.json", self.receipt_raw)]
                ),
            }
        if "SAG_NAMED_JSON_RECORD_V1" in command and "job_obligations" in command:
            return {
                "exit_code": 0,
                "output": frame_named_json_record_stream([]),
            }
        if "run-pin.json" in command and command.startswith("cat "):
            return {
                "exit_code": 0,
                "output": self.run_pin_raw,
            }
        if "invocation_receipts" in command:
            self.receipt_reads += 1
            if self.receipt_reads > 1:
                return {"exit_code": -1, "dispatch_status": "failed", "output": ""}
            return {
                "exit_code": 0,
                "output": frame_json_record_stream([json.dumps(self.receipt)]),
            }
        if "job_obligations" in command or "control_events.jsonl" in command:
            return {
                "exit_code": 0,
                "output": (frame_json_record_stream([]) if "job_obligations" in command else ""),
            }
        return super().execute_command(command, **kwargs)


def test_persist_metrics_writes_json_artifact():
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    metrics = assemble_report_metrics(
        snapshot={},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )

    assert tool._persist_report_metrics(metrics) is True

    assert METRICS_PATH in orch.writes
    parsed = json.loads(orch.writes[METRICS_PATH])
    assert parsed == metrics


def test_analyze_abort_projects_unreached_build_and_test_as_not_attempted():
    metrics = assemble_report_metrics(
        snapshot={
            "verdict": "failed",
            "build_evidence": {"observed": False, "judgment": "unknown"},
            "test_stats": {
                "unique": {
                    "executed": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "raw": {
                    "executed": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
            },
            "phase_records": [
                {
                    "phase": "analyze",
                    "attempt_id": "analyze-1",
                    "termination": "aborted",
                    "outcome": "unknown",
                    "validated_outcome": "unknown",
                    "reason": "iteration budget exhausted",
                }
            ],
        },
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-20T09:38:28Z",
        close_reason="aborted",
    )

    assert metrics["outcome"]["build_state"] == "not_attempted"
    assert metrics["outcome"]["test_state"] == "not_attempted"
    assert metrics["coverage"]["domains_attempted"] is None
    for bucket in metrics["tests"]["claimed"].values():
        assert bucket["availability"] == "unavailable"
        assert bucket["reason"] == "tests were not run"
    assert metrics["tests"]["unattributed_observations"]["executed"] is None


def test_policy_skipped_phases_project_as_not_attempted():
    metrics = assemble_report_metrics(
        snapshot={
            "verdict": "partial",
            "build_evidence": {"observed": False, "judgment": "unknown"},
            "test_stats": {
                "unique": {"executed": 0},
                "raw": {"executed": 0},
            },
            "phase_records": [
                {"phase": "build", "termination": "skipped"},
                {"phase": "test", "termination": "skipped"},
            ],
        },
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-20T10:00:00Z",
    )

    assert metrics["outcome"]["build_state"] == "not_attempted"
    assert metrics["outcome"]["test_state"] == "not_attempted"
    assert metrics["tests"]["unattributed_observations"]["availability"] == "unavailable"


def test_metrics_writer_publishes_one_stable_mutable_head(
    bind_host_evidence_publication_authority,
):
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    first = assemble_report_metrics(
        snapshot={"verdict": "unknown"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    second = assemble_report_metrics(
        snapshot={"verdict": "partial"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:01:00Z",
    )

    assert tool._persist_report_metrics(first) is True
    first_head = bind_host_evidence_publication_authority.latest_head(
        REPORT_METRICS_LOGICAL_ARTIFACT_ID
    )
    assert first_head is not None
    assert first_head.record_id == REPORT_METRICS_LOGICAL_ARTIFACT_ID
    assert first_head.revision == 1

    assert tool._persist_report_metrics(second) is True
    second_head = bind_host_evidence_publication_authority.latest_head(
        REPORT_METRICS_LOGICAL_ARTIFACT_ID
    )
    assert second_head is not None
    assert second_head.record_id == REPORT_METRICS_LOGICAL_ARTIFACT_ID
    assert second_head.revision == 2
    assert second_head.previous_raw_sha256 == first_head.raw_sha256

    live = read_live_report_metrics(orch)
    assert live.complete is True
    assert live.conflict is None
    assert live.payload == second


def test_live_metrics_reader_rejects_rollback_delete_and_tamper():
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    first = assemble_report_metrics(
        snapshot={"verdict": "unknown"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    second = assemble_report_metrics(
        snapshot={"verdict": "partial"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:01:00Z",
    )
    assert tool._persist_report_metrics(first) is True
    first_raw = orch.writes[METRICS_PATH]
    assert tool._persist_report_metrics(second) is True

    orch.writes[METRICS_PATH] = first_raw
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"
    orch.writes[METRICS_PATH] = json.dumps(second, sort_keys=True) + " "
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"
    del orch.writes[METRICS_PATH]
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"


def test_live_metrics_reader_rejects_mirror_only_and_extra_host_head(
    bind_host_evidence_publication_authority,
):
    orch = CapturingOrchestrator()
    metrics = assemble_report_metrics(
        snapshot={},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    orch.writes[METRICS_PATH] = json.dumps(metrics, indent=2, sort_keys=True)
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"

    raw = json.dumps(metrics, indent=2, sort_keys=True).encode()
    bind_host_evidence_publication_authority.publish_revision(
        record_kind="report_metrics",
        record_id="unexpected-report-metrics",
        logical_artifact_id="unexpected-report-metrics",
        raw=raw,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"


def test_cli_and_web_v2_consumers_require_live_host_publication():
    orch = CapturingOrchestrator()
    metrics = assemble_report_metrics(
        snapshot={},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    orch.writes[METRICS_PATH] = json.dumps(metrics, indent=2, sort_keys=True)

    assert _read_metrics_v2_for_cli(orch) is None
    assert _read_web_report_metrics(orch) is None

    assert ReportTool(docker_orchestrator=orch)._persist_report_metrics(metrics) is True
    assert _read_metrics_v2_for_cli(orch) == metrics
    assert _read_web_report_metrics(orch) == metrics


def test_quarantine_revokes_preview_and_rejects_restored_old_bytes(
    bind_host_evidence_publication_authority,
):
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    preview = assemble_report_metrics(
        snapshot={"verdict": "partial"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    assert tool._persist_report_metrics(preview) is True
    published = bind_host_evidence_publication_authority.latest_head(
        REPORT_METRICS_LOGICAL_ARTIFACT_ID
    )
    assert published is not None
    old_raw = orch.writes[METRICS_PATH]

    assert tool._quarantine_report_metrics() is True

    tombstone = bind_host_evidence_publication_authority.latest_head(
        REPORT_METRICS_LOGICAL_ARTIFACT_ID
    )
    assert tombstone is not None
    assert tombstone.publication_state == "revoked"
    assert tombstone.revision == published.revision + 1
    assert tombstone.previous_raw_sha256 == published.raw_sha256
    assert METRICS_PATH not in orch.writes
    assert orch.writes[f"{METRICS_PATH}.rejected"] == old_raw
    absent = read_live_report_metrics(orch)
    assert absent.complete is True
    assert absent.payload is None

    orch.writes[METRICS_PATH] = old_raw
    assert read_live_report_metrics(orch).conflict == "publication_set_mismatch"


def test_metrics_writer_cas_conflict_never_washes_concurrent_tamper():
    class RacingOrchestrator(CapturingOrchestrator):
        race = False

        def execute_command(self, command, **kwargs):
            tokens = shlex.split(command)
            if (
                self.race
                and tokens[:2] == ["python3", "-c"]
                and "fcntl.flock" in tokens[2]
                and "SAG_QUARANTINE_CONFLICT" not in tokens[2]
            ):
                self.writes[METRICS_PATH] = '{"forged":true}'
                self.race = False
            return super().execute_command(command, **kwargs)

    orch = RacingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)
    first = assemble_report_metrics(
        snapshot={"verdict": "unknown"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )
    second = assemble_report_metrics(
        snapshot={"verdict": "partial"},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:01:00Z",
    )
    assert tool._persist_report_metrics(first) is True
    orch.race = True

    assert tool._persist_report_metrics(second) is False
    assert orch.writes[METRICS_PATH] == '{"forged":true}'
    assert read_live_report_metrics(orch).conflict == "record_schema_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "schema_version": 3},
        lambda value: {**value, "future_field": True},
        lambda value: {**value, "run": {**value["run"], "run_order_index": True}},
        lambda value: {**value, "outcome": {**value["outcome"], "verdict": "green"}},
    ],
    ids=["future-version", "extra-field", "malformed-run", "unknown-verdict"],
)
def test_strict_metrics_schema_rejects_future_extra_and_malformed(mutation):
    metrics = assemble_report_metrics(
        snapshot={},
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
    )

    with pytest.raises(ValueError):
        validate_report_metrics_v2(mutation(metrics))


def test_persist_metrics_no_orchestrator_is_safe():
    assert (
        ReportTool(docker_orchestrator=None)._persist_report_metrics({"schema_version": 2}) is False
    )


def test_forward_writer_refuses_to_overwrite_with_a_legacy_artifact():
    orch = CapturingOrchestrator()

    assert (
        ReportTool(docker_orchestrator=orch)._persist_report_metrics(
            {"version": 1, "test": {"total": 2}}
        )
        is False
    )

    assert METRICS_PATH not in orch.writes


def test_post_loop_finalizer_rewrites_the_authoritative_metrics_artifact():
    orch = CapturingOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)

    metrics = tool.finalize_metrics_v2(
        {
            "verdict": "partial",
            "finalized_at": "2026-08-08T12:00:00Z",
            "build_evidence": {"observed": False, "judgment": "unknown"},
            "test_stats": {},
            "conflicts": [],
            "input_refs": [],
        }
    )

    assert metrics["schema_version"] == 2
    assert json.loads(orch.writes[METRICS_PATH]) == metrics


def test_post_loop_finalizer_rehydrates_module_qualified_rows_from_durable_receipts(
    bind_host_evidence_publication_authority,
):
    receipt_id = "inv-maven-test-0001"
    report = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    row = {
        "run_id": "run-pytest",
        "receipt_id": receipt_id,
        "execution_index": 1,
        "execution_ordinal": 1,
        "target_sha": "a" * 40,
        "domain_id": "/workspace/proj",
        "module_coordinate": ".",
        "framework": "junit-xml",
        "owner": "com.acme.ExactTest",
        "test_name": "works",
        "parameter_id": None,
        "outcome": "passed",
        "report_path": report,
        "report_sha256": "e" * 64,
        "disposition": "claimed",
        "qualifying_invocation": True,
    }
    row["execution_id"] = _testcase_execution_id(row)
    receipt = build_receipt(
        receipt_id=receipt_id,
        run_id="run-pytest",
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={report: "e" * 64},
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        testcase_execution_rows={
            "schema_version": 2,
            "status": "complete",
            "report_count": 1,
            "rows": [row],
        },
    )
    orch = IdentityMetricsOrchestrator(receipt)
    # Every evidence_publication now requires the preceding evidence_store_bound
    # event: one run authorizes exactly one container store.
    bind_host_evidence_publication_authority.bind_store(orch)
    bind_host_evidence_publication_authority.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=orch.run_pin_raw.encode(),
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    bind_host_evidence_publication_authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id=receipt_id,
        raw=orch.receipt_raw.encode(),
    )
    tool = ReportTool(docker_orchestrator=orch)

    metrics = tool.finalize_metrics_v2(
        {
            "verdict": "partial",
            "finalized_at": "2026-08-08T12:00:00Z",
            "build_evidence": {"observed": True, "judgment": "success"},
            "test_stats": {
                "unique": {
                    "executed": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "raw": {
                    "executed": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "receipt_scoped": True,
            },
            "conflicts": [],
            "input_refs": [],
        }
    )

    assert metrics["tests"]["claimed"]["latest_subjects"]["executed"] == 1
    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] == 1
    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] == 1
    assert orch.receipt_reads == 1
    assert json.loads(orch.writes[METRICS_PATH]) == metrics


def test_build_receipt_before_test_receipt_does_not_hide_testcase_rows():
    run_id = "run-pytest"
    target_sha = "a" * 40
    report = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    report_sha = "e" * 64
    receipt_id = "inv-maven-test-0002"
    row = {
        "run_id": run_id,
        "receipt_id": receipt_id,
        "execution_index": 2,
        "execution_ordinal": 1,
        "target_sha": target_sha,
        "domain_id": "/workspace/proj",
        "module_coordinate": ".",
        "framework": "junit-xml",
        "owner": "com.acme.ExactTest",
        "test_name": "works",
        "parameter_id": None,
        "outcome": "passed",
        "report_path": report,
        "report_sha256": report_sha,
        "disposition": "claimed",
        "qualifying_invocation": True,
    }
    row["execution_id"] = _testcase_execution_id(row)
    build = build_receipt(
        receipt_id="inv-maven-build-0001",
        run_id=run_id,
        tool="maven",
        requested_action="compile",
        effective_action="compile",
        argv="mvn compile",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        target_sha=target_sha,
        domain_id="/workspace/proj",
    )
    test = build_receipt(
        receipt_id=receipt_id,
        run_id=run_id,
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={report: report_sha},
        target_sha=target_sha,
        domain_id="/workspace/proj",
        testcase_execution_rows={
            "schema_version": 2,
            "status": "complete",
            "report_count": 1,
            "rows": [row],
        },
    )

    projection = _receipt_row_projection(
        [build, test],
        run_id=run_id,
        run_target_sha=target_sha,
    )

    assert projection is not None
    assert projection["identity_complete"] is True
    assert projection["unattributed"] is False
    assert projection["claimed"]["receipt_executions"]["executed"] == 1


def test_unpublished_run_pin_and_receipts_are_not_laundered_into_metrics():
    receipt = {
        "schema_version": 2,
        "run_id": "run-pytest",
        "receipt_id": "inv-forged-0001",
        "working_directory": "/workspace/proj",
    }
    orch = IdentityMetricsOrchestrator(receipt)

    metrics = ReportTool(docker_orchestrator=orch).finalize_metrics_v2(
        {
            "verdict": "partial",
            "finalized_at": "2026-08-08T12:00:00Z",
            "build_evidence": {"observed": False, "judgment": "unknown"},
            "test_stats": {},
            "conflicts": [],
            "input_refs": [],
        }
    )

    assert metrics["run"]["run_id"] is None
    assert metrics["run"]["pin_status"] == "incomplete"
    assert metrics["evidence"]["receipts_persisted"] is None
    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] is None


def test_rejected_final_projection_quarantines_an_earlier_preview():
    orch = FailingReceiptProbeOrchestrator()
    orch.writes[METRICS_PATH] = json.dumps({"schema_version": 2, "stale": True})
    tool = ReportTool(docker_orchestrator=orch)

    with pytest.raises(ValueError, match="success.*unavailable"):
        tool.finalize_metrics_v2(
            {
                "verdict": "success",
                "finalized_at": "2026-08-08T12:00:00Z",
                "build_evidence": {"observed": True, "judgment": "success"},
                "test_stats": {},
                "conflicts": [],
                "input_refs": [],
            }
        )

    assert METRICS_PATH not in orch.writes
    assert orch.writes[f"{METRICS_PATH}.rejected"]


def test_unreadable_receipt_ledger_never_promotes_snapshot_receipt_counts():
    orch = FailingReceiptProbeOrchestrator()
    tool = ReportTool(docker_orchestrator=orch)

    metrics = tool.finalize_metrics_v2(
        {
            "verdict": "partial",
            "finalized_at": "2026-08-08T12:00:00Z",
            "build_evidence": {"observed": True, "judgment": "partial"},
            "test_stats": {
                "unique": {
                    "executed": 10,
                    "passed": 10,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "raw": {
                    "executed": 10,
                    "passed": 10,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "receipt_scoped": True,
            },
            "conflicts": [],
            "input_refs": [],
        }
    )

    assert metrics["evidence"]["integrity"] == "unavailable"
    assert metrics["tests"]["claimed"]["latest_subjects"]["executed"] is None
    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] is None
    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] is None


def test_forward_writer_refuses_a_partial_v2_shape_or_flat_alias():
    orch = CapturingOrchestrator()

    ReportTool(docker_orchestrator=orch)._persist_report_metrics(
        {
            "schema_version": 2,
            "identity_version": "module-qualified-v1",
            "test": {"total": 2},
        }
    )

    assert METRICS_PATH not in orch.writes


def test_persistence_surface_counts_receipts_and_missing_obligations_without_blending():
    receipts = "\n".join(
        json.dumps({"receipt_id": receipt_id}) for receipt_id in ("receipt-1", "receipt-2")
    )
    obligations = "\n".join(
        (
            json.dumps(
                {
                    "job_id": "settled",
                    "process_state": "terminal",
                    "settlement_state": "settled",
                    "settled_receipt_id": "receipt-1",
                }
            ),
            json.dumps(
                {
                    "job_id": "running",
                    "process_state": "running",
                    "settlement_state": "none",
                }
            ),
            json.dumps(
                {
                    "job_id": "lost-receipt",
                    "process_state": "terminal",
                    "settlement_state": "unpersisted",
                    "attempted_receipt_id": "receipt-3",
                }
            ),
        )
    )

    orch = ProbeOrchestrator(receipts=receipts, obligations=obligations)
    surface = ReportTool(docker_orchestrator=orch)._metrics_persistence_surface(
        receipt_records=[json.loads(line) for line in receipts.splitlines()],
        obligation_records=[json.loads(line) for line in obligations.splitlines()],
    )

    assert surface == {
        "receipts_expected": 4,
        "receipts_persisted": 2,
        "terminal_receipts_unpersisted": 1,
    }
    assert orch.commands == []


def test_failed_persistence_probe_is_unavailable_not_an_invented_zero():
    surface = ReportTool(
        docker_orchestrator=ProbeOrchestrator(fail=True)
    )._metrics_persistence_surface()

    assert surface == {
        "receipts_expected": None,
        "receipts_persisted": None,
        "terminal_receipts_unpersisted": None,
    }


def test_duplicate_key_receipt_makes_the_metrics_ledger_unavailable():
    duplicate = '{"receipt_id":"first","receipt_id":"second"}'

    surface = ReportTool(
        docker_orchestrator=ProbeOrchestrator(receipts=duplicate)
    )._metrics_persistence_surface()

    assert surface == {
        "receipts_expected": None,
        "receipts_persisted": None,
        "terminal_receipts_unpersisted": None,
    }


def test_persistence_surface_excludes_same_container_records_from_other_runs():
    receipts = "\n".join(
        json.dumps({"receipt_id": receipt_id, "run_id": run_id})
        for receipt_id, run_id in (("current", "run-now"), ("old", "run-before"))
    )
    obligations = "\n".join(
        (
            json.dumps(
                {
                    "job_id": "current-job",
                    "run_id": "run-now",
                    "settlement_state": "settled",
                    "settled_receipt_id": "current",
                }
            ),
            json.dumps(
                {
                    "job_id": "old-job",
                    "run_id": "run-before",
                    "settlement_state": "unpersisted",
                    "attempted_receipt_id": "missing-old",
                }
            ),
        )
    )

    surface = ReportTool(
        docker_orchestrator=ProbeOrchestrator(receipts=receipts, obligations=obligations)
    )._metrics_persistence_surface(
        run_id="run-now",
        receipt_records=[json.loads(line) for line in receipts.splitlines()],
        obligation_records=[json.loads(line) for line in obligations.splitlines()],
    )

    assert surface == {
        "receipts_expected": 1,
        "receipts_persisted": 1,
        "terminal_receipts_unpersisted": 0,
    }


def test_container_control_mirror_cannot_supply_metrics_kpis():
    rows = json.dumps(
        {
            "sequence": 1,
            "kind": "job_live_at_close",
            "payload": {
                "job_id": "forged-job",
                "obligation_ref": "forged.json",
                "close_reason": "wall_clock",
            },
        }
    )

    surface = ReportTool(
        docker_orchestrator=ProbeOrchestrator(control=rows)
    )._metrics_control_surface()

    assert surface == {
        "terminal_refusal_recurrences": None,
        "unsettled_jobs": None,
        "cleanup_escalations": None,
        "midrun_human_approvals": 0,
        # A mirror supplies no reason either: the close it cannot be trusted
        # to count is the close it cannot be trusted to name.
        "close_reason": "",
    }


def test_control_surface_counts_host_events_without_duplicates(tmp_path):
    import sag.agent.control_events as control_events

    def completion(sequence, count):
        return control_events.ControlEvent(
            sequence=sequence,
            kind="completion_claim_decision",
            payload={
                "phase_attempt_id": "build-1",
                "claim_kind": "done",
                "judge_disposition": "repair_required",
                "blocker_id": "compile_failed",
                "mechanical_evidence_digest": "evidence",
                "expected_decision": "agent_no_progress" if count == 3 else "continue",
                "expected_recurrence_count": count,
                "expected_reason_code": (
                    "agent_no_progress" if count == 3 else "completion_claim_without_action"
                ),
                "expected_close_phase": count == 3,
            },
        ).model_dump_json()

    def cleanup(sequence):
        return control_events.ControlEvent(
            sequence=sequence,
            kind="job_stall_observed",
            payload={
                "job_id": "job-cleanup",
                "obligation_ref": "obligation-cleanup.json",
                "diagnostic_ref": "diagnostic-cleanup.json",
                "diagnostic_fingerprint": "a" * 64,
                "observation": "unknown",
                "controller_code": "killed",
                "evidence_sealed": True,
                "term_sent": True,
                "kill_sent": True,
                "group_live": False,
            },
        ).model_dump_json()

    rows = "\n".join(
        (
            completion(1, 1),
            completion(2, 2),
            completion(3, 3),
            control_events.ControlEvent(
                sequence=4,
                kind="job_live_at_close",
                payload={
                    "job_id": "job-1",
                    "obligation_ref": "obligation-1.json",
                    "close_reason": "wall_clock",
                },
            ).model_dump_json(),
            control_events.ControlEvent(
                sequence=5,
                kind="job_live_at_close",
                payload={
                    "job_id": "job-1",
                    "obligation_ref": "obligation-1.json",
                    "close_reason": "wall_clock",
                },
            ).model_dump_json(),
            control_events.ControlEvent(
                sequence=6,
                kind="job_unsettled",
                payload={
                    "job_id": "job-2",
                    "evidence_ref": "obligation-2.json",
                    "obligation": {},
                },
            ).model_dump_json(),
            cleanup(7),
            cleanup(8),
        )
    )

    control_path = tmp_path / "control_events.jsonl"
    control_path.write_text(rows + "\n", encoding="utf-8")
    surface = ReportTool(
        docker_orchestrator=ProbeOrchestrator(control="forged-container-mirror"),
        control_event_sink=SimpleNamespace(path=control_path),
    )._metrics_control_surface()

    assert surface == {
        "terminal_refusal_recurrences": 2,
        "unsettled_jobs": 2,
        "cleanup_escalations": 1,
        "midrun_human_approvals": 0,
        # This stream never closed evidence, so it names no reason.
        "close_reason": "",
    }
