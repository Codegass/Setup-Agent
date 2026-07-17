import json
from dataclasses import asdict, replace

import pytest

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
    VerdictFinalizer,
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
        self.commands = []
        self.files = {}

    def execute_command(self, command):
        self.commands.append(command)

        if command.startswith("mkdir -p "):
            return {"success": True, "exit_code": 0, "output": ""}

        if command.startswith("test -f ") and " && cat " in command:
            path = command.split()[2]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.files[path]}

        if command.startswith("cat > "):
            path = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.files[path] = payload + "\n"
            return {"success": True, "exit_code": 0, "output": ""}

        if command.startswith("truncate -s -1 "):
            path = command.split()[-1]
            self.files[path] = self.files[path][:-1]
            return {"success": True, "exit_code": 0, "output": ""}

        if command.startswith("mv "):
            _, source, target = command.split()
            self.files[target] = self.files.pop(source)
            return {"success": True, "exit_code": 0, "output": ""}

        return {"success": True, "exit_code": 0, "output": ""}


class FailingReplacementOrchestrator(FakeVerdictOrchestrator):
    def __init__(self):
        super().__init__()
        self.fail_replacement = False

    def execute_command(self, command):
        if command.startswith("mv ") and self.fail_replacement:
            self.commands.append(command)
            return {"success": False, "exit_code": 1, "output": "replacement failed"}
        return super().execute_command(command)


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


def test_finalization_is_byte_identical_and_uses_atomic_rename():
    orchestrator = FakeVerdictOrchestrator()
    state = _tvm_state()
    finalizer = VerdictFinalizer(orchestrator)

    first = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
    commands_after_first = list(orchestrator.commands)
    second = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.schema_version == 3
    assert orchestrator.files[VERDICT_PATH] == first.model_dump_json()
    assert VERDICT_TMP_PATH not in orchestrator.files
    assert orchestrator.commands[len(commands_after_first) :] == [
        f"test -f {VERDICT_PATH} && cat {VERDICT_PATH}"
    ]
    temp_write_index = next(
        index for index, command in enumerate(orchestrator.commands) if VERDICT_TMP_PATH in command
    )
    rename_index = orchestrator.commands.index(f"mv {VERDICT_TMP_PATH} {VERDICT_PATH}")
    assert temp_write_index < rename_index


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

    with pytest.raises(OSError, match="atomically rename"):
        finalizer.finalize(new_state, EvidenceCloseReason.ABORTED)
    with pytest.raises(OSError, match="atomically rename"):
        finalizer.finalize(new_state, EvidenceCloseReason.ABORTED)

    assert new_state.sealed is True
    assert read_verdict_snapshot(orchestrator).run_id == old_snapshot.run_id
    assert id(new_state) not in finalizer._snapshots


def test_snapshot_uses_unique_test_counts_and_keeps_raw_retries_secondary():
    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        _tvm_state(), EvidenceCloseReason.TEST_TERMINATED
    )

    assert snapshot.verdict == "partial"
    assert snapshot.test_stats.discovered == 328
    assert snapshot.test_stats.executed == 328
    assert snapshot.test_stats.passed == 328
    assert snapshot.test_stats.raw.executed == 984
    assert snapshot.test_stats.pass_rate == 100.0
    serialized = snapshot.model_dump()["test_stats"]
    assert serialized["unique"] == {
        "executed": 328,
        "passed": 328,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert serialized["raw"]["executed"] == 984
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
    state = RunEvidenceState(run_id="session-flaky")
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
            output="tests green with retries",
            test_stats=TestStats(
                discovered=5,
                executed=5,
                passed=5,
                flaky_count=2,
            ),
        ),
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

    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 100
    assert snapshot.test_stats.passed == 0
    assert snapshot.test_stats.failed == 100
    assert snapshot.verdict == "failed"
    assert snapshot.test_stats.raw.executed == 101


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

    assert snapshot.test_stats.discovered == 80
    assert snapshot.test_stats.executed == 100
    assert snapshot.test_stats.failed == 100
    assert snapshot.verdict == "failed"


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

    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 100
    assert snapshot.test_stats.failed == 100
    assert "test_stats_basis_incomparable" in snapshot.conflicts
    assert snapshot.verdict == "failed"


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

    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 100
    assert snapshot.test_stats.passed == 100
    assert "test_stats_basis_incomparable" not in snapshot.conflicts
    assert snapshot.verdict == "success"


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

    assert snapshot.test_stats.discovered == 100
    assert snapshot.test_stats.executed == 100
    assert snapshot.test_stats.passed == 100
    assert snapshot.test_stats.failed == 0
    assert snapshot.conflicts == ()
    assert snapshot.verdict == "success"


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

    assert snapshot.test_stats.failed == 2
    assert snapshot.test_stats.errors == 3
    assert snapshot.test_stats.raw.failed == 2
    assert snapshot.test_stats.raw.errors == 3


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
    assert snapshot.conflicts == ("test_report_parse_ambiguous",)


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
