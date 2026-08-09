"""Non-prescriptive context for one evidence-triggered model repair turn.

This module intentionally has no command composer.  A repair context may say
what was observed, which constraints apply, which public semantic affordances
exist, and what kinds of observation could answer the blocker.  It cannot say
which parameters, argv, or ordered steps the model should choose.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sag.agent.evidence_records import (
    PublishedNamedJsonRecordStreamRead,
    read_live_published_json_records,
)
from sag.utils.container_io import (
    WRITE_INVALID_ARGUMENTS,
    WRITE_TRANSPORT_FAILED,
    ContainerWriteResult,
    write_container_text_atomic,
)

REPAIR_CONTEXT_SCHEMA_VERSION: Literal[1] = 1
REPAIR_CONTEXT_DIR = "/workspace/.setup_agent/repair_contexts"
REPAIR_CONTEXT_READ = "read"
REPAIR_CONTEXT_MISSING = "missing"
REPAIR_CONTEXT_READ_FAILED = "transport_read_failed"
REPAIR_CONTEXT_INVALID = "invalid_context"
REPAIR_CONTEXT_INVALID_ID = "invalid_identifier"
REPAIR_CONTEXT_LIVE_UNAVAILABLE = "live_context_unavailable"

# The complete canonical context is copied into the control stream.  These are
# API limits, not presentation limits: no writer or event emitter may truncate
# a context and still claim its digest.  Raw limits run before Pydantic can
# deduplicate/coerce a hostile collection into a small canonical value.
REPAIR_CONTEXT_MAX_CANONICAL_BYTES = 32 * 1024
REPAIR_CONTEXT_MAX_RAW_BYTES = 64 * 1024
REPAIR_CONTEXT_MAX_DEPTH = 8
REPAIR_CONTEXT_MAX_TOTAL_ITEMS = 512
REPAIR_CONTEXT_MAX_COLLECTION_ITEMS = 64
REPAIR_CONTEXT_MAX_AFFORDANCES = 32
REPAIR_CONTEXT_MAX_TEXT_CHARS = 4096
REPAIR_CONTEXT_MAX_REF_CHARS = 256
REPAIR_CONTEXT_MAX_IDENTIFIER_CHARS = 256

_REPAIR_CONTEXT_ID = re.compile(r"rcx-[0-9a-f]{12}")

BlockerOwner = Literal["none", "project", "harness", "unknown"]
ConstraintValue = str | int | float | bool


class _StrictFrozenModel(BaseModel):
    """Revalidate copies so a nested proposal cannot bypass strict schemas."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Any:
        """Never expose Pydantic's validation-bypass constructor publicly.

        These records cross persistence and replay boundaries.  A constructed
        instance is therefore just another untrusted payload and must pass the
        same schema as JSON/read/write/model_copy inputs.
        """

        del _fields_set
        return cls.model_validate(values)

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any) -> Any:
        """Reject oversized wire bytes before JSON duplicate keys can collapse.

        Shape validation necessarily runs after parsing, where a hostile JSON
        object may already have reduced thousands of duplicate keys to one.
        The public byte boundary therefore belongs in front of Pydantic's
        parser, not only in the container read helper.
        """

        if isinstance(json_data, str):
            raw_size = len(json_data.encode("utf-8"))
        elif isinstance(json_data, (bytes, bytearray)):
            raw_size = len(json_data)
        else:
            raise TypeError("repair context JSON must be str, bytes, or bytearray")
        if raw_size > REPAIR_CONTEXT_MAX_RAW_BYTES:
            raise ValueError("repair context JSON exceeds its raw byte limit")
        _reject_duplicate_json_keys(json_data)
        return super().model_validate_json(json_data, *args, **kwargs)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Any:
        del deep
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(dict(update or {}))
        return type(self).model_validate(payload)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _reject_duplicate_json_keys(json_data: str | bytes | bytearray) -> None:
    def reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key!r}")
            value[key] = child
        return value

    json.loads(json_data, object_pairs_hook=reject)


def _bounded_text(value: Any, *, maximum: int, field: str) -> str:
    text = _text(value)
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _refs(values: Sequence[Any] | Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    if not isinstance(values, Sequence):
        raise TypeError("references must be a sequence")
    if len(values) > REPAIR_CONTEXT_MAX_COLLECTION_ITEMS:
        raise ValueError("reference collection exceeds its item limit")
    normalized = {
        _bounded_text(value, maximum=REPAIR_CONTEXT_MAX_REF_CHARS, field="reference")
        for value in values
        if _text(value)
    }
    return tuple(sorted(normalized))


def _validate_raw_shape(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    """Reject deep/wide raw inputs before normalization can hide their size."""

    if budget is None:
        budget = [0]
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", round_trip=True)
    if depth > REPAIR_CONTEXT_MAX_DEPTH:
        raise ValueError("repair context exceeds its nesting-depth limit")
    if isinstance(value, Mapping):
        if len(value) > REPAIR_CONTEXT_MAX_COLLECTION_ITEMS:
            raise ValueError("repair context mapping exceeds its item limit")
        budget[0] += len(value)
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("repair context keys must be strings")
            if len(key) > REPAIR_CONTEXT_MAX_IDENTIFIER_CHARS:
                raise ValueError("repair context key exceeds its length limit")
            _validate_raw_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > REPAIR_CONTEXT_MAX_COLLECTION_ITEMS:
            raise ValueError("repair context collection exceeds its item limit")
        budget[0] += len(value)
        for child in value:
            _validate_raw_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        if len(value) > REPAIR_CONTEXT_MAX_TEXT_CHARS:
            raise ValueError("repair context string exceeds its length limit")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("repair context numbers must be finite")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"repair context contains unsupported {type(value).__name__}")
    if budget[0] > REPAIR_CONTEXT_MAX_TOTAL_ITEMS:
        raise ValueError("repair context exceeds its total-item limit")


def _canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def repair_context_identity(trigger_assessment_id: Any) -> str:
    """Stable one-context-per-trigger identity for replay/idempotence."""

    material = _text(trigger_assessment_id).encode("utf-8")
    if not material:
        raise ValueError("trigger_assessment_id is required")
    return "rcx-" + hashlib.sha256(material).hexdigest()[:12]


class RepairConstraint(_StrictFrozenModel):
    """One typed safety/applicability constraint, never an action step."""

    constraint_id: str
    kind: str
    subject: str
    relation: str
    value: ConstraintValue
    source_refs: tuple[str, ...] = ()

    @field_validator("constraint_id", "kind", "subject", "relation")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = _bounded_text(
            value,
            maximum=REPAIR_CONTEXT_MAX_IDENTIFIER_CHARS,
            field="constraint identifier",
        )
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value: ConstraintValue) -> ConstraintValue:
        if isinstance(value, str) and len(value) > REPAIR_CONTEXT_MAX_TEXT_CHARS:
            raise ValueError("constraint value exceeds its length limit")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("constraint value must be finite")
        return value

    @field_validator("source_refs", mode="before")
    @classmethod
    def _canonical_refs(cls, value: Any) -> tuple[str, ...]:
        return _refs(value)


class ConstraintSet(_StrictFrozenModel):
    """A bounded group of facts that limits, but never selects, an action."""

    constraints: tuple[RepairConstraint, ...] = Field(
        default=(), max_length=REPAIR_CONTEXT_MAX_COLLECTION_ITEMS
    )
    source_refs: tuple[str, ...] = ()

    @field_validator("source_refs", mode="before")
    @classmethod
    def _canonical_refs(cls, value: Any) -> tuple[str, ...]:
        return _refs(value)


class ToolSemanticAffordance(_StrictFrozenModel):
    """Neutral public capability names; no arguments, examples, or ordering."""

    tool: str
    # Name of the public parameter that selects a semantic action.  ``None``
    # means the tool itself is the action kind (bash/search); ``"action"``
    # with an empty enum covers selector-bearing tools such as file I/O.
    action_parameter: Literal["action"] | None = None
    action_kinds: tuple[str, ...] = Field(
        default=(), max_length=REPAIR_CONTEXT_MAX_COLLECTION_ITEMS
    )
    constraint_refs: tuple[str, ...] = ()

    @field_validator("tool")
    @classmethod
    def _required_tool(cls, value: str) -> str:
        text = _bounded_text(
            value,
            maximum=REPAIR_CONTEXT_MAX_IDENTIFIER_CHARS,
            field="tool",
        )
        if not text:
            raise ValueError("tool must not be empty")
        return text.lower()

    @field_validator("action_kinds", mode="before")
    @classmethod
    def _canonical_action_kinds(cls, value: Any) -> tuple[str, ...]:
        return tuple(sorted({kind.lower() for kind in _refs(value)}))

    @field_validator("constraint_refs", mode="before")
    @classmethod
    def _canonical_constraint_refs(cls, value: Any) -> tuple[str, ...]:
        return _refs(value)

    @model_validator(mode="after")
    def _selector_owns_action_kinds(self) -> "ToolSemanticAffordance":
        if self.action_kinds and self.action_parameter is None:
            raise ValueError("action_kinds require action_parameter")
        return self


class RepairFingerprintSet(_StrictFrozenModel):
    """Current mechanical pins; absent facts remain absent rather than null."""

    target_sha: str | None = None
    survey_fingerprint: str | None = None
    config_fingerprint: str | None = None
    document_map_fingerprint: str | None = None
    fact_epoch: int | None = None

    @field_validator(
        "target_sha",
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
    )
    @classmethod
    def _bounded_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            maximum=REPAIR_CONTEXT_MAX_REF_CHARS,
            field="fingerprint",
        )


class RepairContext(_StrictFrozenModel):
    """Facts and policy bounds supplied to a model-owned repair turn."""

    schema_version: Literal[1] = REPAIR_CONTEXT_SCHEMA_VERSION
    repair_context_id: str
    trigger_assessment_id: str
    trigger_receipt_id: str | None = None
    domain_id: str | None = None
    fingerprints: RepairFingerprintSet | None = None
    typed_failure_or_capability: str
    blocker_owner: BlockerOwner
    observed_fact_refs: tuple[str, ...] = ()
    constraint_set: ConstraintSet = Field(default_factory=ConstraintSet)
    allowed_tool_affordances: tuple[ToolSemanticAffordance, ...] = Field(
        default=(), max_length=REPAIR_CONTEXT_MAX_AFFORDANCES
    )
    admissible_observation_types: tuple[str, ...] = ()
    supporting_claim_ids: tuple[str, ...] = ()
    open_conflict_refs: tuple[str, ...] = ()

    @property
    def typed_blocker(self) -> str:
        return self.typed_failure_or_capability

    @model_validator(mode="before")
    @classmethod
    def _raw_input_is_bounded(cls, value: Any) -> Any:
        if isinstance(value, RepairContext):
            value = value.model_dump(mode="python", round_trip=True)
        _validate_raw_shape(value)
        return value

    @field_validator(
        "repair_context_id",
        "trigger_assessment_id",
        "typed_failure_or_capability",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        maximum = (
            REPAIR_CONTEXT_MAX_TEXT_CHARS
            if cls is RepairContext and value and not str(value).startswith(("rcx-", "asm-"))
            else REPAIR_CONTEXT_MAX_IDENTIFIER_CHARS
        )
        text = _bounded_text(value, maximum=maximum, field="repair context field")
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator(
        "observed_fact_refs",
        "admissible_observation_types",
        "supporting_claim_ids",
        "open_conflict_refs",
        mode="before",
    )
    @classmethod
    def _canonical_refs(cls, value: Any) -> tuple[str, ...]:
        return _refs(value)

    @model_validator(mode="after")
    def _engine_owned_identity(self) -> "RepairContext":
        expected = repair_context_identity(self.trigger_assessment_id)
        if self.repair_context_id != expected:
            raise ValueError("repair_context_id must be derived from trigger_assessment_id")
        affordance_tools = tuple(item.tool for item in self.allowed_tool_affordances)
        if len(affordance_tools) != len(set(affordance_tools)):
            raise ValueError("repair context tool affordances must be unique")
        if len(_canonical_payload_bytes(self.model_dump(mode="json"))) > (
            REPAIR_CONTEXT_MAX_CANONICAL_BYTES
        ):
            raise ValueError("repair context exceeds its canonical byte limit")
        return self


def repair_context_canonical_json(context: RepairContext | Mapping[str, Any]) -> str:
    """The exact bounded bytes used by persistence, control events and hashes."""

    payload = (
        context.model_dump(mode="python", round_trip=True)
        if isinstance(context, RepairContext)
        else context
    )
    validated = RepairContext.model_validate(payload)
    body = _canonical_payload_bytes(validated.model_dump(mode="json"))
    if len(body) > REPAIR_CONTEXT_MAX_CANONICAL_BYTES:  # defensive public boundary
        raise ValueError("repair context exceeds its canonical byte limit")
    return body.decode("utf-8")


def repair_context_sha256(context: RepairContext | Mapping[str, Any]) -> str:
    return hashlib.sha256(repair_context_canonical_json(context).encode("utf-8")).hexdigest()


def repair_context_reference_set(
    context: RepairContext,
    *,
    affordance: ToolSemanticAffordance | None = None,
) -> frozenset[str]:
    """Every provenance ref the active context actually exposes to the model."""

    refs = {
        context.trigger_assessment_id,
        *context.observed_fact_refs,
        *context.supporting_claim_ids,
        *context.open_conflict_refs,
        *context.constraint_set.source_refs,
    }
    for constraint in context.constraint_set.constraints:
        refs.update(constraint.source_refs)
    if affordance is not None:
        refs.update(affordance.constraint_refs)
    return frozenset(ref for ref in refs if ref)


@dataclass(frozen=True)
class RepairContextReadResult:
    """Typed outcome of reading one engine-owned repair context."""

    context: RepairContext | None
    code: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.context is not None and self.code == REPAIR_CONTEXT_READ


def repair_context_path(repair_context_id: Any) -> str:
    """Return the sole persistence path allowed for an engine-owned id."""

    identifier = _text(repair_context_id)
    if _REPAIR_CONTEXT_ID.fullmatch(identifier) is None:
        raise ValueError("invalid repair_context_id")
    return f"{REPAIR_CONTEXT_DIR}/{identifier}.json"


def write_repair_context(execute: Any, context: Any) -> ContainerWriteResult:
    """Atomically persist one strict context without a shell-text fallback.

    Returning the shared transport result preserves the exact failure class
    (invalid arguments, write, validation, or publish) for the controller.
    The caller cannot select a path: it is derived exclusively from the
    context's engine-owned identity.
    """

    try:
        payload = (
            context.model_dump(mode="python", round_trip=True)
            if isinstance(context, RepairContext)
            else context
        )
        validated = RepairContext.model_validate(payload)
        path = repair_context_path(validated.repair_context_id)
        body = repair_context_canonical_json(validated)
    except (TypeError, ValueError, ValidationError):
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)
    try:
        result = write_container_text_atomic(
            execute,
            path,
            body,
            validate_json=True,
        )
        if not result.persisted:
            return result
        # Lazy by construction: control_events imports RepairContext for its
        # strict event payload, while evidence_publications imports
        # control_events for the host ledger schema.
        from sag.agent.evidence_publications import publish_evidence_bytes

        publication = publish_evidence_bytes(
            execute,
            record_kind="repair_context",
            record_id=validated.repair_context_id,
            raw=body.encode("utf-8"),
        )
        if publication.published:
            return result
        return ContainerWriteResult(
            False,
            publication.status,
            bytes_written=result.bytes_written,
            sha256=result.sha256,
        )
    except Exception:
        return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)


def read_repair_context(orchestrator: Any, repair_context_id: Any) -> RepairContextReadResult:
    """Read one container-mirror context for forensic/diagnostic use only.

    Both an orchestrator and its execute callback are accepted. A missing file,
    an unavailable transport, and invalid persisted JSON/schema remain three
    different facts; none is silently treated as a usable repair context.

    This function intentionally does not grant live or restart authority.  The
    project and model can mutate the container mirror, so control decisions
    must use :func:`read_live_repair_context` (or recover the complete context
    from the host control stream) instead.
    """

    try:
        path = repair_context_path(repair_context_id)
    except ValueError:
        return RepairContextReadResult(None, REPAIR_CONTEXT_INVALID_ID)

    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        execute = orchestrator if callable(orchestrator) else None
    if execute is None:
        return RepairContextReadResult(None, REPAIR_CONTEXT_READ_FAILED)

    command = f"cat -- {shlex.quote(path)} 2>/dev/null"
    try:
        try:
            result = execute(command, workdir=None, timeout=30)
        except TypeError:
            result = execute(command)
    except Exception:
        return RepairContextReadResult(None, REPAIR_CONTEXT_READ_FAILED)

    if not isinstance(result, Mapping):
        return RepairContextReadResult(None, REPAIR_CONTEXT_READ_FAILED)
    exit_code = result.get("exit_code")
    if exit_code == -1 or result.get("dispatch_status"):
        return RepairContextReadResult(None, REPAIR_CONTEXT_READ_FAILED)
    succeeded = result.get("success")
    if succeeded is None:
        succeeded = exit_code == 0
    if not succeeded:
        if exit_code == 1:
            return RepairContextReadResult(None, REPAIR_CONTEXT_MISSING)
        return RepairContextReadResult(None, REPAIR_CONTEXT_READ_FAILED)

    raw = str(result.get("output") or "")
    if len(raw.encode("utf-8")) > REPAIR_CONTEXT_MAX_RAW_BYTES:
        return RepairContextReadResult(None, REPAIR_CONTEXT_INVALID)
    try:
        context = RepairContext.model_validate_json(raw)
    except (TypeError, ValueError, ValidationError):
        return RepairContextReadResult(None, REPAIR_CONTEXT_INVALID)
    if context.repair_context_id != _text(repair_context_id):
        return RepairContextReadResult(None, REPAIR_CONTEXT_INVALID)
    return RepairContextReadResult(context, REPAIR_CONTEXT_READ)


def validate_repair_context_record(
    payload: Mapping[str, Any],
    expected_id: str,
) -> dict[str, Any]:
    """Validate one exact persisted RepairContext and its atomic basename.

    Host publication authenticates bytes, but does not interpret their record
    kind.  This boundary separately proves the closed schema, engine-derived
    identity, canonical persisted field set, and ``<repair_context_id>.json``
    filename selected by the named transport.
    """

    try:
        repair_context_path(expected_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("repair context filename identity is invalid") from exc
    try:
        body = dict(payload)
        context = RepairContext.model_validate(body)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("repair context schema is not canonical") from exc
    if context.repair_context_id != expected_id:
        raise ValueError("repair context id does not match filename")
    normalized = context.model_dump(mode="json")
    if body != normalized:
        raise ValueError("repair context payload differs from canonical persisted fields")
    # Re-run the public canonical boundary so its byte limit remains part of
    # every live validation path, not just construction and writing.
    repair_context_canonical_json(context)
    return normalized


def read_live_repair_context_ledger(source: Any) -> PublishedNamedJsonRecordStreamRead:
    """Read the complete host-authorized immutable RepairContext ledger.

    The container directory is only a mirror.  A live ledger must preserve
    exact filenames and bytes, validate every context, authorize every raw
    digest through the host control stream, and equal the host's complete
    immutable expected ID set.  Any malformed, future, duplicated, missing,
    tampered, or container-only record therefore invalidates the whole read.
    """

    return read_live_published_json_records(
        source,
        REPAIR_CONTEXT_DIR,
        record_kind="repair_context",
        validator=validate_repair_context_record,
    )


def read_live_repair_context(
    source: Any,
    repair_context_id: Any,
) -> RepairContextReadResult:
    """Select one context only from a complete host-authorized live ledger."""

    try:
        expected_id = _text(repair_context_id)
        repair_context_path(expected_id)
    except (TypeError, ValueError):
        return RepairContextReadResult(None, REPAIR_CONTEXT_INVALID_ID)

    ledger = read_live_repair_context_ledger(source)
    if not ledger.complete or ledger.conflict is not None:
        detail = ledger.detail or ledger.conflict or "repair context ledger is incomplete"
        return RepairContextReadResult(
            None,
            REPAIR_CONTEXT_LIVE_UNAVAILABLE,
            detail,
        )
    matches = [record for record in ledger.records if record.source.record_id == expected_id]
    if not matches:
        return RepairContextReadResult(None, REPAIR_CONTEXT_MISSING)
    if len(matches) != 1:  # defensive: named framing already rejects duplicates
        return RepairContextReadResult(
            None,
            REPAIR_CONTEXT_LIVE_UNAVAILABLE,
            "repair context ledger contains duplicate identity",
        )
    try:
        context = RepairContext.model_validate(matches[0].payload)
    except (TypeError, ValueError, ValidationError) as exc:  # defensive projection
        return RepairContextReadResult(
            None,
            REPAIR_CONTEXT_LIVE_UNAVAILABLE,
            f"validated repair context could not be projected: {exc}",
        )
    return RepairContextReadResult(context, REPAIR_CONTEXT_READ)


def build_repair_context(
    *,
    trigger_assessment_id: str,
    typed_blocker: str,
    blocker_owner: BlockerOwner,
    trigger_receipt_id: str | None = None,
    domain_id: str | None = None,
    fingerprints: RepairFingerprintSet | Mapping[str, Any] | None = None,
    observed_fact_refs: Sequence[str] = (),
    constraint_set: ConstraintSet | Mapping[str, Any] | None = None,
    allowed_tool_affordances: Sequence[ToolSemanticAffordance | Mapping[str, Any]] = (),
    admissible_observation_types: Sequence[str] = (),
    supporting_claim_ids: Sequence[str] = (),
    open_conflict_refs: Sequence[str] = (),
) -> RepairContext:
    """Policy-layer factory; it has no parameter that could carry a proposal."""

    return RepairContext.model_validate(
        {
            "repair_context_id": repair_context_identity(trigger_assessment_id),
            "trigger_assessment_id": trigger_assessment_id,
            "trigger_receipt_id": _text(trigger_receipt_id) or None,
            "domain_id": _text(domain_id) or None,
            "fingerprints": fingerprints,
            "typed_failure_or_capability": typed_blocker,
            "blocker_owner": blocker_owner,
            "observed_fact_refs": observed_fact_refs,
            "constraint_set": constraint_set or ConstraintSet(),
            "allowed_tool_affordances": allowed_tool_affordances,
            "admissible_observation_types": admissible_observation_types,
            "supporting_claim_ids": supporting_claim_ids,
            "open_conflict_refs": open_conflict_refs,
        }
    )
