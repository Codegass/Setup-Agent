import json

import pytest

from sag.agent.receipt_test_rows import testcase_execution_id as _testcase_execution_id
from sag.agent.verdict_finalizer import (
    BuildEvidenceSnapshot,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
)
from sag.main import _render_setup_cli_result
from sag.tools.report_metrics import (
    LegacyReportMetricsV1,
    MetricsContractError,
    assemble_report_metrics,
    build_evidence_layer_projection,
    read_report_metrics,
)
from sag.web.models import TestSummary


def _count(executed, passed, failed=0, errors=0, skipped=0):
    return {
        "executed": executed,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }


def _snapshot(
    *,
    verdict="partial",
    receipt_scoped=True,
    conflicts=(),
    auxiliary=None,
    stale=None,
):
    return RunVerdictSnapshot(
        run_id="metrics-v2-run",
        finalized_at="2026-08-08T12:00:00Z",
        verdict=verdict,
        conflicts=tuple(conflicts),
        build_evidence=BuildEvidenceSnapshot(
            observed=True,
            green=True,
            judgment="success",
            source="physical",
        ),
        test_stats=SnapshotTestStats(
            unique=SnapshotTestCounts(executed=2, passed=2),
            raw=SnapshotTestCounts(executed=2, passed=2),
            judgment="success",
            receipt_scoped=receipt_scoped or None,
            auxiliary_test_stats=auxiliary,
            stale_test_reports=stale,
        ),
    )


def _run_pin():
    return {
        "run_id": "metrics-v2-run",
        "target_repo_sha": "a" * 40,
        "sag_git_sha": "b" * 40,
        "prompt_bundle_sha256": "c" * 64,
        "container_image_digest": "sha256:" + "d" * 64,
        "thinking_model": "weak-thinker",
        "action_model": "weak-actor",
        "sanitized_config": {"max_iterations": 40},
        "feature_flags": {"native_loop": True},
        "random_seed_or_null": None,
        "dependency_cache_state": "cold",
        "host_arch": "arm64",
        "run_order_index": 7,
    }


def _metrics(snapshot, **overrides):
    inputs = {
        "snapshot": snapshot.model_dump(mode="json"),
        "build_evidence": {},
        "test_analysis": {},
        "conflicts": list(snapshot.conflicts),
        "evidence_refs": list(snapshot.input_refs),
        "generated_at": "2026-08-08T12:00:00Z",
        "run_pin": _run_pin(),
        "persistence": {"receipts_persisted": 1, "terminal_receipts_unpersisted": 0},
        "control": {
            "terminal_refusal_recurrences": 0,
            "unsettled_jobs": 0,
            "cleanup_escalations": 0,
            "midrun_human_approvals": 0,
        },
    }
    inputs.update(overrides)
    return assemble_report_metrics(**inputs)


def _receipt(receipt_id, sequence, rows, *, target="a" * 40, status="complete"):
    report_path = "/workspace/project/target/surefire-reports/TEST-a.xml"
    report_sha256 = "e" * 64
    sealed = []
    for index, row in enumerate(rows, 1):
        sealed_row = {
            "run_id": "metrics-v2-run",
            "receipt_id": receipt_id,
            "execution_index": sequence,
            "execution_ordinal": index,
            "target_sha": target,
            "domain_id": "/workspace/project",
            "module_coordinate": row.get("module", "."),
            "framework": "junit-xml",
            "owner": row.get("owner", "com.acme.SharedTest"),
            "test_name": row.get("name", "roundTrip"),
            "parameter_id": row.get("parameter"),
            "outcome": row["outcome"],
            "report_path": row.get(
                "path",
                report_path,
            ),
            "report_sha256": report_sha256,
            "disposition": "claimed",
            "qualifying_invocation": True,
        }
        sealed_row["execution_id"] = _testcase_execution_id(sealed_row)
        sealed.append(sealed_row)
    return {
        "schema_version": 2,
        "run_id": "metrics-v2-run",
        "receipt_id": receipt_id,
        "target_sha": target,
        "domain_id": "/workspace/project",
        "working_directory": "/workspace/project",
        "report_delta": {
            "new": [
                {
                    "path": report_path,
                    "sha256": report_sha256,
                }
            ],
            "changed": [],
        },
        "testcase_execution_rows": {
            "schema_version": 2,
            "status": status,
            "report_count": 1,
            "rows": sealed,
        },
    }


def test_forward_writer_emits_only_v2_layers_and_never_flat_aliases():
    metrics = _metrics(_snapshot())

    assert metrics["schema_version"] == 2
    assert metrics["identity_version"] == "module-qualified-v1"
    assert "version" not in metrics
    assert "test" not in metrics
    assert "build" not in metrics
    encoded = json.dumps(metrics, sort_keys=True)
    for forbidden in ('"total"', '"unique_total"', '"raw_executions"'):
        assert forbidden not in encoded

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert claimed["receipt_executions"] == {
        **_count(2, 2),
        "availability": "available",
        "basis": "receipt-scoped report rows",
    }


def test_exact_receipt_rows_close_subject_case_and_retry_grains_without_collapsing_them():
    receipts = [
        _receipt(
            "inv-maven-test-0001",
            1,
            [{"module": "alpha", "parameter": "size=1", "outcome": "failed"}],
        ),
        _receipt(
            "inv-maven-test-0002",
            2,
            [
                {"module": "alpha", "parameter": "size=1", "outcome": "passed"},
                {"module": "alpha", "parameter": "size=2", "outcome": "skipped"},
                {
                    "module": "beta",
                    "owner": "com.acme.OtherTest",
                    "parameter": None,
                    "outcome": "passed",
                },
            ],
        ),
    ]

    metrics = _metrics(
        _snapshot(),
        receipt_records=receipts,
        persistence={
            "receipts_expected": 2,
            "receipts_persisted": 2,
            "terminal_receipts_unpersisted": 0,
        },
    )

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"] == {
        **_count(2, 2),
        "availability": "available",
        "basis": "latest module-qualified subjects",
    }
    assert claimed["latest_cases"] == {
        **_count(3, 2, skipped=1),
        "availability": "available",
        "basis": "latest parameter-aware cases",
    }
    assert claimed["receipt_executions"] == {
        **_count(4, 2, failed=1, skipped=1),
        "availability": "available",
        "basis": "module-qualified receipt execution rows",
    }
    assert metrics["tests"]["retried_cases"] == 1
    assert metrics["tests"]["flaky_cases"] == 1


def test_multiple_rows_in_one_invocation_are_one_worst_attempt_not_retries():
    first = _receipt(
        "inv-maven-test-0001",
        1,
        [
            {"module": "alpha", "parameter": "same", "outcome": "passed"},
            {"module": "alpha", "parameter": "same", "outcome": "error"},
        ],
    )
    second = _receipt(
        "inv-maven-test-0002",
        2,
        [{"module": "alpha", "parameter": "same", "outcome": "passed"}],
    )

    only_first = _metrics(_snapshot(), receipt_records=[first])
    assert only_first["tests"]["claimed"]["latest_cases"]["errors"] == 1
    assert only_first["tests"]["claimed"]["receipt_executions"]["executed"] == 2
    assert only_first["tests"]["retried_cases"] == 0

    retried = _metrics(_snapshot(), receipt_records=[first, second])
    assert retried["tests"]["claimed"]["latest_cases"]["passed"] == 1
    assert retried["tests"]["claimed"]["receipt_executions"]["executed"] == 3
    assert retried["tests"]["retried_cases"] == 1
    assert retried["tests"]["flaky_cases"] == 1


def test_an_incomplete_current_receipt_refuses_the_entire_identity_rollup():
    complete = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "passed"}],
    )
    incomplete = _receipt("inv-maven-test-0002", 2, [], status="unavailable")

    metrics = _metrics(_snapshot(), receipt_records=[complete, incomplete])

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert claimed["receipt_executions"]["executed"] is None
    assert (
        claimed["receipt_executions"]["reason"] == "current receipt testcase rows were unavailable"
    )
    assert metrics["tests"]["unattributed_observations"]["executed"] is None
    assert "lacked complete" in metrics["tests"]["unattributed_observations"]["reason"]


def test_a_row_not_bound_to_the_receipt_report_delta_cannot_be_claimed():
    receipt = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "passed"}],
    )
    receipt["testcase_execution_rows"]["rows"][0]["report_sha256"] = "f" * 64

    metrics = _metrics(_snapshot(), receipt_records=[receipt])

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] is None


def test_receipt_report_count_or_domain_gaps_make_even_an_empty_envelope_unavailable():
    receipt = _receipt("inv-maven-test-0001", 1, [])
    receipt.pop("domain_id")
    receipt["testcase_execution_rows"]["report_count"] = 0

    metrics = _metrics(_snapshot(), receipt_records=[receipt])

    assert metrics["tests"]["claimed"]["latest_subjects"]["executed"] is None
    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] is None


def test_other_target_rows_are_stale_observations_never_current_claims():
    current = _receipt(
        "inv-maven-test-0002",
        2,
        [{"module": "alpha", "outcome": "passed"}],
    )
    old = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "failed"}],
        target="b" * 40,
    )

    metrics = _metrics(_snapshot(), receipt_records=[old, current])

    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] == 1
    stale = metrics["tests"]["stale_observations"]
    assert stale["executed"] == 1
    assert stale["failed"] == 1
    assert stale["reason_counts"] == {"target_sha_mismatch": 1}


def test_a_stale_only_receipt_cannot_reenter_the_current_receipt_execution_count():
    old = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "failed"}],
        target="b" * 40,
    )

    metrics = _metrics(_snapshot(), receipt_records=[old])

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert claimed["receipt_executions"] == {
        **_count(0, 0),
        "availability": "available",
        "basis": "no receipt rows matched the current target sha",
    }
    assert metrics["tests"]["stale_observations"]["executed"] == 1


def test_same_sha_receipt_from_another_run_is_not_a_retry_or_current_claim():
    current = _receipt(
        "inv-maven-test-0002",
        2,
        [{"module": "alpha", "outcome": "passed"}],
    )
    previous_run = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "failed"}],
    )
    previous_run["run_id"] = "older-run"
    for row in previous_run["testcase_execution_rows"]["rows"]:
        row["run_id"] = "older-run"
        row["execution_id"] = _testcase_execution_id(row)

    metrics = _metrics(_snapshot(), receipt_records=[previous_run, current])

    claimed = metrics["tests"]["claimed"]
    assert claimed["receipt_executions"]["executed"] == 1
    assert metrics["tests"]["retried_cases"] == 0
    assert metrics["tests"]["flaky_cases"] == 0


def test_same_sha_receipt_from_only_another_run_cannot_promote_snapshot_counts():
    previous_run = _receipt(
        "inv-maven-test-0001",
        1,
        [{"module": "alpha", "outcome": "failed"}],
    )
    previous_run["run_id"] = "older-run"
    for row in previous_run["testcase_execution_rows"]["rows"]:
        row["run_id"] = "older-run"
        row["execution_id"] = _testcase_execution_id(row)

    metrics = _metrics(_snapshot(), receipt_records=[previous_run])

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert claimed["receipt_executions"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] is None
    assert (
        metrics["tests"]["unattributed_observations"]["reason"]
        == "receipt rows belonged to a different run epoch"
    )


def test_current_v2_receipt_without_delta_or_envelope_cannot_promote_snapshot_counts():
    receipt = {
        "schema_version": 2,
        "receipt_id": "inv-maven-test-empty-0001",
        "run_id": "metrics-v2-run",
        "target_sha": "a" * 40,
        "domain_id": "/workspace/project",
        "report_delta": {"new": [], "changed": []},
    }

    metrics = _metrics(_snapshot(), receipt_records=[receipt])

    claimed = metrics["tests"]["claimed"]
    assert claimed["latest_subjects"]["executed"] is None
    assert claimed["latest_cases"]["executed"] is None
    assert claimed["receipt_executions"]["executed"] is None
    assert (
        claimed["receipt_executions"]["reason"] == "current receipt testcase rows were unavailable"
    )
    assert metrics["tests"]["unattributed_observations"]["executed"] is None


def test_unscoped_aggregate_is_unattributed_observation_not_a_claim():
    metrics = _metrics(
        _snapshot(receipt_scoped=False),
        persistence={
            "receipts_persisted": 0,
            "terminal_receipts_unpersisted": 0,
        },
    )

    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] == 2
    assert metrics["tests"]["unattributed_observations"]["reason_counts"] == {
        "receipt_scope_unavailable": 2
    }


def test_ignite_shape_keeps_auxiliary_failures_visible_and_non_verdict_bearing():
    auxiliary = _count(2887, 267, failed=28, errors=2481, skipped=111)
    metrics = _metrics(_snapshot(verdict="success", auxiliary=auxiliary))
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )

    text, exit_code = _render_setup_cli_result(
        _snapshot(verdict="success", auxiliary=auxiliary),
        termination,
        "ignite",
        metrics_v2=metrics,
    )

    assert exit_code == 0
    assert "Claimed latest subjects: unavailable" in text
    assert "Receipt executions: 2/2 passed" in text
    assert "Quarantined observations (not verdict-bearing): 267/2887 passed" in text
    assert "28 failed, 2481 errors, 111 skipped" in text
    assert "Tests: 2 unique" not in text


def test_stale_files_stay_visible_without_inventing_observation_outcomes():
    metrics = _metrics(_snapshot(stale=["old-a.xml", "old-b.xml"]))
    stale = metrics["tests"]["stale_observations"]

    assert stale["executed"] is None
    assert stale["report_file_count"] == 2
    assert stale["reason_counts"] is None


def test_success_with_degraded_transport_is_a_writer_contract_error():
    snapshot = _snapshot(
        verdict="success",
        conflicts=("job_terminal_unpersisted:job-1",),
    )
    with pytest.raises(MetricsContractError, match="success.*(?:degraded|failed)"):
        _metrics(
            snapshot,
            persistence={"receipts_persisted": 0, "terminal_receipts_unpersisted": 1},
        )


def test_success_with_unavailable_transport_is_also_a_writer_contract_error():
    with pytest.raises(MetricsContractError, match="success.*unavailable"):
        _metrics(_snapshot(verdict="success"), persistence={})


def test_missing_expected_receipt_degrades_transport_and_caps_success():
    partial = _metrics(
        _snapshot(verdict="partial"),
        persistence={
            "receipts_expected": 2,
            "receipts_persisted": 1,
            "terminal_receipts_unpersisted": 0,
        },
    )
    assert partial["evidence"]["integrity"] == "degraded"

    with pytest.raises(MetricsContractError, match="success.*degraded"):
        _metrics(
            _snapshot(verdict="success"),
            persistence={
                "receipts_expected": 2,
                "receipts_persisted": 1,
                "terminal_receipts_unpersisted": 0,
            },
        )


def test_run_evidence_control_and_coverage_pins_are_explicit():
    metrics = _metrics(_snapshot())

    assert metrics["run"] == {
        "run_id": "metrics-v2-run",
        "target_sha": "a" * 40,
        "sag_sha": "b" * 40,
        "prompt_hash": "c" * 64,
        "control_bundle_hash": metrics["run"]["control_bundle_hash"],
        "image_digest": "sha256:" + "d" * 64,
        "model_pin": "thinking=weak-thinker;action=weak-actor",
        "run_order_index": 7,
        "pin_status": "complete",
        "missing_pins": [],
    }
    assert len(metrics["run"]["control_bundle_hash"]) == 64
    assert metrics["evidence"] == {
        "integrity": "complete",
        "receipts_expected": 1,
        "receipts_persisted": 1,
        "terminal_receipts_unpersisted": 0,
        "conflict_count": 0,
    }
    assert metrics["coverage"] == {
        "domains_discovered": 1,
        "domains_attempted": 1,
        "domains_terminal": 1,
        "domains_with_claimed_tests": 1,
    }
    assert metrics["control"]["midrun_human_approvals"] == 0


def test_legacy_reader_is_a_different_type_and_never_synthesizes_v2():
    legacy = read_report_metrics(
        {
            "version": 1,
            "build": {"state": "success"},
            "test": {"total": 2, "passed": 2},
        }
    )

    assert isinstance(legacy, LegacyReportMetricsV1)
    assert legacy.payload["test"]["total"] == 2
    assert not hasattr(legacy, "tests")


def test_v2_reader_rejects_partial_or_flat_alias_shapes():
    assert (
        read_report_metrics(
            {
                "schema_version": 2,
                "identity_version": "module-qualified-v1",
                "test": {"total": 2},
            }
        )
        is None
    )


def test_web_model_serializes_the_non_verdict_bearing_evidence_layers():
    metrics = _metrics(_snapshot(auxiliary=_count(2887, 267, failed=28, errors=2481, skipped=111)))

    summary = TestSummary(evidence_layers=metrics)
    payload = summary.model_dump(mode="json", by_alias=True)

    assert payload["evidenceLayers"]["schemaVersion"] == 2
    assert payload["evidenceLayers"]["tests"]["quarantinedObservations"]["errors"] == 2481
    assert payload["evidenceLayers"]["evidence"]["integrity"] == "complete"


def test_display_fallback_is_typed_and_cannot_be_mistaken_for_a_v2_artifact():
    projection = build_evidence_layer_projection(
        snapshot=_snapshot(receipt_scoped=False).model_dump(mode="json")
    )

    payload = TestSummary(evidence_layers=projection).model_dump(mode="json", by_alias=True)

    assert payload["evidenceLayers"]["projectionStatus"] == "metrics-v2-artifact-unavailable"
    assert "schemaVersion" not in payload["evidenceLayers"]
    assert payload["evidenceLayers"]["tests"]["unattributedObservations"]["executed"] == 2
    assert payload["evidenceLayers"]["evidence"]["integrity"] == "unavailable"


def test_preaggregated_identity_counts_are_rejected_without_row_level_proof():
    zero_observations = {
        **_count(0, 0),
        "report_file_count": 0,
        "reason_counts": {},
    }
    explicit = {
        "claimed": {
            "latest_subjects": _count(2, 2),
            "latest_cases": _count(3, 3),
            "receipt_executions": _count(4, 4),
        },
        "quarantined_observations": zero_observations,
        "unattributed_observations": zero_observations,
        "stale_observations": zero_observations,
        "retried_cases": 1,
        "flaky_cases": 0,
    }

    with pytest.raises(MetricsContractError, match="not an identity proof"):
        _metrics(
            _snapshot(),
            test_analysis={"metrics_v2_tests": explicit},
        )
