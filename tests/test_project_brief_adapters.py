import json
from copy import deepcopy
from types import SimpleNamespace

from sag.agent.project_brief import PROJECT_BRIEF_PATH, ProjectBriefAdapter
from sag.agent.react_engine import ReActEngine
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class _WorkspaceFiles:
    def __init__(self, project_path="/workspace/project"):
        self.project_path = project_path
        self.files = {
            f"{project_path}/pom.xml": "<project><java.version>8</java.version></project>",
            f"{project_path}/README.md": "Requires JDK 8. Run mvn install.",
            "/workspace/.setup_agent/env_overlay.json": json.dumps(
                {
                    "version": 1,
                    "tools": {
                        "java": {
                            "active": "/opt/jdk-17/bin/java",
                            "candidates": {
                                "/opt/jdk-17/bin/java": {
                                    "version": "17",
                                    "source": "provisioned",
                                    "env": {"JAVA_HOME": "/opt/jdk-17"},
                                    "path_prepend": ["/opt/jdk-17/bin"],
                                }
                            },
                            "blocked": [],
                        }
                    },
                }
            ),
        }
        self.commands = []

    def execute_command(self, command, **_kwargs):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("cat > ") and " <<'" in command:
            first, body = command.split("\n", 1)
            path = first.split()[2]
            delimiter = first.rsplit("'", 2)[1]
            suffix = f"\n{delimiter}"
            self.files[path] = body[: -len(suffix)] + "\n"
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("truncate -s -1 "):
            path = command.split()[-1]
            self.files[path] = self.files[path][:-1]
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("mv "):
            _verb, source, target = command.split()
            self.files[target] = self.files.pop(source)
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("git -C ") and " submodule status" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": " abc123 third_party/lib (heads/main)",
            }
        if command.startswith("cat "):
            path = command.split()[1]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.files[path]}
        return {"success": False, "exit_code": 1, "output": ""}


def _analysis(project_path="/workspace/project"):
    return {
        "project_path": project_path,
        "project_type": "Java",
        "build_system": "Maven",
        "existing_files": ["pom.xml", "README.md"],
        "java_version": "8",
        "java_version_source": "maven-compiler",
        "documentation": {
            "source_path": "README.md",
            "readme_content": "Requires JDK 8. Run mvn install.",
            "java_version_requirement": "8",
            "build_commands": ["mvn install"],
            "test_commands": ["mvn test"],
        },
        "build_recommendation": {
            "build_root": project_path,
            "build_system": "maven",
            "goal": "install",
            "test_root": project_path,
            "test_system": "maven",
        },
    }


def test_analyzer_and_overlay_adapters_preserve_roles_refs_and_relative_roots():
    orchestrator = _WorkspaceFiles()
    artifact = ProjectBriefAdapter(
        orchestrator,
        analyzer_version="analyzer-v1",
    ).compose(_analysis(), project_path=orchestrator.project_path)

    assert artifact.cache_hit is False
    assert PROJECT_BRIEF_PATH in orchestrator.files
    assert artifact.brief.section("recommended-build").build_steps[0].root == "."
    action = artifact.brief.section("actions").instructions[0]
    assert action.instruction_id == "provision-jdk"
    assert action.refs == (
        "env-overlay://java",
        "manifest://pom.xml#java-version",
    )
    assert "/workspace/project" not in json.dumps(
        artifact.brief.model_dump(mode="json"), sort_keys=True
    )


def test_identical_inputs_hit_cache_and_docs_change_recomposes():
    orchestrator = _WorkspaceFiles()
    adapter = ProjectBriefAdapter(orchestrator, analyzer_version="analyzer-v1")
    analysis = _analysis()

    first = adapter.compose(analysis, project_path=orchestrator.project_path)
    second = adapter.compose(deepcopy(analysis), project_path=orchestrator.project_path)
    changed = deepcopy(analysis)
    changed["documentation"]["readme_content"] = "Requires JDK 8. Run mvn verify."
    orchestrator.files[f"{orchestrator.project_path}/README.md"] = "Requires JDK 8. Run mvn verify."
    third = adapter.compose(changed, project_path=orchestrator.project_path)

    assert second.cache_hit is True
    assert second.brief.input_fingerprint == first.brief.input_fingerprint
    assert third.cache_hit is False
    assert third.brief.input_fingerprint != first.brief.input_fingerprint
    assert adapter.composer.composition_count == 2


def test_project_location_is_not_part_of_the_semantic_fingerprint():
    first_orchestrator = _WorkspaceFiles("/workspace/first")
    second_orchestrator = _WorkspaceFiles("/workspace/second")

    first = ProjectBriefAdapter(first_orchestrator, analyzer_version="analyzer-v1").compose(
        _analysis("/workspace/first"), project_path="/workspace/first"
    )
    second = ProjectBriefAdapter(second_orchestrator, analyzer_version="analyzer-v1").compose(
        _analysis("/workspace/second"), project_path="/workspace/second"
    )

    assert first.brief.input_fingerprint == second.brief.input_fingerprint
    assert first.brief.model_dump(mode="json") == second.brief.model_dump(mode="json")


def test_analyzer_hook_attaches_complete_brief_and_planner_projection():
    orchestrator = _WorkspaceFiles()
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orchestrator)
    analysis = _analysis()

    analyzer._compose_project_brief(orchestrator.project_path, analysis)

    assert analysis["project_brief"]["input_fingerprint"]
    assert analysis["project_brief_ref"] == PROJECT_BRIEF_PATH
    assert len(analysis["project_brief_projection"]) <= 1200
    assert analysis["project_brief_cache_hit"] is False
    assert PROJECT_BRIEF_PATH in analyzer._format_analysis_output(analysis)

    trunk = SimpleNamespace(environment_summary={})
    analyzer._record_environment_metrics(trunk, analysis)
    assert trunk.environment_summary["project_brief_ref"] == PROJECT_BRIEF_PATH
    assert (
        trunk.environment_summary["project_brief_fingerprint"]
        == analysis["project_brief"]["input_fingerprint"]
    )
    assert (
        trunk.environment_summary["project_brief_projection"]
        == analysis["project_brief_projection"]
    )


def test_phase_intro_consumes_brief_projection_without_legacy_duplicate_blocks():
    projection = (
        "=== PROJECT BRIEF v1 ===\nRECOMMENDED BUILD:\n- root=python system=python goal=deps"
    )
    trunk = SimpleNamespace(
        environment_summary={
            "build_system": "python",
            "build_recommendation": {
                "build_system": "python",
                "build_root": "/workspace/project/python",
                "goal": "deps",
                "has_native_build": True,
            },
            "project_brief_projection": projection,
            "project_brief_ref": PROJECT_BRIEF_PATH,
        }
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(
        current_phase="build",
        digest_lines=lambda: ["• analyze [success]: project analyzed"],
    )
    engine.context_manager = SimpleNamespace(load_trunk_context=lambda: trunk)
    engine.config = SimpleNamespace(
        max_iterations=150,
        phase_min_floors={},
        phase_handoff_char_budget=6000,
    )
    engine._run_max_iterations = 150
    engine.current_iteration = 5
    engine.phase_handoff = None
    engine.prompt_builder = None
    engine._get_timestamp = lambda: "ts"

    intro = engine._phase_intro_step().content

    assert intro.count(projection) == 1
    assert "NATIVE CORE FIRST" not in intro
    assert "Recommended Build:" not in intro
