"""The finalizer's build evidence comes from the PHYSICAL validator.

Live evidence (ws7-final7 campaign, 2026-07-18):

- Bigtop r1-r3: the last build-role observation was a failed maven attempt, so
  ``_fold_build_evidence`` (last-observation-wins) sealed ``green:false,
  outcome:failed`` and the run rendered FAILED — while 121 compiled classes
  existed on disk, 50/50 tests passed, and the GATE (which reads the physical
  validator) had simultaneously validated the same phase as SUCCESS. One
  snapshot carried both conclusions; the July-13 kernel called this state
  PARTIAL.
- TVM r2: no build-role observation landed, so the verdict was ``unknown`` —
  while r1/r3 (same code, same repo) said ``failed``. The verdict hinged on
  whether one observation got tagged, not on physical state.

Contract restored here: the physical validator is the single build oracle for
gates AND finalizer; tool observations are corroborating refs and a fallback
aggregate (replay has no container), never the primary; the PARTIAL middle and
the module-coverage conflict survive into the sealed snapshot.
"""

from sag.agent.evidence_state import EvidenceRole, StateScope
from sag.agent.evidence_state import RunEvidenceState as _RunEvidenceState
from sag.agent.phase_machine import PhaseAttemptRecord
from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
from sag.evidence import EvidenceStatus, OperationOutcome, TestStats
from sag.tools.base import ToolResult


class RunEvidenceState(_RunEvidenceState):
    def ingest_tool_result(self, scope, tool_name, result, provenance=None, *, roles=()):
        explicit_roles = list(roles)
        if not explicit_roles:
            if scope is StateScope.ARTIFACTS:
                explicit_roles.append(EvidenceRole.BUILD)
            if result.test_stats is not None:
                explicit_roles.append(EvidenceRole.TEST)
        return super().ingest_tool_result(
            scope, tool_name, result, provenance, roles=explicit_roles
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


class FakePhysicalValidator:
    """Answers validate_build_status like the real tri-state oracle."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    def validate_build_status(self, project_name):
        self.calls.append(project_name)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def _finalize(state, validator, *, orchestrator=None):
    finalizer = VerdictFinalizer(
        orchestrator or FakeVerdictOrchestrator(),
        validator=validator,
        project_name="proj",
    )
    return finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)


def _green_tests(state, *, total=50):
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed_success(
            output="tests green",
            test_stats=TestStats(
                discovered=total, executed=total, passed=total, failed=0, skipped=0
            ),
            refs=["output_tests"],
        ),
        provenance="output_tests",
    )


BIGTOP_PHYSICAL = {
    "success": True,
    "build_complete": False,
    "reason": "not all active modules compiled (islands: test-framework failed)",
    "conflicts": ["build_modules_incomplete"],
    "evidence_status": "partial",
    "evidence": {"class_count": 121},
    "evidence_refs": ["output_gradle_ok"],
}


def _bigtop_state() -> RunEvidenceState:
    """Successful island build followed by a FAILED island attempt (the trap)."""
    state = RunEvidenceState(run_id="session-bigtop")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="data-generators built", refs=["output_gradle_ok"]),
        provenance="output_gradle_ok",
    )
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed(
            output="test-framework Groovy compile error",
            operation_outcome=OperationOutcome.FAILED,
            evidence_status=EvidenceStatus.VERIFIED,
            refs=["output_maven_fail"],
        ),
        provenance="output_maven_fail",
    )
    _green_tests(state)
    return state


def test_physical_oracle_outranks_last_failed_observation_bigtop_shape():
    validator = FakePhysicalValidator(BIGTOP_PHYSICAL)
    snapshot = _finalize(_bigtop_state(), validator)

    assert validator.calls == ["proj"]
    assert snapshot.build_evidence.judgment == "partial"
    assert snapshot.build_evidence.source == "physical"
    assert snapshot.build_evidence.compiled_classes == 121
    assert "build_modules_incomplete" in snapshot.conflicts
    # partial build + green tests = the July-13 honest PARTIAL, not failed
    assert snapshot.verdict == "partial"


def test_physical_failure_grounds_failed_even_without_build_observations_tvm_shape():
    state = RunEvidenceState(run_id="session-tvm-r2")
    # no build-role observation at all (the r2 shape); collection errors only
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="collection errors",
            operation_outcome=OperationOutcome.FAILED,
            evidence_status=EvidenceStatus.VERIFIED,
            test_stats=TestStats(discovered=328, executed=328, passed=0, failed=0, errors=328),
            refs=["output_pytest"],
        ),
        provenance="output_pytest",
    )
    validator = FakePhysicalValidator(
        {
            "success": False,
            "build_complete": False,
            "reason": "Top-level package import failed: tvm",
            "conflicts": [],
            "evidence_status": "blocked",
            "evidence": {},
        }
    )
    snapshot = _finalize(state, validator)

    assert snapshot.build_evidence.judgment == "failed"
    assert snapshot.build_evidence.source == "physical"
    # never "unknown" when the physical oracle observed a determinate failure
    assert snapshot.verdict == "failed"


def test_full_physical_success_with_green_tests_is_success():
    state = RunEvidenceState(run_id="session-ok")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="built", refs=["output_build"]),
        provenance="output_build",
    )
    _green_tests(state)
    validator = FakePhysicalValidator(
        {
            "success": True,
            "build_complete": True,
            "reason": "all modules compiled",
            "conflicts": [],
            "evidence_status": "success",
            "evidence": {"class_count": 8916},
        }
    )
    snapshot = _finalize(state, validator)
    assert snapshot.build_evidence.judgment == "success"
    assert snapshot.verdict == "success"


def test_fallback_aggregates_observations_instead_of_last_wins():
    # replay shape: no validator available; mixed success + failed build calls
    snapshot = _finalize(_bigtop_state(), validator=None)
    assert snapshot.build_evidence.source == "observations"
    assert snapshot.build_evidence.judgment == "partial"  # aggregate, not last-only
    assert snapshot.verdict == "partial"


def test_fallback_all_failed_observations_is_failed():
    state = RunEvidenceState(run_id="session-allfail")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed(
            output="boom",
            operation_outcome=OperationOutcome.FAILED,
            evidence_status=EvidenceStatus.VERIFIED,
            refs=["output_fail"],
        ),
        provenance="output_fail",
    )
    snapshot = _finalize(state, validator=None)
    assert snapshot.build_evidence.judgment == "failed"
    assert snapshot.verdict == "failed"


def test_true_unknown_requires_nothing_observed_anywhere():
    state = RunEvidenceState(run_id="session-empty")
    snapshot = _finalize(state, validator=None)
    assert snapshot.build_evidence.judgment == "unknown"
    assert snapshot.verdict == "unknown"


def test_validator_exception_degrades_to_observation_fallback_never_raises():
    validator = FakePhysicalValidator(RuntimeError("container gone"))
    snapshot = _finalize(_bigtop_state(), validator)
    assert snapshot.build_evidence.source == "observations"
    assert snapshot.build_evidence.judgment == "partial"


def test_oracle_divergence_with_gate_record_is_a_visible_conflict():
    """Gate validated build SUCCESS mid-run; physical oracle says FAILED at
    evidence-close (rank gap 2). The divergence must be visible in the sealed
    snapshot, never silent."""
    state = RunEvidenceState(run_id="session-diverge")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="built", refs=["output_build"]),
        provenance="output_build",
    )
    state.record_phase_record(
        PhaseAttemptRecord(
            phase="build",
            attempt_id="build-1",
            termination="completed",
            outcome="success",
            validated_outcome="success",
            key_results="built fine",
            evidence=("r",),
        )
    )

    validator = FakePhysicalValidator(
        {
            "success": False,
            "build_complete": False,
            "reason": "no artifacts",
            "conflicts": [],
            "evidence_status": "blocked",
            "evidence": {},
        }
    )
    snapshot = _finalize(state, validator)
    assert snapshot.build_evidence.judgment == "failed"
    assert "build_oracle_divergence" in snapshot.conflicts
    assert snapshot.verdict == "failed"
