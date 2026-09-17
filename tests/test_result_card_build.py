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


def test_every_schema_this_run_can_no_longer_re_read_is_a_reconstruction():
    """The provenance word is pinned to the finalizer, not to a copied number.

    A v4 seal used to call itself a reading while the reader that authorizes
    seals could not re-read it at all — which is how one surface came to state
    a result and another to deny one existed for the same file. 184 of the 681
    archived records are v4.
    """

    from sag.agent.verdict_finalizer import VERDICT_SCHEMA_VERSION

    for version in range(3, VERDICT_SCHEMA_VERSION):
        payload = snapshot_dict(schema_version=version)
        payload.pop("rates", None)
        assert build_result_card(payload).verdict_source == "legacy", (
            f"a v{version} record is older than the v{VERDICT_SCHEMA_VERSION} seal this system "
            "writes, so the card states it was reconstructed"
        )
    assert build_result_card(
        snapshot_dict(schema_version=VERDICT_SCHEMA_VERSION)
    ).verdict_source == "snapshot"


def _red_test_stats(*, failed: int, errors: int) -> dict:
    """The record's own test counts, with some of them red."""

    counts = {
        "executed": 994,
        "passed": 994 - failed - errors - 61,
        "failed": failed,
        "errors": errors,
        "skipped": 61,
    }
    return {
        "discovered": 472,
        "denominator_basis": "complete",
        "unique": dict(counts),
        "raw": dict(counts),
        "flaky_count": 0,
        "judgment": "partial",
        "collection_errors": 0,
        "receipt_scoped": True,
    }


def test_a_run_whose_tests_went_red_is_named_under_attention():
    """One notion of attention, derived once, so four surfaces cannot disagree.

    The only `failing_tests` item the card could build came from
    `module_metrics["modules"][*]["failing_count"]`, and `module_metrics.json`
    occurs 0 times in 792 archived campaign directories — so that branch has
    never fired on a real run. Meanwhile the dashboard rail flags a row from
    `card.stats`' own failed + errors. The rail pointed at runs whose Overview
    said nothing needed attention.
    """

    card = build_result_card(
        snapshot_dict(verdict="partial", test_stats=_red_test_stats(failed=8, errors=3))
    )

    item = next(item for item in card.attention if item.kind == "failing_tests")
    assert "8" in item.title and "3" in item.title
    assert card.row("tests").headline.startswith("994 executed")


def test_the_attention_item_counts_what_the_tests_row_counts():
    """Both read `test_stats.unique`, so the two lines cannot state different numbers."""

    for failed, errors in ((2, 0), (0, 5), (7, 1)):
        card = build_result_card(
            snapshot_dict(verdict="partial", test_stats=_red_test_stats(failed=failed, errors=errors))
        )
        titles = [item.title for item in card.attention if item.kind == "failing_tests"]
        assert len(titles) == 1, f"{failed} failed / {errors} errors named {titles}"
        if failed:
            assert f"{failed:,} failed" in titles[0]
        if errors:
            assert f"{errors:,}" in titles[0]


def _failing_items(card) -> list:
    return [item for item in card.attention if item.kind == "failing_tests"]


def test_a_run_with_no_red_tests_is_not_named():
    # The red control is built by the same call, so an empty list here cannot
    # pass merely because the derivation is missing.
    assert _failing_items(
        build_result_card(snapshot_dict(verdict="partial", test_stats=_red_test_stats(failed=1, errors=0)))
    )
    assert _failing_items(build_result_card(snapshot_dict())) == []


def test_a_run_that_recorded_no_test_outcomes_is_not_named():
    """Absence is not zero and it is not a failure: nothing to flag either way."""

    assert _failing_items(
        build_result_card(snapshot_dict(verdict="partial", test_stats=_red_test_stats(failed=1, errors=0)))
    )
    card = build_result_card(
        snapshot_dict(
            verdict="partial",
            test_stats={
                "discovered": 0,
                "unique": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
                "raw": {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
                "flaky_count": 0,
                "judgment": "unknown",
            },
        )
    )
    assert _failing_items(card) == []
