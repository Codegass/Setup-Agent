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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from .evidence_assessments import ensure_receipt_assessed
from .invocation_contracts import contract_receipt_fields
from .invocation_receipts import (
    RECEIPT_DIR,
    nearest_domain_root,
    record_invocation,
    report_delta,
    snapshot_reports,
    survey_pins,
)

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


# ---------------------------------------------------------------------------
# settlement (spec §3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settlement:
    """What one settled obligation did, for the notice and the event."""

    job_id: str
    receipt_id: str
    exit_code: int
    claimed_paths: int
    excluded_claimed_paths: int = 0
    contract_id: str = ""

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
            line += f" ({self.excluded_claimed_paths} already claimed by an earlier receipt)"
        return line

    def event_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "receipt_id": self.receipt_id,
            "exit_code": self.exit_code,
        }


def settle_open_obligations(orchestrator: Any) -> List[Settlement]:
    """Settle every obligation whose job has terminated. Never raises.

    Idempotent by construction: an obligation with a `settled_receipt_id` is
    skipped, and a missing exit file leaves its obligation open for the next
    sweep. A job that never terminates is never guessed at.

    Obligations are settled in job-id order and each settlement's claims are
    folded into the claimed set as it goes, so two jobs that terminated in the
    same window cannot both claim one report file.
    """
    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        return []
    try:
        pending = open_obligations(orchestrator)
    except Exception as exc:  # a ledger read never breaks the run
        logger.debug(f"job obligations could not be swept: {exc}")
        return []
    if not pending:
        return []
    claimed = _claimed_report_paths(execute)
    settlements: List[Settlement] = []
    for obligation in pending:
        try:
            settlement = _settle_one(orchestrator, obligation, claimed)
        except Exception as exc:  # settlement never breaks the run
            logger.debug(f"job {obligation.get('job_id')} could not be settled: {exc}")
            continue
        if settlement is None:
            continue
        settlements.append(settlement)
    return settlements


def settlement_from_ledger(orchestrator: Any, obligation: Mapping[str, Any]) -> Optional[Settlement]:
    """Rebuild the `Settlement` of an ALREADY settled obligation, from disk.

    The engine announces settlements (one control event, one notice, the
    post-receipt hooks) whoever performed them — the phase gate settles too,
    so that a claim is never graded against moving books. Reading the receipt
    back is how the announcement states what the receipt says rather than what
    the announcer guessed.
    """
    execute = getattr(orchestrator, "execute_command", None)
    receipt_id = _text((obligation or {}).get("settled_receipt_id"))
    if not callable(execute) or not receipt_id:
        return None
    receipt = _read_existing(execute, f"{RECEIPT_DIR}/{receipt_id}.json") or {}
    exit_code = receipt.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    return Settlement(
        job_id=_text(obligation.get("job_id")),
        receipt_id=receipt_id,
        exit_code=exit_code,
        claimed_paths=len(_delta_paths(receipt.get("report_delta"))),
        excluded_claimed_paths=int(receipt.get("excluded_claimed_paths") or 0),
        contract_id=_text(obligation.get("contract_id")),
    )


def read_exit_code(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    exit_code_path: Any,
) -> Optional[int]:
    """The job's terminal exit code, or None while it has not written one.

    The launcher publishes this file atomically (`printf … > tmp && mv tmp
    final`, docker_orch/orch.py:979-984), so a file that exists holds a
    complete status. Anything else — no file, an unreadable one, a body that
    is not an integer — is "not terminal yet", never a guessed outcome.
    """
    path = _text(exit_code_path)
    if not path:
        return None
    try:
        result = execute(f"cat {shlex.quote(path)} 2>/dev/null") or {}
    except Exception as exc:
        logger.debug(f"exit code {path} unreadable: {exc}")
        return None
    if not _succeeded(result):
        return None
    for line in str(result.get("output") or "").splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            return int(token)
        except ValueError:
            return None
    return None


def _settle_one(
    orchestrator: Any,
    obligation: Mapping[str, Any],
    claimed: Set[str],
) -> Optional[Settlement]:
    execute = orchestrator.execute_command
    exit_code = read_exit_code(execute, obligation.get("exit_code_path"))
    if exit_code is None:
        return None
    working_directory = _text(obligation.get("working_directory"))
    tool = _text(obligation.get("tool"))
    before = dict(obligation.get("before") or {})
    after = snapshot_reports(execute, [working_directory])
    log = _read_complete_log(orchestrator, obligation.get("log_path"))
    module_outcomes, cached_roots = _parse_outcomes(tool, log, working_directory)

    # Attribution (spec §3.2). The window is this job's own `before` against
    # its own `after`; another dispatch may have written into the same roots
    # while it ran, and receipts are ORDERED, so a path an earlier receipt
    # already claimed is excluded here rather than counted twice. Dropping it
    # from `after` is exactly that exclusion: `report_delta` records no
    # deletions, so a path that is not in `after` is claimed by nobody here.
    excluded = sorted(
        path for path in _delta_paths(report_delta(before, after, cached_roots)) if path in claimed
    )
    if excluded:
        after = {path: digest for path, digest in after.items() if path not in set(excluded)}

    metadata = record_invocation(
        execute,
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
        compliance=obligation.get("compliance"),
        module_outcomes=module_outcomes,
        cached_report_roots=cached_roots,
        excluded_claimed_paths=len(excluded),
    )
    receipt_id = _text((metadata or {}).get("receipt_id"))
    if not receipt_id:
        # The receipt did not land. The obligation stays open and the next
        # sweep tries again; a settled book with no receipt would be a lie.
        return None
    claimed.update(_delta_paths(report_delta(before, after, cached_roots)))
    write_obligation(execute, {**dict(obligation), "settled_receipt_id": receipt_id})
    # The same post-receipt hook the synchronous path runs at the observation
    # seam, so a settled failure is assessed exactly like a synchronous one.
    ensure_receipt_assessed(execute, receipt_id)
    return Settlement(
        job_id=_text(obligation.get("job_id")),
        receipt_id=receipt_id,
        exit_code=exit_code,
        claimed_paths=len(_delta_paths(report_delta(before, after, cached_roots))),
        excluded_claimed_paths=len(excluded),
        contract_id=_text(obligation.get("contract_id")),
    )


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


def _read_complete_log(orchestrator: Any, log_path: Any) -> str:
    """The job's COMPLETE log, untruncated.

    Same reason `collect_detached_result` reads it untruncated: the analysis
    is parsed by regex and never reaches the model's context, and the
    orchestrator's emergency truncation would gut the middle of it — which is
    where a reactor summary lives.
    """
    path = _text(log_path)
    if not path:
        return ""
    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        return ""
    command = f"cat {shlex.quote(path)}"
    try:
        try:
            result = execute(command, workdir=None, timeout=120, truncate_output=False) or {}
        except TypeError:
            result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"job log {path} unreadable: {exc}")
        return ""
    return str(result.get("output") or "") if _succeeded(result) else ""


def _claimed_report_paths(execute: Callable[..., Optional[Mapping[str, Any]]]) -> Set[str]:
    """Every report path any existing receipt already claims.

    One glob `cat` of the receipt directory, read with the same discipline as
    every other evidence directory: a line that does not parse is skipped.
    """
    try:
        probe = execute(f"cat {shlex.quote(RECEIPT_DIR)}/*.json 2>/dev/null") or {}
    except Exception as exc:
        logger.debug(f"{RECEIPT_DIR} unavailable for attribution: {exc}")
        return set()
    claimed: Set[str] = set()
    for line in str(probe.get("output") or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            claimed.update(_delta_paths(payload.get("report_delta")))
    return claimed


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
    "Settlement",
    "build_obligation",
    "is_open",
    "open_job_ids",
    "open_obligations",
    "read_exit_code",
    "read_obligations",
    "record_dispatch_obligation",
    "settle_open_obligations",
    "settlement_from_ledger",
    "write_obligation",
]
