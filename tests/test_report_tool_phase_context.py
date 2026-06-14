from sag.agent.context_manager import Task, TrunkContext
from sag.tools.report_tool import ReportTool


class FakeContextManager:
    def __init__(self, trunk):
        self.trunk = trunk
        self.current_task_id = "phase_report"
        self.saved = False

    def load_trunk_context(self):
        return self.trunk

    def _save_trunk_context(self, trunk):
        self.saved = True


def test_final_report_does_not_close_active_phase_branch_before_phase_done():
    trunk = TrunkContext(context_id="t", goal="g", project_url="u", project_name="p")
    trunk.todo_list.append(
        Task(
            id="phase_report",
            description="Generate the final report with the report tool, then phase(action='done').",
        )
    )
    context_manager = FakeContextManager(trunk)
    tool = ReportTool(context_manager=context_manager)

    completed = tool._mark_final_report_task_completed(
        "/workspace/setup-report.md",
        verified_status="success",
    )

    assert completed == "phase_report"
    assert context_manager.current_task_id == "phase_report"
    assert context_manager.saved is True
