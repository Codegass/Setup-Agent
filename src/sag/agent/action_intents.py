"""Engine-owned executable intent identity.

An :class:`ActionIntent` is the one public call the model or controller has
already chosen.  It is not a plan and it is not a repair recommendation.  The
engine source factory assigns provenance and identity; untrusted/model input
cannot self-declare either one.

The action fingerprint deliberately covers only the executable identity:
domain, tool and canonical public parameters.  Tool-call ids, evidence-ref
ordering and explanatory prose remain useful lineage, but none of them can
turn an otherwise identical retry into a materially new action.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .repair_contexts import RepairContext, ToolSemanticAffordance

ACTION_INTENT_SCHEMA_VERSION: Literal[1] = 1

ACTION_INTENT_MAX_CANONICAL_BYTES = 32 * 1024
ACTION_INTENT_MAX_RAW_BYTES = 64 * 1024
ACTION_PARAMS_MAX_CANONICAL_BYTES = 32 * 1024
ACTION_PARAMS_MAX_DEPTH = 8
ACTION_PARAMS_MAX_TOTAL_ITEMS = 512
ACTION_PARAMS_MAX_COLLECTION_ITEMS = 64
ACTION_PARAM_MAX_STRING_CHARS = 16 * 1024
ACTION_INTENT_MAX_TEXT_CHARS = 4096
ACTION_INTENT_MAX_REF_CHARS = 256
ACTION_INTENT_MAX_REFS = 64
_CONTRACT_ID = re.compile(r"ic-[0-9a-f]{12}")

ActionIntentSource = Literal["model", "controller"]

_NON_ACTION_PARAM_KEYS = frozenset(
    {
        "analysis",
        "call_id",
        "comment",
        "explanation",
        "expected_observation",
        "expected_observations",
        "message",
        "notes",
        "rationale",
        "reason",
        "repair_hypothesis",
        "stop_condition",
        "thought",
        "tool_call_id",
    }
)
_REF_KEYS = frozenset(
    {
        "blocking_fact_refs",
        "claim_refs",
        "evidence_refs",
        "fact_refs",
        "refs",
        "supporting_claim_ids",
    }
)


class _StrictFrozenModel(BaseModel):
    """Keep ``model_copy(update=...)`` inside the same validation boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Any:
        del _fields_set
        return cls.model_validate(values)

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any) -> Any:
        """Enforce wire bounds before duplicate JSON keys can disappear."""

        if isinstance(json_data, str):
            raw_size = len(json_data.encode("utf-8"))
        elif isinstance(json_data, (bytes, bytearray)):
            raw_size = len(json_data)
        else:
            raise TypeError("action intent JSON must be str, bytes, or bytearray")
        if raw_size > ACTION_INTENT_MAX_RAW_BYTES:
            raise ValueError("action intent JSON exceeds its raw byte limit")
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


def semantic_action_kind(value: Any) -> str:
    """Normalize only the derived audit kind, never executable parameters."""

    normalized = _text(value).lower()
    if not normalized:
        raise ValueError("semantic action kind must not be empty")
    if len(normalized) > ACTION_INTENT_MAX_REF_CHARS:
        raise ValueError("semantic action kind exceeds its length limit")
    return normalized


def validate_repair_action_affordance(
    *,
    tool: Any,
    params: Mapping[str, Any],
    next_action_kind: Any,
    context: RepairContext,
) -> ToolSemanticAffordance:
    """Bind a model's public call to exactly one active semantic affordance.

    A context exposes either a selector-bearing ``action`` parameter or a
    selector-free tool action.  It cannot introduce arbitrary selector names
    such as ``command``/``operation``. This validator derives a normalized
    audit kind without rewriting the executable parameter value.
    """

    tool_name = semantic_action_kind(tool)
    matches = tuple(
        affordance
        for affordance in context.allowed_tool_affordances
        if affordance.tool == tool_name
    )
    if len(matches) != 1:
        raise ValueError("repair action tool has no unique active affordance")
    affordance = matches[0]
    if affordance.action_parameter is None:
        canonical_kind = tool_name
    else:
        if "action" not in params:
            raise ValueError("repair action omits its affordance action parameter")
        canonical_kind = semantic_action_kind(params.get("action"))
        if affordance.action_kinds and canonical_kind not in affordance.action_kinds:
            raise ValueError("repair action kind is outside the active affordance")
    if semantic_action_kind(next_action_kind) != canonical_kind:
        raise ValueError("repair next_action_kind differs from the canonical action")
    return affordance


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _normalized_refs(values: Sequence[Any] | Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    if not isinstance(values, Sequence):
        raise TypeError("references must be a sequence")
    if len(values) > ACTION_INTENT_MAX_REFS:
        raise ValueError("reference collection exceeds its item limit")
    normalized = {_text(value) for value in values if _text(value)}
    if any(len(value) > ACTION_INTENT_MAX_REF_CHARS for value in normalized):
        raise ValueError("reference exceeds its length limit")
    return tuple(sorted(normalized))


def _is_ref_key(key: str) -> bool:
    normalized = key.strip().lower()
    return (
        normalized in _REF_KEYS or normalized.endswith("_refs") or normalized.endswith("_ref_ids")
    )


def _validate_raw_json_shape(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> None:
    """Bound raw parameters before ignored prose/ref sorting can shrink them."""

    if budget is None:
        budget = [0]
    if depth > ACTION_PARAMS_MAX_DEPTH:
        raise ValueError("action parameters exceed their nesting-depth limit")
    if isinstance(value, Mapping):
        if len(value) > ACTION_PARAMS_MAX_COLLECTION_ITEMS:
            raise ValueError("action parameter mapping exceeds its item limit")
        budget[0] += len(value)
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("action parameter keys must be strings")
            if not key.strip() or len(key) > ACTION_INTENT_MAX_REF_CHARS:
                raise ValueError("action parameter key is empty or oversized")
            _validate_raw_json_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > ACTION_PARAMS_MAX_COLLECTION_ITEMS:
            raise ValueError("action parameter collection exceeds its item limit")
        budget[0] += len(value)
        for child in value:
            _validate_raw_json_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        if len(value) > ACTION_PARAM_MAX_STRING_CHARS:
            raise ValueError("action parameter string exceeds its length limit")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("action parameters must not contain NaN or infinity")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"action parameters must be JSON values, got {type(value).__name__}")
    if budget[0] > ACTION_PARAMS_MAX_TOTAL_ITEMS:
        raise ValueError("action parameters exceed their total-item limit")


def _canonical_value(value: Any, *, parent_key: str = "") -> Any:
    """Return a JSON-safe value with only semantically unordered refs sorted."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("action parameters must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(raw_key, str):
                raise TypeError("action parameter keys must be strings")
            key = raw_key.strip()
            if not key or key.lower() in _NON_ACTION_PARAM_KEYS:
                continue
            result[key] = _canonical_value(item, parent_key=key)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_canonical_value(item) for item in value]
        if _is_ref_key(parent_key):
            # Reference order has no executable meaning.  Canonical JSON is a
            # total ordering even when refs are represented by small objects.
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return items
    raise TypeError(f"action parameters must be JSON values, got {type(value).__name__}")


def _exact_json_value(value: Any) -> Any:
    """Copy a bounded JSON value without dropping or truncating any field."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("exact action parameters must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("exact action parameter keys must be strings")
            if not raw_key or raw_key != raw_key.strip():
                raise ValueError(
                    "exact action parameter keys must be non-empty and whitespace-canonical"
                )
            result[raw_key] = _exact_json_value(child)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_exact_json_value(child) for child in value]
    raise TypeError(f"exact action parameters must be JSON values, got {type(value).__name__}")


def bounded_exact_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate event/dispatch params exactly; never redact, compact or sort lists."""

    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError("exact action params must be a mapping")
    _validate_raw_json_shape(params)
    exact = _exact_json_value(params)
    if not isinstance(exact, dict):  # pragma: no cover - public type is a mapping
        raise TypeError("exact action params must remain a mapping")
    body = json.dumps(
        exact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > ACTION_PARAMS_MAX_CANONICAL_BYTES:
        raise ValueError("exact action parameters exceed their canonical byte limit")
    return exact


def canonical_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Canonicalize public tool parameters without inventing backend argv."""

    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError("action params must be a mapping")
    _validate_raw_json_shape(params)
    canonical = _canonical_value(params)
    if not isinstance(canonical, dict):  # defensive: the public type is a mapping
        raise TypeError("action params must canonicalize to a mapping")
    body = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > ACTION_PARAMS_MAX_CANONICAL_BYTES:
        raise ValueError("action parameters exceed their canonical byte limit")
    return canonical


def action_fingerprint(
    *,
    domain_id: Any,
    tool: Any,
    params: Mapping[str, Any] | None,
    tool_call_id: Any = None,
    prose: Any = None,
    refs: Sequence[Any] = (),
) -> str:
    """Fingerprint the executable identity and deliberately ignore lineage.

    ``tool_call_id``, ``prose`` and ``refs`` are accepted so call sites cannot
    accidentally fold them into a parallel hash.  They are intentionally not
    read.  Different domain/tool/canonical params always produce a different
    digest (subject to the hash's cryptographic collision bound).
    """

    del tool_call_id, prose, refs
    domain = _text(domain_id)
    tool_name = _text(tool).lower()
    if not domain:
        raise ValueError("domain_id is required")
    if not tool_name:
        raise ValueError("tool is required")
    payload = {
        "domain_id": domain,
        "tool": tool_name,
        "canonical_params": canonical_params(params),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "act-" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class ActionIntentSubmission(_StrictFrozenModel):
    """Model/controller call data before the engine assigns provenance.

    There is intentionally no ``source``, ``intent_id`` or fingerprint field.
    ``extra='forbid'`` makes provenance self-attestation a schema error.
    """

    domain_id: str
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    blocking_fact_refs: tuple[str, ...] = ()
    repair_hypothesis: str = ""
    next_action_kind: str = ""
    expected_observation: tuple[str, ...] = ()
    stop_condition: str = ""

    @field_validator("domain_id", "tool")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = _text(value)
        if not text:
            raise ValueError("field must not be empty")
        if len(text) > ACTION_INTENT_MAX_REF_CHARS:
            raise ValueError("field exceeds its length limit")
        return text

    @field_validator("repair_hypothesis", "stop_condition")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        text = _text(value)
        if len(text) > ACTION_INTENT_MAX_TEXT_CHARS:
            raise ValueError("action intent text exceeds its length limit")
        return text

    @field_validator("next_action_kind")
    @classmethod
    def _canonical_next_action_kind(cls, value: str) -> str:
        text = _text(value)
        return semantic_action_kind(text) if text else ""

    @field_validator("params", mode="before")
    @classmethod
    def _canonical_params(cls, value: Any) -> dict[str, Any]:
        return canonical_params(value)

    @field_validator("blocking_fact_refs", "expected_observation", mode="before")
    @classmethod
    def _canonical_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalized_refs(value)

    @model_validator(mode="after")
    def _complete_submission_is_bounded(self) -> "ActionIntentSubmission":
        if len(_canonical_model_bytes(self)) > ACTION_INTENT_MAX_CANONICAL_BYTES:
            raise ValueError("action intent submission exceeds its canonical byte limit")
        return self


class ActionIntent(_StrictFrozenModel):
    """One engine-authored identity for a model/controller public action."""

    schema_version: Literal[1] = ACTION_INTENT_SCHEMA_VERSION
    intent_id: str
    source: ActionIntentSource
    domain_id: str
    tool: str
    canonical_params: dict[str, Any] = Field(default_factory=dict)
    action_fingerprint: str
    trigger_assessment_id: str | None = None
    repair_context_id: str | None = None
    repair_context_sha256: str | None = None
    predecessor_contract_id: str | None = None
    blocking_fact_refs: tuple[str, ...] = ()
    repair_hypothesis: str = ""
    next_action_kind: str
    expected_observation: tuple[str, ...] = ()
    stop_condition: str = ""

    @field_validator("intent_id", "domain_id", "tool")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = _text(value)
        if not text:
            raise ValueError("field must not be empty")
        if len(text) > ACTION_INTENT_MAX_REF_CHARS:
            raise ValueError("field exceeds its length limit")
        return text

    @field_validator("next_action_kind")
    @classmethod
    def _canonical_next_action_kind(cls, value: str) -> str:
        return semantic_action_kind(value)

    @field_validator(
        "trigger_assessment_id",
        "repair_context_id",
    )
    @classmethod
    def _bounded_optional_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = _text(value)
        if not text or len(text) > ACTION_INTENT_MAX_REF_CHARS:
            raise ValueError("optional action-intent identifier is empty or oversized")
        return text

    @field_validator("predecessor_contract_id")
    @classmethod
    def _valid_predecessor_contract_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = _text(value)
        if _CONTRACT_ID.fullmatch(text) is None:
            raise ValueError("predecessor_contract_id is not a canonical contract identity")
        return text

    @field_validator("repair_context_sha256")
    @classmethod
    def _valid_repair_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _text(value).lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("repair_context_sha256 must be a SHA-256 digest")
        return normalized

    @field_validator("repair_hypothesis", "stop_condition")
    @classmethod
    def _bounded_repair_text(cls, value: str) -> str:
        text = _text(value)
        if len(text) > ACTION_INTENT_MAX_TEXT_CHARS:
            raise ValueError("repair intent text exceeds its length limit")
        return text

    @field_validator("canonical_params", mode="before")
    @classmethod
    def _canonical_params(cls, value: Any) -> dict[str, Any]:
        return canonical_params(value)

    @field_validator("blocking_fact_refs", "expected_observation", mode="before")
    @classmethod
    def _canonical_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalized_refs(value)

    @model_validator(mode="after")
    def _fingerprint_matches_action(self) -> "ActionIntent":
        expected = action_fingerprint(
            domain_id=self.domain_id,
            tool=self.tool,
            params=self.canonical_params,
        )
        if self.action_fingerprint != expected:
            raise ValueError("action_fingerprint does not match domain/tool/canonical_params")
        selected_action = self.canonical_params.get("action")
        if (
            selected_action is not None
            and semantic_action_kind(selected_action) != self.next_action_kind
        ):
            raise ValueError("next_action_kind must match canonical_params.action")
        repair_tuple = (
            self.trigger_assessment_id,
            self.repair_context_id,
            self.repair_context_sha256,
        )
        if any(value is not None for value in repair_tuple) and not all(
            value is not None for value in repair_tuple
        ):
            raise ValueError("repair trigger/context/digest must be one complete tuple")
        if self.repair_context_id:
            if self.source != "model":
                raise ValueError("only a model-owned intent may link a repair context")
            required = {
                "trigger_assessment_id": self.trigger_assessment_id,
                "blocking_fact_refs": self.blocking_fact_refs,
                "repair_hypothesis": _text(self.repair_hypothesis),
                "next_action_kind": _text(self.next_action_kind),
                "expected_observation": self.expected_observation,
                "stop_condition": _text(self.stop_condition),
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError("repair-linked action intent is missing: " + ", ".join(missing))
        if len(_canonical_model_bytes(self)) > ACTION_INTENT_MAX_CANONICAL_BYTES:
            raise ValueError("action intent exceeds its canonical byte limit")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "ActionIntent":
        engine_owned = {
            "schema_version",
            "intent_id",
            "source",
            "action_fingerprint",
            "trigger_assessment_id",
            "repair_context_id",
            "repair_context_sha256",
            "predecessor_contract_id",
        }
        attempted = engine_owned.intersection(update or {})
        if attempted:
            raise ValueError(
                "engine-owned action intent fields cannot be changed by model_copy: "
                + ", ".join(sorted(attempted))
            )
        return cast(ActionIntent, super().model_copy(update=update, deep=deep))


IntentIdFactory = Callable[[ActionIntentSource, str, str], str]


def _default_intent_id(source: ActionIntentSource, fingerprint: str, tool_call_id: str) -> str:
    material = f"{source}\x00{fingerprint}\x00{tool_call_id}".encode("utf-8")
    return "intent-" + hashlib.sha256(material).hexdigest()[:12]


class EngineActionIntentFactory:
    """Bind one engine-selected provenance source to untrusted submissions."""

    def __init__(
        self,
        source: ActionIntentSource,
        *,
        id_factory: IntentIdFactory | None = None,
    ) -> None:
        if source not in ("model", "controller"):
            raise ValueError("action intent source must be model or controller")
        self.source: ActionIntentSource = source
        self._id_factory = id_factory or _default_intent_id

    @classmethod
    def for_model(cls, *, id_factory: IntentIdFactory | None = None) -> "EngineActionIntentFactory":
        return cls("model", id_factory=id_factory)

    @classmethod
    def for_controller(
        cls,
        *,
        id_factory: IntentIdFactory | None = None,
    ) -> "EngineActionIntentFactory":
        return cls("controller", id_factory=id_factory)

    def from_submission(
        self,
        submission: ActionIntentSubmission | Mapping[str, Any],
        *,
        intent_id: str | None = None,
        tool_call_id: str = "",
        trigger_assessment_id: str | None = None,
        repair_context_id: str | None = None,
        repair_context_sha256: str | None = None,
        predecessor_contract_id: str | None = None,
    ) -> ActionIntent:
        """Validate call data, then assign source/id/fingerprint in the engine."""

        # An existing Pydantic instance is not privileged: callers can still
        # mutate frozen objects through low-level Python APIs. Re-enter the
        # public schema before assigning engine provenance and identity.
        candidate = ActionIntentSubmission.model_validate(submission)
        fingerprint = action_fingerprint(
            domain_id=candidate.domain_id,
            tool=candidate.tool,
            params=candidate.params,
            tool_call_id=tool_call_id,
            prose=(candidate.repair_hypothesis, candidate.stop_condition),
            refs=candidate.blocking_fact_refs,
        )
        assigned_id = _text(intent_id) or self._id_factory(
            self.source,
            fingerprint,
            _text(tool_call_id),
        )
        repair_values = (
            _text(trigger_assessment_id),
            _text(repair_context_id),
            _text(repair_context_sha256),
        )
        if any(repair_values) and not all(repair_values):
            raise ValueError("repair trigger/context/digest must be one complete tuple")
        next_action_kind = _text(candidate.next_action_kind)
        if all(repair_values) and not next_action_kind:
            raise ValueError("repair-linked next_action_kind must be supplied by the model")
        if not next_action_kind:
            selected_action = _text(candidate.params.get("action"))
            next_action_kind = (
                semantic_action_kind(selected_action)
                if selected_action
                else semantic_action_kind(candidate.tool)
            )
        return ActionIntent(
            intent_id=assigned_id,
            source=self.source,
            domain_id=candidate.domain_id,
            tool=candidate.tool.lower(),
            canonical_params=candidate.params,
            action_fingerprint=fingerprint,
            trigger_assessment_id=_text(trigger_assessment_id) or None,
            repair_context_id=_text(repair_context_id) or None,
            repair_context_sha256=_text(repair_context_sha256) or None,
            predecessor_contract_id=_text(predecessor_contract_id) or None,
            blocking_fact_refs=candidate.blocking_fact_refs,
            repair_hypothesis=candidate.repair_hypothesis,
            next_action_kind=next_action_kind,
            expected_observation=candidate.expected_observation,
            stop_condition=candidate.stop_condition,
        )


def engine_source_factory(source: ActionIntentSource) -> EngineActionIntentFactory:
    """Small integration seam used by the engine at the native-call boundary."""

    return EngineActionIntentFactory(source)
