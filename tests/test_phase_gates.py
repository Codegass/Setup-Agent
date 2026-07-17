"""Phase-boundary evidence gates (spec §3.1). Descriptive: a failed gate
returns evidence + options, never blocks tool use; probe errors fail OPEN."""

from types import SimpleNamespace

from sag.agent.phase_gates import (
    ClaimDisposition,
    ValidatorState,
    check_phase_claim,
    check_phase_done,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome


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
        if "java -version" in command:
            return {"exit_code": 0 if java_ok else 127, "output": "openjdk 17" if java_ok else ""}
        if "test -d" in command:
            return {"exit_code": 0, "output": "exists" if workspace_exists else "missing"}
        if "setup-report-" in command:
            return {"exit_code": 0, "output": "/workspace/setup-report-x.md"}
        return {"exit_code": 0, "output": ""}
    return SimpleNamespace(execute_command=execute_command)


def test_build_done_rejected_without_artifacts():
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=False),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert "artifact" in verdict["reason"].lower() or "build" in verdict["reason"].lower()
    assert verdict["suggestions"], "must offer options"


def test_build_done_accepted_with_artifacts():
    validator = FakeValidator(build_success=True)
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=True),
        orchestrator=_orch(), project_name="demo",
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


def test_build_gate_uses_physical_validator_for_non_jvm_systems():
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=False, build_system="nodejs"),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert verdict["validator_state"] == "red"


def test_test_done_rejected_without_reports():
    verdict = check_phase_done(
        "test", validator=FakeValidator(has_test_reports=False),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert "report" in verdict["reason"].lower()


def test_provision_rejected_without_workspace():
    verdict = check_phase_done(
        "provision", validator=FakeValidator(),
        orchestrator=_orch(workspace_exists=False), project_name="demo",
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
        "build", validator=Exploding(), orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert verdict["validator_state"] == "unavailable"


def test_analyze_unknown_claim_can_end_when_evidence_is_unavailable():
    result = check_phase_claim(
        "analyze",
        PhaseClaim(phase="analyze", claimed_outcome=PhaseOutcome.UNKNOWN),
        validator=FakeValidator(),
        orchestrator=_orch(),
        project_name="demo",
    )
    assert result.accepted is True
    assert result.validator_state is ValidatorState.UNAVAILABLE
    assert result.claim_disposition is ClaimDisposition.CONFIRMED
    assert result.validated_outcome is PhaseOutcome.UNKNOWN


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


def test_all_collection_errors_are_red_even_when_report_exists():
    class CollectionFailure(FakeValidator):
        def validate_test_status(self, project_name=None):
            return {
                "has_test_reports": True,
                "evidence_status": "success",
                "total_tests": 328,
                "error_tests": 328,
                "test_stats": {"executed": 328, "discovered": 328},
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

    assert result.accepted is False
    assert result.validator_state is ValidatorState.RED
    assert result.validated_outcome is PhaseOutcome.FAILED
    assert result.code == "test_collection_failed"


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
