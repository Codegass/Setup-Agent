"""The causal claim graph (Plan 6 Stage C, spec §C5).

A claim is a file. What that claim currently MEANS is a projection of the
event stream over those files — never a file of its own. That split is the
whole point: a run that dies mid-retraction leaves the claims exactly as they
were, because the retraction was never committed.

    events (truth)  +  claim files (subjects)  ->  ClaimGraph  ->  snapshot

**Grouped transitions (plan §Stage C binding note (a)).** A receipt assessment
never moves one claim in isolation: it moves the claim it assessed and then
whatever conclusions lost their support. Those belong together, so every
`claim_transition` event carries a `group_id` (`"grp-" + sha256(trigger)[:12]`)
and the group ends with the terminal record `{group_id, terminal: true}`.
`load()` applies a group only once it sees that record, so a half-written
commit is invisible rather than half-applied.

**Bounded retraction.** Support sets are explicit — `{"all_of": [...]}` or
`{"any_of": [...]}` — and a conclusion may carry SEVERAL of them. They are
alternative PATHS: the conclusion stands while any one path holds (spec §C5,
"another valid support path keeps the conclusion alive"), and only a claim
whose every path is broken flips to `unknown`. A support member stops
supporting when its evidence is `contradicted`/`unknown` or its source went
`stale`/`superseded`; an absent claim file supports nothing, because an
inference whose support is not on record cannot be retracted later.

Cycles are refused AT INSERT (`SupportCycleError`), so no reader ever walks
one, and a transition citing a fact epoch older than the graph's is refused by
name (`StaleFactEpochError`): a late receipt stays historical evidence and
cannot mutate current state.

**Materialization.** `materialize()` rewrites
`/workspace/.setup_agent/claim_graph.json` atomically for readers that want
the current state without replaying. It is derived state, so unlike a claim or
an assessment it is REWRITTEN rather than write-once, and nothing here ever
reads it back — a fresh `load()` takes the events and the claim files only.
"""

import hashlib
import json
import posixpath
import shlex
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.claim_records import CLAIM_DIR, EVIDENCE_STATUSES
from sag.agent.control_events import canonical_json

CLAIM_GRAPH_SCHEMA_VERSION = 1
CLAIM_GRAPH_PATH = "/workspace/.setup_agent/claim_graph.json"
# Heredoc delimiter for the atomic write. The body is single-line canonical
# JSON, so no graph content can ever collide with it.
CLAIM_GRAPH_HEREDOC = "SAGCLAIMGRAPH"
GROUP_ID_DIGEST_CHARS = 12
# The two shapes a support set may take, in the order they are serialized.
SUPPORT_MODES = ("all_of", "any_of")
# What a lost conclusion becomes: `unknown`, never `contradicted`. Losing the
# support for a conclusion says nothing about whether the conclusion is false.
INVALIDATED_STATUS = "unknown"
# When a claim stops supporting the conclusions that rest on it.
LOST_EVIDENCE_STATUSES = ("contradicted", "unknown")
LOST_SOURCE_STATUSES = ("stale", "superseded")

# What a typed verdict does to the claims its contract cited (spec §C5). Data,
# so the bridge below states no policy of its own: a code outside this table
# moves nothing, which is what "an honest failure falsifies no claim" means.
CONFIRMING_CODE = "expectation_met"
CONTRADICTING_PREFIX = "falsifier_"
CONFIRMED_STATUS = "confirmed"
CONTRADICTED_STATUS = "contradicted"


class ClaimGraphError(Exception):
    """A refused graph operation. Never raised for an absent fact."""


class SupportCycleError(ClaimGraphError):
    """The support edge would close a cycle; it was not inserted."""


class StaleFactEpochError(ClaimGraphError):
    """The transition cites an epoch older than the graph's current one."""


class UnknownClaimError(ClaimGraphError):
    """The graph holds no claim file under that id, so it states nothing."""


def group_identity(trigger_id: Any) -> str:
    """``grp-<sha256(trigger id)[:12]>`` (plan §Stage C shared contract).

    The trigger is whatever caused the commit — usually an assessment id — so
    re-committing the same trigger reuses the same group and replay stays
    idempotent.
    """
    material = _text(trigger_id)
    return f"grp-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:GROUP_ID_DIGEST_CHARS]}"


class ClaimGraph:
    """Current claim state plus the support edges between claims.

    The graph is authored (`transition`/`invalidate_dependents`/`commit_group`
    produce the events a caller emits) and replayed (`load` consumes them).
    Authoring is strict — an unknown claim or an unnamed group is a defect, so
    it raises. Replay is lenient — a transition whose claim file is gone is a
    historical fact about a subject this graph does not hold, so it is skipped.
    """

    def __init__(self, *, fact_epoch: Optional[int] = None) -> None:
        self._nodes: Dict[str, Dict[str, str]] = {}
        self._support: Dict[str, List[Tuple[str, Tuple[str, ...]]]] = {}
        self._pending: List[Dict[str, Any]] = []
        self._open_groups: List[str] = []
        self._committed_groups: set[str] = set()
        self._fact_epoch = _epoch(fact_epoch)

    # -- subjects ----------------------------------------------------------

    @property
    def fact_epoch(self) -> Optional[int]:
        return self._fact_epoch

    def add_claim(self, claim: Any) -> Optional[str]:
        """Register one claim file (a persisted payload or a typed record).

        The claim's own `support_claim_ids` become an `all_of` path, because a
        record that names its complete support has already stated one.
        Anything without a usable `claim_id` is not a claim, and is skipped.
        """
        body = _claim_body(claim)
        identifier = _text(body.get("claim_id"))
        if not identifier:
            return None
        node: Dict[str, str] = {}
        for key in ("evidence_status", "source_status"):
            value = _text(body.get(key))
            if value:
                node[key] = value
        self._nodes[identifier] = node
        supports = [_text(value) for value in body.get("support_claim_ids") or ()]
        supports = [value for value in supports if value]
        if supports:
            try:
                self.add_support(identifier, {"all_of": supports})
            except SupportCycleError as exc:
                # A claims directory that states a cycle is a defect to see,
                # not to propagate: the edge is dropped and the graph loads.
                logger.warning(f"claim {identifier} states a cyclic support set: {exc}")
        return identifier

    def claim_ids(self) -> Tuple[str, ...]:
        """Every registered claim id, ordered so two loads read identically."""
        return tuple(sorted(self._nodes))

    def evidence_status(self, claim_id: str) -> Optional[str]:
        """What execution has shown about the claim; None when it holds none."""
        return self._nodes.get(_text(claim_id), {}).get("evidence_status")

    def source_status(self, claim_id: str) -> Optional[str]:
        """Whether the claim's source is still current; None when unstated."""
        return self._nodes.get(_text(claim_id), {}).get("source_status")

    # -- support edges -----------------------------------------------------

    def supports(self, conclusion_id: str) -> Tuple[Dict[str, List[str]], ...]:
        """The alternative support paths of one conclusion, in insert order."""
        return tuple(
            {mode: list(members)} for mode, members in self._support.get(_text(conclusion_id), ())
        )

    def add_support(
        self,
        conclusion_id: str,
        support: Mapping[str, Sequence[str]],
    ) -> Dict[str, List[str]]:
        """Add ONE `all_of`/`any_of` path to `conclusion_id`; refuse a cycle.

        The edge is validated before anything is stored, so a refused insert
        leaves the graph exactly as it was.
        """
        conclusion = _text(conclusion_id)
        if not conclusion:
            raise ClaimGraphError("a support set must name its conclusion")
        mode, members = _support_path(support)
        if conclusion in members:
            raise SupportCycleError(f"{conclusion} cannot support itself")
        for member in members:
            if self._reaches(member, conclusion):
                raise SupportCycleError(
                    f"{conclusion} already supports {member}, so {member} cannot support it"
                )
        self._support.setdefault(conclusion, []).append((mode, members))
        return {mode: list(members)}

    # -- transitions -------------------------------------------------------

    def transition(
        self,
        claim_id: str,
        to_status: str,
        cause_assessment_id: Optional[str] = None,
        group_id: Optional[str] = None,
        *,
        fact_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Move one claim's evidence status inside `group_id`.

        Returns the `claim_transition` payload the caller emits. The graph
        applies it immediately so an invalidation in the same group sees it;
        the events on disk are still the truth, and a group that never gets its
        terminal record simply never happened as far as `load()` is concerned.
        """
        claim = _text(claim_id)
        group = _text(group_id)
        status = _text(to_status)
        if not group:
            raise ClaimGraphError("a claim transition must name its group")
        if group in self._committed_groups:
            raise ClaimGraphError(f"group {group} is already committed")
        if status not in EVIDENCE_STATUSES:
            raise ClaimGraphError(f"{status!r} is not an evidence status")
        if claim not in self._nodes:
            raise UnknownClaimError(f"no claim file is registered under {claim!r}")
        self._guard_epoch(fact_epoch)
        return self._apply(claim, status, _text(cause_assessment_id) or None, group)

    def invalidate_dependents(
        self,
        claim_id: str,
        *,
        group_id: Optional[str] = None,
        cause_assessment_id: Optional[str] = None,
    ) -> Tuple[str, ...]:
        """Flip the conclusions that lost their COMPLETE support to `unknown`.

        Cascades to fixpoint, because a conclusion that just went `unknown`
        stops supporting whatever rested on it. A conclusion with a surviving
        support path is not touched — that is the bound spec §C5 asks for.

        The flips join the group of the transition that caused them (note (a)),
        which defaults to the group currently open.
        """
        group = _text(group_id) or self._current_group()
        if not group:
            raise ClaimGraphError("an invalidation must join a group")
        if group in self._committed_groups:
            raise ClaimGraphError(f"group {group} is already committed")
        cause = _text(cause_assessment_id) or None
        invalidated: List[str] = []
        frontier = [_text(claim_id)]
        while frontier:
            lost = frontier.pop(0)
            for conclusion in self._dependents(lost):
                if self._nodes.get(conclusion, {}).get("evidence_status") == INVALIDATED_STATUS:
                    continue
                if self._supported(conclusion):
                    continue
                self._apply(conclusion, INVALIDATED_STATUS, cause, group)
                invalidated.append(conclusion)
                frontier.append(conclusion)
        return tuple(invalidated)

    def commit_group(self, group_id: str) -> Dict[str, Any]:
        """Close `group_id` with its terminal record; idempotent."""
        group = _text(group_id)
        if not group:
            raise ClaimGraphError("a group commit must name its group")
        terminal = {"group_id": group, "terminal": True}
        if group in self._committed_groups:
            return terminal
        self._committed_groups.add(group)
        if group in self._open_groups:
            self._open_groups.remove(group)
        self._pending.append(terminal)
        return terminal

    def pending_events(self) -> Tuple[Dict[str, Any], ...]:
        """The `claim_transition` payloads authored here, in emit order."""
        return tuple(dict(body) for body in self._pending)

    # -- projection --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """The current state, in one deterministic shape. Absent facts absent."""
        body: Dict[str, Any] = {"schema_version": CLAIM_GRAPH_SCHEMA_VERSION}
        if self._fact_epoch is not None:
            body["fact_epoch"] = self._fact_epoch
        body["claims"] = [
            {"claim_id": identifier, **self._nodes[identifier]}
            for identifier in sorted(self._nodes)
        ]
        body["support"] = [
            {"conclusion_id": conclusion, mode: list(members)}
            for conclusion in sorted(self._support)
            for mode, members in self._support[conclusion]
        ]
        return body

    def materialize(self, execute: Callable[..., Optional[Mapping[str, Any]]]) -> bool:
        """Rewrite the snapshot atomically (temp file + `mv`). False on failure.

        Derived state, so an existing file is REPLACED rather than defended:
        the events it was projected from are the record that must not change.
        """
        try:
            body = canonical_json(self.snapshot())
        except (TypeError, ValueError) as exc:
            logger.debug(f"claim graph is not serializable: {exc}")
            return False
        temp = f"{CLAIM_GRAPH_PATH}.tmp"
        directory = posixpath.dirname(CLAIM_GRAPH_PATH)
        command = (
            f"mkdir -p {shlex.quote(directory)} && "
            f"cat > {shlex.quote(temp)} <<'{CLAIM_GRAPH_HEREDOC}' && "
            f"mv -f {shlex.quote(temp)} {shlex.quote(CLAIM_GRAPH_PATH)}\n"
            f"{body}\n{CLAIM_GRAPH_HEREDOC}"
        )
        try:
            result = execute(command) or {}
        except Exception as exc:
            logger.debug(f"claim graph not materialized: {exc}")
            return False
        return _succeeded(result)

    # -- internals ---------------------------------------------------------

    def _apply(
        self,
        claim_id: str,
        to_status: str,
        cause_assessment_id: Optional[str],
        group_id: str,
    ) -> Dict[str, Any]:
        node = self._nodes.setdefault(claim_id, {})
        payload: Dict[str, Any] = {
            "group_id": group_id,
            "claim_id": claim_id,
            "from_status": node.get("evidence_status") or "untested",
            "to_status": to_status,
        }
        if cause_assessment_id:
            payload["cause_assessment_id"] = cause_assessment_id
        node["evidence_status"] = to_status
        self._pending.append(payload)
        if group_id not in self._open_groups:
            self._open_groups.append(group_id)
        return payload

    def _replay(self, payload: Mapping[str, Any]) -> None:
        """Apply one committed transition without authoring a new event."""
        claim = _text(payload.get("claim_id"))
        status = _text(payload.get("to_status"))
        if claim not in self._nodes:
            logger.debug(f"claim transition for {claim!r} has no claim file; skipped")
            return
        if status not in EVIDENCE_STATUSES:
            logger.debug(f"claim transition for {claim!r} names no evidence status; skipped")
            return
        self._nodes[claim]["evidence_status"] = status

    def _guard_epoch(self, fact_epoch: Optional[int]) -> None:
        cited = _epoch(fact_epoch)
        if cited is None or self._fact_epoch is None:
            return
        if cited < self._fact_epoch:
            raise StaleFactEpochError(
                f"fact epoch {cited} is older than the graph's {self._fact_epoch}; "
                "the receipt stays historical evidence and does not mutate current state"
            )

    def _current_group(self) -> str:
        return self._open_groups[-1] if self._open_groups else ""

    def _dependents(self, claim_id: str) -> Tuple[str, ...]:
        """Conclusions with a DIRECT support path naming `claim_id`."""
        return tuple(
            conclusion
            for conclusion in sorted(self._support)
            if any(claim_id in members for _, members in self._support[conclusion])
        )

    def _supported(self, conclusion_id: str) -> bool:
        """True while ANY support path of the conclusion still holds."""
        paths = self._support.get(conclusion_id) or []
        if not paths:
            return True
        for mode, members in paths:
            alive = [self._alive(member) for member in members]
            if all(alive) if mode == "all_of" else any(alive):
                return True
        return False

    def _alive(self, claim_id: str) -> bool:
        node = self._nodes.get(claim_id)
        if node is None:  # an absent claim file supports nothing
            return False
        if node.get("evidence_status") in LOST_EVIDENCE_STATUSES:
            return False
        return node.get("source_status") not in LOST_SOURCE_STATUSES

    def _reaches(self, start: str, target: str) -> bool:
        """Whether `start` rests, transitively, on `target`."""
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            current = frontier.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            for _, members in self._support.get(current, ()):
                frontier.extend(members)
        return False


def load(
    events: Iterable[Any],
    claim_files: Iterable[Any],
    *,
    fact_epoch: Optional[int] = None,
) -> ClaimGraph:
    """Project the claim files through the committed transitions in `events`.

    `events` are `ControlEvent`s or their recorded mappings, in stream order;
    only `claim_transition` kinds are read. A group is applied only when the
    same stream carries its terminal record, so an uncommitted group — a run
    that died between the transition and the commit — is absent, not partial.

    Nothing here reads `/workspace/.setup_agent/claim_graph.json`: the file is
    a projection of this function, never an input to it.
    """
    graph = ClaimGraph(fact_epoch=fact_epoch)
    for claim in claim_files or ():
        graph.add_claim(claim)

    transitions: List[Dict[str, Any]] = []
    committed: set[str] = set()
    for event in events or ():
        if _event_kind(event) != "claim_transition":
            continue
        payload = _event_payload(event)
        group = _text(payload.get("group_id"))
        if not group:
            continue
        if payload.get("terminal"):
            committed.add(group)
            continue
        transitions.append(dict(payload))

    for payload in transitions:
        if _text(payload.get("group_id")) in committed:
            graph._replay(payload)
    graph._committed_groups.update(committed)
    return graph


# ---------------------------------------------------------------------------
# the live bridge: one assessment -> one committed group (Plan 6 Stage F1)
# ---------------------------------------------------------------------------


def read_claim_files(
    execute: Callable[..., Optional[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Every persisted claim body, ordered by id so two reads agree.

    One bounded glob `cat` — the same read `repair_contracts.read_records`
    performs over the same directory. It is restated here rather than imported
    for the reason `materialize` takes the same argument: this module's
    transport is an `execute` CALLABLE, and reaching for the Stage C3 reader
    would make the C1 graph depend on the C3 repair layer to read its own
    subjects. The equivalence is asserted by test rather than assumed.

    A line that does not parse is skipped rather than failing the read: a
    corrupt neighbour must not hide the claims we do understand.
    """
    try:
        probe = execute(f"cat {shlex.quote(CLAIM_DIR)}/*.json 2>/dev/null") or {}
    except Exception as exc:  # an unreadable directory is an absent fact
        logger.debug(f"claim files unavailable for the graph: {exc}")
        return []
    claims: Dict[str, Dict[str, Any]] = {}
    for line in str(probe.get("output") or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        identifier = _text(payload.get("claim_id")) if isinstance(payload, Mapping) else ""
        if identifier:
            claims[identifier] = dict(payload)
    return [claims[identifier] for identifier in sorted(claims)]


def commit_assessment_transitions(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    *,
    assessment_id: Any,
    typed_code: Any,
    claim_ids: Sequence[str],
    emit: Callable[[str, Mapping[str, Any]], Any],
    fact_epoch: Optional[int] = None,
) -> Tuple[str, ...]:
    """Move the claims one assessment settled, as ONE event group (note (a)).

    A contract cites the stored claims that authorized it, so a verdict on its
    receipt is also a verdict on those claims: `expectation_met` CONFIRMS them,
    and a typed `falsifier_*` CONTRADICTS them and retracts whatever rested on
    them. Every other typed code moves nothing — a compiler error is a real
    fact about the run and falsifies nothing a document states (spec §C5).

    The subjects are the claim FILES, so a claim that never reached disk is
    skipped by name: a contract citing an unpersisted claim is a conflict to
    record, not a crash. Events are handed to `emit` in order and the terminal
    record closes the group, so a run that dies mid-commit leaves a group
    replay reads as absent rather than half-applied.

    Returns the ids that moved. Never raises.
    """
    trigger = _text(assessment_id)
    code = _text(typed_code)
    subjects = [value for value in (_text(item) for item in claim_ids or ()) if value]
    if not trigger or not subjects:
        return ()
    if code == CONFIRMING_CODE:
        status = CONFIRMED_STATUS
    elif code.startswith(CONTRADICTING_PREFIX):
        status = CONTRADICTED_STATUS
    else:
        return ()

    try:
        graph = load((), read_claim_files(execute), fact_epoch=fact_epoch)
        group = group_identity(trigger)
        moved: List[str] = []
        for claim_id in subjects:
            try:
                graph.transition(claim_id, status, trigger, group, fact_epoch=fact_epoch)
            except UnknownClaimError:
                logger.warning(
                    f"contract claim {claim_id!r} has no claim file; {trigger} moved "
                    "nothing for it and the citation stays an open conflict"
                )
                continue
            except ClaimGraphError as exc:
                logger.debug(f"{trigger} could not move {claim_id!r}: {exc}")
                continue
            moved.append(claim_id)
            if status == CONTRADICTED_STATUS:
                moved.extend(
                    graph.invalidate_dependents(
                        claim_id, group_id=group, cause_assessment_id=trigger
                    )
                )
        if not moved:
            return ()
        graph.commit_group(group)
        for payload in graph.pending_events():
            emit("claim_transition", payload)
        graph.materialize(execute)
        return tuple(moved)
    except Exception as exc:  # the graph never breaks the run that fed it
        logger.debug(f"claim transitions for {trigger} were not committed: {exc}")
        return ()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _support_path(support: Mapping[str, Sequence[str]]) -> Tuple[str, Tuple[str, ...]]:
    """Validate one support set into `(mode, members)`; raise otherwise."""
    if not isinstance(support, Mapping):
        raise ClaimGraphError("a support set is an {'all_of'|'any_of': [...]} mapping")
    modes = [mode for mode in SUPPORT_MODES if mode in support]
    if len(modes) != 1 or len(support) != 1:
        raise ClaimGraphError("a support set states exactly one of 'all_of' or 'any_of'")
    mode = modes[0]
    members = [_text(member) for member in support.get(mode) or ()]
    members = [member for member in members if member]
    if not members:
        raise ClaimGraphError(f"an empty {mode} set supports nothing")
    return mode, tuple(members)


def _claim_body(claim: Any) -> Dict[str, Any]:
    """One claim as a mapping, whether it arrived as a payload or a record."""
    if isinstance(claim, Mapping):
        return dict(claim)
    payload = getattr(claim, "payload", None)
    if callable(payload):
        try:
            body = payload()
        except Exception as exc:  # a claim that cannot state itself is skipped
            logger.debug(f"claim record could not be read: {exc}")
            return {}
        if isinstance(body, Mapping):
            return dict(body)
    return {}


def _event_kind(event: Any) -> str:
    if isinstance(event, Mapping):
        return _text(event.get("kind"))
    return _text(getattr(event, "kind", ""))


def _event_payload(event: Any) -> Dict[str, Any]:
    payload = (
        event.get("payload") if isinstance(event, Mapping) else getattr(event, "payload", None)
    )
    return dict(payload) if isinstance(payload, Mapping) else {}


def _epoch(value: Any) -> Optional[int]:
    """An epoch is an integer or an absent fact; never a coerced guess."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


__all__ = [
    "CLAIM_GRAPH_HEREDOC",
    "CLAIM_GRAPH_PATH",
    "CLAIM_GRAPH_SCHEMA_VERSION",
    "CONFIRMED_STATUS",
    "CONFIRMING_CODE",
    "CONTRADICTED_STATUS",
    "CONTRADICTING_PREFIX",
    "INVALIDATED_STATUS",
    "LOST_EVIDENCE_STATUSES",
    "LOST_SOURCE_STATUSES",
    "SUPPORT_MODES",
    "ClaimGraph",
    "ClaimGraphError",
    "StaleFactEpochError",
    "SupportCycleError",
    "UnknownClaimError",
    "commit_assessment_transitions",
    "group_identity",
    "load",
    "read_claim_files",
]
