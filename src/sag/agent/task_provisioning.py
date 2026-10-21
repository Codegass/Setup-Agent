"""What the harness provisions from a pinned task, and how it says so.

The pinned task names the repository, the commit, the launcher JDK major of
each step and the Maven version of each Maven step. Checking out a commit and
installing a runtime execute no project build or test, so the harness makes
those calls itself before the first provision-phase model turn (2026-10-21):
round-3 ledgers spent two to three model requests per run on them, every
argument read from task.json.

This module is the pure part: which calls the task names, how each result
reads as one ``Provisioned: `` line of the provision brief, and the sentence a
harness close records. The engine dispatches the calls and closes the phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from .acceptance_task import AcceptanceTask

PROVISIONED_PREFIX = "Provisioned: "
PROVISIONED_CLOSING_LINE = (
    f"{PROVISIONED_PREFIX}the lines above are what the harness already did from the "
    "pinned task; continue from there."
)
# One error, one line, bounded: the brief states what failed, the record
# carries the full output.
PROVISIONED_ERROR_CHARS = 300


@dataclass(frozen=True)
class ProvisionAction:
    """One `project` call the pinned task names."""

    kind: Literal["checkout", "java", "maven"]
    label: str
    params: Mapping[str, Any]
    expected_commit: str = ""


@dataclass(frozen=True)
class ProvisionPlan:
    actions: tuple[ProvisionAction, ...]
    # Facts that keep the model turn whatever the calls return (steps that
    # name different versions of one tool). Each is a whole brief line.
    notes: tuple[str, ...] = ()
    java_pinned: bool = False

    @property
    def closable(self) -> bool:
        """Whether a fully successful run of the actions may close provision."""
        return self.java_pinned and not self.notes


def _distinct(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(value for value in values if value is not None))


def plan_task_provision(task: AcceptanceTask) -> ProvisionPlan:
    """The checkout, then the first named JDK, then the first named Maven."""
    actions = [
        ProvisionAction(
            kind="checkout",
            label=f"checkout {task.repo} at {task.sha}",
            params={
                "action": "clone",
                "repo_url": f"https://github.com/{task.repo}.git",
                "ref": task.sha,
            },
            expected_commit=task.sha,
        )
    ]
    notes: list[str] = []
    java = _distinct(step.java_major for step in task.steps)
    if java:
        version = str(java[0])
        actions.append(
            ProvisionAction(
                kind="java",
                label=f"java {version} (openjdk)",
                params={
                    "action": "provision",
                    "java_version": version,
                    "java_distribution": "openjdk",
                },
            )
        )
        if len(java) > 1:
            notes.append(
                f"{PROVISIONED_PREFIX}the task steps name different Java versions "
                f"({', '.join(str(value) for value in java)}); the harness handled only "
                f"the first, java {version}."
            )
    maven = _distinct(step.maven_version for step in task.steps if step.runner == "maven")
    if maven:
        version = str(maven[0])
        actions.append(
            ProvisionAction(
                kind="maven",
                label=f"maven {version}",
                params={"action": "provision", "maven_version": version},
            )
        )
        if len(maven) > 1:
            notes.append(
                f"{PROVISIONED_PREFIX}the task steps name different Maven versions "
                f"({', '.join(maven)}); the harness handled only the first, maven {version}."
            )
    return ProvisionPlan(actions=tuple(actions), notes=tuple(notes), java_pinned=bool(java))


def one_line(text: Any, limit: int = PROVISIONED_ERROR_CHARS) -> str:
    """Whitespace collapsed to single spaces, cut to `limit` characters."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _error_text(result: Any) -> str:
    for value in (
        getattr(result, "error", None),
        getattr(result, "failure_signature", None),
        getattr(result, "error_code", None),
        getattr(result, "output", None),
    ):
        if str(value or "").strip():
            return one_line(value)
    return "no error text was returned"


def checkout_path(result: Any) -> str:
    metadata = getattr(result, "metadata", None) or {}
    return str(metadata.get("clone_path") or metadata.get("target_directory") or "").strip()


def assess_provision_result(action: ProvisionAction, result: Any) -> tuple[bool, str]:
    """Whether the call did what the task names, and its brief line."""
    head = f"{PROVISIONED_PREFIX}{action.label} → "
    if not getattr(result, "succeeded", False):
        return False, f"{head}failed: {_error_text(result)}"
    if action.kind != "checkout":
        return True, f"{head}installed and active (ok)"
    path = checkout_path(result)
    metadata = getattr(result, "metadata", None) or {}
    resolved = str(metadata.get("resolved_commit") or "").strip()
    where = f" in {path}" if path else ""
    if not resolved:
        return (
            False,
            f"{head}failed: the harness could not read which commit the checkout{where} is at",
        )
    if resolved != action.expected_commit:
        return False, f"{head}failed: the checkout{where} is at {resolved}, not the pinned commit"
    return True, f"{head}{path or 'the workspace'} (ok)"


def failed_by_exception_line(action: ProvisionAction, exc: BaseException) -> str:
    return (
        f"{PROVISIONED_PREFIX}{action.label} → failed: {one_line(str(exc) or type(exc).__name__)}"
    )


def provision_key_results(task: AcceptanceTask, path: str, labels: Iterable[str]) -> str:
    """The harness close's key results: the checkout and the installed toolchain."""
    installed = " and ".join(labels)
    return (
        f"Checked out {task.repo} at {task.sha} in {path}; {installed} installed and active, "
        "as the pinned task requires."
    )
