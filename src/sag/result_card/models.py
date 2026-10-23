"""The presentation model of a finished run. Shape only; no derivation here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from sag.agent.verdict_dimensions import (
    CICommand,
    FirstUnpassedStep,
    OfficialCI,
    RunControl,
    SagCheck,
)

RowKey = Literal[
    "ci_command",
    "official_ci",
    "unconfirmed",
    "run",
    "summary",
    "coverage",
    "report",
    # Fact rows: built by `sag.result_card.rows` and folded into the card's
    # rows above, never placed on a card themselves.
    "setup",
    "task",
    "build",
    "tests",
    "ci",
]
Tone = Literal["success", "attention", "failed", "neutral"]

# Reading order, fixed so three surfaces and successive runs line up: what the
# project's CI command did, how that compares with the official CI run, what
# SAG could not confirm, how the run ended, and only then the one word derived
# from those lines (spec 2026-10-22).
ROW_ORDER: tuple[RowKey, ...] = (
    "ci_command",
    "official_ci",
    "unconfirmed",
    "run",
    "summary",
    "coverage",
    "report",
)

ROW_LABELS: dict[RowKey, str] = {
    "ci_command": "CI command",
    "official_ci": "Official CI",
    "unconfirmed": "Not confirmed by SAG",
    "run": "Run",
    "summary": "Summary",
    "coverage": "Coverage",
    "report": "Report",
    "setup": "Setup",
    "task": "Required task",
    "build": "Build",
    "tests": "Tests",
    "ci": "Official CI",
}


class _Frozen(BaseModel):
    """Read-only, and camelCase on the wire.

    ``alias_generator`` is what the web API serves under: everything else the
    API returns is camelCase, and a single snake_case body inside a camelCase
    envelope is a trap for every later reader. ``populate_by_name`` keeps the
    Python side unchanged — the terminal block, the report and every builder in
    this package construct and read by field name, and a card already written
    to disk in field-name form still validates.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )


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
    #: What the ADVISOR spent, kept apart from what the executor spent. Two
    #: models answered, and a single added total would say the first spent it
    #: all — hiding which model the bill went to and what turning the advisor
    #: off would save. `advisor_model` names that second model when the run pin
    #: recorded one, which not every run does; these two counts come from the
    #: turns themselves and stand whether it did or not.
    advisor_tokens_in: int | None = None
    advisor_tokens_out: int | None = None
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


# Reuse the sealed measurements and their validation. Only the wire aliases
# differ: the card API is camelCase, including its nested records.
class ResultFirstUnpassedStep(FirstUnpassedStep, _Frozen):
    pass


class ResultCICommand(CICommand, _Frozen):
    first_unpassed_step: ResultFirstUnpassedStep | None = None


class ResultOfficialCI(OfficialCI, _Frozen):
    pass


class ResultSagCheck(SagCheck, _Frozen):
    pass


class ResultRunControl(RunControl, _Frozen):
    pass


class ResultBuild(_Frozen):
    fact: ResultRow
    modules_built: int | None
    modules_total: int | None
    class_files: int | None
    jars: int | None


class ResultTestCounts(_Frozen):
    executed: int
    passed: int
    failed: int
    errors: int
    skipped: int


class ResultTests(_Frozen):
    fact: ResultRow
    counts: ResultTestCounts | None
    lower_bound: bool = False
    raw_executions: int | None = None
    flaky_count: int = 0


class ResultDimensions(_Frozen):
    """Measurements for charts; copied from the same inputs as the text rows."""

    ci_command: ResultCICommand | None
    official_ci: ResultOfficialCI
    sag_checks: tuple[ResultSagCheck, ...]
    run_control: ResultRunControl
    # Local evidence is independent of a supplied task or official CI record.
    # Optional for cards saved before the Workbench exposed these measurements.
    build: ResultBuild | None = None
    tests: ResultTests | None = None
    has_ci_reference: bool | None = None


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
    # Optional so previously saved cards still open without reconstructing
    # measurements from their human-readable sentences.
    dimensions: ResultDimensions | None = None
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
        """The whole card reads as its summary row: the word derived from the rest."""

        return self.row("summary").tone

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
    "ResultDimensions",
    "ResultStats",
    "RowKey",
    "RunResultCard",
    "Tone",
]
