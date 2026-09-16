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
