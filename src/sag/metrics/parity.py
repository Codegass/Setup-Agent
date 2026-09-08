# src/sag/metrics/parity.py
"""Lifecycle parity: did SAG run what the CI command runs?

The CI command is the build's specification — the profiles, phases and
plugin goals a project's own CI considers a working checkout.  This axis
states equivalence and names the gap; it never enters the attainment score.
"""

from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from sag.metrics.build_scope import CiCommand, parse_ci_command

MAVEN_PHASES: tuple[str, ...] = (
    "validate",
    "initialize",
    "generate-sources",
    "process-sources",
    "generate-resources",
    "process-resources",
    "compile",
    "process-classes",
    "generate-test-sources",
    "process-test-sources",
    "generate-test-resources",
    "process-test-resources",
    "test-compile",
    "process-test-classes",
    "test",
    "prepare-package",
    "package",
    "pre-integration-test",
    "integration-test",
    "post-integration-test",
    "verify",
    "install",
    "deploy",
)
# The phases a reader recognises as milestones; the gap is named in these.
_MILESTONES = ("compile", "test", "package", "integration-test", "verify", "install", "deploy")
_PHASE_INDEX = {phase: index for index, phase in enumerate(MAVEN_PHASES)}


class LifecycleParity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["equivalent", "not_equivalent", "unknown"]
    form: Literal["maven_phases", "gradle_tasks", "none"]
    ci_command: str
    sag_commands: tuple[str, ...] = ()
    ci_reach: str | None = None
    sag_reach: str | None = None
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()


def _reach(goals: Sequence[str]) -> str | None:
    phases = [goal for goal in goals if goal in _PHASE_INDEX]
    return max(phases, key=_PHASE_INDEX.__getitem__) if phases else None


def command_parity(
    ci: CiCommand,
    sag: Sequence[CiCommand],
    *,
    ci_default_goal: str | None = None,
) -> LifecycleParity:
    """Compare one CI command with everything SAG dispatched."""

    sag_texts = tuple(command.text for command in sag)
    unknown = LifecycleParity(
        status="unknown", form="none", ci_command=ci.text, sag_commands=sag_texts
    )
    if ci.tool == "unknown" or not sag or any(command.tool != ci.tool for command in sag):
        return unknown
    if ci.unsupported_scope_reason or any(command.unsupported_scope_reason for command in sag):
        return unknown
    if any(
        set(command.profiles) != set(ci.profiles)
        or set(command.projects) != set(ci.projects)
        or (command.build_file or "pom.xml") != (ci.build_file or "pom.xml")
        or (command.project_dir or ".") != (ci.project_dir or ".")
        for command in sag
    ):
        return unknown

    if ci.tool == "maven":
        ci_goals = tuple(ci.goals)
        if not ci_goals and ci_default_goal:
            ci_goals = tuple(ci_default_goal.split())
        if not ci_goals:
            return unknown
        if any(
            goal not in _PHASE_INDEX and ":" not in goal and goal not in {"clean", "site"}
            for goal in ci_goals
        ):
            return unknown
        ci_reach = _reach(ci_goals)
        sag_goals = [goal for command in sag for goal in command.goals]
        sag_reach = _reach(sag_goals)
        missing: list[str] = []
        if ci_reach is not None:
            reached = _PHASE_INDEX[sag_reach] if sag_reach is not None else -1
            missing.extend(
                phase
                for phase in _MILESTONES
                if reached < _PHASE_INDEX[phase] <= _PHASE_INDEX[ci_reach]
            )
            if reached < _PHASE_INDEX[ci_reach] and not missing:
                missing.append(ci_reach)
        ci_plugins = sorted(goal for goal in ci_goals if ":" in goal)
        sag_plugins = {goal for goal in sag_goals if ":" in goal}
        missing.extend(goal for goal in ci_plugins if goal not in sag_plugins)
        missing.extend(
            goal for goal in ("clean", "site") if goal in ci_goals and goal not in sag_goals
        )
        extra = tuple(sorted(sag_plugins - set(ci_plugins)))
        return LifecycleParity(
            status="equivalent" if not missing else "not_equivalent",
            form="maven_phases",
            ci_command=ci.text,
            sag_commands=sag_texts,
            ci_reach=ci_reach,
            sag_reach=sag_reach,
            missing=tuple(missing),
            extra=extra,
        )

    ci_tasks = set(ci.goals) - set(ci.excluded_tasks)
    if not ci_tasks:
        return unknown
    sag_tasks = {
        task for command in sag for task in set(command.goals) - set(command.excluded_tasks)
    }
    missing_tasks = tuple(sorted(ci_tasks - sag_tasks))
    if not missing_tasks and any(
        set(command.excluded_tasks) - set(ci.excluded_tasks) for command in sag
    ):
        # Without Gradle's task graph a new exclusion may remove prerequisites.
        return unknown
    return LifecycleParity(
        status="equivalent" if not missing_tasks else "not_equivalent",
        form="gradle_tasks",
        ci_command=ci.text,
        sag_commands=sag_texts,
        missing=missing_tasks,
        extra=tuple(sorted(sag_tasks - ci_tasks)),
    )


def parity_from_texts(
    ci_text: str, sag_texts: Sequence[str], *, ci_default_goal: str | None = None
) -> LifecycleParity:
    """Convenience for callers holding raw command strings."""

    return command_parity(
        parse_ci_command(ci_text),
        [parse_ci_command(t) for t in sag_texts],
        ci_default_goal=ci_default_goal,
    )


__all__ = ["MAVEN_PHASES", "LifecycleParity", "command_parity", "parity_from_texts"]
