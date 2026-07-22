from types import SimpleNamespace

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.loop_memory import LoopMemory
from sag.agent.phase_machine import PhaseMachine
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import (
    ToolCall,
    ToolExecution,
    ToolExecutionRecord,
    ToolOrchestrator,
)
from sag.evidence import InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolResult


class _SearchTool(BaseTool):
    def __init__(self):
        super().__init__("search_files", "search")
        self.calls = 0

    def execute(self, pattern: str) -> ToolResult:
        self.calls += 1
        return ToolResult.completed_success(output=f"result for {pattern}")


def _execution(command: str, result: ToolResult) -> ToolExecution:
    return ToolExecution(
        call=ToolCall(
            name="run_command",
            raw_params={"command": command},
            validated_params={"command": command},
        ),
        result=result,
        status="failure" if result.operation_outcome is OperationOutcome.FAILED else "success",
        raw_params={"command": command},
        validated_params={"command": command},
        executed_params={"command": command},
        attempted_execution=True,
    )


def _engine():
    engine = ReActEngine.__new__(ReActEngine)
    engine.loop_memory = LoopMemory()
    engine.run_evidence_state = RunEvidenceState(run_id="loop-engine")
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.current_iteration = 1
    engine._force_thinking_next = False
    engine._force_thinking_after_success = False
    engine.prompt_builder = SimpleNamespace(invalidate_trunk_cache=lambda: None)
    engine.context_manager = SimpleNamespace()
    engine.guidance = []
    engine._add_system_guidance = lambda message, priority=5: engine.guidance.append(
        (message, priority)
    )
    return engine


def test_fourth_unchanged_failure_records_blocker_and_requests_thinking():
    engine = _engine()

    for index in range(4):
        result = ToolResult.completed_failure(
            output=f"same failure {index}",
            error="build failed",
            error_code="BUILD_FAILED",
            failure_signature="cmake:missing-target",
        )
        decision = engine._apply_tool_execution_loop_effects(
            _execution("cmake --build build", result)
        )

    assert decision.decision == "force_break"
    assert engine._force_thinking_next is True
    assert engine.run_evidence_state.blockers[0].failure_signature.startswith(
        "loop_without_progress:"
    )
    assert engine.guidance

    engine.run_evidence_state.set_fact(
        "build.artifact_progress",
        1,
        evidence_ref="artifact://new",
    )
    progressed = engine._apply_tool_execution_loop_effects(
        _execution("cmake --build build", result)
    )
    assert progressed.decision == "continue"
    assert engine.run_evidence_state.blockers[0].status == "resolved"


def test_orchestrator_never_hard_breaks_distinct_searches_from_tool_count():
    tool = _SearchTool()
    recent = [
        ToolExecutionRecord(
            signature=f"search_files:[('pattern', 'symbol-{index}')]",
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.SUCCESS,
            timestamp=f"ts-{index}",
        )
        for index in range(16)
    ]
    orchestrator = ToolOrchestrator(
        tools={"search_files": tool},
        context_manager=None,
        recent_tool_executions=recent,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(
        ToolCall(name="search_files", raw_params={"pattern": "symbol-16"})
    )

    assert execution.attempted_execution is True
    assert execution.status == "success"
    assert "force_next_task" not in execution.metadata
    assert tool.calls == 1


def test_repeat_after_force_break_closes_attempt_as_failed_through_policy():
    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="provision")
    engine.transition_policy = PhaseTransitionPolicy(repair_guard=engine.loop_memory)
    engine.run_evidence_state.set_fact(
        "provision.workspace_ready",
        True,
        evidence_ref="workspace://ready",
    )
    engine._repair_budgets = lambda: None
    applied = []
    engine._apply_phase_decision = lambda record, route: applied.append((record, route))

    execution = None
    for _ in range(5):
        result = ToolResult.completed_failure(
            output="same provisioning failure",
            error="failed",
            error_code="PROVISION_FAILED",
            failure_signature="provision:same",
        )
        execution = _execution("install required tool", result)
        decision = engine._apply_tool_execution_loop_effects(execution)

    assert decision.close_phase is True
    assert engine._close_phase_for_loop(decision, execution) is True
    record, route = applied[0]
    assert record.outcome.value == "failed"
    assert record.termination.value == "completed"
    assert route.route.kind == "advance"
    assert route.route.target == "analyze"
