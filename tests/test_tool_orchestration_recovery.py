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
                "repository_url": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": [],
        }
        self.results = list(results)
        self.calls = []

    def execute(self, **params) -> ToolResult:
        self.calls.append(dict(params))
        if self.results:
            return self.results.pop(0)
        return ToolResult(success=False, output="", error="No queued result")


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
    events=None,
    state_updates=None,
):
    if events is None:
        events = []
    if state_updates is None:
        state_updates = []

    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=repository_url,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )


def test_project_setup_recovery_injects_repository_url():
    events = []
    state_updates = []
    tool = ResultTool(
        "project_setup",
        [
            ToolResult(
                success=False,
                output="",
                error="repository_url is required",
                error_code="MISSING_PARAMETERS",
            ),
            ToolResult(success=True, output="cloned repository"),
        ],
    )
    orchestrator = _orchestrator(
        tools={"project_setup": tool},
        repository_url="https://example.com/repo.git",
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
    assert execution.result.success is True
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "project_setup_repository_url"
    assert execution.executed_params == {
        "action": "clone",
        "repository_url": "https://example.com/repo.git",
    }
    assert tool.calls == [
        {"action": "clone"},
        {"action": "clone", "repository_url": "https://example.com/repo.git"},
    ]
    assert execution.metadata["recovery"]["attempted"] is True
    assert execution.metadata["recovery"]["success"] is True
    assert execution.metadata["recovery"]["strategy"] == "project_setup_repository_url"
    recovery_events = [event for event in events if event.event_type == "tool_recovery"]
    assert len(recovery_events) == 1
    assert recovery_events[0].metadata["recovery_strategy"] == "project_setup_repository_url"
    assert recovery_events[0].metadata["success"] is True
    assert recovery_events[0].metadata["replacement_result_success"] is True
    assert recovery_events[0].metadata["recovery_params"] == execution.executed_params
    assert state_updates == [("project_setup", execution.executed_params, execution.result)]


def test_manage_context_recovery_uses_single_in_progress_task():
    events = []
    tool = ResultTool(
        "manage_context",
        [
            ToolResult(
                success=False,
                output="",
                error="No active task to complete",
                error_code="NO_ACTIVE_TASK",
            ),
            ToolResult(success=True, output="completed task"),
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
    assert execution.result.success is True
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


def test_generic_recovery_returns_failure_without_silent_success():
    tool = ResultTool(
        "echo",
        [
            ToolResult(
                success=False,
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
    assert execution.result.success is False
    assert execution.recovery_applied is False
    assert execution.recovery_strategy is None
    assert execution.executed_params == {"command": "run"}
    assert tool.calls == [{"command": "run"}]
    assert execution.metadata["recovery"]["attempted"] is False
    assert execution.metadata["recovery"]["success"] is False
    assert execution.metadata["recovery"]["strategy"] == "generic_no_strategy"
    assert (
        execution.metadata["recovery"]["message"]
        == "No generic recovery strategy available"
    )
