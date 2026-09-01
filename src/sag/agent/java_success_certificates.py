"""The receipt tier of the SAG-MS-1 success certificate (plan r2 T5; MS-1 §18 step 7).

MS-1 C7 (adapter-only inputs): *in live mode every certificate input is
produced by the Part II adapter from E1-E6; hand-authored inputs are
admissible only at `assurance = PROJECTED` and are so labeled.* This module is
the first live piece of that adapter — the one that turns a Gradle dispatch's
own receipt into the certificate's test counts — and it exists so that no
caller ever has an occasion to type a count in.

Three rules of the standard are load-bearing here and are enforced in code
rather than described:

- **The certificate's vocabulary is not the legacy verdict's.** Counts are
  `reported` / `assessed` (§9.1), never `executed`; the word `executed` is
  reserved to the legacy verdict and does not appear on a `TestCounts`. Two
  surfaces that say "executed" and mean different denominators is the
  measurement error the standard was written against.
- **Counts reconcile or they do not exist** (plan P-C). `reported` is
  `passed + failed + errors + skipped` by definition, so a `TestCounts` that
  does not add up is unconstructible — a hand-authored tuple that contradicts
  itself cannot be smuggled past this type.
- **Counts are verdict-bearing only while receipt-bound** (§9.2). The
  authority this returns is `receipt_bound` and nothing else: where the
  receipt's own chain is broken the adapter returns nothing at all, because an
  unbound count is a diagnostic and grading anything with it is the failure
  mode `test_results_authority` exists to prevent.

The counts come from the receipt's suite TOTALS (`receipt_suite_totals`), not
from its identity rows — that is principle P-A restated for the certificate.
kafka's measured run seals 2,048 identity rows over 27,219 executions; a
certificate built from the sample would state a fifteenth of the run and call
it the whole. Where the totals themselves were bounded, `counts_complete` is
false and `disclosed_bounds` names the cap that made them a floor (C11).

The certificate panel itself stays shadow while this lands: nothing here
decides a verdict yet, and the callers-to-be are the certificate evaluator and
the §22 attainment algebra, both of which read `counts_receipt_bound` from the
authority this states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from sag.agent.invocation_receipts import RECEIPT_SCHEMA_VERSION
from sag.agent.receipt_suite_totals import SuiteExecutionTotals, receipt_suite_totals

# §9.2's one verdict-bearing value. A count that cannot claim it is not a
# weaker certificate input; it is not a certificate input.
TEST_RESULTS_AUTHORITY_RECEIPT_BOUND = "receipt_bound"
# The receipt fields the binding predicate (MS-1 §12) reads. Identity of the
# receipt, of the run, of the checkout and of the domain, plus the contract the
# facade froze BEFORE the dispatch — which is what makes the numbers the
# outcome of a SEALED test step rather than of some command that happened.
CHAIN_IDENTITY_FIELDS = ("receipt_id", "run_id", "target_sha", "domain_id")
CHAIN_CONTRACT_FIELDS = ("contract_id", "contract_hash", "execution_binding")
_COUNT_FIELDS = ("reported", "passed", "failed", "errors", "skipped")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CertificateInputError(ValueError):
    """A count that does not reconcile tried to become a certificate input."""


@dataclass(frozen=True)
class TestCounts:
    """MS-1 §9.1 counts for one certificate, in the standard's own words.

    `assessed` is derived and never stored: it is the denominator of the pass
    and red rates, and a stored copy is one more place for a certificate to
    disagree with itself. An all-skipped run has `assessed = 0`, which §7.4
    reads as `UNKNOWN` — this type states the zero and judges nothing.
    """

    reported: int
    passed: int
    failed: int
    errors: int
    skipped: int

    def __post_init__(self) -> None:
        for field in _COUNT_FIELDS:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CertificateInputError(f"certificate {field} must be a count")
        if self.reported != self.passed + self.failed + self.errors + self.skipped:
            raise CertificateInputError(
                "certificate reported count does not equal its dispositions"
            )

    @property
    def assessed(self) -> int:
        """§9.1: the tests that produced a verdict-bearing result."""

        return self.passed + self.failed + self.errors

    @property
    def red(self) -> int:
        """Failures and errors together — the §7.4 outcome test's input."""

        return self.failed + self.errors

    def __add__(self, other: "TestCounts") -> "TestCounts":
        """§9.1: certificate-level counts are the sums over bound receipts."""

        if not isinstance(other, TestCounts):
            return NotImplemented
        return TestCounts(
            reported=self.reported + other.reported,
            passed=self.passed + other.passed,
            failed=self.failed + other.failed,
            errors=self.errors + other.errors,
            skipped=self.skipped + other.skipped,
        )


@dataclass(frozen=True)
class ReceiptBoundTestResults:
    """Counts plus the authority and disclosure that decide what they may grade.

    `receipt_ids` is the evidence ref §9.1 requires beside every per-receipt
    tuple: a certificate states which dispatches it counted, so a reader can go
    back to the receipts and recount.
    """

    counts: TestCounts
    test_results_authority: str
    receipt_ids: tuple[str, ...]
    counts_complete: bool
    disclosed_bounds: tuple[str, ...] = ()


def _chain_intact(
    receipt: Mapping[str, Any],
    *,
    run_id: Optional[str],
    target_sha: Optional[str],
) -> Optional[str]:
    """The receipt's own binding fields, or ``None`` when one of them is broken.

    Returns the receipt id, because a caller that has the chain has the
    evidence ref too and nothing else needs to re-derive it.
    """

    if not isinstance(receipt, Mapping):
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    identity: dict[str, str] = {}
    for field in CHAIN_IDENTITY_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        identity[field] = value.strip()
    # The contract binding is validated as one complete tuple or none at all,
    # so requiring the tuple here is requiring the seal, not three coincidences.
    for field in CHAIN_CONTRACT_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
    for expected, field in ((run_id, "run_id"), (target_sha, "target_sha")):
        wanted = str(expected or "").strip()
        if wanted and identity[field] != wanted:
            return None
    # P-B: the counts are only ever a reading of bytes the delta claims, so a
    # receipt whose claim set is malformed backs nothing. An EMPTY claim set is
    # the same refusal for a different reason — totals summed from no claimed
    # report would be another dispatch's tests wearing this receipt's identity.
    delta = receipt.get("report_delta")
    if not isinstance(delta, Mapping):
        return None
    claims: set[tuple[str, str]] = set()
    claimed = 0
    for bucket in ("new", "changed", "cached"):
        entries = delta.get(bucket)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, Mapping):
                return None
            path = str(entry.get("path") or "").strip()
            digest = str(entry.get("sha256") or "").strip().lower()
            if not path or _SHA256_RE.fullmatch(digest) is None:
                return None
            claims.add((path, digest))
            claimed += 1
    if not claims or len(claims) != claimed:
        return None
    return identity["receipt_id"]


def _counts_of(totals: SuiteExecutionTotals) -> TestCounts:
    return TestCounts(
        reported=totals.tests,
        passed=totals.passed,
        failed=totals.failed,
        errors=totals.errors,
        skipped=totals.skipped,
    )


def receipt_bound_test_counts(
    receipt: Mapping[str, Any],
    *,
    run_id: Optional[str] = None,
    target_sha: Optional[str] = None,
) -> Optional[ReceiptBoundTestResults]:
    """One receipt's certificate counts, or ``None`` when it may not state any.

    ``None`` is the honest answer for every refusal this makes — an unbound
    count is a diagnostic (§9.2) and the certificate's `counts_receipt_bound`
    is simply false. It is never a zero: a zero would be a measured claim that
    the dispatch ran nothing.
    """

    receipt_id = _chain_intact(receipt, run_id=run_id, target_sha=target_sha)
    if receipt_id is None:
        return None
    totals = receipt_suite_totals(receipt)
    if totals is None:
        return None
    return ReceiptBoundTestResults(
        counts=_counts_of(totals),
        test_results_authority=TEST_RESULTS_AUTHORITY_RECEIPT_BOUND,
        receipt_ids=(receipt_id,),
        counts_complete=totals.complete_claims,
        disclosed_bounds=totals.disclosed_bounds,
    )


def certificate_test_counts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    run_id: Optional[str] = None,
    target_sha: Optional[str] = None,
) -> Optional[ReceiptBoundTestResults]:
    """§9.1: the certificate's counts are the sum over every bound receipt.

    A receipt that cannot bind contributes nothing and is not an error — a run
    may hold receipts for other domains, other targets and other tools. What it
    may never do is contribute a count without contributing its binding.
    """

    folded: Optional[TestCounts] = None
    receipt_ids: list[str] = []
    complete = True
    bounds: set[str] = set()
    for receipt in receipts or ():
        bound = receipt_bound_test_counts(receipt, run_id=run_id, target_sha=target_sha)
        if bound is None:
            continue
        folded = bound.counts if folded is None else folded + bound.counts
        receipt_ids.extend(bound.receipt_ids)
        complete = complete and bound.counts_complete
        bounds.update(bound.disclosed_bounds)
    if folded is None:
        return None
    return ReceiptBoundTestResults(
        counts=folded,
        test_results_authority=TEST_RESULTS_AUTHORITY_RECEIPT_BOUND,
        receipt_ids=tuple(receipt_ids),
        counts_complete=complete,
        disclosed_bounds=tuple(sorted(bounds)),
    )


__all__ = [
    "CHAIN_CONTRACT_FIELDS",
    "CHAIN_IDENTITY_FIELDS",
    "TEST_RESULTS_AUTHORITY_RECEIPT_BOUND",
    "CertificateInputError",
    "ReceiptBoundTestResults",
    "TestCounts",
    "certificate_test_counts",
    "receipt_bound_test_counts",
]
