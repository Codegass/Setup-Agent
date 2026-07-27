"""Material-progress retry authority (Plan 6 Stage D1, spec §C7).

A dispatch that already failed once, run again byte-for-byte, learns nothing
and costs a full build. Plan 5 had no memory of that: every phase could re-run
the same reactor with the same argv against the same SHA until the loop breaker
noticed the repetition, long after the minutes were spent.

Spec §C7 gives recurrence an IDENTITY and one authority:

    retry_key = sha256(canonical({
        target_sha, domain_id?, normalized_action{tool, verb, argv_tokens},
        typed_code, environment_fingerprint?
    }))[:16]

* the CONTROLLER signs it — after a failure-class assessment closes a dispatch
  the engine records the key here (`record_failure`), because the engine is the
  layer that sees receipt, contract and assessment together;
* the FACADE validates it — before freezing the next contract the build tool
  recomputes the key over the ABOUT-TO-RUN action and refuses a repeat that
  carries no material delta (`RETRY_WITHOUT_DELTA`).

The facade keeps no recurrence state of its own (spec §C7: "it does not
maintain a second unsynchronized recurrence state"): it reads this ledger and
answers, nothing more.

    /workspace/.setup_agent/retry_ledger.json
    {retry_key: {"count": n, "last_contract_id": ..., "typed_code": ...}}

**What counts as material progress** (plan §Stage D item 2): a different argv,
a different environment fingerprint, an intent whose source is
`accepted_repair`, or a changed fact epoch. Bumping a revision, rewriting
prose and restating an expectation are NOT deltas — which is exactly why the
document-map pin is deliberately absent from `environment_fingerprint` below.

**Transient failures are budgeted, not forbidden** (item 3): a network blip or
a proxy timeout is a fact about the world, not about the project, so
`transient_network`/`timeout` may repeat identically inside a small budget.

**Lifecycle is not a retry** (item 4): a poll, a resume and a detached
completion observe a dispatch that already happened. They never write an entry
and there is therefore never one for them to be refused by.

Persistence is best effort (same contract as the other evidence writers): a
ledger that could not be written is a missing authority record, not a failed
build, and nothing in this module raises at its callers.
"""

import json
import posixpath
import shlex
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from loguru import logger

from sag.agent.control_events import canonical_sha256
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_receipts import target_sha as probe_target_sha
from sag.agent.repair_contracts import intent_source_for_dispatch

RETRY_LEDGER_PATH = "/workspace/.setup_agent/retry_ledger.json"
# Heredoc delimiter for the atomic rewrite. The body is single-line canonical
# JSON, so no ledger content can ever collide with it.
RETRY_LEDGER_HEREDOC = "SAGRETRY"
RETRY_KEY_DIGEST_CHARS = 16
# The typed refusal the facade returns, and the control code it records.
RETRY_WITHOUT_DELTA = "RETRY_WITHOUT_DELTA"
RETRY_WITHOUT_DELTA_CODE = "retry_without_delta"
# The one public facade the retry law governs. `deps` and probes ride the same
# tool, and they are governed by the same key — a repeated resolution that
# failed identically is as uninformative as a repeated compile.
RETRY_TOOL = "build"
# Spec §C7: a repair-authored intent is material progress by construction —
# acceptance created a NEW intent whose preconditions or action changed.
ACCEPTED_REPAIR_INTENT = "accepted_repair"
# Typed codes whose cause is the world rather than the project, and the number
# of dispatches each key may spend on them before a delta is required.
TRANSIENT_CODES = ("transient_network", "timeout")
TRANSIENT_RETRY_BUDGET = 2
# The success verdict (spec §C5). Everything else a dispatch is assessed as is
# a failure this ledger must remember.
EXPECTATION_MET = "expectation_met"
# Codes that ride ALONGSIDE a primary verdict instead of replacing it: a green
# suite that skipped its LLVM cases is still green, and refusing to run it
# again would turn an absence into a build failure.
RIDER_CODE_PREFIXES = ("capability_absent_",)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def argv_tokens(expected_argv: Any) -> List[str]:
    """The frozen argument vector, IN ORDER.

    Order is part of the identity: `-pl core test` and `test -pl core` are the
    same tokens and can be two different builds, so the vector is never sorted.
    Shell quoting is not identity, so both sides are tokenized first.
    """
    text = _text(expected_argv)
    if not text:
        return []
    try:
        return list(shlex.split(text))
    except ValueError:
        return text.split()


def normalized_action(contract: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """`{tool, verb, argv_tokens}` for the action a contract commits to.

    The verb is the EFFECTIVE action — what will physically run — not the
    public verb the model submitted: a `compile` promoted to `install` by the
    surveyed island goal is a different dispatch, and calling it the same one
    would refuse the promotion as a repeat of the thing it replaced.
    """
    requested = (contract or {}).get("requested_call")
    tool = _text(requested.get("tool")) if isinstance(requested, Mapping) else ""
    return {
        "tool": tool,
        "verb": _text((contract or {}).get("effective_action")),
        "argv_tokens": argv_tokens((contract or {}).get("expected_argv")),
    }


def environment_fingerprint(contract: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The environment pin this dispatch was decided on, or None.

    The CONFIG fingerprint is that pin: it is the survey's stamp of the
    toolchain and configuration the harness resolved. The document-map pin is
    deliberately not part of it — documentation is prose, and spec §C7 states
    that changing prose is not material progress. Folding it in here would make
    a re-indexed README a licence to repeat a failed build.
    """
    return _text((contract or {}).get("config_fingerprint")) or None


TOOLCHAIN_REGISTRY_PATH = "/workspace/.setup_agent/toolchains.json"


def toolchain_state_fingerprint(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> Optional[str]:
    """A content hash of the container's toolchain registry, or None.

    Live p6v-cli-r1: registering Maven 3.9.9 after a version-gate failure IS
    the material toolchain change spec §C7 names — but the config pin never
    moves, so the retry of the same compile was refused. The registry file is
    where that state lives; hashing its bytes makes any registration part of
    the retry identity without resolving a single executable.
    """
    try:
        result = execute(f"cat {TOOLCHAIN_REGISTRY_PATH} 2>/dev/null") or {}
    except Exception as exc:  # the ledger must never break a dispatch
        logger.debug(f"toolchain registry unreadable: {exc}")
        return None
    body = (result.get("output") or "").strip()
    if not result.get("success") or not body:
        return None
    return canonical_sha256({"toolchains": body})[:16]


def compute_retry_key(
    contract: Optional[Mapping[str, Any]],
    typed_code: Any,
    *,
    toolchain_state: Optional[str] = None,
) -> str:
    """The plan's `retry_key` for one (dispatch, typed failure) pair.

    Canonical hashing is `control_events.canonical_sha256`, so the ledger's
    identity is the same byte discipline every other digest in the evidence
    chain uses and any reader recomputes it without a parser of its own.

    Absent facts are absent KEYS, here as everywhere: a tree whose SHA could
    not be read, a dispatch outside every surveyed domain and a run with no
    config pin each contribute nothing rather than a null, so two dispatches
    that state the same facts hash the same and one that states more does not.
    """
    material: Dict[str, Any] = {
        "normalized_action": normalized_action(contract),
        "typed_code": _text(typed_code),
    }
    for key, value in (
        ("target_sha", _text((contract or {}).get("target_sha"))),
        ("domain_id", _text((contract or {}).get("domain_id"))),
        ("environment_fingerprint", environment_fingerprint(contract)),
        ("toolchain_state", _text(toolchain_state)),
    ):
        if value:
            material[key] = value
    return canonical_sha256(material)[:RETRY_KEY_DIGEST_CHARS]


def candidate_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    tool: str,
    effective_action: str,
    expected_cwd: str,
    expected_argv: Optional[str],
    requirements: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The retry-relevant view of the dispatch that is ABOUT to be frozen.

    The key must be computed BEFORE the freeze (plan §Stage D item 2) — a
    refused dispatch must leave no contract behind — so this states the same
    pins `freeze_contract` will state, read from the manifest the caller
    already holds plus the one probe the contract is a commitment against.

    It is a contract-LIKE mapping, not a contract: it carries exactly the
    fields the retry law reads and no identity, because nothing here is
    persisted or dispatched. `tests/test_retry_authority.py` asserts that the
    key taken over this view equals the key taken over the contract the real
    freeze produces for the same dispatch, so the two cannot drift apart
    silently.
    """
    candidate: Dict[str, Any] = {
        "requested_call": {"tool": _text(tool)},
        "effective_action": _text(effective_action),
        "intent_source": intent_source_for_dispatch(),
    }
    argv = _text(expected_argv)
    if argv:
        candidate["expected_argv"] = argv
    sha = probe_target_sha(execute, expected_cwd)
    if sha:
        candidate["target_sha"] = sha
    config = _config_fingerprint(requirements)
    if config:
        candidate["config_fingerprint"] = config
    fact = _domain_fact(requirements, expected_cwd)
    domain_id = _text((fact or {}).get("domain_id"))
    if domain_id:
        candidate["domain_id"] = domain_id
    epoch = (fact or {}).get("fact_epoch")
    if isinstance(epoch, int) and not isinstance(epoch, bool):
        candidate["fact_epoch"] = epoch
    return candidate


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def read_ledger(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Every recorded retry key, or an empty ledger when there is none.

    A ledger that cannot be read and a ledger that does not parse are the same
    answer: EMPTY. The alternative — refusing every dispatch because a JSON
    file is truncated — would let one corrupt byte end a run, so a corrupt
    ledger is logged loudly and then treated as no ledger at all.
    """
    try:
        result = execute(f"cat {shlex.quote(RETRY_LEDGER_PATH)}") or {}
    except Exception as exc:
        logger.debug(f"retry ledger unreadable: {exc}")
        return {}
    if not _succeeded(result):
        return {}
    content = str(result.get("output") or "").strip()
    if not content:
        return {}
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, Mapping):
        logger.warning(
            f"retry ledger at {RETRY_LEDGER_PATH} is not a readable key map; it is read "
            "as empty and the next recorded failure rewrites it"
        )
        return {}
    ledger: Dict[str, Dict[str, Any]] = {}
    for key, entry in payload.items():
        if _text(key) and isinstance(entry, Mapping):
            ledger[_text(key)] = dict(entry)
    return ledger


def write_ledger(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    ledger: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Rewrite the whole ledger atomically (temp file + `mv`). False on failure.

    The ledger is small and bounded by the number of distinct failed dispatches
    in one run, so a full rewrite is cheaper than a merge protocol — and a
    reader never sees a half-written authority record. The bytes are the plan's
    shape verbatim: `{retry_key: {count, last_contract_id, typed_code}}`.
    """
    try:
        body = json.dumps(
            {str(key): dict(value) for key, value in dict(ledger).items()},
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        logger.debug(f"retry ledger is not serializable: {exc}")
        return False
    directory = posixpath.dirname(RETRY_LEDGER_PATH)
    temp = f"{RETRY_LEDGER_PATH}.tmp"
    command = (
        f"mkdir -p {shlex.quote(directory)} && "
        f"cat > {shlex.quote(temp)} <<'{RETRY_LEDGER_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(RETRY_LEDGER_PATH)}\n"
        f"{body}\n{RETRY_LEDGER_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"retry ledger not persisted: {exc}")
        return False
    return _succeeded(result)


def record_failure(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    retry_key: Any,
    contract_id: Any,
    typed_code: Any,
) -> Optional[Dict[str, Any]]:
    """Record one failed dispatch under its key; the entry, or None.

    The count is per KEY, so the same failure of the same action against the
    same tree accumulates while a different action starts at one. The contract
    id is the LAST one — the delta check reads it back to compare the candidate
    against what actually ran, and older dispatches under the same key ran the
    same action by construction.
    """
    key = _text(retry_key)
    code = _text(typed_code)
    identifier = _text(contract_id)
    if not key or not code:
        return None
    ledger = read_ledger(execute)
    previous = ledger.get(key) or {}
    entry: Dict[str, Any] = {
        "count": _count(previous) + 1,
        "typed_code": code,
    }
    last_contract = identifier or _text(previous.get("last_contract_id"))
    if last_contract:
        entry["last_contract_id"] = last_contract
    ledger[key] = entry
    if not write_ledger(execute, ledger):
        logger.debug(f"retry key {key} was not recorded; the ledger did not reach disk")
        return None
    return entry


# ---------------------------------------------------------------------------
# the law
# ---------------------------------------------------------------------------


def material_delta(
    candidate: Optional[Mapping[str, Any]],
    entry: Optional[Mapping[str, Any]],
    prior_contract: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Whether this dispatch differs from the recorded one in a way that can
    change the typed cause (spec §C7).

    The four deltas the plan names, and nothing else:

    1. different argv tokens — a different vector can fail differently;
    2. a different environment fingerprint — the toolchain moved;
    3. `intent_source == accepted_repair` — acceptance minted a NEW intent
       whose preconditions or action changed (spec §C6);
    4. a changed fact epoch — the survey learned something since.

    A fact only one side states is UNKNOWN, never a change: the same rule the
    receipt assessor applies to stale pins. Claiming a delta from an absent
    prior contract would make every unreadable file a licence to repeat.

    `entry` names the recorded failure this candidate is being compared with.
    None of the four deltas is a property of the ledger row — the row's count
    is the TRANSIENT budget's business — so it is read here only to keep the
    comparison's two subjects in one signature.
    """
    if _text((candidate or {}).get("intent_source")) == ACCEPTED_REPAIR_INTENT:
        return True
    if not prior_contract:
        return False
    now_tokens = argv_tokens((candidate or {}).get("expected_argv"))
    was_tokens = argv_tokens(prior_contract.get("expected_argv"))
    if now_tokens and was_tokens and now_tokens != was_tokens:
        return True
    now_environment = environment_fingerprint(candidate)
    was_environment = environment_fingerprint(prior_contract)
    if now_environment and was_environment and now_environment != was_environment:
        return True
    now_epoch = _epoch((candidate or {}).get("fact_epoch"))
    was_epoch = _epoch(prior_contract.get("fact_epoch"))
    if now_epoch is not None and was_epoch is not None and now_epoch != was_epoch:
        return True
    return False


def transient_allowance(typed_code: Any, count: Any) -> bool:
    """Whether a typed transient failure may be repeated identically again.

    Only the world-caused codes are budgeted, and the budget is the number of
    dispatches one key may spend on them: with `TRANSIENT_RETRY_BUDGET` of two,
    the second dispatch is allowed and the third is refused (plan §Stage D
    item 3). A deterministic failure has no budget at all.
    """
    if _text(typed_code) not in TRANSIENT_CODES:
        return False
    return _nonnegative_int(count) < TRANSIENT_RETRY_BUDGET


def blocking_entry(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    candidate: Optional[Mapping[str, Any]],
    *,
    ledger: Optional[Mapping[str, Mapping[str, Any]]] = None,
    read_contract: Optional[Callable[[Any], Optional[Mapping[str, Any]]]] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """`(retry_key, entry)` when this dispatch is a repeat with no delta.

    The ledger names the typed codes it has seen, so the candidate key is
    computed once per RECORDED code rather than over a vocabulary this module
    would otherwise have to enumerate. The first recorded key the candidate
    reproduces, and neither a material delta nor a transient budget excuses,
    blocks the dispatch.

    `ledger` is for the caller that already read it — a run with no recorded
    failure has no reason to build a candidate at all, so the facade checks
    that first and hands the entries down rather than reading them twice.
    """
    if ledger is None:
        ledger = read_ledger(execute)
    if not ledger:
        return None
    reader = read_contract or (lambda identifier: read_frozen_contract(execute, identifier))
    # The candidate's identity includes the toolchain registry AS OF NOW: a
    # registration between the recorded failure and this dispatch changes the
    # key, so the version-recovery retry is a new dispatch, not a repeat.
    toolchain_state = toolchain_state_fingerprint(execute)
    for code in sorted({_text(entry.get("typed_code")) for entry in ledger.values()} - {""}):
        key = compute_retry_key(candidate, code, toolchain_state=toolchain_state)
        entry = ledger.get(key)
        if not entry or _text(entry.get("typed_code")) != code:
            continue
        count = _count(entry)
        if transient_allowance(code, count):
            continue
        if material_delta(candidate, entry, reader(entry.get("last_contract_id"))):
            continue
        return key, dict(entry)
    return None


# ---------------------------------------------------------------------------
# the evidence this authority is signed from
# ---------------------------------------------------------------------------


def read_frozen_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    contract_id: Any,
) -> Optional[Dict[str, Any]]:
    """The persisted contract `contract_id`, or None when it cannot be read."""
    identifier = _text(contract_id)
    if not identifier:
        return None
    # Imported here: `invocation_contracts` is the freeze, and importing it at
    # module scope would tie this module's import order to the facade's.
    from sag.agent.invocation_contracts import CONTRACT_DIR

    payload = _read_json(execute, f"{CONTRACT_DIR}/{identifier}.json")
    return payload if isinstance(payload, dict) else None


def failure_codes(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    receipt_id: Any,
) -> List[str]:
    """The typed failure codes assessed against ONE receipt, in stable order.

    `expectation_met` is the success verdict and is not a failure; a
    `capability_absent_*` rider states an absence alongside whatever the
    primary verdict was, so on its own it never makes a dispatch a failed one.
    Recording it would refuse the next run of a suite that passed.
    """
    subject = _text(receipt_id)
    if not subject:
        return []
    codes = set()
    for record in _read_records(execute, ASSESSMENT_DIR):
        if _text(record.get("receipt_id")) != subject:
            continue
        code = _text(record.get("typed_code"))
        if not code or code == EXPECTATION_MET or code.startswith(RIDER_CODE_PREFIXES):
            continue
        codes.add(code)
    return sorted(codes)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _domain_fact(
    requirements: Optional[Mapping[str, Any]],
    working_directory: str,
) -> Optional[Dict[str, Any]]:
    """The `DomainFacts` record this dispatch belongs to (nearest root wins).

    The same containment rule the freeze applies (`invocation_contracts`) and
    the receipt's domain lookup applies: one invocation belongs to ONE domain.
    It is restated rather than imported because the freeze's copy is private to
    the moment a contract is built, and the equivalence is asserted by test
    rather than assumed.
    """
    directory = _normalized_root(working_directory)
    if not directory or not isinstance(requirements, Mapping):
        return None
    facts = requirements.get("domain_facts")
    if not isinstance(facts, (list, tuple)):
        return None
    best: Optional[Dict[str, Any]] = None
    best_root = ""
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        root = _normalized_root(fact.get("root"))
        if not root or not (directory == root or directory.startswith(f"{root}/")):
            continue
        if len(root) >= len(best_root):
            best, best_root = dict(fact), root
    return best


def _config_fingerprint(requirements: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The config pin AS THE SURVEY RECORDED IT (read-through, never a probe)."""
    if not isinstance(requirements, Mapping):
        return None
    stamp = requirements.get("survey")
    value = stamp.get("config_fingerprint") if isinstance(stamp, Mapping) else None
    if value is None:
        value = requirements.get("config_fingerprint")
    return _text(value) or None


def _read_records(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    directory: str,
) -> List[Dict[str, Any]]:
    """Every readable single-line JSON record in one evidence directory."""
    try:
        probe = execute(f"cat {shlex.quote(directory)}/*.json 2>/dev/null") or {}
    except Exception as exc:
        logger.debug(f"{directory} unavailable for the retry authority: {exc}")
        return []
    records: List[Dict[str, Any]] = []
    for line in str(probe.get("output") or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def _read_json(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    path: str,
) -> Optional[Dict[str, Any]]:
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"{path} unreadable for the retry authority: {exc}")
        return None
    content = str(result.get("output") or "").strip()
    if not _succeeded(result) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _nonnegative_int(value: Any) -> int:
    """A stated count, or zero — a ledger row that states nonsense states none."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _count(entry: Optional[Mapping[str, Any]]) -> int:
    return _nonnegative_int((entry or {}).get("count"))


def _epoch(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _normalized_root(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    return posixpath.normpath(raw).rstrip("/") or "/"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


__all__ = [
    "ACCEPTED_REPAIR_INTENT",
    "EXPECTATION_MET",
    "RETRY_KEY_DIGEST_CHARS",
    "RETRY_LEDGER_HEREDOC",
    "RETRY_LEDGER_PATH",
    "RETRY_TOOL",
    "RETRY_WITHOUT_DELTA",
    "RETRY_WITHOUT_DELTA_CODE",
    "RIDER_CODE_PREFIXES",
    "TRANSIENT_CODES",
    "TRANSIENT_RETRY_BUDGET",
    "argv_tokens",
    "blocking_entry",
    "candidate_contract",
    "compute_retry_key",
    "environment_fingerprint",
    "failure_codes",
    "material_delta",
    "normalized_action",
    "read_frozen_contract",
    "read_ledger",
    "record_failure",
    "transient_allowance",
    "write_ledger",
]
