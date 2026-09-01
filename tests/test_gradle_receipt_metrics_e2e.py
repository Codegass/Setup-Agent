# tests/test_gradle_receipt_metrics_e2e.py
"""Plan r2 T5 — the 2026-08-26 kafka shape, at the surface that reported it.

The blocker this closes, in the owner's words: the r2 totals have writers,
validators and tests, and ZERO consumers. `gradle_suite_summaries` states what
every claimed report declared and nothing downstream read it, so live metrics
kept the shape the measured kafka session left behind —

    "receipt_executions": {"executed": null, ..., "reason":
        "current receipt testcase rows were unavailable"}

— for a run that had put 1,176 reports and 27,219 executions on disk.

The receipts here are built by the T1/T2 paths, not by hand: the reactor
harness in `test_gradle_receipt_rows_e2e` drives the real GradleTool over real
XML at the real `/workspace` paths, and `assemble_report_metrics` is the
production article. What is pinned is the CONTRACT the surface now states:

- the count is the RUN's, from the totals tier — never the bounded identity
  sample, which holds 2,048 of those 27,219 executions (P-A);
- every grain says which population it counted, so a sample can never be read
  as a total by juxtaposition (MS-1 C11);
- the counts survive the identity tier's absence entirely, which is the
  measured session's own shape.
"""

import json

import pytest
from test_gradle_receipt_rows_e2e import (
    CLIENTS_LAYOUT,
    GREEN_SUITE_TESTS,
    KAFKA_REDS,
    KAFKA_SESSION_REASON,
    KAFKA_SKIPPED,
    KAFKA_TESTS,
    RED_SUITE_FAILURES,
    RED_SUITE_TESTS,
    _kafka_scale_layout,
    _maven_twin,
    _metrics,
    _run_reactor,
)

from sag.agent.invocation_receipts import validate_receipt_v2
from sag.agent.receipt_test_rows import testcase_execution_id as execution_id_of
from sag.tools.report_metrics import (
    assemble_report_metrics,
    format_evidence_layer_lines,
    validate_report_metrics_v2,
)

# What the measured session ran, and what the surface must now say it ran.
KAFKA_PASSED = KAFKA_TESTS - KAFKA_REDS - KAFKA_SKIPPED
SUITE_TOTALS_BASIS = "gradle suite totals over every claimed report"
BOUNDED_SAMPLE = "(bounded identity sample)"


@pytest.fixture
def kafka_run(tmp_path):
    """One kafka-scale dispatch: 1,176 real reports, parsed by the real readers.

    Per test rather than per module, because the host evidence-publication
    authority every receipt write goes through is installed per test — a
    receipt minted outside it would be minted outside the transport this whole
    chain is about.
    """

    receipt, _ = _run_reactor(tmp_path, _kafka_scale_layout())
    return receipt


def _executions(receipt):
    return _metrics(receipt)["tests"]["claimed"]["receipt_executions"]


# --- the shape, fixed and pinned --------------------------------------------


def test_the_kafka_shape_states_every_execution_the_reports_declared(kafka_run):
    """The whole bucket, to the field. This is the regression, written down."""

    assert _executions(kafka_run) == {
        "executed": KAFKA_TESTS,
        "passed": KAFKA_PASSED,
        "failed": KAFKA_REDS,
        "errors": 0,
        "skipped": KAFKA_SKIPPED,
        "availability": "available",
        "basis": SUITE_TOTALS_BASIS,
    }


def test_the_count_is_the_runs_and_never_the_sample_beside_it(kafka_run):
    """P-A: totals are load-bearing, identities are a bounded sample of them.

    The receipt carries both tiers over one dispatch. Reading the sample as the
    count would publish 2,048 of 27,219 executions — a fifteenth of the run,
    with no field anywhere saying so.
    """

    sample = kafka_run["testcase_execution_rows"]["rows"]
    executions = _executions(kafka_run)

    assert 0 < len(sample) < KAFKA_TESTS
    assert executions["executed"] == KAFKA_TESTS
    assert executions["executed"] != len(sample)
    # And the sample's own disclosure agrees about what it left behind.
    dropped = kafka_run["testcase_row_disclosure"]["rows_truncated"]["dropped_green"]
    assert len(sample) + dropped == KAFKA_TESTS


def test_the_red_identities_come_from_the_bounded_rows_and_all_eight_arrive(kafka_run):
    """A count without names is not actionable; the count is complete anyway.

    Red-first retention is what the bound is for: the sample sheds greens, so
    the eight measured failures are all present as identities AND the totals
    tier counts exactly eight. The two tiers agree about redness while
    disagreeing about volume, which is the whole design.
    """

    reds = [
        row
        for row in kafka_run["testcase_execution_rows"]["rows"]
        if row["outcome"] in {"failed", "error"}
    ]

    assert len(reds) == KAFKA_REDS
    assert kafka_run["testcase_row_disclosure"]["red_rows_complete"] is True
    assert _executions(kafka_run)["failed"] == KAFKA_REDS
    assert _executions(kafka_run)["errors"] == 0


def test_every_grain_names_the_population_it_counted(kafka_run):
    """MS-1 C11: no silent caps, including by juxtaposition.

    `latest_cases` counts the sample and `receipt_executions` counts the run.
    Printed side by side with no word about it, 2,048 cases under 27,219
    executions reads as a run that retried thirteen times.
    """

    claimed = _metrics(kafka_run)["tests"]["claimed"]

    assert BOUNDED_SAMPLE in claimed["latest_cases"]["basis"]
    assert BOUNDED_SAMPLE in claimed["latest_subjects"]["basis"]
    assert claimed["receipt_executions"]["basis"] == SUITE_TOTALS_BASIS
    assert BOUNDED_SAMPLE not in claimed["receipt_executions"]["basis"]
    assert claimed["latest_cases"]["executed"] < claimed["receipt_executions"]["executed"]


def test_the_session_reason_is_gone_from_the_whole_surface(kafka_run):
    """The exact string the 2026-08-26 session published, now unreachable."""

    metrics = _metrics(kafka_run)

    assert validate_report_metrics_v2(metrics) == metrics
    assert KAFKA_SESSION_REASON not in json.dumps(metrics)
    assert f"Receipt executions: {KAFKA_PASSED}/{KAFKA_TESTS} passed" in "\n".join(
        format_evidence_layer_lines(metrics)
    )


def test_the_counts_survive_the_identity_tier_being_absent_entirely(kafka_run):
    """The measured session's own shape: reports summed, no rows sealed at all.

    This is what the receipt looked like on 2026-08-26 — a dispatch whose test
    evidence never reached an identity envelope — and it is the case the
    totals tier exists for. The executions are stated; the subject and case
    grains stay honestly unavailable, because nothing sealed them.
    """

    stripped = {
        field: value
        for field, value in kafka_run.items()
        if field not in {"testcase_execution_rows", "testcase_row_disclosure"}
    }
    stripped = validate_receipt_v2(stripped, expected_id=stripped["receipt_id"])

    claimed = _metrics(stripped)["tests"]["claimed"]

    assert claimed["receipt_executions"]["executed"] == KAFKA_TESTS
    assert claimed["receipt_executions"]["basis"] == SUITE_TOTALS_BASIS
    for grain in ("latest_cases", "latest_subjects"):
        assert claimed[grain]["availability"] == "unavailable"
        assert claimed[grain]["executed"] is None


def _second_dispatch(receipt):
    """The same run's OTHER dispatch — a Maven one, one receipt sequence later.

    A run is not one command. The projection has to sum a Gradle dispatch that
    states totals with a dispatch that has only its sealed rows, and it has to
    do it without either tier standing in for the other or being counted twice.
    """

    twin = _maven_twin(receipt)
    successor = f"{twin['receipt_id'][:-4]}0002"
    twin["receipt_id"] = successor
    for row in twin["testcase_execution_rows"]["rows"]:
        row["receipt_id"] = successor
        row["execution_index"] = 2
        row["execution_id"] = execution_id_of(row)
    return validate_receipt_v2(twin, expected_id=successor)


def test_a_run_of_two_dispatches_sums_each_receipts_own_tier_exactly_once(tmp_path):
    """Gradle's totals plus Maven's rows — one contribution per receipt."""

    gradle_receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)
    maven_receipt = _second_dispatch(gradle_receipt)
    run = RED_SUITE_TESTS + GREEN_SUITE_TESTS

    metrics = assemble_report_metrics(
        snapshot={
            "verdict": "partial",
            "phase_records": [{"phase": "test", "termination": "complete"}],
            "build_evidence": {"observed": True, "judgment": "success"},
        },
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-30T12:00:00Z",
        run_pin={
            "run_id": gradle_receipt["run_id"],
            "target_repo_sha": gradle_receipt["target_sha"],
        },
        persistence={
            "receipts_expected": 2,
            "receipts_persisted": 2,
            "terminal_receipts_unpersisted": 0,
        },
        receipt_records=[gradle_receipt, maven_receipt],
    )
    executions = metrics["tests"]["claimed"]["receipt_executions"]

    assert validate_report_metrics_v2(metrics) == metrics
    assert executions["executed"] == 2 * run
    assert executions["failed"] == 2 * RED_SUITE_FAILURES
    # Both tiers spoke, and the basis names both rather than one for the pair.
    assert executions["basis"].startswith(SUITE_TOTALS_BASIS)
    assert "module-qualified receipt execution rows" in executions["basis"]


def test_an_unbounded_run_reads_the_same_tier_without_the_sample_caveat(tmp_path):
    """The small run: same basis, no caveat, and the counts unchanged.

    A project whose whole suite fits inside the bounds discloses nothing,
    because nothing was dropped — the suffix is a statement about a cap that
    fired, never decoration.
    """

    receipt, _ = _run_reactor(tmp_path, CLIENTS_LAYOUT)
    claimed = _metrics(receipt)["tests"]["claimed"]

    assert claimed["receipt_executions"] == {
        "executed": RED_SUITE_TESTS + GREEN_SUITE_TESTS,
        "passed": RED_SUITE_TESTS + GREEN_SUITE_TESTS - RED_SUITE_FAILURES,
        "failed": RED_SUITE_FAILURES,
        "errors": 0,
        "skipped": 0,
        "availability": "available",
        "basis": SUITE_TOTALS_BASIS,
    }
    assert BOUNDED_SAMPLE not in claimed["latest_cases"]["basis"]
