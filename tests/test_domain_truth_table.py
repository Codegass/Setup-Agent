# tests/test_domain_truth_table.py
"""Domain truth table at the gate and through sealing (Plan 5 Stage C, P0-F).

Ground-truth review 2026-07-26 (§"Unproved independence", §"Partial claims are
upgraded"): the harness told the model that "each island builds independently,
so one island's failure says nothing about the others" while Bigtop's producer
built 3.7.0-SNAPSHOT against consumers pinned at 3.5/3.6 — and then a global
artifact-presence check refined the model's truthful 2/4 partial into validated
success. Domain facts disappeared before sealing.

Schema v1 (plan §"Binding notes (Stage C)") is the cross-lane contract, so the
fixtures here are hand-written recommendation/receipt payloads rather than
survey output: this lane must hold the rollup shape even before lane c1's
producer exists.
"""

import json

from sag.agent.attempt_policy import (
    IncompatibleDomainEdge,
    UntriedIslandsRequirement,
    untried_islands_requirement,
)
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.phase_gates import (
    ClaimDisposition,
    ValidatorState,
    check_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.verdict_finalizer import (
    BuildEvidenceSnapshot,
    RunVerdictSnapshot,
    _fold_build_evidence,
    _fold_test_stats,
)
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

BIGTOP = "/workspace/bigtop"
FRAMEWORK = f"{BIGTOP}/bigtop-test-framework"
GENERATORS = f"{BIGTOP}/bigtop-data-generators"
SPARK = f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark"
QUEUE = f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue"

SPARK_DETAIL = (
    "requires org.apache.bigtop:bigpetstore-data-generator 3.5.0-SNAPSHOT; "
    "producer builds 3.7.0-SNAPSHOT"
)
QUEUE_DETAIL = (
    "requires org.apache.bigtop:bigpetstore-data-generator 3.6.0-SNAPSHOT; "
    "producer builds 3.7.0-SNAPSHOT"
)

BUILD_DOMAINS = [
    {"root": FRAMEWORK, "system": "maven", "languages": ["java"]},
    {
        "root": GENERATORS,
        "system": "gradle",
        "languages": ["java", "groovy"],
        "produces": [
            {
                "group": "org.apache.bigtop",
                "name": "bigpetstore-data-generator",
                "version": "3.7.0-SNAPSHOT",
            }
        ],
    },
    {"root": SPARK, "system": "gradle", "languages": ["scala"]},
    {"root": QUEUE, "system": "gradle", "languages": ["java"]},
]

DOMAIN_EDGES = [
    {
        "consumer": SPARK,
        "producer": GENERATORS,
        "status": "version_incompatible",
        "detail": SPARK_DETAIL,
    },
    {
        "consumer": QUEUE,
        "producer": GENERATORS,
        "status": "version_incompatible",
        "detail": QUEUE_DETAIL,
    },
]

# 2 success, 1 failed via receipt (Spark: a failed receipt outranks its own
# classified blocker), 1 blocked via edge (the queue was never attempted).
BIGTOP_DOMAIN_STATES = {
    FRAMEWORK: {"state": "success"},
    GENERATORS: {"state": "success"},
    SPARK: {"state": "failed", "blocker": SPARK_DETAIL},
    QUEUE: {"state": "blocked", "blocker": QUEUE_DETAIL},
}


def _receipt(receipt_id, working_directory, outcome):
    return {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "tool": "gradle",
        "requested_action": "compile",
        "effective_action": "build",
        "argv": "./gradlew build",
        "working_directory": working_directory,
        "exit_code": 0 if outcome == "completed" else 1,
        "outcome": outcome,
        "report_delta": {"new": [], "changed": []},
    }


BIGTOP_RECEIPTS = (
    _receipt("inv-maven-build_1-0001", FRAMEWORK, "completed"),
    _receipt("inv-gradle-build_1-0002", GENERATORS, "completed"),
    _receipt("inv-gradle-build_1-0003", SPARK, "failed"),
)


def _manifest(*, domains=BUILD_DOMAINS, edges=DOMAIN_EDGES):
    manifest = {
        "survey": {"project_path": BIGTOP},
        "root_shape": "pathological_aggregator",
        "build_system": "maven",
        "build_root": FRAMEWORK,
        "build_islands": [
            {"root": domain["root"], "system": domain["system"]} for domain in domains or ()
        ],
    }
    if domains is not None:
        manifest["build_domains"] = domains
    if edges is not None:
        manifest["domain_edges"] = edges
    return manifest


class DomainOrch:
    """Manifest by lossless file read AND plain `cat` (both readers exist in
    production: the gate uses `read_build_requirements`, the attempt policy
    uses a plain `cat`); receipts by one `cat` of the receipt directory."""

    def __init__(self, manifest=None, receipts=BIGTOP_RECEIPTS):
        self.manifest = manifest
        self.files = {} if manifest is None else {REQUIREMENTS_PATH: json.dumps(manifest)}
        self.receipts = receipts
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=None):
        self.commands.append(command)
        if REQUIREMENTS_PATH in command:
            if self.manifest is None:
                return {"success": False, "exit_code": 1, "output": "No such file"}
            return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
        if "invocation_receipts" in command:
            # write_receipt persists single-line JSON, one file per invocation.
            body = "".join(f"{json.dumps(receipt, sort_keys=True)}\n" for receipt in self.receipts)
            return {"success": True, "exit_code": 0, "output": body}
        if "test -d" in command:
            return {"success": True, "exit_code": 0, "output": "exists"}
        return {"success": True, "exit_code": 0, "output": ""}


class GreenValidator:
    """Global artifact presence says green — the P0-F upgrade pressure."""

    def __init__(self, has_test_reports=True):
        self._has_test_reports = has_test_reports

    def validate_build_status(self, project_name=None):
        return {
            "success": True,
            "build_complete": True,
            "evidence_status": "success",
            "evidence": {"build_system": "maven", "has_artifacts": True, "class_count": 279},
            "reason": "artifacts present under the project root",
        }

    def validate_test_status(self, project_name=None):
        return {
            "has_test_reports": self._has_test_reports,
            "evidence_status": "success",
            "reason": "test reports present",
            "test_stats": {"executed": 50, "passed": 50, "failed": 0, "errors": 0, "skipped": 0},
            "total_tests": 50,
            "unique_tests": 50,
            "unique_passed_tests": 50,
        }


def _claim(phase, outcome):
    return PhaseClaim(phase=phase, claimed_outcome=outcome)


def _gate(phase, outcome, *, orch=None, validator=None):
    return check_phase_claim(
        phase,
        _claim(phase, outcome),
        validator=validator or GreenValidator(),
        orchestrator=orch or DomainOrch(_manifest()),
        project_name=None,
    )


# --------------------------------------------------------------------------- #
# 1. attempt_policy: the falsified independence claim is gone
# --------------------------------------------------------------------------- #
def test_untried_islands_message_drops_the_falsified_independence_claim():
    requirement = UntriedIslandsRequirement(roots=(SPARK, QUEUE), systems=("gradle", "gradle"))
    message = requirement.message()
    assert "builds independently" not in message
    assert "nothing about the others" not in message
    assert "a failed attempt is a receipt, an untried island is not" in message


def test_incompatible_edges_are_named_as_blockers_in_the_message():
    requirement = UntriedIslandsRequirement(
        roots=(QUEUE,),
        systems=("gradle",),
        edges=(IncompatibleDomainEdge(consumer=QUEUE, producer=GENERATORS, detail=QUEUE_DETAIL),),
    )
    message = requirement.message()
    assert QUEUE in message
    assert GENERATORS in message
    assert QUEUE_DETAIL in message
    assert "a failed attempt is a receipt, an untried island is not" in message


def test_edges_default_to_empty_and_claim_nothing_about_independence():
    requirement = UntriedIslandsRequirement(roots=(SPARK,))
    assert requirement.edges == ()
    assert "blocker" not in requirement.message().lower()


def test_manifest_domain_edges_reach_the_requirement():
    """The graph fact the analyzer sealed is the graph fact the model is told."""
    requirement = untried_islands_requirement(
        RunEvidenceState(run_id="domain-edges"),
        DomainOrch(_manifest()),
        phase="build",
        signal="blocked",
        outcome="failed",
    )
    assert requirement is not None
    assert [edge.consumer for edge in requirement.edges] == [SPARK, QUEUE]
    assert QUEUE_DETAIL in requirement.message()


def test_a_manifest_without_domain_edges_yields_no_blockers():
    requirement = untried_islands_requirement(
        RunEvidenceState(run_id="domain-edges"),
        DomainOrch(_manifest(edges=None)),
        phase="build",
        signal="blocked",
        outcome="failed",
    )
    assert requirement is not None
    assert requirement.edges == ()


# --------------------------------------------------------------------------- #
# 2. phase_gates: domain_states from recommendation graph + Stage B receipts
# --------------------------------------------------------------------------- #
def test_bigtop_shaped_domain_states_are_computed_from_receipts_and_edges():
    gate = _gate("build", PhaseOutcome.PARTIAL)
    assert gate.validated_facts["build.domain_states"] == BIGTOP_DOMAIN_STATES


def test_a_failed_receipt_outranks_a_classified_blocker():
    """ "A classified blocker is not a green waiver" — nor a failure eraser."""
    states = _gate("build", PhaseOutcome.PARTIAL).validated_facts["build.domain_states"]
    assert states[SPARK]["state"] == "failed"


def test_a_blocker_outranks_untried():
    states = _gate("build", PhaseOutcome.PARTIAL).validated_facts["build.domain_states"]
    assert states[QUEUE] == {"state": "blocked", "blocker": QUEUE_DETAIL}


def test_no_receipt_and_no_edge_is_untried():
    orch = DomainOrch(_manifest(edges=[]), receipts=())
    states = _gate("build", PhaseOutcome.PARTIAL, orch=orch).validated_facts["build.domain_states"]
    assert {root: entry["state"] for root, entry in states.items()} == {
        FRAMEWORK: "untried",
        GENERATORS: "untried",
        SPARK: "untried",
        QUEUE: "untried",
    }


def test_a_receipt_below_a_domain_root_binds_to_that_domain():
    orch = DomainOrch(
        _manifest(edges=[]),
        receipts=(_receipt("inv-maven-build_1-0001", f"{FRAMEWORK}/submodule", "completed"),),
    )
    states = _gate("build", PhaseOutcome.PARTIAL, orch=orch).validated_facts["build.domain_states"]
    assert states[FRAMEWORK]["state"] == "success"


def test_a_receipt_binds_to_the_nearest_domain_not_to_every_ancestor():
    """An aggregator is not built because something nested under it built."""
    aggregator = {"root": BIGTOP, "system": "maven", "languages": ["java"]}
    orch = DomainOrch(
        _manifest(domains=[aggregator, *BUILD_DOMAINS], edges=[]),
        receipts=(_receipt("inv-gradle-build_1-0001", GENERATORS, "completed"),),
    )
    states = _gate("build", PhaseOutcome.PARTIAL, orch=orch).validated_facts["build.domain_states"]
    assert states[GENERATORS]["state"] == "success"
    assert states[BIGTOP]["state"] == "untried"


def test_a_later_receipt_supersedes_an_earlier_one_at_the_same_domain():
    orch = DomainOrch(
        _manifest(edges=[]),
        receipts=(
            _receipt("inv-gradle-build_1-0007", GENERATORS, "failed"),
            _receipt("inv-gradle-build_2-0011", GENERATORS, "completed"),
        ),
    )
    states = _gate("build", PhaseOutcome.PARTIAL, orch=orch).validated_facts["build.domain_states"]
    assert states[GENERATORS]["state"] == "success"


def test_the_test_rollup_carries_domain_states():
    gate = _gate("test", PhaseOutcome.PARTIAL)
    assert gate.validated_facts["test.stats"]["domain_states"] == BIGTOP_DOMAIN_STATES


# --------------------------------------------------------------------------- #
# 3. Truth table: confirm or downgrade, never upgrade (P0-F)
# --------------------------------------------------------------------------- #
def test_artifact_presence_cannot_refine_a_truthful_partial_into_success():
    gate = _gate("build", PhaseOutcome.PARTIAL)
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.validator_state is ValidatorState.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.accepted is True
    assert SPARK in gate.reason


def test_the_test_gate_cannot_refine_a_truthful_partial_either():
    gate = _gate("test", PhaseOutcome.PARTIAL)
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED


def test_a_failed_claim_with_unclosed_domains_is_not_lifted_to_partial():
    gate = _gate("build", PhaseOutcome.FAILED)
    assert gate.validated_outcome is PhaseOutcome.FAILED
    assert gate.validator_state is ValidatorState.RED
    assert gate.disposition is ClaimDisposition.CONFIRMED


def test_a_partial_claim_with_every_domain_successful_is_still_refined():
    """The cap is unclosed domains, not multi-domain projects as such."""
    orch = DomainOrch(
        _manifest(edges=[]),
        receipts=tuple(
            _receipt(f"inv-gradle-build_1-000{index}", domain["root"], "completed")
            for index, domain in enumerate(BUILD_DOMAINS, start=1)
        ),
    )
    gate = _gate("build", PhaseOutcome.PARTIAL, orch=orch)
    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.disposition is ClaimDisposition.PESSIMISTIC


def test_the_no_upgrade_cap_never_invents_a_downgrade():
    """Boundary: the rule caps refinement AT the claim; it does not contradict
    a success claim the physical oracle confirms."""
    gate = _gate("build", PhaseOutcome.SUCCESS)
    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.disposition is ClaimDisposition.CONFIRMED


def test_a_single_domain_project_emits_no_key_and_behaves_identically():
    """cli/tvm: no multi-domain decomposition surveyed, byte-identical gate."""
    domainless = _gate("build", PhaseOutcome.PARTIAL, orch=DomainOrch(_manifest(domains=None)))
    no_manifest = _gate("build", PhaseOutcome.PARTIAL, orch=DomainOrch())
    assert "build.domain_states" not in domainless.validated_facts
    assert "build.domain_states" not in no_manifest.validated_facts
    assert domainless.validated_outcome is PhaseOutcome.SUCCESS
    assert domainless.disposition is ClaimDisposition.PESSIMISTIC
    assert domainless.to_metadata() == no_manifest.to_metadata()


def test_a_single_domain_test_rollup_carries_no_domain_states_key():
    gate = _gate("test", PhaseOutcome.PARTIAL, orch=DomainOrch(_manifest(domains=None)))
    assert "domain_states" not in gate.validated_facts["test.stats"]


# --------------------------------------------------------------------------- #
# 4. Sealing: domain_states ride with the build evidence block
# --------------------------------------------------------------------------- #
def _state_with_domain_fact(key, value):
    state = RunEvidenceState(run_id="sealed-domains")
    scope = StateScope.ARTIFACTS if key.startswith("build.") else StateScope.TEST_RUNTIME
    state.register_fact(scope, key, value, "gate://build")
    return state


def test_the_new_rollup_key_does_not_invalidate_the_sealed_test_stats():
    """`SnapshotTestStats` is extra-forbid: the gate's new rollup key must not
    turn a valid receipt-scoped rollup into `validated_test_stats_invalid`."""
    gate = _gate("test", PhaseOutcome.PARTIAL)
    state = _state_with_domain_fact("test.stats", gate.validated_facts["test.stats"])
    stats, conflicts = _fold_test_stats(state, test_pass_threshold=0.8)
    assert "validated_test_stats_invalid" not in conflicts
    assert stats.unique.executed == 50


def test_sealed_build_evidence_carries_the_gate_domain_states():
    state = _state_with_domain_fact("build.domain_states", BIGTOP_DOMAIN_STATES)
    build, _conflicts = _fold_build_evidence(state)
    assert build.domain_states == BIGTOP_DOMAIN_STATES
    assert build.model_dump()["domain_states"][SPARK]["state"] == "failed"


def test_sealed_domain_states_survive_the_physical_oracle_fold():
    state = _state_with_domain_fact("build.domain_states", BIGTOP_DOMAIN_STATES)
    build, _conflicts = _fold_build_evidence(state, validator=GreenValidator(), project_name=None)
    assert build.source == "physical"
    assert build.domain_states == BIGTOP_DOMAIN_STATES


def test_sealing_falls_back_to_the_test_rollup_when_the_build_gate_never_ran():
    state = _state_with_domain_fact(
        "test.stats",
        {
            "unique": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "raw": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "domain_states": BIGTOP_DOMAIN_STATES,
        },
    )
    build, _conflicts = _fold_build_evidence(state)
    assert build.domain_states == BIGTOP_DOMAIN_STATES


def test_absent_domain_states_stay_absent_keys():
    """Byte-compat with recorded replay fixtures: no domains, no key."""
    assert BuildEvidenceSnapshot().domain_states is None
    assert "domain_states" not in BuildEvidenceSnapshot().model_dump()
    snapshot = RunVerdictSnapshot(run_id="r", finalized_at="t", verdict="unknown")
    assert "domain_states" not in snapshot.model_dump_json()


def test_unrecognized_domain_state_values_are_not_sealed():
    state = _state_with_domain_fact("build.domain_states", {SPARK: {"state": "maybe"}})
    build, _conflicts = _fold_build_evidence(state)
    assert build.domain_states is None


def test_sealed_domain_states_round_trip_through_the_snapshot_json():
    state = _state_with_domain_fact("build.domain_states", BIGTOP_DOMAIN_STATES)
    build, _conflicts = _fold_build_evidence(state)
    snapshot = RunVerdictSnapshot(
        run_id="r", finalized_at="t", verdict="partial", build_evidence=build
    )
    restored = RunVerdictSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.build_evidence.domain_states == BIGTOP_DOMAIN_STATES


def test_test_gate_domain_states_supersede_the_build_snapshot_per_root():
    """Live p5v-bigtop-r2: the gradle domains ran only in the TEST phase, so
    the build gate froze them 'untried' while completed receipts sat on disk;
    the test gate re-read receipts later and knew better. Newest wins per
    root; roots only the build gate saw keep their build state."""
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.agent.verdict_finalizer import _sealed_domain_states

    state = RunEvidenceState(run_id="sealed-domain-order")
    state.register_fact(
        StateScope.ARTIFACTS,
        "build.domain_states",
        {
            "/w/data-generators": {"state": "untried"},
            "/w/test-framework": {"state": "success"},
        },
        "gate://build",
    )
    state.register_fact(
        StateScope.TEST_RUNTIME,
        "test.stats",
        {
            "executed": 50,
            "domain_states": {"/w/data-generators": {"state": "success"}},
        },
        "gate://test",
    )

    sealed = _sealed_domain_states(state)

    assert sealed == {
        "/w/data-generators": {"state": "success"},
        "/w/test-framework": {"state": "success"},
    }
