from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestStats,
)
from sag.main import _render_setup_cli_result


def _snapshot(verdict, conflicts=()):
    build_modules = {
        "success": {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1},
        "partial": {"rate": 90.0, "band": "most", "numerator": 9, "denominator": 10},
        "failed": {"rate": 0.0, "band": "none", "numerator": 0, "denominator": 1},
    }[verdict]
    return RunVerdictSnapshot(
        run_id=f"cli-mirror-{verdict}",
        finalized_at="2026-07-17T12:00:00Z",
        verdict=verdict,
        conflicts=tuple(conflicts),
        test_stats=SnapshotTestStats(
            discovered=286,
            executed=286,
            passed=284,
            failed=2,
        ),
        rates={
            "build": {
                "modules": build_modules,
                "classes": {"band": "unavailable", "reason": "fixture class census unavailable"},
            },
            "test": {
                "cases": {"rate": 100.0, "band": "fully", "numerator": 286, "denominator": 286},
                "modules": {"band": "unavailable", "reason": "fixture test survey unavailable"},
            },
            "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
        },
    )


def _termination(delivery=ReportDeliveryStatus.DELIVERED):
    return RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=delivery,
    )


def test_snapshot_partial_is_rendered_literally_without_report_mirror():
    snapshot = _snapshot("partial", conflicts=("reactor_scope_narrowed",))

    output, exit_code = _render_setup_cli_result(snapshot, _termination(), "cayenne")

    assert "Setup verdict: partial" in output
    assert exit_code == 1


def test_snapshot_success_cannot_be_demoted_by_report_delivery_failure():
    snapshot = _snapshot("success", conflicts=())

    output, exit_code = _render_setup_cli_result(
        snapshot,
        _termination(ReportDeliveryStatus.FAILED),
        "demo",
    )

    assert " Setup         [green]success[/green]" in output
    assert "Setup verdict" not in output
    assert "the setup report was not written" in output
    assert exit_code == 0


def test_snapshot_failed_cannot_be_promoted_by_completed_flow():
    output, exit_code = _render_setup_cli_result(
        _snapshot("failed"),
        _termination(),
        "demo",
    )

    assert "Setup verdict: failed" in output
    assert exit_code == 1
