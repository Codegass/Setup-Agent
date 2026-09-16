"""Each row states one measurement in the run's own words."""

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.models import ResultStats
from sag.result_card.rows import setup_row, task_row

from result_card_fakes import RUN_ID, phase_record, snapshot_dict


def _snapshot(**overrides) -> RunVerdictSnapshot:
    return RunVerdictSnapshot.model_validate(snapshot_dict(**overrides))


def _termination(
    status: RunTerminationStatus = RunTerminationStatus.COMPLETED,
    delivery: ReportDeliveryStatus = ReportDeliveryStatus.DELIVERED,
) -> RunTermination:
    return RunTermination(termination=status, report_delivery_status=delivery)


def test_setup_row_counts_the_run():
    stats = ResultStats(
        phases_completed=5, phases_total=5, turns=12, tool_calls=20, wall_clock_seconds=390.2
    )
    row = setup_row(_snapshot(), stats=stats, termination=_termination())
    assert row.status == "success"
    assert row.tone == "success"
    assert row.headline == "5/5 phases · 12 turns · 20 tool calls · 6m 30s"
    assert row.detail is None
    assert row.reason is None


def test_setup_row_drops_pieces_it_does_not_have():
    row = setup_row(_snapshot(), stats=ResultStats(turns=3), termination=_termination())
    assert row.headline == "3 turns"


def test_setup_row_says_so_when_it_counted_nothing():
    row = setup_row(_snapshot(), stats=ResultStats(), termination=_termination())
    assert row.headline == "no run counts were recorded"


def test_setup_row_names_an_abnormal_ending():
    row = setup_row(
        _snapshot(verdict="partial"),
        stats=ResultStats(turns=9),
        termination=_termination(RunTerminationStatus.ABORTED),
    )
    assert row.status == "partial"
    assert row.tone == "attention"
    assert row.detail == "the run was aborted"


def test_setup_row_names_a_blocked_phase():
    row = setup_row(
        _snapshot(
            verdict="partial",
            phase_records=[
                phase_record("provision"),
                phase_record(
                    "build",
                    outcome="failed",
                    termination="blocked",
                    reason="enforcer rejected Maven 3.8.7",
                ),
            ],
        ),
        stats=ResultStats(turns=9),
        termination=_termination(),
    )
    assert row.detail == "blocked at build: enforcer rejected Maven 3.8.7"


def test_failed_verdict_is_a_failed_tone():
    row = setup_row(_snapshot(verdict="failed"), stats=ResultStats(), termination=_termination())
    assert row.tone == "failed"


def test_unknown_verdict_asks_for_attention():
    row = setup_row(_snapshot(verdict="unknown"), stats=ResultStats(), termination=_termination())
    assert row.tone == "attention"


def test_task_row_reports_a_complete_task():
    row = task_row(_snapshot())
    assert row.status == "complete"
    assert row.tone == "success"
    assert row.headline == "complete 1/1 steps"
    assert row.detail == "mvn clean verify → exit 0"
    assert row.items == ("smoke-build-test: complete — mvn clean verify → exit 0",)
    assert row.refs == ("inv-maven-1-ee86ae186d94-0002",)


def test_task_row_reports_a_failed_step_with_its_reason():
    snapshot = _snapshot(
        verdict="partial",
        task_completion={
            "run_id": RUN_ID,
            "task_sha256": "b" * 64,
            "status": "incomplete",
            "steps": [
                {
                    "id": "ci-step-1",
                    "command": "mvn -B clean test",
                    "status": "failed",
                    "receipt_id": "inv-maven-1-abc-0001",
                    "exit_code": 1,
                    "reason": "Required command returned a nonzero exit code.",
                },
                {
                    "id": "ci-step-2",
                    "command": "mvn -B verify",
                    "status": "missing",
                    "receipt_id": None,
                    "exit_code": None,
                    "reason": None,
                },
            ],
            "reasons": [],
        },
    )
    row = task_row(snapshot)
    assert row.status == "incomplete"
    assert row.tone == "failed"
    assert row.headline == "incomplete 0/2 steps"
    assert row.items == (
        "ci-step-1: failed — mvn -B clean test → exit 1; "
        "Required command returned a nonzero exit code.",
        "ci-step-2: missing — mvn -B verify",
    )


def test_task_row_glosses_its_unavailable_reason():
    snapshot = _snapshot(
        verdict="partial",
        task_completion={
            "run_id": RUN_ID,
            "task_sha256": None,
            "status": "unavailable",
            "steps": [],
            "reasons": ["task_current_checkout_mismatch"],
        },
    )
    row = task_row(snapshot)
    assert row.status == "unavailable"
    assert row.tone == "attention"
    assert row.headline == "step definition unavailable"
    assert row.reason == (
        "the workspace is not at the task's frozen commit (task_current_checkout_mismatch)"
    )


def test_task_row_is_neutral_when_no_task_was_supplied():
    snapshot_payload = snapshot_dict()
    snapshot_payload.pop("task_completion")
    row = task_row(RunVerdictSnapshot.model_validate(snapshot_payload))
    assert row.status == "not supplied"
    assert row.tone == "neutral"
    assert row.headline == "no required task was supplied"
    assert row.reason == "run with --acceptance-task-file to require specific commands"
