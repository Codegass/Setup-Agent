# tests/test_gradle_unzip_recovery.py
"""Gradle wrapper unzip prerequisite: install once, retry once (spec §3.4-5;
2026-07-24 bigtop: gradlew line 180 unzip: command not found, no recovery)."""

from types import SimpleNamespace

from sag.agent.tool_recovery import ToolRecoveryHandler
from sag.tools.base import ToolResult

UNZIP_ERROR = "/workspace/bigtop/gradlew: line 180: unzip: command not found"


class RecordingOrch:
    def __init__(self):
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        return {"success": True, "exit_code": 0, "output": "ok"}


class FakeGradle:
    def __init__(self):
        self.calls = []

    def safe_execute(self, **params):
        self.calls.append(params)
        return ToolResult.completed_success(output="BUILD SUCCESSFUL")


def _recovery(orch, gradle, states=None):
    return ToolRecoveryHandler(
        tools={"gradle": gradle},
        context_manager=SimpleNamespace(orchestrator=orch),
        successful_states=states if states is not None else {},
        repository_url=None,
        add_system_guidance=lambda *a, **k: None,
    )


def _failed():
    return ToolResult.completed_failure(
        output=UNZIP_ERROR, error=UNZIP_ERROR, error_code="GRADLE_BUILD_FAILED"
    )


def test_unzip_missing_installs_and_retries_once():
    orch, gradle = RecordingOrch(), FakeGradle()
    decision = _recovery(orch, gradle)._recover_gradle_error(
        {"tasks": "test", "working_directory": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark"},
        _failed(),
    )
    assert decision.should_recover is True
    assert any("apt-get install -y unzip" in c for c in orch.commands)
    assert len(gradle.calls) == 1


def test_unzip_install_is_one_shot():
    orch, gradle = RecordingOrch(), FakeGradle()
    states = {"gradle_unzip_installed": True}
    decision = _recovery(orch, gradle, states)._recover_gradle_error(
        {"tasks": "test", "working_directory": "/workspace/bigtop"}, _failed()
    )
    assert decision.should_recover is False
    assert orch.commands == []
