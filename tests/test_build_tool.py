# tests/test_build_tool.py
"""build(action: deps|compile|test|package): one tool, backend per ecosystem.

Spec §4 growth law: the schema is O(1) in ecosystems; backends are selected
from project evidence; verbs that don't apply return verdict=skipped.
Stage 1: backends DELEGATE to the existing MavenTool/GradleTool.
"""

import json
import shlex
from types import SimpleNamespace

import pytest
from container_evidence_fakes import ContainerFS, add_published_mutable_json

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

pytestmark = pytest.mark.usefixtures(
    "facade_contract_authority", "exact_build_facade_authority"
)


class FakeBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, returns a scripted result."""

    def __init__(self, result=None, orchestrator=None):
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")
        self.orchestrator = orchestrator

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class MarkerOrchestrator:
    """Answers build-marker probes: which marker files exist."""

    def __init__(self, markers, files=None, *, publish_manifest=True):
        self.markers = set(markers)
        self.files = dict(files or {})
        self.commands = []
        self.evidence = ContainerFS()
        if publish_manifest:
            raw = self.files.get(REQUIREMENTS_PATH)
            payload = json.loads(raw) if raw is not None else {}
            add_published_mutable_json(
                self,
                self.evidence,
                path=REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=payload,
            )

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command, **kwargs)
        for m in self.markers:
            if m in command:
                return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


class ShellParsingMarkerOrchestrator:
    """Simulates shell tokenization for `test -f <path>` marker probes."""

    def __init__(self, existing_paths):
        self.existing_paths = set(existing_paths)
        self.commands = []
        self.evidence = ContainerFS()
        add_published_mutable_json(
            self,
            self.evidence,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload={},
        )

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command, **kwargs)
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


@pytest.mark.parametrize(
    ("marker", "backend_name", "expected_key", "expected_value"),
    (
        ("pom.xml", "maven", "command", "dependency:resolve"),
        ("settings.gradle", "gradle", "tasks", "dependencies"),
    ),
)
def test_deps_verb_maps_per_ecosystem(
    marker, backend_name, expected_key, expected_value
):
    backend = FakeBackendTool()
    kwargs = {f"{backend_name}_tool": backend}

    BuildTool(MarkerOrchestrator({marker}), **kwargs).execute(
        action="deps", working_directory="/w"
    )

    assert backend.calls[0][expected_key] == expected_value


def test_unknown_system_returns_unknown_with_evidence():
    tool = _tool(set())
    result = tool.execute(action="compile", working_directory="/workspace/p")

    assert result.operation_outcome.value == "unknown"
    assert "checked" in result.facts
    assert result.facts["checked"], "must list the markers probed"


def test_unpublished_manifest_cannot_route_or_freeze_a_build_call():
    orchestrator = MarkerOrchestrator(
        {"pom.xml"},
        files={REQUIREMENTS_PATH: json.dumps({"build_system": "maven"})},
        publish_manifest=False,
    )
    maven = FakeBackendTool()

    result = BuildTool(orchestrator, maven_tool=maven).execute(
        action="compile",
        working_directory="/workspace/p",
    )

    assert result.error_code == "BUILD_REQUIREMENTS_UNAVAILABLE"
    assert result.metadata["runner_dispatched"] is False
    assert result.metadata["blocker_owner"] == "harness"
    assert maven.calls == []
    assert not any("/workspace/p/pom.xml" in command for command in orchestrator.commands)


@pytest.mark.parametrize(
    "invalid_params",
    [
        {"timeout": 0},
        {"timeout": True},
        {"args": ""},
        {"args": "   "},
        {"features": []},
        {"definitions": {}},
    ],
)
def test_invalid_or_unconsumed_public_params_are_refused_before_marker_probe(
    invalid_params,
):
    orchestrator = MarkerOrchestrator({"pom.xml"})
    maven = FakeBackendTool()

    result = BuildTool(orchestrator, maven_tool=maven).execute(
        action="compile",
        working_directory="/workspace/p",
        **invalid_params,
    )

    assert result.error_code == "BUILD_PARAMETER_INVALID"
    assert orchestrator.markers == {"pom.xml"}
    assert maven.calls == []


@pytest.mark.parametrize("system", ["gradle", "python"])
def test_maven_requirement_is_refused_for_wrong_ecosystem(system):
    marker = "build.gradle" if system == "gradle" else "pyproject.toml"
    backend = FakeBackendTool()
    kwargs = {f"{system}_tool": backend}

    result = BuildTool(MarkerOrchestrator({marker}), **kwargs).execute(
        action="compile",
        working_directory="/workspace/p",
        maven_version_requirement="[3.9,4.0)",
    )

    assert result.error_code == "BUILD_PARAMETER_INVALID"
    assert backend.calls == []


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


def test_maven_test_runs_the_full_verify_lifecycle():
    maven = FakeBackendTool()
    tool = _tool({"pom.xml"}, maven=maven)

    tool.execute(action="test", working_directory="/w")

    assert maven.calls[0]["command"] == "verify"
    assert maven.calls[0]["fail_at_end"] is True


def test_maven_requirement_propagates_through_build_backend():
    maven = FakeBackendTool()
    tool = _tool({"pom.xml"}, maven=maven)

    tool.execute(
        action="compile",
        working_directory="/w",
        maven_version_requirement="[3.9,4.0)",
    )

    assert maven.calls[0]["maven_version_requirement"] == "[3.9,4.0)"


def test_args_passthrough():
    maven = FakeBackendTool()
    tool = _tool({"pom.xml"}, maven=maven)

    tool.execute(action="test", args="-Dtest=FooTest", working_directory="/w")

    assert maven.calls[0].get("extra_args") == "-Dtest=FooTest" or "-Dtest=FooTest" in str(
        maven.calls[0]
    )


def test_pathological_gradle_island_promotes_compile_to_manifest_install_goal():
    root = "/workspace/bigtop"
    island = f"{root}/bigtop-data-generators"
    manifest = {
        "survey": {"project_path": root},
        "root_shape": "pathological_aggregator",
        "build_islands": [
            {
                "root": island,
                "system": "gradle",
                "goal": "publishToMavenLocal",
            }
        ],
    }
    orchestrator = MarkerOrchestrator(
        {"build.gradle"},
        files={
            REQUIREMENTS_PATH: json.dumps(manifest),
            f"{island}/build.gradle": (
                "// enough content that a presentation read would be unsafe\n"
                + ("x" * 12000)
                + "\nsubprojects { apply plugin: 'maven-publish' }\n"
            ),
        },
    )
    gradle = FakeBackendTool(orchestrator=orchestrator)
    tool = BuildTool(orchestrator, gradle_tool=gradle)

    result = tool.execute(action="compile", working_directory=island)

    assert result.succeeded
    assert gradle.calls[0]["tasks"] == "publishToMavenLocal"
    assert result.facts == {
        "system": "gradle",
        "action": "install",
        "requested_action": "compile",
        "effective_action": "install",
        "island_root": island,
        "manifest_goal": "publishToMavenLocal",
    }
    assert result.metadata["action_source"] == (
        "/workspace/.setup_agent/build_requirements.json#build_islands"
    )
    assert result.metadata["working_directory"] == island
    assert "[island]" in result.output


def test_pathological_island_promotion_never_replaces_test():
    root = "/workspace/bigtop"
    island = f"{root}/bigtop-data-generators"
    orchestrator = MarkerOrchestrator(
        {"build.gradle"},
        files={
            REQUIREMENTS_PATH: json.dumps(
                {
                    "survey": {"project_path": root},
                    "root_shape": "pathological_aggregator",
                    "build_islands": [
                        {
                            "root": island,
                            "system": "gradle",
                            "goal": "publishToMavenLocal",
                        }
                    ],
                }
            ),
            f"{island}/build.gradle": "apply plugin: 'maven-publish'\n",
        },
    )
    gradle = FakeBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, gradle_tool=gradle).execute(
        action="test",
        working_directory=island,
    )

    assert result.succeeded
    assert gradle.calls[0]["tasks"] == "test"
    assert result.facts["requested_action"] == "test"
    assert result.facts["effective_action"] == "test"
