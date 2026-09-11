"""Parse Maven's summary blocks without turning display labels into identities.

Rows retain their order and multiplicity. A caller deciding build scope must
also check block completion and identity ambiguity; parsing alone proves neither.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_INFO = re.compile(r"\[INFO\][ \t]*(.*)$")
_HEADER = re.compile(r"Reactor Summary(?: for .*)?:$")
_ROW = re.compile(
    r"(?P<module>\S(?:.*?\S)?)[ \t]+(?:\.+[ \t]+)?"
    r"(?P<status>SUCCESS|FAILURE|SKIPPED)(?:[ \t]+\[[^\]\r\n]*\])?[ \t]*$"
)
_RESULT = re.compile(r"BUILD (SUCCESS|FAILURE)$")
_PROJECT = re.compile(
    r"Building (?!(?:jar|bundle|war|ear|zip|tar)(?: archive)?:)"
    r"(?P<name>\S.*?)(?: \[(?P<index>\d+)/(?P<total>\d+)\])?$"
)


@dataclass(frozen=True)
class MavenReactorRow:
    module: str
    status: str
    line_number: int
    raw: str


@dataclass(frozen=True)
class MavenReactorSummary:
    rows: tuple[MavenReactorRow, ...]
    start_line: int
    end_line: int
    result: str | None
    unparsed_lines: tuple[int, ...]

    @property
    def duplicate_labels(self) -> tuple[str, ...]:
        counts = Counter(" ".join(row.module.split()) for row in self.rows)
        return tuple(name for name, count in counts.items() if count > 1)


def parse_maven_reactor(text: str) -> tuple[MavenReactorSummary, ...]:
    """Read header-delimited summaries, retaining incomplete/unknown sections.

    ANSI and timestamp prefixes do not change the grammar. Dots within names
    belong to the name; padding before the terminal status may have any length,
    including zero for Maven's long display names.
    """

    blocks: list[MavenReactorSummary] = []
    rows: list[MavenReactorRow] = []
    unknown: list[int] = []
    start: int | None = None

    def finish(end: int, result: str | None = None) -> None:
        nonlocal start, rows, unknown
        if start is not None:
            blocks.append(MavenReactorSummary(tuple(rows), start, end, result, tuple(unknown)))
        start, rows, unknown = None, [], []

    lines = text.splitlines()
    for number, raw in enumerate(lines, 1):
        clean = _ANSI.sub("", raw)
        info = _INFO.search(clean)
        message = info.group(1).strip() if info else ""
        if info and _HEADER.fullmatch(message):
            finish(number - 1)
            start = number
            continue
        if start is None:
            continue
        result = _RESULT.fullmatch(message) if info else None
        if result:
            finish(number, result.group(1))
            continue
        row = _ROW.fullmatch(message) if info else None
        if row:
            rows.append(MavenReactorRow(row.group("module"), row.group("status"), number, clean))
            continue
        # These belong to a new invocation, even if the prior summary was cut.
        if message.startswith(("Scanning for projects", "Building ")):
            finish(number - 1)
            continue
        if not clean.strip() or (info and (not message or set(message) == {"-"})):
            continue
        unknown.append(number)
    finish(len(lines))
    return tuple(blocks)


def reactor_scope_issues(text: str, blocks: tuple[MavenReactorSummary, ...]) -> tuple[str, ...]:
    """Known log boundaries that prevent a summary from defining one build.

    These are syntax/count checks only. Distinct display names still need an
    identity check at the consumer; this function never resolves coordinates.
    """
    if not blocks:
        return ()
    issues: list[str] = []
    row_lines = {row.line_number for block in blocks for row in block.rows}
    projects: list[tuple[int, re.Match[str]]] = []
    scans: list[int] = []
    results: list[int] = []
    steps: list[int] = []
    for number, raw in enumerate(text.splitlines(), 1):
        clean = _ANSI.sub("", raw)
        if "##[group]" in clean or "##[endgroup]" in clean:
            steps.append(number)
        if number in row_lines:
            continue
        info = _INFO.search(clean)
        if not info:
            continue
        message = info.group(1).strip()
        if _RESULT.fullmatch(message):
            results.append(number)
        if message.startswith("Scanning for projects"):
            scans.append(number)
        project = _PROJECT.fullmatch(message)
        if project:
            projects.append((number, project))
    if len(blocks) != 1 or len(results) != 1:
        issues.append("multiple_or_unfinished_builds")
    if any(b.result is None or b.unparsed_lines or not b.rows for b in blocks):
        issues.append("incomplete_summary")
    start = projects[0][0] if projects else blocks[0].start_line
    end = blocks[-1].end_line
    if (
        len(scans) > 1
        or any(i >= blocks[0].start_line for i in scans)
        or any(i > end for i, _ in projects)
        or any(start < i < end for i in steps)
    ):
        issues.append("invocation_boundaries_unresolved")
    positions = [match.group("index") for _, match in projects]
    totals = [match.group("total") for _, match in projects]
    if len(projects) > 1 and (
        positions != [str(i) for i in range(1, len(projects) + 1)] or len(set(totals)) != 1
    ):
        issues.append("project_headers_span_builds")
    if len(blocks) == 1 and any(
        total is not None and int(total) != len(blocks[0].rows) for total in totals
    ):
        issues.append("project_count_mismatch")
    return tuple(issues)
