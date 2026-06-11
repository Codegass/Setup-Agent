# tests/test_stage1_review_fixes.py
"""Regression tests for the stage-1 tool-consolidation review findings.

Each section reproduces one confirmed P0/P1 finding:
1. bash version-probe exemption must not swallow compound/piped long builds.
2. ProjectTool must accept its documented parameters through safe_execute.
3. Self-healing / state tracking re-keyed to the new tool surface
   (project/build) instead of the retired legacy names.
4. Legacy maven alias must map common lifecycle phases onto valid build verbs.
5. build() without working_directory must heal to the real project directory.
6. search(target='job:<id>') polling is prescribed, never repetition-blocked,
   and detached handoffs carry the promised job ref.
7. Tool recovery routes build/project failures to the maven/gradle/clone
   strategies via the facades' delegate tools.
"""

from types import SimpleNamespace

import pytest

from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.tool_parameters import ToolParameterNormalizer
from sag.agent.tool_recovery import ToolRecoveryHandler
from sag.tools.base import BaseTool, ToolResult
from sag.tools.bash import BashTool
from sag.tools.build.build_tool import BuildTool
from sag.tools.build_utils import detached_handoff_tool_result
from sag.tools.project_tool import ProjectTool


class RecorderTool(BaseTool):
    """Delegate stand-in: records calls, returns queued results (success last)."""

    def __init__(self, name="recorder", results=None):
        super().__init__(name, f"{name} test tool")
        self._parameter_schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": True,
        }
        self.results = list(results or [])
        self.calls = []

    def execute(self, **kwargs) -> ToolResult:
        self.calls.append(dict(kwargs))
        if self.results:
            return self.results.pop(0)
        return ToolResult(success=True, output=f"{self.name} ok")


def _bare_orchestrator(recent=None):
    return ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=recent or [],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )


# --- finding: version-probe exemption mis-routes compound/piped builds -------


@pytest.mark.parametrize(
    "command",
    [
        "mvn --version && mvn clean install",
        "java -version && mvn clean install -DskipTests",
        "mvn clean install 2>&1 | grep -v WARNING",
        "mvn test 2>&1 | grep -v WARNING",
        "pip install -v requests",
    ],
)
def test_compound_or_piped_builds_with_probe_flags_still_dispatch(command):
    tool = BashTool(SimpleNamespace())
    assert tool._is_long_running_command(command) is True


@pytest.mark.parametrize(
    "command",
    ["mvn --version", "./mvnw -v", "./gradlew --version", "mvn -h", "java -version"],
)
def test_bare_version_probes_remain_quick(command):
    tool = BashTool(SimpleNamespace())
    assert tool._is_long_running_command(command) is False


# --- finding (P0): ProjectTool rejects every documented parameter ------------


def _project_tool():
    setup = RecorderTool("setup")
    analyzer = RecorderTool("analyzer")
    system = RecorderTool("system")
    env = RecorderTool("env")
    tool = ProjectTool(
        setup_tool=setup, analyzer_tool=analyzer, system_tool=system, env_tool=env
    )
    return tool, setup, analyzer, system, env


def test_project_safe_execute_accepts_clone_parameters():
    tool, setup, *_ = _project_tool()
    result = tool.safe_execute(action="clone", repo_url="https://github.com/x/y.git")
    assert result.success, f"{result.error_code}: {result.error}"
    assert setup.calls and setup.calls[0]["repository_url"] == "https://github.com/x/y.git"


def test_project_safe_execute_accepts_provision_env_and_analyze_parameters():
    tool, _, analyzer, system, env = _project_tool()

    assert tool.safe_execute(action="provision", java_version="17").success
    assert system.calls[0]["java_version"] == "17"

    assert tool.safe_execute(action="analyze", project_path="/workspace/p").success
    assert analyzer.calls[0]["project_path"] == "/workspace/p"

    assert tool.safe_execute(
        action="env", tool="maven", executable="/opt/maven/bin/mvn"
    ).success
    assert env.calls[0]["executable"] == "/opt/maven/bin/mvn"


def test_project_safe_execute_passes_through_params_taught_elsewhere():
    """target_directory / ref / update_context are taught by prompts and
    recovery guidance; the facade must not reject them."""
    tool, setup, *_ = _project_tool()
    result = tool.safe_execute(
        action="clone",
        repository_url="https://github.com/x/y.git",
        target_directory="/workspace/custom",
        ref="v1.2.3",
    )
    assert result.success, f"{result.error_code}: {result.error}"
    assert setup.calls[0]["target_directory"] == "/workspace/custom"
    assert setup.calls[0]["ref"] == "v1.2.3"


def test_project_safe_execute_still_requires_action():
    tool, *_ = _project_tool()
    result = tool.safe_execute(repo_url="https://github.com/x/y.git")
    assert result.success is False
    assert result.error_code == "MISSING_PARAMETERS"


# --- finding: self-healing keyed to legacy names (normalizer) ----------------


def _normalizer(tools, successful_states=None, repository_url=None, repository_ref=None):
    return ToolParameterNormalizer(
        tools=tools,
        successful_states=successful_states or {},
        repository_url=repository_url,
        repository_ref=repository_ref,
    )


def test_project_clone_injects_repository_url_and_ref_from_state():
    tool, *_ = _project_tool()
    normalizer = _normalizer(
        {"project": tool},
        successful_states={"cloned_repos": set()},
        repository_url="https://example.test/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
    )

    params = normalizer.validate_and_fix("project", {"action": "clone"}, [])

    assert params["repository_url"] == "https://example.test/repo.git"
    assert params["ref"] == "rel/commons-cli-1.11.0"


def test_project_clone_duplicate_guard_switches_to_analyze():
    tool, *_ = _project_tool()
    url = "https://example.test/repo.git"
    normalizer = _normalizer(
        {"project": tool},
        successful_states={"cloned_repos": {url}},
        repository_url=url,
    )

    params = normalizer.validate_and_fix(
        "project", {"action": "clone", "repo_url": url}, []
    )

    assert params["action"] == "analyze"


def test_build_injects_known_working_directory_from_state():
    build = BuildTool(None)
    normalizer = _normalizer(
        {"build": build},
        successful_states={"working_directory": "/workspace/app"},
    )

    params = normalizer.validate_and_fix("build", {"action": "test"}, [])

    assert params["working_directory"] == "/workspace/app"


def test_build_infers_working_directory_from_repository_url():
    build = BuildTool(None)
    normalizer = _normalizer(
        {"build": build},
        repository_url="https://github.com/apache/commons-cli.git",
    )

    params = normalizer.validate_and_fix("build", {"action": "compile"}, [])

    assert params["working_directory"] == "/workspace/commons-cli"


# --- finding: state tracking keyed to legacy names (react engine) ------------


def _engine():
    engine = ReActEngine.__new__(ReActEngine)
    engine.successful_states = {"cloned_repos": set()}
    return engine


def test_update_successful_states_records_project_clone():
    engine = _engine()
    engine._update_successful_states(
        "project",
        {"action": "clone", "repo_url": "https://github.com/x/sample.git"},
        ToolResult(success=True, output="cloned"),
    )
    assert "https://github.com/x/sample.git" in engine.successful_states["cloned_repos"]
    assert engine.successful_states["working_directory"] == "/workspace/sample"


def test_update_successful_states_records_build_success_directory():
    engine = _engine()
    engine._update_successful_states(
        "build",
        {"action": "test", "working_directory": "/workspace/app"},
        ToolResult(success=True, output="BUILD SUCCESSFUL in 2m"),
    )
    assert engine.successful_states["working_directory"] == "/workspace/app"
    assert engine.successful_states["maven_success"] is True


# --- finding: legacy maven alias rejects common invocations -------------------


@pytest.mark.parametrize(
    "command, expected_action",
    [
        ("clean install", "package"),
        ("install", "package"),
        ("verify", "package"),
        ("clean test", "test"),
        ("clean compile", "compile"),
        ("test", "test"),
        ("package", "package"),
        ("dependency:resolve", "deps"),
    ],
)
def test_legacy_maven_alias_maps_lifecycle_phases(command, expected_action):
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    name, params = normalizer.resolve_legacy_alias("maven", {"command": command})

    assert name == "build"
    assert params["action"] == expected_action


def test_legacy_maven_alias_falls_back_to_compile_with_args():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    name, params = normalizer.resolve_legacy_alias(
        "maven", {"command": "org.foo:plugin:goal"}
    )

    assert name == "build"
    assert params["action"] == "compile"
    assert params["args"] == "org.foo:plugin:goal"


def test_legacy_maven_alias_carries_properties_into_args():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    name, params = normalizer.resolve_legacy_alias(
        "maven", {"command": "test", "properties": "-DskipITs=true"}
    )

    assert name == "build"
    assert params["action"] == "test"
    assert "-DskipITs=true" in params["args"]


def test_maven_failure_suggestions_use_valid_build_actions():
    from sag.tools.maven_tool import MavenTool

    assert MavenTool._suggested_build_action("dependency:resolve") == "deps"
    assert MavenTool._suggested_build_action("install") == "package"
    assert MavenTool._suggested_build_action("test") == "test"
    assert MavenTool._suggested_build_action("org.foo:plugin:goal") == "compile"


# --- finding: build() without working_directory in /workspace/<repo> layout --


class ProjectLayoutOrchestrator:
    """Markers exist only under /workspace/<project_name>."""

    def __init__(self, project_name, marker="pom.xml"):
        self.project_name = project_name
        self.marker = marker
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if f"/workspace/{self.project_name}/{self.marker}" in command:
            return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


def test_build_detection_falls_back_to_project_directory():
    maven = RecorderTool("maven")
    tool = BuildTool(
        ProjectLayoutOrchestrator("sample"), maven_tool=maven
    )

    result = tool.execute(action="compile")

    assert result.success, f"verdict={result.verdict}: {result.output}"
    assert maven.calls and maven.calls[0]["working_directory"] == "/workspace/sample"


# --- finding: search(target='job:<id>') polling repetition-blocked -----------


def test_search_job_polling_is_exempt_from_repetition_detection():
    poll_signature = (
        "search:[('max_results', 50), ('pattern', '.'), ('target', 'job:abc123')]"
    )
    recent = [
        {"signature": poll_signature, "success": True, "timestamp": f"ts-{i}"}
        for i in range(9)
    ]
    orchestrator = _bare_orchestrator(recent=recent)

    assert orchestrator._get_repetition_level(poll_signature) == 0
    other = "search:[('target', 'output_5b9a')]"
    assert orchestrator._get_repetition_level(other) == 0


def test_detached_handoff_carries_job_ref():
    result = detached_handoff_tool_result(
        "build",
        "mvn clean install",
        {
            "output": "still running",
            "dispatch": {
                "job_id": "abc123",
                "pid": 42,
                "log_path": "/tmp/sag_jobs/abc123.log",
                "exit_code_path": "/tmp/sag_jobs/abc123.log.exit",
                "soft_timeout": 900,
            },
        },
    )
    assert "job:abc123" in result.refs


# --- finding: recovery dispatch keyed on retired tool names -------------------


def _recovery_handler(tools, **overrides):
    guidance = overrides.pop("guidance", [])
    return ToolRecoveryHandler(
        tools=tools,
        context_manager=overrides.pop("context_manager", None),
        successful_states=overrides.pop("successful_states", {}),
        repository_url=overrides.pop("repository_url", None),
        repository_ref=overrides.pop("repository_ref", None),
        add_system_guidance=lambda message, priority=5: guidance.append(
            (message, priority)
        ),
    )


def test_build_failure_routes_to_maven_java_version_recovery():
    maven = RecorderTool("maven", results=[ToolResult(success=True, output="build ok")])
    system = RecorderTool(
        "system",
        results=[
            ToolResult(success=False, output="", error="missing"),
            ToolResult(success=True, output="installed"),
        ],
    )
    build = BuildTool(None, maven_tool=maven)
    project = ProjectTool(system_tool=system)
    handler = _recovery_handler({"build": build, "project": project})

    failed = ToolResult(
        success=False,
        output="",
        error="Java 17 is required",
        error_code="JAVA_VERSION_MISMATCH",
        facts={"system": "maven", "action": "test"},
        metadata={"analysis": {"java_version_error": {"required": "17", "current": "11"}}},
    )

    decision = handler.recover(
        "build", {"action": "test", "working_directory": "/workspace/app"}, failed
    )

    assert decision.should_recover is True
    assert decision.strategy == "maven_java_version"
    assert system.calls == [
        {"action": "verify_java", "java_version": "17"},
        {"action": "install_java", "java_version": "17"},
    ]
    assert maven.calls == [
        {"command": "test", "working_directory": "/workspace/app"}
    ]
    assert decision.replacement_result.success is True


def test_build_failure_routes_to_gradle_compile_before_test():
    gradle = RecorderTool("gradle", results=[ToolResult(success=True, output="compiled")])
    build = BuildTool(None, gradle_tool=gradle)
    handler = _recovery_handler({"build": build})

    failed = ToolResult(
        success=False,
        output="",
        error="Compilation failure before tests",
        error_code="BUILD_FAILED",
        facts={"system": "gradle", "action": "test"},
    )

    decision = handler.recover(
        "build", {"action": "test", "working_directory": "/workspace/app"}, failed
    )

    assert decision.should_recover is True
    assert decision.strategy == "gradle_compile_before_test"
    assert gradle.calls == [
        {"tasks": "compileJava", "working_directory": "/workspace/app"}
    ]


def test_build_failure_routes_to_maven_known_working_directory():
    maven = RecorderTool("maven", results=[ToolResult(success=True, output="build ok")])
    build = BuildTool(None, maven_tool=maven)
    handler = _recovery_handler(
        {"build": build},
        successful_states={"working_directory": "/workspace/app"},
    )

    failed = ToolResult(
        success=False,
        output="",
        error="pom.xml not found: no such file",
        error_code="MISSING_PROJECT",
        facts={"system": "maven", "action": "test"},
    )

    decision = handler.recover("build", {"action": "test"}, failed)

    assert decision.should_recover is True
    assert decision.strategy == "maven_known_working_directory"
    assert maven.calls == [
        {"command": "test", "working_directory": "/workspace/app"}
    ]


def test_project_clone_failure_recovers_with_injected_repository_url():
    setup = RecorderTool("setup", results=[ToolResult(success=True, output="cloned")])
    project = ProjectTool(setup_tool=setup)
    handler = _recovery_handler(
        {"project": project},
        repository_url="https://example.com/repo.git",
        repository_ref="rel/1.2.3",
    )

    failed = ToolResult(
        success=False, output="", error="repository_url is required"
    )

    decision = handler.recover("project", {"action": "clone"}, failed)

    assert decision.should_recover is True
    assert decision.strategy == "project_setup_repository_url"
    assert setup.calls == [
        {
            "action": "clone",
            "repository_url": "https://example.com/repo.git",
            "ref": "rel/1.2.3",
        }
    ]
