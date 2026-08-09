# tests/test_stage1_review_fixes.py
"""Regression tests for the stage-1 tool-consolidation review findings.

Each section reproduces one confirmed P0/P1 finding:
1. bash version-probe exemption must not swallow compound/piped long builds.
2. ProjectTool must accept its documented parameters through safe_execute.
3. Parameter normalization may translate spellings but cannot inject runtime
   state or replace the model's selected action.
4. Legacy Maven aliases map only exactly equivalent lifecycle phases; lossy
   clean/plugin translations fail validation.
5. build() without working_directory uses its public schema default, never a
   state- or repository-derived path.
6. legacy search(target='job:<id>') polling remains mechanically safe, while
   current runs use a controller-owned barrier,
   and detached handoffs carry the promised job ref.
"""

from types import SimpleNamespace

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import add_published_mutable_json, strict_published_evidence

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.tool_parameters import ToolParameterNormalizer
from sag.tools.base import BaseTool, ToolResult
from sag.tools.bash import BashTool
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.build_utils import detached_handoff_tool_result
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
        return ToolResult.completed_success(output=f"{self.name} ok")


def _bare_orchestrator(recent=None):
    return ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=recent or [],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
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
    tool = ProjectTool(setup_tool=setup, analyzer_tool=analyzer, system_tool=system, env_tool=env)
    return tool, setup, analyzer, system, env


def test_project_safe_execute_accepts_clone_parameters():
    tool, setup, *_ = _project_tool()
    result = tool.safe_execute(action="clone", repo_url="https://github.com/x/y.git")
    assert result.succeeded, f"{result.error_code}: {result.error}"
    assert setup.calls and setup.calls[0]["repository_url"] == "https://github.com/x/y.git"


def test_project_safe_execute_accepts_provision_env_and_analyze_parameters():
    tool, _, analyzer, system, env = _project_tool()

    assert tool.safe_execute(action="provision", java_version="17").succeeded
    assert system.calls[0]["java_version"] == "17"

    assert tool.safe_execute(action="analyze", project_path="/workspace/p").succeeded
    assert analyzer.calls[0]["project_path"] == "/workspace/p"

    assert tool.safe_execute(action="env", tool="maven", executable="/opt/maven/bin/mvn").succeeded
    assert env.calls[0]["executable"] == "/opt/maven/bin/mvn"


def test_project_safe_execute_passes_through_params_taught_elsewhere():
    """Parameters declared by the public schema/prompts reach the facade."""
    tool, setup, *_ = _project_tool()
    result = tool.safe_execute(
        action="clone",
        repository_url="https://github.com/x/y.git",
        target_directory="/workspace/custom",
        ref="v1.2.3",
    )
    assert result.succeeded, f"{result.error_code}: {result.error}"
    assert setup.calls[0]["target_directory"] == "/workspace/custom"
    assert setup.calls[0]["ref"] == "v1.2.3"


def test_project_safe_execute_still_requires_action():
    tool, *_ = _project_tool()
    result = tool.safe_execute(repo_url="https://github.com/x/y.git")
    assert result.succeeded is False
    assert result.error_code == "MISSING_PARAMETERS"


# --- finding: self-healing keyed to legacy names (normalizer) ----------------


def _normalizer(tools, successful_states=None, repository_url=None, repository_ref=None):
    return ToolParameterNormalizer(
        tools=tools,
        successful_states=successful_states or {},
        repository_url=repository_url,
        repository_ref=repository_ref,
    )


def test_project_clone_does_not_inject_repository_url_or_ref_from_state():
    tool, *_ = _project_tool()
    normalizer = _normalizer(
        {"project": tool},
        successful_states={"cloned_repos": set()},
        repository_url="https://example.test/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
    )

    params = normalizer.validate_and_fix("project", {"action": "clone"}, [])

    assert params == {"action": "clone"}


def test_project_clone_duplicate_state_does_not_replace_model_action():
    tool, *_ = _project_tool()
    url = "https://example.test/repo.git"
    normalizer = _normalizer(
        {"project": tool},
        successful_states={"cloned_repos": {url}},
        repository_url=url,
    )

    params = normalizer.validate_and_fix("project", {"action": "clone", "repo_url": url}, [])

    assert params == {"action": "clone", "repo_url": url}


def test_build_uses_schema_default_not_known_working_directory_state():
    build = BuildTool(None)
    normalizer = _normalizer(
        {"build": build},
        successful_states={"working_directory": "/workspace/app"},
    )

    params = normalizer.validate_and_fix("build", {"action": "test"}, [])

    assert params == {"action": "test", "working_directory": "/workspace"}


def test_build_does_not_infer_working_directory_from_repository_url():
    build = BuildTool(None)
    normalizer = _normalizer(
        {"build": build},
        repository_url="https://github.com/apache/commons-cli.git",
    )

    params = normalizer.validate_and_fix("build", {"action": "compile"}, [])

    assert params == {"action": "compile", "working_directory": "/workspace"}


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
        ToolResult.completed_success(output="cloned"),
    )
    assert "https://github.com/x/sample.git" in engine.successful_states["cloned_repos"]
    assert engine.successful_states["working_directory"] == "/workspace/sample"


def test_update_successful_states_records_build_success_directory():
    engine = _engine()
    engine._update_successful_states(
        "build",
        {"action": "test", "working_directory": "/workspace/app"},
        ToolResult.completed_success(output="BUILD SUCCESSFUL in 2m"),
    )
    assert engine.successful_states["working_directory"] == "/workspace/app"
    assert engine.successful_states["maven_success"] is True


def test_update_successful_states_does_not_label_gradle_build_as_maven():
    engine = _engine()
    engine.successful_states["maven_success"] = False

    engine._update_successful_states(
        "build",
        {"action": "compile", "working_directory": "/workspace/gradle-app"},
        ToolResult.completed_success(
            output="BUILD SUCCESSFUL",
            facts={
                "system": "gradle",
                "requested_action": "compile",
                "effective_action": "install",
            },
            metadata={"working_directory": "/workspace/gradle-app"},
        ),
    )

    assert engine.successful_states["working_directory"] == "/workspace/gradle-app"
    assert engine.successful_states["gradle_success"] is True
    assert engine.successful_states["maven_success"] is False


# --- finding: legacy maven alias rejects common invocations -------------------


@pytest.mark.parametrize(
    "command, expected_action",
    [
        ("install", "install"),
        ("verify", "test"),
        ("compile", "compile"),
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


@pytest.mark.parametrize("command", ["clean compile", "clean test", "clean install", "clean verify"])
def test_legacy_maven_clean_lifecycle_is_refused_when_clean_cannot_be_preserved(command):
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    with pytest.raises(ValueError, match="invalid value for action"):
        normalizer.validate_and_fix("maven", {"command": command})


def test_legacy_maven_unknown_goal_is_refused_instead_of_becoming_compile():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    with pytest.raises(ValueError, match="invalid value for action"):
        normalizer.validate_and_fix("maven", {"command": "org.foo:plugin:goal"})


def test_legacy_maven_alias_carries_properties_into_args():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    name, params = normalizer.resolve_legacy_alias(
        "maven", {"command": "test", "properties": "-DskipITs=true"}
    )

    assert name == "build"
    assert params["action"] == "test"
    assert "-DskipITs=true" in params["args"]


def test_legacy_maven_canonical_action_wins_over_conflicting_command_alias():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    params = normalizer.validate_and_fix(
        "maven",
        {"action": "install", "command": "test", "working_directory": "/workspace/p"},
    )

    assert params["action"] == "install"


def test_legacy_maven_mapping_refuses_structured_properties_instead_of_stringifying():
    build = BuildTool(None)
    normalizer = _normalizer({"build": build})

    with pytest.raises(ValueError, match="properties"):
        normalizer.validate_and_fix(
            "maven",
            {"command": "test", "properties": {"skipTests": True}},
        )


# --- finding: build() without working_directory in /workspace/<repo> layout --


class ProjectLayoutOrchestrator:
    """Markers exist only under /workspace/<project_name>."""

    def __init__(self, project_name, marker="pom.xml"):
        self.project_name = project_name
        self.marker = marker
        self.commands = []
        self.evidence = None

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if self.evidence is not None and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command)
        if f"/workspace/{self.project_name}/{self.marker}" in command:
            return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


def test_build_detection_does_not_probe_or_dispatch_from_project_name_fallback(
    exact_build_facade_authority,
):
    maven = RecorderTool("maven")
    orchestrator = ProjectLayoutOrchestrator("sample")
    # Routing descends only from a host-published manifest revision now; a
    # bare or absent container file fails closed before marker detection.
    orchestrator.evidence = strict_published_evidence(
        orchestrator,
        run_id="run-stage1-build-detection",
        target_sha="a" * 40,
    )
    add_published_mutable_json(
        orchestrator,
        orchestrator.evidence,
        path=REQUIREMENTS_PATH,
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        payload=complete_build_requirements_v1(),
    )
    tool = BuildTool(orchestrator, maven_tool=maven)

    result = tool.execute(action="compile")

    assert result.operation_outcome.value == "unknown"
    assert result.error_code == "BUILD_SYSTEM_NOT_DETECTED"
    assert result.metadata == {
        "runner_dispatched": False,
        "working_directory": "/workspace",
    }
    assert result.facts["working_directory"] == "/workspace"
    assert maven.calls == []
    assert orchestrator.commands
    assert not any("/workspace/sample/" in command for command in orchestrator.commands)


# --- legacy job polling remains safe behind the controller barrier ----------


def test_search_job_polling_is_exempt_from_repetition_detection():
    poll_signature = "search:[('max_results', 50), ('pattern', '.'), ('target', 'job:abc123')]"
    recent = [
        {
            "signature": poll_signature,
            "invocation_status": "completed",
            "operation_outcome": "success",
            "timestamp": f"ts-{i}",
        }
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


# --- round-4 gate fixes: steer the model to build(), not hand-rolled mvn ----
# NOTE: test_analyzer_test_task_prescribes_build_tool was deleted with the plan
# pipeline (Category-3 analyzer diet, dim a): the analyzer no longer generates
# an execution plan / plan->todo tasks, so there is no plan task text to steer.
# The bash-nudge regressions below still hold.


def test_bash_mvn_failure_reports_boundary_without_selecting_a_call():
    from sag.tools.bash import BashTool

    tool = BashTool.__new__(BashTool)
    suggestions = tool._generate_error_suggestions(
        {"error_type": "general"}, "cd /workspace/p && ./bin/mvn -q test", 127
    )
    assert any("public build affordance" in s for s in suggestions), suggestions
    assert not any("build(action=" in s for s in suggestions), suggestions


def test_bash_non_build_failure_has_no_build_nudge():
    from sag.tools.bash import BashTool

    tool = BashTool.__new__(BashTool)
    suggestions = tool._generate_error_suggestions(
        {"error_type": "general"}, "cat /workspace/missing.txt", 1
    )
    assert not any("build(action=" in s for s in suggestions), suggestions
