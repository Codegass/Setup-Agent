# tests/test_claim_graph.py
"""Plan 6 Stage C Task C1 — the causal claim graph and its grouped transitions.

Spec §C5: a mismatch is not automatically a contradiction, and a contradiction
is not automatically a cascade. The graph exists so that retraction is
*bounded*: contradicting a claim invalidates only the conclusions whose
COMPLETE support is lost, and a conclusion with a second live support path
survives untouched. The weak model is never asked to notice any of this.

Three structural properties are asserted here rather than assumed:

* the graph is a projection of the event stream, not a file — transitions are
  `claim_transition` control events grouped by `group_id`, a group is real
  only once its terminal record lands (plan §Stage C binding note (a)), and
  `load()` never reads the materialized snapshot;
* support sets are explicit `all_of`/`any_of` sets over stable claim ids, and
  a cycle is refused AT INSERT so no reader ever walks one;
* a transition citing a fact epoch older than the graph's is refused by name —
  a late receipt stays historical evidence and cannot mutate current state.

Scripted-orchestrator style (house pattern, shared with
tests/test_claim_records.py and tests/test_receipt_v2_and_assessments.py).
"""

import hashlib
import json

import pytest
from test_receipt_v2_and_assessments import ContainerFS

from sag.agent.claim_graph import (
    CLAIM_GRAPH_HEREDOC,
    CLAIM_GRAPH_PATH,
    CLAIM_GRAPH_SCHEMA_VERSION,
    ClaimGraph,
    ClaimGraphError,
    StaleFactEpochError,
    SupportCycleError,
    UnknownClaimError,
    group_identity,
    load,
)
from sag.agent.claim_records import (
    EVIDENCE_STATUSES,
    InferredClaim,
    InferredSourceRef,
    PolicyClaim,
    PolicySourceRef,
)
from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ClaimTransitionPayload,
    ControlEvent,
)

HASH = "b" * 64


# ---------------------------------------------------------------------------
# fixtures: claims live as files, so the graph is built from claim payloads
# ---------------------------------------------------------------------------


def policy_claim(source_range, *, kind="lifecycle", value=None, **overrides):
    return PolicyClaim(
        kind=kind,
        typed_value=value or {"runner": "maven", "argv": "mvn -q test"},
        source_class="repository_doc",
        source_ref=PolicySourceRef(
            entry_id="entry-readme",
            source_hash=HASH,
            source_range=source_range,
        ),
        extraction_method="markdown_command",
        **overrides,
    )


def inferred_claim(rule_id, supports, **overrides):
    return InferredClaim(
        kind="capability",
        typed_value={"capability": "llvm"},
        source_class="inferred",
        source_ref=InferredSourceRef(rule_id=rule_id, support_claim_ids=tuple(supports)),
        **overrides,
    )


def transition_event(sequence, payload):
    return ControlEvent(sequence=sequence, kind="claim_transition", payload=payload)


def commit_event(sequence, group):
    return transition_event(sequence, {"group_id": group, "terminal": True})


# ---------------------------------------------------------------------------
# the shared contract: group identity and the new control-event kind
# ---------------------------------------------------------------------------


def test_group_id_is_the_bound_formula():
    """`grp-` + sha256(trigger id)[:12] — the plan's Stage C shared contract."""
    trigger = "asm-rcp_maven_0007-expectation_met-1a2b3c4d"
    digest = hashlib.sha256(trigger.encode("utf-8")).hexdigest()[:12]

    assert group_identity(trigger) == f"grp-{digest}"
    assert group_identity(trigger) == group_identity(trigger)
    assert group_identity(trigger) != group_identity(trigger + "x")


def test_claim_transition_is_a_registered_control_event_kind():
    assert "claim_transition" in CONTROL_EVENT_KINDS

    event = transition_event(
        1,
        {
            "group_id": "grp-000000000001",
            "claim_id": "lifecycle-aaaaaaaaaaaa",
            "from_status": "untested",
            "to_status": "confirmed",
        },
    )

    assert isinstance(event.typed_payload, ClaimTransitionPayload)


def test_existing_event_kinds_stay_byte_stable():
    """A new kind is appended; nothing before it moves, and an existing
    payload still drops the keys it never set."""
    assert CONTROL_EVENT_KINDS[:10] == (
        "planner_response",
        "scheduler_decision",
        "action_envelope",
        "forced_action",
        "tool_result",
        "validator_observation",
        "gate_decision",
        "phase_transition",
        "loop_decision",
        "evidence_close",
    )

    envelope = ControlEvent(
        sequence=1,
        kind="action_envelope",
        payload={
            "envelope_id": "env-1",
            "plan_index": 0,
            "tool": "build",
            "exact_params": {"action": "test"},
            "envelope_sha256": HASH,
        },
    )

    assert "tool_call_id" not in envelope.payload


def test_transition_payload_keeps_absent_facts_absent():
    payload = {
        "group_id": "grp-000000000001",
        "claim_id": "lifecycle-aaaaaaaaaaaa",
        "from_status": "untested",
        "to_status": "confirmed",
    }

    event = transition_event(1, payload)

    assert event.payload == payload
    assert ClaimTransitionPayload.model_validate(payload).model_dump(mode="json") == payload


def test_terminal_record_carries_only_its_group():
    commit = commit_event(2, "grp-000000000001")

    assert commit.payload == {"group_id": "grp-000000000001", "terminal": True}

    with pytest.raises(ValueError):
        transition_event(
            3,
            {
                "group_id": "grp-000000000001",
                "terminal": True,
                "claim_id": "lifecycle-aaaaaaaaaaaa",
                "from_status": "untested",
                "to_status": "confirmed",
            },
        )


def test_a_transition_without_claim_or_statuses_is_refused():
    with pytest.raises(ValueError):
        transition_event(1, {"group_id": "grp-000000000001"})

    with pytest.raises(ValueError):
        transition_event(
            1, {"group_id": "grp-000000000001", "claim_id": "x", "to_status": "confirmed"}
        )

    with pytest.raises(ValueError):
        transition_event(
            1,
            {
                "claim_id": "x",
                "from_status": "untested",
                "to_status": "confirmed",
            },
        )


def test_transition_statuses_are_the_claim_record_vocabulary():
    """One evidence vocabulary across the two modules, asserted not assumed."""
    for status in EVIDENCE_STATUSES:
        ClaimTransitionPayload.model_validate(
            {
                "group_id": "grp-000000000001",
                "claim_id": "lifecycle-aaaaaaaaaaaa",
                "from_status": "untested",
                "to_status": status,
            }
        )

    with pytest.raises(ValueError):
        ClaimTransitionPayload.model_validate(
            {
                "group_id": "grp-000000000001",
                "claim_id": "lifecycle-aaaaaaaaaaaa",
                "from_status": "untested",
                "to_status": "probably_fine",
            }
        )


# ---------------------------------------------------------------------------
# authoring: a transition is an event, and the event round-trips
# ---------------------------------------------------------------------------


def test_authored_transition_round_trips_through_control_event():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())
    group = group_identity("rcp-maven-0001")

    payload = graph.transition(claim.claim_id, "confirmed", "asm-0001", group)
    graph.commit_group(group)

    assert payload == {
        "group_id": group,
        "claim_id": claim.claim_id,
        "from_status": "untested",
        "to_status": "confirmed",
        "cause_assessment_id": "asm-0001",
    }
    assert graph.evidence_status(claim.claim_id) == "confirmed"

    events = [transition_event(index, body) for index, body in enumerate(graph.pending_events(), 1)]

    assert [event.payload for event in events] == list(graph.pending_events())
    assert events[-1].payload == {"group_id": group, "terminal": True}


def test_an_uncaused_transition_records_no_cause_key():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())

    payload = graph.transition(claim.claim_id, "blocked", None, group_identity("t"))

    assert "cause_assessment_id" not in payload


def test_a_transition_must_name_its_group_and_a_known_claim():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())

    with pytest.raises(ClaimGraphError):
        graph.transition(claim.claim_id, "confirmed", None, "")

    with pytest.raises(UnknownClaimError):
        graph.transition("lifecycle-ffffffffffff", "confirmed", None, group_identity("t"))


def test_a_committed_group_cannot_be_reopened():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())
    group = group_identity("rcp-0001")
    graph.transition(claim.claim_id, "confirmed", None, group)
    first = graph.commit_group(group)

    assert graph.commit_group(group) == first
    assert graph.pending_events().count(first) == 1

    with pytest.raises(ClaimGraphError):
        graph.transition(claim.claim_id, "blocked", None, group)


# ---------------------------------------------------------------------------
# replay: the events are the truth
# ---------------------------------------------------------------------------


def committed_history():
    """One documented claim, one physical claim, one inference over both."""
    documented = policy_claim("L4")
    configured = policy_claim("L9", value={"runner": "maven", "argv": "mvn -q verify"})
    concluded = inferred_claim(
        "rule-lifecycle-agreement",
        [documented.claim_id, configured.claim_id],
    )
    claims = [documented.payload(), configured.payload(), concluded.payload()]
    group = group_identity("rcp-maven-0001")
    events = [
        transition_event(
            1,
            {
                "group_id": group,
                "claim_id": documented.claim_id,
                "from_status": "untested",
                "to_status": "confirmed",
                "cause_assessment_id": "asm-0001",
            },
        ),
        commit_event(2, group),
    ]
    return claims, events, (documented, configured, concluded)


def test_replay_is_deterministic():
    claims, events, (documented, _, _) = committed_history()

    first = load(events, claims)
    second = load(events, claims)

    assert first.snapshot() == second.snapshot()
    assert first.evidence_status(documented.claim_id) == "confirmed"
    assert first.pending_events() == ()


def test_an_uncommitted_group_is_invisible_to_load():
    claims, events, (documented, _, _) = committed_history()
    uncommitted = group_identity("rcp-maven-0002")
    events = events + [
        transition_event(
            3,
            {
                "group_id": uncommitted,
                "claim_id": documented.claim_id,
                "from_status": "confirmed",
                "to_status": "contradicted",
                "cause_assessment_id": "asm-0002",
            },
        )
    ]

    graph = load(events, claims)

    assert graph.evidence_status(documented.claim_id) == "confirmed"


def test_a_group_becomes_visible_only_once_its_terminal_record_lands():
    claims, events, (documented, _, _) = committed_history()
    later = group_identity("rcp-maven-0002")
    pending = events + [
        transition_event(
            3,
            {
                "group_id": later,
                "claim_id": documented.claim_id,
                "from_status": "confirmed",
                "to_status": "contradicted",
            },
        )
    ]

    assert load(pending, claims).evidence_status(documented.claim_id) == "confirmed"
    assert (
        load(pending + [commit_event(4, later)], claims).evidence_status(documented.claim_id)
        == "contradicted"
    )


def test_an_authored_group_survives_the_control_event_sink_verbatim(tmp_path):
    """End to end: author -> emit -> JSONL -> reload, with no byte lost."""
    from sag.agent.control_events import ControlEventSink

    claims, events, (documented, _, concluded) = committed_history()
    graph = load(events, claims)
    group = group_identity("rcp-maven-000c")
    graph.transition(documented.claim_id, "contradicted", "asm-000c", group)
    graph.invalidate_dependents(documented.claim_id)
    graph.commit_group(group)

    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    for body in graph.pending_events():
        sink.emit("claim_transition", body)
    recorded = [
        ControlEvent.model_validate_json(line)
        for line in (tmp_path / "control_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [event.payload for event in recorded] == list(graph.pending_events())
    assert load(events + recorded, claims).snapshot() == graph.snapshot()
    assert load(events + recorded, claims).evidence_status(concluded.claim_id) == "unknown"


def test_load_reads_the_support_set_the_claim_file_states():
    claims, events, (documented, configured, concluded) = committed_history()

    graph = load(events, claims)

    assert graph.supports(concluded.claim_id) == (
        {"all_of": [documented.claim_id, configured.claim_id]},
    )


def test_load_skips_a_transition_for_a_claim_that_has_no_file():
    claims, events, _ = committed_history()
    orphan = group_identity("rcp-maven-0009")
    events = events + [
        transition_event(
            3,
            {
                "group_id": orphan,
                "claim_id": "lifecycle-ffffffffffff",
                "from_status": "untested",
                "to_status": "confirmed",
            },
        ),
        commit_event(4, orphan),
    ]

    graph = load(events, claims)

    assert "lifecycle-ffffffffffff" not in graph.claim_ids()


# ---------------------------------------------------------------------------
# support sets, cycles and bounded invalidation
# ---------------------------------------------------------------------------


def test_a_support_cycle_is_rejected_at_insert():
    first = policy_claim("L4")
    second = policy_claim("L9", value={"runner": "maven", "argv": "mvn -q verify"})
    graph = ClaimGraph()
    graph.add_claim(first.payload())
    graph.add_claim(second.payload())
    graph.add_support(first.claim_id, {"all_of": [second.claim_id]})

    with pytest.raises(SupportCycleError):
        graph.add_support(second.claim_id, {"any_of": [first.claim_id]})
    with pytest.raises(SupportCycleError):
        graph.add_support(second.claim_id, {"all_of": [second.claim_id]})

    assert graph.supports(second.claim_id) == ()


def test_a_malformed_support_set_is_refused():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())

    for bad in ({}, {"all_of": []}, {"some_of": ["x"]}, {"all_of": ["x"], "any_of": ["y"]}):
        with pytest.raises(ClaimGraphError):
            graph.add_support(claim.claim_id, bad)


def test_losing_one_arm_of_an_all_of_set_invalidates_the_conclusion():
    claims, events, (documented, configured, concluded) = committed_history()
    graph = load(events, claims)
    group = group_identity("rcp-maven-0003")

    graph.transition(documented.claim_id, "contradicted", "asm-0003", group)
    invalidated = graph.invalidate_dependents(documented.claim_id)
    graph.commit_group(group)

    assert invalidated == (concluded.claim_id,)
    assert graph.evidence_status(concluded.claim_id) == "unknown"
    assert graph.evidence_status(configured.claim_id) == "untested"
    assert [body.get("claim_id") for body in graph.pending_events()] == [
        documented.claim_id,
        concluded.claim_id,
        None,
    ]


def test_a_surviving_any_of_alternative_keeps_the_conclusion():
    claims, events, (documented, configured, concluded) = committed_history()
    graph = load(events, claims)
    graph.add_support(concluded.claim_id, {"any_of": [documented.claim_id, configured.claim_id]})
    group = group_identity("rcp-maven-0004")

    graph.transition(documented.claim_id, "contradicted", "asm-0004", group)
    invalidated = graph.invalidate_dependents(documented.claim_id)

    assert invalidated == ()
    assert graph.evidence_status(concluded.claim_id) == "untested"


def test_losing_every_any_of_alternative_invalidates_the_conclusion():
    documented = policy_claim("L4")
    configured = policy_claim("L9", value={"runner": "maven", "argv": "mvn -q verify"})
    concluded = inferred_claim("rule-either", [documented.claim_id])
    graph = ClaimGraph()
    for claim in (documented, configured, concluded):
        graph.add_claim(claim.payload())
    graph.add_support(concluded.claim_id, {"any_of": [documented.claim_id, configured.claim_id]})
    group = group_identity("rcp-maven-0005")

    graph.transition(documented.claim_id, "contradicted", None, group)
    assert graph.invalidate_dependents(documented.claim_id) == ()

    graph.transition(configured.claim_id, "contradicted", None, group)
    assert graph.invalidate_dependents(configured.claim_id) == (concluded.claim_id,)


def test_invalidation_cascades_through_the_conclusions_it_reaches():
    base = policy_claim("L4")
    middle = inferred_claim("rule-middle", [base.claim_id])
    top = inferred_claim("rule-top", [middle.claim_id])
    graph = ClaimGraph()
    for claim in (base, middle, top):
        graph.add_claim(claim.payload())
    graph.add_support(middle.claim_id, {"all_of": [base.claim_id]})
    graph.add_support(top.claim_id, {"all_of": [middle.claim_id]})
    group = group_identity("rcp-maven-0006")

    graph.transition(base.claim_id, "contradicted", None, group)

    assert graph.invalidate_dependents(base.claim_id) == (middle.claim_id, top.claim_id)
    assert graph.evidence_status(top.claim_id) == "unknown"


def test_a_stale_source_is_lost_support_too():
    """Spec §C5: contradicting a claim OR staling its source can invalidate."""
    base = policy_claim("L4", source_status="stale")
    concluded = inferred_claim("rule-stale", [base.claim_id])
    graph = ClaimGraph()
    graph.add_claim(base.payload())
    graph.add_claim(concluded.payload())
    graph.add_support(concluded.claim_id, {"all_of": [base.claim_id]})

    assert graph.invalidate_dependents(
        base.claim_id, group_id=group_identity("rcp-maven-0007")
    ) == (concluded.claim_id,)


def test_a_conclusion_whose_support_file_is_absent_is_not_supported():
    concluded = inferred_claim("rule-orphan", ["lifecycle-ffffffffffff"])

    graph = load([], [concluded.payload()])

    assert graph.invalidate_dependents(
        "lifecycle-ffffffffffff", group_id=group_identity("rcp-0008")
    ) == (concluded.claim_id,)


def test_invalidation_must_join_a_group():
    base = policy_claim("L4")
    concluded = inferred_claim("rule-grouped", [base.claim_id])
    graph = ClaimGraph()
    graph.add_claim(base.payload())
    graph.add_claim(concluded.payload())
    graph.add_support(concluded.claim_id, {"all_of": [base.claim_id]})

    with pytest.raises(ClaimGraphError):
        graph.invalidate_dependents(base.claim_id)


# ---------------------------------------------------------------------------
# epoch guard
# ---------------------------------------------------------------------------


def test_a_transition_citing_a_stale_fact_epoch_is_refused_by_name():
    claim = policy_claim("L4")
    graph = ClaimGraph(fact_epoch=3)
    graph.add_claim(claim.payload())

    with pytest.raises(StaleFactEpochError):
        graph.transition(
            claim.claim_id, "contradicted", "asm-late", group_identity("late"), fact_epoch=2
        )

    assert graph.evidence_status(claim.claim_id) == "untested"
    assert graph.pending_events() == ()


def test_a_transition_at_or_after_the_current_epoch_is_applied():
    claim = policy_claim("L4")
    graph = ClaimGraph(fact_epoch=3)
    graph.add_claim(claim.payload())

    graph.transition(claim.claim_id, "confirmed", None, group_identity("now"), fact_epoch=3)
    graph.transition(claim.claim_id, "blocked", None, group_identity("now"), fact_epoch=4)

    assert graph.evidence_status(claim.claim_id) == "blocked"


def test_an_epochless_graph_states_no_epoch_and_refuses_nothing():
    claim = policy_claim("L4")
    graph = ClaimGraph()
    graph.add_claim(claim.payload())

    graph.transition(claim.claim_id, "confirmed", None, group_identity("g"), fact_epoch=1)

    assert graph.evidence_status(claim.claim_id) == "confirmed"
    assert "fact_epoch" not in graph.snapshot()


# ---------------------------------------------------------------------------
# materialization: derived state, atomically rewritten
# ---------------------------------------------------------------------------


def test_materialize_writes_the_snapshot_atomically():
    claims, events, (documented, configured, concluded) = committed_history()
    graph = load(events, claims, fact_epoch=3)
    container = ContainerFS()

    assert graph.materialize(container) is True

    write = container.writes()[-1]
    assert f"mv -f {CLAIM_GRAPH_PATH}.tmp {CLAIM_GRAPH_PATH}" in write
    assert CLAIM_GRAPH_HEREDOC in write

    body = json.loads(container.files[CLAIM_GRAPH_PATH])
    assert body["schema_version"] == CLAIM_GRAPH_SCHEMA_VERSION
    assert body["fact_epoch"] == 3
    assert body == graph.snapshot()
    assert [entry["claim_id"] for entry in body["claims"]] == sorted(graph.claim_ids())
    assert body["support"] == [
        {
            "conclusion_id": concluded.claim_id,
            "all_of": [documented.claim_id, configured.claim_id],
        }
    ]


def test_materialize_rewrites_rather_than_refusing_a_new_body():
    claims, events, (documented, _, _) = committed_history()
    graph = load(events, claims)
    container = ContainerFS()
    graph.materialize(container)
    group = group_identity("rcp-maven-000a")
    graph.transition(documented.claim_id, "blocked", None, group)
    graph.commit_group(group)

    assert graph.materialize(container) is True

    body = json.loads(container.files[CLAIM_GRAPH_PATH])
    statuses = {entry["claim_id"]: entry["evidence_status"] for entry in body["claims"]}
    assert statuses[documented.claim_id] == "blocked"


def test_materialize_reports_a_failed_write():
    claims, events, _ = committed_history()

    assert load(events, claims).materialize(ContainerFS(writable=False)) is False


def test_a_fresh_load_ignores_the_materialized_file():
    claims, events, (documented, _, _) = committed_history()
    graph = load(events, claims)
    container = ContainerFS()
    uncommitted = group_identity("rcp-maven-000b")
    graph.transition(documented.claim_id, "contradicted", None, uncommitted)
    graph.materialize(container)

    reloaded = load(
        events
        + [transition_event(index, body) for index, body in enumerate(graph.pending_events(), 3)],
        claims,
    )

    assert json.loads(container.files[CLAIM_GRAPH_PATH]) != reloaded.snapshot()
    assert reloaded.evidence_status(documented.claim_id) == "confirmed"
