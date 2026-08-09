import pytest
from pydantic import ValidationError

import sag.web.task_runner as task_runner_module
from sag.web.task_runner import AgentTaskLauncher, TaskRequest, TaskRunner


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def run(self, workspace_id: str, task: str, source_session: str | None):
        self.calls.append((workspace_id, task, source_session))
        return "RUN-1"


class FakeSessionStore:
    def __init__(self):
        self.started = []
        self.finished = []

    def mark_started(self, *, workspace_id, session_id, task, source_session):
        self.started.append(
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "task": task,
                "source_session": source_session,
            }
        )

    def mark_finished(self, *, workspace_id, session_id, success, outcome, evidence_records=None):
        self.finished.append(
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "success": success,
                "outcome": outcome,
                "evidence_records": evidence_records,
            }
        )


def test_task_runner_creates_new_session_from_workspace_task():
    launcher = FakeLauncher()
    runner = TaskRunner(launcher=launcher)

    response = runner.submit(
        "sag-commons-cli",
        TaskRequest(task="Run formatter tests", source_session="CC-3"),
    )

    assert response["session_id"] == "RUN-1"
    assert launcher.calls == [("sag-commons-cli", "Run formatter tests", "CC-3")]


def test_task_request_rejects_blank_task():
    with pytest.raises(ValidationError) as exc_info:
        TaskRequest(task="")

    assert exc_info.value.errors()[0]["loc"] == ("task",)


def test_task_request_rejects_whitespace_only_task():
    with pytest.raises(ValidationError) as exc_info:
        TaskRequest(task="   ")

    assert exc_info.value.errors()[0]["loc"] == ("task",)


def test_task_request_strips_task_whitespace():
    request = TaskRequest(task="  Run formatter tests  ")

    assert request.task == "Run formatter tests"


def test_task_runner_passes_stripped_task_to_launcher():
    launcher = FakeLauncher()
    runner = TaskRunner(launcher=launcher)

    response = runner.submit(
        "sag-commons-cli",
        TaskRequest(task="  Run formatter tests  "),
    )

    assert response["session_id"] == "RUN-1"
    assert launcher.calls == [("sag-commons-cli", "Run formatter tests", None)]


def test_task_runner_queues_task_without_source_session():
    launcher = FakeLauncher()
    runner = TaskRunner(launcher=launcher)

    response = runner.submit(
        "sag-commons-cli",
        TaskRequest(task="Run formatter tests"),
    )

    assert response == {
        "workspace_id": "sag-commons-cli",
        "session_id": "RUN-1",
        "source_session": None,
        "status": "queued",
    }
    assert launcher.calls == [("sag-commons-cli", "Run formatter tests", None)]


def test_task_runner_uses_falsy_injected_launcher(monkeypatch):
    class FalsyLauncher(FakeLauncher):
        def __bool__(self):
            return False

    def unexpected_default_launcher():
        raise AssertionError("TaskRunner should use the injected launcher")

    monkeypatch.setattr(
        task_runner_module,
        "AgentTaskLauncher",
        unexpected_default_launcher,
    )
    launcher = FalsyLauncher()
    runner = TaskRunner(launcher=launcher)

    response = runner.submit("sag-commons-cli", TaskRequest(task="Run formatter tests"))

    assert response["session_id"] == "RUN-1"
    assert launcher.calls == [("sag-commons-cli", "Run formatter tests", None)]


def test_agent_task_launcher_starts_daemon_thread_with_generated_session(monkeypatch):
    starts = []
    captured = {}

    class FakeUuid:
        hex = "1234567890abcdef"

    class FakeThread:
        def __init__(self, *, target, args, daemon, name):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon
            captured["name"] = name

        def start(self):
            starts.append(True)

    monkeypatch.setattr(task_runner_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(task_runner_module, "Thread", FakeThread)
    launcher = AgentTaskLauncher(session_store=FakeSessionStore())
    monkeypatch.setattr(launcher, "_run_agent", lambda *args: None)

    session_id = launcher.run("sag-commons-cli", "Run formatter tests", "CC-3")

    assert session_id == "UI-12345678"
    assert starts == [True]
    assert captured["daemon"] is True
    assert captured["name"] == "sag-ui-task-UI-12345678"
    assert captured["args"] == (
        "UI-12345678",
        "sag-commons-cli",
        "Run formatter tests",
        "CC-3",
    )


def test_agent_task_launcher_records_session_before_starting_thread(monkeypatch):
    starts = []
    store = FakeSessionStore()

    class FakeUuid:
        hex = "1234567890abcdef"

    class FakeThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            starts.append(True)

    monkeypatch.setattr(task_runner_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(task_runner_module, "Thread", FakeThread)
    launcher = AgentTaskLauncher(session_store=store)
    monkeypatch.setattr(launcher, "_run_agent", lambda *args: None)

    session_id = launcher.run("sag-commons-cli", "Run formatter tests", "CC-3")

    assert session_id == "UI-12345678"
    assert starts == [True]
    assert store.started == [
        {
            "workspace_id": "sag-commons-cli",
            "session_id": "UI-12345678",
            "task": "Run formatter tests",
            "source_session": "CC-3",
        }
    ]


def test_agent_task_launcher_marks_session_finished_after_agent_run(monkeypatch):
    store = FakeSessionStore()
    calls = {}

    class FakeDockerOrchestrator:
        def __init__(self, project_name):
            calls["project_name"] = project_name

        def container_exists(self):
            return True

        def is_container_running(self):
            return True

        def execute_command(self, command):
            return {
                "exit_code": 0,
                "output": '{"project_name": "commons-cli"}',
            }

    class FakeSetupAgent:
        def __init__(self, *, config, orchestrator):
            calls["agent_config"] = config
            calls["agent_orchestrator"] = orchestrator

        def run_task(self, *, project_name, task_description):
            calls["run_task"] = (project_name, task_description)
            return True

    monkeypatch.setattr("sag.docker_orch.orch.DockerOrchestrator", FakeDockerOrchestrator)
    monkeypatch.setattr("sag.agent.agent.SetupAgent", FakeSetupAgent)
    monkeypatch.setattr("sag.config.get_config", lambda: "config")
    monkeypatch.setattr(
        "sag.config.ensure_session_logging",
        lambda config, *, force_new=False: calls.setdefault(
            "session_logging_config", (config, force_new)
        ),
        raising=False,
    )

    launcher = AgentTaskLauncher(session_store=store)

    launcher._run_agent("UI-12345678", "sag-commons-cli", "Run formatter tests", None)

    assert calls["project_name"] == "commons-cli"
    assert calls["session_logging_config"] == ("config", True)
    assert calls["run_task"] == ("commons-cli", "Run formatter tests")
    assert store.finished == [
        {
            "workspace_id": "sag-commons-cli",
            "session_id": "UI-12345678",
            "success": True,
            "outcome": "Task completed: Run formatter tests",
            "evidence_records": [],
        }
    ]


def test_agent_task_launcher_requests_fresh_log_session_for_each_run(monkeypatch):
    store = FakeSessionStore()
    logging_calls = []

    class FakeDockerOrchestrator:
        def __init__(self, project_name):
            self.project_name = project_name

        def container_exists(self):
            return True

        def is_container_running(self):
            return True

        def execute_command(self, command):
            return {"exit_code": 0, "output": '{"project_name": "commons-cli"}'}

    class FakeSetupAgent:
        def __init__(self, *, config, orchestrator):
            pass

        def run_task(self, *, project_name, task_description):
            return True

    monkeypatch.setattr("sag.docker_orch.orch.DockerOrchestrator", FakeDockerOrchestrator)
    monkeypatch.setattr("sag.agent.agent.SetupAgent", FakeSetupAgent)
    monkeypatch.setattr("sag.config.get_config", lambda: "config")
    monkeypatch.setattr(
        "sag.config.ensure_session_logging",
        lambda config, *, force_new=False: logging_calls.append((config, force_new)),
        raising=False,
    )

    launcher = AgentTaskLauncher(session_store=store)

    launcher._run_agent("UI-1", "sag-commons-cli", "First task", None)
    launcher._run_agent("UI-2", "sag-commons-cli", "Second task", None)

    assert logging_calls == [("config", True), ("config", True)]


def test_agent_task_launcher_project_name_falls_back_on_bad_metadata():
    class BadMetadataOrchestrator:
        def execute_command(self, command):
            return {"exit_code": 0, "output": "{not-json"}

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(BadMetadataOrchestrator(), fallback="commons-cli")
        == "commons-cli"
    )


@pytest.mark.parametrize("result", [[], None])
def test_agent_task_launcher_project_name_falls_back_on_non_dict_result(result):
    class NonDictResultOrchestrator:
        def execute_command(self, command):
            return result

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(NonDictResultOrchestrator(), fallback="commons-cli")
        == "commons-cli"
    )


def test_agent_task_launcher_project_name_falls_back_on_non_object_metadata():
    class ListMetadataOrchestrator:
        def execute_command(self, command):
            return {"exit_code": 0, "output": "[]"}

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(ListMetadataOrchestrator(), fallback="commons-cli")
        == "commons-cli"
    )


def test_agent_task_launcher_project_name_falls_back_on_blank_metadata_name():
    class BlankNameMetadataOrchestrator:
        def execute_command(self, command):
            return {"exit_code": 0, "output": '{"project_name": "   "}'}

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(BlankNameMetadataOrchestrator(), fallback="commons-cli")
        == "commons-cli"
    )


def test_agent_task_launcher_project_name_falls_back_on_non_string_metadata_name():
    class NumericNameMetadataOrchestrator:
        def execute_command(self, command):
            return {"exit_code": 0, "output": '{"project_name": 123}'}

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(NumericNameMetadataOrchestrator(), fallback="commons-cli")
        == "commons-cli"
    )


def test_agent_task_launcher_project_name_strips_metadata_name():
    class SpacedNameMetadataOrchestrator:
        def execute_command(self, command):
            return {"exit_code": 0, "output": '{"project_name": " commons-cli "}'}

    launcher = AgentTaskLauncher()

    assert (
        launcher._read_project_name(SpacedNameMetadataOrchestrator(), fallback="fallback")
        == "commons-cli"
    )
