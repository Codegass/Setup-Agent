"""Framed readers for append-only JSON evidence directories.

Evidence stays on disk as one ordinary JSON object per atomic ``*.json``
file.  Framing exists only on the command-output transport: each file is
base64 encoded with its exact byte length and SHA-256, followed by a terminal
record count.  This keeps pretty-printed JSON and embedded newlines inside one
file boundary and makes a clipped multi-file response distinguishable from a
complete prefix.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Literal, Mapping

from sag.runtime.container_io import resolve_control_execute

JSON_RECORD_FRAME_PREFIX = "SAG_JSON_RECORD_V1"
JSON_RECORD_END_PREFIX = "SAG_JSON_RECORD_END_V1"
NAMED_JSON_RECORD_FRAME_PREFIX = "SAG_NAMED_JSON_RECORD_V1"
NAMED_JSON_RECORD_END_PREFIX = "SAG_NAMED_JSON_RECORD_END_V1"
NAMED_JSON_RECORD_MAX_BYTES = 64 * 1024 * 1024
NAMED_JSON_RECORD_MAX_COUNT = 10_000
NAMED_JSON_RECORD_MAX_STREAM_BYTES = 128 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_JSON_BASENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,249}\.json")


@dataclass(frozen=True)
class JsonRecordStreamRead:
    """Decoded records plus the integrity state of their transport.

    ``complete`` describes the outer transport.  A complete stream can still
    carry a malformed source file; in that case valid neighbours remain in
    ``records`` for historically forgiving consumers and ``conflict`` is
    ``record_malformed`` for strict consumers.
    """

    records: tuple[Dict[str, Any], ...] = ()
    complete: bool = False
    conflict: str | None = None


@dataclass(frozen=True)
class NamedJsonRecord:
    """One exact source file carried through the framed transport."""

    filename: str
    raw: bytes
    raw_sha256: str
    byte_count: int
    payload: Dict[str, Any]

    @property
    def record_id(self) -> str:
        """Identity carried by the already-validated atomic basename."""

        return self.filename[:-5]


@dataclass(frozen=True)
class NamedJsonRecordStreamRead:
    """Named records plus the integrity state of their complete transport."""

    records: tuple[NamedJsonRecord, ...] = ()
    complete: bool = False
    conflict: str | None = None


@dataclass(frozen=True)
class EvidencePublicationBinding:
    """Optional linkage a host publication seals beside exact record bytes."""

    run_id: str | None = None
    contract_id: str | None = None
    contract_hash: str | None = None
    logical_artifact_id: str | None = None


@dataclass(frozen=True)
class PublishedNamedJsonRecord:
    """One semantically validated, host-authorized live evidence record."""

    source: NamedJsonRecord
    payload: Dict[str, Any]


@dataclass(frozen=True)
class PublishedNamedJsonRecordStreamRead:
    """All-or-nothing result for a strict live evidence directory read."""

    records: tuple[PublishedNamedJsonRecord, ...] = ()
    complete: bool = False
    conflict: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class PublishedJsonObjectRead:
    """One fixed-path mutable JSON artifact at a host-authorized revision."""

    payload: Dict[str, Any] | None = None
    raw: bytes | None = None
    complete: bool = False
    conflict: str | None = None
    detail: str = ""


LiveRecordScope = Literal["current", "foreign", "forensic"]


def json_record_stream_command(directory: str) -> str:
    """Return one bounded read whose output preserves every file boundary.

    The source files are never rewritten.  ``sha256sum`` and ``wc`` read the
    same immutable atomic file that ``base64`` transports.  The terminal count
    is required even for an empty directory so losing a suffix cannot look
    like a successful shorter stream.  File, count, aggregate-raw and projected
    encoded-output limits are all checked before the corresponding body is
    encoded or emitted.
    """

    quoted = shlex.quote(directory)
    frame_prefix_bytes = len(JSON_RECORD_FRAME_PREFIX.encode("ascii"))
    end_prefix_bytes = len(JSON_RECORD_END_PREFIX.encode("ascii"))
    return (
        "count=0; raw_bytes=0; output_bytes=0; "
        f"for file in {quoted}/*.json; do "
        '[ -f "$file" ] || continue; '
        'bytes=$(wc -c < "$file") || exit 70; '
        '[ "$bytes" -ge 0 ] 2>/dev/null || exit 70; bytes=$((bytes + 0)); '
        f'[ "$bytes" -le {NAMED_JSON_RECORD_MAX_BYTES} ] || exit 70; '
        "next_count=$((count + 1)); "
        f'[ "$next_count" -le {NAMED_JSON_RECORD_MAX_COUNT} ] || exit 70; '
        "next_raw_bytes=$((raw_bytes + bytes)); "
        f'[ "$next_raw_bytes" -le {NAMED_JSON_RECORD_MAX_STREAM_BYTES} ] || exit 70; '
        "encoded_bytes=$(((bytes + 2) / 3 * 4)); "
        f"frame_bytes=$(({frame_prefix_bytes} + 1 + ${{#bytes}} + 1 + 64 + 1 + "
        "encoded_bytes + 1)); "
        f"footer_bytes=$(({end_prefix_bytes} + 1 + ${{#next_count}} + 1)); "
        "next_output_bytes=$((output_bytes + frame_bytes + footer_bytes)); "
        f'[ "$next_output_bytes" -le {NAMED_JSON_RECORD_MAX_STREAM_BYTES} ] || exit 70; '
        'digest=$(sha256sum < "$file") || exit 70; digest=${digest%% *}; '
        'payload=$(base64 < "$file") || exit 70; '
        "payload=$(printf '%s' \"$payload\" | tr -d '\\r\\n') || exit 70; "
        '[ "${#payload}" -eq "$encoded_bytes" ] || exit 70; '
        f"printf '{JSON_RECORD_FRAME_PREFIX}\\t%s\\t%s\\t%s\\n' "
        '"$bytes" "$digest" "$payload" || exit 70; '
        "count=$next_count; raw_bytes=$next_raw_bytes; "
        "output_bytes=$((output_bytes + frame_bytes)); "
        "done; "
        f"printf '{JSON_RECORD_END_PREFIX}\\t%s\\n' \"$count\" || exit 70"
    )


def named_json_record_stream_command(directory: str) -> str:
    """Return a framed read that also authenticates each source basename.

    Live receipt consumers must bind the payload ``receipt_id`` to the atomic
    filename that selected it.  The historical V1 transport intentionally
    omitted that name, so this separate transport keeps existing forgiving
    consumers stable while giving live readers an exact named boundary.  Its
    body is subject to the same pre-encoding directory limits as the legacy
    transport, with the encoded basename included in the output projection.
    """

    quoted = shlex.quote(directory)
    frame_prefix_bytes = len(NAMED_JSON_RECORD_FRAME_PREFIX.encode("ascii"))
    end_prefix_bytes = len(NAMED_JSON_RECORD_END_PREFIX.encode("ascii"))
    return (
        "count=0; raw_bytes=0; output_bytes=0; "
        f"for file in {quoted}/*.json; do "
        '[ -f "$file" ] || continue; '
        "name=${file##*/}; "
        "name_bytes=$(printf '%s' \"$name\" | wc -c) || exit 70; "
        '[ "$name_bytes" -ge 0 ] 2>/dev/null || exit 70; '
        "name_bytes=$((name_bytes + 0)); "
        'bytes=$(wc -c < "$file") || exit 70; '
        '[ "$bytes" -ge 0 ] 2>/dev/null || exit 70; bytes=$((bytes + 0)); '
        f'[ "$bytes" -le {NAMED_JSON_RECORD_MAX_BYTES} ] || exit 70; '
        "next_count=$((count + 1)); "
        f'[ "$next_count" -le {NAMED_JSON_RECORD_MAX_COUNT} ] || exit 70; '
        "next_raw_bytes=$((raw_bytes + bytes)); "
        f'[ "$next_raw_bytes" -le {NAMED_JSON_RECORD_MAX_STREAM_BYTES} ] || exit 70; '
        "name_encoded_bytes=$(((name_bytes + 2) / 3 * 4)); "
        "encoded_bytes=$(((bytes + 2) / 3 * 4)); "
        f"frame_bytes=$(({frame_prefix_bytes} + 1 + name_encoded_bytes + 1 + "
        "${#bytes} + 1 + 64 + 1 + encoded_bytes + 1)); "
        f"footer_bytes=$(({end_prefix_bytes} + 1 + ${{#next_count}} + 1)); "
        "next_output_bytes=$((output_bytes + frame_bytes + footer_bytes)); "
        f'[ "$next_output_bytes" -le {NAMED_JSON_RECORD_MAX_STREAM_BYTES} ] || exit 70; '
        "name_payload=$(printf '%s' \"$name\" | base64 | tr -d '\\r\\n') || exit 70; "
        '[ "${#name_payload}" -eq "$name_encoded_bytes" ] || exit 70; '
        'digest=$(sha256sum < "$file") || exit 70; digest=${digest%% *}; '
        'payload=$(base64 < "$file") || exit 70; '
        "payload=$(printf '%s' \"$payload\" | tr -d '\\r\\n') || exit 70; "
        '[ "${#payload}" -eq "$encoded_bytes" ] || exit 70; '
        f"printf '{NAMED_JSON_RECORD_FRAME_PREFIX}\\t%s\\t%s\\t%s\\t%s\\n' "
        '"$name_payload" "$bytes" "$digest" "$payload" || exit 70; '
        "count=$next_count; raw_bytes=$next_raw_bytes; "
        "output_bytes=$((output_bytes + frame_bytes)); "
        "done; "
        f"printf '{NAMED_JSON_RECORD_END_PREFIX}\\t%s\\n' \"$count\" || exit 70"
    )


def named_json_file_stream_command(path: str) -> str:
    """Return the same exact named transport for one fixed JSON path."""

    quoted = shlex.quote(path)
    return (
        f"file={quoted}; count=0; "
        'if test -f "$file"; then '
        "name=${file##*/}; "
        "name_payload=$(printf '%s' \"$name\" | base64 | tr -d '\\r\\n') || exit 70; "
        'bytes=$(wc -c < "$file") || exit 70; '
        f'[ "$bytes" -le {NAMED_JSON_RECORD_MAX_BYTES} ] || exit 70; '
        'digest=$(sha256sum < "$file") || exit 70; digest=${digest%% *}; '
        'payload=$(base64 < "$file") || exit 70; '
        "payload=$(printf '%s' \"$payload\" | tr -d '\\r\\n') || exit 70; "
        f"printf '{NAMED_JSON_RECORD_FRAME_PREFIX}\\t%s\\t%s\\t%s\\t%s\\n' "
        '"$name_payload" "$bytes" "$digest" "$payload" || exit 70; '
        "count=1; fi; "
        f"printf '{NAMED_JSON_RECORD_END_PREFIX}\\t%s\\n' \"$count\""
    )


def json_record_stream_succeeded(result: Any) -> bool:
    """Whether the executor says the command itself completed.

    This deliberately does not parse framing: a few callers use the helper for
    other read-only commands.  Evidence consumers must additionally call
    :func:`decode_json_record_stream`.
    """

    if not isinstance(result, Mapping):
        return False
    if result.get("success") is False or result.get("dispatch_status"):
        return False
    exit_code = result.get("exit_code")
    return exit_code is None or exit_code == 0


def execute_json_record_stream(source: Any, directory: str, *, timeout: int = 120) -> Any:
    """Read a complete record directory without executor output truncation."""

    execute = resolve_control_execute(source)
    if execute is None:
        raise TypeError("record stream source is not executable")
    command = json_record_stream_command(directory)
    try:
        return execute(
            command,
            workdir=None,
            timeout=timeout,
            truncate_output=False,
        )
    except TypeError as exc:
        # Lightweight unit doubles predate the executor's transport controls.
        detail = str(exc)
        if not any(name in detail for name in ("truncate_output", "workdir", "timeout")):
            raise
        return execute(command)


def execute_named_json_record_stream(source: Any, directory: str, *, timeout: int = 120) -> Any:
    """Read a complete named-record directory without output truncation."""

    execute = resolve_control_execute(source)
    if execute is None:
        raise TypeError("named record stream source is not executable")
    command = named_json_record_stream_command(directory)
    try:
        return execute(
            command,
            workdir=None,
            timeout=timeout,
            truncate_output=False,
        )
    except TypeError as exc:
        detail = str(exc)
        if not any(name in detail for name in ("truncate_output", "workdir", "timeout")):
            raise
        return execute(command)


def execute_named_json_file_stream(source: Any, path: str, *, timeout: int = 120) -> Any:
    """Read one fixed JSON file through the bounded exact named transport."""

    execute = resolve_control_execute(source)
    if execute is None:
        raise TypeError("named file stream source is not executable")
    command = named_json_file_stream_command(path)
    try:
        return execute(
            command,
            workdir=None,
            timeout=timeout,
            truncate_output=False,
        )
    except TypeError as exc:
        detail = str(exc)
        if not any(name in detail for name in ("truncate_output", "workdir", "timeout")):
            raise
        return execute(command)


def frame_json_record_stream(records: Iterable[bytes | str | Mapping[str, Any]]) -> str:
    """Build the production transport representation (primarily for fakes).

    A mapping is serialized compactly; ``str`` and ``bytes`` are transported
    byte-for-byte so tests can exercise pretty, duplicate-key and malformed
    source files without first parsing them.
    """

    lines: List[str] = []
    for record in records:
        if isinstance(record, Mapping):
            raw = json.dumps(
                dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        elif isinstance(record, str):
            raw = record.encode("utf-8")
        elif isinstance(record, bytes):
            raw = record
        else:
            raise TypeError("a framed JSON record must be bytes, str, or a mapping")
        encoded = base64.b64encode(raw).decode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        lines.append(f"{JSON_RECORD_FRAME_PREFIX}\t{len(raw)}\t{digest}\t{encoded}\n")
    lines.append(f"{JSON_RECORD_END_PREFIX}\t{len(lines)}\n")
    return "".join(lines)


def frame_named_json_record_stream(
    records: Iterable[tuple[str, bytes | str | Mapping[str, Any]]],
) -> str:
    """Build the named production transport representation for test doubles."""

    lines: List[str] = []
    for filename, record in records:
        if isinstance(record, Mapping):
            raw = json.dumps(
                dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        elif isinstance(record, str):
            raw = record.encode("utf-8")
        elif isinstance(record, bytes):
            raw = record
        else:
            raise TypeError("a framed named JSON record must be bytes, str, or a mapping")
        name_encoded = base64.b64encode(str(filename).encode("utf-8")).decode("ascii")
        encoded = base64.b64encode(raw).decode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        lines.append(
            f"{NAMED_JSON_RECORD_FRAME_PREFIX}\t{name_encoded}\t{len(raw)}\t"
            f"{digest}\t{encoded}\n"
        )
    lines.append(f"{NAMED_JSON_RECORD_END_PREFIX}\t{len(lines)}\n")
    return "".join(lines)


def _strict_json_object(raw: bytes) -> Dict[str, Any]:
    """Decode exactly one RFC-JSON object, rejecting duplicate keys."""

    def strict_object(pairs):
        payload: Dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate JSON key: {key}")
            payload[key] = value
        return payload

    def reject_constant(value: str):
        raise ValueError(f"non-JSON numeric constant: {value}")

    text = raw.decode("utf-8", errors="strict")
    payload = json.loads(
        text,
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("JSON evidence record must be an object")
    return payload


def _canonical_nonnegative_integer(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("frame length/count is not an integer")
    parsed = int(value)
    if str(parsed) != value:
        raise ValueError("frame length/count is not canonical")
    return parsed


def decode_json_record_stream(result: Any) -> JsonRecordStreamRead:
    """Strictly decode one complete framed command result.

    An outer-frame defect or absent/mismatched footer invalidates the entire
    transport and returns no prefix.  A valid frame whose decoded source file
    is not exactly one duplicate-free JSON object is a named record conflict;
    valid neighbouring objects remain available to explicitly forgiving
    consumers.
    """

    if not json_record_stream_succeeded(result):
        return JsonRecordStreamRead(conflict="stream_unreadable")
    output = result.get("output") if isinstance(result, Mapping) else None
    if not isinstance(output, str) or not output:
        return JsonRecordStreamRead(conflict="stream_incomplete")
    lines = output.splitlines(keepends=True)
    if not lines or "\r" in output or any(not line.endswith("\n") for line in lines[:-1]):
        return JsonRecordStreamRead(conflict="stream_incomplete")

    # DockerOrchestrator normalizes command output with ``.strip()`` even when
    # ``truncate_output=False``.  Therefore the complete final footer arrives
    # without its shell-emitted newline in production.  Only the final footer
    # may omit a delimiter; every preceding frame still requires one.
    footer_line = lines[-1]
    footer = footer_line[:-1] if footer_line.endswith("\n") else footer_line
    footer_fields = footer.split("\t")
    if len(footer_fields) != 2 or footer_fields[0] != JSON_RECORD_END_PREFIX:
        return JsonRecordStreamRead(conflict="stream_incomplete")
    try:
        expected_count = _canonical_nonnegative_integer(footer_fields[1])
    except ValueError:
        return JsonRecordStreamRead(conflict="stream_malformed")
    frame_lines = lines[:-1]
    if expected_count != len(frame_lines):
        return JsonRecordStreamRead(conflict="stream_incomplete")

    records: List[Dict[str, Any]] = []
    malformed_record = False
    for framed_line in frame_lines:
        fields = framed_line[:-1].split("\t")
        if len(fields) != 4 or fields[0] != JSON_RECORD_FRAME_PREFIX:
            return JsonRecordStreamRead(conflict="stream_malformed")
        _, length_text, digest, encoded = fields
        try:
            expected_length = _canonical_nonnegative_integer(length_text)
        except ValueError:
            return JsonRecordStreamRead(conflict="stream_malformed")
        if not _SHA256_RE.fullmatch(digest):
            return JsonRecordStreamRead(conflict="stream_malformed")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return JsonRecordStreamRead(conflict="stream_malformed")
        if len(raw) != expected_length or hashlib.sha256(raw).hexdigest() != digest:
            return JsonRecordStreamRead(conflict="stream_malformed")
        try:
            records.append(_strict_json_object(raw))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            malformed_record = True

    return JsonRecordStreamRead(
        records=tuple(records),
        complete=True,
        conflict="record_malformed" if malformed_record else None,
    )


def decode_named_json_record_stream(result: Any) -> NamedJsonRecordStreamRead:
    """Strictly decode a complete stream of filename-bound JSON records."""

    if not json_record_stream_succeeded(result):
        return NamedJsonRecordStreamRead(conflict="stream_unreadable")
    output = result.get("output") if isinstance(result, Mapping) else None
    if not isinstance(output, str) or not output:
        return NamedJsonRecordStreamRead(conflict="stream_incomplete")
    if len(output.encode("utf-8")) > NAMED_JSON_RECORD_MAX_STREAM_BYTES:
        return NamedJsonRecordStreamRead(conflict="stream_oversized")
    lines = output.splitlines(keepends=True)
    if not lines or "\r" in output or any(not line.endswith("\n") for line in lines[:-1]):
        return NamedJsonRecordStreamRead(conflict="stream_incomplete")

    footer_line = lines[-1]
    footer = footer_line[:-1] if footer_line.endswith("\n") else footer_line
    footer_fields = footer.split("\t")
    if len(footer_fields) != 2 or footer_fields[0] != NAMED_JSON_RECORD_END_PREFIX:
        return NamedJsonRecordStreamRead(conflict="stream_incomplete")
    try:
        expected_count = _canonical_nonnegative_integer(footer_fields[1])
    except ValueError:
        return NamedJsonRecordStreamRead(conflict="stream_malformed")
    frame_lines = lines[:-1]
    if expected_count != len(frame_lines) or expected_count > NAMED_JSON_RECORD_MAX_COUNT:
        return NamedJsonRecordStreamRead(conflict="stream_incomplete")

    records: List[NamedJsonRecord] = []
    filenames: set[str] = set()
    malformed_record = False
    for framed_line in frame_lines:
        fields = framed_line[:-1].split("\t")
        if len(fields) != 5 or fields[0] != NAMED_JSON_RECORD_FRAME_PREFIX:
            return NamedJsonRecordStreamRead(conflict="stream_malformed")
        _, encoded_name, length_text, digest, encoded = fields
        try:
            name_raw = base64.b64decode(encoded_name, validate=True)
            filename = name_raw.decode("utf-8", errors="strict")
            expected_length = _canonical_nonnegative_integer(length_text)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return NamedJsonRecordStreamRead(conflict="stream_malformed")
        if (
            base64.b64encode(name_raw).decode("ascii") != encoded_name
            or _SAFE_JSON_BASENAME_RE.fullmatch(filename) is None
            or filename in filenames
            or expected_length > NAMED_JSON_RECORD_MAX_BYTES
            or _SHA256_RE.fullmatch(digest) is None
        ):
            return NamedJsonRecordStreamRead(conflict="stream_malformed")
        filenames.add(filename)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return NamedJsonRecordStreamRead(conflict="stream_malformed")
        if len(raw) != expected_length or hashlib.sha256(raw).hexdigest() != digest:
            return NamedJsonRecordStreamRead(conflict="stream_malformed")
        try:
            payload = _strict_json_object(raw)
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            malformed_record = True
            continue
        records.append(
            NamedJsonRecord(
                filename=filename,
                raw=raw,
                raw_sha256=digest,
                byte_count=expected_length,
                payload=payload,
            )
        )

    return NamedJsonRecordStreamRead(
        records=tuple(records),
        complete=True,
        conflict="record_malformed" if malformed_record else None,
    )


def read_live_published_json_records(
    source: Any,
    directory: str,
    *,
    record_kind: Any,
    validator: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
    publication_binding: Callable[[Mapping[str, Any]], EvidencePublicationBinding] | None = None,
    record_scope: Callable[[Mapping[str, Any], str | None], LiveRecordScope] | None = None,
    require_complete_publication_set: bool = True,
    timeout: int = 120,
) -> PublishedNamedJsonRecordStreamRead:
    """Read one strict live ledger through its host publication authority.

    The ordering is a security property, not an implementation detail:

    1. preserve the complete atomic filename/raw-byte boundary;
    2. validate the record-kind-specific schema and bind its payload identity
       to that filename; and only then
    3. require the host-owned publication stream to authorize those exact
       bytes and any contract tuple the writer sealed with them.

    One malformed, unrecognized, unpublished or tampered record invalidates
    the whole live ledger.  Historical/diagnostic callers that intentionally
    want a forgiving view continue to use :func:`read_json_records`.
    ``record_scope`` may skip only a schema that is mechanically known to be
    forensic or a fully validated foreign-run identity; an unknown/future
    shape must remain ``current`` so the strict validator rejects it.
    """

    try:
        result = execute_named_json_record_stream(source, directory, timeout=timeout)
    except Exception as exc:
        return PublishedNamedJsonRecordStreamRead(
            conflict="stream_unreadable",
            detail=str(exc),
        )
    decoded = decode_named_json_record_stream(result)
    if not decoded.complete or decoded.conflict is not None:
        return PublishedNamedJsonRecordStreamRead(
            conflict=decoded.conflict or "stream_unreadable",
        )

    # Local import keeps the transport primitive dependency-light.  The
    # publication authority itself depends on strict control-event models,
    # which also consume evidence-backed action schemas.
    from .evidence_publications import (
        MUTABLE_RECORD_KINDS,
        MutablePublicationObservation,
        evidence_publication_authority_for,
    )

    authority = evidence_publication_authority_for(source)
    mutable = record_kind in MUTABLE_RECORD_KINDS
    published: List[PublishedNamedJsonRecord] = []
    current_record_ids: List[str] = []
    current_mutable_records: Dict[str, MutablePublicationObservation] = {}
    for record in decoded.records:
        try:
            scope = (
                record_scope(record.payload, getattr(authority, "run_id", None))
                if record_scope is not None
                else "current"
            )
            if scope not in {"current", "foreign", "forensic"}:
                raise ValueError("live evidence scope classifier returned an invalid value")
            if scope != "current":
                continue
            normalized = validator(record.payload, record.record_id)
            if not isinstance(normalized, Mapping):
                raise TypeError("live evidence validator did not return an object")
            exact_payload = dict(normalized)
            if exact_payload != record.payload:
                raise ValueError("live evidence record is not canonical")
            binding = (
                publication_binding(exact_payload)
                if publication_binding is not None
                else EvidencePublicationBinding()
            )
            if not isinstance(binding, EvidencePublicationBinding):
                raise TypeError("live evidence publication binding has an invalid type")
            logical_artifact_id = binding.logical_artifact_id
            if mutable and not logical_artifact_id:
                raise ValueError("mutable live evidence requires one logical artifact id")
            if not mutable and logical_artifact_id is not None:
                raise ValueError("immutable live evidence cannot carry a logical artifact id")
        except (KeyError, TypeError, ValueError) as exc:
            return PublishedNamedJsonRecordStreamRead(
                conflict="record_schema_invalid",
                detail=f"{record.filename}: {exc}",
            )

        if not mutable:
            check = authority.verify_bytes(
                record_kind=record_kind,
                record_id=record.record_id,
                raw=record.raw,
                run_id=binding.run_id,
                contract_id=binding.contract_id,
                contract_hash=binding.contract_hash,
            )
            if not check.authorized:
                detail = check.detail or check.status
                return PublishedNamedJsonRecordStreamRead(
                    conflict=f"publication_{check.status}",
                    detail=f"{record.filename}: {detail}",
                )
        published.append(PublishedNamedJsonRecord(source=record, payload=exact_payload))
        current_record_ids.append(record.record_id)
        if mutable:
            assert logical_artifact_id is not None
            observed_run_id = binding.run_id or str(getattr(authority, "run_id", "") or "")
            current_mutable_records[record.record_id] = MutablePublicationObservation(
                logical_artifact_id=logical_artifact_id,
                raw_sha256=record.raw_sha256,
                byte_count=record.byte_count,
                run_id=observed_run_id,
                contract_id=binding.contract_id,
                contract_hash=binding.contract_hash,
            )

    if mutable:
        # One authority lock covers every current head, exact byte digest,
        # contract tuple, tombstone and the complete ID set.  Per-record
        # verification followed by an ID-only check has a TOCTOU gap because
        # a stable logical ID can advance revision between those calls.
        set_check = authority.verify_latest_record_set(record_kind, current_mutable_records)
    elif require_complete_publication_set:
        set_check = authority.verify_immutable_record_id_set(record_kind, current_record_ids)
    else:
        set_check = None
    if set_check is not None:
        if not set_check.authorized:
            detail = set_check.detail or set_check.status
            return PublishedNamedJsonRecordStreamRead(
                conflict=f"publication_set_{set_check.status}",
                detail=detail,
            )

    return PublishedNamedJsonRecordStreamRead(records=tuple(published), complete=True)


def read_live_published_mutable_json_object(
    source: Any,
    path: str,
    *,
    record_kind: Any,
    record_id: str,
    validator: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
    publication_binding: Callable[[Mapping[str, Any]], EvidencePublicationBinding],
    record_scope: Callable[[Mapping[str, Any], str | None], LiveRecordScope] | None = None,
    timeout: int = 120,
) -> PublishedJsonObjectRead:
    """Read one fixed mutable JSON artifact at exactly one host head.

    A verified absence is meaningful: it is accepted only when the host has no
    present head for this record kind (including a current tombstone).  A file
    missing while the host expects a present head, or a stale file restored
    after a tombstone, fails the same atomic record-set check as a directory
    ledger.  The fixed ``record_id`` is the publication identity; it need not
    equal the physical basename (for example ``host-run-pin`` vs
    ``run-pin.json``).
    """

    try:
        result = execute_named_json_file_stream(source, path, timeout=timeout)
    except Exception as exc:
        return PublishedJsonObjectRead(conflict="stream_unreadable", detail=str(exc))
    decoded = decode_named_json_record_stream(result)
    if not decoded.complete or decoded.conflict is not None or len(decoded.records) > 1:
        return PublishedJsonObjectRead(
            conflict=decoded.conflict or "stream_unreadable",
        )

    from .evidence_publications import (
        MUTABLE_RECORD_KINDS,
        MutablePublicationObservation,
        evidence_publication_authority_for,
    )

    if record_kind not in MUTABLE_RECORD_KINDS:
        return PublishedJsonObjectRead(
            conflict="record_schema_invalid",
            detail="fixed live object reader requires a mutable record kind",
        )
    authority = evidence_publication_authority_for(source)
    payload: Dict[str, Any] | None = None
    raw: bytes | None = None
    observed: Dict[str, MutablePublicationObservation] = {}
    if decoded.records:
        record = decoded.records[0]
        try:
            scope = (
                record_scope(record.payload, getattr(authority, "run_id", None))
                if record_scope is not None
                else "current"
            )
            if scope not in {"current", "foreign", "forensic"}:
                raise ValueError("live evidence scope classifier returned an invalid value")
            if scope == "current":
                normalized = validator(record.payload, record_id)
                if not isinstance(normalized, Mapping):
                    raise TypeError("live evidence validator did not return an object")
                exact_payload = dict(normalized)
                if exact_payload != record.payload:
                    raise ValueError("live evidence record is not canonical")
                binding = publication_binding(exact_payload)
                if not isinstance(binding, EvidencePublicationBinding):
                    raise TypeError("live evidence publication binding has an invalid type")
                logical_artifact_id = binding.logical_artifact_id
                if not logical_artifact_id:
                    raise ValueError("mutable live evidence requires one logical artifact id")
                observed_run_id = binding.run_id or str(getattr(authority, "run_id", "") or "")
                observed[record_id] = MutablePublicationObservation(
                    logical_artifact_id=logical_artifact_id,
                    raw_sha256=record.raw_sha256,
                    byte_count=record.byte_count,
                    run_id=observed_run_id,
                    contract_id=binding.contract_id,
                    contract_hash=binding.contract_hash,
                )
                payload = exact_payload
                raw = record.raw
        except (KeyError, TypeError, ValueError) as exc:
            return PublishedJsonObjectRead(
                conflict="record_schema_invalid",
                detail=f"{record.filename}: {exc}",
            )

    check = authority.verify_latest_record_set(record_kind, observed)
    if not check.authorized:
        return PublishedJsonObjectRead(
            conflict=f"publication_set_{check.status}",
            detail=check.detail or check.status,
        )
    return PublishedJsonObjectRead(
        payload=payload,
        raw=raw,
        complete=True,
    )


def read_json_records(orchestrator: Any, directory: str) -> List[Dict[str, Any]]:
    """Read valid JSON objects, omitting malformed files only when complete.

    This is the historical forgiving API used for auxiliary views.  It never
    returns a partial transport and never promotes malformed/duplicate-key
    payloads.  Consumers for which one corrupt neighbour invalidates the whole
    ledger inspect :func:`decode_json_record_stream` directly.
    """

    try:
        result = execute_json_record_stream(orchestrator, directory)
    except Exception:
        return []
    decoded = decode_json_record_stream(result)
    if not decoded.complete:
        return []
    return [dict(record) for record in decoded.records]


__all__ = [
    "EvidencePublicationBinding",
    "JSON_RECORD_END_PREFIX",
    "JSON_RECORD_FRAME_PREFIX",
    "JsonRecordStreamRead",
    "LiveRecordScope",
    "NAMED_JSON_RECORD_END_PREFIX",
    "NAMED_JSON_RECORD_FRAME_PREFIX",
    "NAMED_JSON_RECORD_MAX_BYTES",
    "NAMED_JSON_RECORD_MAX_COUNT",
    "NAMED_JSON_RECORD_MAX_STREAM_BYTES",
    "NamedJsonRecord",
    "NamedJsonRecordStreamRead",
    "PublishedNamedJsonRecord",
    "PublishedNamedJsonRecordStreamRead",
    "PublishedJsonObjectRead",
    "decode_named_json_record_stream",
    "decode_json_record_stream",
    "execute_named_json_record_stream",
    "execute_named_json_file_stream",
    "execute_json_record_stream",
    "frame_named_json_record_stream",
    "frame_json_record_stream",
    "named_json_record_stream_command",
    "named_json_file_stream_command",
    "json_record_stream_command",
    "json_record_stream_succeeded",
    "read_json_records",
    "read_live_published_json_records",
    "read_live_published_mutable_json_object",
]
