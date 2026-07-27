"""Pre-dispatch invocation contracts (Plan 6 Stage B, spec §C3).

A receipt says what a runner DID. A contract says what the harness committed
to running BEFORE anything ran — and it is written to disk first, so the
commitment cannot be edited once the output is known. Without it every
"we ran X" is a post-hoc reading of a log the same run produced.

    /workspace/.setup_agent/invocation_contracts/<contract_id>.json

**Where the freeze happens (plan §Stage B binding decision).** The engine
emits the `action_envelope` BEFORE tool execution, and the materialized argv
exists only inside the build facade. So the contract is frozen INSIDE
`build_tool`, after the backend materializes the effective action and argv
(dry — no dispatch) and strictly before the runner runs. It records the
`envelope_id` of the engine's envelope, and the invocation receipt carries the
`contract_id`/`contract_hash` back, which is the chain the verifier walks:
envelope -> contract -> receipt.

**Identity.** `contract_id = "ic-" + sha256(envelope_id + expected_argv)[:12]`
— the Stage A fact-id shape (`stable_fact_id`), so two dispatches of the same
argv under two envelopes are two contracts, and re-freezing the same dispatch
is idempotent. `contract_hash = canonical_sha256(payload sans contract_hash)`,
over the same canonical bytes the file is written in, so any reader recomputes
it without a parser of its own.

**Absent facts are absent keys.** Every pin — target sha, config and
document-map fingerprints, fact epoch, domain, blocking conflicts, expected
argv — is written only when the harness actually knows it. A contract never
guesses a pin, and it never records a null.

**Request scope.** The envelope identity and the intent source live in a
thread-local scope the engine opens around one tool call (`action_context`),
because the tool layer has no other view of the engine's envelope. The frozen
contract is published into the same scope for the duration of the dispatch
(`dispatch_contract`) so the runner that writes the receipt can bind it back
without a new parameter on every internal tool signature.

Persistence is NOT best effort here — this is the one write in the evidence
chain that gates execution. `freeze_contract` returns None when the contract
did not land, and the facade must refuse the dispatch (fail closed,
`CONTRACT_PERSIST_FAILED`).
"""

import hashlib
import itertools
import posixpath
import shlex
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.control_events import canonical_json, canonical_sha256
from sag.agent.invocation_receipts import target_sha as probe_target_sha

CONTRACT_SCHEMA_VERSION = 1
CONTRACT_DIR = "/workspace/.setup_agent/invocation_contracts"
# Heredoc delimiter for the atomic write. The body is single-line canonical
# JSON, so no contract content can ever collide with it.
CONTRACT_HEREDOC = "SAGCONTRACT"
# The typed refusal a facade returns when the contract did not reach disk.
CONTRACT_PERSIST_FAILED = "CONTRACT_PERSIST_FAILED"
# Who authored the intent. `accepted_repair` arrives with Stage C.
INTENT_SOURCES = ("model", "controller")
DEFAULT_INTENT_SOURCE = "model"
# What a dispatch records when no engine envelope was emitted for it (no
# control-event sink configured). A stated absence, never a borrowed identity,
# and unique per dispatch so two contracts never collide on one sentinel.
UNRECORDED_ENVELOPE_PREFIX = "envelope-unrecorded"
# Conflict kinds that BLOCK a dispatch. An incomplete document map is an
# unknown, not a block, so it is not a blocking conflict id (spec §C2).
BLOCKING_CONFLICT_KINDS = ("version_incompatible",)

# What a dispatch of each PUBLIC verb promises to leave behind (Plan 6 Stage C,
# spec §C5). The typed expectation is what the assessor compares the receipt
# against, so it is decided here — before the run — and never after it.
#
# `deps` is deliberately absent: dependency resolution writes no report and no
# artifact, so exit 0 with an empty delta is its NORMAL outcome and a contract
# that expected otherwise would falsify every successful resolution.
EXPECTED_OBSERVATIONS = {
    "build": ("artifact_or_report_delta",),
    "compile": ("artifact_or_report_delta",),
    "install": ("artifact_or_report_delta",),
    "package": ("artifact_or_report_delta",),
    "test": ("report_delta",),
}
# The v1 typed predicate a receipt may be measured against: an exit 0 that left
# nothing observable behind (the gradle NO-SOURCE shape). This is the ONLY
# licence the assessor has to contradict a claim, so the set stays minimal.
DIRECT_FALSIFIERS = (
    {"predicate_id": "empty_delta_despite_success", "kind": "delta_empty_on_exit0"},
)

_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()
_SCOPE = threading.local()


@dataclass(frozen=True)
class ActionContext:
    """The engine's identity for the tool call currently executing."""

    envelope_id: Optional[str] = None
    intent_source: str = DEFAULT_INTENT_SOURCE


_EMPTY_CONTEXT = ActionContext()


def contract_identity(envelope_id: str, expected_argv: Optional[str]) -> str:
    """``ic-<sha256(envelope_id + expected_argv)[:12]>`` (plan §Stage B).

    An unknown argv contributes the empty string rather than a placeholder:
    the envelope alone still identifies the dispatch, and the contract states
    that it pinned no argv by omitting the key.
    """
    material = f"{str(envelope_id or '')}{str(expected_argv or '')}"
    return f"ic-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def contract_hash(payload: Mapping[str, Any]) -> str:
    """``canonical_sha256`` of the payload WITHOUT its own hash field."""
    return canonical_sha256(
        {key: value for key, value in dict(payload).items() if key != "contract_hash"}
    )


def unrecorded_envelope_id() -> str:
    """One sentinel per dispatch for a run with no engine envelope."""
    with _SEQUENCE_LOCK:
        sequence = next(_SEQUENCE)
    return f"{UNRECORDED_ENVELOPE_PREFIX}-{sequence:06d}"


def expected_observations(action: Any) -> List[str]:
    """What a dispatch of this public verb promises to leave behind.

    A verb the table does not name promises NOTHING — an empty list, written as
    an absent key. Guessing an expectation is how a harness invents a mismatch.
    """
    return list(EXPECTED_OBSERVATIONS.get(_text(action).lower(), ()))


def direct_falsifiers(action: Any) -> List[Dict[str, str]]:
    """The typed predicates that may contradict a claim for this verb.

    Bound to `expected_observations`: with nothing expected there is nothing to
    falsify, so a verb that promises no observation names no falsifier either.
    """
    if not expected_observations(action):
        return []
    return [dict(falsifier) for falsifier in DIRECT_FALSIFIERS]


def build_contract(
    *,
    envelope_id: str,
    tool: str,
    params: Optional[Mapping[str, Any]],
    effective_action: str,
    expected_cwd: str,
    expected_argv: Optional[str] = None,
    intent_source: str = DEFAULT_INTENT_SOURCE,
    target_sha: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    document_map_fingerprint: Optional[str] = None,
    fact_epoch: Optional[int] = None,
    domain_id: Optional[str] = None,
    blocking_conflict_ids: Optional[Sequence[str]] = None,
    predecessor_contract_id: Optional[str] = None,
    expected_observations: Optional[Sequence[str]] = None,
    direct_falsifiers: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble one schema-v1 contract. Pure — no probes, no I/O.

    Field names and shapes are the plan's Stage B v1 list verbatim, plus the
    Stage C typed expectations (`expected_observations`/`direct_falsifiers`)
    the assessor reads back; `supersedes_contract_id` is not invented here.
    """
    argv = _text(expected_argv)
    identifier = contract_identity(envelope_id, argv)
    contract: Dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": identifier,
        "envelope_id": _text(envelope_id),
    }
    for key, value in (
        ("target_sha", target_sha),
        ("config_fingerprint", config_fingerprint),
        ("document_map_fingerprint", document_map_fingerprint),
        ("domain_id", domain_id),
    ):
        text = _text(value)
        if text:
            contract[key] = text
    if isinstance(fact_epoch, int) and not isinstance(fact_epoch, bool):
        contract["fact_epoch"] = fact_epoch
    contract["intent_source"] = (
        intent_source if intent_source in INTENT_SOURCES else DEFAULT_INTENT_SOURCE
    )
    contract["requested_call"] = {
        "tool": _text(tool),
        "params": {
            str(key): value for key, value in dict(params or {}).items() if value is not None
        },
    }
    contract["effective_action"] = _text(effective_action)
    contract["expected_cwd"] = _text(expected_cwd)
    if argv:
        contract["expected_argv"] = argv
    observations = [_text(value) for value in expected_observations or ()]
    observations = [value for value in observations if value]
    if observations:
        contract["expected_observations"] = observations
        predicates = [
            {str(key): _text(value) for key, value in dict(falsifier).items()}
            for falsifier in direct_falsifiers or ()
            if isinstance(falsifier, Mapping)
        ]
        if predicates:
            contract["direct_falsifiers"] = predicates
    conflicts = [_text(value) for value in blocking_conflict_ids or ()]
    conflicts = [value for value in conflicts if value]
    if conflicts:
        contract["blocking_conflict_ids"] = conflicts
    predecessor = _text(predecessor_contract_id)
    if predecessor:
        contract["predecessor_contract_id"] = predecessor
    contract["contract_hash"] = contract_hash(contract)
    return contract


def write_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    contract: Mapping[str, Any],
) -> bool:
    """Persist one contract atomically (temp file + `mv`). False on failure.

    The bytes written are the canonical bytes the hash was taken over, so a
    reader recomputes `contract_hash` from the file itself.
    """
    identifier = _text((contract or {}).get("contract_id"))
    if not identifier:
        return False
    try:
        body = canonical_json(dict(contract))
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation contract {identifier} is not serializable: {exc}")
        return False
    final = f"{CONTRACT_DIR}/{identifier}.json"
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(CONTRACT_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{CONTRACT_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{CONTRACT_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"invocation contract {identifier} not persisted: {exc}")
        return False
    return _succeeded(result)


def freeze_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    envelope_id: str,
    tool: str,
    params: Optional[Mapping[str, Any]],
    effective_action: str,
    expected_cwd: str,
    expected_argv: Optional[str],
    intent_source: str,
    requirements: Optional[Mapping[str, Any]],
    document_map_fingerprint: Optional[str] = None,
    predecessor_contract_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Freeze and persist the contract for ONE dispatch; None when it failed.

    The pins are read from the manifest the caller ALREADY holds (the survey
    handoff), never from a second manifest probe; only the target sha is
    probed, from the tree this dispatch will run in, because that is the fact
    the contract is a commitment against.

    A None return is not a warning — the caller has no authority to dispatch.

    The typed expectations are derived from the PUBLIC verb the model
    submitted, not from the materialized action: `verify`, `assemble` and a
    gradle task list are three spellings of the same public promise, and the
    assessor compares against the promise.
    """
    fact = _domain_fact(requirements, expected_cwd)
    action = (params or {}).get("action")
    try:
        contract = build_contract(
            envelope_id=envelope_id,
            tool=tool,
            params=params,
            effective_action=effective_action,
            expected_cwd=expected_cwd,
            expected_argv=expected_argv,
            intent_source=intent_source,
            target_sha=probe_target_sha(execute, expected_cwd),
            config_fingerprint=_config_fingerprint(requirements),
            document_map_fingerprint=document_map_fingerprint,
            fact_epoch=fact.get("fact_epoch") if fact else None,
            domain_id=fact.get("domain_id") if fact else None,
            blocking_conflict_ids=_blocking_conflict_ids(fact),
            predecessor_contract_id=predecessor_contract_id,
            expected_observations=expected_observations(action),
            direct_falsifiers=direct_falsifiers(action),
        )
    except (TypeError, ValueError) as exc:
        # A call the canonical form cannot represent cannot be committed to,
        # and an uncommittable call is not dispatched (fail closed).
        logger.warning(f"invocation contract for {envelope_id} could not be frozen: {exc}")
        return None
    if not write_contract(execute, contract):
        logger.warning(
            f"invocation contract {contract['contract_id']} did not reach disk; "
            "the dispatch it was frozen for must be refused"
        )
        return None
    return contract


def compliance_class(
    expected_argv: Optional[str],
    actual_argv: Optional[str],
) -> Optional[str]:
    """How the physical argv relates to the frozen one; None when unknowable.

    The comparison is over ARGUMENT TOKENS, and the actual argv's first token
    — the runner executable — is excluded on both sides: which `mvn` binary
    the toolchain resolves (a wrapper, a versioned path, a venv interpreter)
    is the runner's own resolution, and the contract pins the argument vector,
    not the binary. Shell quoting is not a difference either; both sides are
    tokenized before they are compared.

    * `exact` — the dispatch ran the frozen vector, token for token.
    * `equivalent` — every frozen token ran, in the frozen order, and the
      runner added tokens of its own on top (its invariant transport and
      evidence flags). The additions are not hidden: the contract's
      `expected_argv` and the receipt's `argv` are both persisted, so a reader
      sees exactly what was added.
    * `deviated` — a frozen token did not run, or ran out of order. The
      dispatch did not honour the contract.

    None means UNKNOWABLE, not compliant: either side stating no argv leaves
    nothing to compare, and a receipt then states no compliance at all.
    """
    expected = _tokens(expected_argv)
    dispatched = _tokens(actual_argv)
    if not expected or not dispatched:
        return None
    actual = dispatched[1:]
    if expected == actual:
        return "exact"
    return "equivalent" if _ordered_subsequence(expected, actual) else "deviated"


# ---------------------------------------------------------------------------
# request scope: the engine's envelope identity, and the frozen contract
# ---------------------------------------------------------------------------


def current_action_context() -> ActionContext:
    """The identity of the tool call executing on this thread."""
    return getattr(_SCOPE, "action", None) or _EMPTY_CONTEXT


def set_action_context(
    *,
    envelope_id: Optional[str],
    intent_source: str = DEFAULT_INTENT_SOURCE,
) -> ActionContext:
    """Open the request scope for one tool call (the engine owns this)."""
    context = ActionContext(
        envelope_id=_text(envelope_id) or None,
        intent_source=(intent_source if intent_source in INTENT_SOURCES else DEFAULT_INTENT_SOURCE),
    )
    _SCOPE.action = context
    return context


def clear_action_context() -> None:
    """Close the request scope; a stale envelope must never be inherited."""
    _SCOPE.action = None


@contextmanager
def action_context(
    *,
    envelope_id: Optional[str],
    intent_source: str = DEFAULT_INTENT_SOURCE,
):
    """Scope one tool call to an envelope identity, restoring the previous."""
    previous = getattr(_SCOPE, "action", None)
    set_action_context(envelope_id=envelope_id, intent_source=intent_source)
    try:
        yield current_action_context()
    finally:
        _SCOPE.action = previous


def current_contract() -> Optional[Dict[str, Any]]:
    """The contract frozen for the dispatch executing on this thread."""
    return getattr(_SCOPE, "contract", None)


@contextmanager
def dispatch_contract(contract: Optional[Mapping[str, Any]]):
    """Publish `contract` for the duration of ONE physical dispatch.

    The runner that writes the receipt reads it from here, so the binding
    never outlives the dispatch it belongs to.
    """
    previous = getattr(_SCOPE, "contract", None)
    _SCOPE.contract = dict(contract) if contract else None
    try:
        yield current_contract()
    finally:
        _SCOPE.contract = previous


def contract_receipt_fields(actual_argv: Optional[str]) -> Dict[str, Any]:
    """The receipt keys that bind one dispatch back to its contract.

    Absent when no contract is bound (a runner called outside the facade) and
    per key when the fact is unknown — `compliance` states nothing about a
    dispatch whose frozen argv the facade never materialized.
    """
    contract = current_contract()
    if not contract:
        return {}
    fields: Dict[str, Any] = {}
    for key in ("contract_id", "contract_hash"):
        value = _text(contract.get(key))
        if value:
            fields[key] = value
    compliance = compliance_class(contract.get("expected_argv"), actual_argv)
    if compliance:
        fields["compliance"] = compliance
    return fields


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _domain_fact(
    requirements: Optional[Mapping[str, Any]],
    working_directory: str,
) -> Optional[Dict[str, Any]]:
    """The `DomainFacts` record this dispatch belongs to (nearest root wins).

    Same containment rule the receipt's domain lookup applies: one invocation
    belongs to ONE domain. A survey with no §C2 projection states no domain,
    so the contract records none.
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


def _blocking_conflict_ids(fact: Optional[Mapping[str, Any]]) -> List[str]:
    """The edge ids that seal this domain, in the survey's own order."""
    if not fact:
        return []
    identifiers: List[str] = []
    for conflict in fact.get("open_conflicts") or ():
        if not isinstance(conflict, Mapping):
            continue
        if conflict.get("kind") not in BLOCKING_CONFLICT_KINDS:
            continue
        edge_id = _text(conflict.get("edge_id"))
        if edge_id and edge_id not in identifiers:
            identifiers.append(edge_id)
    return identifiers


def _config_fingerprint(requirements: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The config pin AS THE SURVEY RECORDED IT (read-through, never a probe)."""
    if not isinstance(requirements, Mapping):
        return None
    stamp = requirements.get("survey")
    value = stamp.get("config_fingerprint") if isinstance(stamp, Mapping) else None
    if value is None:
        value = requirements.get("config_fingerprint")
    return _text(value) or None


def _tokens(argv: Optional[str]) -> Tuple[str, ...]:
    text = _text(argv)
    if not text:
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return tuple(text.split())


def _ordered_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    remaining = iter(actual)
    return all(token in remaining for token in expected)


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
    "ActionContext",
    "BLOCKING_CONFLICT_KINDS",
    "CONTRACT_DIR",
    "CONTRACT_HEREDOC",
    "CONTRACT_PERSIST_FAILED",
    "CONTRACT_SCHEMA_VERSION",
    "DEFAULT_INTENT_SOURCE",
    "DIRECT_FALSIFIERS",
    "EXPECTED_OBSERVATIONS",
    "INTENT_SOURCES",
    "UNRECORDED_ENVELOPE_PREFIX",
    "action_context",
    "build_contract",
    "clear_action_context",
    "compliance_class",
    "contract_hash",
    "contract_identity",
    "contract_receipt_fields",
    "current_action_context",
    "current_contract",
    "direct_falsifiers",
    "dispatch_contract",
    "expected_observations",
    "freeze_contract",
    "set_action_context",
    "unrecorded_envelope_id",
    "write_contract",
]
