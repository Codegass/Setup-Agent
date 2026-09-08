"""Vet a CI cell before its green conclusion is treated as a target.

Two failure modes make an upstream "success" worthless as a yardstick:

* **Laundering.** ``continue-on-error``/``allow-failure`` on the job, or on the
  step that actually runs the build or the tests, turns a red run green.  The
  conclusion is then a statement about the workflow file, not about the code.
  Two config shapes are read — GitHub Actions ``jobs``/``steps`` and GitLab-style
  top-level jobs carrying a ``script`` — and anything else is disclosed as
  un-vetted rather than reported clean.
* **Cell mismatch.** A JDK17 target proves nothing about a run on JDK8, and a
  windows-only cell proves little about a Linux container.  Linux compatibility
  is the harder requirement: matching walks the Linux-compatible cells by JDK
  distance first, and a foreign-platform cell is a last resort, never a
  substitute for a Linux one that is a major or two away.

This module is pure text and data analysis: it reads YAML the caller already
holds and cell identifiers the caller already harvested.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
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

# The spec names continue-on-error and allow-failure; CI systems spell them with
# either separator, so both punctuations of both names are honoured.
LAUNDERING_KEYS: tuple[str, ...] = (
    "continue-on-error",
    "continue_on_error",
    "allow-failure",
    "allow_failure",
)

# GitHub Actions parses ``on:`` to the boolean True under YAML 1.1, so the marker
# for "this is a workflow file" is that key or the literal string.
_ACTIONS_MARKER_KEYS: tuple[Any, ...] = ("jobs", "on", True)

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
LINUX_COMPATIBLE: frozenset[str] = frozenset({"linux", "unknown"})


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


def _swallows_failure(node: dict[Any, Any]) -> bool:
    return any(_is_truthy(node[key]) for key in LAUNDERING_KEYS if key in node)


def _vet_actions_jobs(jobs: dict[Any, Any]) -> LaunderingVet:
    # Locations are emitted in document order: they name positions in the file.
    locations: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if _swallows_failure(job):
            locations.append(f"job:{job_id}")
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if not _swallows_failure(step):
                continue
            # A step builds or tests either through its shell text or through the
            # action it calls, so both name the command this step really runs.
            command = " ".join(
                text for text in (step.get("run"), step.get("uses")) if isinstance(text, str)
            )
            if _mentions_build_or_test(command):
                locations.append(f"job:{job_id}/step:{index}")
    return LaunderingVet(laundered=bool(locations), locations=tuple(locations))


def _vet_gitlab_jobs(document: dict[Any, Any]) -> LaunderingVet | None:
    """Vet top-level GitLab-style jobs, or None when the document has none."""

    job_ids = [
        key
        for key, value in document.items()
        if isinstance(key, str) and isinstance(value, dict) and "script" in value
    ]
    if not job_ids:
        return None
    locations = tuple(f"job:{job_id}" for job_id in job_ids if _swallows_failure(document[job_id]))
    return LaunderingVet(laundered=bool(locations), locations=locations)


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

    if "jobs" in document:
        jobs = document["jobs"]
        if isinstance(jobs, dict):
            return _vet_actions_jobs(jobs)
        return LaunderingVet(laundered=False, locations=("unrecognized-shape",))

    gitlab = _vet_gitlab_jobs(document)
    if gitlab is not None:
        return gitlab

    if any(key in document for key in _ACTIONS_MARKER_KEYS):
        # A workflow that declares no jobs has nothing to swallow.
        return LaunderingVet(laundered=False, locations=())

    # A shape neither reader understands is disclosed as un-vetted; reporting it
    # clean would be indistinguishable from a config this module actually read.
    return LaunderingVet(laundered=False, locations=("unrecognized-shape",))


class BuildCommandStep(BaseModel):
    """One workflow step that builds or tests, with the job that runs it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    job_name_template: str
    text: str
    working_directory: str = "."
    working_directory_source: str = "repository_root"


def _step_directory(document: dict, job: dict, step: dict) -> tuple[str, str]:
    """Resolve the YAML working-directory precedence without evaluating it."""
    for owner, label in ((step, "step"), (job, "job_defaults"), (document, "workflow_defaults")):
        config: dict[Any, Any] | None = owner
        if label != "step":
            defaults = owner.get("defaults")
            config = defaults.get("run") if isinstance(defaults, dict) else None
        if isinstance(config, dict) and "working-directory" in config:
            value = config["working-directory"]
            return (
                (
                    value.strip()
                    if isinstance(value, str) and value.strip()
                    else "${UNKNOWN_WORKING_DIRECTORY}"
                ),
                label,
            )
    return ".", "repository_root"


def extract_build_commands(yaml_text: str) -> tuple[BuildCommandStep, ...]:
    """Every `run:` step that mentions a build or test, in document order."""

    try:
        document = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return ()
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return ()
    found: list[BuildCommandStep] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        template = name if isinstance(name, str) else str(job_id)
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            if _mentions_build_or_test(step["run"]):
                working_directory, directory_source = _step_directory(document, job, step)
                found.append(
                    BuildCommandStep(
                        job_id=str(job_id),
                        job_name_template=template,
                        text=step["run"].strip(),
                        working_directory=working_directory,
                        working_directory_source=directory_source,
                    )
                )
    return tuple(found)


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
        (item for item in parsed if item[1] == jdk_major and item[2] in LINUX_COMPATIBLE),
        key=lambda item: (_OS_RANK[item[2]], item[0]),
    )
    if exact_compatible:
        return CellMatch(cell_id=exact_compatible[0][0], exact=True, caveat=None)

    # Spec 4.2 makes Linux part of mu and nearest-JDK-above the only sanctioned
    # relaxation, so a foreign-platform cell of the exact JDK is passed over
    # here; when it exists the caveat names it, since the reader must know the
    # exact JDK ran somewhere SAG cannot use.
    exact_foreign = sorted(
        (item for item in parsed if item[1] == jdk_major),
        key=lambda item: (_OS_RANK[item[2]], item[0]),
    )
    foreign_os = exact_foreign[0][2] if exact_foreign else None
    passed_over = (
        f"JDK{jdk_major} is only proven on {foreign_os}, not on linux; matched the nearest "
        if foreign_os is not None
        else f"no cell runs JDK{jdk_major}; matched the nearest "
    )

    above = sorted(
        (item for item in parsed if item[1] > jdk_major and item[2] in LINUX_COMPATIBLE),
        key=lambda item: (item[1] - jdk_major, _OS_RANK[item[2]], item[0]),
    )
    if above:
        cell_id, major, _ = above[0]
        return CellMatch(
            cell_id=cell_id,
            exact=False,
            caveat=f"{passed_over}above, JDK{major}",
        )

    below = sorted(
        (item for item in parsed if item[1] < jdk_major and item[2] in LINUX_COMPATIBLE),
        key=lambda item: (jdk_major - item[1], _OS_RANK[item[2]], item[0]),
    )
    if below:
        cell_id, major, _ = below[0]
        return CellMatch(
            cell_id=cell_id,
            exact=False,
            caveat=f"{passed_over}below, JDK{major}",
        )

    if exact_foreign:
        cell_id, _, cell_os = exact_foreign[0]
        return CellMatch(
            cell_id=cell_id,
            exact=True,
            caveat=f"JDK{jdk_major} is only proven on {cell_os}, not on linux",
        )

    return CellMatch(
        cell_id=None,
        exact=False,
        caveat=(
            f"no linux-compatible cell is near JDK{jdk_major}; "
            "a windows or macOS cell is never substituted for one"
        ),
    )
