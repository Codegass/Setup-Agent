"""Host-owned publication authority for container evidence records.

Project code and the model can write ``/workspace/.setup_agent``.  Therefore a
valid JSON file in that tree is not, by itself, live evidence.  This module
maintains the small host-side allowlist that says which *exact bytes* were
published by a trusted writer.  The durable source of that allowlist is the
session's host ``control_events.jsonl``; the container copy is only a mirror.

This module deliberately does not parse record semantics.  A consumer must
first apply the record-kind-specific strict schema, then require a successful
``verify_bytes`` check here.  Keeping those two decisions separate avoids a
second, weaker copy of every evidence schema in the publication layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, TypeAlias, cast

from .control_events import (
    CONTROL_EVENT_MAX_RAW_BYTES,
    EVIDENCE_MUTABLE_RECORD_KINDS,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    EVIDENCE_PUBLICATION_MAX_RECORD_BYTES,
    ControlEvent,
    ControlEventSink,
    EvidencePublicationPayload,
    EvidenceStoreBoundPayload,
    canonical_sha256,
)

EvidenceRecordKind: TypeAlias = Literal[
    "invocation_contract",
    "invocation_receipt",
    "receipt_assessment",
    "policy_claim",
    "job_obligation",
    "repair_context",
    "build_requirements",
    "run_pin",
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
PublicationCheckStatus: TypeAlias = Literal[
    "verified",
    "revoked",
    "unavailable",
    "not_published",
    "mismatch",
    "foreign_run",
]
PublicationAttemptStatus: TypeAlias = Literal[
    "published",
    "revoked",
    "publication_unavailable",
    "publication_conflict",
    "publication_invalid",
]

IMMUTABLE_RECORD_KINDS = frozenset(
    {
        "invocation_contract",
        "invocation_receipt",
        "receipt_assessment",
        "policy_claim",
        "repair_context",
        "stall_diagnostic",
        "stall_seal",
        "testcase_row_input",
    }
)
MUTABLE_RECORD_KINDS = frozenset(EVIDENCE_MUTABLE_RECORD_KINDS)
_ALL_RECORD_KINDS = IMMUTABLE_RECORD_KINDS | MUTABLE_RECORD_KINDS

EVIDENCE_PUBLICATION_EVENT_KIND = "evidence_publication"
EVIDENCE_STORE_BOUND_EVENT_KIND = "evidence_store_bound"
EVIDENCE_PUBLICATION_MAX_CONTROL_STREAM_BYTES = 128 * 1024 * 1024
BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID = "workspace-build-requirements"
DOCUMENT_MAP_LOGICAL_ARTIFACT_ID = "workspace-document-map"
ENV_OVERLAY_LOGICAL_ARTIFACT_ID = "runtime-env-overlay"
RUN_PIN_LOGICAL_ARTIFACT_ID = "host-run-pin"
VERDICT_LOGICAL_ARTIFACT_ID = "run-verdict"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_AUTHORITY_ATTRIBUTE = "_sag_evidence_publication_authority"


class EvidencePublicationError(RuntimeError):
    """Base class for fail-closed publication authority failures."""


class EvidencePublicationConflict(EvidencePublicationError):
    """One publication identity was committed to two different bodies."""


class EvidencePublicationRecoveryError(EvidencePublicationError):
    """The host control stream could not reconstruct one complete allowlist."""


class EvidencePublicationUnavailableError(EvidencePublicationError):
    """A caller attempted to publish without a writable host authority."""


class _HostControlSink(Protocol):
    path: Path

    def emit(
        self,
        kind: Any,
        payload: Mapping[str, Any],
        *,
        source: Any = None,
    ) -> Any: ...


@dataclass(frozen=True)
class PublicationCheck:
    """Typed verification result; only ``verified`` authorizes consumption."""

    status: PublicationCheckStatus
    publication: EvidencePublicationPayload | None = None
    detail: str = ""

    @property
    def authorized(self) -> bool:
        return self.status == "verified"

    def __bool__(self) -> bool:
        return self.authorized


@dataclass(frozen=True)
class PublicationAttempt:
    """Non-raising result used by evidence writers after container commit."""

    status: PublicationAttemptStatus
    publication: EvidencePublicationPayload | None = None
    detail: str = ""

    @property
    def published(self) -> bool:
        return self.status == "published"

    @property
    def committed(self) -> bool:
        return self.status in {"published", "revoked"}

    def __bool__(self) -> bool:
        return self.published


@dataclass(frozen=True)
class EvidencePublicationSnapshot:
    """Thread-safe current-run authority surface for strict ledger readers."""

    run_id: str
    immutable_record_ids: Mapping[EvidenceRecordKind, tuple[str, ...]]
    mutable_heads: Mapping[str, EvidencePublicationPayload]


@dataclass(frozen=True)
class MutablePublicationObservation:
    """One strict named-record observation for an atomic mutable-ledger check."""

    logical_artifact_id: str
    raw_sha256: str
    byte_count: int
    run_id: str
    contract_id: str | None = None
    contract_hash: str | None = None


@dataclass(frozen=True)
class UnavailableEvidencePublicationAuthority:
    """Explicit no-authority state used before/after host sink installation."""

    reason: str
    run_id: str | None = None
    available: bool = False
    writable: bool = False

    def publish_bytes(self, *args: Any, **kwargs: Any) -> EvidencePublicationPayload:
        del args, kwargs
        raise EvidencePublicationUnavailableError(self.reason)

    def verify_bytes(self, *args: Any, **kwargs: Any) -> PublicationCheck:
        del args, kwargs
        return PublicationCheck("unavailable", detail=self.reason)

    verify_latest_bytes = verify_bytes

    def publish_revision(self, *args: Any, **kwargs: Any) -> EvidencePublicationPayload:
        del args, kwargs
        raise EvidencePublicationUnavailableError(self.reason)

    def revoke_latest(self, *args: Any, **kwargs: Any) -> EvidencePublicationPayload:
        del args, kwargs
        raise EvidencePublicationUnavailableError(self.reason)

    def latest_head(self, *args: Any, **kwargs: Any) -> EvidencePublicationPayload | None:
        del args, kwargs
        return None

    def expected_immutable_record_ids(self, *args: Any, **kwargs: Any) -> frozenset[str]:
        del args, kwargs
        return frozenset()

    def verify_immutable_record_id_set(self, *args: Any, **kwargs: Any) -> PublicationCheck:
        del args, kwargs
        return PublicationCheck("unavailable", detail=self.reason)

    def expected_mutable_record_ids(self, *args: Any, **kwargs: Any) -> Mapping[str, str]:
        del args, kwargs
        return {}

    def verify_mutable_record_id_set(self, *args: Any, **kwargs: Any) -> PublicationCheck:
        del args, kwargs
        return PublicationCheck("unavailable", detail=self.reason)

    def verify_latest_record_set(self, *args: Any, **kwargs: Any) -> PublicationCheck:
        del args, kwargs
        return PublicationCheck("unavailable", detail=self.reason)

    def snapshot(self) -> EvidencePublicationSnapshot:
        return EvidencePublicationSnapshot(
            run_id=self.run_id or "unavailable",
            immutable_record_ids={},
            mutable_heads={},
        )


def _exact_record_bytes(raw: bytes) -> bytes:
    if type(raw) is not bytes:
        raise TypeError("evidence publication requires exact bytes")
    if not raw:
        raise ValueError("evidence publication cannot authorize an empty record")
    if len(raw) > EVIDENCE_PUBLICATION_MAX_RECORD_BYTES:
        raise ValueError("evidence publication record exceeds its raw byte limit")
    return raw


def _store_identity(source: Any) -> str | None:
    """Host-derived identity for the one container store bound to an epoch."""

    resolver = getattr(source, "evidence_store_identity", None)
    if callable(resolver):
        try:
            value = str(resolver() or "").strip()
        except Exception:
            # A real orchestrator may be installed before its container exists.
            # Leave the authority unbound until Docker exposes the immutable
            # id; never freeze a provisional object/name identity that would
            # change immediately after container creation.
            return None
        if value:
            return value
        return None
    value = str(getattr(source, "container_id", "") or "").strip()
    if value:
        return f"container_id:{value}"
    return f"object:{id(source)}"


def _immutable_publication_payload(
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    raw: bytes,
    run_id: str,
    contract_id: str | None,
    contract_hash: str | None,
) -> EvidencePublicationPayload:
    if record_kind not in IMMUTABLE_RECORD_KINDS:
        raise ValueError("mutable evidence kind requires publish_revision")
    exact = _exact_record_bytes(raw)
    values: dict[str, Any] = {
        "record_kind": record_kind,
        "record_id": record_id,
        "raw_sha256": hashlib.sha256(exact).hexdigest(),
        "byte_count": len(exact),
        "run_id": run_id,
    }
    if contract_id is not None or contract_hash is not None:
        values["contract_id"] = contract_id
        values["contract_hash"] = contract_hash
    return EvidencePublicationPayload.model_validate(values)


def _publication_state_sha256(publication: EvidencePublicationPayload) -> str:
    return canonical_sha256(publication.model_dump(mode="json", exclude_unset=True))


def _revision_payload(
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    logical_artifact_id: str,
    raw: bytes | None,
    run_id: str,
    revision: int,
    previous_raw_sha256: str,
    previous_publication_sha256: str,
    contract_id: str | None,
    contract_hash: str | None,
    revoked: bool = False,
) -> EvidencePublicationPayload:
    if record_kind not in MUTABLE_RECORD_KINDS:
        raise ValueError("immutable evidence kind cannot enter a revision chain")
    values: dict[str, Any] = {
        "record_kind": record_kind,
        "record_id": record_id,
        "run_id": run_id,
        "logical_artifact_id": logical_artifact_id,
        "revision": revision,
        "previous_raw_sha256": previous_raw_sha256,
        "previous_publication_sha256": previous_publication_sha256,
    }
    if revoked:
        values.update(
            {
                "raw_sha256": EVIDENCE_PUBLICATION_GENESIS_SHA256,
                "byte_count": 0,
                "publication_state": "revoked",
            }
        )
    else:
        exact = _exact_record_bytes(raw)  # type: ignore[arg-type]
        values.update(
            {
                "raw_sha256": hashlib.sha256(exact).hexdigest(),
                "byte_count": len(exact),
            }
        )
        if contract_id is not None or contract_hash is not None:
            values["contract_id"] = contract_id
            values["contract_hash"] = contract_hash
    return EvidencePublicationPayload.model_validate(values)


class EvidencePublicationAuthority:
    """Thread-safe allowlist backed by one host-owned control-event sink."""

    available = True

    def __init__(
        self,
        *,
        run_id: str,
        sink: _HostControlSink | None = None,
    ) -> None:
        if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("evidence publication run id is invalid")
        self.run_id = run_id
        self._sink = sink
        self._records: dict[tuple[EvidenceRecordKind, str], EvidencePublicationPayload] = {}
        self._heads: dict[str, EvidencePublicationPayload] = {}
        self._bound_store_identity: str | None = None
        self._lock = threading.RLock()

    @property
    def writable(self) -> bool:
        return self._sink is not None

    @property
    def publication_count(self) -> int:
        with self._lock:
            return len(self._records) + len(self._heads)

    def bind_store(self, orchestrator: Any) -> None:
        """Bind this evidence epoch to exactly one container-backed store."""

        identity = _store_identity(orchestrator)
        if identity is None:
            return
        with self._lock:
            if self._bound_store_identity is None:
                self._emit_store_binding(identity)
            elif self._bound_store_identity != identity:
                raise EvidencePublicationConflict(
                    "one evidence run cannot authorize more than one container store"
                )

    def _bind_contextual_store(self, orchestrator: Any) -> None:
        """Resolve a contextual fallback without masking a bound store."""

        identity = _store_identity(orchestrator)
        with self._lock:
            if identity is None:
                if self._bound_store_identity is None:
                    # Installation may precede container creation.  Publication
                    # remains unavailable until a durable identity can bind.
                    return
                raise EvidencePublicationConflict(
                    "evidence store has no immutable container identity"
                )
            if self._bound_store_identity is None:
                self._emit_store_binding(identity)
            elif self._bound_store_identity != identity:
                raise EvidencePublicationConflict(
                    "evidence authority was resolved for a different container store"
                )

    def assert_store(self, orchestrator: Any) -> None:
        identity = _store_identity(orchestrator)
        with self._lock:
            if identity is None:
                raise EvidencePublicationConflict(
                    "evidence store has no immutable container identity"
                )
            if self._bound_store_identity is None:
                self._emit_store_binding(identity)
            elif self._bound_store_identity != identity:
                raise EvidencePublicationConflict(
                    "evidence authority was resolved for a different container store"
                )

    def _emit_store_binding(self, identity: str) -> None:
        if self._records or self._heads:
            raise EvidencePublicationConflict(
                "evidence publications cannot precede the immutable store binding"
            )
        if self._sink is None:
            raise EvidencePublicationUnavailableError(
                "evidence store binding requires a writable host control stream"
            )
        payload = EvidenceStoreBoundPayload(run_id=self.run_id, store_identity=identity)
        try:
            self._sink.emit(
                EVIDENCE_STORE_BOUND_EVENT_KIND,
                payload.model_dump(mode="json"),
            )
        except Exception as exc:
            raise EvidencePublicationUnavailableError(
                "host evidence-store binding could not be persisted"
            ) from exc
        self._bound_store_identity = identity

    def _require_bound_store(self) -> str:
        if self._bound_store_identity is None:
            raise EvidencePublicationUnavailableError(
                "evidence authority has no durable immutable store binding"
            )
        return self._bound_store_identity

    def expected_immutable_record_ids(self, record_kind: EvidenceRecordKind) -> frozenset[str]:
        """IDs a complete current-run immutable ledger must contain."""

        if record_kind not in IMMUTABLE_RECORD_KINDS:
            raise ValueError("mutable record kinds are represented by latest heads")
        with self._lock:
            self._require_bound_store()
            return frozenset(record_id for kind, record_id in self._records if kind == record_kind)

    def verify_immutable_record_id_set(
        self,
        record_kind: EvidenceRecordKind,
        observed_record_ids: Any,
    ) -> PublicationCheck:
        """Require one complete, duplicate-free current-run named ledger."""

        if record_kind not in IMMUTABLE_RECORD_KINDS:
            return PublicationCheck(
                "mismatch", detail="mutable record kinds are represented by latest heads"
            )
        with self._lock:
            if self._bound_store_identity is None:
                return PublicationCheck(
                    "unavailable", detail="evidence authority has no durable store binding"
                )
        if not isinstance(observed_record_ids, (list, tuple, set, frozenset)):
            return PublicationCheck("mismatch", detail="observed record ids are not a sequence")
        values = list(observed_record_ids)
        if any(type(value) is not str or not value for value in values):
            return PublicationCheck("mismatch", detail="observed record id is invalid")
        if len(values) != len(set(values)):
            return PublicationCheck("mismatch", detail="observed record ids contain duplicates")
        expected = self.expected_immutable_record_ids(record_kind)
        if set(values) != set(expected):
            return PublicationCheck(
                "mismatch", detail="observed record ids differ from the host expected set"
            )
        return PublicationCheck("verified")

    def latest_head(self, logical_artifact_id: str) -> EvidencePublicationPayload | None:
        """Return the current revision/tombstone for one logical artifact."""

        with self._lock:
            self._require_bound_store()
            return self._heads.get(logical_artifact_id)

    @staticmethod
    def _compatible_mutable_kinds(record_kind: EvidenceRecordKind) -> set[str]:
        return (
            {"build_requirements", "receipt_structure"}
            if record_kind in {"build_requirements", "receipt_structure"}
            else {record_kind}
        )

    def expected_mutable_record_ids(self, record_kind: EvidenceRecordKind) -> Mapping[str, str]:
        """Current non-tombstoned record IDs mapped to their logical heads."""

        if record_kind not in MUTABLE_RECORD_KINDS:
            raise ValueError("immutable record kind has no mutable head set")
        compatible = self._compatible_mutable_kinds(record_kind)
        with self._lock:
            self._require_bound_store()
            pairs = [
                (head.record_id, logical_id)
                for logical_id, head in self._heads.items()
                if head.record_kind in compatible and head.publication_state == "present"
            ]
        if len({record_id for record_id, _ in pairs}) != len(pairs):
            raise EvidencePublicationConflict(
                "two mutable logical heads expose the same current record id"
            )
        return dict(sorted(pairs))

    def verify_mutable_record_id_set(
        self,
        record_kind: EvidenceRecordKind,
        observed_record_ids: Any,
    ) -> PublicationCheck:
        """Require exact current mutable files; tombstones require absence."""

        if not isinstance(observed_record_ids, Mapping):
            return PublicationCheck(
                "mismatch", detail="observed mutable ids are not an id-to-logical mapping"
            )
        with self._lock:
            if self._bound_store_identity is None:
                return PublicationCheck(
                    "unavailable", detail="evidence authority has no durable store binding"
                )
        if any(
            type(record_id) is not str
            or not record_id
            or type(logical_id) is not str
            or not logical_id
            for record_id, logical_id in observed_record_ids.items()
        ):
            return PublicationCheck("mismatch", detail="observed mutable identity is invalid")
        try:
            expected = self.expected_mutable_record_ids(record_kind)
        except (TypeError, ValueError, EvidencePublicationConflict) as exc:
            return PublicationCheck("mismatch", detail=str(exc))
        if dict(observed_record_ids) != dict(expected):
            return PublicationCheck(
                "mismatch", detail="observed mutable ids differ from current host heads"
            )
        return PublicationCheck("verified")

    def verify_latest_record_set(
        self,
        record_kind: EvidenceRecordKind,
        observed_records: Mapping[str, MutablePublicationObservation],
    ) -> PublicationCheck:
        """Atomically authorize a complete current mutable named ledger.

        Per-record verification followed by a separate expected-ID check has a
        TOCTOU gap: a stable logical ID can advance revision between the two
        calls, allowing a reader to return old bytes.  This method validates
        completeness, tombstones and every exact byte/contract binding while
        holding the authority's single head lock.
        """

        if record_kind not in MUTABLE_RECORD_KINDS:
            return PublicationCheck(
                "mismatch", detail="immutable record kind has no mutable head set"
            )
        with self._lock:
            if self._bound_store_identity is None:
                return PublicationCheck(
                    "unavailable", detail="evidence authority has no durable store binding"
                )
        if not isinstance(observed_records, Mapping):
            return PublicationCheck("mismatch", detail="observed mutable records are not a mapping")
        observations = dict(observed_records)
        if any(
            type(record_id) is not str
            or not record_id
            or type(observation) is not MutablePublicationObservation
            or type(observation.logical_artifact_id) is not str
            or not observation.logical_artifact_id
            or type(observation.raw_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", observation.raw_sha256) is None
            or type(observation.byte_count) is not int
            or observation.byte_count < 1
            or observation.byte_count > EVIDENCE_PUBLICATION_MAX_RECORD_BYTES
            or type(observation.run_id) is not str
            or _RUN_ID_RE.fullmatch(observation.run_id) is None
            or (observation.contract_id is not None and type(observation.contract_id) is not str)
            or (
                observation.contract_hash is not None
                and (
                    type(observation.contract_hash) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", observation.contract_hash) is None
                )
            )
            or ((observation.contract_id is None) != (observation.contract_hash is None))
            for record_id, observation in observations.items()
        ):
            return PublicationCheck(
                "mismatch", detail="observed mutable record identity is invalid"
            )
        compatible = self._compatible_mutable_kinds(record_kind)
        with self._lock:
            current: dict[str, tuple[str, EvidencePublicationPayload]] = {}
            for logical_id, head in self._heads.items():
                if head.record_kind not in compatible or head.publication_state != "present":
                    continue
                if head.record_id in current:
                    return PublicationCheck(
                        "mismatch",
                        detail="two mutable logical heads expose the same current record id",
                    )
                current[head.record_id] = (logical_id, head)
            if set(observations) != set(current):
                return PublicationCheck(
                    "mismatch",
                    detail="observed mutable records differ from current host heads",
                )
            for record_id, observation in observations.items():
                logical_id, head = current[record_id]
                if (
                    observation.logical_artifact_id != logical_id
                    or observation.run_id != self.run_id
                    or observation.raw_sha256 != head.raw_sha256
                    or observation.byte_count != head.byte_count
                    or observation.contract_id != head.contract_id
                    or observation.contract_hash != head.contract_hash
                ):
                    return PublicationCheck(
                        "mismatch",
                        publication=head,
                        detail="mutable observation differs from the current host revision",
                    )
        return PublicationCheck("verified")

    def snapshot(self) -> EvidencePublicationSnapshot:
        """Freeze expected immutable sets and mutable heads under one lock."""

        with self._lock:
            self._require_bound_store()
            immutable: dict[EvidenceRecordKind, tuple[str, ...]] = {}
            for raw_kind in sorted(IMMUTABLE_RECORD_KINDS):
                kind = cast(EvidenceRecordKind, raw_kind)
                immutable[kind] = tuple(
                    sorted(
                        record_id
                        for candidate_kind, record_id in self._records
                        if candidate_kind == kind
                    )
                )
            return EvidencePublicationSnapshot(
                run_id=self.run_id,
                immutable_record_ids=immutable,
                mutable_heads=dict(self._heads),
            )

    @classmethod
    def recover_from_host_jsonl(
        cls,
        path: str | Path,
        *,
        run_id: str,
        sink: _HostControlSink | None = None,
    ) -> "EvidencePublicationAuthority":
        """Recover one run's allowlist or reject the complete stream.

        Every row is checked even when it belongs to another run.  Valid
        foreign-run publications are ignored; malformed rows, sequence gaps,
        unknown future event kinds and identity conflicts reject recovery
        instead of leaving a deceptively authoritative prefix.
        """

        source = Path(path)
        if sink is not None and Path(sink.path).resolve() != source.resolve():
            raise EvidencePublicationRecoveryError(
                "publication sink and recovery stream are different host files"
            )
        authority = cls(run_id=run_id, sink=sink)
        if not source.exists():
            return authority
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise EvidencePublicationRecoveryError(
                "host publication stream cannot be inspected"
            ) from exc
        if size > EVIDENCE_PUBLICATION_MAX_CONTROL_STREAM_BYTES:
            raise EvidencePublicationRecoveryError(
                "host publication stream exceeds its raw byte limit"
            )

        expected_sequence = 1
        try:
            with source.open("rb") as handle:
                while True:
                    raw_line = handle.readline(CONTROL_EVENT_MAX_RAW_BYTES + 2)
                    if not raw_line:
                        break
                    if len(raw_line) > CONTROL_EVENT_MAX_RAW_BYTES:
                        raise EvidencePublicationRecoveryError(
                            "host publication stream contains an oversized event"
                        )
                    if raw_line in {b"\n", b"\r\n"} or not raw_line.endswith(b"\n"):
                        raise EvidencePublicationRecoveryError(
                            "host publication stream contains a partial or blank row"
                        )
                    event = ControlEvent.model_validate_json(raw_line)
                    decoded = json.loads(raw_line)
                    if not isinstance(decoded, dict) or type(decoded.get("sequence")) is not int:
                        raise EvidencePublicationRecoveryError(
                            "host publication event sequence is not a strict integer"
                        )
                    if event.sequence != expected_sequence:
                        raise EvidencePublicationRecoveryError(
                            "host publication event sequence is not contiguous"
                        )
                    expected_sequence += 1
                    if event.kind == EVIDENCE_STORE_BOUND_EVENT_KIND:
                        binding = EvidenceStoreBoundPayload.model_validate(event.payload)
                        if binding.run_id != authority.run_id:
                            continue
                        if authority._bound_store_identity is not None:
                            raise EvidencePublicationRecoveryError(
                                "host stream repeats the evidence-store binding"
                            )
                        authority._bound_store_identity = binding.store_identity
                        continue
                    if event.kind != EVIDENCE_PUBLICATION_EVENT_KIND:
                        continue
                    publication = EvidencePublicationPayload.model_validate(event.payload)
                    if publication.run_id == authority.run_id:
                        if authority._bound_store_identity is None:
                            raise EvidencePublicationRecoveryError(
                                "evidence publication precedes its durable store binding"
                            )
                        authority._admit_recovered(publication)
        except EvidencePublicationError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise EvidencePublicationRecoveryError(
                "host publication stream failed strict validation"
            ) from exc
        return authority

    @classmethod
    def for_live_run(
        cls,
        *,
        run_id: str,
        sink: ControlEventSink,
    ) -> "EvidencePublicationAuthority":
        """Recover and attach the host sink used for subsequent publication."""

        return cls.recover_from_host_jsonl(sink.path, run_id=run_id, sink=sink)

    def _admit_recovered(self, publication: EvidencePublicationPayload) -> None:
        if self._bound_store_identity is None:
            raise EvidencePublicationRecoveryError(
                "recovered publication has no immutable store binding"
            )
        if publication.run_id != self.run_id:
            raise EvidencePublicationRecoveryError(
                "publication was admitted into the wrong run authority"
            )
        if publication.record_kind in MUTABLE_RECORD_KINDS:
            logical_id = publication.logical_artifact_id
            assert logical_id is not None
            prior = self._heads.get(logical_id)
            if prior == publication:
                return
            expected_revision = 1 if prior is None else int(prior.revision or 0) + 1
            expected_previous_raw = (
                EVIDENCE_PUBLICATION_GENESIS_SHA256 if prior is None else prior.raw_sha256
            )
            expected_previous_state = (
                EVIDENCE_PUBLICATION_GENESIS_SHA256
                if prior is None
                else _publication_state_sha256(prior)
            )
            if (
                publication.revision != expected_revision
                or publication.previous_raw_sha256 != expected_previous_raw
                or publication.previous_publication_sha256 != expected_previous_state
                or (
                    prior is not None
                    and (
                        publication.record_id != prior.record_id
                        or prior.record_kind
                        not in self._compatible_mutable_kinds(publication.record_kind)
                        or prior.publication_state == "revoked"
                        or (
                            publication.publication_state == "present"
                            and (
                                publication.contract_id != prior.contract_id
                                or publication.contract_hash != prior.contract_hash
                            )
                        )
                    )
                )
            ):
                raise EvidencePublicationRecoveryError(
                    "mutable publication revision chain is discontinuous"
                )
            self._heads[logical_id] = publication
            return
        key = (publication.record_kind, publication.record_id)
        prior = self._records.get(key)
        if prior is None:
            self._records[key] = publication
        elif prior != publication:
            raise EvidencePublicationRecoveryError(
                "host publication identity has conflicting committed bytes"
            )

    def publish_bytes(
        self,
        *,
        record_kind: EvidenceRecordKind,
        record_id: str,
        raw: bytes,
        contract_id: str | None = None,
        contract_hash: str | None = None,
    ) -> EvidencePublicationPayload:
        """Commit exact record bytes to the host stream before admitting them."""

        publication = _immutable_publication_payload(
            record_kind=record_kind,
            record_id=record_id,
            raw=raw,
            run_id=self.run_id,
            contract_id=contract_id,
            contract_hash=contract_hash,
        )
        key = (publication.record_kind, publication.record_id)
        with self._lock:
            self._require_bound_store()
            prior = self._records.get(key)
            if prior is not None:
                if prior == publication:
                    return prior
                raise EvidencePublicationConflict(
                    "evidence publication identity already commits different bytes"
                )
            if self._sink is None:
                raise EvidencePublicationUnavailableError(
                    "evidence publication authority is read-only"
                )
            try:
                self._sink.emit(
                    EVIDENCE_PUBLICATION_EVENT_KIND,
                    publication.model_dump(mode="json", exclude_unset=True),
                )
            except Exception as exc:
                # The allowlist is intentionally updated only after durable
                # host emission.  A container mirror failure is swallowed by
                # ControlEventSink *after* its host append and is therefore not
                # an authority failure.
                raise EvidencePublicationUnavailableError(
                    "host evidence-publication event could not be persisted"
                ) from exc
            self._records[key] = publication
            return publication

    def publish_revision(
        self,
        *,
        record_kind: EvidenceRecordKind,
        record_id: str,
        logical_artifact_id: str,
        raw: bytes,
        expected_previous_raw_sha256: str,
        contract_id: str | None = None,
        contract_hash: str | None = None,
    ) -> EvidencePublicationPayload:
        """Advance one mutable logical head using optimistic host CAS."""

        exact = _exact_record_bytes(raw)
        if record_kind not in MUTABLE_RECORD_KINDS:
            raise ValueError("immutable evidence kind cannot publish a revision")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", logical_artifact_id):
            raise ValueError("logical artifact id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_previous_raw_sha256):
            raise ValueError("expected previous digest is invalid")
        with self._lock:
            self._require_bound_store()
            prior = self._heads.get(logical_artifact_id)
            current_raw = EVIDENCE_PUBLICATION_GENESIS_SHA256 if prior is None else prior.raw_sha256
            candidate_raw = hashlib.sha256(exact).hexdigest()
            if (
                prior is not None
                and prior.publication_state == "present"
                and prior.raw_sha256 == candidate_raw
                and prior.byte_count == len(exact)
            ):
                compatible = self._compatible_mutable_kinds(record_kind)
                if (
                    prior.record_kind in compatible
                    and prior.record_id == record_id
                    and prior.contract_id == contract_id
                    and prior.contract_hash == contract_hash
                ):
                    return prior
                raise EvidencePublicationConflict(
                    "same mutable bytes were offered with different identity or linkage"
                )
            if expected_previous_raw_sha256 != current_raw:
                raise EvidencePublicationConflict(
                    "mutable publication lost its expected host-head revision"
                )
            if prior is not None:
                if prior.publication_state == "revoked":
                    raise EvidencePublicationConflict(
                        "a revoked mutable artifact cannot be resurrected"
                    )
                compatible = self._compatible_mutable_kinds(record_kind)
                if (
                    prior.record_kind not in compatible
                    or prior.record_id != record_id
                    or prior.contract_id != contract_id
                    or prior.contract_hash != contract_hash
                ):
                    raise EvidencePublicationConflict(
                        "mutable revision changed its stable artifact identity"
                    )
            previous_state = (
                EVIDENCE_PUBLICATION_GENESIS_SHA256
                if prior is None
                else _publication_state_sha256(prior)
            )
            publication = _revision_payload(
                record_kind=record_kind,
                record_id=record_id,
                logical_artifact_id=logical_artifact_id,
                raw=exact,
                run_id=self.run_id,
                revision=1 if prior is None else int(prior.revision or 0) + 1,
                previous_raw_sha256=current_raw,
                previous_publication_sha256=previous_state,
                contract_id=contract_id,
                contract_hash=contract_hash,
            )
            self._emit_and_admit_head(publication)
            return publication

    def revoke_latest(
        self,
        *,
        record_kind: EvidenceRecordKind,
        record_id: str,
        logical_artifact_id: str,
        expected_previous_raw_sha256: str,
    ) -> EvidencePublicationPayload:
        """Append a tombstone so no historically published body stays current."""

        if record_kind not in MUTABLE_RECORD_KINDS:
            raise ValueError("immutable evidence cannot be revoked as a mutable head")
        with self._lock:
            self._require_bound_store()
            prior = self._heads.get(logical_artifact_id)
            if prior is not None and prior.publication_state == "revoked":
                compatible = self._compatible_mutable_kinds(record_kind)
                if (
                    prior.record_kind in compatible
                    and prior.record_id == record_id
                    and prior.previous_raw_sha256 == expected_previous_raw_sha256
                ):
                    return prior
                raise EvidencePublicationConflict(
                    "revocation replay changed its stable identity or predecessor"
                )
            compatible = self._compatible_mutable_kinds(record_kind)
            if (
                prior is None
                or prior.raw_sha256 != expected_previous_raw_sha256
                or prior.record_kind not in compatible
                or prior.record_id != record_id
            ):
                raise EvidencePublicationConflict(
                    "mutable revocation lost its expected host-head revision"
                )
            publication = _revision_payload(
                record_kind=record_kind,
                record_id=record_id,
                logical_artifact_id=logical_artifact_id,
                raw=None,
                run_id=self.run_id,
                revision=int(prior.revision or 0) + 1,
                previous_raw_sha256=prior.raw_sha256,
                previous_publication_sha256=_publication_state_sha256(prior),
                contract_id=None,
                contract_hash=None,
                revoked=True,
            )
            self._emit_and_admit_head(publication)
            return publication

    def _emit_and_admit_head(self, publication: EvidencePublicationPayload) -> None:
        self._require_bound_store()
        if self._sink is None:
            raise EvidencePublicationUnavailableError("evidence publication authority is read-only")
        try:
            self._sink.emit(
                EVIDENCE_PUBLICATION_EVENT_KIND,
                publication.model_dump(mode="json", exclude_unset=True),
            )
        except Exception as exc:
            raise EvidencePublicationUnavailableError(
                "host evidence-publication event could not be persisted"
            ) from exc
        assert publication.logical_artifact_id is not None
        self._heads[publication.logical_artifact_id] = publication

    def verify_bytes(
        self,
        *,
        record_kind: EvidenceRecordKind,
        record_id: str,
        raw: bytes,
        run_id: str | None = None,
        contract_id: str | None = None,
        contract_hash: str | None = None,
    ) -> PublicationCheck:
        """Verify exact bytes plus run and optional contract linkage."""

        requested_run = self.run_id if run_id is None else run_id
        if requested_run != self.run_id:
            return PublicationCheck("foreign_run", detail="publication belongs to another run")
        try:
            candidate = _immutable_publication_payload(
                record_kind=record_kind,
                record_id=record_id,
                raw=raw,
                run_id=requested_run,
                contract_id=contract_id,
                contract_hash=contract_hash,
            )
        except (TypeError, ValueError):
            return PublicationCheck("mismatch", detail="publication query is invalid")
        key = (candidate.record_kind, candidate.record_id)
        with self._lock:
            if self._bound_store_identity is None:
                return PublicationCheck(
                    "unavailable", detail="evidence authority has no durable store binding"
                )
            publication = self._records.get(key)
        if publication is None:
            return PublicationCheck("not_published")
        if publication != candidate:
            return PublicationCheck(
                "mismatch",
                publication=publication,
                detail="record bytes or contract linkage differ from host publication",
            )
        return PublicationCheck("verified", publication=publication)

    def verify_latest_bytes(
        self,
        *,
        record_kind: EvidenceRecordKind,
        record_id: str,
        logical_artifact_id: str,
        raw: bytes,
        run_id: str | None = None,
        contract_id: str | None = None,
        contract_hash: str | None = None,
    ) -> PublicationCheck:
        """Authorize bytes only when they match the current logical head."""

        requested_run = self.run_id if run_id is None else run_id
        if requested_run != self.run_id:
            return PublicationCheck("foreign_run", detail="publication belongs to another run")
        if record_kind not in MUTABLE_RECORD_KINDS:
            return PublicationCheck("mismatch", detail="immutable kind requires verify_bytes")
        try:
            exact = _exact_record_bytes(raw)
        except (TypeError, ValueError):
            return PublicationCheck("mismatch", detail="publication query is invalid")
        with self._lock:
            if self._bound_store_identity is None:
                return PublicationCheck(
                    "unavailable", detail="evidence authority has no durable store binding"
                )
            publication = self._heads.get(logical_artifact_id)
        if publication is None:
            return PublicationCheck("not_published")
        if publication.publication_state == "revoked":
            return PublicationCheck("revoked", publication=publication)
        compatible = self._compatible_mutable_kinds(record_kind)
        if publication.record_kind not in compatible:
            return PublicationCheck(
                "mismatch",
                publication=publication,
                detail="latest head was produced for an incompatible artifact kind",
            )
        if (
            publication.record_id != record_id
            or publication.contract_id != contract_id
            or publication.contract_hash != contract_hash
        ):
            return PublicationCheck(
                "mismatch",
                publication=publication,
                detail="latest head identity or contract linkage differs",
            )
        if publication.raw_sha256 != hashlib.sha256(
            exact
        ).hexdigest() or publication.byte_count != len(exact):
            return PublicationCheck(
                "mismatch",
                publication=publication,
                detail="record bytes differ from the latest host revision",
            )
        return PublicationCheck("verified", publication=publication)


AuthorityHandle: TypeAlias = EvidencePublicationAuthority | UnavailableEvidencePublicationAuthority


_CURRENT_AUTHORITY: ContextVar[AuthorityHandle | None] = ContextVar(
    "sag_evidence_publication_authority", default=None
)


def unavailable_evidence_publication_authority(
    reason: str,
    *,
    run_id: str | None = None,
) -> UnavailableEvidencePublicationAuthority:
    normalized = str(reason or "").strip()
    if not normalized:
        raise ValueError("unavailable publication authority requires a reason")
    return UnavailableEvidencePublicationAuthority(normalized, run_id=run_id)


def install_evidence_publication_authority(
    authority: AuthorityHandle,
    *,
    orchestrator: Any | None = None,
):
    """Install authority for the current context and orchestrator workers."""

    if not isinstance(
        authority,
        (EvidencePublicationAuthority, UnavailableEvidencePublicationAuthority),
    ):
        raise TypeError("evidence publication authority has an invalid type")
    if orchestrator is not None:
        if isinstance(authority, EvidencePublicationAuthority):
            authority.bind_store(orchestrator)
        setattr(orchestrator, _AUTHORITY_ATTRIBUTE, authority)
    return _CURRENT_AUTHORITY.set(authority)


def reset_evidence_publication_authority(token: Any) -> None:
    """Restore the prior current-context authority (primarily for tests)."""

    _CURRENT_AUTHORITY.reset(token)


def current_evidence_publication_authority(
    orchestrator: Any | None = None,
) -> AuthorityHandle:
    """Return an explicit available/unavailable authority handle."""

    if orchestrator is not None:
        candidate = getattr(orchestrator, _AUTHORITY_ATTRIBUTE, None)
        if isinstance(
            candidate,
            (EvidencePublicationAuthority, UnavailableEvidencePublicationAuthority),
        ):
            if isinstance(candidate, EvidencePublicationAuthority):
                candidate.assert_store(orchestrator)
            return candidate
        return unavailable_evidence_publication_authority(
            "orchestrator has no host evidence-publication authority"
        )
    candidate = _CURRENT_AUTHORITY.get()
    if candidate is not None:
        return candidate
    return unavailable_evidence_publication_authority(
        "no host evidence-publication authority is installed"
    )


def evidence_publication_authority_for(source: Any) -> AuthorityHandle:
    """Resolve authority from an orchestrator, its bound executor, or context.

    Evidence writers frequently receive ``orchestrator.execute_command``
    rather than the orchestrator itself.  A free-standing fake/callback is not
    an orchestrator merely because it is callable; it deliberately falls back
    to the current context instead of becoming a second authority channel.
    """

    execute_command = getattr(source, "execute_command", None)
    if source is not None and callable(execute_command):
        candidate = getattr(source, _AUTHORITY_ATTRIBUTE, None)
        if isinstance(
            candidate,
            (EvidencePublicationAuthority, UnavailableEvidencePublicationAuthority),
        ):
            if isinstance(candidate, EvidencePublicationAuthority):
                candidate.assert_store(source)
            return candidate
        fallback = current_evidence_publication_authority()
        if isinstance(fallback, EvidencePublicationAuthority):
            fallback._bind_contextual_store(source)
        return fallback
    owner = getattr(source, "__self__", None) if callable(source) else None
    owner_execute = getattr(owner, "execute_command", None)
    if owner is not None and callable(owner_execute):
        candidate = getattr(owner, _AUTHORITY_ATTRIBUTE, None)
        if isinstance(
            candidate,
            (EvidencePublicationAuthority, UnavailableEvidencePublicationAuthority),
        ):
            if isinstance(candidate, EvidencePublicationAuthority):
                candidate.assert_store(owner)
            return candidate
        fallback = current_evidence_publication_authority()
        if isinstance(fallback, EvidencePublicationAuthority):
            fallback._bind_contextual_store(owner)
        return fallback
    return current_evidence_publication_authority()


def content_addressed_record_id(prefix: str, raw: bytes) -> str:
    """Return a bounded identity for a genuinely unique immutable artifact."""

    if type(prefix) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", prefix):
        raise ValueError("publication record prefix is invalid")
    exact = _exact_record_bytes(raw)
    return f"{prefix}-{hashlib.sha256(exact).hexdigest()[:24]}"


def publish_evidence_bytes(
    source: Any,
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    raw: bytes,
    contract_id: str | None = None,
    contract_hash: str | None = None,
) -> PublicationAttempt:
    """Synchronously publish already-persisted exact bytes without raising."""

    authority = evidence_publication_authority_for(source)
    try:
        publication = authority.publish_bytes(
            record_kind=record_kind,
            record_id=record_id,
            raw=raw,
            contract_id=contract_id,
            contract_hash=contract_hash,
        )
    except EvidencePublicationConflict as exc:
        return PublicationAttempt("publication_conflict", detail=str(exc))
    except EvidencePublicationUnavailableError as exc:
        return PublicationAttempt("publication_unavailable", detail=str(exc))
    except (TypeError, ValueError) as exc:
        return PublicationAttempt("publication_invalid", detail=str(exc))
    return PublicationAttempt("published", publication=publication)


def verify_evidence_bytes(
    source: Any,
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    raw: bytes,
    run_id: str | None = None,
    contract_id: str | None = None,
    contract_hash: str | None = None,
) -> PublicationCheck:
    """Verify one immutable current-run record through its host authority."""

    return evidence_publication_authority_for(source).verify_bytes(
        record_kind=record_kind,
        record_id=record_id,
        raw=raw,
        run_id=run_id,
        contract_id=contract_id,
        contract_hash=contract_hash,
    )


def verify_latest_evidence_bytes(
    source: Any,
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    logical_artifact_id: str,
    raw: bytes,
    run_id: str | None = None,
    contract_id: str | None = None,
    contract_hash: str | None = None,
) -> PublicationCheck:
    """Verify exact bytes against one current mutable logical head."""

    return evidence_publication_authority_for(source).verify_latest_bytes(
        record_kind=record_kind,
        record_id=record_id,
        logical_artifact_id=logical_artifact_id,
        raw=raw,
        run_id=run_id,
        contract_id=contract_id,
        contract_hash=contract_hash,
    )


def latest_publication_raw_sha256(source: Any, logical_artifact_id: str) -> str:
    """Digest a mutable writer must CAS against, or the strict genesis token."""

    authority = evidence_publication_authority_for(source)
    head = authority.latest_head(logical_artifact_id)
    return head.raw_sha256 if head is not None else EVIDENCE_PUBLICATION_GENESIS_SHA256


def publish_evidence_revision(
    source: Any,
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    logical_artifact_id: str,
    raw: bytes,
    expected_previous_raw_sha256: str,
    contract_id: str | None = None,
    contract_hash: str | None = None,
) -> PublicationAttempt:
    """Synchronously advance a mutable head after its exact bytes persisted."""

    authority = evidence_publication_authority_for(source)
    try:
        publication = authority.publish_revision(
            record_kind=record_kind,
            record_id=record_id,
            logical_artifact_id=logical_artifact_id,
            raw=raw,
            expected_previous_raw_sha256=expected_previous_raw_sha256,
            contract_id=contract_id,
            contract_hash=contract_hash,
        )
    except EvidencePublicationConflict as exc:
        return PublicationAttempt("publication_conflict", detail=str(exc))
    except EvidencePublicationUnavailableError as exc:
        return PublicationAttempt("publication_unavailable", detail=str(exc))
    except (TypeError, ValueError) as exc:
        return PublicationAttempt("publication_invalid", detail=str(exc))
    return PublicationAttempt("published", publication=publication)


def revoke_evidence_artifact(
    source: Any,
    *,
    record_kind: EvidenceRecordKind,
    record_id: str,
    logical_artifact_id: str,
    expected_previous_raw_sha256: str,
) -> PublicationAttempt:
    """Append a host tombstone for a mutable artifact."""

    authority = evidence_publication_authority_for(source)
    try:
        publication = authority.revoke_latest(
            record_kind=record_kind,
            record_id=record_id,
            logical_artifact_id=logical_artifact_id,
            expected_previous_raw_sha256=expected_previous_raw_sha256,
        )
    except EvidencePublicationConflict as exc:
        return PublicationAttempt("publication_conflict", detail=str(exc))
    except EvidencePublicationUnavailableError as exc:
        return PublicationAttempt("publication_unavailable", detail=str(exc))
    except (TypeError, ValueError) as exc:
        return PublicationAttempt("publication_invalid", detail=str(exc))
    return PublicationAttempt("revoked", publication=publication)


__all__ = [
    "AuthorityHandle",
    "BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID",
    "DOCUMENT_MAP_LOGICAL_ARTIFACT_ID",
    "ENV_OVERLAY_LOGICAL_ARTIFACT_ID",
    "EVIDENCE_PUBLICATION_EVENT_KIND",
    "EVIDENCE_PUBLICATION_MAX_CONTROL_STREAM_BYTES",
    "EvidencePublicationAuthority",
    "EvidencePublicationConflict",
    "EvidencePublicationError",
    "EvidencePublicationRecoveryError",
    "EvidencePublicationSnapshot",
    "EvidencePublicationUnavailableError",
    "EvidenceRecordKind",
    "IMMUTABLE_RECORD_KINDS",
    "MUTABLE_RECORD_KINDS",
    "MutablePublicationObservation",
    "PublicationCheck",
    "PublicationCheckStatus",
    "PublicationAttempt",
    "PublicationAttemptStatus",
    "RUN_PIN_LOGICAL_ARTIFACT_ID",
    "VERDICT_LOGICAL_ARTIFACT_ID",
    "UnavailableEvidencePublicationAuthority",
    "current_evidence_publication_authority",
    "content_addressed_record_id",
    "evidence_publication_authority_for",
    "install_evidence_publication_authority",
    "latest_publication_raw_sha256",
    "publish_evidence_bytes",
    "publish_evidence_revision",
    "reset_evidence_publication_authority",
    "revoke_evidence_artifact",
    "unavailable_evidence_publication_authority",
    "verify_evidence_bytes",
    "verify_latest_evidence_bytes",
]
