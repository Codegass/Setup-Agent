from io import StringIO

from rich.console import Console

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent
from sag.tools.base import ToolResult


def test_tool_result_preserves_declared_raw_data():
    result = ToolResult.completed_success(
        output="ok",
        raw_data={"full_report": "report text", "report_snapshot": {"status": "success"}},
    )

    assert result.raw_data["full_report"] == "report text"
    assert result.model_dump()["raw_data"]["report_snapshot"]["status"] == "success"


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


# Plan 2 Task 8: old protocol removed
