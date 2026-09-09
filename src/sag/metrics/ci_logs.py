# src/sag/metrics/ci_logs.py
"""A CI job log names every module the build ran.

This is the exact rung of a cell's build universe: Gradle prints one
``> Task :path:compileJava`` per compiled project and Maven prints a Reactor
Summary whose display names are the ones SAG's own Maven receipts record.
Pure: the caller hands in text; nothing here touches the network.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sag.metrics.maven_reactor import parse_maven_reactor, reactor_scope_issues
from sag.metrics.module_keys import module_key, module_keys

# Successful build tasks identify projects participating in the CI build.
# NO-SOURCE and UP-TO-DATE still identify selected projects (including empty
# aggregators); they do not prove fresh physical output. Test tasks are absent:
# a module that ran tests is the pool rung's lower-bound fact.
_GRADLE_TASK_RE = re.compile(
    r"> Task (?P<path>(?::[A-Za-z0-9_.\-]+)*):"
    r"(?P<task>compileJava|compileKotlin|compileScala|compileGroovy|classes|jar)\b(?P<suffix>[^\r\n]*)"
)
# Packaging plugins also print unindented `Building jar: <path>` lines.
# They name artifacts, not additional projects.
_MAVEN_BUILDING_RE = re.compile(
    r"\[INFO\] Building (?!(?:jar|war|ear|zip|tar)(?: archive)?:)"
    r"(?P<name>\S.*?)(?: \[(?P<index>\d+)/(?P<total>\d+)\])?\s*$"
)
_MAVEN_RESULT_RE = re.compile(r"\[INFO\] BUILD (?P<result>SUCCESS|FAILURE)[ \t]*$")
_JOB_FILE_RE = re.compile(r"^\d+_(?P<job>.+)\.txt$")


class LogModules(BaseModel):
    """What one job log proves about the build universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Literal["gradle", "maven"] | None = None
    modules: tuple[str, ...] = ()
    skipped: int = 0
    failed: int = 0
    ambiguous_labels: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def modules_from_log(text: str) -> LogModules:
    """The modules a job log proves were built, by the tool that printed it."""

    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    gradle = list(_GRADLE_TASK_RE.finditer(text))
    if gradle:
        built: set[str] = set()
        failed: set[str] = set()
        skipped = 0
        for match in gradle:
            key = module_key(match.group("path") or ".")
            suffix = match.group("suffix").split()
            if "FAILED" in suffix:
                failed.add(key)
            elif "SKIPPED" in suffix:
                skipped += 1
            else:
                built.add(key)
        return LogModules(
            tool="gradle", modules=module_keys(built - failed), failed=len(failed), skipped=skipped
        )

    blocks = parse_maven_reactor(text)
    lines = text.splitlines()
    results = [
        (i, match) for i, line in enumerate(lines) if (match := _MAVEN_RESULT_RE.search(line))
    ]
    summary_lines = {row.line_number - 1 for block in blocks for row in block.rows}
    building = [
        (i, match)
        for i, line in enumerate(lines)
        if i not in summary_lines and (match := _MAVEN_BUILDING_RE.search(line))
    ]
    scans = [
        i
        for i, line in enumerate(lines)
        if i not in summary_lines and "[INFO] Scanning for projects" in line
    ]
    step_boundaries = [
        i for i, line in enumerate(lines) if "##[group]" in line or "##[endgroup]" in line
    ]
    if blocks:
        rows = [row for block in blocks for row in block.rows]
        notes: list[str] = []
        ambiguous = tuple(dict.fromkeys(name for b in blocks for name in b.duplicate_labels))
        # module_keys is a coordinate normalizer, not a display-name resolver.
        # Even distinct labels must not collapse through it into one coordinate.
        keys: dict[str, list[str]] = {}
        for row in rows:
            keys.setdefault(module_key(row.module), []).append(row.module)
        collisions = tuple(name for names in keys.values() if len(set(names)) > 1 for name in names)
        ambiguous = tuple(dict.fromkeys((*ambiguous, *collisions)))
        if ambiguous:
            notes.append("Maven module identity ambiguous: " + ", ".join(ambiguous))
        notes.extend(
            f"Maven build scope unavailable: {issue}"
            for issue in reactor_scope_issues(text, blocks)
        )
        maven_built = [row.module for row in rows if row.status == "SUCCESS"]
        return LogModules(
            tool="maven",
            modules=module_keys(maven_built) if not notes else (),
            skipped=sum(row.status == "SKIPPED" for row in rows),
            failed=sum(row.status == "FAILURE" for row in rows),
            ambiguous_labels=ambiguous,
            notes=tuple(notes),
        )

    if (
        len(results) == len(building) == 1
        and building[0][0] < results[0][0]
        and (not scans or (len(scans) == 1 and scans[0] < building[0][0]))
        and building[0][1].group("total") in (None, "1")
        and building[0][1].group("index") in (None, "1")
        and not any(building[0][0] < i < results[0][0] for i in step_boundaries)
    ):
        # One bounded build, one project, one terminal result. A marker in a
        # different invocation cannot turn the whole mixed job into root scope.
        succeeded = results[0][1].group("result") == "SUCCESS"
        return LogModules(
            tool="maven", modules=(".",) if succeeded else (), failed=0 if succeeded else 1
        )
    if results or building:
        return LogModules(
            tool="maven",
            notes=("Maven single-module build scope unavailable: ambiguous invocation boundaries",),
        )
    return LogModules()


def job_logs(zip_path: Path) -> dict[str, str]:
    """Job name → the job's full log, from a run's downloaded log archive.

    GitHub names the per-job file ``<index>_<job name>.txt`` at the archive
    root; per-step files live under ``<job name>/`` and are not read.
    """

    logs: dict[str, str] = {}
    ambiguous: set[str] = set()
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            if "/" in name:
                continue
            match = _JOB_FILE_RE.match(name)
            if match is None:
                continue
            job = match.group("job")
            if job in logs:
                ambiguous.add(job)
            logs[job] = archive.read(name).decode("utf-8", "replace")
    for job in ambiguous:
        del logs[job]
    return logs


__all__ = ["LogModules", "job_logs", "modules_from_log"]
