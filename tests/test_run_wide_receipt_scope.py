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
        "50/100 — 4 executions visible on disk but bound to no receipt"
    )


def test_one_receipted_test_cannot_launder_the_geode_corpus():
    """geode with a single attributed execution: 1/9754 stays 1/9754, and the
    10,448 unattributed executions are still named next to it."""
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
    assert grains["cases"].payload()["reason"] == (
        "1/9754 — 10,448 executions visible on disk but bound to no receipt"
    )


def test_the_disclosure_names_both_numbers_without_a_denominator():
    """No discovered count is no excuse to state only one of the two volumes."""
    stats = SnapshotTestStats(
        discovered=None,
        unique=SnapshotTestCounts(executed=50, passed=50),
        raw=SnapshotTestCounts(executed=50, passed=50),
        receipt_scoped=True,
        auxiliary_test_stats={"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    )

    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())

    assert UNATTRIBUTED_CONFLICT in conflicts
    assert grains["cases"].payload()["reason"] == (
        "50 executed, static discovery found no count — "
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
    ].payload()["reason"] == ("50/100 — 4 executions visible on disk but bound to no receipt")
    # Neither their presence nor their deletion moves the word.
    assert _verdict_for(with_reports) == "success"
    assert _verdict_for(without_reports) == "success"


def test_the_unattributed_conflict_is_adjudicated_not_capping():
    """The kernel-level statement of the same property."""
    from sag.verdict import ADJUDICATED_CONFLICTS, run_verdict

    assert UNATTRIBUTED_CONFLICT in ADJUDICATED_CONFLICTS
    assert run_verdict("success", "success", (UNATTRIBUTED_CONFLICT,)) == "success"
    # It is still an honest-uncertainty conflict for every other reader, and a
    # genuine evidence conflict standing beside it still caps.
    beside_a_parse_error = (UNATTRIBUTED_CONFLICT, "test_report_parse_error")
    assert run_verdict("success", "success", beside_a_parse_error) == "partial"


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
        "50/100 — 4 executions visible on disk but bound to no receipt, 6 under rewritten claims"
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

    assert grains["cases"].payload()["reason"] == "50/100 — 6 executions under rewritten claims"
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
    # Uncertainty about evidence the harness could not READ still caps, beside
    # either door: excluded volume is measured, a parse error is not.
    assert (
        run_verdict("success", "success", (STALE_CONFLICT, "test_report_parse_error")) == "partial"
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
        "0/9754 — 10,448 executions visible on disk but bound to no receipt"
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
