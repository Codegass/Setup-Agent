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
