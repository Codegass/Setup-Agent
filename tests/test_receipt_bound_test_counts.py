# tests/test_receipt_bound_test_counts.py
"""The certificate adapter's receipt tier — MS-1 §18 C7, on a kafka-scale receipt.

C7 says every live certificate input is ADAPTED from the evidence layer and
that hand-authored inputs are admissible only at `assurance = PROJECTED`. The
adapter under test is the first live piece of that: it turns one Gradle
dispatch's receipt into the certificate's §9.1 counts and states the one
authority (`receipt_bound`) those counts may be graded with.

The receipt it is measured against is the 2026-08-26 kafka session's shape —
19 modules, 1,176 claimed reports, 27,219 executions, 8 red, 32 skipped —
because that run is the reason the tier exists. Its identity sample holds
2,048 rows; a certificate built from the sample would state a fifteenth of the
run and call it the project.

(`TestCounts` is aliased on import below. pytest collects anything named
`Test*` out of a test module, and the standard's own name for the count tuple
is not negotiable to suit a collector.)
"""

import pytest
from pydantic import ValidationError

from sag.agent.invocation_receipts import build_receipt, validate_receipt_v2
from sag.agent.java_success_certificates import (
    TEST_RESULTS_AUTHORITY_RECEIPT_BOUND,
)
from sag.agent.java_success_certificates import TestCounts as CertificateCounts
from sag.agent.java_success_certificates import (
    certificate_test_counts,
    receipt_bound_test_counts,
)

RUN_ID = "run-kafka-d2r6"
TARGET_SHA = "26b251a4" + "0" * 32
ROOT = "/workspace/kafka"
CONTRACT = {
    "contract_id": "ic-0123456789ab",
    "contract_hash": "a" * 64,
    "execution_binding": "argv_v1",
    "compliance": "exact",
}

# `docs/superpowers/reports/gradle-evidence-20260830.md`, the measured session.
KAFKA_MODULES = (
    "clients",
    "connect-api",
    "connect-json",
    "connect-runtime",
    "core",
    "metadata",
    "raft",
    "server-common",
    "storage",
    "streams",
    "streams-scala",
    "tools",
    "trogdor",
    "group-coordinator",
    "transaction-coordinator",
    "shell",
    "examples",
    "jmh-benchmarks",
    "generator",
)
KAFKA_REPORTS = 1176
KAFKA_TESTS = 27_219
KAFKA_REDS = 8
KAFKA_SKIPPED = 32
# The three red-bearing modules the study cross-checked, and how the eight
# failures fell across them.
KAFKA_RED_MODULES = {"clients": 3, "metadata": 3, "streams": 2}


def _kafka_suites():
    """19 (project, task-dir) totals summing to exactly the measured shape."""

    base, extra = divmod(KAFKA_TESTS, len(KAFKA_MODULES))
    suites = []
    for index, module in enumerate(KAFKA_MODULES):
        tests = base + (1 if index < extra else 0)
        suites.append(
            {
                "module": f":{module}",
                "task": "test",
                "xml_files": 62,
                "tests": tests,
                "failures": KAFKA_RED_MODULES.get(module, 0),
                "errors": 0,
                "skipped": KAFKA_SKIPPED if index == 0 else 0,
            }
        )
    return suites


def _claimed_reports(count=KAFKA_REPORTS):
    return {
        f"{ROOT}/m{index % len(KAFKA_MODULES)}/build/test-results/test/TEST-{index:04d}.xml": (
            f"{index:064x}"
        )
        for index in range(count)
    }


def _kafka_receipt(*, receipt_id="inv-gradle-test-0001", summaries=None, reports=None, **overrides):
    """One kafka-scale gradle receipt, through the production builder."""

    fields = {
        "run_id": RUN_ID,
        "tool": "gradle",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "./gradlew --build-cache test",
        "working_directory": ROOT,
        "exit_code": 0,
        "before": {},
        "after": _claimed_reports() if reports is None else reports,
        "target_sha": TARGET_SHA,
        "domain_id": "kafka",
        "gradle_suite_summaries": {"suites": _kafka_suites()} if summaries is None else summaries,
        **CONTRACT,
    }
    fields.update(overrides)
    receipt = build_receipt(receipt_id=receipt_id, **fields)
    # Every receipt in this file is one the live reader would accept, and one
    # whose totals the builder actually took: an adapter proved against a
    # payload the engine could never write proves nothing about the engine.
    if fields["gradle_suite_summaries"]:
        assert "gradle_suite_summaries" in receipt, receipt.get("evidence_omissions")
    return validate_receipt_v2(receipt, expected_id=receipt_id)


# --- the counts themselves --------------------------------------------------


def test_the_kafka_receipt_yields_the_counts_the_reports_declared():
    """27,219 executions, from the totals — not from the sample beside them."""

    bound = receipt_bound_test_counts(_kafka_receipt(), run_id=RUN_ID, target_sha=TARGET_SHA)

    assert bound is not None
    assert bound.counts == CertificateCounts(
        reported=KAFKA_TESTS,
        passed=KAFKA_TESTS - KAFKA_REDS - KAFKA_SKIPPED,
        failed=KAFKA_REDS,
        errors=0,
        skipped=KAFKA_SKIPPED,
    )
    # §9.1's derived denominators, computed once and never stored.
    assert bound.counts.assessed == KAFKA_TESTS - KAFKA_SKIPPED
    assert bound.counts.red == KAFKA_REDS
    assert bound.test_results_authority == TEST_RESULTS_AUTHORITY_RECEIPT_BOUND
    assert bound.receipt_ids == ("inv-gradle-test-0001",)
    assert bound.counts_complete is True
    assert bound.disclosed_bounds == ()


def test_the_certificate_never_speaks_the_legacy_verdicts_word():
    """§9.1: `executed` is reserved to the legacy verdict.

    Two surfaces that both say "executed" over different denominators is the
    measurement error the standard was written against, so the certificate's
    count type does not carry the word at all.
    """

    counts = CertificateCounts(reported=4, passed=2, failed=1, errors=0, skipped=1)

    assert not hasattr(counts, "executed")
    assert "executed" not in vars(counts)


@pytest.mark.parametrize(
    "counts",
    [
        {"reported": 3, "passed": 1, "failed": 1, "errors": 0, "skipped": 0},
        {"reported": 1, "passed": 0, "failed": 2, "errors": 0, "skipped": 0},
        {"reported": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": -1},
        {"reported": 1, "passed": True, "failed": 0, "errors": 0, "skipped": 0},
    ],
)
def test_a_count_tuple_that_does_not_reconcile_is_unconstructible(counts):
    """P-C at the certificate boundary: the owner's `tests=1, failures=2` shape.

    A hand-authored tuple is exactly what C7 forbids in live mode, and the
    cheapest possible defence is a type that refuses to hold a contradiction.
    """

    with pytest.raises(ValidationError):
        CertificateCounts(**counts)


def test_counts_add_across_receipts_the_way_the_standard_sums_them():
    """§9.1: certificate-level counts are the sums over bound receipts."""

    first = CertificateCounts(reported=4, passed=2, failed=1, errors=1, skipped=0)
    second = CertificateCounts(reported=3, passed=1, failed=0, errors=0, skipped=2)

    assert first + second == CertificateCounts(reported=7, passed=3, failed=1, errors=1, skipped=2)
    assert (first + second).assessed == 5


# --- the chain that makes them verdict-bearing ------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"run_id": "run-someone-else"}, id="another run"),
        pytest.param({"target_sha": "f" * 40}, id="another checkout"),
    ],
)
def test_counts_bound_to_another_run_or_target_are_not_this_certificates(kwargs):
    """§9.2: unbound counts are diagnostics. They grade nothing, so none arrive."""

    query = {"run_id": RUN_ID, "target_sha": TARGET_SHA}
    query.update(kwargs)

    assert receipt_bound_test_counts(_kafka_receipt(), **query) is None


def test_a_dispatch_with_no_frozen_contract_states_no_certificate_counts():
    """A count is the outcome of a SEALED test step or it is an observation.

    The contract binding is validated as one complete tuple or none at all, so
    a receipt without it was not measured against anything the run froze before
    it ran — MS-1 §12's binding predicate has nothing to bind.
    """

    unsealed = _kafka_receipt(
        contract_id=None, contract_hash=None, execution_binding=None, compliance=None
    )

    assert "contract_id" not in unsealed
    assert receipt_bound_test_counts(unsealed, run_id=RUN_ID, target_sha=TARGET_SHA) is None


def test_totals_summed_from_no_claimed_report_are_another_dispatchs_tests():
    """P-B: a count is a reading of bytes THIS receipt's delta claims."""

    receipt = dict(_kafka_receipt())
    receipt["report_delta"] = {"new": [], "changed": []}

    assert receipt_bound_test_counts(receipt, run_id=RUN_ID, target_sha=TARGET_SHA) is None


def test_a_receipt_on_a_dead_schema_version_is_not_a_live_certificate_input():
    """T6's rule, restated: 3 is the only version a live reader takes."""

    receipt = dict(_kafka_receipt())
    receipt["schema_version"] = 4

    assert receipt_bound_test_counts(receipt, run_id=RUN_ID, target_sha=TARGET_SHA) is None


def test_a_receipt_that_harvested_no_totals_contributes_nothing_not_a_zero():
    """A zero would be a measured claim that the dispatch ran no tests."""

    receipt = _kafka_receipt(summaries=False)

    assert "gradle_suite_summaries" not in receipt
    assert receipt_bound_test_counts(receipt, run_id=RUN_ID, target_sha=TARGET_SHA) is None
    assert certificate_test_counts([receipt], run_id=RUN_ID, target_sha=TARGET_SHA) is None


# --- disclosure and aggregation ---------------------------------------------


def test_a_bounded_total_is_a_floor_and_says_which_cap_made_it_one():
    """C11: no silent caps. A partial total never presents as a whole one."""

    suites = _kafka_suites()
    bounded = _kafka_receipt(
        summaries={
            "suites": suites[:8],
            "truncated": True,
            "dropped_suites": len(suites) - 8,
            "unsummarized_files": 400,
        }
    )

    bound = receipt_bound_test_counts(bounded, run_id=RUN_ID, target_sha=TARGET_SHA)

    assert bound is not None
    assert bound.counts.reported == sum(suite["tests"] for suite in suites[:8])
    assert bound.counts.reported < KAFKA_TESTS
    assert bound.counts_complete is False
    assert bound.disclosed_bounds == ("truncated", "unsummarized_files")
    # Still receipt-bound: what the read reached, it reached through the same
    # digest-verified bytes. Partial is a reach, not a loss of authority.
    assert bound.test_results_authority == TEST_RESULTS_AUTHORITY_RECEIPT_BOUND


def test_a_run_of_two_dispatches_sums_them_and_keeps_both_evidence_refs():
    first = _kafka_receipt(
        receipt_id="inv-gradle-test-0001",
        summaries={"suites": _kafka_suites()[:10]},
    )
    second = _kafka_receipt(
        receipt_id="inv-gradle-test-0002",
        summaries={"suites": _kafka_suites()[10:]},
        reports=_claimed_reports(40),
    )
    foreign = _kafka_receipt(receipt_id="inv-gradle-test-0003", run_id="run-earlier")

    folded = certificate_test_counts([first, second, foreign], run_id=RUN_ID, target_sha=TARGET_SHA)

    assert folded is not None
    assert folded.counts.reported == KAFKA_TESTS
    assert folded.counts.failed == KAFKA_REDS
    assert folded.counts.skipped == KAFKA_SKIPPED
    assert folded.receipt_ids == ("inv-gradle-test-0001", "inv-gradle-test-0002")
    assert folded.counts_complete is True
