# tests/test_provider_no_deps_rung.py
"""The post-provider `--no-deps` rung (Plan 3 Task 1).

Live evidence (`logs/session_20260726_032047_95642` main.log:15892, output
`output_6ca8d2557d5e`): the in-repo provider `apache-tvm-ffi` builds and
installs as `0.1.13.dev47`, but PEP 440 orders `0.1.13.dev47 < 0.1.13`, so the
retried root install re-fails the SAME version-floor resolution the provider
recovery was supposed to clear. One more narrated rung follows:

  1. `{venv}/bin/python -m pip install -e . --no-deps`
  2. `{venv}/bin/python -m pip install <declared deps minus the provider>`

Scripted-orchestrator style (house pattern, tests/test_python_tool.py):
first-matching-substring rule wins, every command recorded.
"""

from sag.tools.internal.python_tool import PythonTool
from tests.test_python_tool import (
    Orch,
    TVM_MISSING_PROVIDER,
    TVM_NATIVE_MANIFEST,
    TVM_PROVIDER_INSTALL,
    TVM_ROOT_INSTALL,
    fail,
    ok,
    tvm_provider_rules,
)

# The provider builds fine, just below the declared floor: the manifest floor
# is `>=0.1.13` and the local checkout produces `0.1.13.dev47`.
TVM_FLOOR_MANIFEST = {
    **TVM_NATIVE_MANIFEST,
    "python_declared_dependencies": [
        "apache-tvm-ffi>=0.1.13",
        "ml_dtypes",
        "numpy",
        "typing_extensions",
    ],
}

NO_DEPS_INSTALL = f"{TVM_ROOT_INSTALL} --no-deps"
REMAINING_INSTALL = (
    "/workspace/tvm/.venv/bin/python -m pip install ml_dtypes numpy typing_extensions"
)


def floor_rules(*, no_deps=None, remaining=None):
    """Root install fails identically forever (the version floor is what the
    provider cannot satisfy); the rung rules come FIRST so the substring
    `pip install -e .` of the root rule never swallows `--no-deps`."""
    return [
        (NO_DEPS_INSTALL, no_deps if no_deps is not None else ok("root installed without deps")),
        (REMAINING_INSTALL, remaining if remaining is not None else ok("deps installed")),
        (TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER)),
        *tvm_provider_rules(),
    ]


def test_no_deps_rung_runs_when_provider_still_sits_below_the_declared_floor():
    orch = Orch(manifest=dict(TVM_FLOOR_MANIFEST), rules=floor_rules())

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    # (a) the --no-deps rung ran, exactly once, after the provider recovery.
    assert orch.commands.count(NO_DEPS_INSTALL) == 1
    assert orch.commands.index(TVM_PROVIDER_INSTALL) < orch.commands.index(NO_DEPS_INSTALL)
    # (b) the follow-up install carries the remaining declared deps and NOT the
    # provider's own distribution (that one is already installed from source).
    assert orch.commands.count(REMAINING_INSTALL) == 1
    assert orch.commands.index(NO_DEPS_INSTALL) < orch.commands.index(REMAINING_INSTALL)
    assert "apache-tvm-ffi" not in REMAINING_INSTALL
    # (c) honest success, flagged in metadata for the report layer.
    assert result.succeeded is True
    assert result.metadata["provider_no_deps_rung"] is True
    assert result.metadata["local_provider_recovery"]["root_retry"] is True
    # (d) both rung commands are narrated in the transcript.
    assert f"$ {NO_DEPS_INSTALL}" in result.output
    assert f"$ {REMAINING_INSTALL}" in result.output


def test_no_deps_rung_failure_is_an_honest_failure():
    orch = Orch(
        manifest=dict(TVM_FLOOR_MANIFEST),
        rules=floor_rules(no_deps=fail("ERROR: Could not install packages due to an OSError")),
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(NO_DEPS_INSTALL) == 1
    # the rung stops at the first failure: no remaining-deps install, and no
    # false success claim.
    assert orch.commands.count(REMAINING_INSTALL) == 0
    assert result.metadata.get("provider_no_deps_rung") is not True
    assert f"$ {NO_DEPS_INSTALL}" in result.output
    assert "Could not install packages" in (result.error or "") + result.output


def test_remaining_dependency_install_failure_is_an_honest_failure():
    orch = Orch(
        manifest=dict(TVM_FLOOR_MANIFEST),
        rules=floor_rules(remaining=fail("ERROR: No matching distribution found for ml_dtypes")),
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(NO_DEPS_INSTALL) == 1
    assert orch.commands.count(REMAINING_INSTALL) == 1
    assert result.metadata.get("provider_no_deps_rung") is not True
    assert f"$ {REMAINING_INSTALL}" in result.output


def test_no_deps_rung_stays_dormant_when_the_retried_root_install_succeeds():
    """The Plan-2 recovery path is untouched: a provider that DOES satisfy the
    floor still ends at the plain retried root install."""
    root_attempts = 0

    def fail_then_ok(_command):
        nonlocal root_attempts
        root_attempts += 1
        return fail(TVM_MISSING_PROVIDER) if root_attempts == 1 else ok("root installed")

    orch = Orch(
        manifest=dict(TVM_FLOOR_MANIFEST),
        rules=[(TVM_ROOT_INSTALL, fail_then_ok), *tvm_provider_rules()],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert not any("--no-deps" in command for command in orch.commands)
    assert "provider_no_deps_rung" not in result.metadata


def test_no_deps_rung_stays_dormant_when_the_retry_fails_on_another_distribution():
    """The rung exists for the provider's OWN version floor. A retry that fails
    naming a different missing distribution is a different problem and must not
    be papered over with --no-deps."""
    root_attempts = 0

    def other_missing(_command):
        nonlocal root_attempts
        root_attempts += 1
        if root_attempts == 1:
            return fail(TVM_MISSING_PROVIDER)
        return fail("ERROR: No matching distribution found for some-other-dep>=2.0")

    orch = Orch(
        manifest=dict(TVM_FLOOR_MANIFEST),
        rules=[(TVM_ROOT_INSTALL, other_missing), *tvm_provider_rules()],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert not any("--no-deps" in command for command in orch.commands)
    assert "provider_no_deps_rung" not in result.metadata


def test_no_deps_rung_skips_the_follow_up_when_the_provider_is_the_only_declared_dep():
    orch = Orch(
        manifest={
            **TVM_FLOOR_MANIFEST,
            "python_declared_dependencies": ["apache-tvm-ffi>=0.1.13"],
        },
        rules=floor_rules(),
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert orch.commands.count(NO_DEPS_INSTALL) == 1
    assert not any(
        command.startswith("/workspace/tvm/.venv/bin/python -m pip install ml_dtypes")
        for command in orch.commands
    )
    assert result.metadata["provider_no_deps_rung"] is True
