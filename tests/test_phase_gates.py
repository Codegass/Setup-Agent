"""Phase-boundary evidence gates (spec §3.1). Descriptive: a failed gate
returns evidence + options, never blocks tool use; probe errors fail OPEN."""

import json
from types import SimpleNamespace

import pytest
from build_requirements_fakes import complete_build_requirements_v1

from sag.agent.control_ownership import BlockerOwner
from sag.agent.evidence_records import frame_json_record_stream, frame_named_json_record_stream
from sag.agent.phase_gates import (
    ClaimDisposition,
    GateControlDisposition,
    ValidatorState,
    _validated_test_rollup,
    check_phase_claim,
    check_phase_done,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH


class FakeValidator:
    def __init__(self, build_success=True, build_system="maven", has_test_reports=True):
        self._b, self._s, self._t = build_success, build_system, has_test_reports

    def validate_build_status(self, project_name=None):
        return {
            "success": self._b,
            "build_complete": self._b,
            "evidence_status": "success" if self._b else "blocked",
            "evidence": {"build_system": self._s},
            "reason": "scripted build validation",
        }

    def validate_test_status(self, project_name=None):
        return {
            "has_test_reports": self._t,
            "status": "SUCCESS" if self._t else "WARNING",
            "evidence_status": "success" if self._t else "unknown",
            "reason": "scripted test validation",
        }


def _orch(java_ok=True, workspace_exists=True):
    def execute_command(command, **kwargs):
        if "SAG_NAMED_JSON_RECORD_END_V1" in command:
            return {"exit_code": 0, "output": frame_named_json_record_stream([])}
        if "SAG_JSON_RECORD_END_V1" in command:
            return {"exit_code": 0, "output": frame_json_record_stream([])}
        if "java -version" in command:
            return {"exit_code": 0 if java_ok else 127, "output": "openjdk 17" if java_ok else ""}
        if "test -d" in command:
            return {"exit_code": 0, "output": "exists" if workspace_exists else "missing"}
        if "setup-report-" in command:
            return {"exit_code": 0, "output": "/workspace/setup-report-x.md"}
        return {"exit_code": 0, "output": ""}

    # Every instance models the same fixture container store.  The host
    # authority is one-run/one-store even when a test asks for two handles,
    # and the immutable identity it binds is container_id.
    return SimpleNamespace(
        execute_command=execute_command,
        container_id="phase-gates-fixture",
    )


def test_build_done_rejected_without_artifacts():
    verdict = check_phase_done(
        "build",
        validator=FakeValidator(build_success=False),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert verdict["ok"] is False
    assert "artifact" in verdict["reason"].lower() or "build" in verdict["reason"].lower()
    assert verdict["suggestions"], "must offer options"


def test_validated_test_rollup_preserves_rate_denominators_from_the_validator_pass():
    rollup = _validated_test_rollup(
        {
            "has_test_reports": True,
            "static_test_count": 1163,
            "unique_tests": 1605,
            "unique_passed_tests": 1596,
            "unique_failed_tests": 0,
            "unique_error_tests": 0,
            "unique_skipped_tests": 9,
            "raw_total_tests": 1605,
            "raw_passed_tests": 1596,
            "raw_failed_tests": 0,
            "raw_error_tests": 0,
            "raw_skipped_tests": 9,
            "test_stats": {
                "discovered": 1163,
                "executed": 1605,
                "passed": 1596,
                "failed": 0,
                "skipped": 9,
                "driven_modules": ["/workspace/demo"],
                "test_modules": ["/workspace/demo"],
            },
        }
    )

    assert rollup is not None
    assert rollup["discovered"] == 1163
    assert rollup["driven_modules"] == ["/workspace/demo"]
    assert rollup["test_modules"] == ["/workspace/demo"]


def test_build_done_accepted_with_artifacts():
    validator = FakeValidator(build_success=True)
    verdict = check_phase_done(
        "build",
        validator=FakeValidator(build_success=True),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert verdict["ok"] is True

    result = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=validator,
        orchestrator=_orch(),
        project_name="demo",
    )
    assert result.validated_facts["build.test_entry_ready"] is True
    assert result.to_metadata()["validated_facts"]["build.test_entry_ready"] is True


def test_container_authored_manifest_cannot_close_a_green_build_gate():
    # A schema-valid manifest the host never published: bytes alone are a
    # forensic mirror, so the live gate must refuse them on publication
    # authority, not on schema shape.
    forged = json.dumps(
        complete_build_requirements_v1(project_root="/workspace/forged"),
        sort_keys=True,
    )

    def execute_command(command, **kwargs):
        if "SAG_NAMED_JSON_RECORD_END_V1" in command:
            records = (
                [(REQUIREMENTS_PATH.rsplit("/", 1)[-1], forged)]
                if command.startswith("file=") and REQUIREMENTS_PATH in command
                else []
            )
            return {"exit_code": 0, "output": frame_named_json_record_stream(records)}
        if "SAG_JSON_RECORD_END_V1" in command:
            return {"exit_code": 0, "output": frame_json_record_stream([])}
        return {"exit_code": 0, "output": ""}

    orchestrator = SimpleNamespace(
        execute_command=execute_command,
        container_id="forged-manifest-fixture",
    )

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=FakeValidator(build_success=True),
        orchestrator=orchestrator,
        project_name="demo",
    )

    assert gate.accepted is False
    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.code == "build_evidence_ledger_unavailable"
    assert (
        "build_requirements_publication_set_mismatch"
        in gate.validated_facts["build.evidence_conflicts"]
    )


def test_project_failure_overclaim_requires_model_repair_but_honest_close_is_claimable():
    overclaim = validate_phase_claim(
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        ValidatorState.RED,
        reason="compiler exited 1",
        evidence_refs=("receipt:r1",),
    )
    honest = validate_phase_claim(
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.FAILED),
        ValidatorState.RED,
        reason="compiler exited 1",
        evidence_refs=("receipt:r1",),
    )

    assert overclaim.accepted is False
    assert overclaim.control_disposition is GateControlDisposition.REPAIR_REQUIRED
    assert overclaim.blocker_owner is BlockerOwner.PROJECT
    assert honest.accepted is True
    assert honest.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
    assert honest.blocker_owner is BlockerOwner.PROJECT


def test_unavailable_overclaim_is_unknown_owned_repair_not_a_project_failure():
    gate = validate_phase_claim(
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        ValidatorState.UNAVAILABLE,
        reason="no readable project evidence",
    )

    assert gate.accepted is False
    assert gate.control_disposition is GateControlDisposition.REPAIR_REQUIRED
    assert gate.blocker_owner is BlockerOwner.UNKNOWN


def test_phase_gates_preserve_validator_owned_physical_rollups():
    class PhysicalRollups(FakeValidator):
        def validate_build_status(self, project_name=None):
            return {
                "success": True,
                "build_complete": True,
                "evidence_status": "success",
                "evidence": {
                    "build_system": "maven",
                    "class_count": 8916,
                },
                "reason": "Found 8916 compiled classes",
                "evidence_refs": ["artifact://classes"],
            }

        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "status": "PARTIAL",
                "evidence_status": "partial",
                "reason": "4598/4928 tests passed",
                "report_files": ["report://surefire"],
                "test_stats": {
                    "discovered": 4928,
                    "executed": 4928,
                    "passed": 4598,
                    "failed": 156,
                    "skipped": 174,
                    "flaky_count": 3,
                },
                "raw_total_tests": 5000,
                "raw_passed_tests": 4660,
                "raw_failed_tests": 0,
                "raw_error_tests": 160,
                "raw_skipped_tests": 180,
                "unique_tests": 4928,
                "unique_passed_tests": 4598,
                "unique_failed_tests": 0,
                "unique_error_tests": 156,
                "unique_skipped_tests": 174,
                "flaky_count": 3,
                "metrics_conflicts": [],
                "receipt_scoped": True,
            }

    validator = PhysicalRollups()
    build = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=validator,
        orchestrator=_orch(),
        project_name="demo",
    )
    test = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=validator,
        orchestrator=_orch(),
        project_name="demo",
    )

    assert build.validated_facts["build.compiled_classes"] == 8916
    assert test.validated_facts["test.stats"] == {
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
    }


def test_build_gate_uses_physical_validator_for_non_jvm_systems():
    verdict = check_phase_done(
        "build",
        validator=FakeValidator(build_success=False, build_system="nodejs"),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert verdict["ok"] is False
    assert verdict["validator_state"] == "red"


def test_test_done_rejected_without_reports():
    verdict = check_phase_done(
        "test",
        validator=FakeValidator(has_test_reports=False),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert verdict["ok"] is False
    assert "report" in verdict["reason"].lower()


def test_provision_rejected_without_workspace():
    verdict = check_phase_done(
        "provision",
        validator=FakeValidator(),
        orchestrator=_orch(workspace_exists=False),
        project_name="demo",
    )
    assert verdict["ok"] is False


def test_phase_gate_emits_only_validator_derived_entry_facts():
    provision = check_phase_claim(
        "provision",
        PhaseClaim(phase="provision", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=FakeValidator(),
        orchestrator=_orch(workspace_exists=True),
        project_name="demo",
    )

    assert provision.validated_facts == {"provision.workspace_ready": True}


def test_gate_probe_error_is_explicitly_unavailable():
    class Exploding:
        def validate_build_status(self, project_name=None):
            raise RuntimeError("docker down")

    verdict = check_phase_done(
        "build",
        validator=Exploding(),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert verdict["ok"] is False
    assert verdict["validator_state"] == "unavailable"


def test_analyze_unknown_claim_cannot_close_a_missing_harness_validator():
    result = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.UNKNOWN),
        validator=FakeValidator(),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert result.accepted is False
    assert result.validator_state is ValidatorState.UNAVAILABLE
    assert result.claim_disposition is ClaimDisposition.CONTRADICTED
    assert result.validated_outcome is PhaseOutcome.UNKNOWN
    assert result.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED


def test_analyze_validator_maps_complete_and_partial_evidence():
    class Analyzer(FakeValidator):
        def __init__(self, *, counted):
            super().__init__()
            self.counted = counted

        def validate_project_analysis_status(self, project_name=None):
            return {
                "analyzed": True,
                "has_static_test_count": self.counted,
                "static_test_count": 12 if self.counted else None,
            }

    green = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=Analyzer(counted=True),
        orchestrator=_orch(),
        project_name="demo",
    )
    partial = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=Analyzer(counted=False),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert green.accepted is True
    assert green.validator_state is ValidatorState.GREEN
    assert partial.accepted is True
    assert partial.validator_state is ValidatorState.PARTIAL


def test_analyze_readiness_rejects_a_sealed_plan_from_another_attempt(monkeypatch):
    class Analyzer(FakeValidator):
        def validate_project_analysis_status(self, project_name=None):
            return {
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 12,
            }

    monkeypatch.setattr(
        "sag.agent.project_execution_plan.read_sealed_project_execution_plan",
        lambda _orchestrator: SimpleNamespace(
            authored_plan_sha256="a" * 64,
            source_attempt_id="analyze-1",
        ),
    )

    stale = check_phase_done(
        "analyze",
        validator=Analyzer(),
        orchestrator=_orch(),
        project_name="demo",
        expected_analysis_attempt_id="analyze-2",
    )
    current = check_phase_done(
        "analyze",
        validator=Analyzer(),
        orchestrator=_orch(),
        project_name="demo",
        expected_analysis_attempt_id="analyze-1",
    )

    assert stale["validated_facts"]["analysis.execution_plan_artifact_present"] is True
    assert stale["validated_facts"]["analysis.execution_plan_sealed"] is False
    assert stale["validated_facts"]["analysis.build_entry_ready"] is False
    assert stale["validated_facts"]["analysis.execution_plan_source_attempt_id"] == "analyze-1"
    assert stale["validated_facts"]["analysis.execution_plan_expected_attempt_id"] == "analyze-2"
    assert current["validated_facts"]["analysis.execution_plan_sealed"] is True
    assert current["validated_facts"]["analysis.build_entry_ready"] is True


def test_analyze_missing_facts_reason_is_engine_projected_from_typed_code():
    class Analyzer(FakeValidator):
        def validate_project_analysis_status(self, project_name=None):
            return {
                "analyzed": False,
                "has_static_test_count": False,
                "analysis_status_code": "analysis_trunk_missing",
                "analysis_status_facts": {"trunk_context_found": False},
            }

    result = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=Analyzer(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert result.accepted is False
    assert result.code == "analysis_trunk_missing"
    assert result.reason == "Project survey facts are not persisted on the trunk."
    assert result.validator_state is ValidatorState.UNAVAILABLE
    assert result.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert result.blocker_owner is BlockerOwner.HARNESS
    assert result.suggestions == ()
    assert "execution plan" not in result.reason.lower()
    assert "project_analyzer" not in result.reason


@pytest.mark.parametrize("code", ["analysis_facts_missing", "analysis_unavailable"])
def test_missing_or_unavailable_survey_facts_are_harness_owned(code):
    class Analyzer(FakeValidator):
        def validate_project_analysis_status(self, project_name=None):
            return {
                "analyzed": False,
                "has_static_test_count": False,
                "analysis_status_code": code,
                "analysis_status_facts": {"manifest_readable": False},
            }

    result = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.FAILED),
        validator=Analyzer(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert result.accepted is False
    assert result.validated_outcome is PhaseOutcome.UNKNOWN
    assert result.validator_state is ValidatorState.UNAVAILABLE
    assert result.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert result.blocker_owner is BlockerOwner.HARNESS
    assert result.suggestions == ()


def test_absent_static_denominator_is_honest_partial_without_analyze_prescription():
    class Analyzer(FakeValidator):
        def validate_project_analysis_status(self, project_name=None):
            return {
                "analyzed": True,
                "has_static_test_count": False,
                "analysis_status_code": "analysis_static_count_missing",
            }

    result = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=Analyzer(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert result.accepted is True
    assert result.validator_state is ValidatorState.PARTIAL
    assert result.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
    assert result.suggestions == ()
    assert "project(action='analyze')" not in result.reason


def test_all_collection_errors_are_red_even_when_report_exists():
    class CollectionFailure(FakeValidator):
        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "evidence_status": "success",
                "total_tests": 328,
                "error_tests": 328,
                "test_stats": {"executed": 328, "discovered": 328},
                "receipt_scoped": True,
                "reason": "collection errors",
                "report_files": ["report://junit"],
            }

    result = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=CollectionFailure(),
        orchestrator=_orch(),
        project_name="demo",
    )

    # Premise updated 2026-08-10: terminal execution closes even when red;
    # collection-error counts remain sealed content, not a gate rejection.
    assert result.accepted is True
    assert result.validator_state is ValidatorState.GREEN
    assert result.validated_outcome is PhaseOutcome.SUCCESS
    assert result.code == "test_execution_observed"


def test_red_tests_never_reject_a_test_phase_close():
    """Execution is what SAG grades. A driven terminal suite with heavy
    failures closes claimably; red belongs to the project, not the harness."""

    class HeavyRedSuite(FakeValidator):
        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "status": "FAILED",
                "evidence_status": "failed",
                "reason": "20 passed, 60 failed, 20 errors",
                "report_files": ["report://terminal-runner"],
                "test_stats": {
                    "discovered": 100,
                    "executed": 100,
                    "passed": 20,
                    "failed": 60,
                    "errors": 20,
                    "skipped": 0,
                },
                "unique_tests": 100,
                "unique_passed_tests": 20,
                "unique_failed_tests": 60,
                "unique_error_tests": 20,
                "unique_skipped_tests": 0,
                "receipt_scoped": True,
            }

    gate = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=HeavyRedSuite(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert gate.accepted is True
    assert gate.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
    assert "test_failures" not in gate.code


def test_counts_that_did_not_come_from_the_claim_partition_cannot_close_the_phase():
    """`receipt_scoped` absent has exactly ONE meaning after universal scoping.

    The compact in-container parser now states `receipt_scoped: True` on every
    rollup it produces — a receipt-free run partitions its corpus like any
    other and seals a zero headline plus the auxiliary volume. So a rollup
    that arrives WITHOUT the key came from the shell find/cat fallback (the
    compact parser could not run), whose counts carry no provenance partition
    at all. Those counts are still published as facts, and they still may not
    close the test phase.
    """

    class UnpartitionedFallback(FakeValidator):
        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "evidence_status": "success",
                "reason": "10 passed",
                "report_files": ["report://shell-fallback"],
                "test_stats": {"discovered": 10, "executed": 10, "passed": 10},
                "unique_tests": 10,
                "unique_passed_tests": 10,
            }

    gate = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=UnpartitionedFallback(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert gate.accepted is False
    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.code == "test_receipt_missing"
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.validated_facts["test.stats"]["unique"]["executed"] == 10
    assert "receipt_scoped" not in gate.validated_facts["test.stats"]


def test_detected_but_unexecuted_tests_are_red():
    class NoExecution(FakeValidator):
        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "evidence_status": "success",
                "test_stats": {"executed": 0, "discovered": 12},
                "reason": "empty runner",
            }

    result = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS),
        validator=NoExecution(),
        orchestrator=_orch(),
        project_name="demo",
    )

    assert result.accepted is False
    assert result.validator_state is ValidatorState.RED
    assert result.code == "tests_not_executed"
