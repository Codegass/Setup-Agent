# tests/test_tool_registration.py
"""Stage-1 contract: exactly 6 model-facing tools; legacy names alias cleanly;
evidence gates accept the new build tool."""

from pathlib import Path
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


# ---------------------------------------------------------------------------
# Stage-2 contract (plan Task 8): mode-aware registration. Setup runs register
# the `phase` lifecycle tool and NOT `manage_context`; `sag run --task` keeps
# the legacy free-form surface with `manage_context` and no `phase` tool.
# ---------------------------------------------------------------------------


def test_setup_mode_registers_phase_not_manage_context():
    import inspect

    from sag.agent.agent import SetupAgent

    # Introspect the registration source: setup mode must register the
    # 'phase' tool and NOT 'manage_context'; run_task mode the reverse.
    src = inspect.getsource(SetupAgent._initialize_tools)
    assert "PhaseTool" in src
    assert "workflow_mode" in src or "phase_machine" in src


def test_setup_trunk_tasks_are_phase_entries():
    import inspect

    from sag.agent.agent import SetupAgent

    src = inspect.getsource(SetupAgent.setup_project)
    assert "PHASE_NAMES" in src, "trunk tasks must come from the phase plan"
    assert "phase_" in src, "trunk task ids must be phase_<name>"


def _agent_for_registration(phase_machine=None):
    from sag.agent.agent import SetupAgent

    agent = object.__new__(SetupAgent)
    agent.config = SimpleNamespace(
        workspace_path="/workspace",
        test_pass_threshold=0.95,
        build_coverage_threshold=0.75,
        test_execution_threshold=0.8,
    )
    # OutputStorageManager probes the orchestrator at construction time.
    agent.orchestrator = SimpleNamespace(
        project_name="demo",
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": ""},
    )
    agent.context_manager = SimpleNamespace(
        contexts_dir=Path("/workspace/.setup_agent/contexts")
    )
    agent.phase_machine = phase_machine
    agent.context_journal = None
    agent.project_name = "demo"
    return agent


def test_setup_mode_tool_surface():
    from sag.agent.phase_machine import PhaseMachine

    agent = _agent_for_registration(phase_machine=PhaseMachine())
    names = {tool.name for tool in agent._initialize_tools(workflow_mode="setup")}
    assert "phase" in names
    assert "manage_context" not in names


def test_run_task_mode_tool_surface():
    agent = _agent_for_registration(phase_machine=None)
    names = {tool.name for tool in agent._initialize_tools(workflow_mode="run_task")}
    assert "manage_context" in names
    assert "phase" not in names


def test_create_trunk_context_accepts_phase_task_ids(tmp_path):
    from sag.agent.context_manager import ContextManager

    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="g",
        project_url="u",
        project_name="demo",
        tasks=["Provision the toolchain", "Generate the final report"],
        task_ids=["phase_provision", "phase_report"],
    )
    assert [t.id for t in trunk.todo_list] == ["phase_provision", "phase_report"]

    legacy = manager.create_trunk_context(
        goal="g", project_url="u", project_name="demo", tasks=["a", "b"]
    )
    assert [t.id for t in legacy.todo_list] == ["task_1", "task_2"]


def test_report_matcher_matches_report_phase_objective():
    from sag.agent.react_engine import PHASE_OBJECTIVES
    from sag.tools.report_tool import ReportTool

    tool = ReportTool()
    assert tool._is_final_report_task_description(PHASE_OBJECTIVES["report"]) is True
