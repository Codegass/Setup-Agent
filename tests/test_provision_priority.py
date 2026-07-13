"""Provisioning must follow the PRIMARY language ADDITIVELY, not substitutively.

Live evidence (session 20260713_014403_27874): TVM is a Python-primary repo
(root pyproject.toml/setup.py, python/ package) that ships a jvm/ Maven binding
subdirectory. On the live clone path the provision phase installed "Java JDK
default and Maven" (pulled by jvm/pom.xml) and never installed the python
toolchain — the direct cause of T1's broken venv. After the classifier
root-precedence fix it swung the other way: python installed, Java for the jvm
binding silently DROPPED. Neither is correct.

Two invariants under test, both exercised through the LIVE provisioning flow
(_install_dependencies_for_project_type / detect_jvm_binding_dir), not a helper
that nothing calls:

A. A Python-primary repo (classifier -> project_type 'python') that also ships a
   JVM binding subdirectory (jvm/pom.xml) provisions the python toolchain AND,
   ADDITIVELY, Java+Maven for that binding. Python must not displace Java, and
   Java must not displace python. Java-primary projects are unchanged.

B. The report's '## Actionable Recommendations' template must be
   language-aware: a snapshot whose build system is python renders pytest/pip
   guidance and NEVER emits 'mvn' strings.
"""

from sag.tools.internal.project_setup_tool import (
    ProjectSetupTool,
    detect_jvm_binding_dir,
)


# ---------------------------------------------------------------------------
# Part A — detect_jvm_binding_dir: the LIVE trigger for additive provisioning
# ---------------------------------------------------------------------------

# The TVM-shaped classifier output: root marker says python; the maxdepth-2
# build-file scan also captured the nested jvm/pom.xml binding.
TVM_PROJECT_TYPE = {
    "type": "python",
    "language": "python",
    "build_files": [
        "/workspace/tvm/jvm/pom.xml",  # nested Java binding
        "/workspace/tvm/pyproject.toml",  # root python marker
    ],
    "suggested_tools": ["uv", "bash"],
}

# A pure Java (Maven) project — must be untouched by the additive path.
MAVEN_PROJECT_TYPE = {
    "type": "maven",
    "language": "java",
    "build_files": ["/workspace/svc/pom.xml"],
    "suggested_tools": ["maven", "bash"],
}


def test_python_primary_with_jvm_subdir_detects_binding():
    """TVM shape: python primary + nested jvm/pom.xml -> the binding directory
    is detected so Java can be provisioned ADDITIVELY."""
    binding = detect_jvm_binding_dir(TVM_PROJECT_TYPE, "/workspace/tvm")
    assert binding == "/workspace/tvm/jvm"


def test_python_primary_root_pom_is_not_a_binding():
    """A python-primary repo whose only pom.xml is at the ROOT is the repo's own
    build file, not a nested binding — no additive Java provisioning triggered."""
    project_type = {
        "type": "python",
        "build_files": ["/workspace/p/pom.xml", "/workspace/p/pyproject.toml"],
    }
    assert detect_jvm_binding_dir(project_type, "/workspace/p") is None


def test_java_primary_project_has_no_binding():
    """A Java-primary project never routes through the additive python+jvm path."""
    assert detect_jvm_binding_dir(MAVEN_PROJECT_TYPE, "/workspace/svc") is None


def test_python_only_repo_has_no_binding():
    """A pure python repo (no JVM build file anywhere) has no binding to add."""
    project_type = {
        "type": "python",
        "build_files": ["/workspace/p/pyproject.toml"],
    }
    assert detect_jvm_binding_dir(project_type, "/workspace/p") is None


# ---------------------------------------------------------------------------
# Part A — the LIVE install path provisions BOTH toolchains for the TVM shape.
# ---------------------------------------------------------------------------

class _RecordingOrchestrator:
    """Records apt install commands so the test can prove the JDK+Maven for the
    jvm binding are installed ADDITIVELY. Answers pom reads (no enforced Java
    version) and apt installs with success."""

    def __init__(self):
        self.install_commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        if "apt-get install" in command:
            self.install_commands.append(command)
        return {"success": True, "output": "", "exit_code": 0}


def test_live_python_primary_additively_provisions_jvm_binding(monkeypatch):
    """The concrete TVM regression, through the LIVE path: python primary +
    nested jvm/pom.xml -> python deps installed AND (additively) the JVM
    binding's Java+Maven installed. Java is ADDED, never SUBSTITUTED for python,
    and python is never SUBSTITUTED for Java."""
    orch = _RecordingOrchestrator()
    tool = ProjectSetupTool(orchestrator=orch)

    # Isolate the additive branch: stand in for the (separately tested) python
    # installer so this test asserts purely on the additive JVM provisioning.
    python_calls = {}

    def fake_python(directory):
        python_calls["directory"] = directory
        return {"success": True, "installed": "Python dependencies via pip"}

    monkeypatch.setattr(tool, "_install_python_dependencies", fake_python)
    # Keep environment side-effects (overlay registration / java_home probing)
    # out of the unit under test — the install COMMANDS are the contract.
    monkeypatch.setattr(tool, "_setup_java_environment", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_register_java_runtime_overlay", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_register_maven_runtime_overlay", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_provision_required_maven_if_needed", lambda *a, **k: False)

    result = tool._install_dependencies_for_project_type(
        TVM_PROJECT_TYPE, "/workspace/tvm"
    )

    # Python was provisioned (not displaced by the jvm binding).
    assert python_calls["directory"] == "/workspace/tvm"
    assert result["success"] is True

    # AND Java+Maven were provisioned ADDITIVELY for the jvm binding.
    assert result.get("jvm_binding", {}).get("success") is True
    maven_installs = [c for c in orch.install_commands if " maven" in c]
    assert maven_installs, "expected an apt install that provisions maven for the jvm binding"
    assert any("jdk" in c for c in orch.install_commands), (
        "expected a JDK to be installed for the jvm binding"
    )
    # The additive install is narrated in the combined 'installed' string.
    assert "Python dependencies" in result["installed"]
    assert "Maven" in result["installed"]


def test_live_java_primary_provisions_no_python(monkeypatch):
    """Guardrail: a Java-primary repo installs the JDK+Maven and NEVER routes
    into python provisioning — Java-primary provisioning is byte-identical."""
    orch = _RecordingOrchestrator()
    tool = ProjectSetupTool(orchestrator=orch)

    called = {"python": False}
    monkeypatch.setattr(
        tool, "_install_python_dependencies",
        lambda d: called.__setitem__("python", True) or {"success": True},
    )
    monkeypatch.setattr(tool, "_setup_java_environment", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_register_java_runtime_overlay", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_register_maven_runtime_overlay", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_provision_required_maven_if_needed", lambda *a, **k: False)

    result = tool._install_dependencies_for_project_type(
        MAVEN_PROJECT_TYPE, "/workspace/svc"
    )

    assert result["success"] is True
    assert called["python"] is False
    assert "jvm_binding" not in result  # additive path not entered for java-primary
    # No python toolchain packages were installed.
    assert not any("python3-venv" in c for c in orch.install_commands)


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
    pom.xml must not classify the whole repo as Maven. The nested pom is still
    captured in build_files so additive Java provisioning can find it."""
    orch = _FindOrchestrator(
        "/workspace/tvm",
        files=["jvm/pom.xml", "pyproject.toml"],  # nested pom listed FIRST
    )
    tool = ProjectSetupTool(orchestrator=orch)
    detected = tool._detect_project_type_in_directory("/workspace/tvm")
    assert detected["type"] == "python"
    assert detected["language"] == "python"
    # The nested pom survives in build_files -> the additive path can detect it.
    assert detect_jvm_binding_dir(detected, "/workspace/tvm") == "/workspace/tvm/jvm"


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
