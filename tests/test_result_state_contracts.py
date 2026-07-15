from io import StringIO
from types import SimpleNamespace

from rich.console import Console

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent
from sag.agent.agent_state_evaluator import AgentStateAnalysis, AgentStateEvaluator, AgentStatus
from sag.agent.react_types import StepType
from sag.tools.base import ToolResult


class FakeContextManager:
    current_task_id = None

    def load_trunk_context(self):
        return {
            "todo_list": [{"id": "task_1", "description": "Clone repository", "status": "pending"}]
        }


def test_tool_result_preserves_declared_raw_data():
    result = ToolResult.completed_success(
        output="ok",
        raw_data={"full_report": "report text", "report_snapshot": {"status": "success"}},
    )

    assert result.raw_data["full_report"] == "report text"
    assert result.model_dump()["raw_data"]["report_snapshot"]["status"] == "success"


def test_agent_status_has_stuck_state():
    assert AgentStatus.STUCK.value == "stuck"


def test_agent_state_analysis_uses_declared_guidance_fields():
    analysis = AgentStateAnalysis(
        status=AgentStatus.STUCK,
        needs_guidance=True,
        guidance_message="Use project_analyzer",
        guidance_priority=10,
    )

    assert analysis.guidance_message == "Use project_analyzer"
    assert analysis.guidance_priority == 10


def test_agent_state_evaluator_guidance_branch_uses_declared_fields():
    evaluator = AgentStateEvaluator(FakeContextManager())

    analysis = evaluator._check_ghost_state([SimpleNamespace(tool_name="maven")])

    assert analysis.status == AgentStatus.STUCK
    assert analysis.needs_guidance is True
    assert "GHOST STATE" in analysis.guidance_message
    assert analysis.guidance_priority == 10


class FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeConfig:
    ui_mode = False
    max_iterations = 3


class FakeOrchestrator:
    def __init__(self):
        self.last_comments = []

    def update_last_comment(self, comment):
        self.last_comments.append(comment)


class FakeRunTaskContextManager:
    def __init__(self):
        self.trunk_context = FakeTrunkContext()

    def load_or_create_trunk_context(self, **kwargs):
        return self.trunk_context

    def get_current_context_info(self):
        return {"context_id": "trunk_test"}


class FakeTrunkContext:
    def add_task(self, description):
        raise AssertionError("run_task must not append sag run --task requests to setup TODO")


class FakeReActEngine:
    def __init__(self):
        self.calls = []

    def run_react_loop(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_run_task_uses_run_task_completion_without_appending_setup_todo(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "create_command_logger",
        lambda command, project: (FakeLogger(), "cmd_test"),
    )
    monkeypatch.setattr(agent_module, "get_session_logger", lambda: None)

    agent = SetupAgent.__new__(SetupAgent)
    agent.config = FakeConfig()
    agent.orchestrator = FakeOrchestrator()
    agent.max_iterations = 3
    agent.console = Console(file=StringIO())
    agent.ui_manager = None
    agent.context_manager = FakeRunTaskContextManager()
    agent.tools = []
    agent.react_engine = FakeReActEngine()
    agent.agent_logger = FakeLogger()
    agent._ensure_container_running = lambda project_name: True
    agent._initialize_context_and_tools = lambda workflow_mode="setup": None
    agent._emit = lambda *args, **kwargs: None
    agent._provide_task_summary = lambda success, task_description: None

    success = agent.run_task(
        "sag-commons-cli",
        "Smoke test only: inspect /workspace/commons-cli and run mvn -version.",
    )

    assert success is True
    assert agent.orchestrator.last_comments == [
        "Task completed: Smoke test only: inspect /workspace/commons-cli and run mvn -version."
    ]
    assert agent.react_engine.calls[0]["completion_mode"] == "run_task"
    assert "TASK COMPLETE:" in agent.react_engine.calls[0]["initial_prompt"]
    assert "existing setup TODO" in agent.react_engine.calls[0]["initial_prompt"]


def test_agent_state_evaluator_run_task_completion_ignores_setup_todo_workflow():
    evaluator = AgentStateEvaluator(FakeContextManager(), completion_mode="run_task")
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="Apache Maven 3.6.3"),
        ),
        SimpleNamespace(
            step_type=StepType.THOUGHT,
            content="TASK COMPLETE: mvn -version succeeded.",
        ),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=2,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is True
    assert analysis.needs_guidance is False


def test_agent_state_evaluator_run_task_completion_accepts_verified_no_more_action():
    evaluator = AgentStateEvaluator(FakeContextManager(), completion_mode="run_task")
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="Apache Maven 3.6.3"),
        ),
        SimpleNamespace(
            step_type=StepType.THOUGHT,
            content=(
                "The Maven version diagnostic is already verified successfully with "
                "a zero exit code, so no further terminal action is needed."
            ),
        ),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=3,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is True
    assert analysis.needs_guidance is False


def test_agent_state_evaluator_run_task_completion_accepts_bare_task_complete():
    evaluator = AgentStateEvaluator(FakeContextManager(), completion_mode="run_task")
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="Apache Maven 3.6.3"),
        ),
        SimpleNamespace(step_type=StepType.THOUGHT, content="TASK COMPLETE"),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=3,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is True


def test_agent_state_evaluator_run_task_completion_rejects_negated_task_complete():
    evaluator = AgentStateEvaluator(FakeContextManager(), completion_mode="run_task")
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="partial output"),
        ),
        SimpleNamespace(
            step_type=StepType.THOUGHT,
            content="The task complete condition is not met yet.",
        ),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=3,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is False


def test_agent_state_evaluator_run_task_completion_rejects_negated_verification():
    evaluator = AgentStateEvaluator(FakeContextManager(), completion_mode="run_task")
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="partial output"),
        ),
        SimpleNamespace(
            step_type=StepType.THOUGHT,
            content="The diagnostic is not verified successfully, so more action is needed.",
        ),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=3,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is False


def test_agent_state_evaluator_setup_mode_keeps_setup_todo_workflow_guard():
    evaluator = AgentStateEvaluator(FakeContextManager())
    steps = [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="bash",
            tool_result=ToolResult.completed_success(output="Apache Maven 3.6.3"),
        ),
        SimpleNamespace(
            step_type=StepType.THOUGHT,
            content="TASK COMPLETE: mvn -version succeeded.",
        ),
    ]

    analysis = evaluator.evaluate(
        steps=steps,
        current_iteration=2,
        recent_tool_executions=[],
        steps_since_context_switch=1,
    )

    assert analysis.is_task_complete is False
    assert analysis.needs_guidance is True
