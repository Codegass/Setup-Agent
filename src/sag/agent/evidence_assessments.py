"""Append-only typed assessments of receipts and control events (Plan 6 Stage 0).

Spec §C4: an `InvocationReceipt` states what a runner PHYSICALLY did and is
finalized once. What that run MEANS is a separate record, because the meaning
arrives later and from a different authority than the exit code.

Plan 5 conflated the two: `mark_semantic_failure` re-read a finalized receipt
and overwrote it (the gradle NO-SOURCE downgrade), so the bytes on disk
depended on how many classifiers had run since — a receipt could not be a
stable evidence anchor. This module replaces that path:

    /workspace/.setup_agent/evidence_assessments/<assessment_id>.json

* `ReceiptAssessment` — a typed verdict ABOUT one receipt ("this exit 0
  compiled nothing"). It never touches the receipt file.
* `ControlAssessment` — a typed pre-dispatch/control fact ("these args were
  refused"). A refusal dispatched no runner, so it mints no receipt, but it is
  not silence either.

Plan 6 Stage C (spec §C5) adds the thing that WRITES those receipt verdicts:
`assess_receipt` compares a frozen contract with the receipt of the dispatch it
authorized and answers with ONE typed code. The point of the taxonomy is that a
mismatch is not automatically a contradiction — a proxy timeout, a fingerprint
the harness has moved past and a genuinely empty compile are three different
facts, and Plan 5 recorded all three as "the build failed".

Two properties make replay safe: `assessment_id` is derived from the subject
and the typed code, so writing the same verdict twice is idempotent; and a
DIFFERENT body under an existing id is refused, never merged and never
overwritten. Nothing in a payload comes from a clock, so the same evidence
always reconstructs the same bytes.

Persistence is best effort HERE (same contract as invocation_receipts): this
module never raises. Turning a failed write into an evidence-closure failure
is the phase gate's business, not the runner's.
"""

import hashlib
import itertools
import json
import posixpath
import re
import shlex
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.control_ownership import BlockerOwner, blocker_owner_for_assessment
from sag.agent.evidence_records import (
    EvidencePublicationBinding,
    PublishedNamedJsonRecordStreamRead,
    read_live_published_json_records,
)
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    PYTHON_FACADE_EXECUTION_BINDING,
    compliance_class,
    live_contract_valid,
    python_operation_for_public_action,
    read_frozen_contract,
)
from sag.agent.invocation_receipts import (
    RECEIPT_DIR,
    read_producer_observations,
    receipt_record_scope,
    validate_receipt_v2,
)
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
)

ASSESSMENT_SCHEMA_VERSION = 2
ASSESSMENT_DIR = "/workspace/.setup_agent/evidence_assessments"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# assessment content can ever collide with it.
ASSESSMENT_HEREDOC = "SAGASSESSMENT"
DETAIL_MAX_CHARS = 200
SUBJECT_SLUG_MAX_CHARS = 48
CODE_SLUG_MAX_CHARS = 40

# Structured prerequisite riders.  They describe what the runner/testcase
# physically reported; they do not authorize an install, a service start, or
# any other action.  Candidate extraction walks the complete output but keeps
# only a bounded number of bounded lines, so a large build log cannot turn the
# assessor into an unbounded prompt/parser surface.
PREREQUISITE_EXECUTABLE_MISSING = "prerequisite_executable_missing"
PREREQUISITE_SERVICE_UNAVAILABLE = "prerequisite_service_unavailable"
PREREQUISITE_OUTPUT_CANDIDATE_CAP = 64
PREREQUISITE_OUTPUT_LINE_MAX_CHARS = 2_000
PREREQUISITE_FINDING_CAP = 16
PREREQUISITE_FIELD_MAX_CHARS = 500
ASSESSMENT_MAX_CANONICAL_BYTES = 1 << 20
ASSESSMENT_SEQUENCE_MAX_ITEMS = 64
_ASSESSMENT_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")

# The typed stages a control assessment may name (spec §C4). A stage outside
# this set is a programming error, not a fact about the run, so it is refused
# rather than persisted.
CONTROL_STAGES = (
    "precondition",
    "materialization",
    "envelope",
    "dispatch",
    "postcondition",
    "gate",
)

# --- the §C5 receipt taxonomy ----------------------------------------------
# Codes whose CAUSE lies outside the project: nothing was learned about the
# code, so they leave the expected claim unknown/blocked and can never
# contradict it, however suggestive the rest of the receipt looks.
BLOCKED_CLASS_CODES = (
    "no_dispatch",
    "transient_network",
    "timeout",
    "permission_denied",
    "precondition_unmet",
)
# The contract was frozen against pins the harness has since moved past. The
# receipt stays historical evidence; it just no longer speaks for NOW.
STALE_FINGERPRINT = "stale_fingerprint"
# The dispatch did not run the frozen vector. That is an observation about the
# dispatch, never evidence against a contract it declined to honour.
DEVIATED_RECEIPT = "deviated_receipt"
# The dispatch honoured the contract and produced what it promised.
EXPECTATION_MET = "expectation_met"
# It honoured the contract and did not. An honest failure — a compiler error is
# a real fact about the run, but it falsifies no claim on its own.
EXPECTATION_UNMET = "expectation_unmet"
# A valid, honored contract whose operation-specific positive predicate is
# absent. This is neither green nor proof the project is wrong.
EXPECTATION_UNOBSERVED = "expectation_unobserved"
# Current evidence could not be tied to one live v2 commitment. The harness,
# not the project/model, owns this integrity defect.
CONTRACT_BINDING_UNKNOWN = "contract_binding_unknown"
# `falsifier_<predicate_id>` is the ONLY contradicting shape (spec §C5).
FALSIFIER_PREFIX = "falsifier_"
# `capability_absent_<name>` rides alongside the primary verdict.
CAPABILITY_PREFIX = "capability_absent_"

# The compliance classes that let a receipt speak for the contract at all.
# `None` is UNKNOWABLE, not compliant, so it is deliberately not here.
COMPLIANT_CLASSES = ("exact", "equivalent")

# What a dispatch STATE means, typed. The routing authority is the typed fact
# the facade already holds — never a raw failure string (spec §C6).
DISPATCH_STATUS_CODES = {
    "cancelled": "no_dispatch",
    "pending": "no_dispatch",
    "timeout": "timeout",
}
# Which typed tool error codes name a cause outside the project. Data, so a new
# runner adds a row instead of a branch.
BLOCKED_CLASS_ERROR_CODES = {
    "CONNECTION_ERROR": "transient_network",
    "ENV_ACTIVATION_NOT_CONFIRMED": "precondition_unmet",
    "NETWORK_ERROR": "transient_network",
    "PERMISSION_ERROR": "permission_denied",
    "PREREQUISITE_INCOMPLETE": "precondition_unmet",
    "VERSION_MISMATCH": "precondition_unmet",
}
# The pins a contract and the harness's current state can disagree about. A pin
# only one side states is UNKNOWN, never a mismatch.
FINGERPRINT_KEYS = (
    "target_sha",
    "survey_fingerprint",
    "config_fingerprint",
    "document_map_fingerprint",
    "domain_id",
    "fact_epoch",
)
# The named capabilities a skip reason can reveal as absent. PATTERNS, not
# project names: the table is data, an ecosystem adds a row, and the assessor
# never learns what a project is called.
CAPABILITY_PATTERNS = (
    {"name": "llvm", "pattern": "need llvm|LLVM"},
    {"name": "cuda", "pattern": "CUDA"},
)

_COMMAND_NOT_FOUND_RE = re.compile(
    r"(?<![\w./+-])(?P<name>[A-Za-z_][A-Za-z0-9_.+/-]{0,127})" r"\s*:\s*command\s+not\s+found\b",
    re.IGNORECASE,
)
_SHELL_NOT_FOUND_RE = re.compile(
    r"\b(?:ba|z|da|k)?sh:\s*(?:(?:line\s+)?\d+:\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.+/-]{0,127})\s*:\s*not\s+found\b",
    re.IGNORECASE,
)
_NO_SUCH_EXECUTABLE_AFTER_RE = re.compile(
    r"\bno\s+such\s+executable\b\s*(?::|=|\bfor\b)\s*"
    r"[`'\"]?(?P<name>[A-Za-z_./][A-Za-z0-9_.+/-]{0,127})",
    re.IGNORECASE,
)
_NO_SUCH_EXECUTABLE_BEFORE_RE = re.compile(
    r"(?<![\w./+-])(?P<name>[A-Za-z_][A-Za-z0-9_.+/-]{0,127})" r"\s*:\s*no\s+such\s+executable\b",
    re.IGNORECASE,
)
_CANNOT_RUN_PROGRAM_RE = re.compile(
    r"\bcannot\s+run\s+program\s+[`'\"]"
    r"(?P<name>[^`'\"\s]{1,128})[`'\"]"
    r"[^\r\n]{0,240}\bno\s+such\s+file\s+or\s+directory\b",
    re.IGNORECASE,
)
_ENDPOINT_RE = re.compile(
    r"(?<![\w.-])(?P<host>\[[0-9A-Fa-f:]+\]|localhost|"
    r"(?:\d{1,3}\.){3}\d{1,3}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)"
    r":(?P<port>\d{1,5})(?!\d)",
    re.IGNORECASE,
)
_CONNECTION_REFUSED_RE = re.compile(
    r"\bconnection\s+(?:was\s+)?refused\b|\bconnectionrefusederror\b|" r"\beconnrefused\b",
    re.IGNORECASE,
)
_SERVICE_HINT_PATTERNS = (
    re.compile(
        r"\bservice(?:_hint)?\s*[:=]\s*[`'\"]?" r"(?P<hint>[A-Za-z][A-Za-z0-9_.+-]{0,63})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<hint>[A-Za-z][A-Za-z0-9_.+-]{0,63})\s+service\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<hint>[A-Za-z][A-Za-z0-9+.-]{0,31})://",
        re.IGNORECASE,
    ),
)

_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()


def next_control_event_id(scope: str) -> str:
    """`ctl-<scope>-<seq>` — one id per control event in this process.

    Same shape and same guarantee as `next_receipt_id`: two refusals are two
    events, so they are two assessments rather than one idempotent write.
    """
    with _SEQUENCE_LOCK:
        sequence = next(_SEQUENCE)
    return f"ctl-{_slug(scope) or 'control'}-{sequence:04d}"


def assessment_id(subject_id: str, typed_code: str) -> str:
    """The deterministic id of one assessment of `subject_id` as `typed_code`.

    Readable prefix (the evidence directory is meant to be read by a human
    reviewing a run) plus a digest of the exact pair, so two subjects whose
    slugs truncate to the same text still get distinct files.
    """
    subject = str(subject_id or "")
    code = str(typed_code or "")
    digest = hashlib.sha256(f"{subject}\x00{code}".encode("utf-8")).hexdigest()[:8]
    return (
        f"asm-{_slug(subject)[:SUBJECT_SLUG_MAX_CHARS]}"
        f"-{_slug(code)[:CODE_SLUG_MAX_CHARS]}-{digest}"
    )


@dataclass(frozen=True)
class ReceiptAssessment:
    """A typed verdict about ONE finalized receipt (spec §C4/§C5)."""

    receipt_id: str
    typed_code: str
    detail: str = ""
    fingerprints: Optional[Mapping[str, str]] = None
    created_event: Optional[str] = None
    blocker_owner: BlockerOwner | str | None = None
    name: Optional[str] = None
    endpoint: Optional[str] = None
    service_hint: Optional[str] = None
    scope: Optional[str] = None
    evidence_ref: Optional[str] = None

    @property
    def subject_id(self) -> str:
        return str(self.receipt_id or "").strip()

    @property
    def assessment_id(self) -> str:
        code = str(self.typed_code or "").strip()
        # One receipt can report more than one missing executable or endpoint.
        # Keep the payload's general typed code while deriving identity from
        # normalized structured facts.  Detail/stacktrace text and evidence
        # reference are deliberately absent from the identity.
        if code == PREREQUISITE_EXECUTABLE_MISSING:
            code = "\x00".join((code, _normalize_executable(self.name), _bounded_field(self.scope)))
        elif code == PREREQUISITE_SERVICE_UNAVAILABLE:
            code = "\x00".join(
                (code, _normalize_endpoint(self.endpoint), _bounded_field(self.scope))
            )
        return assessment_id(self.subject_id, code)

    @property
    def owner(self) -> BlockerOwner:
        if self.blocker_owner is not None:
            return BlockerOwner(self.blocker_owner)
        return blocker_owner_for_assessment(self.typed_code)

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "receipt_id": self.subject_id,
            "typed_code": str(self.typed_code or "").strip(),
            "blocker_owner": self.owner.value,
        }
        detail = _bounded(self.detail)
        if detail:
            body["detail"] = detail
        if self.fingerprints:
            body["fingerprints"] = {
                str(key): str(value) for key, value in dict(self.fingerprints).items()
            }
        created_event = str(self.created_event or "").strip()
        if created_event:
            body["created_event"] = created_event
        code = str(self.typed_code or "").strip()
        name = (
            _normalize_executable(self.name)
            if code == PREREQUISITE_EXECUTABLE_MISSING
            else self.name
        )
        endpoint = (
            _normalize_endpoint(self.endpoint)
            if code == PREREQUISITE_SERVICE_UNAVAILABLE
            else self.endpoint
        )
        for key, value in (
            ("name", name),
            ("endpoint", endpoint),
            ("service_hint", self.service_hint),
            ("scope", self.scope),
            ("evidence_ref", self.evidence_ref),
        ):
            bounded = _bounded_field(value)
            if bounded:
                body[key] = bounded
        return body


@dataclass(frozen=True)
class ControlAssessment:
    """A typed control fact about an intent that never reached a runner.

    It can establish that a control precondition is blocked or unknown; it can
    never contradict a project-owned claim, and it never mints a receipt.
    """

    event_or_intent_id: str
    stage: str
    typed_code: str
    detail: str = ""
    blocker_owner: BlockerOwner | str | None = None
    observed_facts: Optional[Mapping[str, Any]] = None
    evidence_refs: Tuple[str, ...] = ()

    @property
    def subject_id(self) -> str:
        return str(self.event_or_intent_id or "").strip()

    @property
    def assessment_id(self) -> str:
        return assessment_id(self.subject_id, str(self.typed_code or "").strip())

    @property
    def owner(self) -> BlockerOwner:
        if self.blocker_owner is not None:
            return BlockerOwner(self.blocker_owner)
        return blocker_owner_for_assessment(self.typed_code)

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "event_or_intent_id": self.subject_id,
            "stage": str(self.stage or "").strip(),
            "typed_code": str(self.typed_code or "").strip(),
            "blocker_owner": self.owner.value,
        }
        detail = _bounded(self.detail)
        if detail:
            body["detail"] = detail
        if self.observed_facts:
            body["observed_facts"] = dict(self.observed_facts)
        refs = tuple(
            dict.fromkeys(str(ref).strip() for ref in self.evidence_refs if str(ref).strip())
        )
        if refs:
            body["evidence_refs"] = list(refs)
        return body


def validate_assessment_v2(
    payload: Mapping[str, Any],
    *,
    expected_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one exact persisted assessment union or fail closed.

    Host publication proves where bytes came from; this validator proves what
    those bytes mean. Receipt and control assessments have disjoint subject
    fields and exact key sets. Their deterministic identity is recomputed, and
    reconstructing the corresponding dataclass enforces every normalization
    rule used by the writer. Ownership is a closed enum but remains an input to
    reconstruction: gate authority and prerequisite UNKNOWN are intentionally
    more precise than the generic code fallback.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("assessment must be an object")
    body = dict(payload)
    if type(body.get("schema_version")) is not int or body.get("schema_version") != (
        ASSESSMENT_SCHEMA_VERSION
    ):
        raise ValueError("assessment schema_version is not live v2")

    identifier = body.get("assessment_id")
    if not isinstance(identifier, str) or _ASSESSMENT_SAFE_ID.fullmatch(identifier) is None:
        raise ValueError("assessment_id is invalid")
    if expected_id is not None:
        if (
            not isinstance(expected_id, str)
            or _ASSESSMENT_SAFE_ID.fullmatch(expected_id) is None
            or identifier != expected_id
        ):
            raise ValueError("assessment id does not match filename")

    is_receipt = "receipt_id" in body
    is_control = "event_or_intent_id" in body
    if is_receipt == is_control:
        raise ValueError("assessment must name exactly one persisted union subject")

    common_required = {
        "schema_version",
        "assessment_id",
        "typed_code",
        "blocker_owner",
    }
    receipt_optional = {
        "detail",
        "fingerprints",
        "created_event",
        "name",
        "endpoint",
        "service_hint",
        "scope",
        "evidence_ref",
    }
    control_optional = {"detail", "observed_facts", "evidence_refs"}
    required = common_required | ({"receipt_id"} if is_receipt else {"event_or_intent_id", "stage"})
    allowed = required | (receipt_optional if is_receipt else control_optional)
    if set(body) != set(body) & allowed or not required <= set(body):
        raise ValueError(
            "assessment fields are not exact: "
            f"unknown={sorted(set(body) - allowed)} missing={sorted(required - set(body))}"
        )

    typed_code = body.get("typed_code")
    if not isinstance(typed_code, str) or _ASSESSMENT_SAFE_ID.fullmatch(typed_code) is None:
        raise ValueError("assessment typed_code is invalid")
    subject_key = "receipt_id" if is_receipt else "event_or_intent_id"
    subject = body.get(subject_key)
    if not isinstance(subject, str) or _ASSESSMENT_SAFE_ID.fullmatch(subject) is None:
        raise ValueError(f"assessment {subject_key} is invalid")

    try:
        owner = BlockerOwner(body.get("blocker_owner"))
    except (TypeError, ValueError) as exc:
        raise ValueError("assessment blocker_owner is invalid") from exc

    if "detail" in body and (
        not isinstance(body.get("detail"), str)
        or body.get("detail") != _bounded(body.get("detail"))
    ):
        raise ValueError("assessment detail is not canonical and bounded")

    if is_receipt:
        fingerprints = body.get("fingerprints")
        if fingerprints is not None:
            if (
                not isinstance(fingerprints, Mapping)
                or not fingerprints
                or len(fingerprints) > ASSESSMENT_SEQUENCE_MAX_ITEMS
                or any(
                    not isinstance(key, str)
                    or not key
                    or key != key.strip()
                    or not isinstance(value, str)
                    or not value
                    or value != value.strip()
                    for key, value in fingerprints.items()
                )
            ):
                raise ValueError("assessment fingerprints are invalid")
        candidate = ReceiptAssessment(
            receipt_id=subject,
            typed_code=typed_code,
            detail=body.get("detail", ""),
            fingerprints=dict(fingerprints) if isinstance(fingerprints, Mapping) else None,
            created_event=body.get("created_event"),
            blocker_owner=owner,
            name=body.get("name"),
            endpoint=body.get("endpoint"),
            service_hint=body.get("service_hint"),
            scope=body.get("scope"),
            evidence_ref=body.get("evidence_ref"),
        )
    else:
        stage = body.get("stage")
        if not isinstance(stage, str) or stage not in CONTROL_STAGES:
            raise ValueError("control assessment stage is invalid")
        observed_facts = body.get("observed_facts")
        if observed_facts is not None and (
            not isinstance(observed_facts, Mapping) or not observed_facts
        ):
            raise ValueError("control assessment observed_facts are invalid")
        raw_refs = body.get("evidence_refs")
        if raw_refs is not None:
            if (
                not isinstance(raw_refs, list)
                or not raw_refs
                or len(raw_refs) > ASSESSMENT_SEQUENCE_MAX_ITEMS
                or any(
                    not isinstance(ref, str) or not ref or ref != ref.strip() for ref in raw_refs
                )
                or len(set(raw_refs)) != len(raw_refs)
            ):
                raise ValueError("control assessment evidence_refs are invalid")
        candidate = ControlAssessment(
            event_or_intent_id=subject,
            stage=stage,
            typed_code=typed_code,
            detail=body.get("detail", ""),
            blocker_owner=owner,
            observed_facts=(dict(observed_facts) if isinstance(observed_facts, Mapping) else None),
            evidence_refs=tuple(raw_refs or ()),
        )

    reconstructed = candidate.payload()
    if reconstructed != body:
        raise ValueError("assessment payload differs from its canonical persisted union")
    try:
        encoded = json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("assessment is not canonical JSON data") from exc
    if len(encoded) > ASSESSMENT_MAX_CANONICAL_BYTES:
        raise ValueError("assessment exceeds its canonical byte limit")
    return body


def write_assessment(execute, assessment) -> bool:
    """Append one assessment atomically; True when the file holds this body.

    Append-only means three things here: the same body under an existing id is
    a no-op success (a replay must not double-write), a DIFFERENT body under an
    existing id is refused and logged (an id is a claim about identity, so a
    collision is a defect to see, not to resolve silently), and the write
    itself is temp-file + `mv` so no reader ever sees half an assessment.
    """
    try:
        payload = validate_assessment_v2(assessment.payload())
        identifier = payload["assessment_id"]
        body = json.dumps(payload, allow_nan=False, sort_keys=True)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug(f"evidence assessment is invalid: {exc}")
        return False
    final = f"{ASSESSMENT_DIR}/{identifier}.json"
    existing_raw = _read_existing_raw(execute, final)
    if existing_raw is not None:
        if existing_raw == body:
            return _publish_assessment_bytes(
                execute,
                identifier,
                body.encode("utf-8"),
            )
        logger.warning(
            f"evidence assessment {identifier} already records a different body; "
            "assessments are append-only and this write was refused"
        )
        return False
    try:
        result = compare_publish_container_text_atomic(
            execute,
            final,
            body,
            expected_content=None,
            validate_json=True,
        )
    except Exception as exc:
        logger.debug(f"evidence assessment {identifier} not persisted: {exc}")
        return False
    if result.code == WRITE_COMPARE_CONFLICT:
        # An identical concurrent writer may have won after our absence read.
        # Only exact canonical bytes are replay success; a semantic parse is
        # insufficient because host publication seals the raw artifact.
        if _read_existing_raw(execute, final) == body:
            return _publish_assessment_bytes(execute, identifier, body.encode("utf-8"))
        logger.warning(
            f"evidence assessment {identifier} lost an absent-write race to "
            "different bytes; the collision was refused"
        )
        return False
    if not result.persisted:
        logger.debug(f"evidence assessment {identifier} not persisted: {result.code}")
        return False
    return _publish_assessment_bytes(execute, identifier, body.encode("utf-8"))


def _publish_assessment_bytes(execute: Any, identifier: str, raw: bytes) -> bool:
    from sag.agent.evidence_publications import publish_evidence_bytes

    publication = publish_evidence_bytes(
        execute,
        record_kind="receipt_assessment",
        record_id=identifier,
        raw=raw,
    )
    if publication.published:
        return True
    logger.warning(
        f"evidence assessment {identifier} reached the container but host publication "
        f"failed: {publication.status}"
    )
    return False


# ---------------------------------------------------------------------------
# the assessor: one contract, one receipt, one typed verdict (spec §C5)
# ---------------------------------------------------------------------------


def ensure_receipt_assessed(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt_id: Any,
) -> bool:
    """Backstop: assess a valid dispatched receipt no facade path assessed.

    Detached/external facade dispatches may reach the engine observation seam
    before an assessment is present. The write is idempotent, so a receipt the
    facade already assessed is a no-op. This function never mints a contract
    and never raises.
    """
    identifier = str(receipt_id or "").strip()
    if not identifier:
        return False
    try:
        slug = _slug(identifier)
        for name in _list_assessment_files(execute):
            if slug and slug in name:
                return False  # already assessed by the dispatching layer
        receipt = read_receipt(execute, identifier)
        if not isinstance(receipt, Mapping):
            return False
        contract_id = str(receipt.get("contract_id") or "").strip()
        if not contract_id:
            return False
        contract = read_frozen_contract(execute, contract_id)
        if not isinstance(contract, Mapping):
            return False
        assessment = assess_receipt(contract, receipt)
        return write_assessment(execute, assessment)
    except Exception as exc:  # a backstop must never break an observation
        logger.debug(f"receipt {identifier} backstop assessment skipped: {exc}")
        return False


def _list_assessment_files(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> List[str]:
    try:
        result = execute(f"ls {ASSESSMENT_DIR} 2>/dev/null") or {}
    except Exception:
        return []
    return [line.strip() for line in (result.get("output") or "").splitlines() if line.strip()]


def read_live_assessment_ledger(orchestrator: Any) -> PublishedNamedJsonRecordStreamRead:
    """Read the complete live assessment ledger or return a typed conflict.

    Assessments are immutable authority records.  A source file becomes live
    only when its exact basename, canonical v2 union payload, raw bytes and the
    complete immutable ID set all match the host publication ledger.
    """

    return read_live_published_json_records(
        orchestrator,
        ASSESSMENT_DIR,
        record_kind="receipt_assessment",
        validator=lambda payload, expected_id: validate_assessment_v2(
            payload,
            expected_id=expected_id,
        ),
    )


def read_assessments(orchestrator: Any) -> List[Dict[str, Any]]:
    """Project the complete strict live assessment ledger as payloads.

    The historical forgiving directory parser is intentionally not used at a
    live authority seam.  An unpublished, malformed, tampered, truncated or
    deletion-incomplete ledger projects no positive assessments.
    """

    ledger = read_live_assessment_ledger(orchestrator)
    if not ledger.complete or ledger.conflict is not None:
        logger.debug(
            "live assessment ledger unavailable: "
            f"{ledger.conflict or ledger.detail or 'incomplete'}"
        )
        return []
    return [dict(record.payload) for record in ledger.records]


def contract_receipt_binding_problem(
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
) -> str:
    """A typed current-authority defect, or empty when the chain is exact."""

    if not live_contract_valid(contract):
        return "receipt is not bound to a live schema-v2 invocation contract"
    assert isinstance(contract, Mapping)
    if not isinstance(receipt, Mapping):
        return "receipt body is unavailable"
    for key in ("contract_id", "contract_hash", "run_id", "execution_binding"):
        if _text(receipt.get(key)) != _text(contract.get(key)):
            return f"receipt {key} differs from its invocation contract"
    for key in (
        "target_sha",
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
        "domain_id",
        "fact_epoch",
    ):
        if (key in contract) != (key in receipt) or (
            key in contract and _text(contract.get(key)) != _text(receipt.get(key))
        ):
            return f"receipt {key} pin differs from its invocation contract"
    if _text(receipt.get("tool")).lower() != _text(contract.get("effective_tool")).lower():
        return "receipt executor differs from its invocation contract"
    if _text(receipt.get("effective_action")) != _text(contract.get("effective_action")):
        return "receipt effective action differs from its invocation contract"
    expected_cwd = posixpath.normpath(_text(contract.get("expected_cwd")))
    for key in ("working_directory", "actual_cwd"):
        value = _text(receipt.get(key))
        if not value or posixpath.normpath(value) != expected_cwd:
            return f"receipt {key} differs from its invocation contract"

    binding = _text(contract.get("execution_binding"))
    if binding == ARGV_EXECUTION_BINDING:
        recomputed = compliance_class(contract.get("expected_argv"), receipt.get("argv"))
        if recomputed is None or _text(receipt.get("compliance")) != recomputed:
            return "receipt argv compliance is absent or was not mechanically recomputed"
    elif binding == PYTHON_FACADE_EXECUTION_BINDING:
        if "compliance" in receipt:
            return "python semantic receipt must not claim argv compliance"
        requested = contract.get("requested_call")
        params = requested.get("params") if isinstance(requested, Mapping) else None
        if (
            not isinstance(params, Mapping)
            or python_operation_for_public_action(params.get("action"))
            != _text(receipt.get("effective_action"))
            or _text(receipt.get("requested_action")) != _text(receipt.get("effective_action"))
        ):
            return "python receipt operation differs from its public action binding"
    else:  # live_contract_valid already rejects this; retain a total function.
        return "receipt execution binding is unknown"
    return ""


def _current_contract_binding_problem(
    contract: Mapping[str, Any],
    current: Optional[Mapping[str, Any]],
) -> str:
    """Return the missing current project pin that makes authority unknowable.

    A model/project dispatch is live authority only when the harness can bind
    the frozen decision to the *current* target, survey, document map, domain,
    and fact epoch.  Missing is not stale: it is an evidence-integrity hole.
    The sole carve-out is the explicitly controller-owned bash lane used for
    harness/D0 mechanics, which has no surveyed project tuple by design.
    """

    if (
        _text(contract.get("intent_source")) == "controller"
        and _text(contract.get("effective_tool")) == "bash"
    ):
        return ""
    requested = contract.get("requested_call")
    if not isinstance(requested, Mapping) or _text(requested.get("tool")) != "build":
        return ""
    for key in FINGERPRINT_KEYS:
        if key not in contract:
            return f"invocation contract has no current-authority {key} pin"
        if not isinstance(current, Mapping) or key not in current:
            return f"current {key} pin is unavailable"
        if not _text(current.get(key)):
            return f"current {key} pin is unavailable"
    return ""


def _delta_nonempty(value: Any) -> bool:
    return isinstance(value, Mapping) and any(value.get(key) for key in ("new", "changed"))


def _python_semantic_result(
    contract: Mapping[str, Any],
    receipt: Mapping[str, Any],
    current_fingerprints: Optional[Mapping[str, Any]],
) -> str:
    operation = _text(contract.get("effective_action"))
    if operation in {"setup_env", "build", "compile"}:
        observations = read_producer_observations(receipt)
        if not observations or observations.get("operation") != operation:
            return "unobserved"
        if operation == "setup_env":
            setup = observations.get("setup") or {}
            if setup.get("install_outcome") != "success":
                return "unobserved"
            pip_check = setup.get("pip_check")
            imports = setup.get("imports")
            if not isinstance(pip_check, Mapping) or pip_check.get("status") != "clean":
                return "unobserved"
            if (
                not isinstance(imports, Mapping)
                or imports.get("status") != "complete"
                or imports.get("failed_count") != 0
            ):
                return "unobserved"
            current_targets = (current_fingerprints or {}).get("python_import_targets")
            current_targets_sha256 = _text(
                (current_fingerprints or {}).get("python_import_targets_sha256")
            )
            if (
                not isinstance(current_targets, list)
                or not current_targets
                or imports.get("targets") != current_targets
                or _text(imports.get("targets_sha256")) != current_targets_sha256
            ):
                return "unobserved"
            return "met"
        if operation == "build":
            build = observations.get("build") or {}
            return (
                "met"
                if build.get("artifact_status") == "produced" and build.get("artifacts")
                else "unobserved"
            )
        compile_observation = observations.get("compile") or {}
        return "met" if compile_observation.get("status") == "valid" else "unobserved"

    if operation in {"test", "native"}:
        # The current receipt records useful diagnostics, but it does not yet
        # freeze the effective selector for tests or the full resolver /
        # definitions / rebuild-trace tuple for native repair.  Promoting
        # either shape would let a hidden scope rewrite or a loose feature
        # probe masquerade as the contracted semantic operation.  A future
        # typed schema may add those bindings explicitly; v1 stays fail-closed.
        return "unobserved"
    return "unobserved"


def _operation_semantic_result(
    contract: Mapping[str, Any],
    receipt: Mapping[str, Any],
    current_fingerprints: Optional[Mapping[str, Any]],
) -> str:
    binding = _text(contract.get("execution_binding"))
    if binding == PYTHON_FACADE_EXECUTION_BINDING:
        return _python_semantic_result(contract, receipt, current_fingerprints)
    if binding != ARGV_EXECUTION_BINDING:
        return "unobserved"
    promised = _expected_observations(contract)
    if not promised:
        # Exact argv + terminal zero is itself the explicit predicate for a
        # dependency-resolution command that promised no artifact/report.
        return "met"
    if any(
        observation == "report_delta" and _delta_nonempty(receipt.get("report_delta"))
        for observation in promised
    ):
        return "met"
    if any(
        observation == "artifact_or_report_delta"
        and (
            _delta_nonempty(receipt.get("report_delta"))
            or _delta_nonempty(receipt.get("artifact_delta"))
            or bool(receipt.get("module_outcomes"))
            or bool(receipt.get("producer_observations"))
        )
        for observation in promised
    ):
        return "met"
    predicate = _falsified_predicate(contract, receipt)
    return f"falsifier:{predicate}" if predicate else "unobserved"


def assess_receipt(
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
    *,
    current_fingerprints: Optional[Mapping[str, str]] = None,
    dispatch_status: Optional[str] = None,
    error_code: Optional[str] = None,
) -> ReceiptAssessment:
    """What ONE finalized receipt means against the contract that authorized it.

    The order below IS the taxonomy (spec §C5), because each rule disqualifies
    the ones under it:

    1. a cause outside the project — no dispatch, network, timeout, permission,
       an unmet environment precondition — means nothing was learned about the
       code, so it is a blocked-class code and can never contradict;
    2. a pin the harness has moved past makes the receipt historical, not
       current, so it is `stale_fingerprint`;
    3. a dispatch that left the frozen vector is a `deviated_receipt`: an extra
       observation about the dispatch, never evidence against a contract it
       declined to honour;
    4. only then, and only for an exact/equivalent fresh receipt, may a typed
       direct falsifier fire — `falsifier_<predicate_id>`, the one contradicting
       shape in the vocabulary;
    5. otherwise the exit code decides between `expectation_met` and
       `expectation_unmet`.

    `dispatch_status` and `error_code` are the TYPED facts the facade already
    holds (the invocation status and the tool's own error code). Raw failure
    text is diagnostics, never the routing authority (spec §C6).

    Pure: no I/O, no clock, no probes — the same evidence always yields the same
    verdict, which is what makes the persisted assessment replayable.
    """
    pins = _pinned_fingerprints(contract, receipt)
    identifier = _text((receipt or {}).get("receipt_id"))

    def verdict(typed_code: str, detail: str) -> ReceiptAssessment:
        return ReceiptAssessment(
            receipt_id=identifier,
            typed_code=typed_code,
            detail=detail,
            fingerprints=pins or None,
        )

    binding_problem = contract_receipt_binding_problem(contract, receipt)
    if binding_problem:
        return verdict(CONTRACT_BINDING_UNKNOWN, binding_problem)

    assert isinstance(contract, Mapping)
    current_problem = _current_contract_binding_problem(contract, current_fingerprints)
    if current_problem:
        return verdict(CONTRACT_BINDING_UNKNOWN, current_problem)

    blocked = _blocked_class(receipt, dispatch_status, error_code)
    if blocked is not None:
        return verdict(*blocked)

    stale = _stale_pin(pins, current_fingerprints)
    if stale is not None:
        key, pinned, current = stale
        return verdict(
            STALE_FINGERPRINT,
            f"the contract pinned {key}={pinned}; the current {key} is {current}",
        )

    compliance = _text((receipt or {}).get("compliance"))
    if compliance == "deviated":
        return verdict(
            DEVIATED_RECEIPT,
            f"the dispatch ran {_text((receipt or {}).get('argv'))!r} instead of the "
            f"frozen {_text((contract or {}).get('expected_argv'))!r}",
        )

    exit_code = _exit_code(receipt)
    promised = _expected_observations(contract)
    stated = ", ".join(promised) if promised else "the contracted operation"
    if exit_code != 0:
        return verdict(EXPECTATION_UNMET, f"exit {exit_code} against the expected {stated}")

    semantic = _operation_semantic_result(contract, receipt, current_fingerprints)
    if semantic == "met":
        return verdict(EXPECTATION_MET, f"typed evidence satisfied {stated}")
    if semantic.startswith("falsifier:"):
        predicate = semantic.split(":", 1)[1]
        return verdict(
            f"{FALSIFIER_PREFIX}{predicate}",
            f"exit 0 produced none of the expected {stated}",
        )
    return verdict(
        EXPECTATION_UNOBSERVED,
        f"exit 0 did not carry the typed positive evidence required for {stated}",
    )


def capability_absences(receipt: Optional[Mapping[str, Any]]) -> List[ReceiptAssessment]:
    """`capability_absent_<name>` for every capability a skip reason revealed.

    These ride ALONGSIDE the primary verdict rather than replacing it: a test
    suite that skipped its LLVM cases still passed the cases it ran, and the
    skip reason is the only place the environment says the capability is
    missing at all. One assessment per NAME, in the table's own order, so the
    same receipt always yields the same list.
    """
    identifier = _text((receipt or {}).get("receipt_id"))
    reasons = _skip_reasons(receipt)
    if not identifier or not reasons:
        return []
    absences: List[ReceiptAssessment] = []
    for entry in CAPABILITY_PATTERNS:
        name = _text(entry.get("name"))
        pattern = str(entry.get("pattern") or "")
        if not name or not pattern:
            continue
        try:
            matcher = re.compile(pattern)
        except re.error:
            logger.debug(f"capability pattern for {name} is not a valid expression")
            continue
        hit = next(((node, reason) for node, reason in reasons if matcher.search(reason)), None)
        if hit is None:
            continue
        node, reason = hit
        absences.append(
            ReceiptAssessment(
                receipt_id=identifier,
                typed_code=f"{CAPABILITY_PREFIX}{name}",
                detail=f"{node} was skipped: {reason}",
            )
        )
    return absences


def prerequisite_assessments(
    receipt: Optional[Mapping[str, Any]],
    output: Optional[str] = None,
    *,
    evidence_ref: Optional[str] = None,
) -> List[ReceiptAssessment]:
    """Mechanical missing-executable/service riders for one receipt.

    Sources are deliberately limited to FAILED/ERROR testcase reasons already
    bound to the receipt and candidate lines from the complete runner output.
    Project names, ports, and ecosystem defaults never supply a service name.
    The observations carry ``blocker_owner=unknown``: seeing a prerequisite
    boundary is not authority to install a package or start a service.

    The primary receipt assessment is not read or changed here.  In particular,
    an ``expectation_unmet`` remains red when either rider is also present.
    """
    identifier = _text((receipt or {}).get("receipt_id"))
    if not identifier:
        return []
    scope = _prerequisite_scope(receipt)
    output_ref = _text(evidence_ref) or _text((receipt or {}).get("output_ref")) or identifier
    fragments: List[Tuple[str, str, str]] = [
        (f"testcase {node_id}" if node_id else "failed testcase", reason, identifier)
        for node_id, reason in _failure_reasons(receipt)[:PREREQUISITE_OUTPUT_CANDIDATE_CAP]
    ]
    fragments.extend(
        ("runner output", line, output_ref) for line in _prerequisite_output_candidates(output)
    )

    executable_hits: List[Tuple[str, str, str, str]] = []
    service_hits: List[Tuple[str, str, str, str, str]] = []
    seen_executables = set()
    endpoint_positions: Dict[Tuple[str, str], int] = {}
    for source, fragment, source_ref in fragments:
        for name in _missing_executables(fragment):
            identity = (name, scope)
            if identity in seen_executables:
                continue
            seen_executables.add(identity)
            executable_hits.append((name, scope, source_ref, source))
        for endpoint, service_hint in _unavailable_services(fragment):
            identity = (endpoint, scope)
            existing_position = endpoint_positions.get(identity)
            if existing_position is not None:
                existing = service_hits[existing_position]
                # The receipt reason may name only the endpoint while its
                # complete output explicitly labels the service. Prefer the
                # stronger original evidence without inventing a hint.
                if not existing[1] and service_hint:
                    service_hits[existing_position] = (
                        endpoint,
                        service_hint,
                        scope,
                        source_ref,
                        source,
                    )
                continue
            endpoint_positions[identity] = len(service_hits)
            service_hits.append((endpoint, service_hint, scope, source_ref, source))

    findings: List[ReceiptAssessment] = []
    for name, hit_scope, source_ref, source in executable_hits[:PREREQUISITE_FINDING_CAP]:
        findings.append(
            ReceiptAssessment(
                receipt_id=identifier,
                typed_code=PREREQUISITE_EXECUTABLE_MISSING,
                blocker_owner=BlockerOwner.UNKNOWN,
                name=name,
                scope=hit_scope,
                evidence_ref=source_ref,
                detail=f"{source} reported missing executable {name}",
            )
        )
    remaining = max(0, PREREQUISITE_FINDING_CAP - len(findings))
    for endpoint, hint, hit_scope, source_ref, source in service_hits[:remaining]:
        findings.append(
            ReceiptAssessment(
                receipt_id=identifier,
                typed_code=PREREQUISITE_SERVICE_UNAVAILABLE,
                blocker_owner=BlockerOwner.UNKNOWN,
                endpoint=endpoint,
                service_hint=hint or None,
                scope=hit_scope,
                evidence_ref=source_ref,
                detail=f"{source} reported connection refused at {endpoint}",
            )
        )
    return findings


def _prerequisite_scope(receipt: Optional[Mapping[str, Any]]) -> str:
    """The narrowest scope the receipt itself stated, never a project guess."""
    for key in ("domain_id", "actual_cwd", "working_directory"):
        value = _bounded_field((receipt or {}).get(key))
        if value:
            return value
    return "unknown"


def _prerequisite_output_candidates(output: Optional[str]) -> List[str]:
    """Bounded candidate windows sampled from the complete runner output.

    Keep the first and last halves when more than the cap match.  This covers
    setup failures near the start and terminal diagnostics near the end while
    bounding every downstream regex and persisted detail.
    """
    text = str(output or "")
    if not text:
        return []
    half = max(1, PREREQUISITE_OUTPUT_CANDIDATE_CAP // 2)
    first: List[str] = []
    last: List[str] = []
    triggers = (
        "command not found",
        ": not found",
        "no such executable",
        "cannot run program",
        "connection refused",
        "connectionrefusederror",
        "econnrefused",
    )
    for raw_line in text.splitlines():
        lowered = raw_line.lower()
        positions = [lowered.find(trigger) for trigger in triggers]
        positions = [position for position in positions if position >= 0]
        if not positions:
            continue
        anchor = min(positions)
        half_line = PREREQUISITE_OUTPUT_LINE_MAX_CHARS // 2
        start = max(0, anchor - half_line)
        line = raw_line[start : start + PREREQUISITE_OUTPUT_LINE_MAX_CHARS]
        if len(first) < half:
            first.append(line)
            continue
        last.append(line)
        if len(last) > PREREQUISITE_OUTPUT_CANDIDATE_CAP - half:
            last.pop(0)
    return first + last


def _missing_executables(text: str) -> List[str]:
    """Normalized executable tokens explicitly named by one evidence line."""
    names: List[str] = []
    for pattern in (
        _COMMAND_NOT_FOUND_RE,
        _SHELL_NOT_FOUND_RE,
        _NO_SUCH_EXECUTABLE_AFTER_RE,
        _NO_SUCH_EXECUTABLE_BEFORE_RE,
        _CANNOT_RUN_PROGRAM_RE,
    ):
        for match in pattern.finditer(text):
            name = _normalize_executable(match.group("name"))
            if name and name not in names:
                names.append(name)
    return names


def _normalize_executable(value: Any) -> str:
    candidate = _text(value).strip("`'\".,;:()[]{}")
    candidate = candidate.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]{0,127}", candidate):
        return ""
    # ``RuntimeError: No such executable: ccm`` contains the same punctuation
    # as the before-form, but the exception class is a label, not a command.
    if candidate.lower().endswith(("error", "exception")):
        return ""
    return candidate


def _unavailable_services(text: str) -> List[Tuple[str, str]]:
    """Endpoints on an explicit refusal line, plus only evidence-stated hints."""
    if not _CONNECTION_REFUSED_RE.search(text):
        return []
    service_hint = _service_hint(text)
    observations: List[Tuple[str, str]] = []
    for match in _ENDPOINT_RE.finditer(text):
        endpoint = _normalize_endpoint(match.group(0))
        if endpoint and (endpoint, service_hint) not in observations:
            observations.append((endpoint, service_hint))
    return observations


def _normalize_endpoint(value: Any) -> str:
    candidate = _text(value)
    match = _ENDPOINT_RE.fullmatch(candidate)
    if not match:
        return ""
    host = match.group("host").lower()
    try:
        port = int(match.group("port"))
    except (TypeError, ValueError):
        return ""
    if not 0 < port <= 65_535:
        return ""
    plain_host = host.strip("[]")
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", plain_host):
        if any(int(part) > 255 for part in plain_host.split(".")):
            return ""
    return f"{host}:{port}"


def _service_hint(text: str) -> str:
    """An explicit service label or URI scheme from the same evidence line."""
    stopwords = {"a", "local", "remote", "target", "the", "this", "upstream"}
    for pattern in _SERVICE_HINT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        hint = _text(match.group("hint")).lower()
        if hint and hint not in stopwords and hint not in ("http", "https", "tcp"):
            return hint
    return ""


def read_receipt(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt_id: Any,
) -> Optional[Dict[str, Any]]:
    """The finalized receipt `receipt_id`, or None when it cannot be read.

    The assessor works from the persisted bytes, not from whatever the runner
    happened to keep in memory: an assessment of a receipt nobody can read
    would be a verdict about nothing.
    """
    if not isinstance(receipt_id, str):
        return None
    identifier = receipt_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", identifier):
        return None

    def binding(payload: Mapping[str, Any]) -> EvidencePublicationBinding:
        return EvidencePublicationBinding(
            run_id=str(payload["run_id"]),
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
        )

    read = read_live_published_json_records(
        execute,
        RECEIPT_DIR,
        record_kind="invocation_receipt",
        validator=lambda payload, expected_id: validate_receipt_v2(
            payload,
            expected_id=expected_id,
        ),
        publication_binding=binding,
        record_scope=receipt_record_scope,
    )
    if not read.complete or read.conflict is not None:
        logger.debug(
            f"invocation receipt ledger unavailable for {identifier}: "
            f"{read.conflict or read.detail}"
        )
        return None
    for record in read.records:
        if record.source.record_id == identifier:
            return dict(record.payload)
    return None


def assess_dispatch(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
    current_fingerprints: Optional[Mapping[str, str]] = None,
    dispatch_status: Optional[str] = None,
    error_code: Optional[str] = None,
    output: Optional[str] = None,
    evidence_ref: Optional[str] = None,
) -> List[ReceiptAssessment]:
    """Assess ONE dispatch and persist every verdict; return the ones that landed.

    The primary verdict first, then any structured prerequisite and other
    riders. Persistence is idempotent (`write_assessment`), so re-assessing the
    same receipt — a replay, a second pass over the same execution trace —
    writes nothing new.

    `output` is the dispatch's complete runner output when the caller still
    holds it. The receipt keeps only a hash of that text, so a fault the build
    stated in prose — a java version mismatch, say — is readable here and
    nowhere else. `evidence_ref`, when supplied, is the durable reference for
    that complete text; otherwise prerequisite riders cite their receipt.
    """
    if not _text((receipt or {}).get("receipt_id")):
        return []
    assessments = [
        assess_receipt(
            contract,
            receipt,
            current_fingerprints=current_fingerprints,
            dispatch_status=dispatch_status,
            error_code=error_code,
        )
    ]
    assessments.extend(prerequisite_assessments(receipt, output, evidence_ref=evidence_ref))
    assessments.extend(capability_absences(receipt))
    assessments.extend(dependency_incompatibilities(receipt))
    assessments.extend(java_version_mismatch(receipt, output))
    return [assessment for assessment in assessments if write_assessment(execute, assessment)]


def java_version_mismatch(
    receipt: Optional[Mapping[str, Any]],
    output: Optional[str],
) -> List[ReceiptAssessment]:
    """`java_version_mismatch` when the build stated both majors it disagreed on.

    Rides alongside the primary verdict like the capability and dependency
    findings. Both majors must be present and different: one alone, or two that
    agree, is not a mismatch, and inferring the missing half would be the
    harness inventing a requirement the build never stated.
    """
    identifier = _text((receipt or {}).get("receipt_id"))
    text = str(output or "")
    if not identifier or not text:
        return []
    for row in JAVA_MISMATCH_PATTERNS:
        required = _first_major(row.get("required"), text)
        detected = _first_major(row.get("detected"), text)
        if required is None or detected is None or required == detected:
            continue
        return [
            ReceiptAssessment(
                receipt_id=identifier,
                typed_code=JAVA_MISMATCH_CODE,
                detail=f"build requires java {required}, ran under java {detected}",
            )
        ]
    return []


def _first_major(pattern: Any, text: str) -> Optional[int]:
    """The first java major a pattern finds, or None when it finds none."""
    expression = str(pattern or "")
    if not expression:
        return None
    try:
        match = re.search(expression, text, re.IGNORECASE)
    except re.error:
        logger.debug(f"java mismatch pattern is not a valid expression: {expression!r}")
        return None
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


# Spec §5 S2: a FAILED testcase whose message matches a known dependency-
# mismatch shape emits its own distinct typed code, so targeted retrieval can
# route to dependency metadata instead of the module docs. Data, not project
# names — extend by adding rows.
DEPENDENCY_FAILURE_PATTERNS = (
    {"name": "numpy", "pattern": r"NumPy dtype|numpy\.dtype|numpy dtype"},
)
DEPENDENCY_PREFIX = "dependency_incompatible_"

# Plan 7 round two: a build that states BOTH the java it needs and the java it
# got has diagnosed itself, and that statement is the strongest provenance
# there is — the runner said it, in its own output. Live p7-polaris: Gradle
# printed "requires Java 21." / "Detected Java version: 17"; live p7-camel: the
# wrapper ran under 17 against a build needing 17+. In both runs the model read
# the sentence and closed the phase without provisioning, because no typed code
# named the failure and so no repair could be proposed for it.
#
# Each row needs BOTH majors. A pattern that finds only one is not a mismatch —
# guessing the other half is how a harness invents a requirement.
JAVA_MISMATCH_PATTERNS = (
    {
        # Maven Enforcer's current wording puts both physical facts on one
        # line: "Required Java version 17 is not met by current version 11".
        "required": r"Required\s+Java\s+version\s+(\d+)",
        "detected": r"current\s+version\s+(\d+)",
    },
    {
        # Gradle: "... requires Java 21.\n Detected Java version: 17"
        "required": r"requires\s+Java\s+(?:version\s+)?(\d+)",
        "detected": r"Detected\s+Java\s+version\s*:?\s*(\d+)",
    },
    {
        # Maven Enforcer RequireJavaVersion: "Detected JDK Version: 11.0.22 is
        # not in the allowed range [17,)."
        "required": r"allowed\s+range\s+[\[\(]\s*(\d+)",
        "detected": r"Detected\s+JDK\s+Version\s*:?\s*(\d+)",
    },
    {
        # javac / toolchain: "release version 21 not supported" against a
        # stated current version.
        "required": r"(?:release|target)\s+version\s+(\d+)\s+not\s+supported",
        "detected": r"(?:java|jdk)\s+version\s*[\":]*\s*(\d+)",
    },
)
JAVA_MISMATCH_CODE = "java_version_mismatch"


def dependency_incompatibilities(
    receipt: Optional[Mapping[str, Any]],
) -> List[ReceiptAssessment]:
    """`dependency_incompatible_<name>` for failure reasons the table names.

    Rides alongside the primary verdict exactly like `capability_absences`:
    the distinct code is what lets the R2 repair chain start from dependency
    metadata (live TVM S2: `ValueError: Could not convert T.float32 to a
    NumPy dtype` after the LLVM rebuild made execution real).
    """
    identifier = _text((receipt or {}).get("receipt_id"))
    reasons = _failure_reasons(receipt)
    if not identifier or not reasons:
        return []
    findings: List[ReceiptAssessment] = []
    for entry in DEPENDENCY_FAILURE_PATTERNS:
        name = _text(entry.get("name"))
        pattern = str(entry.get("pattern") or "")
        if not name or not pattern:
            continue
        try:
            matcher = re.compile(pattern)
        except re.error:
            logger.debug(f"dependency pattern for {name} is not a valid expression")
            continue
        hit = next(((node, reason) for node, reason in reasons if matcher.search(reason)), None)
        if hit is None:
            continue
        node_id, reason = hit
        findings.append(
            ReceiptAssessment(
                receipt_id=identifier,
                typed_code=f"{DEPENDENCY_PREFIX}{name}",
                detail=f"{node_id}: {reason}"[:400],
            )
        )
    return findings


def _failure_reasons(receipt: Optional[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """`(node_id, reason)` for every FAILED/ERROR testcase with a message."""
    outcomes = (receipt or {}).get("testcase_outcomes")
    nodes = outcomes.get("nodes") if isinstance(outcomes, Mapping) else None
    if not isinstance(nodes, (list, tuple)):
        return []
    reasons: List[Tuple[str, str]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or _text(node.get("status")) not in ("failed", "error"):
            continue
        reason = _text(node.get("reason"))
        if reason:
            reasons.append((_text(node.get("node_id")), reason))
    return reasons


def _blocked_class(
    receipt: Optional[Mapping[str, Any]],
    dispatch_status: Optional[str],
    error_code: Optional[str],
) -> Optional[Tuple[str, str]]:
    """The blocked-class code this dispatch earned, or None when none applies."""
    status = _text(dispatch_status).lower()
    if status in DISPATCH_STATUS_CODES:
        return DISPATCH_STATUS_CODES[status], f"the dispatch ended as {status}"
    if _exit_code(receipt) is None:
        return "no_dispatch", "the receipt records no exit state for this dispatch"
    code = _text(error_code).upper()
    typed = BLOCKED_CLASS_ERROR_CODES.get(code)
    if typed:
        return typed, f"the runner reported {code}"
    return None


def _stale_pin(
    pinned: Mapping[str, str],
    current: Optional[Mapping[str, str]],
) -> Optional[Tuple[str, str, str]]:
    """The first pin the contract and the present disagree about.

    A pin only one side states is UNKNOWN, never a mismatch — calling it stale
    would invent a disagreement neither side ever expressed.
    """
    for key in FINGERPRINT_KEYS:
        was = _text(pinned.get(key))
        now = _text((current or {}).get(key))
        if was and now and was != now:
            return key, was, now
    return None


def _falsified_predicate(
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """The `predicate_id` this receipt establishes, or None (v1: one predicate).

    `delta_empty_on_exit0`: the runner said success and left nothing behind.
    For a verb that promises an artifact OR a report delta, the artifact side
    must be an EXPLICIT absence — a receipt that says nothing about artifacts
    states an unknown, and an unknown never contradicts (spec §C5). So a green
    compile whose receipt carries no artifact facts is not falsified; when the
    receipt gains an artifact delta, the predicate covers it with no change
    here.
    """
    observations = _expected_observations(contract)
    if not observations:
        return None
    if _exit_code(receipt) != 0:
        return None
    if _delta_is_empty((receipt or {}).get("report_delta")) is not True:
        return None
    if "artifact_or_report_delta" in observations:
        if _delta_is_empty((receipt or {}).get("artifact_delta")) is not True:
            return None
    for falsifier in (contract or {}).get("direct_falsifiers") or ():
        if not isinstance(falsifier, Mapping):
            continue
        if _text(falsifier.get("kind")) != "delta_empty_on_exit0":
            continue
        predicate = _text(falsifier.get("predicate_id"))
        if predicate:
            return predicate
    return None


def _expected_observations(contract: Optional[Mapping[str, Any]]) -> List[str]:
    raw = (contract or {}).get("expected_observations")
    if not isinstance(raw, (list, tuple)):
        return []
    return [text for text in (_text(value) for value in raw) if text]


def _pinned_fingerprints(
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """The pins this dispatch was decided on; the CONTRACT's pin wins.

    The contract is the commitment, so where both state a pin the contract's is
    the one the harness promised against.
    """
    pins: Dict[str, str] = {}
    for source in (receipt, contract):
        for key in FINGERPRINT_KEYS:
            value = _text((source or {}).get(key))
            if value:
                pins[key] = value
    return pins


def _skip_reasons(receipt: Optional[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """`(node_id, reason)` for every SKIPPED testcase the receipt recorded."""
    outcomes = (receipt or {}).get("testcase_outcomes")
    nodes = outcomes.get("nodes") if isinstance(outcomes, Mapping) else None
    if not isinstance(nodes, (list, tuple)):
        return []
    reasons: List[Tuple[str, str]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or _text(node.get("status")) != "skipped":
            continue
        reason = _text(node.get("reason"))
        if reason:
            reasons.append((_text(node.get("node_id")), reason))
    return reasons


def _delta_is_empty(delta: Any) -> Optional[bool]:
    """True/False when the delta states its lists; None when it states nothing."""
    if not isinstance(delta, Mapping):
        return None
    buckets: List[Sequence[Any]] = []
    for key in ("new", "changed"):
        value = delta.get(key)
        if not isinstance(value, (list, tuple)):
            return None
        buckets.append(value)
    return not any(buckets)


def _exit_code(receipt: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The exit state the receipt recorded; None when it recorded none."""
    value = (receipt or {}).get("exit_code")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _read_existing_raw(execute, path: str) -> Optional[str]:
    """Exact existing assessment bytes as text, or None when not observed.

    Callers compare this byte-for-byte with their canonical body. Parsing here
    would let pretty JSON or duplicate keys masquerade as an idempotent replay
    and acquire host publication authority they never had.
    """
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"evidence assessment {path} unreadable: {exc}")
        return None
    content = str(result.get("output") or "")
    if not _succeeded(result) or not content.strip():
        return None
    return content


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


def _bounded(detail: Any) -> str:
    return " ".join(str(detail or "").split())[:DETAIL_MAX_CHARS]


def _bounded_field(value: Any) -> str:
    return " ".join(str(value or "").split())[:PREREQUISITE_FIELD_MAX_CHARS]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")
