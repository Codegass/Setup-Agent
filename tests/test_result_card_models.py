"""The card's shape: seven rows in one order, absence stated, nothing coerced."""

import pytest
from pydantic import ValidationError

from sag.result_card.models import (
    ROW_LABELS,
    ROW_ORDER,
    AttentionItem,
    ResultRow,
    ResultStats,
    RunResultCard,
)


def _row(key: str) -> ResultRow:
    return ResultRow(key=key, label=ROW_LABELS[key], status="unknown", tone="neutral", headline="—")


def _card(**overrides) -> RunResultCard:
    base = dict(
        run_id="run-1",
        verdict="success",
        verdict_source="snapshot",
        rows=tuple(_row(key) for key in ROW_ORDER),
        stats=ResultStats(),
    )
    base.update(overrides)
    return RunResultCard(**base)


def test_row_order_is_the_reading_order():
    assert ROW_ORDER == ("setup", "task", "build", "tests", "coverage", "ci", "report")


def test_every_row_key_has_a_label():
    assert set(ROW_LABELS) == set(ROW_ORDER)
    assert ROW_LABELS["task"] == "Required task"
    assert ROW_LABELS["ci"] == "Official CI"


def test_card_requires_exactly_the_seven_rows_in_order():
    card = _card()
    assert tuple(row.key for row in card.rows) == ROW_ORDER


def test_card_rejects_a_missing_row():
    rows = tuple(_row(key) for key in ROW_ORDER if key != "coverage")
    with pytest.raises(ValidationError, match="seven rows"):
        _card(rows=rows)


def test_card_rejects_rows_out_of_order():
    rows = tuple(_row(key) for key in reversed(ROW_ORDER))
    with pytest.raises(ValidationError, match="seven rows"):
        _card(rows=rows)


def test_row_finder_returns_the_named_row():
    card = _card()
    assert card.row("tests").key == "tests"


def test_card_tone_is_the_setup_rows_tone():
    rows = list(_row(key) for key in ROW_ORDER)
    rows[0] = ResultRow(
        key="setup", label="Setup", status="partial", tone="attention", headline="—"
    )
    assert _card(rows=tuple(rows)).tone == "attention"


def test_stats_default_to_stated_absence():
    stats = ResultStats()
    assert stats.turns is None
    assert stats.wall_clock_seconds is None


def test_models_are_frozen():
    row = _row("build")
    with pytest.raises(ValidationError):
        row.headline = "changed"


def test_attention_item_carries_its_refs():
    item = AttentionItem(kind="task_step", title="ci-step-1: failed", refs=("inv-maven-1-a-0001",))
    assert item.detail is None
    assert item.refs == ("inv-maven-1-a-0001",)


def test_overriding_the_run_id_keeps_the_nested_records_with_it():
    """A snapshot rejects a task completion belonging to another run."""

    from sag.agent.verdict_finalizer import RunVerdictSnapshot

    from result_card_fakes import snapshot_dict

    payload = snapshot_dict(run_id="20260101_000000_000000_abcdef123456_1-2-deadbeef")
    snapshot = RunVerdictSnapshot.model_validate(payload)
    assert snapshot.task_completion.run_id == snapshot.run_id
    assert snapshot.ci_comparison.run_id == snapshot.run_id


def test_an_explicit_nested_override_still_wins():
    """A caller who supplies its own task completion has said what it wants."""

    from result_card_fakes import RUN_ID, snapshot_dict

    payload = snapshot_dict(
        run_id="20260101_000000_000000_abcdef123456_1-2-deadbeef",
        task_completion={"run_id": RUN_ID, "task_sha256": None, "status": "unavailable",
                         "steps": [], "reasons": ["task_run_pin_unavailable"]},
    )
    assert payload["task_completion"]["run_id"] == RUN_ID


def test_the_card_body_serializes_camel_case_like_the_rest_of_the_api():
    """The API serves camelCase around the card; the body speaks it too.

    Before this, `resultCard` was the only aliased key and everything inside it
    arrived snake_case, so a reader had to switch conventions mid-object.
    """

    card = _card(
        session_dir="logs/session_recorded",
        stats=ResultStats(wall_clock_seconds=1.5, tool_calls=3, phases_completed=5),
    )

    dumped = card.model_dump(mode="json", by_alias=True)

    assert dumped["schemaVersion"] == 1
    assert dumped["runId"] == "run-1"
    assert dumped["verdictSource"] == "snapshot"
    assert dumped["sessionDir"] == "logs/session_recorded"
    assert dumped["stats"]["wallClockSeconds"] == 1.5
    assert dumped["stats"]["toolCalls"] == 3
    assert dumped["stats"]["phasesCompleted"] == 5
    assert [key for key in dumped if "_" in key] == []
    assert [key for key in dumped["stats"] if "_" in key] == []


def test_the_card_reads_back_from_the_shape_it_serves():
    """A camelCase body validates back into the same card, field for field."""

    card = _card(session_dir="logs/session_recorded", stats=ResultStats(tool_calls=3))

    assert RunResultCard.model_validate(card.model_dump(mode="json", by_alias=True)) == card


def test_python_side_construction_keeps_its_field_names():
    """Every caller in this repository builds the card by field name."""

    card = _card(session_dir="logs/session_recorded")

    assert card.session_dir == "logs/session_recorded"
    assert card.verdict_source == "snapshot"
    assert RunResultCard.model_validate(card.model_dump(mode="json")).run_id == "run-1"
