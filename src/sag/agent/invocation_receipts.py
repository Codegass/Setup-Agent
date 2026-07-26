"""Minimal invocation receipts for runner calls (Plan 5 Stage B, P0-A).

Ground-truth review 2026-07-26 (§"Evidence is snapshot-global instead of
receipt-scoped"): the validator scanned the project tree after several
invocations and treated every matching XML as current evidence. It could not
say which invocation wrote which report, so auxiliary reports and stale
retries entered the primary rollup (Bigtop's 54/54).

A receipt makes that answerable. Every physical maven/gradle/pytest runner
call brackets itself with a content-hash snapshot of the report XMLs under
its own scan roots and persists ONE atomic schema-v1 JSON file:

    /workspace/.setup_agent/invocation_receipts/<receipt_id>.json

Schema v1 is the cross-lane contract (plan §"Binding notes (Stage B)"): keys
are never renamed and the storage path never moves. Consumers (lane b2's
validator/gate) read the receipts instead of a recursive filesystem scan.

Persistence is best effort HERE: this module never raises and never blocks
the command result the model is waiting for. A failed write is reported as a
fact (`receipt_persisted: false` in ToolResult metadata); turning that fact
into a closure failure is the phase gate's business, not the runner's.
"""

import itertools
import json
import shlex
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from loguru import logger

RECEIPT_SCHEMA_VERSION = 1
RECEIPT_DIR = "/workspace/.setup_agent/invocation_receipts"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# receipt content can ever collide with it.
RECEIPT_HEREDOC = "SAGRECEIPT"

# What makes an XML a TEST REPORT. Mirrors the in-container `is_report_file`
# of physical_validator (surefire / failsafe / gradle test-results / pytest
# junit); the two must agree or a receipt would claim files the validator
# never scans — or miss files it does.
REPORT_PATH_MARKERS = (
    "/target/surefire-reports/",
    "/target/failsafe-reports/",
    "/build/test-results/",
    "/.setup_agent/pytest-reports/",
)

_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()


def next_sequence() -> int:
    """Process-global monotonic sequence — receipt ids cannot collide."""
    with _SEQUENCE_LOCK:
        return next(_SEQUENCE)


def next_receipt_id(scope: str, attempt: Any) -> str:
    """`inv-<phase-or-tool>-<attempt-or-seq>-<seq>` (plan §schema v1)."""
    return f"inv-{_slug(scope) or 'runner'}-{_slug(attempt) or '1'}-{next_sequence():04d}"


def snapshot_reports(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    scan_roots: Iterable[str],
) -> Dict[str, str]:
    """Content hashes of every report XML under `scan_roots`, path -> sha256.

    ONE shell round-trip per side of an invocation: `find` filters the report
    shapes itself (no xargs, no second `cat` pass) and hashes what it kept.
    A transport failure yields an empty snapshot rather than an exception —
    an unmeasurable delta must not break the build the model asked for.
    """
    roots = _unique_roots(scan_roots)
    if not roots:
        return {}
    predicates = " -o ".join(
        f"-path {shlex.quote(f'*{marker}*.xml')}" for marker in REPORT_PATH_MARKERS
    )
    command = (
        "find "
        + " ".join(shlex.quote(root) for root in roots)
        + f" -type f \\( {predicates} \\) -exec sha256sum {{}} + 2>/dev/null"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:  # evidence collection never breaks the runner
        logger.debug(f"report snapshot skipped: {exc}")
        return {}
    return _parse_sha256sum(result.get("output") or "")


def report_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> Dict[str, List[Dict[str, str]]]:
    """What THIS invocation wrote: reports that appeared or changed content.

    Unchanged files never appear — a byte-identical XML from an earlier
    attempt is not this invocation's evidence. Both keys are always present:
    an empty list is the stated fact "this invocation wrote no new/changed
    reports", which is exactly what the primary rollup needs to hear.
    """
    new: List[Dict[str, str]] = []
    changed: List[Dict[str, str]] = []
    for path in sorted(after):
        digest = after[path]
        if path not in before:
            new.append({"path": path, "sha256": digest})
        elif before[path] != digest:
            changed.append({"path": path, "sha256": digest})
    return {"new": new, "changed": changed}


def build_receipt(
    *,
    receipt_id: str,
    tool: str,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> Dict[str, Any]:
    """Assemble a schema-v1 receipt. Absent facts serialize as absent keys."""
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "tool": tool,
        "requested_action": requested_action,
        "effective_action": effective_action,
        "argv": argv,
        "working_directory": working_directory,
    }
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        receipt["exit_code"] = exit_code
    receipt["outcome"] = "completed" if exit_code == 0 else "failed"
    receipt["report_delta"] = report_delta(before, after)
    return receipt


def write_receipt(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt: Mapping[str, Any],
) -> bool:
    """Persist one receipt atomically (temp file + `mv`). False on failure.

    Never raises: the caller is mid-invocation and owes the model a result.
    """
    receipt_id = str((receipt or {}).get("receipt_id") or "").strip()
    if not receipt_id:
        return False
    try:
        body = json.dumps(receipt, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation receipt {receipt_id} is not serializable: {exc}")
        return False
    final = f"{RECEIPT_DIR}/{receipt_id}.json"
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(RECEIPT_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{RECEIPT_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{RECEIPT_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"invocation receipt {receipt_id} not persisted: {exc}")
        return False
    success = result.get("success")
    if success is None:
        success = result.get("exit_code") == 0
    return bool(success)


def record_invocation(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    tool: str,
    attempt: Any,
    requested_action: str,
    effective_action: str,
    argv: str,
    working_directory: str,
    exit_code: Optional[int],
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> Dict[str, Any]:
    """Persist the receipt for one runner call; return its ToolResult metadata.

    Byte-compat (Plans 2-4 pattern): a persisted receipt adds ONLY
    `receipt_id`; a failed write adds ONLY `receipt_persisted: false`.
    """
    receipt = build_receipt(
        receipt_id=next_receipt_id(tool, attempt),
        tool=tool,
        requested_action=requested_action,
        effective_action=effective_action,
        argv=argv,
        working_directory=working_directory,
        exit_code=exit_code,
        before=before,
        after=after,
    )
    if write_receipt(execute, receipt):
        return {"receipt_id": receipt["receipt_id"]}
    return {"receipt_persisted": False}


def _unique_roots(scan_roots: Iterable[str]) -> List[str]:
    roots: List[str] = []
    for raw in scan_roots or ():
        root = str(raw or "").strip()
        if not root:
            continue
        root = root.rstrip("/") or "/"
        if root not in roots:
            roots.append(root)
    return roots


def _parse_sha256sum(output: str) -> Dict[str, str]:
    """`<hash>  <path>` lines; anything else (stderr noise) is ignored."""
    snapshot: Dict[str, str] = {}
    for line in (output or "").splitlines():
        digest, separator, path = line.partition("  ")
        if not separator:
            continue
        # GNU sha256sum escapes newline/backslash filenames with a leading '\'.
        digest = digest.strip().lstrip("\\")
        path = path.strip()
        if not path or len(digest) != 64:
            continue
        try:
            int(digest, 16)
        except ValueError:
            continue
        snapshot[path] = digest
    return snapshot


def _slug(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in str(value or "").strip()
    ).strip("_")
