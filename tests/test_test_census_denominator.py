"""Static discovery stays diagnostic beside runtime outcome accounting (#39).

Spec: ``docs/superpowers/specs/2026-08-15-test-cases-denominator-design.md``.

LIVE EVIDENCE — all three acceptance shapes are transcribed from recorded runs,
never invented:

* **polaris** (d2r3, ``logs/session_20260814_100129_764872_b92b9a5c90a1_26484``):
  the analyzer sealed ``static_test_count: 1347`` beside a census whose module
  list names 8 modules totalling 593 out of ``by_module_total: 20``, and the
  the old verdict sealed ``cases 16496/1347 (unbounded)``. The runtime outcome
  accounting now stays 16496/16496 while both static observations remain named
  diagnostics.
* **camel** (d2r2/d2r3): no receipt-scoped runtime outcomes and no unified
  static census. Both absences remain explicit, and neither becomes ``0/0``.
* **tapestry-5** (d2r2): ``discovered: 1614`` with ``unique.executed: 2417`` —
  the numerator legitimately exceeds a COMPLETE census because parameterized
  suites and factories expand declarations into executions.

The census is a diagnostic FLOOR, never a runtime denominator (spec §1): no
assertion here caps or scales the execution population to it.
"""

from __future__ import annotations

import pytest

from sag.agent.physical_validator import PhysicalValidator
from sag.agent.verdict_finalizer import SnapshotTestCounts, SnapshotTestStats
from sag.agent.verdict_finalizer import test_grain_rates as grain_rates
from sag.case_census import (
    BASIS_COMPLETE,
    BASIS_NONE,
    BASIS_PARTIAL,
    CENSUS_CONFLICT,
    census_from_catalog_summary,
    produce_census,
)
from sag.project_fact_sheet import project_fact_sheet_metadata

# Aliased: pytest tries to COLLECT any module-level name starting with `Test`,
# and warns on both of these because they define `__init__`.
from sag.testcases.catalog import TestCaseCatalog as CaseCatalog
from sag.testcases.catalog import TestCaseDescriptor as CaseDescriptor
from sag.verdict import ADJUDICATED_CONFLICTS
from sag.verdict_rates import UNBOUNDED_CONFLICT

# --------------------------------------------------------------------------- #
# Recorded material.
# --------------------------------------------------------------------------- #

# control_events.jsonl sequence 56, `project(action=analyze)` metadata, verbatim.
POLARIS_BY_MODULE = {
    "api": 24,
    "impl": 194,
    "integration-tests": 8,
    "management-model": 4,
    "metastore-maintenance": 1,
    "polaris-core": 284,
    "relational-jdbc": 71,
    "store-nosql": 7,
}
POLARIS_MODULE_TOTAL = 20
POLARIS_BARE_TOTAL = 1347
POLARIS_MODULE_SUM = 593
POLARIS_EXECUTED = 16496

# The same census untruncated, from the trunk context dumped in that session's
# main.log — 20 modules, and they add up to the 1,347 the bare total claims.
POLARIS_TRUNK_BY_MODULE = {
    "integration-tests": 8,
    "polaris-core": 284,
    "management-model": 4,
    "impl": 194,
    "relational-jdbc": 71,
    "store-nosql": 7,
    "api": 24,
    "metastore-maintenance": 1,
    "metastore-types": 21,
    "metastore": 15,
    "varint": 3,
    "quarkus-distcache": 21,
    "weld": 1,
    "retain-cel": 3,
    "spark": 47,
    "admin": 29,
    "server": 1,
    "service": 607,
    "misc-types": 5,
    "generator": 1,
}

TAPESTRY_DISCOVERED = 1614
TAPESTRY_EXECUTED = 2417

# camel-quarkus (d2r3, ``logs/session_20260814_093336_763203_12dba3497295_26013``,
# ``.setup_agent/contexts/trunk_20260814_093418.json``): the analyzer sealed
# ``total_tests: 2765`` beside a 384-module breakdown that sums to 3,282 — the
# module list is LARGER than the bare total, the opposite of polaris, and it is
# the only direction the live corpus ever disagrees in. Of the 22 distinct
# census shapes archived in ``logs/``: 12 agree, 3 carry no module dimension at
# all, and all 7 disagreements sum ABOVE their bare total (camel 26842/27073,
# camel-quarkus 2765/3282, samza 2223/2279, kie-kogito 331/375, gora 275/297,
# ignite 15085/15104, jackrabbit 2534/2543). That run executed nothing under a
# receipt (``unique.executed: 0``).
CAMEL_QUARKUS_BARE_TOTAL = 2765
CAMEL_QUARKUS_MODULE_SUM = 3282
CAMEL_QUARKUS_EXECUTED = 0


def _stats(**kwargs) -> SnapshotTestStats:
    executed = kwargs.pop("executed", 0)
    counts = SnapshotTestCounts(executed=executed, passed=executed)
    return SnapshotTestStats(unique=counts, raw=counts, **kwargs)


def _cases(stats: SnapshotTestStats) -> tuple[dict, tuple[str, ...]]:
    grains, conflicts = grain_rates(stats, driven_modules=set(), test_modules=set())
    return grains["cases"].payload(), conflicts


# --------------------------------------------------------------------------- #
# §2.1 — one producer: the per-module sum with named modules.
# --------------------------------------------------------------------------- #


def test_polaris_module_sum_wins_and_names_both_numbers():
    """The 1,347 no module list explains never becomes the denominator."""
    census = produce_census(
        by_module=POLARIS_BY_MODULE,
        module_total=POLARIS_MODULE_TOTAL,
        bare_total=POLARIS_BARE_TOTAL,
    )

    assert census.discovered == POLARIS_MODULE_SUM
    assert census.bare_total == POLARIS_BARE_TOTAL
    assert census.conflicts == (CENSUS_CONFLICT,)
    assert census.basis == BASIS_PARTIAL
    assert census.measured_modules == 8
    assert census.total_modules == POLARIS_MODULE_TOTAL
    assert census.unmeasured_modules == 12


def test_agreeing_sources_seal_no_conflict_and_a_complete_basis():
    """The same producer over the untruncated trunk census: 20 modules, 1,347."""
    full = {**POLARIS_BY_MODULE, "everything-else": POLARIS_BARE_TOTAL - POLARIS_MODULE_SUM}
    census = produce_census(by_module=full, module_total=9, bare_total=POLARIS_BARE_TOTAL)

    assert census.discovered == POLARIS_BARE_TOTAL
    assert census.basis == BASIS_COMPLETE
    assert census.unmeasured_modules == 0
    assert census.conflicts == ()


def test_a_census_with_no_module_dimension_is_still_one_producer():
    """A flat scan has no module it failed to measure — and no second source."""
    census = produce_census(bare_total=TAPESTRY_DISCOVERED)

    assert census.discovered == TAPESTRY_DISCOVERED
    assert census.basis == BASIS_COMPLETE
    assert census.conflicts == ()


def test_no_census_invents_no_number():
    census = produce_census()

    assert census.discovered is None
    assert census.basis == BASIS_NONE
    assert census.conflicts == ()


def test_both_recorded_catalog_shapes_reach_the_same_producer():
    """The trunk writes ``total_tests``; the bounded fact sheet ``total_count``.

    Same census, two budgets. Read through one producer, the trunk's full
    breakdown explains its own total and the bounded record says how much of
    that total its module list accounts for.
    """
    trunk = census_from_catalog_summary(
        {"total_tests": POLARIS_BARE_TOTAL, "by_module": POLARIS_TRUNK_BY_MODULE},
        bare_total=POLARIS_BARE_TOTAL,
    )
    assert trunk.discovered == POLARIS_BARE_TOTAL
    assert trunk.basis == BASIS_COMPLETE
    assert trunk.conflicts == ()

    bounded = census_from_catalog_summary(
        {
            "total_count": POLARIS_BARE_TOTAL,
            "by_module": POLARIS_BY_MODULE,
            "by_module_total": POLARIS_MODULE_TOTAL,
        }
    )
    assert bounded.discovered == POLARIS_MODULE_SUM
    assert bounded.basis == BASIS_PARTIAL
    assert bounded.conflicts == (CENSUS_CONFLICT,)


def test_a_truncated_record_states_what_its_module_list_accounts_for():
    """The record polaris's reviewer read must reconcile with itself (§2.1)."""
    metadata = project_fact_sheet_metadata(
        {
            "project_path": "/workspace/polaris",
            "project_type": "Java",
            "test_catalog": {
                "total_count": POLARIS_BARE_TOTAL,
                "by_module": POLARIS_TRUNK_BY_MODULE,
            },
        }
    )

    catalog = metadata["test_catalog_summary"]
    assert catalog["total_count"] == POLARIS_BARE_TOTAL
    assert catalog["by_module_total"] == POLARIS_MODULE_TOTAL
    assert len(catalog["by_module"]) == 8
    assert catalog["by_module_measured"] == POLARIS_MODULE_SUM
    assert catalog["by_module_measured"] == sum(catalog["by_module"].values())


def test_an_untruncated_record_grows_no_second_total():
    metadata = project_fact_sheet_metadata(
        {
            "project_path": "/workspace/demo",
            "project_type": "Java",
            "test_catalog": {"total_count": 4, "by_module": {"core": 3, "io": 1}},
        }
    )

    assert "by_module_measured" not in metadata["test_catalog_summary"]


def test_runner_enumerated_rows_are_the_producer_and_never_mix_languages():
    """Rule §2.3: a Java static count never explains a Python collection."""
    census = produce_census(
        by_module=POLARIS_BY_MODULE,
        module_total=POLARIS_MODULE_TOTAL,
        bare_total=POLARIS_BARE_TOTAL,
        collected=1927,
    )

    assert census.discovered == 1927
    assert census.basis == BASIS_COMPLETE
    assert census.conflicts == ()


# --------------------------------------------------------------------------- #
# §4 — the three acceptance shapes, beside the sealed outcome-accounting grain.
# --------------------------------------------------------------------------- #


def test_polaris_shape_one_denominator_a_named_floor_and_both_numbers():
    payload, conflicts = _cases(
        _stats(
            executed=POLARIS_EXECUTED,
            discovered=POLARIS_MODULE_SUM,
            denominator_basis=BASIS_PARTIAL,
            denominator_unmeasured_modules=12,
            denominator_module_total=POLARIS_MODULE_TOTAL,
            denominator_bare_total=POLARIS_BARE_TOTAL,
        )
    )

    assert payload["denominator"] == POLARIS_EXECUTED
    assert payload["numerator"] == POLARIS_EXECUTED
    assert payload["band"] == "fully"
    assert "593 static test declarations observed" in payload["reason"]
    # The floor is named with the module count missing from it (§2.2) ...
    assert "12 of 20 modules unmeasured" in payload["reason"]
    # ... the rejected total stays visible beside the one that won (§2.1) ...
    assert "593 in the module list against a bare total of 1,347" in payload["reason"]
    # ... and a partial census never claims the excess IS expansion (§2.4).
    assert "parameterized expansion" not in payload["reason"]
    # The grain states the disagreement; it does not re-derive the conflict,
    # which the producer raised once at the seam that resolved it.
    assert conflicts == ()


def test_camel_quarkus_shape_names_both_totals_when_the_module_list_is_larger():
    """The live direction: the module list explains MORE than the bare total.

    A clause that says the bare total has "no module list to explain it" states
    the opposite of this record — 384 modules explain 3,282 of a 2,765 claim.
    Both numbers are named and neither is called the unexplained one, so the
    sentence stays true whichever way the two producers disagree.
    """
    payload, conflicts = _cases(
        _stats(
            executed=CAMEL_QUARKUS_EXECUTED,
            discovered=CAMEL_QUARKUS_MODULE_SUM,
            denominator_basis=BASIS_COMPLETE,
            denominator_bare_total=CAMEL_QUARKUS_BARE_TOTAL,
        )
    )

    assert payload["band"] == "unavailable"
    assert "3,282 static test declarations observed" in payload["reason"]
    assert "3,282 in the module list against a bare total of 2,765" in payload["reason"]
    assert "no module list to explain" not in payload["reason"]
    assert conflicts == ()


def test_one_fqn_in_two_modules_puts_the_module_sum_above_the_total():
    """Why the live direction exists, at the seam that produces both numbers.

    ``count()`` dedupes on ``package.class::method``; ``_by_module`` appends the
    key once per add. One test FQN reachable from two modules is therefore one
    descriptor and two module entries — an arithmetic property of the catalog,
    not a miscount to be explained away.
    """
    shared = {
        "package": "org.apache.camel.quarkus.core",
        "class_name": "CoreTest",
        "method_name": "loads",
        "file_path": "core/CoreTest.java",
    }
    catalog = CaseCatalog()
    catalog.add(CaseDescriptor(**shared, module="extensions/core"))
    catalog.add(CaseDescriptor(**shared, module="integration-tests/core"))
    catalog.add(
        CaseDescriptor(
            package="org.apache.camel.quarkus.main",
            class_name="MainTest",
            method_name="starts",
            file_path="main/MainTest.java",
            module="extensions/main",
        )
    )

    summary = catalog.to_dict()
    assert summary["total_count"] == 2
    assert sum(summary["by_module"].values()) == 3

    census = census_from_catalog_summary(summary)
    assert census.discovered == 3
    assert census.bare_total == 2
    assert census.discovered > census.bare_total
    assert census.basis == BASIS_COMPLETE
    assert census.conflicts == (CENSUS_CONFLICT,)


def test_camel_shape_keeps_todays_unavailable_and_invents_nothing():
    payload, conflicts = _cases(_stats(executed=0, denominator_basis=BASIS_NONE))

    assert payload == {
        "band": "unavailable",
        "reason": "no receipt-scoped runtime outcomes were accounted",
    }
    assert conflicts == ()


def test_tapestry_shape_accounts_runtime_outcomes_and_states_static_expansion():
    payload, conflicts = _cases(
        _stats(
            executed=TAPESTRY_EXECUTED,
            discovered=TAPESTRY_DISCOVERED,
            denominator_basis=BASIS_COMPLETE,
        )
    )

    assert payload["band"] == "fully"
    assert payload["numerator"] == TAPESTRY_EXECUTED
    assert payload["denominator"] == TAPESTRY_EXECUTED
    assert "parameterized expansion over 1,614 declared" in payload["reason"]
    assert "1,614 static test declarations observed" in payload["reason"]
    assert "numerator exceeds denominator" not in payload["reason"]
    assert conflicts == ()


def test_a_complete_static_census_is_diagnostic_not_the_runtime_denominator():
    payload, _ = _cases(
        _stats(executed=100, discovered=200, denominator_basis=BASIS_COMPLETE),
    )

    assert payload == {
        "rate": 100.0,
        "band": "fully",
        "numerator": 100,
        "denominator": 100,
        "reason": (
            "200 static test declarations observed "
            "(diagnostic only; not the runtime denominator)"
        ),
    }


def test_an_unnamed_basis_still_labels_static_discovery_as_diagnostic():
    payload, _ = _cases(_stats(executed=TAPESTRY_EXECUTED, discovered=TAPESTRY_DISCOVERED))

    assert payload["reason"] == (
        "1,614 static test declarations observed " "(diagnostic only; not the runtime denominator)"
    )


def test_the_partial_floor_is_stated_even_when_the_census_bounds_the_run():
    payload, _ = _cases(
        _stats(
            executed=100,
            discovered=593,
            denominator_basis=BASIS_PARTIAL,
            denominator_unmeasured_modules=12,
            denominator_module_total=20,
        )
    )

    assert payload["band"] == "fully"
    assert payload["numerator"] == payload["denominator"] == 100
    assert payload["reason"] == (
        "593 static test declarations observed "
        "(diagnostic only; not the runtime denominator); "
        "12 of 20 modules unmeasured"
    )


# --------------------------------------------------------------------------- #
# The basis is SEALED with the stats, and the disagreement is adjudicated.
# --------------------------------------------------------------------------- #


def test_the_basis_travels_into_the_sealed_snapshot():
    stats = _stats(
        executed=POLARIS_EXECUTED,
        discovered=POLARIS_MODULE_SUM,
        denominator_basis=BASIS_PARTIAL,
        denominator_unmeasured_modules=12,
        denominator_module_total=POLARIS_MODULE_TOTAL,
        denominator_bare_total=POLARIS_BARE_TOTAL,
    )

    sealed = stats.model_dump(mode="json")
    assert sealed["denominator_basis"] == BASIS_PARTIAL
    assert sealed["denominator_unmeasured_modules"] == 12
    assert sealed["denominator_module_total"] == POLARIS_MODULE_TOTAL
    assert sealed["denominator_bare_total"] == POLARIS_BARE_TOTAL


def test_an_unmeasured_basis_stays_an_absent_key():
    sealed = _stats(executed=1, discovered=1).model_dump(mode="json")

    for key in (
        "denominator_basis",
        "denominator_unmeasured_modules",
        "denominator_module_total",
        "denominator_bare_total",
    ):
        assert key not in sealed


def test_a_census_disagreement_never_downgrades_the_run():
    """It grades the harness's bookkeeping, not what the run executed."""
    assert CENSUS_CONFLICT in ADJUDICATED_CONFLICTS
    # Historical malformed fractions still keep their strict validation cap.
    assert UNBOUNDED_CONFLICT not in ADJUDICATED_CONFLICTS


# --------------------------------------------------------------------------- #
# The producer is wired: one seam decides `discovered`, and it seals its basis.
# --------------------------------------------------------------------------- #


def _validator_status(monkeypatch, metrics: dict, *, collected=None) -> dict:
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.project_path = "/workspace"
    monkeypatch.setattr(
        PhysicalValidator, "parse_test_reports_with_catalog", lambda self, project_dir: metrics
    )
    monkeypatch.setattr(PhysicalValidator, "_python_collected_count", lambda self, name: collected)
    return validator.validate_test_status("polaris")


def _polaris_metrics() -> dict:
    return {
        "valid": True,
        "total_tests": POLARIS_EXECUTED,
        "passed_tests": POLARIS_EXECUTED - 52,
        "failed_tests": 52,
        "error_tests": 0,
        "skipped_tests": 0,
        "flaky_count": 0,
        "collection_errors": 0,
        "collection_errors_skipped": 0,
        "collection_error_summary": None,
        "report_files": ["/workspace/polaris/polaris-core/build/test-results/test/x.xml"],
        "metrics_conflicts": [],
        "parsing_errors": [],
        "catalog_test_count": POLARIS_BARE_TOTAL,
        "catalog_by_module": POLARIS_BY_MODULE,
        "catalog_module_total": POLARIS_MODULE_TOTAL,
    }


def test_validator_seals_the_module_sum_not_the_unexplained_total(monkeypatch):
    status = _validator_status(monkeypatch, _polaris_metrics())

    assert status["static_test_count"] == POLARIS_MODULE_SUM
    assert status["test_stats"]["discovered"] == POLARIS_MODULE_SUM
    assert status["test_stats"]["denominator_basis"] == BASIS_PARTIAL
    assert status["test_stats"]["denominator_unmeasured_modules"] == 12
    assert status["test_stats"]["denominator_bare_total"] == POLARIS_BARE_TOTAL
    assert CENSUS_CONFLICT in status["conflicts"]


def test_validator_seals_none_when_nothing_was_censused(monkeypatch):
    metrics = _polaris_metrics()
    for key in ("catalog_test_count", "catalog_by_module", "catalog_module_total"):
        metrics.pop(key)

    status = _validator_status(monkeypatch, metrics)

    assert status["static_test_count"] is None
    assert status["test_stats"]["discovered"] is None
    assert status["test_stats"]["denominator_basis"] == BASIS_NONE
    assert CENSUS_CONFLICT not in status["conflicts"]


@pytest.mark.parametrize("collected", [1927])
def test_validator_keeps_python_collection_priority(monkeypatch, collected):
    status = _validator_status(monkeypatch, _polaris_metrics(), collected=collected)

    assert status["static_test_count"] == collected
    assert status["test_stats"]["denominator_basis"] == BASIS_COMPLETE
    assert CENSUS_CONFLICT not in status["conflicts"]


def test_the_gate_rollup_carries_the_basis_into_the_snapshot(monkeypatch):
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.agent.phase_gates import _validated_test_rollup
    from sag.agent.verdict_finalizer import _fold_test_stats

    status = _validator_status(monkeypatch, _polaris_metrics())
    rollup = _validated_test_rollup(status)

    assert rollup["discovered"] == POLARIS_MODULE_SUM
    assert rollup["denominator_basis"] == BASIS_PARTIAL
    assert rollup["denominator_unmeasured_modules"] == 12

    state = RunEvidenceState(run_id="census-run")
    state.register_fact(StateScope.TEST_RUNTIME, "test.stats", rollup, "gate://test")
    stats, conflicts = _fold_test_stats(state)

    assert stats.discovered == POLARIS_MODULE_SUM
    assert stats.denominator_basis == BASIS_PARTIAL
    assert stats.denominator_unmeasured_modules == 12
    assert CENSUS_CONFLICT in conflicts
