"""The presentation model of a finished run. Shape only; no derivation here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

RowKey = Literal["setup", "task", "build", "tests", "coverage", "ci", "report"]
Tone = Literal["success", "attention", "failed", "neutral"]

# Reading order, fixed so three surfaces and successive runs line up.
ROW_ORDER: tuple[RowKey, ...] = (
    "setup",
    "task",
    "build",
    "tests",
    "coverage",
    "ci",
    "report",
)

ROW_LABELS: dict[RowKey, str] = {
    "setup": "Setup",
    "task": "Required task",
    "build": "Build",
    "tests": "Tests",
    "coverage": "Coverage",
    "ci": "Official CI",
    "report": "Report",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResultRow(_Frozen):
    """One measurement, stated once.

    ``status`` is the word the run recorded, never a synonym. ``reason`` is
    present exactly when the row has nothing to measure, so a reader never has
    to guess whether a blank means zero or means unknown.
    """

    key: RowKey
    label: str
    status: str
    tone: Tone
    headline: str
    detail: str | None = None
    reason: str | None = None
    items: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()


class ResultStats(_Frozen):
    """How the run went, as counts. Every field may be absent."""

    phases_completed: int | None = None
    phases_total: int | None = None
    turns: int | None = None
    tool_calls: int | None = None
    tool_failures: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    wall_clock_seconds: float | None = None
    model: str | None = None
    advisor_model: str | None = None


class AttentionItem(_Frozen):
    kind: Literal[
        "failing_tests",
        "task_step",
        "blocked_phase",
        "ci_finding",
        "build_warning",
        "report",
    ]
    title: str
    detail: str | None = None
    refs: tuple[str, ...] = ()


class RunResultCard(_Frozen):
    schema_version: Literal[1] = 1
    run_id: str
    project: str | None = None
    goal: str | None = None
    commit: str | None = None
    container: str | None = None
    session_dir: str | None = None
    verdict: Literal["success", "partial", "failed", "unknown"]
    verdict_source: Literal["snapshot", "legacy", "unavailable"]
    rows: tuple[ResultRow, ...]
    stats: ResultStats
    attention: tuple[AttentionItem, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _carries_every_row_once_in_order(self) -> "RunResultCard":
        keys = tuple(row.key for row in self.rows)
        if keys != ROW_ORDER:
            raise ValueError(
                f"a card states all seven rows in reading order; got {keys}"
            )
        return self

    @property
    def tone(self) -> Tone:
        """The whole card reads as its setup row: one word for the run."""

        return self.rows[0].tone

    def row(self, key: RowKey) -> ResultRow:
        for row in self.rows:
            if row.key == key:
                return row
        raise KeyError(key)


__all__ = [
    "ROW_LABELS",
    "ROW_ORDER",
    "AttentionItem",
    "ResultRow",
    "ResultStats",
    "RowKey",
    "RunResultCard",
    "Tone",
]
