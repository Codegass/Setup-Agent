"""One producer for the argument vector a JVM dispatch runs.

Two sites compute that vector. The facade freezes an invocation contract over
it BEFORE anything physical runs (`MavenBackend.expected_argv`,
`GradleBackend.expected_argv`), and the runner builds the physical command a
moment later (`MavenTool._build_maven_command`,
`GradleTool._build_gradle_command`). When each derives the vector on its own
they drift, and the receipt seals an argv the contract never named.

The rule both sites read from here: the argv is what the contract named, in
the contracted order — nothing appended, nothing reordered.
"""

import shlex
from typing import Any, List

# The task gradle runs when nothing — neither the action nor the caller's own
# args — names one.
DEFAULT_GRADLE_TASK = "build"

# Gradle options that consume the FOLLOWING token as their value. A value is
# never a task selection however task-shaped it reads: `-x test` excludes that
# task and `--tests SomeTest` filters one, and neither is the caller naming
# work to run.
_GRADLE_VALUE_OPTIONS = frozenset(
    {
        "-b",
        "--build-file",
        "-c",
        "--settings-file",
        "-g",
        "--gradle-user-home",
        "-I",
        "--init-script",
        "-x",
        "--exclude-task",
        "-p",
        "--project-dir",
        "-D",
        "-P",
        "--tests",
        "--include-build",
        "--project-cache-dir",
        "--console",
        "--warning-mode",
        "--max-workers",
        "--priority",
    }
)


def argv_tokens(value: Any) -> List[str]:
    """`value` as real argv tokens, whether it arrives as a list or a string.

    Malformed quoting falls back to a whitespace split rather than raising:
    reading a caller's args must never be the thing that stops a dispatch the
    contract already authorized.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    text = str(value)
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def bare_gradle_task(selection: Any) -> str:
    """The project-wide task a scoped selection narrows: `:clients:test` -> `test`."""
    return str(selection).rsplit(":", 1)[-1]


def gradle_task_selections(gradle_args: Any) -> List[str]:
    """The tasks the caller's own args already select.

    Positional tokens only: a token starting with `-` is an option, and the
    token after a value-taking option is that option's value.
    """
    selections: List[str] = []
    take_value = False
    for token in argv_tokens(gradle_args):
        if take_value:
            take_value = False
            continue
        if token.startswith("-"):
            take_value = token in _GRADLE_VALUE_OPTIONS
            continue
        selections.append(token)
    return selections


def gradle_task_tokens(tasks: Any, gradle_args: Any = None) -> List[str]:
    """The task tokens appended after the caller's args — the contracted set.

    A task the caller already selected is never appended a second time, and a
    SCOPED selection is never joined by its bare superset: `:clients:test test`
    asks gradle for one module and then for every module, and the superset
    silently wins (kafka d2r3 — a run scoped to clients tested the whole
    project). The action's default task is what drops, never the caller's
    selection: the narrower request is the one that was actually made.

    Suppression is per task, so an action whose default is a task LIST keeps
    every task the caller did not narrow. `build` stands in only when neither
    side names a task at all.
    """
    selections = gradle_task_selections(gradle_args)
    requested = argv_tokens(tasks)
    if not requested and not selections:
        return [DEFAULT_GRADLE_TASK]
    named = {
        token for selection in selections for token in (selection, bare_gradle_task(selection))
    }
    return [task for task in requested if task not in named and bare_gradle_task(task) not in named]


def maven_action_tokens(command: Any, goals: Any = None, extra_args: Any = None) -> List[str]:
    """The lifecycle the contract named, then its goals and extra args.

    seatunnel-web d2r2: a repair asked for `spotless:apply` while the action
    was `compile`, so the frozen contract read `compile spotless:apply` and
    compile ran first. Construction gets no vote on that order — the contract
    names it, and hoisting a goal ahead of the action would seal an argv
    nobody committed to.
    """
    return argv_tokens(command) + argv_tokens(goals) + argv_tokens(extra_args)
