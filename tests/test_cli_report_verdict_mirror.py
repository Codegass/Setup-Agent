from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
    SnapshotTestStats,
)
from sag.main import _render_setup_cli_result


def _snapshot(verdict, conflicts=()):
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
    )


def _termination(delivery=ReportDeliveryStatus.DELIVERED):
    return RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=delivery,
    )


def test_snapshot_partial_is_rendered_literally_without_report_mirror():
    snapshot = _snapshot("partial", conflicts=("reactor_scope_narrowed",))

    output, exit_code = _render_setup_cli_result(snapshot, _termination(), "cayenne")

    assert "Verdict: PARTIAL" in output
    assert exit_code == 1


def test_snapshot_success_cannot_be_demoted_by_report_delivery_failure():
    snapshot = _snapshot("success", conflicts=())

    output, exit_code = _render_setup_cli_result(
        snapshot,
        _termination(ReportDeliveryStatus.FAILED),
        "demo",
    )

    assert "Verdict: SUCCESS" in output
    assert "report delivery failed" in output.lower()
    assert exit_code == 0


def test_snapshot_failed_cannot_be_promoted_by_completed_flow():
    output, exit_code = _render_setup_cli_result(
        _snapshot("failed"),
        _termination(),
        "demo",
    )

    assert "Verdict: FAILED" in output
    assert exit_code == 1
