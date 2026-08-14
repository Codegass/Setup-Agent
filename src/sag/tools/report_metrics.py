"""Metrics-v2 writer and explicitly typed legacy report-metrics reader.

The forward artifact is a project decision surface, not a convenient bag of
aliases.  Subject, parameter-aware case, receipt-execution, and report-
observation counts stay in separate branches.  In particular, an aggregate
whose module-qualified identity was not sealed is rendered as unavailable; it
is never renamed to a subject or case count.

Historical ``version: 1`` artifacts remain readable through
``LegacyReportMetricsV1`` for forensic display.  That type cannot be mistaken
for a metrics-v2 mapping and no adapter synthesizes v2 identities from it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sag.agent.receipt_test_rows import (
    ROW_ENVELOPE_VERSION,
    TestcaseRowContractError,
    aggregate_testcase_execution_rows,
    validate_testcase_execution_row,
)

METRICS_PATH = "/workspace/.setup_agent/report_metrics.json"
REPORT_METRICS_LOGICAL_ARTIFACT_ID = "workspace-report-metrics"
METRICS_SCHEMA_VERSION = 2
METRICS_IDENTITY_VERSION = "module-qualified-v1"
COUNT_FIELDS = ("executed", "passed", "failed", "errors", "skipped")
OBSERVATION_DISPOSITIONS = ("quarantined", "unattributed", "stale")
TEST_OUTCOMES = ("passed", "failed", "error", "skipped")
_OUTCOME_FIELD = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "skipped": "skipped",
}
FORWARD_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "identity_version",
        "run",
        "outcome",
        "evidence",
        "tests",
        "coverage",
        "control",
    }
)
_RUN_FIELDS = (
    "run_id",
    "target_sha",
    "sag_sha",
    "prompt_hash",
    "control_bundle_hash",
    "image_digest",
    "model_pin",
    "run_order_index",
)
_RUN_SURFACE_FIELDS = frozenset((*_RUN_FIELDS, "pin_status", "missing_pins"))
_OUTCOME_FIELDS = frozenset({"verdict", "build_state", "test_state", "terminal_reason"})
_EVIDENCE_FIELDS = frozenset(
    {
        "integrity",
        "receipts_expected",
        "receipts_persisted",
        "terminal_receipts_unpersisted",
        "conflict_count",
    }
)
_CLAIMED_FIELDS = frozenset({"latest_subjects", "latest_cases", "receipt_executions"})
_TEST_FIELDS = frozenset(
    {
        "claimed",
        "quarantined_observations",
        "unattributed_observations",
        "stale_observations",
        "retried_cases",
        "flaky_cases",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "domains_discovered",
        "domains_attempted",
        "domains_terminal",
        "domains_with_claimed_tests",
    }
)
_CONTROL_FIELDS = frozenset(
    {
        "terminal_refusal_recurrences",
        "unsettled_jobs",
        "cleanup_escalations",
        "midrun_human_approvals",
    }
)
_COUNT_FIELDS_SET = frozenset(COUNT_FIELDS)
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_HEX_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class MetricsContractError(ValueError):
    """The requested projection would make a v2 artifact dishonest."""


@dataclass(frozen=True)
class LegacyReportMetricsV1:
    """A historical v1 artifact, kept outside the forward writer type."""

    payload: Mapping[str, Any]
    schema_family: str = "legacy-v1-forensic"


def _require_exact_fields(
    value: Any,
    expected: frozenset[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise MetricsContractError(f"{label} fields are not the strict metrics-v2 schema")
    return value


def _strict_nonnegative_int_or_none(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MetricsContractError(f"{label} must be a non-negative strict integer or null")
    return value


def _strict_nonempty_text(value: Any, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise MetricsContractError(f"{label} must be non-empty text")
    return value


def _validate_count_bucket(
    value: Any,
    *,
    label: str,
    observation: bool = False,
) -> None:
    if not isinstance(value, Mapping):
        raise MetricsContractError(f"{label} must be an object")
    availability = value.get("availability")
    if availability == "available":
        expected = _COUNT_FIELDS_SET | {"availability", "basis"}
    elif availability == "unavailable":
        expected = _COUNT_FIELDS_SET | {"availability", "reason"}
    else:
        raise MetricsContractError(f"{label}.availability is invalid")
    if observation:
        expected |= {"report_file_count", "reason_counts"}
    if set(value) != expected:
        raise MetricsContractError(f"{label} fields are not canonical")

    if availability == "available":
        counts: dict[str, int] = {}
        for field in COUNT_FIELDS:
            item = _strict_nonnegative_int_or_none(value.get(field), label=f"{label}.{field}")
            if item is None:
                raise MetricsContractError(f"{label} available counts cannot be null")
            counts[field] = item
        if counts["executed"] != sum(counts[field] for field in COUNT_FIELDS[1:]):
            raise MetricsContractError(f"{label} executed count is inconsistent")
        _strict_nonempty_text(value.get("basis"), label=f"{label}.basis")
    else:
        if any(value.get(field) is not None for field in COUNT_FIELDS):
            raise MetricsContractError(f"{label} unavailable counts must be null")
        _strict_nonempty_text(value.get("reason"), label=f"{label}.reason")

    if observation:
        _strict_nonnegative_int_or_none(
            value.get("report_file_count"), label=f"{label}.report_file_count"
        )
        reason_counts = value.get("reason_counts")
        if reason_counts is not None:
            if not isinstance(reason_counts, Mapping) or any(
                type(reason) is not str or not reason.strip() or type(count) is not int or count < 0
                for reason, count in reason_counts.items()
            ):
                raise MetricsContractError(f"{label}.reason_counts is invalid")
            executed = value.get("executed")
            if isinstance(executed, int) and sum(reason_counts.values()) not in {0, executed}:
                raise MetricsContractError(f"{label}.reason_counts does not reconcile")


def validate_report_metrics_v2(payload: Any) -> dict[str, Any]:
    """Validate the complete forward metrics schema without publication trust.

    This is the semantic half of the live boundary.  It intentionally accepts
    only the exact metrics-v2 shape emitted by :func:`assemble_report_metrics`:
    unknown future fields and versions fail closed instead of being silently
    projected into campaign KPIs.
    """

    top = _require_exact_fields(payload, FORWARD_TOP_LEVEL_FIELDS, label="report metrics")
    if top.get("schema_version") != METRICS_SCHEMA_VERSION:
        raise MetricsContractError("report metrics schema version is not supported")
    if top.get("identity_version") != METRICS_IDENTITY_VERSION:
        raise MetricsContractError("report metrics identity version is not supported")

    run = _require_exact_fields(top.get("run"), _RUN_SURFACE_FIELDS, label="report metrics run")
    run_id = run.get("run_id")
    if run_id is not None and (type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None):
        raise MetricsContractError("report metrics run id is invalid")
    for field in ("target_sha", "sag_sha"):
        value = run.get(field)
        if value is not None and (type(value) is not str or _HEX_SHA_RE.fullmatch(value) is None):
            raise MetricsContractError(f"report metrics {field} is invalid")
    for field in ("prompt_hash", "control_bundle_hash"):
        value = run.get(field)
        if value is not None and (type(value) is not str or _SHA256_RE.fullmatch(value) is None):
            raise MetricsContractError(f"report metrics {field} is invalid")
    image_digest = run.get("image_digest")
    if image_digest is not None and (
        type(image_digest) is not str or _IMAGE_DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise MetricsContractError("report metrics image digest is invalid")
    model_pin = run.get("model_pin")
    if model_pin is not None:
        _strict_nonempty_text(model_pin, label="report metrics model pin")
    _strict_nonnegative_int_or_none(
        run.get("run_order_index"), label="report metrics run order index"
    )
    missing_pins = run.get("missing_pins")
    expected_missing = [field for field in _RUN_FIELDS if run.get(field) is None]
    if (
        not isinstance(missing_pins, list)
        or any(type(field) is not str for field in missing_pins)
        or missing_pins != expected_missing
    ):
        raise MetricsContractError("report metrics missing-pins projection is inconsistent")
    expected_pin_status = "complete" if not expected_missing else "incomplete"
    if run.get("pin_status") != expected_pin_status:
        raise MetricsContractError("report metrics pin status is inconsistent")

    outcome = _require_exact_fields(
        top.get("outcome"), _OUTCOME_FIELDS, label="report metrics outcome"
    )
    if outcome.get("verdict") not in {"success", "partial", "failed", "unknown"}:
        raise MetricsContractError("report metrics verdict is invalid")
    for field in ("build_state", "test_state", "terminal_reason"):
        _strict_nonempty_text(outcome.get(field), label=f"report metrics outcome {field}")

    evidence = _require_exact_fields(
        top.get("evidence"), _EVIDENCE_FIELDS, label="report metrics evidence"
    )
    if evidence.get("integrity") not in {"complete", "degraded", "failed", "unavailable"}:
        raise MetricsContractError("report metrics evidence integrity is invalid")
    expected_receipts = _strict_nonnegative_int_or_none(
        evidence.get("receipts_expected"), label="report metrics receipts expected"
    )
    persisted_receipts = _strict_nonnegative_int_or_none(
        evidence.get("receipts_persisted"), label="report metrics receipts persisted"
    )
    unpersisted_receipts = _strict_nonnegative_int_or_none(
        evidence.get("terminal_receipts_unpersisted"),
        label="report metrics terminal receipts unpersisted",
    )
    conflict_count = _strict_nonnegative_int_or_none(
        evidence.get("conflict_count"), label="report metrics conflict count"
    )
    if conflict_count is None:
        raise MetricsContractError("report metrics conflict count cannot be null")
    if (
        expected_receipts is not None
        and persisted_receipts is not None
        and unpersisted_receipts is not None
        and expected_receipts < persisted_receipts + unpersisted_receipts
    ):
        raise MetricsContractError("report metrics receipt accounting is inconsistent")
    if outcome.get("verdict") == "success" and evidence.get("integrity") != "complete":
        raise MetricsContractError("success report metrics require complete evidence integrity")

    tests = _require_exact_fields(top.get("tests"), _TEST_FIELDS, label="report metrics tests")
    claimed = _require_exact_fields(
        tests.get("claimed"), _CLAIMED_FIELDS, label="report metrics claimed tests"
    )
    for field in _CLAIMED_FIELDS:
        _validate_count_bucket(claimed.get(field), label=f"report metrics claimed {field}")
    for field in (
        "quarantined_observations",
        "unattributed_observations",
        "stale_observations",
    ):
        _validate_count_bucket(tests.get(field), label=f"report metrics {field}", observation=True)
    for field in ("retried_cases", "flaky_cases"):
        _strict_nonnegative_int_or_none(tests.get(field), label=f"report metrics {field}")

    coverage = _require_exact_fields(
        top.get("coverage"), _COVERAGE_FIELDS, label="report metrics coverage"
    )
    for field in _COVERAGE_FIELDS:
        _strict_nonnegative_int_or_none(coverage.get(field), label=f"report metrics {field}")
    control = _require_exact_fields(
        top.get("control"), _CONTROL_FIELDS, label="report metrics control"
    )
    for field in _CONTROL_FIELDS:
        _strict_nonnegative_int_or_none(control.get(field), label=f"report metrics {field}")
    return dict(top)


def read_report_metrics(payload: Any) -> dict[str, Any] | LegacyReportMetricsV1 | None:
    """Forensic payload parser; this does not establish live authority.

    Historical tooling may inspect its result, but verdict, KPI and evaluator
    consumers must use :func:`read_live_report_metrics` so valid-looking
    container bytes cannot substitute for a host publication.
    """

    if not isinstance(payload, Mapping):
        return None
    copied = dict(payload)
    if copied.get("schema_version") == METRICS_SCHEMA_VERSION:
        try:
            return validate_report_metrics_v2(copied)
        except (TypeError, ValueError):
            return None
    if copied.get("version") == 1 and "schema_version" not in copied:
        return LegacyReportMetricsV1(payload=copied)
    return None


def read_live_report_metrics(source: Any):
    """Read the fixed metrics artifact at the exact current host revision."""

    from sag.agent.evidence_records import (
        EvidencePublicationBinding,
        read_live_published_mutable_json_object,
    )

    def validate(payload: Mapping[str, Any], expected_id: str) -> Mapping[str, Any]:
        if expected_id != REPORT_METRICS_LOGICAL_ARTIFACT_ID:
            raise MetricsContractError("report metrics publication identity is invalid")
        return validate_report_metrics_v2(payload)

    def publication_binding(payload: Mapping[str, Any]) -> EvidencePublicationBinding:
        run = payload["run"]
        run_id = run.get("run_id") if isinstance(run, Mapping) else None
        return EvidencePublicationBinding(
            run_id=run_id if isinstance(run_id, str) else None,
            logical_artifact_id=REPORT_METRICS_LOGICAL_ARTIFACT_ID,
        )

    return read_live_published_mutable_json_object(
        source,
        METRICS_PATH,
        record_kind="report_metrics",
        record_id=REPORT_METRICS_LOGICAL_ARTIFACT_ID,
        validator=validate,
        publication_binding=publication_binding,
    )


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _all_null_counts(*, reason: str) -> dict[str, Any]:
    return {
        **{field: None for field in COUNT_FIELDS},
        "availability": "unavailable",
        "reason": reason,
    }


def _known_counts(
    value: Mapping[str, Any] | None,
    *,
    basis: str,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    aliases = {
        "executed": ("executed", "total", "total_tests"),
        "passed": ("passed", "passed_tests"),
        "failed": ("failed", "failed_tests"),
        "errors": ("errors", "error", "error_tests"),
        "skipped": ("skipped", "skipped_tests"),
    }
    counts: dict[str, int] = {}
    for field, names in aliases.items():
        item = None
        for name in names:
            if name in value:
                item = _int_or_none(value.get(name))
                break
        if item is None:
            return None
        counts[field] = item
    if counts["executed"] != sum(counts[field] for field in COUNT_FIELDS[1:]):
        raise MetricsContractError(
            f"{basis} executed must equal passed + failed + errors + skipped"
        )
    return {**counts, "availability": "available", "basis": basis}


def _excluded_volume(value: Mapping[str, Any] | None, *, basis: str) -> dict[str, Any] | None:
    """One excluded destination's volume — measured, unmeasurable, or absent.

    The excluded partitions carry an ``unparseable`` marker for reports whose
    volume could not be measured at all. A report the parser could not open
    contributes zero executions, so a destination holding nothing BUT such
    reports used to publish ``executed: 0`` — a measured outcome for bytes
    nobody measured, and indistinguishable from a destination where reports
    existed and genuinely ran nothing. Zero is a count; an unreadable report
    has none.
    """
    if not isinstance(value, Mapping):
        return None
    counts = _known_counts(value, basis=basis)
    if counts is None:
        return None
    unparseable = _int_or_none(value.get("unparseable")) or 0
    if unparseable and counts["executed"] == 0:
        return _all_null_counts(
            reason=f"{unparseable} excluded report(s) could not be parsed; volume unmeasured"
        )
    return counts


def _zero_counts(*, basis: str) -> dict[str, Any]:
    return {
        **{field: 0 for field in COUNT_FIELDS},
        "availability": "available",
        "basis": basis,
    }


def _count_outcomes(outcomes: Sequence[str], *, basis: str) -> dict[str, Any]:
    counts = {field: 0 for field in COUNT_FIELDS}
    for outcome in outcomes:
        if outcome not in TEST_OUTCOMES:
            raise MetricsContractError(f"unsupported receipt testcase outcome {outcome!r}")
        counts["executed"] += 1
        counts[_OUTCOME_FIELD[outcome]] += 1
    return {**counts, "availability": "available", "basis": basis}


def _nonempty_text(value: Any) -> str:
    return str(value or "").strip()


def _receipt_report_entries(receipt: Mapping[str, Any]) -> list[Any]:
    delta = receipt.get("report_delta")
    if not isinstance(delta, Mapping):
        return []
    entries: list[Any] = []
    for bucket in ("new", "changed", "cached"):
        values = delta.get(bucket)
        if values is None:
            continue
        if not isinstance(values, list):
            entries.append(None)
            continue
        entries.extend(values)
    return entries


def _decorate_claimed_aggregation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add presentation metadata to the shared raw aggregation contract."""

    claimed = value.get("claimed")
    if not isinstance(claimed, Mapping):
        raise MetricsContractError("shared testcase aggregation omitted claimed counts")
    bases = {
        "latest_subjects": "latest module-qualified subjects",
        "latest_cases": "latest parameter-aware cases",
        "receipt_executions": "module-qualified receipt execution rows",
    }
    decorated: dict[str, Any] = {}
    for name, basis in bases.items():
        counts = claimed.get(name)
        if not isinstance(counts, Mapping):
            raise MetricsContractError(f"shared testcase aggregation omitted {name}")
        decorated[name] = {**dict(counts), "availability": "available", "basis": basis}
    return {
        **decorated,
        "retried_cases": _int_or_none(value.get("retried_cases")),
        "flaky_cases": _int_or_none(value.get("flaky_cases")),
    }


def _receipt_row_projection(
    receipts: Sequence[Mapping[str, Any]] | None,
    *,
    run_id: Optional[str],
    run_target_sha: Optional[str],
) -> dict[str, Any] | None:
    """Classify only envelopes bound to this exact run and target.

    A reused container may retain receipts for the same checkout SHA.  Those
    are neither retries nor stale observations in this run, so they are
    ignored before any envelope or row can influence the projection.
    """

    active_run = _nonempty_text(run_id)
    target = _nonempty_text(run_target_sha)
    if receipts is None:
        return {
            "claimed": None,
            "identity_complete": False,
            "current_receipt_seen": False,
            "unattributed": True,
            "stale": None,
            "ledger_unavailable": True,
        }
    if not receipts or not active_run or not target:
        return None
    current_rows: list[Mapping[str, Any]] = []
    stale_rows: list[Mapping[str, Any]] = []
    current_report_receipt_seen = False
    current_complete = True
    row_envelope_seen = False
    unattributed = False
    foreign_run_seen = False
    stale_files: set[str] = set()

    for receipt in receipts:
        receipt_run = _nonempty_text(receipt.get("run_id"))
        if receipt_run != active_run:
            # Includes schema-v2 receipts from an older run and every legacy
            # receipt that predates run identity. Neither may be promoted.
            foreign_run_seen = True
            continue
        entries = _receipt_report_entries(receipt)
        envelope = receipt.get("testcase_execution_rows")
        receipt_id = _nonempty_text(receipt.get("receipt_id"))
        receipt_target = _nonempty_text(receipt.get("target_sha"))
        receipt_domain = _nonempty_text(receipt.get("domain_id"))
        report_claims = {
            (_nonempty_text(entry.get("path")), _nonempty_text(entry.get("sha256")).lower())
            for entry in entries
            if isinstance(entry, Mapping)
        }
        claims_valid = len(report_claims) == len(entries) and all(
            path and re.fullmatch(r"[0-9a-f]{64}", digest) for path, digest in report_claims
        )
        if not entries:
            if receipt.get("schema_version") == 2 and receipt_target == target:
                # A current v2 receipt with no report delta is known to exist,
                # but without a sealed row envelope it cannot authorize the
                # snapshot's aggregate receipt-scoped counts.
                current_report_receipt_seen = True
                current_complete = False
            if isinstance(envelope, Mapping):
                row_envelope_seen = True
                if receipt_target == target:
                    current_report_receipt_seen = True
                    current_complete = False
                else:
                    unattributed = True
            continue
        if not receipt_target:
            unattributed = True
            current_complete = False
            continue
        is_current = receipt_target == target
        if is_current:
            current_report_receipt_seen = True
        if not isinstance(envelope, Mapping):
            if is_current:
                current_complete = False
            continue
        row_envelope_seen = True
        report_count = envelope.get("report_count")
        if (
            receipt.get("schema_version") != 2
            or envelope.get("schema_version") != ROW_ENVELOPE_VERSION
            or envelope.get("status") != "complete"
            or not receipt_id
            or not receipt_domain
            or not claims_valid
            or isinstance(report_count, bool)
            or not isinstance(report_count, int)
            or report_count != len(report_claims)
        ):
            if is_current:
                current_complete = False
            else:
                unattributed = True
            continue
        raw_rows = envelope.get("rows")
        if not isinstance(raw_rows, list):
            if is_current:
                current_complete = False
            continue
        try:
            validated = [
                validate_testcase_execution_row(
                    row,
                    receipt_id=receipt_id,
                    run_id=active_run,
                    target_sha=receipt_target,
                    domain_id=receipt_domain,
                    report_claims=report_claims,
                )
                for row in raw_rows
                if isinstance(row, Mapping)
            ]
            if len(validated) != len(raw_rows):
                raise MetricsContractError("receipt testcase envelope contains a non-object row")
        except TestcaseRowContractError:
            if is_current:
                current_complete = False
            else:
                unattributed = True
            continue
        if is_current:
            current_rows.extend(validated)
        else:
            stale_rows.extend(validated)
            stale_files.update(str(row["report_path"]) for row in validated)

    if not (current_report_receipt_seen or row_envelope_seen or unattributed or foreign_run_seen):
        return None
    claimed = None
    if current_report_receipt_seen and current_complete:
        try:
            claimed = _decorate_claimed_aggregation(aggregate_testcase_execution_rows(current_rows))
        except TestcaseRowContractError:
            current_complete = False
    stale = (
        _observation_bucket(
            _count_outcomes(
                [str(row["outcome"]) for row in stale_rows],
                basis="receipt rows from another target sha",
            ),
            report_file_count=len(stale_files),
            reason="target_sha_mismatch",
        )
        if stale_rows
        else None
    )
    return {
        "claimed": claimed,
        "identity_complete": claimed is not None,
        "current_receipt_seen": current_report_receipt_seen,
        "unattributed": (
            unattributed
            or foreign_run_seen
            or (current_report_receipt_seen and not current_complete)
        ),
        "foreign_run_seen": foreign_run_seen,
        "stale": stale,
    }


def _observation_bucket(
    counts: Mapping[str, Any],
    *,
    report_file_count: int | None,
    reason: str | None = None,
) -> dict[str, Any]:
    result = dict(counts)
    result["report_file_count"] = report_file_count
    executed = _int_or_none(result.get("executed"))
    result["reason_counts"] = (
        {reason: executed} if reason and executed is not None else ({} if executed == 0 else None)
    )
    return result


def _canonical_snapshot(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = snapshot.get("canonical_snapshot")
    return nested if isinstance(nested, Mapping) else snapshot


def _snapshot_test_facts(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_snapshot(snapshot)
    test_stats = canonical.get("test_stats")
    if isinstance(test_stats, Mapping):
        unique = _known_counts(
            test_stats.get("unique") if isinstance(test_stats.get("unique"), Mapping) else None,
            basis="sealed aggregate latest identities",
        )
        raw = _known_counts(
            test_stats.get("raw") if isinstance(test_stats.get("raw"), Mapping) else None,
            basis="sealed report rows",
        )
        return {
            "unique": unique,
            "raw": raw,
            "receipt_scoped": test_stats.get("receipt_scoped") is True,
            "auxiliary": _excluded_volume(
                (
                    test_stats.get("auxiliary_test_stats")
                    if isinstance(test_stats.get("auxiliary_test_stats"), Mapping)
                    else None
                ),
                basis="reports outside qualifying receipt claims",
            ),
            "stale_reports": [
                str(path)
                for path in (test_stats.get("stale_test_reports") or ())
                if str(path).strip()
            ],
            "stale": _excluded_volume(
                (
                    test_stats.get("stale_test_stats")
                    if isinstance(test_stats.get("stale_test_stats"), Mapping)
                    else None
                ),
                basis="reports whose receipt claim was superseded",
            ),
            "flaky": _int_or_none(test_stats.get("flaky_count")),
        }

    status = snapshot.get("status")
    status = status if isinstance(status, Mapping) else {}
    unique = _known_counts(
        {
            "executed": status.get("tests_unique", status.get("tests_total")),
            "passed": status.get("tests_passed_unique", status.get("tests_passed")),
            "failed": status.get("tests_failed_unique", status.get("tests_failed")),
            "errors": status.get("tests_errors_unique", status.get("tests_errors")),
            "skipped": status.get("tests_skipped_unique", status.get("tests_skipped")),
        },
        basis="legacy report aggregate",
    )
    raw = _known_counts(
        {
            "executed": status.get("tests_total_raw"),
            "passed": status.get("tests_passed_raw"),
            "failed": status.get("tests_failed_raw"),
            "errors": status.get("tests_errors_raw"),
            "skipped": status.get("tests_skipped_raw"),
        },
        basis="legacy report rows",
    )
    return {
        "unique": unique,
        "raw": raw,
        "receipt_scoped": False,
        "auxiliary": None,
        "stale_reports": [],
        "stale": None,
        "flaky": _int_or_none(status.get("tests_flaky")),
    }


def _project_tests(
    snapshot: Mapping[str, Any],
    test_analysis: Mapping[str, Any],
    *,
    receipt_records: Sequence[Mapping[str, Any]] | None = (),
    run_id: Optional[str] = None,
    run_target_sha: Optional[str] = None,
) -> dict[str, Any]:
    facts = _snapshot_test_facts(snapshot)
    identity_reason = "module-qualified subject/case identity was not sealed"
    row_projection = _receipt_row_projection(
        receipt_records,
        run_id=run_id,
        run_target_sha=run_target_sha,
    )
    raw_candidate = facts["raw"]
    raw = (
        raw_candidate
        if isinstance(raw_candidate, Mapping) and raw_candidate.get("executed")
        else facts["unique"]
    )
    receipt_scoped = facts["receipt_scoped"]
    if receipt_scoped and raw is not None:
        receipt_executions = {
            **{field: raw[field] for field in COUNT_FIELDS},
            "availability": "available",
            "basis": "receipt-scoped report rows",
        }
        unattributed = _observation_bucket(
            _zero_counts(basis="receipt scoping covered all primary report rows"),
            report_file_count=0,
        )
    else:
        receipt_executions = _all_null_counts(
            reason="qualifying receipt execution identity was not sealed"
        )
        if raw is None:
            unattributed = _observation_bucket(
                _all_null_counts(reason="unscoped report observations were not measured"),
                report_file_count=None,
            )
        else:
            unattributed = _observation_bucket(
                {
                    **{field: raw[field] for field in COUNT_FIELDS},
                    "availability": "available",
                    "basis": "unscoped report rows",
                },
                report_file_count=_int_or_none(test_analysis.get("report_file_count")),
                reason="receipt_scope_unavailable",
            )

    auxiliary = facts["auxiliary"]
    auxiliary_files = test_analysis.get("auxiliary_report_files")
    auxiliary_file_count = len(auxiliary_files) if isinstance(auxiliary_files, list) else None
    if auxiliary is not None:
        quarantined = _observation_bucket(
            auxiliary,
            report_file_count=auxiliary_file_count,
            reason="outside_qualifying_receipt_claim",
        )
    elif receipt_scoped:
        quarantined = _observation_bucket(
            _zero_counts(basis="receipt-scoped report scan"),
            report_file_count=0,
        )
    else:
        quarantined = _observation_bucket(
            _all_null_counts(reason="receipt scope unavailable"),
            report_file_count=None,
        )

    stale_reports = facts["stale_reports"]
    stale_counts = facts["stale"]
    row_stale = row_projection.get("stale") if isinstance(row_projection, Mapping) else None
    if stale_reports:
        row_stale_files = (
            _int_or_none(row_stale.get("report_file_count"))
            if isinstance(row_stale, Mapping)
            else 0
        )
        stale = _observation_bucket(
            # The volume is stated when the partition measured it, and only
            # then: a superseded claim that named paths alone dropped its
            # executions out of every disclosure, but a seal that never counted
            # them is not a licence to invent an outcome for them. Reports the
            # parser could not open are unmeasured here too, never a measured
            # zero — `_excluded_volume` draws that line.
            stale_counts
            if stale_counts is not None
            else _all_null_counts(reason="stale report outcomes were not counted by this seal"),
            report_file_count=len(set(stale_reports)) + (row_stale_files or 0),
            reason="receipt_claim_superseded" if stale_counts is not None else None,
        )
    elif isinstance(row_stale, Mapping):
        stale = dict(row_stale)
    elif receipt_scoped:
        stale = _observation_bucket(
            _zero_counts(basis="receipt hash validation"),
            report_file_count=0,
        )
    else:
        stale = _observation_bucket(
            _all_null_counts(reason="receipt scope unavailable"),
            report_file_count=None,
        )

    latest_subjects = _all_null_counts(reason=identity_reason)
    latest_cases = _all_null_counts(reason=identity_reason)
    retried_cases = None
    flaky_cases = None
    if isinstance(row_projection, Mapping):
        claimed_rows = row_projection.get("claimed")
        if isinstance(claimed_rows, Mapping):
            latest_subjects = dict(claimed_rows["latest_subjects"])
            latest_cases = dict(claimed_rows["latest_cases"])
            receipt_executions = dict(claimed_rows["receipt_executions"])
            retried_cases = _int_or_none(claimed_rows.get("retried_cases"))
            flaky_cases = _int_or_none(claimed_rows.get("flaky_cases"))
        elif row_projection.get("current_receipt_seen") is True:
            receipt_executions = _all_null_counts(
                reason="current receipt testcase rows were unavailable"
            )
        elif row_projection.get("current_receipt_seen") is not True:
            receipt_executions = (
                _all_null_counts(reason="receipt target binding was unavailable")
                if row_projection.get("unattributed") is True
                else _zero_counts(basis="no receipt rows matched the current target sha")
            )
        if row_projection.get("unattributed") is True:
            unattributed = _observation_bucket(
                _all_null_counts(
                    reason=(
                        "invocation receipt ledger was unavailable"
                        if row_projection.get("ledger_unavailable") is True
                        else (
                            "receipt rows belonged to a different run epoch"
                            if row_projection.get("foreign_run_seen") is True
                            else "one or more receipt rows lacked complete module-qualified identity"
                        )
                    )
                ),
                report_file_count=None,
            )
        elif isinstance(claimed_rows, Mapping):
            unattributed = _observation_bucket(
                _zero_counts(basis="all current-target receipt rows were identity-sealed"),
                report_file_count=0,
            )

    return {
        "claimed": {
            "latest_subjects": latest_subjects,
            "latest_cases": latest_cases,
            "receipt_executions": receipt_executions,
        },
        "quarantined_observations": quarantined,
        "unattributed_observations": unattributed,
        "stale_observations": stale,
        "retried_cases": retried_cases,
        "flaky_cases": flaky_cases,
    }


def _run_surface(run_pin: Mapping[str, Any]) -> dict[str, Any]:
    control_material = {
        "sanitized_config": run_pin.get("sanitized_config"),
        "feature_flags": run_pin.get("feature_flags"),
        "random_seed_or_null": run_pin.get("random_seed_or_null"),
        "dependency_cache_state": run_pin.get("dependency_cache_state"),
        "host_arch": run_pin.get("host_arch"),
    }
    control_hash = (
        _canonical_sha256(control_material)
        if (
            isinstance(control_material["sanitized_config"], Mapping)
            and isinstance(control_material["feature_flags"], Mapping)
            and all(
                field in run_pin
                for field in (
                    "random_seed_or_null",
                    "dependency_cache_state",
                    "host_arch",
                )
            )
            and isinstance(control_material["dependency_cache_state"], str)
            and isinstance(control_material["host_arch"], str)
        )
        else None
    )
    thinking = run_pin.get("thinking_model")
    action = run_pin.get("action_model")
    model_pin = (
        f"thinking={thinking};action={action}"
        if isinstance(thinking, str) and thinking and isinstance(action, str) and action
        else None
    )
    surface = {
        "run_id": run_pin.get("run_id"),
        "target_sha": run_pin.get("target_repo_sha"),
        "sag_sha": run_pin.get("sag_git_sha"),
        "prompt_hash": run_pin.get("prompt_bundle_sha256"),
        "control_bundle_hash": control_hash,
        "image_digest": run_pin.get("container_image_digest"),
        "model_pin": model_pin,
        "run_order_index": _int_or_none(run_pin.get("run_order_index")),
    }
    missing = [field for field, value in surface.items() if value is None]
    return {
        **surface,
        "pin_status": "complete" if not missing else "incomplete",
        "missing_pins": missing,
    }


def _outcome_surface(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_snapshot(snapshot)
    verdict = canonical.get("verdict")
    if not isinstance(verdict, str):
        status = snapshot.get("status")
        verdict = (
            (status.get("verdict") or status.get("overall"))
            if isinstance(status, Mapping)
            else None
        )
    build = canonical.get("build_evidence")
    build = build if isinstance(build, Mapping) else {}
    tests = canonical.get("test_stats")
    tests = tests if isinstance(tests, Mapping) else {}
    status = snapshot.get("status")
    status = status if isinstance(status, Mapping) else {}
    return {
        "verdict": verdict if verdict in {"success", "partial", "failed", "unknown"} else "unknown",
        "build_state": str(build.get("judgment") or status.get("overall") or "unavailable"),
        "test_state": str(tests.get("judgment") or status.get("overall") or "unavailable"),
        "terminal_reason": str(canonical.get("close_reason") or "evidence_close_unavailable"),
    }


def _evidence_surface(
    conflicts: List[str],
    persistence: Mapping[str, Any],
) -> dict[str, Any]:
    persisted = _int_or_none(persistence.get("receipts_persisted"))
    unpersisted = _int_or_none(persistence.get("terminal_receipts_unpersisted"))
    conflict_unpersisted = {
        conflict
        for conflict in conflicts
        if conflict.startswith("job_terminal_unpersisted:")
        or conflict.startswith("terminal_receipt_unpersisted:")
    }
    if unpersisted is None and conflict_unpersisted:
        unpersisted = len(conflict_unpersisted)
    expected = _int_or_none(persistence.get("receipts_expected"))
    if expected is None and persisted is not None and unpersisted is not None:
        expected = persisted + unpersisted

    failed_codes = {"output_storage_failed", "snapshot_persistence_failed"}
    if failed_codes.intersection(conflicts):
        integrity = "failed"
    elif (isinstance(unpersisted, int) and unpersisted > 0) or any(
        "unpersisted" in conflict for conflict in conflicts
    ):
        integrity = "degraded"
    elif (
        isinstance(expected, int)
        and isinstance(persisted, int)
        and isinstance(unpersisted, int)
        and persisted + unpersisted < expected
    ):
        integrity = "degraded"
    elif persisted is None or expected is None or unpersisted is None:
        integrity = "unavailable"
    else:
        integrity = "complete"
    return {
        "integrity": integrity,
        "receipts_expected": expected,
        "receipts_persisted": persisted,
        "terminal_receipts_unpersisted": unpersisted,
        "conflict_count": len(set(conflicts)),
    }


def _coverage_surface(snapshot: Mapping[str, Any], tests: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_snapshot(snapshot)
    build = canonical.get("build_evidence")
    build = build if isinstance(build, Mapping) else {}
    domain_states = build.get("domain_states")
    states = []
    if isinstance(domain_states, Mapping):
        for value in domain_states.values():
            if isinstance(value, Mapping):
                states.append(str(value.get("state") or "unknown"))
    elif build.get("observed") is True:
        states = [str(build.get("judgment") or "unknown")]

    if not states:
        return {
            "domains_discovered": None,
            "domains_attempted": None,
            "domains_terminal": None,
            "domains_with_claimed_tests": None,
        }
    discovered = len(states)
    attempted = sum(state != "untried" for state in states)
    terminal = sum(state in {"success", "partial", "failed", "blocked"} for state in states)
    claimed_executed = (
        tests.get("claimed", {}).get("receipt_executions", {}).get("executed")
        if isinstance(tests.get("claimed"), Mapping)
        else None
    )
    domains_with_claimed_tests = (
        int(bool(claimed_executed)) if discovered == 1 and claimed_executed is not None else None
    )
    return {
        "domains_discovered": discovered,
        "domains_attempted": attempted,
        "domains_terminal": terminal,
        "domains_with_claimed_tests": domains_with_claimed_tests,
    }


def _control_surface(value: Mapping[str, Any]) -> dict[str, int | None]:
    return {
        field: _int_or_none(value.get(field))
        for field in (
            "terminal_refusal_recurrences",
            "unsettled_jobs",
            "cleanup_escalations",
            "midrun_human_approvals",
        )
    }


def assemble_report_metrics(
    *,
    snapshot: Dict[str, Any],
    build_evidence: Dict[str, Any],
    test_analysis: Dict[str, Any],
    conflicts: List[str],
    evidence_refs: List[str],
    generated_at: str,
    execution_metrics: Optional[Dict[str, Any]] = None,
    run_pin: Optional[Mapping[str, Any]] = None,
    persistence: Optional[Mapping[str, Any]] = None,
    coverage: Optional[Mapping[str, Any]] = None,
    control: Optional[Mapping[str, Any]] = None,
    receipt_records: Optional[Sequence[Mapping[str, Any]]] = (),
) -> Dict[str, Any]:
    """Assemble the sole forward metrics contract.

    ``build_evidence``, ``evidence_refs``, ``generated_at``, and
    ``execution_metrics`` remain accepted while call sites migrate, but they
    are not copied into unversioned aliases.  Reproducibility comes from the
    run pin and test data comes only from explicitly dispositioned layers.
    """

    del build_evidence, evidence_refs, generated_at, execution_metrics
    snapshot = snapshot or {}
    test_analysis = test_analysis or {}
    conflicts = list(dict.fromkeys(str(item) for item in (conflicts or ()) if item))
    if test_analysis.get("metrics_v2_tests") is not None:
        raise MetricsContractError(
            "pre-aggregated metrics_v2_tests are not an identity proof; "
            "seal module-qualified execution and observation rows instead"
        )
    tests = _project_tests(
        snapshot,
        test_analysis,
        receipt_records=receipt_records,
        run_id=(run_pin or {}).get("run_id"),
        run_target_sha=(run_pin or {}).get("target_repo_sha"),
    )
    outcome = _outcome_surface(snapshot)
    evidence = _evidence_surface(conflicts, persistence or {})
    receipt_execution_count = tests.get("claimed", {}).get("receipt_executions", {}).get("executed")
    if (
        isinstance(receipt_execution_count, int)
        and receipt_execution_count > 0
        and evidence["receipts_persisted"] == 0
    ):
        evidence["integrity"] = "failed"
        evidence["conflict_count"] += 1
    expected = evidence["receipts_expected"]
    persisted = evidence["receipts_persisted"]
    unpersisted = evidence["terminal_receipts_unpersisted"]
    if (
        isinstance(expected, int)
        and isinstance(persisted, int)
        and isinstance(unpersisted, int)
        and expected < persisted + unpersisted
    ):
        raise MetricsContractError(
            "receipts_expected cannot be smaller than persisted plus terminal-unpersisted"
        )
    if outcome["verdict"] == "success" and evidence["integrity"] != "complete":
        raise MetricsContractError(
            f"success verdict cannot be emitted with {evidence['integrity']} evidence transport"
        )

    coverage_surface = (
        {
            field: _int_or_none((coverage or {}).get(field))
            for field in (
                "domains_discovered",
                "domains_attempted",
                "domains_terminal",
                "domains_with_claimed_tests",
            )
        }
        if coverage is not None
        else _coverage_surface(snapshot, tests)
    )
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "identity_version": METRICS_IDENTITY_VERSION,
        "run": _run_surface(run_pin or {}),
        "outcome": outcome,
        "evidence": evidence,
        "tests": tests,
        "coverage": coverage_surface,
        "control": _control_surface(control or {}),
    }


def build_evidence_layer_projection(
    *,
    snapshot: Mapping[str, Any],
    test_analysis: Mapping[str, Any] | None = None,
    conflicts: List[str] | None = None,
) -> dict[str, Any]:
    """Build a display-only fallback when the durable v2 artifact is absent.

    This deliberately has no ``schema_version``, run pin, outcome, coverage,
    or control surface, so an API consumer cannot submit it to the campaign
    evaluator as a metrics-v2 project. It preserves the safe disposition we
    can still make from the seal: old aggregates are unattributed report
    observations, while subject/case and receipt-execution claims stay absent.
    """

    analysis = test_analysis or {}
    if analysis.get("metrics_v2_tests") is not None:
        raise MetricsContractError(
            "pre-aggregated metrics_v2_tests are not an identity proof; "
            "seal module-qualified execution and observation rows instead"
        )
    normalized_conflicts = list(
        dict.fromkeys(str(item) for item in (conflicts or ()) if str(item).strip())
    )
    return {
        "projection_status": "metrics-v2-artifact-unavailable",
        "tests": _project_tests(snapshot or {}, analysis),
        "evidence": _evidence_surface(normalized_conflicts, {}),
    }


def _format_counts(value: Any) -> str:
    if not isinstance(value, Mapping) or value.get("executed") is None:
        reason = value.get("reason") if isinstance(value, Mapping) else None
        file_count = value.get("report_file_count") if isinstance(value, Mapping) else None
        details = [str(reason)] if reason else []
        if isinstance(file_count, int):
            details.append(f"{file_count} report files observed")
        return f"unavailable ({'; '.join(details)})" if details else "unavailable"
    return (
        f"{value.get('passed')}/{value.get('executed')} passed, "
        f"{value.get('failed')} failed, {value.get('errors')} errors, "
        f"{value.get('skipped')} skipped"
    )


def format_evidence_layer_lines(metrics: Mapping[str, Any] | None) -> list[str]:
    """Render all verdict-bearing and non-verdict-bearing test layers."""

    if not isinstance(metrics, Mapping) or not (
        metrics.get("schema_version") == 2
        or metrics.get("projection_status") == "metrics-v2-artifact-unavailable"
    ):
        unavailable = "unavailable (metrics-v2 artifact unavailable)"
        return [
            f"Claimed latest subjects: {unavailable}",
            f"Claimed latest cases: {unavailable}",
            f"Receipt executions: {unavailable}",
            f"Quarantined observations (not verdict-bearing): {unavailable}",
            f"Unattributed observations (not verdict-bearing): {unavailable}",
            f"Stale observations (not verdict-bearing): {unavailable}",
            "Evidence transport: unavailable",
        ]
    tests_value = metrics.get("tests")
    tests: Mapping[str, Any] = tests_value if isinstance(tests_value, Mapping) else {}
    claimed_value = tests.get("claimed")
    claimed: Mapping[str, Any] = claimed_value if isinstance(claimed_value, Mapping) else {}
    evidence_value = metrics.get("evidence")
    evidence: Mapping[str, Any] = evidence_value if isinstance(evidence_value, Mapping) else {}
    integrity = str(evidence.get("integrity") or "unavailable")
    unpersisted = evidence.get("terminal_receipts_unpersisted")
    transport = integrity
    if isinstance(unpersisted, int) and unpersisted:
        transport = f"{integrity} ({unpersisted} terminal receipt unpersisted)"
    return [
        f"Claimed latest subjects: {_format_counts(claimed.get('latest_subjects'))}",
        f"Claimed latest cases: {_format_counts(claimed.get('latest_cases'))}",
        f"Receipt executions: {_format_counts(claimed.get('receipt_executions'))}",
        "Quarantined observations (not verdict-bearing): "
        + _format_counts(tests.get("quarantined_observations")),
        "Unattributed observations (not verdict-bearing): "
        + _format_counts(tests.get("unattributed_observations")),
        "Stale observations (not verdict-bearing): "
        + _format_counts(tests.get("stale_observations")),
        f"Evidence transport: {transport}",
    ]


__all__ = [
    "LegacyReportMetricsV1",
    "METRICS_IDENTITY_VERSION",
    "METRICS_PATH",
    "METRICS_SCHEMA_VERSION",
    "REPORT_METRICS_LOGICAL_ARTIFACT_ID",
    "MetricsContractError",
    "assemble_report_metrics",
    "build_evidence_layer_projection",
    "format_evidence_layer_lines",
    "read_live_report_metrics",
    "read_report_metrics",
    "validate_report_metrics_v2",
]
