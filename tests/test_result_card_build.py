"""Assembly: seven rows, what needs attention, and what the run noted."""

from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
    RunVerdictSnapshot,
)
from sag.result_card.build import build_result_card
from sag.result_card.models import ROW_ORDER

from result_card_fakes import (
    RUN_ID,
    evaluated_ci_comparison,
    module_metrics,
    phase_record,
    snapshot_dict,
)


def _termination(delivery=ReportDeliveryStatus.DELIVERED) -> RunTermination:
    return RunTermination(
        termination=RunTerminationStatus.COMPLETED, report_delivery_status=delivery
    )


def test_card_from_a_clean_run():
    card = build_result_card(
        snapshot_dict(),
        module_metrics=module_metrics(),
        termination=_termination(),
        project="commons-cli",
        container="sag-commons-cli",
        turn_count=12,
        tool_calls=20,
        tool_failures=2,
        trajectory_session={"wall_clock_seconds": 390.2},
    )
    assert card.verdict == "success"
    assert card.verdict_source == "snapshot"
    assert tuple(row.key for row in card.rows) == ROW_ORDER
    assert card.tone == "success"
    assert card.stats.turns == 12
    assert card.stats.tool_failures == 2
    assert card.row("setup").headline == "12 turns · 20 tool calls · 6m 30s"
    assert card.attention == ()
    assert card.notes == ()


def test_card_accepts_a_model_as_well_as_a_dict():
    snapshot = RunVerdictSnapshot.model_validate(snapshot_dict())
    assert build_result_card(snapshot).verdict == "success"


def test_phase_counts_come_from_the_phase_records():
    card = build_result_card(
        snapshot_dict(
            phase_records=[
                phase_record("provision"),
                phase_record("analyze"),
                phase_record("build", outcome="failed", termination="blocked", reason="no JDK"),
            ]
        )
    )
    assert card.stats.phases_total == 3
    assert card.stats.phases_completed == 2


def test_model_names_come_from_the_run_pin():
    card = build_result_card(
        snapshot_dict(),
        run_pin={"action_model": "gpt-5.4-mini", "advisor": {"model": "openai/gpt-5.6-terra"}},
    )
    assert card.stats.model == "gpt-5.4-mini"
    assert card.stats.advisor_model == "openai/gpt-5.6-terra"


def test_tokens_come_from_the_token_usage_rows():
    card = build_result_card(
        snapshot_dict(),
        token_usage=[{"prompt_tokens": 100, "output_tokens": 7}, {"prompt_tokens": 50}],
    )
    assert card.stats.tokens_in == 150
    assert card.stats.tokens_out == 7


def test_attention_leads_with_incomplete_task_steps():
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            task_completion={
                "run_id": RUN_ID,
                "task_sha256": "c" * 64,
                "status": "incomplete",
                "steps": [
                    {
                        "id": "ci-step-1",
                        "command": "mvn verify",
                        "status": "failed",
                        "receipt_id": "inv-1",
                        "exit_code": 1,
                        "reason": "Required command returned a nonzero exit code.",
                    }
                ],
                "reasons": [],
            },
        )
    )
    first = card.attention[0]
    assert first.kind == "task_step"
    assert first.title == "Required task step ci-step-1 failed"
    assert first.detail == "mvn verify → exit 1"
    assert first.refs == ("inv-1",)


def test_attention_names_a_blocked_phase_then_failing_modules():
    metrics = module_metrics()
    metrics["modules"][0].update(
        {
            "tests_failed": 2,
            "failing_count": 2,
            "failing_names": ["a.BTest#one", "a.BTest#two"],
        }
    )
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            phase_records=[
                phase_record("build", outcome="failed", termination="blocked", reason="no JDK")
            ],
        ),
        module_metrics=metrics,
    )
    kinds = [item.kind for item in card.attention]
    assert kinds[0] == "blocked_phase"
    assert "failing_tests" in kinds
    failing = next(item for item in card.attention if item.kind == "failing_tests")
    assert failing.title == "commons-cli · 2 failing"
    assert failing.detail == "a.BTest#one, a.BTest#two"


def test_failing_names_are_capped_with_a_remainder():
    metrics = module_metrics()
    names = [f"a.BTest#m{index}" for index in range(8)]
    metrics["modules"][0].update(
        {"tests_failed": 8, "failing_count": 8, "failing_names": names}
    )
    card = build_result_card(snapshot_dict(verdict="partial"), module_metrics=metrics)
    failing = next(item for item in card.attention if item.kind == "failing_tests")
    assert failing.detail.endswith("+3 more")


def test_one_module_metrics_payload_reaches_every_reader_of_it():
    """A read-only view of the file is the same file, to both readers of it.

    `build_row` and `_attention` each narrow the payload themselves. While one
    accepted any `Mapping` and the other only a `dict`, a caller handing over a
    `MappingProxyType` got a card that raised the file's failing-test bullets
    and dropped the file's jar count out of the build row in the same breath —
    one artifact, two answers, and no failure anywhere to say so.
    """

    from types import MappingProxyType

    metrics = module_metrics()
    metrics["modules"][0].update(
        {"tests_failed": 2, "failing_count": 2, "failing_names": ["a.BTest#one"]}
    )
    payload = snapshot_dict(verdict="partial")
    plain = build_result_card(payload, module_metrics=metrics)
    frozen = build_result_card(payload, module_metrics=MappingProxyType(metrics))

    assert "4 jars" in plain.row("build").detail
    assert frozen.row("build") == plain.row("build")
    assert frozen.attention == plain.attention


def test_a_failed_report_delivery_needs_attention():
    card = build_result_card(
        snapshot_dict(), termination=_termination(ReportDeliveryStatus.FAILED)
    )
    assert card.attention[-1].kind == "report"


def test_notes_are_glossed_conflicts_in_order_without_repeats():
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            conflicts=["test_reports_stale", "test_reports_stale", "test_execution_interrupted"],
        )
    )
    assert card.notes == (
        "some test reports were rewritten after being read and were set aside",
        "the test run stopped before it finished; the counts are what it reached",
    )


def test_a_ci_finding_that_names_work_needs_attention():
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            ci_comparison=evaluated_ci_comparison(
                verdict="not_met",
                clean=False,
                red_observed=3,
                unexpected_red_ids=["a.B#c", "a.B#d", "a.B#e"],
                reason_codes=["NEW_RED_BEYOND_TARGET"],
            ),
        )
    )
    finding = next(item for item in card.attention if item.kind == "ci_finding")
    assert finding.title == "NEW_RED_BEYOND_TARGET: tests failed here that pass in CI"


def test_a_current_record_names_its_source():
    card = build_result_card(snapshot_dict(verdict="unknown", schema_version=5))
    assert card.verdict == "unknown"
    assert card.verdict_source == "snapshot"


def test_an_older_record_is_read_as_the_version_it_carries():
    # A v3 record is read additively and keeps the version it was written with,
    # so the card says where its words came from rather than assuming.
    payload = snapshot_dict(schema_version=3)
    payload.pop("rates")
    card = build_result_card(payload)
    assert card.verdict_source == "legacy"
