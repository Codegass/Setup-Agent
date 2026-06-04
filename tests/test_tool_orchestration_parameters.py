import pytest

from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.agent.tool_parameters import ToolParameterNormalizer
from sag.tools.base import BaseTool, ToolResult


class BashLikeTool(BaseTool):
    def __init__(self):
        super().__init__("bash", "Bash-like test tool")

    def execute(self, command: str, timeout: int, working_directory: str = "") -> ToolResult:
        return ToolResult(
            success=True,
            output=f"{working_directory}: {command} ({timeout})",
            metadata={
                "command": command,
                "timeout": timeout,
                "working_directory": working_directory,
            },
        )


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
        track_tool_execution=lambda signature, success: tracking_calls.append((signature, success)),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
        **overrides,
    )
    return orchestrator, events, tracking_calls, state_updates


def test_parameter_alias_default_and_state_injection_are_recorded():
    orchestrator, events, tracking_calls, state_updates = _orchestrator()

    execution = orchestrator.execute(ToolCall(name="bash", raw_params={"cmd": "echo hi"}))

    assert execution.status == "success"
    assert execution.executed_params == {
        "command": "echo hi",
        "timeout": 60,
        "working_directory": "/workspace/project",
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
    assert ("schema_alias", "command", "help", "echo hi") in fixes
    assert ("default", "timeout", None, 60) in fixes
    assert (
        "state_injection",
        "working_directory",
        None,
        "/workspace/project",
    ) in fixes
    assert tracking_calls == [
        (
            "bash:[('command', 'echo hi'), ('timeout', 60), "
            "('working_directory', '/workspace/project')]",
            True,
        )
    ]
    assert len(state_updates) == 1


def test_parameter_normalizer_owns_alias_default_and_state_injection():
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
        "working_directory": "/workspace/project",
    }
    recorded_fixes = {(fix.source, fix.field, fix.before, fix.after) for fix in fixes}
    assert ("schema_alias", "command", "help", "echo hi") in recorded_fixes
    assert ("default", "timeout", None, 60) in recorded_fixes
    assert (
        "state_injection",
        "working_directory",
        None,
        "/workspace/project",
    ) in recorded_fixes


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


def test_bash_parameter_normalizer_appends_fail_at_end_to_simple_maven_command():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"command": "mvn test"}, fixes)

    assert params["command"] == "mvn test --fail-at-end"
    assert (
        "safety_fix",
        "command",
        "mvn test",
        "mvn test --fail-at-end",
    ) in {(fix.source, fix.field, fix.before, fix.after) for fix in fixes}


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


def test_bash_parameter_normalizer_appends_fail_at_end_to_compound_maven_segment():
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

    assert params["command"] == "cd /workspace/project && mvn test --fail-at-end"


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
            return ToolResult(success=True, output=command)

    orchestrator, events, tracking_calls, state_updates = _orchestrator(tools={"echo": EchoTool()})

    def raise_validation(tool_name, params, parameter_fixes=None):
        raise RuntimeError("schema broke")

    monkeypatch.setattr(orchestrator.parameter_normalizer, "validate_and_fix", raise_validation)

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "run"}))

    assert execution.status == "validation_failed"
    assert execution.result.success is False
    assert execution.result.error_code == "PARAMETER_VALIDATION_FAILED"
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert execution.validated_params is None
    assert execution_attempts == []
    assert tracking_calls == []
    assert state_updates == []
    assert [event.event_type for event in events] == ["tool_start", "tool_error"]


def test_react_engine_no_longer_exposes_parameter_wrapper():
    engine = ReActEngine.__new__(ReActEngine)

    with pytest.raises(AttributeError):
        getattr(engine, "_validate_and_fix_parameters")
