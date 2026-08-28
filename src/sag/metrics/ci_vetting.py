"""Vet a CI cell before its green conclusion is treated as a target.

Two failure modes make an upstream "success" worthless as a yardstick:

* **Laundering.** ``continue-on-error: true`` on the job, or on the step that
  actually runs the build or the tests, turns a red run green.  The conclusion
  is then a statement about the workflow file, not about the code.
* **Cell mismatch.** A JDK17 target proves nothing about a run on JDK8, and a
  windows-only cell proves little about a Linux container.  Matching therefore
  prefers the toolchain first and the platform second, and never silently
  substitutes a windows or macOS cell for a Linux one.

This module is pure text and data analysis: it reads YAML the caller already
holds and cell identifiers the caller already harvested.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sag.metrics.target_record import CellTarget

CellOs = Literal["linux", "macos", "windows", "unknown"]

# A step whose run text mentions one of these is a build or test step, so
# swallowing its exit status launders the cell's conclusion.
BUILD_TEST_TOKENS: tuple[str, ...] = (
    "mvn",
    "mvnw",
    "gradle",
    "gradlew",
    "make check",
    " test",
    "verify",
)

_FALSEY_STRINGS = frozenset({"", "false", "0", "no", "off", "none", "null"})

_JDK_TOKEN_RE = re.compile(
    r"(?:jdk|openjdk|java|temurin|zulu|corretto|graalvm)[\s._\-]*v?(\d{1,2})(?!\d)",
    re.IGNORECASE,
)
_PAREN_GROUP_RE = re.compile(r"\(([^)]*)\)")
_STANDALONE_INT_RE = re.compile(r"(?<![\w.])(\d{1,2})(?![\w.])")

_LINUX_TOKENS = ("ubuntu", "linux", "debian", "fedora", "alpine")
_MACOS_TOKENS = ("macos", "mac-os", "osx", "darwin")
_WINDOWS_TOKENS = ("windows", "win-", "win32", "win64")

# Linux is the container platform SAG runs on; an unlabelled cell is treated as
# Linux-compatible rather than as a foreign platform.
_OS_RANK: dict[CellOs, int] = {"linux": 0, "unknown": 1, "macos": 2, "windows": 3}
_LINUX_COMPATIBLE = frozenset({"linux", "unknown"})


class LaunderingVet(BaseModel):
    """Whether a workflow config swallows build or test failures, and where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    laundered: bool
    locations: tuple[str, ...] = ()


class CellMatch(BaseModel):
    """The cell chosen for a given JDK major, and what was compromised to get it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cell_id: str | None = None
    exact: bool = False
    caveat: str | None = Field(default=None, max_length=512)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY_STRINGS
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(value)


def _mentions_build_or_test(run_text: str) -> bool:
    lowered = run_text.lower()
    return any(token in lowered for token in BUILD_TEST_TOKENS)


def vet_workflow_config(yaml_text: str) -> LaunderingVet:
    """Report every place a workflow config swallows a build or test failure."""

    try:
        document = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # Disclosure, not a crash: an unreadable config is reported as
        # un-vetted rather than asserted clean or asserted laundered.
        return LaunderingVet(laundered=False, locations=("unparseable",))
    if not isinstance(document, dict):
        return LaunderingVet(laundered=False, locations=("unparseable",))

    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return LaunderingVet(laundered=False, locations=())

    # Locations are emitted in document order: they name positions in the file.
    locations: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if _is_truthy(job.get("continue-on-error")):
            locations.append(f"job:{job_id}")
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if not _is_truthy(step.get("continue-on-error")):
                continue
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            if _mentions_build_or_test(run_text):
                locations.append(f"job:{job_id}/step:{index}")
    return LaunderingVet(laundered=bool(locations), locations=tuple(locations))


def extract_cell_jdk(cell_id: str) -> int | None:
    """Return the JDK major a cell identifier names, or None."""

    if not isinstance(cell_id, str) or not cell_id.strip():
        return None
    token_match = _JDK_TOKEN_RE.search(cell_id)
    if token_match is not None:
        major = int(token_match.group(1))
        if 6 <= major <= 99:
            return major
    # Matrix cells often render as "build (17, false)" or "(8, ubuntu-latest)",
    # where the toolchain is a bare axis value with no JDK word next to it.
    for group in _PAREN_GROUP_RE.findall(cell_id):
        for candidate in _STANDALONE_INT_RE.findall(group):
            major = int(candidate)
            if 6 <= major <= 99:
                return major
    return None


def extract_cell_os(cell_id: str) -> CellOs:
    """Return the platform a cell identifier names, or ``unknown``."""

    if not isinstance(cell_id, str):
        return "unknown"
    lowered = cell_id.lower()
    if any(token in lowered for token in _LINUX_TOKENS):
        return "linux"
    if any(token in lowered for token in _MACOS_TOKENS):
        return "macos"
    if any(token in lowered for token in _WINDOWS_TOKENS):
        return "windows"
    return "unknown"


def match_cell(cells: Sequence[CellTarget], jdk_major: int) -> CellMatch:
    """Choose the cell that best proves something about a run on ``jdk_major``."""

    parsed: list[tuple[str, int, CellOs]] = []
    for cell in cells:
        major = extract_cell_jdk(cell.cell_id)
        if major is None:
            continue
        parsed.append((cell.cell_id, major, extract_cell_os(cell.cell_id)))
    if not parsed:
        return CellMatch(
            cell_id=None,
            exact=False,
            caveat="no harvested cell names a parseable JDK major",
        )

    exact_compatible = sorted(
        (item for item in parsed if item[1] == jdk_major and item[2] in _LINUX_COMPATIBLE),
        key=lambda item: (_OS_RANK[item[2]], item[0]),
    )
    if exact_compatible:
        return CellMatch(cell_id=exact_compatible[0][0], exact=True, caveat=None)

    exact_foreign = sorted(
        (item for item in parsed if item[1] == jdk_major),
        key=lambda item: (_OS_RANK[item[2]], item[0]),
    )
    if exact_foreign:
        cell_id, _, cell_os = exact_foreign[0]
        return CellMatch(
            cell_id=cell_id,
            exact=True,
            caveat=f"JDK{jdk_major} is only proven on {cell_os}, not on linux",
        )

    above = sorted(
        (item for item in parsed if item[1] > jdk_major and item[2] in _LINUX_COMPATIBLE),
        key=lambda item: (item[1] - jdk_major, _OS_RANK[item[2]], item[0]),
    )
    if above:
        cell_id, major, _ = above[0]
        return CellMatch(
            cell_id=cell_id,
            exact=False,
            caveat=f"no cell runs JDK{jdk_major}; matched the nearest above, JDK{major}",
        )

    below = sorted(
        (item for item in parsed if item[1] < jdk_major and item[2] in _LINUX_COMPATIBLE),
        key=lambda item: (jdk_major - item[1], _OS_RANK[item[2]], item[0]),
    )
    if below:
        cell_id, major, _ = below[0]
        return CellMatch(
            cell_id=cell_id,
            exact=False,
            caveat=f"no cell runs JDK{jdk_major}; matched the nearest below, JDK{major}",
        )

    return CellMatch(
        cell_id=None,
        exact=False,
        caveat=(
            f"no linux-compatible cell is near JDK{jdk_major}; "
            "a windows or macOS cell is never substituted for one"
        ),
    )
