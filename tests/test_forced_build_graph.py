import shlex

import pytest

from sag.agent.attempt_policy import (
    required_test_attempt,
    resolve_survey_test_candidates,
)
from sag.agent.evidence_state import StateScope
from sag.agent.forced_build_graph import verify_forced_candidate_build_graph
from sag.tools.base import ToolResult
from test_test_attempt_policy import ManifestOrchestrator, _ready_state

_NO_PARENT = object()
_DEFAULT_PARENT = object()


class GraphOrchestrator:
    def __init__(self, files=None, realpaths=None, directories=None):
        self.files = dict(files or {})
        self.realpaths = dict(realpaths or {})
        self.directories = set(directories or ())
        self.directories.update(
            posix_parent for file_path in self.files for posix_parent in _path_parents(file_path)
        )

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            path = shlex.split(command)[-1]
            resolved = self.realpaths.get(path, path)
            if resolved is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": f"{resolved}\n"}
        if command.startswith("cat -- "):
            path = shlex.split(command)[-1]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {
                "success": True,
                "exit_code": 0,
                "output": self.files[path],
            }
        if command.startswith("if test -f "):
            tokens = shlex.split(command)
            path = tokens[tokens.index("-f") + 1]
            output = "__SAG_GRAPH_FILE__" if path in self.files else "__SAG_GRAPH_ABSENT__"
            return {"success": True, "exit_code": 0, "output": output}
        if command.startswith("if test -d "):
            tokens = shlex.split(command)
            path = tokens[tokens.index("-d") + 1]
            output = (
                "__SAG_GRAPH_DIRECTORY__" if path in self.directories else "__SAG_GRAPH_ABSENT__"
            )
            return {"success": True, "exit_code": 0, "output": output}
        raise AssertionError(f"unexpected graph probe: {command}")


def _path_parents(path):
    parts = path.rstrip("/").split("/")
    return {"/".join(parts[:index]) or "/" for index in range(1, len(parts))}


def _pom(
    *modules,
    default_modules=(),
    parent_relative=_NO_PARENT,
):
    module_xml = "".join(f"<module>{module}</module>" for module in modules)
    parent_xml = ""
    if parent_relative is not _NO_PARENT:
        if parent_relative is _DEFAULT_PARENT:
            relative_xml = ""
        elif parent_relative is None:
            relative_xml = "<relativePath/>"
        else:
            relative_xml = f"<relativePath>{parent_relative}</relativePath>"
        parent_xml = (
            "<parent><groupId>example</groupId><artifactId>parent</artifactId>"
            f"<version>1</version>{relative_xml}</parent>"
        )
    profile_xml = ""
    if default_modules:
        defaults = "".join(f"<module>{module}</module>" for module in default_modules)
        profile_xml = (
            "<profiles><profile><id>default-graph</id>"
            "<activation><activeByDefault>true</activeByDefault></activation>"
            f"<modules>{defaults}</modules></profile></profiles>"
        )
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion>"
        f"{parent_xml}<modules>{module_xml}</modules>{profile_xml}</project>"
    )


def test_maven_reactor_rejects_parent_relative_module_outside_project():
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom("../outside"),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_module_outside_project"


def test_maven_reactor_rejects_module_symlink_outside_project():
    root = "/workspace/reactor"
    module = f"{root}/linked-module"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom("linked-module"),
        },
        realpaths={module: "/outside/linked-module"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_module_outside_project"


def test_maven_reactor_rejects_self_cycle_by_realpath():
    root = "/workspace/reactor"
    orch = GraphOrchestrator({f"{root}/pom.xml": _pom(".")})

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_module_cycle"


def test_maven_recursive_and_default_profile_modules_stay_forceable():
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom("core", default_modules=("default-extra",)),
            f"{root}/core/pom.xml": _pom("nested"),
            f"{root}/core/nested/pom.xml": _pom(),
            f"{root}/default-extra/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (
        root,
        f"{root}/core",
        f"{root}/core/nested",
        f"{root}/default-extra",
    )


def test_bigtop_style_maven_island_inside_checkout_stays_forceable():
    project = "/workspace/bigtop"
    island = f"{project}/bigtop-data-generators"
    orch = GraphOrchestrator(
        {
            f"{island}/pom.xml": _pom("generator-runtime"),
            f"{island}/generator-runtime/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=project,
        candidate_root=island,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (
        island,
        f"{island}/generator-runtime",
    )


def test_maven_pom_symlink_outside_project_is_unavailable():
    root = "/workspace/reactor"
    pom = f"{root}/pom.xml"
    orch = GraphOrchestrator(
        {pom: _pom()},
        realpaths={pom: "/outside/reactor/pom.xml"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_pom_outside_project"


def test_maven_direct_and_transitive_local_parents_stay_inside_project():
    root = "/workspace/project"
    candidate = f"{root}/app"
    direct = f"{root}/parents/direct"
    base = f"{root}/parents/base"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(parent_relative="../parents/direct/pom.xml"),
            f"{direct}/pom.xml": _pom(parent_relative="../base/pom.xml"),
            f"{base}/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (candidate, direct, base)


def test_maven_parent_pom_symlink_outside_project_is_unavailable():
    root = "/workspace/project"
    candidate = f"{root}/app"
    parent_pom = f"{root}/parents/pom.xml"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(parent_relative="../parents/pom.xml"),
            parent_pom: _pom(),
        },
        realpaths={parent_pom: "/outside/parent/pom.xml"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_pom_outside_project"


def test_maven_parent_chain_cycle_is_unavailable():
    root = "/workspace/project"
    candidate = f"{root}/app"
    parent = f"{root}/parent"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(parent_relative="../parent/pom.xml"),
            f"{parent}/pom.xml": _pom(parent_relative="../app/pom.xml"),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_module_cycle"


def test_maven_dynamic_parent_relative_path_fails_closed():
    root = "/workspace/project"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom(parent_relative="${parent.relativePath}"),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_parent_path_unresolved"


def test_maven_default_parent_absent_continues_as_remote_parent():
    root = "/workspace/reactor"
    orch = GraphOrchestrator({f"{root}/pom.xml": _pom(parent_relative=_DEFAULT_PARENT)})

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (root,)


def test_maven_default_parent_inside_project_is_traversed():
    root = "/workspace/project"
    candidate = f"{root}/app"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(parent_relative=_DEFAULT_PARENT),
            f"{root}/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (candidate, root)


def test_maven_default_parent_existing_outside_project_is_unavailable():
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom(parent_relative=_DEFAULT_PARENT),
            "/workspace/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_pom_outside_project"


def test_maven_empty_parent_relative_path_disables_local_parent_lookup():
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom(parent_relative=None),
            "/workspace/pom.xml": _pom(),
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "verified"
    assert result.visited_roots == (root,)


@pytest.mark.parametrize(
    "config",
    [
        "-f ../other/pom.xml",
        "--activate-profiles external",
        "-s ../settings.xml",
        "--global-settings=../global-settings.xml",
    ],
)
def test_maven_config_graph_changing_options_fail_closed(config):
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom(),
            f"{root}/.mvn/maven.config": config,
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_config_changes_graph"


def test_maven_config_non_graph_option_keeps_reactor_forceable():
    root = "/workspace/reactor"
    orch = GraphOrchestrator(
        {
            f"{root}/pom.xml": _pom(),
            f"{root}/.mvn/maven.config": "-DskipTests",
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="maven",
    )

    assert result.status == "verified"


def test_nearest_safe_maven_config_shadows_unsafe_parent_config():
    project = "/workspace/project"
    candidate = f"{project}/module"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(),
            f"{project}/.mvn/maven.config": "-DskipTests",
            "/workspace/.mvn/maven.config": "-f ../outside/pom.xml",
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=project,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "verified"


def test_nearest_empty_maven_directory_shadows_unsafe_parent_config():
    project = "/workspace/project"
    candidate = f"{project}/module"
    orch = GraphOrchestrator(
        {
            f"{candidate}/pom.xml": _pom(),
            f"{project}/.mvn/maven.config": "-f ../outside/pom.xml",
        },
        directories={f"{candidate}/.mvn"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=project,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "verified"


def test_nearest_empty_maven_directory_symlink_outside_project_is_unavailable():
    project = "/workspace/project"
    candidate = f"{project}/module"
    maven_dir = f"{candidate}/.mvn"
    orch = GraphOrchestrator(
        {f"{candidate}/pom.xml": _pom()},
        directories={maven_dir},
        realpaths={maven_dir: "/outside/project-config"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=project,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_basedir_outside_project"


def test_empty_maven_directory_above_project_is_unavailable():
    project = "/workspace/project"
    candidate = f"{project}/module"
    orch = GraphOrchestrator(
        {f"{candidate}/pom.xml": _pom()},
        directories={"/workspace/.mvn"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=project,
        candidate_root=candidate,
        system="maven",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "maven_basedir_outside_project"


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            'include(":app")\n' 'project(":app").projectDir = file("../outside-app")',
            "gradle_project_outside_project",
        ),
        (
            'include(":app")\n' 'project(":app").projectDir = providers.gradleProperty("appDir")',
            "gradle_project_dir_dynamic",
        ),
        (
            'includeFlat("sibling")',
            "gradle_include_flat_unsafe",
        ),
        (
            'includeBuild("../outside-plugin")',
            "gradle_include_build_outside_project",
        ),
        (
            "includeBuild(pluginDirectory)",
            "gradle_graph_expression_dynamic",
        ),
    ],
)
def test_gradle_external_or_dynamic_relocation_is_unavailable(settings, reason):
    root = "/workspace/gradle-root"
    orch = GraphOrchestrator({f"{root}/settings.gradle": settings})

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == reason


def test_gradle_literal_includes_and_internal_relocations_stay_forceable():
    root = "/workspace/gradle-root"
    orch = GraphOrchestrator(
        {
            f"{root}/settings.gradle.kts": (
                'include("app", "lib")\n'
                'project(":lib").projectDir = file("components/lib")\n'
                'includeBuild("build-logic")'
            )
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="gradle",
    )

    assert result.status == "verified"
    assert result.visited_roots == (root, f"{root}/build-logic")


def test_gradle_subproject_discovers_parent_settings_and_rejects_external_relocation():
    root = "/workspace/gradle-root"
    candidate = f"{root}/modules/app"
    orch = GraphOrchestrator(
        {
            f"{root}/settings.gradle": (
                'include(":app")\n' 'project(":app").projectDir = file("../outside-app")'
            )
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "gradle_project_outside_project"


def test_gradle_subproject_discovers_nearest_safe_parent_settings():
    root = "/workspace/gradle-root"
    build_root = f"{root}/nested"
    candidate = f"{build_root}/app"
    orch = GraphOrchestrator(
        {
            f"{root}/settings.gradle": 'includeBuild("../outside-plugin")',
            f"{build_root}/settings.gradle.kts": 'include("app")',
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="gradle",
    )

    assert result.status == "verified"
    assert result.visited_roots == (build_root,)


def test_gradle_subproject_rejects_ambiguous_parent_settings_pair():
    root = "/workspace/gradle-root"
    candidate = f"{root}/modules/app"
    orch = GraphOrchestrator(
        {
            f"{root}/settings.gradle": 'include("app")',
            f"{root}/settings.gradle.kts": 'include("app")',
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "gradle_settings_ambiguous"


def test_gradle_subproject_rejects_nearest_settings_above_project_root():
    root = "/workspace/project"
    candidate = f"{root}/sub"
    orch = GraphOrchestrator(
        {
            "/workspace/settings.gradle": (
                'include(":outside")\n' 'project(":outside").projectDir = file("../outside")'
            )
        }
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "gradle_settings_outside_project"


def test_gradle_subproject_with_no_settings_to_filesystem_root_is_single_project():
    root = "/workspace/project"
    candidate = f"{root}/sub"
    orch = GraphOrchestrator()

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=candidate,
        system="gradle",
    )

    assert result.status == "verified"
    assert result.visited_roots == (candidate,)


def test_gradle_settings_symlink_outside_project_is_unavailable():
    root = "/workspace/gradle-root"
    settings = f"{root}/settings.gradle"
    orch = GraphOrchestrator(
        {settings: 'include("app")'},
        realpaths={settings: "/outside/settings.gradle"},
    )

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "gradle_settings_outside_project"


@pytest.mark.parametrize(
    "settings",
    [
        'apply from: "graph.gradle"',
        "apply(from = graphScript)",
        "apply(from: '../outside.gradle')",
    ],
)
def test_gradle_settings_apply_from_fails_closed(settings):
    root = "/workspace/gradle-root"
    orch = GraphOrchestrator({f"{root}/settings.gradle": settings})

    result = verify_forced_candidate_build_graph(
        orch,
        project_root=root,
        candidate_root=root,
        system="gradle",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "gradle_apply_from_unresolved"


def test_public_graph_verifier_rejects_unknown_build_system():
    root = "/workspace/project"

    result = verify_forced_candidate_build_graph(
        GraphOrchestrator(),
        project_root=root,
        candidate_root=root,
        system="bazel",
    )

    assert result.status == "unavailable"
    assert result.reason_code == "unsupported_build_system"


def test_attempt_policy_subproject_cannot_bypass_parent_gradle_settings():
    project = "/workspace/gradle-root"
    candidate = f"{project}/modules/app"
    orch = ManifestOrchestrator()
    orch.manifest.update(
        {
            "survey": {"project_path": project},
            "test_root": candidate,
            "test_system": "gradle",
            "test_islands": [{"root": candidate, "system": "gradle"}],
        }
    )
    orch.files[f"{project}/settings.gradle"] = (
        'include(":app")\n' 'project(":app").projectDir = file("../outside-app")'
    )
    orch.files[f"{candidate}/build.gradle"] = ""

    resolution = resolve_survey_test_candidates(orch)

    assert resolution.status == "unsafe_coordinates"
    requirement = required_test_attempt(
        _ready_state(),
        orch,
        phase="test",
        attempt_id="test-1",
        resolution=resolution,
    )
    assert requirement is not None
    assert requirement.required_action == {
        "tool": "project",
        "params": {"action": "analyze"},
    }


@pytest.mark.parametrize(
    ("survey_system", "marker"),
    [
        ("maven", "build.gradle"),
        ("gradle", "pom.xml"),
    ],
)
def test_attempt_policy_rejects_survey_and_build_facade_backend_mismatch(
    survey_system,
    marker,
):
    project = "/workspace/project"
    candidate = f"{project}/app"
    orch = ManifestOrchestrator()
    orch.manifest.update(
        {
            "survey": {"project_path": project},
            "test_root": candidate,
            "test_system": survey_system,
            "test_islands": [{"root": candidate, "system": survey_system}],
        }
    )
    orch.files = {
        f"{candidate}/{marker}": _pom() if marker == "pom.xml" else "",
    }

    resolution = resolve_survey_test_candidates(orch)

    assert resolution.status == "unsafe_coordinates"
    requirement = required_test_attempt(
        _ready_state(),
        orch,
        phase="test",
        attempt_id="test-1",
        resolution=resolution,
    )
    assert requirement is not None
    assert requirement.required_action["tool"] == "project"


def test_attempt_policy_rejects_pytest_survey_when_pom_wins_marker_order():
    project = "/workspace/project"
    candidate = f"{project}/app"
    orch = ManifestOrchestrator()
    orch.manifest.update(
        {
            "survey": {"project_path": project},
            "test_root": candidate,
            "test_system": "pytest",
            "test_islands": [{"root": candidate, "system": "pytest"}],
        }
    )
    orch.files = {
        f"{candidate}/pom.xml": _pom(),
        f"{candidate}/pyproject.toml": "[project]\nname='example'\n",
    }

    resolution = resolve_survey_test_candidates(orch)

    assert resolution.status == "unsafe_coordinates"


def test_attempt_policy_rejects_unknown_survey_system_without_dispatch():
    orch = ManifestOrchestrator()
    orch.manifest["test_system"] = "bazel"
    orch.manifest["test_islands"] = [
        {
            "root": orch.manifest["test_root"],
            "system": "bazel",
        }
    ]

    resolution = resolve_survey_test_candidates(orch)

    assert resolution.status == "coordinates_missing"
    assert resolution.candidates == ()
    requirement = required_test_attempt(
        _ready_state(),
        orch,
        phase="test",
        attempt_id="test-1",
        resolution=resolution,
    )
    assert requirement is not None
    assert requirement.required_action["tool"] == "project"


def test_attempt_policy_allows_pytest_only_when_python_marker_wins():
    project = "/workspace/project"
    candidate = f"{project}/app"
    orch = ManifestOrchestrator()
    orch.manifest.update(
        {
            "survey": {"project_path": project},
            "test_root": candidate,
            "test_system": "pytest",
            "test_islands": [{"root": candidate, "system": "pytest"}],
        }
    )
    orch.files = {
        f"{candidate}/pyproject.toml": "[project]\nname='example'\n",
    }

    resolution = resolve_survey_test_candidates(orch)

    assert resolution.status == "available"
    assert resolution.candidates[0].system == "pytest"


def test_attempt_policy_refuses_unsafe_graph_and_allows_safe_maven_island():
    project = "/workspace/bigtop"
    island = f"{project}/bigtop-data-generators"
    orch = ManifestOrchestrator()
    orch.manifest.update(
        {
            "test_root": island,
            "test_system": "maven",
            "test_islands": [{"root": island, "system": "maven"}],
        }
    )
    orch.files[f"{island}/pom.xml"] = _pom("../outside")

    unsafe = resolve_survey_test_candidates(orch)

    assert unsafe.status == "unsafe_coordinates"
    state = _ready_state()
    requirement = required_test_attempt(
        state,
        orch,
        phase="test",
        attempt_id="test-1",
    )
    assert requirement is not None
    assert requirement.required_action == {
        "tool": "project",
        "params": {"action": "analyze"},
    }
    state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "project",
        ToolResult.completed_failure(
            output="survey still contains an unsafe build graph",
            error="unsafe module graph",
        ),
        params={"action": "analyze"},
        source_phase="test",
        source_attempt_id="test-1",
    )
    assert (
        required_test_attempt(
            state,
            orch,
            phase="test",
            attempt_id="test-1",
            resolution=unsafe,
        )
        is None
    )

    orch.files[f"{island}/pom.xml"] = _pom("generator-runtime")
    orch.files[f"{island}/generator-runtime/pom.xml"] = _pom()

    safe = resolve_survey_test_candidates(orch)
    requirement = required_test_attempt(
        _ready_state(),
        orch,
        phase="test",
        attempt_id="test-1",
        resolution=safe,
    )

    assert safe.status == "available"
    assert requirement is not None
    assert requirement.required_action["params"]["working_directory"] == island
