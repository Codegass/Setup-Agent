# tests/test_python_phase_verdict.py
"""Python-aware phase guidance + the scoped blocked-build verdict cap.

Live-run 2026-06-24 (pyyaml probe) false-red, root cause (1): the Java-centric
build objective told the agent "if the analyzer reports NO Java compile target,
phase(action='blocked')" — on a Python project the agent obeyed, and the
unscoped blocked-build cap turned an honest physical PARTIAL (1287/1287 pytest
passed, C-extension .so missing) into a FAILED final verdict.

Two coordinated fixes under test here:

A. phase_objective(phase, build_system): Python projects get build/test
   objectives that prescribe deps -> compile and pytest via build tool, and
   explicitly forbid blocking on the absence of a Java compile target. The
   Java strings stay byte-identical (snapshot test below).

B. Scoped cap in SetupAgent._get_verified_final_status: an agent-blocked build
   phase caps the verdict to FAILED only when physical build evidence AGREES
   (success=False). With real build evidence (success=True — the Python
   ladder's success/partial-with-imports, or Java artifacts) the cap is
   PARTIAL and the reason records both sides, keeping dishonesty catchable
   without evidence-contradicted false-reds.
"""

from types import SimpleNamespace

from sag.agent.agent import SetupAgent
from sag.agent.phase_machine import PHASE_NAMES, PhaseMachine
from sag.agent.react_engine import PHASE_OBJECTIVES, ReActEngine, phase_objective
from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD

# ---------------------------------------------------------------------------
# fakes (pattern mirrors tests/test_agent_final_status.py)
# ---------------------------------------------------------------------------


class FakePhysicalValidator:
    def __init__(
        self,
        build_status,
        test_status,
        analysis_status=None,
        test_pass_threshold=DEFAULT_TEST_PASS_THRESHOLD,
    ):
        self.build_status = build_status
        self.test_status = test_status
        self.analysis_status = analysis_status or {"analyzed": False}
        self.test_pass_threshold = test_pass_threshold

    def validate_build_status(self, project_name):
        return self.build_status

    def validate_test_status(self, project_name):
        return self.test_status

    def validate_project_analysis_status(self, project_name):
        return self.analysis_status


def _agent_with_validator(validator):
    agent = object.__new__(SetupAgent)
    agent.workflow_mode = "continue"
    agent.orchestrator = SimpleNamespace(project_name="pyyaml")
    agent.project_name = "pyyaml"
    agent.context_manager = SimpleNamespace(project_name="pyyaml")
    agent.physical_validator = validator
    return agent


def _attach_phase_machine(agent, block_phase=None):
    machine = PhaseMachine()
    for name in PHASE_NAMES:
        if name == block_phase:
            machine.mark_blocked(f"{name} blocked", [])
        else:
            machine.mark_done("ok", [])
    agent.react_engine = SimpleNamespace(phase_machine=machine)
    return machine


def _pytest_all_green_test_status(total=1287):
    return {
        "has_test_reports": True,
        "status": "SUCCESS",
        "reason": "All tests passed",
        "pass_rate": 100.0,
        "total_tests": total,
        "passed_tests": total,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "test_exclusions": [],
        "modules_without_tests": [],
        "conflicts": [],
    }


def _python_partial_build_status():
    """The Python evidence ladder's honest PARTIAL: real build (venv, imports
    ok) but declared C-extensions have no built .so artifact."""
    return {
        "success": True,
        "build_complete": False,
        "reason": "declared C-extensions have no built .so artifact",
        "evidence": {"build_system": "python"},
        "evidence_status": "partial",
        "conflicts": ["build_modules_incomplete"],
    }


# ---------------------------------------------------------------------------
# B. scoped blocked-build cap
# ---------------------------------------------------------------------------


def test_python_partial_physical_evidence_drives_partial_verdict():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status=_python_partial_build_status(),
            test_status=_pytest_all_green_test_status(),
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 1287,
            },
        )
    )
    _attach_phase_machine(agent, block_phase="build")

    result = agent._legacy_get_verified_final_status(react_engine_success=True)

    assert agent.final_verdict == "partial"
    assert result is True, "flow-control follows the physical evidence"


def test_python_phase_termination_does_not_leak_into_verdict_reason():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status=_python_partial_build_status(),
            test_status=_pytest_all_green_test_status(),
        )
    )
    _attach_phase_machine(agent, block_phase="build")

    agent._legacy_get_verified_final_status(react_engine_success=True)

    assert agent.final_verdict == "partial"
    reason = agent.final_verdict_reason
    assert "blocked" not in reason, reason


def test_blocked_build_with_no_physical_evidence_stays_failed():
    """When physical evidence AGREES with the block (no build evidence),
    the cap stays FAILED exactly as today — dishonesty stays catchable."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={
                "success": False,
                "build_complete": False,
                "reason": "No build evidence found (no artifacts or build fingerprints)",
            },
            test_status={
                "has_test_reports": False,
                "status": "WARNING",
                "reason": "No test reports found",
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
        )
    )
    _attach_phase_machine(agent, block_phase="build")

    result = agent._legacy_get_verified_final_status(react_engine_success=True)

    assert result is False
    assert agent.final_verdict == "failed"


def test_java_green_evidence_is_not_capped_by_phase_termination():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={
                "success": True,
                "build_complete": True,
                "reason": "Found 120 compiled classes (build appears successful)",
            },
            test_status={
                "has_test_reports": True,
                "status": "SUCCESS",
                "reason": "All tests passed",
                "pass_rate": 100.0,
                "total_tests": 100,
                "passed_tests": 100,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
        )
    )
    _attach_phase_machine(agent, block_phase="build")

    result = agent._legacy_get_verified_final_status(react_engine_success=True)

    assert agent.final_verdict == "success"
    assert result is True
    assert agent.final_verdict_reason == ""


def test_blocked_build_with_evidence_never_promotes_past_physical_failure():
    """Physical validation still rules: real build evidence but a failing
    test gate keeps the run FAILED even with the scoped cap."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status=_python_partial_build_status(),
            test_status={
                "has_test_reports": True,
                "status": "FAILED",
                "reason": "most tests failed",
                "pass_rate": 10.0,
                "total_tests": 100,
                "passed_tests": 10,
                "failed_tests": 90,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
        )
    )
    _attach_phase_machine(agent, block_phase="build")

    result = agent._legacy_get_verified_final_status(react_engine_success=True)

    assert result is False
    assert agent.final_verdict == "failed"


# ---------------------------------------------------------------------------
# A. project-aware phase objectives
# ---------------------------------------------------------------------------

# Byte-identical snapshot of the JAVA build objective (the template source).
# If this fails, the Java guidance changed — that is out of scope for the
# Python fix and must be an intentional, separate change.
_JAVA_BUILD_OBJECTIVE_SNAPSHOT = (
    "Make the project compile: build(action='compile'). Follow the analyzer's "
    "Recommended Build when it differs from a plain root compile — an aggregator "
    "root over Groovy modules needs build(action='package'/'install'), and a "
    "Gradle-primary project needs the Gradle build. If the analyzer reports NO Java "
    "compile target (a packaging/meta-project), phase(action='blocked') with that "
    "evidence instead of forcing a compile. If compilation fails on missing "
    "dependencies, build(action='deps') can resolve them — but do not run deps "
    "first by default (multi-module reactors can fail dependency resolution while "
    "compiling fine). Never run mvn/gradle via bash — build resolves the "
    "registered toolchain. Long builds detach; poll the job ref with search."
)


def test_python_build_objective_forbids_blocking_on_missing_java_target():
    obj = phase_objective("build", "pip/poetry")
    assert "build(action='deps')" in obj
    assert "build(action='compile')" in obj
    assert "no Java compile target" in obj
    assert "NOT grounds for phase(action='blocked')" in obj
    assert obj != PHASE_OBJECTIVES["build"]


def test_python_test_objective_prescribes_pytest_via_build_tool():
    obj = phase_objective("test", "python")
    assert "pytest" in obj
    assert "build(action='test')" in obj


def test_java_build_objective_is_byte_identical_snapshot():
    assert PHASE_OBJECTIVES["build"] == _JAVA_BUILD_OBJECTIVE_SNAPSHOT
    # Java (and unknown) projects keep the exact same guidance.
    for system in ("maven", "gradle", "unknown", None):
        assert phase_objective("build", system) == _JAVA_BUILD_OBJECTIVE_SNAPSHOT
        assert phase_objective("test", system) == PHASE_OBJECTIVES["test"]


# ---------------------------------------------------------------------------
# A. intro-step wiring: the objective the model actually sees at phase start
# ---------------------------------------------------------------------------


def _engine_at_build_phase(environment_summary):
    engine = ReActEngine.__new__(ReActEngine)
    machine = PhaseMachine()
    machine.mark_done("repo cloned; toolchain installed", [])
    machine.mark_done("analyzed", [])
    engine.phase_machine = machine
    engine.config = SimpleNamespace(phase_min_floors={}, max_iterations=150)
    engine.current_iteration = 10

    class FakeCM:
        def load_trunk_context(self):
            return SimpleNamespace(environment_summary=environment_summary)

    engine.context_manager = FakeCM()
    return engine


def test_build_intro_uses_python_objective_for_python_project():
    engine = _engine_at_build_phase(
        {
            "build_system": "pip/poetry",
            "build_recommendation": {
                "build_system": "pip/poetry",
                "goal": "compile",
                "build_root": "/workspace/pyyaml",
                "is_aggregator_only": False,
                "rationale": "",
            },
        }
    )
    intro = engine._phase_intro_step().content
    assert "NOT grounds for phase(action='blocked')" in intro
    assert "Make the project compile" not in intro


def test_build_intro_keeps_java_objective_for_maven_project():
    engine = _engine_at_build_phase(
        {
            "build_system": "Maven",
            "build_recommendation": {
                "build_system": "maven",
                "goal": "install",
                "build_root": "/workspace/demo",
                "is_aggregator_only": False,
                "rationale": "Root Maven module has main sources.",
            },
        }
    )
    intro = engine._phase_intro_step().content
    assert _JAVA_BUILD_OBJECTIVE_SNAPSHOT in intro


def test_build_intro_defaults_to_java_objective_without_trunk():
    """Best-effort plumbing: a missing/broken trunk must not break the intro."""

    class BoomCM:
        def load_trunk_context(self):
            raise RuntimeError("no container")

    engine = _engine_at_build_phase({})
    engine.context_manager = BoomCM()
    intro = engine._phase_intro_step().content
    assert _JAVA_BUILD_OBJECTIVE_SNAPSHOT in intro
