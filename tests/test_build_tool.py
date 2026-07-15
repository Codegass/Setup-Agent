# tests/test_build_tool.py
"""build(action: deps|compile|test|package): one tool, backend per ecosystem.

Spec §4 growth law: the schema is O(1) in ecosystems; backends are selected
from project evidence; verbs that don't apply return verdict=skipped.
Stage 1: backends DELEGATE to the existing MavenTool/GradleTool.
"""

import shlex
from types import SimpleNamespace

from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool


class FakeBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, returns a scripted result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class MarkerOrchestrator:
    """Answers build-marker probes: which marker files exist."""

    def __init__(self, markers):
        self.markers = set(markers)

    def execute_command(self, command, **kwargs):
        for m in self.markers:
            if m in command:
                return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


class ShellParsingMarkerOrchestrator:
    """Simulates shell tokenization for `test -f <path>` marker probes."""

    def __init__(self, existing_paths):
        self.existing_paths = set(existing_paths)
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        tokens = shlex.split(command)
        try:
            path = tokens[tokens.index("-f") + 1]
        except (ValueError, IndexError):
            path = ""
        output = "exists" if path in self.existing_paths else "missing"
        return {"success": True, "output": output, "exit_code": 0}


def _tool(markers, maven=None, gradle=None):
    return BuildTool(
        MarkerOrchestrator(markers),
        maven_tool=maven or FakeBackendTool(),
        gradle_tool=gradle or FakeBackendTool(),
    )


def test_maven_project_routes_compile_to_maven_backend():
    maven = FakeBackendTool()
    tool = _tool({"pom.xml"}, maven=maven)

    result = tool.execute(action="compile", working_directory="/workspace/p")

    assert result.succeeded
    assert maven.calls and maven.calls[0]["command"] == "compile"
    assert result.facts["system"] == "maven"


def test_build_marker_probe_quotes_working_directory_with_spaces():
    maven = FakeBackendTool()
    orchestrator = ShellParsingMarkerOrchestrator({"/workspace/project with spaces/pom.xml"})
    tool = BuildTool(orchestrator, maven_tool=maven)

    result = tool.execute(action="compile", working_directory="/workspace/project with spaces")

    assert result.succeeded
    assert result.facts["system"] == "maven"
    assert maven.calls and maven.calls[0]["working_directory"] == "/workspace/project with spaces"


def test_gradle_kts_project_routes_test_to_gradle_backend():
    gradle = FakeBackendTool()
    tool = _tool({"build.gradle.kts"}, gradle=gradle)

    result = tool.execute(action="test", working_directory="/workspace/p")

    assert gradle.calls and gradle.calls[0]["tasks"] == "test"
    assert result.facts["system"] == "gradle"


def test_deps_verb_maps_per_ecosystem():
    maven, gradle = FakeBackendTool(), FakeBackendTool()
    _tool({"pom.xml"}, maven=maven).execute(action="deps", working_directory="/w")
    _tool({"settings.gradle"}, gradle=gradle).execute(action="deps", working_directory="/w")

    assert maven.calls[0]["command"] == "dependency:resolve"
    assert gradle.calls[0]["tasks"] == "dependencies"


def test_unknown_system_returns_unknown_with_evidence():
    tool = _tool(set())
    result = tool.execute(action="compile", working_directory="/workspace/p")

    assert result.operation_outcome.value == "unknown"
    assert "checked" in result.facts
    assert result.facts["checked"], "must list the markers probed"


def test_test_stats_surface_in_facts():
    from sag.evidence import TestStats

    maven = FakeBackendTool(
        result=ToolResult.completed_success(
            output="tests done",
            test_stats=TestStats(executed=214, passed=206, failed=3, skipped=5),
        )
    )
    tool = _tool({"pom.xml"}, maven=maven)

    result = tool.execute(action="test", working_directory="/w")

    assert result.facts["executed"] == 214
    assert result.facts["passed"] == 206
    assert result.operation_outcome.value == "partial"


def test_args_passthrough():
    maven = FakeBackendTool()
    tool = _tool({"pom.xml"}, maven=maven)

    tool.execute(action="test", args="-Dtest=FooTest", working_directory="/w")

    assert maven.calls[0].get("extra_args") == "-Dtest=FooTest" or "-Dtest=FooTest" in str(
        maven.calls[0]
    )
