"""The job obligations ledger (Plan 8 Stage 1, spec §3.1).

The receipt system carried an unstated assumption: dispatch -> terminal exit ->
receipt -> assessment -> claim, all inside one tool call. A dispatch that
outlives the 900-second soft window breaks it — the work continues, the call
returns — and both runners answered by throwing the evidence away at one
explicit line:

    # gradle_tool.py:682, maven_tool.py:1061
    if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
        return

p7d polaris (`logs/session_20260729_111737_22356`) wrote exactly ONE receipt
for the whole run — `inv-gradle-1-0001`, the failed Java-17 compile, exit 1.
The successful Java-21 retry detached and left no receipt; the test job
detached and left no receipt; 321 tests ran, all passed, and nothing could
claim them. p7d camel (`logs/session_20260729_111740_22389`) is the same shape
at 11,492 tests.

Nothing was missing at that seam. The `before` snapshot (taken by
`snapshot_reports` before `_run_build`), the facade-frozen contract returned by
the side-effect-free `ensure_dispatch_contract` lookup, and the detach handle's `log_path` /
`exit_code_path` are all in hand. An OBLIGATION is that evidence written down:

    /workspace/.setup_agent/job_obligations/<job_id>.json

Persistence uses the shared bounded atomic transport. The dispatch identity is
immutable; only the explicit process/settlement lifecycle may move forward.
The same body under an existing id is a no-op success, and every non-monotonic
or identity-changing rewrite is refused. This module never raises.
"""

import hashlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from sag.runtime.container_io import resolve_control_execute
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
)

from .control_events import EVIDENCE_PUBLICATION_GENESIS_SHA256
from .evidence_assessments import ensure_receipt_assessed
from .evidence_publications import (
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_revision,
    verify_latest_evidence_bytes,
)
from .evidence_records import (
    EvidencePublicationBinding,
    read_live_published_json_records,
)
from .invocation_contracts import contract_receipt_fields
from .invocation_receipts import (
    RECEIPT_DIR,
    active_receipt_run_id,
    nearest_domain_root,
    next_receipt_id,
    next_sequence,
    receipt_record_scope,
    record_invocation,
    report_delta,
    snapshot_reports,
    survey_pins,
    validate_receipt_v2,
)
from .project_execution_plan import sealed_test_disposition_status

OBLIGATION_SCHEMA_VERSION = 3
OBLIGATION_DIR = "/workspace/.setup_agent/job_obligations"
DETACHED_TERMINAL_AUTHORITY = "docker_exec_inspect_v1"

PROCESS_RUNNING = "running"
PROCESS_TERMINAL = "terminal"
SETTLEMENT_NONE = "none"
SETTLEMENT_PENDING = "pending"
SETTLEMENT_SETTLED = "settled"
SETTLEMENT_UNPERSISTED = "unpersisted"
MAX_SETTLEMENT_PERSIST_ATTEMPTS = 2
OBLIGATION_MAX_CANONICAL_BYTES = 64 * 1024 * 1024
OBLIGATION_MAX_BEFORE_ENTRIES = 100_000
OBLIGATION_MAX_TEXT_BYTES = 1 << 20
OBLIGATION_WRITE_RETRIES = 3
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SAFE_RECEIPT_ID = re.compile(r"inv-[A-Za-z0-9._-]{1,190}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_OBLIGATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "run_id",
        "tool",
        "attempt",
        "requested_action",
        "effective_action",
        "argv",
        "working_directory",
        "before",
        "log_path",
        "exit_code_path",
        "terminal_authority",
        "docker_exec_id",
        "container_id",
        "start_accepted",
        "startup_identity_verified",
        "runner_dispatch_state",
        "pid",
        "pgid",
        "pid_path",
        "pgid_path",
        "identity_path",
        "process_identity_token",
        "process_state",
        "settlement_state",
        "terminal_exit_code",
        "terminal_marker_ref",
        "terminal_observed_at",
        "settlement_attempts",
        "attempted_receipt_id",
        "receipt_persistence_code",
        "settled_receipt_id",
    }
)
_OBLIGATION_OPTIONAL_FIELDS = frozenset(
    {
        "handoff_reason",
        "contract_id",
        "contract_hash",
        "execution_binding",
        "compliance",
        "requirements_pins",
        "domain_id",
        "dispatch_sequence",
        "effective_jdk",
    }
)
_OBLIGATION_FIELDS = _OBLIGATION_REQUIRED_FIELDS | _OBLIGATION_OPTIONAL_FIELDS

_LEGACY_OBLIGATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "tool",
        "attempt",
        "requested_action",
        "effective_action",
        "argv",
        "working_directory",
        "before",
        "log_path",
        "exit_code_path",
        "settled_receipt_id",
    }
)
_LEGACY_OBLIGATION_OPTIONAL_FIELDS = frozenset(
    {
        "contract_id",
        "contract_hash",
        "execution_binding",
        "compliance",
        "dispatch_sequence",
        "effective_jdk",
    }
)

_LIFECYCLE_FIELDS = {
    "process_state",
    "settlement_state",
    "terminal_exit_code",
    "terminal_marker_ref",
    "terminal_observed_at",
    "settlement_attempts",
    "attempted_receipt_id",
    "receipt_persistence_code",
    "settled_receipt_id",
}


def _obligation_text(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    maximum_bytes: int = OBLIGATION_MAX_TEXT_BYTES,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"job obligation {field} must be canonical text")
    if not value and not allow_empty:
        raise ValueError(f"job obligation {field} must be non-empty")
    if len(value.encode("utf-8")) > maximum_bytes or "\x00" in value:
        raise ValueError(f"job obligation {field} is out of bounds")
    return value


def _validate_obligation_path(value: Any, field: str) -> str:
    path = _obligation_text(value, field)
    if not path.startswith("/") or posixpath.normpath(path) != path:
        raise ValueError(f"job obligation {field} must be absolute and canonical")
    return path


def _validate_before_snapshot(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > OBLIGATION_MAX_BEFORE_ENTRIES:
        raise ValueError("job obligation before snapshot is invalid")
    normalized: Dict[str, str] = {}
    for raw_path, raw_digest in value.items():
        path = _validate_obligation_path(raw_path, "before path")
        digest = _obligation_text(raw_digest, "before digest", maximum_bytes=64)
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("job obligation before digest is not sha256")
        normalized[path] = digest
    if normalized != dict(value):
        raise ValueError("job obligation before snapshot is not canonical")
    return normalized


def _validate_effective_jdk(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("job obligation effective_jdk is invalid")
    allowed = {
        "major",
        "requirement_major",
        "requirement_authority",
        "runtime_authority",
        "provenance",
    }
    if set(value) - allowed:
        raise ValueError("job obligation effective_jdk has unknown fields")
    normalized: Dict[str, Any] = {}
    for key, item in value.items():
        if key == "provenance":
            if not isinstance(item, Mapping) or not item:
                raise ValueError("job obligation effective_jdk provenance is invalid")
            json.dumps(item, allow_nan=False, sort_keys=True)
            normalized[key] = dict(item)
        else:
            normalized[key] = _obligation_text(item, f"effective_jdk.{key}")
    if normalized != dict(value):
        raise ValueError("job obligation effective_jdk is not canonical")
    return normalized


def _validate_contract_tuple(payload: Mapping[str, Any]) -> None:
    fields = {"contract_id", "contract_hash", "execution_binding"}
    present = set(payload).intersection(fields)
    if present not in (set(), fields):
        raise ValueError("job obligation contract binding is incomplete")
    if not present:
        if "compliance" in payload:
            raise ValueError("job obligation compliance has no contract")
        return
    contract_id = _obligation_text(payload.get("contract_id"), "contract_id")
    contract_hash = _obligation_text(payload.get("contract_hash"), "contract_hash")
    binding = _obligation_text(payload.get("execution_binding"), "execution_binding")
    if (
        re.fullmatch(r"ic-[0-9a-f]{12}", contract_id) is None
        or _SHA256.fullmatch(contract_hash) is None
    ):
        raise ValueError("job obligation contract identity is invalid")
    if binding not in {"argv_v1", "python_facade_v1"}:
        raise ValueError("job obligation execution binding is invalid")
    if "compliance" in payload:
        compliance = _obligation_text(payload.get("compliance"), "compliance")
        if binding != "argv_v1" or compliance not in {"exact", "equivalent", "deviated"}:
            raise ValueError("job obligation compliance is invalid")


def _validate_lifecycle(payload: Mapping[str, Any]) -> None:
    process = payload.get("process_state")
    settlement = payload.get("settlement_state")
    attempts = payload.get("settlement_attempts")
    if type(attempts) is not int or attempts < 0 or attempts > MAX_SETTLEMENT_PERSIST_ATTEMPTS:
        raise ValueError("job obligation settlement_attempts is invalid")
    if process not in {PROCESS_RUNNING, PROCESS_TERMINAL}:
        raise ValueError("job obligation process_state is invalid")
    if settlement not in {
        SETTLEMENT_NONE,
        SETTLEMENT_PENDING,
        SETTLEMENT_SETTLED,
        SETTLEMENT_UNPERSISTED,
    }:
        raise ValueError("job obligation settlement_state is invalid")

    terminal_exit = payload.get("terminal_exit_code")
    marker = payload.get("terminal_marker_ref")
    observed = payload.get("terminal_observed_at")
    attempted = payload.get("attempted_receipt_id")
    persistence = payload.get("receipt_persistence_code")
    settled = payload.get("settled_receipt_id")
    nullable = (terminal_exit, marker, observed, attempted, persistence, settled)
    if process == PROCESS_RUNNING:
        if (
            settlement != SETTLEMENT_NONE
            or attempts != 0
            or any(item is not None for item in nullable)
        ):
            raise ValueError("running job obligation carries terminal state")
        return
    if settlement == SETTLEMENT_NONE:
        raise ValueError("terminal job obligation has no settlement state")
    if type(terminal_exit) is not int:
        raise ValueError("terminal job obligation exit code is invalid")
    expected_terminal_ref = f"docker-exec:{payload.get('docker_exec_id')}"
    if _obligation_text(marker, "terminal_marker_ref") != expected_terminal_ref:
        raise ValueError("terminal observation differs from the frozen daemon exec")
    timestamp = _obligation_text(observed, "terminal_observed_at", maximum_bytes=128)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("terminal observation timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("terminal observation timestamp has no timezone")
    if attempted is not None:
        attempted_text = _obligation_text(attempted, "attempted_receipt_id")
        if _SAFE_RECEIPT_ID.fullmatch(attempted_text) is None:
            raise ValueError("attempted receipt id is invalid")
    if persistence is not None:
        _obligation_text(persistence, "receipt_persistence_code")
    if settled is not None:
        settled_text = _obligation_text(settled, "settled_receipt_id")
        if _SAFE_RECEIPT_ID.fullmatch(settled_text) is None:
            raise ValueError("settled receipt id is invalid")
    if settlement == SETTLEMENT_PENDING:
        if settled is not None or (persistence is not None and attempted is None):
            raise ValueError("pending settlement fields are inconsistent")
        return
    if attempts <= 0 or attempted is None:
        raise ValueError("terminal settlement has no attempted receipt")
    if settlement == SETTLEMENT_SETTLED:
        if settled != attempted or persistence is not None:
            raise ValueError("settled obligation fields are inconsistent")
    elif settled is not None or persistence is None:
        raise ValueError("unpersisted obligation fields are inconsistent")


def validate_obligation_v3(
    payload: Mapping[str, Any],
    *,
    expected_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one exact current job lifecycle record or fail closed."""

    if not isinstance(payload, Mapping):
        raise ValueError("job obligation must be an object")
    body = dict(payload)
    if type(body.get("schema_version")) is not int or body.get("schema_version") != (
        OBLIGATION_SCHEMA_VERSION
    ):
        raise ValueError("live job obligation schema must be v3")
    unknown = set(body) - _OBLIGATION_FIELDS
    missing = _OBLIGATION_REQUIRED_FIELDS - set(body)
    if unknown or missing:
        raise ValueError(
            f"job obligation fields are not exact: unknown={unknown} missing={missing}"
        )
    identifier = _obligation_text(body.get("job_id"), "job_id", maximum_bytes=200)
    if _SAFE_JOB_ID.fullmatch(identifier) is None:
        raise ValueError("job obligation id is unsafe")
    if expected_id is not None and identifier != expected_id:
        raise ValueError("job obligation id does not match filename")
    _obligation_text(body.get("run_id"), "run_id", maximum_bytes=256)
    tool = _obligation_text(body.get("tool"), "tool", maximum_bytes=32)
    if tool not in {"maven", "gradle", "python", "bash"}:
        raise ValueError("job obligation tool is not registered")
    if type(body.get("attempt")) is not int or body["attempt"] <= 0:
        raise ValueError("job obligation attempt is invalid")
    for field in ("requested_action", "effective_action"):
        _obligation_text(body.get(field), field)
    _obligation_text(body.get("argv"), "argv", maximum_bytes=1 << 20)
    for field in ("working_directory", "log_path", "exit_code_path"):
        _validate_obligation_path(body.get(field), field)
    _validate_before_snapshot(body.get("before"))
    if body.get("terminal_authority") != DETACHED_TERMINAL_AUTHORITY:
        raise ValueError("job obligation terminal authority is invalid")
    for field in ("docker_exec_id", "container_id"):
        value = _obligation_text(body.get(field), field, maximum_bytes=64)
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"job obligation {field} is invalid")
    if body.get("start_accepted") is not True:
        raise ValueError("job obligation start acceptance is not proven")
    if body.get("startup_identity_verified") is not True:
        raise ValueError("job obligation startup identity is not verified")
    if body.get("runner_dispatch_state") != "accepted":
        raise ValueError("job obligation dispatch state is not accepted")

    integer_fields = {key for key in ("pid", "pgid") if key in body}
    if integer_fields not in (set(), {"pid", "pgid"}):
        raise ValueError("job obligation process identity is incomplete")
    if integer_fields:
        if any(type(body[key]) is not int or body[key] <= 0 for key in integer_fields):
            raise ValueError("job obligation process identity is invalid")
        if body["pid"] != body["pgid"]:
            raise ValueError("job obligation process is not its session leader")
    identity_fields = {"pid_path", "pgid_path", "identity_path", "process_identity_token"}
    present_identity = set(body).intersection(identity_fields)
    if present_identity not in (set(), identity_fields):
        raise ValueError("job obligation process provenance is incomplete")
    if present_identity:
        for field in ("pid_path", "pgid_path", "identity_path"):
            _validate_obligation_path(body.get(field), field)
        token = _obligation_text(
            body.get("process_identity_token"),
            "process_identity_token",
            maximum_bytes=64,
        )
        if _SHA256.fullmatch(token) is None:
            raise ValueError("job obligation process identity token is invalid")
    if "handoff_reason" in body:
        _obligation_text(body.get("handoff_reason"), "handoff_reason")
    _validate_contract_tuple(body)
    if "requirements_pins" in body:
        pins = body.get("requirements_pins")
        if (
            not isinstance(pins, Mapping)
            or not pins
            or set(pins)
            - {
                "survey_fingerprint",
                "config_fingerprint",
                "document_map_fingerprint",
            }
        ):
            raise ValueError("job obligation requirements pins are invalid")
        for key, value in pins.items():
            _obligation_text(value, f"requirements_pins.{key}")
    if "domain_id" in body:
        _obligation_text(body.get("domain_id"), "domain_id")
    if "dispatch_sequence" in body and (
        type(body.get("dispatch_sequence")) is not int or body["dispatch_sequence"] <= 0
    ):
        raise ValueError("job obligation dispatch_sequence is invalid")
    if "effective_jdk" in body:
        _validate_effective_jdk(body.get("effective_jdk"))
    _validate_lifecycle(body)
    canonical = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(canonical.encode("utf-8")) > OBLIGATION_MAX_CANONICAL_BYTES:
        raise ValueError("job obligation exceeds its canonical byte limit")
    return body


def _validate_historical_obligation(
    payload: Mapping[str, Any],
    *,
    expected_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate the exact legacy on-disk shape for forensic classification."""

    if not isinstance(payload, Mapping):
        raise ValueError("historical job obligation must be an object")
    body = dict(payload)
    allowed = _LEGACY_OBLIGATION_REQUIRED_FIELDS | _LEGACY_OBLIGATION_OPTIONAL_FIELDS
    if (
        body.get("schema_version") != 1
        or set(body) - allowed
        or not (_LEGACY_OBLIGATION_REQUIRED_FIELDS <= set(body))
    ):
        raise ValueError("historical job obligation fields are not exact")
    identifier = _obligation_text(body.get("job_id"), "job_id", maximum_bytes=200)
    if _SAFE_JOB_ID.fullmatch(identifier) is None or (
        expected_id is not None and identifier != expected_id
    ):
        raise ValueError("historical job obligation id is invalid")
    tool = _obligation_text(body.get("tool"), "tool", maximum_bytes=32)
    if tool not in {"maven", "gradle", "python", "bash"}:
        raise ValueError("historical job obligation tool is not registered")
    if type(body.get("attempt")) is not int or body["attempt"] <= 0:
        raise ValueError("historical job obligation attempt is invalid")
    for field in ("requested_action", "effective_action"):
        _obligation_text(body.get(field), field)
    _obligation_text(body.get("argv"), "argv", maximum_bytes=1 << 20)
    for field in ("working_directory", "log_path", "exit_code_path"):
        _validate_obligation_path(body.get(field), field)
    _validate_before_snapshot(body.get("before"))
    settled = body.get("settled_receipt_id")
    if (
        settled is not None
        and _SAFE_RECEIPT_ID.fullmatch(_obligation_text(settled, "settled_receipt_id")) is None
    ):
        raise ValueError("historical settled receipt id is invalid")
    if "contract_id" in body:
        contract_id = _obligation_text(body.get("contract_id"), "contract_id")
        if re.fullmatch(r"ic-[0-9a-f]{12}", contract_id) is None:
            raise ValueError("historical contract id is invalid")
    if (
        "contract_hash" in body
        and _SHA256.fullmatch(
            _obligation_text(body.get("contract_hash"), "contract_hash", maximum_bytes=64)
        )
        is None
    ):
        raise ValueError("historical contract hash is invalid")
    if "execution_binding" in body and _obligation_text(
        body.get("execution_binding"), "execution_binding"
    ) not in {"argv_v1", "python_facade_v1"}:
        raise ValueError("historical execution binding is invalid")
    if "compliance" in body and _obligation_text(body.get("compliance"), "compliance") not in {
        "exact",
        "equivalent",
        "deviated",
    }:
        raise ValueError("historical compliance is invalid")
    if "dispatch_sequence" in body and (
        type(body.get("dispatch_sequence")) is not int or body["dispatch_sequence"] <= 0
    ):
        raise ValueError("historical dispatch sequence is invalid")
    if "effective_jdk" in body:
        _validate_effective_jdk(body.get("effective_jdk"))
    canonical = json.dumps(body, allow_nan=False, sort_keys=True).encode("utf-8")
    if len(canonical) > OBLIGATION_MAX_CANONICAL_BYTES:
        raise ValueError("historical job obligation exceeds its byte limit")
    return body


def obligation_record_scope(
    payload: Mapping[str, Any],
    current_run_id: Optional[str],
) -> str:
    """Ignore only fully validated forensic or foreign-run job records."""

    if not isinstance(payload, Mapping):
        return "current"
    schema = payload.get("schema_version")
    if schema == 1:
        try:
            _validate_historical_obligation(payload)
        except (TypeError, ValueError):
            return "current"
        return "forensic"
    if schema != OBLIGATION_SCHEMA_VERSION:
        return "current"
    try:
        body = validate_obligation_v3(payload)
    except (TypeError, ValueError):
        return "current"
    current = _text(current_run_id)
    recorded = _text(body.get("run_id"))
    if current and recorded and current != recorded:
        return "foreign"
    return "current"


def build_obligation(
    *,
    job_id: str,
    run_id: Optional[str] = None,
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    before: Mapping[str, str],
    log_path: str,
    exit_code_path: str,
    terminal_authority: str,
    docker_exec_id: str,
    container_id: str,
    start_accepted: bool,
    startup_identity_verified: bool,
    runner_dispatch_state: str,
    pid: Optional[int] = None,
    pgid: Optional[int] = None,
    pid_path: Optional[str] = None,
    pgid_path: Optional[str] = None,
    identity_path: Optional[str] = None,
    process_identity_token: Optional[str] = None,
    handoff_reason: Optional[str] = None,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    execution_binding: Optional[str] = None,
    compliance: Optional[str] = None,
    requirements_pins: Optional[Mapping[str, str]] = None,
    domain_id: Optional[str] = None,
    dispatch_sequence: Optional[int] = None,
    effective_jdk: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble one obligation body. Absent facts serialize as absent keys.

    `requirements_pins`, `domain_id`, and `effective_jdk` are dispatch-time
    facts `record_invocation` would otherwise have to re-derive after the job
    finishes. The manifest is not carried into the ledger — settlement happens
    turns later and must state what was pinned at DISPATCH time, not what a
    re-read would say now.

    `dispatch_sequence` is WHERE this dispatch sits in receipt order. Receipt
    ids already carry a process-global monotonic ordinal
    (`next_receipt_id`/`next_sequence`), so a receipt whose ordinal is greater
    than this one was written after the dispatch — which is the only way
    settlement can tell a receipt written INSIDE its window from one an earlier,
    already-closed attempt wrote before it (spec §3.2). Absent when the caller
    could not state it; settlement then falls back to excluding every claim,
    because an obligation that cannot order itself must not claim a path
    somebody else vouched for.
    """
    # A schema-v3 obligation is itself part of one run epoch.  Defaulting at
    # this builder boundary keeps direct/controller callers from creating an
    # epoch-less record whose later receipt silently acquires the process
    # default and can no longer be matched after a settlement-mark retry.
    resolved_run_id = _text(run_id) or active_receipt_run_id()
    obligation: Dict[str, Any] = {
        "schema_version": OBLIGATION_SCHEMA_VERSION,
        "job_id": _text(job_id),
        "run_id": resolved_run_id,
        "tool": _text(tool),
        "attempt": attempt,
        "requested_action": _text(requested_action),
        "effective_action": _text(effective_action),
        "argv": _text(argv),
        "working_directory": _text(working_directory),
        "before": {str(path): str(digest) for path, digest in dict(before or {}).items()},
        "log_path": _text(log_path),
        "exit_code_path": _text(exit_code_path),
        "terminal_authority": _text(terminal_authority),
        "docker_exec_id": _text(docker_exec_id),
        "container_id": _text(container_id),
        "start_accepted": start_accepted,
        "startup_identity_verified": startup_identity_verified,
        "runner_dispatch_state": _text(runner_dispatch_state),
    }
    for key, value in (
        ("contract_id", contract_id),
        ("contract_hash", contract_hash),
        ("execution_binding", execution_binding),
        ("compliance", compliance),
        ("domain_id", domain_id),
    ):
        text = _text(value)
        if text:
            obligation[key] = text
    pins = {str(key): str(value) for key, value in dict(requirements_pins or {}).items() if value}
    if pins:
        obligation["requirements_pins"] = pins
    if isinstance(effective_jdk, Mapping) and effective_jdk:
        obligation["effective_jdk"] = dict(effective_jdk)
    if isinstance(dispatch_sequence, int) and not isinstance(dispatch_sequence, bool):
        obligation["dispatch_sequence"] = dispatch_sequence
    # Startup identity is immutable dispatch evidence.  WS9 cleanup refuses
    # any record whose PID/PGID are absent, non-positive, or not the same
    # session leader, but the ledger records what the launcher actually said
    # rather than repairing an invalid handle here.
    for process_key, process_value in (("pid", pid), ("pgid", pgid)):
        if (
            isinstance(process_value, int)
            and not isinstance(process_value, bool)
            and process_value > 0
        ):
            obligation[process_key] = process_value
    for path_key, path_value in (
        ("pid_path", pid_path),
        ("pgid_path", pgid_path),
        ("identity_path", identity_path),
        ("process_identity_token", process_identity_token),
        ("handoff_reason", handoff_reason),
    ):
        text = _text(path_value)
        if text:
            obligation[path_key] = text
    # Process truth and evidence-persistence truth are independent. A terminal
    # process whose receipt failed to persist must never turn back into a
    # "running" job merely because `settled_receipt_id` is absent.
    obligation.update(
        {
            "process_state": PROCESS_RUNNING,
            "settlement_state": SETTLEMENT_NONE,
            "terminal_exit_code": None,
            "terminal_marker_ref": None,
            "terminal_observed_at": None,
            "settlement_attempts": 0,
            "attempted_receipt_id": None,
            "receipt_persistence_code": None,
            "settled_receipt_id": None,
        }
    )
    return obligation


def record_dispatch_obligation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    result: Mapping[str, Any],
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    before: Mapping[str, str],
    requirements: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Compatibility wrapper returning a job id only when its ledger landed."""
    outcome = record_dispatch_obligation_result(
        execute,
        result=result,
        tool=tool,
        attempt=attempt,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        before=before,
        requirements=requirements,
    )
    return outcome.job_id if outcome.persisted else None


@dataclass(frozen=True)
class DispatchObligationResult:
    job_id: str
    persisted: bool
    code: str
    log_path: str = ""
    exit_code_path: str = ""
    pid: Any = None
    pgid: Any = None
    pid_path: str = ""
    pgid_path: str = ""
    identity_path: str = ""
    process_identity_token: str = ""
    terminal_authority: str = ""
    docker_exec_id: str = ""
    container_id: str = ""
    start_accepted: Any = None
    startup_identity_verified: Any = None
    runner_dispatch_state: str = ""

    def metadata(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_obligation_persisted": self.persisted,
            "job_obligation_persistence_code": self.code,
            "log_path": self.log_path,
            "exit_code_path": self.exit_code_path,
            "pid": self.pid,
            "pgid": self.pgid,
            "pid_path": self.pid_path,
            "pgid_path": self.pgid_path,
            "identity_path": self.identity_path,
            "process_identity_token": self.process_identity_token,
            "terminal_authority": self.terminal_authority,
            "docker_exec_id": self.docker_exec_id,
            "container_id": self.container_id,
            "start_accepted": self.start_accepted,
            "startup_identity_verified": self.startup_identity_verified,
            "runner_dispatch_state": self.runner_dispatch_state,
        }


def record_dispatch_obligation_result(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    result: Mapping[str, Any],
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    before: Mapping[str, str],
    requirements: Optional[Mapping[str, Any]] = None,
) -> DispatchObligationResult:
    """Record a detached handle without losing it when ledger persistence fails.

    The returned handle is small enough for ToolResult/control-event metadata,
    so a running job remains visible to the controller even if the complete
    obligation (which can contain a multi-megabyte `before` snapshot) fails.

    Called from inside the runners' `dispatch_contract` scope, so the contract
    binding is read the same way the synchronous receipt reads it.
    """
    handle = result.get("dispatch") if isinstance(result, Mapping) else None
    handle = handle if isinstance(handle, Mapping) else {}
    job_id = _text(handle.get("job_id"))
    log_path = _text(handle.get("log_path"))
    exit_code_path = _text(handle.get("exit_code_path"))
    base: Dict[str, Any] = {
        "job_id": job_id,
        "log_path": log_path,
        "exit_code_path": exit_code_path,
        "pid": handle.get("pid"),
        "pgid": handle.get("pgid"),
        "pid_path": _text(handle.get("pid_path")),
        "pgid_path": _text(handle.get("pgid_path")),
        "identity_path": _text(handle.get("identity_path")),
        "process_identity_token": _text(handle.get("process_identity_token")),
        "terminal_authority": _text(handle.get("terminal_authority")),
        "docker_exec_id": _text(handle.get("docker_exec_id")),
        "container_id": _text(handle.get("container_id")),
        "start_accepted": handle.get("start_accepted"),
        "startup_identity_verified": handle.get("startup_identity_verified"),
        "runner_dispatch_state": _text(handle.get("runner_dispatch_state")),
    }
    if (
        not job_id
        or not log_path
        or not exit_code_path
        or base["terminal_authority"] != DETACHED_TERMINAL_AUTHORITY
        or _SHA256.fullmatch(base["docker_exec_id"]) is None
        or _SHA256.fullmatch(base["container_id"]) is None
        or base["start_accepted"] is not True
        or handle.get("started") is not True
        or base["startup_identity_verified"] is not True
        or base["runner_dispatch_state"] != "accepted"
        or type(base["pid"]) is not int
        or type(base["pgid"]) is not int
        or base["pid"] <= 0
        or base["pid"] != base["pgid"]
        or not base["pid_path"]
        or not base["pgid_path"]
        or not base["identity_path"]
        or _SHA256.fullmatch(base["process_identity_token"]) is None
    ):
        return DispatchObligationResult(
            persisted=False,
            code="invalid_dispatch_handle",
            **base,
        )
    obligation = build_obligation(
        job_id=job_id,
        run_id=active_receipt_run_id(),
        tool=tool,
        attempt=attempt,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        before=before,
        log_path=log_path,
        exit_code_path=exit_code_path,
        terminal_authority=base["terminal_authority"],
        docker_exec_id=base["docker_exec_id"],
        container_id=base["container_id"],
        start_accepted=True,
        startup_identity_verified=True,
        runner_dispatch_state="accepted",
        pid=handle.get("pid"),
        pgid=handle.get("pgid"),
        pid_path=_text(handle.get("pid_path")),
        pgid_path=_text(handle.get("pgid_path")),
        identity_path=_text(handle.get("identity_path")),
        process_identity_token=_text(handle.get("process_identity_token")),
        handoff_reason=_text(result.get("handoff_reason") or handle.get("handoff_reason")),
        requirements_pins=survey_pins(requirements),
        domain_id=nearest_domain_root(requirements, working_directory),
        # Taken AFTER the handle check above: an ordinal spent on a dispatch
        # that never started would be a hole in receipt order for no fact.
        dispatch_sequence=next_sequence(),
        **contract_receipt_fields(argv),
    )
    persisted = write_obligation(execute, obligation)
    return DispatchObligationResult(
        persisted=persisted,
        code="persisted" if persisted else "transport_write_failed",
        **base,
    )


def write_obligation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    obligation: Optional[Mapping[str, Any]],
) -> bool:
    """CAS one lifecycle revision, then advance its host-owned live head.

    The container file is a mutable stable slot (``job_id``), not authority by
    itself.  A transition may use only the exact raw revision currently sealed
    by the host.  Container rollback/deletion/tampering, a stale concurrent
    writer, or a host publication failure leaves the candidate forensic-only
    and returns ``False``.
    """

    try:
        payload = validate_obligation_v3(obligation or {})
        identifier = payload["job_id"]
        body = json.dumps(payload, allow_nan=False, sort_keys=True)
        raw = body.encode("utf-8")
        authority = evidence_publication_authority_for(execute)
        if _text(getattr(authority, "run_id", None)) != payload["run_id"]:
            raise ValueError("job obligation run differs from host authority")
    except (TypeError, ValueError) as exc:
        logger.debug(f"job obligation is invalid: {exc}")
        return False
    final = f"{OBLIGATION_DIR}/{identifier}.json"

    for _attempt in range(OBLIGATION_WRITE_RETRIES):
        existing_status, existing_raw, existing = _read_obligation_raw(execute, final)
        if existing_status == "invalid":
            logger.warning(
                f"job obligation {identifier} has invalid existing bytes; update refused"
            )
            return False
        try:
            host_head = latest_publication_raw_sha256(execute, identifier)
        except Exception as exc:
            logger.debug(f"job obligation {identifier} host head unavailable: {exc}")
            return False

        if existing_status == "valid":
            assert existing_raw is not None and existing is not None
            existing_digest = hashlib.sha256(existing_raw.encode("utf-8")).hexdigest()
            if existing_digest != host_head:
                logger.warning(
                    f"job obligation {identifier} container revision differs from its host head"
                )
                return False
            if existing_raw == body:
                return _latest_obligation_authorized(execute, payload, raw)
            if not _is_lifecycle_transition(existing, payload):
                logger.warning(
                    f"job obligation {identifier} lifecycle or dispatch identity changed; "
                    "update refused"
                )
                return False
            expected_content: Optional[str] = existing_raw
            expected_previous = existing_digest
        else:
            if host_head != EVIDENCE_PUBLICATION_GENESIS_SHA256:
                logger.warning(
                    f"job obligation {identifier} is absent while its host head still exists"
                )
                return False
            expected_content = None
            expected_previous = EVIDENCE_PUBLICATION_GENESIS_SHA256

        try:
            result = compare_publish_container_text_atomic(
                execute,
                final,
                body,
                expected_content=expected_content,
                validate_json=True,
            )
        except Exception as exc:
            logger.debug(f"job obligation {identifier} not persisted: {exc}")
            return False
        if result.code == WRITE_COMPARE_CONFLICT:
            continue
        if not result.persisted:
            logger.debug(f"job obligation {identifier} not persisted: {result.code}")
            return False

        publication = publish_evidence_revision(
            execute,
            record_kind="job_obligation",
            record_id=identifier,
            logical_artifact_id=identifier,
            raw=raw,
            expected_previous_raw_sha256=expected_previous,
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
        )
        if publication.published:
            return True
        # An identical concurrent publisher may have committed after our CAS.
        if _latest_obligation_authorized(execute, payload, raw):
            return True
        logger.warning(
            f"job obligation {identifier} reached the container but host publication "
            f"failed: {publication.status}"
        )
        return False

    logger.warning(f"job obligation {identifier} exhausted concurrent CAS retries")
    return False


# The §3.3 cap's stand-in job id when the ledger itself could not be read.
# §6.8 / P4: a failed ledger read once parsed as "no obligations", which
# LIFTED the cap — removing evidence improved the verdict. The sentinel keeps
# the cap held on a stated inability instead.
LEDGER_UNREADABLE = "job-obligations-ledger-unreadable"


def read_obligations(orchestrator: Any) -> Optional[List[Dict[str, Any]]]:
    """Return the complete current host-authorized mutable ledger.

    ``[]`` is a host-verified empty current set.  ``None`` covers malformed,
    unpublished, rolled-back, deleted, stale-head, future-schema or transport-
    incomplete state so removing evidence can never release the job barrier.
    """

    read = read_live_published_json_records(
        orchestrator,
        OBLIGATION_DIR,
        record_kind="job_obligation",
        validator=lambda payload, expected_id: validate_obligation_v3(
            payload,
            expected_id=expected_id,
        ),
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=payload["run_id"],
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
            logical_artifact_id=payload["job_id"],
        ),
        record_scope=obligation_record_scope,
    )
    if not read.complete or read.conflict is not None:
        logger.warning(
            f"{OBLIGATION_DIR} live ledger unavailable: "
            f"{read.conflict or read.detail or 'incomplete'}"
        )
        return None
    return sorted(
        (dict(record.payload) for record in read.records),
        key=lambda record: record["job_id"],
    )


def open_obligations(orchestrator: Any) -> List[Dict[str, Any]]:
    """Compatibility surface for obligations that still block model work."""
    return [record for record in (read_obligations(orchestrator) or []) if blocks_model(record)]


def open_job_ids(orchestrator: Any) -> tuple:
    """Job ids holding the controller barrier, in job-id order."""
    return tuple(_text(record.get("job_id")) for record in open_obligations(orchestrator))


def is_open(obligation: Optional[Mapping[str, Any]]) -> bool:
    """Legacy alias. New control code should state `blocks_model` directly."""
    return blocks_model(obligation)


def process_is_live(obligation: Optional[Mapping[str, Any]]) -> bool:
    """Whether the ledger states that the physical process may still run.

    Schema-v1 records had no process state. An unsettled v1 record therefore
    remains conservatively live; legacy marker bytes are forensic only and can
    never discharge the current controller barrier.
    """
    body = obligation or {}
    state = _text(body.get("process_state"))
    if state:
        return state == PROCESS_RUNNING
    return not _text(body.get("settled_receipt_id"))


def settlement_is_pending(obligation: Optional[Mapping[str, Any]]) -> bool:
    return _text((obligation or {}).get("settlement_state")) == SETTLEMENT_PENDING


def is_terminal_unpersisted(obligation: Optional[Mapping[str, Any]]) -> bool:
    body = obligation or {}
    return (
        _text(body.get("process_state")) == PROCESS_TERMINAL
        and _text(body.get("settlement_state")) == SETTLEMENT_UNPERSISTED
    )


def blocks_model(obligation: Optional[Mapping[str, Any]]) -> bool:
    return process_is_live(obligation) or settlement_is_pending(obligation)


# ---------------------------------------------------------------------------
# settlement (spec §3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitObservation:
    """One typed projection of daemon terminal truth or forensic marker bytes."""

    state: str
    marker_ref: str
    exit_code: Optional[int] = None
    detail: str = ""
    start_accepted: bool = False


@dataclass(frozen=True)
class TerminalObservation:
    """Small durable process fact written before the potentially large receipt."""

    job_id: str
    exit_code: int
    marker_ref: str
    observed_at: str
    obligation_ref: str

    def event_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "exit_code": self.exit_code,
            "marker_ref": self.marker_ref,
            "observed_at": self.observed_at,
            "obligation_ref": self.obligation_ref,
        }


@dataclass(frozen=True)
class TerminalUnpersisted:
    """A terminal runner whose complete receipt exhausted transport retries."""

    job_id: str
    exit_code: int
    attempted_receipt_id: str
    persistence_code: str
    attempt_count: int
    obligation_ref: str
    log_ref: str
    contract_id: str = ""

    def notice(self) -> str:
        return (
            f"[terminal-unpersisted] job {self.job_id}: exit {self.exit_code} — "
            f"receipt {self.attempted_receipt_id} not persisted "
            f"({self.persistence_code}, attempts={self.attempt_count})"
        )

    def event_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "exit_code": self.exit_code,
            "attempted_receipt_id": self.attempted_receipt_id,
            "persistence_code": self.persistence_code,
            "attempt_count": self.attempt_count,
            "obligation_ref": self.obligation_ref,
            "log_ref": self.log_ref,
            **({"contract_id": self.contract_id} if self.contract_id else {}),
        }


@dataclass(frozen=True)
class ObligationReconciliation:
    running_job_ids: Tuple[str, ...] = ()
    settlement_pending_job_ids: Tuple[str, ...] = ()
    terminal_observations: Tuple[TerminalObservation, ...] = ()
    settlements: Tuple["Settlement", ...] = ()
    terminal_unpersisted: Tuple[TerminalUnpersisted, ...] = ()
    integrity_failures: Tuple[str, ...] = ()

    @property
    def barrier_active(self) -> bool:
        return bool(
            self.running_job_ids or self.settlement_pending_job_ids or self.integrity_failures
        )


@dataclass(frozen=True)
class _SettlementAttempt:
    settlement: Optional["Settlement"] = None
    terminal_unpersisted: Optional[TerminalUnpersisted] = None
    remains_pending: bool = False
    integrity_failure: str = ""


def _receipt_evidence_omissions(receipt: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    """``(field, reason)`` for each observability field the receipt dropped.

    The receipt already records this (`build_receipt` refuses to let one
    unrepresentable field void the exit code, the argv and the report delta),
    and nothing read it back. It is bounded by construction: only the four
    omittable evidence fields can appear, each once.
    """
    omissions: list[Tuple[str, str]] = []
    for entry in (receipt or {}).get("evidence_omissions") or ():
        if not isinstance(entry, Mapping):
            continue
        field = _text(entry.get("field"))
        if not field:
            continue
        reasons = [_text(reason) for reason in entry.get("reasons") or () if _text(reason)]
        omissions.append((field, "; ".join(reasons) or "no reason stated"))
    return tuple(omissions)


@dataclass(frozen=True)
class Settlement:
    """What one settled obligation did, for the notice and the event."""

    job_id: str
    receipt_id: str
    exit_code: int
    claimed_paths: int
    excluded_claimed_paths: int = 0
    contract_id: str = ""
    evidence_omissions: Tuple[Tuple[str, str], ...] = ()

    def notice(self) -> str:
        """The ONE bounded line the next observation carries (spec §3.2.7).

        A settled receipt must not surprise the model — "where did this come
        from?" — and must not be fabricated into a tool result either. One
        line, stating the job, its terminal exit code, the receipt it wrote
        and how much of its own write window it could claim.
        """
        line = (
            f"[settled] job {self.job_id}: exit {self.exit_code} — "
            f"receipt {self.receipt_id}, {self.claimed_paths} report paths claimed"
        )
        if self.excluded_claimed_paths:
            # "earlier" would be false: a receipt that takes a path out of this
            # window was written AFTER the dispatch, which is exactly why it
            # takes it (Category 3 — the harness never states a fact that is
            # not true).
            line += f" ({self.excluded_claimed_paths} already claimed by an intervening receipt)"
        for field, reason in self.evidence_omissions:
            # The dispatch OBSERVED this and the receipt could not carry it.
            # Silence here is what made a receipt with no module list read like
            # a build that named one module.
            line += f"; {field} was observed but not recorded on the receipt ({reason})"
        return line

    def event_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "receipt_id": self.receipt_id,
            "exit_code": self.exit_code,
        }


def settle_open_obligations(
    orchestrator: Any,
    obligations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> List[Settlement]:
    """Compatibility wrapper returning only newly completed settlements."""
    return list(reconcile_job_obligations(orchestrator, obligations=obligations).settlements)


def reconcile_job_obligations(
    orchestrator: Any,
    obligations: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    observed_at: Optional[str] = None,
) -> ObligationReconciliation:
    """Reconcile process and persistence truth without returning control early.

    A valid exit is first committed as terminal/pending. Only then is the
    complete receipt attempted. Missing markers remain running; unreadable or
    malformed markers are typed harness-integrity failures, never guessed
    project outcomes.
    """
    execute = resolve_control_execute(orchestrator)
    if not callable(execute):
        return ObligationReconciliation(integrity_failures=("orchestrator_unavailable",))
    try:
        records = read_obligations(orchestrator) if obligations is None else obligations
    except Exception as exc:
        logger.debug(f"job obligations could not be swept: {exc}")
        return ObligationReconciliation(
            integrity_failures=(f"ledger_read_failed:{type(exc).__name__}",)
        )
    if records is None:
        return ObligationReconciliation(integrity_failures=("ledger_unreadable",))

    claims_state = _receipt_claims(execute)
    claims = claims_state or []
    running: List[str] = []
    pending_ids: List[str] = []
    terminal_observations: List[TerminalObservation] = []
    settlements: List[Settlement] = []
    unpersisted: List[TerminalUnpersisted] = []
    integrity: List[str] = []
    timestamp = observed_at or _utc_now()

    for source in records or ():
        obligation = dict(source)
        job_id = _text(obligation.get("job_id"))
        if not job_id:
            integrity.append("obligation_missing_job_id")
            continue
        attempt_failure = settlement_attempts_integrity_failure(obligation)
        if attempt_failure:
            integrity.append(attempt_failure)
            continue
        if is_terminal_unpersisted(obligation):
            # This is durable lifecycle state, not an edge notification.  A
            # restarted engine must recover the same evidence-integrity block
            # from the ledger; the engine's announcement guard is what keeps
            # the corresponding control event at-most-once per run.
            try:
                terminal_observations.append(_terminal_observation_from_record(obligation))
                unpersisted.append(_terminal_unpersisted_from_record(obligation))
            except (TypeError, ValueError):
                integrity.append(f"{job_id}:invalid_terminal_unpersisted_state")
            continue
        if _lifecycle_state(obligation) == (
            PROCESS_TERMINAL,
            SETTLEMENT_SETTLED,
        ):
            failure = settled_receipt_integrity_failure(
                execute,
                obligation,
                receipt_ledger_readable=claims_state is not None,
            )
            if failure:
                integrity.append(failure)
            else:
                # Receipt settlement and assessment publication are distinct.
                # A restart must be able to finish a partially published bundle.
                ensure_receipt_assessed(
                    execute,
                    obligation.get("settled_receipt_id"),
                    evidence_ref=_text(obligation.get("settled_receipt_id")),
                    output_loader=lambda: _read_complete_log(
                        orchestrator, obligation.get("log_path")
                    ),
                )
            continue
        try:
            if process_is_live(obligation):
                exit_observation = observe_detached_terminal(orchestrator, obligation)
                if exit_observation.state == PROCESS_RUNNING:
                    running.append(job_id)
                    continue
                if exit_observation.state != PROCESS_TERMINAL:
                    integrity.append(f"{job_id}:terminal_authority_{exit_observation.state}")
                    continue
                obligation = _terminal_pending_record(
                    obligation,
                    exit_code=int(exit_observation.exit_code),
                    marker_ref=exit_observation.marker_ref,
                    observed_at=timestamp,
                )
                if not write_obligation(execute, obligation):
                    integrity.append(f"{job_id}:terminal_observation_unpersisted")
                    continue
                terminal_observations.append(
                    TerminalObservation(
                        job_id=job_id,
                        exit_code=int(exit_observation.exit_code),
                        marker_ref=exit_observation.marker_ref,
                        observed_at=timestamp,
                        obligation_ref=_obligation_ref(job_id),
                    )
                )

            if not settlement_is_pending(obligation):
                integrity.append(f"{job_id}:illegal_lifecycle_state")
                continue
            if claims_state is None:
                integrity.append(f"{job_id}:receipt_ledger_unreadable")
                continue
            attempt = _settle_one(orchestrator, obligation, claims)
        except Exception as exc:  # reconciliation never breaks the run
            logger.debug(f"job {job_id} could not be reconciled: {exc}")
            integrity.append(f"{job_id}:reconciliation_{type(exc).__name__}")
            continue
        if attempt.settlement is not None:
            settlements.append(attempt.settlement)
        elif attempt.terminal_unpersisted is not None:
            unpersisted.append(attempt.terminal_unpersisted)
        elif attempt.integrity_failure:
            integrity.append(attempt.integrity_failure)
        elif attempt.remains_pending:
            pending_ids.append(job_id)

    return ObligationReconciliation(
        running_job_ids=tuple(sorted(set(running))),
        settlement_pending_job_ids=tuple(sorted(set(pending_ids))),
        terminal_observations=tuple(terminal_observations),
        settlements=tuple(settlements),
        terminal_unpersisted=tuple(unpersisted),
        integrity_failures=tuple(integrity),
    )


def settlement_from_ledger(
    orchestrator: Any,
    obligation: Mapping[str, Any],
) -> Optional[Settlement]:
    """Rebuild the `Settlement` of an ALREADY settled obligation, from disk.

    The engine announces settlements (one control event, one notice, the
    post-receipt hooks) whoever performed them — the phase gate settles too,
    so that a claim is never graded against moving books. Reading the receipt
    back is how the announcement states what the receipt says rather than what
    the announcer guessed.
    """
    execute = resolve_control_execute(orchestrator)
    receipt_id = _text((obligation or {}).get("settled_receipt_id"))
    exit_code = (obligation or {}).get("terminal_exit_code")
    if (
        not callable(execute)
        or not receipt_id
        or receipt_id != _text((obligation or {}).get("attempted_receipt_id"))
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
    ):
        return None
    ledger = _read_live_receipt_ledger(execute)
    receipt = ledger.get(receipt_id) if ledger is not None else None
    if receipt is None or not _receipt_matches_obligation(receipt, obligation, exit_code):
        return None
    return Settlement(
        job_id=_text(obligation.get("job_id")),
        receipt_id=receipt_id,
        exit_code=exit_code,
        claimed_paths=len(_delta_paths(receipt.get("report_delta"))),
        excluded_claimed_paths=int(receipt.get("excluded_claimed_paths") or 0),
        contract_id=_text(obligation.get("contract_id")),
        evidence_omissions=_receipt_evidence_omissions(receipt),
    )


def read_exit_code(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    exit_code_path: Any,
) -> Optional[int]:
    """Compatibility projection of a typed exit-marker observation."""
    observation = observe_exit_marker(execute, exit_code_path)
    return observation.exit_code if observation.state == PROCESS_TERMINAL else None


def observe_detached_terminal(
    orchestrator: Any,
    obligation: Mapping[str, Any],
) -> ExitObservation:
    """Project daemon-owned detached truth into one typed observation.

    The container's ``.exit`` file is explicitly outside this authority
    boundary: the project runner and descendants share its UID and can rewrite
    it. Only Docker's host-side exec record, bound to both the frozen exec id
    and container id carried by the host-published obligation, can close the
    physical process lifecycle.
    """

    docker_exec_id = _text((obligation or {}).get("docker_exec_id"))
    start_accepted = (obligation or {}).get("start_accepted")
    marker_ref = f"docker-exec:{docker_exec_id}" if docker_exec_id else "docker-exec:unknown"
    if type(start_accepted) is not bool:
        return ExitObservation(
            "malformed",
            marker_ref,
            detail="detached_start_acceptance_missing",
        )
    inspector = getattr(orchestrator, "inspect_detached_terminal", None)
    if not callable(inspector):
        return ExitObservation(
            "unreadable",
            marker_ref,
            detail="detached_daemon_inspector_unavailable",
        )
    try:
        result = inspector(dict(obligation))
    except Exception as exc:
        logger.debug(f"detached daemon terminal observation unavailable: {exc}")
        return ExitObservation("unreadable", marker_ref, detail=type(exc).__name__)
    if not isinstance(result, Mapping) or result.get("probe_success") is not True:
        detail = _text((result or {}).get("probe_error")) if isinstance(result, Mapping) else ""
        return ExitObservation(
            "unreadable",
            marker_ref,
            detail=detail or "detached_daemon_inspect_failed",
        )
    state = _text(result.get("state"))
    if state == "running" and result.get("running") is True and result.get("finished") is False:
        # Running=true is a daemon-owned proof that exec_start crossed the
        # acceptance boundary.  The ephemeral controller may freeze this fact
        # before a later terminal observation; a finished-only record may
        # never create it retroactively.
        return ExitObservation(PROCESS_RUNNING, marker_ref, start_accepted=True)
    exit_code = result.get("exit_code")
    if (
        start_accepted is True
        and state == "finished"
        and result.get("finished") is True
        and result.get("running") is False
        and type(exit_code) is int
        and 0 <= exit_code <= 255
    ):
        return ExitObservation(
            PROCESS_TERMINAL,
            marker_ref,
            exit_code=exit_code,
            start_accepted=True,
        )
    if start_accepted is not True and state == "finished":
        return ExitObservation(
            "unreadable",
            marker_ref,
            detail="detached_start_acceptance_unproven",
        )
    return ExitObservation("malformed", marker_ref, detail="detached_daemon_state_invalid")


def observe_exit_marker(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    exit_code_path: Any,
) -> ExitObservation:
    """Forensic-only parser for a legacy container exit marker.

    Live reconciliation never calls this function: a same-UID runner can
    rewrite the marker. It remains available only for explicit historical
    diagnostics and compatibility projections that carry no settlement
    authority.
    """
    path = _text(exit_code_path)
    if not path:
        return ExitObservation("unreadable", path, detail="missing_exit_code_path")
    try:
        result = execute(f"cat {shlex.quote(path)} 2>/dev/null") or {}
    except Exception as exc:
        logger.debug(f"exit code {path} unreadable: {exc}")
        return ExitObservation("unreadable", path, detail=type(exc).__name__)
    if not _succeeded(result):
        output = str(result.get("output") or "").strip()
        lowered = output.lower()
        transport_failed = (
            result.get("exit_code") == -1
            or bool(result.get("dispatch_status"))
            or any(
                marker in lowered
                for marker in (
                    "permission denied",
                    "read-only",
                    "container is not running",
                    "container gone",
                    "cannot connect",
                )
            )
        )
        return ExitObservation(
            "unreadable" if transport_failed else "absent",
            path,
            detail=output[:200],
        )
    tokens = [line.strip() for line in str(result.get("output") or "").splitlines() if line.strip()]
    if len(tokens) != 1:
        return ExitObservation("malformed", path, detail="expected_one_integer")
    try:
        exit_code = int(tokens[0])
    except ValueError:
        return ExitObservation("malformed", path, detail=tokens[0][:200])
    return ExitObservation(PROCESS_TERMINAL, path, exit_code=exit_code)


def _settle_one(
    orchestrator: Any,
    obligation: Mapping[str, Any],
    claims: List[Tuple[Optional[int], Tuple[str, ...]]],
) -> _SettlementAttempt:
    execute = resolve_control_execute(orchestrator)
    if not callable(execute):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:control_transport_unavailable"
        )
    exit_code = obligation.get("terminal_exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:terminal_exit_missing"
        )

    receipt_id = _text(obligation.get("attempted_receipt_id"))
    if not receipt_id:
        receipt_id = next_receipt_id(_text(obligation.get("tool")), obligation.get("attempt"))
        frozen = {
            **dict(obligation),
            "attempted_receipt_id": receipt_id,
        }
        if not write_obligation(execute, frozen):
            return _SettlementAttempt(
                integrity_failure=(
                    f"{_text(obligation.get('job_id'))}:receipt_identity_unpersisted"
                )
            )
        obligation = frozen

    receipt_ledger = _read_live_receipt_ledger(execute)
    if receipt_ledger is None:
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:receipt_ledger_unreadable"
        )
    existing_receipt = receipt_ledger.get(receipt_id)
    if existing_receipt is not None:
        if not _receipt_matches_obligation(existing_receipt, obligation, exit_code):
            return _SettlementAttempt(
                integrity_failure=(f"{_text(obligation.get('job_id'))}:receipt_identity_conflict")
            )
        return _finalize_settlement(
            orchestrator,
            obligation,
            existing_receipt,
            claims,
        )

    attempt_failure = settlement_attempts_integrity_failure(obligation)
    if attempt_failure:
        return _SettlementAttempt(integrity_failure=attempt_failure)
    previous_attempts = obligation.get("settlement_attempts") or 0
    if previous_attempts >= MAX_SETTLEMENT_PERSIST_ATTEMPTS:
        return _terminalize_unpersisted(
            execute,
            obligation,
            _text(obligation.get("receipt_persistence_code")) or "transport_write_failed",
        )

    attempting = {
        **dict(obligation),
        "settlement_attempts": previous_attempts + 1,
    }
    if not write_obligation(execute, attempting):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:attempt_state_unpersisted"
        )
    obligation = attempting

    working_directory = _text(obligation.get("working_directory"))
    tool = _text(obligation.get("tool"))
    before = dict(obligation.get("before") or {})
    after = snapshot_reports(execute, [working_directory])
    log = _read_complete_log(orchestrator, obligation.get("log_path"))
    module_outcomes, cached_roots = _parse_outcomes(tool, log or "", working_directory)

    # Attribution (spec §3.2). The window is this job's own `before` against
    # its own `after`, and the one real caveat is INTERVENING work: another
    # dispatch may have written into the same roots while this job ran. Receipts
    # are ordered, so "intervening" is decidable — a receipt whose ordinal is
    # greater than this dispatch's was written after it — and only those paths
    # are excluded. Dropping one from `after` is the exclusion: `report_delta`
    # records no deletions, so a path that is not in `after` is claimed by
    # nobody here.
    #
    # The scope matters in both directions. A receipt from BEFORE the dispatch
    # is outside the window and takes nothing: it vouched for a version of the
    # report that this job has since rewritten, and excluding it is how a
    # detached retry lost its own evidence — the delta came out empty, the file
    # on disk matched only the superseded claim, and the report landed in
    # `stale_test_reports` instead of the main count. Measured on the real
    # in-container parser: pre-dispatch claim alone -> 0 counted, 1 stale; both
    # claims present -> 3 counted, 0 stale, counted exactly ONCE (claims are
    # path -> set(sha), so overlap cannot double count).
    claimed = _intervening_claims(claims, _dispatch_sequence(obligation))
    excluded = sorted(
        path for path in _delta_paths(report_delta(before, after, cached_roots)) if path in claimed
    )
    if excluded:
        after = {path: digest for path, digest in after.items() if path not in set(excluded)}
    mine = _delta_paths(report_delta(before, after, cached_roots))
    # The SAME harvest the synchronous path runs, over this job's own settled
    # window. kafka died on exactly this path; a receipt settled here has to
    # state what one written in-line would, or the two paths disagree about
    # what a Gradle run left behind.
    module_outcomes, harvested = _parse_test_harvest(
        execute,
        tool=tool,
        working_directory=working_directory,
        delta=report_delta(before, after, cached_roots),
        module_outcomes=module_outcomes,
        test_disposition=sealed_test_disposition_status(orchestrator),
    )

    if tool == "maven":
        from sag.tools.internal.maven_tool import _reactor_receipt_fields

        reactor_fields = _reactor_receipt_fields(log)
        module_outcomes = reactor_fields.pop("module_outcomes")
        harvested.update(reactor_fields)

    metadata = record_invocation(
        execute,
        receipt_id=receipt_id,
        run_id=_text(obligation.get("run_id")) or None,
        tool=tool,
        attempt=obligation.get("attempt"),
        requested_action=_text(obligation.get("requested_action")),
        effective_action=_text(obligation.get("effective_action")),
        argv=_text(obligation.get("argv")),
        working_directory=working_directory,
        exit_code=exit_code,
        before=before,
        after=after,
        output=log,
        requirements=_requirements_view(obligation),
        contract_id=obligation.get("contract_id"),
        contract_hash=obligation.get("contract_hash"),
        execution_binding=obligation.get("execution_binding"),
        compliance=obligation.get("compliance"),
        effective_jdk=obligation.get("effective_jdk"),
        module_outcomes=module_outcomes,
        **harvested,
        cached_report_roots=cached_roots,
        excluded_claimed_paths=len(excluded),
    )
    persisted_id = _text((metadata or {}).get("receipt_id"))
    if persisted_id:
        receipt_ledger = _read_live_receipt_ledger(execute)
        if receipt_ledger is None:
            return _SettlementAttempt(
                integrity_failure=(f"{_text(obligation.get('job_id'))}:receipt_ledger_unreadable")
            )
        receipt = receipt_ledger.get(persisted_id)
        if receipt is None:
            return _SettlementAttempt(
                integrity_failure=f"{_text(obligation.get('job_id'))}:receipt_missing"
            )
        return _finalize_settlement(orchestrator, obligation, receipt, claims, output=log)

    persistence_code = _text((metadata or {}).get("receipt_persistence_code")) or (
        "transport_write_failed"
    )
    failed = {
        **dict(obligation),
        "receipt_persistence_code": persistence_code,
    }
    if int(failed.get("settlement_attempts") or 0) >= MAX_SETTLEMENT_PERSIST_ATTEMPTS:
        return _terminalize_unpersisted(execute, failed, persistence_code)
    if not write_obligation(execute, failed):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:failure_state_unpersisted"
        )
    return _SettlementAttempt(remains_pending=True)


def _finalize_settlement(
    orchestrator: Any,
    obligation: Mapping[str, Any],
    receipt: Mapping[str, Any],
    claims: List[Tuple[Optional[int], Tuple[str, ...]]],
    *,
    output: Optional[str] = None,
) -> _SettlementAttempt:
    execute = resolve_control_execute(orchestrator)
    if execute is None:
        return _SettlementAttempt(integrity_failure="control_transport_unavailable")
    receipt_id = _text(receipt.get("receipt_id"))
    exit_code = receipt.get("exit_code")
    if not receipt_id or not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:receipt_unreadable"
        )
    settled = {
        **dict(obligation),
        "settlement_state": SETTLEMENT_SETTLED,
        "settled_receipt_id": receipt_id,
        "receipt_persistence_code": None,
    }
    if not write_obligation(execute, settled):
        return _SettlementAttempt(
            integrity_failure=f"{_text(obligation.get('job_id'))}:settlement_mark_unpersisted"
        )
    mine = tuple(_delta_paths(receipt.get("report_delta")))
    claims.append((_receipt_sequence(receipt_id), mine))
    ensure_receipt_assessed(
        execute,
        receipt_id,
        output=output,
        evidence_ref=receipt_id,
        output_loader=lambda: _read_complete_log(orchestrator, obligation.get("log_path")),
    )
    return _SettlementAttempt(
        settlement=Settlement(
            job_id=_text(obligation.get("job_id")),
            receipt_id=receipt_id,
            exit_code=exit_code,
            claimed_paths=len(mine),
            excluded_claimed_paths=int(receipt.get("excluded_claimed_paths") or 0),
            contract_id=_text(obligation.get("contract_id")),
            evidence_omissions=_receipt_evidence_omissions(receipt),
        )
    )


def _terminalize_unpersisted(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    obligation: Mapping[str, Any],
    persistence_code: str,
) -> _SettlementAttempt:
    terminal = {
        **dict(obligation),
        "settlement_state": SETTLEMENT_UNPERSISTED,
        "receipt_persistence_code": persistence_code,
        "settled_receipt_id": None,
    }
    job_id = _text(obligation.get("job_id"))
    if not write_obligation(execute, terminal):
        return _SettlementAttempt(integrity_failure=f"{job_id}:unpersisted_state_unpersisted")
    return _SettlementAttempt(terminal_unpersisted=_terminal_unpersisted_from_record(terminal))


def _terminal_unpersisted_from_record(
    obligation: Mapping[str, Any],
) -> TerminalUnpersisted:
    """Project one durable terminal/unpersisted ledger state."""
    job_id = _text(obligation.get("job_id"))
    return TerminalUnpersisted(
        job_id=job_id,
        exit_code=int(obligation.get("terminal_exit_code")),
        attempted_receipt_id=_text(obligation.get("attempted_receipt_id")),
        persistence_code=_text(obligation.get("receipt_persistence_code"))
        or "transport_write_failed",
        attempt_count=int(obligation.get("settlement_attempts") or 0),
        obligation_ref=_obligation_ref(job_id),
        log_ref=_text(obligation.get("log_path")),
        contract_id=_text(obligation.get("contract_id")),
    )


def _terminal_observation_from_record(
    obligation: Mapping[str, Any],
) -> TerminalObservation:
    """Rebuild the small terminal fact after a controller restart."""
    job_id = _text(obligation.get("job_id"))
    exit_code = obligation.get("terminal_exit_code")
    marker_ref = _text(obligation.get("terminal_marker_ref"))
    observed_at = _text(obligation.get("terminal_observed_at"))
    if (
        not job_id
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not marker_ref
        or not observed_at
    ):
        raise ValueError("terminal observation fields are incomplete")
    return TerminalObservation(
        job_id=job_id,
        exit_code=exit_code,
        marker_ref=marker_ref,
        observed_at=observed_at,
        obligation_ref=_obligation_ref(job_id),
    )


def _terminal_pending_record(
    obligation: Mapping[str, Any],
    *,
    exit_code: int,
    marker_ref: str,
    observed_at: str,
) -> Dict[str, Any]:
    return {
        **dict(obligation),
        "schema_version": OBLIGATION_SCHEMA_VERSION,
        "process_state": PROCESS_TERMINAL,
        "settlement_state": SETTLEMENT_PENDING,
        "terminal_exit_code": exit_code,
        "terminal_marker_ref": marker_ref,
        "terminal_observed_at": observed_at,
        "settlement_attempts": int(obligation.get("settlement_attempts") or 0),
        "attempted_receipt_id": obligation.get("attempted_receipt_id"),
        "receipt_persistence_code": obligation.get("receipt_persistence_code"),
        "settled_receipt_id": None,
    }


def _receipt_matches_obligation(
    receipt: Mapping[str, Any],
    obligation: Mapping[str, Any],
    exit_code: int,
) -> bool:
    """Protect a pre-frozen receipt id from collision on crash replay."""
    return all(
        (
            _text(receipt.get("receipt_id")) == _text(obligation.get("attempted_receipt_id")),
            _text(receipt.get("run_id")) == _text(obligation.get("run_id")),
            _text(receipt.get("tool")) == _text(obligation.get("tool")),
            _text(receipt.get("argv")) == _text(obligation.get("argv")),
            _text(receipt.get("working_directory")) == _text(obligation.get("working_directory")),
            receipt.get("exit_code") == exit_code,
        )
    )


def settled_receipt_integrity_failure(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    obligation: Mapping[str, Any],
    *,
    receipt_ledger_readable: bool,
) -> str:
    """Validate the durable witness named by a settled lifecycle record.

    ``terminal/settled`` is not self-authenticating.  The receipt is the
    evidence that discharged the obligation, so every reconciliation rereads
    it and checks the same frozen execution identity used by crash recovery.
    A missing or corrupt witness keeps an integrity barrier; reconciliation
    never retries the project command or writes a replacement receipt.
    """
    job_id = _text(obligation.get("job_id")) or "obligation_missing_job_id"
    if not receipt_ledger_readable:
        return f"{job_id}:receipt_ledger_unreadable"
    receipt_id = _text(obligation.get("settled_receipt_id"))
    attempted_receipt_id = _text(obligation.get("attempted_receipt_id"))
    exit_code = obligation.get("terminal_exit_code")
    if not receipt_id:
        return f"{job_id}:settled_receipt_id_missing"
    if not attempted_receipt_id or receipt_id != attempted_receipt_id:
        return f"{job_id}:settled_receipt_identity_mismatch"
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return f"{job_id}:settled_terminal_exit_invalid"
    receipt_ledger = _read_live_receipt_ledger(execute)
    if receipt_ledger is None:
        return f"{job_id}:receipt_ledger_unreadable"
    receipt = receipt_ledger.get(receipt_id)
    if receipt is None:
        return f"{job_id}:settled_receipt_missing"
    if "unparseable" in receipt:
        return f"{job_id}:settled_receipt_malformed"
    if not _receipt_matches_obligation(receipt, obligation, exit_code):
        return f"{job_id}:settled_receipt_identity_mismatch"
    return ""


def settlement_attempts_integrity_failure(obligation: Mapping[str, Any]) -> str:
    """Return a typed schema-integrity failure for an impossible retry count.

    Missing counts remain the explicit schema-v1 compatibility value of zero.
    Schema-v3 records are written with the field from birth; any present value
    must be an integer in the closed controller-owned retry budget.  Negative
    or over-budget input is corruption, not extra retry authority.
    """

    job_id = _text((obligation or {}).get("job_id")) or "obligation_missing_job_id"
    raw = (obligation or {}).get("settlement_attempts")
    if raw is None:
        return ""
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or raw < 0
        or raw > MAX_SETTLEMENT_PERSIST_ATTEMPTS
    ):
        return f"{job_id}:invalid_settlement_attempts"
    return ""


def _obligation_ref(job_id: str) -> str:
    return f"{OBLIGATION_DIR}/{job_id}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_outcomes(
    tool: str,
    log: str,
    working_directory: str,
) -> Tuple[Sequence[Mapping[str, str]], Sequence[str]]:
    """The SAME parsers the synchronous path runs, over the job's own log.

    Imported here rather than at module scope: the runners import this module
    to record their obligations, and a module-scope import back into them
    would tie the ledger's import order to the tools'.
    """
    if tool == "gradle":
        from sag.tools.internal.gradle_tool import (
            _gradle_cached_report_dirs,
            _gradle_module_outcomes,
        )

        return _gradle_module_outcomes(log), _gradle_cached_report_dirs(log, working_directory)
    if tool == "maven":
        from sag.tools.internal.maven_tool import _reactor_module_outcomes

        # Maven vouches for no report it did not rewrite: there is no reactor
        # equivalent of Gradle's FROM-CACHE, so the synchronous path passes no
        # cached roots either.
        return _reactor_module_outcomes(log), []
    return [], []


def _parse_test_harvest(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    tool: str,
    working_directory: str,
    delta: Mapping[str, Any],
    module_outcomes: Sequence[Mapping[str, str]],
    test_disposition: Optional[str],
) -> Tuple[Sequence[Mapping[str, Any]], Dict[str, Any]]:
    """The runner's own post-dispatch report harvest, run at settlement.

    Returns `(module_outcomes, receipt kwargs)`. A detached job holds exactly
    what the harvest needs — the container, the working directory and its own
    settled report window — so the settled receipt states the same evidence a
    synchronous one does. The kafka run this exists for died on THIS path; a
    harvest wired only into the tool would have left it reporting nothing all
    over again.

    The job's ACTION is no longer among those inputs. It named the harvest's
    old task-name gate, and a job dispatched as `smokeTest` settled with its
    reports on disk and nothing in its receipt. What the job wrote is the
    delta's to answer; what it means that it wrote nothing is the sealed
    plan's `test_disposition`, which settlement reads for the same run the job
    belongs to.

    Imported lazily for the same reason `_parse_outcomes` is: the runners
    import this module to record their obligations.
    """

    if tool != "gradle":
        return module_outcomes, {}
    from sag.tools.internal.gradle_tool import (
        _gradle_module_outcomes_with_counts,
        gradle_test_harvest,
    )

    harvest = gradle_test_harvest(
        execute,
        working_directory=working_directory,
        delta=delta,
        test_disposition=test_disposition,
    )
    return (
        _gradle_module_outcomes_with_counts(module_outcomes, harvest.module_tests_reported),
        {
            "gradle_suite_summaries": harvest.suite_summaries,
            "gradle_row_disclosure": harvest.row_disclosure,
            "harvested_testcase_outcomes": harvest.testcase_outcomes,
            "declared_omissions": harvest.omissions,
        },
    )


def _requirements_view(obligation: Mapping[str, Any]) -> Dict[str, Any]:
    """The manifest projection `record_invocation` reads, as of DISPATCH time.

    `survey_pins` and `nearest_domain_root` both read a manifest the caller
    holds. Settlement holds none — it runs turns later, and a re-read manifest
    could state pins this dispatch was never decided on. So the obligation
    carries the two answers and this rebuilds the smallest shape that yields
    exactly them.
    """
    view: Dict[str, Any] = dict(obligation.get("requirements_pins") or {})
    domain = _text(obligation.get("domain_id"))
    if domain:
        view["build_domains"] = [{"root": domain}]
    return view


def _read_complete_log(orchestrator: Any, log_path: Any) -> Optional[str]:
    """The job's COMPLETE log, untruncated.

    Same reason `collect_detached_result` reads it untruncated: the analysis
    is parsed by regex and never reaches the model's context, and the
    orchestrator's emergency truncation would gut the middle of it — which is
    where a reactor summary lives.
    """
    path = _text(log_path)
    if not path:
        return None
    execute = resolve_control_execute(orchestrator)
    if not callable(execute):
        return None
    command = f"cat {shlex.quote(path)}"
    try:
        try:
            result = execute(command, workdir=None, timeout=120, truncate_output=False) or {}
        except TypeError:
            result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"job log {path} unreadable: {exc}")
        return None
    return str(result.get("output") or "") if _succeeded(result) else None


def _receipt_claims(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> Optional[List[Tuple[Optional[int], Tuple[str, ...]]]]:
    """What every existing receipt claims, each under its own ordinal.

    One bounded named stream reads the complete current receipt directory.
    Schema, filename, exact bytes, run/contract linkage and the host-owned
    expected id set must all agree before any path can participate in first-
    claim attribution.
    """
    ledger = _read_live_receipt_ledger(execute)
    if ledger is None:
        return None
    claims: List[Tuple[Optional[int], Tuple[str, ...]]] = []
    for payload in ledger.values():
        paths = tuple(_delta_paths(payload.get("report_delta")))
        if paths:
            claims.append((_receipt_sequence(payload.get("receipt_id")), paths))
    return claims


def _read_live_receipt_ledger(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the complete host-published current receipt ledger.

    There is deliberately no per-file/raw fallback.  A missing publication,
    deletion, rollback, malformed sibling, filename mismatch or incomplete
    expected set makes the whole attribution/settlement view unavailable.
    """

    read = read_live_published_json_records(
        execute,
        RECEIPT_DIR,
        record_kind="invocation_receipt",
        validator=lambda payload, expected_id: validate_receipt_v2(
            payload,
            expected_id=expected_id,
        ),
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=payload["run_id"],
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
        ),
        record_scope=receipt_record_scope,
    )
    if not read.complete or read.conflict is not None:
        logger.debug(
            f"{RECEIPT_DIR} live ledger unavailable: "
            f"{read.conflict or read.detail or 'incomplete'}"
        )
        return None
    return {record.source.record_id: dict(record.payload) for record in read.records}


def _intervening_claims(
    claims: Sequence[Tuple[Optional[int], Tuple[str, ...]]],
    dispatch_sequence: Optional[int],
) -> Set[str]:
    """The paths receipts written AFTER this dispatch already claimed.

    Two unknowns are treated the same way, and conservatively: an obligation
    with no recorded ordinal cannot tell a window from a history, and a receipt
    whose id carries no ordinal cannot be placed in one. Either way the path is
    excluded — a settling receipt never takes a path some other receipt vouched
    for when the harness cannot prove the vouching came later.
    """
    paths: Set[str] = set()
    for sequence, claimed in claims:
        if dispatch_sequence is None or sequence is None or sequence > dispatch_sequence:
            paths.update(claimed)
    return paths


def _dispatch_sequence(obligation: Mapping[str, Any]) -> Optional[int]:
    """Where this dispatch sits in receipt order, or None when unrecorded."""
    value = (obligation or {}).get("dispatch_sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _receipt_sequence(receipt_id: Any) -> Optional[int]:
    """The ordinal `next_receipt_id` stamped on a receipt id, or None.

    Ids are `inv-<slug>-<slug>-<seq:04d>` and `_slug` replaces every
    non-alphanumeric character with `_`, so the trailing dash-delimited field is
    the sequence and nothing else can be.
    """
    text = _text(receipt_id)
    if "-" not in text:
        return None
    try:
        return int(text.rsplit("-", 1)[-1])
    except ValueError:
        return None


def _delta_paths(delta: Any) -> List[str]:
    """Every report path one `report_delta` claims, in all three buckets."""
    paths: List[str] = []
    if not isinstance(delta, Mapping):
        return paths
    for bucket in ("new", "changed", "cached"):
        for entry in delta.get(bucket) or ():
            path = _text((entry or {}).get("path")) if isinstance(entry, Mapping) else ""
            if path and path not in paths:
                paths.append(path)
    return paths


def _lifecycle_state(record: Mapping[str, Any]) -> Tuple[str, str]:
    process = _text(record.get("process_state"))
    settlement = _text(record.get("settlement_state"))
    if process and settlement:
        return process, settlement
    # Explicit compatibility for schema-v1 ledgers. A settled v1 record is
    # terminal; an unsettled one remains conservatively live until its marker
    # is reconciled.
    if _text(record.get("settled_receipt_id")):
        return PROCESS_TERMINAL, SETTLEMENT_SETTLED
    return PROCESS_RUNNING, SETTLEMENT_NONE


def _without_lifecycle(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in record.items() if key not in _LIFECYCLE_FIELDS}


def _is_lifecycle_transition(existing: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Accept only an immutable dispatch plus one forward lifecycle edge."""
    existing_identity = _without_lifecycle(existing)
    payload_identity = _without_lifecycle(payload)
    # Schema v1 -> v3 is the one permitted identity normalization.
    if existing_identity.get("schema_version") == 1:
        existing_identity = {**existing_identity, "schema_version": OBLIGATION_SCHEMA_VERSION}
    if existing_identity != payload_identity:
        return False

    old = _lifecycle_state(existing)
    new = _lifecycle_state(payload)
    allowed = {
        ((PROCESS_RUNNING, SETTLEMENT_NONE), (PROCESS_TERMINAL, SETTLEMENT_PENDING)),
        ((PROCESS_TERMINAL, SETTLEMENT_PENDING), (PROCESS_TERMINAL, SETTLEMENT_PENDING)),
        ((PROCESS_TERMINAL, SETTLEMENT_PENDING), (PROCESS_TERMINAL, SETTLEMENT_SETTLED)),
        ((PROCESS_TERMINAL, SETTLEMENT_PENDING), (PROCESS_TERMINAL, SETTLEMENT_UNPERSISTED)),
    }
    if (old, new) not in allowed:
        return False

    old_exit = existing.get("terminal_exit_code")
    new_exit = payload.get("terminal_exit_code")
    if old[0] == PROCESS_TERMINAL and old_exit != new_exit:
        return False
    if new[0] == PROCESS_TERMINAL and (not isinstance(new_exit, int) or isinstance(new_exit, bool)):
        return False

    old_receipt = _text(existing.get("attempted_receipt_id"))
    new_receipt = _text(payload.get("attempted_receipt_id"))
    if old_receipt and old_receipt != new_receipt:
        return False
    if settlement_attempts_integrity_failure(existing) or settlement_attempts_integrity_failure(
        payload
    ):
        return False
    old_attempts = existing.get("settlement_attempts") or 0
    new_attempts = payload.get("settlement_attempts") or 0
    if (
        not isinstance(old_attempts, int)
        or isinstance(old_attempts, bool)
        or not isinstance(new_attempts, int)
        or isinstance(new_attempts, bool)
        or new_attempts < old_attempts
        or new_attempts > old_attempts + 1
    ):
        return False

    settled_id = _text(payload.get("settled_receipt_id"))
    code = _text(payload.get("receipt_persistence_code"))
    if new[1] == SETTLEMENT_SETTLED:
        return bool(settled_id and settled_id == new_receipt and not code)
    if new[1] == SETTLEMENT_UNPERSISTED:
        return bool(not settled_id and new_receipt and code)
    return not settled_id


def _reject_duplicate_object(pairs):
    body = {}
    for key, value in pairs:
        if key in body:
            raise ValueError(f"duplicate JSON key: {key}")
        body[key] = value
    return body


def _read_obligation_raw(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    path: str,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Return ``valid`` exact bytes, ``absent`` observation, or ``invalid``.

    A failed ``cat`` is only an absence candidate; the locked CAS later proves
    actual absence.  Any observed bytes must be exact canonical v3 before they
    can serve as a lifecycle predecessor.
    """

    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception:
        return "absent", None, None
    if not _succeeded(result):
        return "absent", None, None
    raw = str(result.get("output") or "")
    if not raw:
        return "invalid", raw, None
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_object)
        expected_id = posixpath.basename(path)[:-5]
        validated = validate_obligation_v3(payload, expected_id=expected_id)
        canonical = json.dumps(validated, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        return "invalid", raw, None
    if canonical != raw:
        return "invalid", raw, None
    return "valid", raw, validated


def _latest_obligation_authorized(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    payload: Mapping[str, Any],
    raw: bytes,
) -> bool:
    try:
        check = verify_latest_evidence_bytes(
            execute,
            record_kind="job_obligation",
            record_id=payload["job_id"],
            logical_artifact_id=payload["job_id"],
            raw=raw,
            run_id=payload["run_id"],
            contract_id=payload.get("contract_id"),
            contract_hash=payload.get("contract_hash"),
        )
    except Exception:
        return False
    return check.authorized


def _read_existing(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    path: str,
) -> Optional[Dict[str, Any]]:
    """The obligation already at `path`, or None when there is none to honour.

    An unparseable file is reported as a body that matches nothing, so the
    caller refuses instead of overwriting bytes it cannot account for.
    """
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"job obligation {path} unreadable: {exc}")
        return None
    content = str(result.get("output") or "").strip()
    if not _succeeded(result) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {"unparseable": path}
    return payload if isinstance(payload, dict) else {"unparseable": path}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


__all__ = [
    "DispatchObligationResult",
    "DETACHED_TERMINAL_AUTHORITY",
    "ExitObservation",
    "MAX_SETTLEMENT_PERSIST_ATTEMPTS",
    "OBLIGATION_DIR",
    "OBLIGATION_SCHEMA_VERSION",
    "ObligationReconciliation",
    "Settlement",
    "TerminalObservation",
    "TerminalUnpersisted",
    "blocks_model",
    "build_obligation",
    "is_terminal_unpersisted",
    "is_open",
    "observe_exit_marker",
    "observe_detached_terminal",
    "open_job_ids",
    "open_obligations",
    "process_is_live",
    "read_exit_code",
    "read_obligations",
    "record_dispatch_obligation",
    "record_dispatch_obligation_result",
    "reconcile_job_obligations",
    "settled_receipt_integrity_failure",
    "settlement_attempts_integrity_failure",
    "settlement_is_pending",
    "settle_open_obligations",
    "settlement_from_ledger",
    "validate_obligation_v3",
    "write_obligation",
]
