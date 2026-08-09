import pytest

from sag.agent.action_intents import action_fingerprint
from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator, format_tool_result
from sag.agent.tool_parameters import ToolParameterNormalizer
from sag.tools.base import BaseTool, ToolResult
from sag.tools.project_tool import ProjectTool


class BashLikeTool(BaseTool):
    def __init__(self):
        super().__init__("bash", "Bash-like test tool")
        # Exercise defaults explicitly declared by the public schema.
        self._parameter_schema["properties"]["timeout"]["default"] = 60

    def execute(self, command: str, timeout: int, working_directory: str = "") -> ToolResult:
        return ToolResult.completed_success(
            output=f"{working_directory}: {command} ({timeout})",
            metadata={
                "command": command,
                "timeout": timeout,
                "working_directory": working_directory,
            },
        )


class ProjectSetupLikeTool(BaseTool):
    def __init__(self):
        super().__init__("project_setup", "Project setup test tool")
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "repository_url": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": [],
        }

    def execute(self, **params) -> ToolResult:
        return ToolResult.completed_success(output=str(params))


class SchemaLikeTool(BaseTool):
    def __init__(self, name, properties, required=None):
        super().__init__(name, f"{name} schema test tool")
        self._parameter_schema = {
            "type": "object",
            "properties": properties,
            "required": required or [],
        }

    def execute(self, **params) -> ToolResult:
        return ToolResult.completed_success(output=str(params))


def _orchestrator(**overrides):
    events = overrides.pop("events", [])
    tracking_calls = overrides.pop("tracking_calls", [])
    state_updates = overrides.pop("state_updates", [])

    orchestrator = ToolOrchestrator(
        tools=overrides.pop("tools", {"bash": BashLikeTool()}),
        context_manager=None,
        recent_tool_executions=[],
        successful_states=overrides.pop(
            "successful_states",
            {
                "working_directory": "/workspace/project",
                "maven_success": False,
                "cloned_repos": set(),
            },
        ),
        repository_url=overrides.pop("repository_url", None),
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
        **overrides,
    )
    return orchestrator, events, tracking_calls, state_updates


def test_parameter_alias_and_schema_defaults_are_recorded_without_state_injection():
    orchestrator, events, tracking_calls, state_updates = _orchestrator()

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "echo hi"}))

    assert execution.status == "success"
    assert execution.executed_params == {
        "command": "echo hi",
        "timeout": 60,
        "working_directory": "",
    }
    assert execution.call.validated_params == execution.executed_params
    assert [event.event_type for event in events] == [
        "tool_start",
        "tool_parameters_fixed",
        "tool_result",
    ]

    fixed_event = events[1]
    assert fixed_event.metadata["raw_params"] == {"cmd": "echo hi"}
    assert fixed_event.metadata["validated_params"] == execution.executed_params
    assert fixed_event.metadata["parameter_fixes"] == execution.parameter_fixes
    assert fixed_event.metadata["params_changed"] is True

    fixes = {(fix.source, fix.field, fix.before, fix.after) for fix in execution.parameter_fixes}
    assert ("schema_alias", "command", None, "echo hi") in fixes
    assert ("default", "timeout", None, 60) in fixes
    assert not any(fix.source == "state_injection" for fix in execution.parameter_fixes)
    assert tracking_calls == [
        (
            "bash:[('command', 'echo hi'), ('timeout', 60), ('working_directory', '')]",
            execution.result,
        )
    ]
    assert len(state_updates) == 1


@pytest.mark.parametrize("tool_name", ["python", "maven", "gradle"])
def test_direct_build_backend_cwd_alias_normalizes_to_working_directory(tool_name):
    properties = {
        "working_directory": {"type": "string"},
    }
    if tool_name == "maven":
        properties["command"] = {"type": "string"}
    tool = SchemaLikeTool(
        tool_name,
        properties,
    )
    orchestrator, _events, _tracking, _updates = _orchestrator(
        tools={tool_name: tool},
    )

    execution = orchestrator.execute(
        ToolCall(
            name=tool_name,
            raw_params={"cwd": "/workspace/project"},
        )
    )

    assert execution.status == "success"
    expected = {
        "working_directory": "/workspace/project",
    }
    assert execution.executed_params == expected
    assert any(
        fix.source == "schema_alias"
        and fix.field == "working_directory"
        and fix.after == "/workspace/project"
        for fix in execution.parameter_fixes
    )


def test_legacy_maven_goals_alias_executes_test_and_records_compatibility_fix():
    build = SchemaLikeTool(
        "build",
        {
            "action": {
                "type": "string",
                "enum": ["deps", "compile", "test", "package", "install"],
            },
            "working_directory": {"type": "string"},
        },
        required=["action"],
    )
    orchestrator, events, _tracking, _updates = _orchestrator(tools={"build": build})

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"goals": "test", "working_directory": "/workspace/project"},
        )
    )

    assert execution.status == "success"
    assert execution.call.name == "build"
    assert execution.executed_params == {
        "action": "test",
        "working_directory": "/workspace/project",
    }
    assert any(
        fix.source == "schema_alias"
        and fix.field == "goals"
        and fix.before == "test"
        and fix.after is None
        for fix in execution.parameter_fixes
    )
    assert any(event.event_type == "tool_parameters_fixed" for event in events)


def test_legacy_web_search_preserves_explicit_result_limit():
    search = SchemaLikeTool(
        "search",
        {
            "target": {"type": "string"},
            "pattern": {"type": "string"},
            "max_results": {"type": "integer", "default": 50},
        },
        required=["target"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"search": search},
        successful_states={},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "web_search",
        {"query": "maven reactor docs", "max_results": 3},
    )

    assert params == {"target": "web:maven reactor docs", "max_results": 3}


def test_legacy_output_search_refuses_unrepresentable_preview_semantics():
    search = SchemaLikeTool(
        "search",
        {"target": {"type": "string"}, "pattern": {"type": "string"}},
        required=["target"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"search": search},
        successful_states={},
        repository_url=None,
    )

    with pytest.raises(ValueError, match="no exact search equivalent"):
        normalizer.validate_and_fix(
            "output_search",
            {"action": "preview", "ref_id": "output_abc", "head_lines": 20},
        )


def test_legacy_project_setup_maps_clone_fields_and_canonical_repo_wins():
    project = SchemaLikeTool(
        "project",
        {
            "action": {"type": "string", "enum": ["clone", "provision", "analyze", "env"]},
            "repo_url": {"type": "string"},
            "ref": {"type": "string"},
        },
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={},
        repository_url="https://state.invalid/repo.git",
    )

    params = normalizer.validate_and_fix(
        "project_setup",
        {
            "action": "clone",
            "repo_url": "https://canonical.test/repo.git",
            "repository_url": "https://alias.invalid/repo.git",
            "branch": "release-1",
        },
    )

    assert params == {
        "action": "clone",
        "repo_url": "https://canonical.test/repo.git",
        "ref": "release-1",
    }


def test_legacy_project_analyzer_conflicting_action_is_refused():
    project = SchemaLikeTool(
        "project",
        {"action": {"type": "string", "enum": ["clone", "provision", "analyze", "env"]}},
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={},
        repository_url=None,
    )

    with pytest.raises(ValueError, match="is not analyze"):
        normalizer.validate_and_fix("project_analyzer", {"action": "clone"})


def test_project_env_conditional_default_is_frozen_without_polluting_other_actions():
    project = ProjectTool()
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={},
        repository_url=None,
    )

    env_params = normalizer.validate_and_fix(
        "project",
        {"action": "env", "tool": "maven", "executable": "/opt/mvn"},
    )
    clone_params = normalizer.validate_and_fix(
        "project",
        {"action": "clone", "repo_url": "https://example.test/repo.git"},
    )

    assert env_params["activate"] is True
    assert "activate" not in clone_params


@pytest.mark.parametrize(
    "tool_name, params, message",
    [
        ("system", {"action": "detect_missing"}, "no exact project equivalent"),
        ("system", {"action": "install"}, "only with packages"),
        (
            "env",
            {"action": "register", "tool": "maven", "executable": "/opt/mvn"},
            "only when activate=true",
        ),
        ("env", {"action": "activate", "tool": "maven"}, "no exact project equivalent"),
    ],
)
def test_legacy_project_backends_refuse_non_equivalent_actions(tool_name, params, message):
    project = SchemaLikeTool(
        "project",
        {
            "action": {"type": "string", "enum": ["clone", "provision", "analyze", "env"]},
            "packages": {"type": "array"},
            "java_version": {"type": "string"},
            "tool": {"type": "string"},
            "executable": {"type": "string"},
            "activate": {"type": "boolean", "enum": [True]},
        },
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={},
        repository_url=None,
    )

    with pytest.raises(ValueError, match=message):
        normalizer.validate_and_fix(tool_name, params)


def test_normalized_action_envelope_is_emitted_before_tool_execution():
    trace = []

    class TracedBashTool(BashLikeTool):
        def execute(self, command: str, timeout: int, working_directory: str = "") -> ToolResult:
            trace.append(("tool", {"command": command, "timeout": timeout}))
            return super().execute(command, timeout, working_directory)

    def before_tool_execute(call, params):
        trace.append(("envelope", call.name, dict(params)))
        return "envelope-1"

    orchestrator, _, _, _ = _orchestrator(
        tools={"bash": TracedBashTool()},
        before_tool_execute=before_tool_execute,
    )

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "echo hi"}))

    assert trace[0] == (
        "envelope",
        "bash",
        {
            "command": "echo hi",
            "timeout": 60,
            "working_directory": "",
        },
    )
    assert trace[1][0] == "tool"
    assert execution.metadata["control_envelope_id"] == "envelope-1"


def test_pre_dispatch_hook_and_runner_receive_the_same_explicit_bash_params():
    from sag.tools.bash import BashTool

    class ExactBashOrchestrator:
        project_name = "must-not-be-injected"

        def __init__(self):
            self.calls = []

        def execute_command(self, command, **kwargs):
            self.calls.append((command, dict(kwargs)))
            if command.startswith("test -d -- "):
                return {"success": True, "exit_code": 0, "output": ""}
            return {
                "success": True,
                "exit_code": 0,
                "output": "exact",
                "duration": 0.01,
            }

    docker = ExactBashOrchestrator()
    frozen = []
    submitted = {
        "command": "printf exact",
        "timeout": 7,
        "working_directory": "/workspace/project",
    }
    orchestrator, _, _, _ = _orchestrator(
        tools={"bash": BashTool(docker)},
        before_tool_execute=lambda call, params: frozen.append(
            (call.name, dict(params))
        )
        or "envelope-exact",
    )

    execution = orchestrator.execute(ToolCall(name="bash", raw_params=dict(submitted)))

    assert execution.status == "success"
    assert frozen == [("bash", submitted)]
    assert execution.call.validated_params == submitted
    assert execution.executed_params == submitted
    assert execution.result.metadata["execution"]["command"] == submitted["command"]
    assert execution.result.metadata["execution"]["cwd"] == submitted["working_directory"]
    assert docker.calls[-1][0] == submitted["command"]
    assert docker.calls[-1][1]["workdir"] == submitted["working_directory"]


def test_omitted_and_explicit_bash_defaults_freeze_to_one_canonical_action():
    from sag.tools.bash import BashTool

    tool = BashTool(None)
    normalizer = ToolParameterNormalizer(
        tools={"bash": tool},
        successful_states={"working_directory": "/workspace/ignored"},
        repository_url="https://example.test/ignored.git",
    )

    omitted = normalizer.validate_and_fix("bash", {"command": "pwd"}, [])
    explicit = normalizer.validate_and_fix(
        "bash",
        {
            "command": "pwd",
            "timeout": 60,
            "working_directory": "/workspace",
        },
        [],
    )

    assert omitted == explicit == {
        "command": "pwd",
        "timeout": 60,
        "working_directory": "/workspace",
    }
    assert action_fingerprint(domain_id="run:/workspace", tool="bash", params=omitted) == (
        action_fingerprint(domain_id="run:/workspace", tool="bash", params=explicit)
    )


def test_unexpected_pre_dispatch_control_error_fails_closed_without_tool_execution():
    executions = []

    class TracedBashTool(BashLikeTool):
        def execute(self, command: str, timeout: int, working_directory: str = "") -> ToolResult:
            executions.append(command)
            return super().execute(command, timeout, working_directory)

    def broken_control(_call, _params):
        raise ValueError("forged intent payload")

    orchestrator, events, tracking, updates = _orchestrator(
        tools={"bash": TracedBashTool()},
        before_tool_execute=broken_control,
    )

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "echo hi"}))

    assert execution.status == "validation_failed"
    assert execution.attempted_execution is False
    assert execution.result.error_code == "PRE_DISPATCH_CONTROL_FAILED"
    assert execution.result.metadata["runner_dispatched"] is False
    assert execution.result.metadata["exception_type"] == "ValueError"
    assert executions == []
    assert tracking == []
    assert updates == []
    assert [event.event_type for event in events] == [
        "tool_start",
        "tool_parameters_fixed",
        "tool_error",
    ]


def test_parameter_normalizer_is_independent_of_runtime_state():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={
            "working_directory": "/workspace/project",
            "maven_success": False,
            "cloned_repos": set(),
        },
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"cmd": "echo hi"}, fixes)

    assert params == {
        "command": "echo hi",
        "timeout": 60,
        "working_directory": "",
    }
    recorded_fixes = {(fix.source, fix.field, fix.before, fix.after) for fix in fixes}
    assert ("schema_alias", "command", None, "echo hi") in recorded_fixes
    assert ("default", "timeout", None, 60) in recorded_fixes
    assert not any(fix.source == "state_injection" for fix in fixes)


def test_bash_parameter_normalizer_does_not_append_fail_at_end_to_non_maven_commands():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "bash",
        {"command": "find /workspace -name 'mvnw' | tail -5"},
        fixes,
    )

    assert params["command"] == "find /workspace -name 'mvnw' | tail -5"


def test_bash_parameter_normalizer_preserves_model_maven_command_byte_for_byte():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"command": "mvn test"}, fixes)

    assert params["command"] == "mvn test"
    assert not any(fix.field == "command" for fix in fixes)


def test_bash_parameter_normalizer_does_not_append_fail_at_end_to_maven_version_command():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"command": "mvn -version"}, fixes)

    assert params["command"] == "mvn -version"
    assert not any(fix.reason == "Appended Maven fail-at-end flag to bash command" for fix in fixes)


@pytest.mark.parametrize(
    "command",
    [
        "mvn --version",
        "mvn help:effective-pom",
        "mvn dependency:tree",
    ],
)
def test_bash_parameter_normalizer_does_not_append_fail_at_end_to_maven_diagnostics(command):
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"command": command}, fixes)

    assert params["command"] == command
    assert not any(fix.reason == "Appended Maven fail-at-end flag to bash command" for fix in fixes)


def test_bash_parameter_normalizer_does_not_append_fail_at_end_to_compound_maven_version():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "bash",
        {"command": "cd /workspace/project && mvn -version"},
        fixes,
    )

    assert params["command"] == "cd /workspace/project && mvn -version"


def test_project_setup_parameter_normalizer_does_not_inject_repository_identity():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"project_setup": ProjectSetupLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url="https://example.test/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
    )

    params = normalizer.validate_and_fix("project_setup", {"action": "clone"}, fixes)

    assert params == {"action": "clone"}
    assert not any(fix.source == "state_injection" for fix in fixes)


@pytest.mark.parametrize("alias", ["tag", "release", "commit", "commit_hash", "version_ref"])
def test_project_setup_parameter_normalizer_maps_version_handle_aliases_to_ref(alias):
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"project_setup": ProjectSetupLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "project_setup",
        {
            "action": "clone",
            "repository_url": "https://example.test/repo.git",
            alias: "rel/commons-cli-1.11.0",
        },
        fixes,
    )

    assert params["ref"] == "rel/commons-cli-1.11.0"
    assert alias not in params


def test_project_parameter_normalizer_maps_path_to_project_path():
    fixes = []
    project = SchemaLikeTool(
        "project",
        {
            "action": {"type": "string"},
            "project_path": {"type": "string"},
        },
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={"working_directory": "/workspace/paramiko"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "project", {"action": "analyze", "path": "/workspace/paramiko"}, fixes
    )

    assert params == {"action": "analyze", "project_path": "/workspace/paramiko"}
    assert ("schema_alias", "project_path", None, "/workspace/paramiko") in {
        (fix.source, fix.field, fix.before, fix.after) for fix in fixes
    }


def test_project_path_alias_on_non_analyze_action_fails_validation():
    project = SchemaLikeTool(
        "project",
        {
            "action": {"type": "string"},
            "project_path": {"type": "string"},
        },
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"project": project},
        successful_states={"working_directory": "/workspace"},
        repository_url=None,
    )

    with pytest.raises(ValueError, match="unexpected parameters: path"):
        normalizer.validate_and_fix(
            "project", {"action": "clone", "path": "/workspace/paramiko"}
        )


def test_report_parameter_normalizer_maps_fields_but_preserves_canonical_action():
    fixes = []
    report = SchemaLikeTool(
        "report",
        {
            "action": {"type": "string", "enum": ["generate"]},
            "summary": {"type": "string"},
            "status": {"type": "string"},
            "details": {"type": "string"},
            "evidence_refs": {"type": "array"},
        },
        required=["action"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"report": report},
        successful_states={"working_directory": "/workspace/paramiko"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "report",
        {
            "action": "generate",
            "summary": "Paramiko is ready",
            "status": "success",
            "key_results": ["dependencies installed", "tests passed"],
            "evidence": "/workspace/paramiko/.setup_agent/report.json",
        },
        fixes,
    )

    assert params == {
        "action": "generate",
        "summary": "Paramiko is ready",
        "status": "success",
        "details": "dependencies installed tests passed",
        "evidence_refs": ["/workspace/paramiko/.setup_agent/report.json"],
    }
    assert {fix.source for fix in fixes} >= {"schema_alias", "safety_fix"}
    assert not any(fix.field == "action" for fix in fixes)


def test_report_content_and_outcome_field_aliases_execute_through_orchestrator():
    report = SchemaLikeTool(
        "report",
        {
            "action": {"type": "string", "enum": ["generate"]},
            "summary": {"type": "string"},
            "status": {"type": "string"},
        },
        required=["action", "summary", "status"],
    )
    orchestrator, _events, _tracking, _updates = _orchestrator(
        tools={"report": report},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="report",
            raw_params={
                "action": "generate",
                "content": "TVM native smoke remained bounded.",
                "outcome": "partial",
            },
        )
    )

    assert execution.status == "success"
    assert execution.executed_params == {
        "action": "generate",
        "summary": "TVM native smoke remained bounded.",
        "status": "partial",
    }
    assert {
        (fix.source, fix.field, fix.before, fix.after)
        for fix in execution.parameter_fixes
    } >= {
        ("schema_alias", "summary", None, "TVM native smoke remained bounded."),
        ("schema_alias", "status", None, "partial"),
    }


def test_report_canonical_fields_win_over_live_aliases():
    fixes = []
    report = SchemaLikeTool(
        "report",
        {
            "action": {"type": "string", "enum": ["generate"]},
            "summary": {"type": "string"},
            "status": {"type": "string"},
        },
        required=["action", "summary", "status"],
    )
    normalizer = ToolParameterNormalizer(
        tools={"report": report},
        successful_states={},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "report",
        {
            "action": "generate",
            "summary": "canonical",
            "status": "partial",
            "content": "a much longer alias must not replace canonical",
            "outcome": "failed",
        },
        fixes,
    )

    assert params == {
        "action": "generate",
        "summary": "canonical",
        "status": "partial",
    }
    assert {
        (fix.source, fix.field, fix.before, fix.after)
        for fix in fixes
    } >= {
        (
            "schema_alias",
            "content",
            "a much longer alias must not replace canonical",
            None,
        ),
        ("schema_alias", "outcome", "failed", None),
    }


def test_bash_parameter_normalizer_preserves_compound_maven_segment():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "bash",
        {"command": "cd /workspace/project && mvn test"},
        fixes,
    )

    assert params["command"] == "cd /workspace/project && mvn test"
    assert not any(fix.field == "command" for fix in fixes)


def test_invalid_report_action_is_refused_instead_of_replaced():
    report = SchemaLikeTool(
        "report",
        {"action": {"type": "string", "enum": ["generate"]}},
        required=["action"],
    )
    orchestrator, events, tracking, updates = _orchestrator(tools={"report": report})

    execution = orchestrator.execute(
        ToolCall(name="report", raw_params={"action": "report"})
    )

    assert execution.status == "validation_failed"
    assert execution.attempted_execution is False
    assert execution.call.raw_params == {"action": "report"}
    assert execution.result.error_code == "PARAMETER_VALIDATION_FAILED"
    assert tracking == []
    assert updates == []
    assert [event.event_type for event in events] == ["tool_start", "tool_error"]


def test_model_observation_never_renders_tool_authored_repair_suggestions():
    result = ToolResult.completed_failure(
        output="runner failed before producing artifacts",
        error="build failed",
        error_code="BUILD_FAILED",
        suggestions=[
            "build(action='test', args='-pl !broken-module')",
            "apt-get install guessed-package",
        ],
    )

    rendered = format_tool_result("bash", result)

    assert "runner failed before producing artifacts" in rendered
    assert "BUILD_FAILED" in rendered
    assert "Suggestions:" not in rendered
    assert "-pl !broken-module" not in rendered
    assert "apt-get install guessed-package" not in rendered


def test_tool_orchestrator_no_longer_owns_parameter_strategy_helpers():
    assert not hasattr(ToolOrchestrator, "_apply_tool_specific_fixes")
    assert not hasattr(ToolOrchestrator, "_fix_parameter_names")
    assert not hasattr(ToolOrchestrator, "_get_smart_default")


def test_validation_failed_status_when_fixing_raises(monkeypatch):
    execution_attempts = []

    class EchoTool(BaseTool):
        def __init__(self):
            super().__init__("echo", "Echo test tool")

        def execute(self, command: str) -> ToolResult:
            execution_attempts.append(command)
            return ToolResult.completed_success(output=command)

    orchestrator, events, tracking_calls, state_updates = _orchestrator(tools={"echo": EchoTool()})

    def raise_validation(tool_name, params, parameter_fixes=None):
        raise RuntimeError("schema broke")

    monkeypatch.setattr(orchestrator.parameter_normalizer, "validate_and_fix", raise_validation)

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "run"}))

    assert execution.status == "validation_failed"
    assert execution.result.succeeded is False
    assert execution.result.error_code == "PARAMETER_VALIDATION_FAILED"
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert execution.validated_params is None
    assert execution_attempts == []
    assert tracking_calls == []
    assert state_updates == []
    assert [event.event_type for event in events] == ["tool_start", "tool_error"]
    error_metadata = events[-1].metadata
    assert error_metadata["invocation_status"] == "completed"
    assert error_metadata["operation_outcome"] == "failed"
    assert error_metadata["evidence_status"] == "verified"
    assert error_metadata["failure_signature"] == execution.result.failure_signature
    assert error_metadata["error_tail_preview"] == execution.result.error_tail_preview
    assert error_metadata["output_ref"] == execution.result.output_ref


def test_react_engine_no_longer_exposes_parameter_wrapper():
    engine = ReActEngine.__new__(ReActEngine)

    with pytest.raises(AttributeError):
        getattr(engine, "_validate_and_fix_parameters")
