"""Deconvolve JUnit retries into one final verdict per test execution.

A retry framework does not rewrite the failed attempt; it appends the retry
after it.  So a suite that reports ``tests="4"`` for three distinct tests is not
corrupt -- one test carries two ``<testcase>`` elements, the failure first and
the passing retry second.  Counting rows therefore double-counts retried tests,
and counting ``failures=`` attributes reports a red the run itself already
cleared.

**The last-entry rule**: for one execution, the FINAL attempt is the LAST entry
in document order.  That entry alone decides red or green; the earlier attempts
only mark the execution as flaky.

**A repeat is a retry only after a red attempt.**  Retry frameworks re-run
failed tests, never passing ones, so a repeated identity whose previous attempt
passed is not a retry at all: JUnit truncates long parameterized display names
with an ellipsis, and distinct parameter sets then render as the same string.
apache/kafka run 27721225836 carries 2237 such collisions -- ``Lz4CompressionTest``
alone renders 3460 executions under 2596 names.  Merging those would erase 2237
real tests and report 545 "flaky" tests that never once failed.  Repeats that
follow a green attempt are therefore kept as separate executions, distinguished
by an occurrence suffix and disclosed through ``duplicate_name_ids``.

Verified against that run's JDK17 main pool: 36264 ``<testcase>`` rows over 1807
suites deconvolve to 36259 executions, 5 flaky, and 0 finally red -- which is
why the run itself concluded green, and it matches the 5 failures the pool's own
``failures=`` attributes report.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TestOutcome = Literal["passed", "failed", "error", "skipped"]

_RED_OUTCOMES = frozenset({"failed", "error"})
_ACCEPTED_ROOTS = frozenset({"testsuite", "testsuites"})


class TestEntry(BaseModel):
    """One ``<testcase>`` element: one attempt at one test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Parameterized JUnit display names run long: apache/kafka run 27721225836
    # carries identities past 512 characters, so this bound is a sanity cap on
    # malformed XML, not the record layer's 512-character identity bound.
    test_id: str = Field(min_length=1, max_length=4_096)
    outcome: TestOutcome


class Deconvolved(BaseModel):
    """Per-execution verdicts after the last-entry rule is applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executed_ids: tuple[str, ...] = ()
    final_red_ids: tuple[str, ...] = ()
    flaky_ids: tuple[str, ...] = ()
    final_skipped_ids: tuple[str, ...] = ()
    raw_entry_count: int = Field(default=0, ge=0)
    duplicate_name_ids: tuple[str, ...] = ()


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _entry_outcome(case: ET.Element) -> TestOutcome:
    children = {_strip_namespace(child.tag) for child in case}
    if "failure" in children:
        return "failed"
    if "error" in children:
        return "error"
    if "skipped" in children:
        return "skipped"
    return "passed"


def parse_junit_entries(xml_bytes: bytes) -> tuple[TestEntry, ...]:
    """Return every ``<testcase>`` attempt in DOCUMENT ORDER.

    Document order is load-bearing: :func:`deconvolve` reads the last entry for
    an execution as its final attempt, so the entries must never be sorted or
    de-duplicated here.
    """

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"malformed JUnit XML: {exc}") from exc

    root_tag = _strip_namespace(root.tag)
    if root_tag not in _ACCEPTED_ROOTS:
        raise ValueError(f"unsupported JUnit root element: {root_tag}")

    entries: list[TestEntry] = []
    for case in root.iter():
        if _strip_namespace(case.tag) != "testcase":
            continue
        name = (case.get("name") or "").strip()
        if not name:
            raise ValueError("a JUnit testcase is missing its name")
        classname = (case.get("classname") or "").strip()
        test_id = f"{classname}#{name}" if classname else name
        entries.append(TestEntry(test_id=test_id, outcome=_entry_outcome(case)))
    return tuple(entries)


class _Execution:
    __slots__ = ("identity", "final_outcome", "attempts")

    def __init__(self, identity: str, final_outcome: TestOutcome) -> None:
        self.identity = identity
        self.final_outcome = final_outcome
        self.attempts = 1


def _disambiguate(test_id: str, occurrence: int, taken: set[str]) -> str:
    if occurrence == 1 and test_id not in taken:
        return test_id
    suffix = occurrence
    while True:
        candidate = f"{test_id} [duplicate-name {suffix}]"
        if candidate not in taken:
            return candidate
        suffix += 1


def deconvolve(entries: tuple[TestEntry, ...]) -> Deconvolved:
    """Collapse retry attempts into one final verdict per execution."""

    executions: list[_Execution] = []
    open_execution: dict[str, _Execution] = {}
    occurrences: dict[str, int] = {}
    taken: set[str] = set()

    for entry in entries:
        current = open_execution.get(entry.test_id)
        if current is not None and current.final_outcome in _RED_OUTCOMES:
            # The last-entry rule: the newest attempt replaces the red verdict.
            current.final_outcome = entry.outcome
            current.attempts += 1
            continue
        occurrence = occurrences.get(entry.test_id, 0) + 1
        occurrences[entry.test_id] = occurrence
        identity = _disambiguate(entry.test_id, occurrence, taken)
        taken.add(identity)
        execution = _Execution(identity, entry.outcome)
        executions.append(execution)
        open_execution[entry.test_id] = execution

    executed_ids = tuple(sorted(execution.identity for execution in executions))
    final_red_ids = tuple(
        sorted(
            execution.identity
            for execution in executions
            if execution.final_outcome in _RED_OUTCOMES
        )
    )
    flaky_ids = tuple(
        sorted(
            execution.identity
            for execution in executions
            if execution.attempts > 1 and execution.final_outcome == "passed"
        )
    )
    final_skipped_ids = tuple(
        sorted(
            execution.identity for execution in executions if execution.final_outcome == "skipped"
        )
    )
    duplicate_name_ids = tuple(
        sorted(test_id for test_id, count in occurrences.items() if count > 1)
    )
    return Deconvolved(
        executed_ids=executed_ids,
        final_red_ids=final_red_ids,
        flaky_ids=flaky_ids,
        final_skipped_ids=final_skipped_ids,
        raw_entry_count=len(entries),
        duplicate_name_ids=duplicate_name_ids,
    )
