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
`snapshot_reports` before `_run_build`), the contract frozen by
`ensure_dispatch_contract` and the detach handle's `log_path` /
`exit_code_path` are all in hand. An OBLIGATION is that evidence written down:

    /workspace/.setup_agent/job_obligations/<job_id>.json

Persistence is the convention every other evidence directory already uses —
one atomic single-line JSON file, the same body under an existing id is a no-op
success, a DIFFERENT body under an existing id is refused rather than merged,
and this module never raises. The ledger is APPEND-ONLY with exactly one
permitted rewrite: `settled_receipt_id` moving from null to a receipt id.
Everything else about a dispatch was true when the dispatch happened.
"""

import json
import shlex
from typing import Any, Callable, Dict, List, Mapping, Optional

from loguru import logger

from .invocation_contracts import contract_receipt_fields
from .invocation_receipts import nearest_domain_root, survey_pins

OBLIGATION_SCHEMA_VERSION = 1
OBLIGATION_DIR = "/workspace/.setup_agent/job_obligations"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# obligation content can ever collide with it.
OBLIGATION_HEREDOC = "SAGJOBOBLIGATION"


def build_obligation(
    *,
    job_id: str,
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    before: Mapping[str, str],
    log_path: str,
    exit_code_path: str,
    contract_id: Optional[str] = None,
    contract_hash: Optional[str] = None,
    compliance: Optional[str] = None,
    requirements_pins: Optional[Mapping[str, str]] = None,
    domain_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble one obligation body. Absent facts serialize as absent keys.

    `requirements_pins` and `domain_id` are the two facts `record_invocation`
    would otherwise re-derive from the survey manifest the CALLER holds. The
    manifest is not carried into the ledger — settlement happens turns later
    and must state what was pinned at DISPATCH time, not what a re-read would
    say now.
    """
    obligation: Dict[str, Any] = {
        "schema_version": OBLIGATION_SCHEMA_VERSION,
        "job_id": _text(job_id),
        "tool": _text(tool),
        "attempt": attempt,
        "requested_action": _text(requested_action),
        "effective_action": _text(effective_action),
        "argv": _text(argv),
        "working_directory": _text(working_directory),
        "before": {str(path): str(digest) for path, digest in dict(before or {}).items()},
        "log_path": _text(log_path),
        "exit_code_path": _text(exit_code_path),
    }
    for key, value in (
        ("contract_id", contract_id),
        ("contract_hash", contract_hash),
        ("compliance", compliance),
        ("domain_id", domain_id),
    ):
        text = _text(value)
        if text:
            obligation[key] = text
    pins = {str(key): str(value) for key, value in dict(requirements_pins or {}).items() if value}
    if pins:
        obligation["requirements_pins"] = pins
    # The one mutable field, and it is present as an explicit null from birth:
    # "not settled yet" is a state the ledger states, not one a reader infers
    # from a missing key.
    obligation["settled_receipt_id"] = None
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
    """Record the obligation for one detached dispatch; return its job id.

    Returns None — and writes nothing — when the handoff names no job, no log
    or no exit-code path. An obligation nobody could ever settle is not a
    fact, it is a leak, and `job_unsettled` would then indict a job that never
    ran.

    Called from inside the runners' `dispatch_contract` scope, so the contract
    binding is read the same way the synchronous receipt reads it.
    """
    handle = result.get("dispatch") if isinstance(result, Mapping) else None
    handle = handle if isinstance(handle, Mapping) else {}
    job_id = _text(handle.get("job_id"))
    log_path = _text(handle.get("log_path"))
    exit_code_path = _text(handle.get("exit_code_path"))
    if not job_id or not log_path or not exit_code_path:
        return None
    obligation = build_obligation(
        job_id=job_id,
        tool=tool,
        attempt=attempt,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        before=before,
        log_path=log_path,
        exit_code_path=exit_code_path,
        requirements_pins=survey_pins(requirements),
        domain_id=nearest_domain_root(requirements, working_directory),
        **contract_receipt_fields(argv),
    )
    return job_id if write_obligation(execute, obligation) else None


def write_obligation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    obligation: Optional[Mapping[str, Any]],
) -> bool:
    """Persist one obligation atomically; True when the file holds this body.

    Same contract as `write_repair`/`write_claim`/`write_assessment`, plus the
    one transition the ledger's lifecycle needs: a body that differs from the
    stored one ONLY by `settled_receipt_id` moving from null to a receipt id
    is the settlement, and it is written. Every other difference is refused
    and logged rather than merged.
    """
    identifier = _text((obligation or {}).get("job_id"))
    if not identifier:
        return False
    payload = dict(obligation or {})
    try:
        body = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"job obligation {identifier} is not serializable: {exc}")
        return False
    final = f"{OBLIGATION_DIR}/{identifier}.json"
    existing = _read_existing(execute, final)
    if existing is not None:
        if existing == payload:
            return True
        if not _is_settlement_of(existing, payload):
            logger.warning(
                f"job obligation {identifier} already records a different dispatch; "
                "obligations are append-only and this write was refused"
            )
            return False
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(OBLIGATION_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{OBLIGATION_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{OBLIGATION_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"job obligation {identifier} not persisted: {exc}")
        return False
    return _succeeded(result)


def read_obligations(orchestrator: Any) -> List[Dict[str, Any]]:
    """Every readable obligation, in job-id order.

    One glob `cat`, and a line that does not parse is skipped rather than
    failing the read — the same discipline `repair_contracts.read_records`
    applies to every other evidence directory.
    """
    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        execute = orchestrator if callable(orchestrator) else None
    if execute is None:
        return []
    try:
        probe = execute(f"cat {shlex.quote(OBLIGATION_DIR)}/*.json 2>/dev/null")
    except Exception as exc:
        logger.debug(f"{OBLIGATION_DIR} unavailable: {exc}")
        return []
    records: List[Dict[str, Any]] = []
    for line in str((probe or {}).get("output") or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping) and _text(payload.get("job_id")):
            records.append(dict(payload))
    return sorted(records, key=lambda record: _text(record.get("job_id")))


def open_obligations(orchestrator: Any) -> List[Dict[str, Any]]:
    """The obligations no receipt has settled yet."""
    return [record for record in read_obligations(orchestrator) if is_open(record)]


def open_job_ids(orchestrator: Any) -> tuple:
    """Job ids of every unsettled obligation, in job-id order."""
    return tuple(_text(record.get("job_id")) for record in open_obligations(orchestrator))


def is_open(obligation: Optional[Mapping[str, Any]]) -> bool:
    return not _text((obligation or {}).get("settled_receipt_id"))


def _is_settlement_of(existing: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Whether `payload` is `existing` with its settlement written, and nothing
    else touched."""
    if not is_open(existing) or not _text(payload.get("settled_receipt_id")):
        return False
    return {key: value for key, value in existing.items() if key != "settled_receipt_id"} == {
        key: value for key, value in payload.items() if key != "settled_receipt_id"
    }


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
    "OBLIGATION_DIR",
    "OBLIGATION_HEREDOC",
    "OBLIGATION_SCHEMA_VERSION",
    "build_obligation",
    "is_open",
    "open_job_ids",
    "open_obligations",
    "read_obligations",
    "record_dispatch_obligation",
    "write_obligation",
]
