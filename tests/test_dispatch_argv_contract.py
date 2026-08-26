# tests/test_dispatch_argv_contract.py
"""The dispatched argv is exactly the contracted action set.

Two live convictions:

* kafka d2r3 — the model asked for `--no-daemon :clients:test`, and the
  dispatch ran `:clients:test test`. The builder appended the verb's bare task
  after the caller's scoped selection, so gradle ran every module's tests on
  top of the one module that was asked for, and the run was scored on work
  nobody requested.
* seatunnel-web d2r2 — a repair asked for `spotless:apply` while the action
  was `compile`. The frozen contract named `compile spotless:apply`, and
  compile ran first. That order is the contract's to name; construction
  reproduces it token for token and never hoists a goal on its own.

Both producers of the vector are exercised here — the facade's pre-dispatch
prediction and the runner's physical command — because a rule only one of them
obeys is a rule the receipt can still contradict.
"""

import shlex
from types import SimpleNamespace

from sag.agent.invocation_contracts import compliance_class
from sag.tools.build.backends import GradleBackend, MavenBackend
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool


def _gradle_backend():
    return GradleBackend(SimpleNamespace(orchestrator=None))


def _maven_tool():
    tool = MavenTool.__new__(MavenTool)
    tool.orchestrator = None
    return tool


def _physical_gradle(params, properties=""):
    return GradleTool._build_gradle_command(
        None,
        "gradle",
        params.get("tasks"),
        properties,
        params.get("gradle_args"),
        "",
        False,
        False,
        False,
        params.get("fail_at_end", False),
    )


def _physical_maven(params):
    return _maven_tool()._build_maven_command(
        command=params.get("command"),
        goals="",
        profiles="",
        properties="",
        fail_at_end=params.get("fail_at_end", False),
        extra_args=params.get("extra_args"),
    )


# ---------------------------------------------------------------------------
# kafka d2r3: a scoped selection is never joined by its bare superset
# ---------------------------------------------------------------------------


def test_a_scoped_gradle_selection_is_never_dispatched_with_its_bare_superset():
    params = _gradle_backend().materialize(
        "test", "--no-daemon :clients:test", "/workspace/kafka", None
    )

    tokens = shlex.split(_physical_gradle(params))

    assert tokens == ["gradle", "--continue", "--no-daemon", ":clients:test"]
    # The whole-project superset of the scoped selection, and the default task
    # that would stand in for it, are both unconstructible from this call.
    assert "test" not in tokens
    assert "build" not in tokens


def test_the_facade_freezes_the_scoped_vector_the_runner_builds():
    backend = _gradle_backend()
    params = backend.materialize("test", "--no-daemon :clients:test", "/workspace/kafka", None)

    assert backend.expected_argv(params) == "--continue --no-daemon :clients:test"
    assert compliance_class(backend.expected_argv(params), _physical_gradle(params)) == "exact"


def test_a_scoped_selection_of_one_default_task_leaves_the_others_alone():
    """Suppression is per task, not per call: the caller narrowed compileJava,
    so compileScala is still the contracted work it always was."""
    params = {"tasks": "compileJava compileScala", "gradle_args": ":core:compileJava"}

    tokens = shlex.split(_physical_gradle(params))

    assert tokens == ["gradle", ":core:compileJava", "compileScala"]


def test_an_excluded_task_is_not_read_as_a_selection():
    """`-x test` is the option's value, not the caller naming work to run —
    the packaging contract's own exclusion must not eat the verb's task."""
    backend = _gradle_backend()
    params = backend.materialize("package", "--info", "/workspace/proj", None)

    assert params["gradle_args"] == "--info -x test"
    assert shlex.split(_physical_gradle(params)) == [
        "gradle",
        "--continue",
        "--info",
        "-x",
        "test",
        "assemble",
    ]


def test_a_call_that_names_no_task_at_all_still_defaults_to_build():
    tokens = shlex.split(_physical_gradle({"tasks": "", "gradle_args": "--info"}))

    assert tokens == ["gradle", "--info", "build"]


# ---------------------------------------------------------------------------
# seatunnel-web d2r2: the action precedes the extra args, as the contract names
# ---------------------------------------------------------------------------


def test_the_maven_action_precedes_the_extra_args_the_contract_named():
    backend = MavenBackend(SimpleNamespace(orchestrator=None))
    params = backend.materialize("compile", "spotless:apply", "/workspace/seatunnel-web", None)

    physical = _physical_maven(params)

    assert shlex.split(physical) == ["mvn", "--fail-at-end", "compile", "spotless:apply"]
    assert backend.expected_argv(params) == "--fail-at-end compile spotless:apply"
    assert compliance_class(backend.expected_argv(params), physical) == "exact"


def test_the_maven_vector_appends_nothing_the_contract_did_not_name():
    backend = MavenBackend(SimpleNamespace(orchestrator=None))
    params = backend.materialize("test", None, "/workspace/seatunnel-web", None)

    assert shlex.split(_physical_maven(params)) == ["mvn", "--fail-at-end", "test"]
    assert compliance_class(backend.expected_argv(params), _physical_maven(params)) == "exact"
