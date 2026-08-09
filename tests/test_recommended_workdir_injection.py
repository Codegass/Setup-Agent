"""Tool parameters preserve the model's selected working directory.

Analyzer recommendations and mutable orchestration state are observations, not
authority to rewrite an omitted model parameter.  The normalizer may translate a
submitted cwd alias to the canonical field, but it must not invent a cwd.
"""

import pytest

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
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )


def _workdir(execution):
    return execution.executed_params["working_directory"]


def _assert_omitted_workdir_is_not_injected(execution, raw_params):
    assert execution.call.raw_params == raw_params
    assert execution.call.validated_params == raw_params
    assert execution.executed_params == raw_params
    assert execution.result.metadata["working_directory"] == ""
    assert not any(fix.field == "working_directory" for fix in execution.parameter_fixes)


def test_compile_omitting_workdir_is_not_rewritten_from_analyzer_recommendation():
    orch = _orchestrator(_REC)
    raw_params = {"action": "compile"}

    execution = orch.execute(ToolCall(name="build", raw_params=raw_params))

    _assert_omitted_workdir_is_not_injected(execution, raw_params)


def test_test_omitting_workdir_is_not_rewritten_from_analyzer_recommendation():
    orch = _orchestrator(_REC)
    raw_params = {"action": "test"}

    execution = orch.execute(ToolCall(name="build", raw_params=raw_params))

    _assert_omitted_workdir_is_not_injected(execution, raw_params)


def test_explicit_working_directory_is_respected():
    orch = _orchestrator(_REC)
    execution = orch.execute(
        ToolCall(
            name="build", raw_params={"action": "test", "working_directory": "/workspace/other"}
        )
    )
    assert _workdir(execution) == "/workspace/other"


@pytest.mark.parametrize(
    "alias",
    ["cwd", "workdir", "working_dir", "work_dir", "dir", "directory"],
)
def test_build_workdir_alias_is_normalized_without_analyzer_injection(alias):
    orch = _orchestrator(_REC)

    execution = orch.execute(
        ToolCall(
            name="build",
            raw_params={"action": "deps", alias: "/workspace/tvm"},
        )
    )

    assert execution.status == "success"
    assert execution.executed_params == {
        "action": "deps",
        "working_directory": "/workspace/tvm",
    }
    assert any(
        fix.source == "schema_alias"
        and fix.field == "working_directory"
        and fix.after == "/workspace/tvm"
        for fix in execution.parameter_fixes
    )
    assert not any(
        fix.reason == "analyzer-recommended reactor root (model omitted working_directory)"
        for fix in execution.parameter_fixes
    )


def test_canonical_build_workdir_wins_over_conflicting_alias():
    orch = _orchestrator(_REC)

    execution = orch.execute(
        ToolCall(
            name="build",
            raw_params={
                "action": "test",
                "working_directory": "/workspace/canonical",
                "cwd": "/workspace/alias-that-must-not-win",
            },
        )
    )

    assert execution.status == "success"
    assert _workdir(execution) == "/workspace/canonical"
    assert any(
        fix.source == "schema_alias"
        and fix.field == "cwd"
        and fix.before == "/workspace/alias-that-must-not-win"
        and fix.after is None
        for fix in execution.parameter_fixes
    )


def test_omitted_workdir_is_not_injected_from_orchestration_state():
    orch = _orchestrator(None)
    raw_params = {"action": "compile"}

    execution = orch.execute(ToolCall(name="build", raw_params=raw_params))

    _assert_omitted_workdir_is_not_injected(execution, raw_params)
