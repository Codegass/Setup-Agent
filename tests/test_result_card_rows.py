"""Each row states one measurement in the run's own words."""

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.models import ResultStats
from sag.result_card.rows import build_row, coverage_row, setup_row, task_row, tests_row

from result_card_fakes import (
    CLEAN_TEST_COUNTS,
    RUN_ID,
    module_metrics,
    phase_record,
    snapshot_dict,
)


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


def test_build_row_counts_modules_and_names_its_diagnostics():
    row = build_row(_snapshot(), module_metrics=module_metrics())
    assert row.status == "success"
    assert row.tone == "success"
    assert row.headline == "1/1 modules built"
    assert row.detail == (
        "119 class files · 4 jars · counts are diagnostic, CI defines scope"
    )


def test_build_row_without_a_reactor_count_states_the_word():
    snapshot = _snapshot(
        build_evidence={
            "observed": True,
            "green": False,
            "judgment": "failed",
            "source": "physical",
            "outcome": "failed",
            "evidence_status": "verified",
            "refs": [],
            "compiled_classes": None,
        }
    )
    row = build_row(snapshot)
    assert row.status == "failed"
    assert row.tone == "failed"
    assert row.headline == "failed"
    assert row.detail is None


def test_build_row_unknown_judgment_states_a_reason():
    snapshot = _snapshot(
        verdict="unknown",
        build_evidence={
            "observed": False,
            "green": False,
            "judgment": "unknown",
            "source": "none",
            "outcome": "unknown",
            "evidence_status": "unknown",
            "refs": [],
        },
    )
    row = build_row(snapshot)
    assert row.status == "unknown"
    assert row.reason == "no build result was recorded for this run"


def test_tests_row_reports_counts_and_the_non_skipped_rate():
    row = tests_row(_snapshot())
    assert row.status == "executed"
    assert row.tone == "success"
    assert row.headline == "994 executed · 933 passed · 0 failed · 0 errors · 61 skipped"
    assert row.detail == "100% of non-skipped passed"


def test_tests_row_never_rounds_red_up_to_a_full_hundred():
    counts = {"executed": 10_000, "passed": 9_999, "failed": 0, "errors": 1, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 10_000,
            "denominator_basis": "complete",
            "unique": counts,
            "raw": counts,
            "flaky_count": 0,
            "judgment": "success",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.tone == "attention"
    assert row.detail == "<100% of non-skipped passed"


def test_tests_row_notes_raw_executions_when_they_differ():
    raw = {"executed": 1_200, "passed": 1_139, "failed": 0, "errors": 0, "skipped": 61}
    snapshot = _snapshot(
        test_stats={
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": dict(CLEAN_TEST_COUNTS),
            "raw": raw,
            "flaky_count": 0,
            "judgment": "success",
            "receipt_scoped": True,
        }
    )
    row = tests_row(snapshot)
    assert row.detail == "100% of non-skipped passed · 1,200 raw executions"


def test_tests_row_states_a_lower_bound_from_the_evidence_accounting():
    report_metrics = {
        "schema_version": 2,
        "tests": {
            "claimed": {
                "receipt_executions": {
                    "executed": 2048,
                    "passed": 2000,
                    "failed": 48,
                    "errors": 0,
                    "skipped": 0,
                    "availability": "partial",
                    "bound": "lower",
                    "reason": "row disclosure truncated",
                }
            }
        },
    }
    row = tests_row(_snapshot(), report_metrics=report_metrics)
    assert row.headline.startswith("≥994 executed")


def test_tests_row_says_when_no_tests_ran():
    zero = {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": zero,
            "raw": zero,
            "flaky_count": 0,
            "judgment": "unknown",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.status == "unavailable"
    assert row.tone == "attention"
    assert row.headline == "no test results were recorded"
    assert row.reason == "the run recorded no test outcomes"


def test_interrupted_tests_keep_their_prefix_counts():
    counts = {"executed": 120, "passed": 118, "failed": 2, "errors": 0, "skipped": 0}
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 472,
            "denominator_basis": "partial",
            "unique": counts,
            "raw": counts,
            "flaky_count": 0,
            "judgment": "partial",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.status == "interrupted"
    assert row.tone == "attention"
    assert row.headline == "120 executed · 118 passed · 2 failed · 0 errors · 0 skipped"


def test_coverage_row_reports_a_collected_rate():
    rates = snapshot_dict()["rates"]
    rates["coverage"] = {"line_rate": 54.2, "source": "jacoco", "status": "collected"}
    row = coverage_row(_snapshot(rates=rates))
    assert row.status == "collected"
    assert row.headline == "54.2% line coverage"
    assert row.detail == "source jacoco"
    assert row.tone == "neutral"


def test_coverage_row_names_why_nothing_was_collected():
    row = coverage_row(_snapshot())
    assert row.status == "not collected"
    assert row.headline == "not collected"
    assert row.reason == "fixture coverage not collected"
