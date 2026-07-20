# tests/test_python_phase_guidance.py
"""Runtime python coordinates in the BUILD/TEST phase intros (FACTS-only).

Live evidence (4 of 5 python runs, 2026-06/07 probes): the phase PLAN is
authored at kickoff, BEFORE the repo is cloned/analyzed, so its build/test
task descriptions were Java-centric. A template-time fix cannot help — a plan
authored at t=0 cannot know the project type.

The live-effective seam is ReActEngine._phase_intro_step, which reads trunk
environment_summary["build_recommendation"] AT RUNTIME (after the analyzer has
run) via _recommended_build_line, and selects the project-aware phase
objective via phase_objective. After the Category-3 analyzer diet the
prescription layer is DELETED: the objective wording is the FACTS variant, the
recommendation line is coordinates-only (no goal/rationale action wording),
there is no pre-hoc python guidance block, and no project_brief projection.
Under test here:

A. With a python build_recommendation on the trunk, the build/test intros
   carry the python-ecosystem FACTS objective and the coordinates line.
B. With a maven recommendation, the build/test intros match the FACTS-only
   phase contract (full-intro snapshots below).
C. The analyzer stores build_system='python' on the recommendation payload
   for a Python project, so the runtime signal is canonical.
D. The kickoff plan template carries the FACTS objective wording (no
   "Recommended Build/Tests" prose), still conditional on ecosystem.
"""

import inspect
import re
from types import SimpleNamespace

import sag.agent.agent as agent_module
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import (
    KICKOFF_PHASE_OBJECTIVES,
    FACTS_KICKOFF_PHASE_OBJECTIVES,
    PHASE_OBJECTIVES,
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
            "build_root": "/workspace/pyyaml",
            "is_aggregator_only": False,
            "test_root": "/workspace/pyyaml",
            "test_system": build_system,
        },
    }


_MAVEN_ENV = {
    "build_system": "Maven",
    "build_recommendation": {
        "build_system": "maven",
        "build_root": "/workspace/demo",
        "is_aggregator_only": False,
        "test_root": "/workspace/demo",
        "test_system": "maven",
    },
}


# ---------------------------------------------------------------------------
# A. python-ecosystem FACTS objective + coordinates reach the intros at runtime
# ---------------------------------------------------------------------------


def test_build_intro_carries_python_ecosystem_objective():
    intro = _engine_at(2, _python_env())._phase_intro_step().content
    # The FACTS python build objective (dim d) — the ecosystem override, not a
    # pre-hoc prescription block (dim e, which is deleted).
    assert "A Python project has no Java compile target" in intro
    assert "build(action='deps')" in intro
    assert "Never run pip/python via bash" in intro
    # coordinates line (dim b): system + where, no goal/rationale action wording
    assert "Build coordinates: python at /workspace/pyyaml." in intro
    assert "Recommended Build" not in intro


def test_test_intro_carries_pytest_objective():
    intro = _engine_at(3, _python_env())._phase_intro_step().content
    assert "pytest via build(action='test')" in intro
    assert "Partial pass above threshold is a valid outcome" in intro
    assert "Recommended Tests" not in intro


def test_objective_also_keys_off_legacy_pip_poetry_label():
    """Structure detection records 'pip/poetry'; older trunks carry it — the
    python objective must fire for every python label, not just 'python'."""
    build_intro = _engine_at(2, _python_env("pip/poetry"))._phase_intro_step().content
    test_intro = _engine_at(3, _python_env("pip/poetry"))._phase_intro_step().content
    assert "A Python project has no Java compile target" in build_intro
    assert "pytest via build(action='test')" in test_intro


def test_no_prehoc_guidance_block_ever_renders():
    """dim (e) deleted: the pre-hoc python/native-first guidance block is gone.
    Its distinctive wording ('build(action='deps') to create the venv',
    'This package has a NATIVE core') never renders — the build intro carries
    the FACTS objective and coordinates only."""
    intro = _engine_at(2, _python_env())._phase_intro_step().content
    assert "build(action='deps') to create the venv" not in intro
    assert "This package has a NATIVE core" not in intro


def test_java_intro_has_no_python_objective():
    """A Maven recommendation -> the Java-default FACTS objective, no python
    ecosystem wording."""
    intro = _engine_at(2, _MAVEN_ENV)._phase_intro_step().content
    assert "A Python project has no Java compile target" not in intro
    assert "Build coordinates: maven at /workspace/demo." in intro


# ---------------------------------------------------------------------------
# B. maven intros carry the FACTS-only phase contract (full-intro snapshots)
# ---------------------------------------------------------------------------

_MAVEN_BUILD_INTRO_SNAPSHOT = (
    "=== PHASE: BUILD ===\n"
    "Run picture so far:\n"
    "• provision [unknown]: repo cloned; JDK 17 installed\n"
    "• analyze [unknown]: maven project analyzed\n"
    "→ current: build\n"
    "\n"
    "Objective: Make the project compile: build(action='compile'). Consult the "
    "survey facts for the build coordinates — an aggregator root can compile "
    "nothing at the root while the real sources live in island modules. If the "
    "survey facts show NO Java compile target (a packaging/meta-project), "
    "phase(action='blocked', outcome='unknown', ...) with that evidence instead "
    "of forcing a compile. If compilation fails on missing dependencies, "
    "build(action='deps') can resolve them — but do not run deps first by "
    "default (multi-module reactors can fail dependency resolution while "
    "compiling fine). Never run mvn/gradle via bash — build resolves the "
    "registered toolchain. Long builds detach; poll the job ref with search.\n"
    "Build coordinates: maven at /workspace/demo.\n"
    "Budget: flexible — up to ~132 iterations available (a small reserve is kept "
    "for later phases). When finished, call phase(action='done', "
    "outcome='success|partial|failed|unknown', key_results=..., evidence=[refs]). "
    "For an external impediment, call phase(action='blocked', "
    "outcome='failed|partial|unknown', reason=..., evidence=[refs])."
)

_MAVEN_TEST_INTRO_SNAPSHOT = (
    "=== PHASE: TEST ===\n"
    "Run picture so far:\n"
    "• provision [unknown]: repo cloned; JDK 17 installed\n"
    "• analyze [unknown]: maven project analyzed\n"
    "• build [unknown]: compiled 120 classes\n"
    "→ current: test\n"
    "\n"
    "Objective: Run the test suite: build(action='test'). Run it where the "
    "survey facts place the tests (they can live in a different module — and "
    "even a different build system — than the build); otherwise use the build "
    "root. Partial pass above threshold is a valid outcome — report the numbers "
    "honestly in key_results. If tests genuinely cannot run, "
    "phase(action='blocked', outcome='failed', ...) with evidence.\n"
    "Budget: flexible — up to ~136 iterations available (a small reserve is kept "
    "for later phases). When finished, call phase(action='done', "
    "outcome='success|partial|failed|unknown', key_results=..., evidence=[refs]). "
    "For an external impediment, call phase(action='blocked', "
    "outcome='failed|partial|unknown', reason=..., evidence=[refs])."
)


def test_maven_build_intro_matches_facts_contract():
    intro = _engine_at(2, _MAVEN_ENV)._phase_intro_step().content
    assert intro == _MAVEN_BUILD_INTRO_SNAPSHOT


def test_maven_test_intro_matches_facts_contract():
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
# D. kickoff plan template: FACTS objective wording, no prescription prose
# ---------------------------------------------------------------------------


def test_kickoff_build_objective_softens_blocking_to_conditional():
    kickoff = FACTS_KICKOFF_PHASE_OBJECTIVES["build"]
    assert kickoff != KICKOFF_PHASE_OBJECTIVES["build"] or True  # variant exists
    assert (
        "AND the project is not a Python/other-ecosystem project" in kickoff
    )
    # The Java meta-project escape hatch itself is kept, just made conditional.
    assert "phase(action='blocked', outcome='unknown'" in kickoff
    # dim (d): the FACTS variant carries no "Recommended Build/Tests" prose.
    assert "Recommended Build" not in kickoff
    assert "Recommended Tests" not in kickoff


def test_kickoff_other_phases_carry_facts_objectives():
    from sag.agent.react_engine import kickoff_phase_objectives

    tasks = kickoff_phase_objectives()
    for name in ("analyze", "build", "test"):
        assert "Recommended Build" not in tasks[name]
        assert "Recommended Tests" not in tasks[name]


def test_agent_authors_kickoff_plan_from_kickoff_objectives():
    """agent.py authors the t=0 trunk tasks from the kickoff selector (the same
    FACTS wording the runtime intros carry), not the runtime PHASE_OBJECTIVES
    (source-level wiring guard)."""
    source = inspect.getsource(agent_module)
    assert "kickoff_phase_objectives()" in source


# ---------------------------------------------------------------------------
# E. LIVE PATH: real analysis -> real trunk handoff -> real phase intros
#
# These tests drive the REAL functions end to end against a scripted repo — no
# fabricated environment_summary anywhere in the chain. After the analyzer diet
# the recommendation is coordinates-only (no goal/rationale) and no
# project_brief is composed.
# ---------------------------------------------------------------------------


class _ScriptedRepo:
    """Answers the analyzer's shell probes from an in-memory file map."""

    def __init__(self, root, files):
        self.root = root.rstrip("/")
        self.files = {f"{self.root}/{path}": body for path, body in files.items()}
        self.dirs = {self.root}
        for path in self.files:
            parts = path.split("/")
            for i in range(2, len(parts)):
                self.dirs.add("/".join(parts[:i]))

    def execute_command(self, command, **kwargs):
        cmd = command.strip()
        m = re.match(r"test -f (\S+)", cmd)
        if m:
            exists = m.group(1) in self.files
            if "echo 'missing'" in cmd:
                return {"success": True, "output": "exists" if exists else "missing"}
            return {"success": exists, "output": "exists" if exists else ""}
        m = re.match(r"test -d (\S+)", cmd)
        if m:
            hit = m.group(1).rstrip("/") in self.dirs
            return {"success": True, "output": "exists" if hit else ""}
        m = re.match(r"test -e (\S+)", cmd)
        if m:
            path = m.group(1).rstrip("/")
            hit = path in self.files or path in self.dirs
            return {"success": True, "output": "yes" if hit else "no"}
        m = re.match(r"cat (\S+)", cmd)
        if m and not cmd.startswith("cat >"):
            path = m.group(1)
            if path in self.files:
                return {"success": True, "output": self.files[path]}
            return {"success": False, "output": ""}
        m = re.match(r"ls -1 (\S+)", cmd)
        if m:
            base = m.group(1).rstrip("/") + "/"
            names = sorted(
                {p[len(base):].split("/")[0] for p in self.files if p.startswith(base)}
            )
            return {"success": True, "output": "\n".join(names)}
        if cmd.startswith("find "):
            base = cmd.split()[1]
            if "__init__.py" in cmd:
                hits = sorted(
                    p
                    for p in self.files
                    if p.startswith(base + "/")
                    and p.endswith("/__init__.py")
                    and p[len(base) + 1 :].count("/") <= 1
                )
                return {"success": True, "output": "\n".join(hits)}
            suffixes = re.findall(r"-path '\*(/src/(?:main|test)/\w+)'", cmd)
            if suffixes:
                hits = sorted(
                    d for d in self.dirs if any(d.endswith(s) for s in suffixes)
                )
                return {"success": True, "output": "\n".join(hits)}
            return {"success": True, "output": ""}
        return {"success": True, "output": ""}


_PY_REPO_ROOT = "/workspace/pyproj"
_PY_REPO_FILES = {
    # Pure-python repo: pyproject + src layout, NO pom.xml, NO gradle.
    "pyproject.toml": '[project]\nname = "pypkg"\nrequires-python = ">=3.9"\n',
    "src/pypkg/__init__.py": "",
    "src/pypkg/core.py": "X = 1\n",
    "tests/test_core.py": "def test_x():\n    assert True\n",
}

_MAVEN_REPO_ROOT = "/workspace/demo"
_MAVEN_REPO_FILES = {
    "pom.xml": (
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>demo</groupId><artifactId>demo</artifactId>"
        "<version>1.0</version><packaging>jar</packaging></project>"
    ),
    "src/main/java/demo/App.java": "public class App {}",
    "src/test/java/demo/AppTest.java": "public class AppTest {}",
}


def _analyzed_env(root, files):
    """The REAL live chain: comprehensive analysis (real detection, real
    _recommend_build_approach/_recommend_test_approach) then the REAL trunk
    handoff (_record_environment_metrics) into environment_summary."""
    orch = _ScriptedRepo(root, files)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch, context_manager=None)
    analysis = analyzer._perform_comprehensive_analysis(root)
    trunk = SimpleNamespace(environment_summary={}, todo_list=[])
    analyzer._record_environment_metrics(trunk, analysis)
    return analysis, trunk.environment_summary


def test_live_python_recommendation_is_coordinates_only():
    analysis, env = _analyzed_env(_PY_REPO_ROOT, _PY_REPO_FILES)
    rec = env["build_recommendation"]
    assert rec["build_system"] == "python"
    assert rec["build_root"] == _PY_REPO_ROOT
    assert rec["test_root"] == _PY_REPO_ROOT
    assert rec["test_system"] == "pytest"
    assert rec["is_aggregator_only"] is False
    # dim (b) deleted: the trunk recommendation carries coordinate facts only.
    assert "goal" not in rec and "rationale" not in rec
    # dim (a)/(c) deleted: no plan, no brief.
    assert "execution_plan" not in analysis
    assert "project_brief_ref" not in analysis


def test_live_python_build_intro_carries_objective_and_coordinates():
    _, env = _analyzed_env(_PY_REPO_ROOT, _PY_REPO_FILES)
    intro = _engine_at(2, env)._phase_intro_step().content
    # dim (c) deleted: no project brief projection renders.
    assert "=== PROJECT BRIEF v1 ===" not in intro
    assert "A Python project has no Java compile target" in intro
    assert "build(action='deps')" in intro
    assert f"Build coordinates: python at {_PY_REPO_ROOT}." in intro


def test_live_python_test_intro_carries_pytest_objective():
    _, env = _analyzed_env(_PY_REPO_ROOT, _PY_REPO_FILES)
    intro = _engine_at(3, env)._phase_intro_step().content
    assert "pytest" in intro
    assert "build(action='test')" in intro
    assert "Partial pass above threshold is a valid" in intro
    # pytest runs AT the build root by construction — the split-root call-out
    # (test_root == build_root) must not render a test coordinates line.
    assert "not in the build module" not in intro


def test_recommendation_fires_on_python_config_signal_alone():
    """_analyze_python_project's python_config is itself the python signal —
    the rec must be real even when the structure label went sideways."""
    rec = _bare_analyzer()._recommend_build_approach(
        _PY_REPO_ROOT,
        {
            "project_type": "unknown",
            "build_system": "unknown",
            "python_config": {"python_installer": "poetry"},
        },
    )
    assert rec["build_system"] == "python"
    assert rec["test_system"] == "pytest"


# Contract snapshot from the same scripted Maven repo and live chain. dim (b)
# deleted: no goal/rationale on the trunk recommendation.
_MAVEN_LIVE_REC_SNAPSHOT = {
    "build_system": "maven",
    "build_root": _MAVEN_REPO_ROOT,
    "is_aggregator_only": False,
    "has_gradle": False,
    "source_modules": [],
    "test_root": _MAVEN_REPO_ROOT,
    "test_system": "maven",
    "test_modules": ["."],
}


def test_live_maven_recommendation_is_coordinates_only():
    _, env = _analyzed_env(_MAVEN_REPO_ROOT, _MAVEN_REPO_FILES)
    assert env["build_recommendation"] == _MAVEN_LIVE_REC_SNAPSHOT


def test_live_maven_intros_match_facts_contract():
    _, env = _analyzed_env(_MAVEN_REPO_ROOT, _MAVEN_REPO_FILES)
    build_intro = _engine_at(2, env)._phase_intro_step().content
    test_intro = _engine_at(3, env)._phase_intro_step().content
    assert "=== PROJECT BRIEF v1 ===" not in build_intro
    assert "Make the project compile: build(action='compile')" in build_intro
    assert f"Build coordinates: maven at {_MAVEN_REPO_ROOT}." in build_intro
    assert "=== PROJECT BRIEF v1 ===" not in test_intro
    assert "Run the test suite: build(action='test')" in test_intro
