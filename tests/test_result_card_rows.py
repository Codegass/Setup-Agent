"""Each row states one measurement in the run's own words."""

from sag.agent.control_events import canonical_sha256
from sag.agent.java_success_certificates import evaluate_java_success_certificate
from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.models import ResultStats
from sag.result_card.rows import (
    build_row,
    ci_row,
    coverage_row,
    report_row,
    setup_row,
    task_row,
    tests_row,
)

from result_card_fakes import (
    CLEAN_TEST_COUNTS,
    RUN_ID,
    attainment,
    module_metrics,
    phase_record,
    snapshot_dict,
)
from test_java_success_certificates import _obligations, _payload, _scope


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


def test_setup_row_will_not_invent_a_phase_numerator():
    row = setup_row(
        _snapshot(), stats=ResultStats(phases_total=5, turns=3), termination=_termination()
    )
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


def test_build_row_will_not_invent_a_module_numerator():
    snapshot = _snapshot(
        build_evidence={
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "refs": [],
            "compiled_classes": 119,
            "reactor_modules_total": 4,
        }
    )
    row = build_row(snapshot)
    assert row.headline == "success"
    assert row.detail == (
        "module count not recorded · 119 class files · counts are diagnostic, CI defines scope"
    )


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


def test_tests_row_keeps_its_counts_and_says_the_judgment_is_missing():
    snapshot = _snapshot(
        verdict="partial",
        test_stats={
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": dict(CLEAN_TEST_COUNTS),
            "raw": dict(CLEAN_TEST_COUNTS),
            "flaky_count": 0,
            "judgment": "unknown",
            "receipt_scoped": True,
        },
    )
    row = tests_row(snapshot)
    assert row.status == "unavailable"
    assert row.tone == "attention"
    assert row.headline == "994 executed · 933 passed · 0 failed · 0 errors · 61 skipped"
    assert row.reason == "the run recorded test outcomes but no judgment about them"


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


# A comparison only reaches "evaluated" while it still holds the certificate and
# target digests that bound it, so the fixture carries a real certificate for
# this run. No row reads it; it is what the snapshot requires to exist at all.
_CI_TARGET_SHA = snapshot_dict()["ci_comparison"]["target_sha"]
_CI_SCOPE = _scope(run_id=RUN_ID, target_sha=_CI_TARGET_SHA)


def _bound_obligations(unit: str, ids: tuple[str, ...]):
    """`_obligations` stamps its own epoch; this run's scope carries its own."""
    return _obligations(unit, ids, ids, subject=_CI_SCOPE.subject).model_copy(
        update={"evidence_epoch": _CI_SCOPE.evidence_epoch}
    )


_CI_CERTIFICATE_INPUT = _payload(
    scope=_CI_SCOPE,
    build_steps=_bound_obligations("build_plan_step", ("build-1",)),
    build_units=_bound_obligations("single_maven_project", ("root",)),
    test_steps=_bound_obligations("test_plan_step", ("test-1",)),
    test_targets=_bound_obligations("single_test_project", ("root",)),
    evidence_items=_bound_obligations(
        "evidence_binding", ("plan", "build-receipt", "test-receipt")
    ),
)
_CI_CERTIFICATE = evaluate_java_success_certificate(_CI_CERTIFICATE_INPUT)


def _evaluated(**overrides) -> dict:
    comparison = dict(snapshot_dict()["ci_comparison"])
    comparison.update(
        {
            "status": "evaluated",
            "certificate": _CI_CERTIFICATE,
            "certificate_input_sha256": canonical_sha256(
                _CI_CERTIFICATE_INPUT.model_dump(mode="json")
            ),
            "target_record_sha256": canonical_sha256(
                {"repo": comparison["repo"], "sha": comparison["target_sha"]}
            ),
            "attainment": attainment(**overrides),
            "acceptance_command": "mvn -B -f pom.xml -V clean test --batch-mode",
            "receipt_ids": ["inv-maven-1-17c8a2e62d8a-0001"],
            "reasons": [],
        }
    )
    return comparison


def test_ci_row_reports_a_met_comparison_with_its_denominator():
    row = ci_row(_snapshot(ci_comparison=_evaluated()))
    assert row.status == "met"
    assert row.tone == "success"
    assert row.headline == "met 523/523"
    assert row.detail == (
        'cell "Apache Jenkins commons-dbutils Linux JDK 17 #455" · lifecycle equivalent'
    )


def test_ci_row_names_missing_lifecycle_phases():
    comparison = _evaluated(
        lifecycle_parity={
            "status": "not_equivalent",
            "form": "maven_phases",
            "ci_command": "mvn -V verify",
            "sag_commands": ["mvn test"],
            "ci_reach": "verify",
            "sag_reach": "test",
            "missing": ["package", "verify"],
            "extra": [],
        }
    )
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.detail.endswith("lifecycle not_equivalent · missing package, verify")


def test_ci_row_without_a_scope_score_says_so():
    # A build stated as a conclusion echoes no module universe, so the whole
    # scope fraction goes with it.
    comparison = _evaluated(
        verdict="partial",
        alpha=None,
        alpha_build=None,
        build_form="conclusion",
        modules_matched=0,
        modules_target=0,
        modules_basis=None,
    )
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.headline == "partial · scope score unavailable"
    assert row.tone == "attention"


def test_ci_row_lists_findings_with_their_glosses():
    comparison = _evaluated(
        verdict="not_met",
        clean=False,
        red_observed=3,
        unexpected_red_ids=["a.B#c", "a.B#d", "a.B#e"],
        reason_codes=["NEW_RED_BEYOND_TARGET"],
    )
    row = ci_row(_snapshot(ci_comparison=comparison))
    assert row.tone == "failed"
    assert row.items[0] == "NEW_RED_BEYOND_TARGET: tests failed here that pass in CI"
    assert row.items[1] == "red beyond CI: 3 tests"
    assert "a.B#c" in row.items[2]


def test_ci_row_not_compared_explains_itself():
    row = ci_row(_snapshot())
    assert row.status == "not compared"
    assert row.tone == "neutral"
    assert row.headline == "not compared"
    assert row.reason == (
        "no CI job on this commit matches the run's JDK and OS (official_ci_cell_not_matched)"
    )


def test_ci_row_without_any_comparison_at_all():
    payload = snapshot_dict()
    payload.pop("ci_comparison")
    row = ci_row(RunVerdictSnapshot.model_validate(payload))
    assert row.status == "not compared"
    assert row.reason == "no CI job was supplied to compare against"


def test_report_row_points_at_the_delivered_file():
    row = report_row(_termination(), report_path="logs/session_x/setup-report-1.md")
    assert row.status == "delivered"
    assert row.tone == "neutral"
    assert row.headline == "logs/session_x/setup-report-1.md"
    assert row.refs == ("logs/session_x/setup-report-1.md",)


def test_report_row_flags_a_failed_delivery():
    row = report_row(_termination(delivery=ReportDeliveryStatus.FAILED))
    assert row.status == "failed"
    assert row.tone == "attention"
    assert row.headline == "the setup report was not written"
    assert row.reason == "the run result itself is unchanged"


def test_report_row_without_a_termination_is_unavailable():
    row = report_row(None)
    assert row.status == "unavailable"
    assert row.reason == "the run did not record whether a report was written"
