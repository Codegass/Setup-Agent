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
from sag.agent.receipt_test_rows import (
    DELTA_TESTCASE_ROW_CAP,
    testcase_execution_id as execution_id_of,
)
from sag.tools.report_metrics import (
    assemble_report_metrics,
    format_evidence_layer_lines,
    validate_report_metrics_v2,
)

# What the measured session ran, and what the surface must now say it ran.
KAFKA_PASSED = KAFKA_TESTS - KAFKA_REDS - KAFKA_SKIPPED
SUITE_TOTALS_BASIS = "gradle suite totals over every claimed report"
BOUNDED_SAMPLE = "(bounded identity sample)"
BOUNDED_EXECUTIONS_REASON = (
    "a current receipt stated no suite totals and its identity rows were bounded"
)


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


# --- the other half of P-A: no totals tier, and the sample is not a total ----
#
# The totals tier is what makes 27,219 sayable. Where it is absent — a gradle
# dispatch whose head-reader transport failed, or ANY maven or pytest dispatch,
# which never had one — the only execution evidence a receipt holds is its
# sealed identity rows, and since T1 bounded that read on every runner those
# rows are a sample of at most `DELTA_TESTCASE_ROW_CAP`. Publishing the sample
# as the count is the same error the suffix was invented to prevent, committed
# one field over: it is `available`, it reads as a total, and only a
# parenthetical distinguishes 2,048 executions from 2,048 of 27,219.


def _without_suite_totals(receipt):
    """The kafka dispatch whose tier-1 head reader failed, receipt and all.

    `record_invocation` reads the totals and the rows over two separate
    transports, so one can fail while the other seals a complete envelope. The
    receipt then says so — `gradle_suite_summaries` absent, the omission
    declared in the engine's own vocabulary — and carries a full, capped,
    red-complete identity sample beside it.
    """

    stripped = {field: value for field, value in receipt.items() if field != "gradle_suite_summaries"}
    stripped["evidence_omissions"] = sorted(
        [
            *(stripped.get("evidence_omissions") or []),
            {
                "field": "gradle_suite_summaries",
                "status": "unavailable",
                "reasons": ["gradle_suite_totals_unreadable"],
            },
        ],
        key=lambda entry: entry["field"],
    )
    return validate_receipt_v2(stripped, expected_id=stripped["receipt_id"])


def test_a_gradle_run_whose_totals_tier_failed_withholds_the_count_it_cannot_state(
    kafka_run,
):
    """The probe, pinned: 2,048 sealed rows never become 2,048 executions.

    Both transports are real here — the envelope is `complete`, its rows are
    exactly the cap, its reds are all present — and none of that makes the
    sample a count of the run. The surface states the bound instead of a
    fifteenth of the executions.
    """

    stripped = _without_suite_totals(kafka_run)
    claimed = _metrics(stripped)["tests"]["claimed"]

    assert stripped["testcase_execution_rows"]["status"] == "complete"
    assert len(stripped["testcase_execution_rows"]["rows"]) == DELTA_TESTCASE_ROW_CAP
    assert claimed["receipt_executions"] == {
        "executed": None,
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
        "availability": "unavailable",
        "reason": BOUNDED_EXECUTIONS_REASON,
    }
    # The identity grains are unharmed: they count the sample, they say so, and
    # withholding the run's count is not a reason to withhold the names.
    for grain in ("latest_cases", "latest_subjects"):
        assert claimed[grain]["availability"] == "available"
        assert BOUNDED_SAMPLE in claimed[grain]["basis"]


def test_a_maven_run_past_the_row_cap_states_no_count_rather_than_its_sample(kafka_run):
    """The unconditional case: a runner with no totals tier at all.

    Nothing about this is gradle-specific — maven and pytest receipts reach the
    same shape whenever the run outgrows the cap, which is exactly where an
    understated count does the most damage.
    """

    twin = _maven_twin(kafka_run)
    executions = _metrics(twin)["tests"]["claimed"]["receipt_executions"]

    assert len(twin["testcase_execution_rows"]["rows"]) == DELTA_TESTCASE_ROW_CAP < KAFKA_TESTS
    assert "gradle_suite_summaries" not in twin
    assert executions["availability"] == "unavailable"
    assert executions["executed"] is None
    assert executions["reason"] == BOUNDED_EXECUTIONS_REASON


def test_a_bounded_row_tier_makes_the_sum_beside_it_a_floor_not_a_total(kafka_run):
    """Partiality is contagious across dispatches, as it is across totals.

    Gradle's totals are whole and maven's rows are capped; added together they
    are a floor. A floor published as the run's count is the same lie with a
    larger number on it, so the aggregate is withheld while each receipt's own
    evidence stays exactly as valid as it was.
    """

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
            "run_id": kafka_run["run_id"],
            "target_repo_sha": kafka_run["target_sha"],
        },
        persistence={
            "receipts_expected": 2,
            "receipts_persisted": 2,
            "terminal_receipts_unpersisted": 0,
        },
        receipt_records=[kafka_run, _second_dispatch(kafka_run)],
    )
    executions = metrics["tests"]["claimed"]["receipt_executions"]

    assert validate_report_metrics_v2(metrics) == metrics
    assert executions["availability"] == "unavailable"
    assert executions["reason"] == BOUNDED_EXECUTIONS_REASON


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
