# tests/engine_driver.py
"""Drive ACTION steps through the engine the way the native dispatcher does.

Plan 2 Task 8 deleted `ReActEngine._execute_steps` (the old loop's step-list
executor). The native dispatcher appends each ACTION step to `engine.steps`
and then calls `_execute_action_step(step)`; tests that used to hand a list to
`_execute_steps` use this helper so they exercise the same production path.
"""

from sag.agent.react_types import StepType


def execute_native_like(engine, steps):
    """Stand in for `_execute_native_calls` with pre-built ACTION steps.

    Mirrors the dispatcher: every step that actually ran is returned (so phase
    signals still reach `_handle_phase_signals`) and execution stops at the
    first batch-break reason.
    """
    executed = []
    for step in steps:
        engine.steps.append(step)
        executed.append(step)
        if engine._execute_action_step(step) is not None:
            break
    return executed


def execute_action_steps(engine, steps):
    """Append and execute each step, stopping where the dispatcher would.

    Returns the reason the batch stopped (phase transition pending, or the loop
    breaker closed the phase), or None when every step ran.
    """
    for step in steps:
        engine.steps.append(step)
        if getattr(step, "step_type", None) is not StepType.ACTION:
            continue
        stop_reason = engine._execute_action_step(step)
        if stop_reason is not None:
            return stop_reason
    return None
