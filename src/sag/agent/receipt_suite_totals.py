"""One live reading of receipt totals (plan r2 T5; principle P-A).

Maven now seals the exact delta parser's complete result totals here too.
That global census does not prove a module map. Existing Gradle per-suite
summaries and historical receipts retain their original interpretation.

r2 changed what a Gradle receipt MEANS. `gradle_suite_summaries` states, per
(project, task-dir), what every claimed report's own `<testsuite>` root
declared, and `testcase_execution_rows` beside it is a BOUNDED SAMPLE of the
same executions. Totals are load-bearing; identities are the sample.

Until this module the totals had writers, validators and tests and not one
consumer. The 2026-08-26 kafka session left 1,176 reports and 27,219
executions on disk and every live surface said

    {"executed": null, ..., "reason": "current receipt testcase rows were unavailable"}

because the only tier anything downstream could read was the identity tier,
and that tier had nothing to give. This is the reading the consumers share:
`report_metrics`' claimed-execution counts and the success certificate's
receipt-bound `TestCounts` (`java_success_certificates`) both come through
here, so the two surfaces cannot drift into stating different numbers for one
receipt's run.

It does not re-fold the section. `invocation_receipts` already folds it to
reconcile a receipt at BUILD time (P-C: a receipt whose numbers contradict
each other is unconstructible), and a second fold carrying its own arithmetic
is precisely how two surfaces start disagreeing about one field. This adds
only what a consumer needs and a validator does not:

- `passed`, which no report declares — it is what is left of `tests` once the
  three declared dispositions are taken out, and the per-suite conservation
  rule the validator enforces is what makes that subtraction safe;
- `disclosed_bounds`, the names of the caps that fired on the summing read, so
  a partial total is never presented as a whole one (MS-1 C11).

The one thing it never returns is a zero standing in for an absent
measurement: a receipt that states no totals reads as ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from sag.agent.invocation_receipts import RECEIPT_SCHEMA_VERSION, _suite_totals

# The four ways `gradle_suite_summaries` can be less than the whole run, in the
# receipt's own words. Each is a disclosure the harvest wrote because a bound
# fired; together they are the complete answer to "does this total speak for
# every report this dispatch claimed?".
SUITE_TOTALS_BOUND_FIELDS = (
    "truncated",
    "unreadable_suites",
    "unsummarized_files",
    "post_snapshot_rewrite",
)


@dataclass(frozen=True)
class SuiteExecutionTotals:
    """One receipt's complete counts, apart from its bounded identity sample.

    `complete_claims` is the strong reading: the section still names every
    (module, task) pair it summed AND no claimed report went unsummed, so these
    counts account for every execution — and every red — the dispatch produced.
    When it is false the counts are a floor, and `disclosed_bounds` names which
    cap made them one.
    """

    tests: int
    passed: int
    failed: int
    errors: int
    skipped: int
    complete_claims: bool
    disclosed_bounds: tuple[str, ...] = ()

    def __add__(self, other: "SuiteExecutionTotals") -> "SuiteExecutionTotals":
        """Sum two receipts' totals; partiality is contagious, never averaged."""

        if not isinstance(other, SuiteExecutionTotals):
            return NotImplemented
        return SuiteExecutionTotals(
            tests=self.tests + other.tests,
            passed=self.passed + other.passed,
            failed=self.failed + other.failed,
            errors=self.errors + other.errors,
            skipped=self.skipped + other.skipped,
            complete_claims=self.complete_claims and other.complete_claims,
            disclosed_bounds=tuple(
                sorted(set(self.disclosed_bounds) | set(other.disclosed_bounds))
            ),
        )


def receipt_suite_totals(receipt: Mapping[str, Any]) -> Optional[SuiteExecutionTotals]:
    """This receipt's own suite totals, or ``None`` when it states none.

    Gradle carries per-(project, task) summaries. Maven carries the exact
    delta parser's global totals, including logical cases in retry reports.
    Both use the existing count tier; neither lends identity to a missing
    module map. Historical Maven receipts with no totals still return None.

    Nothing here interprets a receipt's other fields — whether the totals may
    be attributed to a run, a target or a certificate is the caller's binding
    question, and each caller answers it with its own chain.
    """

    if not isinstance(receipt, Mapping):
        return None
    if str(receipt.get("tool") or "").strip().lower() not in {"maven", "gradle"}:
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    try:
        summed = _suite_totals(receipt)
    except (TypeError, ValueError):
        return None
    if summed is None:
        return None
    totals = summed["totals"]
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    if passed < 0:
        # Unreachable past the build-time conservation rule, and it stays a
        # refusal rather than a clamp: a reader that repaired an impossible
        # receipt into a plausible one would be inventing the run's evidence.
        return None
    section = receipt.get("gradle_suite_summaries") or {}
    bounds = tuple(field for field in SUITE_TOTALS_BOUND_FIELDS if field in section)
    return SuiteExecutionTotals(
        tests=totals["tests"],
        passed=passed,
        failed=totals["failures"],
        errors=totals["errors"],
        skipped=totals["skipped"],
        complete_claims=summed["complete_claims"],
        disclosed_bounds=bounds,
    )


def sum_receipt_suite_totals(
    receipts: Sequence[Mapping[str, Any]],
) -> Optional[SuiteExecutionTotals]:
    """Fold every stated total in a receipt sequence, or ``None`` if none states one."""

    folded: Optional[SuiteExecutionTotals] = None
    for receipt in receipts:
        totals = receipt_suite_totals(receipt)
        if totals is None:
            continue
        folded = totals if folded is None else folded + totals
    return folded


__all__ = [
    "SUITE_TOTALS_BOUND_FIELDS",
    "SuiteExecutionTotals",
    "receipt_suite_totals",
    "sum_receipt_suite_totals",
]
