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
* What geode DOES expose is an arming asymmetry: receipt scoping was armed on
  receipt PRESENCE while exclusion is enforced against report CLAIMS, so a run
  whose only receipts are compile receipts publishes a guaranteed zero
  headline over a 10,448-execution corpus — while the SAME corpus with no
  ledger at all took the unscoped branch and published all 10,448.  Deleting
  attributed evidence improved the number, which is P4 inverted.  Fixed
  2026-08-14 by removing the arming entirely: the partition is universal, the
  headline is what the claims cover, and the excluded volume is named.  The
  first review of this file argued that arming on claims "would hand the
  headline to reports no receipt vouches for" — it does the opposite, and the
  zero-receipt path was already doing exactly that.

The control-side dropper is separate and real: the close path consulted zero
receipts.  ``required_test_attempt`` returns its analyze requirement before
``terminal_test_receipts`` is ever reached, every receipt predicate in
``attempt_policy`` is candidate-bound (and the state that fires the close is
exactly the state with no candidates), and ``phase_tool`` stamped
``test_execution_receipts: 0`` as a hard-coded literal rather than a count.
"""

import contextlib
from pathlib import Path
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
    """An unresolvable coordinate with no recorded clone root bounds the consult
    at the workspace — the widest bound this predicate may ever take."""
    state = _ready_state()
    _record_build_phase_test(state, root="/tmp/elsewhere")

    assert run_test_receipts(state) == ()


# ---------------------------------------------------------------------------
# Task #52 — the consult is bounded to the project, never to all of /workspace
# ---------------------------------------------------------------------------
SIBLING_ROOT = "/workspace/vendored-sample/bigtop-data-generators"


def _provisioned(state: RunEvidenceState, clone_root: str = "/workspace/bigtop"):
    """Record the clone root exactly as ``_record_gate_facts`` does at provision.

    The provision gate's evidence ref IS the directory it probed for the clone
    (``_inspect_provision``), so the run state already carries the one boundary
    an unresolvable survey cannot supply.
    """
    state.set_fact(
        "provision.workspace_ready",
        True,
        evidence_ref=clone_root,
        source_phase="provision",
        source_attempt_id="provision-1",
    )
    return state


def test_a_sibling_checkout_receipt_does_not_satisfy_the_run_wide_predicate():
    """The hole this closes: with coordinates unresolvable the consult fell back
    to ALL of /workspace, so a terminal test dispatch in a sibling checkout or a
    vendored sample — a directory this run never claims — answered "did THIS
    project's test run?" with yes."""
    state = _provisioned(_ready_state())
    _record_build_phase_test(state, root=SIBLING_ROOT)

    assert run_test_receipts(state) == ()


def test_the_forced_analyze_survives_a_sibling_checkout_receipt():
    """The control consequence: a receipt outside the clone root lifts neither
    the forced survey refresh nor the resolution cap."""
    state = _provisioned(_ready_state())
    _record_build_phase_test(state, root=SIBLING_ROOT)

    requirement = required_test_attempt(
        state,
        UnreadableManifestOrchestrator(),
        phase="test",
        attempt_id="test-1",
    )

    assert requirement is not None
    assert requirement.reason_code == "manifest_unreadable"


def test_the_recorded_clone_root_still_admits_the_projects_own_receipt():
    """Bounding narrows the scope; it never deletes the run's own evidence."""
    state = _provisioned(_ready_state())
    _record_build_phase_test(state)

    assert len(run_test_receipts(state)) == 1


def test_the_scope_names_which_boundary_bounded_the_count():
    """Three bounds, three names. A reader must never have to infer from a path
    whether the count was bounded to the project or spans the whole container.
    """
    from sag.agent.attempt_policy import run_test_receipt_scope

    survey_bound = run_test_receipt_scope(
        _provisioned(_ready_state()),
        project_root="/workspace/bigtop/bigtop-data-generators",
    )
    clone_bound = run_test_receipt_scope(_provisioned(_ready_state()))
    unbound = run_test_receipt_scope(_ready_state())

    assert (survey_bound.name, survey_bound.root) == (
        "project_root",
        "/workspace/bigtop/bigtop-data-generators",
    )
    assert (clone_bound.name, clone_bound.root) == ("clone_root", "/workspace/bigtop")
    assert (unbound.name, unbound.root) == ("workspace_fallback", "/workspace")


def test_a_provision_ref_that_is_not_a_clone_root_is_not_a_boundary():
    """``_record_gate_facts`` falls back to a ``validator:<phase>:<attempt>``
    provenance when a gate carries no evidence refs, and a path outside the
    container workspace is not this run's clone either. Neither may be read as
    a directory to count over: the honest answer is the named fallback."""
    from sag.agent.attempt_policy import run_test_receipt_scope

    for ref in ("validator:provision:provision-1", "/tmp/elsewhere", "/workspace"):
        scope = run_test_receipt_scope(_provisioned(_ready_state(), clone_root=ref))

        assert (scope.name, scope.root) == ("workspace_fallback", "/workspace")


def test_the_provision_gate_records_a_ref_this_reader_accepts():
    """The coupling fence. The boundary is only as good as the ref provision
    writes, and that ref lives in another module: if the provision gate ever
    stops naming the clone directory, this consult silently widens back to the
    whole workspace with nothing to say it did."""
    from sag.agent.attempt_policy import provisioned_clone_root
    from sag.agent.evidence_records import (
        frame_json_record_stream,
        frame_named_json_record_stream,
    )
    from sag.agent.phase_gates import check_phase_claim

    def execute_command(command, **kwargs):
        if "SAG_NAMED_JSON_RECORD_END_V1" in command:
            return {"exit_code": 0, "output": frame_named_json_record_stream([])}
        if "SAG_JSON_RECORD_END_V1" in command:
            return {"exit_code": 0, "output": frame_json_record_stream([])}
        if "test -d" in command:
            return {"exit_code": 0, "output": "exists"}
        return {"exit_code": 0, "output": ""}

    claim = PhaseClaim(phase="provision", claimed_outcome=PhaseOutcome.SUCCESS)
    gate = check_phase_claim(
        "provision",
        claim,
        validator=None,
        orchestrator=SimpleNamespace(
            execute_command=execute_command,
            container_id="run-wide-scope-fixture",
        ),
        project_name="bigtop",
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(current_attempt_id="provision-1")
    engine.run_evidence_state = RunEvidenceState(run_id="provision-ref")

    engine._record_gate_facts("provision", gate)

    assert engine.run_evidence_state.fact_value("provision.workspace_ready") is True
    assert provisioned_clone_root(engine.run_evidence_state) == "/workspace/bigtop"


def test_the_workspace_fallback_seals_the_scope_it_counted_over():
    """The residual widening is disclosed, never silent.

    With neither a survey coordinate nor a recorded clone root the consult still
    counts over /workspace — but the sealed fact says so, so the gate and the
    trajectory can weigh a count taken over a boundary that proves nothing about
    THIS project."""
    state = _ready_state()
    # A manifest with no survey boundary is never live authority: the read
    # fails closed and hands the close no coordinate at all.
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["survey"] = {}
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="test",
            current_attempt_id="test-1",
            is_complete=False,
        ),
        validator=None,
        orchestrator=orchestrator,
        project_name="bigtop",
        gate_fn=AcceptingGate(),
        run_evidence_state=state,
    )

    result = tool.execute(action="done", outcome="failed")

    assert result.error_code == "TEST_ATTEMPT_REQUIRED"
    assert result.facts["run_wide_test_receipts"] == 0
    assert result.facts["run_wide_test_receipt_scope"] == {
        "name": "workspace_fallback",
        "root": "/workspace",
    }


def _boundless_survey():
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["survey"] = {}
    return orchestrator


@pytest.mark.parametrize(
    ("orchestrator_factory", "state_factory", "expected"),
    (
        (ManifestOrchestrator, lambda: _ready_state(), "project_root"),
        (_boundless_survey, lambda: _provisioned(_ready_state()), "clone_root"),
        (_boundless_survey, lambda: _ready_state(), "workspace_fallback"),
    ),
    ids=("survey_coordinate", "provisioned_clone_root", "nothing_narrower"),
)
def test_the_sealed_count_never_travels_without_the_scope_it_counted_over(
    orchestrator_factory, state_factory, expected
):
    """Whichever of the three boundaries the consult took, the count and that
    boundary are sealed together. A bare number is exactly the shape that made
    ``test_execution_receipts: 0`` readable as an answer to a question nobody
    beside it had asked."""
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="test",
            current_attempt_id="test-1",
            is_complete=False,
        ),
        validator=None,
        orchestrator=orchestrator_factory(),
        project_name="bigtop",
        gate_fn=AcceptingGate(),
        run_evidence_state=state_factory(),
    )

    result = tool.execute(action="done", outcome="failed")

    assert result.error_code == "TEST_ATTEMPT_REQUIRED"
    assert result.facts["run_wide_test_receipts"] == 0
    assert result.facts["run_wide_test_receipt_scope"]["name"] == expected


def test_the_sealed_count_excludes_a_sibling_checkouts_receipt():
    """The same bounding on the sealed side: a resolved survey coordinate is the
    boundary the count is taken over, so a receipt from a directory outside the
    project is not reported to the model as this run's run-wide evidence."""
    state = _provisioned(_ready_state())
    _record_build_phase_test(state, root=SIBLING_ROOT)
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
    assert result.facts["run_wide_test_receipts"] == 0
    assert result.facts["run_wide_test_receipt_scope"] == {
        "name": "project_root",
        "root": "/workspace/bigtop",
    }


def test_a_sibling_checkouts_receipt_cannot_lift_the_unavailable_cap():
    """The other half of the widening: both close sites read
    ``_unresolved_test_coordinates_after_refresh``, so an out-of-project receipt
    lifted the UNAVAILABLE cap and let an unresolved coordinate seal green."""
    state = _provisioned(_ready_state())
    state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "project",
        ToolResult.completed_failure(output="refresh failed", error="manifest unavailable"),
        params={"action": "analyze"},
        source_phase="test",
        source_attempt_id="test-1",
    )
    _record_build_phase_test(state, root=SIBLING_ROOT)
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

    resolution = engine._unresolved_test_coordinates_after_refresh()
    capped = engine._cap_unresolved_test_gate(claim, green)

    assert resolution is not None
    assert resolution.status == "manifest_unreadable"
    assert capped.validator_state is ValidatorState.UNAVAILABLE
    assert capped.code == "test_candidate_resolution_unavailable"


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
    existed while that zero was written into the record.

    The name now carries the scope. The counted fact is RUN-WIDE while the
    requirement that produced the rejection is candidate-bound, and one number
    labelled for neither question reads as the answer to the code beside it:
    ``TEST_ATTEMPT_REQUIRED`` ("no terminal test receipt") next to ``1``. Both
    scopes are sealed, each under a name that says which question it answers.
    """
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
    assert result.facts["run_wide_test_receipts"] == 1
    assert result.facts["candidate_bound_test_receipts"] == 0
    assert "test_execution_receipts" not in result.facts


def test_the_closure_survey_is_read_once_per_claim(monkeypatch):
    """One question, one computation.

    `_test_receipt_scope_facts` re-ran `resolve_survey_test_candidates` — a
    manifest read plus several realpath probes — to answer a question
    `required_test_attempt` had just answered for the rejection it is
    describing. Two reads of one survey can also disagree: the requirement and
    the fact beside it would then be graded against different coordinates.
    """
    from sag.agent import attempt_policy

    calls = []
    real = attempt_policy.resolve_survey_test_candidates

    def counted(orchestrator):
        calls.append(orchestrator)
        return real(orchestrator)

    monkeypatch.setattr(attempt_policy, "resolve_survey_test_candidates", counted)
    # The pre-fix second reader imported the symbol into its own namespace, so
    # patching only `attempt_policy` would not have seen it.
    monkeypatch.setattr(
        "sag.tools.phase_tool.resolve_survey_test_candidates", counted, raising=False
    )
    state = _ready_state()
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
    assert len(calls) == 1


def test_a_non_test_phase_claim_spends_no_survey_probe(monkeypatch):
    """The single read is still LAZY: a claim that never asks the test-closure
    question must not pay for the answer."""
    from sag.agent import attempt_policy

    calls = []
    real = attempt_policy.resolve_survey_test_candidates

    def counted(orchestrator):
        calls.append(orchestrator)
        return real(orchestrator)

    monkeypatch.setattr(attempt_policy, "resolve_survey_test_candidates", counted)
    monkeypatch.setattr(
        "sag.tools.phase_tool.resolve_survey_test_candidates", counted, raising=False
    )
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="build",
            current_attempt_id="build-1",
            is_complete=False,
        ),
        validator=None,
        orchestrator=ManifestOrchestrator(),
        project_name="bigtop",
        gate_fn=AcceptingGate(),
        run_evidence_state=_ready_state(),
    )

    tool.execute(action="done", outcome="success")

    assert calls == []


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

    assert payload["band"] == "unavailable"
    assert payload["reason"] == (
        "no receipt-scoped runtime outcomes were accounted; "
        "9,754 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "10,448 executions visible on disk but bound to no receipt"
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
    assert rates["test"]["cases"]["band"] == "unavailable"
    assert "numerator" not in rates["test"]["cases"]
    # The sentence must survive the sealed-snapshot schema, or the run aborts
    # on its own verdict at finalize.
    validate_verdict_snapshot_v3(snapshot.model_dump(mode="json"))


def test_auxiliary_volume_is_disclosed_even_when_the_headline_is_not_zero():
    """A nonzero headline never switches the disclosure off.

    The trigger used to be `headline == 0`, so ONE receipted test deleted both
    the conflict and every trace of the volume standing beside it — a live
    incentive to launder a corpus by running one test through a
    receipt-producing tool. Excluded volume is excluded volume at any headline.
    """
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT in conflicts
    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "4 executions visible on disk but bound to no receipt"
    )


def test_one_receipted_test_cannot_launder_the_geode_corpus():
    """One attributed execution is accounted; excluded volume stays named."""
    stats = SnapshotTestStats(
        discovered=9754,
        unique=SnapshotTestCounts(executed=1, passed=1),
        raw=SnapshotTestCounts(executed=1, passed=1),
        receipt_scoped=True,
        auxiliary_test_stats={
            "executed": 10448,
            "passed": 10422,
            "failed": 3,
            "errors": 0,
            "skipped": 23,
        },
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT in conflicts
    assert grains["cases"].payload()["numerator"] == 1
    assert grains["cases"].payload()["denominator"] == 1
    assert grains["cases"].payload()["reason"] == (
        "9,754 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "10,448 executions visible on disk but bound to no receipt"
    )


def test_the_disclosure_survives_without_a_static_diagnostic_count():
    stats = SnapshotTestStats(
        discovered=None,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT in conflicts
    assert grains["cases"].payload()["numerator"] == 50
    assert grains["cases"].payload()["denominator"] == 50
    assert grains["cases"].payload()["reason"] == (
        "4 executions visible on disk but bound to no receipt"
    )


def test_a_zero_headline_without_auxiliary_volume_is_not_a_conflict():
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(),
        raw=SnapshotTestCounts(),
        receipt_scoped=True,
    )

    _, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT not in conflicts


# ---------------------------------------------------------------------------
# Amendment item 4 — the disclosure is monotone on the REPORT axis too
# ---------------------------------------------------------------------------
def _bigtop_stats(*, auxiliary: bool) -> SnapshotTestStats:
    """bigtop's real partition: 50 attributed cases, 4 reports nobody claimed."""
    return SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        judgment="success",
        receipt_scoped=True,
        auxiliary_test_stats=(
            {"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0}
            if auxiliary
            else None
        ),
    )


def _verdict_for(stats: SnapshotTestStats) -> str:
    from sag.agent.verdict_finalizer import BuildEvidenceSnapshot, _snapshot_verdict

    _, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())
    return _snapshot_verdict(
        BuildEvidenceSnapshot(observed=True, green=True, judgment="success", source="physical"),
        stats,
        conflicts,
    )


def test_deleting_the_unattributed_reports_never_changes_the_verdict_word():
    """The capping version violated P4 from the REPORT side.

    bigtop's shape: headline 50 attributed + 4 unclaimed reports capped the run
    at `partial`; `rm` on those four unclaimed XML files lifted the cap to
    `success`. Evidence PRESENCE worsened the verdict, which is the same
    inversion the arming asymmetry had on the receipt side — and stray XML may
    legitimately pre-date the checkout, so its presence is not the run's doing.

    Non-capping is the only treatment monotone on both axes: the headline band
    already derives from attributed counts alone, so adding or deleting
    unattributed reports moves the DISCLOSURE and never the word.
    """
    with_reports = _bigtop_stats(auxiliary=True)
    without_reports = _bigtop_stats(auxiliary=False)

    _, conflicts = grain_rates(with_reports, driven_modules=set(), test_modules=set())

    # Sealed exactly as before: named, visible, and still out of the numerator.
    assert UNATTRIBUTED_CONFLICT in conflicts
    assert grain_rates(with_reports, driven_modules=set(), test_modules=set())[0][
        "cases"
    ].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "4 executions visible on disk but bound to no receipt"
    )
    # Neither their presence nor their deletion moves the word.
    assert _verdict_for(with_reports) == "success"
    assert _verdict_for(without_reports) == "success"


def test_the_unattributed_conflict_is_adjudicated_not_capping():
    """The kernel-level statement of the same property."""
    from sag.verdict import ADJUDICATED_CONFLICTS, run_verdict

    assert UNATTRIBUTED_CONFLICT in ADJUDICATED_CONFLICTS
    assert run_verdict("success", "success", (UNATTRIBUTED_CONFLICT,)) == "success"
    # It is still an honest-uncertainty conflict for every other reader, and a
    # genuine evidence conflict standing beside it still caps. (The exemplar
    # was `test_report_parse_error` until item 12 showed a report nobody could
    # read can always be deleted; an unreadable RECEIPT cannot be traded away.)
    beside_an_unreadable_receipt = (UNATTRIBUTED_CONFLICT, "test_receipt_unreadable")
    assert run_verdict("success", "success", beside_an_unreadable_receipt) == "partial"


# ---------------------------------------------------------------------------
# Amendment item 5 — the stale third destination is disclosed
# ---------------------------------------------------------------------------
def test_stale_volume_is_named_separately_from_auxiliary_volume():
    """A superseded-sha claim used to drop its volume out of every sentence.

    Three destinations exist (verified / auxiliary / stale) and only two were
    ever spoken aloud, so a rewritten report was indistinguishable from a report
    that never existed.
    """
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
        stale_test_stats={"executed": 6, "passed": 6, "failed": 0, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "4 executions visible on disk but bound to no receipt, 6 under rewritten claims"
    )
    assert UNATTRIBUTED_CONFLICT in conflicts
    # Stale volume is disclosed, never counted.
    assert grains["cases"].payload()["numerator"] == 50


def test_stale_volume_alone_is_still_disclosed():
    """Nothing unattributed, six executions under rewritten claims: the sentence
    exists and the unattributed conflict does not (stale rides
    ``test_reports_stale``, which the rollup already emits)."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        stale_test_stats={"executed": 6, "passed": 5, "failed": 1, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "6 executions under rewritten claims"
    )
    assert UNATTRIBUTED_CONFLICT not in conflicts


def test_the_parser_counts_the_volume_under_rewritten_claims(tmp_path, monkeypatch):
    """The counts the disclosure needs come from the same partition pass."""
    from test_receipt_scoped_rollup import _surefire_xml, _validator, _write

    workspace = ReceiptWorkspace(tmp_path)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    superseded = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.StaleTest.xml",
        "org.apache.bigtop.datagen.StaleTest",
        [f"stale{i}" for i in range(10)],
    )
    payload = _receipt("inv-test-1-0001", workspace.primary_root, new=[kept, superseded])
    _write(
        superseded,
        _surefire_xml("org.apache.bigtop.datagen.StaleTest", [f"stale{i}" for i in range(7)]),
    )
    workspace.write_receipt(payload)
    _bind_primary_coordinate(monkeypatch, workspace)
    validator, _ = _validator(workspace)

    result = validator.parse_test_reports(str(workspace.project))

    assert result["total_tests"] == 25
    assert result["stale_test_reports"] == [str(superseded)]
    # The bytes on disk hold 7 cases; the claim vouches for the 10 that are gone.
    assert result["stale_test_stats"] == {
        "executed": 7,
        "passed": 7,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


# ---------------------------------------------------------------------------
# Amendment item 7 — both excluded doors are graded alike
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _own_evidence_epoch(tmp_path, name: str):
    """One host authority per shape: these are two RUNS, one container each."""
    from sag.agent.control_events import ControlEventSink
    from sag.agent.evidence_publications import (
        EvidencePublicationAuthority,
        install_evidence_publication_authority,
        reset_evidence_publication_authority,
    )

    token = install_evidence_publication_authority(
        EvidencePublicationAuthority.for_live_run(
            run_id="run-pytest",
            sink=ControlEventSink(tmp_path / f"{name}-control-events.jsonl"),
        )
    )
    try:
        yield
    finally:
        reset_evidence_publication_authority(token)


def _rewrite_shape(tmp_path, monkeypatch, *, publish_the_claim: bool):
    """One corpus, one rewrite, the receipt that reveals it present or absent.

    Two primary reports. Receipt A claims TEST-Kept.xml and still matches it, so
    the headline is 25 either way. TEST-Other.xml is written with 10 cases and
    then REWRITTEN to 8 — the shape of a dispatch that went through the
    receipting tool and was then re-run through ``bash``. Whether receipt B
    exists decides only which excluded bucket those 8 executions land in.
    """
    from test_receipt_scoped_rollup import _surefire_xml, _validator, _write

    from sag.agent.phase_gates import _validated_test_rollup

    name = "claimed" if publish_the_claim else "unclaimed"
    workspace = ReceiptWorkspace(tmp_path / name)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    other = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.OtherTest.xml",
        "org.apache.bigtop.datagen.OtherTest",
        [f"other{i}" for i in range(10)],
    )
    workspace.write_receipt(_receipt("inv-test-1-0001", workspace.primary_root, new=[kept]))
    claim_b = _receipt("inv-test-1-0002", workspace.primary_root, new=[other])
    _write(
        other,
        _surefire_xml("org.apache.bigtop.datagen.OtherTest", [f"other{i}" for i in range(8)]),
    )
    if publish_the_claim:
        workspace.write_receipt(claim_b)
    _bind_primary_coordinate(monkeypatch, workspace)
    with _own_evidence_epoch(tmp_path, name):
        validator, _ = _validator(workspace)
        result = validator.parse_test_reports(str(workspace.project))
    return result, _validated_test_rollup(result)


def test_deleting_the_receipt_that_revealed_a_rewrite_never_changes_the_word(tmp_path, monkeypatch):
    """P4, on the receipt axis: the run that produced MORE evidence must not
    seal the worse word.

    The excluded volume leaves the headline through two doors. STALE was
    claimed and the bytes were then rewritten; AUXILIARY is claimed by nobody.
    While stale capped and auxiliary did not, DELETING receipt B lifted an
    identical corpus with an identical headline from ``partial`` to ``success``
    — 'discarding a receipt ... may never make [a verdict] better'
    (2026-07-29 evidence-lifecycle spec, P4).

    Exclusion, not the cap, is the anti-fabrication mechanism, and exclusion is
    unchanged: the 8 rewritten executions are still counted by nobody. A run
    that wants them uncapped only ever had to dispatch through a non-receipting
    tool from the start, so the cap defended nothing and taxed the run that
    used the receipting tool first.
    """
    from sag.verdict import run_verdict

    claimed, claimed_rollup = _rewrite_shape(tmp_path, monkeypatch, publish_the_claim=True)
    unclaimed, unclaimed_rollup = _rewrite_shape(tmp_path, monkeypatch, publish_the_claim=False)

    # Same corpus, same headline, same excluded volume — only the door differs.
    assert claimed["total_tests"] == unclaimed["total_tests"] == 25
    assert claimed["stale_test_stats"]["executed"] == 8
    assert claimed.get("auxiliary_test_stats") is None
    assert unclaimed["auxiliary_test_stats"]["executed"] == 8
    assert unclaimed.get("stale_test_stats") is None
    # Both doors are still named out loud, each by its own conflict.
    assert "test_reports_stale" in claimed_rollup["conflicts"]
    assert "test_reports_stale" not in unclaimed_rollup["conflicts"]

    claimed_verdict = run_verdict("success", "success", claimed_rollup["conflicts"])
    unclaimed_verdict = run_verdict("success", "success", unclaimed_rollup["conflicts"])

    assert claimed_verdict == unclaimed_verdict == "success"


def test_the_stale_conflict_is_adjudicated_not_capping():
    """The kernel-level statement of the same property, beside item 4's.

    ``test_reports_stale`` names volume the headline already excludes, so it has
    no second claim on the verdict to make — the same rationale that made
    ``test_executions_unattributed_to_receipts`` non-capping, applied to the
    other excluded door.
    """
    from sag.verdict import ADJUDICATED_CONFLICTS, run_verdict
    from sag.verdict_rates import EXCLUDED_VOLUME_CONFLICTS, STALE_CONFLICT

    assert EXCLUDED_VOLUME_CONFLICTS == {UNATTRIBUTED_CONFLICT, STALE_CONFLICT}
    assert EXCLUDED_VOLUME_CONFLICTS <= ADJUDICATED_CONFLICTS
    assert run_verdict("success", "success", (STALE_CONFLICT,)) == "success"
    assert run_verdict("success", "success", EXCLUDED_VOLUME_CONFLICTS) == "success"
    # An evidence-CLOSURE failure still caps beside either door. This assertion
    # named `test_report_parse_error` until item 12: a report the harness could
    # not read can be deleted, and deleting it lifted the word, so it grades
    # nothing either. An unreadable RECEIPT takes the whole headline's
    # authority with it and cannot be traded for a better verdict.
    assert (
        run_verdict("success", "success", (STALE_CONFLICT, "test_receipt_unreadable")) == "partial"
    )


# ---------------------------------------------------------------------------
# Amendment item 6 — fallback parity: the unpartitioned corpus is not a headline
# ---------------------------------------------------------------------------
def _sealed_snapshot(rollup: dict, *, run_id: str = "receipt-scope-shape"):
    from sag.agent.verdict_finalizer import VerdictFinalizer

    state = RunEvidenceState(run_id=run_id)
    state.register_fact(StateScope.TEST_RUNTIME, "test.stats", rollup, "artifact://test-rollup")
    state.seal(finalized_at="2026-08-14T00:00:00Z", close_reason="test_terminated")
    return VerdictFinalizer(orchestrator=None)._snapshot_for_state(state)


def _counts(executed: int) -> dict:
    return {
        "executed": executed,
        "passed": executed,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


def test_an_unpartitioned_rollup_is_unattributed_volume_not_a_headline():
    """The shell find/cat rescan partitions nothing, so everything it counted is
    unclaimed by construction. Sealing it as the headline gave the LESS
    machinery the HIGHER number for the same corpus."""
    snapshot = _sealed_snapshot(
        {
            "discovered": 9754,
            "unique": _counts(10448),
            "raw": _counts(10448),
        }
    )

    assert snapshot.test_stats.unique.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats == _counts(10448)
    assert UNATTRIBUTED_CONFLICT in snapshot.conflicts
    assert snapshot.rates["test"]["cases"]["reason"] == (
        "no receipt-scoped runtime outcomes were accounted; "
        "9,754 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "10,448 executions visible on disk but bound to no receipt"
    )
    # `receipt_scoped` stays absent: the routing states the provenance, it does
    # not invent one.
    assert snapshot.test_stats.receipt_scoped is None


def test_both_parsers_seal_the_same_zero_receipt_corpus():
    """Fallback parity. For a run with no receipts the compact parser seals
    headline 0 + auxiliary 10,448; the shell rescan used to seal 10,448 as the
    headline — the two paths disagreed by the entire corpus, and the higher
    number came from the LESS machinery.

    The phase-close refusal is untouched by this routing: an unpartitioned
    rollup still closes nothing (``test_receipt_missing``), fenced by
    ``tests/test_phase_gates.py::
    test_counts_that_did_not_come_from_the_claim_partition_cannot_close_the_phase``.
    """
    partitioned = _sealed_snapshot(
        {
            "discovered": 9754,
            "unique": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "raw": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "receipt_scoped": True,
            "auxiliary_test_stats": _counts(10448),
        },
        run_id="compact-parser-shape",
    )
    unpartitioned = _sealed_snapshot(
        {
            "discovered": 9754,
            "unique": _counts(10448),
            "raw": _counts(10448),
        },
        run_id="shell-fallback-shape",
    )

    assert partitioned.rates["test"]["cases"] == unpartitioned.rates["test"]["cases"]
    assert partitioned.verdict == unpartitioned.verdict
    assert partitioned.test_stats.unique == unpartitioned.test_stats.unique


# ---------------------------------------------------------------------------
# Amendment item 8 — the parse-error CAP follows attribution, not the scan
# ---------------------------------------------------------------------------
CORRUPT_XML = "<testsuite><testcase classname='X' name='y'"


def _corrupt_shape(tmp_path, monkeypatch, *, name: str, keep_the_stray: bool = True):
    """25 attributed cases plus one stray XML that cannot be parsed at all.

    The stray sits under the auxiliary coordinate: nobody claims it, so it
    leaves the headline through the AUXILIARY door and its bytes were never
    vouched for by this run.
    """
    from test_receipt_scoped_rollup import _validator, _write

    from sag.agent.phase_gates import _validated_test_rollup

    workspace = ReceiptWorkspace(tmp_path / name)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    if keep_the_stray:
        _write(
            workspace.auxiliary_root / "target" / "surefire-reports" / "TEST-Truncated.xml",
            CORRUPT_XML,
        )
    workspace.write_receipt(_receipt("inv-test-1-0001", workspace.primary_root, new=[kept]))
    _bind_primary_coordinate(monkeypatch, workspace)
    with _own_evidence_epoch(tmp_path, name):
        validator, _ = _validator(workspace)
        result = validator.parse_test_reports(str(workspace.project))
    return result, _validated_test_rollup(result)


def test_an_unreadable_unclaimed_report_leaves_by_its_own_door(tmp_path, monkeypatch):
    """A stray XML nobody claims is counted where it belongs, never in the
    attributed channel.

    ``excluded_counts`` parsed the auxiliary and stale files through the SAME
    ``parse_report`` that feeds ``parsing_errors``, so an unparseable report
    outside the claim set was reported as a claimed report the run could not
    read (and, until item 12, capped through it). A receipt vouches for no byte
    of that file: it is one more thing the run can see and cannot attribute, and
    it says so at its own door.
    """
    result, rollup = _corrupt_shape(tmp_path, monkeypatch, name="stray")

    assert result["total_tests"] == 25
    assert result["parsing_errors"] == []
    assert "test_report_parse_error" not in rollup["conflicts"]
    # Never counted — and stated as unmeasurable rather than as a measured zero.
    assert result["auxiliary_test_stats"] == {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "unparseable": 1,
    }


def test_deleting_a_corrupt_stray_report_never_changes_the_word(tmp_path, monkeypatch):
    """P4 again, through the parse-error door.

    While an unclaimed unparseable report capped, ``rm`` on that one file
    lifted the same corpus with the same headline from ``partial`` to
    ``success`` — 'removing evidence must never improve a verdict'. Item 12
    holds the same line for the claimed door, where the file can equally be
    deleted; this arm keeps the unclaimed one fenced.
    """
    from sag.verdict import run_verdict

    present, present_rollup = _corrupt_shape(tmp_path, monkeypatch, name="present")
    deleted, deleted_rollup = _corrupt_shape(
        tmp_path, monkeypatch, name="deleted", keep_the_stray=False
    )

    assert present["total_tests"] == deleted["total_tests"] == 25
    assert deleted.get("auxiliary_test_stats") is None
    present_verdict = run_verdict("success", "success", present_rollup["conflicts"])
    deleted_verdict = run_verdict("success", "success", deleted_rollup["conflicts"])

    assert present_verdict == deleted_verdict == "success"


# ---------------------------------------------------------------------------
# Amendment item 12 — an unreadable report is disclosed, never graded
# ---------------------------------------------------------------------------
def _intact_claim_corrupt_shape(
    tmp_path,
    monkeypatch,
    *,
    name: str,
    publish_the_claim: bool = True,
    keep_the_report: bool = True,
):
    """25 attributed cases plus ONE corrupt report a receipt claims by hash.

    The receipt is written AFTER the corrupt bytes, so its claim MATCHES them:
    the file is attributed, not stale. ``publish_the_claim`` decides whether
    that receipt exists at all; ``keep_the_report`` decides whether the corrupt
    file does. Nothing else differs between the arms, and the headline is 25 in
    every one of them.
    """
    from test_receipt_scoped_rollup import _validator, _write

    from sag.agent.phase_gates import _validated_test_rollup

    workspace = ReceiptWorkspace(tmp_path / name)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    broken = _write(
        workspace.primary_root / "target" / "surefire-reports" / "TEST-Truncated.xml",
        CORRUPT_XML,
    )
    workspace.write_receipt(_receipt("inv-test-1-0001", workspace.primary_root, new=[kept]))
    claim = _receipt("inv-test-1-0002", workspace.primary_root, new=[broken])
    if publish_the_claim:
        workspace.write_receipt(claim)
    if not keep_the_report:
        broken.unlink()
    _bind_primary_coordinate(monkeypatch, workspace)
    with _own_evidence_epoch(tmp_path, name):
        validator, _ = _validator(workspace)
        result = validator.parse_test_reports(str(workspace.project))
    return result, _validated_test_rollup(result)


def test_deleting_the_receipt_that_claimed_a_corrupt_report_never_changes_the_word(
    tmp_path, monkeypatch
):
    """P4 on the receipt axis, through the parse-error door.

    Item 8 moved EXCLUDED parse failures out of the capping channel and left
    ATTRIBUTED ones in it, which made the cap follow the receipt: the same
    truncated bytes capped when a receipt claimed them and did not when the
    receipt was deleted. One corpus, headline 25 either way, one file either
    way — ``partial`` with the claim, ``success`` without it. Deleting a
    receipt improved the sealed word, which is the inversion items 4 and 7
    each closed from the other side.
    """
    from sag.verdict import run_verdict

    claimed, claimed_rollup = _intact_claim_corrupt_shape(
        tmp_path, monkeypatch, name="claim-kept"
    )
    unclaimed, unclaimed_rollup = _intact_claim_corrupt_shape(
        tmp_path, monkeypatch, name="claim-deleted", publish_the_claim=False
    )

    assert claimed["total_tests"] == unclaimed["total_tests"] == 25
    # The door the receipt chooses is still NAMED, and still a different door.
    assert claimed["unmeasured_test_stats"] == {"unparseable": 1}
    assert unclaimed["auxiliary_test_stats"]["unparseable"] == 1
    assert unclaimed.get("unmeasured_test_stats") is None

    assert run_verdict("success", "success", claimed_rollup["conflicts"]) == "success"
    assert run_verdict("success", "success", unclaimed_rollup["conflicts"]) == "success"


def test_deleting_the_corrupt_report_a_receipt_claims_never_changes_the_word(
    tmp_path, monkeypatch
):
    """P4 on the REPORT axis, inside the attributed channel.

    A claimed report that is deleted leaves no trace at all — ``content_sha256``
    returns None and the claim attributes nothing — so while an unreadable
    claimed report capped, ``rm`` on that one file lifted the same corpus with
    the same headline from ``partial`` to ``success``. No treatment where an
    unreadable report caps can be monotone here: the file can always be
    deleted. Unreadable reports are therefore disclosed and never graded.
    """
    from sag.verdict import run_verdict

    present, present_rollup = _intact_claim_corrupt_shape(
        tmp_path, monkeypatch, name="report-kept"
    )
    deleted, deleted_rollup = _intact_claim_corrupt_shape(
        tmp_path, monkeypatch, name="report-deleted", keep_the_report=False
    )

    assert present["total_tests"] == deleted["total_tests"] == 25
    assert deleted.get("unmeasured_test_stats") is None
    assert "test_report_parse_error" not in deleted_rollup["conflicts"]

    assert run_verdict("success", "success", present_rollup["conflicts"]) == "success"
    assert run_verdict("success", "success", deleted_rollup["conflicts"]) == "success"


def test_the_unreadable_report_conflict_is_adjudicated_not_capping():
    """The kernel-level statement, beside items 4 and 7.

    ``test_report_parse_error`` says one thing: a report could not be read. A
    report that could not be read has no volume in the headline to defend and
    can always be deleted, so grading it can only pay a run for destroying it.
    The cap stays where deletion cannot buy it: an unreadable RECEIPT takes the
    authority for the whole headline with it.
    """
    from sag.verdict import ADJUDICATED_CONFLICTS, run_verdict
    from sag.verdict_rates import UNCOUNTED_REPORT_CONFLICTS, UNREADABLE_REPORT_CONFLICT

    assert UNREADABLE_REPORT_CONFLICT == "test_report_parse_error"
    assert UNCOUNTED_REPORT_CONFLICTS == {
        UNATTRIBUTED_CONFLICT,
        "test_reports_stale",
        UNREADABLE_REPORT_CONFLICT,
    }
    assert UNCOUNTED_REPORT_CONFLICTS <= ADJUDICATED_CONFLICTS
    assert run_verdict("success", "success", (UNREADABLE_REPORT_CONFLICT,)) == "success"
    assert run_verdict("success", "success", UNCOUNTED_REPORT_CONFLICTS) == "success"
    assert run_verdict("success", "success", ("test_receipt_unreadable",)) == "partial"


def test_the_disclosure_names_the_reports_under_intact_claims():
    """The third door speaks in the same sentence as the other two, in its own
    clause: 'under receipt claims' is a claim that still matches bytes nobody
    could read, which is neither unclaimed nor rewritten."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        unmeasured_test_stats={"unparseable": 2},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "2 unparseable reports under receipt claims"
    )
    assert "test_report_parse_error" in conflicts
    assert UNATTRIBUTED_CONFLICT not in conflicts


def test_all_three_doors_are_named_in_one_sentence():
    """No door borrows another's words, and the excluded-volume clauses that
    existed before this one are unchanged."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={
            "executed": 4,
            "passed": 4,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "unparseable": 1,
        },
        stale_test_stats={
            "executed": 6,
            "passed": 6,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        },
        unmeasured_test_stats={"unparseable": 1},
    )

    grains, _ = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "4 executions visible on disk but bound to no receipt "
        "(1 unparseable report), 6 under rewritten claims, "
        "1 unparseable report under receipt claims"
    )


def test_an_unreadable_claimed_report_is_counted_and_pathed(tmp_path, monkeypatch):
    """The other direction: a receipt claimed those exact bytes and the harness
    cannot read them. That is a THIRD door out of the headline, and it is
    disclosed exactly like the other two — its own paths, its own count, its own
    clause — rather than graded (amendment item 12)."""
    result, rollup = _intact_claim_corrupt_shape(tmp_path, monkeypatch, name="claimed-corrupt")

    assert result["total_tests"] == 25
    assert len(result["parsing_errors"]) == 1
    assert "test_report_parse_error" in rollup["conflicts"]
    # Named, pathed and counted — never a measured zero, never counted into any
    # volume, and never merged into a door that would call it unclaimed.
    assert [Path(item).name for item in result["unmeasured_test_reports"]] == [
        "TEST-Truncated.xml"
    ]
    assert result["unmeasured_test_stats"] == {"unparseable": 1}
    assert rollup["unmeasured_test_stats"] == {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "unparseable": 1,
    }
    assert result.get("auxiliary_test_stats") is None
    assert result.get("stale_test_stats") is None
    # …and it survives the seal, so the sentence a reader sees is the one this
    # corpus produced: real parser, real files, real rollup, real snapshot.
    snapshot = _sealed_snapshot(rollup, run_id="unmeasured-door")
    assert snapshot.test_stats.unmeasured_test_stats["unparseable"] == 1
    assert [Path(item).name for item in snapshot.test_stats.unmeasured_test_reports] == [
        "TEST-Truncated.xml"
    ]
    assert snapshot.rates["test"]["cases"]["reason"] == (
        "1 unparseable report under receipt claims"
    )


def test_an_unreadable_report_under_a_rewritten_claim_is_disclosed_not_capping(
    tmp_path, monkeypatch
):
    """The stale door gets the same treatment as the auxiliary one: the run
    stated a hash, the bytes moved, and what stands there now cannot be read.
    The rewrite is disclosed by ``test_reports_stale`` and its own counts; it
    does not additionally mint the capping parse-error conflict."""
    from test_receipt_scoped_rollup import _validator, _write

    from sag.agent.phase_gates import _validated_test_rollup

    workspace = ReceiptWorkspace(tmp_path)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    superseded = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.StaleTest.xml",
        "org.apache.bigtop.datagen.StaleTest",
        [f"stale{i}" for i in range(10)],
    )
    payload = _receipt("inv-test-1-0001", workspace.primary_root, new=[kept, superseded])
    _write(superseded, CORRUPT_XML)
    workspace.write_receipt(payload)
    _bind_primary_coordinate(monkeypatch, workspace)
    validator, _ = _validator(workspace)

    result = validator.parse_test_reports(str(workspace.project))
    rollup = _validated_test_rollup(result)

    assert result["total_tests"] == 25
    assert result["parsing_errors"] == []
    assert "test_report_parse_error" not in rollup["conflicts"]
    assert "test_reports_stale" in rollup["conflicts"]
    assert result["stale_test_stats"] == {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "unparseable": 1,
    }


def test_the_disclosure_names_the_reports_it_could_not_read():
    """Excluded volume that could not be measured is still SAID. Counting it as
    a silent zero is how a corrupt unclaimed report became invisible."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={
            "executed": 4,
            "passed": 4,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "unparseable": 1,
        },
        stale_test_stats={
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "unparseable": 2,
        },
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "4 executions visible on disk but bound to no receipt "
        "(1 unparseable report), 2 unparseable reports under rewritten claims"
    )
    assert UNATTRIBUTED_CONFLICT in conflicts


def test_an_unmeasurable_excluded_volume_still_names_itself():
    """Zero measurable executions plus one unreadable file is not 'nothing
    excluded': the disclosure and its conflict both survive."""
    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "unparseable": 1,
        },
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert grains["cases"].payload()["reason"] == (
        "100 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "1 unparseable report visible on disk but bound to no receipt"
    )
    assert UNATTRIBUTED_CONFLICT in conflicts


# ---------------------------------------------------------------------------
# Amendment item 9 — the observation fold is unattributed volume too
# ---------------------------------------------------------------------------
def _console_state(run_id: str = "console-fold"):
    """A run whose ONLY test evidence is a tool result's parsed console text."""
    from sag.agent.evidence_state import EvidenceRole
    from sag.evidence import OperationOutcome, TestStats

    state = RunEvidenceState(run_id=run_id)
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(output="build complete", facts={"build_success": True}),
        roles=[EvidenceRole.BUILD],
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        ToolResult.completed(
            output="Tests run: 120, Failures: 2",
            operation_outcome=OperationOutcome.FAILED,
            test_stats=TestStats(
                discovered=200,
                executed=120,
                passed=118,
                failed=2,
                skipped=0,
                flaky_count=3,
            ),
        ),
        roles=[EvidenceRole.TEST],
    )
    return state


def test_console_derived_counts_are_unattributed_volume_not_a_headline():
    """The observation fold ran when NO ``test.stats`` fact existed at all, and
    sealed console-derived counts as the full headline — no partition, no
    conflict, less machinery and a bigger number than either parser. It is the
    same inversion fallback parity closed, arriving through the render layer
    this repo's principles forbid as an authority."""
    from sag.agent.verdict_finalizer import _fold_test_stats

    state = _console_state()
    stats, _conflicts = _fold_test_stats(state)

    assert stats.unique == SnapshotTestCounts()
    assert stats.raw == SnapshotTestCounts()
    assert stats.judgment == "unknown"
    # Moved, never deleted — and its provenance travels with it.
    assert stats.auxiliary_test_stats == {
        "executed": 120,
        "passed": 118,
        "failed": 2,
        "errors": 0,
        "skipped": 0,
    }
    assert stats.unattributed_source == "tool_observations"
    # Derived facts do not outlive the counts they came from.
    assert stats.flaky_count == 0
    # The routing states the provenance; it never invents a partition.
    assert stats.receipt_scoped is None


def test_the_console_disclosure_names_where_the_number_came_from():
    from sag.agent.verdict_finalizer import VerdictFinalizer

    state = _console_state("console-fold-sealed")
    state.seal(finalized_at="2026-08-14T00:00:00Z", close_reason="test_terminated")
    snapshot = VerdictFinalizer(orchestrator=None)._snapshot_for_state(state)

    assert UNATTRIBUTED_CONFLICT in snapshot.conflicts
    assert snapshot.rates["test"]["cases"]["reason"] == (
        "no receipt-scoped runtime outcomes were accounted; "
        "200 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "120 executions reported in tool output but bound to no receipt"
    )
    assert snapshot.rates["test"]["cases"]["band"] == "unavailable"


# ---------------------------------------------------------------------------
# Amendment item 10 — a zeroed count takes its derived facts with it
# ---------------------------------------------------------------------------
def test_fallback_parity_drops_the_facts_its_counts_carried():
    """The unpartitioned rollup's counts move to unattributed volume, so the
    conflicts DERIVED from those counts (and the flaky tally computed over
    them) must move with them. A sealed ``test_failures_detected`` over a
    headline of 0/0/0 states a red the snapshot no longer carries."""
    snapshot = _sealed_snapshot(
        {
            "discovered": 100,
            "unique": {"executed": 50, "passed": 40, "failed": 8, "errors": 2, "skipped": 0},
            "raw": {"executed": 50, "passed": 40, "failed": 8, "errors": 2, "skipped": 0},
            "flaky_count": 4,
            "conflicts": ["test_failures_detected", "test_errors_detected", "metrics_conflict"],
        },
        run_id="fallback-derived-facts",
    )

    assert snapshot.test_stats.unique == SnapshotTestCounts()
    assert snapshot.test_stats.flaky_count == 0
    assert "test_failures_detected" not in snapshot.conflicts
    assert "test_errors_detected" not in snapshot.conflicts
    # A conflict that did NOT come from those counts is untouched.
    assert "metrics_conflict" in snapshot.conflicts
    assert snapshot.test_stats.auxiliary_test_stats == {
        "executed": 50,
        "passed": 40,
        "failed": 8,
        "errors": 2,
        "skipped": 0,
    }


# ---------------------------------------------------------------------------
# Amendment item 11 — one close, one survey read
# ---------------------------------------------------------------------------
def _counted_survey(monkeypatch):
    from sag.agent import attempt_policy

    calls = []
    real = attempt_policy.resolve_survey_test_candidates

    def counted(orchestrator):
        calls.append(orchestrator)
        return real(orchestrator)

    monkeypatch.setattr(attempt_policy, "resolve_survey_test_candidates", counted)
    monkeypatch.setattr(
        "sag.agent.react_engine.resolve_survey_test_candidates", counted, raising=False
    )
    return calls


def _capping_engine(phase: str = "test"):
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
        current_phase=phase,
        current_attempt_id="test-1",
        is_complete=False,
    )
    engine.run_evidence_state = state
    engine.orchestrator = UnreadableManifestOrchestrator()
    return engine


def test_one_engine_close_asks_the_survey_once(monkeypatch):
    """P3: one question, one computation.

    ``_cap_unresolved_test_gate`` asked ``resolve_survey_test_candidates``
    twice for one grading — once through the unresolved-coordinate probe and
    once through the forced-refusal probe — and ``_missing_required_test_attempt``
    asked a third time in the same close. Three reads of one survey can
    disagree, and then the cap, the refusals and the requirement answer
    different coordinates while the record shows one close.
    """
    calls = _counted_survey(monkeypatch)
    engine = _capping_engine()
    claim = PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS)
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        code="test_execution_observed",
        claim=claim,
    )
    survey = engine._test_candidate_survey()

    engine._cap_unresolved_test_gate(claim, green, survey=survey)
    engine._missing_required_test_attempt(survey=survey)

    assert len(calls) == 1


def test_a_close_that_never_asks_still_spends_no_probe(monkeypatch):
    """The shared read stays LAZY: threading one answer through the close must
    not make a non-test grading pay for an answer it never reads."""
    calls = _counted_survey(monkeypatch)
    engine = _capping_engine(phase="build")
    claim = PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.SUCCESS)
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        code="build_success",
        claim=claim,
    )

    engine._cap_unresolved_test_gate(claim, green, survey=engine._test_candidate_survey())

    assert calls == []


def test_the_sealed_gate_states_the_survey_the_close_was_graded_against(monkeypatch):
    """The tail of "one question, one computation" (task #38 item 7).

    `_emit_control_gate` read `resolve_survey_test_candidates` for itself, so
    the `test_candidate_resolution` a test-phase `gate_decision` seals came
    from a SECOND read of the survey the cap and the requirement had already
    answered from. The record then shows one close whose grading and whose
    sealed coordinates could disagree — the same fact/word split the shared
    reader exists to prevent.
    """
    calls = _counted_survey(monkeypatch)
    engine = _capping_engine()
    claim = PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS)
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        code="test_execution_observed",
        claim=claim,
    )
    survey = engine._test_candidate_survey()

    engine._cap_unresolved_test_gate(claim, green, survey=survey)
    engine._missing_required_test_attempt(survey=survey)
    engine._emit_control_gate(claim, green, survey=survey)

    assert len(calls) == 1


def test_a_seal_handed_no_survey_still_answers_the_question_itself(monkeypatch):
    """Threading is not a precondition: a caller that never opened a close
    survey (the report-reserve close) still seals the coordinates, from its own
    single lazy read."""
    calls = _counted_survey(monkeypatch)
    engine = _capping_engine()
    claim = PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS)
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        code="test_execution_observed",
        claim=claim,
    )

    engine._emit_control_gate(claim, green)

    assert len(calls) == 1


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
