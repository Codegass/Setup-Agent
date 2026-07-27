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
import shlex
import threading
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from loguru import logger

ASSESSMENT_SCHEMA_VERSION = 1
ASSESSMENT_DIR = "/workspace/.setup_agent/evidence_assessments"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# assessment content can ever collide with it.
ASSESSMENT_HEREDOC = "SAGASSESSMENT"
DETAIL_MAX_CHARS = 200
SUBJECT_SLUG_MAX_CHARS = 48
CODE_SLUG_MAX_CHARS = 40

# The typed stages a control assessment may name (spec §C4). A stage outside
# this set is a programming error, not a fact about the run, so it is refused
# rather than persisted.
CONTROL_STAGES = ("precondition", "materialization", "envelope", "dispatch")

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

    @property
    def subject_id(self) -> str:
        return str(self.receipt_id or "").strip()

    @property
    def assessment_id(self) -> str:
        return assessment_id(self.subject_id, str(self.typed_code or "").strip())

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "receipt_id": self.subject_id,
            "typed_code": str(self.typed_code or "").strip(),
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

    @property
    def subject_id(self) -> str:
        return str(self.event_or_intent_id or "").strip()

    @property
    def assessment_id(self) -> str:
        return assessment_id(self.subject_id, str(self.typed_code or "").strip())

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "event_or_intent_id": self.subject_id,
            "stage": str(self.stage or "").strip(),
            "typed_code": str(self.typed_code or "").strip(),
        }
        detail = _bounded(self.detail)
        if detail:
            body["detail"] = detail
        return body


def write_assessment(execute, assessment) -> bool:
    """Append one assessment atomically; True when the file holds this body.

    Append-only means three things here: the same body under an existing id is
    a no-op success (a replay must not double-write), a DIFFERENT body under an
    existing id is refused and logged (an id is a claim about identity, so a
    collision is a defect to see, not to resolve silently), and the write
    itself is temp-file + `mv` so no reader ever sees half an assessment.
    """
    subject = assessment.subject_id
    code = str(getattr(assessment, "typed_code", "") or "").strip()
    if not subject or not code:
        return False
    payload = assessment.payload()
    if isinstance(assessment, ControlAssessment) and payload["stage"] not in CONTROL_STAGES:
        logger.debug(f"control assessment for {subject} names no typed stage; not persisted")
        return False
    identifier = payload["assessment_id"]
    try:
        body = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"evidence assessment {identifier} is not serializable: {exc}")
        return False
    final = f"{ASSESSMENT_DIR}/{identifier}.json"
    existing = _read_existing(execute, final)
    if existing is not None:
        if existing == payload:
            return True
        logger.warning(
            f"evidence assessment {identifier} already records a different body; "
            "assessments are append-only and this write was refused"
        )
        return False
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(ASSESSMENT_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{ASSESSMENT_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{ASSESSMENT_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"evidence assessment {identifier} not persisted: {exc}")
        return False
    return _succeeded(result)


def _read_existing(execute, path: str) -> Optional[Dict[str, Any]]:
    """The assessment already at `path`, or None when there is none to honour.

    An unparseable file is reported as a body that matches nothing, so the
    caller refuses instead of overwriting bytes it cannot account for.
    """
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"evidence assessment {path} unreadable: {exc}")
        return None
    content = str(result.get("output") or "").strip()
    if not _succeeded(result) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {"unparseable": path}
    return payload if isinstance(payload, dict) else {"unparseable": path}


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


def _bounded(detail: Any) -> str:
    return " ".join(str(detail or "").split())[:DETAIL_MAX_CHARS]


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")
