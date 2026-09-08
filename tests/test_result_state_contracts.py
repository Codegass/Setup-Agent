from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

import sag.agent.agent as agent_module
import sag.agent.error_logger as error_logger_module
import sag.runtime.env_overlay as env_overlay_module
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
        self.survey_status = "present"
        self.recent_tool_executions = ["previous-command-result"]
        self.tool_orchestrator_history = self.recent_tool_executions
        self.history_at_call = None
        self.shared_history_at_call = False

    def _ensure_project_facts(self):
        return self.survey_status

    def run_react_loop(self, **kwargs):
        self.history_at_call = list(self.recent_tool_executions)
        self.shared_history_at_call = self.recent_tool_executions is self.tool_orchestrator_history
        self.tool_orchestrator_history.append("current-command-result")
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
    agent.agent_logger = FakeLogger()
    agent._ensure_container_running = lambda project_name: True

    def initialize(workflow_mode="setup"):
        assert workflow_mode == "run_task"
        agent.context_manager = FakeRunTaskContextManager()
        agent.tools = []
        agent.react_engine = FakeReActEngine()

    agent._initialize_context_and_tools = initialize
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
    assert agent.react_engine.history_at_call == []
    assert agent.react_engine.shared_history_at_call is True
    assert agent.react_engine.recent_tool_executions == ["current-command-result"]


def test_run_task_does_not_start_model_when_framework_survey_fails(monkeypatch):
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
    agent.agent_logger = FakeLogger()
    agent._ensure_container_running = lambda project_name: True

    def initialize(workflow_mode="setup"):
        assert workflow_mode == "run_task"
        agent.context_manager = FakeRunTaskContextManager()
        agent.tools = []
        agent.react_engine = FakeReActEngine()
        agent.react_engine.survey_status = "failed"

    agent._initialize_context_and_tools = initialize
    agent._emit = lambda *args, **kwargs: None
    agent._provide_task_summary = lambda success, task_description: None

    success = agent.run_task("sag-demo", "run the requested verification")

    assert success is False
    assert agent.react_engine.calls == []
    assert agent.orchestrator.last_comments == []


def test_run_task_bootstraps_overlay_after_authority_before_context_io(monkeypatch):
    events = []

    class FakeOverlayStore:
        def __init__(self, orchestrator):
            assert orchestrator is agent.orchestrator

        def bootstrap_current_run(self):
            events.append("overlay")
            return "reset"

    class OrderedContextManager:
        def __init__(self, **kwargs):
            events.append("context")

    class OrderedEngine:
        def __init__(self, **kwargs):
            events.append("engine")

    monkeypatch.setattr(env_overlay_module, "EnvOverlayStore", FakeOverlayStore)
    monkeypatch.setattr(agent_module, "ContextManager", OrderedContextManager)
    monkeypatch.setattr(agent_module, "ReActEngine", OrderedEngine)
    monkeypatch.setattr(
        error_logger_module.ErrorLogger,
        "get_instance",
        lambda **kwargs: SimpleNamespace(),
    )

    agent = SetupAgent.__new__(SetupAgent)
    agent.run_id = "run-overlay-order"
    agent.run_evidence_state = None
    agent.context_manager = None
    agent.config = SimpleNamespace(workspace_path="/workspace", ui_mode=False)
    agent.orchestrator = SimpleNamespace()
    agent.agent_logger = FakeLogger()
    agent.phase_machine = None
    agent.context_journal = None
    agent.verdict_finalizer = None
    agent.control_event_sink = None
    agent.ui_manager = None
    agent._initialize_control_recording = lambda: events.append("authority")
    agent._initialize_tools = lambda workflow_mode: events.append("tools") or []
    agent._bind_advisor_consult = lambda: None
    agent._initialize_run_pin_template = lambda: None

    agent._initialize_context_and_tools(workflow_mode="run_task")

    assert events == ["authority", "overlay", "context", "tools", "engine"]


def test_overlay_bootstrap_failure_prevents_context_io(monkeypatch):
    events = []

    class FailedOverlayStore:
        def __init__(self, orchestrator):
            pass

        def bootstrap_current_run(self):
            events.append("overlay")
            raise RuntimeError("overlay publication failed")

    monkeypatch.setattr(env_overlay_module, "EnvOverlayStore", FailedOverlayStore)
    monkeypatch.setattr(
        error_logger_module.ErrorLogger,
        "get_instance",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        agent_module,
        "ContextManager",
        lambda **kwargs: events.append("context"),
    )

    agent = SetupAgent.__new__(SetupAgent)
    agent.run_id = "run-overlay-failure"
    agent.run_evidence_state = None
    agent.context_manager = None
    agent.config = SimpleNamespace(workspace_path="/workspace", ui_mode=False)
    agent.orchestrator = SimpleNamespace()
    agent.agent_logger = FakeLogger()
    agent._initialize_control_recording = lambda: events.append("authority")

    with pytest.raises(RuntimeError, match="overlay publication failed"):
        agent._initialize_context_and_tools(workflow_mode="run_task")

    assert events == ["authority", "overlay"]


# Plan 2 Task 8: old protocol removed
