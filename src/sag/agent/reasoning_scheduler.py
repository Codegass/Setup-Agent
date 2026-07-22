"""Reasoning-on-demand scheduler for executable ReAct plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .current_plan import (
    CurrentPlan,
    ExecutablePlanStep,
    PlanFault,
    PlanFaultCode,
)


class SchedulerMode(str, Enum):
    THINK = "think"
    ACTION = "action"


class ReasoningTrigger(str, Enum):
    INITIAL = "initial"
    OBSERVATION_FAILURE = "observation_failure"
    OBSERVATION_PARTIAL = "observation_partial"
    OBSERVATION_CONFLICT = "observation_conflict"
    OBSERVATION_UNKNOWN = "observation_unknown"
    TERMINAL_POLL = "terminal_poll"
    GATE_REJECTION = "gate_rejection"
    PHASE_CHANGE = "phase_change"
    LOOP_BREAKER = "loop_breaker"
    PLAN_EXHAUSTED = "plan_exhausted"
    PLAN_FAULT = "plan_fault"
    MALFORMED_PLAN = "malformed_plan"
    ACTOR_MISMATCH = "actor_mismatch"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True, slots=True)
class SchedulerTurn:
    mode: SchedulerMode
    reasons: tuple[ReasoningTrigger, ...] = ()
    step: ExecutablePlanStep | None = None
    fault: PlanFault | None = None

    @property
    def should_think(self) -> bool:
        return self.mode is SchedulerMode.THINK


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class ReasoningScheduler:
    """Own the deterministic think/action transition table.

    Triggers are accumulated in insertion order and consumed by one thinking
    turn.  No action is returned until its tool, placeholders, and executable
    preconditions have been checked by :class:`CurrentPlan`.
    """

    _INVALIDATING_TRIGGERS = frozenset(
        {
            ReasoningTrigger.OBSERVATION_FAILURE,
            ReasoningTrigger.OBSERVATION_PARTIAL,
            ReasoningTrigger.OBSERVATION_CONFLICT,
            ReasoningTrigger.OBSERVATION_UNKNOWN,
            ReasoningTrigger.TERMINAL_POLL,
            ReasoningTrigger.GATE_REJECTION,
            ReasoningTrigger.PHASE_CHANGE,
            ReasoningTrigger.LOOP_BREAKER,
            ReasoningTrigger.PLAN_EXHAUSTED,
            ReasoningTrigger.PLAN_FAULT,
            ReasoningTrigger.MALFORMED_PLAN,
            ReasoningTrigger.ACTOR_MISMATCH,
        }
    )

    def __init__(
        self,
        *,
        available_tools: Sequence[str] | set[str] | Mapping[str, Any],
        heartbeat_actions: int | None = 5,
    ) -> None:
        if heartbeat_actions is not None and heartbeat_actions < 1:
            raise ValueError("heartbeat_actions must be positive or None")
        self.available_tools = frozenset(available_tools)
        self.heartbeat_actions = heartbeat_actions
        self.current_plan: CurrentPlan | None = None
        self.next_step_index = 0
        self.prior_results: dict[str, dict[str, Any]] = {}
        self.thinking_turns = 0
        self.action_turns = 0
        self.actions_since_thinking = 0

        self._pending_reasons: dict[ReasoningTrigger, None] = {ReasoningTrigger.INITIAL: None}
        self._awaiting_plan = False
        self._plan_invalidated = True
        self._active_step: ExecutablePlanStep | None = None
        self._last_fault: PlanFault | None = None

    @property
    def awaiting_plan(self) -> bool:
        return self._awaiting_plan

    @property
    def active_step(self) -> ExecutablePlanStep | None:
        return self._active_step

    def request_reasoning(self, trigger: ReasoningTrigger | str) -> None:
        reason = ReasoningTrigger(trigger)
        self._pending_reasons.setdefault(reason, None)
        if reason in self._INVALIDATING_TRIGGERS:
            self._plan_invalidated = True

    # Compatibility with the vocabulary used by LoopMemory and the engine.
    request_thinking = request_reasoning

    def next_turn(self) -> SchedulerTurn:
        if self._pending_reasons:
            return self._thinking_turn()

        if self._awaiting_plan:
            fault = PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "the previous thinking turn did not provide an executable CURRENT_PLAN",
            )
            self._last_fault = fault
            self.request_reasoning(ReasoningTrigger.MALFORMED_PLAN)
            return self._thinking_turn()

        if self._active_step is not None:
            raise RuntimeError("an action result must be observed before scheduling another turn")

        if self.current_plan is None or self._plan_invalidated:
            fault = self._last_fault or PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "no valid current plan is available",
            )
            self._last_fault = fault
            self.request_reasoning(ReasoningTrigger.PLAN_FAULT)
            return self._thinking_turn()

        if self.next_step_index >= len(self.current_plan.steps):
            fault = PlanFault(
                PlanFaultCode.PLAN_EXHAUSTED,
                "current plan is exhausted",
                step_index=self.next_step_index,
            )
            self._last_fault = fault
            self.request_reasoning(ReasoningTrigger.PLAN_EXHAUSTED)
            return self._thinking_turn()

        try:
            step = self.current_plan.resolve_step(
                self.next_step_index,
                prior_results=self.prior_results,
                available_tools=self.available_tools,
            )
        except PlanFault as fault:
            self._last_fault = fault
            self.request_reasoning(ReasoningTrigger.PLAN_FAULT)
            return self._thinking_turn()

        self._active_step = step
        self.action_turns += 1
        self.actions_since_thinking += 1
        return SchedulerTurn(mode=SchedulerMode.ACTION, step=step)

    def _thinking_turn(self) -> SchedulerTurn:
        reasons = tuple(self._pending_reasons)
        self._pending_reasons.clear()
        self._awaiting_plan = True
        self.thinking_turns += 1
        self.actions_since_thinking = 0
        return SchedulerTurn(
            mode=SchedulerMode.THINK,
            reasons=reasons,
            fault=self._last_fault,
        )

    def accept_plan(self, plan: CurrentPlan) -> None:
        if not isinstance(plan, CurrentPlan):
            raise TypeError("accept_plan requires CurrentPlan")
        self.current_plan = plan
        self.next_step_index = 0
        self.prior_results = {}
        self._active_step = None
        self._awaiting_plan = False
        self._plan_invalidated = False
        self._last_fault = None

    def accept_thinking_response(self, response: str) -> bool:
        try:
            plan = CurrentPlan.from_thinking_response(response)
        except PlanFault as fault:
            self.reject_plan(fault)
            return False
        self.accept_plan(plan)
        return True

    def reject_plan(self, fault: PlanFault | Exception | str) -> None:
        if isinstance(fault, PlanFault):
            plan_fault = fault
        else:
            plan_fault = PlanFault(PlanFaultCode.MALFORMED_PLAN, str(fault))
        self._last_fault = plan_fault
        self._awaiting_plan = False
        self._plan_invalidated = True
        self._active_step = None
        self.request_reasoning(ReasoningTrigger.MALFORMED_PLAN)

    def resume_plan_after_thinking(self) -> None:
        """Explicitly confirm the unexecuted suffix after a heartbeat/review.

        Production callers normally install the fresh plan emitted by the
        thinking model.  This explicit operation also supports a heartbeat
        confirming that the existing executable suffix remains unchanged.
        """
        if not self._awaiting_plan:
            raise RuntimeError("no thinking turn is awaiting plan confirmation")
        if self.current_plan is None:
            raise RuntimeError("there is no plan suffix to resume")
        self._awaiting_plan = False
        self._plan_invalidated = False
        self._last_fault = None

    def validate_actor_action(self, tool: str | None, params: Mapping[str, Any] | None) -> bool:
        expected = self._active_step
        actual_params = dict(params or {})
        if (
            expected is not None
            and tool == expected.tool
            and actual_params == expected.exact_params
        ):
            return True

        if expected is None:
            message = "actor attempted an action without a scheduled executable step"
        else:
            message = (
                "actor action does not exactly match the scheduled step: "
                f"expected {expected.tool} {expected.exact_params!r}, "
                f"received {tool!r} {actual_params!r}"
            )
        self._last_fault = PlanFault(
            PlanFaultCode.ACTOR_MISMATCH,
            message,
            step_index=expected.plan_index if expected is not None else None,
        )
        self._active_step = None
        self.request_reasoning(ReasoningTrigger.ACTOR_MISMATCH)
        return False

    def observe_result(self, result: Any, *, is_poll: bool | None = None) -> None:
        step = self._active_step
        if step is None:
            raise RuntimeError("cannot observe a result without an active planned action")

        snapshot = self._result_snapshot(result)
        self.prior_results[f"step_{step.plan_index + 1}"] = snapshot
        self.next_step_index = step.plan_index + 1
        self._active_step = None

        invocation = snapshot["invocation_status"]
        outcome = snapshot["operation_outcome"]
        assessment = snapshot["evidence_assessment"]

        # A detached handoff is not a terminal UNKNOWN.  Its stable poll_ref is
        # data for the next planned action and takes precedence over heartbeat.
        if invocation == "pending" and outcome == "unknown":
            return

        terminal_poll = self._is_terminal_poll(step, snapshot) if is_poll is None else is_poll
        if terminal_poll:
            self.request_reasoning(ReasoningTrigger.TERMINAL_POLL)
            return

        if outcome == "failed" or invocation in {"crashed", "timeout"}:
            self.request_reasoning(ReasoningTrigger.OBSERVATION_FAILURE)
        elif outcome == "partial" or assessment == "partial":
            self.request_reasoning(ReasoningTrigger.OBSERVATION_PARTIAL)
        elif assessment == "conflict" or bool(snapshot["conflicts"]):
            self.request_reasoning(ReasoningTrigger.OBSERVATION_CONFLICT)
        elif outcome in {"unknown", "skipped"} or assessment == "unknown":
            self.request_reasoning(ReasoningTrigger.OBSERVATION_UNKNOWN)
        elif self.current_plan is None or self.next_step_index >= len(self.current_plan.steps):
            self.request_reasoning(ReasoningTrigger.PLAN_EXHAUSTED)
        elif (
            self.heartbeat_actions is not None
            and self.actions_since_thinking >= self.heartbeat_actions
        ):
            self.request_reasoning(ReasoningTrigger.HEARTBEAT)

    # Alternate name that reads naturally in deterministic replay fixtures.
    observe_action = observe_result

    @staticmethod
    def _result_snapshot(result: Any) -> dict[str, Any]:
        invocation = _enum_value(_read(result, "invocation_status", "completed"))
        outcome = _enum_value(_read(result, "operation_outcome", "unknown"))
        assessment = _enum_value(_read(result, "evidence_assessment", "unknown"))
        evidence_status = _enum_value(_read(result, "evidence_status", "unknown"))
        succeeded = _read(result, "succeeded", None)
        if succeeded is None:
            succeeded = invocation == "completed" and outcome == "success"
        return {
            "succeeded": bool(succeeded),
            "invocation_status": str(invocation),
            "operation_outcome": str(outcome),
            "evidence_assessment": str(assessment),
            "evidence_status": str(evidence_status),
            "output": _read(result, "output", ""),
            "output_ref": _read(result, "output_ref"),
            "poll_ref": _read(result, "poll_ref"),
            "metadata": dict(_read(result, "metadata", {}) or {}),
            "facts": dict(_read(result, "facts", {}) or {}),
            "refs": list(_read(result, "refs", []) or []),
            "evidence_refs": list(_read(result, "evidence_refs", []) or []),
            "conflicts": list(_read(result, "conflicts", []) or []),
        }

    @staticmethod
    def _is_terminal_poll(
        step: ExecutablePlanStep,
        snapshot: Mapping[str, Any],
    ) -> bool:
        if snapshot["invocation_status"] == "pending":
            return False
        if snapshot.get("poll_ref"):
            return True
        target = step.exact_params.get("target")
        if step.tool == "search" and isinstance(target, str) and target.startswith("job:"):
            return True
        command = step.exact_params.get("command")
        return isinstance(command, str) and "/tmp/sag_jobs/" in command


__all__ = [
    "ReasoningScheduler",
    "ReasoningTrigger",
    "SchedulerMode",
    "SchedulerTurn",
]
