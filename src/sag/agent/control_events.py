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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .action_intents import bounded_exact_params
from .repair_contexts import (
    RepairContext,
    repair_context_sha256,
)

CONTROL_EVENT_SCHEMA_VERSION = 5
STRICT_CONTROL_PAYLOAD_MAX_CANONICAL_BYTES = 32 * 1024
CONTROL_EVENT_MAX_RAW_BYTES = 256 * 1024
# Document maps (400 entries with bounded section indexes) are the largest
# live artifact class. The host event stores only its digest/length, but the
# publication boundary must still accept those exact already-bounded bytes.
EVIDENCE_PUBLICATION_MAX_RECORD_BYTES = 32 * 1024 * 1024
#: How many components one `window_digest` may name. A window longer than this
#: is stated by its newest components plus a marker for the rest (spec §2.1) —
#: the bound is a bound on the RECORD, and a record about a run never ends it.
WINDOW_DIGEST_MAX_COMPONENTS = 2048
#: The first slot of a truncated window, naming how many older components the
#: record could not name. It is deliberately not an `output_` handle: nothing
#: resolves it, and the full tier declares it out-of-store by name instead of
#: handing a reader bytes that are not the window's.
WINDOW_TRUNCATION_REF = "window_truncated:{dropped}"
#: The answer a call gets when the batch it was in ended before its turn. One
#: code, because one thing happened to it; the reason it was given is the
#: observation it was answered with, sealed as that turn's [C].
#:
#: It lives here, beside the kinds, because it is the ONE refusal that is owed
#: no `loop_decision` — the recurrence ladder reads outcomes and a cancelled
#: call produced none — which makes it the `#cancelled` term of the
#: conservation formula (§2.2 rule 5). The engine that writes it and the
#: reducer that counts it must mean the same string.
CANCELLED_CALL_REFUSAL_CODE = "CALL_NOT_EXECUTED"


def _reject_duplicate_json_keys(json_data: str | bytes | bytearray) -> None:
    """Reject duplicate object members before the JSON parser can collapse them."""

    def reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key!r}")
            value[key] = child
        return value

    json.loads(json_data, object_pairs_hook=reject)


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
    # Plan 6 Stage C: appended, never inserted — the ten kinds above keep their
    # positions so anything reading this tuple by order stays correct.
    "claim_transition",
    # Plan 8 Stage 1: the books of a detached job, closed after the call that
    # started it returned. Appended for the same reason.
    "job_settled",
    # Historical Plan-8 close vocabulary. It remains parseable for archived
    # streams; new runs use `job_live_at_close` and never emit this conflation.
    "job_unsettled",
    # WS2: process truth and evidence-persistence truth are orthogonal. These
    # kinds are appended so every historical tuple position stays stable.
    "job_terminal_observed",
    "job_terminal_unpersisted",
    "job_live_at_close",
    "job_barrier_integrity_failure",
    "completion_claim_decision",
    # WS9: a bounded physical diagnostic/cleanup observation for one
    # registered job group.  It is not a conclusion about project cause.
    "job_stall_observed",
    # Repair lineage v3: appended so every historical tuple position remains
    # stable. The payload contains the exact bounded context used live.
    "repair_context_opened",
    # Evidence authority foundation: the host-owned control stream commits the
    # exact bytes of records mirrored into the project-writable container.  A
    # mirror row alone never becomes publication authority.
    "evidence_publication",
    # The immutable Docker store identity is committed before the first
    # publication so restart cannot rebind one evidence epoch to a replacement
    # container with the same reusable name.
    "evidence_store_bound",
    # Gate truth (spec 2026-08-14 §3.1): a word already delivered to the model
    # may only be replaced out loud. Appended for the same positional reason.
    "gate_outcome_revised",
    # Observation trajectory (spec 2026-08-14 §2.1): one sealed record per
    # turn, stating the exact window the model saw. Appended last, like every
    # kind before it, so no reader keyed on position moves.
    "turn_record",
    # Spec 2026-08-14 §2.2 rule 4: a call that never reached a tool says so.
    # Appended for the same positional reason.
    "refusal_record",
    # Task #53: the controller barrier's own voice. Every other job kind on the
    # barrier path is exceptional, so a run that waited 5,033.9s across 160
    # probes wrote nothing at all. Appended for the same positional reason.
    "job_barrier_wait",
)
ControlEventKind = Literal[
    "planner_response",
    "scheduler_decision",
    "action_envelope",
    "repair_context_opened",
    "forced_action",
    "tool_result",
    "validator_observation",
    "gate_decision",
    "phase_transition",
    "loop_decision",
    "evidence_close",
    "claim_transition",
    "job_settled",
    "job_unsettled",
    "job_terminal_observed",
    "job_terminal_unpersisted",
    "job_live_at_close",
    "job_barrier_integrity_failure",
    "completion_claim_decision",
    "job_stall_observed",
    "evidence_publication",
    "evidence_store_bound",
    "gate_outcome_revised",
    "turn_record",
    "refusal_record",
    "job_barrier_wait",
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

    # Absent only on historical run-pin artifacts. Current writers always set
    # it; metrics-v2 marks a pin without it incomplete and the evaluator
    # rejects it as a current project row.
    run_id: str | None = Field(default=None, min_length=1)

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
        run_id: str,
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
            run_id=run_id,
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
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any):
        del _fields_set
        return cls.model_validate(values)

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any):
        if isinstance(json_data, str):
            raw_size = len(json_data.encode("utf-8"))
        elif isinstance(json_data, (bytes, bytearray)):
            raw_size = len(json_data)
        else:
            raise TypeError("control payload JSON must be str, bytes, or bytearray")
        # Strict bounded payloads never need more wire slack than this; the
        # 32KiB semantic payload cap is enforced after parsing where required.
        if raw_size > STRICT_CONTROL_PAYLOAD_MAX_CANONICAL_BYTES * 2:
            raise ValueError("control payload JSON exceeds its raw byte limit")
        _reject_duplicate_json_keys(json_data)
        return super().model_validate_json(json_data, *args, **kwargs)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ):
        del deep
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(dict(update or {}))
        return type(self).model_validate(payload)


EVIDENCE_MUTABLE_RECORD_KINDS = frozenset(
    {
        "job_obligation",
        "build_requirements",
        "run_pin",
        "document_map",
        "env_overlay",
        "receipt_structure",
        "stall_cleanup",
        "report_metrics",
        "verdict",
    }
)
EVIDENCE_PUBLICATION_GENESIS_SHA256 = "0" * 64


class EvidenceStoreBoundPayload(_StrictPayload):
    """Host commitment binding one evidence run to one immutable store."""

    run_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strict=True,
    )
    store_identity: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        strict=True,
    )


class EvidencePublicationPayload(_StrictPayload):
    """Host-owned commitment to one immutable evidence-record byte string.

    The closed vocabulary prevents a generic file hash from accidentally
    becoming evidence authority.  Contract linkage is absent-preserving: a
    record either commits both the contract identity and body hash or neither.
    """

    record_kind: Literal[
        "invocation_contract",
        "invocation_receipt",
        "receipt_assessment",
        "policy_claim",
        "job_obligation",
        "repair_context",
        "build_requirements",
        "run_pin",
        # Reserved now so wiring another live artifact does not silently widen
        # the authority vocabulary or require a control-event schema change.
        "document_map",
        "env_overlay",
        "receipt_structure",
        "stall_diagnostic",
        "stall_seal",
        "stall_cleanup",
        "report_metrics",
        "testcase_row_input",
        "verdict",
    ]
    record_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strict=True,
    )
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    byte_count: int = Field(
        ge=0,
        le=EVIDENCE_PUBLICATION_MAX_RECORD_BYTES,
        strict=True,
    )
    run_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strict=True,
    )
    contract_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strict=True,
    )
    contract_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    publication_state: Literal["present", "revoked"] = "present"
    logical_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        strict=True,
    )
    revision: int | None = Field(default=None, ge=1, strict=True)
    previous_raw_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    previous_publication_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )

    @model_serializer(mode="wrap")
    def _absent_contract_binding_stays_absent(self, handler):
        data = handler(self)
        for field in (
            "contract_id",
            "contract_hash",
            "logical_artifact_id",
            "revision",
            "previous_raw_sha256",
            "previous_publication_sha256",
        ):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data

    @model_validator(mode="after")
    def _contract_binding_is_absent_or_complete(self) -> "EvidencePublicationPayload":
        contract_id_present = "contract_id" in self.model_fields_set
        contract_hash_present = "contract_hash" in self.model_fields_set
        if contract_id_present != contract_hash_present:
            raise ValueError("publication contract identity and hash must appear together")
        if contract_id_present and (self.contract_id is None or self.contract_hash is None):
            raise ValueError("publication contract identity and hash cannot be null")
        if self.record_kind == "invocation_contract" and (
            self.contract_id != self.record_id or self.contract_hash is None
        ):
            raise ValueError(
                "an invocation-contract publication must bind its own identity and hash"
            )
        mutable = self.record_kind in EVIDENCE_MUTABLE_RECORD_KINDS
        lineage_fields = (
            "logical_artifact_id",
            "revision",
            "previous_raw_sha256",
            "previous_publication_sha256",
        )
        present_lineage = {field for field in lineage_fields if field in self.model_fields_set}
        if mutable:
            if present_lineage != set(lineage_fields) or any(
                getattr(self, field) is None for field in lineage_fields
            ):
                raise ValueError("mutable publication requires one complete revision tuple")
            if self.record_id != self.logical_artifact_id:
                raise ValueError(
                    "mutable publication record_id must equal its stable logical artifact id"
                )
        elif present_lineage:
            raise ValueError("immutable publication cannot carry mutable revision fields")
        if self.publication_state == "revoked":
            if not mutable:
                raise ValueError("only a mutable artifact can be revoked")
            if self.raw_sha256 != EVIDENCE_PUBLICATION_GENESIS_SHA256 or self.byte_count != 0:
                raise ValueError("revocation must carry the canonical empty tombstone")
            if contract_id_present:
                raise ValueError("revocation cannot bind an invocation contract")
        elif self.byte_count < 1 or self.raw_sha256 == EVIDENCE_PUBLICATION_GENESIS_SHA256:
            raise ValueError("present publication must commit non-empty record bytes")
        return self


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
    intent_id: str | None = Field(default=None, min_length=1)
    intent_source: Literal["model", "controller"] | None = None
    action_fingerprint: str | None = Field(default=None, min_length=1)
    trigger_assessment_id: str | None = Field(default=None, min_length=1)
    repair_context_id: str | None = Field(default=None, min_length=1)
    repair_context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    domain_id: str | None = Field(default=None, min_length=1, max_length=256)
    blocking_fact_refs: tuple[str, ...] = Field(default=(), max_length=64)
    repair_hypothesis: str = Field(default="", max_length=4096)
    next_action_kind: str | None = Field(default=None, min_length=1, max_length=256)
    expected_observation: tuple[str, ...] = Field(default=(), max_length=64)
    stop_condition: str = Field(default="", max_length=4096)

    @field_validator("exact_params", mode="before")
    @classmethod
    def _exact_params_are_bounded(cls, value: Any) -> dict[str, Any]:
        return bounded_exact_params(value)

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
        lineage = (
            self.intent_id,
            self.intent_source,
            self.action_fingerprint,
        )
        if any(value is not None for value in lineage) and not all(
            value is not None for value in lineage
        ):
            raise ValueError("action intent identity must be recorded as one complete tuple")
        repair_tuple = (
            self.trigger_assessment_id,
            self.repair_context_id,
            self.repair_context_sha256,
        )
        repair_audit = (
            self.domain_id,
            self.blocking_fact_refs,
            self.repair_hypothesis,
            self.next_action_kind,
            self.expected_observation,
            self.stop_condition,
        )
        repair_linked = any(value is not None for value in repair_tuple) or any(repair_audit)
        if repair_linked:
            if not all(value is not None for value in repair_tuple):
                raise ValueError("repair trigger/context/digest must be one complete tuple")
            if not all(lineage) or self.intent_source != "model":
                raise ValueError("repair-linked envelope requires a complete model intent")
            missing = [
                name
                for name, value in (
                    ("domain_id", self.domain_id),
                    ("blocking_fact_refs", self.blocking_fact_refs),
                    ("repair_hypothesis", self.repair_hypothesis.strip()),
                    ("next_action_kind", self.next_action_kind),
                    ("expected_observation", self.expected_observation),
                    ("stop_condition", self.stop_condition.strip()),
                )
                if not value
            ]
            if missing:
                raise ValueError("repair intent audit is incomplete: " + ", ".join(missing))
        expected = action_envelope_sha256(
            plan_index=self.plan_index,
            tool_call_id=self.tool_call_id,
            tool=self.tool,
            exact_params=self.exact_params,
            intent_id=self.intent_id,
            intent_source=self.intent_source,
            action_fingerprint=self.action_fingerprint,
            trigger_assessment_id=self.trigger_assessment_id,
            repair_context_id=self.repair_context_id,
            repair_context_sha256=self.repair_context_sha256,
            domain_id=self.domain_id,
            blocking_fact_refs=self.blocking_fact_refs or None,
            repair_hypothesis=self.repair_hypothesis or None,
            next_action_kind=self.next_action_kind,
            expected_observation=self.expected_observation or None,
            stop_condition=self.stop_condition or None,
        )
        if self.envelope_sha256 != expected:
            raise ValueError("action envelope hash mismatch")
        if (
            len(canonical_json(self.model_dump(mode="json", exclude_unset=True)).encode("utf-8"))
            > STRICT_CONTROL_PAYLOAD_MAX_CANONICAL_BYTES
        ):
            raise ValueError("action envelope exceeds its canonical byte limit")
        return self

    @model_serializer(mode="wrap")
    def _intent_lineage_absent_stays_absent(self, handler):
        data = handler(self)
        for name in (
            "intent_id",
            "intent_source",
            "action_fingerprint",
            "trigger_assessment_id",
            "repair_context_id",
            "repair_context_sha256",
            "domain_id",
            "blocking_fact_refs",
            "repair_hypothesis",
            "next_action_kind",
            "expected_observation",
            "stop_condition",
        ):
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


class RepairContextOpenedPayload(_StrictPayload):
    """Full bounded context projection that makes repair replay self-contained."""

    source_gate_sequence: int = Field(ge=1)
    source_phase_attempt_id: str = Field(min_length=1, max_length=256)
    context: RepairContext
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _context_hash_matches_projection(self) -> "RepairContextOpenedPayload":
        if repair_context_sha256(self.context) != self.context_sha256:
            raise ValueError("repair context hash mismatch")
        if len(canonical_json(self.model_dump(mode="json")).encode("utf-8")) > (
            STRICT_CONTROL_PAYLOAD_MAX_CANONICAL_BYTES
        ):
            raise ValueError("repair context opened payload exceeds its byte limit")
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
    # Plan 4 primary-coordinate follow-up (live p5v-bigtop-r1): to_snapshot()
    # gained `primary`, and this strict model silently killed every
    # forced_action / gate_decision event that carried it.
    primary: TestCandidatePayload | None = None

    @model_serializer(mode="wrap")
    def _primary_absent_stays_absent(self, handler):
        """Hash stability across schema generations: the action digest is
        recomputed from this dump, so an event recorded WITHOUT `primary`
        (pre-Plan-4 fixtures) must keep dumping without it, while an event
        that carried it (even as null) keeps it."""
        data = handler(self)
        if "primary" not in self.model_fields_set:
            data.pop("primary", None)
        return data

    @model_validator(mode="after")
    def _status_matches_candidates(self) -> "TestCandidateResolutionPayload":
        if self.status == "available" and (
            not self.workspace_root or not self.project_root or not self.candidates
        ):
            raise ValueError("available test-candidate resolution requires roots and candidates")
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
    intent_id: str | None = Field(default=None, min_length=1)
    intent_source: Literal["controller"] | None = None
    action_fingerprint: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _valid_action_digest(self) -> "ForcedActionPayload":
        if (
            forced_action_sha256(
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
                intent_id=self.intent_id,
                intent_source=self.intent_source,
                action_fingerprint=self.action_fingerprint,
            )
            != self.action_sha256
        ):
            raise ValueError("forced action hash mismatch")
        lineage = (self.intent_id, self.intent_source, self.action_fingerprint)
        if any(value is not None for value in lineage) and not all(
            value is not None for value in lineage
        ):
            raise ValueError("forced action intent identity must be complete")
        return self

    @model_serializer(mode="wrap")
    def _intent_lineage_absent_stays_absent(self, handler):
        data = handler(self)
        for name in ("intent_id", "intent_source", "action_fingerprint"):
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


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

    @field_validator("params", mode="before")
    @classmethod
    def _params_are_exact_and_bounded(cls, value: Any) -> dict[str, Any]:
        return bounded_exact_params(value)

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

    @field_validator("params", mode="before")
    @classmethod
    def _params_are_exact_and_bounded(cls, value: Any) -> dict[str, Any]:
        return bounded_exact_params(value)

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
    control_disposition: (
        Literal[
            "terminal_claimable",
            "wait_required",
            "repair_required",
            "harness_recovery_required",
            "terminal_blocked",
        ]
        | None
    ) = None
    blocker_owner: Literal["none", "project", "harness", "unknown"] | None = None

    @model_serializer(mode="wrap")
    def _ownership_absent_stays_absent(self, handler):
        data = handler(self)
        for name in ("control_disposition", "blocker_owner"):
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


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
    control_disposition: (
        Literal[
            "terminal_claimable",
            "wait_required",
            "repair_required",
            "harness_recovery_required",
            "terminal_blocked",
        ]
        | None
    ) = None
    blocker_owner: Literal["none", "project", "harness", "unknown"] | None = None
    # Required by ReplayHeader v3. Absent stays absent for archived v1/v2
    # transcripts and is rejected by the v3 replay policy, not this parser.
    code: str | None = Field(default=None, min_length=1, max_length=256)
    # Spec 2026-08-14 §3.3. `gate_result` is the gate's own serialization,
    # byte-identical to the copy embedded in the tool result that delivered it;
    # the flat fields above are the event's bounded projection of that same
    # object. `supersedes` names the earlier grading this one replaces.
    decision_id: str | None = Field(default=None, min_length=1, max_length=128)
    supersedes: str | None = Field(default=None, min_length=1, max_length=128)
    gate_result: dict[str, Any] | None = None
    # Spec 2026-08-14 §3.4. Names the claim this decision grades, so a reader
    # pairs it with the word delivered for THAT claim rather than with whatever
    # word happened to be delivered last. `key_results` above is bounded like
    # every other flat field; this digest is taken from the claim itself.
    claim_sha256: str | None = Field(default=None, min_length=1, max_length=128)
    # `reason` and `evidence_refs` above describe the validator's grading, not
    # necessarily the model claim. New streams therefore retain the exact
    # model-authored PhaseClaim metadata whose digest is `claim_sha256`.
    # Archived streams predate this snapshot and keep their opaque claim token.
    phase_claim: dict[str, Any] | None = None
    # Model-authored Analyze plans live in a separate bounded artifact. These
    # optional fields preserve the complete PhaseClaim identity for new
    # transcripts while archived pre-plan streams remain parseable.
    execution_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    execution_plan_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _claim_snapshot_is_not_null(self) -> "GateDecisionPayload":
        if "phase_claim" in self.model_fields_set and self.phase_claim is None:
            raise ValueError("gate phase claim snapshot cannot be null")
        if self.phase_claim is not None and not self.claim_sha256:
            raise ValueError("gate phase claim snapshot requires its claim identity")
        return self

    @model_serializer(mode="wrap")
    def _ownership_absent_stays_absent(self, handler):
        data = handler(self)
        for name in (
            "control_disposition",
            "blocker_owner",
            "code",
            "decision_id",
            "supersedes",
            "gate_result",
            "claim_sha256",
            "phase_claim",
            "execution_plan_sha256",
            "execution_plan_ref",
        ):
            if name not in self.model_fields_set:
                data.pop(name, None)
        return data


class GateOutcomeRevisedPayload(_StrictPayload):
    """A word the model already acted on, replaced in the open (spec §3.1).

    The engine may not re-derive an outcome after delivering it. When new
    evidence must change it anyway, this event and the observation the model
    reads are rendered from the same object, and the replacing `gate_decision`
    names `delivered_decision_id` in its `supersedes`.
    """

    phase: str = Field(min_length=1)
    delivered_decision_id: str = Field(min_length=1, max_length=128)
    revised_decision_id: str = Field(min_length=1, max_length=128)
    delivered_outcome: Literal["success", "partial", "failed", "unknown"]
    revised_outcome: Literal["success", "partial", "failed", "unknown"]
    delivered_accepted: bool
    revised_accepted: bool
    reason: str = ""
    code: str = Field(min_length=1, max_length=256)
    observation_text: str = Field(min_length=1)
    source_attempt_id: str | None = None

    @model_validator(mode="after")
    def _a_revision_replaces_a_different_decision(self) -> "GateOutcomeRevisedPayload":
        if self.delivered_decision_id == self.revised_decision_id:
            raise ValueError("a revision must name two distinct gradings")
        if (self.delivered_outcome, self.delivered_accepted) == (
            self.revised_outcome,
            self.revised_accepted,
        ):
            raise ValueError("a revision must name a word that actually moved")
        return self


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


class WindowDigestPayload(_StrictPayload):
    """Component-level reference to the EXACT messages array one turn saw.

    [A] — "what did the model see" — was archaeology: branch history is a
    truncated, compaction-shaped approximation of the rendered window, and
    every observation-poisoning investigation paid to reconstruct it (spec §0).
    A digest ends that. `system_prompt_sha256` names which prompt build spoke;
    `component_refs` names each message of the rendered array, in the order it
    was rendered, so resolving the refs and concatenating them in list order
    reproduces the window byte-for-byte.

    Bytes are stored ONCE: identical messages across turns resolve to the same
    ref. Which is exactly why the list is neither deduplicated nor sorted — two
    identical messages occupy two positions, and a set of refs reconstructs
    nothing. An empty list is the honest statement that no component could be
    stored, never a partial window pretending to be whole.
    """

    system_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Bounded like every other strict field. A window longer than this cannot
    # be stated exactly; the engine keeps the newest components that fit and
    # spends the first slot on a `window_truncated:<n>` marker naming how many
    # older ones are not here, so a short list never passes for a whole window.
    component_refs: tuple[str, ...] = Field(default=(), max_length=WINDOW_DIGEST_MAX_COMPONENTS)

    @field_validator("component_refs")
    @classmethod
    def _every_component_is_named(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not str(ref).strip() for ref in value):
            raise ValueError("a window component reference cannot be empty")
        return value


class TurnRecordPayload(_StrictPayload):
    """One sealed turn: what the model saw, what it said, what it heard.

    Sealed by the engine only, through the same publication path as every
    other control event (spec §2.1). Controller-initiated moves — forced
    actions and engine-generated gate closes — seal records of their own with
    `actor: controller`, in the same `turn_id` sequence, so the harness's turns
    stop being invisible next to the model's.

    Absence is stated, never implied: a turn whose call was refused before an
    envelope existed carries `envelope_ref: None`, a turn nobody was billed for
    carries no tokens, and a turn taken before this run rendered anything at
    all — a controller move ahead of the first model window — carries
    `window_digest: None`. Hashing the empty string instead would have put a
    64-hex prompt identity on the record that resolves to nothing, reads like
    any other prompt hash, and compares equal across every run that sealed one.
    What a record never does is end before it began.
    """

    turn_id: int = Field(ge=1)
    phase: str = Field(min_length=1)
    iteration: int | None = Field(default=None, ge=0)
    actor: Literal["model", "controller"]
    window_digest: WindowDigestPayload | None = None
    envelope_ref: str | None = Field(default=None, min_length=1)
    observation_ref: str | None = Field(default=None, min_length=1)
    gate_decision_id: str | None = Field(default=None, min_length=1)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    t0: str = Field(min_length=1)
    t1: str = Field(min_length=1)

    @model_validator(mode="after")
    def _a_turn_ends_after_it_starts(self) -> "TurnRecordPayload":
        try:
            start = datetime.fromisoformat(self.t0)
            end = datetime.fromisoformat(self.t1)
        except ValueError as exc:
            raise ValueError(f"a turn's timestamps must be ISO-8601: {exc}") from exc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end < start:
            raise ValueError("a turn cannot end before it began")
        return self


class RefusalRecordPayload(_StrictPayload):
    """A call that never reached a tool, stated instead of dropped (§2.2 rule 4).

    Measured silence: a refused call left a `loop_decision` describing an
    execution that never happened, and nothing else — no envelope, because
    nothing was dispatched, and no `tool_result`, because nothing answered
    (cassandra ×2, samza-hello, camel, tapestry-5, camel-quarkus seq
    124/138/216). Every derived turn inherited a hole where a decision was.

    This record stands in BOTH empty places at once. It is the envelope of a
    call nobody accepted — hence `exact_params_sha256`, which is what the
    envelope would have committed, in the one form that stays bounded and lets
    a refusal be compared with the retry that follows it (camel-quarkus seq
    124→125 and 138→139 are the same parameters twice) — and it is the answer
    that call received, which is `refusal_code`: the reason, from the refusal
    itself, never inferred from what happened next.
    """

    tool: str = Field(min_length=1, max_length=256)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    refusal_code: str = Field(min_length=1, max_length=256)
    exact_params_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Spec §2.2 rule 3. A dispatched repair action states its intent in its
    # envelope's own fields; a REFUSED one has no envelope, and the model's
    # whole stated hypothesis lived in branch history and nowhere in the
    # authoritative layer (six calls across the archives, all refused before
    # dispatch). What is recorded here is what the model SUBMITTED — not a
    # validated projection of it, because the reason the call was refused is
    # usually that the submission was not valid.
    repair_intent: dict[str, Any] | None = None

    @field_validator("repair_intent", mode="before")
    @classmethod
    def _submitted_intent_is_bounded(cls, value: Any) -> Any:
        return None if value is None else bounded_exact_params(value)

    @model_serializer(mode="wrap")
    def _an_intent_nobody_submitted_stays_absent(self, handler):
        data = handler(self)
        for field in ("tool_call_id", "repair_intent"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


class EvidenceClosePayload(_StrictPayload):
    reason: Literal[
        "test_terminated",
        "dependents_skipped",
        "aborted",
        "cancelled",
    ]


# The evidence vocabulary a claim moves through (spec §C5). It is spelled out
# here rather than imported because `claim_records` imports THIS module for its
# canonical digests; tests/test_claim_graph.py asserts the two agree.
ClaimEvidenceStatus = Literal[
    "untested", "unknown", "confirmed", "blocked", "contradicted", "not_applicable"
]


class ClaimTransitionPayload(_StrictPayload):
    """One step of a grouped claim-graph commit (plan §Stage C note (a)).

    Every event of a group carries the same `group_id`, and the group ends with
    the terminal record `{group_id, terminal: true}`. Replay treats a group
    with no terminal record as absent, so a run that died mid-commit leaves the
    graph exactly where it was rather than half-retracted.

    Two shapes, one kind: a TRANSITION names the claim and both statuses; the
    TERMINAL record names only its group. Neither may borrow the other's
    fields, so a reader can tell a commit from a mutation without context.
    """

    group_id: str = Field(min_length=1)
    claim_id: str | None = Field(default=None, min_length=1)
    from_status: ClaimEvidenceStatus | None = None
    to_status: ClaimEvidenceStatus | None = None
    cause_assessment_id: str | None = Field(default=None, min_length=1)
    terminal: bool | None = None

    @model_serializer(mode="wrap")
    def _absent_facts_stay_absent(self, handler):
        """A key the event never carried is never dumped back into it.

        Same hash-stability rule `TestCandidateResolutionPayload` learned: any
        digest taken over this dump must see the recorded bytes, not the
        model's defaults.
        """
        data = handler(self)
        for field in ("claim_id", "from_status", "to_status", "cause_assessment_id", "terminal"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data

    @model_validator(mode="after")
    def _one_shape_or_the_other(self) -> "ClaimTransitionPayload":
        if self.terminal:
            if self.claim_id or self.from_status or self.to_status or self.cause_assessment_id:
                raise ValueError("a group-commit record carries only its group id")
            return self
        if not self.claim_id or not self.from_status or not self.to_status:
            raise ValueError("a claim transition requires claim_id, from_status and to_status")
        return self


class JobSettledPayload(_StrictPayload):
    """One detached job's books, closed (Plan 8 §3.2).

    Three facts and no more: which job, which receipt it finally wrote, and
    the terminal exit code it wrote it from. Everything else about the
    settlement is IN that receipt, and a second copy here would be a second
    place to disagree.
    """

    job_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    exit_code: int


class JobUnsettledPayload(_StrictPayload):
    """One detached job the run never heard back from (Plan 8 §3.2).

    The settled branch has been in the stream since Stage 1 landed; this is its
    other half. A job with no terminal exit code is recorded on the verdict as
    `job_unsettled:<job_id>` with the obligation file as provenance, and that is
    a STATE — so it belongs in the transcript, or an offline replay of the run
    reaches evidence-close with a conflict it can neither see nor reproduce.

    `obligation` is the same bounded projection the verdict's fact carries (what
    was dispatched, and where its log is), so the two cannot disagree. Nothing
    here is guessed from the partial log: an unfinished job is neither a pass
    nor a failure, and the payload states only what the dispatch was.
    """

    job_id: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
    obligation: dict[str, Any] = Field(default_factory=dict)


class JobTerminalObservedPayload(_StrictPayload):
    """Small process fact persisted before receipt settlement is attempted."""

    job_id: str = Field(min_length=1)
    exit_code: int
    marker_ref: str = Field(min_length=1)
    observed_at: str = Field(min_length=1)
    obligation_ref: str = Field(min_length=1)


class JobTerminalUnpersistedPayload(_StrictPayload):
    """Terminal process whose complete invocation receipt could not persist."""

    job_id: str = Field(min_length=1)
    exit_code: int
    # An ephemeral handle can fail before a receipt identity can be frozen.
    attempted_receipt_id: str = ""
    persistence_code: str = Field(min_length=1)
    attempt_count: int = Field(ge=0)
    obligation_ref: str = Field(min_length=1)
    log_ref: str = ""
    contract_id: str | None = Field(default=None, min_length=1)


class JobLiveAtClosePayload(_StrictPayload):
    """A process still lacking a terminal marker at the final close boundary."""

    job_id: str = Field(min_length=1)
    obligation_ref: str = Field(min_length=1)
    log_ref: str = ""
    close_reason: str = Field(min_length=1)


class JobBarrierWaitPayload(_StrictPayload):
    """One bounded heartbeat from the controller-owned wait.

    It states what the barrier saw and how much of its own budget is left, and
    it concludes nothing: a wait is not a diagnosis. `progressing` is the
    controller's own progress predicate for THIS observation, carried so a
    frozen log with a live CPU tick is legible in the stream while it happens
    rather than only in a post-mortem. It is null when the controller formed no
    predicate — the first sample for a job has nothing to compare against, and
    a wait that observed nothing at all states nothing.
    """

    job_id: str = Field(min_length=1)
    obligation_ref: str = Field(min_length=1)
    waits: int = Field(ge=1)
    waited_seconds: int = Field(ge=0)
    remaining_seconds: int = Field(ge=0)
    process_state: str = Field(min_length=1)
    log_size: int = Field(ge=0)
    cpu_ticks_delta: int = Field(ge=0)
    artifact_sha256: str = ""
    report_sha256: str = ""
    progressing: bool | None = None


class JobBarrierIntegrityFailurePayload(_StrictPayload):
    """Bounded controller failures that prevented safe barrier reconciliation."""

    failures: tuple[str, ...] = Field(min_length=1)

    @field_validator("failures")
    @classmethod
    def _failures_are_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not failure.strip() for failure in value):
            raise ValueError("job barrier integrity failures must be non-empty strings")
        return value


class CompletionClaimDecisionPayload(_StrictPayload):
    """The authoritative no-op completion decision made by LoopMemory."""

    phase_attempt_id: str = Field(min_length=1)
    claim_kind: Literal["done", "blocked"]
    judge_disposition: Literal[
        "terminal_claimable",
        "wait_required",
        "repair_required",
        "harness_recovery_required",
        "terminal_blocked",
    ]
    blocker_id: str = ""
    mechanical_evidence_digest: str = Field(min_length=1)
    assessment_fingerprints: tuple[str, ...] = ()
    open_job_fingerprints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    target_fingerprint: str = ""
    config_fingerprint: str = ""
    fact_fingerprint: str = ""
    expected_decision: Literal["continue", "agent_no_progress", "not_counted"]
    expected_recurrence_count: int = Field(ge=0)
    expected_reason_code: str = Field(min_length=1)
    expected_close_phase: bool = False


class JobStallObservedPayload(_StrictPayload):
    """One physical stall-control observation, never a project diagnosis."""

    job_id: str = Field(min_length=1)
    obligation_ref: str = Field(min_length=1)
    diagnostic_ref: str = Field(min_length=1)
    diagnostic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation: Literal[
        "cpu_active",
        "io_wait",
        "thread_join_wait",
        "deadlock_signature",
        "unknown",
    ]
    progress_signals: tuple[str, ...] = ()
    controller_code: str = Field(min_length=1)
    evidence_sealed: bool = False
    term_sent: bool = False
    kill_sent: bool = False
    group_live: bool

    @field_validator("progress_signals")
    @classmethod
    def _progress_signals_are_typed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {
            "cpu_active",
            "log_growth",
            "artifact_delta",
            "report_delta",
            "child_process_transition",
        }
        if any(signal not in allowed for signal in value):
            raise ValueError("job stall progress signal is not recognized")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _cleanup_fields_are_consistent(self) -> "JobStallObservedPayload":
        if self.kill_sent and not self.term_sent:
            raise ValueError("KILL cannot precede TERM")
        if (self.term_sent or self.kill_sent) and not self.evidence_sealed:
            raise ValueError("cleanup signals require sealed evidence")
        return self


def job_stall_transition(payload: Mapping[str, Any]) -> str | None:
    """Project only mechanically proven progress/stall into LoopMemory."""

    observed = JobStallObservedPayload.model_validate(payload)
    if observed.progress_signals:
        return "progress"
    if observed.evidence_sealed:
        return "stalled"
    # Empty progress signals also occur when a later probe is incomplete or
    # transport fails. Absence of an observation is not a no-progress fact.
    return None


_PAYLOAD_MODELS: dict[str, type[_StrictPayload]] = {
    "planner_response": PlannerResponsePayload,
    "scheduler_decision": SchedulerDecisionPayload,
    "action_envelope": ActionEnvelopePayload,
    "repair_context_opened": RepairContextOpenedPayload,
    "forced_action": ForcedActionPayload,
    "tool_result": ToolResultPayload,
    "validator_observation": ValidatorObservationPayload,
    "gate_decision": GateDecisionPayload,
    "phase_transition": PhaseTransitionPayload,
    "loop_decision": LoopDecisionPayload,
    "evidence_close": EvidenceClosePayload,
    "claim_transition": ClaimTransitionPayload,
    "job_settled": JobSettledPayload,
    "job_unsettled": JobUnsettledPayload,
    "job_terminal_observed": JobTerminalObservedPayload,
    "job_terminal_unpersisted": JobTerminalUnpersistedPayload,
    "job_live_at_close": JobLiveAtClosePayload,
    "job_barrier_integrity_failure": JobBarrierIntegrityFailurePayload,
    "job_barrier_wait": JobBarrierWaitPayload,
    "completion_claim_decision": CompletionClaimDecisionPayload,
    "job_stall_observed": JobStallObservedPayload,
    "evidence_publication": EvidencePublicationPayload,
    "evidence_store_bound": EvidenceStoreBoundPayload,
    "gate_outcome_revised": GateOutcomeRevisedPayload,
    "turn_record": TurnRecordPayload,
    "refusal_record": RefusalRecordPayload,
}


class ControlEvent(BaseModel):
    """One strict event row. Payload fields are validated per event kind."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    sequence: int = Field(ge=1)
    kind: ControlEventKind
    payload: dict[str, Any]
    source: SourceExcerpt | None = None
    timestamp: str | None = None
    event_id: str | None = None
    run_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_serializer(mode="wrap")
    def _omit_unscoped_run(self, handler):
        data = handler(self)
        if self.run_id is None:
            data.pop("run_id", None)
        return data

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any):
        del _fields_set
        return cls.model_validate(values)

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any):
        if isinstance(json_data, str):
            raw_size = len(json_data.encode("utf-8"))
        elif isinstance(json_data, (bytes, bytearray)):
            raw_size = len(json_data)
        else:
            raise TypeError("control event JSON must be str, bytes, or bytearray")
        if raw_size > CONTROL_EVENT_MAX_RAW_BYTES:
            raise ValueError("control event JSON exceeds its raw byte limit")
        _reject_duplicate_json_keys(json_data)
        return super().model_validate_json(json_data, *args, **kwargs)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "ControlEvent":
        del deep
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(dict(update or {}))
        return type(self).model_validate(payload)

    @model_validator(mode="after")
    def _validate_payload(self) -> "ControlEvent":
        model = _PAYLOAD_MODELS[self.kind].model_validate(self.payload)
        if self.run_id is not None and self.kind in {
            "evidence_store_bound",
            "evidence_publication",
        }:
            if getattr(model, "run_id", None) != self.run_id:
                raise ValueError("control event run identity disagrees with its evidence payload")
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
    intent_id: str | None = None,
    intent_source: str | None = None,
    action_fingerprint: str | None = None,
    trigger_assessment_id: str | None = None,
    repair_context_id: str | None = None,
    repair_context_sha256: str | None = None,
    domain_id: str | None = None,
    blocking_fact_refs: tuple[str, ...] | list[str] | None = None,
    repair_hypothesis: str | None = None,
    next_action_kind: str | None = None,
    expected_observation: tuple[str, ...] | list[str] | None = None,
    stop_condition: str | None = None,
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
    payload: dict[str, Any] = {
        "plan_index": identity,
        "tool": str(tool),
        "exact_params": bounded_exact_params(exact_params),
    }
    lineage = {
        "intent_id": intent_id,
        "intent_source": intent_source,
        "action_fingerprint": action_fingerprint,
        "trigger_assessment_id": trigger_assessment_id,
        "repair_context_id": repair_context_id,
        "repair_context_sha256": repair_context_sha256,
        "domain_id": domain_id,
        "blocking_fact_refs": list(blocking_fact_refs) if blocking_fact_refs is not None else None,
        "repair_hypothesis": repair_hypothesis,
        "next_action_kind": next_action_kind,
        "expected_observation": (
            list(expected_observation) if expected_observation is not None else None
        ),
        "stop_condition": stop_condition,
    }
    if any(value is not None for value in lineage.values()):
        payload.update({key: value for key, value in lineage.items() if value is not None})
    return canonical_sha256(payload)


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
    intent_id: str | None = None,
    intent_source: str | None = None,
    action_fingerprint: str | None = None,
) -> str:
    """Digest the complete harness-owned action contract."""
    payload: dict[str, Any] = {
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
    lineage = {
        "intent_id": intent_id,
        "intent_source": intent_source,
        "action_fingerprint": action_fingerprint,
    }
    if any(value is not None for value in lineage.values()):
        payload.update({key: value for key, value in lineage.items() if value is not None})
    return canonical_sha256(payload)


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
        run_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
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
            resolved_source = (
                source
                if source is None or isinstance(source, SourceExcerpt)
                else SourceExcerpt.model_validate(source)
            )
            event = ControlEvent(
                sequence=sequence,
                kind=kind,
                payload=(
                    payload.model_dump(mode="json")
                    if isinstance(payload, BaseModel)
                    else dict(payload)
                ),
                source=resolved_source,
                timestamp=self._clock(),
                event_id=self._id_factory(sequence),
                run_id=self.run_id,
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
    "CONTROL_EVENT_MAX_RAW_BYTES",
    "CONTROL_EVENT_SCHEMA_VERSION",
    "ControlEvent",
    "ControlEventKind",
    "ControlEventSink",
    "EVIDENCE_MUTABLE_RECORD_KINDS",
    "EVIDENCE_PUBLICATION_GENESIS_SHA256",
    "EVIDENCE_PUBLICATION_MAX_RECORD_BYTES",
    "EvidencePublicationPayload",
    "EvidenceStoreBoundPayload",
    "RefusalRecordPayload",
    "RunPin",
    "SourceExcerpt",
    "SourceFileManifest",
    "TurnRecordPayload",
    "WINDOW_DIGEST_MAX_COMPONENTS",
    "WINDOW_TRUNCATION_REF",
    "WindowDigestPayload",
    "action_envelope_sha256",
    "job_stall_transition",
    "canonical_json",
    "canonical_sha256",
    "compact_control_value",
    "forced_action_sha256",
    "sanitize_config",
]
