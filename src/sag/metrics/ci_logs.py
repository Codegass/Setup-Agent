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

from sag.metrics.module_keys import module_key, module_keys

# Successful build tasks identify projects participating in the CI build.
# NO-SOURCE and UP-TO-DATE still identify selected projects (including empty
# aggregators); they do not prove fresh physical output. Test tasks are absent:
# a module that ran tests is the pool rung's lower-bound fact.
_GRADLE_TASK_RE = re.compile(
    r"> Task (?P<path>(?::[A-Za-z0-9_.\-]+)*):"
    r"(?P<task>compileJava|compileKotlin|compileScala|compileGroovy|classes|jar)\b(?P<suffix>[^\r\n]*)"
)
_MAVEN_SUMMARY_RE = re.compile(
    r"\[INFO\][ \t]+(?P<name>\S[^\r\n]*?)[ \t]+\.{2,}[ \t]+(?P<status>SUCCESS|FAILURE|SKIPPED)\b"
)
# Packaging plugins also print unindented `Building jar: <path>` lines.
# They name artifacts, not additional projects.
_MAVEN_BUILDING_RE = re.compile(
    r"\[INFO\] Building (?!(?:jar|war|ear|zip|tar)(?: archive)?:)"
    r"(?P<name>\S.*?)(?: \[\d+/\d+\])?\s*$"
)
_MAVEN_RESULT_RE = re.compile(r"\[INFO\] BUILD (?P<result>SUCCESS|FAILURE)\b")
_JOB_FILE_RE = re.compile(r"^\d+_(?P<job>.+)\.txt$")


class LogModules(BaseModel):
    """What one job log proves about the build universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Literal["gradle", "maven"] | None = None
    modules: tuple[str, ...] = ()
    skipped: int = 0
    failed: int = 0


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

    rows = list(_MAVEN_SUMMARY_RE.finditer(text))
    if rows:
        maven_built = [match.group("name") for match in rows if match.group("status") == "SUCCESS"]
        return LogModules(
            tool="maven",
            modules=module_keys(maven_built) if maven_built else (),
            skipped=sum(1 for match in rows if match.group("status") == "SKIPPED"),
            failed=sum(1 for match in rows if match.group("status") == "FAILURE"),
        )

    result = _MAVEN_RESULT_RE.search(text)
    building = [
        match for match in (_MAVEN_BUILDING_RE.search(line) for line in text.splitlines()) if match
    ]
    if result is not None and len(building) == 1:
        # One project, no reactor: the root is the whole universe.
        succeeded = result.group("result") == "SUCCESS"
        return LogModules(
            tool="maven", modules=(".",) if succeeded else (), failed=0 if succeeded else 1
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
