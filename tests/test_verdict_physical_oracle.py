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

from test_verdict_finalizer import (
    FakeVerdictOrchestrator,
    bind_verdict_authority,
)

from sag.agent.evidence_state import EvidenceRole
from sag.agent.evidence_state import RunEvidenceState as _RunEvidenceState
from sag.agent.evidence_state import StateScope
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


def _finalize(
    state,
    validator,
    *,
    orchestrator=None,
    reason=EvidenceCloseReason.TEST_TERMINATED,
):
    verdict_orchestrator = orchestrator or FakeVerdictOrchestrator()
    bind_verdict_authority(verdict_orchestrator, state.run_id)
    finalizer = VerdictFinalizer(
        verdict_orchestrator,
        validator=validator,
        project_name="proj",
    )
    return finalizer.finalize(state, reason)


def _green_tests(state, *, total=50):
    """Green tests with the provenance a live dispatch states.

    Rebased 2026-08-14 (spec amendment item 9): these fixtures are about the
    PHYSICAL BUILD oracle, and a headline test count with no claim partition
    behind it is exactly what the finalizer now declines to publish. The
    receipt-scoped rollup is what a real test close seals.
    """
    counts = {"executed": total, "passed": total, "failed": 0, "errors": 0, "skipped": 0}
    state.register_fact(
        StateScope.TEST_RUNTIME,
        "test.stats",
        {
            "discovered": total,
            "unique": dict(counts),
            "raw": dict(counts),
            "receipt_scoped": True,
        },
        "output_tests",
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
    # Premise updated 2026-08-10: the physical failure stays in build_evidence;
    # this fake has no module denominator, so the compatibility word is partial.
    assert snapshot.verdict == "partial"


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
    assert snapshot.verdict == "partial"


def test_terminal_reactor_authority_seals_module_rate_without_class_ratio_conflict():
    state = RunEvidenceState(run_id="session-ignite-reactor")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="reactor built", refs=["output_build"]),
        provenance="output_build",
    )
    _green_tests(state, total=20)
    validator = FakePhysicalValidator(
        {
            "success": True,
            "build_complete": True,
            "reason": "41 of 41 reactor modules succeeded",
            "conflicts": [],
            "evidence_status": "success",
            "evidence": {
                "authority": "terminal_reactor_receipt",
                "terminal_reactor_receipt_id": "inv-ignite-build",
                "reactor_modules_succeeded": 41,
                "reactor_modules_total": 41,
                # These remain visible diagnostics, but one source file can
                # emit many classes and therefore cannot bound this count.
                "class_count": 17779,
                "source_files": 5470,
            },
        }
    )
    # Deliberately stale/incomplete filesystem view: receipt authority must
    # prevent this close-time diagnostic from re-capping the sealed build.
    validator.module_scan = lambda _project_name: {
        "summary": {"modules_built": 25, "modules_total": 37},
        "modules": [],
        "project_dir": "/workspace/proj",
    }

    snapshot = _finalize(state, validator)

    assert snapshot.build_evidence.judgment == "success"
    assert snapshot.rates["build"]["modules"] == {
        "rate": 100.0,
        "band": "fully",
        "numerator": 41,
        "denominator": 41,
        "reason": "terminal root reactor receipt is authoritative",
    }
    assert snapshot.rates["build"]["classes"] == {
        "band": "unavailable",
        "reason": (
            "terminal reactor receipt is authoritative; filesystem class/source "
            "counts are diagnostic and not comparable"
        ),
    }
    assert "build_modules_incomplete" not in snapshot.conflicts
    assert "rate_denominator_not_a_bound" not in snapshot.conflicts
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
    assert snapshot.verdict == "partial"


def test_true_unknown_requires_nothing_observed_anywhere():
    state = RunEvidenceState(run_id="session-empty")
    snapshot = _finalize(state, validator=None)
    assert snapshot.build_evidence.judgment == "unknown"
    assert snapshot.verdict == "partial"


def test_analyze_abort_does_not_turn_a_static_scan_into_a_failed_build():
    state = RunEvidenceState(run_id="session-analyze-abort")
    state.record_phase_record(
        PhaseAttemptRecord(
            phase="analyze",
            attempt_id="analyze-1",
            termination="aborted",
            outcome="failed",
            validated_outcome="failed",
            reason="iteration budget exhausted",
        )
    )
    validator = FakePhysicalValidator(
        {
            "success": False,
            "build_complete": False,
            "reason": "no compiled classes found",
            "conflicts": ["build_validation_failed"],
            "evidence_status": "blocked",
            "evidence": {"class_count": 0, "source_files": 5470},
        }
    )

    snapshot = _finalize(state, validator, reason=EvidenceCloseReason.ABORTED)

    assert validator.calls == []
    assert snapshot.verdict == "failed"
    assert snapshot.build_evidence.observed is False
    assert snapshot.build_evidence.judgment == "unknown"
    assert snapshot.build_evidence.source == "none"
    assert snapshot.rates["build"]["modules"] == {
        "band": "unavailable",
        "reason": "build was not run",
    }
    assert snapshot.rates["test"]["cases"] == {
        "band": "unavailable",
        "reason": "tests were not run",
    }
    assert "build_validation_failed" not in snapshot.conflicts


def test_policy_skipped_build_and_test_are_not_treated_as_executed_phases():
    state = RunEvidenceState(run_id="session-policy-skips")
    for phase in ("build", "test"):
        state.record_phase_record(
            PhaseAttemptRecord(
                phase=phase,
                attempt_id=f"{phase}-1",
                termination="skipped",
                outcome="skipped",
                validated_outcome="skipped",
                reason="phase skipped by transition policy",
            )
        )
    validator = FakePhysicalValidator(
        {
            "success": False,
            "build_complete": False,
            "reason": "no compiled classes found",
            "conflicts": ["build_validation_failed"],
            "evidence_status": "blocked",
            "evidence": {"class_count": 0, "source_files": 10},
        }
    )

    snapshot = _finalize(state, validator)

    assert validator.calls == []
    assert snapshot.build_evidence.observed is False
    assert snapshot.rates["build"]["modules"]["reason"] == "build was not run"
    assert snapshot.rates["test"]["cases"]["reason"] == "tests were not run"


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
    assert snapshot.verdict == "partial"


class ModuleAwareValidator(FakePhysicalValidator):
    """Physical oracle + the module-coverage machinery the finalizer folds.

    Bigtop live regression #2 (2026-07-18, post-oracle-fix): validate_build_status
    for a pathological aggregator root is trivially 'complete', so the run
    sealed SUCCESS while half the islands never built and only a subset of
    test-bearing modules ran tests. The July kernel capped this at PARTIAL via
    build_modules_incomplete / reactor_scope_narrowed — those conflicts must
    survive into the sealed snapshot.
    """

    def __init__(self, status, *, systems, modules_by_system, tests_by_path):
        super().__init__(status)
        self.project_path = "/workspace"
        self._systems = systems
        self._modules_by_system = modules_by_system
        self._tests_by_path = tests_by_path

    def _detect_build_system(self, project_dir):
        return self._systems[0]

    def scan_modules(self, project_dir, build_system):
        return [dict(m) for m in self._modules_by_system.get(build_system, [])]

    def parse_module_test_reports(self, module_dir, report_dirs):
        return dict(self._tests_by_path.get(module_dir.rsplit("/", 1)[-1], {}))


def _bigtop_module_validator():
    return ModuleAwareValidator(
        {
            "success": True,
            "build_complete": True,  # trivially true for the aggregator root
            "reason": "Build fingerprints found for maven project",
            "conflicts": [],
            "evidence_status": "success",
            "evidence": {"class_count": 115},
        },
        systems=["maven", "gradle"],
        modules_by_system={
            "maven": [
                {
                    "path": ".",
                    "name": ".",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": False,
                },
                {
                    "path": "bigtop-test-framework",
                    "name": "bigtop-test-framework",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": True,
                },
            ],
            "gradle": [
                {
                    "path": "bigtop-data-generators/bigtop-samplers",
                    "name": "bigtop-samplers",
                    "class_count": 39,
                    "jar_count": 1,
                    "report_dirs": ["/workspace/bigtop/x/build/test-results/test"],
                    "has_test_sources": True,
                },
                {
                    "path": "bigtop-bigpetstore/bigpetstore-transaction-queue",
                    "name": "tq",
                    "class_count": 0,
                    "jar_count": 0,
                    "report_dirs": [],
                    "has_test_sources": True,
                },
            ],
        },
        tests_by_path={
            "bigtop-samplers": {"tests_total": 50, "tests_passed": 50, "failing_count": 0}
        },
    )


def test_module_coverage_conflicts_cap_pathological_aggregator_at_partial():
    validator = _bigtop_module_validator()
    state = RunEvidenceState(run_id="session-bigtop-islands")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="islands built", refs=["output_build"]),
        provenance="output_build",
    )
    _green_tests(state)
    snapshot = _finalize(state, validator)

    assert snapshot.build_evidence.judgment == "success"  # physical top-level
    assert "build_modules_incomplete" in snapshot.conflicts
    assert "reactor_scope_narrowed" in snapshot.conflicts
    # coverage shortfall caps the run: never SUCCESS with unbuilt islands
    assert snapshot.verdict == "partial"


def test_python_projects_keep_module_conflict_suppression():
    validator = ModuleAwareValidator(
        {
            "success": True,
            "build_complete": True,
            "reason": "Python build verified",
            "conflicts": [],
            "evidence_status": "success",
            "evidence": {},
        },
        systems=["python"],
        modules_by_system={},
        tests_by_path={},
    )
    state = RunEvidenceState(run_id="session-python")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="venv ok", refs=["output_build"]),
        provenance="output_build",
    )
    _green_tests(state)
    snapshot = _finalize(state, validator)
    assert snapshot.conflicts == ()
    assert snapshot.verdict == "partial"


def test_pr9_phantom_green_gate_reaches_the_sealed_verdict():
    """PR #9's bigtop fix, end-to-end through the finalizer.

    The phantom-green gate (a JVM build with 0 compiled .class files and only
    vendored JARs is NOT green) lives inside validate_build_status. During the
    ws7 implementation window the finalizer never consulted it (last-
    observation-wins), so an agent observation claiming success could have
    resealed the exact false-green PR #9 killed. This locks the chain: the
    physical gate's verdict reaches the sealed snapshot even when the build
    observation says success.
    """
    state = RunEvidenceState(run_id="session-phantom")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="BUILD SUCCESS (vendored jars satisfied the tool)",
            refs=["output_vendored"],
        ),
        provenance="output_vendored",
    )
    validator = FakePhysicalValidator(
        {
            "success": False,
            "build_complete": False,
            "reason": (
                "No compiled .class files found for maven build (3 non-class "
                "artifact(s) such as vendored JARs are not evidence the project compiled)"
            ),
            "conflicts": [],
            "evidence_status": "blocked",
            "evidence": {"class_count": 0, "artifact_count": 3},
        }
    )
    snapshot = _finalize(state, validator)

    assert snapshot.build_evidence.judgment == "failed"
    assert snapshot.build_evidence.source == "physical"
    assert snapshot.build_evidence.compiled_classes == 0
    assert snapshot.verdict == "partial"
