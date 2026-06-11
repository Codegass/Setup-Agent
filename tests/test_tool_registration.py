# tests/test_tool_registration.py
"""Stage-1 contract: exactly 6 model-facing tools; legacy names alias cleanly;
evidence gates accept the new build tool."""

from types import SimpleNamespace

from sag.tools.context_tool import ContextTool

EXPECTED_TOOLS = {"bash", "file_io", "build", "project", "search", "manage_context", "report"}
# NOTE: manage_context + report remain in stage 1 (phase machine is stage 2;
# report render-slim is stage 3) -> 7 registered, 6 after stage 2.


def test_build_action_counts_as_build_evidence():
    history = [{"type": "action", "tool_name": "build", "success": True, "output": "BUILD SUCCESS"}]
    cm = SimpleNamespace(
        current_task_id="task_4",
        load_branch_history=lambda task_id: SimpleNamespace(history=history),
    )
    tool = ContextTool(cm)
    assert tool._has_required_build_or_test_tool_execution() is True


def test_legacy_maven_call_aliases_to_build():
    from sag.agent.tool_parameters import ToolParameterNormalizer

    class FakeBuild:
        def _get_parameters_schema(self):
            return {"type": "object", "properties": {"action": {}, "args": {},
                                                     "working_directory": {}, "timeout": {}}}

    mgr = ToolParameterNormalizer(
        tools={"build": FakeBuild()},
        successful_states={},
        repository_url=None,
        logger=SimpleNamespace(
            warning=lambda *a, **k: None, info=lambda *a, **k: None, error=lambda *a, **k: None,
        ),
    )
    name, params = mgr.resolve_legacy_alias("maven", {"command": "test", "working_directory": "/w"})
    assert name == "build"
    assert params["action"] == "test"
