# tests/test_python_phase_guidance.py
"""Runtime python guidance in the BUILD/TEST phase intros (live-effective).

Live evidence (4 of 5 python runs, 2026-06/07 probes): the phase PLAN is
authored at kickoff, BEFORE the repo is cloned/analyzed, so its build/test
task descriptions are the Java-centric text ("provision the JDK";
"If the analyzer reports NO Java compile target, phase(action='blocked')").
A template-time fix cannot help — a plan authored at t=0 cannot know the
project type. Agents obeyed the static text, blocked the build phase, and
under-executed tests (0-2 executions where 1287 previously passed).

The live-effective seam is ReActEngine._phase_intro_step, which already reads
trunk environment_summary["build_recommendation"] AT RUNTIME (after the
analyzer has run) via _recommended_build_line. Under test here:

A. With a python build_recommendation on the trunk, the build-phase intro
   carries the explicit no-blocked python guidance and the test-phase intro
   carries the pytest guidance.
B. With a maven recommendation, the build/test intros are BYTE-IDENTICAL to
   the pre-change strings (full-intro snapshots below).
C. The analyzer stores build_system='python' on the recommendation payload
   for a Python project, so the runtime signal is canonical.
D. The kickoff plan template no longer unconditionally instructs blocking on
   a missing Java compile target (conditional sentence), while the runtime
   PHASE_OBJECTIVES stay byte-identical.
"""

import inspect
from types import SimpleNamespace

import sag.agent.agent as agent_module
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import (
    KICKOFF_PHASE_OBJECTIVES,
    PHASE_OBJECTIVES,
    PYTHON_BUILD_PHASE_GUIDANCE,
    PYTHON_TEST_PHASE_GUIDANCE,
    ReActEngine,
)
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


# ---------------------------------------------------------------------------
# fixtures (pattern mirrors tests/test_python_phase_verdict.py)
# ---------------------------------------------------------------------------


def _engine_at(phase_done_count, environment_summary):
    """Engine positioned at build (2 phases done) or test (3 done), with the
    given trunk environment_summary — the runtime plumbing under test."""
    engine = ReActEngine.__new__(ReActEngine)
    machine = PhaseMachine()
    results = [
        "repo cloned; JDK 17 installed",
        "maven project analyzed",
        "compiled 120 classes",
    ]
    for i in range(phase_done_count):
        machine.mark_done(results[i], [])
    engine.phase_machine = machine
    engine.config = SimpleNamespace(phase_min_floors={}, max_iterations=150)
    engine.current_iteration = 10

    class FakeCM:
        def load_trunk_context(self):
            return SimpleNamespace(environment_summary=environment_summary)

    engine.context_manager = FakeCM()
    return engine


def _python_env(build_system="python"):
    return {
        "build_system": build_system,
        "build_recommendation": {
            "build_system": build_system,
            "goal": "compile",
            "build_root": "/workspace/pyyaml",
            "is_aggregator_only": False,
            "rationale": "",
            "test_root": "/workspace/pyyaml",
            "test_system": build_system,
        },
    }


_MAVEN_ENV = {
    "build_system": "Maven",
    "build_recommendation": {
        "build_system": "maven",
        "goal": "install",
        "build_root": "/workspace/demo",
        "is_aggregator_only": False,
        "rationale": "Root Maven module has main sources.",
        "test_root": "/workspace/demo",
        "test_system": "maven",
    },
}


# ---------------------------------------------------------------------------
# A. python guidance reaches the intros at runtime
# ---------------------------------------------------------------------------


def test_build_intro_carries_no_blocked_python_guidance():
    intro = _engine_at(2, _python_env())._phase_intro_step().content
    assert PYTHON_BUILD_PHASE_GUIDANCE in intro
    assert "NOT grounds for phase(action='blocked')" in intro
    assert "build(action='deps') to create the venv" in intro
    assert "Never run pip/pytest via bash" in intro


def test_test_intro_carries_pytest_guidance():
    intro = _engine_at(3, _python_env())._phase_intro_step().content
    assert PYTHON_TEST_PHASE_GUIDANCE in intro
    assert "build(action='test')" in intro
    assert "pytest with a JUnit XML report" in intro
    assert "partial pass above threshold is a valid, honest outcome" in intro


def test_guidance_also_keys_off_legacy_pip_poetry_label():
    """Structure detection records 'pip/poetry'; older trunks carry it — the
    guidance must fire for every python label, not just the canonical one."""
    build_intro = _engine_at(2, _python_env("pip/poetry"))._phase_intro_step().content
    test_intro = _engine_at(3, _python_env("pip/poetry"))._phase_intro_step().content
    assert PYTHON_BUILD_PHASE_GUIDANCE in build_intro
    assert PYTHON_TEST_PHASE_GUIDANCE in test_intro


def test_guidance_absent_without_python_signal():
    """No recommendation at all -> Java-default intro, no python guidance."""
    intro = _engine_at(2, {})._phase_intro_step().content
    assert PYTHON_BUILD_PHASE_GUIDANCE not in intro
    assert PYTHON_TEST_PHASE_GUIDANCE not in intro


# ---------------------------------------------------------------------------
# B. maven intros stay byte-identical (full pre-change snapshots)
# ---------------------------------------------------------------------------

# Captured VERBATIM from _phase_intro_step on the pre-change code with the
# _MAVEN_ENV fixture above. If these fail, the Java intro changed — out of
# scope for the python fix and must be an intentional, separate change.
_MAVEN_BUILD_INTRO_SNAPSHOT = (
    "=== PHASE: BUILD ===\n"
    "Run picture so far:\n"
    "✓ provision: repo cloned; JDK 17 installed\n"
    "✓ analyze: maven project analyzed\n"
    "→ current: build\n"
    "\n"
    "Objective: Make the project compile: build(action='compile'). Follow the "
    "analyzer's Recommended Build when it differs from a plain root compile — "
    "an aggregator root over Groovy modules needs build(action='package'/'install'), "
    "and a Gradle-primary project needs the Gradle build. If the analyzer reports "
    "NO Java compile target (a packaging/meta-project), phase(action='blocked') "
    "with that evidence instead of forcing a compile. If compilation fails on "
    "missing dependencies, build(action='deps') can resolve them — but do not run "
    "deps first by default (multi-module reactors can fail dependency resolution "
    "while compiling fine). Never run mvn/gradle via bash — build resolves the "
    "registered toolchain. Long builds detach; poll the job ref with search.\n"
    "Recommended Build: maven 'install' in /workspace/demo — Root Maven module "
    "has main sources.\n"
    "Budget: flexible — up to ~132 iterations available (a small reserve is kept "
    "for later phases). When finished, call phase(action='done', key_results=..., "
    "evidence=[refs]). If it cannot be finished, phase(action='blocked', "
    "reason=..., evidence=[refs])."
)

_MAVEN_TEST_INTRO_SNAPSHOT = (
    "=== PHASE: TEST ===\n"
    "Run picture so far:\n"
    "✓ provision: repo cloned; JDK 17 installed\n"
    "✓ analyze: maven project analyzed\n"
    "✓ build: compiled 120 classes\n"
    "→ current: test\n"
    "\n"
    "Objective: Run the test suite: build(action='test'). Run it in the "
    "analyzer's Recommended Tests target (the tests can live in a different "
    "module — and even a different build system — than the build, e.g. Gradle "
    "test modules beside a Maven build); otherwise use the build root. Partial "
    "pass above threshold is a valid outcome — report the numbers honestly in "
    "key_results. If tests genuinely cannot run, phase(action='blocked') with "
    "evidence.\n"
    "Budget: flexible — up to ~136 iterations available (a small reserve is kept "
    "for later phases). When finished, call phase(action='done', key_results=..., "
    "evidence=[refs]). If it cannot be finished, phase(action='blocked', "
    "reason=..., evidence=[refs])."
)


def test_maven_build_intro_byte_identical_to_pre_change():
    intro = _engine_at(2, _MAVEN_ENV)._phase_intro_step().content
    assert intro == _MAVEN_BUILD_INTRO_SNAPSHOT


def test_maven_test_intro_byte_identical_to_pre_change():
    intro = _engine_at(3, _MAVEN_ENV)._phase_intro_step().content
    assert intro == _MAVEN_TEST_INTRO_SNAPSHOT


# ---------------------------------------------------------------------------
# C. analyzer emits the canonical python signal on the recommendation
# ---------------------------------------------------------------------------


def _bare_analyzer():
    return ProjectAnalyzerTool(docker_orchestrator=None, context_manager=None)


def test_analyzer_recommendation_stores_python_build_system():
    rec = _bare_analyzer()._recommend_build_approach(
        "/workspace/pyyaml",
        {"project_type": "Python", "build_system": "pip/poetry"},
    )
    assert rec["build_system"] == "python"
    assert rec["is_aggregator_only"] is False


def test_analyzer_recommendation_stores_python_even_without_label():
    """The signal must be canonical even when structure detection left the
    build_system unknown (the payload previously inherited whatever was there)."""
    rec = _bare_analyzer()._recommend_build_approach(
        "/workspace/pyyaml",
        {"project_type": "Python", "build_system": "unknown"},
    )
    assert rec["build_system"] == "python"


def test_analyzer_recommendation_untouched_for_java():
    rec = _bare_analyzer()._recommend_build_approach(
        "/workspace/demo",
        {"project_type": "Java", "build_system": "Maven"},
    )
    assert rec["build_system"] == "Maven"


# ---------------------------------------------------------------------------
# D. kickoff plan template: conditional, no longer instructs blocking on python
# ---------------------------------------------------------------------------


def test_kickoff_build_objective_softens_blocking_to_conditional():
    kickoff = KICKOFF_PHASE_OBJECTIVES["build"]
    assert kickoff != PHASE_OBJECTIVES["build"], (
        "the kickoff variant must actually differ (a no-op .replace would "
        "silently reintroduce the unconditional blocking instruction)"
    )
    assert (
        "AND the project is not a Python/other-ecosystem project" in kickoff
    )
    # The Java meta-project escape hatch itself is kept, just made conditional.
    assert "phase(action='blocked')" in kickoff


def test_kickoff_other_phases_identical_to_runtime_objectives():
    for name, text in PHASE_OBJECTIVES.items():
        if name == "build":
            continue
        assert KICKOFF_PHASE_OBJECTIVES[name] == text


def test_agent_authors_kickoff_plan_from_kickoff_objectives():
    """agent.py authors the t=0 trunk tasks from the kickoff variant, not the
    runtime PHASE_OBJECTIVES (source-level wiring guard)."""
    source = inspect.getsource(agent_module)
    assert "KICKOFF_PHASE_OBJECTIVES" in source
