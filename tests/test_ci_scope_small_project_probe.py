"""Offline checks of the manual real-build probe's comparison controls."""

from sag.metrics.attainment import CertificateView
from sag.metrics.target_record import CellTarget, TargetRecord
from scripts.ci_scope_small_project_probe import target_ablations


def test_scope_removal_changes_only_the_external_build_axis():
    cell = CellTarget(
        cell_id="JDK17",
        build="ok",
        executed_count=0,
        red_count=0,
        grade="B",
        modules=("a", "b"),
        modules_basis="log",
    )
    target = TargetRecord(
        repo="example/project",
        sha="a" * 40,
        harvested_at="2026-09-07T00:00:00Z",
        cells=(cell,),
        matched_cell=cell.cell_id,
    )
    view = CertificateView(
        repo=target.repo,
        target_sha=target.sha,
        authority_ok=True,
        counts_receipt_bound=True,
        build_ok=True,
        executed_count=2,
        red_count=0,
        modules=("a",),
    )
    result = target_ablations(view, target)
    assert result["full_target"]["alpha_build"] == {"numerator": 1, "denominator": 2}
    assert result["full_target"]["verdict"] == "partial"
    assert result["without_target_scope"]["build_form"] == "conclusion"
    assert result["without_target_scope"]["verdict"] == "partial"
    assert result["without_target_scope"]["alpha"] is None
    assert result["without_target_scope"]["alpha_build"] is None
    assert result["without_target"]["attainment"] is None
    for name in ("mismatched_repo", "mismatched_sha"):
        assert result[name]["verdict"] == "invalid"
        assert result[name]["alpha"] is None


def test_a_failed_physical_mutation_cannot_be_counted_as_an_ablation():
    from types import SimpleNamespace

    import pytest

    from scripts.ci_scope_small_project_probe import checked_control

    audit = SimpleNamespace(execute_control_command=lambda _: {"exit_code": 1})
    with pytest.raises(RuntimeError, match="control mutation failed"):
        checked_control(audit, "move experiment outputs")
