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
import re
import shlex
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.invocation_receipts import RECEIPT_DIR

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
    "config_fingerprint",
    "document_map_fingerprint",
    "survey_fingerprint",
)
# The named capabilities a skip reason can reveal as absent. PATTERNS, not
# project names: the table is data, an ecosystem adds a row, and the assessor
# never learns what a project is called.
CAPABILITY_PATTERNS = (
    {"name": "llvm", "pattern": "need llvm|LLVM"},
    {"name": "cuda", "pattern": "CUDA"},
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


# ---------------------------------------------------------------------------
# the assessor: one contract, one receipt, one typed verdict (spec §C5)
# ---------------------------------------------------------------------------


def ensure_receipt_assessed(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt_id: Any,
) -> bool:
    """Backstop: assess a dispatched receipt no facade path assessed.

    Live p6v-bigtop-r3: the tool-recovery delegate freezes its own fallback
    contract, but only the build facade ran the assessor — the recovery
    dispatch's receipt had a contract and no verdict. This runs at the engine
    observation seam for exactly that gap; the write is idempotent, so a
    facade-assessed receipt is a no-op. Never raises.
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
        from sag.agent.retry_authority import read_frozen_contract

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

    promised = _expected_observations(contract)
    stated = ", ".join(promised) if promised else "no stated observation"
    if compliance in COMPLIANT_CLASSES:
        predicate = _falsified_predicate(contract, receipt)
        if predicate:
            return verdict(
                f"{FALSIFIER_PREFIX}{predicate}",
                f"exit 0 produced none of the expected {stated}",
            )

    exit_code = _exit_code(receipt)
    if exit_code == 0:
        return verdict(EXPECTATION_MET, f"exit 0; nothing contradicts the expected {stated}")
    return verdict(EXPECTATION_UNMET, f"exit {exit_code} against the expected {stated}")


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


def read_receipt(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt_id: Any,
) -> Optional[Dict[str, Any]]:
    """The finalized receipt `receipt_id`, or None when it cannot be read.

    The assessor works from the persisted bytes, not from whatever the runner
    happened to keep in memory: an assessment of a receipt nobody can read
    would be a verdict about nothing.
    """
    identifier = _text(receipt_id)
    if not identifier:
        return None
    payload = _read_existing(execute, f"{RECEIPT_DIR}/{identifier}.json")
    if not isinstance(payload, dict) or "unparseable" in payload:
        return None
    return payload


def assess_dispatch(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    contract: Optional[Mapping[str, Any]],
    receipt: Optional[Mapping[str, Any]],
    current_fingerprints: Optional[Mapping[str, str]] = None,
    dispatch_status: Optional[str] = None,
    error_code: Optional[str] = None,
    output: Optional[str] = None,
) -> List[ReceiptAssessment]:
    """Assess ONE dispatch and persist every verdict; return the ones that landed.

    The primary verdict first, then any capability absence. Persistence is
    idempotent (`write_assessment`), so re-assessing the same receipt — a
    replay, a second pass over the same execution trace — writes nothing new.

    `output` is the dispatch's complete runner output when the caller still
    holds it. The receipt keeps only a hash of that text, so a fault the build
    stated in prose — a java version mismatch, say — is readable here and
    nowhere else.
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


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")
