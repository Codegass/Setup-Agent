"""The ONE producer of the test-cases denominator (#39).

Owner definition (unchanged): test rate = executed / discovered. This module
does not touch the metric; it decides how ``discovered`` is PRODUCED and what
that number may claim about itself.

Two rules, both drawn from the corpus:

1. **A census is a floor, never a ceiling.** A static census counts test
   DECLARATIONS; executions expand it (parameterization, factories, repeats)
   and shrink it (filters, exclusions, skipped modules). Nothing here caps or
   scales a numerator to the census — the unbounded band is the correct and
   permanent treatment when receipt-backed executions exceed it.
2. **One producer per run.** polaris d2r3 sealed ``discovered: 1347`` beside a
   census whose module list named 8 modules totalling 593 out of 20: two
   producers wrote one field and neither carried provenance, so no reader could
   tell which number the run had actually measured. The auditable producer is
   the per-module sum with NAMED modules; a bare total the module list does not
   explain never becomes the denominator, and the disagreement is sealed with
   both numbers rather than silently resolved.

The basis a census reports about itself is the other half of the fix. A number
with no statement of what it covers reads as a complete survey even when twelve
of twenty modules were never counted, so every census names itself
``complete``, ``partial`` or ``none`` and carries the module arithmetic behind
that word.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The disagreement, named. It is an observation about the harness's own
# bookkeeping — it says nothing about what the run executed — so it is
# adjudicated here (module sum wins, both numbers sealed) and never re-graded
# as a run-level uncertainty (`sag.verdict.ADJUDICATED_CONFLICTS`).
CENSUS_CONFLICT = "test_census_sources_disagree"

# Every surveyed test-bearing module was measured.
BASIS_COMPLETE = "complete"
# Some modules were never counted: the denominator is a FLOOR and says so,
# naming how many modules are missing from it.
BASIS_PARTIAL = "partial"
# No census exists. There is no number, and none is invented.
BASIS_NONE = "none"

CENSUS_BASES = (BASIS_COMPLETE, BASIS_PARTIAL, BASIS_NONE)


@dataclass(frozen=True)
class TestCensus:
    """One census: its number, what that number covers, and its provenance."""

    discovered: int | None
    basis: str
    measured_modules: int = 0
    total_modules: int = 0
    # The count the module sum disagrees with — in either direction, since a
    # catalog that dedupes its total but appends per module lists one shared
    # FQN twice — kept as evidence rather than dropped: banning a number from
    # being the denominator is not the same as hiding it.
    bare_total: int | None = None
    conflicts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unmeasured_modules(self) -> int:
        return max(self.total_modules - self.measured_modules, 0)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _module_counts(by_module: Any) -> dict[str, int]:
    """Named modules with an integer count — the auditable part of a census."""
    if not isinstance(by_module, Mapping):
        return {}
    counts: dict[str, int] = {}
    for name, raw in by_module.items():
        module = str(name).strip()
        if not module or isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            continue
        counts[module] = raw
    return counts


def produce_census(
    *,
    by_module: Any = None,
    module_total: Any = None,
    bare_total: Any = None,
    collected: Any = None,
) -> TestCensus:
    """Decide the run's one denominator from every census source it has.

    ``collected`` is a runner's own enumeration of its test rows (pytest
    ``--collect-only`` receipts). It enumerates what it counts, so it is
    already the auditable producer and it wins outright — which is also what
    keeps a Java ``@Test`` scan out of a Python denominator (992229d): the
    static sources are not consulted at all when it exists.

    Otherwise the module breakdown is the producer and its sum is the
    denominator. A bare total that disagrees with that sum is kept as evidence
    and named in a conflict; it never becomes the number.
    """

    enumerated = _positive_int(collected)
    if enumerated is not None:
        return TestCensus(discovered=enumerated, basis=BASIS_COMPLETE)

    counts = _module_counts(by_module)
    total = _positive_int(bare_total)
    if counts:
        summed = sum(counts.values())
        measured = len(counts)
        # A breakdown that names fewer modules than the catalog holds measured
        # only those it named; the rest are unmeasured, not absent.
        declared_modules = _positive_int(module_total) or measured
        total_modules = max(declared_modules, measured)
        conflicts = (CENSUS_CONFLICT,) if total is not None and total != summed else ()
        return TestCensus(
            discovered=summed if summed > 0 else None,
            basis=BASIS_COMPLETE if total_modules == measured else BASIS_PARTIAL,
            measured_modules=measured,
            total_modules=total_modules,
            bare_total=total,
            conflicts=conflicts,
        )

    if total is not None:
        # A flat scan with no module dimension has no module it failed to
        # measure and no second source to contradict it. Refusing it would
        # delete a real floor (P4) rather than fix a provenance defect.
        return TestCensus(discovered=total, basis=BASIS_COMPLETE, bare_total=total)

    return TestCensus(discovered=None, basis=BASIS_NONE)


def census_from_catalog_summary(
    summary: Any,
    *,
    bare_total: Any = None,
    collected: Any = None,
) -> TestCensus:
    """Read the ``test_catalog_summary`` shapes the analyzer writes.

    The trunk writes ``total_tests`` with the full breakdown; the bounded
    public fact sheet writes ``total_count`` with the modules it had room for
    plus ``by_module_total``. Both are the same census seen through different
    budgets, and both are read here so only one producer ever exists.
    """
    catalog = summary if isinstance(summary, Mapping) else {}
    declared = catalog.get("total_count")
    if declared is None:
        declared = catalog.get("total_tests")
    return produce_census(
        by_module=catalog.get("by_module"),
        module_total=catalog.get("by_module_total"),
        bare_total=bare_total if bare_total is not None else declared,
        collected=collected,
    )
