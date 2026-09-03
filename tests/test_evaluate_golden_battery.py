from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sag.agent.receipt_test_rows import testcase_execution_id as _testcase_execution_id
from scripts.evaluate_golden_battery import (
    COUNT_FIELDS,
    LEGACY_V1_MODE,
    METRICS_V2_IDENTITY_VERSION,
    METRICS_V2_SCHEMA_VERSION,
    EvaluationError,
    MetricRef,
    aggregate_v2_tests,
    case_key,
    compare_metric_refs,
    compare_project_metrics,
    evaluate_v2_campaign,
    expand_seeded_report_delta,
    main,
    recompute_legacy_v1,
    subject_key,
    validate_count_measurement,
    validate_count_object,
    validate_v2_project,
    verify_fixture_manifest,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "battery_20260808"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _fixture_path(role: str) -> Path:
    entry = next(item for item in _manifest()["objects"] if item["role"] == role)
    return FIXTURE_ROOT / entry["path"]


def _json_fixture(role: str) -> dict:
    return json.loads(_fixture_path(role).read_text(encoding="utf-8"))


def _records() -> dict:
    return _json_fixture("metrics_v2_identity_records")


def _tests_surface() -> dict:
    records = _records()
    return aggregate_v2_tests(records["receipt_executions"], records["report_observations"])


def _project(*, run_order_index: int = 0, tests: dict | None = None) -> dict:
    return {
        "schema_version": METRICS_V2_SCHEMA_VERSION,
        "identity_version": METRICS_V2_IDENTITY_VERSION,
        "run": {
            "run_id": f"fixture-run-{run_order_index}",
            "target_sha": "fixture-target-sha",
            "sag_sha": "fixture-sag-sha",
            "prompt_hash": "fixture-prompt-hash",
            "control_bundle_hash": "fixture-control-bundle-hash",
            "image_digest": "sha256:fixture-image",
            "model_pin": "fixture-model",
            "run_order_index": run_order_index,
        },
        "outcome": {
            "verdict": "partial",
            "build_state": "success",
            "test_state": "partial",
            "terminal_reason": "fixture_complete",
        },
        "rates": {
            "build": {
                "modules": {
                    "rate": 100.0,
                    "band": "fully",
                    "numerator": 2,
                    "denominator": 2,
                },
                "classes": {
                    "band": "unavailable",
                    "reason": "fixture class census unavailable",
                },
            },
            "test": {
                "cases": {
                    "rate": 75.0,
                    "band": "most",
                    "numerator": 3,
                    "denominator": 4,
                },
                "modules": {
                    "rate": 50.0,
                    "band": "half",
                    "numerator": 1,
                    "denominator": 2,
                },
            },
            "coverage": {
                "status": "unavailable",
                "reason": "coverage pass not run",
            },
        },
        "evidence": {
            "integrity": "complete",
            "receipts_expected": 4,
            "receipts_persisted": 4,
            "terminal_receipts_unpersisted": 0,
            "conflict_count": 0,
        },
        "tests": copy.deepcopy(tests if tests is not None else _tests_surface()),
        "coverage": {
            "domains_discovered": 2,
            "domains_attempted": 2,
            "domains_terminal": 2,
            "domains_with_claimed_tests": 2,
        },
        "control": {
            "terminal_refusal_recurrences": 0,
            "unsettled_jobs": 0,
            "cleanup_escalations": 0,
            "midrun_human_approvals": 0,
        },
    }


def test_rates_pass_through_evaluator_rows_without_math_and_v3_is_incomparable():
    baseline = _project(run_order_index=0)
    candidate = _project(run_order_index=0)
    candidate["rates"]["coverage"] = {
        "status": "unavailable",
        "reason": "candidate coverage not collected",
    }
    exact_rates = copy.deepcopy(baseline["rates"])

    validated = validate_v2_project(baseline)
    compared = compare_project_metrics(
        baseline,
        candidate,
        baseline_grain="subject",
        baseline_disposition="claimed",
    )

    assert validated["rates"] == exact_rates
    assert compared["rates_comparable"] is True
    assert compared["rates"] == {
        "baseline": exact_rates,
        "candidate": candidate["rates"],
    }

    legacy_v3 = copy.deepcopy(candidate)
    legacy_v3.pop("rates")
    incomparable = compare_project_metrics(
        baseline,
        legacy_v3,
        baseline_grain="subject",
        baseline_disposition="claimed",
    )
    assert incomparable["rates_comparable"] is False
    assert incomparable["rates"] == {"baseline": exact_rates, "candidate": None}


def test_legacy_mode_recomputes_the_two_audited_v1_baselines():
    result = recompute_legacy_v1(_json_fixture("legacy_v1_audited_baseline"))

    assert result["schema_version"] == 1
    assert result["legacy_mode"] == LEGACY_V1_MODE
    assert result["explicit_legacy"] is True
    assert result["campaigns"] == [
        {
            "campaign_id": "2026-07-27",
            "project_count": 23,
            "verdict_distribution": {
                "success": 2,
                "partial": 14,
                "failed": 7,
                "unknown": 0,
            },
            "canonical_unique": 44_983,
            "primary_raw": 45_423,
            "auxiliary": 13_507,
        },
        {
            "campaign_id": "2026-08-08",
            "project_count": 23,
            "verdict_distribution": {
                "success": 2,
                "partial": 16,
                "failed": 5,
                "unknown": 0,
            },
            "canonical_unique": 15_322,
            "primary_raw": 18_080,
            "auxiliary": 65_776,
        },
    ]


def test_legacy_mode_must_be_explicit_and_cannot_read_v2():
    legacy = _json_fixture("legacy_v1_audited_baseline")
    legacy.pop("legacy_mode")
    with pytest.raises(EvaluationError, match="explicitly"):
        recompute_legacy_v1(legacy)

    legacy["legacy_mode"] = LEGACY_V1_MODE
    legacy["schema_version"] = 2
    with pytest.raises(EvaluationError, match="schema_version=1"):
        recompute_legacy_v1(legacy)


def test_legacy_cli_prints_the_explicit_label(capsys):
    assert main(["legacy", str(_fixture_path("legacy_v1_audited_baseline"))]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["legacy_mode"] == LEGACY_V1_MODE
    assert body["explicit_legacy"] is True


def test_subject_identity_is_module_qualified_and_ignores_report_provenance():
    records = _records()["receipt_executions"]
    alpha = records[0]
    beta = records[3]
    assert alpha["owner"] == beta["owner"]
    assert alpha["test_name"] == beta["test_name"]
    assert subject_key(alpha) != subject_key(beta)

    moved_report = {**alpha, "report_path": "some/other/attempt/TEST.xml"}
    assert subject_key(moved_report) == subject_key(alpha)
    assert case_key(moved_report) == case_key(alpha)

    new_target = {**alpha, "target_sha": "different-target"}
    assert subject_key(new_target) != subject_key(alpha)


def test_parameter_values_change_cases_but_not_subjects():
    records = _records()["receipt_executions"]
    parameter_one = records[1]
    parameter_two = records[2]
    assert subject_key(parameter_one) == subject_key(parameter_two)
    assert case_key(parameter_one) != case_key(parameter_two)


def test_v2_aggregation_keeps_all_four_grains_separate():
    tests = _tests_surface()

    assert tests["claimed"] == {
        "latest_subjects": {
            "executed": 2,
            "passed": 2,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        },
        "latest_cases": {
            "executed": 3,
            "passed": 2,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
        },
        "receipt_executions": {
            "executed": 4,
            "passed": 2,
            "failed": 1,
            "errors": 0,
            "skipped": 1,
        },
    }
    assert tests["retried_cases"] == 1
    assert tests["flaky_cases"] == 1
    assert tests["quarantined_observations"]["errors"] == 1
    assert tests["unattributed_observations"]["passed"] == 1
    assert tests["stale_observations"]["failed"] == 1


def test_duplicate_report_view_does_not_double_count_one_physical_execution():
    records = _records()
    assert len(records["receipt_executions"]) == 5
    tests = aggregate_v2_tests(records["receipt_executions"], records["report_observations"])
    assert tests["claimed"]["receipt_executions"]["executed"] == 4
    assert tests["claimed"]["latest_cases"]["executed"] == 3


def test_legacy_execution_rows_are_readable_fixtures_but_never_promoted():
    legacy_path = (
        FIXTURE_ROOT
        / "sha256-f044b63a054dbbea9a1d6bcaf3f8d6c9fcfa6c957f7bdc0a06209705bb2b62fc.json"
    )
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))

    with pytest.raises(EvaluationError, match="requires run_id"):
        aggregate_v2_tests(legacy["receipt_executions"], legacy["report_observations"])


def test_evaluator_recomputes_execution_id_and_rejects_report_rebinding():
    records = _records()
    invented = copy.deepcopy(records["receipt_executions"])
    invented[0]["execution_id"] = "arbitrary-id"
    with pytest.raises(EvaluationError, match="execution_id"):
        aggregate_v2_tests(invented, records["report_observations"])

    rebound = copy.deepcopy(records["receipt_executions"])
    rebound[0]["report_path"] = "other/TEST.xml"
    with pytest.raises(EvaluationError, match="execution_id"):
        aggregate_v2_tests(rebound, records["report_observations"])


def test_retry_history_does_not_overwrite_physical_failure_history():
    tests = _tests_surface()
    assert tests["claimed"]["latest_cases"]["failed"] == 0
    assert tests["claimed"]["receipt_executions"]["failed"] == 1
    assert tests["flaky_cases"] == 1


def test_subject_outcome_folds_current_cases_by_severity():
    records = _records()
    executions = copy.deepcopy(records["receipt_executions"])
    error_case = copy.deepcopy(executions[1])
    error_case.update(
        {
            "receipt_id": "receipt-alpha-3",
            "execution_index": 3,
            "execution_ordinal": 3,
            "parameter_id": "size=3",
            "outcome": "error",
        }
    )
    error_case["execution_id"] = _testcase_execution_id(error_case)
    executions.append(error_case)

    tests = aggregate_v2_tests(executions, records["report_observations"])
    assert tests["claimed"]["latest_subjects"] == {
        "executed": 2,
        "passed": 1,
        "failed": 0,
        "errors": 1,
        "skipped": 0,
    }


def test_conflicting_duplicate_execution_id_is_rejected():
    records = _records()
    executions = copy.deepcopy(records["receipt_executions"])
    executions[-1]["outcome"] = "failed"
    with pytest.raises(EvaluationError, match="conflicting duplicate execution_id"):
        aggregate_v2_tests(executions, records["report_observations"])


@pytest.mark.parametrize("disposition", ["quarantined", "unattributed", "stale"])
def test_nonclaimed_observations_cannot_be_mutated_into_claimed_metrics(disposition):
    records = _records()
    receipt_mutation = copy.deepcopy(records["receipt_executions"][0])
    receipt_mutation["execution_id"] = f"bad-{disposition}"
    receipt_mutation["disposition"] = disposition

    with pytest.raises(EvaluationError, match="disposition='claimed'"):
        aggregate_v2_tests(
            [*records["receipt_executions"], receipt_mutation],
            records["report_observations"],
        )


def test_report_observation_with_claimed_disposition_is_rejected():
    records = _records()
    observations = copy.deepcopy(records["report_observations"])
    observations[0]["disposition"] = "claimed"
    with pytest.raises(EvaluationError, match="never added to claimed"):
        aggregate_v2_tests(records["receipt_executions"], observations)


def test_count_object_uses_all_numeric_or_all_null_fields():
    absent = {field: None for field in COUNT_FIELDS}
    assert validate_count_object(absent) == absent

    partial = {**absent, "executed": 0}
    with pytest.raises(EvaluationError, match="cannot mix null"):
        validate_count_object(partial)

    inconsistent = {"executed": 3, "passed": 2, "failed": 0, "errors": 0, "skipped": 0}
    with pytest.raises(EvaluationError, match="must equal"):
        validate_count_object(inconsistent)


def test_lower_bound_measurement_keeps_counts_but_cannot_make_a_rate_or_delta():
    baseline = _project()
    candidate = _project()
    candidate["tests"]["claimed"]["latest_subjects"] = {
        "executed": 2,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "availability": "partial",
        "bound": "lower",
        "basis": "latest module-qualified subjects (bounded identity sample)",
        "reason": "identity rows were bounded and truncated",
    }

    measurement = validate_count_measurement(
        candidate["tests"]["claimed"]["latest_subjects"]
    )
    assert measurement["executed"] == 2
    assert measurement["bound"] == "lower"

    compared = compare_project_metrics(
        baseline,
        candidate,
        baseline_grain="subject",
        baseline_disposition="claimed",
    )
    assert compared["candidate_measurement"] == {
        "availability": "partial",
        "bound": "lower",
        "display": "≥2",
        "basis": "latest module-qualified subjects (bounded identity sample)",
        "reason": "identity rows were bounded and truncated",
    }
    assert compared["delta_comparable"] is False
    assert set(compared["delta"].values()) == {None}

    campaign = evaluate_v2_campaign([candidate])
    assert campaign["project_macro"]["median_project_subject_pass_rate"] is None
    assert campaign["diagnostic_totals"]["claimed"]["subjects"]["display"] == "≥2"
    assert campaign["diagnostic_totals"]["claimed"]["subjects"]["bound"] == "lower"


def test_incomplete_suite_total_reason_survives_evaluator_projection():
    baseline = _project()
    candidate = _project()
    candidate["tests"]["claimed"]["receipt_executions"] = {
        "executed": 27219,
        "passed": 27211,
        "failed": 8,
        "errors": 0,
        "skipped": 0,
        "availability": "partial",
        "bound": "lower",
        "basis": "gradle suite totals over the claimed reports the read reached",
        "reason": (
            "gradle suite totals were incomplete (disclosed bounds: unsummarized_files); "
            "counts cover only the claimed reports the read reached"
        ),
    }

    compared = compare_project_metrics(
        baseline,
        candidate,
        baseline_grain="receipt_execution",
        baseline_disposition="claimed",
    )

    assert compared["candidate_measurement"]["display"] == "≥27,219"
    assert "disclosed bounds: unsummarized_files" in compared["candidate_measurement"]["reason"]
    assert compared["delta_comparable"] is False
    assert set(compared["delta"].values()) == {None}

    campaign = evaluate_v2_campaign([candidate])
    aggregate = campaign["diagnostic_totals"]["claimed"]["receipt_executions"]
    assert aggregate["availability"] == "partial"
    assert aggregate["bound"] == "lower"
    assert aggregate["display"] == "≥27,219"
    assert "disclosed bounds: unsummarized_files" in aggregate["reason"]


def test_partial_measurement_requires_lower_bound_metadata():
    base = {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "availability": "partial",
        "bound": "lower",
        "basis": "bounded identity sample",
        "reason": "sample truncated",
    }
    for missing in ("bound", "basis", "reason"):
        broken = {**base}
        broken.pop(missing)
        with pytest.raises(EvaluationError):
            validate_count_measurement(broken)


def test_campaign_accepts_explicitly_unavailable_identity_diagnostics_without_promoting_them():
    project = _project()
    unavailable = {field: None for field in COUNT_FIELDS}
    project["tests"]["claimed"]["latest_subjects"] = dict(unavailable)
    project["tests"]["claimed"]["latest_cases"] = dict(unavailable)
    project["tests"]["retried_cases"] = None
    project["tests"]["flaky_cases"] = None
    project["coverage"] = {
        "domains_discovered": None,
        "domains_attempted": None,
        "domains_terminal": None,
        "domains_with_claimed_tests": None,
    }
    project["control"]["terminal_refusal_recurrences"] = None
    project["control"]["cleanup_escalations"] = None

    validated = validate_v2_project(project)
    result = evaluate_v2_campaign([validated])

    assert result["project_macro"]["median_project_subject_pass_rate"] is None
    assert result["project_macro"]["all_discovered_domains_terminal_project_rate"] == 0.0
    assert result["project_macro"]["zero_unsettled_and_no_recurrence_overflow_project_rate"] == 0.0
    assert result["diagnostic_totals"]["claimed"]["subjects"] == unavailable
    assert result["diagnostic_totals"]["claimed"]["cases"] == unavailable


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "target_sha",
        "sag_sha",
        "prompt_hash",
        "control_bundle_hash",
        "image_digest",
        "model_pin",
        "run_order_index",
    ],
)
def test_every_v2_project_retains_all_run_pins(field):
    project = _project()
    del project["run"][field]
    with pytest.raises(EvaluationError, match=field):
        validate_v2_project(project)


@pytest.mark.parametrize(
    "field",
    [
        "integrity",
        "receipts_expected",
        "receipts_persisted",
        "terminal_receipts_unpersisted",
        "conflict_count",
    ],
)
def test_every_v2_project_retains_all_evidence_status_fields(field):
    project = _project()
    del project["evidence"][field]
    with pytest.raises(EvaluationError, match=field):
        validate_v2_project(project)


def test_complete_integrity_rejects_a_missing_expected_receipt():
    project = _project()
    project["evidence"].update(
        {
            "receipts_expected": 2,
            "receipts_persisted": 1,
            "terminal_receipts_unpersisted": 0,
            "integrity": "complete",
        }
    )

    with pytest.raises(EvaluationError, match="expected receipts are missing"):
        validate_v2_project(project)


def test_comparison_rejects_v1_against_v2():
    with pytest.raises(EvaluationError, match="schema_version differs"):
        compare_project_metrics(
            _json_fixture("legacy_v1_audited_baseline"),
            _project(),
            baseline_grain="subject",
            baseline_disposition="claimed",
        )


def test_direct_metric_comparison_is_v2_only():
    counts = {"executed": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
    legacy = MetricRef(1, "legacy-canonical", "claimed", "subject", counts)
    with pytest.raises(EvaluationError, match="metrics-v2 only"):
        compare_metric_refs(legacy, legacy)


def test_comparison_rejects_identity_version_mismatch():
    counts = {"executed": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
    baseline = MetricRef(2, METRICS_V2_IDENTITY_VERSION, "claimed", "subject", counts)
    candidate = MetricRef(2, "module-qualified-v2", "claimed", "subject", counts)
    with pytest.raises(EvaluationError, match="identity_version differs"):
        compare_metric_refs(baseline, candidate)


def test_comparison_rejects_subjects_against_cases():
    with pytest.raises(EvaluationError, match="grain differs"):
        compare_project_metrics(
            _project(),
            _project(),
            baseline_grain="subject",
            baseline_disposition="claimed",
            candidate_grain="case",
        )


def test_comparison_rejects_cases_against_receipt_executions():
    with pytest.raises(EvaluationError, match="grain differs"):
        compare_project_metrics(
            _project(),
            _project(),
            baseline_grain="case",
            baseline_disposition="claimed",
            candidate_grain="receipt_execution",
        )


def test_comparison_rejects_observation_disposition_mismatch():
    with pytest.raises(EvaluationError, match="disposition differs"):
        compare_project_metrics(
            _project(),
            _project(),
            baseline_grain="report_observation",
            baseline_disposition="quarantined",
            candidate_grain="report_observation",
            candidate_disposition="stale",
        )


def test_comparison_of_identical_surface_returns_field_deltas():
    candidate = _project()
    candidate["tests"]["claimed"]["latest_subjects"] = {
        "executed": 3,
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    result = compare_project_metrics(
        _project(),
        candidate,
        baseline_grain="subject",
        baseline_disposition="claimed",
    )
    assert result["grain"] == "subject"
    assert result["disposition"] == "claimed"
    assert result["delta"] == {
        "executed": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


def test_comparison_rejects_run_pin_drift_without_explicit_invalidation():
    candidate = _project()
    candidate["run"]["prompt_hash"] = "different-prompt"

    with pytest.raises(EvaluationError, match="run pins drift"):
        compare_project_metrics(
            _project(),
            candidate,
            baseline_grain="subject",
            baseline_disposition="claimed",
        )

    result = compare_project_metrics(
        _project(),
        candidate,
        baseline_grain="subject",
        baseline_disposition="claimed",
        invalidation_reason="prompt experiment",
    )
    assert result["invalidated"] is True
    assert result["run_pin_drift"]["prompt_hash"] == (
        "fixture-prompt-hash",
        "different-prompt",
    )


def test_success_with_degraded_or_unpersisted_evidence_is_rejected():
    degraded = _project()
    degraded["outcome"]["verdict"] = "success"
    degraded["evidence"]["integrity"] = "degraded"
    with pytest.raises(EvaluationError, match="cannot be success"):
        validate_v2_project(degraded)

    unpersisted = _project()
    unpersisted["outcome"]["verdict"] = "success"
    unpersisted["evidence"]["receipts_expected"] = 5
    unpersisted["evidence"]["terminal_receipts_unpersisted"] = 1
    unpersisted["evidence"]["integrity"] = "degraded"
    with pytest.raises(EvaluationError, match="terminal receipts unpersisted"):
        validate_v2_project(unpersisted)


def test_campaign_scorecard_is_project_macro_first_and_totals_stay_separate():
    first = _project(run_order_index=0)
    second = _project(run_order_index=1)
    second["outcome"]["verdict"] = "partial"
    second["evidence"]["integrity"] = "degraded"
    second["control"]["terminal_refusal_recurrences"] = 3

    result = evaluate_v2_campaign([first, second])

    assert list(result).index("project_macro") < list(result).index("diagnostic_totals")
    assert result["project_macro"] == {
        "verdict_distribution": {
            "success": 0,
            "partial": 2,
            "failed": 0,
            "unknown": 0,
        },
        "evidence_complete_project_rate": 0.5,
        "all_discovered_domains_terminal_project_rate": 1.0,
        "median_project_subject_pass_rate": 1.0,
        "zero_unsettled_and_no_recurrence_overflow_project_rate": 1.0,
        "autonomy_invariant": True,
    }
    assert result["diagnostic_totals"]["claimed"]["subjects"]["executed"] == 4
    assert result["diagnostic_totals"]["claimed"]["cases"]["executed"] == 6
    assert result["diagnostic_totals"]["claimed"]["receipt_executions"]["executed"] == 8
    assert result["diagnostic_totals"]["observations"]["quarantined"]["executed"] == 2


def test_campaign_rejects_midrun_human_approval_and_recurrence_overflow():
    approval = _project()
    approval["control"]["midrun_human_approvals"] = 1
    with pytest.raises(EvaluationError, match="autonomy invariant"):
        evaluate_v2_campaign([approval])

    overflow = _project()
    overflow["control"]["terminal_refusal_recurrences"] = 4
    with pytest.raises(EvaluationError, match="recurrence overflow"):
        evaluate_v2_campaign([overflow])


def test_fixture_manifest_is_content_addressed_and_covers_live_shapes():
    result = verify_fixture_manifest(MANIFEST_PATH)
    assert result["fixture_set"] == "battery_20260808"
    assert result["object_count"] == 8
    roles = {item["role"] for item in result["objects"]}
    assert roles == {
        "legacy_v1_audited_baseline",
        "metrics_v2_identity_records",
        "lucene_large_report_delta_seed",
        "camel_settlement_shape",
        "http_maven_wrapper_bootstrap",
        "camel_quarkus_java_version_failure",
        "gradle_success_tail",
        "gate_refusal_no_progress_sequence",
    }

    manifest = _manifest()
    for entry in manifest["objects"]:
        assert Path(entry["path"]).name.startswith(f"sha256-{entry['sha256']}.")
        assert entry["sources"]
        assert all(len(source["sha256"]) == 64 for source in entry["sources"])


def test_lucene_seed_recreates_large_shape_without_storing_large_array():
    seed_path = _fixture_path("lucene_large_report_delta_seed")
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    expanded = expand_seeded_report_delta(seed)

    assert seed_path.stat().st_size < 1024
    assert expanded["terminal"] == {
        "receipt_id": "inv-gradle-1-0002",
        "lifecycle_state": "finished",
        "outcome": "completed",
        "exit_code": 0,
    }
    assert len(expanded["report_delta"]["new"]) == 1683
    assert expanded["report_delta"]["changed"] == []
    assert expanded["report_delta"]["new"][0] != expanded["report_delta"]["new"][-1]


def test_text_and_settlement_fixtures_pin_the_live_regressions():
    http = _fixture_path("http_maven_wrapper_bootstrap").read_text(encoding="utf-8")
    assert "apache-maven-3.9.11-bin.zip" in http
    assert "Failed to validate Maven distribution SHA-256" in http

    java = _fixture_path("camel_quarkus_java_version_failure").read_text(encoding="utf-8")
    assert "Required Java version 17 is not met by current version: 11.0.31" in java

    gradle = _fixture_path("gradle_success_tail").read_text(encoding="utf-8")
    assert "checkKotlinGradlePluginConfigurationErrors" in gradle
    assert "BUILD SUCCESSFUL" in gradle

    settlement = _json_fixture("camel_settlement_shape")
    assert settlement["terminal_observation"]["exit_marker_present"] is True
    assert settlement["terminal_observation"]["exit_code"] == 137
    assert settlement["job_obligation"]["settled_receipt_id"] is None
    counts = settlement["terminal_observation"]["physical_counts"]
    assert validate_count_object(counts)["executed"] == 22_151


def test_gate_fixture_changes_prose_without_changing_evidence_or_progress():
    fixture = _json_fixture("gate_refusal_no_progress_sequence")
    events = fixture["events"]
    assert len({event["model_prose"] for event in events}) == 3
    assert {event["gate_code"] for event in events} == {"build_red"}
    assert all(event["material_action_since_previous_claim"] is False for event in events)
    assert fixture["evidence_fingerprint"] == "httpcomponents-wrapper-checksum-no-classes-v1"
