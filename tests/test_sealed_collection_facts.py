# tests/test_sealed_collection_facts.py
"""Reviewer integration fix for Plan 4 Stage A: the collection facts computed
by the physical validator (Task 2) must survive INTO the sealed verdict.
`SnapshotTestStats` is frozen/extra-forbid, so without declared fields the
gate rollup's collection keys are dropped at sealing — the exact projection
failure the 2026-07-26 audit diagnosed, one layer deeper."""

from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.phase_gates import _validated_test_rollup
from sag.agent.verdict_finalizer import SnapshotTestStats, _fold_test_stats

TVM_STATUS = {
    "test_stats": {
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "collection_errors": 28,
        "collection_errors_skipped": 28,
        "collection_error_summary": (
            "RuntimeError: None of the following targets are supported "
            "by this build of TVM: ['llvm', ...]"
        ),
    },
    "total_tests": 0,
    "unique_tests": 0,
    "error_tests": 0,
    "has_test_reports": True,
    "collection_errors": 28,
    "collection_errors_skipped": 28,
    "collection_error_summary": (
        "RuntimeError: None of the following targets are supported "
        "by this build of TVM: ['llvm', ...]"
    ),
}


def test_gate_rollup_carries_collection_facts():
    rollup = _validated_test_rollup(TVM_STATUS)
    assert rollup is not None
    assert rollup["collection_errors"] == 28
    assert rollup["collection_errors_skipped"] == 28
    assert rollup["collection_error_summary"].startswith("RuntimeError: None of the following")


def test_snapshot_test_stats_declares_collection_fields():
    stats = SnapshotTestStats.model_validate(
        {
            "collection_errors": 28,
            "collection_errors_skipped": 28,
            "collection_error_summary": "RuntimeError: boom",
        }
    )
    assert stats.collection_errors == 28
    assert stats.collection_errors_skipped == 28
    assert stats.collection_error_summary == "RuntimeError: boom"
    dumped = stats.model_dump()
    assert dumped["collection_errors"] == 28


def test_fold_threads_collection_facts_from_the_validated_rollup():
    state = RunEvidenceState(run_id="sealed-collection")
    rollup = _validated_test_rollup(TVM_STATUS)
    state.register_fact(StateScope.TEST_RUNTIME, "test.stats", rollup, "gate://test")

    stats, _conflicts = _fold_test_stats(state, test_pass_threshold=0.8)

    assert stats.collection_errors == 28
    assert stats.collection_errors_skipped == 28
    assert stats.collection_error_summary.startswith("RuntimeError:")


def test_absent_collection_facts_stay_none():
    stats = SnapshotTestStats()
    assert stats.collection_errors is None
    assert stats.collection_errors_skipped is None
    assert stats.collection_error_summary is None

# ---------------------------------------------------------------------------
# Plan 5 Task B2 follow-up: the receipt-scoped basis, the quarantined
# auxiliary counts, and the named stale reports must survive INTO the sealed
# verdict — and a receipt-free run must serialize byte-identically to before.

BIGTOP_SCOPED_STATUS = {
    "test_stats": {"executed": 50, "passed": 50, "failed": 0, "errors": 0, "skipped": 0},
    "total_tests": 50,
    "unique_tests": 50,
    "error_tests": 0,
    "has_test_reports": True,
    "receipt_scoped": True,
    "auxiliary_test_stats": {"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    "stale_test_reports": ["/workspace/bigtop/old/TEST-Old.xml"],
}


def test_fold_seals_receipt_scoped_basis_and_auxiliary_quarantine():
    state = RunEvidenceState(run_id="sealed-receipt-scope")
    rollup = _validated_test_rollup(BIGTOP_SCOPED_STATUS)
    state.register_fact(StateScope.TEST_RUNTIME, "test.stats", rollup, "gate://test")

    stats, _conflicts = _fold_test_stats(state, test_pass_threshold=0.8)

    assert stats.receipt_scoped is True
    assert stats.auxiliary_test_stats == {
        "executed": 4,
        "passed": 4,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert stats.stale_test_reports == ["/workspace/bigtop/old/TEST-Old.xml"]
    assert stats.unique.passed == 50

    dumped = stats.model_dump()
    assert dumped["receipt_scoped"] is True
    assert dumped["auxiliary_test_stats"]["passed"] == 4


def test_receipt_free_snapshot_serializes_without_the_new_keys():
    dumped = SnapshotTestStats().model_dump()
    for key in ("receipt_scoped", "auxiliary_test_stats", "stale_test_reports"):
        assert key not in dumped
