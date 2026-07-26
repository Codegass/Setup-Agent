# tests/test_semantic_action_conservation.py
"""P0-C — semantic action conservation across the build facade (Plan 5 Stage D).

LIVE EVIDENCE (bigtop, 2026-07-26 three-project ground-truth review):

  * bigpetstore-spark's sources all live under src/main/scala. The gradle
    backend mapped EVERY compile to `compileJava`, gradle answered `NO-SOURCE`,
    and the harness scored a green compile over a module it never compiled.
  * a naked `mvn install` ran environment-dependent tests DURING the build
    phase and manufactured a failure that belongs to another environment.
  * the model asked for `compile` and the facade executed `install`; nothing in
    the observation it read said so.

Four contracts, one per section below: language-aware gradle compile, NO-SOURCE
cannot close a source-bearing compile, packaging never runs tests, and every
SEMANTIC mutation is narrated before the model reasons about the result.
"""

import json
import shlex

from sag.tools.base import ToolResult
from sag.tools.build.backends import GradleBackend, MavenBackend
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.gradle_tool import GradleTool

ISLAND = "/workspace/bigtop/bigpetstore-spark"


# --------------------------------------------------------------------------- #
# Test doubles: a container that answers ONLY file reads and directory probes,
# because directory existence is the only fact these contracts may consult.
# --------------------------------------------------------------------------- #
class ProbeOrchestrator:
    """Answers `cat` reads from `files` and `test -d` loops from `directories`."""

    def __init__(self, files=None, directories=(), markers=(), project_name="proj"):
        self.files = dict(files or {})
        self.directories = {d.rstrip("/") for d in directories}
        self.markers = set(markers)
        self.project_name = project_name
        self.commands = []

    def read_file(self, path):
        if path in self.files:
            return {"success": True, "exit_code": 0, "content": self.files[path]}
        return {"success": False, "exit_code": 1, "content": ""}

    def _dir_probe(self, command):
        quoted = command.split("for d in ", 1)[1].split("; do", 1)[0]
        hits = [p for p in shlex.split(quoted) if p.rstrip("/") in self.directories]
        return {"success": True, "exit_code": 0, "output": "\n".join(hits)}

    def execute_command(self, command, workdir=None, timeout=None, **_):
        self.commands.append(command)
        if command.startswith("for d in ") and "test -d" in command:
            return self._dir_probe(command)
        if command.startswith("cat "):
            path = shlex.split(command)[-1]
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in command:  # build-marker probes
            tokens = shlex.split(command)
            try:
                path = tokens[tokens.index("-f") + 1]
            except (ValueError, IndexError):
                path = ""
            return {
                "success": True,
                "exit_code": 0,
                "output": "exists" if path in self.markers else "missing",
            }
        return {"success": True, "exit_code": 0, "output": ""}


class RecordingBackendTool:
    """Stands in for MavenTool/GradleTool: records kwargs, returns one result."""

    def __init__(self, orchestrator=None, result=None):
        self.orchestrator = orchestrator
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESSFUL")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class GradleLogOrchestrator:
    """Replays one gradle log for GradleTool's monitored dispatch path."""

    def __init__(self, output, exit_code=0):
        self.monitored = {"output": output, "exit_code": exit_code}
        self.project_name = None
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append(command)
        if command in ("which gradle", "command -v gradle"):
            return {"success": True, "output": "/usr/bin/gradle", "exit_code": 0}
        if command.startswith("test -x /usr/bin/gradle"):
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if command == "/usr/bin/gradle -version":
            return {"success": True, "output": "Gradle 8.5", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}

    def execute_command_with_monitoring(self, command, **kwargs):
        return dict(self.monitored)


def _gradle_compile_call(files=None, directories=()):
    tool = RecordingBackendTool(
        orchestrator=ProbeOrchestrator(files=files, directories=directories)
    )
    GradleBackend(tool).run("compile", None, ISLAND, None)
    return tool.calls[0], tool.orchestrator


def _maven_call(verb, args=None):
    tool = RecordingBackendTool()
    MavenBackend(tool).run(verb, args, "/workspace/p", None)
    return tool.calls[0]


def _gradle_call(verb, args=None, files=None):
    tool = RecordingBackendTool(orchestrator=ProbeOrchestrator(files=files))
    GradleBackend(tool).run(verb, args, ISLAND, None)
    return tool.calls[0]


# --------------------------------------------------------------------------- #
# 1) Language-aware gradle compile: the executed tasks are the UNION of the
#    compile tasks the project's own source directories require.
# --------------------------------------------------------------------------- #
def test_scala_source_dir_adds_compile_scala_next_to_compile_java():
    call, _orch = _gradle_compile_call(directories=[f"{ISLAND}/src/main/scala"])
    assert call["tasks"] == "compileJava compileScala"
    assert call["_compile_source_languages"] == ["scala"]


def test_kotlin_source_dir_adds_compile_kotlin():
    call, _orch = _gradle_compile_call(directories=[f"{ISLAND}/src/main/kotlin"])
    assert call["tasks"] == "compileJava compileKotlin"
    assert call["_compile_source_languages"] == ["kotlin"]


def test_groovy_source_dir_adds_compile_groovy():
    call, _orch = _gradle_compile_call(directories=[f"{ISLAND}/src/main/groovy"])
    assert call["tasks"] == "compileJava compileGroovy"
    assert call["_compile_source_languages"] == ["groovy"]


def test_mixed_language_dirs_run_the_whole_union():
    call, _orch = _gradle_compile_call(
        directories=[
            f"{ISLAND}/src/main/scala",
            f"{ISLAND}/src/main/groovy",
            f"{ISLAND}/src/main/java",
        ]
    )
    assert call["tasks"] == "compileJava compileGroovy compileScala"


def test_java_only_project_compiles_java_alone():
    call, _orch = _gradle_compile_call(directories=[f"{ISLAND}/src/main/java"])
    assert call["tasks"] == "compileJava"
    assert "_compile_source_languages" not in call


def test_absent_source_dirs_never_invent_a_compile_task():
    call, _orch = _gradle_compile_call()
    assert call["tasks"] == "compileJava"


def test_subproject_scala_sources_are_probed_when_a_settings_file_exists():
    call, _orch = _gradle_compile_call(
        files={f"{ISLAND}/settings.gradle": "include ':spark', ':queue'\n"},
        directories=[f"{ISLAND}/spark/src/main/scala"],
    )
    assert call["tasks"] == "compileJava compileScala"


def test_kotlin_dsl_settings_include_form_is_read():
    call, _orch = _gradle_compile_call(
        files={f"{ISLAND}/settings.gradle.kts": 'include(":spark")\n'},
        directories=[f"{ISLAND}/spark/src/main/kotlin"],
    )
    assert call["tasks"] == "compileJava compileKotlin"


def test_nested_include_coordinate_maps_to_a_nested_directory():
    call, _orch = _gradle_compile_call(
        files={f"{ISLAND}/settings.gradle": "include ':group:spark'\n"},
        directories=[f"{ISLAND}/group/spark/src/main/scala"],
    )
    assert call["tasks"] == "compileJava compileScala"


def test_subproject_dirs_are_not_probed_without_a_settings_file():
    call, _orch = _gradle_compile_call(directories=[f"{ISLAND}/spark/src/main/scala"])
    assert call["tasks"] == "compileJava"


def test_the_probe_consults_directory_existence_only():
    _call, orch = _gradle_compile_call(directories=[f"{ISLAND}/src/main/scala"])
    assert orch.commands, "the language probe must actually ask the container"
    assert all(
        command.startswith("cat ") or "test -d" in command for command in orch.commands
    ), orch.commands


# --------------------------------------------------------------------------- #
# 2) NO-SOURCE cannot close a source-bearing compile.
# --------------------------------------------------------------------------- #
NO_SOURCE_MISMATCH = (
    "scala sources present; executed compile tasks all reported NO-SOURCE "
    "— the compile did not cover the sources"
)


def _gradle_tool_run(log, languages, exit_code=0, tasks="compileJava"):
    orchestrator = GradleLogOrchestrator(log, exit_code=exit_code)
    return GradleTool(orchestrator).execute(
        tasks=tasks,
        working_directory="/workspace/p",
        use_wrapper=False,
        _compile_source_languages=languages,
    )


def test_all_no_source_compile_over_scala_sources_is_not_a_success():
    result = _gradle_tool_run(
        "> Task :compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n",
        ["scala"],
    )
    assert result.succeeded is False
    assert result.error == NO_SOURCE_MISMATCH
    assert result.metadata["analysis"]["build_successful"] is False


def test_the_no_source_mismatch_is_recorded_as_a_conflict():
    result = _gradle_tool_run(
        "> Task :compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n",
        ["scala"],
    )
    assert result.conflicts == ["gradle_success_vs_compile_no_source"]


def test_subproject_qualified_compile_tasks_are_judged_too():
    result = _gradle_tool_run(
        "> Task :bigpetstore-spark:compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n",
        ["scala"],
    )
    assert result.succeeded is False
    assert result.error == NO_SOURCE_MISMATCH


def test_every_probed_language_is_named_in_the_mismatch():
    result = _gradle_tool_run(
        "> Task :compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n",
        ["kotlin", "groovy"],
    )
    assert result.error == (
        "groovy, kotlin sources present; executed compile tasks all reported "
        "NO-SOURCE — the compile did not cover the sources"
    )


def test_one_compile_task_that_did_work_keeps_the_success():
    result = _gradle_tool_run(
        "\n".join(
            [
                "> Task :compileJava NO-SOURCE",
                "> Task :compileScala",
                "BUILD SUCCESSFUL in 12s",
            ]
        ),
        ["scala"],
        tasks="compileJava compileScala",
    )
    assert result.succeeded is True


def test_no_source_without_a_probed_language_never_manufactures_a_mismatch():
    result = _gradle_tool_run(
        "> Task :compileJava NO-SOURCE\nBUILD SUCCESSFUL in 3s\n",
        None,
    )
    assert result.succeeded is True
    assert "compile_source_mismatch" not in result.metadata["analysis"]


def test_no_source_on_a_non_compile_task_is_not_a_compile_mismatch():
    result = _gradle_tool_run(
        "\n".join(
            [
                "> Task :processResources NO-SOURCE",
                "> Task :compileScala",
                "BUILD SUCCESSFUL in 9s",
            ]
        ),
        ["scala"],
        tasks="compileJava compileScala",
    )
    assert result.succeeded is True


# --------------------------------------------------------------------------- #
# 3) install/package never run tests — the `test` verb is the only test owner.
# --------------------------------------------------------------------------- #
def test_maven_install_argv_skips_tests():
    assert _maven_call("install")["extra_args"] == "-DskipTests"


def test_maven_package_argv_skips_tests():
    assert _maven_call("package")["extra_args"] == "-DskipTests"


def test_maven_install_keeps_the_callers_args_and_appends_the_skip():
    assert _maven_call("install", "-Pdist")["extra_args"] == "-Pdist -DskipTests"


def test_maven_install_never_doubles_an_explicit_caller_skip():
    extra = _maven_call("install", "-DskipTests")["extra_args"]
    assert extra == "-DskipTests"
    assert extra.count("-DskipTests") == 1


def test_maven_install_respects_a_caller_test_skip_property():
    extra = _maven_call("install", "-Dmaven.test.skip=true")["extra_args"]
    assert extra == "-Dmaven.test.skip=true"


def test_maven_install_respects_an_explicit_caller_test_selection():
    # A caller naming tests has already decided the test policy; overriding it
    # with a skip would silently delete the selection.
    extra = _maven_call("install", "-Dtest=ShellTest")["extra_args"]
    assert extra == "-Dtest=ShellTest"


def test_maven_compile_and_test_verbs_carry_no_skip():
    for verb in ("deps", "compile", "test"):
        assert "skipTests" not in str(_maven_call(verb).get("extra_args") or ""), verb


def test_gradle_publish_argv_excludes_the_test_task():
    call = _gradle_call(
        "install",
        files={f"{ISLAND}/build.gradle": "apply plugin: 'maven-publish'\n"},
    )
    assert call["tasks"] == "publishToMavenLocal"
    assert call["gradle_args"] == "-x test"


def test_gradle_assemble_paths_exclude_the_test_task():
    for verb in ("package", "install"):
        call = _gradle_call(verb, files={f"{ISLAND}/build.gradle": "apply plugin: 'java'\n"})
        assert call["tasks"] == "assemble", verb
        assert call["gradle_args"] == "-x test", verb


def test_gradle_packaging_keeps_the_callers_args_and_appends_the_exclude():
    call = _gradle_call("package", args="--info")
    assert call["gradle_args"] == "--info -x test"


def test_gradle_never_doubles_an_explicit_caller_exclude():
    call = _gradle_call("package", args="-x test")
    assert call["gradle_args"] == "-x test"


def test_gradle_compile_and_test_verbs_keep_the_test_task():
    for verb in ("deps", "compile", "test"):
        assert "-x test" not in str(_gradle_call(verb).get("gradle_args") or ""), verb


# --------------------------------------------------------------------------- #
# 4) Visible semantic delta: the mutation leads the observation.
# --------------------------------------------------------------------------- #
def _island_manifest(root, island, system, goal):
    return {
        "survey": {"project_path": root},
        "root_shape": "pathological_aggregator",
        "build_islands": [{"root": island, "system": system, "goal": goal}],
    }


def test_delta_line_leads_the_output_on_a_compile_to_install_promotion():
    root = "/workspace/bigtop"
    island = f"{root}/bigtop-data-generators"
    orchestrator = ProbeOrchestrator(
        markers=[f"{island}/build.gradle"],
        files={
            REQUIREMENTS_PATH: json.dumps(
                _island_manifest(root, island, "gradle", "publishToMavenLocal")
            ),
            f"{island}/build.gradle": "apply plugin: 'maven-publish'\n",
        },
    )
    gradle = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, gradle_tool=gradle).execute(
        action="compile", working_directory=island
    )

    first = (result.output or "").splitlines()[0]
    assert first.startswith("[build] requested 'compile' -> executing ")
    assert "publishToMavenLocal -x test" in first
    assert "install" in first
    # The island provenance line survives underneath it.
    assert "[island]" in result.output


def test_delta_line_leads_the_output_when_a_skip_flag_is_added():
    orchestrator = ProbeOrchestrator(markers=["/workspace/p/pom.xml"])
    maven = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, maven_tool=maven).execute(
        action="install", working_directory="/workspace/p"
    )

    first = (result.output or "").splitlines()[0]
    assert first.startswith("[build] requested 'install' -> executing 'install -DskipTests' (")
    assert first.endswith(")")


def test_gradle_install_that_cannot_publish_narrates_the_substitution():
    orchestrator = ProbeOrchestrator(
        markers=["/workspace/p/build.gradle"],
        files={"/workspace/p/build.gradle": "apply plugin: 'java'\n"},
    )
    gradle = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, gradle_tool=gradle).execute(
        action="install", working_directory="/workspace/p"
    )

    first = (result.output or "").splitlines()[0]
    assert first.startswith("[build] requested 'install' -> executing 'assemble -x test' (")
    assert "maven-publish" in first


def test_plain_compile_to_compile_java_gets_no_delta_line():
    orchestrator = ProbeOrchestrator(markers=["/workspace/p/build.gradle"])
    gradle = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, gradle_tool=gradle).execute(
        action="compile", working_directory="/workspace/p"
    )

    assert gradle.calls[0]["tasks"] == "compileJava"
    assert "[build] requested" not in (result.output or "")


def test_language_aware_task_union_is_translation_not_a_semantic_delta():
    # The compile verb still compiles and only compiles: naming compileScala
    # alongside compileJava is a task-name translation, not a lifecycle change,
    # so it stays out of the delta narration (the executed task list is already
    # in the gradle result).
    orchestrator = ProbeOrchestrator(
        markers=["/workspace/p/build.gradle"],
        directories=["/workspace/p/src/main/scala"],
    )
    gradle = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, gradle_tool=gradle).execute(
        action="compile", working_directory="/workspace/p"
    )

    assert gradle.calls[0]["tasks"] == "compileJava compileScala"
    assert "[build] requested" not in (result.output or "")


def test_maven_compile_verb_gets_no_delta_line():
    orchestrator = ProbeOrchestrator(markers=["/workspace/p/pom.xml"])
    maven = RecordingBackendTool(orchestrator=orchestrator)

    result = BuildTool(orchestrator, maven_tool=maven).execute(
        action="compile", working_directory="/workspace/p"
    )

    assert "[build] requested" not in (result.output or "")


class _ExclusionHistoryOrch:
    """Fake command runner: only answers the command-history grep."""

    def __init__(self, history: str) -> None:
        self.history = history

    def execute_command(self, command: str, **_kwargs):
        if "command_history" in command:
            return {"success": True, "output": self.history, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def _detect_exclusions(history: str) -> list[str]:
    from sag.agent.physical_validator import PhysicalValidator

    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = _ExclusionHistoryOrch(history)
    validator._cache = {}
    validator._cache_timestamps = {}
    validator._execute_command_with_logging = (
        lambda cmd, desc="", **kw: validator.docker_orchestrator.execute_command(cmd)
    )
    return validator._detect_test_exclusions("/workspace/proj")


def test_packaging_skip_flags_are_not_test_exclusions():
    """P0-C: install/package legitimately carry -DskipTests / -x test."""
    exclusions = _detect_exclusions(
        "mvn install -DskipTests\ngradle publishToMavenLocal -x test\n"
    )
    assert "ALL_TESTS_SKIPPED" not in exclusions
    assert "GRADLE_TESTS_EXCLUDED" not in exclusions


def test_skip_flags_on_test_commands_still_flagged():
    exclusions = _detect_exclusions("mvn test -DskipTests\ngradle test -x test\n")
    assert "ALL_TESTS_SKIPPED" in exclusions
    assert "GRADLE_TESTS_EXCLUDED" in exclusions
