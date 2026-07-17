"""Run-scoped recurrence detection keyed by actions, outcomes, and progress."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, Mapping

_STATE_SCOPES = (
    "environment",
    "dependencies",
    "artifacts",
    "test_runtime",
    "project_analysis",
)
_VOLATILE_ARG_KEYS = frozenset(
    {
        "timestamp",
        "pid",
        "process_id",
        "job_id",
        "poll_ref",
        "output_ref",
        "output_ref_id",
        "cursor",
        "output_cursor",
        "poll_sequence",
        "sequence",
    }
)
_OUTPUT_REF = re.compile(r"\boutput_[A-Za-z0-9_-]+\b")
_JOB_REF = re.compile(r"\bjob:[A-Za-z0-9_-]+\b")
_ISO_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z|[+-]\d{2}:?\d{2})?\b")
_GENERATED_PATH = re.compile(
    r"(?:/workspace)?/\.setup_agent/(?:contexts|tool_traces|logs|jobs)/[^\s]+"
)
_TEMP_PATH = re.compile(r"(?:/private)?/tmp/[^\s]+")
_WHITESPACE = re.compile(r"\s+")


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _normalize_text(value: Any) -> str:
    return _WHITESPACE.sub(" ", str(value or "").strip()).lower()


def _normalize_volatile(value: str) -> str:
    normalized = _OUTPUT_REF.sub("<output-ref>", value)
    normalized = _JOB_REF.sub("<job-ref>", normalized)
    normalized = _ISO_TIMESTAMP.sub("<timestamp>", normalized)
    normalized = _GENERATED_PATH.sub("<generated-path>", normalized)
    return _TEMP_PATH.sub("<temp-path>", normalized)


def _normalize_command(command: Any) -> str:
    source = _normalize_volatile(str(command or "").strip())
    try:
        tokens = shlex.split(source)
    except ValueError:
        tokens = source.split()
    normalized_tokens = []
    previous = ""
    for token in tokens:
        if previous in {"--pid", "-p"} and token.isdigit():
            normalized_tokens.append("<pid>")
        elif re.fullmatch(r"(?:cursor|sequence|offset)=\d+", token, re.IGNORECASE):
            key = token.partition("=")[0].lower()
            normalized_tokens.append(f"{key}=<cursor>")
        else:
            normalized_tokens.append(token)
        previous = token
    return shlex.join(normalized_tokens)


def _normalize_workdir(value: Any) -> str:
    source = str(value or "/workspace").strip() or "/workspace"
    return posixpath.normpath(source)


def _stable_value(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_volatile(_WHITESPACE.sub(" ", value.strip()))
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, order=True)
class ActionKey:
    tool_name: str
    normalized_target: tuple[str, ...]

    @classmethod
    def from_event(cls, event: "LoopEvent") -> "ActionKey":
        tool = _normalize_text(event.tool_name)
        args = dict(event.args)
        if tool == "repair":
            target = (
                _normalize_text(args.get("source_phase")),
                _normalize_text(args.get("target_phase")),
            )
        elif tool in {"build", "maven", "gradle", "python"}:
            action = args.get("action") or args.get("goal") or args.get("command") or "execute"
            target = (
                _normalize_command(action),
                _normalize_workdir(args.get("working_directory") or args.get("workdir")),
            )
        elif tool in {"poll_command", "poll", "search_job"}:
            target = ("<async-job>",)
        elif "search" in tool:
            query = (
                args.get("pattern")
                or args.get("query")
                or args.get("search_term")
                or args.get("path")
                or ""
            )
            target = (_normalize_text(query),)
        elif "command" in args:
            target = (_normalize_command(args.get("command")),)
        else:
            target = tuple(
                f"{key}={_stable_value(value)}"
                for key, value in sorted(args.items())
                if key not in _VOLATILE_ARG_KEYS
            ) or ("execute",)
        return cls(tool_name=tool, normalized_target=target)


@dataclass(frozen=True, order=True)
class OutcomeKey:
    operation_outcome: str
    error_code: str
    failure_signature: str

    @classmethod
    def from_event(cls, event: "LoopEvent") -> "OutcomeKey":
        return cls(
            operation_outcome=_enum_text(event.operation_outcome) or "unknown",
            error_code=_normalize_text(event.error_code),
            failure_signature=_normalize_text(_normalize_volatile(event.failure_signature)),
        )


@dataclass(frozen=True, order=True)
class RelevantStateVector:
    values: tuple[tuple[str, int], ...]

    @classmethod
    def from_mapping(
        cls,
        state: Mapping[str, int],
        scopes: Iterable[str],
    ) -> "RelevantStateVector":
        normalized = {
            _enum_text(key): int(value)
            for key, value in state.items()
            if _enum_text(key) in _STATE_SCOPES
        }
        selected_scopes = {_enum_text(item) for item in scopes}
        ordered_scopes = tuple(scope for scope in _STATE_SCOPES if scope in selected_scopes)
        return cls(tuple((scope, normalized.get(scope, 0)) for scope in ordered_scopes))

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(scope for scope, _ in self.values)

    def as_dict(self) -> dict[str, int]:
        return dict(self.values)


@dataclass(frozen=True)
class LoopEvent:
    tool_name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    operation_outcome: str = "unknown"
    error_code: str = ""
    failure_signature: str = ""
    relevant_state: Mapping[str, int] = field(default_factory=dict)
    phase: str = ""
    attempt_id: str = ""
    iteration: int = 0
    evidence_ref: str = ""
    invocation_status: str = "completed"
    relevant_scopes: tuple[str, ...] = ()
    job_id: str = ""
    output_cursor: str = ""

    def with_state(self, state: Mapping[str, int]) -> "LoopEvent":
        return replace(self, relevant_state=dict(state))

    def with_output_cursor(self, cursor: str) -> "LoopEvent":
        return replace(self, output_cursor=str(cursor))


@dataclass(frozen=True)
class RecurrenceRecord:
    action_key: ActionKey
    outcome_key: OutcomeKey
    relevant_state_vector: RelevantStateVector
    phase: str
    attempt_id: str
    iteration: int
    evidence_ref: str
    occurrence_count: int
    decision: str


LoopDecisionKind = Literal[
    "continue",
    "guide",
    "force_break",
    "close_phase",
    "diversity_advisory",
]


@dataclass(frozen=True)
class LoopDecision:
    decision: LoopDecisionKind
    action_key: ActionKey
    outcome_key: OutcomeKey
    relevant_state_vector: RelevantStateVector
    recurrence_count: int
    prior_attempt_ids: tuple[str, ...] = ()
    failure_ref: str = ""
    missing_progress_scopes: tuple[str, ...] = ()
    blocker_signature: str = ""
    resolved_blocker_signatures: tuple[str, ...] = ()
    request_thinking: bool = False
    close_phase: bool = False
    reason_code: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "action_key": {
                "tool_name": self.action_key.tool_name,
                "normalized_target": list(self.action_key.normalized_target),
            },
            "outcome_key": {
                "operation_outcome": self.outcome_key.operation_outcome,
                "error_code": self.outcome_key.error_code,
                "failure_signature": self.outcome_key.failure_signature,
            },
            "relevant_state_vector": self.relevant_state_vector.as_dict(),
            "recurrence_count": self.recurrence_count,
            "prior_attempt_ids": list(self.prior_attempt_ids),
            "failure_ref": self.failure_ref,
            "missing_progress_scopes": list(self.missing_progress_scopes),
            "blocker_signature": self.blocker_signature,
            "resolved_blocker_signatures": list(self.resolved_blocker_signatures),
            "request_thinking": self.request_thinking,
            "close_phase": self.close_phase,
            "reason_code": self.reason_code,
        }


@dataclass
class _RecurrenceChain:
    vector: RelevantStateVector
    count: int = 1
    records: list[RecurrenceRecord] = field(default_factory=list)
    force_break_armed: bool = False


def _relevant_scopes(event: LoopEvent) -> tuple[str, ...]:
    if event.relevant_scopes:
        return tuple(_enum_text(scope) for scope in event.relevant_scopes)
    tool = _normalize_text(event.tool_name)
    args = dict(event.args)
    action = _normalize_text(args.get("action") or args.get("goal"))
    command = _normalize_text(args.get("command"))
    if tool == "repair":
        return (
            ("environment", "dependencies", "artifacts")
            if _normalize_text(args.get("target_phase")) == "build"
            else ("project_analysis", "dependencies")
        )
    if tool in {"search_files", "file_io", "project_analyzer"}:
        return ("project_analysis",)
    if (
        tool in {"poll_command", "poll", "search_job"}
        or event.job_id
        or event.output_cursor
        or ".setup_agent/jobs" in command
        or "job:" in command
    ):
        return ("artifacts", "test_runtime")
    if tool == "project" and action == "analyze":
        return ("project_analysis",)
    if tool in {"project", "system", "env"}:
        return ("environment", "dependencies")
    if tool in {"build", "maven", "gradle", "python", "run_command", "bash"}:
        if re.search(r"(?:^|[-_])(?:test|verify|check)(?:$|[-_])", action) or re.search(
            r"(?:^|\s)(?:test|pytest|verify)(?:\s|$)",
            command,
        ):
            return ("environment", "dependencies", "artifacts", "test_runtime")
        if action in {"deps", "install_dependencies"} or " install " in f" {command} ":
            return ("environment", "dependencies", "artifacts")
        return ("environment", "dependencies", "artifacts")
    return _STATE_SCOPES


def _is_recurrence_candidate(event: LoopEvent, outcome: OutcomeKey) -> bool:
    """Limit hard recurrence handling to failures and stalled pending work."""
    if _normalize_text(event.tool_name) == "repair":
        return True
    if _enum_text(event.invocation_status) == "pending":
        return True
    if outcome.operation_outcome in {"failed", "partial"}:
        return True
    return outcome.operation_outcome == "unknown" and bool(
        outcome.error_code or outcome.failure_signature
    )


class LoopMemory:
    """Detect recurrence only when the action outcome and relevant state agree."""

    def __init__(self, *, diversity_threshold: int = 16) -> None:
        self.diversity_threshold = int(diversity_threshold)
        if self.diversity_threshold <= 0:
            raise ValueError("diversity threshold must be positive")
        self._chains: dict[tuple[ActionKey, OutcomeKey], _RecurrenceChain] = {}
        self._history: list[RecurrenceRecord] = []
        self._diversity: dict[tuple[str, str], set[ActionKey]] = {}
        self._poll_tokens: dict[ActionKey, tuple[str, str, str]] = {}
        self._active_blockers: dict[str, tuple[ActionKey, RelevantStateVector]] = {}
        self._armed_key: tuple[ActionKey, OutcomeKey] | None = None

    @property
    def records(self) -> tuple[RecurrenceRecord, ...]:
        return tuple(self._history)

    @staticmethod
    def _blocker_signature(
        action_key: ActionKey,
        outcome_key: OutcomeKey,
        vector: RelevantStateVector,
    ) -> str:
        payload = json.dumps(
            {
                "tool": action_key.tool_name,
                "target": action_key.normalized_target,
                "outcome": (
                    outcome_key.operation_outcome,
                    outcome_key.error_code,
                    outcome_key.failure_signature,
                ),
                "state": vector.values,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"loop_without_progress:{action_key.tool_name}:{digest}"

    @staticmethod
    def _prior_attempt_ids(chain: _RecurrenceChain) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(record.attempt_id for record in chain.records if record.attempt_id)
        )

    def _decision(
        self,
        kind: LoopDecisionKind,
        *,
        action_key: ActionKey,
        outcome_key: OutcomeKey,
        vector: RelevantStateVector,
        chain: _RecurrenceChain,
        event: LoopEvent,
        reason_code: str,
    ) -> LoopDecision:
        return LoopDecision(
            decision=kind,
            action_key=action_key,
            outcome_key=outcome_key,
            relevant_state_vector=vector,
            recurrence_count=chain.count,
            prior_attempt_ids=self._prior_attempt_ids(chain),
            failure_ref=event.evidence_ref
            or next(
                (record.evidence_ref for record in reversed(chain.records) if record.evidence_ref),
                "",
            ),
            missing_progress_scopes=vector.scopes if kind != "continue" else (),
            blocker_signature=(
                self._blocker_signature(action_key, outcome_key, vector)
                if kind in {"force_break", "close_phase"}
                else ""
            ),
            request_thinking=kind in {"guide", "force_break"},
            close_phase=kind == "close_phase",
            reason_code=reason_code,
        )

    def _record(
        self,
        chain: _RecurrenceChain,
        event: LoopEvent,
        decision: LoopDecision,
    ) -> None:
        record = RecurrenceRecord(
            action_key=decision.action_key,
            outcome_key=decision.outcome_key,
            relevant_state_vector=decision.relevant_state_vector,
            phase=str(event.phase),
            attempt_id=str(event.attempt_id),
            iteration=int(event.iteration),
            evidence_ref=str(event.evidence_ref),
            occurrence_count=chain.count,
            decision=decision.decision,
        )
        chain.records.append(record)
        self._history.append(record)

    def _diversity_decision(
        self,
        event: LoopEvent,
        decision: LoopDecision,
    ) -> LoopDecision:
        if decision.decision != "continue":
            return decision
        scope = (str(event.phase or "run"), decision.action_key.tool_name)
        actions = self._diversity.setdefault(scope, set())
        was_new = decision.action_key not in actions
        actions.add(decision.action_key)
        if was_new and len(actions) >= self.diversity_threshold:
            return replace(
                decision,
                decision="diversity_advisory",
                reason_code="action_diversity_advisory",
            )
        return decision

    def observe(self, event: LoopEvent) -> LoopDecision:
        if not isinstance(event, LoopEvent):
            raise TypeError("LoopMemory.observe requires LoopEvent")
        action_key = ActionKey.from_event(event)
        outcome_key = OutcomeKey.from_event(event)
        vector = RelevantStateVector.from_mapping(
            event.relevant_state,
            _relevant_scopes(event),
        )
        base_key = (action_key, outcome_key)
        current_state = {
            _enum_text(scope): int(value) for scope, value in event.relevant_state.items()
        }
        poll_token = (
            str(event.job_id),
            str(event.output_cursor),
            _enum_text(event.invocation_status),
        )
        prior_poll_token = self._poll_tokens.get(action_key)
        poll_lifecycle_progress = bool(
            (event.job_id or event.output_cursor)
            and prior_poll_token != poll_token
            and (
                poll_token[2] == "pending"
                or (prior_poll_token is not None and prior_poll_token[2] == "pending")
            )
        )
        if event.job_id or event.output_cursor:
            self._poll_tokens[action_key] = poll_token

        resolved_blockers = []
        for signature, blocker in tuple(self._active_blockers.items()):
            blocked_action, blocked_vector = blocker
            prior_state = blocked_vector.as_dict()
            state_progress = all(scope in current_state for scope in prior_state) and any(
                current_state[scope] > value for scope, value in prior_state.items()
            )
            if state_progress or (poll_lifecycle_progress and blocked_action == action_key):
                resolved_blockers.append(signature)
                del self._active_blockers[signature]

        if self._armed_key is not None:
            armed_chain = self._chains[self._armed_key]
            if (
                base_key != self._armed_key
                or vector != armed_chain.vector
                or poll_lifecycle_progress
            ):
                armed_chain.force_break_armed = False
                self._armed_key = None

        chain = self._chains.get(base_key)
        if poll_lifecycle_progress:
            chain = _RecurrenceChain(vector=vector)
            self._chains[base_key] = chain
            decision = self._decision(
                "continue",
                action_key=action_key,
                outcome_key=outcome_key,
                vector=vector,
                chain=chain,
                event=event,
                reason_code="poll_lifecycle_progress",
            )
        elif not _is_recurrence_candidate(event, outcome_key):
            chain = _RecurrenceChain(vector=vector)
            self._chains[base_key] = chain
            decision = self._decision(
                "continue",
                action_key=action_key,
                outcome_key=outcome_key,
                vector=vector,
                chain=chain,
                event=event,
                reason_code="outcome_not_loop_candidate",
            )
        elif chain is None or chain.vector != vector:
            chain = _RecurrenceChain(vector=vector)
            self._chains[base_key] = chain
            decision = self._decision(
                "continue",
                action_key=action_key,
                outcome_key=outcome_key,
                vector=vector,
                chain=chain,
                event=event,
                reason_code="new_recurrence_chain",
            )
        elif chain.force_break_armed and self._armed_key == base_key:
            chain.count += 1
            decision = self._decision(
                "close_phase",
                action_key=action_key,
                outcome_key=outcome_key,
                vector=vector,
                chain=chain,
                event=event,
                reason_code="loop_repeated_after_break",
            )
            chain.force_break_armed = False
            self._armed_key = None
        else:
            if chain.count < 4:
                chain.count += 1
            if chain.count >= 4:
                chain.count = 4
                chain.force_break_armed = True
                self._armed_key = base_key
                kind: LoopDecisionKind = "force_break"
                reason = "loop_without_progress"
            else:
                kind = "guide"
                reason = "recurrence_without_progress"
            decision = self._decision(
                kind,
                action_key=action_key,
                outcome_key=outcome_key,
                vector=vector,
                chain=chain,
                event=event,
                reason_code=reason,
            )

        decision = self._diversity_decision(event, decision)
        if decision.decision == "force_break":
            self._active_blockers[decision.blocker_signature] = (action_key, vector)
        if resolved_blockers:
            decision = replace(
                decision,
                resolved_blocker_signatures=tuple(dict.fromkeys(resolved_blockers)),
            )
        self._record(chain, event, decision)
        return decision

    def check(
        self,
        request: Any,
        relevant_state_vector: Mapping[str, int],
    ) -> str | None:
        """Implement ``RepairRecurrenceGuard`` using the same run-wide memory."""
        event = LoopEvent(
            tool_name="repair",
            args={
                "source_phase": request.from_phase,
                "target_phase": request.target_phase,
            },
            operation_outcome="requested",
            error_code="repair_requested",
            failure_signature=request.failure_signature,
            relevant_state=dict(relevant_state_vector),
            relevant_scopes=tuple(relevant_state_vector),
            phase=request.from_phase,
            attempt_id=request.source_attempt_id,
            evidence_ref=next(iter(request.evidence_refs), ""),
        )
        decision = self.observe(event)
        return None if decision.decision == "continue" else "repair_without_progress"

    @classmethod
    def from_repair_records(cls, records: Iterable[Any]) -> "LoopMemory":
        memory = cls()
        for record in records:
            if not getattr(record, "accepted", False):
                continue
            event = LoopEvent(
                tool_name="repair",
                args={
                    "source_phase": record.from_phase,
                    "target_phase": record.target_phase,
                },
                operation_outcome="requested",
                error_code="repair_requested",
                failure_signature=record.failure_signature,
                relevant_state=dict(record.state_vector),
                relevant_scopes=tuple(record.state_vector),
                phase=record.from_phase,
                attempt_id=record.source_attempt_id,
                evidence_ref=next(iter(record.evidence_refs), ""),
            )
            memory.observe(event)
        return memory
