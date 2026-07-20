"""Unit tests for the Category-3 panel runner's pure orchestration logic.

The runner's SIDE EFFECTS (worktree, docker, live sag runs) are not exercised
here; its DECISIONS are — the panel run plan (interleave + repeats), the
calibration floor formula, ledger idempotency/resumability, and archival
checksum records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_category3_panel import (
    LedgerEntry,
    RunnerError,
    append_ledger,
    archive_session,
    calibration_floor,
    calibration_run_plan,
    campaign_run_order,
    classify_suite_failures,
    filtered_run_order,
    ledger_has,
    load_ledger,
    panel_run_plan,
    register_suite_baseline,
    run_key,
    sha256_file,
)


# --------------------------------------------------------------------------
# run plan: 24 runs, interleaved P,F per probe, repeats 1..3
# --------------------------------------------------------------------------
def test_panel_run_plan_is_24_interleaved_runs():
    plan = panel_run_plan()
    assert len(plan) == 24
    # 4 probes x 2 stages x 3 repeats
    probes = {p for p, _s, _r in plan}
    assert probes == {"bigtop", "tvm", "pyyaml", "httpcomponents-client"}
    for probe in probes:
        rows = [(s, r) for p, s, r in plan if p == probe]
        # interleaved P,F,P,F,P,F
        assert rows == [("P", 1), ("F", 1), ("P", 2), ("F", 2), ("P", 3), ("F", 3)], probe


def test_panel_run_plan_groups_by_probe():
    plan = panel_run_plan()
    # all of one probe's runs are contiguous
    order = [p for p, _s, _r in plan]
    first_index = {p: order.index(p) for p in set(order)}
    for probe, start in first_index.items():
        block = order[start : start + 6]
        assert set(block) == {probe}


def test_campaign_run_order_is_sequential_across_the_whole_plan():
    order = campaign_run_order()
    combined = [*calibration_run_plan(), *panel_run_plan()]
    # Every run keyed once, index sequential 0..N-1 in plan order (calibration
    # first, then the interleaved panel) — the total order the agent stamps.
    assert len(order) == len(combined)
    assert sorted(order.values()) == list(range(len(combined)))
    for index, (probe, stage, repeat) in enumerate(combined):
        assert order[run_key(probe, stage, repeat)] == index
    # Calibration runs precede every panel run.
    cal_keys = [run_key(p, s, r) for p, s, r in calibration_run_plan()]
    panel_keys = [run_key(p, s, r) for p, s, r in panel_run_plan()]
    assert max(order[k] for k in cal_keys) < min(order[k] for k in panel_keys)


def test_campaign_run_order_is_stable_and_resumable():
    # Deterministic: re-deriving the order gives identical indices regardless of
    # which runs the ledger already recorded (resume must not renumber).
    assert campaign_run_order() == campaign_run_order()


def test_filtered_run_order_numbers_a_subset_plan_0_to_n_minus_1():
    """Reviewer P2: under --only-probes the effective plan is a subset of the
    canonical 27-slot campaign, and its run-order index must run 0..N-1 over that
    subset — not carry the sparse full-plan slot numbers."""
    # The confirm2 campaign's effective plan: no pyyaml calibration, panel
    # restricted to tvm + httpcomponents-client (interleaved P,F, repeats 1..3).
    panel = [
        item
        for item in panel_run_plan()
        if item[0] in {"tvm", "httpcomponents-client"}
    ]
    order = filtered_run_order(panel)
    assert len(order) == len(panel) == 12
    # Contiguous 0..N-1, one index per key, in plan order.
    assert sorted(order.values()) == list(range(len(panel)))
    for index, (probe, stage, repeat) in enumerate(panel):
        assert order[run_key(probe, stage, repeat)] == index
    # The tvm runs occupy the FILTERED low slots (0,1,2,...), NOT the full-plan
    # slots (8,10,... which is the exact defect that crashed the confirm run).
    assert order["tvm-P-r1"] == 0
    assert order["tvm-F-r1"] == 1
    assert order["tvm-P-r1"] < order["tvm-F-r1"]


def test_filtered_run_order_over_full_plan_matches_campaign_run_order():
    """The canonical helper is just filtered_run_order over the whole plan, so a
    full campaign still numbers 0..N-1 across calibration + panel."""
    full = [*calibration_run_plan(), *panel_run_plan()]
    assert filtered_run_order(full) == campaign_run_order()
    assert sorted(campaign_run_order().values()) == list(range(len(full)))


# --------------------------------------------------------------------------
# suite baseline reds (panel precondition, analyzer-diet.md:536-555)
# --------------------------------------------------------------------------
def test_baseline_classifier_accepts_exactly_the_six_registered_reds():
    failed = [
        "tests/test_evidence_ingestion.py::test_evidence_ingestion",
        "tests/test_stage1_review_fixes.py::test_stage1_review_fixes_a",
        "tests/test_stage1_review_fixes.py::test_stage1_review_fixes_b",
        "tests/test_lineage_idempotence_followup.py::test_lineage_idempotence_followup_a",
        "tests/test_lineage_idempotence_followup.py::test_lineage_idempotence_followup_b",
        "tests/test_packaging_smoke.py::test_packaging_smoke",
    ]
    registered, new = classify_suite_failures(failed)
    assert not new
    assert len(registered) == 6


def test_baseline_classifier_flags_a_new_regression():
    failed = [
        "tests/test_evidence_ingestion.py::test_evidence_ingestion",
        "tests/test_something_else.py::test_new_regression",
    ]
    registered, new = classify_suite_failures(failed)
    assert new == ["tests/test_something_else.py::test_new_regression"]


def test_baseline_classifier_flags_count_overflow_of_an_accepted_red():
    # a THIRD test_stage1_review_fixes failure (max_count 2) is a new spread
    failed = [
        "x::test_stage1_review_fixes_a",
        "x::test_stage1_review_fixes_b",
        "x::test_stage1_review_fixes_c",
    ]
    registered, new = classify_suite_failures(failed)
    assert len(registered) == 2
    assert new == ["x::test_stage1_review_fixes_c"]


def test_register_suite_baseline_blocks_on_new_failure(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    with pytest.raises(RunnerError):
        register_suite_baseline(
            worktree=tmp_path,
            ledger_path=ledger,
            failed_node_ids=["x::test_brand_new_failure"],
        )
    # nothing registered when blocked
    assert load_ledger(ledger) == []


def test_register_suite_baseline_records_reds_and_is_idempotent(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    reds = [
        "x::test_evidence_ingestion",
        "x::test_stage1_review_fixes_a",
        "x::test_stage1_review_fixes_b",
        "x::test_lineage_idempotence_followup_a",
        "x::test_lineage_idempotence_followup_b",
        "x::test_packaging_smoke",
    ]
    register_suite_baseline(worktree=tmp_path, ledger_path=ledger, failed_node_ids=reds)
    rows = [r for r in load_ledger(ledger) if r["kind"] == "baseline-red"]
    assert len(rows) == 1
    # idempotent: a second call adds no new baseline-red row
    register_suite_baseline(worktree=tmp_path, ledger_path=ledger, failed_node_ids=reds)
    rows = [r for r in load_ledger(ledger) if r["kind"] == "baseline-red"]
    assert len(rows) == 1


# --------------------------------------------------------------------------
# calibration floor formula
# --------------------------------------------------------------------------
def test_calibration_floor_is_floor_of_0_8_times_min():
    assert calibration_floor([100, 120, 150]) == 80  # floor(0.8 * 100)


def test_calibration_floor_never_below_one():
    # floor(0.8 * 1) == 0 -> guarded up to 1
    assert calibration_floor([1, 5, 9]) == 1


def test_calibration_floor_requires_three_values():
    with pytest.raises(ValueError):
        calibration_floor([100, 120])


def test_calibration_floor_rejects_zero_or_negative():
    with pytest.raises(ValueError):
        calibration_floor([0, 100, 120])


# --------------------------------------------------------------------------
# ledger: append + idempotent resume
# --------------------------------------------------------------------------
def test_run_key_is_stable():
    assert run_key("bigtop", "P", 2) == "bigtop-P-r2"


def test_ledger_roundtrip_and_membership(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    entry = LedgerEntry(
        kind="run",
        run_key="bigtop-P-r1",
        probe="bigtop",
        stage="P",
        repeat=1,
        run_id="run-abc",
        artifact_dir="bigtop-P-r1",
        checksums={"verdict.json": "a" * 64},
    )
    append_ledger(ledger, entry)
    loaded = load_ledger(ledger)
    assert len(loaded) == 1
    assert loaded[0]["run_key"] == "bigtop-P-r1"
    assert ledger_has(loaded, "bigtop-P-r1")
    assert not ledger_has(loaded, "bigtop-F-r1")


def test_ledger_membership_only_counts_completed_runs(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    # a floor/calibration entry is not a run and must not mark a run done
    append_ledger(ledger, LedgerEntry(kind="floor", run_key=None, note="pyyaml floor=80"))
    loaded = load_ledger(ledger)
    assert not ledger_has(loaded, "pyyaml-P-r1")


def test_ledger_append_is_additive(tmp_path):
    ledger = tmp_path / "campaign-ledger.jsonl"
    append_ledger(ledger, LedgerEntry(kind="run", run_key="a-P-r1"))
    append_ledger(ledger, LedgerEntry(kind="run", run_key="b-F-r2"))
    loaded = load_ledger(ledger)
    assert [e["run_key"] for e in loaded] == ["a-P-r1", "b-F-r2"]


def test_load_ledger_absent_file_is_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------
# checksums
# --------------------------------------------------------------------------
def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    f = tmp_path / "x.json"
    f.write_bytes(b'{"verdict":"success"}')
    assert sha256_file(f) == hashlib.sha256(b'{"verdict":"success"}').hexdigest()


def test_calibration_run_plan_is_three_excluded_arm_p_runs():
    from scripts.run_category3_panel import calibration_run_plan

    plan = calibration_run_plan()
    assert plan == [("pyyaml", "cal", 1), ("pyyaml", "cal", 2), ("pyyaml", "cal", 3)]
    # 'cal' stage is not a panel stage -> excluded from the 24-run panel
    from scripts.run_category3_panel import panel_run_plan

    assert not any(s == "cal" for _p, s, _r in panel_run_plan())


def test_floor_ledger_row_carries_numeric_floor(tmp_path):
    from scripts.run_category3_panel import LedgerEntry, append_ledger, load_ledger

    ledger = tmp_path / "campaign-ledger.jsonl"
    append_ledger(ledger, LedgerEntry(kind="floor", probe="pyyaml", floor=80, note="x"))
    rows = load_ledger(ledger)
    assert rows[0]["floor"] == 80
    assert rows[0]["kind"] == "floor"


def test_archive_session_preserves_layout_and_hashes(tmp_path):
    session = tmp_path / "session"
    setup = session / ".setup_agent"
    setup.mkdir(parents=True)
    (setup / "verdict.json").write_text('{"verdict":"success"}', encoding="utf-8")
    (setup / "control_events.jsonl").write_text("", encoding="utf-8")
    # session-root probe log
    (session / "agent_execution.log").write_text("trace", encoding="utf-8")
    dest = tmp_path / "out"
    checksums = archive_session(session, dest)
    # layout preserved: .setup_agent/ vs session root
    assert (dest / ".setup_agent" / "verdict.json").is_file()
    assert (dest / "agent_execution.log").is_file()
    assert ".setup_agent/verdict.json" in checksums
    assert "agent_execution.log" in checksums
    assert len(checksums[".setup_agent/verdict.json"]) == 64


def test_archive_session_recurses_directories_and_globs(tmp_path):
    # round-review P1-4: raw JUnit reports (a directory) and command_*.log
    # (a glob) are archived with per-file checksums.
    session = tmp_path / "session"
    setup = session / ".setup_agent"
    reports = setup / "pytest-reports"
    reports.mkdir(parents=True)
    (reports / "pytest-attempt-000001.xml").write_text("<testsuite/>", encoding="utf-8")
    (setup / "verdict.json").write_text("{}", encoding="utf-8")
    (session / "command_project_tvm.log").write_text("cmd", encoding="utf-8")
    dest = tmp_path / "out"
    checksums = archive_session(session, dest)
    assert (dest / ".setup_agent" / "pytest-reports" / "pytest-attempt-000001.xml").is_file()
    assert ".setup_agent/pytest-reports/pytest-attempt-000001.xml" in checksums
    assert (dest / "command_project_tvm.log").is_file()
    assert "command_project_tvm.log" in checksums


def test_archive_session_with_no_sealed_files_raises(tmp_path):
    session = tmp_path / "empty"
    (session / ".setup_agent").mkdir(parents=True)
    with pytest.raises(RunnerError):
        archive_session(session, tmp_path / "out")
