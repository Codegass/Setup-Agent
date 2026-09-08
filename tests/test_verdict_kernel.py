"""Scan facts do not cap execution; evidence integrity still does."""

import pytest

from sag.verdict import BUILD_SCOPE_CONFLICTS, run_verdict


def test_scan_scope_conflicts_are_recorded_facts_not_caps():
    assert BUILD_SCOPE_CONFLICTS == frozenset(
        {
            "build_modules_incomplete",
            "reactor_scope_narrowed",
            "build_coverage_scope_unverified",
            "module_scan_contradicts_physical_build",
        }
    )
    for conflict in BUILD_SCOPE_CONFLICTS:
        assert run_verdict("success", "success", [conflict]) == "success"


@pytest.mark.parametrize(
    "conflict",
    [
        "build_receipts_unreadable",
        "build_receipt_not_terminal",
        "build_receipt_scope_unavailable",
        "build_requirements_unavailable",
        "build_validation_failed",
        "build_oracle_divergence",
        "module_scan_unreadable",
        "rate_denominator_not_a_bound",
        "test_execution_interrupted",
    ],
)
def test_integrity_failures_still_cap_execution(conflict):
    assert run_verdict("success", "success", [conflict]) == "partial"
