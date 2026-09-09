"""Offline D3R1 projections exercise the downstream gates without publishing history.

Receipt/assessment readers are isolated because these are archived excerpts,
not newly authorized receipts. The real completion summary, physical status,
phase observation and snapshot fold run unchanged. Original unique/raw report
counts come from a separately hash-verified projection, never the row samples.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_test_execution_completion import CALIBRATION_CASES, validator_for

from sag.agent.evidence_assessments import execution_faults, prerequisite_assessments
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ValidatorState, _inspect_test
from sag.agent.verdict_finalizer import VerdictFinalizer, validate_verdict_snapshot_v3

ARCHIVED_ROLLUPS = json.loads(
    (Path(__file__).parent / "fixtures/d3r1_remediation/archived_test_rollups.json").read_text()
)["cases"]


def _projected_validator(monkeypatch, case):
    receipt = {
        **case["receipt_fields"],
        "testcase_execution_rows": {
            "status": "partial",
            "rows": [row["record"] for row in case["row_samples"]],
        },
        "testcase_outcomes": {"nodes": [row["record"] for row in case["testcase_outcome_samples"]]},
    }
    assessments = [item["record"] for item in case["assessments"]]
    fragments = None
    if case["output"]["record_output_matches_receipt_hash"]:
        fragments = "\n".join(part["text"] for part in case["output"]["excerpts"])
    assessments += [
        item.payload()
        for item in (
            *execution_faults(receipt, fragments),
            *prerequisite_assessments(receipt, fragments),
        )
    ]
    # Diagnostic excerpts cannot certify a complete original output scan:
    # no bundle-complete marker is manufactured or historical record published.
    return validator_for(monkeypatch, [(receipt, case["contract"])], assessments)


def _archived_report_metrics(case):
    archived = ARCHIVED_ROLLUPS[case["id"]]
    assert archived["source"] == case["historical_result"]["source"]
    original = archived["test_stats"]
    metrics = {
        "valid": True,
        "discovered": original["discovered"],
        "receipt_scoped": original["receipt_scoped"],
        "flaky_count": original["flaky_count"],
        "report_files": [f"forensic:{archived['source']['path']}#local.test_stats"],
    }
    for field, ordinary, unique, raw in (
        ("executed", "total_tests", "unique_tests", "raw_total_tests"),
        ("passed", "passed_tests", "unique_passed_tests", "raw_passed_tests"),
        ("failed", "failed_tests", "unique_failed_tests", "raw_failed_tests"),
        ("errors", "error_tests", "unique_error_tests", "raw_error_tests"),
        ("skipped", "skipped_tests", "unique_skipped_tests", "raw_skipped_tests"),
    ):
        metrics[ordinary] = original["unique"][field]
        metrics[unique] = original["unique"][field]
        metrics[raw] = original["raw"][field]
    return metrics


@pytest.mark.parametrize("case", CALIBRATION_CASES, ids=lambda case: case["id"])
def test_archived_failure_survives_physical_gate_and_finalizer(monkeypatch, case):
    validator = _projected_validator(monkeypatch, case)
    monkeypatch.setattr(
        validator, "parse_test_reports_with_catalog", lambda _root: _archived_report_metrics(case)
    )
    monkeypatch.setattr(validator, "_python_collected_count", lambda _project: None)

    summary = validator._test_execution_receipt_summary(case["receipt_fields"]["working_directory"])
    status = validator.validate_test_status(case["id"])
    assert status["test_execution_state"] == summary["state"]
    assert status["status"] != "SUCCESS"

    observed = _inspect_test(validator, case["id"])
    assert observed.state in {ValidatorState.PARTIAL, ValidatorState.RED}
    rollup = observed.validated_facts["test.stats"]
    assert rollup["execution_state"] == summary["state"]
    original = ARCHIVED_ROLLUPS[case["id"]]
    assert rollup["unique"] == original["test_stats"]["unique"]
    assert rollup["raw"] == original["test_stats"]["raw"]

    state = RunEvidenceState(run_id=f"offline-calibration-{case['id']}")
    state.set_fact("test.stats", rollup, evidence_ref="forensic:archived-test-rollup")
    state.seal(finalized_at="2026-09-08T00:00:00Z", close_reason="test_terminated")
    assert original["build"]["judgment"] == "success"
    build_validator = SimpleNamespace(
        validate_build_status=lambda _project: {
            "success": True,
            "build_complete": True,
            "evidence_status": "verified",
            "evidence": {"class_count": original["build"]["compiled_classes"]},
        }
    )
    # Pure snapshot projection only: no finalizer.finalize(), file writes,
    # host publication, new CI comparison or claimed real rerun occurs here.
    snapshot = VerdictFinalizer(
        None, validator=build_validator, project_name=case["id"]
    )._snapshot_for_state(state)
    assert snapshot.test_stats.judgment == summary["state"]
    assert snapshot.verdict == ("failed" if summary["state"] == "failed" else "partial")
    assert snapshot.test_stats.unique.model_dump() == original["test_stats"]["unique"]
    assert snapshot.test_stats.raw.model_dump() == original["test_stats"]["raw"]
    assert validate_verdict_snapshot_v3(snapshot.model_dump(mode="json")) == snapshot
