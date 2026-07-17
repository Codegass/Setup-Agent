from sag.agent.current_plan import CurrentPlan, PlanStep
from sag.agent.reasoning_scheduler import (
    ReasoningScheduler,
    ReasoningTrigger,
    SchedulerMode,
)


def _step(number, *, tool="bash", params=None, preconditions=()):
    return PlanStep(
        tool=tool,
        exact_params=params or {"command": f"step-{number}"},
        preconditions=preconditions,
        expected_evidence=(f"step {number} result",),
        success_criteria=(f"step {number} succeeds",),
    )


def _success(**overrides):
    result = {
        "invocation_status": "completed",
        "operation_outcome": "success",
        "evidence_assessment": "success",
        "evidence_status": "verified",
        "succeeded": True,
        "output": "ok",
        "output_ref": None,
        "poll_ref": None,
        "metadata": {},
    }
    result.update(overrides)
    return result


def _failure(**overrides):
    result = {
        "invocation_status": "completed",
        "operation_outcome": "failed",
        "evidence_assessment": "blocked",
        "evidence_status": "verified",
        "succeeded": False,
        "output": "failed",
        "output_ref": "output_failure",
        "poll_ref": None,
        "metadata": {},
    }
    result.update(overrides)
    return result


def _take_action(scheduler):
    turn = scheduler.next_turn()
    assert turn.mode is SchedulerMode.ACTION
    assert turn.step is not None
    return turn.step


def test_six_successful_actions_need_exactly_initial_and_heartbeat_thinks():
    scheduler = ReasoningScheduler(available_tools={"bash"}, heartbeat_actions=5)
    plan = CurrentPlan(steps=tuple(_step(number) for number in range(1, 7)))

    first = scheduler.next_turn()
    assert first.mode is SchedulerMode.THINK
    assert first.reasons == (ReasoningTrigger.INITIAL,)
    scheduler.accept_plan(plan)

    observed_commands = []
    for number in range(1, 7):
        turn = scheduler.next_turn()
        if turn.mode is SchedulerMode.THINK:
            assert number == 6
            assert turn.reasons == (ReasoningTrigger.HEARTBEAT,)
            # A heartbeat may confirm that the already executable suffix is
            # still valid; it must not rewind or silently discard it.
            scheduler.resume_plan_after_thinking()
            turn = scheduler.next_turn()
        assert turn.mode is SchedulerMode.ACTION
        observed_commands.append(turn.step.exact_params["command"])
        scheduler.observe_result(_success())

    assert observed_commands == [f"step-{number}" for number in range(1, 7)]
    assert scheduler.thinking_turns == 2
    assert scheduler.action_turns == 6


def test_failure_at_step_three_coalesces_simultaneous_triggers_before_step_four():
    scheduler = ReasoningScheduler(available_tools={"bash"})
    scheduler.next_turn()
    scheduler.accept_plan(CurrentPlan(steps=tuple(_step(number) for number in range(1, 7))))

    for _ in range(2):
        _take_action(scheduler)
        scheduler.observe_result(_success())

    _take_action(scheduler)
    scheduler.observe_result(_failure())
    scheduler.request_reasoning(ReasoningTrigger.GATE_REJECTION)
    scheduler.request_reasoning(ReasoningTrigger.LOOP_BREAKER)

    replanning = scheduler.next_turn()
    assert replanning.mode is SchedulerMode.THINK
    assert set(replanning.reasons) == {
        ReasoningTrigger.OBSERVATION_FAILURE,
        ReasoningTrigger.GATE_REJECTION,
        ReasoningTrigger.LOOP_BREAKER,
    }
    assert scheduler.thinking_turns == 2

    scheduler.accept_plan(CurrentPlan(steps=(_step(4), _step(5), _step(6))))
    step_four = _take_action(scheduler)
    assert step_four.exact_params == {"command": "step-4"}


def test_pending_unknown_dispatch_continues_to_planned_poll_without_thinking():
    scheduler = ReasoningScheduler(available_tools={"build", "search"})
    scheduler.next_turn()
    scheduler.accept_plan(
        CurrentPlan(
            steps=(
                _step(1, tool="build", params={"action": "test"}),
                _step(
                    2,
                    tool="search",
                    params={"target": "{{step_1.poll_ref}}"},
                    preconditions=("{{step_1.poll_ref}}",),
                ),
            )
        )
    )

    dispatch = _take_action(scheduler)
    assert dispatch.tool == "build"
    scheduler.observe_result(
        {
            "invocation_status": "pending",
            "operation_outcome": "unknown",
            "evidence_assessment": "unknown",
            "evidence_status": "unknown",
            "succeeded": False,
            "output": "still running",
            "output_ref": None,
            "poll_ref": "job:build-1",
            "metadata": {"dispatch_status": "running_detached"},
        }
    )

    poll = scheduler.next_turn()
    assert poll.mode is SchedulerMode.ACTION
    assert poll.step.tool == "search"
    assert poll.step.exact_params == {"target": "job:build-1"}
    assert scheduler.thinking_turns == 1


def test_terminal_poll_success_or_failure_requests_reasoning():
    for terminal in (_success(poll_ref="job:one"), _failure(poll_ref="job:one")):
        scheduler = ReasoningScheduler(available_tools={"search"})
        scheduler.next_turn()
        scheduler.accept_plan(
            CurrentPlan(
                steps=(
                    _step(1, tool="search", params={"target": "job:one"}),
                    _step(2, tool="search", params={"target": "output:next"}),
                )
            )
        )
        _take_action(scheduler)
        scheduler.observe_result(terminal, is_poll=True)

        turn = scheduler.next_turn()
        assert turn.mode is SchedulerMode.THINK
        assert ReasoningTrigger.TERMINAL_POLL in turn.reasons


def test_partial_conflict_and_completed_unknown_each_request_reasoning():
    observations = (
        _success(operation_outcome="partial", evidence_assessment="partial", succeeded=False),
        _success(evidence_assessment="conflict"),
        _success(
            operation_outcome="unknown",
            evidence_assessment="unknown",
            succeeded=False,
        ),
    )
    expected = (
        ReasoningTrigger.OBSERVATION_PARTIAL,
        ReasoningTrigger.OBSERVATION_CONFLICT,
        ReasoningTrigger.OBSERVATION_UNKNOWN,
    )

    for observation, trigger in zip(observations, expected, strict=True):
        scheduler = ReasoningScheduler(available_tools={"bash"})
        scheduler.next_turn()
        scheduler.accept_plan(CurrentPlan(steps=(_step(1), _step(2))))
        _take_action(scheduler)
        scheduler.observe_result(observation)
        assert trigger in scheduler.next_turn().reasons


def test_unknown_tool_precondition_fault_and_actor_mismatch_never_yield_action():
    unknown = ReasoningScheduler(available_tools={"bash"})
    unknown.next_turn()
    unknown.accept_plan(CurrentPlan(steps=(_step(1, tool="invented"),)))
    turn = unknown.next_turn()
    assert turn.mode is SchedulerMode.THINK
    assert turn.step is None
    assert turn.fault is not None
    assert ReasoningTrigger.PLAN_FAULT in turn.reasons

    unmet = ReasoningScheduler(available_tools={"bash"})
    unmet.next_turn()
    unmet.accept_plan(
        CurrentPlan(
            steps=(
                _step(1),
                _step(2, preconditions=("{{step_1.succeeded}}",)),
            )
        )
    )
    _take_action(unmet)
    unmet.observe_result(_failure())
    # Explicitly retain the suffix to prove its false precondition is still
    # checked before an actor turn.
    unmet.next_turn()
    unmet.resume_plan_after_thinking()
    fault_turn = unmet.next_turn()
    assert fault_turn.mode is SchedulerMode.THINK
    assert fault_turn.step is None

    mismatch = ReasoningScheduler(available_tools={"bash"})
    mismatch.next_turn()
    mismatch.accept_plan(CurrentPlan(steps=(_step(1),)))
    _take_action(mismatch)
    assert mismatch.validate_actor_action("bash", {"command": "guessed"}) is False
    mismatch_turn = mismatch.next_turn()
    assert mismatch_turn.mode is SchedulerMode.THINK
    assert mismatch_turn.step is None
    assert ReasoningTrigger.ACTOR_MISMATCH in mismatch_turn.reasons


def test_phase_change_rejects_old_plan_and_heartbeat_is_configurable():
    scheduler = ReasoningScheduler(available_tools={"bash"}, heartbeat_actions=None)
    scheduler.next_turn()
    scheduler.accept_plan(CurrentPlan(steps=tuple(_step(number) for number in range(1, 7))))
    for _ in range(5):
        _take_action(scheduler)
        scheduler.observe_result(_success())
    assert scheduler.next_turn().mode is SchedulerMode.ACTION

    scheduler.request_reasoning(ReasoningTrigger.PHASE_CHANGE)
    phase_turn = scheduler.next_turn()
    assert phase_turn.mode is SchedulerMode.THINK
    assert phase_turn.reasons == (ReasoningTrigger.PHASE_CHANGE,)
    scheduler.reject_plan("bad JSON")
    retry = scheduler.next_turn()
    assert retry.mode is SchedulerMode.THINK
    assert retry.reasons == (ReasoningTrigger.MALFORMED_PLAN,)
