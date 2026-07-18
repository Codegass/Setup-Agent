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

CONTROL_EVENT_SCHEMA_VERSION = 1
CONTROL_EVENT_KINDS = (
    "planner_response",
    "scheduler_decision",
    "action_envelope",
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

    target_repo_sha: str = Field(min_length=1)
    container_image_digest: str = Field(min_length=1)
    sag_git_sha: str = Field(min_length=1)
    thinking_model: str = Field(min_length=1)
    action_model: str = Field(min_length=1)
    sanitized_config: dict[str, Any]
    prompt_bundle_sha256: str
    feature_flags: dict[str, bool]
    random_seed_or_null: int | None
    dependency_cache_state: str = Field(min_length=1)
    host_arch: str = Field(min_length=1)

    @field_validator("target_repo_sha", "sag_git_sha")
    @classmethod
    def _valid_git_sha(cls, value: str) -> str:
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
    plan_index: int = Field(ge=0)
    tool: str = Field(min_length=1)
    exact_params: dict[str, Any]
    envelope_sha256: str


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
    output_sha256: str | None = None

    @model_validator(mode="after")
    def _no_full_output_body(self) -> "ToolResultPayload":
        forbidden = {"raw_output", "full_output", "prompt"}.intersection(self.result)
        if forbidden:
            raise ValueError("control events must reference full output, not embed it")
        output = self.result.get("output")
        if output is not None and len(str(output)) > 512:
            raise ValueError("control-event result summaries are limited to 512 characters")
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
        object.__setattr__(self, "payload", model.model_dump(mode="json"))
        return self

    @property
    def typed_payload(self) -> _StrictPayload:
        return cast(_StrictPayload, _PAYLOAD_MODELS[self.kind].model_validate(self.payload))


def action_envelope_sha256(*, plan_index: int, tool: str, exact_params: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {"plan_index": int(plan_index), "tool": str(tool), "exact_params": dict(exact_params)}
    )


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
    "sanitize_config",
]
