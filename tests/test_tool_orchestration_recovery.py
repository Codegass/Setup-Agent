from dataclasses import dataclass

from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class ResultTool(BaseTool):
    def __init__(self, name, results):
        super().__init__(name, "Result test tool")
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "command": {"type": "string"},
                "task": {"type": "string"},
                "tasks": {"type": "string"},
                "repository_url": {"type": "string"},
                "ref": {"type": "string"},
                "summary": {"type": "string"},
                "task_id": {"type": "string"},
                "java_version": {"type": "string"},
                "working_directory": {"type": "string"},
                "pom_file": {"type": "string"},
                "path": {"type": "string"},
                "properties": {"type": ["string", "array"]},
                "fail_at_end": {"type": "boolean"},
                "timeout": {"type": "integer"},
            },
            "required": [],
        }
        self.results = list(results)
        self.calls = []

    def execute(self, **params) -> ToolResult:
        self.calls.append(dict(params))
        if self.results:
            return self.results.pop(0)
        return ToolResult.completed_failure(output="", error="No queued result")


@dataclass
class TaskStatus:
    value: str


@dataclass
class Task:
    id: str
    status: TaskStatus


class TrunkContext:
    def __init__(self, tasks):
        self.todo_list = tasks

    def get_next_pending_task(self):
        return next(
            (task for task in self.todo_list if task.status.value == "pending"),
            None,
        )


class ContextManager:
    def __init__(self, trunk_context):
        self.trunk_context = trunk_context
        self.current_task_id = None

    def load_trunk_context(self):
        return self.trunk_context


def _orchestrator(
    *,
    tools,
    context_manager=None,
    repository_url=None,
    repository_ref=None,
    events=None,
    state_updates=None,
    successful_states=None,
    guidance=None,
):
    if events is None:
        events = []
    if state_updates is None:
        state_updates = []
    if successful_states is None:
        successful_states = {}
    if guidance is None:
        guidance = []

    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=[],
        successful_states=successful_states,
        repository_url=repository_url,
        repository_ref=repository_ref,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: guidance.append((message, priority)),
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )


class FakeBuildOrchestrator:
    def __init__(self, output, project_name=None):
        self.output = output
        self.project_name = project_name
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        return {"success": True, "output": self.output, "exit_code": 0}


class BuildContextManager:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator


class WorkspaceRecoveryOrchestrator:
    def __init__(self, successes=None):
        self.successes = list(successes or [True, True, True])
        self.commands = []

    def execute_command(self, command, workdir=None):
        self.commands.append({"command": command, "workdir": workdir})
        success = self.successes.pop(0) if self.successes else True
        return {
            "success": success,
            "output": "ok" if success else "failed",
            "exit_code": 0 if success else 1,
        }


def test_project_setup_recovery_injects_repository_url():
    events = []
    state_updates = []
    tool = ResultTool(
        "project_setup",
        [
            ToolResult.completed_failure(
                output="",
                error="repository_url is required",
                error_code="MISSING_PARAMETERS",
            ),
            ToolResult.completed_success(output="cloned repository"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"project_setup": tool},
        repository_url="https://example.com/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
        events=events,
        state_updates=state_updates,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="project_setup",
            raw_params={"action": "clone"},
            validated_params={"action": "clone"},
        )
    )

    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "project_setup_repository_url"
    assert execution.executed_params == {
        "action": "clone",
        "repository_url": "https://example.com/repo.git",
        "ref": "rel/commons-cli-1.11.0",
    }
    assert tool.calls == [
        {"action": "clone"},
        {
            "action": "clone",
            "repository_url": "https://example.com/repo.git",
            "ref": "rel/commons-cli-1.11.0",
        },
    ]
    assert execution.metadata["recovery"]["attempted"] is True
    assert execution.metadata["recovery"]["success"] is True
    assert execution.metadata["recovery"]["strategy"] == "project_setup_repository_url"
    recovery_events = [event for event in events if event.event_type == "tool_recovery"]
    assert len(recovery_events) == 1
    assert recovery_events[0].metadata["recovery_strategy"] == "project_setup_repository_url"
    assert recovery_events[0].metadata["recovery_params"]["ref"] == "rel/commons-cli-1.11.0"
    assert recovery_events[0].metadata["success"] is True
    assert recovery_events[0].metadata["replacement_result_succeeded"] is True
    assert recovery_events[0].metadata["recovery_params"] == execution.executed_params
    assert state_updates == [("project_setup", execution.executed_params, execution.result)]


def test_recovery_and_error_events_include_required_metadata():
    events = []
    project_setup = ResultTool(
        "project_setup",
        [
            ToolResult.completed_failure(output="", error="missing url"),
            ToolResult.completed_success(output="cloned"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"project_setup": project_setup},
        repository_url="https://example/repo.git",
        events=events,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="project_setup",
            raw_params={"action": "clone"},
            validated_params={"action": "clone"},
        )
    )

    recovery_event = next(event for event in events if event.event_type == "tool_recovery")
    result_event = events[-1]
    assert recovery_event.metadata["recovery_strategy"] == "project_setup_repository_url"
    assert recovery_event.metadata["attempted"] is True
    assert recovery_event.metadata["success"] is True
    assert recovery_event.metadata["guidance"]
    assert recovery_event.metadata["replacement_result_succeeded"] is True
    assert (
        recovery_event.metadata["recovery_params"]["repository_url"] == "https://example/repo.git"
    )
    assert recovery_event.metadata["parameter_diff"]["repository_url"]["before"] is None
    assert (
        recovery_event.metadata["parameter_diff"]["repository_url"]["after"]
        == "https://example/repo.git"
    )
    assert result_event.metadata["recovery_applied"] is True
    assert result_event.metadata["status"] == execution.status

    events = []
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )
    execution = orchestrator.execute(ToolCall(name="missing", raw_params={}))

    error_event = events[-1]
    assert error_event.event_type == "tool_error"
    assert error_event.metadata["error_code"] == "UNKNOWN_TOOL"
    assert error_event.metadata["category"] == "validation"
    assert error_event.metadata["suggestions"]
    assert error_event.metadata["original_error"] == execution.result.error
    assert error_event.metadata["recovery_attempted"] is False


def test_manage_context_recovery_uses_single_in_progress_task():
    events = []
    tool = ResultTool(
        "manage_context",
        [
            ToolResult.completed_failure(
                output="",
                error="No active task to complete",
                error_code="NO_ACTIVE_TASK",
            ),
            ToolResult.completed_success(output="completed task"),
        ],
    )
    context_manager = ContextManager(
        TrunkContext(
            [
                Task(id="task_1", status=TaskStatus("pending")),
                Task(id="task_2", status=TaskStatus("in_progress")),
            ]
        )
    )
    orchestrator = _orchestrator(
        tools={"manage_context": tool},
        context_manager=context_manager,
        events=events,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="manage_context",
            raw_params={"action": "complete_with_results", "summary": "done"},
            validated_params={"action": "complete_with_results", "summary": "done"},
        )
    )

    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "manage_context_active_task"
    assert context_manager.current_task_id == "task_2"
    assert tool.calls == [
        {"action": "complete_with_results", "summary": "done"},
        {"action": "complete_with_results", "summary": "done"},
    ]
    assert execution.executed_params == {"action": "complete_with_results", "summary": "done"}
    assert execution.metadata["recovery"]["attempted"] is True
    assert execution.metadata["recovery"]["success"] is True
    assert execution.metadata["recovery"]["strategy"] == "manage_context_active_task"
    recovery_events = [event for event in events if event.event_type == "tool_recovery"]
    assert len(recovery_events) == 1
    assert recovery_events[0].metadata["recovery_strategy"] == "manage_context_active_task"
    assert recovery_events[0].metadata["success"] is True


def test_manage_context_invalid_task_id_recovery_uses_next_pending_task():
    events = []
    tool = ResultTool(
        "manage_context",
        [
            ToolResult.completed_failure(
                output="",
                error="Invalid task ID: task_1",
                error_code="INVALID_TASK_ID",
            ),
            ToolResult.completed_success(output="started task_2"),
        ],
    )
    context = ContextManager(
        TrunkContext(
            [
                Task("task_1", TaskStatus("completed")),
                Task("task_2", TaskStatus("pending")),
            ]
        )
    )
    orchestrator = _orchestrator(
        tools={"manage_context": tool},
        context_manager=context,
        events=events,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="manage_context",
            raw_params={"action": "start_task", "task_id": "task_1"},
            validated_params={"action": "start_task", "task_id": "task_1"},
        )
    )

    expected_params = {"action": "start_task", "task_id": "task_2"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "manage_context_invalid_task_id"
    assert execution.executed_params == expected_params
    assert tool.calls == [
        {"action": "start_task", "task_id": "task_1"},
        expected_params,
    ]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params
    recovery_event = next(event for event in events if event.event_type == "tool_recovery")
    assert recovery_event.metadata["recovery_strategy"] == "manage_context_invalid_task_id"
    assert recovery_event.metadata["parameter_diff"] == {
        "task_id": {"before": "task_1", "after": "task_2"}
    }


def test_generic_recovery_returns_failure_without_silent_success():
    tool = ResultTool(
        "echo",
        [
            ToolResult.completed_failure(
                output="",
                error="something failed",
                error_code="GENERIC_FAILURE",
            ),
        ],
    )
    orchestrator = _orchestrator(tools={"echo": tool})

    execution = orchestrator.execute(
        ToolCall(
            name="echo",
            raw_params={"command": "run"},
            validated_params={"command": "run"},
        )
    )

    assert execution.status == "failure"
    assert execution.result.succeeded is False
    assert execution.recovery_applied is False
    assert execution.recovery_strategy is None
    assert execution.executed_params == {"command": "run"}
    assert tool.calls == [{"command": "run"}]
    assert execution.metadata["recovery"]["attempted"] is False
    assert execution.metadata["recovery"]["success"] is False
    assert execution.metadata["recovery"]["strategy"] == "generic_no_strategy"
    assert execution.metadata["recovery"]["message"] == "No generic recovery strategy available"


def test_maven_java_version_recovery_installs_and_retries():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="Java 17 is required",
                error_code="JAVA_VERSION_MISMATCH",
                metadata={
                    "analysis": {
                        "java_version_error": {
                            "required": "17",
                            "current": "11",
                        }
                    }
                },
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    system = ResultTool(
        "system",
        [
            ToolResult.completed_failure(output="", error="missing"),
            ToolResult.completed_success(output="installed"),
        ],
    )
    orchestrator = _orchestrator(tools={"maven": maven, "system": system})

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_strategy == "maven_java_version"
    assert execution.executed_params == {"command": "test"}
    assert maven.calls == [{"command": "test"}, {"command": "test"}]
    assert system.calls == [
        {"action": "verify_java", "java_version": "17"},
        {"action": "install_java", "java_version": "17"},
    ]
    assert execution.metadata["recovery"]["recovery_params"] == {"command": "test"}


def test_maven_java_version_failed_install_preserves_maven_execution_params():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="Java 17 is required",
                error_code="JAVA_VERSION_MISMATCH",
                metadata={
                    "analysis": {
                        "java_version_error": {
                            "required": "17",
                            "current": "11",
                        }
                    }
                },
            ),
        ],
    )
    system = ResultTool(
        "system",
        [
            ToolResult.completed_failure(output="", error="missing"),
            ToolResult.completed_failure(output="", error="install failed"),
        ],
    )
    orchestrator = _orchestrator(tools={"maven": maven, "system": system})

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    assert execution.status == "recovery_failed"
    assert execution.recovery_strategy == "maven_java_version"
    assert execution.executed_params == {"command": "test"}
    assert execution.metadata["recovery"]["recovery_params"] == {"command": "test"}
    assert execution.metadata["recovery"]["repair_params"] == {
        "action": "install_java",
        "java_version": "17",
    }
    assert system.calls == [
        {"action": "verify_java", "java_version": "17"},
        {"action": "install_java", "java_version": "17"},
    ]
    assert maven.calls == [{"command": "test"}]


def test_maven_working_directory_recovery_retries_known_directory():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="pom.xml not found: no such file",
                error_code="MISSING_PROJECT",
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"maven": maven},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_known_working_directory"
    assert execution.executed_params == {
        "command": "test",
        "working_directory": "/workspace/app",
    }
    assert maven.calls == [
        {"command": "test"},
        {"command": "test", "working_directory": "/workspace/app"},
    ]
    assert execution.metadata["recovery"]["recovery_params"] == execution.executed_params


def test_maven_compile_before_test_recovery():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="Compilation failure before tests",
                error_code="BUILD_FAILED",
            ),
            ToolResult.completed_success(output="compiled"),
        ],
    )
    orchestrator = _orchestrator(tools={"maven": maven})

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test", "working_directory": "/workspace/app"},
            validated_params={"command": "test", "working_directory": "/workspace/app"},
        )
    )

    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_compile_before_test"
    assert execution.executed_params == {
        "command": "compile",
        "working_directory": "/workspace/app",
    }
    assert maven.calls == [
        {"command": "test", "working_directory": "/workspace/app"},
        {"command": "compile", "working_directory": "/workspace/app"},
    ]
    assert execution.metadata["recovery"]["recovery_params"] == execution.executed_params


def test_maven_pom_discovery_recovery_targets_detected_pom():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="The goal requires a pom but no pom was found",
                error_code="BUILD_FAILED",
                metadata={"analysis": {"error_type": "MISSING_PROJECT"}},
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    build_orchestrator = FakeBuildOrchestrator(
        "\n".join(
            [
                "/workspace/other/pom.xml",
                "/workspace/sample/module/pom.xml",
                "/workspace/sample/pom.xml",
            ]
        ),
        project_name="sample",
    )
    orchestrator = _orchestrator(
        tools={"maven": maven},
        context_manager=BuildContextManager(build_orchestrator),
    )

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    expected_params = {
        "command": "test",
        "pom_file": "/workspace/sample/pom.xml",
        "working_directory": "/workspace/sample",
    }
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_pom_discovery"
    assert execution.executed_params == expected_params
    assert build_orchestrator.commands == ["find /workspace -maxdepth 4 -name pom.xml | head -20"]
    assert maven.calls == [{"command": "test"}, expected_params]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_maven_no_pom_xml_recovery_targets_detected_pom_before_known_directory():
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="No pom.xml found at /workspace",
                error_code="NO_POM_XML",
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    build_orchestrator = FakeBuildOrchestrator(
        "/workspace/sample/pom.xml",
        project_name="sample",
    )
    orchestrator = _orchestrator(
        tools={"maven": maven},
        context_manager=BuildContextManager(build_orchestrator),
        successful_states={"working_directory": "/workspace/known-good"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    expected_params = {
        "command": "test",
        "pom_file": "/workspace/sample/pom.xml",
        "working_directory": "/workspace/sample",
    }
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_pom_discovery"
    assert execution.executed_params == expected_params
    assert maven.calls == [{"command": "test"}, expected_params]


def test_maven_module_and_test_exclusion_recovery_records_exclusions():
    successful_states = {}
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="Module and test failures",
                error_code="BUILD_FAILED",
                metadata={
                    "analysis": {
                        "failed_modules": [
                            {"artifact_id": "bad-module"},
                            {"pom_path": "/workspace/app/other/pom.xml"},
                        ],
                        "failed_tests": [
                            "com.example.FooTest.shouldFail",
                            "com.example.BarTest#bad",
                        ],
                    }
                },
            ),
            ToolResult.completed_success(output="remaining modules ok"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"maven": maven},
        successful_states=successful_states,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test", "properties": ["skipITs=true"]},
            validated_params={"command": "test", "properties": ["skipITs=true"]},
        )
    )

    expected_params = {
        "command": "test",
        "properties": [
            "skipITs=true",
            "-pl !bad-module,!other",
            "-am",
            "test=!com.example.BarTest#bad,!com.example.FooTest#shouldFail",
        ],
        "fail_at_end": True,
    }
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "maven_exclude_modules_or_tests"
    assert execution.executed_params == expected_params
    assert maven.calls == [
        {"command": "test", "properties": ["skipITs=true"]},
        expected_params,
    ]
    assert successful_states["excluded_modules"] == {"bad-module", "other"}
    assert successful_states["excluded_tests"] == {
        "com.example.BarTest#bad",
        "com.example.FooTest#shouldFail",
    }
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_maven_version_error_returns_env_overlay_guidance_without_retry():
    guidance = []
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output=(
                    "[ERROR] Detected Maven Version: 3.8.7 is not in the allowed range [3.9,)."
                ),
                error="Maven build failed",
                error_code="MAVEN_VERSION_ERROR",
                metadata={
                    "maven_version_requirement": {
                        "raw": "[3.9,)",
                        "source": "build_error",
                        "kind": "range",
                    },
                    "maven_runtime": {
                        "executable": "/usr/bin/mvn",
                        "version": "3.8.7",
                        "source": "system",
                    },
                },
            )
        ],
    )
    orchestrator = _orchestrator(tools={"maven": maven}, guidance=guidance)

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "compile", "working_directory": "/workspace/app"},
            validated_params={"command": "compile", "working_directory": "/workspace/app"},
        )
    )

    assert execution.status == "recovery_attempted"
    assert execution.recovery_strategy == "maven_version_contract_guidance"
    assert execution.result.error_code == "MAVEN_VERSION_ERROR"
    assert execution.executed_params == {
        "command": "compile",
        "working_directory": "/workspace/app",
    }
    assert maven.calls == [{"command": "compile", "working_directory": "/workspace/app"}]
    assert len(guidance) == 1
    assert guidance[0][1] == "high"
    assert "MAVEN VERSION REQUIREMENT" in guidance[0][0]
    assert "project(action='env'" in guidance[0][0]


def test_maven_timeout_returns_guidance_without_retry():
    guidance = []
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="timed out",
                error_code="TIMEOUT_WALL",
                metadata={
                    "execution_time": 301.2,
                    "termination_reason": "wall_clock_timeout",
                },
            ),
        ],
    )
    orchestrator = _orchestrator(tools={"maven": maven}, guidance=guidance)

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.succeeded is False
    assert execution.result.error_code == "MAVEN_TIMEOUT_HANDLED"
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "maven_timeout_guidance"
    assert execution.executed_params == {"command": "test"}
    assert maven.calls == [{"command": "test"}]
    assert len(guidance) == 1
    assert guidance[0][1] == "high"
    assert "MAVEN TIMEOUT" in guidance[0][0]
    assert execution.metadata["recovery"]["success"] is False
    assert execution.metadata["recovery"]["replacement_result_succeeded"] is False


def test_maven_timeout_takes_precedence_over_retry_recovery():
    guidance = []
    maven = ResultTool(
        "maven",
        [
            ToolResult.completed_failure(
                output="",
                error="pom not found during timeout",
                error_code="TIMEOUT_IDLE",
                metadata={
                    "execution_time": 120.0,
                    "termination_reason": "idle_timeout",
                },
            ),
            ToolResult.completed_success(output="should not retry"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"maven": maven},
        successful_states={"working_directory": "/workspace/app"},
        guidance=guidance,
    )

    execution = orchestrator.execute(
        ToolCall(
            name="maven",
            raw_params={"command": "test"},
            validated_params={"command": "test"},
        )
    )

    assert execution.status == "recovery_attempted"
    assert execution.recovery_strategy == "maven_timeout_guidance"
    assert execution.result.error_code == "MAVEN_TIMEOUT_HANDLED"
    assert len(maven.calls) == 1
    assert len(guidance) == 1
    assert guidance[0][1] == "high"
    assert "MAVEN TIMEOUT" in guidance[0][0]


def test_gradle_timeout_returns_guidance_without_retry():
    guidance = []
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="timed out",
                error_code="TIMEOUT_WALL",
                metadata={
                    "execution_time": 250.0,
                    "termination_reason": "wall_clock_timeout",
                },
            ),
        ],
    )
    orchestrator = _orchestrator(tools={"gradle": gradle}, guidance=guidance)

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"task": "test"},
            validated_params={"task": "test"},
        )
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.succeeded is False
    assert execution.result.error_code == "GRADLE_TIMEOUT_HANDLED"
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "gradle_timeout_guidance"
    assert execution.executed_params == {"task": "test"}
    assert gradle.calls == [{"task": "test"}]
    assert len(guidance) == 1
    assert guidance[0][1] == "high"
    assert "GRADLE TIMEOUT" in guidance[0][0]
    assert execution.metadata["recovery"]["success"] is False
    assert execution.metadata["recovery"]["replacement_result_succeeded"] is False


def test_gradle_working_directory_recovery_retries_known_directory():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="build.gradle not found: no such file",
                error_code="MISSING_PROJECT",
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"gradle": gradle},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"task": "test"},
            validated_params={"task": "test"},
        )
    )

    expected_params = {"task": "test", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_known_working_directory"
    assert execution.executed_params == expected_params
    assert gradle.calls == [{"task": "test"}, expected_params]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_gradle_build_file_not_found_recovery_retries_known_directory():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="No build.gradle or build.gradle.kts found in /workspace",
                error_code="BUILD_FILE_NOT_FOUND",
            ),
            ToolResult.completed_success(output="build ok"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"gradle": gradle},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"tasks": "test"},
            validated_params={"tasks": "test"},
        )
    )

    expected_params = {"tasks": "test", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_known_working_directory"
    assert execution.executed_params == expected_params
    assert gradle.calls == [{"tasks": "test"}, expected_params]


def test_gradle_compile_fallback_recovery():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="Compilation failure before tests",
                error_code="BUILD_FAILED",
            ),
            ToolResult.completed_success(output="compiled"),
        ],
    )
    orchestrator = _orchestrator(tools={"gradle": gradle})

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"task": "test", "working_directory": "/workspace/app"},
            validated_params={"task": "test", "working_directory": "/workspace/app"},
        )
    )

    expected_params = {"tasks": "compileJava", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_compile_before_test"
    assert execution.executed_params == expected_params
    assert gradle.calls == [
        {"task": "test", "working_directory": "/workspace/app"},
        expected_params,
    ]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_gradle_compile_fallback_recovery_accepts_tasks_parameter():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="Compilation failure before tests",
                error_code="BUILD_FAILED",
            ),
            ToolResult.completed_success(output="compiled"),
        ],
    )
    orchestrator = _orchestrator(tools={"gradle": gradle})

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"tasks": "test", "working_directory": "/workspace/app"},
            validated_params={"tasks": "test", "working_directory": "/workspace/app"},
        )
    )

    expected_params = {"tasks": "compileJava", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_compile_before_test"
    assert execution.executed_params == expected_params
    assert gradle.calls == [
        {"tasks": "test", "working_directory": "/workspace/app"},
        expected_params,
    ]


def test_gradle_compile_fallback_recovery_accepts_command_alias():
    gradle = ResultTool(
        "gradle",
        [
            ToolResult.completed_failure(
                output="",
                error="Compilation failure before tests",
                error_code="BUILD_FAILED",
            ),
            ToolResult.completed_success(output="compiled"),
        ],
    )
    orchestrator = _orchestrator(tools={"gradle": gradle})

    execution = orchestrator.execute(
        ToolCall(
            name="gradle",
            raw_params={"command": "test", "working_directory": "/workspace/app"},
            validated_params={"command": "test", "working_directory": "/workspace/app"},
        )
    )

    expected_params = {"tasks": "compileJava", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.recovery_strategy == "gradle_compile_before_test"
    assert execution.executed_params == expected_params
    assert gradle.calls == [
        {"command": "test", "working_directory": "/workspace/app"},
        expected_params,
    ]


def test_bash_timeout_guidance_adds_system_guidance():
    guidance = []
    bash = ResultTool(
        "bash",
        [
            ToolResult.completed_failure(
                output="",
                error="timed out",
                error_code="TIMEOUT_WALL",
                metadata={
                    "monitoring_info": {"execution_time": 180.5},
                    "termination_reason": "wall_clock_timeout",
                },
            ),
            ToolResult.completed_success(output="should not retry"),
        ],
    )
    orchestrator = _orchestrator(tools={"bash": bash}, guidance=guidance)

    execution = orchestrator.execute(
        ToolCall(
            name="bash",
            raw_params={"command": "mvn test", "timeout": 120},
            validated_params={"command": "mvn test", "timeout": 120},
        )
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.succeeded is False
    assert execution.result.error_code == "TIMEOUT_HANDLED"
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "bash_timeout_guidance"
    assert execution.executed_params == {"command": "mvn test", "timeout": 120}
    assert bash.calls == [{"command": "mvn test", "timeout": 120}]
    assert len(guidance) == 1
    assert guidance[0][1] == "high"
    assert "TIMEOUT HANDLED" in guidance[0][0]
    assert any(
        "Maven command timed out" in suggestion for suggestion in execution.result.suggestions
    )
    assert execution.metadata["recovery"]["recovery_params"] == {
        "command": "mvn test",
        "timeout": 120,
    }


def test_bash_workspace_recreation_retries_original_command():
    bash = ResultTool(
        "bash",
        [
            ToolResult.completed_failure(
                output="",
                error="OCI runtime exec failed: no such file or directory",
                metadata={"exit_code": 127},
            ),
            ToolResult.completed_success(output="workspace fixed"),
        ],
    )
    workspace_orchestrator = WorkspaceRecoveryOrchestrator()
    orchestrator = _orchestrator(
        tools={"bash": bash},
        context_manager=BuildContextManager(workspace_orchestrator),
    )

    execution = orchestrator.execute(
        ToolCall(
            name="bash",
            raw_params={"command": "pwd", "working_directory": "/missing"},
            validated_params={"command": "pwd", "working_directory": "/missing"},
        )
    )

    expected_params = {"command": "pwd", "working_directory": "/workspace"}
    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_strategy == "bash_workspace_recreation"
    assert execution.executed_params == expected_params
    assert bash.calls == [
        {"command": "pwd", "working_directory": "/missing"},
        expected_params,
    ]
    assert workspace_orchestrator.commands == [
        {"command": "mkdir -p /workspace", "workdir": None},
        {"command": "chmod 755 /workspace", "workdir": None},
        {"command": "touch /workspace/.sag_workspace_marker", "workdir": None},
    ]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_bash_known_working_directory_recovery():
    bash = ResultTool(
        "bash",
        [
            ToolResult.completed_failure(
                output="",
                error="working directory is unavailable",
            ),
            ToolResult.completed_success(output="ok"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"bash": bash},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="bash",
            raw_params={"command": "ls"},
            validated_params={"command": "ls"},
        )
    )

    expected_params = {"command": "ls", "working_directory": "/workspace/app"}
    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_strategy == "bash_known_working_directory"
    assert execution.executed_params == expected_params
    assert bash.calls == [{"command": "ls"}, expected_params]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_file_io_path_recovery_uses_known_working_directory():
    file_io = ResultTool(
        "file_io",
        [
            ToolResult.completed_failure(
                output="",
                error="README.md not found",
            ),
            ToolResult.completed_success(output="contents"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"file_io": file_io},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="file_io",
            raw_params={"action": "read", "path": "README.md"},
            validated_params={"action": "read", "path": "README.md"},
        )
    )

    expected_params = {"action": "read", "path": "/workspace/app/README.md"}
    assert execution.status == "recovered"
    assert execution.result.succeeded is True
    assert execution.recovery_strategy == "file_io_known_working_directory"
    assert execution.executed_params == expected_params
    assert file_io.calls == [
        {"action": "read", "path": "README.md"},
        expected_params,
    ]
    assert execution.metadata["recovery"]["recovery_params"] == expected_params


def test_recovery_failed_status_when_replacement_result_fails():
    bash = ResultTool(
        "bash",
        [
            ToolResult.completed_failure(
                output="",
                error="working directory is unavailable",
            ),
            ToolResult.completed_failure(output="", error="still failed"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"bash": bash},
        successful_states={"working_directory": "/workspace/app"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="bash",
            raw_params={"command": "ls"},
            validated_params={"command": "ls"},
        )
    )

    expected_params = {"command": "ls", "working_directory": "/workspace/app"}
    assert execution.status == "recovery_failed"
    assert execution.result.succeeded is False
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "bash_known_working_directory"
    assert execution.executed_params == expected_params
    assert bash.calls == [{"command": "ls"}, expected_params]
    assert execution.metadata["recovery"]["attempted"] is True
    assert execution.metadata["recovery"]["success"] is False
    assert execution.metadata["recovery"]["replacement_result_succeeded"] is False
    assert execution.metadata["recovery"]["recovery_params"] == expected_params
