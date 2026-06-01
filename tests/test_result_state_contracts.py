from types import SimpleNamespace

from sag.agent.agent_state_evaluator import AgentStateAnalysis, AgentStateEvaluator, AgentStatus
from sag.tools.base import ToolResult


class FakeContextManager:
    current_task_id = None

    def load_trunk_context(self):
        return {
            "todo_list": [
                {"id": "task_1", "description": "Clone repository", "status": "pending"}
            ]
        }


def test_tool_result_preserves_declared_raw_data():
    result = ToolResult(
        success=True,
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
