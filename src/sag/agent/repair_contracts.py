"""Typed targeted retrieval and the reactive RepairContract (Plan 6 Stage C3).

Spec §C6: a failure is not a reason to read the repository. A failure is a
reason to read the FEW documents that could state how this typed failure is
answered — and only after a current evidence assessment has named it.

    ReceiptAssessment / ControlAssessment
      -> typed code routes a bounded selection of the document map
      -> the Stage A extractors run on those entries only
      -> claims and equal-applicability conflicts are recorded
      -> ONE public call is proposed, or `unknown` is the answer

Three rules make this safe rather than merely convenient.

**Raw failure text is not the routing authority.** The typed code is. A
signature string is diagnostic; it never selects a document, never picks an
argv, and never appears in a proposal.

**No claims, no proposal.** An empty selection, an unreadable entry, and a
document that states nothing all produce the same empty result. The caller
treats that as `unknown`. `supporting_claim_ids` is mandatory and non-empty on
every `RepairContract` that exists at all, and the ids are looked up from
stored claims — the model cannot self-attest provenance.

**The repair never dispatches.** It proposes ONE `{tool, params}` public call
and says so in the observation the failing receipt produced. Acceptance is the
model calling that exact call; only the normal Stage B pre-dispatch sequence
may then freeze it. That is what keeps this inside the Category-3 allowance
for evidence-triggered operational safeguards: the harness reacts to its own
evidence, it does not plan.

    /workspace/.setup_agent/repair_contracts/<repair_id>.json

Persistence mirrors `claim_records`/`evidence_assessments`: one atomic file per
repair, the same body under an existing id is a no-op success, a different body
under an existing id is refused rather than merged. This module never raises.

**Acceptance scope.** `intent_source="accepted_repair"` and the `repair_id` are
carried in a thread-local scope the engine opens around one tool call, exactly
as `invocation_contracts.action_context` carries the envelope identity, because
the tool layer has no other view of what the model just accepted.
`stamp_repair` is the one function that writes them onto a frozen contract.
"""

import hashlib
import json
import posixpath
import shlex
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.claim_records import (
    extract_policy_claims,
    find_claim_conflicts,
    write_claims,
)
from sag.agent.control_events import canonical_json
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_contracts import (
    DEFAULT_INTENT_SOURCE,
    contract_hash,
    current_action_context,
)

REPAIR_SCHEMA_VERSION = 1
REPAIR_DIR = "/workspace/.setup_agent/repair_contracts"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# repair content can ever collide with it.
REPAIR_HEREDOC = "SAGREPAIR"
REPAIR_ID_DIGEST_CHARS = 12
# Spec §C6 step 1: the selection is bounded BEFORE anything is read, so a
# repository with a thousand documents costs the same as one with five.
MAX_RETRIEVED_ENTRIES = 5
# The one public facade a repair may propose. A repair that cannot be expressed
# as a call the model already has is not a repair, it is a plan.
REPAIR_TOOL = "build"
# Spec §C6: acceptance creates a NEW intent whose source is the repair.
ACCEPTED_REPAIR_INTENT = "accepted_repair"
NO_SUPPORTING_CLAIMS = "no supporting claims"
NO_SAFE_PROPOSAL = "no safe applicable proposal"

# ---------------------------------------------------------------------------
# the typed-code vocabulary (plan §Stage C shared contracts)
# ---------------------------------------------------------------------------

# The success code. Everything a repair reacts to is, by definition, not this.
EXPECTATION_MET = "expectation_met"
# Codes whose repair material lives in dependency metadata rather than in the
# domain's own documentation. Prefix-matched so the vocabulary stays extensible
# without this module learning a new name each time.
DEPENDENCY_CODE_PREFIXES = ("dependency_",)
# The assessor's dependency-mismatch family; the suffix names the subject.
DEPENDENCY_INCOMPATIBLE_PREFIX = "dependency_incompatible_"
CAPABILITY_CODE_PREFIX = "capability_absent_"
FALSIFIER_CODE_PREFIX = "falsifier_"
# The Stage C base failure/blocked codes. `deviated_receipt` is included as a
# retrieval TRIGGER only: a deviated receipt may motivate a repair, and spec §C5
# still forbids it from contradicting the contract it deviated from.
FAILURE_CLASS_CODES = frozenset(
    {
        "no_dispatch",
        "transient_network",
        "timeout",
        "permission_denied",
        "precondition_unmet",
        "stale_fingerprint",
        "deviated_receipt",
        "compile_no_source_mismatch",
        "semantic_failure",
    }
)
FAILURE_CLASS_PREFIXES = (
    CAPABILITY_CODE_PREFIX,
    FALSIFIER_CODE_PREFIX,
) + DEPENDENCY_CODE_PREFIXES

# ---------------------------------------------------------------------------
# what each typed code is allowed to READ (spec §C6 step 1)
# ---------------------------------------------------------------------------

# A capability is turned on by a build configuration, not by prose about the
# project: CI workflows (the map collects yaml from workflow directories only),
# CMake, and the install/build sections of documentation.
CAPABILITY_ENTRY_KINDS = ("yaml", "cmake")
# Where a dependency is pinned: python metadata, requirements files, Docker.
# Live p6v-tvm-r4: the project's own NumPy pin lives in a docker INSTALL
# SCRIPT (`docker/install/*.sh`) — shell entries are where Docker-era pins
# live, exactly the "Docker/installation scripts" family §C1 discovers.
DEPENDENCY_ENTRY_KINDS = ("toml", "python", "requirements", "dockerfile", "shell")
# Everything else is a question about THIS domain, answered by its own docs.
DOC_ENTRY_KINDS = ("markdown",)
# Section titles that mark documentation as being about building/installing.
# Substring data, not project names — a heading is matched, never a repository.
INSTALL_SECTION_KEYWORDS = (
    "install",
    "build",
    "compile",
    "setup",
    "prerequisite",
    "requirement",
    "dependenc",
    "getting started",
    "quick start",
)

# ---------------------------------------------------------------------------
# what a documented command means to the public facade
# ---------------------------------------------------------------------------

# Command tokens mapped to the ONE public verb `build` exposes. Data, not
# policy: a token outside this table proposes nothing at all.
ARGV_ACTIONS = {
    "test": "test",
    "verify": "test",
    "check": "test",
    "pytest": "test",
    "package": "package",
    "assemble": "package",
    "install": "install",
    "compile": "compile",
    "build": "compile",
}
# Runners whose documented command IS dependency resolution, whatever verb it
# spells: `pip install X` installs a dependency, not a build.
DEPENDENCY_RUNNERS = ("pip",)
DEPENDENCY_ACTION = "deps"
# The typed native affordance (spec §C8).
NATIVE_ACTION = "native"
# The value that ENABLES a capability. The `capability_absent_<f>` code is the
# evidence that makes this the repair; it is never read off a claim, whose
# stated value is just as likely to be the project's OFF default.
NATIVE_ENABLED = "ON"
# The `env` claim scopes whose typed_value may state a build definition: a CI
# `CMAKE_ARGS` assignment and the `-DUSE_X=ON` inside it, plus CMake's own
# `set()`/`option()` declarations.
NATIVE_CLAIM_SCOPES = ("environment", "cmake_definition", "cmake_set", "cmake_option")
# The typed observations Stage C2 freezes per action. `deps` resolves
# coordinates and states no typed observation, so it records no key at all.
EXPECTED_OBSERVATIONS = {
    "compile": ("artifact_or_report_delta",),
    "package": ("artifact_or_report_delta",),
    "install": ("artifact_or_report_delta",),
    "test": ("report_delta",),
}

_SCOPE = threading.local()


# ---------------------------------------------------------------------------
# identity and vocabulary
# ---------------------------------------------------------------------------


def repair_identity(trigger_assessment_id: Any) -> str:
    """``rep-<sha256(trigger_assessment_id)[:12]>`` (plan §Stage C, verbatim).

    Identity is the TRIGGER: re-deriving a repair for the same assessment is
    the same repair, so a replay writes the same file rather than a second
    proposal for one failure.
    """
    material = str(trigger_assessment_id or "").encode("utf-8")
    return f"rep-{hashlib.sha256(material).hexdigest()[:REPAIR_ID_DIGEST_CHARS]}"


def is_failure_class(typed_code: Any) -> bool:
    """Whether a typed code may trigger retrieval at all.

    This is the RETRIEVAL trigger set, not the phase gate's failure authority
    (`phase_gates._FAILURE_CLASS_ASSESSMENT_CODES`): a timeout is worth reading
    a document about without being a semantic failure of the project.
    """
    code = _text(typed_code)
    if not code or code == EXPECTATION_MET:
        return False
    return code in FAILURE_CLASS_CODES or code.startswith(FAILURE_CLASS_PREFIXES)


# ---------------------------------------------------------------------------
# selection (spec §C6 step 1)
# ---------------------------------------------------------------------------


def select_entries(
    typed_code: str,
    *,
    document_map: Optional[Mapping[str, Any]],
    domain_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """The bounded set of map entries this typed code is allowed to read.

    Ordering is deterministic and domain-first: the domain's own documents
    answer before the repository's, and ties break on path. The cap is applied
    last, so a crowded repository loses the least relevant entries rather than
    a random five.
    """
    code = _text(typed_code)
    entries = [_entry_view(item) for item in (document_map or {}).get("entries") or ()]
    entries = [item for item in entries if item.get("path")]
    root = _normalized_root(domain_root)

    if code.startswith(CAPABILITY_CODE_PREFIX):
        selected = [item for item in entries if _states_capability_configuration(item)]
    elif code.startswith(DEPENDENCY_CODE_PREFIXES):
        selected = [item for item in entries if _kind(item) in DEPENDENCY_ENTRY_KINDS]
    else:
        selected = [
            item
            for item in entries
            if _kind(item) in DOC_ENTRY_KINDS and (not root or _under(item, root))
        ]
    selected.sort(key=lambda item: (0 if root and _under(item, root) else 1, item["path"]))
    return selected[:MAX_RETRIEVED_ENTRIES]


def retrieve_for(
    typed_code: str,
    *,
    document_map: Optional[Mapping[str, Any]],
    fetch_text: Callable[[Mapping[str, Any]], Optional[str]],
    checkout_root: str,
    domain_id: Optional[str] = None,
    applicability: Optional[Mapping[str, Any]] = None,
    domain_roots: Sequence[str] = (),
    execute: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Read the selected entries, extract, record, and answer with claims.

    `fetch_text` is the caller's bounded read — ONE `cat` per selected entry,
    because this module has no transport of its own and must not acquire one.
    An entry it cannot read contributes nothing; it never degrades into a
    guess about what that document would have said.

    `checkout_root` is required rather than defaulted for the same reason lane
    a2 requires it: a documented command whose directory is a guess is worse
    than no claim at all.

    Returns `{entries, claims, conflicts}`, and returns all three EMPTY when no
    safe applicable claim was found — the caller's `unknown` (spec §C6 step 5).
    """
    entries = select_entries(
        typed_code,
        document_map=document_map,
        domain_root=(applicability or {}).get("domain"),
    )
    if not entries:
        return _unknown()

    read: List[Dict[str, Any]] = []
    extracted: List[Any] = []
    for item in entries:
        try:
            text = fetch_text(item)
        except Exception as exc:  # a transport failure is an unknown, not a raise
            logger.debug(f"repair retrieval could not read {item.get('path')!r}: {exc}")
            continue
        if not text:
            continue
        read.append(item)
        extracted.extend(
            extract_policy_claims(
                item,
                text,
                checkout_root=checkout_root,
                domain_roots=domain_roots,
            )
        )

    applicable = [(claim, claim.payload()) for claim in extracted]
    applicable = [pair for pair in applicable if _applies(pair[1], applicability)]
    if not applicable:
        return _unknown()
    claims = [claim for claim, _ in applicable]

    if execute is not None and not write_claims(execute, claims):
        # A claim that did not reach disk still answers this retrieval, but the
        # provenance lookup a later proposal depends on would be missing, so the
        # failure is visible rather than silent.
        logger.warning(
            f"targeted retrieval for {_text(typed_code)!r} could not persist every claim; "
            "a proposal citing an unpersisted claim has no lookupable provenance"
        )
    logger.debug(
        f"targeted retrieval for {_text(typed_code)!r} in domain {_text(domain_id) or 'unknown'}: "
        f"{len(read)} entries, {len(claims)} claims"
    )
    return {
        "entries": read,
        "claims": [payload for _, payload in applicable],
        "conflicts": find_claim_conflicts(claims),
    }


# ---------------------------------------------------------------------------
# the proposal (spec §C6)
# ---------------------------------------------------------------------------


def propose_public_call(
    trigger: Mapping[str, Any],
    claims: Sequence[Any],
    *,
    domain_root: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The ONE proposal these claims support, or None and a typed reason.

    The proposal is `{tool, params, supporting_claim_ids}` — the public call
    and the STORED claims that authorize it, never one without the other.

    Deterministic by construction: the FIRST applicable claim in the
    retriever's own order wins, and the retriever's order is the map's path
    order. Two orderings of the same claims cannot produce two proposals.

    * a `capability_absent_<feature>` code whose definition switch a stored
      claim states proposes the typed native affordance (spec §C8);
    * a lifecycle claim scoped to the domain proposes its documented verb
      against that domain's root;
    * a SINGLE exact dependency pin proposes `deps` targeting that literal;
      several pins propose the plain `deps` resolution, because choosing one
      of them would be a guess rather than a citation;
    * a `capability_absent_*` code proposes NOTHING otherwise — turning a
      capability on without a project-owned switch is an unsourced action.
    """
    views = [view for view in (_claim_view(claim) for claim in claims or ()) if view]
    if not views:
        return None, NO_SUPPORTING_CLAIMS

    code = _text((trigger or {}).get("typed_code"))
    root = _normalized_root(domain_root)

    # The native affordance runs FIRST for a capability code: the absent
    # capability IS the failure, and a lifecycle command that happens to be
    # documented nearby answers a different question.
    native = _native_proposal(code, views, root)
    if native is not None:
        return native, ""

    for view in views:
        if view["kind"] != "lifecycle":
            continue
        domain = _normalized_root(view["applicability"].get("domain"))
        if not domain or (root and domain != root):
            continue
        action = _lifecycle_action(view["typed_value"])
        if not action:
            continue
        return (
            {
                "tool": REPAIR_TOOL,
                "params": {"action": action, "working_directory": domain},
                "supporting_claim_ids": [view["claim_id"]],
            },
            "",
        )

    if code.startswith(CAPABILITY_CODE_PREFIX):
        return None, NO_SAFE_PROPOSAL

    pins = [view for view in views if view["kind"] == "dependency"]
    # A `dependency_incompatible_<name>` code STATES its subject: filtering
    # the pins to that package is a citation, not a choice (live p6v-tvm-r6:
    # the docker script pins several packages, and the un-filtered rule
    # proposed a bare `deps` that would never install the 1.26 line).
    if code.startswith(DEPENDENCY_INCOMPATIBLE_PREFIX):
        subject = code[len(DEPENDENCY_INCOMPATIBLE_PREFIX) :]
        subject_pins = [
            view
            for view in pins
            if _text(view["typed_value"].get("package")).lower() == subject.lower()
        ]
        if subject_pins:
            pins = subject_pins
    if pins:
        params: Dict[str, Any] = {"action": DEPENDENCY_ACTION}
        directory = root or _normalized_root(pins[0]["applicability"].get("domain"))
        if directory:
            params["working_directory"] = directory
        # One exact pin IDENTIFIES the repair, so the proposal targets it and
        # cites the claim it came from. Several pins identify a dependency SET,
        # which the project's own manifest already resolves; naming one of them
        # would cite a claim for a choice the claim does not state.
        literal = _exact_pin(pins[0]["typed_value"]) if len(pins) == 1 else ""
        if literal:
            params["args"] = literal
        return (
            {
                "tool": REPAIR_TOOL,
                "params": params,
                "supporting_claim_ids": [view["claim_id"] for view in pins],
            },
            "",
        )

    return None, NO_SAFE_PROPOSAL


def _native_proposal(
    typed_code: str,
    views: Sequence[Mapping[str, Any]],
    domain_root: str,
) -> Optional[Dict[str, Any]]:
    """The `build(action='native', ...)` these claims support, or None.

    Two halves, both required (spec §C8):

    * the typed code names the ABSENT capability, which is what makes `ON` the
      repair rather than a preference — the value is derived from the evidence,
      never read off a claim whose project default may well be `OFF`;
    * a stored `env` claim states the feature's own `USE_<FEATURE>` switch,
      which is what makes the switch project-owned rather than invented.

    Definitions that name no feature (`BUILD_TESTING`) join only when a claim
    states them too, with the claim's own value — they configure the same
    build, so they need the same provenance.

    The allowlists are the build facade's module data, imported HERE rather
    than at module scope: `sag.agent` must not depend on `sag.tools` (the same
    reason `invocation_contracts` defers its import of this module). Restating
    the tables locally would be the other option, and two copies of an EXACT
    allowlist eventually disagree — which is how a proposal the facade must
    refuse gets composed.
    """
    if not typed_code.startswith(CAPABILITY_CODE_PREFIX):
        return None
    feature = typed_code[len(CAPABILITY_CODE_PREFIX) :].strip().lower()
    if not feature:
        return None
    from sag.tools.build.backends import (
        NATIVE_DEFINITION_KEY,
        NATIVE_DEFINITION_VALUES,
        NATIVE_FEATURE_DEFINITION_PREFIX,
        native_feature_definition,
    )

    switch = native_feature_definition(feature)

    definitions: Dict[str, str] = {}
    supporting: List[str] = []
    for view in views:
        if view["kind"] != "env":
            continue
        typed_value = view["typed_value"]
        if _text(typed_value.get("scope")) not in NATIVE_CLAIM_SCOPES:
            continue
        name = _text(typed_value.get("name"))
        if not NATIVE_DEFINITION_KEY.match(name) or name in definitions:
            continue
        if name == switch:
            definitions[name] = NATIVE_ENABLED
        elif name.startswith(NATIVE_FEATURE_DEFINITION_PREFIX):
            # A switch for a capability this failure did not name would request
            # a feature nobody resolved; the facade rejects that pair, so it is
            # never proposed either.
            continue
        elif _text(typed_value.get("value")) in NATIVE_DEFINITION_VALUES:
            definitions[name] = _text(typed_value.get("value"))
        else:
            continue
        supporting.append(view["claim_id"])
    if switch not in definitions:
        return None

    params: Dict[str, Any] = {
        "action": NATIVE_ACTION,
        "features": [feature],
        "definitions": {key: definitions[key] for key in sorted(definitions)},
    }
    if domain_root:
        params["working_directory"] = domain_root
    return {
        "tool": REPAIR_TOOL,
        "params": params,
        "supporting_claim_ids": supporting,
    }


def _exact_pin(typed_value: Mapping[str, Any]) -> str:
    """`pkg==literal` when this dependency claim states one, else empty.

    Only `==`: a range states what a project TOLERATES, and installing one
    version out of a range is a decision the claim never made.
    """
    if _text(typed_value.get("specifier")) != "==":
        return ""
    package = _text(typed_value.get("package"))
    version = _text(typed_value.get("version"))
    return f"{package}=={version}" if package and version else ""


def build_repair(
    trigger: Mapping[str, Any],
    claims: Sequence[Any],
    *,
    domain_id: Optional[str] = None,
    domain_root: Optional[str] = None,
    fact_epoch: Optional[int] = None,
    open_conflicts: Sequence[Mapping[str, Any]] = (),
) -> Optional[Dict[str, Any]]:
    """One `RepairContract` (spec §C6 fields), or None when nothing is safe.

    `trigger` is a persisted assessment body: a `ReceiptAssessment` names the
    receipt it is about, a `ControlAssessment` names no receipt at all — and
    the difference survives here as an ABSENT key, never a null.
    """
    assessment_id = _text((trigger or {}).get("assessment_id"))
    typed_code = _text((trigger or {}).get("typed_code"))
    if not assessment_id or not typed_code:
        return None

    proposal, reason = propose_public_call(trigger, claims, domain_root=domain_root)
    if proposal is None:
        logger.debug(f"no repair proposed for {assessment_id} ({typed_code}): {reason}")
        return None
    supporting = [_text(value) for value in proposal["supporting_claim_ids"]]
    supporting = [value for value in supporting if value]
    if not supporting:
        # Unreachable by construction; asserted anyway because a proposal
        # without provenance is exactly what this stage forbids.
        return None

    call = {"tool": proposal["tool"], "params": dict(proposal["params"])}
    action = _text(call["params"].get("action"))
    repair: Dict[str, Any] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "repair_id": repair_identity(assessment_id),
        "trigger_assessment_id": assessment_id,
    }
    receipt_id = _text((trigger or {}).get("receipt_id"))
    if receipt_id:
        repair["trigger_receipt_id"] = receipt_id
    fingerprints = (trigger or {}).get("fingerprints")
    if isinstance(fingerprints, Mapping) and fingerprints:
        repair["fingerprints"] = {str(key): str(value) for key, value in fingerprints.items()}
    domain = _text(domain_id)
    if domain:
        repair["domain_id"] = domain
    if isinstance(fact_epoch, int) and not isinstance(fact_epoch, bool):
        repair["fact_epoch"] = fact_epoch
    repair["typed_failure_or_capability"] = typed_code

    directory = _text(call["params"].get("working_directory"))
    if directory:
        repair["required_preconditions"] = [{"predicate": "directory_exists", "path": directory}]
    repair["proposed_public_call"] = {
        "tool": call["tool"],
        "params": dict(call["params"]),
    }
    envelope: Dict[str, Any] = {"tool": call["tool"], "actions": [action]}
    if directory:
        envelope["working_directories"] = [directory]
    repair["permitted_semantic_envelope"] = envelope
    observations = EXPECTED_OBSERVATIONS.get(action)
    if observations:
        repair["expected_observations"] = list(observations)
    repair["supporting_claim_ids"] = supporting
    conflicts = [dict(record) for record in open_conflicts or () if isinstance(record, Mapping)]
    if conflicts:
        repair["open_conflicts"] = conflicts
    return repair


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def write_repair(execute: Callable[..., Any], repair: Optional[Mapping[str, Any]]) -> bool:
    """Persist one repair atomically; True when the file holds exactly this body.

    Same contract as `write_claim`/`write_assessment`: the same body under an
    existing id is a no-op success (a replay must not double-write), and a
    DIFFERENT body under an existing id is refused and logged rather than
    merged — an id is a claim about identity, so a collision is a defect to
    see, not to resolve silently.
    """
    identifier = _text((repair or {}).get("repair_id"))
    if not identifier or not (repair or {}).get("supporting_claim_ids"):
        return False
    payload = dict(repair or {})
    try:
        body = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"repair contract {identifier} is not serializable: {exc}")
        return False
    final = f"{REPAIR_DIR}/{identifier}.json"
    existing = _read_existing(execute, final)
    if existing is not None:
        if existing == payload:
            return True
        logger.warning(
            f"repair contract {identifier} already records a different proposal; "
            "repairs are written once per trigger and this write was refused"
        )
        return False
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(REPAIR_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{REPAIR_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{REPAIR_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"repair contract {identifier} not persisted: {exc}")
        return False
    return _succeeded(result)


# ---------------------------------------------------------------------------
# surfacing (reactive, one bounded block)
# ---------------------------------------------------------------------------


def repair_block(repair: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The plan's block format, verbatim. None when the repair states too little.

    The params are rendered canonically so the model sees the exact call it
    must issue to accept — acceptance is compared by equality, and a prettier
    rendering here would be a call it cannot reproduce.
    """
    body = dict(repair or {})
    code = _text(body.get("typed_failure_or_capability"))
    call = body.get("proposed_public_call") or {}
    tool = _text(call.get("tool")) if isinstance(call, Mapping) else ""
    params = call.get("params") if isinstance(call, Mapping) else None
    identifiers = [_text(value) for value in body.get("supporting_claim_ids") or ()]
    identifiers = [value for value in identifiers if value]
    if not code or not tool or not identifiers:
        return None
    return (
        f"[repair] {code}: proposed {tool}({canonical_json(dict(params or {}))}) "
        f"— provenance {', '.join(identifiers)}; accept by calling it, or state why not."
    )


def surfacing_block(orchestrator: Any, receipt_id: str) -> Optional[str]:
    """The one block to append after `receipt_id`'s observation, or None.

    Both halves are required, in this order: a CURRENT failure-class assessment
    of this receipt (the routing authority, spec §C6) and a live proposal for
    that assessment. An assessment with no proposal surfaces nothing, because
    "we noticed" is not a corrective action; a proposal whose assessment is not
    about this receipt surfaces nothing, because it answers another failure.
    """
    subject = _text(receipt_id)
    if not subject:
        return None
    triggers = sorted(
        {
            _text(record.get("assessment_id"))
            for record in read_records(orchestrator, ASSESSMENT_DIR)
            if _text(record.get("receipt_id")) == subject
            and is_failure_class(record.get("typed_code"))
        }
        - {""}
    )
    if not triggers:
        return None
    proposals = {
        _text(record.get("trigger_assessment_id")): record
        for record in read_records(orchestrator, REPAIR_DIR)
    }
    for assessment_id in triggers:
        block = repair_block(proposals.get(assessment_id))
        if block:
            return block
    return None


# ---------------------------------------------------------------------------
# acceptance: the request scope, and the stamp it leaves on a frozen contract
# ---------------------------------------------------------------------------


def accepted_repair_for(
    orchestrator: Any,
    tool: str,
    params: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """The repair whose proposed call this dispatch EXACTLY is, or None.

    Equality over the whole call, not a prefix or a subset: a different action,
    a different directory or one extra parameter is a different intent, and
    borrowing a repair's provenance for it would be the self-attestation spec
    §C6 forbids.
    """
    requested_tool = _text(tool)
    requested_params = dict(params or {})
    if requested_tool != REPAIR_TOOL:
        return None
    for record in sorted(
        read_records(orchestrator, REPAIR_DIR),
        key=lambda item: _text(item.get("repair_id")),
    ):
        call = record.get("proposed_public_call")
        if not isinstance(call, Mapping):
            continue
        if _text(call.get("tool")) != requested_tool:
            continue
        if dict(call.get("params") or {}) != requested_params:
            continue
        return _text(record.get("repair_id")) or None
    return None


def current_acceptance() -> Optional[str]:
    """The repair the tool call executing on this thread accepted, if any."""
    return getattr(_SCOPE, "repair_id", None)


def set_accepted_repair(repair_id: Optional[str]) -> Optional[str]:
    """Open the acceptance scope for one tool call (the engine owns this)."""
    _SCOPE.repair_id = _text(repair_id) or None
    return _SCOPE.repair_id


def clear_accepted_repair() -> None:
    """Close the scope; a stale acceptance must never be inherited."""
    _SCOPE.repair_id = None


def intent_source_for_dispatch() -> str:
    """Who authored the intent this dispatch is freezing a contract for.

    A controller-authored call is never an accepted repair even when a repair
    scope is live: the harness forced that action, the model did not accept
    anything. Otherwise a live acceptance wins over the default `model`.
    """
    context = current_action_context()
    if context.intent_source != DEFAULT_INTENT_SOURCE:
        return context.intent_source
    return ACCEPTED_REPAIR_INTENT if current_acceptance() else context.intent_source


def stamp_repair(contract: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Record the accepted repair on a frozen contract; unchanged when none.

    `contract_id` is untouched — the dispatch is the same dispatch — and the
    hash is recomputed over the canonical payload it was defined over, so a
    reader still verifies the file it holds. Absent acceptance is an absent
    key, never `repair_id: null`.

    This is the seam Stage B's freeze consumes: `invocation_contracts` owns the
    contract payload and this lane does not edit it, so the wire-up is one line
    inside `freeze_contract` — stamp the built contract before `write_contract`
    persists it — plus `accepted_repair` joining `INTENT_SOURCES`, which the
    module already reserves for this stage.
    """
    body = dict(contract or {})
    repair_id = current_acceptance()
    if not body or not repair_id or intent_source_for_dispatch() != ACCEPTED_REPAIR_INTENT:
        return body
    body["intent_source"] = ACCEPTED_REPAIR_INTENT
    body["repair_id"] = repair_id
    body["contract_hash"] = contract_hash(body)
    return body


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _unknown() -> Dict[str, Any]:
    """The one shape every "no safe applicable claim" answer takes."""
    return {"entries": [], "claims": [], "conflicts": []}


def _entry_view(entry: Any) -> Dict[str, Any]:
    """One document map entry as its persisted body, whatever the caller holds."""
    if isinstance(entry, Mapping):
        return dict(entry)
    payload = getattr(entry, "payload", None)
    return dict(payload()) if callable(payload) else {}


def _kind(entry: Mapping[str, Any]) -> str:
    return _text(entry.get("kind")).lower()


def _under(entry: Mapping[str, Any], root: str) -> bool:
    path = _text(entry.get("path"))
    return bool(root) and (path == root or path.startswith(f"{root}/"))


def _states_capability_configuration(entry: Mapping[str, Any]) -> bool:
    """Whether this entry is one a capability code may read.

    Documentation qualifies only through its INDEX: a document with an
    install/build section states how to build; a changelog does not, and
    reading it would be the unbounded repository read §C6 exists to prevent.
    """
    kind = _kind(entry)
    if kind in CAPABILITY_ENTRY_KINDS:
        return True
    if kind not in DOC_ENTRY_KINDS:
        return False
    for section in entry.get("section_index") or ():
        if not isinstance(section, Mapping):
            continue
        title = _text(section.get("title_or_key")).lower()
        if any(keyword in title for keyword in INSTALL_SECTION_KEYWORDS):
            return True
    return False


def _applies(claim: Mapping[str, Any], applicability: Optional[Mapping[str, Any]]) -> bool:
    """Whether a claim's applicability is compatible with the requested one.

    Compatible, not equal: a claim that states nothing about the domain applies
    to it (a repository README speaks for its modules), while a claim that
    states a DIFFERENT domain does not. Absent is unconstrained; stated and
    different is a mismatch.
    """
    requested = {
        str(key): value for key, value in dict(applicability or {}).items() if value is not None
    }
    if not requested:
        return True
    stated = dict(claim.get("applicability") or {})
    for key, value in requested.items():
        held = stated.get(key)
        if held is not None and str(held) != str(value):
            return False
    return True


def _claim_view(claim: Any) -> Optional[Dict[str, Any]]:
    """One claim as the fields a proposal needs, from its persisted body.

    A claim with no `claim_id` carries no provenance — it cannot be looked up
    later — so it is not a claim a proposal may rest on.
    """
    body = claim if isinstance(claim, Mapping) else None
    if body is None:
        payload = getattr(claim, "payload", None)
        body = payload() if callable(payload) else None
    if not isinstance(body, Mapping):
        return None
    identifier = _text(body.get("claim_id"))
    kind = _text(body.get("kind"))
    if not identifier or not kind:
        return None
    return {
        "claim_id": identifier,
        "kind": kind,
        "typed_value": dict(body.get("typed_value") or {}),
        "applicability": dict(body.get("applicability") or {}),
    }


def _lifecycle_action(typed_value: Mapping[str, Any]) -> Optional[str]:
    """The public verb a documented lifecycle command maps to, or None.

    None is the common answer and the right one: `cmake ..` configures a tree,
    which the public facade does not expose, and a repair that cannot be
    expressed as an existing public call is not proposed at all.
    """
    runner = _text(typed_value.get("tool")).lower()
    if runner in DEPENDENCY_RUNNERS:
        return DEPENDENCY_ACTION
    argv = [_text(token) for token in typed_value.get("argv") or ()]
    if not argv:
        return None
    for token in argv[1:]:
        action = ARGV_ACTIONS.get(token.lower())
        if action:
            return action
    return ARGV_ACTIONS.get(runner)


def read_records(orchestrator: Any, directory: str) -> List[Dict[str, Any]]:
    """Every readable single-line JSON record in one evidence directory.

    Same bounded read the phase gate uses: one glob `cat`, and a line that does
    not parse is skipped rather than failing the read — a corrupt neighbour
    must not hide the records we do understand.

    Public because the build facade's Stage E provenance gate needs the same
    read over `claims/` and `evidence_assessments/`: two readers of the same
    directories must not disagree about what a stored record is.
    """
    execute = getattr(orchestrator, "execute_command", None)
    if not callable(execute):
        return []
    try:
        probe = execute(
            f"cat {shlex.quote(directory)}/*.json 2>/dev/null", workdir=None, timeout=30
        )
    except Exception as exc:
        logger.debug(f"{directory} unavailable for repair surfacing: {exc}")
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
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def _read_existing(execute: Callable[..., Any], path: str) -> Optional[Dict[str, Any]]:
    """The repair already at `path`, or None when there is none to honour.

    An unparseable file is reported as a body that matches nothing, so the
    caller refuses instead of overwriting bytes it cannot account for.
    """
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"repair contract {path} unreadable: {exc}")
        return None
    content = str(result.get("output") or "").strip()
    if not _succeeded(result) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {"unparseable": path}
    return payload if isinstance(payload, dict) else {"unparseable": path}


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
    "ARGV_ACTIONS",
    "CAPABILITY_CODE_PREFIX",
    "CAPABILITY_ENTRY_KINDS",
    "DEPENDENCY_CODE_PREFIXES",
    "DEPENDENCY_ENTRY_KINDS",
    "DOC_ENTRY_KINDS",
    "EXPECTED_OBSERVATIONS",
    "FAILURE_CLASS_CODES",
    "MAX_RETRIEVED_ENTRIES",
    "NATIVE_ACTION",
    "NATIVE_CLAIM_SCOPES",
    "NATIVE_ENABLED",
    "NO_SAFE_PROPOSAL",
    "NO_SUPPORTING_CLAIMS",
    "REPAIR_DIR",
    "REPAIR_HEREDOC",
    "REPAIR_SCHEMA_VERSION",
    "REPAIR_TOOL",
    "accepted_repair_for",
    "build_repair",
    "clear_accepted_repair",
    "current_acceptance",
    "intent_source_for_dispatch",
    "is_failure_class",
    "propose_public_call",
    "read_records",
    "repair_block",
    "repair_identity",
    "retrieve_for",
    "select_entries",
    "set_accepted_repair",
    "stamp_repair",
    "surfacing_block",
    "write_repair",
]
