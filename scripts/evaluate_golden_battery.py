#!/usr/bin/env python3
"""Evaluate the golden battery without collapsing evidence grains.

Metrics v2 deliberately starts a new series.  Its subject, parameter-aware
case, physical receipt-execution, and report-observation counts are independent
surfaces.  This module provides the small amount of shared machinery needed to
build deterministic fixtures, validate project rows, calculate project-macro
scorecards, and reject comparisons that would cross a schema, identity,
disposition, or counting grain.

Historical metrics-v1 data is accepted only by :func:`recompute_legacy_v1` and
only when the input explicitly labels itself ``legacy-v1-forensic``.  It is
never adapted into a v2 row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sag.agent.receipt_test_rows import (
    TestcaseRowContractError,
    aggregate_testcase_execution_rows,
)

METRICS_V2_SCHEMA_VERSION = 2
METRICS_V2_IDENTITY_VERSION = "module-qualified-v1"
LEGACY_V1_MODE = "legacy-v1-forensic"
MAX_TERMINAL_REFUSAL_RECURRENCES = 3

COUNT_FIELDS = ("executed", "passed", "failed", "errors", "skipped")
OUTCOMES = ("passed", "failed", "error", "skipped")
VERDICTS = ("success", "partial", "failed", "unknown")
OBSERVATION_DISPOSITIONS = ("quarantined", "unattributed", "stale")
GRAINS = ("subject", "case", "receipt_execution", "report_observation")

_OUTCOME_TO_FIELD = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "skipped": "skipped",
}


class EvaluationError(ValueError):
    """The input would make the score ambiguous or non-comparable."""


@dataclass(frozen=True)
class MetricRef:
    """One explicitly typed metric surface selected from a project artifact."""

    schema_version: int
    identity_version: str
    disposition: str
    grain: str
    counts: Mapping[str, int | None]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvaluationError(f"{path} must be an array")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{path} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationError(f"{path} must be a non-negative integer")
    return int(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _empty_counts() -> dict[str, int]:
    return {field: 0 for field in COUNT_FIELDS}


def _count_outcomes(outcomes: Iterable[str]) -> dict[str, int]:
    counts = _empty_counts()
    for outcome in outcomes:
        if outcome not in OUTCOMES:
            raise EvaluationError(f"unsupported test outcome {outcome!r}")
        counts["executed"] += 1
        counts[_OUTCOME_TO_FIELD[outcome]] += 1
    return counts


def validate_count_object(value: Any, path: str = "counts") -> dict[str, int | None]:
    """Validate one five-field count object.

    A measurement is either fully known or fully absent.  A known ``executed``
    value must equal the four outcome buckets; a missing measurement uses null
    in every field rather than a misleading zero.
    """

    raw = _mapping(value, path)
    result: dict[str, int | None] = {}
    null_fields = 0
    for field in COUNT_FIELDS:
        if field not in raw:
            raise EvaluationError(f"{path}.{field} is required")
        item = raw[field]
        if item is None:
            result[field] = None
            null_fields += 1
        else:
            result[field] = _nonnegative_int(item, f"{path}.{field}")
    if null_fields not in (0, len(COUNT_FIELDS)):
        raise EvaluationError(f"{path} cannot mix null and numeric count fields")
    if not null_fields:
        outcomes = 0
        for field in COUNT_FIELDS[1:]:
            value = result[field]
            assert value is not None
            outcomes += value
        if result["executed"] != outcomes:
            raise EvaluationError(f"{path}.executed must equal passed + failed + errors + skipped")
    return result


def _identity_material(record: Mapping[str, Any]) -> dict[str, Any]:
    material = {
        "identity_version": METRICS_V2_IDENTITY_VERSION,
        "target_sha": _nonempty_string(record.get("target_sha"), "identity.target_sha"),
        "domain_id": _nonempty_string(record.get("domain_id"), "identity.domain_id"),
        "module_coordinate": _nonempty_string(
            record.get("module_coordinate"), "identity.module_coordinate"
        ),
        "framework": _nonempty_string(record.get("framework"), "identity.framework"),
        "owner": _nonempty_string(record.get("owner"), "identity.owner"),
        "test_name": _nonempty_string(record.get("test_name"), "identity.test_name"),
    }
    return material


def subject_key(record: Mapping[str, Any]) -> str:
    """Return a stable, ecosystem-neutral, module-qualified subject key."""

    return f"{METRICS_V2_IDENTITY_VERSION}:{_sha256(_identity_material(record))}"


def case_key(record: Mapping[str, Any]) -> str:
    """Return a parameter-aware case key for a subject."""

    if "parameter_id" not in record:
        raise EvaluationError("identity.parameter_id is required (use null for a default case)")
    parameter_id = record["parameter_id"]
    if not isinstance(parameter_id, (str, int, float, bool, type(None))):
        raise EvaluationError("identity.parameter_id must be a JSON scalar or null")
    material = _identity_material(record)
    material["parameter_id"] = parameter_id
    return f"{METRICS_V2_IDENTITY_VERSION}:{_sha256(material)}"


def _observation_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    disposition = _nonempty_string(record.get("disposition"), "report_observation.disposition")
    if disposition not in OBSERVATION_DISPOSITIONS:
        raise EvaluationError(
            "report observation disposition must be quarantined, unattributed, or stale; "
            "observations are never added to claimed receipt metrics"
        )
    outcome = _nonempty_string(record.get("outcome"), "report_observation.outcome")
    if outcome not in OUTCOMES:
        raise EvaluationError(f"unsupported report observation outcome {outcome!r}")
    return {
        "disposition": disposition,
        "outcome": outcome,
        "reason": _nonempty_string(record.get("reason"), "report_observation.reason"),
        "report_path": _nonempty_string(
            record.get("report_path"), "report_observation.report_path"
        ),
        "subject_key": subject_key(record) if record.get("attributed_identity") is True else None,
        "case_key": case_key(record) if record.get("attributed_identity") is True else None,
    }


def aggregate_v2_tests(
    receipt_executions: Sequence[Mapping[str, Any]],
    report_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate row-level v2 evidence without mixing its four grains.

    Duplicate views of the same ``execution_id`` are idempotent when their
    semantic fields agree.  Report paths and timestamps are provenance and do
    not create a second physical execution.  A conflicting duplicate is an
    error rather than last-write-wins.
    """

    try:
        execution_aggregate = aggregate_testcase_execution_rows(receipt_executions)
    except TestcaseRowContractError as exc:
        raise EvaluationError(str(exc)) from exc

    observations: dict[str, dict[str, Any]] = {}
    for index, raw_record in enumerate(report_observations):
        record = _mapping(raw_record, f"report_observations[{index}]")
        observation_id = _nonempty_string(
            record.get("observation_id"), f"report_observations[{index}].observation_id"
        )
        semantics = _observation_semantics(record)
        prior = observations.get(observation_id)
        if prior is not None and prior != semantics:
            raise EvaluationError(f"conflicting duplicate observation_id {observation_id!r}")
        observations[observation_id] = semantics

    observation_buckets: dict[str, dict[str, Any]] = {}
    for disposition in OBSERVATION_DISPOSITIONS:
        bucket = [row for row in observations.values() if row["disposition"] == disposition]
        reasons: dict[str, int] = {}
        for row in bucket:
            reason = str(row["reason"])
            reasons[reason] = reasons.get(reason, 0) + 1
        observation_buckets[f"{disposition}_observations"] = {
            **_count_outcomes(str(row["outcome"]) for row in bucket),
            "report_file_count": len({str(row["report_path"]) for row in bucket}),
            "reason_counts": dict(sorted(reasons.items())),
        }

    return {
        **execution_aggregate,
        **observation_buckets,
    }


def _validate_observation_bucket(value: Any, path: str) -> dict[str, Any]:
    raw = _mapping(value, path)
    counts = validate_count_object(raw, path)
    report_file_count = raw.get("report_file_count")
    if report_file_count is not None:
        report_file_count = _nonnegative_int(report_file_count, f"{path}.report_file_count")
    reason_counts_raw = raw.get("reason_counts")
    if reason_counts_raw is None:
        reason_counts: dict[str, int] | None = None
    else:
        reason_counts = {}
        for reason, count in _mapping(reason_counts_raw, f"{path}.reason_counts").items():
            reason_key = _nonempty_string(reason, f"{path}.reason_counts key")
            reason_counts[reason_key] = _nonnegative_int(
                count, f"{path}.reason_counts.{reason_key}"
            )
        if counts["executed"] is not None and sum(reason_counts.values()) != counts["executed"]:
            raise EvaluationError(f"{path}.reason_counts must account for every observation")
    return {
        **counts,
        "report_file_count": report_file_count,
        "reason_counts": reason_counts,
    }


def validate_v2_project(value: Any, path: str = "project") -> Mapping[str, Any]:
    """Validate the complete metrics-v2 project decision surface."""

    project = _mapping(value, path)
    if project.get("schema_version") != METRICS_V2_SCHEMA_VERSION:
        raise EvaluationError(
            f"{path}.schema_version must be {METRICS_V2_SCHEMA_VERSION}; "
            "legacy v1 is not a v2 project"
        )
    identity_version = _nonempty_string(project.get("identity_version"), f"{path}.identity_version")
    if identity_version != METRICS_V2_IDENTITY_VERSION:
        raise EvaluationError(
            f"{path}.identity_version {identity_version!r} is unsupported; "
            f"expected {METRICS_V2_IDENTITY_VERSION!r}"
        )

    run = _mapping(project.get("run"), f"{path}.run")
    for field in (
        "run_id",
        "target_sha",
        "sag_sha",
        "prompt_hash",
        "control_bundle_hash",
        "image_digest",
        "model_pin",
    ):
        _nonempty_string(run.get(field), f"{path}.run.{field}")
    _nonnegative_int(run.get("run_order_index"), f"{path}.run.run_order_index")

    outcome = _mapping(project.get("outcome"), f"{path}.outcome")
    verdict = _nonempty_string(outcome.get("verdict"), f"{path}.outcome.verdict")
    if verdict not in VERDICTS:
        raise EvaluationError(f"{path}.outcome.verdict is unsupported")
    for field in ("build_state", "test_state", "terminal_reason"):
        _nonempty_string(outcome.get(field), f"{path}.outcome.{field}")

    evidence = _mapping(project.get("evidence"), f"{path}.evidence")
    integrity = _nonempty_string(evidence.get("integrity"), f"{path}.evidence.integrity")
    if integrity not in ("complete", "degraded", "failed", "unavailable"):
        raise EvaluationError(f"{path}.evidence.integrity is unsupported")
    evidence_counts = {}
    for field in (
        "receipts_expected",
        "receipts_persisted",
        "terminal_receipts_unpersisted",
        "conflict_count",
    ):
        evidence_counts[field] = _nonnegative_int(evidence.get(field), f"{path}.evidence.{field}")
    if evidence_counts["receipts_persisted"] > evidence_counts["receipts_expected"]:
        raise EvaluationError(f"{path}.evidence.receipts_persisted exceeds receipts_expected")
    if (
        evidence_counts["receipts_persisted"] + evidence_counts["terminal_receipts_unpersisted"]
        > evidence_counts["receipts_expected"]
    ):
        raise EvaluationError(
            f"{path}.evidence receipts_expected is smaller than persisted plus unpersisted"
        )
    if (
        integrity == "complete"
        and evidence_counts["receipts_persisted"] + evidence_counts["terminal_receipts_unpersisted"]
        != evidence_counts["receipts_expected"]
    ):
        raise EvaluationError(
            f"{path}.evidence.integrity cannot be complete while expected receipts are missing"
        )
    if evidence_counts["terminal_receipts_unpersisted"]:
        if integrity == "complete":
            raise EvaluationError(
                f"{path}.evidence.integrity cannot be complete with terminal receipts unpersisted"
            )
        if verdict == "success":
            raise EvaluationError(
                f"{path}.outcome.verdict cannot be success with terminal receipts unpersisted"
            )
    if verdict == "success" and integrity != "complete":
        raise EvaluationError(
            f"{path}.outcome.verdict cannot be success when evidence integrity is {integrity}"
        )

    tests = _mapping(project.get("tests"), f"{path}.tests")
    claimed = _mapping(tests.get("claimed"), f"{path}.tests.claimed")
    for field in ("latest_subjects", "latest_cases", "receipt_executions"):
        validate_count_object(claimed.get(field), f"{path}.tests.claimed.{field}")
    for disposition in OBSERVATION_DISPOSITIONS:
        field = f"{disposition}_observations"
        _validate_observation_bucket(tests.get(field), f"{path}.tests.{field}")
    retried_raw = tests.get("retried_cases")
    flaky_raw = tests.get("flaky_cases")
    retried_cases = (
        None
        if retried_raw is None
        else _nonnegative_int(retried_raw, f"{path}.tests.retried_cases")
    )
    flaky_cases = (
        None if flaky_raw is None else _nonnegative_int(flaky_raw, f"{path}.tests.flaky_cases")
    )
    if retried_cases is not None and flaky_cases is not None and flaky_cases > retried_cases:
        raise EvaluationError(f"{path}.tests.flaky_cases cannot exceed retried_cases")

    coverage = _mapping(project.get("coverage"), f"{path}.coverage")
    coverage_counts: dict[str, int | None] = {}
    for field in (
        "domains_discovered",
        "domains_attempted",
        "domains_terminal",
        "domains_with_claimed_tests",
    ):
        raw_count = coverage.get(field)
        coverage_counts[field] = (
            None if raw_count is None else _nonnegative_int(raw_count, f"{path}.coverage.{field}")
        )
    discovered = coverage_counts["domains_discovered"]
    attempted = coverage_counts["domains_attempted"]
    terminal = coverage_counts["domains_terminal"]
    with_tests = coverage_counts["domains_with_claimed_tests"]
    known_domain_counts = (discovered, attempted, terminal)
    if any(value is None for value in known_domain_counts):
        if not all(value is None for value in known_domain_counts):
            raise EvaluationError(
                f"{path}.coverage cannot mix unavailable and numeric domain lifecycle counts"
            )
        if with_tests is not None:
            raise EvaluationError(
                f"{path}.coverage.domains_with_claimed_tests requires domain lifecycle counts"
            )
    else:
        assert discovered is not None and attempted is not None and terminal is not None
        if attempted > discovered:
            raise EvaluationError(f"{path}.coverage.domains_attempted exceeds domains_discovered")
        if terminal > attempted:
            raise EvaluationError(f"{path}.coverage.domains_terminal exceeds domains_attempted")
        if with_tests is not None and with_tests > terminal:
            raise EvaluationError(
                f"{path}.coverage.domains_with_claimed_tests exceeds domains_terminal"
            )

    control = _mapping(project.get("control"), f"{path}.control")
    for field in (
        "terminal_refusal_recurrences",
        "unsettled_jobs",
        "cleanup_escalations",
    ):
        if control.get(field) is not None:
            _nonnegative_int(control.get(field), f"{path}.control.{field}")
    _nonnegative_int(
        control.get("midrun_human_approvals"),
        f"{path}.control.midrun_human_approvals",
    )
    return project


def _selected_counts(project: Mapping[str, Any], grain: str, disposition: str) -> Mapping[str, Any]:
    tests = _mapping(project["tests"], "project.tests")
    if grain == "subject":
        field = "latest_subjects"
    elif grain == "case":
        field = "latest_cases"
    elif grain == "receipt_execution":
        field = "receipt_executions"
    elif grain == "report_observation":
        if disposition not in OBSERVATION_DISPOSITIONS:
            raise EvaluationError(
                "report_observation grain requires quarantined, unattributed, or stale disposition"
            )
        return _mapping(tests[f"{disposition}_observations"], "observation counts")
    else:
        raise EvaluationError(f"unsupported metric grain {grain!r}")
    if disposition != "claimed":
        raise EvaluationError(f"{grain} grain requires disposition='claimed'")
    return _mapping(_mapping(tests["claimed"], "project.tests.claimed")[field], "claimed counts")


def select_metric(project: Any, *, grain: str, disposition: str) -> MetricRef:
    """Select one explicitly tagged metric surface from a v2 project."""

    validated = validate_v2_project(project)
    counts = validate_count_object(_selected_counts(validated, grain, disposition), "metric.counts")
    return MetricRef(
        schema_version=METRICS_V2_SCHEMA_VERSION,
        identity_version=str(validated["identity_version"]),
        disposition=disposition,
        grain=grain,
        counts=counts,
    )


def compare_metric_refs(baseline: MetricRef, candidate: MetricRef) -> dict[str, Any]:
    """Compare only refs with identical schema, identity, disposition, and grain."""

    if (
        baseline.schema_version != METRICS_V2_SCHEMA_VERSION
        or candidate.schema_version != METRICS_V2_SCHEMA_VERSION
    ):
        raise EvaluationError("comparison rejected: metric comparison is metrics-v2 only")
    for field in ("schema_version", "identity_version", "disposition", "grain"):
        left = getattr(baseline, field)
        right = getattr(candidate, field)
        if left != right:
            raise EvaluationError(f"comparison rejected: {field} differs ({left!r} != {right!r})")
    left_counts = validate_count_object(baseline.counts, "baseline.counts")
    right_counts = validate_count_object(candidate.counts, "candidate.counts")
    delta: dict[str, int | None] = {}
    for field in COUNT_FIELDS:
        left = left_counts[field]
        right = right_counts[field]
        delta[field] = None if left is None or right is None else right - left
    return {
        "schema_version": baseline.schema_version,
        "identity_version": baseline.identity_version,
        "disposition": baseline.disposition,
        "grain": baseline.grain,
        "baseline": left_counts,
        "candidate": right_counts,
        "delta": delta,
    }


def compare_project_metrics(
    baseline: Any,
    candidate: Any,
    *,
    baseline_grain: str,
    baseline_disposition: str,
    candidate_grain: str | None = None,
    candidate_disposition: str | None = None,
    invalidation_reason: str | None = None,
) -> dict[str, Any]:
    """Select and compare two project surfaces, keeping both selectors explicit."""

    candidate_grain = candidate_grain or baseline_grain
    candidate_disposition = candidate_disposition or baseline_disposition
    try:
        left = select_metric(
            baseline,
            grain=baseline_grain,
            disposition=baseline_disposition,
        )
        right = select_metric(
            candidate,
            grain=candidate_grain,
            disposition=candidate_disposition,
        )
    except EvaluationError as exc:
        baseline_schema = baseline.get("schema_version") if isinstance(baseline, Mapping) else None
        candidate_schema = (
            candidate.get("schema_version") if isinstance(candidate, Mapping) else None
        )
        if baseline_schema != candidate_schema:
            raise EvaluationError(
                f"comparison rejected: schema_version differs "
                f"({baseline_schema!r} != {candidate_schema!r})"
            ) from exc
        raise
    baseline_run = _mapping(_mapping(baseline, "baseline").get("run"), "baseline.run")
    candidate_run = _mapping(_mapping(candidate, "candidate").get("run"), "candidate.run")
    pin_fields = (
        "target_sha",
        "sag_sha",
        "prompt_hash",
        "control_bundle_hash",
        "image_digest",
        "model_pin",
        "run_order_index",
    )
    drift = {
        field: (baseline_run.get(field), candidate_run.get(field))
        for field in pin_fields
        if baseline_run.get(field) != candidate_run.get(field)
    }
    normalized_invalidation_reason = (
        invalidation_reason.strip() if isinstance(invalidation_reason, str) else ""
    )
    if drift and not normalized_invalidation_reason:
        raise EvaluationError(
            "comparison rejected: run pins drift without explicit invalidation ("
            + ", ".join(sorted(drift))
            + ")"
        )
    result = compare_metric_refs(left, right)
    if drift:
        result["invalidated"] = True
        result["invalidation_reason"] = normalized_invalidation_reason
        result["run_pin_drift"] = drift
    return result


def _sum_counts(vectors: Iterable[Mapping[str, Any]]) -> dict[str, int | None]:
    validated = [validate_count_object(vector) for vector in vectors]
    if not validated or any(vector["executed"] is None for vector in validated):
        return {field: None for field in COUNT_FIELDS}
    totals: dict[str, int | None] = {}
    for field in COUNT_FIELDS:
        total = 0
        for vector in validated:
            value = vector[field]
            assert value is not None
            total += value
        totals[field] = total
    return totals


def evaluate_v2_campaign(projects: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a project-macro-first v2 scorecard and separate diagnostic totals."""

    if not projects:
        raise EvaluationError("a v2 campaign must contain at least one project")
    validated = [
        validate_v2_project(project, f"projects[{index}]") for index, project in enumerate(projects)
    ]
    run_orders = [int(_mapping(project["run"], "run")["run_order_index"]) for project in validated]
    if len(run_orders) != len(set(run_orders)):
        raise EvaluationError("run.run_order_index must be unique within a campaign")

    approvals = sum(
        int(_mapping(project["control"], "control")["midrun_human_approvals"])
        for project in validated
    )
    if approvals:
        raise EvaluationError("autonomy invariant failed: midrun_human_approvals must equal zero")
    over_cap = []
    for index, project in enumerate(validated):
        recurrence = _mapping(project["control"], "control").get("terminal_refusal_recurrences")
        if isinstance(recurrence, int) and recurrence > MAX_TERMINAL_REFUSAL_RECURRENCES:
            over_cap.append(index)
    if over_cap:
        raise EvaluationError(
            "terminal refusal recurrence overflow in project rows "
            + ", ".join(str(index) for index in over_cap)
        )

    project_count = len(validated)
    verdict_distribution = {verdict: 0 for verdict in VERDICTS}
    subject_pass_rates: list[float] = []
    evidence_complete = 0
    all_domains_terminal = 0
    zero_unsettled_and_no_overflow = 0

    for project in validated:
        outcome = _mapping(project["outcome"], "outcome")
        verdict_distribution[str(outcome["verdict"])] += 1
        evidence = _mapping(project["evidence"], "evidence")
        evidence_complete += int(evidence["integrity"] == "complete")
        coverage = _mapping(project["coverage"], "coverage")
        all_domains_terminal += int(
            isinstance(coverage.get("domains_terminal"), int)
            and isinstance(coverage.get("domains_discovered"), int)
            and coverage["domains_terminal"] == coverage["domains_discovered"]
        )
        control = _mapping(project["control"], "control")
        unsettled = control.get("unsettled_jobs")
        recurrence = control.get("terminal_refusal_recurrences")
        zero_unsettled_and_no_overflow += int(
            isinstance(unsettled, int)
            and unsettled == 0
            and isinstance(recurrence, int)
            and recurrence <= MAX_TERMINAL_REFUSAL_RECURRENCES
        )
        subjects = validate_count_object(
            _mapping(_mapping(project["tests"], "tests")["claimed"], "claimed")["latest_subjects"],
            "latest_subjects",
        )
        subject_executed = subjects["executed"]
        subject_passed = subjects["passed"]
        if subject_executed not in (None, 0):
            assert subject_executed is not None
            assert subject_passed is not None
            subject_pass_rates.append(subject_passed / subject_executed)

    def rate(count: int) -> float:
        return count / project_count

    claimed_totals = {}
    for grain, field in (
        ("subjects", "latest_subjects"),
        ("cases", "latest_cases"),
        ("receipt_executions", "receipt_executions"),
    ):
        claimed_totals[grain] = _sum_counts(
            _mapping(_mapping(project["tests"], "tests")["claimed"], "claimed")[field]
            for project in validated
        )
    observation_totals = {
        disposition: _sum_counts(
            _mapping(project["tests"], "tests")[f"{disposition}_observations"]
            for project in validated
        )
        for disposition in OBSERVATION_DISPOSITIONS
    }

    return {
        "schema_version": METRICS_V2_SCHEMA_VERSION,
        "identity_version": METRICS_V2_IDENTITY_VERSION,
        "project_count": project_count,
        "project_macro": {
            "verdict_distribution": verdict_distribution,
            "evidence_complete_project_rate": rate(evidence_complete),
            "all_discovered_domains_terminal_project_rate": rate(all_domains_terminal),
            "median_project_subject_pass_rate": (
                statistics.median(subject_pass_rates) if subject_pass_rates else None
            ),
            "zero_unsettled_and_no_recurrence_overflow_project_rate": rate(
                zero_unsettled_and_no_overflow
            ),
            "autonomy_invariant": True,
        },
        "diagnostic_totals": {
            "claimed": claimed_totals,
            "observations": observation_totals,
        },
    }


def recompute_legacy_v1(payload: Any) -> dict[str, Any]:
    """Recompute the frozen v1 forensic baseline without adapting it to v2."""

    root = _mapping(payload, "legacy")
    if root.get("schema_version") != 1:
        raise EvaluationError("legacy recomputation requires schema_version=1")
    if root.get("legacy_mode") != LEGACY_V1_MODE:
        raise EvaluationError(f"legacy v1 input must explicitly set legacy_mode={LEGACY_V1_MODE!r}")
    campaigns = _sequence(root.get("campaigns"), "legacy.campaigns")
    results = []
    for campaign_index, raw_campaign in enumerate(campaigns):
        path = f"legacy.campaigns[{campaign_index}]"
        campaign = _mapping(raw_campaign, path)
        campaign_id = _nonempty_string(campaign.get("campaign_id"), f"{path}.campaign_id")
        verdict_rows = _sequence(campaign.get("verdict_rows"), f"{path}.verdict_rows")
        verdict_distribution = {verdict: 0 for verdict in VERDICTS}
        projects_seen: set[str] = set()
        for row_index, raw_row in enumerate(verdict_rows):
            row = _mapping(raw_row, f"{path}.verdict_rows[{row_index}]")
            project = _nonempty_string(
                row.get("project"), f"{path}.verdict_rows[{row_index}].project"
            )
            if project in projects_seen:
                raise EvaluationError(f"{path} repeats project {project!r}")
            projects_seen.add(project)
            verdict = _nonempty_string(
                row.get("verdict"), f"{path}.verdict_rows[{row_index}].verdict"
            )
            if verdict not in VERDICTS:
                raise EvaluationError(f"{path} has unsupported verdict {verdict!r}")
            verdict_distribution[verdict] += 1

        components = _mapping(campaign.get("count_components"), f"{path}.count_components")
        totals: dict[str, int] = {}
        for metric in ("canonical_unique", "primary_raw", "auxiliary"):
            rows = _sequence(components.get(metric), f"{path}.count_components.{metric}")
            total = 0
            for row_index, raw_row in enumerate(rows):
                row = _mapping(raw_row, f"{path}.count_components.{metric}[{row_index}]")
                _nonempty_string(row.get("source_ref"), f"{path}.{metric}[{row_index}].source_ref")
                total += _nonnegative_int(row.get("count"), f"{path}.{metric}[{row_index}].count")
            totals[metric] = total
        results.append(
            {
                "campaign_id": campaign_id,
                "project_count": len(projects_seen),
                "verdict_distribution": verdict_distribution,
                **totals,
            }
        )
    return {
        "schema_version": 1,
        "legacy_mode": LEGACY_V1_MODE,
        "explicit_legacy": True,
        "campaigns": results,
    }


def expand_seeded_report_delta(seed_fixture: Any) -> dict[str, Any]:
    """Deterministically expand the bounded Lucene large-array seed fixture."""

    seed = _mapping(seed_fixture, "seed_fixture")
    if seed.get("fixture_kind") != "seeded_report_delta":
        raise EvaluationError("seed_fixture.fixture_kind must be 'seeded_report_delta'")
    seed_value = _nonempty_string(seed.get("seed"), "seed_fixture.seed")
    entry_count = _nonnegative_int(seed.get("entry_count"), "seed_fixture.entry_count")
    root = _nonempty_string(seed.get("path_root"), "seed_fixture.path_root").rstrip("/")
    modules = [
        _nonempty_string(module, f"seed_fixture.modules[{index}]")
        for index, module in enumerate(_sequence(seed.get("modules"), "seed_fixture.modules"))
    ]
    if not modules:
        raise EvaluationError("seed_fixture.modules must not be empty")
    entries = []
    for index in range(entry_count):
        token = hashlib.sha256(f"{seed_value}:path:{index}".encode("utf-8")).hexdigest()
        payload_hash = hashlib.sha256(f"{seed_value}:payload:{index}".encode("utf-8")).hexdigest()
        module = modules[index % len(modules)].strip("/")
        entries.append(
            {
                "path": f"{root}/{module}/build/test-results/test/TEST-{token[:24]}.xml",
                "sha256": payload_hash,
            }
        )
    return {
        "terminal": _mapping(seed.get("terminal"), "seed_fixture.terminal"),
        "report_delta": {"changed": [], "new": entries},
    }


def verify_fixture_manifest(manifest_path: Path) -> dict[str, Any]:
    """Verify every bounded fixture object against its content address."""

    manifest_path = manifest_path.resolve()
    manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    if manifest.get("content_address") != "sha256":
        raise EvaluationError("manifest.content_address must be 'sha256'")
    root = manifest_path.parent.resolve()
    objects = _sequence(manifest.get("objects"), "manifest.objects")
    roles: set[str] = set()
    verified = []
    for index, raw_entry in enumerate(objects):
        entry = _mapping(raw_entry, f"manifest.objects[{index}]")
        role = _nonempty_string(entry.get("role"), f"manifest.objects[{index}].role")
        if role in roles:
            raise EvaluationError(f"manifest repeats role {role!r}")
        roles.add(role)
        relative = Path(_nonempty_string(entry.get("path"), f"manifest.objects[{index}].path"))
        object_path = (root / relative).resolve()
        if root not in object_path.parents:
            raise EvaluationError(f"fixture path escapes manifest root: {relative}")
        data = object_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected = _nonempty_string(entry.get("sha256"), f"manifest.objects[{index}].sha256")
        if digest != expected:
            raise EvaluationError(f"fixture hash mismatch for role {role!r}")
        if object_path.name.split(".", 1)[0] != f"sha256-{digest}":
            raise EvaluationError(f"fixture filename is not content-addressed for role {role!r}")
        expected_size = _nonnegative_int(entry.get("bytes"), f"manifest.objects[{index}].bytes")
        if len(data) != expected_size:
            raise EvaluationError(f"fixture byte length mismatch for role {role!r}")
        verified.append({"role": role, "sha256": digest, "bytes": len(data)})
    return {
        "fixture_set": manifest.get("fixture_set"),
        "object_count": len(verified),
        "objects": verified,
    }


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    legacy = subparsers.add_parser("legacy", help="recompute an explicitly labeled v1 fixture")
    legacy.add_argument("artifact")

    evaluate = subparsers.add_parser("evaluate", help="evaluate a v2 campaign artifact")
    evaluate.add_argument("artifact", help="JSON object with a projects array, or one project row")

    compare = subparsers.add_parser("compare", help="compare identical v2 metric surfaces")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--baseline-grain", required=True, choices=GRAINS)
    compare.add_argument(
        "--baseline-disposition",
        required=True,
        choices=("claimed", *OBSERVATION_DISPOSITIONS),
    )
    compare.add_argument("--candidate-grain", choices=GRAINS)
    compare.add_argument(
        "--candidate-disposition",
        choices=("claimed", *OBSERVATION_DISPOSITIONS),
    )

    fixtures = subparsers.add_parser("verify-fixtures", help="verify a fixture manifest")
    fixtures.add_argument("manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "legacy":
            result = recompute_legacy_v1(_load_json(args.artifact))
        elif args.command == "evaluate":
            payload = _load_json(args.artifact)
            if isinstance(payload, Mapping) and "projects" in payload:
                projects = _sequence(payload["projects"], "projects")
            else:
                projects = [payload]
            result = evaluate_v2_campaign(projects)  # type: ignore[arg-type]
        elif args.command == "compare":
            result = compare_project_metrics(
                _load_json(args.baseline),
                _load_json(args.candidate),
                baseline_grain=args.baseline_grain,
                baseline_disposition=args.baseline_disposition,
                candidate_grain=args.candidate_grain,
                candidate_disposition=args.candidate_disposition,
            )
        else:
            result = verify_fixture_manifest(Path(args.manifest))
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        print(f"golden-battery evaluation failed: {exc}", file=sys.stderr)
        return 2
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
