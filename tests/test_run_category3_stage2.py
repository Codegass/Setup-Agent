"""Unit tests for the Category-3 Stage-2 ablation runner's pure logic.

The runner's SIDE EFFECTS (worktree, docker, live sag runs) are not exercised;
its resume-critical DECISIONS are — specifically the run-order total order,
which must survive an interruption that archived a candidate's r1/r2 run rows
before its s2-decision row was written.

Regression under test (real 2026-07-19 campaign): the counter recovered the
next index only from s2-decision rows, so an interrupted candidate's archived
run rows were invisible; the resumed rep re-used a spent index (18,19 re-used,
20 skipped). The fix persists run_order_index on EVERY run row and recovers the
next free index from ALL archived run pins (max+1), validating uniqueness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_category3_panel import LedgerEntry, append_ledger
from scripts.run_category3_stage2 import (
    RunOrderCounter,
    Stage2Error,
    recover_run_order_index,
)


def _run_row(ledger: Path, key: str, index: int | None) -> None:
    append_ledger(
        ledger,
        LedgerEntry(kind="run", run_key=key, run_order_index=index),
    )


def _decision_row(ledger: Path, mask: str, rep_indexes: list[int | None]) -> None:
    """An s2-decision row shaped like the runner writes (reps carry their own
    run_order_index for readability, but the counter no longer trusts them)."""
    reps = [
        {"repeat": i + 1, "run_key": f"httpcomponents-client-S2-{mask}-r{i+1}",
         "run_order_index": idx}
        for i, idx in enumerate(rep_indexes)
    ]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"kind": "s2-decision", "candidate_mask": mask, "reps": reps,
             "decision": "keep"},
            sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# every run row persists its run_order_index (the resume anchor)
# --------------------------------------------------------------------------
def test_run_ledger_row_persists_run_order_index(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    _run_row(ledger, "httpcomponents-client-S2-11111-r1", 7)
    loaded = json.loads(ledger.read_text().splitlines()[0])
    assert loaded["run_order_index"] == 7


def test_recover_run_order_index_reads_the_archived_run_row(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    _run_row(ledger, "httpcomponents-client-S2-00000-r1", 18)
    _run_row(ledger, "httpcomponents-client-S2-00000-r2", 19)
    from scripts.run_category3_panel import load_ledger

    rows = load_ledger(ledger)
    assert recover_run_order_index(rows, "httpcomponents-client-S2-00000-r1") == 18
    assert recover_run_order_index(rows, "httpcomponents-client-S2-00000-r2") == 19
    assert recover_run_order_index(rows, "nonexistent") is None


# --------------------------------------------------------------------------
# the counter continues past the highest index ANY archived run consumed
# --------------------------------------------------------------------------
def test_counter_starts_at_zero_on_empty_ledger(tmp_path):
    counter = RunOrderCounter(tmp_path / "campaign-ledger.jsonl")
    assert [counter.take() for _ in range(3)] == [0, 1, 2]


def test_counter_resumes_past_the_highest_archived_run_index(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    for i in range(6):
        _run_row(ledger, f"httpcomponents-client-S2-01111-r{i}", i)
    counter = RunOrderCounter(ledger)
    assert counter.take() == 6


def test_counter_ignores_non_run_rows(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    append_ledger(ledger, LedgerEntry(kind="baseline-red", note="{}"))
    _run_row(ledger, "httpcomponents-client-S2-11111-r1", 0)
    _decision_row(ledger, "11111", [0, 1, 2])  # decision-row indexes are noise
    counter = RunOrderCounter(ledger)
    # Only run rows are trusted: the sole run row consumed 0 -> next is 1.
    assert counter.take() == 1


# --------------------------------------------------------------------------
# THE regression: an interrupted candidate (r1/r2 archived, NO decision row)
# must NOT hand its spent indexes back to the resumed r3.
# --------------------------------------------------------------------------
def test_interrupted_candidate_r3_resume_gets_a_fresh_unique_index(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    # A prior candidate fully decided, consuming indexes 15,16,17.
    for offset, rep in enumerate((1, 2, 3)):
        _run_row(ledger, f"httpcomponents-client-S2-00001-r{rep}", 15 + offset)
    _decision_row(ledger, "00001", [15, 16, 17])
    # The NEXT candidate 00000 is interrupted after r1/r2 archived (indexes
    # 18,19 already spent) but BEFORE its decision row was written.
    _run_row(ledger, "httpcomponents-client-S2-00000-r1", 18)
    _run_row(ledger, "httpcomponents-client-S2-00000-r2", 19)

    # Resume: the counter must continue past 19, not re-derive 18 from the last
    # decision row (the pre-fix bug re-handed 18 -> collision 18,19,18).
    counter = RunOrderCounter(ledger)
    r3_index = counter.take()
    assert r3_index == 20

    # And the whole recovered total order is collision-free.
    from scripts.run_category3_panel import load_ledger

    rows = load_ledger(ledger)
    archived = [
        row["run_order_index"] for row in rows if row.get("kind") == "run"
    ]
    all_indexes = archived + [r3_index]
    assert len(all_indexes) == len(set(all_indexes)), all_indexes
    assert sorted(all_indexes) == list(range(15, 21))


# --------------------------------------------------------------------------
# uniqueness / recoverability are validated on resume (fail closed)
# --------------------------------------------------------------------------
def test_counter_rejects_a_duplicate_index_in_archived_runs(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    _run_row(ledger, "httpcomponents-client-S2-00000-r1", 18)
    _run_row(ledger, "httpcomponents-client-S2-00000-r2", 18)  # corrupt duplicate
    with pytest.raises(Stage2Error, match="claimed by both"):
        RunOrderCounter(ledger)


def test_counter_rejects_a_run_row_missing_its_index(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    _run_row(ledger, "httpcomponents-client-S2-00000-r1", None)  # pre-fix row
    with pytest.raises(Stage2Error, match="no run_order_index"):
        RunOrderCounter(ledger)
