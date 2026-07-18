"""Deterministic control-layer replay over production policy components."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult, bind_tool_result_output_storage

from .control_events import (
    CONTROL_EVENT_SCHEMA_VERSION,
    ControlEvent,
    SourceFileManifest,
    action_envelope_sha256,
    canonical_json,
    canonical_sha256,
)
from .current_plan import CurrentPlan
from .evidence_state import EvidenceRole, RunEvidenceState, StateScope
from .loop_memory import LoopDecision, LoopEvent, LoopMemory
from .phase_gates import ValidatorState, validate_phase_claim
from .phase_machine import PhaseAttemptRecord, PhaseClaim, PhaseMachine
from .phase_transitions import (
    PhaseTransitionPolicy,
    RepairBudgets,
    RepairRequest,
)
from .reasoning_scheduler import (
    ReasoningScheduler,
    ReasoningTrigger,
    SchedulerMode,
    SchedulerTurn,
)
from .verdict_finalizer import (
    EvidenceCloseReason,
    RunVerdictSnapshot,
    VerdictFinalizer,
)


class ReplayValidationError(ValueError):
    """The transcript is malformed or impossible under production policy."""


class ReplayMismatchError(ReplayValidationError):
    """A valid transcript no longer reproduces its frozen expectations."""


class InitialFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    value: Any
    evidence_ref: str = Field(min_length=1)
    scope: StateScope | None = None


class ReplayInitialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_phase: Literal["provision", "analyze", "build", "test", "report"]
    heartbeat_actions: int | None = Field(default=5, ge=1)
    available_tools: tuple[str, ...] = Field(min_length=1)
    facts: tuple[InitialFact, ...] = ()
    conflicts: tuple[str, ...] = ()
    repair_global_remaining: int = Field(default=2, ge=0)
    repair_phase_remaining: dict[str, int] = Field(default_factory=lambda: {"test": 1, "build": 1})


class ReplayHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    fixture_kind: Literal["recorded_tool_transcript"]
    probe: Literal["tvm", "bigtop", "paramiko", "cassandra-java-driver"]
    run_id: str = Field(min_length=1)
    source_manifest: tuple[SourceFileManifest, ...] = Field(min_length=1)
    initial_state: ReplayInitialState
    finalized_at: str = Field(min_length=1)
    expected_snapshot: dict[str, Any]
    expected_event_digest: str


class ReplayTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: ReplayHeader
    events: tuple[ControlEvent, ...]

    @classmethod
    def read(cls, path: str | Path) -> "ReplayTranscript":
        transcript_path = Path(path)
        try:
            rows = [
                json.loads(line)
                for line in transcript_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayValidationError(f"cannot read replay transcript: {exc}") from exc
        if not rows:
            raise ReplayValidationError("replay transcript is empty")
        try:
            header = ReplayHeader.model_validate(rows[0])
            events = tuple(ControlEvent.model_validate(row) for row in rows[1:])
        except (TypeError, ValueError, ValidationError) as exc:
            raise ReplayValidationError(str(exc)) from exc
        for expected, event in enumerate(events, 1):
            if event.sequence != expected:
                raise ReplayValidationError(
                    f"event sequence must be monotonic: expected {expected}, got {event.sequence}"
                )
        manifest_paths = {item.path for item in header.source_manifest}
        for event in events:
            if event.source is None:
                raise ReplayValidationError("recorded fixtures require source provenance per event")
            if event.source.path not in manifest_paths:
                raise ReplayValidationError(
                    f"event source {event.source.path!r} is absent from source_manifest"
                )
        return cls(header=header, events=events)


@dataclass(frozen=True)
class RepairRouteResult:
    edge: tuple[str, str]
    accepted: bool
    reason_code: str


@dataclass(frozen=True)
class ReplayResult:
    header: ReplayHeader
    snapshot: RunVerdictSnapshot
    expected_snapshot: dict[str, Any]
    produced_event_digest: str
    expected_event_digest: str
    unconsumed_events: tuple[str, ...]
    phase_records: tuple[PhaseAttemptRecord, ...]
    loop_decisions: tuple[LoopDecision, ...]
    repair_routes: tuple[RepairRouteResult, ...]
    planner_response_count: int
    executed_envelope_count: int
    compatibility_action_model_calls: int = 0

    def phase(self, phase: str) -> PhaseAttemptRecord:
        matches = self.phase_attempts(phase)
        if not matches:
            raise KeyError(phase)
        return matches[-1]

    def phase_attempts(self, phase: str) -> tuple[PhaseAttemptRecord, ...]:
        return tuple(record for record in self.phase_records if record.phase == phase)


class _ReplayOutputStorage:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}

    def register(self, ref: str, value: str = "recorded output") -> None:
        self._refs[ref] = value

    def has_output_ref(self, ref: str) -> bool:
        return ref in self._refs

    def retrieve_output(self, ref: str) -> str | None:
        return self._refs.get(ref)


class _ReplayVerdictOrchestrator:
    """In-memory persistence seam used by the production VerdictFinalizer."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def execute_command(self, command: str) -> dict[str, Any]:
        if command.startswith("mkdir -p "):
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("test -f ") and " && cat " in command:
            path = command.split()[2]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.files[path]}
        if command.startswith("cat > "):
            first, payload = command.split("\n", 1)
            path = first.split()[2]
            delimiter = first.rsplit("<<", 1)[1].strip().strip("'")
            suffix = f"\n{delimiter}"
            content = payload[: -len(suffix)] if payload.endswith(suffix) else payload
            self.files[path] = content + "\n"
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("truncate -s -1 "):
            path = command.split()[-1]
            self.files[path] = self.files[path][:-1]
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("mv "):
            _, source, target = command.split()
            self.files[target] = self.files.pop(source)
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


class ControlReplayRunner:
    """Consume recorded inputs through the shipped scheduler/gates/policy/finalizer."""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], Any] | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        verify_expected: bool = True,
    ) -> None:
        # Factories are deliberate tripwires. Replays never invoke either one.
        self.llm_factory = llm_factory
        self.orchestrator_factory = orchestrator_factory
        self.verify_expected = verify_expected

    @classmethod
    def offline(cls, *, verify_expected: bool = True) -> "ControlReplayRunner":
        def _forbidden() -> Any:
            raise AssertionError("offline replay attempted an external call")

        return cls(
            llm_factory=_forbidden,
            orchestrator_factory=_forbidden,
            verify_expected=verify_expected,
        )

    def run(self, path: str | Path) -> ReplayResult:
        transcript = ReplayTranscript.read(path)
        header = transcript.header
        initial = header.initial_state
        scheduler = ReasoningScheduler(
            available_tools=initial.available_tools,
            heartbeat_actions=initial.heartbeat_actions,
        )
        machine = PhaseMachine(start_phase=initial.start_phase)
        state = RunEvidenceState(run_id=header.run_id)
        for fact in initial.facts:
            state.set_fact(
                fact.key,
                fact.value,
                evidence_ref=fact.evidence_ref,
                scope=fact.scope,
            )
        for conflict in initial.conflicts:
            state.record_conflict(conflict)
        loop_memory = LoopMemory()
        transition_policy = PhaseTransitionPolicy(repair_guard=loop_memory)
        budgets = RepairBudgets(
            global_remaining=initial.repair_global_remaining,
            phase_remaining=dict(initial.repair_phase_remaining),
        )
        output_storage = _ReplayOutputStorage()
        verdict_orchestrator = _ReplayVerdictOrchestrator()
        finalizer = VerdictFinalizer(verdict_orchestrator)

        active_envelope: dict[str, Any] | None = None
        pending_scheduler_turn: SchedulerTurn | None = None
        pending_record: PhaseAttemptRecord | None = None
        produced: list[dict[str, Any]] = []
        loop_decisions: list[LoopDecision] = []
        repairs: list[RepairRouteResult] = []
        planner_response_count = 0
        envelope_count = 0
        snapshot: RunVerdictSnapshot | None = None

        for event in transcript.events:
            payload = event.payload
            try:
                if event.kind == "scheduler_decision":
                    if pending_scheduler_turn is not None:
                        if pending_scheduler_turn.mode is not SchedulerMode.ACTION:
                            raise ReplayValidationError(
                                "scheduler decision replaced an unconsumed thinking turn"
                            )
                        # A live actor mismatch has no executable envelope. The
                        # following scheduler decision is sufficient evidence
                        # that production invalidated the outstanding action.
                        scheduler.validate_actor_action(None, None)
                        pending_scheduler_turn = None
                    turn = scheduler.next_turn()
                    expected_mode = SchedulerMode(payload["mode"])
                    if turn.mode is not expected_mode:
                        raise ReplayMismatchError(
                            f"scheduler mode mismatch: {turn.mode.value} != {expected_mode.value}"
                        )
                    reasons = tuple(reason.value for reason in turn.reasons)
                    if reasons != tuple(payload["reasons"]):
                        raise ReplayMismatchError(
                            f"scheduler reasons mismatch: {reasons!r} != {payload['reasons']!r}"
                        )
                    actual_plan_index = turn.step.plan_index if turn.step is not None else None
                    if actual_plan_index != payload.get("plan_index"):
                        raise ReplayMismatchError("scheduler plan index differs from transcript")
                    pending_scheduler_turn = turn
                elif event.kind == "planner_response":
                    if not scheduler.awaiting_plan:
                        raise ReplayValidationError("planner response arrived without a think turn")
                    if (
                        pending_scheduler_turn is not None
                        and pending_scheduler_turn.mode is not SchedulerMode.THINK
                    ):
                        raise ReplayValidationError("planner response consumed an action turn")
                    plan_payload = dict(payload["plan"])
                    if plan_payload.get("rejected") is True:
                        scheduler.reject_plan(str(plan_payload.get("code") or "malformed_plan"))
                    else:
                        plan = CurrentPlan.model_validate(plan_payload)
                        if canonical_sha256(plan_payload) != payload["response_sha256"]:
                            raise ReplayValidationError("planner response hash mismatch")
                        scheduler.accept_plan(plan)
                    pending_scheduler_turn = None
                    planner_response_count += 1
                elif event.kind == "action_envelope":
                    turn = pending_scheduler_turn or scheduler.next_turn()
                    pending_scheduler_turn = None
                    if turn.mode is not SchedulerMode.ACTION or turn.step is None:
                        raise ReplayValidationError(
                            "action envelope is impossible in scheduler state"
                        )
                    calculated_hash = action_envelope_sha256(
                        plan_index=payload["plan_index"],
                        tool=payload["tool"],
                        exact_params=payload["exact_params"],
                    )
                    if calculated_hash != payload["envelope_sha256"]:
                        raise ReplayValidationError("action envelope hash mismatch")
                    if (
                        turn.step.plan_index != payload["plan_index"]
                        or turn.step.tool != payload["tool"]
                    ):
                        raise ReplayMismatchError("recorded envelope differs from scheduled action")
                    if not scheduler.validate_actor_action(
                        turn.step.tool,
                        turn.step.exact_params,
                    ):
                        raise ReplayMismatchError("production scheduler rejected recorded envelope")
                    active_envelope = dict(payload)
                    envelope_count += 1
                elif event.kind == "tool_result":
                    if active_envelope is None:
                        raise ReplayValidationError("tool result has no active action envelope")
                    if payload["envelope_id"] != active_envelope["envelope_id"]:
                        raise ReplayValidationError("tool result references a different envelope")
                    if (
                        payload["tool"] != active_envelope["tool"]
                        or payload["params"] != active_envelope["exact_params"]
                    ):
                        raise ReplayValidationError(
                            "tool result differs from its normalized action envelope"
                        )
                    result_payload = dict(payload["result"])
                    output_ref = result_payload.get("output_ref")
                    if output_ref:
                        output_storage.register(
                            str(output_ref), str(result_payload.get("output", ""))
                        )
                    with bind_tool_result_output_storage(output_storage):
                        result = ToolResult.model_validate(result_payload)
                    scheduler.observe_result(result)
                    state.ingest_tool_result(
                        StateScope(payload["scope"]),
                        payload["tool"],
                        result,
                        provenance=result.output_ref
                        or next(iter(result.evidence_refs or result.refs), None),
                        roles=tuple(EvidenceRole(role) for role in payload["roles"]),
                        execution_id=payload["execution_id"],
                        params=payload["params"],
                        source_phase=payload["source_phase"] or None,
                        source_attempt_id=payload["source_attempt_id"] or None,
                    )
                    active_envelope = None
                elif event.kind == "validator_observation":
                    evidence_ref = next(iter(payload["evidence_refs"]), None)
                    if payload["validated_facts"] and not evidence_ref:
                        raise ReplayValidationError("validated facts require evidence provenance")
                    # Production records these facts only after the paired gate
                    # decision is accepted. The observation event is audit data,
                    # not a second state mutation.
                elif event.kind == "gate_decision":
                    if payload["phase"] != machine.current_phase:
                        raise ReplayValidationError("gate decision targets the wrong open phase")
                    claim = PhaseClaim(
                        phase=payload["phase"],
                        signal=payload["signal"],
                        claimed_outcome=payload["claimed_outcome"],
                        key_results=payload["key_results"],
                        reason=payload["reason"],
                        evidence_refs=tuple(payload["evidence_refs"]),
                    )
                    gate = validate_phase_claim(
                        claim,
                        ValidatorState(payload["validator_state"]),
                        reason=payload["reason"],
                        evidence_refs=tuple(payload["evidence_refs"]),
                        validated_facts=payload["validated_facts"],
                    )
                    if gate.accepted is not payload["expected_accepted"]:
                        raise ReplayMismatchError("gate acceptance differs from transcript")
                    if gate.validated_outcome.value != payload["expected_outcome"]:
                        raise ReplayMismatchError("gate outcome differs from transcript")
                    if not gate.accepted:
                        scheduler.request_reasoning(ReasoningTrigger.GATE_REJECTION)
                        pending_record = None
                    else:
                        for key, value in gate.validated_facts.items():
                            evidence_ref = next(
                                iter(gate.evidence_refs), f"validator://{claim.phase}"
                            )
                            state.set_fact(
                                key,
                                value,
                                evidence_ref=evidence_ref,
                                source_phase=claim.phase,
                                source_attempt_id=machine.current_attempt_id,
                            )
                        if gate.evidence_refs:
                            state.record_phase_evidence(
                                str(machine.current_attempt_id), gate.evidence_refs
                            )
                        pending_record = machine.close_attempt(gate)
                elif event.kind == "phase_transition":
                    if pending_record is None:
                        raise ReplayValidationError("phase transition has no validated attempt")
                    repair_payload = payload.get("repair_request")
                    if repair_payload is not None:
                        request = RepairRequest.from_metadata(repair_payload)
                        decision = transition_policy.request_repair(
                            request,
                            state=state,
                            budgets=budgets,
                            source_record=pending_record,
                        )
                        accepted = decision.route.kind == "repair"
                        repairs.append(
                            RepairRouteResult(
                                edge=(request.from_phase, request.target_phase),
                                accepted=accepted,
                                reason_code=decision.reason_code,
                            )
                        )
                        if accepted:
                            budgets = RepairBudgets(
                                global_remaining=max(0, budgets.global_remaining - 1),
                                phase_remaining={
                                    **dict(budgets.phase_remaining),
                                    request.from_phase: max(
                                        0, budgets.phase_remaining.get(request.from_phase, 0) - 1
                                    ),
                                },
                            )
                    else:
                        decision = transition_policy.decide(
                            pending_record,
                            state=state,
                            budgets=budgets,
                        )
                    if (
                        decision.route.kind != payload["expected_kind"]
                        or decision.route.target != payload["expected_target"]
                        or decision.reason_code != payload["expected_reason_code"]
                    ):
                        raise ReplayMismatchError("phase transition differs from production policy")
                    appended = machine.apply(decision)
                    for record in appended:
                        state.record_phase_record(record)
                    scheduler.request_reasoning(ReasoningTrigger.PHASE_CHANGE)
                    pending_record = None
                elif event.kind == "loop_decision":
                    event_payload = dict(payload["event"])
                    recorded_recurrence_count = event_payload.pop("recurrence_count", None)
                    decision = loop_memory.observe(LoopEvent(**event_payload))
                    if (
                        recorded_recurrence_count is not None
                        and int(recorded_recurrence_count) != decision.recurrence_count
                    ):
                        raise ReplayMismatchError(
                            "loop recurrence count differs from production LoopMemory"
                        )
                    if (
                        decision.decision != payload["expected_decision"]
                        or decision.reason_code != payload["expected_reason_code"]
                    ):
                        raise ReplayMismatchError(
                            "loop decision differs from production LoopMemory"
                        )
                    if decision.request_thinking:
                        scheduler.request_reasoning(ReasoningTrigger.LOOP_BREAKER)
                    loop_decisions.append(decision)
                elif event.kind == "evidence_close":
                    if (
                        active_envelope is not None
                        or pending_scheduler_turn is not None
                        or pending_record is not None
                    ):
                        raise ReplayValidationError("evidence closed with unconsumed control state")
                    state.seal(
                        finalized_at=header.finalized_at,
                        close_reason=payload["reason"],
                    )
                    snapshot = finalizer.finalize(
                        state,
                        EvidenceCloseReason(payload["reason"]),
                    )
                else:  # pragma: no cover - ControlEvent validation owns this
                    raise ReplayValidationError(f"unsupported event kind: {event.kind}")
            except ReplayValidationError:
                raise
            except Exception as exc:
                raise ReplayValidationError(
                    f"event {event.sequence} ({event.kind}) is impossible: {exc}"
                ) from exc
            produced.append(
                {"sequence": event.sequence, "kind": event.kind, "payload": event.payload}
            )

        if snapshot is None:
            raise ReplayValidationError("transcript did not reach evidence_close")
        unconsumed = tuple(
            name
            for name, present in (
                ("active_envelope", active_envelope is not None),
                ("pending_scheduler_turn", pending_scheduler_turn is not None),
                ("pending_phase_record", pending_record is not None),
            )
            if present
        )
        digest = canonical_sha256(produced)
        actual_snapshot = snapshot.model_dump(mode="json")
        if self.verify_expected:
            if actual_snapshot != header.expected_snapshot:
                raise ReplayMismatchError("replayed snapshot differs from frozen expectation")
            if digest != header.expected_event_digest:
                raise ReplayMismatchError("produced event digest differs from frozen expectation")
            if unconsumed:
                raise ReplayMismatchError(f"unconsumed replay state: {unconsumed!r}")
        return ReplayResult(
            header=header,
            snapshot=snapshot,
            expected_snapshot=header.expected_snapshot,
            produced_event_digest=digest,
            expected_event_digest=header.expected_event_digest,
            unconsumed_events=unconsumed,
            phase_records=machine.records,
            loop_decisions=tuple(loop_decisions),
            repair_routes=tuple(repairs),
            planner_response_count=planner_response_count,
            executed_envelope_count=envelope_count,
        )


__all__ = [
    "ControlReplayRunner",
    "ReplayHeader",
    "ReplayMismatchError",
    "ReplayResult",
    "ReplayTranscript",
    "ReplayValidationError",
]
