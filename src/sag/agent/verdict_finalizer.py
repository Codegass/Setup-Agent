"""Evidence-close finalization and immutable run lifecycle contracts."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag.agent.physical_validator import evaluate_run_verdict
from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.evidence import EvidenceStatus, OperationOutcome, TestStats
from sag.verdict import rescue_blocked_build, run_verdict

from .evidence_state import EvidenceRole, RunEvidenceState, ToolObservation
from .output_storage import atomic_write_container_text

VERDICT_SNAPSHOT_PATH = "/workspace/.setup_agent/verdict.json"
VERDICT_SCHEMA_VERSION = 3


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
    outcome: OperationOutcome = OperationOutcome.UNKNOWN
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    refs: tuple[str, ...] = ()
    compiled_classes: int | None = None


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
    conflicts: tuple[str, ...] = ()
    phase_records: tuple[PhaseRecordSnapshot, ...] = ()

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


def _fold_build_evidence(state: RunEvidenceState) -> BuildEvidenceSnapshot:
    observations = [
        observation
        for observation in state.tool_observations
        if EvidenceRole.BUILD in observation.roles
        and observation.result.invocation_status.value != "pending"
    ]
    if not observations:
        return BuildEvidenceSnapshot()

    latest = observations[-1]
    explicit_green = _explicit_build_green(latest)
    green = (
        explicit_green
        if explicit_green is not None
        else latest.result.operation_outcome is OperationOutcome.SUCCESS
    )
    return BuildEvidenceSnapshot(
        observed=True,
        green=green,
        outcome=latest.result.operation_outcome,
        evidence_status=latest.result.evidence_status,
        refs=_dedupe(ref for observation in observations for ref in _result_refs(observation)),
        compiled_classes=_nonnegative_int(state.fact_value("build.compiled_classes")),
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
    errors = errors or 0
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


def _fold_test_stats(
    state: RunEvidenceState,
    *,
    test_pass_threshold: float,
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
        validated_judgment: Literal["success", "failed", "unknown"] = "unknown"
        if validated_unique.executed > 0:
            validated_judgment = cast(
                Literal["success", "failed", "unknown"],
                evaluate_run_verdict(
                    True,
                    round((validated_unique.passed / validated_unique.executed) * 100.0, 1),
                    test_pass_threshold=test_pass_threshold,
                ),
            )
        return (
            SnapshotTestStats(
                discovered=_nonnegative_int(validated_rollup.get("discovered")),
                unique=validated_unique,
                raw=validated_raw,
                flaky_count=_nonnegative_int(validated_rollup.get("flaky_count")) or 0,
                judgment=validated_judgment,
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
    unique, unique_failures, unique_errors = primary
    raw = SnapshotTestCounts(
        executed=sum(stats.executed for stats, _, _ in snapshots),
        passed=sum(stats.passed for stats, _, _ in snapshots),
        failed=sum(failures for _, failures, _ in snapshots),
        errors=sum(errors for _, _, errors in snapshots),
        skipped=sum(stats.skipped for stats, _, _ in snapshots),
    )
    conflicts = ("test_stats_basis_incomparable",) if len(frontier) > 1 else ()
    judgment: Literal["success", "failed", "unknown"] = "unknown"
    if unique.executed > 0:
        judgment = cast(
            Literal["success", "failed", "unknown"],
            evaluate_run_verdict(
                True,
                round((unique.passed / unique.executed) * 100.0, 1),
                test_pass_threshold=test_pass_threshold,
            ),
        )
    return (
        SnapshotTestStats(
            discovered=unique.discovered,
            unique=SnapshotTestCounts(
                executed=unique.executed,
                passed=unique.passed,
                failed=unique_failures,
                errors=unique_errors,
                skipped=unique.skipped,
            ),
            raw=raw,
            flaky_count=unique.flaky_count,
            judgment=judgment,
        ),
        conflicts,
    )


def _snapshot_verdict(
    build: BuildEvidenceSnapshot,
    tests: SnapshotTestStats,
    conflicts: tuple[str, ...],
) -> Literal["success", "partial", "failed", "unknown"]:
    if not build.observed or build.outcome is OperationOutcome.UNKNOWN:
        return "unknown"

    build_judge = {
        OperationOutcome.SUCCESS: "success",
        OperationOutcome.PARTIAL: "partial",
        OperationOutcome.FAILED: "failed",
        OperationOutcome.SKIPPED: "unknown",
    }.get(build.outcome)
    build_judge = rescue_blocked_build(build_judge, build.green)

    if not build.green:
        physical_verdict = "failed"
    elif tests.judgment == "success":
        physical_verdict = "success"
    elif tests.judgment == "failed":
        physical_verdict = "failed"
    else:
        physical_verdict = "partial"
    return cast(
        Literal["success", "partial", "failed", "unknown"],
        run_verdict(build_judge, physical_verdict, conflicts),
    )


def _phase_record_snapshot(record) -> PhaseRecordSnapshot:
    return PhaseRecordSnapshot.model_validate(asdict(record))


def _read_snapshot_text(orchestrator) -> str | None:
    result = orchestrator.execute_command(
        f"test -f {VERDICT_SNAPSHOT_PATH} && cat {VERDICT_SNAPSHOT_PATH}"
    )
    if result.get("exit_code") != 0 and not result.get("success"):
        return None
    output = result.get("output")
    return str(output) if output is not None else None


def _unknown_snapshot(conflict: str) -> RunVerdictSnapshot:
    return RunVerdictSnapshot(
        run_id="unknown",
        finalized_at="unknown",
        verdict="unknown",
        conflicts=(conflict,),
    )


def read_verdict_snapshot(orchestrator) -> RunVerdictSnapshot:
    """Read the immutable snapshot; missing/corrupt data never triggers recomputation."""
    content = _read_snapshot_text(orchestrator)
    if content is None:
        return _unknown_snapshot("snapshot_missing")
    try:
        return RunVerdictSnapshot.model_validate_json(content)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _unknown_snapshot("snapshot_corrupt")


class VerdictFinalizer:
    def __init__(self, orchestrator, *, test_pass_threshold: float = DEFAULT_TEST_PASS_THRESHOLD):
        self.orchestrator = orchestrator
        self.test_pass_threshold = test_pass_threshold
        self._snapshots: dict[int, RunVerdictSnapshot] = {}
        self._expected_snapshots: dict[int, RunVerdictSnapshot] = {}

    def _snapshot_for_state(self, state: RunEvidenceState) -> RunVerdictSnapshot:
        cache_key = id(state)
        cached = self._expected_snapshots.get(cache_key)
        if cached is not None:
            return cached

        build = _fold_build_evidence(state)
        tests, test_conflicts = _fold_test_stats(
            state,
            test_pass_threshold=self.test_pass_threshold,
        )
        conflicts = _dedupe([*state.conflicts, *test_conflicts])
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
            verdict=_snapshot_verdict(
                build,
                tests,
                conflicts,
            ),
            build_evidence=build,
            test_stats=tests,
            conflicts=conflicts,
            phase_records=tuple(_phase_record_snapshot(record) for record in state.phase_records),
        )
        self._expected_snapshots[cache_key] = snapshot
        return snapshot

    def has_current_snapshot(self, state: RunEvidenceState) -> bool:
        """Return whether the verdict path contains this run's canonical bytes."""
        if not state.sealed:
            return False
        snapshot = self._snapshot_for_state(state)
        content = _read_snapshot_text(self.orchestrator)
        if content != snapshot.model_dump_json():
            return False
        try:
            persisted = RunVerdictSnapshot.model_validate_json(content)
        except (ValueError, TypeError, json.JSONDecodeError):
            return False
        return persisted.run_id == state.run_id

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
        if self.has_current_snapshot(state):
            self._snapshots[cache_key] = snapshot
            return snapshot

        mkdir_result = self.orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        if mkdir_result.get("exit_code") != 0 and not mkdir_result.get("success"):
            raise OSError("failed to create verdict snapshot directory")
        atomic_write_container_text(
            self.orchestrator,
            VERDICT_SNAPSHOT_PATH,
            snapshot.model_dump_json(),
        )
        if not self.has_current_snapshot(state):
            raise OSError("persisted verdict snapshot does not match current run")
        self._snapshots[cache_key] = snapshot
        return snapshot
