"""Evidence-close finalization and immutable run lifecycle contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from sag.evidence import EvidenceStatus, OperationOutcome, TestStats
from sag.runtime.container_io import ContainerFileReadError, read_container_text
from sag.utils.container_io import compare_publish_container_text_atomic
from sag.verdict import rescue_blocked_build, run_verdict
from sag.verdict_rates import (
    UNATTRIBUTED_CONFLICT,
    GrainRate,
    band_for,
    demote_heavy_red,
    derived_verdict_word,
    unbounded_conflicts,
)

from .evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    VERDICT_LOGICAL_ARTIFACT_ID,
    EvidencePublicationError,
    MutablePublicationObservation,
    evidence_publication_authority_for,
)
from .evidence_records import decode_named_json_record_stream, execute_named_json_file_stream
from .evidence_state import EvidenceRole, RunEvidenceState, ToolObservation

VERDICT_SNAPSHOT_PATH = "/workspace/.setup_agent/verdict.json"
LEGACY_VERDICT_SCHEMA_VERSION = 3
VERDICT_SCHEMA_VERSION = 4
_VERDICT_FILENAME = "verdict.json"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")

# Where unattributed volume was READ, for the one case that is not files on
# disk. The claim partition and the shell rescan both read reports a reader can
# go open; the tool-observation fold reads numbers a runner printed, which is
# the render layer and never an authority. Both volumes are disclosed the same
# way and counted by nobody — the sentence just has to say which one it is
# looking at, and absence keeps meaning "reports on disk".
UNATTRIBUTED_FROM_OBSERVATIONS = "tool_observations"


class EvidenceCloseReason(str, Enum):
    TEST_TERMINATED = "test_terminated"
    DEPENDENTS_SKIPPED = "dependents_skipped"
    ABORTED = "aborted"
    CANCELLED = "cancelled"


class RunTerminationStatus(str, Enum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    CANCELLED = "cancelled"


class ReportDeliveryStatus(str, Enum):
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunTermination(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    termination: RunTerminationStatus
    snapshot_ref: str = VERDICT_SNAPSHOT_PATH
    report_delivery_status: ReportDeliveryStatus


class SnapshotTestCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executed: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0


class SnapshotTestStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    discovered: int | None = None
    unique: SnapshotTestCounts = Field(default_factory=SnapshotTestCounts)
    raw: SnapshotTestCounts = Field(default_factory=SnapshotTestCounts)
    flaky_count: int = 0
    judgment: Literal["success", "failed", "unknown"] = "unknown"
    # Plan 4 audit fix: pytest collection failures are first-class sealed
    # facts, never laundered into executed-test counts (None = not observed).
    collection_errors: int | None = None
    collection_errors_skipped: int | None = None
    collection_error_summary: str | None = None
    # Plan 5 Task B2 follow-up: the receipt-scoped basis and its quarantined
    # neighbors travel into the sealed snapshot with the counts they scoped
    # (None = the run predates receipts / observed none).
    receipt_scoped: bool | None = None
    auxiliary_test_stats: dict[str, int] | None = None
    stale_test_reports: list[str] | None = None
    stale_test_stats: dict[str, int] | None = None
    # WHERE the unattributed volume was read, so the disclosure can say it.
    # ABSENT means reports on disk — the only door that ever existed, and the
    # same absent-key convention `receipt_scoped` uses. The tool-observation
    # fold routes console-derived counts through the same destination and must
    # not describe them as files anyone can go look at, so it says so.
    unattributed_source: Literal["tool_observations"] | None = None

    @model_serializer(mode="wrap")
    def _omit_unobserved_collection_fields(self, handler):
        """Byte-compat with pre-Plan-4 snapshots: absent facts serialize as
        absent keys, so recorded replay fixtures keep verifying unchanged."""
        data = handler(self)
        for key in (
            "collection_errors",
            "collection_errors_skipped",
            "collection_error_summary",
            "receipt_scoped",
            "auxiliary_test_stats",
            "stale_test_reports",
            "stale_test_stats",
            "unattributed_source",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="before")
    @classmethod
    def _upgrade_flat_unique_counts(cls, value: Any) -> Any:
        """Read v2 flat snapshots while serializing one explicit unique basis."""
        if not isinstance(value, dict):
            return value
        upgraded = dict(value)
        count_fields = ("executed", "passed", "failed", "errors", "skipped")
        flat_counts = {field: upgraded.pop(field) for field in count_fields if field in upgraded}
        if "unique" not in upgraded and flat_counts:
            upgraded["unique"] = flat_counts
        return upgraded

    @property
    def executed(self) -> int:
        return self.unique.executed

    @property
    def passed(self) -> int:
        return self.unique.passed

    @property
    def failed(self) -> int:
        return self.unique.failed

    @property
    def errors(self) -> int:
        return self.unique.errors

    @property
    def skipped(self) -> int:
        return self.unique.skipped

    @property
    def pass_rate(self) -> float:
        if self.executed <= 0:
            return 0.0
        return round((self.passed / self.executed) * 100.0, 1)

    @property
    def execution_rate(self) -> float | None:
        if not self.discovered:
            return None
        return min(round((self.executed / self.discovered) * 100.0, 1), 100.0)


class BuildEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed: bool = False
    green: bool = False
    # Tri-state build judgment. The PHYSICAL validator is the primary oracle
    # (live ws7-final7 regression: last-observation-wins sealed bigtop as
    # failed while 121 compiled classes and 50/50 green tests sat on disk —
    # the July-13 kernel honestly called that PARTIAL). ``source`` records
    # which oracle produced the judgment so gate/finalizer divergence is
    # diagnosable, never silent.
    judgment: Literal["success", "partial", "failed", "unknown"] = "unknown"
    source: Literal["physical", "observations", "none"] = "none"
    outcome: OperationOutcome = OperationOutcome.UNKNOWN
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    refs: tuple[str, ...] = ()
    compiled_classes: int | None = None
    source_files: int | None = None
    # Plan 5 Task C2 (P0-F): per-domain build states, sealed WITH the build
    # evidence they describe — {"<root>": {"state": ..., "blocker": "<detail>"?}}.
    # None = no multi-domain decomposition was surveyed (single-domain
    # projects), which is why the serializer below drops the key entirely.
    domain_states: dict[str, dict[str, str]] | None = None

    @model_serializer(mode="wrap")
    def _omit_unsurveyed_domain_states(self, handler):
        """Absent facts serialize as absent keys, so recorded replay fixtures
        (and their exact-dict assertions) keep verifying unchanged."""
        data = handler(self)
        if data.get("source_files") is None:
            data.pop("source_files", None)
        if data.get("domain_states") is None:
            data.pop("domain_states", None)
        return data


class PhaseClaimSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    claimed_outcome: str
    signal: str = "done"
    key_results: str = ""
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()


class PhaseRecordSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    attempt_id: str
    termination: str
    outcome: str
    transition: str | None = None
    key_results: str = ""
    reason: str = ""
    evidence: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    claim: PhaseClaimSnapshot | None = None
    validated_outcome: str
    claim_disposition: str | None = None
    legacy_claim: bool = False
    prerequisite_ref: str = ""

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v1_phase_record(cls, value: Any) -> Any:
        """Read additive WS1 records written before claim validation existed."""
        if not isinstance(value, dict):
            return value
        upgraded = dict(value)
        upgraded.setdefault("validated_outcome", upgraded.get("outcome", "unknown"))
        upgraded.setdefault("evidence_refs", upgraded.get("evidence", ()))
        if upgraded.get("transition") == "":
            upgraded["transition"] = None
        return upgraded


class RunVerdictSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = VERDICT_SCHEMA_VERSION
    run_id: str
    finalized_at: str
    input_refs: tuple[str, ...] = ()
    verdict: Literal["success", "partial", "failed", "unknown"]
    build_evidence: BuildEvidenceSnapshot = Field(default_factory=BuildEvidenceSnapshot)
    test_stats: SnapshotTestStats = Field(default_factory=SnapshotTestStats)
    rates: dict[str, Any] = Field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    phase_records: tuple[PhaseRecordSnapshot, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _load_historical_rates_additively(cls, value: Any) -> Any:
        """Historical v3 artifacts remain readable without becoming v4.

        The writer stamps v4 through the field default.  A v3 reader merely
        supplies the additive empty projection; it never rewrites history.
        """
        if not isinstance(value, dict):
            return value
        upgraded = dict(value)
        if upgraded.get("schema_version") == LEGACY_VERDICT_SCHEMA_VERSION:
            upgraded.setdefault("rates", {})
        return upgraded

    def model_dump_json(self, **kwargs: Any) -> str:
        """The one canonical serializer used for both memory and persistence."""
        indent = kwargs.pop("indent", None)
        if kwargs:
            return super().model_dump_json(indent=indent, **kwargs)
        separators = None if indent is not None else (",", ":")
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=separators,
            ensure_ascii=True,
            indent=indent,
        )


def _dedupe(values) -> tuple[str, ...]:
    seen = set()
    ordered = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(str(value))
    return tuple(ordered)


def _result_refs(observation: ToolObservation) -> tuple[str, ...]:
    result = observation.result
    finding_refs = [ref for finding in result.validator_findings for ref in finding.refs]
    return _dedupe(
        [
            result.output_ref,
            *result.evidence_refs,
            *result.refs,
            *finding_refs,
            observation.provenance,
        ]
    )


def _explicit_build_green(observation: ToolObservation) -> bool | None:
    result = observation.result
    for source in (result.facts, result.raw_data or {}, result.metadata or {}):
        for key in ("build_green", "build_success", "build_complete", "build_successful"):
            value = source.get(key)
            if isinstance(value, bool):
                return value
    return None


_JUDGMENT_OUTCOME = {
    "success": OperationOutcome.SUCCESS,
    "partial": OperationOutcome.PARTIAL,
    "failed": OperationOutcome.FAILED,
    "unknown": OperationOutcome.UNKNOWN,
}

_EVIDENCE_STATUS_MAP = {
    "success": EvidenceStatus.VERIFIED,
    "green": EvidenceStatus.VERIFIED,
    "verified": EvidenceStatus.VERIFIED,
    "partial": EvidenceStatus.VERIFIED,
    "blocked": EvidenceStatus.VERIFIED,
    "conflict": EvidenceStatus.CONFLICT,
}


def _module_coverage_conflicts(validator, project_name) -> tuple[str, ...]:
    """The July kernel's module/island coverage honesty, folded at evidence-close.

    Delegates to the SHARED coverage module (sag.agent.module_coverage) — the
    same computation the phase gates render mid-run as an agent-facing
    checklist. One algorithm, two consumers, no drift.

    Plan 8 §3.5: the same OBJECT, now. `_physical_build_status` has just made
    the validator scan; folding the conflicts from that result rather than from
    a second walk is what keeps the sealed verdict and the sentence the model
    read describing one tree.
    """
    from sag.agent.module_coverage import coverage_conflicts, shared_module_scan

    return coverage_conflicts(shared_module_scan(validator, project_name))


_DOMAIN_STATES = frozenset({"success", "failed", "blocked", "untried"})


def _sealed_domain_states(state: RunEvidenceState) -> dict[str, dict[str, str]] | None:
    """Per-domain build states, newest gate evaluation first (Task C2).

    Live p5v-bigtop-r2: the model ran the gradle domains only in the TEST
    phase, so the build gate's snapshot froze them 'untried' while completed
    receipts sat on disk. The test gate re-reads receipts LATER, so its copy
    supersedes the build fact per root; roots only the build gate saw keep
    their build state. Unrecognized state values are dropped rather than
    sealed — the rollup contract is schema v1's four states, and an
    unparseable domain fact is an absent fact, never an invented one.
    """
    build_value = state.fact_value("build.domain_states")
    rollup = state.fact_value("test.stats")
    test_value = rollup.get("domain_states") if isinstance(rollup, Mapping) else None
    value: Mapping[str, Any] | None
    if isinstance(build_value, Mapping) and isinstance(test_value, Mapping):
        value = {**build_value, **test_value}
    elif isinstance(build_value, Mapping):
        value = build_value
    else:
        value = test_value if isinstance(test_value, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    sealed: dict[str, dict[str, str]] = {}
    for root, entry in value.items():
        if not isinstance(entry, Mapping):
            continue
        domain_state = str(entry.get("state") or "").strip().lower()
        if domain_state not in _DOMAIN_STATES:
            continue
        sealed_entry = {"state": domain_state}
        blocker = str(entry.get("blocker") or "").strip()
        if blocker:
            sealed_entry["blocker"] = blocker
        sealed[str(root)] = sealed_entry
    return sealed or None


def _physical_build_status(validator, project_name) -> dict[str, Any] | None:
    """One authoritative physical scan at evidence-close; never raises."""
    if validator is None:
        return None
    try:
        status = validator.validate_build_status(project_name)
    except Exception as exc:  # container gone, replay harness, etc.
        logger.warning(f"physical build oracle unavailable at finalize: {exc}")
        return None
    return status if isinstance(status, dict) else None


def _physical_judgment(status: dict[str, Any]) -> str | None:
    success = status.get("success")
    if success is True:
        return "success" if status.get("build_complete", True) else "partial"
    if success is False:
        return "failed"
    return None


_ACTION_PARAM_KEYS = ("action", "command", "task", "tasks", "goal", "operation")


def _observation_action_group(observation) -> tuple[str, str]:
    params = observation.params if isinstance(observation.params, dict) else {}
    for key in _ACTION_PARAM_KEYS:
        value = params.get(key)
        if value:
            return (observation.tool_name, str(value))
    return (observation.tool_name, "")


def _aggregate_observation_judgment(observations) -> str:
    """Fallback (no container, e.g. replay): AGGREGATE, never bare last-wins.

    Rule (mirrors the WS7 attempt-history philosophy): within one action group
    (tool + action verb), a LATER success supersedes earlier failures — retry
    semantics, someone fixed it. A later failure never erases an earlier
    success — it may be a different target (live bigtop: the failed maven
    island erased the built gradle islands under bare last-wins). Mixed
    evidence renders partial; only the physical oracle can adjudicate further.
    """
    groups: dict[tuple[str, str], list[OperationOutcome]] = {}
    for observation in observations:
        groups.setdefault(_observation_action_group(observation), []).append(
            observation.result.operation_outcome
        )

    group_judgments: list[str] = []
    for outcomes in groups.values():
        informative = [
            outcome
            for outcome in outcomes
            if outcome
            in (OperationOutcome.SUCCESS, OperationOutcome.PARTIAL, OperationOutcome.FAILED)
        ]
        if not informative:
            continue
        last_success = max(
            (i for i, o in enumerate(informative) if o is OperationOutcome.SUCCESS),
            default=-1,
        )
        last_failure = max(
            (i for i, o in enumerate(informative) if o is OperationOutcome.FAILED),
            default=-1,
        )
        if OperationOutcome.PARTIAL in informative:
            group_judgments.append("partial")
        elif last_success >= 0 and last_failure < 0:
            group_judgments.append("success")
        elif last_failure >= 0 and last_success < 0:
            group_judgments.append("failed")
        elif last_success > last_failure:
            group_judgments.append("success")  # retry recovered the action
        else:
            group_judgments.append("partial")  # success then failure: mixed truth

    if not group_judgments:
        return "unknown"
    if all(judgment == "success" for judgment in group_judgments):
        return "success"
    if all(judgment == "failed" for judgment in group_judgments):
        return "failed"
    return "partial"


def _fold_build_evidence(
    state: RunEvidenceState,
    validator=None,
    project_name=None,
) -> tuple[BuildEvidenceSnapshot, tuple[str, ...]]:
    """Fold build evidence: PHYSICAL validator first, observations as fallback.

    Returns the snapshot plus any conflicts the physical oracle emitted (e.g.
    ``build_modules_incomplete``) so the module-coverage honesty of the old
    kernel survives into the sealed run state.
    """
    observations = [
        observation
        for observation in state.tool_observations
        if EvidenceRole.BUILD in observation.roles
        and observation.result.invocation_status.value != "pending"
    ]
    observation_refs = _dedupe(
        ref for observation in observations for ref in _result_refs(observation)
    )
    domain_states = _sealed_domain_states(state)

    physical = _physical_build_status(validator, project_name)
    judgment = _physical_judgment(physical) if physical is not None else None
    if judgment is not None:
        evidence = physical.get("evidence") if isinstance(physical.get("evidence"), dict) else {}
        compiled = _nonnegative_int(
            evidence.get("class_count") if isinstance(evidence, dict) else None
        )
        if compiled is None:
            compiled = _nonnegative_int(state.fact_value("build.compiled_classes"))
        source_files = _nonnegative_int(
            evidence.get("source_files") if isinstance(evidence, dict) else None
        )
        if source_files is None:
            source_files = _nonnegative_int(state.fact_value("build.source_files"))
        physical_refs = tuple(
            str(ref) for ref in (physical.get("evidence_refs") or ()) if str(ref).strip()
        )
        conflicts = tuple(
            dict.fromkeys(
                [
                    *(
                        str(conflict)
                        for conflict in (physical.get("conflicts") or ())
                        if str(conflict).strip()
                    ),
                    *_module_coverage_conflicts(validator, project_name),
                ]
            )
        )
        evidence_status = _EVIDENCE_STATUS_MAP.get(
            str(physical.get("evidence_status") or "").strip().lower(),
            EvidenceStatus.VERIFIED,
        )
        return (
            BuildEvidenceSnapshot(
                observed=True,
                green=judgment == "success",
                judgment=judgment,
                source="physical",
                outcome=_JUDGMENT_OUTCOME[judgment],
                evidence_status=evidence_status,
                refs=_dedupe([*physical_refs, *observation_refs]),
                compiled_classes=compiled,
                source_files=source_files,
                domain_states=domain_states,
            ),
            conflicts,
        )

    if not observations:
        return BuildEvidenceSnapshot(domain_states=domain_states), ()

    judgment = _aggregate_observation_judgment(observations)
    latest = observations[-1]
    explicit_green = _explicit_build_green(latest)
    # Explicit evidence facts on the result (WS0) outrank the outcome
    # aggregate — the verified-artifact rescue: a tool that failed AFTER
    # verifying real build evidence is a partial, not a dead build. The
    # inverse holds too (explicitly-negated evidence caps success).
    if explicit_green is True and judgment == "failed":
        judgment = "partial"
    elif explicit_green is False and judgment == "success":
        judgment = "partial"
    green = explicit_green if explicit_green is not None else judgment == "success"
    return (
        BuildEvidenceSnapshot(
            observed=True,
            green=green,
            judgment=judgment,
            source="observations",
            outcome=_JUDGMENT_OUTCOME[judgment],
            evidence_status=latest.result.evidence_status,
            refs=observation_refs,
            compiled_classes=_nonnegative_int(state.fact_value("build.compiled_classes")),
            source_files=_nonnegative_int(state.fact_value("build.source_files")),
            domain_states=domain_states,
        ),
        (),
    )


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _first_count(sources: tuple[dict[str, Any], ...], *keys: str) -> int | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _coerce_result_stats(observation: ToolObservation) -> tuple[TestStats, int, int] | None:
    result = observation.result
    stats = result.test_stats
    metadata = result.metadata or {}
    analysis_value = metadata.get("analysis")
    analysis = analysis_value if isinstance(analysis_value, dict) else {}
    error_analysis_value = metadata.get("error_analysis")
    error_analysis = error_analysis_value if isinstance(error_analysis_value, dict) else {}
    nested_stats = tuple(
        value
        for value in (
            analysis.get("tests_run"),
            analysis.get("test_results"),
            error_analysis.get("test_stats"),
        )
        if isinstance(value, dict)
    )
    sources = (result.raw_data or {}, metadata, *nested_stats)
    if stats is None:
        return None
    errors = _first_count(
        sources,
        "unique_error_tests",
        "error_tests",
        "errors",
        "error",
    )
    if errors is None:
        # Current TestStats carries errors independently. Older parser payloads
        # may instead expose the split only in metadata, which remains the
        # higher-priority source above.
        errors = stats.errors
    distinct_failures = _first_count(
        sources,
        "unique_failed_tests",
        "failed_tests",
        "failures",
    )
    if distinct_failures is None:
        # WS0 TestStats historically combines failures and errors. Preserve
        # the minimal snapshot's distinct fields when an error count exists.
        distinct_failures = max(stats.failed - errors, 0)
    flaky_count = _first_count(sources, "flaky_count")
    if flaky_count is not None and flaky_count != stats.flaky_count:
        stats = stats.model_copy(update={"flaky_count": flaky_count})
    return stats, distinct_failures, errors


_EXCLUDED_COUNT_FIELDS = ("executed", "passed", "failed", "errors", "skipped")


def _excluded_counts(value: Any) -> dict[str, int] | None:
    """One excluded destination's volume, or None when it was never observed."""
    if not isinstance(value, Mapping):
        return None
    return {str(name): _nonnegative_int(count) or 0 for name, count in value.items()}


def _counts_payload(counts: SnapshotTestCounts) -> dict[str, int]:
    return {field: getattr(counts, field) for field in _EXCLUDED_COUNT_FIELDS}


def _sum_excluded_counts(left: dict[str, int] | None, right: dict[str, int]) -> dict[str, int]:
    """Add one excluded volume to another without inventing an absent one."""
    if not left:
        return dict(right)
    merged = dict(left)
    for name, count in right.items():
        merged[name] = merged.get(name, 0) + count
    return merged


# Conflicts a rollup DERIVES from its own headline counts. When those counts
# leave the headline the statements about them leave with them: a sealed
# `test_failures_detected` beside a headline of 0/0/0 states a red the snapshot
# no longer carries, and a fact must not outlive its basis.
_COUNT_DERIVED_CONFLICTS = frozenset({"test_failures_detected", "test_errors_detected"})


def _fold_test_stats(
    state: RunEvidenceState,
) -> tuple[SnapshotTestStats, tuple[str, ...]]:
    validated_rollup = state.fact_value("test.stats")
    if isinstance(validated_rollup, dict):
        conflicts = _dedupe(validated_rollup.get("conflicts") or ())
        unique_value = validated_rollup.get("unique")
        raw_value = validated_rollup.get("raw")

        def counts(value: Any) -> SnapshotTestCounts | None:
            if not isinstance(value, dict):
                return None
            parsed = {
                name: _nonnegative_int(value.get(name))
                for name in ("executed", "passed", "failed", "errors", "skipped")
            }
            if any(item is None for item in parsed.values()):
                return None
            result = SnapshotTestCounts(
                executed=cast(int, parsed["executed"]),
                passed=cast(int, parsed["passed"]),
                failed=cast(int, parsed["failed"]),
                errors=cast(int, parsed["errors"]),
                skipped=cast(int, parsed["skipped"]),
            )
            if result.passed + result.failed + result.errors + result.skipped != result.executed:
                return None
            return result

        validated_unique = counts(unique_value)
        validated_raw = counts(raw_value)
        if (
            validated_unique is None
            or validated_raw is None
            or validated_raw.executed < validated_unique.executed
        ):
            return SnapshotTestStats(), _dedupe([*conflicts, "validated_test_stats_invalid"])
        auxiliary = _excluded_counts(validated_rollup.get("auxiliary_test_stats"))
        flaky_count = _nonnegative_int(validated_rollup.get("flaky_count")) or 0
        if not validated_rollup.get("receipt_scoped"):
            # Fallback parity. `receipt_scoped` is constant for the compact
            # in-container parser, so a rollup without it came from the shell
            # find/cat rescan, which partitions NOTHING: every count it carries
            # is a report no receipt claims. Sealing that as the headline gave
            # the LESS machinery the HIGHER number — for a zero-receipt run the
            # compact parser sealed headline 0 + auxiliary N while the fallback
            # sealed N, so the two paths disagreed by the entire corpus. The
            # counts are routed to where their provenance puts them, not
            # deleted: same disclosure sentence, same conflict, and the
            # phase-close refusal (`test_receipt_missing`) is unchanged.
            auxiliary = _sum_excluded_counts(auxiliary, _counts_payload(validated_raw))
            validated_unique = SnapshotTestCounts()
            validated_raw = SnapshotTestCounts()
            # The counts moved, so everything DERIVED from them moves with
            # them: the red conflicts restate a headline that is now zero, and
            # the flaky tally was computed over identities this snapshot no
            # longer claims. Both are still visible where the volume went.
            conflicts = tuple(item for item in conflicts if item not in _COUNT_DERIVED_CONFLICTS)
            flaky_count = 0
        # Execution, not the project's pass percentage, is the physical fact
        # this snapshot records.  Red remains visible in the counts and the
        # heavy-red rate signal; it is not a failed SAG execution.
        validated_judgment: Literal["success", "failed", "unknown"] = (
            "success" if validated_unique.executed > 0 else "unknown"
        )
        return (
            SnapshotTestStats(
                discovered=_nonnegative_int(validated_rollup.get("discovered")),
                unique=validated_unique,
                raw=validated_raw,
                flaky_count=flaky_count,
                judgment=validated_judgment,
                collection_errors=_nonnegative_int(validated_rollup.get("collection_errors")),
                collection_errors_skipped=_nonnegative_int(
                    validated_rollup.get("collection_errors_skipped")
                ),
                collection_error_summary=(
                    str(validated_rollup.get("collection_error_summary")).strip() or None
                    if validated_rollup.get("collection_error_summary")
                    else None
                ),
                receipt_scoped=True if validated_rollup.get("receipt_scoped") else None,
                auxiliary_test_stats=auxiliary,
                stale_test_reports=(
                    [str(item) for item in validated_rollup.get("stale_test_reports") or ()] or None
                ),
                stale_test_stats=_excluded_counts(validated_rollup.get("stale_test_stats")),
            ),
            conflicts,
        )

    observations = [
        observation
        for observation in state.tool_observations
        if EvidenceRole.TEST in observation.roles
    ]
    snapshots = [
        item for observation in observations if (item := _coerce_result_stats(observation))
    ]
    if not snapshots:
        return SnapshotTestStats(), ()

    latest_by_basis: dict[tuple[int | None, int], tuple[int, tuple[TestStats, int, int]]] = {}
    for index, snapshot in enumerate(snapshots):
        stats, _, _ = snapshot
        latest_by_basis[(stats.discovered, stats.executed)] = (index, snapshot)

    candidates = tuple(sorted(latest_by_basis.values(), key=lambda item: item[0]))

    def dominates(left: TestStats, right: TestStats) -> bool:
        if left.discovered is None:
            return right.discovered is None and left.executed > right.executed
        if right.discovered is None:
            return left.executed >= right.executed
        return (
            left.discovered >= right.discovered
            and left.executed >= right.executed
            and (left.discovered > right.discovered or left.executed > right.executed)
        )

    frontier = tuple(
        candidate
        for candidate in candidates
        if not any(
            dominates(other[1][0], candidate[1][0])
            for other in candidates
            if other is not candidate
        )
    )
    _, primary = max(
        frontier,
        key=lambda item: (
            item[1][0].executed,
            item[1][1] + item[1][2],
            item[1][2],
            -item[1][0].passed,
            item[0],
        ),
    )
    unique, _, _ = primary
    raw = SnapshotTestCounts(
        executed=sum(stats.executed for stats, _, _ in snapshots),
        passed=sum(stats.passed for stats, _, _ in snapshots),
        failed=sum(failures for _, failures, _ in snapshots),
        errors=sum(errors for _, _, errors in snapshots),
        skipped=sum(stats.skipped for stats, _, _ in snapshots),
    )
    conflicts = ("test_stats_basis_incomparable",) if len(frontier) > 1 else ()
    # Observation-fold parity (spec amendment item 9). Reaching here means NO
    # `test.stats` fact exists at all, so these counts were parsed out of a
    # runner's CONSOLE TEXT: no report file was opened, no receipt claims them,
    # and the render layer is exactly the authority this repo's principles
    # forbid. Sealing them as the full headline gave the fold with the LEAST
    # machinery the BIGGEST number, with no partition, no conflict and no
    # sentence — the same inversion fallback parity closed one branch above.
    #
    # The counts are routed to unattributed volume, never deleted: same
    # destination, same conflict, and a sentence that names both the volume and
    # where it was read. The facts derived from them (judgment, the flaky
    # tally) go with them. The phase-close refusal is untouched — no
    # `receipt_scoped` marker is invented here, so counts that did not come
    # from a claim partition still close nothing. `discovered` is a DISCOVERY
    # fact rather than an execution one and stays, so the grain can show 0 of
    # what was surveyed instead of a ratio with nothing on either side.
    return (
        SnapshotTestStats(
            discovered=unique.discovered,
            auxiliary_test_stats=_counts_payload(raw),
            unattributed_source=UNATTRIBUTED_FROM_OBSERVATIONS,
        ),
        conflicts,
    )


def _excluded_executions(counts: dict[str, int] | None) -> int:
    """One excluded destination's execution volume (0 = nothing excluded)."""
    if not isinstance(counts, Mapping):
        return 0
    return _nonnegative_int(counts.get("executed")) or 0


def _unattributed_executions(stats: SnapshotTestStats) -> int:
    """Executions visible on disk that no receipt claims (0 = none excluded).

    geode ran two `./gradlew test` dispatches to BUILD SUCCESSFUL through the
    `bash` tool, which emits no invocation receipt. 10,448 executions landed on
    disk, the headline sealed 0, and `rates.test.cases` read `0/9754` band
    `none` with no sentence anywhere saying why. The exclusion is correct — an
    unreceipted report has no provenance — but a silent zero states the
    opposite of what happened.

    The disclosure asks ONLY what was excluded, never what was counted. Gating
    it on a zero headline meant one receipted test erased both the conflict and
    the volume beside it: the cheapest way to clear geode's 10,448 would have
    been to run a single test through a receipt-producing tool, not to
    attribute the corpus. Excluded volume is disclosed at any headline, and the
    sentence names both numbers so a reader can see the ratio.
    """
    return _excluded_executions(stats.auxiliary_test_stats)


def _excluded_unparseable(counts: dict[str, int] | None) -> int:
    """Reports at one excluded destination whose volume could not be measured.

    An unreadable excluded report contributes ZERO executions, so a corpus
    whose only stray XML is corrupt measures 0 and — before this — said
    nothing at all. Never counted, never capping, always said.
    """
    if not isinstance(counts, Mapping):
        return 0
    return _nonnegative_int(counts.get("unparseable")) or 0


def _unparseable_clause(count: int) -> str:
    return f"{count:,} unparseable report{'' if count == 1 else 's'}"


def _excluded_volume_clauses(
    unattributed: int,
    stale: int,
    *,
    unattributed_unparseable: int = 0,
    stale_unparseable: int = 0,
    unattributed_source: str | None = None,
) -> list[str]:
    """Name each excluded destination separately, in one shared vocabulary.

    A report leaves the headline through one of two doors and they mean
    different things: AUXILIARY is claimed by nobody, STALE was claimed and the
    bytes were then rewritten. Only the first was ever spoken aloud, so a
    superseded-sha claim silently dropped its volume and a rewritten report read
    exactly like a report that never existed. Neither is ever counted.

    What could not be READ at either door is named in that door's own clause,
    so a sentence carrying both never leaves a reader guessing which unparseable
    count belongs to which volume.
    """
    where = (
        "reported in tool output"
        if unattributed_source == UNATTRIBUTED_FROM_OBSERVATIONS
        else "visible on disk"
    )
    clauses: list[str] = []
    if unattributed:
        clause = f"{unattributed:,} executions {where} but bound to no receipt"
        if unattributed_unparseable:
            clause += f" ({_unparseable_clause(unattributed_unparseable)})"
        clauses.append(clause)
    elif unattributed_unparseable:
        unreadable = _unparseable_clause(unattributed_unparseable)
        clauses.append(f"{unreadable} {where} but bound to no receipt")
    if stale:
        # "executions" is said once per sentence, by whichever clause opens it.
        volume = f"{stale:,}" if clauses else f"{stale:,} executions"
        clause = f"{volume} under rewritten claims"
        if stale_unparseable:
            clause += f" ({_unparseable_clause(stale_unparseable)})"
        clauses.append(clause)
    elif stale_unparseable:
        clauses.append(f"{_unparseable_clause(stale_unparseable)} under rewritten claims")
    return clauses


def test_grain_rates(
    stats: SnapshotTestStats,
    driven_modules: set[str],
    test_modules: set[str],
) -> tuple[dict[str, GrainRate], tuple[str, ...]]:
    """Return execution-based test case and surveyed-module grains."""

    unattributed = _unattributed_executions(stats)
    unattributed_unparseable = _excluded_unparseable(stats.auxiliary_test_stats)
    excluded = ", ".join(
        _excluded_volume_clauses(
            unattributed,
            _excluded_executions(stats.stale_test_stats),
            unattributed_unparseable=unattributed_unparseable,
            stale_unparseable=_excluded_unparseable(stats.stale_test_stats),
            unattributed_source=stats.unattributed_source,
        )
    )
    if stats.discovered:
        cases = GrainRate(
            numerator=stats.unique.executed,
            denominator=stats.discovered,
            reason=(
                f"{stats.unique.executed}/{stats.discovered} — {excluded}" if excluded else None
            ),
        )
    else:
        # Both volumes or neither: without a denominator the grain shows no
        # ratio, so the attributed count has to be IN the sentence or a reader
        # sees the excluded volume alone and reads it as the whole story.
        cases = GrainRate(
            0,
            None,
            reason=(
                f"{stats.unique.executed} executed, static discovery found no count — {excluded}"
                if excluded
                else "static discovery found no count"
            ),
        )
    cases, conflicts = demote_heavy_red(
        cases,
        failed=stats.unique.failed,
        errors=stats.unique.errors,
        executed=stats.unique.executed,
    )
    if unattributed or unattributed_unparseable:
        # Visibility without authority: the volume is named, never counted.
        # An unclaimed report the parser could not open has no volume to state
        # and is disclosed all the same — the conflict and the sentence travel
        # together, or the sentence is a fact nothing points at.
        conflicts += (UNATTRIBUTED_CONFLICT,)

    if test_modules:
        modules = GrainRate(
            numerator=len(driven_modules & test_modules),
            denominator=len(test_modules),
        )
    else:
        modules = GrainRate(0, None, reason="no test modules surveyed")
    return {"cases": cases, "modules": modules}, conflicts


def _module_sets_from_rollup(state: RunEvidenceState) -> tuple[set[str], set[str]]:
    rollup = state.fact_value("test.stats")
    if not isinstance(rollup, Mapping):
        return set(), set()

    def values(name: str) -> set[str]:
        raw = rollup.get(name)
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    return values("driven_modules"), values("test_modules")


def _coverage_rate_payload(state: RunEvidenceState) -> dict[str, Any]:
    summary = state.fact_value("coverage.summary")
    if not isinstance(summary, Mapping):
        return {"status": "unavailable", "reason": "coverage pass not run"}

    status = str(summary.get("status") or "").strip().lower()
    if status == "unavailable":
        reason = str(summary.get("reason") or "coverage evidence unavailable").strip()
        return {"status": "unavailable", "reason": reason}

    raw_rate = summary.get("line_rate")
    source = str(summary.get("source") or summary.get("coverage_source") or "").strip()
    if (
        status in ("", "collected")
        and type(raw_rate) in (int, float)
        and 0.0 <= float(raw_rate) <= 100.0
        and source
    ):
        return {
            "line_rate": round(float(raw_rate), 1),
            "source": source,
            "status": "collected",
        }
    return {"status": "unavailable", "reason": "coverage evidence invalid"}


def _snapshot_rates(
    state: RunEvidenceState,
    build: BuildEvidenceSnapshot,
    tests: SnapshotTestStats,
    *,
    validator=None,
    project_name=None,
) -> tuple[dict[str, Any], GrainRate, GrainRate, tuple[str, ...]]:
    """Assemble the one serialized rates block from already-held evidence."""
    from sag.agent.module_coverage import build_grain_rates, shared_module_scan

    build_grains, build_conflicts = build_grain_rates(
        shared_module_scan(validator, project_name),
        compiled_classes=build.compiled_classes,
        source_files=build.source_files,
    )
    driven_modules, test_modules = _module_sets_from_rollup(state)
    test_grains, rate_conflicts = test_grain_rates(
        tests,
        driven_modules=driven_modules,
        test_modules=test_modules,
    )
    rates = {
        "build": {name: grain.payload() for name, grain in build_grains.items()},
        "test": {name: grain.payload() for name, grain in test_grains.items()},
        "coverage": _coverage_rate_payload(state),
    }
    # An impossible fraction is a conflict, never a quiet top band: the
    # 2026-08-10 acceptance sealed classes 201/68 and cases 1605/1163 with an
    # empty conflict list and a manufactured `success`.
    rate_conflicts += build_conflicts
    rate_conflicts += unbounded_conflicts(*build_grains.values(), *test_grains.values())
    return rates, build_grains["modules"], test_grains["cases"], rate_conflicts


def _snapshot_verdict(
    build: BuildEvidenceSnapshot,
    tests: SnapshotTestStats,
    conflicts: tuple[str, ...],
) -> Literal["success", "partial", "failed", "unknown"]:
    # Tri-state fold (restores the July-13 kernel's PARTIAL middle; live
    # ws7-final7 regression: `not green -> failed` erased it and bigtop's
    # partial-build + 50/50 green tests rendered FAILED):
    #   build success -> tests decide (green=success, red=failed, none=partial)
    #   build partial -> capped at partial (tests red still fail)
    #   build failed  -> failed
    #   build unknown -> strong test evidence grounds the verdict; only a run
    #                    with NOTHING observed anywhere stays unknown.
    if build.judgment == "failed":
        physical_verdict = "failed"
    elif build.judgment == "success":
        if tests.judgment == "success":
            physical_verdict = "success"
        elif tests.judgment == "failed":
            physical_verdict = "failed"
        else:
            physical_verdict = "partial"
    elif build.judgment == "partial":
        physical_verdict = "failed" if tests.judgment == "failed" else "partial"
    else:  # unknown build
        if tests.judgment == "failed":
            physical_verdict = "failed"
        elif tests.judgment == "success":
            physical_verdict = "partial"
        else:
            return "unknown"

    # No separate blocked-build rescue here: the physical oracle IS the rescue
    # (agent beliefs never reach this fold; judgment already reflects the real
    # build). rescue_blocked_build stays exported for the legacy surfaces.
    build_judge = build.judgment if build.judgment != "unknown" else None
    return cast(
        Literal["success", "partial", "failed", "unknown"],
        run_verdict(build_judge, physical_verdict, conflicts),
    )


_OUTCOME_RANK = {"failed": 0, "partial": 1, "success": 2}


def _oracle_divergence_conflicts(state: RunEvidenceState, build) -> tuple[str, ...]:
    """Gate-vs-finalizer divergence is a VISIBLE conflict, never silent.

    Live ws7-final7: one sealed snapshot carried the build phase gate-validated
    as SUCCESS next to build evidence FAILED. With both sides now reading the
    physical oracle this should not recur; if physical state genuinely changed
    between the mid-run gate and evidence-close (rank gap >= 2), the snapshot
    says so instead of shipping a quiet contradiction.
    """
    build_rank = _OUTCOME_RANK.get(build.judgment)
    if build_rank is None:
        return ()
    for record in state.phase_records:
        phase = str(getattr(record, "phase", "") or "")
        if phase != "build":
            continue
        validated = getattr(record, "validated_outcome", None)
        validated_value = getattr(validated, "value", validated)
        validated_rank = _OUTCOME_RANK.get(str(validated_value or ""))
        if validated_rank is not None and abs(validated_rank - build_rank) >= 2:
            return ("build_oracle_divergence",)
    return ()


def _phase_record_snapshot(record) -> PhaseRecordSnapshot:
    return PhaseRecordSnapshot.model_validate(asdict(record))


def _read_snapshot_text(orchestrator) -> str | None:
    try:
        return read_container_text(orchestrator, VERDICT_SNAPSHOT_PATH, exact_bytes=True)
    except (ContainerFileReadError, TypeError, ValueError):
        # A failed clean observation does not license a second read through
        # the project runtime overlay. Lightweight forensic doubles can expose
        # ``read_file`` or an in-memory ``files`` mapping, both of which the
        # exact reader already handles without invoking a command.
        return None


def _unknown_snapshot(conflict: str) -> RunVerdictSnapshot:
    return RunVerdictSnapshot(
        run_id="unknown",
        finalized_at="unknown",
        verdict="unknown",
        conflicts=(conflict,),
    )


def read_verdict_snapshot(orchestrator) -> RunVerdictSnapshot:
    """Forensically parse container bytes without granting live authority.

    This compatibility API is intentionally suitable only for diagnostics and
    historical artifact inspection. Verdict-bearing consumers must use
    :func:`read_live_verdict_snapshot`, which requires the exact current bytes
    to be authorized by the host control stream.
    """
    content = _read_snapshot_text(orchestrator)
    if content is None:
        return _unknown_snapshot("snapshot_missing")
    try:
        return RunVerdictSnapshot.model_validate_json(content)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _unknown_snapshot("snapshot_corrupt")


def _validated_rate_grain(
    payload: Any,
    *,
    label: str,
    allow_heavy_red_demotion: bool = False,
) -> GrainRate:
    if not isinstance(payload, Mapping):
        raise ValueError(f"verdict {label} rate must be an object")
    band = payload.get("band")
    if band == "unavailable":
        if set(payload) != {"band", "reason"}:
            raise ValueError(f"verdict unavailable {label} rate shape is invalid")
        reason = payload.get("reason")
        if type(reason) is not str or not reason.strip():
            raise ValueError(f"verdict unavailable {label} reason is invalid")
        return GrainRate(0, None, reason=reason)

    if band == "unbounded":
        # Both counts are real observations and stay sealed; only their ratio
        # is refused, so this shape carries the counts AND the reason.
        if set(payload) != {"band", "reason", "numerator", "denominator"}:
            raise ValueError(f"verdict unbounded {label} rate shape is invalid")
        reason = payload.get("reason")
        if type(reason) is not str or not reason.strip():
            raise ValueError(f"verdict unbounded {label} reason is invalid")
        numerator = payload.get("numerator")
        denominator = payload.get("denominator")
        if type(numerator) is not int or type(denominator) is not int:
            raise ValueError(f"verdict unbounded {label} counts are invalid")
        if denominator <= 0 or numerator <= denominator:
            raise ValueError(f"verdict unbounded {label} counts do reconcile as a rate")
        return GrainRate(numerator, denominator, reason=reason)

    # A measured fraction may carry one optional sentence (spec §1 item 3: the
    # cases grain names the executions excluded from its own numerator). The
    # sentence is additive — absent stays absent, so recorded v4 artifacts
    # written before it keep validating byte-identically.
    if set(payload) not in (
        {"rate", "band", "numerator", "denominator"},
        {"rate", "band", "numerator", "denominator", "reason"},
    ):
        raise ValueError(f"verdict collected {label} rate shape is invalid")
    reason = payload.get("reason")
    if "reason" in payload and (type(reason) is not str or not reason.strip()):
        raise ValueError(f"verdict collected {label} reason is invalid")
    numerator = payload.get("numerator")
    denominator = payload.get("denominator")
    rate = payload.get("rate")
    if type(numerator) is not int or numerator < 0:
        raise ValueError(f"verdict {label} numerator is invalid")
    if type(denominator) is not int or denominator <= 0:
        raise ValueError(f"verdict {label} denominator is invalid")
    if type(rate) not in (int, float) or float(rate) < 0.0:
        raise ValueError(f"verdict {label} percentage is invalid")
    expected_rate = round(numerator / denominator * 100.0, 1)
    if float(rate) != expected_rate:
        raise ValueError(f"verdict {label} percentage does not reconcile")
    expected_band = band_for(numerator, denominator)
    if band != expected_band:
        if not (allow_heavy_red_demotion and expected_band == "fully" and band == "most"):
            raise ValueError(f"verdict {label} band does not reconcile")
        return GrainRate(numerator, denominator, reason=reason, band_override="most")
    return GrainRate(numerator, denominator, reason=reason)


def _validated_rates_block(
    payload: Any,
    *,
    conflicts: tuple[str, ...],
) -> tuple[GrainRate, GrainRate]:
    if not isinstance(payload, Mapping) or set(payload) != {"build", "test", "coverage"}:
        raise ValueError("verdict rates block shape is invalid")
    build = payload.get("build")
    tests = payload.get("test")
    if not isinstance(build, Mapping) or set(build) != {"modules", "classes"}:
        raise ValueError("verdict build rates shape is invalid")
    if not isinstance(tests, Mapping) or set(tests) != {"cases", "modules"}:
        raise ValueError("verdict test rates shape is invalid")

    build_modules = _validated_rate_grain(build["modules"], label="build modules")
    _validated_rate_grain(build["classes"], label="build classes")
    test_cases = _validated_rate_grain(
        tests["cases"],
        label="test cases",
        allow_heavy_red_demotion="test_failures_heavy" in conflicts,
    )
    _validated_rate_grain(tests["modules"], label="test modules")

    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("verdict coverage rate must be an object")
    if coverage.get("status") == "unavailable":
        if set(coverage) != {"status", "reason"}:
            raise ValueError("verdict unavailable coverage shape is invalid")
        reason = coverage.get("reason")
        if type(reason) is not str or not reason.strip():
            raise ValueError("verdict unavailable coverage reason is invalid")
    elif coverage.get("status") == "collected":
        if set(coverage) != {"status", "line_rate", "source"}:
            raise ValueError("verdict collected coverage shape is invalid")
        line_rate = coverage.get("line_rate")
        source = coverage.get("source")
        if type(line_rate) not in (int, float) or not 0.0 <= float(line_rate) <= 100.0:
            raise ValueError("verdict coverage line rate is invalid")
        if type(source) is not str or not source.strip():
            raise ValueError("verdict coverage source is invalid")
    else:
        raise ValueError("verdict coverage status is invalid")
    return build_modules, test_cases


def validate_verdict_snapshot_v3(payload: Mapping[str, Any]) -> RunVerdictSnapshot:
    """Validate one decoded v3/v4 verdict payload without granting authority.

    The historical function name is retained for offline callers.  V3 is
    display/forensic only; the live reader separately requires current v4.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("verdict payload must be an object")
    if type(payload.get("schema_version")) is not int:
        raise ValueError("verdict schema version must be a strict integer")
    schema_version = payload.get("schema_version")
    if schema_version not in (LEGACY_VERDICT_SCHEMA_VERSION, VERDICT_SCHEMA_VERSION):
        raise ValueError("verdict schema version is not supported")

    raw_test_stats = payload.get("test_stats", {})
    if not isinstance(raw_test_stats, Mapping):
        raise ValueError("verdict test stats must be an object")

    def validate_raw_count(value: Any, *, label: str) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"verdict {label} count is invalid")

    for basis in ("unique", "raw"):
        raw_counts = raw_test_stats.get(basis, {})
        if not isinstance(raw_counts, Mapping):
            raise ValueError(f"verdict {basis} test counts must be an object")
        for field in ("executed", "passed", "failed", "errors", "skipped"):
            if field in raw_counts:
                validate_raw_count(
                    raw_counts[field],
                    label=f"{basis} test {field}",
                )
    for field in (
        "discovered",
        "flaky_count",
        "collection_errors",
        "collection_errors_skipped",
    ):
        value = raw_test_stats.get(field)
        if value is not None:
            validate_raw_count(value, label=field)
    for destination in ("auxiliary", "stale"):
        excluded = raw_test_stats.get(f"{destination}_test_stats")
        if excluded is None:
            continue
        if not isinstance(excluded, Mapping):
            raise ValueError(f"verdict {destination} test stats must be an object")
        for name, value in excluded.items():
            if type(name) is not str or not name:
                raise ValueError(f"verdict {destination} test stat name is invalid")
            validate_raw_count(value, label=f"{destination} test {name}")

    raw_build = payload.get("build_evidence", {})
    if not isinstance(raw_build, Mapping):
        raise ValueError("verdict build evidence must be an object")
    compiled_classes = raw_build.get("compiled_classes")
    if compiled_classes is not None:
        validate_raw_count(compiled_classes, label="compiled class")
    source_files = raw_build.get("source_files")
    if source_files is not None:
        validate_raw_count(source_files, label="source file")

    snapshot = RunVerdictSnapshot.model_validate(payload)
    if type(snapshot.run_id) is not str or _RUN_ID_RE.fullmatch(snapshot.run_id) is None:
        raise ValueError("verdict run id is invalid")
    finalized_at = snapshot.finalized_at
    if type(finalized_at) is not str or _UTC_TIMESTAMP_RE.fullmatch(finalized_at) is None:
        raise ValueError("verdict finalized_at is not a sealed UTC timestamp")
    try:
        parsed = datetime.fromisoformat(finalized_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("verdict finalized_at is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("verdict finalized_at is not UTC")

    def validate_counts(counts: SnapshotTestCounts, *, label: str) -> None:
        values = (
            counts.executed,
            counts.passed,
            counts.failed,
            counts.errors,
            counts.skipped,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError(f"verdict {label} counts are invalid")
        if counts.executed != sum(values[1:]):
            raise ValueError(f"verdict {label} counts do not reconcile")

    tests = snapshot.test_stats
    if tests.discovered is not None and (type(tests.discovered) is not int or tests.discovered < 0):
        raise ValueError("verdict discovered test count is invalid")
    if type(tests.flaky_count) is not int or tests.flaky_count < 0:
        raise ValueError("verdict flaky test count is invalid")
    validate_counts(tests.unique, label="unique test")
    validate_counts(tests.raw, label="raw test")
    if tests.raw.executed < tests.unique.executed:
        raise ValueError("verdict raw test count is narrower than the unique basis")
    if snapshot.build_evidence.compiled_classes is not None and (
        type(snapshot.build_evidence.compiled_classes) is not int
        or snapshot.build_evidence.compiled_classes < 0
    ):
        raise ValueError("verdict compiled class count is invalid")
    if snapshot.build_evidence.source_files is not None and (
        type(snapshot.build_evidence.source_files) is not int
        or snapshot.build_evidence.source_files < 0
    ):
        raise ValueError("verdict source file count is invalid")
    if schema_version == LEGACY_VERDICT_SCHEMA_VERSION:
        if snapshot.rates:
            raise ValueError("schema-v3 verdict cannot carry rates")
        return snapshot

    build_modules, test_cases = _validated_rates_block(
        snapshot.rates,
        conflicts=snapshot.conflicts,
    )
    expected_verdict = run_verdict(
        None,
        derived_verdict_word(build_modules, test_cases),
        snapshot.conflicts,
    )
    if snapshot.verdict != expected_verdict:
        raise ValueError("verdict word does not reconcile with the rate bands")
    return snapshot


def read_live_verdict_snapshot(orchestrator, *, authority=None) -> RunVerdictSnapshot:
    """Return only the fixed verdict file at its exact host-authorized head.

    Any missing file, malformed/future schema, duplicate key, filename
    mismatch, foreign run, unpublished mirror, stale revision, deletion or
    tombstone becomes an explicit ``unknown`` snapshot. No fallback recomputes
    or improves the verdict from report metrics.
    """

    try:
        result = execute_named_json_file_stream(orchestrator, VERDICT_SNAPSHOT_PATH)
    except Exception:
        return _unknown_snapshot("snapshot_live_stream_unreadable")
    decoded = decode_named_json_record_stream(result)
    if not decoded.complete or decoded.conflict is not None or len(decoded.records) > 1:
        return _unknown_snapshot(f"snapshot_live_{decoded.conflict or 'stream_unreadable'}")
    try:
        host_authority = authority or evidence_publication_authority_for(orchestrator)
        getattr(host_authority, "assert_store")(orchestrator)
    except (AttributeError, EvidencePublicationError, TypeError, ValueError):
        return _unknown_snapshot("snapshot_live_publication_unavailable")

    observed: dict[str, MutablePublicationObservation] = {}
    snapshot: RunVerdictSnapshot | None = None
    try:
        head = host_authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    except (AttributeError, EvidencePublicationError, TypeError, ValueError):
        return _unknown_snapshot("snapshot_live_publication_unavailable")
    if head is not None and (
        head.record_kind != "verdict"
        or head.record_id != VERDICT_LOGICAL_ARTIFACT_ID
        or head.logical_artifact_id != VERDICT_LOGICAL_ARTIFACT_ID
        or head.revision != 1
    ):
        return _unknown_snapshot("snapshot_live_publication_head_invalid")
    if decoded.records:
        record = decoded.records[0]
        if record.filename != _VERDICT_FILENAME:
            return _unknown_snapshot("snapshot_live_filename_invalid")
        try:
            snapshot = validate_verdict_snapshot_v3(record.payload)
        except (TypeError, ValueError):
            return _unknown_snapshot("snapshot_live_schema_invalid")
        if snapshot.schema_version != VERDICT_SCHEMA_VERSION:
            return _unknown_snapshot("snapshot_live_schema_invalid")
        canonical = snapshot.model_dump_json().encode("utf-8")
        if record.raw != canonical:
            return _unknown_snapshot("snapshot_live_noncanonical")
        if snapshot.run_id != str(getattr(host_authority, "run_id", "") or ""):
            return _unknown_snapshot("snapshot_live_foreign_run")
        observed[VERDICT_LOGICAL_ARTIFACT_ID] = MutablePublicationObservation(
            logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
            raw_sha256=record.raw_sha256,
            byte_count=record.byte_count,
            run_id=snapshot.run_id,
        )

    try:
        check = host_authority.verify_latest_record_set("verdict", observed)
    except (AttributeError, EvidencePublicationError, TypeError, ValueError):
        return _unknown_snapshot("snapshot_live_publication_unavailable")
    if not check.authorized:
        return _unknown_snapshot(f"snapshot_live_publication_{check.status}")
    if snapshot is None:
        return _unknown_snapshot("snapshot_live_missing")
    return snapshot


class VerdictFinalizer:
    def __init__(
        self,
        orchestrator,
        *,
        validator=None,
        project_name: str | None = None,
    ):
        self.orchestrator = orchestrator
        # The physical validator is the build oracle at evidence-close (same
        # oracle the gates consult). None (e.g. replay) degrades the fold to
        # the observation aggregate.
        self.validator = validator
        self.project_name = project_name
        self._snapshots: dict[int, RunVerdictSnapshot] = {}
        self._expected_snapshots: dict[int, RunVerdictSnapshot] = {}

    def _snapshot_for_state(self, state: RunEvidenceState) -> RunVerdictSnapshot:
        cache_key = id(state)
        cached = self._expected_snapshots.get(cache_key)
        if cached is not None:
            return cached

        build, build_conflicts = _fold_build_evidence(
            state,
            validator=self.validator,
            project_name=self.project_name,
        )
        tests, test_conflicts = _fold_test_stats(state)
        rates, build_modules_rate, test_cases_rate, rate_conflicts = _snapshot_rates(
            state,
            build,
            tests,
            validator=self.validator,
            project_name=self.project_name,
        )
        conflicts = _dedupe(
            [
                *state.conflicts,
                *build_conflicts,
                *test_conflicts,
                *rate_conflicts,
                *_oracle_divergence_conflicts(state, build),
            ]
        )
        input_refs = _dedupe(
            [
                *(
                    ref
                    for observation in state.tool_observations
                    for ref in _result_refs(observation)
                ),
                *(fact.provenance for fact in state.facts),
                *(ref for record in state.phase_records for ref in record.evidence),
            ]
        )
        snapshot = RunVerdictSnapshot(
            run_id=state.run_id,
            finalized_at=state.finalized_at or "unknown",
            input_refs=input_refs,
            verdict=run_verdict(
                None,
                derived_verdict_word(build_modules_rate, test_cases_rate),
                conflicts,
            ),
            build_evidence=build,
            test_stats=tests,
            rates=rates,
            conflicts=conflicts,
            phase_records=tuple(_phase_record_snapshot(record) for record in state.phase_records),
        )
        self._expected_snapshots[cache_key] = snapshot
        return snapshot

    def has_current_snapshot(self, state: RunEvidenceState) -> bool:
        """Return whether host authority seals this run's exact current bytes."""
        if not state.sealed:
            return False
        snapshot = self._snapshot_for_state(state)
        return read_live_verdict_snapshot(self.orchestrator) == snapshot

    def finalize(
        self,
        state: RunEvidenceState,
        reason: EvidenceCloseReason,
    ) -> RunVerdictSnapshot:
        if not isinstance(reason, EvidenceCloseReason):
            raise TypeError("finalize requires a typed EvidenceCloseReason")

        if state.close_reason is not None and state.close_reason != reason.value:
            raise ValueError(
                "conflicting evidence-close reason: "
                f"sealed={state.close_reason}, requested={reason.value}"
            )

        cache_key = id(state)
        if cache_key in self._snapshots:
            if not self.has_current_snapshot(state):
                raise RuntimeError("cached verdict snapshot is not current on disk")
            return self._snapshots[cache_key]

        if not state.sealed:
            finalized_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            state.seal(finalized_at=finalized_at, close_reason=reason.value)

        snapshot = self._snapshot_for_state(state)
        try:
            validated_snapshot = validate_verdict_snapshot_v3(snapshot.model_dump(mode="json"))
            if validated_snapshot.schema_version != VERDICT_SCHEMA_VERSION:
                raise ValueError("writer did not produce the current verdict schema")
        except (TypeError, ValueError) as exc:
            raise ValueError("sealed verdict snapshot violates the live schema") from exc
        if self.has_current_snapshot(state):
            self._snapshots[cache_key] = snapshot
            return snapshot

        body = snapshot.model_dump_json()
        raw = body.encode("utf-8")
        try:
            current = read_container_text(
                self.orchestrator,
                VERDICT_SNAPSHOT_PATH,
                exact_bytes=True,
            )
        except (ContainerFileReadError, TypeError, ValueError) as exc:
            raise OSError("failed to read verdict snapshot for compare-and-publish") from exc

        if current != body:
            if current is not None:
                raise OSError("verdict compare-and-publish found unexpected existing bytes")
            persisted = compare_publish_container_text_atomic(
                self.orchestrator,
                VERDICT_SNAPSHOT_PATH,
                body,
                expected_content=None,
                validate_json=True,
            )
            if not persisted.persisted:
                raise OSError("verdict compare-and-publish failed: " f"{persisted.code}")

        # Container persistence precedes publication deliberately. If the host
        # sink fails, these bytes remain useful forensic evidence but are not a
        # live verdict and no cache entry is admitted.
        try:
            authority = evidence_publication_authority_for(self.orchestrator)
            if snapshot.run_id != str(getattr(authority, "run_id", "") or ""):
                raise OSError("host publication run binding differs from verdict run")
            head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
            if head is not None:
                if head.revision != 1:
                    raise OSError("host verdict publication revision is not terminal")
                current_check = authority.verify_latest_bytes(
                    record_kind="verdict",
                    record_id=VERDICT_LOGICAL_ARTIFACT_ID,
                    logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
                    raw=raw,
                    run_id=snapshot.run_id,
                )
                if not current_check.authorized:
                    raise OSError("host verdict publication head differs from sealed bytes")
            authority.publish_revision(
                record_kind="verdict",
                record_id=VERDICT_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
                raw=raw,
                expected_previous_raw_sha256=(
                    head.raw_sha256 if head is not None else EVIDENCE_PUBLICATION_GENESIS_SHA256
                ),
            )
        except OSError:
            raise
        except (EvidencePublicationError, TypeError, ValueError) as exc:
            raise OSError("verdict host publication failed") from exc

        if read_live_verdict_snapshot(self.orchestrator, authority=authority) != snapshot:
            raise OSError("published verdict snapshot is not the current live run")
        self._snapshots[cache_key] = snapshot
        return snapshot
