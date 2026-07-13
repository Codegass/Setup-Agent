"""Provisioning must follow the PRIMARY language, not a binding subdirectory.

Live evidence (session 20260713_014403_27874): TVM is a Python-primary repo
(root pyproject.toml/setup.py, python/ package) that ships a jvm/ Maven binding
subdirectory. The provision phase installed "Java JDK default and Maven" (pulled
by jvm/pom.xml) and never installed python3-venv/python3-pip — the direct setup
for T1's broken venv. The final report even recommended 'mvn clean test' for a
Python project.

Two invariants under test:

A. When the project analysis classifies the repo as Python-primary
   (project_type / build_recommendation.build_system == 'python'), the python
   toolchain (python3, python3-venv, python3-pip) is REQUIRED provisioning.
   A Java binding subdirectory (jvm/pom.xml) must NOT displace it — installing
   Java too is fine (additive), never substitutive. Java-primary projects are
   unchanged.

B. The report's '## Actionable Recommendations' template must be
   language-aware: a snapshot whose build system is python renders pytest/pip
   guidance and NEVER emits 'mvn' strings.
"""

from sag.tools.internal.project_setup_tool import (
    ProjectSetupTool,
    provision_requirements,
)


# ---------------------------------------------------------------------------
# Part A — provisioning follows the primary language
# ---------------------------------------------------------------------------

# The TVM-shaped analysis: root markers say Python; a jvm/ Maven binding lives
# in a subdirectory. This is exactly what _analyze_project_structure emits at
# the repo root (root pyproject.toml -> project_type Python) plus the
# build_recommendation the analyzer derives for a python project.
TVM_ANALYSIS = {
    "project_type": "Python",
    "build_system": "pip/poetry",
    "build_recommendation": {
        "build_system": "python",
        "goal": "deps",
        "test_system": "pytest",
    },
    "python_config": {"python_installer": "pip"},
}

# A pure Java (Maven) project — must be untouched by the fix.
MAVEN_ANALYSIS = {
    "project_type": "Java",
    "build_system": "Maven",
    "build_recommendation": {
        "build_system": "maven",
        "goal": "compile",
    },
}


def test_python_primary_with_jvm_subdir_requires_python_toolchain():
    """TVM shape: python primary + jvm/pom present -> python3-venv/pip REQUIRED,
    regardless of the jvm binding directory."""
    reqs = provision_requirements(TVM_ANALYSIS)
    apt = reqs["apt_packages"]
    assert "python3" in apt
    assert "python3-venv" in apt
    assert "python3-pip" in apt
    # The jvm binding must not have displaced the python toolchain: python
    # remains the primary provisioning target.
    assert reqs["primary"] == "python"


def test_python_primary_provisions_python_even_when_java_added():
    """Installing Java too is additive — never substitutive. Even if a Java
    binding is present, the python toolchain is still required."""
    analysis = dict(TVM_ANALYSIS)
    analysis["java_binding_present"] = True
    reqs = provision_requirements(analysis)
    apt = reqs["apt_packages"]
    # python toolchain still there ...
    assert {"python3", "python3-venv", "python3-pip"}.issubset(set(apt))
    # ... and adding maven/jdk on top would be fine (additive), but must never
    # remove the python packages.
    assert reqs["primary"] == "python"


def test_python_primary_via_build_recommendation_only():
    """The build_recommendation.build_system == 'python' signal alone is enough
    to require the python toolchain (project_type may be Unknown at report time)."""
    analysis = {
        "project_type": "Unknown",
        "build_system": "Unknown",
        "build_recommendation": {"build_system": "python"},
    }
    reqs = provision_requirements(analysis)
    assert {"python3", "python3-venv", "python3-pip"}.issubset(set(reqs["apt_packages"]))
    assert reqs["primary"] == "python"


def test_java_primary_project_unchanged():
    """A Java-primary project provisions the JDK, NOT the python toolchain."""
    reqs = provision_requirements(MAVEN_ANALYSIS)
    apt = reqs["apt_packages"]
    assert reqs["primary"] == "java"
    assert "python3-venv" not in apt
    assert "python3-pip" not in apt


# ---------------------------------------------------------------------------
# Part A — the setup tool's directory classifier must not let a nested pom.xml
# displace a root-level python marker (this is the concrete TVM regression).
# ---------------------------------------------------------------------------

class _FindOrchestrator:
    """Answers only the maxdepth-2 build-file find used by
    _detect_project_type_in_directory. Emits full paths, jvm/pom.xml FIRST so
    the old first-match-wins loop would pick Maven."""

    def __init__(self, directory, files):
        self.directory = directory.rstrip("/")
        self.files = files

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        if command.startswith("find ") and "pom.xml" in command:
            paths = "\n".join(f"{self.directory}/{f}" for f in self.files)
            return {"success": True, "output": paths, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def test_root_python_marker_beats_nested_jvm_pom():
    """TVM regression: find -maxdepth 2 returns jvm/pom.xml AND root
    pyproject.toml. The root python marker must win — a binding subdirectory's
    pom.xml must not classify the whole repo as Maven."""
    orch = _FindOrchestrator(
        "/workspace/tvm",
        files=["jvm/pom.xml", "pyproject.toml"],  # nested pom listed FIRST
    )
    tool = ProjectSetupTool(orchestrator=orch)
    detected = tool._detect_project_type_in_directory("/workspace/tvm")
    assert detected["type"] == "python"
    assert detected["language"] == "python"


def test_nested_pom_without_root_marker_still_maven():
    """Guardrail: a genuine Maven project whose only pom.xml is one level down
    (e.g. a single-module repo/*) is still Maven — root-precedence must not
    break the ordinary nested-only case."""
    orch = _FindOrchestrator(
        "/workspace/svc",
        files=["service/pom.xml"],  # no root marker at all
    )
    tool = ProjectSetupTool(orchestrator=orch)
    detected = tool._detect_project_type_in_directory("/workspace/svc")
    assert detected["type"] == "maven"


def test_root_pom_beats_nested_python_marker():
    """Symmetric guardrail: a Java-primary repo (root pom.xml) with a nested
    python helper dir must remain Maven."""
    orch = _FindOrchestrator(
        "/workspace/j",
        files=["pom.xml", "tools/pyproject.toml"],
    )
    tool = ProjectSetupTool(orchestrator=orch)
    detected = tool._detect_project_type_in_directory("/workspace/j")
    assert detected["type"] == "maven"


# ---------------------------------------------------------------------------
# Part B — language-aware report recommendations
# ---------------------------------------------------------------------------

def _render_recommendations(build_system):
    """Render just the Issues & Recommendations section for a snapshot whose
    build system is `build_system`."""
    from sag.tools.report_tool import ReportTool

    tool = ReportTool.__new__(ReportTool)  # no orchestrator needed for rendering
    tool.context_manager = None
    tool.physical_validator = None
    snapshot = {
        "status": {
            "pass_pct": 100.0,
            "execution_rate": 100.0,
        },
        "attention": {"raw": []},
        "project": {"type": "Python", "build_system": build_system},
        "physical_evidence": {"build_system": build_system},
    }
    return "\n".join(tool._render_issues_recommendations(snapshot))


def test_python_snapshot_recommendations_have_no_mvn():
    """A python snapshot must render pytest/pip guidance and NEVER 'mvn'."""
    text = _render_recommendations("python")
    assert "mvn" not in text
    assert "pytest" in text


def test_python_snapshot_recommendations_low_exec_rate_no_mvn():
    """Even the low-execution-rate branch (which historically emitted
    'mvn test -pl ...') must stay python for a python snapshot."""
    from sag.tools.report_tool import ReportTool

    tool = ReportTool.__new__(ReportTool)
    tool.context_manager = None
    tool.physical_validator = None
    snapshot = {
        "status": {
            "pass_pct": 100.0,
            "execution_rate": 42.0,  # < 90 -> triggers the "increase rate" branch
            "skipped_modules": ["mod_a", "mod_b"],
        },
        "attention": {
            "raw": [
                {"severity": "WARNING", "icon": "⚠️", "message": "a warning"}
            ]
        },
        "project": {"type": "Python", "build_system": "python"},
        "physical_evidence": {"build_system": "python"},
    }
    text = "\n".join(tool._render_issues_recommendations(snapshot))
    assert "mvn" not in text
    assert "pytest" in text


def test_maven_snapshot_recommendations_unchanged():
    """Java snapshots keep the maven recommendations byte-for-byte."""
    text = _render_recommendations("maven")
    assert "mvn clean test -DskipTests=false" in text
