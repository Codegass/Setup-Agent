# tests/test_kafka_build_scope_acceptance.py
"""The build axis measured on kafka's real evidence: CI pool vs SAG's tree.

CI's JUnit pool (run 27721225836 at 26b251a4) encodes 34 test-bearing modules
by Gradle project path; SAG's 2026-08-26 evidence tree encodes 19 by
directory.  18 agree under the canonical grammar; the observed `storage/api`
does not match CI's `storage/storage-api`. This fixture applies no verified
project-name/directory mapping and discloses that identity as unmatched.
Every number here was measured before it was pinned
(docs/superpowers/specs/2026-09-07-ci-defined-build-scope-design.md).
"""

import tarfile
from pathlib import Path

import pytest

from sag.metrics.attainment import CertificateView, Pair, evaluate_attainment
from sag.metrics.module_keys import module_keys
from sag.metrics.target_record import CellTarget, TargetRecord
from scripts.d3_harvest_target import read_pool

POOL = Path(
    "logs/ci-ground-truth-probe-20260827/kafka-run-27721225836/junit-xml-17-noflaky-nonew.zip"
)
TREE = Path("logs/gradle-evidence-20260830/kafka-d2r6-container/test-results.tar")

pytestmark = pytest.mark.skipif(
    not (POOL.exists() and TREE.exists()),
    reason="kafka CI pool and SAG evidence tar are archived under logs/ only",
)


def _sag_modules() -> tuple[str, ...]:
    prefixes = set()
    with tarfile.open(TREE) as tar:
        for member in tar.getmembers():
            if (
                member.isfile()
                and "/test-results/" in member.name
                and member.name.endswith(".xml")
                and "/binary/" not in member.name
            ):
                prefixes.add(member.name.split("/build/test-results/")[0])
    return module_keys(prefixes)


def test_kafka_build_axis_is_eighteen_of_thirty_four():
    reading = read_pool(POOL)
    assert len(reading.modules) == 34

    cell = CellTarget(
        cell_id="junit-xml-17-noflaky-nonew",
        build="ok",
        executed_count=len(reading.deconvolved.executed_ids),
        red_count=len(reading.deconvolved.final_red_ids),
        flaky_count=len(reading.deconvolved.flaky_ids),
        skipped=len(reading.deconvolved.final_skipped_ids),
        modules=reading.modules,
        modules_basis="test_bearing",
        grade="A",
    )
    target = TargetRecord(
        repo="apache/kafka",
        sha="26b251a451ce941d3d7a55e6487bcb7f16b5ad48",
        harvested_at="2026-08-27T00:00:00Z",
        cells=(cell,),
        matched_cell=cell.cell_id,
    )
    ours = _sag_modules()
    assert len(ours) == 19
    view = CertificateView(
        repo="apache/kafka",
        target_sha=target.sha,
        authority_ok=True,
        counts_receipt_bound=True,
        build_ok=True,
        executed_count=27_219,
        red_count=8,
        modules=ours,
    )

    result = evaluate_attainment(view, target)

    assert result.build_form == "modules"
    assert result.modules_basis == "test_bearing"
    assert result.alpha_build == Pair(numerator=18, denominator=34)
    assert result.modules_matched == 18
    assert len(result.missing_module_ids) == 16
    assert result.unmatched_observed_module_ids == ("storage/api",)
    assert result.built is False
    # The verdict is still decided by the reds CI does not have (E2b, 2026-09-03).
    assert result.verdict == "not_met"
