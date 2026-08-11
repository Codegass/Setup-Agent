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

**Identity.** Historical schema-v1 contracts retain their original
`sha256(envelope_id + expected_argv [+ effective_jdk])` identity for read-only
display. Every executable schema-v2 contract instead hashes the run, envelope,
intent id/domain/fingerprint, exact public call, effective executor/action/cwd,
versioned execution binding, argv, effective JDK, and survey fingerprint.
`contract_hash = canonical_sha256(payload sans contract_hash)` seals the whole
record, including the target/config/document-map/domain pins. Those remaining
pins intentionally do not mint a second identity inside one action: if they
change under the same commitment, absent-or-identical CAS refuses the write and
the runner stays stopped instead of treating checkout drift as a new action.

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
import json
import posixpath
import re
import shlex
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.action_intents import action_fingerprint as compute_action_fingerprint
from sag.agent.action_intents import (
    bounded_exact_params,
)
from sag.agent.control_events import canonical_json, canonical_sha256
from sag.agent.invocation_receipts import (
    active_receipt_run_id,
)
from sag.agent.invocation_receipts import target_sha as probe_target_sha
from sag.utils.container_io import compare_publish_container_text_atomic

CONTRACT_SCHEMA_VERSION = 2
LEGACY_CONTRACT_SCHEMA_VERSION = 1
CONTRACT_DIR = "/workspace/.setup_agent/invocation_contracts"
# Heredoc delimiter for the atomic write. The body is single-line canonical
# JSON, so no contract content can ever collide with it.
CONTRACT_HEREDOC = "SAGCONTRACT"
# The typed refusal a facade returns when the contract did not reach disk.
CONTRACT_PERSIST_FAILED = "CONTRACT_PERSIST_FAILED"
# An internal backend without a facade-frozen contract has no authority to
# perform even its discovery/preflight side effects.
CONTRACT_AUTHORITY_MISSING = "CONTRACT_AUTHORITY_MISSING"
CONTRACT_MAX_CANONICAL_BYTES = 64 * 1024
CONTRACT_MAX_RAW_BYTES = 128 * 1024
CONTRACT_TEXT_MAX_BYTES = 4096
CONTRACT_SEQUENCE_MAX_ITEMS = 64
_CONTRACT_ID = re.compile(r"ic-[0-9a-f]{12}")
_TARGET_SHA = re.compile(r"[0-9a-f]{7,64}")
# Who authored the intent. Historical `accepted_repair` contracts remain
# readable JSON, but live writers no longer mint that harness-authored source.
INTENT_SOURCES = ("model", "controller")
DEFAULT_INTENT_SOURCE = "model"
# What a dispatch records when no engine envelope was emitted for it (no
# control-event sink configured). A stated absence, never a borrowed identity,
# and unique per dispatch so two contracts never collide on one sentinel.
UNRECORDED_ENVELOPE_PREFIX = "envelope-unrecorded"
# Conflict kinds that BLOCK a dispatch. An incomplete document map is an
# unknown, not a block, so it is not a blocking conflict id (spec §C2).
BLOCKING_CONFLICT_KINDS = ("version_incompatible",)

# How the facade binds an engine-owned public ActionIntent to physical evidence.
# ``argv_v1`` freezes a materialized argument vector and receipts must recompute
# exact/equivalent compliance. ``python_facade_v1`` freezes the public build
# action instead: Python operations are bounded multi-command transactions, so
# pretending the facade knew one argv would be a forged commitment.
ARGV_EXECUTION_BINDING = "argv_v1"
PYTHON_FACADE_EXECUTION_BINDING = "python_facade_v1"
EXECUTION_BINDINGS = (ARGV_EXECUTION_BINDING, PYTHON_FACADE_EXECUTION_BINDING)
EXECUTOR_ALLOWLIST_V1 = frozenset({"maven", "gradle", "python", "bash"})

_V2_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "envelope_id",
        "intent_id",
        "intent_domain_id",
        "action_fingerprint",
        "requested_call",
        "effective_tool",
        "effective_action",
        "expected_cwd",
        "intent_source",
        "execution_binding",
        "contract_id",
        "contract_hash",
    }
)
_V2_OPTIONAL_FIELDS = frozenset(
    {
        "target_sha",
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
        "fact_epoch",
        "domain_id",
        "trigger_assessment_id",
        "repair_context_id",
        "repair_context_sha256",
        "expected_argv",
        "expected_observations",
        "direct_falsifiers",
        "supporting_claim_ids",
        "blocking_conflict_ids",
        "predecessor_contract_id",
        "effective_jdk",
    }
)
_V2_FIELDS = _V2_REQUIRED_FIELDS | _V2_OPTIONAL_FIELDS

# One shared public-action mapping. Backends, the internal Python executor, the
# read-only judge and the assessor all consume this table; a parallel mapping
# would let one layer authorize an operation another layer did not freeze.
PYTHON_PUBLIC_ACTION_TO_OPERATION = {
    "deps": "setup_env",
    "compile": "compile",
    "test": "test",
    "package": "build",
    "install": "build",
    "native": "native",
}
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
PYTHON_EXPECTED_OBSERVATIONS = {
    "deps": ("python_setup_observation",),
    "compile": ("python_compile_observation",),
    "test": ("python_test_report",),
    "package": ("python_wheel_observation",),
    "install": ("python_wheel_observation",),
    "native": ("python_native_capability_observations",),
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
    intent_id: Optional[str] = None
    intent_domain_id: Optional[str] = None
    intent_exact_params: Optional[Mapping[str, Any]] = None
    action_fingerprint: Optional[str] = None
    trigger_assessment_id: Optional[str] = None
    repair_context_id: Optional[str] = None
    repair_context_sha256: Optional[str] = None
    predecessor_contract_id: Optional[str] = None
    # True when the live controller owes an exact action_envelope event.  A
    # missing envelope in that mode is an integrity failure, never a license to
    # mint a sinkless compatibility identity.
    control_recording_active: bool = False

    def __post_init__(self) -> None:
        if self.intent_source not in INTENT_SOURCES:
            raise ValueError("action context intent_source is not recognized")
        if type(self.control_recording_active) is not bool:
            raise ValueError("control_recording_active must be boolean")
        predecessor = str(self.predecessor_contract_id or "").strip()
        if self.predecessor_contract_id is not None and (
            _CONTRACT_ID.fullmatch(predecessor) is None
        ):
            raise ValueError("action context predecessor identity is invalid")
        if predecessor:
            object.__setattr__(self, "predecessor_contract_id", predecessor)
        exact_params: Optional[Dict[str, Any]] = None
        if self.intent_exact_params is not None:
            exact_params = bounded_exact_params(self.intent_exact_params)
            object.__setattr__(self, "intent_exact_params", exact_params)
        intent_tuple = (
            str(self.intent_id or "").strip(),
            str(self.intent_domain_id or "").strip(),
            exact_params,
            str(self.action_fingerprint or "").strip(),
        )
        # An empty parameter mapping is a complete exact call, so presence —
        # not truthiness — decides whether that member exists.
        intent_present = (
            bool(intent_tuple[0]),
            bool(intent_tuple[1]),
            exact_params is not None,
            bool(intent_tuple[3]),
        )
        if any(intent_present) and not all(intent_present):
            raise ValueError("action context intent identity must be one complete tuple")
        repair_tuple = tuple(
            str(value).strip() if value is not None else ""
            for value in (
                self.trigger_assessment_id,
                self.repair_context_id,
                self.repair_context_sha256,
            )
        )
        if any(repair_tuple) and not all(repair_tuple):
            raise ValueError("repair trigger/context/digest must be one complete tuple")
        if all(repair_tuple):
            digest = repair_tuple[2].lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("repair_context_sha256 must be a SHA-256 digest")
            if (
                self.intent_source != "model"
                or not str(self.intent_id or "").strip()
                or not str(self.intent_domain_id or "").strip()
                or self.intent_exact_params is None
                or not str(self.action_fingerprint or "").strip()
            ):
                raise ValueError("repair-linked action context requires a complete model intent")
            object.__setattr__(self, "repair_context_sha256", digest)


_EMPTY_CONTEXT = ActionContext()


def contract_identity(
    envelope_id: str,
    expected_argv: Optional[str],
    effective_jdk: Optional[Mapping[str, Any]] = None,
) -> str:
    """Historical schema-v1 identity (read compatibility only).

    New contracts use :func:`contract_identity_v2`. Keeping this exact helper
    is what lets a reader verify old evidence without letting that evidence
    become live dispatch authority.
    """
    material = f"{str(envelope_id or '')}{str(expected_argv or '')}"
    if effective_jdk:
        # A mechanical retry under a newly observed runtime is a different
        # physical commitment even when its model envelope and argv are
        # unchanged.  Historical contracts (no runtime binding) retain their
        # byte-identical identity domain.
        material += canonical_json(dict(effective_jdk))
    return f"ic-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def contract_identity_v2(contract: Mapping[str, Any]) -> str:
    """Identity of every executable commitment in a schema-v2 contract.

    The projection has a fixed key set. Optional identity values therefore
    contribute JSON ``null`` instead of disappearing from the hash domain,
    while exact public parameters preserve explicit nulls and list order. Pins
    outside this projection remain sealed by ``contract_hash`` and CAS: drift
    within one action is an integrity collision, not a new dispatch identity.
    """

    requested = contract.get("requested_call")
    requested_call = dict(requested) if isinstance(requested, Mapping) else requested
    projection = {
        "run_id": contract.get("run_id"),
        "envelope_id": contract.get("envelope_id"),
        "intent_id": contract.get("intent_id"),
        "intent_domain_id": contract.get("intent_domain_id"),
        "action_fingerprint": contract.get("action_fingerprint"),
        "requested_call": requested_call,
        "effective_tool": contract.get("effective_tool"),
        "effective_action": contract.get("effective_action"),
        "expected_cwd": contract.get("expected_cwd"),
        "execution_binding": contract.get("execution_binding"),
        "expected_argv": contract.get("expected_argv"),
        "effective_jdk": contract.get("effective_jdk"),
        "survey_fingerprint": contract.get("survey_fingerprint"),
    }
    return f"ic-{hashlib.sha256(canonical_json(projection).encode('utf-8')).hexdigest()[:12]}"


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


def authorized_facade_envelope_id(context: ActionContext) -> Optional[str]:
    """Resolve facade authority without manufacturing repair/control lineage.

    An exact recorded envelope always wins. The unique unrecorded identity is
    a narrow compatibility path for a sinkless, non-repair facade call whose
    complete ActionIntent identity was already minted by the engine. Empty
    default scope, active repair and an active control recorder fail closed.
    """

    if context.envelope_id:
        if (
            context.intent_id
            and context.intent_domain_id
            and context.intent_exact_params is not None
            and context.action_fingerprint
        ):
            return context.envelope_id
        return None
    if (
        context.control_recording_active
        or context.repair_context_id is not None
        or not str(context.intent_id or "").strip()
        or not str(context.intent_domain_id or "").strip()
        or context.intent_exact_params is None
        or not str(context.action_fingerprint or "").strip()
    ):
        return None
    return unrecorded_envelope_id()


def python_operation_for_public_action(action: Any) -> Optional[str]:
    """The internal Python operation named by one public build action."""

    return PYTHON_PUBLIC_ACTION_TO_OPERATION.get(_text(action).lower())


def validate_execution_binding(
    *,
    execution_binding: Any,
    requested_call: Mapping[str, Any],
    effective_tool: Any,
    effective_action: Any,
    expected_argv: Any,
    supporting_claim_ids: Optional[Sequence[str]] = None,
) -> str:
    """Validate one explicit facade binding and return its canonical name."""

    binding = _text(execution_binding)
    if binding not in EXECUTION_BINDINGS:
        raise ValueError("invocation contract execution_binding is not recognized")
    if binding == ARGV_EXECUTION_BINDING:
        executor = _text(effective_tool).lower()
        if executor not in EXECUTOR_ALLOWLIST_V1:
            raise ValueError("argv_v1 requires an allowlisted executor")
        if executor == "python":
            raise ValueError("Python requires the python_facade_v1 semantic binding")
        if not _text(expected_argv):
            raise ValueError("argv_v1 requires a non-empty expected_argv")
        return binding

    if _text(expected_argv):
        raise ValueError("python_facade_v1 forbids a guessed expected_argv")
    if _text(requested_call.get("tool")).lower() != "build":
        raise ValueError("python_facade_v1 requires the public build facade")
    if _text(effective_tool).lower() != "python":
        raise ValueError("python_facade_v1 requires the Python executor")
    params = requested_call.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("python_facade_v1 requested params are invalid")
    operation = python_operation_for_public_action(params.get("action"))
    if operation is None or operation != _text(effective_action):
        raise ValueError("python_facade_v1 action does not map to its effective operation")
    # Maven-only state cannot be silently ignored by a Python dispatch.
    if params.get("maven_version_requirement") not in (None, ""):
        raise ValueError("python_facade_v1 cannot carry a Maven requirement")
    # These operations do not consume free-form args. Refuse rather than
    # freezing a parameter the producer would silently ignore.
    if operation in {"build", "compile", "native"} and params.get("args") not in (None, ""):
        raise ValueError(f"python {operation} does not consume public args")
    if operation != "native" and (
        params.get("features") is not None or params.get("definitions") is not None
    ):
        raise ValueError("python non-native operation cannot carry native parameters")
    if operation == "native" and (
        not isinstance(params.get("features"), list)
        or not params.get("features")
        or not isinstance(params.get("definitions"), Mapping)
        or not params.get("definitions")
    ):
        raise ValueError("python native operation requires explicit features and definitions")
    if operation == "setup_env" and params.get("args") not in (None, ""):
        raise ValueError("python deps targets are disabled until typed claim authority is shared")
    return binding


def expected_observations(
    action: Any,
    execution_binding: Optional[str] = None,
) -> List[str]:
    """What a dispatch of this public verb promises to leave behind.

    A verb the table does not name promises NOTHING — an empty list, written as
    an absent key. Guessing an expectation is how a harness invents a mismatch.
    """
    table = (
        PYTHON_EXPECTED_OBSERVATIONS
        if execution_binding == PYTHON_FACADE_EXECUTION_BINDING
        else EXPECTED_OBSERVATIONS
    )
    return list(table.get(_text(action).lower(), ()))


def direct_falsifiers(
    action: Any,
    execution_binding: Optional[str] = None,
) -> List[Dict[str, str]]:
    """The typed predicates that may contradict a claim for this verb.

    Bound to `expected_observations`: with nothing expected there is nothing to
    falsify, so a verb that promises no observation names no falsifier either.
    """
    if execution_binding == PYTHON_FACADE_EXECUTION_BINDING:
        return []
    if not expected_observations(action, execution_binding):
        return []
    return [dict(falsifier) for falsifier in DIRECT_FALSIFIERS]


def _canonical_contract_text(
    value: Any,
    field: str,
    *,
    lowercase: bool = False,
) -> str:
    """One bounded, already-canonical contract string.

    Schema validation never coerces numbers/bools to strings and never trims a
    value on the reader's behalf.  Writers may normalize before construction;
    persisted authority must already be the canonical bytes it claims to be.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"invocation contract {field} must be canonical non-empty text")
    if len(value.encode("utf-8")) > CONTRACT_TEXT_MAX_BYTES:
        raise ValueError(f"invocation contract {field} exceeds its length limit")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"invocation contract {field} contains unsafe control text")
    if lowercase and value != value.lower():
        raise ValueError(f"invocation contract {field} must be lowercase")
    return value


def _canonical_contract_text_list(value: Any, field: str) -> List[str]:
    """Validate a bounded JSON list of unique canonical non-empty strings."""

    if not isinstance(value, list):
        raise ValueError(f"invocation contract {field} must be a list")
    if len(value) > CONTRACT_SEQUENCE_MAX_ITEMS:
        raise ValueError(f"invocation contract {field} exceeds its item limit")
    canonical = [_canonical_contract_text(item, f"{field} item") for item in value]
    if len(canonical) != len(set(canonical)):
        raise ValueError(f"invocation contract {field} contains duplicates")
    return canonical


def _derived_contract_observations(
    requested_call: Mapping[str, Any],
    execution_binding: str,
) -> List[str]:
    if requested_call.get("tool") != "build":
        return []
    params = requested_call.get("params")
    action = params.get("action") if isinstance(params, Mapping) else None
    return expected_observations(action, execution_binding)


def _derived_contract_falsifiers(
    requested_call: Mapping[str, Any],
    execution_binding: str,
) -> List[Dict[str, str]]:
    if requested_call.get("tool") != "build":
        return []
    params = requested_call.get("params")
    action = params.get("action") if isinstance(params, Mapping) else None
    return direct_falsifiers(action, execution_binding)


def build_contract(
    *,
    run_id: str,
    envelope_id: str,
    tool: str,
    params: Optional[Mapping[str, Any]],
    effective_tool: str,
    effective_action: str,
    expected_cwd: str,
    execution_binding: str,
    intent_id: str,
    intent_domain_id: str,
    intent_exact_params: Mapping[str, Any],
    action_fingerprint: str,
    expected_argv: Optional[str] = None,
    intent_source: str = DEFAULT_INTENT_SOURCE,
    trigger_assessment_id: Optional[str] = None,
    repair_context_id: Optional[str] = None,
    repair_context_sha256: Optional[str] = None,
    target_sha: Optional[str] = None,
    survey_fingerprint: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
    document_map_fingerprint: Optional[str] = None,
    fact_epoch: Optional[int] = None,
    domain_id: Optional[str] = None,
    blocking_conflict_ids: Optional[Sequence[str]] = None,
    predecessor_contract_id: Optional[str] = None,
    expected_observations: Optional[Sequence[str]] = None,
    direct_falsifiers: Optional[Sequence[Mapping[str, Any]]] = None,
    supporting_claim_ids: Optional[Sequence[str]] = None,
    effective_jdk: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble one schema-v2 contract. Pure — no probes, no I/O.

    Field names and shapes are the plan's Stage B v1 list verbatim, plus the
    Stage C typed expectations (`expected_observations`/`direct_falsifiers`)
    the assessor reads back; `supersedes_contract_id` is not invented here.

    `supporting_claim_ids` are the STORED claims that authorized this dispatch
    (spec §C8: "the contract stores claim IDs"). A dispatch that needed no
    project-owned authority states none, as an absent key.
    """
    run = _canonical_contract_text(run_id, "run_id")
    intent_identifier = _canonical_contract_text(intent_id, "intent_id")
    intent_domain = _canonical_contract_text(intent_domain_id, "intent_domain_id")
    fingerprint = _canonical_contract_text(action_fingerprint, "action_fingerprint")
    if not all((run, intent_identifier, intent_domain, fingerprint)):
        raise ValueError("schema-v2 contract requires complete run and intent lineage")
    exact_params = bounded_exact_params(intent_exact_params)
    submitted_params = bounded_exact_params(params)
    if submitted_params != exact_params:
        raise ValueError("requested_call params differ from the engine-owned exact intent")
    requested_tool = _canonical_contract_text(tool, "requested_call.tool", lowercase=True)
    requested_call = {"tool": requested_tool, "params": exact_params}
    recomputed_fingerprint = compute_action_fingerprint(
        domain_id=intent_domain,
        tool=requested_call["tool"],
        params=exact_params,
    )
    if fingerprint != recomputed_fingerprint:
        raise ValueError("action_fingerprint does not match the frozen public call")

    argv = (
        _canonical_contract_text(expected_argv, "expected_argv")
        if expected_argv is not None
        else ""
    )
    runtime_binding = _effective_jdk(effective_jdk)
    contract: Dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "run_id": run,
        "envelope_id": _canonical_contract_text(envelope_id, "envelope_id"),
        "intent_id": intent_identifier,
        "intent_domain_id": intent_domain,
        "action_fingerprint": fingerprint,
        "requested_call": requested_call,
        "effective_tool": _canonical_contract_text(
            effective_tool,
            "effective_tool",
            lowercase=True,
        ),
        "effective_action": _canonical_contract_text(
            effective_action,
            "effective_action",
        ),
        "expected_cwd": _canonical_contract_text(expected_cwd, "expected_cwd"),
    }
    for key, value in (
        ("target_sha", target_sha),
        ("survey_fingerprint", survey_fingerprint),
        ("config_fingerprint", config_fingerprint),
        ("document_map_fingerprint", document_map_fingerprint),
        ("domain_id", domain_id),
    ):
        if value is not None:
            text = _canonical_contract_text(value, key)
            if key == "target_sha" and _TARGET_SHA.fullmatch(text) is None:
                raise ValueError("invocation contract target_sha is not a git object name")
            contract[key] = text
    if fact_epoch is not None:
        if type(fact_epoch) is not int or fact_epoch <= 0:
            raise ValueError("invocation contract fact_epoch must be a positive integer")
        contract["fact_epoch"] = fact_epoch
    if intent_source not in INTENT_SOURCES:
        raise ValueError("invocation contract intent_source is not recognized")
    contract["intent_source"] = intent_source
    repair_tuple = tuple(
        _text(value)
        for value in (
            trigger_assessment_id,
            repair_context_id,
            repair_context_sha256,
        )
    )
    if any(repair_tuple) and not all(repair_tuple):
        raise ValueError("repair trigger/context/digest must be one complete tuple")
    if all(repair_tuple):
        digest = repair_tuple[2].lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("repair_context_sha256 must be a SHA-256 digest")
        if intent_source != "model" or not _text(intent_id) or not _text(action_fingerprint):
            raise ValueError("repair-linked contract requires a complete model intent")
        repair_context_sha256 = digest
    for key, value in (
        ("trigger_assessment_id", trigger_assessment_id),
        ("repair_context_id", repair_context_id),
        ("repair_context_sha256", repair_context_sha256),
    ):
        text = _text(value)
        if text:
            contract[key] = text
    if isinstance(supporting_claim_ids, (str, bytes)):
        raise ValueError("supporting_claim_ids must be a sequence of identifiers")
    supporting = (
        _canonical_contract_text_list(list(supporting_claim_ids), "supporting_claim_ids")
        if supporting_claim_ids is not None
        else []
    )
    contract["execution_binding"] = validate_execution_binding(
        execution_binding=execution_binding,
        requested_call=requested_call,
        effective_tool=contract["effective_tool"],
        effective_action=effective_action,
        expected_argv=argv,
        supporting_claim_ids=supporting,
    )
    if argv:
        contract["expected_argv"] = argv
    derived_observations = _derived_contract_observations(
        requested_call,
        contract["execution_binding"],
    )
    if expected_observations is None:
        observations = derived_observations
    else:
        if isinstance(expected_observations, (str, bytes)):
            raise ValueError("expected_observations must be the derived list")
        observations = list(expected_observations)
        if observations != derived_observations:
            raise ValueError("expected_observations differ from the shared action mapping")
    if observations:
        contract["expected_observations"] = observations
    derived_predicates = _derived_contract_falsifiers(
        requested_call,
        contract["execution_binding"],
    )
    if direct_falsifiers is None:
        predicates = derived_predicates
    else:
        if isinstance(direct_falsifiers, (str, bytes)):
            raise ValueError("direct_falsifiers must be the derived list")
        predicates = [dict(falsifier) for falsifier in direct_falsifiers]
        if predicates != derived_predicates:
            raise ValueError("direct_falsifiers differ from the shared action mapping")
    if observations:
        if predicates:
            contract["direct_falsifiers"] = predicates
    elif predicates:
        raise ValueError("direct_falsifiers require a registered expectation")
    if supporting:
        contract["supporting_claim_ids"] = supporting
    if isinstance(blocking_conflict_ids, (str, bytes)):
        raise ValueError("blocking_conflict_ids must be a sequence of identifiers")
    conflicts = (
        _canonical_contract_text_list(
            list(blocking_conflict_ids),
            "blocking_conflict_ids",
        )
        if blocking_conflict_ids is not None
        else []
    )
    if conflicts:
        contract["blocking_conflict_ids"] = conflicts
    predecessor = _text(predecessor_contract_id)
    if predecessor:
        contract["predecessor_contract_id"] = predecessor
    if runtime_binding:
        contract["effective_jdk"] = runtime_binding
    contract["contract_id"] = contract_identity_v2(contract)
    contract["contract_hash"] = contract_hash(contract)
    _validate_contract_lineage(contract)
    return contract


def write_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    contract: Mapping[str, Any],
) -> bool:
    """Publish one schema-v2 contract absent-or-identical.

    An existing identical body is replay success. An existing different body
    is an identity collision and is never overwritten. The final compare and
    rename share a container lock, so two SAG processes cannot both win after
    reading absence.
    """
    try:
        _validate_contract_lineage(contract, live_authority=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation contract lineage is invalid: {exc}")
        return False
    identifier = _text((contract or {}).get("contract_id"))
    if not identifier:
        return False
    try:
        body = canonical_json(dict(contract))
    except (TypeError, ValueError) as exc:
        logger.debug(f"invocation contract {identifier} is not serializable: {exc}")
        return False
    final = f"{CONTRACT_DIR}/{identifier}.json"
    expected: Optional[str]
    try:
        existing = execute(f"cat {shlex.quote(final)}") or {}
    except Exception as exc:
        logger.debug(f"invocation contract {identifier} could not prove store state: {exc}")
        return False
    if _succeeded(existing):
        expected = str(existing.get("output") or "")
        if expected == body:
            return _publish_contract_bytes(execute, contract, expected.encode("utf-8"))
        logger.warning(
            f"invocation contract {identifier} already records different bytes; "
            "contracts are append-only and this write was refused"
        )
        return False
    try:
        absent = execute(f"test ! -e {shlex.quote(final)}") or {}
    except Exception as exc:
        logger.debug(f"invocation contract {identifier} absence probe failed: {exc}")
        return False
    if not _succeeded(absent):
        logger.debug(f"invocation contract {identifier} existence is unknown")
        return False
    expected = None
    try:
        result = compare_publish_container_text_atomic(
            execute,
            final,
            body,
            expected_content=expected,
            validate_json=True,
        )
    except Exception as exc:
        logger.debug(f"invocation contract {identifier} not persisted: {exc}")
        return False
    if not result.persisted:
        logger.debug(f"invocation contract {identifier} not persisted: {result.code}")
        return False
    return _publish_contract_bytes(execute, contract, body.encode("utf-8"))


def _publish_contract_bytes(
    execute: Any,
    contract: Mapping[str, Any],
    raw: bytes,
) -> bool:
    """Seal already-container-persisted contract bytes in the host ledger."""

    from sag.agent.evidence_publications import (
        evidence_publication_authority_for,
        publish_evidence_bytes,
    )

    identifier = _text(contract.get("contract_id"))
    authority = evidence_publication_authority_for(execute)
    if _text(getattr(authority, "run_id", None)) != _text(contract.get("run_id")):
        logger.warning(
            f"invocation contract {identifier} reached the container under a different "
            "host publication run"
        )
        return False
    publication = publish_evidence_bytes(
        execute,
        record_kind="invocation_contract",
        record_id=identifier,
        raw=raw,
        contract_id=identifier,
        contract_hash=_text(contract.get("contract_hash")),
    )
    if publication.published:
        return True
    logger.warning(
        f"invocation contract {identifier} reached the container but host publication "
        f"failed: {publication.status}"
    )
    return False


def read_frozen_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    contract_id: Any,
) -> Optional[Dict[str, Any]]:
    """Read one persisted contract from its authoritative contract store."""

    identifier = _text(contract_id)
    if _CONTRACT_ID.fullmatch(identifier) is None:
        return None
    path = f"{CONTRACT_DIR}/{identifier}.json"
    # Historical v1 remains readable for display/migration only. It can never
    # enter ``current_contract`` or dispatch because live authority requires
    # schema v2. Live v2 bytes below must come through the host publication
    # ledger; this direct branch is deliberately v1-only.
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"invocation contract {identifier} is unreadable: {exc}")
        return None
    raw_content = str(result.get("output") or "")
    if len(raw_content.encode("utf-8")) > CONTRACT_MAX_RAW_BYTES:
        return None
    content = raw_content.strip()
    if not _succeeded(result) or not content:
        return None
    try:
        historical = json.loads(content, object_pairs_hook=_reject_duplicate_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(historical, dict):
        return None
    if historical.get("schema_version", LEGACY_CONTRACT_SCHEMA_VERSION) == (
        LEGACY_CONTRACT_SCHEMA_VERSION
    ):
        try:
            _validate_contract_lineage(historical)
        except (TypeError, ValueError):
            return None
        return historical if historical.get("contract_id") == identifier else None

    from sag.agent.evidence_records import (
        EvidencePublicationBinding,
        read_live_published_json_records,
    )

    def validate_live(payload: Mapping[str, Any], expected_id: str) -> Mapping[str, Any]:
        _validate_contract_lineage(payload, live_authority=True)
        if payload.get("contract_id") != expected_id:
            raise ValueError("contract id does not match filename")
        return dict(payload)

    ledger = read_live_published_json_records(
        execute,
        CONTRACT_DIR,
        record_kind="invocation_contract",
        validator=validate_live,
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=str(payload["run_id"]),
            contract_id=str(payload["contract_id"]),
            contract_hash=str(payload["contract_hash"]),
        ),
        record_scope=contract_record_scope,
    )
    if not ledger.complete or ledger.conflict is not None:
        return None
    payload = next(
        (
            dict(record.payload)
            for record in ledger.records
            if record.source.record_id == identifier
        ),
        None,
    )
    if payload is None:
        return None
    try:
        _validate_contract_lineage(payload, live_authority=True)
    except (TypeError, ValueError):
        return None
    return payload


def contract_record_scope(
    payload: Mapping[str, Any],
    current_run_id: Optional[str],
) -> str:
    """Classify only fully validated old-run contracts outside live authority.

    Schema-v1 contracts remain forensic history.  A schema-v2 contract is
    foreign only after its complete strict lineage, identity and body hash
    validate and its non-empty run differs from the host run.  Any malformed,
    unknown or future shape stays current so the live ledger fails closed.
    """

    if not isinstance(payload, Mapping):
        return "current"
    schema = payload.get("schema_version")
    if schema == LEGACY_CONTRACT_SCHEMA_VERSION:
        try:
            _validate_contract_lineage(payload)
        except (TypeError, ValueError):
            return "current"
        return "forensic"
    if schema != CONTRACT_SCHEMA_VERSION:
        return "current"
    try:
        _validate_contract_lineage(payload, live_authority=True)
    except (TypeError, ValueError):
        return "current"
    current = _text(current_run_id)
    recorded = _text(payload.get("run_id"))
    if current and recorded and recorded != current:
        return "foreign"
    return "current"


def freeze_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    run_id: str,
    envelope_id: str,
    tool: str,
    params: Optional[Mapping[str, Any]],
    effective_tool: str,
    effective_action: str,
    expected_cwd: str,
    expected_argv: Optional[str],
    execution_binding: str,
    intent_source: str,
    intent_id: str,
    intent_domain_id: str,
    intent_exact_params: Mapping[str, Any],
    action_fingerprint: str,
    trigger_assessment_id: Optional[str] = None,
    repair_context_id: Optional[str] = None,
    repair_context_sha256: Optional[str] = None,
    requirements: Optional[Mapping[str, Any]] = None,
    document_map_fingerprint: Optional[str] = None,
    predecessor_contract_id: Optional[str] = None,
    supporting_claim_ids: Optional[Sequence[str]] = None,
    effective_jdk: Optional[Mapping[str, Any]] = None,
    target_sha_value: Optional[str] = None,
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
            run_id=run_id,
            envelope_id=envelope_id,
            tool=tool,
            params=params,
            effective_tool=effective_tool,
            effective_action=effective_action,
            expected_cwd=expected_cwd,
            expected_argv=expected_argv,
            execution_binding=execution_binding,
            intent_source=intent_source,
            intent_id=intent_id,
            intent_domain_id=intent_domain_id,
            intent_exact_params=intent_exact_params,
            action_fingerprint=action_fingerprint,
            trigger_assessment_id=trigger_assessment_id,
            repair_context_id=repair_context_id,
            repair_context_sha256=repair_context_sha256,
            target_sha=(_text(target_sha_value) or probe_target_sha(execute, expected_cwd)),
            survey_fingerprint=_survey_fingerprint(requirements),
            config_fingerprint=_config_fingerprint(requirements),
            document_map_fingerprint=document_map_fingerprint,
            fact_epoch=fact.get("fact_epoch") if fact else None,
            # Receipts bind their domain coordinate to the nearest surveyed
            # root. Freeze the same physical coordinate; the survey's opaque
            # domain record id is not a runner coordinate.
            domain_id=fact.get("root") if fact else None,
            blocking_conflict_ids=_blocking_conflict_ids(fact),
            predecessor_contract_id=predecessor_contract_id,
            expected_observations=expected_observations(action, execution_binding),
            direct_falsifiers=direct_falsifiers(action, execution_binding),
            supporting_claim_ids=supporting_claim_ids,
            effective_jdk=effective_jdk,
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
    intent_id: Optional[str] = None,
    intent_domain_id: Optional[str] = None,
    intent_exact_params: Optional[Mapping[str, Any]] = None,
    action_fingerprint: Optional[str] = None,
    trigger_assessment_id: Optional[str] = None,
    repair_context_id: Optional[str] = None,
    repair_context_sha256: Optional[str] = None,
    predecessor_contract_id: Optional[str] = None,
    control_recording_active: bool = False,
) -> ActionContext:
    """Open the request scope for one tool call (the engine owns this)."""
    context = ActionContext(
        envelope_id=_text(envelope_id) or None,
        intent_source=intent_source,
        intent_id=_text(intent_id) or None,
        intent_domain_id=_text(intent_domain_id) or None,
        intent_exact_params=(
            bounded_exact_params(intent_exact_params) if intent_exact_params is not None else None
        ),
        action_fingerprint=_text(action_fingerprint) or None,
        trigger_assessment_id=_text(trigger_assessment_id) or None,
        repair_context_id=_text(repair_context_id) or None,
        repair_context_sha256=_text(repair_context_sha256).lower() or None,
        predecessor_contract_id=_text(predecessor_contract_id) or None,
        control_recording_active=control_recording_active,
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
    intent_id: Optional[str] = None,
    intent_domain_id: Optional[str] = None,
    intent_exact_params: Optional[Mapping[str, Any]] = None,
    action_fingerprint: Optional[str] = None,
    trigger_assessment_id: Optional[str] = None,
    repair_context_id: Optional[str] = None,
    repair_context_sha256: Optional[str] = None,
    predecessor_contract_id: Optional[str] = None,
    control_recording_active: bool = False,
):
    """Scope one tool call to an envelope identity, restoring the previous."""
    previous = getattr(_SCOPE, "action", None)
    set_action_context(
        envelope_id=envelope_id,
        intent_source=intent_source,
        intent_id=intent_id,
        intent_domain_id=intent_domain_id,
        intent_exact_params=intent_exact_params,
        action_fingerprint=action_fingerprint,
        trigger_assessment_id=trigger_assessment_id,
        repair_context_id=repair_context_id,
        repair_context_sha256=repair_context_sha256,
        predecessor_contract_id=predecessor_contract_id,
        control_recording_active=control_recording_active,
    )
    try:
        yield current_action_context()
    finally:
        _SCOPE.action = previous


def current_contract() -> Optional[Dict[str, Any]]:
    """The contract frozen for the dispatch executing on this thread."""
    candidate = getattr(_SCOPE, "contract", None)
    if not candidate:
        return None
    try:
        _validate_contract_lineage(candidate, live_authority=True)
    except (TypeError, ValueError) as exc:
        logger.warning(f"active invocation contract authority is invalid: {exc}")
        return None
    return dict(candidate)


def live_contract_valid(contract: Optional[Mapping[str, Any]]) -> bool:
    """True only for a complete schema-v2 contract eligible for dispatch."""

    try:
        _validate_contract_lineage(contract or {}, live_authority=True)
    except (TypeError, ValueError):
        return False
    return True


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


def ensure_dispatch_contract(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    tool: str,
    effective_action: str,
    expected_cwd: str,
    expected_argv: Optional[str],
    requirements: Optional[Mapping[str, Any]] = None,
    internal_params: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return the facade-frozen contract or state that authority is absent.

    Internal tools are not a second contract producer. A direct internal call
    has no pre-dispatch model/controller commitment and must be refused before
    discovery, setup, or runner work. The retained arguments keep the public
    call seam source-compatible while making this check side-effect free.
    """
    existing = current_contract()
    if not existing:
        del execute, requirements
        return None, False
    binding = _text(existing.get("execution_binding"))
    if _text(existing.get("effective_tool")).lower() != _text(tool).lower():
        return None, False
    if _text(existing.get("effective_action")) != _text(effective_action):
        return None, False
    if _normalized_root(existing.get("expected_cwd")) != _normalized_root(expected_cwd):
        return None, False
    if binding == ARGV_EXECUTION_BINDING:
        recomputed = compliance_class(existing.get("expected_argv"), expected_argv)
        return (dict(existing), False) if recomputed in {"exact", "equivalent"} else (None, False)
    if binding == PYTHON_FACADE_EXECUTION_BINDING and python_facade_dispatch_matches(
        existing,
        operation=effective_action,
        working_directory=expected_cwd,
        internal_params=internal_params,
    ):
        return dict(existing), False
    del execute, requirements
    return None, False


def python_facade_dispatch_matches(
    contract: Mapping[str, Any],
    *,
    operation: Any,
    working_directory: Any,
    internal_params: Optional[Mapping[str, Any]],
) -> bool:
    """Whether exact internal Python parameters implement this v2 contract."""

    try:
        _validate_contract_lineage(contract, live_authority=True)
    except (TypeError, ValueError):
        return False
    if contract.get("execution_binding") != PYTHON_FACADE_EXECUTION_BINDING:
        return False
    if _text(contract.get("effective_tool")).lower() != "python":
        return False
    requested = contract.get("requested_call")
    params = requested.get("params") if isinstance(requested, Mapping) else None
    if not isinstance(params, Mapping) or not isinstance(internal_params, Mapping):
        return False
    expected_operation = python_operation_for_public_action(params.get("action"))
    actual_operation = _text(operation).lower()
    if (
        expected_operation != actual_operation
        or contract.get("effective_action") != actual_operation
    ):
        return False
    expected_cwd = _normalized_root(contract.get("expected_cwd"))
    actual_cwd = _normalized_root(working_directory)
    public_cwd = _normalized_root(params.get("working_directory"))
    if not expected_cwd or not (expected_cwd == actual_cwd == public_cwd):
        return False

    try:
        actual = bounded_exact_params(internal_params)
    except (TypeError, ValueError):
        return False
    if _text(actual.get("operation")).lower() != actual_operation:
        return False
    if _normalized_root(actual.get("working_directory")) != actual_cwd:
        return False
    public_args = params.get("args")
    if actual.get("args") != public_args:
        return False
    # The public schema's optional timeout may arrive as an explicit JSON null
    # after tool-call normalization. PythonTool materializes that same absence
    # as its deterministic 600-second internal default; only a non-null public
    # value is an exact caller-selected timeout.
    public_timeout = params.get("timeout")
    if public_timeout is None:
        public_timeout = 600
    if actual.get("timeout", 600) != public_timeout:
        return False
    native = actual.get("native")
    if actual_operation == "native":
        expected_native = {
            "features": list(params.get("features") or ()),
            "definitions": dict(params.get("definitions") or {}),
        }
        if native != expected_native:
            return False
    elif native is not None:
        return False
    return set(actual).issubset({"operation", "working_directory", "args", "timeout", "native"})


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
    for key in ("contract_id", "contract_hash", "execution_binding"):
        value = _text(contract.get(key))
        if value:
            fields[key] = value
    compliance = compliance_class(contract.get("expected_argv"), actual_argv)
    if compliance:
        fields["compliance"] = compliance
    effective_jdk = contract.get("effective_jdk")
    if isinstance(effective_jdk, Mapping) and effective_jdk:
        fields["effective_jdk"] = dict(effective_jdk)
    return fields


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _reject_duplicate_json_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = child
    return value


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
        facts = ()
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
    if best is not None:
        return best

    recommendation = requirements.get("build_recommendation")
    nested = recommendation if isinstance(recommendation, Mapping) else {}
    test_system = _text(requirements.get("test_system") or nested.get("test_system"))
    test_root = _normalized_root(requirements.get("test_root") or nested.get("test_root"))
    if (
        test_system in {"maven", "gradle"}
        and test_root
        and (directory == test_root or directory.startswith(f"{test_root}/"))
    ):
        return {"root": test_root}
    return None


def _validate_v2_contract_shape(
    contract: Mapping[str, Any],
    requested: Mapping[str, Any],
    params: Mapping[str, Any],
) -> None:
    """Validate every schema-v2 field before it may grant live authority."""

    for field in (
        "run_id",
        "envelope_id",
        "intent_id",
        "intent_domain_id",
        "action_fingerprint",
        "effective_action",
        "contract_id",
        "contract_hash",
    ):
        _canonical_contract_text(contract.get(field), field)
    requested_tool = _canonical_contract_text(
        requested.get("tool"),
        "requested_call.tool",
        lowercase=True,
    )
    effective_tool = _canonical_contract_text(
        contract.get("effective_tool"),
        "effective_tool",
        lowercase=True,
    )
    if effective_tool not in EXECUTOR_ALLOWLIST_V1:
        raise ValueError("invocation contract effective_tool is not registered")
    source = _canonical_contract_text(contract.get("intent_source"), "intent_source")
    if source not in INTENT_SOURCES:
        raise ValueError("invocation contract intent_source is not recognized")

    cwd = _canonical_contract_text(contract.get("expected_cwd"), "expected_cwd")
    if not cwd.startswith("/") or posixpath.normpath(cwd) != cwd:
        raise ValueError("invocation contract expected_cwd must be an absolute canonical path")
    if "expected_argv" in contract:
        _canonical_contract_text(contract.get("expected_argv"), "expected_argv")

    if requested_tool == "build":
        action = _canonical_contract_text(
            params.get("action"),
            "requested_call.params.action",
            lowercase=True,
        )
        if action not in PYTHON_PUBLIC_ACTION_TO_OPERATION:
            raise ValueError("invocation contract public build action is not recognized")
        public_cwd = params.get("working_directory")
        if public_cwd is not None:
            public_root = _canonical_contract_text(
                public_cwd,
                "requested_call.params.working_directory",
            )
            if not public_root.startswith("/") or posixpath.normpath(public_root) != public_root:
                raise ValueError("requested build working_directory must be canonical absolute")

    for field in (
        "survey_fingerprint",
        "config_fingerprint",
        "document_map_fingerprint",
        "domain_id",
        "trigger_assessment_id",
        "repair_context_id",
        "repair_context_sha256",
        "predecessor_contract_id",
    ):
        if field in contract:
            _canonical_contract_text(contract.get(field), field)
    if "target_sha" in contract:
        sha = _canonical_contract_text(contract.get("target_sha"), "target_sha")
        if _TARGET_SHA.fullmatch(sha) is None:
            raise ValueError("invocation contract target_sha is not a git object name")
    if "fact_epoch" in contract:
        epoch = contract.get("fact_epoch")
        if type(epoch) is not int or epoch <= 0:
            raise ValueError("invocation contract fact_epoch must be a positive integer")

    for field in ("supporting_claim_ids", "blocking_conflict_ids"):
        if field in contract:
            _canonical_contract_text_list(contract.get(field), field)

    binding = _canonical_contract_text(contract.get("execution_binding"), "execution_binding")
    derived_observations = _derived_contract_observations(requested, binding)
    actual_observations = (
        _canonical_contract_text_list(
            contract.get("expected_observations"),
            "expected_observations",
        )
        if "expected_observations" in contract
        else []
    )
    if actual_observations != derived_observations:
        raise ValueError("expected_observations differ from the shared action mapping")

    derived_falsifiers = _derived_contract_falsifiers(requested, binding)
    raw_falsifiers = contract.get("direct_falsifiers", [])
    if not isinstance(raw_falsifiers, list):
        raise ValueError("invocation contract direct_falsifiers must be a list")
    if len(raw_falsifiers) > CONTRACT_SEQUENCE_MAX_ITEMS:
        raise ValueError("invocation contract direct_falsifiers exceeds its item limit")
    actual_falsifiers: List[Dict[str, str]] = []
    for predicate in raw_falsifiers:
        if not isinstance(predicate, Mapping) or set(predicate) != {"predicate_id", "kind"}:
            raise ValueError("invocation contract direct_falsifier shape is invalid")
        actual_falsifiers.append(
            {
                "predicate_id": _canonical_contract_text(
                    predicate.get("predicate_id"),
                    "direct_falsifier.predicate_id",
                ),
                "kind": _canonical_contract_text(
                    predicate.get("kind"),
                    "direct_falsifier.kind",
                ),
            }
        )
    if actual_falsifiers != derived_falsifiers:
        raise ValueError("direct_falsifiers differ from the shared action mapping")


def _validate_contract_lineage(
    contract: Mapping[str, Any],
    *,
    live_authority: bool = False,
) -> None:
    """Fail closed on forged, malformed or partial authority at every edge."""

    if not isinstance(contract, Mapping):
        raise TypeError("invocation contract must be a mapping")
    schema_version = contract.get("schema_version")
    if type(schema_version) is not int or schema_version not in {
        LEGACY_CONTRACT_SCHEMA_VERSION,
        CONTRACT_SCHEMA_VERSION,
    }:
        raise ValueError("invocation contract schema_version is not recognized")
    if schema_version == LEGACY_CONTRACT_SCHEMA_VERSION and live_authority:
        raise ValueError("schema-v1 contract is historical and has no live authority")
    identifier = _text(contract.get("contract_id"))
    if _CONTRACT_ID.fullmatch(identifier) is None:
        raise ValueError("invocation contract identity is invalid")
    predecessor = _text(contract.get("predecessor_contract_id"))
    if "predecessor_contract_id" in contract:
        if _CONTRACT_ID.fullmatch(predecessor) is None:
            raise ValueError("invocation contract predecessor identity is invalid")
        if predecessor == identifier:
            raise ValueError("invocation contract predecessor cannot name itself")
    envelope_id = _text(contract.get("envelope_id"))
    if not envelope_id:
        raise ValueError("invocation contract envelope_id is required")
    source = _text(contract.get("intent_source"))
    if source not in INTENT_SOURCES:
        raise ValueError("invocation contract intent_source is not recognized")
    requested = contract.get("requested_call")
    if not isinstance(requested, Mapping) or not _text(requested.get("tool")):
        raise ValueError("invocation contract requested_call is invalid")
    params = requested.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("invocation contract requested params must be an object")
    bounded_exact_params(params)
    if not _text(contract.get("effective_action")):
        raise ValueError("invocation contract effective_action is required")
    if not _text(contract.get("expected_cwd")):
        raise ValueError("invocation contract expected_cwd is required")
    runtime_binding: Dict[str, Any] | None = None
    if "effective_jdk" in contract:
        raw_runtime = contract.get("effective_jdk")
        if not isinstance(raw_runtime, Mapping):
            raise ValueError("invocation contract effective_jdk must be an object")
        runtime_binding = _effective_jdk(raw_runtime)
        if runtime_binding is None or dict(raw_runtime) != runtime_binding:
            raise ValueError("invocation contract effective_jdk is not the exact canonical binding")
    if schema_version == CONTRACT_SCHEMA_VERSION:
        keys = set(contract)
        unknown = keys - _V2_FIELDS
        missing = _V2_REQUIRED_FIELDS - keys
        if unknown or missing:
            raise ValueError(
                "schema-v2 contract fields are not exact: "
                f"unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        if set(requested) != {"tool", "params"}:
            raise ValueError("schema-v2 requested_call fields are not exact")
        _validate_v2_contract_shape(contract, requested, params)
        run_id = _text(contract.get("run_id"))
        intent_id = _text(contract.get("intent_id"))
        intent_domain_id = _text(contract.get("intent_domain_id"))
        fingerprint = _text(contract.get("action_fingerprint"))
        if not all((run_id, intent_id, intent_domain_id, fingerprint)):
            raise ValueError("schema-v2 contract has incomplete run or intent lineage")
        if fingerprint != compute_action_fingerprint(
            domain_id=intent_domain_id,
            tool=requested.get("tool"),
            params=params,
        ):
            raise ValueError("invocation contract action_fingerprint is invalid")
        validate_execution_binding(
            execution_binding=contract.get("execution_binding"),
            requested_call=requested,
            effective_tool=contract.get("effective_tool"),
            effective_action=contract.get("effective_action"),
            expected_argv=contract.get("expected_argv"),
            supporting_claim_ids=contract.get("supporting_claim_ids"),
        )
        expected_identifier = contract_identity_v2(contract)
    else:
        expected_identifier = contract_identity(
            envelope_id,
            _text(contract.get("expected_argv")) or None,
            runtime_binding,
        )
    if identifier != expected_identifier:
        raise ValueError("invocation contract identity does not match its commitment")
    intent_tuple = (
        _text(contract.get("intent_id")),
        _text(contract.get("action_fingerprint")),
    )
    if (
        schema_version == LEGACY_CONTRACT_SCHEMA_VERSION
        and any(intent_tuple)
        and not all(intent_tuple)
    ):
        raise ValueError("invocation contract intent identity must be one complete tuple")
    repair_tuple = tuple(
        _text(contract.get(key))
        for key in (
            "trigger_assessment_id",
            "repair_context_id",
            "repair_context_sha256",
        )
    )
    if any(repair_tuple) and not all(repair_tuple):
        raise ValueError("repair trigger/context/digest must be one complete tuple")
    if all(repair_tuple):
        digest = repair_tuple[2].lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("repair_context_sha256 must be a SHA-256 digest")
        if source != "model" or not all(intent_tuple):
            raise ValueError("repair-linked contract requires a complete model intent")
    recorded_hash = _text(contract.get("contract_hash")).lower()
    if len(recorded_hash) != 64 or any(char not in "0123456789abcdef" for char in recorded_hash):
        raise ValueError("invocation contract hash is invalid")
    if recorded_hash != contract_hash(contract):
        raise ValueError("invocation contract hash does not match its payload")
    if len(canonical_json(dict(contract)).encode("utf-8")) > CONTRACT_MAX_CANONICAL_BYTES:
        raise ValueError("invocation contract exceeds its canonical byte limit")


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


def _survey_fingerprint(requirements: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The survey evidence pin already present in the held manifest."""

    if not isinstance(requirements, Mapping):
        return None
    stamp = requirements.get("survey")
    value = stamp.get("survey_fingerprint") if isinstance(stamp, Mapping) else None
    if value is None:
        value = requirements.get("survey_fingerprint")
    return _text(value) or None


def _effective_jdk(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Canonical, harness-owned runtime binding; absent facts stay absent."""
    if not isinstance(value, Mapping):
        return None
    binding: Dict[str, Any] = {}
    for key in (
        "major",
        "requirement_major",
        "requirement_authority",
        "runtime_authority",
    ):
        text = _text(value.get(key))
        if text:
            binding[key] = text
    provenance = value.get("provenance")
    if isinstance(provenance, Mapping):
        normalized = {
            str(key): item
            for key, item in provenance.items()
            if item is not None and (not isinstance(item, str) or item.strip())
        }
        if normalized:
            # Canonical serialization here rejects values that could not be
            # frozen consistently before build_contract hashes the payload.
            canonical_json(normalized)
            binding["provenance"] = normalized
    return binding or None


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
    "ARGV_EXECUTION_BINDING",
    "EXECUTION_BINDINGS",
    "EXECUTOR_ALLOWLIST_V1",
    "ActionContext",
    "BLOCKING_CONFLICT_KINDS",
    "CONTRACT_DIR",
    "CONTRACT_AUTHORITY_MISSING",
    "CONTRACT_HEREDOC",
    "CONTRACT_PERSIST_FAILED",
    "CONTRACT_SCHEMA_VERSION",
    "DEFAULT_INTENT_SOURCE",
    "DIRECT_FALSIFIERS",
    "EXPECTED_OBSERVATIONS",
    "INTENT_SOURCES",
    "PYTHON_FACADE_EXECUTION_BINDING",
    "PYTHON_PUBLIC_ACTION_TO_OPERATION",
    "UNRECORDED_ENVELOPE_PREFIX",
    "action_context",
    "authorized_facade_envelope_id",
    "build_contract",
    "clear_action_context",
    "compliance_class",
    "contract_hash",
    "contract_identity",
    "contract_identity_v2",
    "contract_record_scope",
    "contract_receipt_fields",
    "current_action_context",
    "current_contract",
    "direct_falsifiers",
    "dispatch_contract",
    "expected_observations",
    "ensure_dispatch_contract",
    "freeze_contract",
    "read_frozen_contract",
    "live_contract_valid",
    "python_facade_dispatch_matches",
    "python_operation_for_public_action",
    "set_action_context",
    "unrecorded_envelope_id",
    "validate_execution_binding",
    "write_contract",
]
