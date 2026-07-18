from types import SimpleNamespace

from sag.agent.current_plan import CurrentPlan, ExecutablePlanStep, PlanStep
from sag.agent.react_engine import ReActEngine
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.react_response_parser import ReActResponseParser
from sag.agent.react_types import ReactModelMode, ReActStep, StepType
from sag.agent.reasoning_scheduler import (
    ReasoningScheduler,
    ReasoningTrigger,
    SchedulerMode,
)
from sag.agent.tool_orchestration import ToolCall, ToolExecution
from sag.config.prompt_loader import load_react_engine_prompts
from sag.tools.base import BaseTool, ToolResult


class _SchemaTool(BaseTool):
    def __init__(self, name, properties):
        super().__init__(name, f"{name} scheduler schema tool")
        self._parameter_schema = {
            "type": "object",
            "properties": properties,
            "required": ["action"],
        }

    def execute(self, **params):
        return ToolResult.completed_success(output=str(params))


def _plan_step(command="pwd"):
    return PlanStep(
        tool="bash",
        exact_params={"command": command},
        preconditions=(),
        expected_evidence=("command result",),
        success_criteria=("exit code zero",),
    )


def _plan_response(command="pwd"):
    return f"""THOUGHT: The next operation is deterministic.

CURRENT_PLAN:
{{
  "steps": [
    {{
      "tool": "bash",
      "exact_params": {{"command": "{command}"}},
      "preconditions": [],
      "expected_evidence": ["command result"],
      "success_criteria": ["exit code zero"]
    }}
  ],
  "invalidate_on": ["failure", "conflict", "unknown", "phase_change"]
}}"""


def _scheduled_engine(*, tools=("bash",)):
    engine = ReActEngine.__new__(ReActEngine)
    available_tools = set(tools)
    if isinstance(tools, dict):
        available_tools = set(tools)
        engine.tools = tools
        engine.successful_states = {"working_directory": "/workspace/paramiko"}
        engine.repository_url = None
        engine.repository_ref = None
    engine.reasoning_scheduler = ReasoningScheduler(available_tools=available_tools)
    engine._scheduler_active = True
    engine._scheduled_turn = None
    engine.guidance = []
    engine._add_system_guidance = lambda message, priority=0: engine.guidance.append(
        (message, priority)
    )
    return engine


def test_mode_prompts_require_typed_plan_and_exact_scheduled_action():
    builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=SimpleNamespace(),
        tools={},
    )
    planned = ExecutablePlanStep(
        plan_index=1,
        tool="search",
        exact_params={"target": "output_readme", "query": "JDK"},
        expected_evidence=("matching lines",),
        success_criteria=("requirements identified",),
    )

    thinking = builder.build_mode_prompt(
        "base prompt",
        ReactModelMode.THINKING,
        reasoning_reasons=(ReasoningTrigger.OBSERVATION_CONFLICT,),
        scheduler_fault="evidence disagrees",
    )
    action = builder.build_mode_prompt(
        "base prompt",
        ReactModelMode.ACTION,
        planned_step=planned,
    )

    assert "CURRENT_PLAN:" in thinking
    assert '"exact_params"' in thinking
    assert "explicit prior-step placeholders" in thinking
    assert "observation_conflict" in thinking
    assert "evidence disagrees" in thinking
    assert thinking.endswith("base prompt")

    assert "AUTHORITATIVE EXECUTABLE PLAN STEP" in action
    assert "TOOL: search" in action
    assert 'EXACT_PARAMS: {"query": "JDK", "target": "output_readme"}' in action
    assert "Do not add, remove, repair, or guess parameters" in action
    assert action.endswith("base prompt")


def test_thinking_response_installs_plan_before_any_actor_turn():
    engine = _scheduled_engine()
    parser = ReActResponseParser(lambda: "ts")
    thinking_turn = engine.reasoning_scheduler.next_turn()
    response = _plan_response()
    parsed = parser.parse(response, model_used="thinker", was_thinking_model=True)

    safe_steps = engine._prepare_scheduler_steps(response, parsed, thinking_turn)

    assert [step.step_type for step in safe_steps] == [StepType.THOUGHT]
    assert isinstance(engine.reasoning_scheduler.current_plan, CurrentPlan)
    actor_turn = engine.reasoning_scheduler.next_turn()
    assert actor_turn.mode is SchedulerMode.ACTION
    assert actor_turn.step.exact_params == {"command": "pwd"}


def test_thinking_plan_is_schema_canonical_before_actor_comparison():
    tools = {
        "project": _SchemaTool(
            "project",
            {
                "action": {"type": "string"},
                "project_path": {"type": "string"},
            },
        ),
        "phase": _SchemaTool(
            "phase",
            {
                "action": {"type": "string"},
                "outcome": {"type": "string"},
                "key_results": {"type": "string"},
            },
        ),
    }
    engine = _scheduled_engine(tools=tools)
    parser = ReActResponseParser(lambda: "ts")
    response = """THOUGHT: Execute the schema-valid actions exactly.

CURRENT_PLAN:
{
  "steps": [
    {
      "tool": "project",
      "exact_params": {"action": "analyze", "path": "/workspace/paramiko"},
      "preconditions": [],
      "expected_evidence": ["analysis"],
      "success_criteria": ["analysis completes"]
    },
    {
      "tool": "phase",
      "exact_params": {
        "action": "done",
        "outcome": "partial",
        "key_results": {"passed": 541, "executed": 559}
      },
      "preconditions": [],
      "expected_evidence": ["phase close"],
      "success_criteria": ["phase accepted"]
    }
  ]
}"""
    thinking_turn = engine.reasoning_scheduler.next_turn()

    safe_steps = engine._prepare_scheduler_steps(
        response,
        parser.parse(response, model_used="thinker", was_thinking_model=True),
        thinking_turn,
    )

    assert [step.step_type for step in safe_steps] == [StepType.THOUGHT]
    plan = engine.reasoning_scheduler.current_plan
    assert plan is not None
    assert plan.steps[0].exact_params == {
        "action": "analyze",
        "project_path": "/workspace/paramiko",
    }
    assert plan.steps[1].exact_params["key_results"] == '{"executed":559,"passed":541}'

    actor_turn = engine.reasoning_scheduler.next_turn()
    actor_response = (
        "ACTION: project\nPARAMETERS: "
        '{"action": "analyze", "project_path": "/workspace/paramiko"}'
    )
    safe_actions = engine._prepare_scheduler_steps(
        actor_response,
        parser.parse(actor_response, model_used="actor", was_thinking_model=False),
        actor_turn,
    )

    assert len(safe_actions) == 1
    assert safe_actions[0].tool_params == {
        "action": "analyze",
        "project_path": "/workspace/paramiko",
    }


def test_malformed_plan_and_actor_mismatch_execute_no_tool_step():
    parser = ReActResponseParser(lambda: "ts")
    malformed = _scheduled_engine()
    thinking_turn = malformed.reasoning_scheduler.next_turn()
    response = "THOUGHT: actor can decide the parameters later"
    parsed = parser.parse(response, model_used="thinker", was_thinking_model=True)

    safe_steps = malformed._prepare_scheduler_steps(response, parsed, thinking_turn)

    assert all(step.step_type is not StepType.ACTION for step in safe_steps)
    retry = malformed.reasoning_scheduler.next_turn()
    assert retry.mode is SchedulerMode.THINK
    assert retry.reasons == (ReasoningTrigger.MALFORMED_PLAN,)

    mismatch = _scheduled_engine()
    first = mismatch.reasoning_scheduler.next_turn()
    mismatch._prepare_scheduler_steps(
        _plan_response(),
        parser.parse(_plan_response(), model_used="thinker", was_thinking_model=True),
        first,
    )
    action_turn = mismatch.reasoning_scheduler.next_turn()
    guessed = parser.parse(
        'ACTION: bash\nPARAMETERS: {"command": "git status"}',
        model_used="actor",
        was_thinking_model=False,
    )

    safe_actions = mismatch._prepare_scheduler_steps(
        'ACTION: bash\nPARAMETERS: {"command": "git status"}',
        guessed,
        action_turn,
    )

    assert safe_actions == []
    assert mismatch.guidance
    correction = mismatch.reasoning_scheduler.next_turn()
    assert correction.mode is SchedulerMode.THINK
    assert correction.reasons == (ReasoningTrigger.ACTOR_MISMATCH,)


def test_exact_actor_action_executes_and_success_advances_without_legacy_think(monkeypatch):
    engine = _scheduled_engine()
    scheduler = engine.reasoning_scheduler
    scheduler.next_turn()
    scheduler.accept_plan(CurrentPlan(steps=(_plan_step("pwd"), _plan_step("ls"))))
    scheduled = scheduler.next_turn()
    assert scheduled.mode is SchedulerMode.ACTION

    engine.steps = []
    engine.context_manager = SimpleNamespace(current_task_id=None)
    engine.current_iteration = 3
    engine.config = SimpleNamespace(verbose=False)
    engine.agent_logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda name: None)
    engine.emit = lambda *args, **kwargs: None
    engine.output_storage = None
    engine.loop_memory = None
    engine.phase_machine = None
    engine._force_thinking_next = False
    engine._force_thinking_after_success = False
    engine._record_tool_execution = lambda *args, **kwargs: args[2]
    engine._add_observation_step = lambda text: engine.steps.append(
        ReActStep(step_type=StepType.OBSERVATION, content=text, timestamp="ts")
    )
    result = ToolResult.completed_success(output="/workspace")
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "pwd"}),
        result=result,
        status="success",
        raw_params={"command": "pwd"},
        validated_params={"command": "pwd"},
        executed_params={"command": "pwd"},
        observation_text="/workspace",
        attempted_execution=True,
    )
    monkeypatch.setattr(
        engine,
        "_get_tool_orchestrator",
        lambda: SimpleNamespace(execute=lambda call: execution),
    )
    actor_step = ReActStep(
        step_type=StepType.ACTION,
        content="Using bash",
        tool_name="bash",
        tool_params={"command": "pwd"},
        timestamp="ts",
        model_used="actor",
    )
    assert scheduler.validate_actor_action(actor_step.tool_name, actor_step.tool_params)

    engine._execute_steps([actor_step])

    assert actor_step.tool_result is result
    assert engine._force_thinking_after_success is False
    assert engine._force_thinking_next is False
    next_turn = scheduler.next_turn()
    assert next_turn.mode is SchedulerMode.ACTION
    assert next_turn.step.exact_params == {"command": "ls"}


def test_engine_scheduler_trigger_bridge_coalesces_phase_gate_and_loop_requests():
    engine = _scheduled_engine()
    scheduler = engine.reasoning_scheduler
    scheduler.next_turn()
    scheduler.accept_plan(CurrentPlan(steps=(_plan_step(),)))

    assert engine._request_scheduler_reasoning(ReasoningTrigger.PHASE_CHANGE)
    assert engine._request_scheduler_reasoning(ReasoningTrigger.GATE_REJECTION)
    assert engine._request_scheduler_reasoning(ReasoningTrigger.LOOP_BREAKER)

    turn = scheduler.next_turn()
    assert turn.mode is SchedulerMode.THINK
    assert turn.reasons == (
        ReasoningTrigger.PHASE_CHANGE,
        ReasoningTrigger.GATE_REJECTION,
        ReasoningTrigger.LOOP_BREAKER,
    )
