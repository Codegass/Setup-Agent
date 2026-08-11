"""Strict primitives for filesystem-first JVM build evidence.

This module deliberately starts with the public-boundary machinery shared by
all JVM build evidence records.  Domain schemas are added only after their
field contracts are frozen; keeping the primitives independent prevents a
temporary schema guess from becoming persistence or replay authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Callable, Literal, Mapping, Sequence, TypeVar, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

BUILD_EVIDENCE_SCHEMA_VERSION = 1
BUILD_EVIDENCE_GENESIS_SHA256 = "0" * 64
BUILD_EVIDENCE_MAX_CANONICAL_BYTES = 16 * 1024 * 1024
BUILD_EVIDENCE_MAX_RAW_BYTES = 20 * 1024 * 1024
BUILD_EVIDENCE_MAX_ENTRIES = 10_000
BUILD_EVIDENCE_MAX_TOTAL_VALUES = BUILD_EVIDENCE_MAX_ENTRIES * 128
BUILD_EVIDENCE_MAX_JSON_DEPTH = 64
BUILD_EVIDENCE_MAX_PATH_CHARS = 4_096
BUILD_EVIDENCE_MAX_TEXT_CHARS = 16_384
BUILD_EVIDENCE_MAX_IDENTIFIER_CHARS = 512

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*\Z")
_SOURCE_GLOB_SEGMENT_RE = re.compile(r"[A-Za-z0-9._+@*?!\[\]\-]+\Z")

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class StrictBuildEvidenceModel(BaseModel):
    """One validation boundary for constructor, copy, construct, and JSON.

    Pydantic's default ``model_copy(update=...)`` and ``model_construct`` APIs
    intentionally bypass validation.  Evidence records cannot expose either
    escape hatch because the same models cross producer, persistence, replay,
    and Physical Validator boundaries.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    @model_validator(mode="after")
    def _enforce_canonical_byte_bound(self) -> StrictBuildEvidenceModel:
        canonical_build_evidence_bytes(self)
        return self

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Any = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Any:
        if strict is not None and strict is not True:
            raise TypeError("build evidence validation cannot disable strict mode")
        if any(
            option is not None for option in (extra, from_attributes, context, by_alias, by_name)
        ):
            raise TypeError("build evidence validation accepts no boundary overrides")
        return super().model_validate(obj, strict=True, extra="forbid")

    @classmethod
    def model_construct(
        cls,
        _fields_set: set[str] | None = None,
        **values: Any,
    ) -> Any:
        del _fields_set
        return cls.model_validate(values)

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if args or kwargs:
            raise TypeError("build evidence JSON validation accepts no parser overrides")
        return validate_build_evidence_json(cls, json_data)

    @classmethod
    def model_validate_strings(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Any = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Any:
        del obj, strict, extra, context, by_alias, by_name
        raise TypeError("build evidence does not permit string-coercing validation")

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

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Any:
        if include is not None or exclude is not None:
            raise TypeError("build evidence copy cannot omit required fields")
        return self.model_copy(update=update, deep=deep)


def validate_build_evidence_json(
    model_type: type[_ModelT],
    raw: str | bytes | bytearray,
) -> _ModelT:
    """Validate one bounded JSON value without allowing duplicate-key loss."""

    wire = _bounded_json_wire(raw)
    # TypeAdapter reaches Pydantic's compiled JSON validator directly.  This is
    # intentional: calling ``model_type.model_validate_json`` would recurse
    # through StrictBuildEvidenceModel, while validating the already-decoded
    # Python value would lose JSON's strict tuple/array semantics.
    value = cast(_ModelT, TypeAdapter(model_type).validate_json(wire))
    canonical_build_evidence_bytes(value)
    return value


def canonical_build_evidence_bytes(value: Any) -> bytes:
    """Return deterministic, bounded canonical JSON bytes for evidence data."""

    payload = _json_ready(value)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"build evidence is not canonical JSON: {exc}") from exc
    if len(encoded) > BUILD_EVIDENCE_MAX_CANONICAL_BYTES:
        raise ValueError("build evidence canonical bytes exceed the size limit")
    return encoded


def canonical_build_evidence_sha256(value: Any) -> str:
    """Hash the canonical JSON representation of one evidence value."""

    return hashlib.sha256(canonical_build_evidence_bytes(value)).hexdigest()


def canonical_content_id(kind: str, projection: Any) -> str:
    """Create a versioned, kind-separated ID from an identity projection."""

    normalized_kind = validate_identifier(kind, field="content ID kind")
    digest = canonical_build_evidence_sha256(
        {
            "identity_kind": normalized_kind,
            "identity_schema_version": BUILD_EVIDENCE_SCHEMA_VERSION,
            "projection": projection,
        }
    )
    return f"{normalized_kind}-{digest}"


def generator_unit_identity_hash(projection: Any) -> str:
    """Hash the complete generator-unit identity projection."""

    return _typed_identity_hash("generator-unit", projection)


def compile_unit_identity_hash(projection: Any) -> str:
    """Hash the complete compile-unit identity projection."""

    return _typed_identity_hash("compile-unit", projection)


def package_unit_identity_hash(projection: Any) -> str:
    """Hash the complete package-unit identity projection."""

    return _typed_identity_hash("package-unit", projection)


def physical_output_aggregate_sha256(roots: Sequence[Any], entries: Sequence[Any]) -> str:
    """Hash an ordered physical-output witness projection."""

    return _typed_identity_hash(
        "physical-output",
        {"roots": tuple(roots), "entries": tuple(entries)},
    )


def validate_sha256(value: Any, *, field: str = "sha256") -> str:
    """Require one lowercase, full-width SHA-256 digest."""

    text = validate_bounded_text(
        value,
        field=field,
        maximum=64,
        allow_empty=False,
    )
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must be a lowercase 64-character sha256 digest")
    return text


def validate_identifier(value: Any, *, field: str = "identifier") -> str:
    """Require a bounded canonical identifier without silent normalization."""

    text = validate_bounded_text(
        value,
        field=field,
        maximum=BUILD_EVIDENCE_MAX_IDENTIFIER_CHARS,
        allow_empty=False,
    )
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ValueError(f"{field} is not a canonical identifier")
    return text


def validate_bounded_text(
    value: Any,
    *,
    field: str,
    maximum: int = BUILD_EVIDENCE_MAX_TEXT_CHARS,
    allow_empty: bool = False,
) -> str:
    """Validate text exactly as supplied; writers must normalize beforehand."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds its length limit")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} contains a forbidden control character")
    return value


def validate_relative_path(value: Any, *, field: str = "path") -> str:
    """Require one contained canonical POSIX path relative to project/root."""

    path = _validate_path_text(value, field=field)
    if path.startswith("/"):
        raise ValueError(f"{field} must be relative")
    if path in {".", ".."} or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError(f"{field} is not a contained canonical relative path")
    if posixpath.normpath(path) != path:
        raise ValueError(f"{field} is not normalized")
    return path


def validate_absolute_path(value: Any, *, field: str = "path") -> str:
    """Require one normalized absolute POSIX path without parent traversal."""

    path = _validate_path_text(value, field=field)
    if not path.startswith("/"):
        raise ValueError(f"{field} must be absolute")
    if path != "/" and (path.endswith("/") or "//" in path):
        raise ValueError(f"{field} is not normalized")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError(f"{field} contains traversal")
    if posixpath.normpath(path) != path:
        raise ValueError(f"{field} is not normalized")
    return path


def validate_nonnegative_int(value: object, *, field: str) -> int:
    """Reject boolean coercion and negative counts."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def validate_bounded_sequence(
    value: Any,
    *,
    field: str,
    maximum: int = BUILD_EVIDENCE_MAX_ENTRIES,
) -> tuple[Any, ...]:
    """Require an already-materialized bounded non-text sequence."""

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds its entry limit")
    return tuple(value)


def validate_unique_canonical_sequence(
    value: Any,
    *,
    field: str,
    key: Callable[[Any], str],
    maximum: int = BUILD_EVIDENCE_MAX_ENTRIES,
) -> tuple[Any, ...]:
    """Require bounded, duplicate-free items in their canonical key order."""

    items = validate_bounded_sequence(value, field=field, maximum=maximum)
    keys = tuple(key(item) for item in items)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field} contains a duplicate")
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return items


def _typed_identity_hash(kind: str, projection: Any) -> str:
    return canonical_build_evidence_sha256(
        {
            "identity_kind": kind,
            "identity_schema_version": BUILD_EVIDENCE_SCHEMA_VERSION,
            "projection": projection,
        }
    )


def _bounded_json_wire(raw: str | bytes | bytearray) -> bytes:
    if isinstance(raw, str):
        wire = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        wire = raw
    elif isinstance(raw, bytearray):
        wire = bytes(raw)
    else:
        raise TypeError("build evidence JSON must be str, bytes, or bytearray")
    if len(wire) > BUILD_EVIDENCE_MAX_RAW_BYTES:
        raise ValueError("build evidence JSON exceeds its raw byte limit")
    try:
        decoded = wire.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("build evidence JSON must be UTF-8") from exc
    try:
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid build evidence JSON: {exc.msg}") from exc
    _validate_json_shape(parsed)
    return wire


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = child
    return value


def _validate_json_shape(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> None:
    if budget is None:
        budget = [0]
    if depth > BUILD_EVIDENCE_MAX_JSON_DEPTH:
        raise ValueError("build evidence JSON exceeds its nesting-depth limit")
    budget[0] += 1
    if budget[0] > BUILD_EVIDENCE_MAX_TOTAL_VALUES:
        raise ValueError("build evidence JSON exceeds its total-value limit")
    if isinstance(value, Mapping):
        if len(value) > BUILD_EVIDENCE_MAX_ENTRIES:
            raise ValueError("build evidence JSON object exceeds its entry limit")
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("build evidence JSON keys must be text")
            if len(key) > BUILD_EVIDENCE_MAX_IDENTIFIER_CHARS:
                raise ValueError("build evidence JSON key exceeds its length limit")
            _validate_json_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        if len(value) > BUILD_EVIDENCE_MAX_ENTRIES:
            raise ValueError("build evidence JSON array exceeds its entry limit")
        for child in value:
            _validate_json_shape(child, depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        if len(value) > BUILD_EVIDENCE_MAX_CANONICAL_BYTES:
            raise ValueError("build evidence JSON string exceeds its length limit")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("build evidence JSON numbers must be finite")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"build evidence JSON contains unsupported {type(value).__name__}")


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("build evidence canonical JSON keys must be text")
            result[key] = _json_ready(child)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("build evidence canonical JSON numbers must be finite")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"build evidence contains unsupported {type(value).__name__}")


def _validate_path_text(value: Any, *, field: str) -> str:
    path = validate_bounded_text(
        value,
        field=field,
        maximum=BUILD_EVIDENCE_MAX_PATH_CHARS,
        allow_empty=False,
    )
    if "\\" in path:
        raise ValueError(f"{field} must use POSIX separators")
    return path


def _as_identifier(value: str) -> str:
    return validate_identifier(value)


def _as_sha256(value: str) -> str:
    return validate_sha256(value)


def _as_text(value: str) -> str:
    return validate_bounded_text(value, field="text")


def _as_relative_path(value: str) -> str:
    return validate_relative_path(value)


def _as_absolute_path(value: str) -> str:
    return validate_absolute_path(value)


CanonicalIdentifier = Annotated[str, AfterValidator(_as_identifier)]
Sha256 = Annotated[str, AfterValidator(_as_sha256)]
BoundedText = Annotated[str, AfterValidator(_as_text)]
RelativePath = Annotated[str, AfterValidator(_as_relative_path)]
AbsolutePath = Annotated[str, AfterValidator(_as_absolute_path)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]

BuildSystem = Literal["maven", "gradle"]
PathKind = Literal["file", "tree"]
VerificationMode = Literal["exclusive_tree", "shared_owned_entries"]
JvmLanguage = Literal["java", "kotlin", "scala", "groovy", "aspectj", "other"]
UnitRole = Literal["required", "optional", "out_of_scope"]
PhysicalMemberRole = Literal[
    "source",
    "config",
    "generator_input",
    "generator_tool",
    "generator_dependency",
    "compiler",
    "toolchain",
    "classpath",
    "resource",
    "packaging_input",
    "packaging_tool",
    "external_dependency",
    "upstream_output",
    "other",
]


def _bounded_text_tuple(
    values: Sequence[str],
    *,
    field: str,
    canonical_order: bool = False,
) -> tuple[str, ...]:
    items = validate_bounded_sequence(values, field=field)
    normalized = tuple(
        validate_bounded_text(item, field=field, maximum=BUILD_EVIDENCE_MAX_TEXT_CHARS)
        for item in items
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains a duplicate")
    if canonical_order and normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return normalized


def _identifier_tuple(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    items = validate_bounded_sequence(values, field=field)
    normalized = tuple(validate_identifier(item, field=field) for item in items)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains a duplicate")
    if normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return normalized


def _relative_path_tuple(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    items = validate_bounded_sequence(values, field=field)
    normalized = tuple(validate_relative_path(item, field=field) for item in items)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains a duplicate path")
    if normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return normalized


def _model_tuple(
    values: Sequence[Any],
    *,
    field: str,
    key: Callable[[Any], str],
) -> tuple[Any, ...]:
    return validate_unique_canonical_sequence(values, field=field, key=key)


def _ordered_model_tuple(
    values: Sequence[Any],
    *,
    field: str,
    key: Callable[[Any], str],
) -> tuple[Any, ...]:
    """Validate an adapter-ordered member tuple without re-sorting it.

    Classpath, tool, resource, and packaging input order can affect JVM task
    identity.  These collections therefore preserve the mechanically resolved
    execution order while still rejecting duplicate semantic keys.
    """

    items = validate_bounded_sequence(values, field=field)
    keys = tuple(key(item) for item in items)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field} contains a duplicate")
    return items


def _require_primary_id(*, actual: str, kind: str, projection: Any, field: str) -> None:
    expected = canonical_content_id(kind, projection)
    if actual != expected:
        raise ValueError(f"{field} identity does not match its primary-ID projection")


class ActualInvocationScopeV1(StrictBuildEvidenceModel):
    build_system: BuildSystem
    selected_modules_or_tasks: tuple[BoundedText, ...]
    lifecycle_or_tasks: tuple[BoundedText, ...]
    profiles_or_variants: tuple[BoundedText, ...]
    resume_from: BoundedText | None
    also_make: bool
    selector_fingerprint: Sha256

    @field_validator(
        "selected_modules_or_tasks",
        "lifecycle_or_tasks",
        "profiles_or_variants",
    )
    @classmethod
    def _validate_scope_lists(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field=info.field_name)


class BuildPathBasis(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    member_role: PhysicalMemberRole
    path: AbsolutePath
    path_kind: PathKind
    resolution_provenance: BoundedText


class ResolvedMemberSpec(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    member_role: PhysicalMemberRole
    path_kind: PathKind
    path: AbsolutePath | None
    coordinate: BoundedText | None
    upstream_unit_id: CanonicalIdentifier | None
    upstream_root_id: CanonicalIdentifier | None
    resolution_provenance: BoundedText

    @model_validator(mode="after")
    def _validate_location_form(self) -> ResolvedMemberSpec:
        has_path = self.path is not None
        has_coordinate = self.coordinate is not None
        has_upstream = self.upstream_unit_id is not None or self.upstream_root_id is not None
        if has_upstream and (self.upstream_unit_id is None or self.upstream_root_id is None):
            raise ValueError("upstream member form requires both unit and root IDs")
        if has_upstream:
            if has_path or has_coordinate:
                raise ValueError("upstream member form cannot carry path or coordinate")
            if self.member_role != "upstream_output":
                raise ValueError("upstream member form requires upstream_output member role")
        elif has_coordinate:
            if not has_path:
                raise ValueError("coordinate member form requires an adapter-resolved path")
            if self.member_role == "upstream_output":
                raise ValueError("upstream_output role requires the upstream member form")
        elif not has_path:
            raise ValueError("resolved member requires a path, coordinate, or upstream form")
        elif self.member_role == "upstream_output":
            raise ValueError("upstream_output role requires the upstream member form")
        return self


class OutputRootSpec(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    root_id: CanonicalIdentifier
    path: RelativePath
    verification_mode: VerificationMode
    output_requirement: Literal["nonempty", "empty_permitted"]


class RequestedArtifactSpec(StrictBuildEvidenceModel):
    artifact_id: CanonicalIdentifier
    root_id: CanonicalIdentifier
    path: RelativePath
    kind: Literal["jar", "war", "ear", "zip", "resource", "other"]


class SourceDigestEntry(StrictBuildEvidenceModel):
    source_id: CanonicalIdentifier
    path: RelativePath
    sha256: Sha256


class ResolvedDigestEntry(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    member_role: PhysicalMemberRole
    path_kind: PathKind
    path: AbsolutePath
    byte_count: NonnegativeInt
    sha256: Sha256


class CompileOutputDigestEntry(StrictBuildEvidenceModel):
    compile_unit_id: CanonicalIdentifier
    root_id: CanonicalIdentifier
    sha256: Sha256


class CompilerEnvironmentSpec(StrictBuildEvidenceModel):
    executable_path_basis: BuildPathBasis
    compiler_version: BoundedText
    compiler_args: tuple[BoundedText, ...]
    classpath_members: tuple[ResolvedMemberSpec, ...]
    toolchain_path_basis: BuildPathBasis
    resolution_provenance: BoundedText

    @field_validator("compiler_args")
    @classmethod
    def _validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="compiler_args")

    @field_validator("classpath_members")
    @classmethod
    def _validate_classpath(
        cls, value: tuple[ResolvedMemberSpec, ...]
    ) -> tuple[ResolvedMemberSpec, ...]:
        return _ordered_model_tuple(
            value,
            field="classpath_members",
            key=lambda item: item.member_id,
        )

    @model_validator(mode="after")
    def _validate_path_kinds(self) -> CompilerEnvironmentSpec:
        if self.executable_path_basis.path_kind != "file":
            raise ValueError("compiler executable path basis must be a file")
        if self.executable_path_basis.member_role != "compiler":
            raise ValueError("compiler executable path basis requires compiler member role")
        if self.toolchain_path_basis.path_kind != "tree":
            raise ValueError("compiler toolchain path basis must be a tree")
        if self.toolchain_path_basis.member_role != "toolchain":
            raise ValueError("compiler toolchain path basis requires toolchain member role")
        if any(
            member.member_role not in {"classpath", "upstream_output"}
            for member in self.classpath_members
        ):
            raise ValueError("compiler classpath member has an incompatible member role")
        return self


class GeneratorEnvironmentSpec(StrictBuildEvidenceModel):
    executable_path_basis: BuildPathBasis
    tool_or_plugin_members: tuple[ResolvedMemberSpec, ...]
    dependency_members: tuple[ResolvedMemberSpec, ...]
    generator_args: tuple[BoundedText, ...]
    toolchain_path_basis: BuildPathBasis

    @field_validator("tool_or_plugin_members", "dependency_members")
    @classmethod
    def _validate_members(
        cls, value: tuple[ResolvedMemberSpec, ...], info: Any
    ) -> tuple[ResolvedMemberSpec, ...]:
        return _ordered_model_tuple(
            value,
            field=info.field_name,
            key=lambda item: item.member_id,
        )

    @field_validator("generator_args")
    @classmethod
    def _validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="generator_args")

    @model_validator(mode="after")
    def _validate_path_kinds(self) -> GeneratorEnvironmentSpec:
        if self.executable_path_basis.path_kind != "file":
            raise ValueError("generator executable path basis must be a file")
        if self.executable_path_basis.member_role != "generator_tool":
            raise ValueError("generator executable path basis requires generator_tool member role")
        if self.toolchain_path_basis.path_kind != "tree":
            raise ValueError("generator toolchain path basis must be a tree")
        if self.toolchain_path_basis.member_role != "toolchain":
            raise ValueError("generator toolchain path basis requires toolchain member role")
        if any(member.member_role != "generator_tool" for member in self.tool_or_plugin_members):
            raise ValueError("generator tool member has an incompatible member role")
        if any(member.member_role != "generator_dependency" for member in self.dependency_members):
            raise ValueError("generator dependency has an incompatible member role")
        return self


class PackagingEnvironmentSpec(StrictBuildEvidenceModel):
    executable_path_basis: BuildPathBasis
    plugin_or_tool_members: tuple[ResolvedMemberSpec, ...]
    external_dependency_members: tuple[ResolvedMemberSpec, ...]
    packaging_args: tuple[BoundedText, ...]
    toolchain_path_basis: BuildPathBasis

    @field_validator("plugin_or_tool_members", "external_dependency_members")
    @classmethod
    def _validate_members(
        cls, value: tuple[ResolvedMemberSpec, ...], info: Any
    ) -> tuple[ResolvedMemberSpec, ...]:
        return _ordered_model_tuple(
            value,
            field=info.field_name,
            key=lambda item: item.member_id,
        )

    @field_validator("packaging_args")
    @classmethod
    def _validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="packaging_args")

    @model_validator(mode="after")
    def _validate_path_kinds(self) -> PackagingEnvironmentSpec:
        if self.executable_path_basis.path_kind != "file":
            raise ValueError("packaging executable path basis must be a file")
        if self.executable_path_basis.member_role != "packaging_tool":
            raise ValueError("packaging executable path basis requires packaging_tool member role")
        if self.toolchain_path_basis.path_kind != "tree":
            raise ValueError("packaging toolchain path basis must be a tree")
        if self.toolchain_path_basis.member_role != "toolchain":
            raise ValueError("packaging toolchain path basis requires toolchain member role")
        if any(member.member_role != "packaging_tool" for member in self.plugin_or_tool_members):
            raise ValueError("packaging tool member has an incompatible member role")
        if any(
            member.member_role != "external_dependency"
            for member in self.external_dependency_members
        ):
            raise ValueError("packaging dependency has an incompatible member role")
        return self


class RequiredSourceEdge(StrictBuildEvidenceModel):
    source_id: CanonicalIdentifier
    compile_unit_id: CanonicalIdentifier


class DelegationEdge(StrictBuildEvidenceModel):
    parent_source_id: CanonicalIdentifier
    child_scope_id: CanonicalIdentifier
    expected_child_path: RelativePath
    expected_child_sha256: Sha256


class ClassificationBasisV1(StrictBuildEvidenceModel):
    basis_kind: Literal[
        "model_rule",
        "explicit_scope",
        "generated_contract",
        "child_scope",
        "unsupported_adapter",
    ]
    model_id: CanonicalIdentifier
    basis_ref: CanonicalIdentifier


class SourceClassification(StrictBuildEvidenceModel):
    source_id: CanonicalIdentifier
    language: JvmLanguage
    source_kind: Literal["main", "test", "generated", "custom"]
    classification: Literal[
        "required",
        "explicitly_excluded",
        "inactive_profile",
        "out_of_lifecycle_scope",
        "delegated_child_scope",
        "vendor",
        "compiler_intermediate",
        "unsupported_active",
    ]
    classification_basis: ClassificationBasisV1
    expected_compile_unit_ids: tuple[CanonicalIdentifier, ...]
    child_scope_id: CanonicalIdentifier | None

    @field_validator("expected_compile_unit_ids")
    @classmethod
    def _validate_expected_units(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="expected_compile_unit_ids")

    @model_validator(mode="after")
    def _validate_classification_shape(self) -> SourceClassification:
        if (
            self.classification in {"required", "unsupported_active"}
            and not self.expected_compile_unit_ids
        ):
            raise ValueError(
                "required or unsupported-active classification needs an expected compile unit"
            )
        if (
            self.classification == "unsupported_active"
            and self.classification_basis.basis_kind != "unsupported_adapter"
        ):
            raise ValueError("unsupported-active classification requires an unsupported basis")
        if self.classification == "delegated_child_scope":
            if self.child_scope_id is None or self.expected_compile_unit_ids:
                raise ValueError("delegated classification requires exactly one child scope")
            if self.classification_basis.basis_kind != "child_scope":
                raise ValueError("delegated classification requires a child-scope basis")
        elif self.child_scope_id is not None:
            raise ValueError("child_scope_id is valid only for delegated classification")
        return self


class JvmSourceCandidate(StrictBuildEvidenceModel):
    source_id: CanonicalIdentifier
    path: RelativePath
    sha256: Sha256
    language: JvmLanguage
    origin: Literal["repository", "generated"]
    role_hint: Literal["production", "test", "custom", "unknown"]
    discovered_root: RelativePath
    symlink_status: Literal["contained", "symlink_contained", "escaped", "unreadable"]

    @model_validator(mode="after")
    def _validate_primary_id(self) -> JvmSourceCandidate:
        _require_primary_id(
            actual=self.source_id,
            kind="jvm-source",
            projection={"path": self.path, "sha256": self.sha256},
            field="source_id",
        )
        return self


class ContainerEvidenceEpochV1(StrictBuildEvidenceModel):
    evidence_epoch_id: CanonicalIdentifier
    immutable_container_id: CanonicalIdentifier
    project_root: AbsolutePath
    created_at: BoundedText
    initial_source_state_fingerprint: Sha256
    baseline_output_root_digests: tuple[Sha256, ...]

    @field_validator("baseline_output_root_digests")
    @classmethod
    def _validate_baseline_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="baseline_output_root_digests")
        if len(set(items)) != len(items):
            raise ValueError("baseline_output_root_digests contains a duplicate")
        if items != tuple(sorted(items)):
            raise ValueError("baseline_output_root_digests must be in canonical sorted order")
        return items

    @model_validator(mode="after")
    def _validate_primary_id(self) -> ContainerEvidenceEpochV1:
        _require_primary_id(
            actual=self.evidence_epoch_id,
            kind="container-evidence-epoch-v1",
            projection={
                "immutable_container_id": self.immutable_container_id,
                "project_root": self.project_root,
            },
            field="evidence_epoch_id",
        )
        return self


class BuildGoalScopeV1(StrictBuildEvidenceModel):
    scope_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    authority_ref: CanonicalIdentifier
    scope_revision: PositiveInt
    predecessor_scope_hash: Sha256 | None
    validation_target: Literal["project", "domain", "explicit_modules"]
    requested_action: BoundedText
    requested_lifecycle: BoundedText
    requested_modules_or_domains: tuple[CanonicalIdentifier, ...]
    requested_profiles_or_variants: tuple[BoundedText, ...]
    source_set_roles: tuple[Literal["production", "test", "generated", "custom"], ...]
    scope_fingerprint: Sha256

    @field_validator("requested_modules_or_domains")
    @classmethod
    def _validate_requested_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="requested_modules_or_domains")

    @field_validator("requested_profiles_or_variants")
    @classmethod
    def _validate_profiles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(
            value,
            field="requested_profiles_or_variants",
            canonical_order=True,
        )

    @field_validator("source_set_roles")
    @classmethod
    def _validate_source_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="source_set_roles")
        if len(set(items)) != len(items):
            raise ValueError("source_set_roles contains a duplicate")
        if items != tuple(sorted(items)):
            raise ValueError("source_set_roles must be in canonical sorted order")
        return items

    @model_validator(mode="after")
    def _validate_scope(self) -> BuildGoalScopeV1:
        _require_primary_id(
            actual=self.scope_id,
            kind="build-goal-scope-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "authority_ref": self.authority_ref,
            },
            field="scope_id",
        )
        if self.scope_revision == 1 and self.predecessor_scope_hash is not None:
            raise ValueError("scope revision 1 cannot name a predecessor scope hash")
        if self.scope_revision > 1 and self.predecessor_scope_hash is None:
            raise ValueError("later scope revision requires predecessor_scope_hash")
        if self.validation_target == "explicit_modules" and not self.requested_modules_or_domains:
            raise ValueError("explicit_modules validation target requires requested modules")
        return self


class JvmSourceCensusV1(StrictBuildEvidenceModel):
    census_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    project_root: AbsolutePath
    census_revision: PositiveInt
    source_state_fingerprint: Sha256
    sources: tuple[JvmSourceCandidate, ...]
    unreadable_roots: tuple[BoundedText, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("sources")
    @classmethod
    def _validate_sources(
        cls, value: tuple[JvmSourceCandidate, ...]
    ) -> tuple[JvmSourceCandidate, ...]:
        sources = _model_tuple(value, field="sources", key=lambda item: item.source_id)
        paths = tuple(source.path for source in sources)
        if len(set(paths)) != len(paths):
            raise ValueError("sources contains a duplicate path")
        return sources

    @field_validator("unreadable_roots", "conflicts")
    @classmethod
    def _validate_text_sets(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field=info.field_name, canonical_order=True)

    @model_validator(mode="after")
    def _validate_primary_id(self) -> JvmSourceCensusV1:
        _require_primary_id(
            actual=self.census_id,
            kind="jvm-source-census-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "project_root": self.project_root,
            },
            field="census_id",
        )
        return self


class EvaluatedModuleV1(StrictBuildEvidenceModel):
    module_id: CanonicalIdentifier
    domain_id: CanonicalIdentifier
    build_system: BuildSystem
    module_coordinate: BoundedText
    project_path: RelativePath
    role: UnitRole
    model_provenance: BoundedText


class CompileDependencyEdge(StrictBuildEvidenceModel):
    upstream_compile_unit_id: CanonicalIdentifier
    upstream_output_root_id: CanonicalIdentifier
    downstream_compile_unit_id: CanonicalIdentifier
    dependency_basis: BoundedText


class SourcePatternRuleV1(StrictBuildEvidenceModel):
    rule_id: CanonicalIdentifier
    compile_unit_id: CanonicalIdentifier
    dialect: Literal["posix_glob_v1"] = "posix_glob_v1"
    pattern: BoundedText
    model_provenance: BoundedText

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value:
            raise ValueError("source glob pattern must be a relative POSIX pattern")
        segments = value.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("source glob pattern is not safely contained")
        for segment in segments:
            if _SOURCE_GLOB_SEGMENT_RE.fullmatch(segment) is None:
                raise ValueError("source glob pattern uses an unsupported grammar token")
            if "**" in segment and segment != "**":
                raise ValueError("source glob recursive wildcard must occupy a whole segment")
            bracket_open = False
            bracket_content = 0
            for character in segment:
                if character == "[":
                    if bracket_open:
                        raise ValueError("source glob pattern contains nested brackets")
                    bracket_open = True
                    bracket_content = 0
                elif character == "]":
                    if not bracket_open or bracket_content == 0:
                        raise ValueError(
                            "source glob pattern contains an unsafe bracket expression"
                        )
                    bracket_open = False
                elif bracket_open:
                    bracket_content += 1
            if bracket_open:
                raise ValueError("source glob pattern contains an unclosed bracket expression")
        return value

    @model_validator(mode="after")
    def _validate_primary_id(self) -> SourcePatternRuleV1:
        _require_primary_id(
            actual=self.rule_id,
            kind="source-pattern-rule-v1",
            projection={
                "compile_unit_id": self.compile_unit_id,
                "dialect": self.dialect,
                "pattern": self.pattern,
                "model_provenance": self.model_provenance,
            },
            field="rule_id",
        )
        return self


class CompileUnitExpectation(StrictBuildEvidenceModel):
    compile_unit_id: CanonicalIdentifier
    domain_id: CanonicalIdentifier
    build_system: BuildSystem
    module_coordinate: BoundedText
    source_set: Literal["main", "test", "generated", "custom"]
    language: JvmLanguage
    adapter_status: Literal["supported", "unsupported_active"]
    task_or_execution: BoundedText
    role: UnitRole
    scope_disposition: Literal[
        "active",
        "inactive_profile",
        "out_of_lifecycle_scope",
        "vendor",
        "explicit_scope_exclusion",
    ]
    source_roots: tuple[RelativePath, ...]
    include_rules: tuple[SourcePatternRuleV1, ...]
    exclude_rules: tuple[SourcePatternRuleV1, ...]
    compiler_environment_spec: CompilerEnvironmentSpec
    output_roots: tuple[OutputRootSpec, ...]
    output_requirement: Literal["nonempty", "empty_permitted", "none"]
    output_requirement_basis: BoundedText
    requested_lifecycle_basis: BoundedText
    model_provenance: BoundedText

    @field_validator("source_roots")
    @classmethod
    def _validate_source_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _relative_path_tuple(value, field="source_roots")

    @field_validator("include_rules", "exclude_rules")
    @classmethod
    def _validate_rules(
        cls, value: tuple[SourcePatternRuleV1, ...], info: Any
    ) -> tuple[SourcePatternRuleV1, ...]:
        return _model_tuple(value, field=info.field_name, key=lambda item: item.rule_id)

    @field_validator("output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _model_tuple(value, field="output_roots", key=lambda item: item.root_id)

    @model_validator(mode="after")
    def _validate_output_contract(self) -> CompileUnitExpectation:
        if self.role == "required" and self.scope_disposition != "active":
            raise ValueError("required compile unit must have active scope disposition")
        if self.role != "required" and self.scope_disposition == "active":
            raise ValueError("non-required compile unit cannot have active scope disposition")
        rules = (*self.include_rules, *self.exclude_rules)
        if any(rule.compile_unit_id != self.compile_unit_id for rule in rules):
            raise ValueError("source pattern rule owner does not match compile unit")
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("include and exclude rules cannot share one rule ID")
        if self.output_requirement == "none" and self.output_roots:
            raise ValueError("output requirement none cannot declare output roots")
        if self.output_requirement != "none" and not self.output_roots:
            raise ValueError("output requirement needs at least one output root")
        if self.output_roots and any(
            root.output_requirement != self.output_requirement for root in self.output_roots
        ):
            raise ValueError("output root requirement differs from compile unit requirement")
        return self


class GeneratorUnitExpectation(StrictBuildEvidenceModel):
    generator_unit_id: CanonicalIdentifier
    module_coordinate: BoundedText
    task_or_execution: BoundedText
    role: UnitRole
    input_roots: tuple[ResolvedMemberSpec, ...]
    generator_environment_spec: GeneratorEnvironmentSpec
    generated_root_ids: tuple[CanonicalIdentifier, ...]
    model_provenance: BoundedText

    @field_validator("input_roots")
    @classmethod
    def _validate_input_roots(
        cls, value: tuple[ResolvedMemberSpec, ...]
    ) -> tuple[ResolvedMemberSpec, ...]:
        roots = _ordered_model_tuple(value, field="input_roots", key=lambda item: item.member_id)
        if any(root.member_role != "generator_input" for root in roots):
            raise ValueError("generator input root requires generator_input member role")
        return roots

    @field_validator("generated_root_ids")
    @classmethod
    def _validate_generated_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="generated_root_ids")

    @model_validator(mode="after")
    def _validate_required_output_contract(self) -> GeneratorUnitExpectation:
        if self.role == "required" and not self.generated_root_ids:
            raise ValueError("required generator requires at least one generated output root")
        return self


class PackageUnitExpectation(StrictBuildEvidenceModel):
    package_unit_id: CanonicalIdentifier
    module_coordinate: BoundedText
    task_or_execution: BoundedText
    role: UnitRole
    input_compile_unit_ids: tuple[CanonicalIdentifier, ...]
    resource_roots: tuple[ResolvedMemberSpec, ...]
    manifest_or_packaging_config: tuple[ResolvedMemberSpec, ...]
    packaging_environment_spec: PackagingEnvironmentSpec
    output_roots: tuple[OutputRootSpec, ...]
    requested_artifacts: tuple[RequestedArtifactSpec, ...]
    model_provenance: BoundedText

    @field_validator("input_compile_unit_ids")
    @classmethod
    def _validate_compile_inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="input_compile_unit_ids")
        normalized = tuple(
            validate_identifier(item, field="input_compile_unit_ids") for item in items
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("input_compile_unit_ids contains a duplicate")
        return normalized

    @field_validator("resource_roots")
    @classmethod
    def _validate_resource_roots(
        cls, value: tuple[ResolvedMemberSpec, ...]
    ) -> tuple[ResolvedMemberSpec, ...]:
        roots = _ordered_model_tuple(
            value,
            field="resource_roots",
            key=lambda item: item.member_id,
        )
        if any(root.member_role != "resource" for root in roots):
            raise ValueError("package resource root requires resource member role")
        return roots

    @field_validator("manifest_or_packaging_config")
    @classmethod
    def _validate_config(
        cls, value: tuple[ResolvedMemberSpec, ...]
    ) -> tuple[ResolvedMemberSpec, ...]:
        members = _ordered_model_tuple(
            value,
            field="manifest_or_packaging_config",
            key=lambda item: item.member_id,
        )
        if any(member.member_role != "packaging_input" for member in members):
            raise ValueError("packaging config requires packaging_input member role")
        return members

    @field_validator("output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _output_root_spec_tuple(value, field="output_roots")

    @field_validator("requested_artifacts")
    @classmethod
    def _validate_artifacts(
        cls, value: tuple[RequestedArtifactSpec, ...]
    ) -> tuple[RequestedArtifactSpec, ...]:
        return _model_tuple(
            value,
            field="requested_artifacts",
            key=lambda item: item.artifact_id,
        )

    @model_validator(mode="after")
    def _validate_output_contract(self) -> PackageUnitExpectation:
        if self.role == "required" and (not self.output_roots or not self.requested_artifacts):
            raise ValueError("required package requires at least one output root and artifact")
        roots = {root.root_id: root for root in self.output_roots}
        for artifact in self.requested_artifacts:
            root = roots.get(artifact.root_id)
            if root is None:
                raise ValueError(f"artifact references unknown output root {artifact.root_id!r}")
            root_prefix = root.path.rstrip("/") + "/"
            if artifact.path != root.path and not artifact.path.startswith(root_prefix):
                raise ValueError("artifact path is not contained by its output root")
        return self


class GeneratedRootContract(StrictBuildEvidenceModel):
    generated_root_id: CanonicalIdentifier
    output_root: OutputRootSpec
    producer_unit_kind: Literal["compile", "generator"]
    producer_unit_id: CanonicalIdentifier
    consumer_unit_ids: tuple[CanonicalIdentifier, ...]
    mode: Literal["same_round_intermediate", "later_compile_input"]
    active_profile_or_variant: BoundedText
    model_provenance: BoundedText

    @field_validator("consumer_unit_ids")
    @classmethod
    def _validate_consumers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        consumers = _identifier_tuple(value, field="consumer_unit_ids")
        if not consumers:
            raise ValueError("generated root requires at least one consumer")
        return consumers

    @model_validator(mode="after")
    def _validate_output_root_identity(self) -> GeneratedRootContract:
        if self.generated_root_id != self.output_root.root_id:
            raise ValueError("generated_root_id must match output root root_id")
        return self


class ChildScopeExpectation(StrictBuildEvidenceModel):
    child_scope_id: CanonicalIdentifier
    parent_scope_id: CanonicalIdentifier
    child_project_root: AbsolutePath
    build_system: BuildSystem
    ownership_basis: BoundedText


class EvaluatedBuildModelV1(StrictBuildEvidenceModel):
    model_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    scope_id: CanonicalIdentifier
    goal_scope_revision: PositiveInt
    goal_scope_fingerprint: Sha256
    project_root: AbsolutePath
    model_revision: PositiveInt
    build_config_fingerprint: Sha256
    active_profiles_or_variants: tuple[BoundedText, ...]
    build_config_members: tuple[ResolvedMemberSpec, ...]
    modules: tuple[EvaluatedModuleV1, ...]
    generator_units: tuple[GeneratorUnitExpectation, ...]
    compile_units: tuple[CompileUnitExpectation, ...]
    package_units: tuple[PackageUnitExpectation, ...]
    compile_dependency_edges: tuple[CompileDependencyEdge, ...]
    generated_root_contracts: tuple[GeneratedRootContract, ...]
    child_build_scopes: tuple[ChildScopeExpectation, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("active_profiles_or_variants", "conflicts")
    @classmethod
    def _validate_text_sets(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field=info.field_name, canonical_order=True)

    @field_validator("build_config_members")
    @classmethod
    def _validate_build_config_members(
        cls, value: tuple[ResolvedMemberSpec, ...]
    ) -> tuple[ResolvedMemberSpec, ...]:
        members = _ordered_model_tuple(
            value,
            field="build_config_members",
            key=lambda item: item.member_id,
        )
        if any(member.member_role != "config" for member in members):
            raise ValueError("build config member requires config member role")
        return members

    @field_validator("modules")
    @classmethod
    def _validate_modules(
        cls, value: tuple[EvaluatedModuleV1, ...]
    ) -> tuple[EvaluatedModuleV1, ...]:
        return _model_tuple(value, field="modules", key=lambda item: item.module_id)

    @field_validator("generator_units")
    @classmethod
    def _validate_generators(
        cls, value: tuple[GeneratorUnitExpectation, ...]
    ) -> tuple[GeneratorUnitExpectation, ...]:
        return _model_tuple(
            value,
            field="generator_units",
            key=lambda item: item.generator_unit_id,
        )

    @field_validator("compile_units")
    @classmethod
    def _validate_compilers(
        cls, value: tuple[CompileUnitExpectation, ...]
    ) -> tuple[CompileUnitExpectation, ...]:
        return _model_tuple(
            value,
            field="compile_units",
            key=lambda item: item.compile_unit_id,
        )

    @field_validator("package_units")
    @classmethod
    def _validate_packages(
        cls, value: tuple[PackageUnitExpectation, ...]
    ) -> tuple[PackageUnitExpectation, ...]:
        return _model_tuple(
            value,
            field="package_units",
            key=lambda item: item.package_unit_id,
        )

    @field_validator("compile_dependency_edges")
    @classmethod
    def _validate_dependency_edges(
        cls, value: tuple[CompileDependencyEdge, ...]
    ) -> tuple[CompileDependencyEdge, ...]:
        return _model_tuple(
            value,
            field="compile_dependency_edges",
            key=lambda item: (
                f"{item.upstream_compile_unit_id}|{item.upstream_output_root_id}|"
                f"{item.downstream_compile_unit_id}"
            ),
        )

    @field_validator("generated_root_contracts")
    @classmethod
    def _validate_generated_contracts(
        cls, value: tuple[GeneratedRootContract, ...]
    ) -> tuple[GeneratedRootContract, ...]:
        return _model_tuple(
            value,
            field="generated_root_contracts",
            key=lambda item: item.generated_root_id,
        )

    @field_validator("child_build_scopes")
    @classmethod
    def _validate_child_scopes(
        cls, value: tuple[ChildScopeExpectation, ...]
    ) -> tuple[ChildScopeExpectation, ...]:
        return _model_tuple(
            value,
            field="child_build_scopes",
            key=lambda item: item.child_scope_id,
        )

    @model_validator(mode="after")
    def _validate_primary_id_and_cross_links(self) -> EvaluatedBuildModelV1:
        _require_primary_id(
            actual=self.model_id,
            kind="evaluated-build-model-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "scope_id": self.scope_id,
                "project_root": self.project_root,
            },
            field="model_id",
        )

        modules_by_coordinate = {module.module_coordinate: module for module in self.modules}
        if len(modules_by_coordinate) != len(self.modules):
            raise ValueError("modules contain a duplicate module coordinate")
        module_paths = tuple(module.project_path for module in self.modules)
        if len(set(module_paths)) != len(module_paths):
            raise ValueError("modules contain a duplicate project path")

        module_coordinates = set(modules_by_coordinate)
        unit_module_coordinates = (
            *(unit.module_coordinate for unit in self.generator_units),
            *(unit.module_coordinate for unit in self.compile_units),
            *(unit.module_coordinate for unit in self.package_units),
        )
        for unit_coordinate in unit_module_coordinates:
            if unit_coordinate not in module_coordinates:
                raise ValueError(
                    f"unit {unit_coordinate!r} has no matching evaluated module cross-link"
                )

        unit_ids = (
            *(unit.generator_unit_id for unit in self.generator_units),
            *(unit.compile_unit_id for unit in self.compile_units),
            *(unit.package_unit_id for unit in self.package_units),
        )
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("build model contains a duplicate unit ID across unit kinds")

        all_output_roots = (
            *(root for unit in self.compile_units for root in unit.output_roots),
            *(contract.output_root for contract in self.generated_root_contracts),
            *(root for unit in self.package_units for root in unit.output_roots),
        )
        output_root_ids = tuple(root.root_id for root in all_output_roots)
        if len(set(output_root_ids)) != len(output_root_ids):
            raise ValueError(
                "build model contains a duplicate output root ID or ownership collision"
            )
        artifact_ids = tuple(
            artifact.artifact_id
            for unit in self.package_units
            for artifact in unit.requested_artifacts
        )
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("build model contains a duplicate requested artifact ID")

        for unit in self.compile_units:
            module = modules_by_coordinate[unit.module_coordinate]
            if unit.domain_id != module.domain_id or unit.build_system != module.build_system:
                raise ValueError(
                    f"compile unit {unit.compile_unit_id!r} module cross-link is inconsistent"
                )

        compile_units = {unit.compile_unit_id: unit for unit in self.compile_units}
        generator_units = {unit.generator_unit_id: unit for unit in self.generator_units}
        dependency_graph: dict[str, set[str]] = {unit_id: set() for unit_id in compile_units}
        dependency_membership: set[tuple[str, str, str]] = set()
        for edge in self.compile_dependency_edges:
            if edge.upstream_compile_unit_id == edge.downstream_compile_unit_id:
                raise ValueError("compile dependency cannot be a self edge")
            upstream = compile_units.get(edge.upstream_compile_unit_id)
            if upstream is None:
                raise ValueError(
                    f"dependency references unknown compile unit {edge.upstream_compile_unit_id}"
                )
            downstream = compile_units.get(edge.downstream_compile_unit_id)
            if downstream is None:
                raise ValueError(
                    f"dependency references unknown compile unit {edge.downstream_compile_unit_id}"
                )
            if edge.upstream_output_root_id not in {root.root_id for root in upstream.output_roots}:
                raise ValueError(
                    f"dependency references unknown output root {edge.upstream_output_root_id}"
                )
            dependency_graph[edge.upstream_compile_unit_id].add(edge.downstream_compile_unit_id)
            edge_key = (
                edge.upstream_compile_unit_id,
                edge.upstream_output_root_id,
                edge.downstream_compile_unit_id,
            )
            dependency_membership.add(edge_key)
            matching_members = tuple(
                member
                for member in downstream.compiler_environment_spec.classpath_members
                if member.upstream_unit_id == edge.upstream_compile_unit_id
                and member.upstream_root_id == edge.upstream_output_root_id
            )
            if len(matching_members) != 1:
                raise ValueError(
                    "compile dependency edge requires exactly one matching downstream "
                    "classpath member"
                )
        for downstream in self.compile_units:
            for member in downstream.compiler_environment_spec.classpath_members:
                if member.upstream_unit_id is None:
                    continue
                member_key = (
                    member.upstream_unit_id,
                    member.upstream_root_id,
                    downstream.compile_unit_id,
                )
                if member_key not in dependency_membership:
                    raise ValueError(
                        "upstream classpath member has no matching compile dependency edge"
                    )
        _require_acyclic_graph(dependency_graph, field="compile dependency")

        generated_contracts = {
            contract.generated_root_id: contract for contract in self.generated_root_contracts
        }
        generated_root_owners: dict[str, str] = {}
        for generator in self.generator_units:
            for root_id in generator.generated_root_ids:
                if root_id in generated_root_owners:
                    raise ValueError(f"generated root {root_id!r} has duplicate generator owners")
                generated_root_owners[root_id] = generator.generator_unit_id
                contract = generated_contracts.get(root_id)
                if contract is None:
                    raise ValueError(f"generator generated_root {root_id!r} has no contract")
                if (
                    contract.producer_unit_kind != "generator"
                    or contract.producer_unit_id != generator.generator_unit_id
                ):
                    raise ValueError(
                        f"generator generated_root {root_id!r} producer cross-link is inconsistent"
                    )

        for contract in self.generated_root_contracts:
            if contract.active_profile_or_variant not in self.active_profiles_or_variants:
                raise ValueError(
                    "generated root profile or variant is not active: "
                    f"{contract.active_profile_or_variant!r}"
                )
            if contract.mode == "same_round_intermediate":
                if contract.producer_unit_kind != "compile" or contract.consumer_unit_ids != (
                    contract.producer_unit_id,
                ):
                    raise ValueError(
                        "same_round generated root requires identical producer and consumer"
                    )
            if contract.producer_unit_kind == "generator":
                generator_producer = generator_units.get(contract.producer_unit_id)
                if generator_producer is None:
                    raise ValueError(
                        f"generated root references unknown generator {contract.producer_unit_id}"
                    )
                if contract.generated_root_id not in generator_producer.generated_root_ids:
                    raise ValueError("generated root producer cross-link is inconsistent")
            else:
                compile_producer = compile_units.get(contract.producer_unit_id)
                if compile_producer is None:
                    raise ValueError(
                        f"generated root references unknown compiler {contract.producer_unit_id}"
                    )
                if (
                    contract.mode == "same_round_intermediate"
                    and compile_producer.adapter_status != "supported"
                ):
                    raise ValueError("same_round generated root requires a supported compiler unit")
            unknown_consumers = set(contract.consumer_unit_ids) - compile_units.keys()
            if unknown_consumers:
                raise ValueError(
                    f"generated root consumer is unknown: {sorted(unknown_consumers)!r}"
                )
        for index, left in enumerate(all_output_roots):
            for right in all_output_roots[index + 1 :]:
                if left.path == right.path:
                    if (
                        left.verification_mode != "shared_owned_entries"
                        or right.verification_mode != "shared_owned_entries"
                    ):
                        raise ValueError(
                            "co-located output roots require shared_owned_entries mode"
                        )
                    continue
                left_prefix = left.path.rstrip("/") + "/"
                right_prefix = right.path.rstrip("/") + "/"
                if right.path.startswith(left_prefix):
                    outer = left
                elif left.path.startswith(right_prefix):
                    outer = right
                else:
                    continue
                if outer.verification_mode != "shared_owned_entries":
                    raise ValueError(
                        "an outer nested output root requires shared_owned_entries mode"
                    )
        unit_output_roots: dict[str, set[str]] = {
            unit.compile_unit_id: {root.root_id for root in unit.output_roots}
            for unit in self.compile_units
        }
        unit_output_roots.update(
            {unit.generator_unit_id: set(unit.generated_root_ids) for unit in self.generator_units}
        )
        member_groups: tuple[tuple[str, tuple[ResolvedMemberSpec, ...]], ...] = (
            ("build config", self.build_config_members),
            *(
                (
                    f"generator unit {unit.generator_unit_id} inputs",
                    unit.input_roots,
                )
                for unit in self.generator_units
            ),
            *(
                (
                    f"compile unit {unit.compile_unit_id} classpath",
                    unit.compiler_environment_spec.classpath_members,
                )
                for unit in self.compile_units
            ),
            *(
                (
                    f"generator unit {unit.generator_unit_id} environment",
                    (
                        *unit.generator_environment_spec.tool_or_plugin_members,
                        *unit.generator_environment_spec.dependency_members,
                    ),
                )
                for unit in self.generator_units
            ),
            *(
                (
                    f"package unit {unit.package_unit_id} environment",
                    (
                        *unit.resource_roots,
                        *unit.manifest_or_packaging_config,
                        *unit.packaging_environment_spec.plugin_or_tool_members,
                        *unit.packaging_environment_spec.external_dependency_members,
                    ),
                )
                for unit in self.package_units
            ),
        )
        for owner, members in member_groups:
            member_ids = tuple(member.member_id for member in members)
            if len(set(member_ids)) != len(member_ids):
                raise ValueError(f"{owner} contains a duplicate member ID")
            for member in members:
                if member.upstream_unit_id is None:
                    continue
                roots = unit_output_roots.get(member.upstream_unit_id)
                if roots is None:
                    raise ValueError(
                        f"{owner} member {member.member_id!r} references unknown upstream unit"
                    )
                if member.upstream_root_id not in roots:
                    raise ValueError(
                        f"{owner} member {member.member_id!r} references unknown upstream root"
                    )

        build_path_bases = (
            *(unit.compiler_environment_spec.executable_path_basis for unit in self.compile_units),
            *(unit.compiler_environment_spec.toolchain_path_basis for unit in self.compile_units),
            *(
                unit.generator_environment_spec.executable_path_basis
                for unit in self.generator_units
            ),
            *(
                unit.generator_environment_spec.toolchain_path_basis
                for unit in self.generator_units
            ),
            *(unit.packaging_environment_spec.executable_path_basis for unit in self.package_units),
            *(unit.packaging_environment_spec.toolchain_path_basis for unit in self.package_units),
        )
        member_descriptors: dict[str, bytes] = {}

        def register_member(member_id: str, descriptor_kind: str, body: Any) -> None:
            descriptor = canonical_build_evidence_bytes(
                {
                    "descriptor_kind": descriptor_kind,
                    "body": body,
                }
            )
            previous = member_descriptors.setdefault(member_id, descriptor)
            if previous != descriptor:
                raise ValueError(f"member_id {member_id!r} is rebound to a different semantic body")

        for basis in build_path_bases:
            register_member(basis.member_id, "build_path_basis", basis)
        for _, members in member_groups:
            for member in members:
                register_member(member.member_id, "resolved_member", member)
        for root in all_output_roots:
            register_member(root.member_id, "output_root", root)

        for package in self.package_units:
            unknown_inputs = set(package.input_compile_unit_ids) - compile_units.keys()
            if unknown_inputs:
                raise ValueError(
                    f"package input compile unit is unknown: {sorted(unknown_inputs)!r}"
                )
            artifact_paths = tuple(artifact.path for artifact in package.requested_artifacts)
            if len(set(artifact_paths)) != len(artifact_paths):
                raise ValueError(
                    f"package {package.package_unit_id!r} has duplicate artifact paths"
                )

        child_roots: list[str] = []
        project_prefix = self.project_root.rstrip("/") + "/"
        for child in self.child_build_scopes:
            if child.parent_scope_id != self.scope_id:
                raise ValueError("child scope parent cross-link does not match model scope")
            if child.child_scope_id == self.scope_id:
                raise ValueError("child scope creates a self edge or cycle")
            if not child.child_project_root.startswith(project_prefix):
                raise ValueError("child project root escapes the evaluated project root")
            child_roots.append(child.child_project_root)
        for index, left_child_root in enumerate(child_roots):
            left_prefix = left_child_root.rstrip("/") + "/"
            for right_child_root in child_roots[index + 1 :]:
                right_prefix = right_child_root.rstrip("/") + "/"
                if (
                    left_child_root == right_child_root
                    or left_child_root.startswith(right_prefix)
                    or right_child_root.startswith(left_prefix)
                ):
                    raise ValueError("child project roots overlap without a disjoint partition")
        return self


class JvmBuildExpectationV1(StrictBuildEvidenceModel):
    expectation_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    census_id: CanonicalIdentifier
    scope_id: CanonicalIdentifier
    model_id: CanonicalIdentifier
    scope_fingerprint: Sha256
    source_state_fingerprint: Sha256
    build_config_fingerprint: Sha256
    required_java_edges: tuple[RequiredSourceEdge, ...]
    source_classifications: tuple[SourceClassification, ...]
    unclassified_source_ids: tuple[CanonicalIdentifier, ...]
    required_generator_unit_ids: tuple[CanonicalIdentifier, ...]
    required_compile_unit_ids: tuple[CanonicalIdentifier, ...]
    required_package_unit_ids: tuple[CanonicalIdentifier, ...]
    unsupported_active_unit_ids: tuple[CanonicalIdentifier, ...]
    required_child_scope_ids: tuple[CanonicalIdentifier, ...]
    delegation_edges: tuple[DelegationEdge, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("required_java_edges")
    @classmethod
    def _validate_required_edges(
        cls, value: tuple[RequiredSourceEdge, ...]
    ) -> tuple[RequiredSourceEdge, ...]:
        return _model_tuple(
            value,
            field="required_java_edges",
            key=lambda item: f"{item.source_id}|{item.compile_unit_id}",
        )

    @field_validator("source_classifications")
    @classmethod
    def _validate_classifications(
        cls, value: tuple[SourceClassification, ...]
    ) -> tuple[SourceClassification, ...]:
        return _model_tuple(
            value,
            field="source_classifications",
            key=lambda item: item.source_id,
        )

    @field_validator("delegation_edges")
    @classmethod
    def _validate_delegations(cls, value: tuple[DelegationEdge, ...]) -> tuple[DelegationEdge, ...]:
        return _model_tuple(
            value,
            field="delegation_edges",
            key=lambda item: item.parent_source_id,
        )

    @field_validator(
        "unclassified_source_ids",
        "required_generator_unit_ids",
        "required_compile_unit_ids",
        "required_package_unit_ids",
        "unsupported_active_unit_ids",
        "required_child_scope_ids",
    )
    @classmethod
    def _validate_identifier_sets(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _identifier_tuple(value, field=info.field_name)

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_identity_and_source_conservation(self) -> JvmBuildExpectationV1:
        classifications = {
            classification.source_id: classification
            for classification in self.source_classifications
        }
        if set(self.unclassified_source_ids) & classifications.keys():
            raise ValueError("unclassified source IDs overlap classified sources")

        edges_by_source: dict[str, set[str]] = {}
        for edge in self.required_java_edges:
            edges_by_source.setdefault(edge.source_id, set()).add(edge.compile_unit_id)
            classification = classifications.get(edge.source_id)
            if classification is None or classification.classification not in {
                "required",
                "unsupported_active",
            }:
                raise ValueError(
                    f"required source edge {edge.source_id!r} has no active classification"
                )
            if classification.language != "java":
                raise ValueError("non-Java required source cannot carry a Java source edge")
            admissible_unit_ids = (
                (*self.required_compile_unit_ids, *self.unsupported_active_unit_ids)
                if classification.classification == "required"
                else self.unsupported_active_unit_ids
            )
            if edge.compile_unit_id not in admissible_unit_ids:
                raise ValueError(
                    f"required source edge references an unknown active unit {edge.compile_unit_id!r}"
                )

        delegated_by_source = {edge.parent_source_id: edge for edge in self.delegation_edges}
        for classification in self.source_classifications:
            if classification.classification_basis.model_id != self.model_id:
                raise ValueError("source classification model cross-link is inconsistent")
            expected_units = set(classification.expected_compile_unit_ids)
            if classification.classification == "required":
                active_unit_ids = set(self.required_compile_unit_ids) | set(
                    self.unsupported_active_unit_ids
                )
                if not expected_units.issubset(active_unit_ids):
                    raise ValueError("required classification references an unknown active unit")
                if not expected_units.intersection(self.required_compile_unit_ids):
                    raise ValueError(
                        "required classification must include at least one supported required unit"
                    )
                if classification.language == "java":
                    if edges_by_source.get(classification.source_id, set()) != expected_units:
                        raise ValueError(
                            "required Java source classification and required edge sets differ"
                        )
                elif classification.source_id in edges_by_source:
                    raise ValueError("non-Java required source carries a Java source edge")
            elif classification.classification == "unsupported_active":
                if not expected_units.issubset(self.unsupported_active_unit_ids):
                    raise ValueError(
                        "unsupported classification references an unknown unsupported unit"
                    )
                if classification.language == "java":
                    if edges_by_source.get(classification.source_id, set()) != expected_units:
                        raise ValueError(
                            "unsupported active Java classification and required edge sets differ"
                        )
                elif classification.source_id in edges_by_source:
                    raise ValueError("unsupported non-Java source carries a Java source edge")
            elif classification.source_id in edges_by_source:
                raise ValueError("non-required classification carries a required source edge")

            delegation = delegated_by_source.get(classification.source_id)
            if classification.classification == "delegated_child_scope":
                if delegation is None:
                    raise ValueError("delegated classification is missing its delegation edge")
                if classification.classification_basis.basis_ref != classification.child_scope_id:
                    raise ValueError(
                        "delegated classification basis_ref does not match child_scope_id"
                    )
                if delegation.child_scope_id != classification.child_scope_id:
                    raise ValueError("delegation child_scope cross-link is inconsistent")
                if delegation.child_scope_id not in self.required_child_scope_ids:
                    raise ValueError("delegation references a non-required child scope")
            elif delegation is not None:
                raise ValueError("delegation edge belongs to a non-delegated source")

        if set(delegated_by_source) != {
            item.source_id
            for item in self.source_classifications
            if item.classification == "delegated_child_scope"
        }:
            raise ValueError("delegation edges do not conserve delegated classifications")

        projection = self.model_dump(
            mode="python",
            exclude={"expectation_id", "conflicts"},
        )
        _require_primary_id(
            actual=self.expectation_id,
            kind="jvm-build-expectation-v1",
            projection=projection,
            field="expectation_id",
        )
        return self


class CurrentPhysicalMemberV1(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    member_role: PhysicalMemberRole
    path_kind: PathKind
    path: AbsolutePath
    byte_count: NonnegativeInt
    sha256: Sha256


class CurrentOutputRootV1(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    root_id: CanonicalIdentifier
    path: AbsolutePath
    verification_mode: VerificationMode
    tree_or_owned_entries_sha256: Sha256
    entry_count: NonnegativeInt
    scan_complete: bool


class CurrentOutputEntryV1(StrictBuildEvidenceModel):
    root_id: CanonicalIdentifier
    relative_path: RelativePath
    kind: Literal["class", "jar", "war", "resource", "other"]
    byte_count: NonnegativeInt
    sha256: Sha256


class MissingPhysicalMemberV1(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    path: AbsolutePath


class UnreadablePhysicalMemberV1(StrictBuildEvidenceModel):
    member_id: CanonicalIdentifier
    path: AbsolutePath
    reason: BoundedText


class CurrentPhysicalBasisSnapshotV1(StrictBuildEvidenceModel):
    physical_basis_snapshot_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    expectation_id: CanonicalIdentifier
    build_model_id: CanonicalIdentifier
    requested_member_ids: tuple[CanonicalIdentifier, ...]
    current_members: tuple[CurrentPhysicalMemberV1, ...]
    current_output_roots: tuple[CurrentOutputRootV1, ...]
    current_output_entries: tuple[CurrentOutputEntryV1, ...]
    missing_members: tuple[MissingPhysicalMemberV1, ...]
    unreadable_members: tuple[UnreadablePhysicalMemberV1, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("requested_member_ids")
    @classmethod
    def _validate_requested_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="requested_member_ids")

    @field_validator("current_members")
    @classmethod
    def _validate_current_members(
        cls, value: tuple[CurrentPhysicalMemberV1, ...]
    ) -> tuple[CurrentPhysicalMemberV1, ...]:
        try:
            return _model_tuple(
                value,
                field="current_members",
                key=lambda item: item.member_id,
            )
        except ValueError as exc:
            raise ValueError(f"current_members requested partition is invalid: {exc}") from exc

    @field_validator("current_output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[CurrentOutputRootV1, ...]
    ) -> tuple[CurrentOutputRootV1, ...]:
        roots = _model_tuple(
            value,
            field="current_output_roots",
            key=lambda item: item.member_id,
        )
        root_ids = tuple(item.root_id for item in roots)
        if len(set(root_ids)) != len(root_ids):
            raise ValueError("current_output_roots contains a duplicate root_id")
        return roots

    @field_validator("current_output_entries")
    @classmethod
    def _validate_output_entries(
        cls, value: tuple[CurrentOutputEntryV1, ...]
    ) -> tuple[CurrentOutputEntryV1, ...]:
        return _model_tuple(
            value,
            field="current_output_entries",
            key=lambda item: f"{item.root_id}|{item.relative_path}",
        )

    @field_validator("missing_members")
    @classmethod
    def _validate_missing_members(
        cls, value: tuple[MissingPhysicalMemberV1, ...]
    ) -> tuple[MissingPhysicalMemberV1, ...]:
        return _model_tuple(value, field="missing_members", key=lambda item: item.member_id)

    @field_validator("unreadable_members")
    @classmethod
    def _validate_unreadable_members(
        cls, value: tuple[UnreadablePhysicalMemberV1, ...]
    ) -> tuple[UnreadablePhysicalMemberV1, ...]:
        return _model_tuple(value, field="unreadable_members", key=lambda item: item.member_id)

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_partition_and_identity(self) -> CurrentPhysicalBasisSnapshotV1:
        partition_ids = (
            *(item.member_id for item in self.current_members),
            *(item.member_id for item in self.current_output_roots),
            *(item.member_id for item in self.missing_members),
            *(item.member_id for item in self.unreadable_members),
        )
        if len(set(partition_ids)) != len(partition_ids):
            raise ValueError("physical requested-member partition contains a duplicate")
        if set(partition_ids) != set(self.requested_member_ids):
            raise ValueError("physical partition does not exactly match requested members")

        roots = {root.root_id: root for root in self.current_output_roots}
        entries_by_root: dict[str, int] = {root_id: 0 for root_id in roots}
        for root in self.current_output_roots:
            if not root.scan_complete:
                raise ValueError(f"output root {root.root_id!r} scan_complete is false")
        for entry in self.current_output_entries:
            if entry.root_id not in roots:
                raise ValueError(f"output entry references unknown root {entry.root_id!r}")
            entries_by_root[entry.root_id] += 1
        for root_id, root in roots.items():
            if root.entry_count != entries_by_root[root_id]:
                raise ValueError(f"output root {root_id!r} entry_count does not match entries")

        projection = self.model_dump(
            mode="python",
            exclude={"physical_basis_snapshot_id", "conflicts"},
        )
        _require_primary_id(
            actual=self.physical_basis_snapshot_id,
            kind="current-physical-basis-v1",
            projection=projection,
            field="physical_basis_snapshot_id",
        )
        return self


def _source_digest_tuple(
    value: Sequence[SourceDigestEntry], *, field: str
) -> tuple[SourceDigestEntry, ...]:
    entries = _model_tuple(value, field=field, key=lambda item: item.source_id)
    paths = tuple(item.path for item in entries)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{field} contains a duplicate source path")
    return entries


def _resolved_digest_tuple(
    value: Sequence[ResolvedDigestEntry], *, field: str
) -> tuple[ResolvedDigestEntry, ...]:
    entries = _ordered_model_tuple(value, field=field, key=lambda item: item.member_id)
    paths = tuple(item.path for item in entries)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{field} contains a duplicate resolved path")
    return entries


def _compile_output_digest_tuple(
    value: Sequence[CompileOutputDigestEntry], *, field: str
) -> tuple[CompileOutputDigestEntry, ...]:
    return _ordered_model_tuple(
        value,
        field=field,
        key=lambda item: f"{item.compile_unit_id}|{item.root_id}",
    )


def _output_root_spec_tuple(
    value: Sequence[OutputRootSpec], *, field: str
) -> tuple[OutputRootSpec, ...]:
    roots = _model_tuple(value, field=field, key=lambda item: item.root_id)
    paths = tuple(item.path for item in roots)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{field} contains a duplicate output path")
    return roots


def _requested_artifact_tuple(
    value: Sequence[RequestedArtifactSpec], *, field: str
) -> tuple[RequestedArtifactSpec, ...]:
    artifacts = _model_tuple(value, field=field, key=lambda item: item.artifact_id)
    paths = tuple(item.path for item in artifacts)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{field} contains a duplicate artifact path")
    return artifacts


class FinalGeneratorUnitBasis(StrictBuildEvidenceModel):
    generator_unit_id: CanonicalIdentifier
    build_config_fingerprint: Sha256
    input_entries: tuple[ResolvedDigestEntry, ...]
    generator_executable_path: AbsolutePath
    generator_executable_sha256: Sha256
    generator_tool_entries: tuple[ResolvedDigestEntry, ...]
    generator_dependency_entries: tuple[ResolvedDigestEntry, ...]
    generator_args_fingerprint: Sha256
    generator_toolchain_fingerprint: Sha256
    generated_output_roots: tuple[OutputRootSpec, ...]
    generator_unit_identity_hash: Sha256

    @field_validator(
        "input_entries",
        "generator_tool_entries",
        "generator_dependency_entries",
    )
    @classmethod
    def _validate_digest_entries(
        cls, value: tuple[ResolvedDigestEntry, ...], info: Any
    ) -> tuple[ResolvedDigestEntry, ...]:
        return _resolved_digest_tuple(value, field=info.field_name)

    @field_validator("generated_output_roots")
    @classmethod
    def _validate_generated_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _output_root_spec_tuple(value, field="generated_output_roots")

    @model_validator(mode="after")
    def _validate_identity(self) -> FinalGeneratorUnitBasis:
        if not self.generated_output_roots:
            raise ValueError("final generator basis requires a generated output root")
        projection = {
            "generator_unit_id": self.generator_unit_id,
            "build_config_fingerprint": self.build_config_fingerprint,
            "input_entries": self.input_entries,
            "generator_executable_path": self.generator_executable_path,
            "generator_executable_sha256": self.generator_executable_sha256,
            "generator_tool_entries": self.generator_tool_entries,
            "generator_dependency_entries": self.generator_dependency_entries,
            "generator_args_fingerprint": self.generator_args_fingerprint,
            "generator_toolchain_fingerprint": self.generator_toolchain_fingerprint,
            "generated_output_roots": self.generated_output_roots,
        }
        if self.generator_unit_identity_hash != generator_unit_identity_hash(projection):
            raise ValueError("generator unit identity hash does not match its projection")
        return self


class FinalCompileUnitBasis(StrictBuildEvidenceModel):
    compile_unit_id: CanonicalIdentifier
    build_config_fingerprint: Sha256
    required_source_entries: tuple[SourceDigestEntry, ...]
    source_set_fingerprint: Sha256
    compiler_executable_path: AbsolutePath
    compiler_executable_sha256: Sha256
    compiler_version: BoundedText
    compiler_args_fingerprint: Sha256
    classpath_entries: tuple[ResolvedDigestEntry, ...]
    dependency_unit_identity_hashes: tuple[Sha256, ...]
    toolchain_fingerprint: Sha256
    output_roots: tuple[OutputRootSpec, ...]
    compile_unit_identity_hash: Sha256

    @field_validator("required_source_entries")
    @classmethod
    def _validate_sources(
        cls, value: tuple[SourceDigestEntry, ...]
    ) -> tuple[SourceDigestEntry, ...]:
        return _source_digest_tuple(value, field="required_source_entries")

    @field_validator("classpath_entries")
    @classmethod
    def _validate_classpath(
        cls, value: tuple[ResolvedDigestEntry, ...]
    ) -> tuple[ResolvedDigestEntry, ...]:
        return _resolved_digest_tuple(value, field="classpath_entries")

    @field_validator("dependency_unit_identity_hashes")
    @classmethod
    def _validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="dependency_unit_identity_hashes")
        if len(set(items)) != len(items):
            raise ValueError("dependency_unit_identity_hashes contains a duplicate")
        return items

    @field_validator("output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _output_root_spec_tuple(value, field="output_roots")

    @model_validator(mode="after")
    def _validate_identity(self) -> FinalCompileUnitBasis:
        projection = self.model_dump(mode="python", exclude={"compile_unit_identity_hash"})
        if self.compile_unit_identity_hash != compile_unit_identity_hash(projection):
            raise ValueError("compile unit identity hash does not match its projection")
        return self


class FinalPackageUnitBasis(StrictBuildEvidenceModel):
    package_unit_id: CanonicalIdentifier
    build_config_fingerprint: Sha256
    input_compile_unit_identity_hashes: tuple[Sha256, ...]
    input_compile_output_digests: tuple[CompileOutputDigestEntry, ...]
    resource_entries: tuple[ResolvedDigestEntry, ...]
    packaging_executable_path: AbsolutePath
    packaging_executable_sha256: Sha256
    packaging_tool_entries: tuple[ResolvedDigestEntry, ...]
    external_dependency_entries: tuple[ResolvedDigestEntry, ...]
    packaging_args_fingerprint: Sha256
    packaging_toolchain_fingerprint: Sha256
    packaging_config_fingerprint: Sha256
    output_roots: tuple[OutputRootSpec, ...]
    requested_artifacts: tuple[RequestedArtifactSpec, ...]
    package_unit_identity_hash: Sha256

    @field_validator("input_compile_unit_identity_hashes")
    @classmethod
    def _validate_compile_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="input_compile_unit_identity_hashes")
        if len(set(items)) != len(items):
            raise ValueError("input_compile_unit_identity_hashes contains a duplicate")
        return items

    @field_validator("input_compile_output_digests")
    @classmethod
    def _validate_output_digests(
        cls, value: tuple[CompileOutputDigestEntry, ...]
    ) -> tuple[CompileOutputDigestEntry, ...]:
        return _compile_output_digest_tuple(value, field="input_compile_output_digests")

    @field_validator(
        "resource_entries",
        "packaging_tool_entries",
        "external_dependency_entries",
    )
    @classmethod
    def _validate_resolved_entries(
        cls, value: tuple[ResolvedDigestEntry, ...], info: Any
    ) -> tuple[ResolvedDigestEntry, ...]:
        return _resolved_digest_tuple(value, field=info.field_name)

    @field_validator("requested_artifacts")
    @classmethod
    def _validate_artifacts(
        cls, value: tuple[RequestedArtifactSpec, ...]
    ) -> tuple[RequestedArtifactSpec, ...]:
        return _requested_artifact_tuple(value, field="requested_artifacts")

    @field_validator("output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _output_root_spec_tuple(value, field="output_roots")

    @model_validator(mode="after")
    def _validate_identity(self) -> FinalPackageUnitBasis:
        if not self.output_roots or not self.requested_artifacts:
            raise ValueError("final package basis requires an output root and artifact")
        roots = {root.root_id: root for root in self.output_roots}
        for artifact in self.requested_artifacts:
            root = roots.get(artifact.root_id)
            if root is None:
                raise ValueError(
                    f"package artifact references unknown output root {artifact.root_id!r}"
                )
            root_prefix = root.path.rstrip("/") + "/"
            if artifact.path != root.path and not artifact.path.startswith(root_prefix):
                raise ValueError("package artifact path is not contained by its output root")
        projection = {
            "package_unit_id": self.package_unit_id,
            "build_config_fingerprint": self.build_config_fingerprint,
            "input_compile_unit_identity_hashes": (self.input_compile_unit_identity_hashes),
            "input_compile_output_digests": self.input_compile_output_digests,
            "resource_entries": self.resource_entries,
            "packaging_executable_path": self.packaging_executable_path,
            "packaging_executable_sha256": self.packaging_executable_sha256,
            "packaging_tool_entries": self.packaging_tool_entries,
            "external_dependency_entries": self.external_dependency_entries,
            "packaging_args_fingerprint": self.packaging_args_fingerprint,
            "packaging_toolchain_fingerprint": self.packaging_toolchain_fingerprint,
            "packaging_config_fingerprint": self.packaging_config_fingerprint,
            "output_roots": self.output_roots,
            "requested_artifacts": self.requested_artifacts,
        }
        if self.package_unit_identity_hash != package_unit_identity_hash(projection):
            raise ValueError("package unit identity hash does not match its projection")
        return self


class FinalBuildBasisV1(StrictBuildEvidenceModel):
    basis_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    basis_revision: PositiveInt
    expectation_id: CanonicalIdentifier
    physical_basis_snapshot_id: CanonicalIdentifier
    source_state_fingerprint: Sha256
    build_config_fingerprint: Sha256
    generator_unit_bases: tuple[FinalGeneratorUnitBasis, ...]
    compile_unit_bases: tuple[FinalCompileUnitBasis, ...]
    package_unit_bases: tuple[FinalPackageUnitBasis, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("generator_unit_bases")
    @classmethod
    def _validate_generator_bases(
        cls, value: tuple[FinalGeneratorUnitBasis, ...]
    ) -> tuple[FinalGeneratorUnitBasis, ...]:
        return _model_tuple(
            value,
            field="generator_unit_bases",
            key=lambda item: item.generator_unit_id,
        )

    @field_validator("compile_unit_bases")
    @classmethod
    def _validate_compile_bases(
        cls, value: tuple[FinalCompileUnitBasis, ...]
    ) -> tuple[FinalCompileUnitBasis, ...]:
        return _model_tuple(
            value,
            field="compile_unit_bases",
            key=lambda item: item.compile_unit_id,
        )

    @field_validator("package_unit_bases")
    @classmethod
    def _validate_package_bases(
        cls, value: tuple[FinalPackageUnitBasis, ...]
    ) -> tuple[FinalPackageUnitBasis, ...]:
        return _model_tuple(
            value,
            field="package_unit_bases",
            key=lambda item: item.package_unit_id,
        )

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_primary_id(self) -> FinalBuildBasisV1:
        _require_primary_id(
            actual=self.basis_id,
            kind="final-build-basis-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "expectation_id": self.expectation_id,
            },
            field="basis_id",
        )
        unit_ids = (
            *(item.generator_unit_id for item in self.generator_unit_bases),
            *(item.compile_unit_id for item in self.compile_unit_bases),
            *(item.package_unit_id for item in self.package_unit_bases),
        )
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("final build basis contains duplicate unit IDs")
        if (
            any(
                item.build_config_fingerprint != self.build_config_fingerprint
                for item in self.generator_unit_bases
            )
            or any(
                item.build_config_fingerprint != self.build_config_fingerprint
                for item in self.compile_unit_bases
            )
            or any(
                item.build_config_fingerprint != self.build_config_fingerprint
                for item in self.package_unit_bases
            )
        ):
            raise ValueError("unit basis build config fingerprint differs from final build basis")
        return self


class GeneratorUnitObservationV1(FinalGeneratorUnitBasis):
    observation_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    run_id: CanonicalIdentifier
    receipt_id: CanonicalIdentifier
    contract_id: CanonicalIdentifier
    outcome: Literal[
        "executed_success",
        "current_noop",
        "up_to_date",
        "from_cache",
        "skipped",
        "failed",
    ]
    actual_invocation_scope: ActualInvocationScopeV1
    conflicts: tuple[BoundedText, ...]

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_observation_id(self) -> GeneratorUnitObservationV1:
        _require_primary_id(
            actual=self.observation_id,
            kind="generator-observation-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "run_id": self.run_id,
                "receipt_id": self.receipt_id,
                "contract_id": self.contract_id,
                "generator_unit_id": self.generator_unit_id,
            },
            field="observation_id",
        )
        return self


class CompileUnitObservationV1(StrictBuildEvidenceModel):
    observation_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    run_id: CanonicalIdentifier
    receipt_id: CanonicalIdentifier
    contract_id: CanonicalIdentifier
    compile_unit_id: CanonicalIdentifier
    build_config_fingerprint: Sha256
    compile_unit_identity_hash: Sha256
    source_entries: tuple[SourceDigestEntry, ...]
    source_set_fingerprint: Sha256
    compiler_executable_path: AbsolutePath
    compiler_executable_sha256: Sha256
    compiler_version: BoundedText
    compiler_args_fingerprint: Sha256
    classpath_entries: tuple[ResolvedDigestEntry, ...]
    dependency_unit_identity_hashes: tuple[Sha256, ...]
    toolchain_fingerprint: Sha256
    observed_output_roots: tuple[OutputRootSpec, ...]
    outcome: Literal[
        "executed_success",
        "current_noop",
        "up_to_date",
        "from_cache",
        "no_source",
        "skipped",
        "failed",
    ]
    actual_invocation_scope: ActualInvocationScopeV1
    generated_during_compile: tuple[SourceDigestEntry, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("source_entries", "generated_during_compile")
    @classmethod
    def _validate_sources(
        cls, value: tuple[SourceDigestEntry, ...], info: Any
    ) -> tuple[SourceDigestEntry, ...]:
        return _source_digest_tuple(value, field=info.field_name)

    @field_validator("classpath_entries")
    @classmethod
    def _validate_classpath(
        cls, value: tuple[ResolvedDigestEntry, ...]
    ) -> tuple[ResolvedDigestEntry, ...]:
        return _resolved_digest_tuple(value, field="classpath_entries")

    @field_validator("dependency_unit_identity_hashes")
    @classmethod
    def _validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        items = validate_bounded_sequence(value, field="dependency_unit_identity_hashes")
        if len(set(items)) != len(items):
            raise ValueError("dependency_unit_identity_hashes contains a duplicate")
        return items

    @field_validator("observed_output_roots")
    @classmethod
    def _validate_output_roots(
        cls, value: tuple[OutputRootSpec, ...]
    ) -> tuple[OutputRootSpec, ...]:
        return _output_root_spec_tuple(value, field="observed_output_roots")

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_identities(self) -> CompileUnitObservationV1:
        identity_projection = {
            "compile_unit_id": self.compile_unit_id,
            "build_config_fingerprint": self.build_config_fingerprint,
            "required_source_entries": self.source_entries,
            "source_set_fingerprint": self.source_set_fingerprint,
            "compiler_executable_path": self.compiler_executable_path,
            "compiler_executable_sha256": self.compiler_executable_sha256,
            "compiler_version": self.compiler_version,
            "compiler_args_fingerprint": self.compiler_args_fingerprint,
            "classpath_entries": self.classpath_entries,
            "dependency_unit_identity_hashes": self.dependency_unit_identity_hashes,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "output_roots": self.observed_output_roots,
        }
        if self.compile_unit_identity_hash != compile_unit_identity_hash(identity_projection):
            raise ValueError("compile unit identity hash does not match its projection")
        _require_primary_id(
            actual=self.observation_id,
            kind="compile-observation-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "run_id": self.run_id,
                "receipt_id": self.receipt_id,
                "contract_id": self.contract_id,
                "compile_unit_id": self.compile_unit_id,
            },
            field="observation_id",
        )
        return self


class PackageUnitObservationV1(FinalPackageUnitBasis):
    observation_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    run_id: CanonicalIdentifier
    receipt_id: CanonicalIdentifier
    contract_id: CanonicalIdentifier
    observed_artifact_paths: tuple[RelativePath, ...]
    outcome: Literal[
        "executed_success",
        "current_noop",
        "up_to_date",
        "from_cache",
        "skipped",
        "failed",
    ]
    actual_invocation_scope: ActualInvocationScopeV1
    conflicts: tuple[BoundedText, ...]

    @field_validator("observed_artifact_paths")
    @classmethod
    def _validate_observed_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _relative_path_tuple(value, field="observed_artifact_paths")

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_observation_id(self) -> PackageUnitObservationV1:
        _require_primary_id(
            actual=self.observation_id,
            kind="package-observation-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "run_id": self.run_id,
                "receipt_id": self.receipt_id,
                "contract_id": self.contract_id,
                "package_unit_id": self.package_unit_id,
            },
            field="observation_id",
        )
        return self


class OutputRootWitness(StrictBuildEvidenceModel):
    root_id: CanonicalIdentifier
    path: RelativePath
    verification_mode: VerificationMode
    tree_or_owned_entries_sha256: Sha256
    entry_count: NonnegativeInt


class PhysicalOutputEntry(StrictBuildEvidenceModel):
    root_id: CanonicalIdentifier
    relative_path: RelativePath
    kind: Literal["class", "jar", "war", "resource", "other"]
    byte_count: NonnegativeInt
    sha256: Sha256


class PhysicalOutputWitnessV1(StrictBuildEvidenceModel):
    witness_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    producer_observation_kind: Literal["generator", "compile", "package"]
    producer_observation_id: CanonicalIdentifier
    producer_unit_id: CanonicalIdentifier
    producer_unit_identity_hash: Sha256
    roots: tuple[OutputRootWitness, ...]
    entries: tuple[PhysicalOutputEntry, ...]
    aggregate_sha256: Sha256

    @field_validator("roots")
    @classmethod
    def _validate_roots(cls, value: tuple[OutputRootWitness, ...]) -> tuple[OutputRootWitness, ...]:
        roots = _model_tuple(value, field="roots", key=lambda item: item.root_id)
        paths = tuple(item.path for item in roots)
        if len(set(paths)) != len(paths):
            raise ValueError("roots contains a duplicate path")
        return roots

    @field_validator("entries")
    @classmethod
    def _validate_entries(
        cls, value: tuple[PhysicalOutputEntry, ...]
    ) -> tuple[PhysicalOutputEntry, ...]:
        return _model_tuple(
            value,
            field="entries",
            key=lambda item: f"{item.root_id}|{item.relative_path}",
        )

    @model_validator(mode="after")
    def _validate_identity_and_root_ownership(self) -> PhysicalOutputWitnessV1:
        roots = {root.root_id: root for root in self.roots}
        entry_counts = {root_id: 0 for root_id in roots}
        for entry in self.entries:
            if entry.root_id not in roots:
                raise ValueError(f"output witness entry references unknown root {entry.root_id!r}")
            entry_counts[entry.root_id] += 1
        for root_id, root in roots.items():
            if root.entry_count != entry_counts[root_id]:
                raise ValueError(f"output witness root {root_id!r} entry_count is inconsistent")
        if self.aggregate_sha256 != physical_output_aggregate_sha256(self.roots, self.entries):
            raise ValueError("output witness aggregate identity does not match roots and entries")
        _require_primary_id(
            actual=self.witness_id,
            kind="physical-output-witness-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "producer_observation_kind": self.producer_observation_kind,
                "producer_observation_id": self.producer_observation_id,
            },
            field="witness_id",
        )
        return self


class SourceCensusCounts(StrictBuildEvidenceModel):
    total_candidates: NonnegativeInt
    classified: NonnegativeInt
    unclassified: NonnegativeInt
    delegated: NonnegativeInt
    out_of_scope_by_reason: Mapping[BoundedText, NonnegativeInt]

    @field_validator("out_of_scope_by_reason", mode="before")
    @classmethod
    def _copy_reason_mapping(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return dict(value)
        return value

    @field_validator("out_of_scope_by_reason")
    @classmethod
    def _validate_reason_counts(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if len(value) > BUILD_EVIDENCE_MAX_ENTRIES:
            raise ValueError("out_of_scope_by_reason exceeds its entry limit")
        for reason, count in value.items():
            validate_bounded_text(reason, field="out_of_scope_by_reason")
            validate_nonnegative_int(count, field=f"out_of_scope_by_reason[{reason!r}]")
        return MappingProxyType(dict(value))

    @field_serializer("out_of_scope_by_reason")
    def _serialize_reason_counts(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_conservation(self) -> SourceCensusCounts:
        if self.total_candidates != self.classified + self.unclassified:
            raise ValueError("source_census total_candidates count is not conserved")
        if self.delegated > self.classified:
            raise ValueError("source_census delegated count exceeds classified")
        if sum(self.out_of_scope_by_reason.values()) > self.classified:
            raise ValueError("source_census out-of-scope reason counts exceed classified")
        return self


class JavaSourceCounts(StrictBuildEvidenceModel):
    local_expected: NonnegativeInt
    local_current: NonnegativeInt
    delegated_expected: NonnegativeInt
    delegated_current: NonnegativeInt
    project_expected: NonnegativeInt
    project_current: NonnegativeInt
    project_missing: NonnegativeInt
    project_stale: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> JavaSourceCounts:
        if self.project_expected != self.local_expected + self.delegated_expected:
            raise ValueError("java_sources project_expected is not the local/delegated sum")
        if self.project_current != self.local_current + self.delegated_current:
            raise ValueError("java_sources project_current is not the local/delegated sum")
        if self.local_current > self.local_expected:
            raise ValueError("java_sources local current count exceeds expected")
        if self.delegated_current > self.delegated_expected:
            raise ValueError("java_sources delegated current count exceeds expected")
        if self.project_expected != (
            self.project_current + self.project_missing + self.project_stale
        ):
            raise ValueError(
                "java_sources project_current, missing, and stale counts do not conserve "
                "project_expected"
            )
        return self


class JavaEdgeCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    current: NonnegativeInt
    missing: NonnegativeInt
    stale: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> JavaEdgeCounts:
        if self.expected != self.current + self.missing + self.stale:
            raise ValueError("java_edges expected count is not conserved")
        return self


class GeneratorUnitCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    current: NonnegativeInt
    missing_or_invalid: NonnegativeInt
    unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> GeneratorUnitCounts:
        if self.expected != self.current + self.missing_or_invalid + self.unverifiable:
            raise ValueError("generator unit expected count is not conserved")
        return self


class CompileUnitCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    coverage_current: NonnegativeInt
    coverage_missing_or_invalid: NonnegativeInt
    coverage_unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> CompileUnitCounts:
        if self.expected != (
            self.coverage_current + self.coverage_missing_or_invalid + self.coverage_unverifiable
        ):
            raise ValueError("compile unit expected count is not conserved")
        return self


class PhysicalOutputCounts(StrictBuildEvidenceModel):
    observed_class_count: NonnegativeInt
    witnesses_expected: NonnegativeInt
    witnesses_verified: NonnegativeInt
    sealed_entries: NonnegativeInt
    verified_entries: NonnegativeInt
    missing_entries: NonnegativeInt
    mismatched_entries: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> PhysicalOutputCounts:
        if self.witnesses_verified > self.witnesses_expected:
            raise ValueError("physical witness verified count exceeds expected")
        if self.sealed_entries != (
            self.verified_entries + self.missing_entries + self.mismatched_entries
        ):
            raise ValueError("sealed output entry count is not conserved")
        return self


class PackageUnitCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    current: NonnegativeInt
    missing_or_invalid: NonnegativeInt
    unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> PackageUnitCounts:
        if self.expected != self.current + self.missing_or_invalid + self.unverifiable:
            raise ValueError("package unit expected count is not conserved")
        return self


class ActiveLanguageUnitCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    supported_current: NonnegativeInt
    unsupported_or_unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> ActiveLanguageUnitCounts:
        if self.expected != self.supported_current + self.unsupported_or_unverifiable:
            raise ValueError("active language unit expected count is not conserved")
        return self


class ChildScopeCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    complete: NonnegativeInt
    incomplete_or_unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> ChildScopeCounts:
        if self.expected != self.complete + self.incomplete_or_unverifiable:
            raise ValueError("child scope expected count is not conserved")
        return self


class ModuleCounts(StrictBuildEvidenceModel):
    expected: NonnegativeInt
    built: NonnegativeInt
    partial: NonnegativeInt
    unverifiable: NonnegativeInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> ModuleCounts:
        if self.expected != self.built + self.partial + self.unverifiable:
            raise ValueError("modules expected count is not conserved")
        return self


class SelectedGeneratorProof(StrictBuildEvidenceModel):
    generator_unit_id: CanonicalIdentifier
    generator_unit_identity_hash: Sha256
    observation_id: CanonicalIdentifier
    witness_id: CanonicalIdentifier


class SelectedCompileProof(StrictBuildEvidenceModel):
    compile_unit_id: CanonicalIdentifier
    compile_unit_identity_hash: Sha256
    observation_id: CanonicalIdentifier
    # Compile-input coverage and physical-output integrity are independent.
    # A current whole-unit observation remains selected when its output witness
    # is missing or invalid; that state is non-complete, but it must not erase
    # the mechanically proven source coverage.
    witness_id: CanonicalIdentifier | None


class SelectedPackageProof(StrictBuildEvidenceModel):
    package_unit_id: CanonicalIdentifier
    package_unit_identity_hash: Sha256
    observation_id: CanonicalIdentifier
    witness_id: CanonicalIdentifier


class SelectedChildReconciliation(StrictBuildEvidenceModel):
    child_scope_id: CanonicalIdentifier
    child_census_id: CanonicalIdentifier
    child_expectation_id: CanonicalIdentifier
    reconciliation_id: CanonicalIdentifier


class BuildReconciliationV1(StrictBuildEvidenceModel):
    reconciliation_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    source_census_id: CanonicalIdentifier
    goal_scope_id: CanonicalIdentifier
    build_model_id: CanonicalIdentifier
    expectation_id: CanonicalIdentifier
    final_basis_id: CanonicalIdentifier
    input_set_digest: Sha256
    requested_action: BoundedText
    status: Literal["complete", "partial", "absent", "unverifiable"]
    source_census: SourceCensusCounts
    java_sources: JavaSourceCounts
    java_edges: JavaEdgeCounts
    generator_units: GeneratorUnitCounts
    compile_units: CompileUnitCounts
    physical_outputs: PhysicalOutputCounts
    package_units: PackageUnitCounts
    active_language_units: ActiveLanguageUnitCounts
    child_scopes: ChildScopeCounts
    modules: ModuleCounts
    selected_generator_proofs: tuple[SelectedGeneratorProof, ...]
    selected_compile_proofs: tuple[SelectedCompileProof, ...]
    selected_package_proofs: tuple[SelectedPackageProof, ...]
    selected_child_reconciliations: tuple[SelectedChildReconciliation, ...]
    conflicts: tuple[BoundedText, ...]
    evidence_refs: tuple[CanonicalIdentifier, ...]

    @field_validator("selected_generator_proofs")
    @classmethod
    def _validate_generator_proofs(
        cls, value: tuple[SelectedGeneratorProof, ...]
    ) -> tuple[SelectedGeneratorProof, ...]:
        return _model_tuple(
            value,
            field="selected_generator_proofs",
            key=lambda item: item.generator_unit_id,
        )

    @field_validator("selected_compile_proofs")
    @classmethod
    def _validate_compile_proofs(
        cls, value: tuple[SelectedCompileProof, ...]
    ) -> tuple[SelectedCompileProof, ...]:
        return _model_tuple(
            value,
            field="selected_compile_proofs",
            key=lambda item: item.compile_unit_id,
        )

    @field_validator("selected_package_proofs")
    @classmethod
    def _validate_package_proofs(
        cls, value: tuple[SelectedPackageProof, ...]
    ) -> tuple[SelectedPackageProof, ...]:
        return _model_tuple(
            value,
            field="selected_package_proofs",
            key=lambda item: item.package_unit_id,
        )

    @field_validator("selected_child_reconciliations")
    @classmethod
    def _validate_child_proofs(
        cls, value: tuple[SelectedChildReconciliation, ...]
    ) -> tuple[SelectedChildReconciliation, ...]:
        children = _model_tuple(
            value,
            field="selected_child_reconciliations",
            key=lambda item: item.child_scope_id,
        )
        for field in (
            "child_census_id",
            "child_expectation_id",
            "reconciliation_id",
        ):
            identities = tuple(getattr(item, field) for item in children)
            if len(set(identities)) != len(identities):
                raise ValueError("selected child reconciliation contains a duplicate " f"{field}")
        return children

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @field_validator("evidence_refs")
    @classmethod
    def _validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="evidence_refs")

    @model_validator(mode="after")
    def _validate_counts_and_identity(self) -> BuildReconciliationV1:
        if len(self.selected_generator_proofs) != self.generator_units.current:
            raise ValueError("selected_generator proof count differs from generator current")
        if len(self.selected_compile_proofs) != self.compile_units.coverage_current:
            raise ValueError("selected_compile proof count differs from coverage_current")
        if len(self.selected_package_proofs) != self.package_units.current:
            raise ValueError("selected_package proof count differs from package current")
        if len(self.selected_child_reconciliations) != self.child_scopes.complete:
            raise ValueError("selected child reconciliation count differs from child complete")

        selected_witnesses = (
            len(self.selected_generator_proofs)
            + sum(proof.witness_id is not None for proof in self.selected_compile_proofs)
            + len(self.selected_package_proofs)
        )
        if self.physical_outputs.witnesses_verified != selected_witnesses:
            raise ValueError("physical witnesses_verified count differs from selected proofs")
        selected_refs: set[str] = set()
        selected_observations: list[str] = []
        selected_witness_ids: list[str] = []

        def add_selected_ref(observation_id: str, witness_id: str | None) -> None:
            selected_refs.add(observation_id)
            selected_observations.append(observation_id)
            if witness_id is not None:
                selected_refs.add(witness_id)
                selected_witness_ids.append(witness_id)

        for generator_proof in self.selected_generator_proofs:
            add_selected_ref(generator_proof.observation_id, generator_proof.witness_id)
        for compile_proof in self.selected_compile_proofs:
            add_selected_ref(compile_proof.observation_id, compile_proof.witness_id)
        for package_proof in self.selected_package_proofs:
            add_selected_ref(package_proof.observation_id, package_proof.witness_id)
        selected_refs.update(item.reconciliation_id for item in self.selected_child_reconciliations)
        if len(set(selected_observations)) != len(selected_observations):
            raise ValueError("selected proofs contain a duplicate observation ID")
        if len(set(selected_witness_ids)) != len(selected_witness_ids):
            raise ValueError("selected proofs contain a duplicate witness ID")
        if not selected_refs.issubset(self.evidence_refs):
            raise ValueError("evidence_refs omit a selected observation, witness, or child")

        if self.status == "complete":
            blockers = (
                self.source_census.unclassified,
                self.java_sources.project_missing,
                self.java_sources.project_stale,
                self.java_edges.missing,
                self.java_edges.stale,
                self.generator_units.missing_or_invalid,
                self.generator_units.unverifiable,
                self.compile_units.coverage_missing_or_invalid,
                self.compile_units.coverage_unverifiable,
                self.physical_outputs.witnesses_expected - self.physical_outputs.witnesses_verified,
                self.physical_outputs.missing_entries,
                self.physical_outputs.mismatched_entries,
                self.package_units.missing_or_invalid,
                self.package_units.unverifiable,
                self.active_language_units.unsupported_or_unverifiable,
                self.child_scopes.incomplete_or_unverifiable,
                self.modules.partial,
                self.modules.unverifiable,
            )
            if any(blockers) or self.conflicts:
                raise ValueError("complete reconciliation cannot carry blockers or conflicts")

        _require_primary_id(
            actual=self.reconciliation_id,
            kind="build-reconciliation-v1",
            projection={
                "evidence_epoch_id": self.evidence_epoch_id,
                "input_set_digest": self.input_set_digest,
            },
            field="reconciliation_id",
        )
        return self


class EvidenceRecordDigestV1(StrictBuildEvidenceModel):
    record_id: CanonicalIdentifier
    raw_sha256: Sha256


EvidenceRecordKind = Literal[
    "generator_observation",
    "compile_observation",
    "package_observation",
    "output_witness",
    "child_census",
    "child_expectation",
    "child_reconciliation",
]

_REQUIRED_EVIDENCE_RECORD_KINDS = (
    "generator_observation",
    "compile_observation",
    "package_observation",
    "output_witness",
    "child_census",
    "child_expectation",
    "child_reconciliation",
)


class EvidenceRecordSetCompletenessV1(StrictBuildEvidenceModel):
    record_kind: EvidenceRecordKind
    expected_record_ids: tuple[CanonicalIdentifier, ...]
    observed_record_ids: tuple[CanonicalIdentifier, ...]
    observed_record_hashes: tuple[EvidenceRecordDigestV1, ...]
    status: Literal[
        "complete",
        "unavailable",
        "set_mismatch",
        "tombstoned",
        "conflict",
    ]
    tombstoned_record_ids: tuple[CanonicalIdentifier, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator(
        "expected_record_ids",
        "observed_record_ids",
        "tombstoned_record_ids",
    )
    @classmethod
    def _validate_record_ids(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _identifier_tuple(value, field=info.field_name)

    @field_validator("observed_record_hashes")
    @classmethod
    def _validate_record_hashes(
        cls, value: tuple[EvidenceRecordDigestV1, ...]
    ) -> tuple[EvidenceRecordDigestV1, ...]:
        return _model_tuple(
            value,
            field="observed_record_hashes",
            key=lambda item: item.record_id,
        )

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_completeness_contract(self) -> EvidenceRecordSetCompletenessV1:
        hash_ids = tuple(item.record_id for item in self.observed_record_hashes)
        if hash_ids != self.observed_record_ids:
            raise ValueError("observed record hash IDs do not exactly match observed IDs")

        expected = set(self.expected_record_ids)
        observed = set(self.observed_record_ids)
        tombstoned = set(self.tombstoned_record_ids)
        if observed & tombstoned:
            raise ValueError("observed and tombstoned record IDs overlap")
        if not tombstoned.issubset(expected):
            raise ValueError("tombstoned record IDs are not part of the expected set")

        if self.status == "complete":
            if expected != observed:
                raise ValueError("complete record set expected and observed IDs differ")
            if tombstoned:
                raise ValueError("complete record set cannot contain tombstones")
            if self.conflicts:
                raise ValueError("complete record set cannot contain conflicts")
        elif self.status == "set_mismatch":
            if expected == observed:
                raise ValueError("set_mismatch record set has equal expected and observed IDs")
            if not self.conflicts:
                raise ValueError("set_mismatch record set requires conflicts")
        elif self.status == "tombstoned":
            if not tombstoned:
                raise ValueError("tombstoned record set requires a tombstone")
            if not self.conflicts:
                raise ValueError("tombstoned record set requires conflicts")
        elif self.status in {"unavailable", "conflict"} and not self.conflicts:
            raise ValueError(f"{self.status} record set requires conflicts")
        return self


class BuildEvidenceSetSnapshotV1(StrictBuildEvidenceModel):
    snapshot_id: CanonicalIdentifier
    evidence_epoch_id: CanonicalIdentifier
    admitted_run_ids: tuple[CanonicalIdentifier, ...]
    generator_observations: tuple[GeneratorUnitObservationV1, ...]
    compile_observations: tuple[CompileUnitObservationV1, ...]
    package_observations: tuple[PackageUnitObservationV1, ...]
    output_witnesses: tuple[PhysicalOutputWitnessV1, ...]
    child_censuses: tuple[JvmSourceCensusV1, ...]
    child_expectations: tuple[JvmBuildExpectationV1, ...]
    child_reconciliations: tuple[BuildReconciliationV1, ...]
    record_sets: tuple[EvidenceRecordSetCompletenessV1, ...]
    conflicts: tuple[BoundedText, ...]

    @field_validator("admitted_run_ids")
    @classmethod
    def _validate_run_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _identifier_tuple(value, field="admitted_run_ids")

    @field_validator("generator_observations")
    @classmethod
    def _validate_generator_observations(
        cls, value: tuple[GeneratorUnitObservationV1, ...]
    ) -> tuple[GeneratorUnitObservationV1, ...]:
        return _model_tuple(
            value,
            field="generator_observations",
            key=lambda item: item.observation_id,
        )

    @field_validator("compile_observations")
    @classmethod
    def _validate_compile_observations(
        cls, value: tuple[CompileUnitObservationV1, ...]
    ) -> tuple[CompileUnitObservationV1, ...]:
        return _model_tuple(
            value,
            field="compile_observations",
            key=lambda item: item.observation_id,
        )

    @field_validator("package_observations")
    @classmethod
    def _validate_package_observations(
        cls, value: tuple[PackageUnitObservationV1, ...]
    ) -> tuple[PackageUnitObservationV1, ...]:
        return _model_tuple(
            value,
            field="package_observations",
            key=lambda item: item.observation_id,
        )

    @field_validator("output_witnesses")
    @classmethod
    def _validate_output_witnesses(
        cls, value: tuple[PhysicalOutputWitnessV1, ...]
    ) -> tuple[PhysicalOutputWitnessV1, ...]:
        return _model_tuple(
            value,
            field="output_witnesses",
            key=lambda item: item.witness_id,
        )

    @field_validator("child_censuses")
    @classmethod
    def _validate_child_censuses(
        cls, value: tuple[JvmSourceCensusV1, ...]
    ) -> tuple[JvmSourceCensusV1, ...]:
        return _model_tuple(
            value,
            field="child_censuses",
            key=lambda item: item.census_id,
        )

    @field_validator("child_expectations")
    @classmethod
    def _validate_child_expectations(
        cls, value: tuple[JvmBuildExpectationV1, ...]
    ) -> tuple[JvmBuildExpectationV1, ...]:
        return _model_tuple(
            value,
            field="child_expectations",
            key=lambda item: item.expectation_id,
        )

    @field_validator("child_reconciliations")
    @classmethod
    def _validate_child_reconciliations(
        cls, value: tuple[BuildReconciliationV1, ...]
    ) -> tuple[BuildReconciliationV1, ...]:
        return _model_tuple(
            value,
            field="child_reconciliations",
            key=lambda item: item.reconciliation_id,
        )

    @field_validator("record_sets")
    @classmethod
    def _validate_record_sets(
        cls, value: tuple[EvidenceRecordSetCompletenessV1, ...]
    ) -> tuple[EvidenceRecordSetCompletenessV1, ...]:
        rows = validate_bounded_sequence(value, field="record_sets")
        kinds = tuple(item.record_kind for item in rows)
        if len(set(kinds)) != len(kinds):
            raise ValueError("record_sets contains a duplicate record kind")
        if kinds != _REQUIRED_EVIDENCE_RECORD_KINDS:
            raise ValueError("record_sets is missing or misorders a required record kind")
        return rows

    @field_validator("conflicts")
    @classmethod
    def _validate_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_text_tuple(value, field="conflicts", canonical_order=True)

    @model_validator(mode="after")
    def _validate_authorized_sets_and_identity(self) -> BuildEvidenceSetSnapshotV1:
        object_ids = {
            "generator_observation": tuple(
                item.observation_id for item in self.generator_observations
            ),
            "compile_observation": tuple(item.observation_id for item in self.compile_observations),
            "package_observation": tuple(item.observation_id for item in self.package_observations),
            "output_witness": tuple(item.witness_id for item in self.output_witnesses),
            "child_census": tuple(item.census_id for item in self.child_censuses),
            "child_expectation": tuple(item.expectation_id for item in self.child_expectations),
            "child_reconciliation": tuple(
                item.reconciliation_id for item in self.child_reconciliations
            ),
        }
        for row in self.record_sets:
            if object_ids[row.record_kind] != row.observed_record_ids:
                raise ValueError(
                    f"{row.record_kind} typed objects do not match observed record IDs"
                )

        observation_refs = (
            *(
                (observation.evidence_epoch_id, observation.run_id)
                for observation in self.generator_observations
            ),
            *(
                (observation.evidence_epoch_id, observation.run_id)
                for observation in self.compile_observations
            ),
            *(
                (observation.evidence_epoch_id, observation.run_id)
                for observation in self.package_observations
            ),
        )
        for observation_epoch_id, observation_run_id in observation_refs:
            if observation_epoch_id != self.evidence_epoch_id:
                raise ValueError("observation evidence epoch cross-link is inconsistent")
            if observation_run_id not in self.admitted_run_ids:
                raise ValueError(
                    f"observation run {observation_run_id!r} is not in admitted_run_ids"
                )
        for witness in self.output_witnesses:
            if witness.evidence_epoch_id != self.evidence_epoch_id:
                raise ValueError("output witness evidence epoch cross-link is inconsistent")

        observation_index: dict[str, tuple[str, str, str]] = {}

        def add_observation(
            observation_id: str,
            observation_kind: str,
            unit_id: str,
            unit_identity_hash: str,
        ) -> None:
            if observation_id in observation_index:
                raise ValueError("observation IDs conflict across typed observation sets")
            observation_index[observation_id] = (
                observation_kind,
                unit_id,
                unit_identity_hash,
            )

        for generator_observation in self.generator_observations:
            add_observation(
                generator_observation.observation_id,
                "generator",
                generator_observation.generator_unit_id,
                generator_observation.generator_unit_identity_hash,
            )
        for compile_observation in self.compile_observations:
            add_observation(
                compile_observation.observation_id,
                "compile",
                compile_observation.compile_unit_id,
                compile_observation.compile_unit_identity_hash,
            )
        for package_observation in self.package_observations:
            add_observation(
                package_observation.observation_id,
                "package",
                package_observation.package_unit_id,
                package_observation.package_unit_identity_hash,
            )

        compile_observations = {
            observation.observation_id: observation for observation in self.compile_observations
        }
        for witness in self.output_witnesses:
            producer = observation_index.get(witness.producer_observation_id)
            if producer is None:
                raise ValueError("output witness references an orphan producer observation")
            producer_kind, producer_unit_id, producer_identity_hash = producer
            if witness.producer_observation_kind != producer_kind:
                raise ValueError("output witness producer observation kind is inconsistent")
            if (
                witness.producer_unit_id != producer_unit_id
                or witness.producer_unit_identity_hash != producer_identity_hash
            ):
                raise ValueError("output witness producer unit identity is inconsistent")

            if producer_kind == "compile":
                compile_observation = compile_observations[witness.producer_observation_id]
                observed_roots = tuple(
                    (root.root_id, root.path, root.verification_mode)
                    for root in compile_observation.observed_output_roots
                )
                witnessed_roots = tuple(
                    (root.root_id, root.path, root.verification_mode) for root in witness.roots
                )
                if witnessed_roots != observed_roots:
                    raise ValueError(
                        "compile witness root ID/path/verification_mode differs from observation"
                    )

        child_epoch_ids = (
            *(child.evidence_epoch_id for child in self.child_censuses),
            *(child.evidence_epoch_id for child in self.child_expectations),
            *(child.evidence_epoch_id for child in self.child_reconciliations),
        )
        for child_epoch_id in child_epoch_ids:
            if child_epoch_id != self.evidence_epoch_id:
                raise ValueError("child evidence epoch cross-link is inconsistent")

        censuses = {item.census_id: item for item in self.child_censuses}
        expectations = {item.expectation_id: item for item in self.child_expectations}
        referenced_censuses: set[str] = set()
        referenced_expectations: set[str] = set()
        for reconciliation in self.child_reconciliations:
            expectation = expectations.get(reconciliation.expectation_id)
            if expectation is None:
                raise ValueError("child reconciliation expectation cross-link is missing")
            census = censuses.get(reconciliation.source_census_id)
            if census is None:
                raise ValueError("child reconciliation census cross-link is missing")
            if expectation.census_id != census.census_id:
                raise ValueError("child expectation census cross-link is inconsistent")
            if (
                reconciliation.goal_scope_id != expectation.scope_id
                or reconciliation.build_model_id != expectation.model_id
            ):
                raise ValueError("child reconciliation expectation cross-links are inconsistent")
            referenced_censuses.add(census.census_id)
            referenced_expectations.add(expectation.expectation_id)
        if referenced_censuses != set(censuses) or referenced_expectations != set(expectations):
            raise ValueError("child census/expectation/reconciliation sets are not bijective")

        projection = self.model_dump(mode="python", exclude={"snapshot_id", "conflicts"})
        _require_primary_id(
            actual=self.snapshot_id,
            kind="build-evidence-set-v1",
            projection=projection,
            field="snapshot_id",
        )
        return self


BuildEvidenceBody = (
    ContainerEvidenceEpochV1
    | BuildGoalScopeV1
    | JvmSourceCensusV1
    | EvaluatedBuildModelV1
    | JvmBuildExpectationV1
    | CurrentPhysicalBasisSnapshotV1
    | FinalBuildBasisV1
    | GeneratorUnitObservationV1
    | CompileUnitObservationV1
    | PackageUnitObservationV1
    | PhysicalOutputWitnessV1
    | BuildEvidenceSetSnapshotV1
    | BuildReconciliationV1
)

BuildEvidenceRecordKind = Literal[
    "container_evidence_epoch",
    "build_goal_scope",
    "jvm_source_census",
    "evaluated_build_model",
    "jvm_build_expectation",
    "current_physical_basis",
    "final_build_basis",
    "generator_observation",
    "compile_observation",
    "package_observation",
    "output_witness",
    "build_evidence_set",
    "build_reconciliation",
]

_BUILD_EVIDENCE_BODY_TYPES: dict[str, type[StrictBuildEvidenceModel]] = {
    "container_evidence_epoch": ContainerEvidenceEpochV1,
    "build_goal_scope": BuildGoalScopeV1,
    "jvm_source_census": JvmSourceCensusV1,
    "evaluated_build_model": EvaluatedBuildModelV1,
    "jvm_build_expectation": JvmBuildExpectationV1,
    "current_physical_basis": CurrentPhysicalBasisSnapshotV1,
    "final_build_basis": FinalBuildBasisV1,
    "generator_observation": GeneratorUnitObservationV1,
    "compile_observation": CompileUnitObservationV1,
    "package_observation": PackageUnitObservationV1,
    "output_witness": PhysicalOutputWitnessV1,
    "build_evidence_set": BuildEvidenceSetSnapshotV1,
    "build_reconciliation": BuildReconciliationV1,
}

_BUILD_EVIDENCE_PRIMARY_ID_FIELDS = {
    "container_evidence_epoch": "evidence_epoch_id",
    "build_goal_scope": "scope_id",
    "jvm_source_census": "census_id",
    "evaluated_build_model": "model_id",
    "jvm_build_expectation": "expectation_id",
    "current_physical_basis": "physical_basis_snapshot_id",
    "final_build_basis": "basis_id",
    "generator_observation": "observation_id",
    "compile_observation": "observation_id",
    "package_observation": "observation_id",
    "output_witness": "witness_id",
    "build_evidence_set": "snapshot_id",
    "build_reconciliation": "reconciliation_id",
}

_BUILD_EVIDENCE_MUTABLE_REVISIONS = {
    "build_goal_scope": "scope_revision",
    "jvm_source_census": "census_revision",
    "evaluated_build_model": "model_revision",
    "final_build_basis": "basis_revision",
}


class SealedBuildEvidenceV1(StrictBuildEvidenceModel):
    record_kind: BuildEvidenceRecordKind
    record_id: CanonicalIdentifier
    schema_version: Literal[1]
    evidence_epoch_id: CanonicalIdentifier
    content_sha256: Sha256
    byte_count: NonnegativeInt
    producer_role: BoundedText
    created_at: BoundedText
    logical_artifact_id: CanonicalIdentifier | None
    revision: PositiveInt | None
    predecessor_content_sha256: Sha256 | None
    body: BuildEvidenceBody

    @classmethod
    def seal(
        cls,
        *,
        record_kind: BuildEvidenceRecordKind,
        body: BuildEvidenceBody,
        evidence_epoch_id: str,
        producer_role: str,
        created_at: str,
        record_id: str | None = None,
        logical_artifact_id: str | None = None,
        revision: int | None = None,
        predecessor_content_sha256: str | None = None,
    ) -> SealedBuildEvidenceV1:
        primary_id = _build_evidence_body_primary_id(record_kind, body)
        canonical_body = canonical_build_evidence_bytes(body)
        return cls(
            record_kind=record_kind,
            record_id=record_id if record_id is not None else primary_id,
            schema_version=1,
            evidence_epoch_id=evidence_epoch_id,
            content_sha256=hashlib.sha256(canonical_body).hexdigest(),
            byte_count=len(canonical_body),
            producer_role=producer_role,
            created_at=created_at,
            logical_artifact_id=logical_artifact_id,
            revision=revision,
            predecessor_content_sha256=predecessor_content_sha256,
            body=body,
        )

    @model_validator(mode="after")
    def _validate_seal(self) -> SealedBuildEvidenceV1:
        expected_type = _BUILD_EVIDENCE_BODY_TYPES[self.record_kind]
        if type(self.body) is not expected_type:
            raise ValueError("record_kind does not match the sealed body type")
        if self.body.evidence_epoch_id != self.evidence_epoch_id:
            raise ValueError("envelope evidence_epoch_id does not match body epoch")

        primary_id = _build_evidence_body_primary_id(self.record_kind, self.body)
        if self.record_id != primary_id:
            raise ValueError("record_id does not match the body's exact primary identity")

        canonical_body = canonical_build_evidence_bytes(self.body)
        expected_sha256 = hashlib.sha256(canonical_body).hexdigest()
        if self.content_sha256 != expected_sha256:
            raise ValueError("content_sha256 does not seal the current body bytes")
        if self.byte_count != len(canonical_body):
            raise ValueError("byte_count does not match the current body bytes")

        revision_field = _BUILD_EVIDENCE_MUTABLE_REVISIONS.get(self.record_kind)
        mutable_values = (
            self.logical_artifact_id,
            self.revision,
            self.predecessor_content_sha256,
        )
        if revision_field is None:
            if any(item is not None for item in mutable_values):
                raise ValueError(
                    "immutable evidence cannot carry logical artifact or revision metadata"
                )
            return self

        if any(item is None for item in mutable_values):
            raise ValueError("mutable evidence requires logical ID, revision, and predecessor")
        if self.logical_artifact_id != self.record_id:
            raise ValueError("logical_artifact_id must equal record_id for a mutable head")
        body_revision = getattr(self.body, revision_field)
        if self.revision != body_revision:
            raise ValueError("envelope revision does not match the mutable body revision")
        if self.record_kind == "build_goal_scope":
            if not isinstance(self.body, BuildGoalScopeV1):
                raise ValueError("build goal scope envelope has the wrong body type")
            scope_predecessor = self.body.predecessor_scope_hash
            expected_scope_predecessor = (
                None if self.revision == 1 else self.predecessor_content_sha256
            )
            if scope_predecessor != expected_scope_predecessor:
                raise ValueError("scope predecessor hash does not match the envelope predecessor")
        if self.revision == 1:
            if self.predecessor_content_sha256 != BUILD_EVIDENCE_GENESIS_SHA256:
                raise ValueError("revision 1 requires the genesis predecessor")
        elif self.predecessor_content_sha256 == BUILD_EVIDENCE_GENESIS_SHA256:
            raise ValueError("later revision cannot use the genesis predecessor")
        return self


def _build_evidence_body_primary_id(
    record_kind: str,
    body: StrictBuildEvidenceModel,
) -> str:
    expected_type = _BUILD_EVIDENCE_BODY_TYPES.get(record_kind)
    if expected_type is None:
        raise ValueError(f"unknown build evidence record kind {record_kind!r}")
    if type(body) is not expected_type:
        raise ValueError("record_kind does not match the sealed body type")
    field = _BUILD_EVIDENCE_PRIMARY_ID_FIELDS[record_kind]
    return validate_identifier(getattr(body, field), field=f"{record_kind} primary ID")


def _require_acyclic_graph(graph: Mapping[str, set[str]], *, field: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"{field} graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, set()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


__all__ = [
    "ActiveLanguageUnitCounts",
    "ActualInvocationScopeV1",
    "BUILD_EVIDENCE_MAX_CANONICAL_BYTES",
    "BUILD_EVIDENCE_MAX_ENTRIES",
    "BUILD_EVIDENCE_MAX_IDENTIFIER_CHARS",
    "BUILD_EVIDENCE_MAX_JSON_DEPTH",
    "BUILD_EVIDENCE_MAX_PATH_CHARS",
    "BUILD_EVIDENCE_MAX_RAW_BYTES",
    "BUILD_EVIDENCE_MAX_TEXT_CHARS",
    "BUILD_EVIDENCE_MAX_TOTAL_VALUES",
    "BUILD_EVIDENCE_GENESIS_SHA256",
    "BUILD_EVIDENCE_SCHEMA_VERSION",
    "BuildEvidenceBody",
    "BuildEvidenceRecordKind",
    "BuildEvidenceSetSnapshotV1",
    "BuildGoalScopeV1",
    "BuildPathBasis",
    "BuildReconciliationV1",
    "ChildScopeCounts",
    "ChildScopeExpectation",
    "ClassificationBasisV1",
    "CompileDependencyEdge",
    "CompileOutputDigestEntry",
    "CompileUnitCounts",
    "CompileUnitExpectation",
    "CompileUnitObservationV1",
    "CompilerEnvironmentSpec",
    "ContainerEvidenceEpochV1",
    "CurrentOutputEntryV1",
    "CurrentOutputRootV1",
    "CurrentPhysicalBasisSnapshotV1",
    "CurrentPhysicalMemberV1",
    "DelegationEdge",
    "EvidenceRecordDigestV1",
    "EvidenceRecordKind",
    "EvidenceRecordSetCompletenessV1",
    "EvaluatedBuildModelV1",
    "EvaluatedModuleV1",
    "FinalBuildBasisV1",
    "FinalCompileUnitBasis",
    "FinalGeneratorUnitBasis",
    "FinalPackageUnitBasis",
    "GeneratedRootContract",
    "GeneratorEnvironmentSpec",
    "GeneratorUnitCounts",
    "GeneratorUnitExpectation",
    "GeneratorUnitObservationV1",
    "JavaEdgeCounts",
    "JavaSourceCounts",
    "JvmBuildExpectationV1",
    "JvmSourceCandidate",
    "JvmSourceCensusV1",
    "MissingPhysicalMemberV1",
    "ModuleCounts",
    "OutputRootSpec",
    "OutputRootWitness",
    "PackageUnitCounts",
    "PackageUnitExpectation",
    "PackageUnitObservationV1",
    "PackagingEnvironmentSpec",
    "PhysicalMemberRole",
    "PhysicalOutputCounts",
    "PhysicalOutputEntry",
    "PhysicalOutputWitnessV1",
    "RequestedArtifactSpec",
    "RequiredSourceEdge",
    "ResolvedDigestEntry",
    "ResolvedMemberSpec",
    "SealedBuildEvidenceV1",
    "SelectedChildReconciliation",
    "SelectedCompileProof",
    "SelectedGeneratorProof",
    "SelectedPackageProof",
    "SourceCensusCounts",
    "SourceClassification",
    "SourceDigestEntry",
    "SourcePatternRuleV1",
    "StrictBuildEvidenceModel",
    "UnreadablePhysicalMemberV1",
    "canonical_build_evidence_bytes",
    "canonical_build_evidence_sha256",
    "canonical_content_id",
    "compile_unit_identity_hash",
    "generator_unit_identity_hash",
    "package_unit_identity_hash",
    "physical_output_aggregate_sha256",
    "validate_absolute_path",
    "validate_bounded_sequence",
    "validate_bounded_text",
    "validate_build_evidence_json",
    "validate_identifier",
    "validate_nonnegative_int",
    "validate_relative_path",
    "validate_sha256",
    "validate_unique_canonical_sequence",
]
