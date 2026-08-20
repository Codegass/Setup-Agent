import json
from dataclasses import asdict, replace

import pytest
from container_evidence_fakes import ContainerFS

from sag.agent.evidence_publications import (
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.evidence_state import (
    EvidenceRole,
)
from sag.agent.evidence_state import RunEvidenceState as _RunEvidenceState
from sag.agent.evidence_state import (
    StateScope,
)
from sag.agent.phase_gates import ValidatorState, validate_phase_claim
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.verdict_finalizer import (
    EvidenceCloseReason,
    PhaseRecordSnapshot,
    RunVerdictSnapshot,
    SnapshotTestCounts,
    SnapshotTestStats,
)
from sag.agent.verdict_finalizer import VerdictFinalizer as _VerdictFinalizer
from sag.agent.verdict_finalizer import (
    read_verdict_snapshot,
)
from sag.evidence import EvidenceStatus, OperationOutcome, TestStats
from sag.tools.base import ToolResult

VERDICT_PATH = "/workspace/.setup_agent/verdict.json"
VERDICT_TMP_PATH = f"{VERDICT_PATH}.tmp"


class RunEvidenceState(_RunEvidenceState):
    """Direct finalizer fixtures assign the roles the engine normally owns."""

    def ingest_tool_result(self, scope, tool_name, result, provenance=None, *, roles=()):
        explicit_roles = list(roles)
        if not explicit_roles:
            if scope is StateScope.ARTIFACTS:
                explicit_roles.append(EvidenceRole.BUILD)
            if result.test_stats is not None:
                explicit_roles.append(EvidenceRole.TEST)
        return super().ingest_tool_result(
            scope,
            tool_name,
            result,
            provenance,
            roles=explicit_roles,
        )


class FakeVerdictOrchestrator:
    def __init__(self):
        self.filesystem = ContainerFS()
        self.commands = self.filesystem.commands
        self.files = self.filesystem.files

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)


class FailingReplacementOrchestrator(FakeVerdictOrchestrator):
    def __init__(self):
        super().__init__()
        self.fail_replacement = False

    def execute_command(self, command, **kwargs):
        if "fcntl.flock" in command and self.fail_replacement:
            self.commands.append(command)
            return {"success": False, "exit_code": 1, "output": "replacement failed"}
        return super().execute_command(command, **kwargs)


class _MemoryControlSink:
    path = "/host/verdict-finalizer-test-control-events.jsonl"

    def __init__(self):
        self.events = []

    def emit(self, kind, payload, *, source=None):
        del source
        self.events.append((kind, dict(payload)))


def bind_verdict_authority(orchestrator, run_id):
    authority = EvidencePublicationAuthority(run_id=run_id, sink=_MemoryControlSink())
    token = install_evidence_publication_authority(
        authority,
        orchestrator=orchestrator,
    )
    reset_evidence_publication_authority(token)
    return authority


class VerdictFinalizer(_VerdictFinalizer):
    """Bind each legacy unit double to the state run it is exercising."""

    def finalize(self, state, reason):
        candidate = getattr(self.orchestrator, "_sag_evidence_publication_authority", None)
        if (
            not isinstance(candidate, EvidencePublicationAuthority)
            or candidate.run_id != state.run_id
        ):
            bind_verdict_authority(self.orchestrator, state.run_id)
        return super().finalize(state, reason)


class _RateValidator:
    def validate_build_status(self, _project_name):
        return {
            "success": True,
            "build_complete": True,
            "evidence_status": "verified",
            "evidence": {
                "class_count": 3400,
                "source_files": 3412,
            },
            "evidence_refs": ["artifact://physical-build"],
        }

    def module_scan(self, _project_name):
        return {
            "summary": {
                "modules_built": 14,
                "modules_total": 14,
            },
            "modules": [],
            "project_dir": "/workspace/project",
        }


def _set_rate_test_rollup(state, *, passed, failed, errors, driven_modules):
    state.set_fact(
        "test.stats",
        {
            "discovered": 100,
            "unique": {
                "executed": 100,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": 0,
            },
            "raw": {
                "executed": 100,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": 0,
            },
            "flaky_count": 0,
            "driven_modules": driven_modules,
            "test_modules": ["core", "io"],
            # The provenance a live dispatch states: these counts came out of
            # the receipt-claim partition. A rollup WITHOUT the marker is the
            # shell fallback's unpartitioned corpus and is sealed as
            # unattributed volume, never as the headline.
            "receipt_scoped": True,
        },
        evidence_ref="receipt://test-rollup",
        source_phase="test",
    )


def test_v4_finalize_round_trip_carries_the_complete_rates_block():
    state = RunEvidenceState(run_id="session-rate-v4")
    _set_rate_test_rollup(
        state,
        passed=90,
        failed=10,
        errors=0,
        driven_modules=["core"],
    )
    state.set_fact(
        "coverage.summary",
        {
            "status": "collected",
            "line_rate": 55.5,
            "source": "jacoco-injected",
        },
        evidence_ref="coverage://module-metrics",
    )

    orchestrator = FakeVerdictOrchestrator()
    snapshot = VerdictFinalizer(
        orchestrator,
        validator=_RateValidator(),
        project_name="project",
    ).finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    assert snapshot.schema_version == 4
    assert snapshot.rates == {
        "build": {
            "modules": {
                "rate": 100.0,
                "band": "fully",
                "numerator": 14,
                "denominator": 14,
            },
            "classes": {
                "rate": 99.6,
                "band": "most",
                "numerator": 3400,
                "denominator": 3412,
            },
        },
        "test": {
            "cases": {
                "rate": 100.0,
                "band": "fully",
                "numerator": 100,
                "denominator": 100,
            },
            "modules": {
                "rate": 50.0,
                "band": "half",
                "numerator": 1,
                "denominator": 2,
            },
        },
        "coverage": {
            "line_rate": 55.5,
            "source": "jacoco-injected",
            "status": "collected",
        },
    }
    assert read_verdict_snapshot(orchestrator) == snapshot


def test_heavy_red_v4_is_partial_from_bands_not_failed_by_pass_rate():
    """The old 80% pass line is gone: execution is full, then heavy red
    demotes exactly the cases grain to most and records the weak conflict."""
    state = RunEvidenceState(run_id="session-heavy-red-v4")
    _set_rate_test_rollup(
        state,
        passed=20,
        failed=60,
        errors=20,
        driven_modules=["core", "io"],
    )

    snapshot = VerdictFinalizer(
        FakeVerdictOrchestrator(),
        validator=_RateValidator(),
        project_name="project",
    ).finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    assert "test_failures_heavy" in snapshot.conflicts
    assert snapshot.rates["test"]["cases"]["band"] == "most"
    assert snapshot.verdict == "partial"


def test_v3_fixture_payload_loads_with_an_empty_rates_block():
    payload = {
        "schema_version": 3,
        "run_id": "historical-v3",
        "finalized_at": "2026-08-10T00:00:00Z",
        "verdict": "partial",
    }

    snapshot = RunVerdictSnapshot.model_validate(payload)

    assert snapshot.schema_version == 3
    assert snapshot.rates == {}


def test_test_grain_rates_cases_and_modules_with_weak_signal():
    from sag.agent.verdict_finalizer import test_grain_rates

    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(
            executed=100,
            passed=20,
            failed=60,
            errors=20,
            skipped=0,
        ),
        raw=SnapshotTestCounts(
            executed=100,
            passed=20,
            failed=60,
            errors=20,
            skipped=0,
        ),
    )

    grains, conflicts = test_grain_rates(
        stats,
        driven_modules={"/w/p/core"},
        test_modules={"/w/p/core", "/w/p/io"},
    )

    assert grains["cases"].band == "most"
    assert grains["cases"].rate == 100.0
    assert conflicts == ("test_failures_heavy",)
    assert grains["modules"].payload()["numerator"] == 1
    assert grains["modules"].band == "half"


def test_test_grain_rates_type_their_absences():
    from sag.agent.verdict_finalizer import test_grain_rates

    stats = SnapshotTestStats(
        discovered=None,
        unique=SnapshotTestCounts(),
        raw=SnapshotTestCounts(),
    )

    grains, conflicts = test_grain_rates(
        stats,
        driven_modules=set(),
        test_modules=set(),
    )

    assert grains["cases"].payload()["reason"] == "static discovery found no count"
    assert grains["modules"].payload()["reason"] == "no test modules surveyed"
    assert conflicts == ()


def _record_machine_history(state: RunEvidenceState, machine: PhaseMachine) -> None:
    for record in machine.records:
        state.record_phase_record(record)


def _tvm_state() -> RunEvidenceState:
    state = RunEvidenceState(run_id="session-tvm")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed(
            output="native and Python artifacts built",
            operation_outcome=OperationOutcome.PARTIAL,
            evidence_status=EvidenceStatus.VERIFIED,
            facts={"build_success": True, "build_complete": False},
            refs=["output_build"],
        ),
        provenance="output_build",
    )
    for attempt in range(3):
        state.ingest_tool_result(
            StateScope.TEST_RUNTIME,
            "build",
            ToolResult.completed_success(
                output=f"pytest retry {attempt + 1}",
                test_stats=TestStats(
                    discovered=328,
                    executed=328,
                    passed=328,
                    failed=0,
                    skipped=0,
                ),
                refs=[f"output_test_{attempt + 1}"],
            ),
            provenance=f"output_test_{attempt + 1}",
        )

    machine = PhaseMachine()
    machine.mark_done("cloned", ["output_clone"])
    machine.mark_done("analyzed", ["output_analysis"])
    machine.mark_done("native core partial; imports verified", ["output_build"])
    machine.mark_done("328 tests green", ["output_test_3"])
    _record_machine_history(state, machine)
    return state


def test_finalization_is_byte_identical_and_uses_compare_publish_cas():
    orchestrator = FakeVerdictOrchestrator()
    state = _tvm_state()
    finalizer = VerdictFinalizer(orchestrator)

    first = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
    commands_after_first = list(orchestrator.commands)
    second = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.schema_version == 4
    assert orchestrator.files[VERDICT_PATH] == first.model_dump_json()
    assert VERDICT_TMP_PATH not in orchestrator.files
    assert not any(
        "fcntl.flock" in command for command in orchestrator.commands[len(commands_after_first) :]
    )
    assert sum("fcntl.flock" in command for command in orchestrator.commands) == 1
    assert not any(path.startswith(f"{VERDICT_PATH}.candidate.") for path in orchestrator.files)


@pytest.mark.parametrize("disk_state", ["missing", "corrupt", "other_run", "stale"])
def test_cached_finalization_rejects_noncurrent_persisted_snapshot(disk_state):
    orchestrator = FakeVerdictOrchestrator()
    state = _tvm_state()
    finalizer = VerdictFinalizer(orchestrator)
    snapshot = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
    persisted = json.loads(snapshot.model_dump_json())

    if disk_state == "missing":
        del orchestrator.files[VERDICT_PATH]
    elif disk_state == "corrupt":
        orchestrator.files[VERDICT_PATH] = "{not-json"
    elif disk_state == "other_run":
        persisted["run_id"] = "another-run"
        orchestrator.files[VERDICT_PATH] = json.dumps(
            persisted,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        persisted["verdict"] = "unknown"
        orchestrator.files[VERDICT_PATH] = json.dumps(
            persisted,
            sort_keys=True,
            separators=(",", ":"),
        )
    disk_bytes = orchestrator.files.get(VERDICT_PATH)

    with pytest.raises(RuntimeError, match="cached verdict snapshot is not current"):
        finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    assert orchestrator.files.get(VERDICT_PATH) == disk_bytes


def test_conflicting_reason_is_rejected_before_read_write_or_state_change():
    orchestrator = FakeVerdictOrchestrator()
    state = _tvm_state()
    finalizer = VerdictFinalizer(orchestrator)
    first = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
    commands_after_first = list(orchestrator.commands)

    with pytest.raises(ValueError, match="conflicting evidence-close reason"):
        finalizer.finalize(state, EvidenceCloseReason.ABORTED)

    assert state.close_reason == EvidenceCloseReason.TEST_TERMINATED.value
    assert orchestrator.commands == commands_after_first
    assert orchestrator.files[VERDICT_PATH] == first.model_dump_json()


def test_sealed_retry_never_accepts_or_caches_an_older_run_snapshot():
    orchestrator = FailingReplacementOrchestrator()
    old_state = RunEvidenceState(run_id="older-run")
    old_snapshot = VerdictFinalizer(orchestrator).finalize(old_state, EvidenceCloseReason.ABORTED)
    new_state = RunEvidenceState(run_id="current-run")
    finalizer = VerdictFinalizer(orchestrator)
    orchestrator.fail_replacement = True

    with pytest.raises(OSError, match="compare-and-publish"):
        finalizer.finalize(new_state, EvidenceCloseReason.ABORTED)
    with pytest.raises(OSError, match="compare-and-publish"):
        finalizer.finalize(new_state, EvidenceCloseReason.ABORTED)

    assert new_state.sealed is True
    assert read_verdict_snapshot(orchestrator).run_id == old_snapshot.run_id
    assert id(new_state) not in finalizer._snapshots


def test_console_retry_volume_is_disclosed_and_never_a_headline():
    """Rebased 2026-08-14 (spec amendment item 9). TVM's three pytest retries
    were parsed out of CONSOLE TEXT: no report file was opened and no receipt
    claims them, so they are unattributed volume exactly like an unclaimed XML
    on disk. The selection still chooses the basis whose ``discovered`` this
    snapshot seals; what it never does any more is publish a headline count
    nobody can attribute."""
    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        _tvm_state(), EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.verdict == "partial"
    assert snapshot.test_stats.discovered == 328
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.judgment == "unknown"
    # Moved, never deleted: the three retries' rows are the excluded volume.
    assert snapshot.test_stats.auxiliary_test_stats == {
        "executed": 984,
        "passed": 984,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert "test_executions_unattributed_to_receipts" in snapshot.conflicts
    serialized = snapshot.model_dump()["test_stats"]
    assert serialized["unique"] == {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert serialized["unattributed_source"] == "tool_observations"
    assert "executed" not in serialized
    assert "pass_rate" not in serialized


def test_v2_flat_test_stats_upgrade_to_explicit_unique_basis():
    from sag.agent.verdict_finalizer import SnapshotTestStats

    stats = SnapshotTestStats.model_validate(
        {
            "discovered": 10,
            "executed": 10,
            "passed": 9,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "raw": {"executed": 12, "passed": 10, "failed": 2},
            "flaky_count": 2,
        }
    )

    assert stats.executed == 10
    assert stats.unique.passed == 9
    assert stats.raw.executed == 12
    assert stats.flaky_count == 2
    assert "executed" not in stats.model_dump()


def test_compileall_basis_mismatch_is_a_snapshot_metrics_conflict():
    state = RunEvidenceState(run_id="session-compileall-conflict")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="compileall invalid",
            facts={"build_success": True},
            conflicts=["metrics_conflict"],
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="tests green",
            test_stats=TestStats(
                discovered=10,
                executed=10,
                passed=10,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert "metrics_conflict" in snapshot.conflicts
    assert snapshot.verdict == "partial"


def test_flaky_count_flows_into_unique_snapshot_basis():
    """Rebased 2026-08-14 onto the provenance a live dispatch states.

    A flaky tally is computed over identities the headline claims, so it can
    only be sealed where those identities are: a receipt-scoped rollup. Stated
    over console text it was a fact with no basis, which is why the observation
    fold now drops it with the counts it came from.
    """
    state = RunEvidenceState(run_id="session-flaky")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.register_fact(
        StateScope.TEST_RUNTIME,
        "test.stats",
        {
            "discovered": 5,
            "unique": {"executed": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
            "raw": {"executed": 7, "passed": 5, "failed": 2, "errors": 0, "skipped": 0},
            "flaky_count": 2,
            "receipt_scoped": True,
        },
        "artifact://test-rollup",
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.test_stats.unique.passed == 5
    assert snapshot.test_stats.flaky_count == 2
    assert snapshot.model_dump()["test_stats"]["flaky_count"] == 2


def test_narrow_passing_retry_cannot_replace_failed_full_suite_basis():
    state = RunEvidenceState(run_id="session-targeted-retry")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="full suite failed",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=100,
                executed=100,
                passed=0,
                failed=100,
                skipped=0,
            ),
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="targeted retry passed",
            test_stats=TestStats(
                discovered=1,
                executed=1,
                passed=1,
                failed=0,
                skipped=0,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14 (spec amendment item 9): console counts are excluded
    # volume, so the selection is read where it still decides something — the
    # discovered basis. The one-test retry's `discovered: 1` did not replace
    # the full suite's 100.
    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 0
    # Premise updated 2026-08-10: red tests grade execution, not pass rate.
    assert snapshot.verdict == "partial"
    assert snapshot.test_stats.auxiliary_test_stats["executed"] == 101
    assert snapshot.test_stats.auxiliary_test_stats["failed"] == 100


def test_executed_count_preserves_broader_suite_when_it_exceeds_discovered():
    state = RunEvidenceState(run_id="session-parameterized-drift")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="parameterized full suite failed",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=80,
                executed=100,
                passed=0,
                failed=100,
                skipped=0,
            ),
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="smaller retry passed",
            test_stats=TestStats(
                discovered=90,
                executed=90,
                passed=90,
                failed=0,
                skipped=0,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14: the 100-executed basis still wins the selection, so
    # its `discovered: 80` is the denominator sealed — the 90/90 retry did not
    # take it.
    assert snapshot.test_stats.discovered == 80
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats["executed"] == 190
    # Premise updated 2026-08-10: red tests grade execution, not pass rate.
    assert snapshot.verdict == "partial"


def test_pareto_incomparable_retry_keeps_broader_failed_execution_basis():
    state = RunEvidenceState(run_id="session-pareto-incomparable")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="full suite failed",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=100,
                executed=100,
                passed=0,
                failed=100,
                skipped=0,
            ),
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="narrow discovery retry passed",
            test_stats=TestStats(
                discovered=101,
                executed=1,
                passed=1,
                failed=0,
                skipped=0,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14: the broader basis still wins, and the incomparable
    # frontier is still named — the counts are just disclosed rather than
    # published as a headline nobody can attribute.
    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats["failed"] == 100
    assert "test_stats_basis_incomparable" in snapshot.conflicts
    # Red is execution evidence; the incomparable basis remains an integrity cap.
    assert snapshot.verdict == "partial"


def test_dominant_complete_basis_supersedes_missing_discovered_without_conflict():
    state = RunEvidenceState(run_id="session-complete-dominates-incomplete")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="incomplete basis failed",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=None,
                executed=50,
                passed=0,
                failed=50,
                skipped=0,
            ),
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="complete dominant suite passed",
            test_stats=TestStats(
                discovered=100,
                executed=100,
                passed=100,
                failed=0,
                skipped=0,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14: the complete basis dominates, so its discovered count
    # is the one sealed and no incomparable-frontier conflict is minted.
    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats["executed"] == 150
    assert "test_stats_basis_incomparable" not in snapshot.conflicts
    # No module scan was supplied, so build.modules is unavailable, not fully.
    assert snapshot.verdict == "partial"


def test_equal_complete_basis_uses_latest_typed_status():
    state = RunEvidenceState(run_id="session-equal-basis-latest")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="first full suite failed",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=100,
                executed=100,
                passed=0,
                failed=100,
                skipped=0,
            ),
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="latest equal suite passed",
            test_stats=TestStats(
                discovered=100,
                executed=100,
                passed=100,
                failed=0,
                skipped=0,
            ),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14: equal bases collapse to one frontier entry, so no
    # incomparable conflict is minted; the only conflict left is the disclosure
    # of the volume itself.
    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats["executed"] == 200
    assert snapshot.conflicts == ("test_executions_unattributed_to_receipts",)
    # No module scan was supplied, so build.modules is unavailable, not fully.
    assert snapshot.verdict == "partial"


def test_untyped_test_count_facts_do_not_manufacture_primary_stats():
    state = RunEvidenceState(run_id="session-untyped-counts")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "bash",
        ToolResult.completed_success(
            output="untyped parser summary",
            facts={"executed": 1, "passed": 1, "failed": 0, "skipped": 0},
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.test_stats.discovered is None
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.raw.executed == 0
    assert snapshot.verdict == "partial"


def test_validator_rollup_facts_are_the_canonical_snapshot_basis():
    state = RunEvidenceState(run_id="session-cassandra-rollup")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="reactor compiled",
            facts={"build_success": True},
        ),
    )
    state.set_fact(
        "build.compiled_classes",
        8916,
        evidence_ref="artifact://classes",
        source_phase="build",
    )
    state.set_fact(
        "test.stats",
        {
            "discovered": 4928,
            "unique": {
                "executed": 4928,
                "passed": 4598,
                "failed": 0,
                "errors": 156,
                "skipped": 174,
            },
            "raw": {
                "executed": 5000,
                "passed": 4660,
                "failed": 0,
                "errors": 160,
                "skipped": 180,
            },
            "flaky_count": 3,
            "conflicts": ["test_errors_detected"],
            "receipt_scoped": True,
        },
        evidence_ref="report://surefire",
        source_phase="test",
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.build_evidence.compiled_classes == 8916
    assert snapshot.test_stats.unique.executed == 4928
    assert snapshot.test_stats.unique.passed == 4598
    assert snapshot.test_stats.unique.errors == 156
    assert snapshot.test_stats.raw.executed == 5000
    assert snapshot.test_stats.flaky_count == 3
    assert snapshot.conflicts == ("test_errors_detected",)
    # No module scan was supplied, so build.modules is unavailable, not fully.
    assert snapshot.verdict == "partial"


def test_snapshot_separates_maven_failures_from_errors():
    state = RunEvidenceState(run_id="session-errors")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="tests terminal",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=12,
                executed=12,
                passed=6,
                failed=5,
                skipped=1,
            ),
            metadata={
                "analysis": {
                    "tests_run": {
                        "total": 12,
                        "failures": 2,
                        "errors": 3,
                        "skipped": 1,
                    }
                }
            },
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    # Rebased 2026-08-14: the split is the property under test and it survives
    # into the destination the console volume now lands in.
    assert snapshot.test_stats.auxiliary_test_stats["failed"] == 2
    assert snapshot.test_stats.auxiliary_test_stats["errors"] == 3
    assert snapshot.test_stats.failed == 0
    assert snapshot.test_stats.errors == 0


def test_conflict_caps_a_green_physical_verdict_at_partial():
    state = RunEvidenceState(run_id="session-conflict")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="build complete",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="tests complete",
            test_stats=TestStats(executed=10, passed=10, failed=0, skipped=0),
            conflicts=["test_report_parse_ambiguous"],
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.verdict == "partial"
    assert snapshot.conflicts == (
        "test_report_parse_ambiguous",
        # Rebased 2026-08-14: the console volume is disclosed beside it.
        "test_executions_unattributed_to_receipts",
    )


def test_verified_build_evidence_rescues_failed_build_judge_to_partial():
    state = RunEvidenceState(run_id="session-rescue")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_failure(
            output="tool reported failure after artifacts were verified",
            error="build tool failed",
            error_code="BUILD_TOOL_FAILED",
            facts={"build_success": True},
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="tests complete",
            test_stats=TestStats(executed=10, passed=10, failed=0, skipped=0),
        ),
    )

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.build_evidence.green is True
    assert snapshot.verdict == "partial"


def test_phase_records_are_preserved_as_detached_audit_history():
    state = _tvm_state()
    expected = [asdict(record) for record in state.phase_records]

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    actual = snapshot.model_dump(mode="json")["phase_records"]
    expected_json = json.loads(json.dumps(expected, default=lambda value: value.value))
    for record in expected_json:
        record["prerequisite_ref"] = ""
    assert actual == expected_json
    assert snapshot.verdict == "partial", "phase outcomes are audit-only verdict inputs"


def test_phase_claim_and_validated_outcome_are_preserved_for_audit():
    state = _tvm_state()
    machine = PhaseMachine()
    validation = validate_phase_claim(
        PhaseClaim(phase="provision", claimed_outcome=PhaseOutcome.FAILED),
        ValidatorState.GREEN,
    )
    record = replace(machine.close_attempt(validation), attempt_id="provision-claim-2")
    state.record_phase_record(record)

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    audited = snapshot.phase_records[-1]
    assert audited.claim is not None
    assert audited.claim.claimed_outcome == "failed"
    assert audited.validated_outcome == "success"
    assert audited.outcome == "success"
    assert audited.claim_disposition == "pessimistic"
    serialized_claim = snapshot.model_dump(mode="json")["phase_records"][-1]["claim"]
    assert "execution_plan_sha256" not in serialized_claim
    assert "execution_plan_ref" not in serialized_claim
    legacy_bytes = snapshot.model_dump_json()
    assert RunVerdictSnapshot.model_validate_json(legacy_bytes).model_dump_json() == legacy_bytes


def test_phase_claim_execution_plan_identity_round_trips_in_verdict_snapshot():
    state = _tvm_state()
    machine = PhaseMachine()
    machine.mark_done("workspace ready", ["output_clone"])
    validation = validate_phase_claim(
        PhaseClaim(
            phase="analyze",
            claimed_outcome=PhaseOutcome.SUCCESS,
            execution_plan_sha256="a" * 64,
            execution_plan_ref="/workspace/.setup_agent/project_execution_plan.json",
        ),
        ValidatorState.GREEN,
    )
    record = replace(machine.close_attempt(validation), attempt_id="analyze-plan-2")
    state.record_phase_record(record)

    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        state, EvidenceCloseReason.TEST_TERMINATED
    )

    serialized = snapshot.model_dump(mode="json")["phase_records"][-1]["claim"]
    assert serialized["execution_plan_sha256"] == "a" * 64
    assert serialized["execution_plan_ref"] == "/workspace/.setup_agent/project_execution_plan.json"
    reread = RunVerdictSnapshot.model_validate_json(snapshot.model_dump_json())
    assert reread.phase_records[-1].claim is not None
    assert reread.phase_records[-1].claim.execution_plan_sha256 == "a" * 64


def test_phase_record_snapshot_upgrades_pre_claim_validation_shape():
    record = PhaseRecordSnapshot.model_validate(
        {
            "phase": "build",
            "attempt_id": "build-1",
            "termination": "completed",
            "outcome": "failed",
            "transition": "",
            "evidence": ["output_1"],
        }
    )

    assert record.validated_outcome == "failed"
    assert record.evidence_refs == ("output_1",)
    assert record.transition is None


def test_sealed_state_rejects_all_later_evidence_mutation():
    state = _tvm_state()
    VerdictFinalizer(FakeVerdictOrchestrator()).finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    with pytest.raises(RuntimeError, match="sealed"):
        state.ingest_tool_result(
            StateScope.TEST_RUNTIME,
            "build",
            ToolResult.completed_success(output="late report-side evidence"),
        )


def test_read_missing_snapshot_returns_unknown_without_recomputation():
    snapshot = read_verdict_snapshot(FakeVerdictOrchestrator())

    assert snapshot.verdict == "unknown"
    assert snapshot.conflicts == ("snapshot_missing",)
    assert snapshot.input_refs == ()


def test_read_corrupt_snapshot_returns_unknown_without_recomputation():
    orchestrator = FakeVerdictOrchestrator()
    orchestrator.files[VERDICT_PATH] = "{definitely not JSON"

    snapshot = read_verdict_snapshot(orchestrator)

    assert snapshot.verdict == "unknown"
    assert snapshot.conflicts == ("snapshot_corrupt",)
    assert snapshot.input_refs == ()


def test_read_round_trips_the_persisted_immutable_snapshot():
    orchestrator = FakeVerdictOrchestrator()
    written = VerdictFinalizer(orchestrator).finalize(
        _tvm_state(), EvidenceCloseReason.TEST_TERMINATED
    )

    read_back = read_verdict_snapshot(orchestrator)

    assert read_back.model_dump_json() == written.model_dump_json()
    with pytest.raises(Exception):
        read_back.verdict = "failed"
