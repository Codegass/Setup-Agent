"""build() working_directory defaults to the analyzer's recommended reactor root.

Regression guard for the wiring gap: the analyzer computes build_root/test_root but
it was only surfaced as advisory prose, so a model that omitted working_directory
fell back to a blind /workspace and under-scoped the reactor. The orchestrator now
injects the recommended root when (and only when) the model omits one.
"""

from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.tools.base import BaseTool, ToolResult


class BuildLikeTool(BaseTool):
    def __init__(self):
        super().__init__("build", "Build test tool")
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "working_directory": {"type": "string"},
            },
            "required": [],
        }

    def execute(self, action="compile", working_directory="", **_):
        return ToolResult.completed_success(
            output=working_directory,
            metadata={"working_directory": working_directory, "action": action},
        )


class _Trunk:
    def __init__(self, rec):
        self.environment_summary = {"build_recommendation": rec} if rec else {}


class _CM:
    def __init__(self, rec):
        self._rec = rec

    def load_trunk_context(self):
        return _Trunk(self._rec)


_REC = {
    "build_system": "maven",
    "build_root": "/workspace/proj",
    "test_root": "/workspace/proj/tests-module",
    "test_system": "maven",
}


def _orchestrator(rec):
    return ToolOrchestrator(
        tools={"build": BuildLikeTool()},
        context_manager=_CM(rec),
        recent_tool_executions=[],
        successful_states={"working_directory": "/workspace", "cloned_repos": set()},
        repository_url=None,
        track_tool_execution=lambda signature, success: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )


def _workdir(execution):
    return execution.executed_params["working_directory"]


def test_build_defaults_to_recommended_build_root():
    orch = _orchestrator(_REC)
    execution = orch.execute(ToolCall(name="build", raw_params={"action": "compile"}))
    assert _workdir(execution) == "/workspace/proj"


def test_test_defaults_to_recommended_test_root():
    orch = _orchestrator(_REC)
    execution = orch.execute(ToolCall(name="build", raw_params={"action": "test"}))
    assert _workdir(execution) == "/workspace/proj/tests-module"


def test_explicit_working_directory_is_respected():
    orch = _orchestrator(_REC)
    execution = orch.execute(
        ToolCall(
            name="build", raw_params={"action": "test", "working_directory": "/workspace/other"}
        )
    )
    assert _workdir(execution) == "/workspace/other"


def test_no_recommendation_falls_back_to_state_default():
    orch = _orchestrator(None)
    execution = orch.execute(ToolCall(name="build", raw_params={"action": "compile"}))
    assert _workdir(execution) == "/workspace"
