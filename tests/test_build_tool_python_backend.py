# tests/test_build_tool_python_backend.py
"""PythonBackend on the consolidated BuildTool (spec §4 growth law + plan Task 5).

Contract:
- marker priority: a JVM repo with a stray requirements.txt stays JVM
  (python markers probe AFTER maven/gradle); a pyproject-only dir is python;
- verb delegation: deps->setup_env, compile->compile, test->test,
  package->build, install->build all reach python_tool.execute;
- the envelope reports facts["system"] == "python";
- the facade's JDK pre-flight is SKIPPED for python (PythonPreflight already
  runs inside python_tool.setup_env — running it twice would double-provision).

Agent wiring (agent.py constructing PythonTool and passing it to BuildTool)
is verified by reading, per the plan; the direct-construction tests below
cover the registration path itself.
"""

import json
import shlex

import pytest

from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH


class FakePythonTool:
    """Stands in for the internal PythonTool: records calls, scripted result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(success=True, output="ok")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, scripted result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult(success=True, output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class ScriptedOrch:
    """Answers `test -f <path>` marker probes from a set of existing paths,
    the manifest read, and `java -version`; records every command so
    pre-flight activity is observable."""

    def __init__(self, existing_paths, manifest=None, java="17"):
        self.existing_paths = set(existing_paths)
        self.manifest = manifest or {}
        self.java = java
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append(command)
        if "java -version" in command:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if command == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0,
                        "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in command:
            tokens = shlex.split(command)
            try:
                path = tokens[tokens.index("-f") + 1]
            except (ValueError, IndexError):
                path = ""
            output = "exists" if path in self.existing_paths else "missing"
            return {"success": True, "exit_code": 0, "output": output}
        return {"success": True, "exit_code": 0, "output": ""}


def _tool(existing_paths, python=None, maven=None, gradle=None, manifest=None):
    orch = ScriptedOrch(existing_paths, manifest=manifest)
    tool = BuildTool(
        orch,
        maven_tool=maven,
        gradle_tool=gradle,
        python_tool=python,
    )
    return tool, orch


# ---------------------------------------------------------------------------
# Marker priority
# ---------------------------------------------------------------------------


def test_jvm_repo_with_stray_requirements_txt_stays_maven():
    maven = FakeBackendTool()
    python = FakePythonTool()
    tool, _ = _tool(
        {"/workspace/p/pom.xml", "/workspace/p/requirements.txt"},
        python=python,
        maven=maven,
    )

    result = tool.execute(action="compile", working_directory="/workspace/p")

    assert result.facts["system"] == "maven"
    assert maven.calls, "maven backend must run"
    assert not python.calls, "python backend must NOT run on a JVM repo"


def test_pyproject_only_selects_python():
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = tool.execute(action="compile", working_directory="/workspace/p")

    assert result.facts["system"] == "python"
    assert python.calls


# ---------------------------------------------------------------------------
# Verb delegation table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb,operation",
    [
        ("deps", "setup_env"),
        ("compile", "compile"),
        ("test", "test"),
        ("package", "build"),
        ("install", "build"),
    ],
)
def test_verb_reaches_python_tool_with_mapped_operation(verb, operation):
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = tool.execute(action=verb, working_directory="/workspace/p")

    assert python.calls, f"{verb} must delegate to python_tool"
    call = python.calls[0]
    assert call["operation"] == operation
    assert call["working_directory"] == "/workspace/p"
    assert result.facts["system"] == "python"
    assert result.facts["action"] == verb


def test_args_and_timeout_pass_through():
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    tool.execute(
        action="test", args="-k smoke", working_directory="/workspace/p", timeout=120
    )

    call = python.calls[0]
    assert call["args"] == "-k smoke"
    assert call["timeout"] == 120


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_python_test_envelope_reports_system_python():
    python = FakePythonTool(ToolResult(success=True, output="3 passed"))
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = tool.execute(action="test", working_directory="/workspace/p")

    assert result.success
    assert result.verdict == "success"
    assert result.facts["system"] == "python"


def test_no_python_backend_registered_is_an_honest_failure():
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=None)

    result = tool.execute(action="test", working_directory="/workspace/p")

    assert not result.success
    assert result.verdict == "failed"


# ---------------------------------------------------------------------------
# Pre-flight routing: python skips the JDK pre-flight
# ---------------------------------------------------------------------------


def test_python_system_skips_jdk_preflight():
    # Even a manifest that declares a java_version (mixed repo) must not
    # trigger the JDK pre-flight when the selected system is python:
    # PythonPreflight runs inside python_tool.setup_env instead.
    python = FakePythonTool()
    tool, orch = _tool(
        {"/workspace/p/pyproject.toml"},
        python=python,
        manifest={"java_version": "17"},
    )

    result = tool.execute(action="test", working_directory="/workspace/p")

    assert not any(
        "java -version" in c for c in orch.commands
    ), "JdkPreflight must not probe the JVM on a python project"
    assert "[pre-flight]" not in (result.output or "")


def test_maven_system_still_runs_jdk_preflight():
    maven = FakeBackendTool()
    tool, orch = _tool(
        {"/workspace/p/pom.xml"}, maven=maven, manifest={"java_version": "17"}
    )

    tool.execute(action="test", working_directory="/workspace/p")

    assert any("java -version" in c for c in orch.commands)
