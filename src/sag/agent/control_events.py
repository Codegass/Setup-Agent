"""Canonical structured control events for live recording and offline replay.

The event stream intentionally contains decisions and structured observations,
not prompts or full tool output.  Large output stays in OutputStorage and is
identified here by a stable reference and digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTROL_EVENT_SCHEMA_VERSION = 2
# `planner_response` and `scheduler_decision` are HISTORICAL kinds: the engine
# stopped emitting them when Plan 2 Task 8 deleted the reasoning scheduler and
# the plan lock. They stay in the schema so transcripts recorded before that
# still parse and verify (replay reads and skips them).
CONTROL_EVENT_KINDS = (
    "planner_response",
    "scheduler_decision",
    "action_envelope",
    "forced_action",
    "tool_result",
    "validator_observation",
    "gate_decision",
    "phase_transition",
    "loop_decision",
    "evidence_close",
)
ControlEventKind = Literal[
    "planner_response",
    "scheduler_decision",
    "action_envelope",
    "forced_action",
    "tool_result",
    "validator_observation",
    "gate_decision",
    "phase_transition",
    "loop_decision",
    "evidence_close",
]

_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|base[_-]?url|api[_-]?base|endpoint|url)$",
    re.IGNORECASE,
)
_SECRET_VALUE_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential)$",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def _redact_url_credentials(value: str) -> str:
    return _URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", value)


def sanitize_config(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Return reproducibility settings without credentials or secret-bearing endpoints."""
    source: Any = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)

    def visit(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): visit(child)
                for key, child in item.items()
                if not _SENSITIVE_CONFIG_KEY.search(str(key))
            }
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return _redact_url_credentials(item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)

    sanitized = visit(source)
    return dict(sanitized)


def compact_control_value(value: Any, *, max_string: int = 512) -> Any:
    """Bound structured event fields and remove prompt/full-output shaped children."""
    denied = {"raw_output", "full_output", "prompt", "prompt_body", "stdout", "stderr"}

    def visit(item: Any, depth: int = 0) -> Any:
        if depth >= 6:
            return "<depth-limited>"
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json")
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in list(item.items())[:128]:
                key_text = str(key)
                if key_text.lower() in denied or _SECRET_VALUE_KEY.search(key_text):
                    continue
                result[key_text] = visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in list(item)[:128]]
        if isinstance(item, str):
            redacted = _redact_url_credentials(item)
            return redacted if len(redacted) <= max_string else redacted[:max_string] + "..."
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return visit(str(item), depth + 1)

    return visit(value)


def canonical_json(value: Any) -> str:
    """Return the one byte representation used by hashes and JSONL files."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class SourceFileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str
    source_sag_sha: str | None = None

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("source manifest requires a SHA-256 digest")
        return normalized


class SourceExcerpt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    line_ref: str = Field(min_length=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("source excerpt requires a SHA-256 digest")
        return normalized


class RunPin(BaseModel):
    """Complete reproducibility facts for one live or replayed run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # None until the target repo SHA is observed. The pin is written
    # UNCONDITIONALLY at agent startup so the reproducibility file always
    # exists (a run that never observes a target SHA still leaves a pin for
    # post-mortem); it is rewritten with the real SHA the moment one is seen.
    # The collector's current-run validation demands the observed SHA, so a
    # still-null pin fails collection exactly as an absent pin used to.
    target_repo_sha: str | None = None
    container_image_digest: str = Field(min_length=1)
    sag_git_sha: str = Field(min_length=1)
    thinking_model: str = Field(min_length=1)
    action_model: str = Field(min_length=1)
    sanitized_config: dict[str, Any]
    prompt_bundle_sha256: str
    feature_flags: dict[str, bool]
    # Spec-required run-order index (protocol deviation registered for the
    # 2026-07-19 stage-1 runs, reconstructed from ledger order there).
    run_order_index: int | None = None
    random_seed_or_null: int | None
    dependency_cache_state: str = Field(min_length=1)
    host_arch: str = Field(min_length=1)
    # Advisor telemetry for the run: {"mode", "calls": [...]} (spec §3.2).
    # None on legacy/external pins built before the advisor existed, so the
    # ablation comparison can tell "no advisor" from "advisor consulted 0×".
    advisor: dict[str, Any] | None = None

    @field_validator("target_repo_sha", "sag_git_sha")
    @classmethod
    def _valid_git_sha(cls, value: str | None) -> str | None:
        # target_repo_sha is optional (None until observed); sag_git_sha is
        # always required by its own field constraint, so None never reaches
        # here for it.
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("git pins must be full 40-character SHAs")
        return normalized

    @field_validator("container_image_digest")
    @classmethod
    def _valid_image_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        digest = normalized.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("container image pin must be a sha256 digest")
        return f"sha256:{digest}"

    @field_validator("prompt_bundle_sha256")
    @classmethod
    def _valid_prompt_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("prompt bundle pin must be a SHA-256 digest")
        return normalized

    @classmethod
    def runtime_defaults(
        cls,
        *,
        target_repo_sha: str,
        container_image_digest: str,
        sag_git_sha: str,
        thinking_model: str,
        action_model: str,
        sanitized_config: Mapping[str, Any],
        prompt_bundle_sha256: str,
        feature_flags: Mapping[str, bool],
        random_seed: int | None,
        dependency_cache_state: str,
    ) -> "RunPin":
        return cls(
            target_repo_sha=target_repo_sha,
            container_image_digest=container_image_digest,
            sag_git_sha=sag_git_sha,
            thinking_model=thinking_model,
            action_model=action_model,
            sanitized_config=dict(sanitized_config),
            prompt_bundle_sha256=prompt_bundle_sha256,
            feature_flags=dict(feature_flags),
            random_seed_or_null=random_seed,
            dependency_cache_state=dependency_cache_state,
            host_arch=platform.machine() or "unknown",
        )


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerResponsePayload(_StrictPayload):
    plan_id: str = Field(min_length=1)
    plan: dict[str, Any]
    response_sha256: str


class SchedulerDecisionPayload(_StrictPayload):
    mode: Literal["think", "action"]
    reasons: tuple[str, ...] = ()
    plan_index: int | None = Field(default=None, ge=0)


class ActionEnvelopePayload(_StrictPayload):
    envelope_id: str = Field(min_length=1)
    plan_index: int | None = Field(default=None, ge=0)
    tool_call_id: str | None = Field(default=None, min_length=1)
    tool: str = Field(min_length=1)
    exact_params: dict[str, Any]
    envelope_sha256: str

    @model_validator(mode="after")
    def _carries_an_action_identity(self) -> "ActionEnvelopePayload":
        """One of the two protocol identities must key the envelope.

        Recorded transcripts carry `plan_index` (scheduler protocol); native
        tool-calling turns carry `tool_call_id`. An identityless envelope
        cannot be correlated with its `tool_result`, so it is rejected here
        rather than emitted and lost downstream.
        """
        if self.plan_index is None and not self.tool_call_id:
            raise ValueError("action envelope requires plan_index or tool_call_id")
        return self


class TestCandidatePayload(_StrictPayload):
    root: str = Field(min_length=1)
    system: str = Field(min_length=1)


class TestCandidateResolutionPayload(_StrictPayload):
    status: Literal[
        "available",
        "manifest_unreadable",
        "coordinates_missing",
        "unsafe_coordinates",
    ]
    workspace_root: str | None = None
    project_root: str | None = None
    candidates: tuple[TestCandidatePayload, ...] = ()

    @model_validator(mode="after")
    def _status_matches_candidates(self) -> "TestCandidateResolutionPayload":
        if self.status == "available" and (
            not self.workspace_root
            or not self.project_root
            or not self.candidates
        ):
            raise ValueError(
                "available test-candidate resolution requires roots and candidates"
            )
        if self.status != "available" and self.candidates:
            raise ValueError("failed test-candidate resolution cannot contain coordinates")
        return self


class ForcedActionPayload(_StrictPayload):
    """Harness-owned action emitted without a planner/scheduler step."""

    envelope_id: str = Field(min_length=1)
    policy: Literal["test_attempt_required"]
    trigger: Literal[
        "termination_refusal",
        "repair_refusal",
        "terminal_metadata",
        "phase_floor",
        "loop_close",
    ]
    phase: Literal["test"]
    source_attempt_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    tool: Literal["build", "search", "project"]
    exact_params: dict[str, Any]
    candidate_root: str | None = None
    candidate_system: str | None = None
    parent_execution_id: str | None = None
    candidate_resolution: TestCandidateResolutionPayload
    action_sha256: str

    @model_validator(mode="after")
    def _valid_action_digest(self) -> "ForcedActionPayload":
        if forced_action_sha256(
            policy=self.policy,
            trigger=self.trigger,
            phase=self.phase,
            source_attempt_id=self.source_attempt_id,
            reason_code=self.reason_code,
            tool=self.tool,
            exact_params=self.exact_params,
            candidate_root=self.candidate_root,
            candidate_system=self.candidate_system,
            parent_execution_id=self.parent_execution_id,
            candidate_resolution=self.candidate_resolution.model_dump(mode="json"),
        ) != self.action_sha256:
            raise ValueError("forced action hash mismatch")
        return self


class ActualExecutionPayload(_StrictPayload):
    execution_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    params: dict[str, Any]
    scope: Literal[
        "environment",
        "dependencies",
        "artifacts",
        "test_runtime",
        "project_analysis",
    ]
    roles: tuple[Literal["build", "test"], ...] = ()
    result: dict[str, Any]

    @model_validator(mode="after")
    def _no_full_output_body(self) -> "ActualExecutionPayload":
        _validate_control_result_projection(self.result)
        return self


class ToolResultPayload(_StrictPayload):
    envelope_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    params: dict[str, Any]
    scope: Literal[
        "environment",
        "dependencies",
        "artifacts",
        "test_runtime",
        "project_analysis",
    ]
    roles: tuple[Literal["build", "test"], ...] = ()
    result: dict[str, Any]
    source_phase: str = ""
    source_attempt_id: str = ""
    actual_executions: tuple[ActualExecutionPayload, ...] = ()
    output_sha256: str | None = None

    @model_validator(mode="after")
    def _no_full_output_body(self) -> "ToolResultPayload":
        _validate_control_result_projection(self.result)
        return self


class ValidatorObservationPayload(_StrictPayload):
    phase: str = Field(min_length=1)
    validator_state: Literal["green", "partial", "red", "unavailable"]
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    validated_facts: dict[str, Any] = Field(default_factory=dict)


class GateDecisionPayload(_StrictPayload):
    phase: str = Field(min_length=1)
    signal: Literal["done", "blocked"] = "done"
    claimed_outcome: Literal["success", "partial", "failed", "unknown"]
    validator_state: Literal["green", "partial", "red", "unavailable"]
    expected_accepted: bool
    expected_outcome: Literal["success", "partial", "failed", "unknown"]
    reason: str = ""
    key_results: str = ""
    evidence_refs: tuple[str, ...] = ()
    validated_facts: dict[str, Any] = Field(default_factory=dict)
    source_attempt_id: str | None = None
    test_candidate_resolution: TestCandidateResolutionPayload | None = None


class PhaseTransitionPayload(_StrictPayload):
    expected_kind: Literal["advance", "repair", "evidence_close", "report", "flow_close"]
    expected_target: str | None = None
    expected_reason_code: str = Field(min_length=1)
    repair_request: dict[str, Any] | None = None


class LoopDecisionPayload(_StrictPayload):
    event: dict[str, Any]
    expected_decision: Literal[
        "continue", "guide", "force_break", "close_phase", "diversity_advisory"
    ]
    expected_reason_code: str = Field(min_length=1)


class EvidenceClosePayload(_StrictPayload):
    reason: Literal[
        "test_terminated",
        "dependents_skipped",
        "aborted",
        "cancelled",
    ]


_PAYLOAD_MODELS: dict[str, type[_StrictPayload]] = {
    "planner_response": PlannerResponsePayload,
    "scheduler_decision": SchedulerDecisionPayload,
    "action_envelope": ActionEnvelopePayload,
    "forced_action": ForcedActionPayload,
    "tool_result": ToolResultPayload,
    "validator_observation": ValidatorObservationPayload,
    "gate_decision": GateDecisionPayload,
    "phase_transition": PhaseTransitionPayload,
    "loop_decision": LoopDecisionPayload,
    "evidence_close": EvidenceClosePayload,
}


class ControlEvent(BaseModel):
    """One strict event row. Payload fields are validated per event kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    kind: ControlEventKind
    payload: dict[str, Any]
    source: SourceExcerpt | None = None
    timestamp: str | None = None
    event_id: str | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> "ControlEvent":
        model = _PAYLOAD_MODELS[self.kind].model_validate(self.payload)
        # Preserve legacy fixture shape while retaining every field explicitly
        # recorded by new live runs.
        object.__setattr__(
            self,
            "payload",
            model.model_dump(mode="json", exclude_unset=True),
        )
        return self

    @property
    def typed_payload(self) -> _StrictPayload:
        return cast(_StrictPayload, _PAYLOAD_MODELS[self.kind].model_validate(self.payload))


def action_envelope_sha256(
    *,
    plan_index: int | None = None,
    tool: str,
    exact_params: Mapping[str, Any],
    tool_call_id: str | None = None,
) -> str:
    """Digest one action envelope under whichever protocol identity keys it.

    `plan_index` wins whenever it is present, so every envelope recorded
    under the scheduler protocol keeps hashing byte-identically. Native
    turns have no plan index and substitute `tool_call:<id>` in the slot the
    plan index occupied.
    """
    if plan_index is not None:
        identity: Any = int(plan_index)
    elif tool_call_id:
        identity = f"tool_call:{tool_call_id}"
    else:
        raise ValueError("action envelope hash requires plan_index or tool_call_id")
    return canonical_sha256(
        {"plan_index": identity, "tool": str(tool), "exact_params": dict(exact_params)}
    )


def forced_action_sha256(
    *,
    policy: str,
    trigger: str,
    phase: str,
    source_attempt_id: str,
    reason_code: str,
    tool: str,
    exact_params: Mapping[str, Any],
    candidate_root: str | None,
    candidate_system: str | None,
    parent_execution_id: str | None,
    candidate_resolution: Mapping[str, Any],
) -> str:
    """Digest the complete harness-owned action contract."""
    return canonical_sha256(
        {
            "policy": policy,
            "trigger": trigger,
            "phase": phase,
            "source_attempt_id": source_attempt_id,
            "reason_code": reason_code,
            "tool": tool,
            "exact_params": dict(exact_params),
            "candidate_root": candidate_root,
            "candidate_system": candidate_system,
            "parent_execution_id": parent_execution_id,
            "candidate_resolution": dict(candidate_resolution),
        }
    )


def _validate_control_result_projection(result: Mapping[str, Any]) -> None:
    forbidden = {"raw_output", "full_output", "prompt"}.intersection(result)
    if forbidden:
        raise ValueError("control events must reference full output, not embed it")
    output = result.get("output")
    if output is not None and len(str(output)) > 512:
        raise ValueError("control-event result summaries are limited to 512 characters")


class ControlEventSink:
    """Thread-safe append-only JSONL sink with deterministic injection seams."""

    def __init__(
        self,
        path: str | Path,
        *,
        mirror: Callable[[str], None] | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[int], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mirror = mirror
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self._id_factory = id_factory or (lambda sequence: f"control-{sequence:06d}")
        self._lock = threading.RLock()
        self._sequence = self._read_last_sequence()

    def _read_last_sequence(self) -> int:
        if not self.path.exists():
            return 0
        last = ""
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        if not last:
            return 0
        return int(ControlEvent.model_validate_json(last).sequence)

    @property
    def sequence(self) -> int:
        return self._sequence

    def emit(
        self,
        kind: ControlEventKind,
        payload: Mapping[str, Any] | BaseModel,
        *,
        source: SourceExcerpt | Mapping[str, Any] | None = None,
    ) -> ControlEvent:
        with self._lock:
            sequence = self._sequence + 1
            event = ControlEvent(
                sequence=sequence,
                kind=kind,
                payload=(
                    payload.model_dump(mode="json")
                    if isinstance(payload, BaseModel)
                    else dict(payload)
                ),
                source=source,
                timestamp=self._clock(),
                event_id=self._id_factory(sequence),
            )
            line = canonical_json(event) + "\n"
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._sequence = sequence
            if self._mirror is not None:
                try:
                    self._mirror(line)
                except Exception as exc:  # host truth remains append-only if mirroring is down
                    logging.getLogger(__name__).warning(
                        "control-event mirror failed at sequence %s: %s", sequence, exc
                    )
            return event

    @staticmethod
    def write_run_pin(
        path: str | Path,
        pin: RunPin | Mapping[str, Any],
        *,
        mirror: Callable[[str], None] | None = None,
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        validated = pin if isinstance(pin, RunPin) else RunPin.model_validate(pin)
        temporary = target.with_name(f".{target.name}.tmp")
        payload = canonical_json(validated)
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
        if mirror is not None:
            mirror(payload)
        return target


__all__ = [
    "CONTROL_EVENT_KINDS",
    "CONTROL_EVENT_SCHEMA_VERSION",
    "ControlEvent",
    "ControlEventKind",
    "ControlEventSink",
    "RunPin",
    "SourceExcerpt",
    "SourceFileManifest",
    "action_envelope_sha256",
    "canonical_json",
    "canonical_sha256",
    "compact_control_value",
    "forced_action_sha256",
    "sanitize_config",
]
