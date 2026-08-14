"""Fix A — run-wide receipts, receipt-consulting closes, named unattribution.

Spec: ``docs/superpowers/specs/2026-08-14-gate-truth-and-receipt-scope-design.md``
§1 (task #48).  The forensic gate required by §1.4 was run against the archived
geode session before any of this was written; its finding is recorded in the
implementation commit and summarized here because it moves the target:

* geode's two ``./gradlew test`` dispatches went through the ``bash`` tool,
  which emits no invocation contract and no receipt.  The run's whole ledger
  was two ``compileJava`` receipts with ``report_delta {"changed": [],
  "new": []}``.  So the dropper was NOT phase-scoping of ``records`` and NOT a
  primary-coordinate claims filter — ``_verified_report_claims`` returned
  ``{}`` because the admitted receipts claim nothing.  Both spec §1.4
  candidates are ruled out by the bytes.
* What geode DOES expose is an arming asymmetry: receipt scoping is armed on
  receipt PRESENCE while exclusion is enforced against report CLAIMS, so a run
  whose only receipts are compile receipts publishes a guaranteed zero
  headline over a 10,448-execution corpus.  Arming on claims instead would
  hand the headline to reports no receipt vouches for, which is the exact
  fabrication receipt scoping exists to stop.  §1 item 3 is therefore the
  correct mitigation and these fences hold the headline at zero.

The control-side dropper is separate and real: the close path consulted zero
receipts.  ``required_test_attempt`` returns its analyze requirement before
``terminal_test_receipts`` is ever reached, every receipt predicate in
``attempt_policy`` is candidate-bound (and the state that fires the close is
exactly the state with no candidates), and ``phase_tool`` stamped
``test_execution_receipts: 0`` as a hard-coded literal rather than a count.
"""

from types import SimpleNamespace

import pytest
from test_receipt_scoped_rollup import (
    ReceiptOrchestrator,
    ReceiptWorkspace,
    _bind_primary_coordinate,
    _receipt,
)
from test_test_attempt_policy import (
    AcceptingGate,
    ManifestOrchestrator,
    UnreadableManifestOrchestrator,
    _ready_state,
    _terminal_gradle_result,
)

from sag.agent.attempt_policy import required_test_attempt, run_test_receipts
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.react_engine import ReActEngine
from sag.agent.verdict_finalizer import SnapshotTestCounts, SnapshotTestStats
from sag.agent.verdict_finalizer import test_grain_rates as grain_rates
from sag.tools.base import ToolResult
from sag.tools.phase_tool import PhaseTool

UNATTRIBUTED_CONFLICT = "test_executions_unattributed_to_receipts"


def _record_build_phase_test(
    state: RunEvidenceState,
    *,
    root: str = "/workspace/bigtop/bigtop-data-generators",
    result: ToolResult | None = None,
) -> None:
    """The freemarker shape: the decisive test dispatch ran in the BUILD phase."""
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "gradle",
        result or _terminal_gradle_result(),
        params={"tasks": "test", "working_directory": root},
        source_phase="build",
        source_attempt_id="build-1",
    )


# ---------------------------------------------------------------------------
# §1 item 1 — run-wide receipts feed the report scan
# ---------------------------------------------------------------------------
def test_a_build_phase_receipt_binds_its_reports_during_the_test_phase(bigtop_reports, monkeypatch):
    """A build-phase ``build(action=test)`` receipt counts exactly like a
    test-phase one: the claims input is scoped by run, never by phase."""
    workspace = bigtop_reports
    reports = sorted((workspace.primary_root / "target" / "surefire-reports").glob("*.xml"))
    workspace.write_receipt(_receipt("inv-build-1-0001", workspace.primary_root, new=reports))
    _bind_primary_coordinate(monkeypatch, workspace)
    orchestrator = ReceiptOrchestrator(workspace)
    validator = PhysicalValidator(
        docker_orchestrator=orchestrator,
        project_path=str(workspace.workspace),
    )

    result = validator.parse_test_reports(str(workspace.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 50


# ---------------------------------------------------------------------------
# §1 item 2 — the close path consults run-wide receipts
# ---------------------------------------------------------------------------
def test_run_wide_receipts_are_neither_phase_nor_candidate_scoped():
    state = _ready_state()
    _record_build_phase_test(state)

    receipts = run_test_receipts(state, project_root="/workspace/bigtop")

    assert len(receipts) == 1
    assert receipts[0].source_phase == "build"


def test_a_run_wide_test_receipt_removes_the_forced_analyze_requirement():
    """freemarker: counts sealed from a build-phase receipt while the close
    demanded a survey refresh it could never satisfy."""
    state = _ready_state()
    _record_build_phase_test(state)

    assert (
        required_test_attempt(
            state,
            UnreadableManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_an_unreceipted_run_still_fails_closed_on_an_unresolved_coordinate():
    """The receipt consult widens nothing when no test ever ran."""
    requirement = required_test_attempt(
        _ready_state(),
        UnreadableManifestOrchestrator(),
        phase="test",
        attempt_id="test-1",
    )

    assert requirement is not None
    assert requirement.reason_code == "manifest_unreadable"


@pytest.mark.parametrize(
    "result",
    (
        ToolResult.completed_success(output="rendered but never dispatched"),
        ToolResult.completed_success(
            output="dispatched with no command",
            metadata={"runner_dispatched": True},
        ),
    ),
    ids=("no_dispatch_attestation", "no_command"),
)
def test_an_undispatched_test_is_not_a_run_wide_receipt(result):
    """Intent is not execution: the run-wide predicate demands the same
    dispatch attestation the candidate-bound one does."""
    state = _ready_state()
    _record_build_phase_test(state, result=result)

    assert run_test_receipts(state) == ()
    assert (
        required_test_attempt(
            state,
            UnreadableManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is not None
    )


def test_a_dispatch_outside_the_boundary_is_not_a_run_wide_receipt():
    """An unresolvable coordinate still bounds the consult at the workspace."""
    state = _ready_state()
    _record_build_phase_test(state, root="/tmp/elsewhere")

    assert run_test_receipts(state) == ()


def test_a_run_wide_receipt_makes_the_unavailable_close_unconstructible():
    """Both close sites read ``_unresolved_test_coordinates_after_refresh``;
    with a run-wide test receipt present neither may cap to UNAVAILABLE."""
    state = _ready_state()
    state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "project",
        ToolResult.completed_failure(output="refresh failed", error="manifest unavailable"),
        params={"action": "analyze"},
        source_phase="test",
        source_attempt_id="test-1",
    )
    _record_build_phase_test(state)
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(
        current_phase="test",
        current_attempt_id="test-1",
        is_complete=False,
    )
    engine.run_evidence_state = state
    engine.orchestrator = UnreadableManifestOrchestrator()
    claim = PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS)
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        code="test_execution_observed",
        claim=claim,
    )

    assert engine._unresolved_test_coordinates_after_refresh() is None
    capped = engine._cap_unresolved_test_gate(claim, green)
    assert capped is green
    assert capped.code == "test_execution_observed"


def test_the_sealed_receipt_count_is_counted_not_a_literal_zero():
    """``phase_tool`` sealed ``test_execution_receipts: 0`` as a constant; in
    freemarker a terminal ``action=test`` receipt with 158 report claims
    existed while that zero was written into the record."""
    state = _ready_state()
    # An auxiliary-island receipt: run-wide real, candidate-bound absent, so the
    # requirement still fires and the sealed count must still be honest.
    _record_build_phase_test(state, root="/workspace/bigtop/bigtop-test-framework")
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="test",
            current_attempt_id="test-1",
            is_complete=False,
        ),
        validator=None,
        orchestrator=ManifestOrchestrator(),
        project_name="bigtop",
        gate_fn=AcceptingGate(),
        run_evidence_state=state,
    )

    result = tool.execute(action="done", outcome="failed")

    assert result.error_code == "TEST_ATTEMPT_REQUIRED"
    assert result.facts["test_execution_receipts"] == 1


# ---------------------------------------------------------------------------
# §1 item 3 — unattributed executions are named, never silently zeroed
# ---------------------------------------------------------------------------
def _geode_stats() -> SnapshotTestStats:
    return SnapshotTestStats(
        discovered=9754,
        unique=SnapshotTestCounts(),
        raw=SnapshotTestCounts(),
        receipt_scoped=True,
        auxiliary_test_stats={
            "executed": 10448,
            "passed": 10422,
            "failed": 3,
            "errors": 0,
            "skipped": 23,
        },
    )


def test_unattributed_executions_name_the_volume_in_the_cases_grain():
    grains, conflicts = grain_rates(
        _geode_stats(),
        driven_modules=set(),
        test_modules=set(),
    )
    payload = grains["cases"].payload()

    assert payload["numerator"] == 0
    assert payload["denominator"] == 9754
    assert payload["reason"] == (
        "0/9754 — 10,448 executions visible on disk but bound to no receipt"
    )
    assert UNATTRIBUTED_CONFLICT in conflicts


def test_the_named_conflict_reaches_the_sealed_verdict():
    from sag.agent.verdict_finalizer import VerdictFinalizer, validate_verdict_snapshot_v3

    state = RunEvidenceState(run_id="geode-shape")
    state.register_fact(
        StateScope.TEST_RUNTIME,
        "test.stats",
        {
            "discovered": 9754,
            "unique": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "raw": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "receipt_scoped": True,
            "auxiliary_test_stats": {
                "executed": 10448,
                "passed": 10422,
                "failed": 3,
                "errors": 0,
                "skipped": 23,
            },
        },
        "artifact://test-rollup",
    )
    state.seal(finalized_at="2026-08-14T00:00:00Z", close_reason="test_terminated")
    snapshot = VerdictFinalizer(orchestrator=None)._snapshot_for_state(state)
    rates = snapshot.rates

    assert UNATTRIBUTED_CONFLICT in snapshot.conflicts
    assert "10,448 executions" in rates["test"]["cases"]["reason"]
    # Anti-fabrication: visibility without authority. The excluded volume is
    # named, never promoted into the numerator.
    assert rates["test"]["cases"]["numerator"] == 0
    # The sentence must survive the sealed-snapshot schema, or the run aborts
    # on its own verdict at finalize.
    validate_verdict_snapshot_v3(snapshot.model_dump(mode="json"))


def test_auxiliary_volume_without_a_zero_headline_is_not_a_conflict():
    """Quarantined neighbors are normal whenever the headline was attributed."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT not in conflicts
    assert "reason" not in grains["cases"].payload()


def test_a_zero_headline_without_auxiliary_volume_is_not_a_conflict():
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(),
        raw=SnapshotTestCounts(),
        receipt_scoped=True,
    )

    _, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT not in conflicts


@pytest.fixture
def bigtop_reports(tmp_path):
    workspace = ReceiptWorkspace(tmp_path)
    workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.AlphaTest.xml",
        "org.apache.bigtop.datagen.AlphaTest",
        [f"alpha{i}" for i in range(25)],
    )
    workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.BetaTest.xml",
        "org.apache.bigtop.datagen.BetaTest",
        [f"beta{i}" for i in range(25)],
    )
    return workspace
