# tests/test_python_tool.py
"""python_tool: setup_env / test / build / compile (spec 2026-07-07 Component 3).

Scripted-orchestrator style (house pattern: tests/test_build_preflight.py,
tests/test_python_preflight.py): substring rules -> canned results, every
command recorded. Contract under test:

- setup_env runs the pre-flight first, then the manifest's install commands
  in ladder order; a failed poetry/pipenv command falls back to the pip rung
  NARRATED as a deviation; a version-shaped pip failure re-provisions and
  reruns exactly once (bounded retry).
- test records the collect-only denominator into COLLECTED_JSON, then runs
  pytest exactly once with --junitxml under PYTEST_REPORT_DIR; test failures
  are an HONEST result, never a rerun trigger.
- build (wheel) is evidence-only: failure carries evidence_only metadata so
  callers never redden a verdict on it.
"""

import importlib.metadata
import json
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import sag.tools.internal.build_preflight as bp
from sag.agent.invocation_receipts import RECEIPT_DIR
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import (
    _NATIVE_PROJECT_READY_SCRIPT,
    _PYTEST_ATTEMPT_TAG_SCRIPT,
    COLLECTED_JSON,
    PYTEST_REPORT_DIR,
    PythonTool,
    verify_project_owned_path,
)


def pytest_runs(orch):
    """Physical pytest invocations recorded on the orchestrator.

    The P0-A invocation receipt quotes the run's own argv inside its JSON
    body, so a bare "-m pytest" substring scan of the command log would count
    one run twice.
    """
    return [
        command
        for command in orch.commands
        if "-m pytest" in command
        and "--collect-only" not in command
        and "--version" not in command
        and RECEIPT_DIR not in command
    ]


def ok(output=""):
    return {"success": True, "exit_code": 0, "output": output}


def fail(output="", exit_code=1):
    return {"success": False, "exit_code": exit_code, "output": output}


class FailThenOk:
    """Stateful rule: fail `times` times, then succeed."""

    def __init__(self, fail_output, times=1):
        self.fail_output = fail_output
        self.remaining = times

    def __call__(self, cmd):
        if self.remaining > 0:
            self.remaining -= 1
            return fail(self.fail_output)
        return ok("")


class Orch:
    """Scriptable orchestrator: first matching substring rule wins."""

    def __init__(self, manifest=None, rules=None, python_output="Python 3.12.4"):
        self.manifest = manifest
        self.rules = list(rules or [])
        self.python_output = python_output
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        if "python3 --version" in cmd:
            return ok(self.python_output)
        if cmd.startswith("cat ") and REQUIREMENTS_PATH in cmd:
            if self.manifest is None:
                return fail("No such file")
            return ok(json.dumps(self.manifest))
        for substring, result in self.rules:
            if substring in cmd:
                return result(cmd) if callable(result) else dict(result)
        return ok("")


class MonitoringOrch(Orch):
    """Orchestrator double that exposes the long-running monitored path."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitored_calls = []

    def execute_command_with_monitoring(
        self,
        cmd,
        *,
        workdir=None,
        silent_timeout=None,
        absolute_timeout=None,
        optimize_for_maven=None,
    ):
        self.monitored_calls.append(
            {
                "command": cmd,
                "workdir": workdir,
                "silent_timeout": silent_timeout,
                "absolute_timeout": absolute_timeout,
                "optimize_for_maven": optimize_for_maven,
            }
        )
        return self.execute_command(cmd, workdir=workdir)


MANIFEST = {
    "python_version": "3.12",
    "python_constraint": ">=3.9",
    "python_venv": "/workspace/proj/.venv",
    "python_installer": "pip",
    "python_install_commands": [
        "{venv}/bin/python -m pip install -r requirements.txt",
        "{venv}/bin/python -m pip install -r requirements-dev.txt",
    ],
    "python_packages": ["proj"],
    "test_hints": {"pytest_args": None, "test_deps": []},
}


TVM_NATIVE_MANIFEST = {
    **MANIFEST,
    "python_venv": "/workspace/tvm/.venv",
    "python_install_commands": [
        "/workspace/tvm/.venv/bin/python -m pip install -e .",
    ],
    "python_distribution_name": "apache-tvm",
    "python_declared_dependencies": ["apache-tvm-ffi>=0.1.13"],
    "python_local_providers": [
        {
            "distribution_name": "apache-tvm-ffi",
            "root": "3rdparty/tvm-ffi",
        }
    ],
    "python_root": "/workspace/tvm",
    "native_build_mode": "pep517-integrated",
    "survey": {"project_path": "/workspace/tvm"},
}

TVM_NATIVE_TEST_MANIFEST = {
    **TVM_NATIVE_MANIFEST,
    "has_native_build": True,
    "python_packages": ["tvm"],
    "python_package_paths": [
        {
            "import_name": "tvm",
            "path": "python/tvm",
            "source": "pyproject.toml:tool.scikit-build.wheel.packages",
        }
    ],
    "native_artifact_roots": ["build", "python/tvm/lib"],
    "python_smoke_candidates": [
        {
            "path": "tests/python/all-platform-minimal-test",
            "source": "pyproject.toml:tool.cibuildwheel.test-command",
        }
    ],
}
TVM_SMOKE_PATH = "tests/python/all-platform-minimal-test"
TVM_SMOKE_REALPATH = f"/workspace/tvm/{TVM_SMOKE_PATH}"

SUBDIR_NATIVE_TEST_MANIFEST = {
    **MANIFEST,
    "survey": {"project_path": "/workspace/tvm"},
    "python_root": "/workspace/tvm/python",
    "python_venv": "/workspace/tvm/python/.venv",
    "python_distribution_name": "native-pkg",
    "python_packages": ["native_pkg"],
    "python_package_paths": [
        {
            "import_name": "native_pkg",
            "path": "src/native_pkg",
            "source": "pyproject.toml:tool.scikit-build.wheel.packages",
        }
    ],
    "has_native_build": True,
    "native_build_mode": "pep517-integrated",
    "native_artifact_roots": [
        "python/_native-build",
        "python/src/native_pkg/lib",
    ],
    "python_smoke_candidates": [
        {
            "path": "python/tests/smoke",
            "source": "pyproject.toml:tool.cibuildwheel.test-command",
        }
    ],
}
SUBDIR_SMOKE_PATH = "python/tests/smoke"
SUBDIR_SMOKE_REALPATH = f"/workspace/tvm/{SUBDIR_SMOKE_PATH}"

TVM_ROOT_INSTALL = "/workspace/tvm/.venv/bin/python -m pip install -e ."
TVM_PROVIDER_ROOT = "/workspace/tvm/3rdparty/tvm-ffi"
TVM_PROVIDER_INSTALL = (
    "/workspace/tvm/.venv/bin/python -m pip install -e /workspace/tvm/3rdparty/tvm-ffi"
)
TVM_MISSING_PROVIDER = (
    "ERROR: Could not find a version that satisfies the requirement "
    "apache-tvm-ffi>=0.1.13 (from apache-tvm)\n"
    "ERROR: No matching distribution found for apache-tvm-ffi>=0.1.13"
)


def tvm_provider_rules(*, provider_name="apache-tvm-ffi", provider_install=None):
    """Exact provider probes; root-install behavior is supplied by each test."""
    return [
        (
            "realpath -m -- /workspace /workspace/tvm " "/workspace/tvm/3rdparty/tvm-ffi",
            ok("/workspace\n/workspace/tvm\n/workspace/tvm/3rdparty/tvm-ffi\n"),
        ),
        (f"test -e {TVM_PROVIDER_ROOT}", ok("EXISTS")),
        (f"test -f {TVM_PROVIDER_ROOT}/pyproject.toml", ok("EXISTS")),
        (
            f"cat {TVM_PROVIDER_ROOT}/pyproject.toml",
            ok(f'[project]\nname = "{provider_name}"\nversion = "0.1.13"\n'),
        ),
        (
            TVM_PROVIDER_INSTALL,
            provider_install if provider_install is not None else ok("provider installed"),
        ),
    ]


def tvm_native_smoke_rules(collection_output, *, native_ready=False, target_exists=True):
    return [
        (
            "SAG_NATIVE_PROJECT_READY",
            ok("SAG_NATIVE_PROJECT_READY") if native_ready else fail("not ready"),
        ),
        (
            f"realpath -m -- /workspace /workspace/tvm {TVM_SMOKE_REALPATH}",
            ok(f"/workspace\n/workspace/tvm\n{TVM_SMOKE_REALPATH}\n"),
        ),
        (
            f"test -e {TVM_SMOKE_REALPATH}",
            ok("EXISTS") if target_exists else ok("MISSING"),
        ),
        (
            f"--collect-only -q {TVM_SMOKE_PATH} --maxfail=1",
            ok(collection_output),
        ),
    ]


def subdir_native_smoke_rules(collection_output):
    return [
        ("SAG_NATIVE_PROJECT_READY", fail("not ready")),
        (
            f"realpath -m -- /workspace /workspace/tvm {SUBDIR_SMOKE_REALPATH}",
            ok(f"/workspace\n/workspace/tvm\n{SUBDIR_SMOKE_REALPATH}\n"),
        ),
        (f"test -e {SUBDIR_SMOKE_REALPATH}", ok("EXISTS")),
        (
            f"--collect-only -q {SUBDIR_SMOKE_REALPATH} --maxfail=1",
            ok(collection_output),
        ),
    ]


def compile_metrics(
    source_count,
    compiled_source_count,
    *,
    status="valid",
    foreign_pyc_count=0,
):
    coverage = compiled_source_count / source_count if status == "valid" and source_count else None
    return ok(
        json.dumps(
            {
                "status": status,
                "source_count": source_count,
                "compiled_source_count": compiled_source_count,
                "missing_source_count": max(source_count - compiled_source_count, 0),
                "foreign_pyc_count": foreign_pyc_count,
                "coverage": coverage,
                "cache_tag": "cpython-312",
                "conflicts": ["metrics_conflict"] if status == "invalid" else [],
                "missing_sources": [],
                "foreign_pycs": (
                    ["/workspace/proj/src/proj/__pycache__/old.cpython-311.pyc"]
                    if foreign_pyc_count
                    else []
                ),
            }
        )
    )


# ---------------------------------------------------------------------------
# setup_env
# ---------------------------------------------------------------------------


def test_setup_env_runs_preflight_then_install_commands_in_ladder_order():
    orch = Orch(manifest=dict(MANIFEST))
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is True
    preflight = next(i for i, c in enumerate(orch.commands) if "python3 --version" in c)
    first = next(
        i
        for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -r requirements.txt" in c
    )
    second = next(
        i
        for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -r requirements-dev.txt" in c
    )
    # Pre-flight first, then the manifest commands in ladder order, with the
    # {venv} placeholder filled from the manifest venv.
    assert preflight < first < second


def test_setup_env_creates_missing_venv_before_installing():
    orch = Orch(manifest=dict(MANIFEST))  # no EXISTS rule -> venv missing
    PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    venv_create = next(
        i for i, c in enumerate(orch.commands) if "-m venv /workspace/proj/.venv" in c
    )
    first_install = next(
        i for i, c in enumerate(orch.commands) if "pip install -r requirements.txt" in c
    )
    assert venv_create < first_install


def test_setup_env_skips_venv_creation_when_present():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("test -x /workspace/proj/.venv/bin/python", ok("EXISTS"))],
    )
    PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert not any("-m venv" in c for c in orch.commands)


def test_setup_env_poetry_failure_falls_back_to_pip_narrated_as_deviation():
    manifest = {
        **MANIFEST,
        "python_installer": "poetry",
        "python_install_commands": ["poetry install"],
    }
    orch = Orch(manifest=manifest, rules=[("poetry install", fail("poetry: boom"))])
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    attempted = next(i for i, c in enumerate(orch.commands) if "poetry install" in c)
    fallback = next(
        i
        for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -e ." in c
    )
    assert attempted < fallback  # the project's own tool was tried FIRST
    # The deviation is narrated in the observation — the generated setup docs
    # must reflect what actually ran (spec Component 3).
    assert (
        "[deviation] poetry install failed; fell back to pip install -e . "
        "— setup docs must list the fallback"
    ) in result.output
    assert result.succeeded is True


def test_setup_env_mismatch_preflight_narration_is_prepended(monkeypatch):
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    manifest = {**MANIFEST, "python_version": "3.11", "python_constraint": ">=3.11"}
    orch = Orch(manifest=manifest, python_output="Python 3.8.10")
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.output.startswith("[pre-flight] Required: Python 3.11")
    assert "uv-provisioned 3.11" in result.output
    # The uv provisioning already created the venv; no second creation.
    assert not any("-m venv" in c for c in orch.commands)


def test_version_shaped_install_failure_reprovisions_and_reruns_once(monkeypatch):
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    manifest = {
        **MANIFEST,
        "python_version": None,
        "python_constraint": None,
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
    }
    orch = Orch(
        manifest=manifest,
        rules=[
            (
                "pip install -e .",
                FailThenOk(
                    "ERROR: Package 'proj' requires a different Python: " "3.12.4 not in '>=3.13'",
                    times=1,
                ),
            )
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    attempts = [c for c in orch.commands if "pip install -e ." in c]
    assert len(attempts) == 2  # initial + exactly one retry
    assert any("uv python install 3.13" in c for c in orch.commands)  # re-provisioned
    assert "retry 1/1" in result.output
    assert result.succeeded is True


def test_version_retry_is_bounded_to_exactly_once(monkeypatch):
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    manifest = {
        **MANIFEST,
        "python_version": None,
        "python_constraint": None,
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
    }
    orch = Orch(
        manifest=manifest,
        rules=[
            (
                "pip install -e .",
                FailThenOk(
                    "ERROR: Package 'proj' requires a different Python: " "3.12.4 not in '>=3.13'",
                    times=99,
                ),
            )
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    attempts = [c for c in orch.commands if "pip install -e ." in c]
    assert len(attempts) == 2  # never more than one retry, even on repeat failure
    assert result.succeeded is False


def test_native_setup_recovers_exact_local_provider_then_retries_root_command():
    root_install = FailThenOk(TVM_MISSING_PROVIDER)
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, root_install),
            *tvm_provider_rules(),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert [command for command in orch.commands if command == TVM_ROOT_INSTALL] == [
        TVM_ROOT_INSTALL,
        TVM_ROOT_INSTALL,
    ]
    assert [command for command in orch.commands if command == TVM_PROVIDER_INSTALL] == [
        TVM_PROVIDER_INSTALL
    ]
    first_root = orch.commands.index(TVM_ROOT_INSTALL)
    provider = orch.commands.index(TVM_PROVIDER_INSTALL)
    second_root = len(orch.commands) - 1 - orch.commands[::-1].index(TVM_ROOT_INSTALL)
    assert first_root < provider < second_root
    assert result.metadata["local_provider_recovery"] == {
        "distribution_name": "apache-tvm-ffi",
        "provider_root": "3rdparty/tvm-ffi",
        "provider_command": TVM_PROVIDER_INSTALL,
        "provider_succeeded": True,
        "root_retry": True,
    }
    install_commands = [
        command for command in orch.commands if " -m pip install " in f" {command} "
    ]
    assert all("SETUPTOOLS_SCM_PRETEND_VERSION" not in command for command in install_commands)
    assert all("--no-deps" not in command for command in install_commands)


def test_native_setup_recovers_provider_when_wrapper_masks_pip_failure_as_success():
    root_calls = 0

    def masked_failure_then_success(_command):
        nonlocal root_calls
        root_calls += 1
        if root_calls == 1:
            return {
                "success": True,
                "exit_code": 0,
                "output": TVM_MISSING_PROVIDER,
            }
        return ok("root installed")

    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, masked_failure_then_success),
            *tvm_provider_rules(),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert orch.commands.count(TVM_ROOT_INSTALL) == 2
    assert orch.commands.count(TVM_PROVIDER_INSTALL) == 1
    assert result.metadata["local_provider_recovery"]["provider_succeeded"] is True
    assert result.metadata["local_provider_recovery"]["root_retry"] is True


def test_native_setup_does_not_preinstall_provider_when_root_install_succeeds():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[(TVM_ROOT_INSTALL, ok("root installed"))],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any(TVM_PROVIDER_ROOT in command for command in orch.commands)
    assert "local_provider_recovery" not in result.metadata


def test_native_setup_rejects_provider_whose_pyproject_name_does_not_match():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER)),
            *tvm_provider_rules(provider_name="not-apache-tvm-ffi"),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert TVM_PROVIDER_INSTALL not in orch.commands
    assert "local_provider_recovery" not in result.metadata


def test_native_setup_rejects_manifest_provider_name_mismatch_without_probing():
    manifest = {
        **TVM_NATIVE_MANIFEST,
        "python_local_providers": [
            {
                "distribution_name": "some-other-ffi",
                "root": "3rdparty/tvm-ffi",
            }
        ],
    }
    orch = Orch(
        manifest=manifest,
        rules=[(TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER))],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any("realpath -m" in command for command in orch.commands)
    assert not any(TVM_PROVIDER_ROOT in command for command in orch.commands)


def test_native_setup_rejects_provider_not_declared_by_root_project():
    manifest = {
        **TVM_NATIVE_MANIFEST,
        "python_declared_dependencies": ["numpy>=2"],
    }
    orch = Orch(
        manifest=manifest,
        rules=[(TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER))],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any("realpath -m" in command for command in orch.commands)
    assert TVM_PROVIDER_INSTALL not in orch.commands


def test_native_setup_rejects_ambiguous_matching_providers_without_probing():
    manifest = {
        **TVM_NATIVE_MANIFEST,
        "python_local_providers": [
            {
                "distribution_name": "apache-tvm-ffi",
                "root": "3rdparty/tvm-ffi",
            },
            {
                "distribution_name": "apache_tvm_ffi",
                "root": "vendor/tvm-ffi",
            },
        ],
    }
    orch = Orch(
        manifest=manifest,
        rules=[(TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER))],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any("realpath -m" in command for command in orch.commands)
    assert TVM_PROVIDER_INSTALL not in orch.commands


def test_native_setup_rejects_provider_path_escape_without_probing():
    manifest = {
        **TVM_NATIVE_MANIFEST,
        "python_local_providers": [
            {
                "distribution_name": "apache-tvm-ffi",
                "root": "../outside/tvm-ffi",
            }
        ],
    }
    orch = Orch(
        manifest=manifest,
        rules=[(TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER))],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any("realpath -m" in command for command in orch.commands)
    assert not any("outside/tvm-ffi" in command for command in orch.commands)


def test_native_setup_rejects_provider_symlink_that_resolves_outside_project():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER)),
            (
                "realpath -m -- /workspace /workspace/tvm " "/workspace/tvm/3rdparty/tvm-ffi",
                ok("/workspace\n/workspace/tvm\n/opt/shared/tvm-ffi\n"),
            ),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any(
        f"test -f {TVM_PROVIDER_ROOT}/pyproject.toml" in command
        for command in orch.commands
    )
    assert TVM_PROVIDER_INSTALL not in orch.commands


def test_native_setup_rejects_provider_when_checkout_root_resolves_outside_workspace():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER)),
            (
                "realpath -m -- /workspace /workspace/tvm " "/workspace/tvm/3rdparty/tvm-ffi",
                ok("/workspace\n" "/opt/shared/tvm\n" "/opt/shared/tvm/3rdparty/tvm-ffi\n"),
            ),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert not any(
        f"test -f {TVM_PROVIDER_ROOT}/pyproject.toml" in command
        for command in orch.commands
    )
    assert TVM_PROVIDER_INSTALL not in orch.commands


def test_native_setup_provider_install_failure_does_not_retry_root():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, fail(TVM_MISSING_PROVIDER)),
            *tvm_provider_rules(provider_install=fail("provider build failed")),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 1
    assert orch.commands.count(TVM_PROVIDER_INSTALL) == 1
    assert result.metadata["local_provider_recovery"]["provider_succeeded"] is False
    assert result.metadata["local_provider_recovery"]["root_retry"] is False
    assert "provider build failed" in (result.error or "")


def test_native_setup_root_retry_is_bounded_to_once_after_provider_success():
    orch = Orch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            (TVM_ROOT_INSTALL, FailThenOk(TVM_MISSING_PROVIDER, times=99)),
            *tvm_provider_rules(),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert orch.commands.count(TVM_ROOT_INSTALL) == 2
    assert orch.commands.count(TVM_PROVIDER_INSTALL) == 1
    assert result.metadata["local_provider_recovery"]["root_retry"] is True


def test_native_pep517_setup_uses_extended_monitored_timeout():
    orch = MonitoringOrch(
        manifest=dict(TVM_NATIVE_MANIFEST),
        rules=[
            ("test -x /workspace/tvm/.venv/bin/python", ok("EXISTS")),
            (TVM_ROOT_INSTALL, ok("root installed")),
        ],
    )

    result = PythonTool(orch).execute(
        "setup_env",
        working_directory="/workspace/tvm",
        timeout=37,
    )

    assert result.succeeded is True
    root_call = next(call for call in orch.monitored_calls if call["command"] == TVM_ROOT_INSTALL)
    assert root_call == {
        "command": TVM_ROOT_INSTALL,
        "workdir": "/workspace/tvm",
        "silent_timeout": 2400,
        "absolute_timeout": 2400,
        "optimize_for_maven": False,
    }


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_test_writes_collected_denominator_and_junitxml_report():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("tests/test_a.py::test_x\n42 tests collected in 0.12s"))],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.succeeded is True
    writes = [c for c in orch.commands if COLLECTED_JSON in c and "<<" in c]
    assert writes and '"collected": 42' in writes[0]
    runs = pytest_runs(orch)
    assert len(runs) == 1
    assert runs[0].startswith("/workspace/proj/.venv/bin/python -m pytest")
    assert f"--junitxml={PYTEST_REPORT_DIR}/pytest-" in runs[0]
    # collect-only denominator is recorded BEFORE the honest run
    collect = next(i for i, c in enumerate(orch.commands) if "--collect-only" in c)
    run = next(i for i, c in enumerate(orch.commands) if "--junitxml" in c)
    assert collect < run


def test_test_marks_docker_dispatch_failure_as_no_runner_execution():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("1 test collected in 0.01s")),
            (
                "--junitxml",
                {
                    "success": False,
                    "exit_code": -1,
                    "output": "docker exec was never accepted",
                    "dispatch_status": "dispatch_failed",
                    "runner_dispatched": False,
                },
            ),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert result.succeeded is False
    assert result.metadata["command"].endswith(
        "--junitxml=/workspace/.setup_agent/pytest-reports/pytest-attempt-000001.xml"
    )
    assert result.metadata["runner_dispatched"] is False


def test_test_nonzero_exit_remains_a_physical_runner_execution():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("1 test collected in 0.01s")),
            (
                "--junitxml",
                {
                    "success": False,
                    "exit_code": 1,
                    "output": "1 failed in 0.01s",
                    "runner_dispatched": True,
                },
            ),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert result.metadata["runner_dispatched"] is True


def test_test_assigns_monotonic_attempt_ids_and_persists_them_in_junit():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("1 test collected in 0.01s"))],
    )
    tool = PythonTool(orch)

    first = tool.execute("test", working_directory="/workspace/proj")
    second = tool.execute("test", working_directory="/workspace/proj")

    assert first.metadata["attempt_id"] == 1
    assert second.metadata["attempt_id"] == 2
    assert first.metadata["report"].endswith("pytest-attempt-000001.xml")
    assert second.metadata["report"].endswith("pytest-attempt-000002.xml")
    tag_commands = [command for command in orch.commands if "SAG_ATTEMPT_TAGGED" in command]
    assert len(tag_commands) == 2
    assert " 1" in tag_commands[0]
    assert " 2" in tag_commands[1]


def test_attempt_tag_script_writes_suite_property_atomically(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase '
        'classname="tests.test_api" name="test_ok"/></testsuite></testsuites>'
    )

    completed = subprocess.run(
        [sys.executable, "-c", _PYTEST_ATTEMPT_TAG_SCRIPT, str(report), "7"],
        capture_output=True,
        text=True,
        check=True,
    )

    root = ET.parse(report).getroot()
    properties = {element.get("name"): element.get("value") for element in root.iter("property")}
    assert completed.stdout.strip() == "SAG_ATTEMPT_TAGGED"
    assert properties["sag.attempt_id"] == "7"
    assert not (tmp_path / "report.xml.attempt.tmp").exists()


def test_native_ready_script_rejects_external_symlinked_artifact(tmp_path, monkeypatch):
    project = tmp_path / "project"
    build = project / "build"
    outside = tmp_path / "outside"
    build.mkdir(parents=True)
    outside.mkdir()
    external_artifact = outside / "libnative.so"
    external_artifact.write_bytes(b"not project owned")
    (build / "libnative.so").symlink_to(external_artifact)

    class Distribution:
        metadata = {"Name": "demo-native"}

        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            return json.dumps(
                {
                    "url": project.as_uri(),
                    "dir_info": {"editable": True},
                }
            )

    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native-ready",
            "demo-native",
            str(project),
            str(project),
            json.dumps(["json"]),
            json.dumps(["build"]),
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        exec(_NATIVE_PROJECT_READY_SCRIPT, {"__name__": "__main__"})

    assert exc_info.value.code == 1


def test_native_ready_script_rejects_symlinked_checkout_with_owned_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside_project = tmp_path / "outside" / "project"
    artifact = outside_project / "build" / "libnative.so"
    workspace.mkdir()
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"native")
    checkout = workspace / "tvm"
    checkout.symlink_to(outside_project, target_is_directory=True)

    class Distribution:
        metadata = {"Name": "demo-native"}

        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            return json.dumps(
                {
                    "url": outside_project.as_uri(),
                    "dir_info": {"editable": True},
                }
            )

    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native-ready",
            "demo-native",
            str(checkout),
            str(checkout),
            json.dumps(["json"]),
            json.dumps(["build"]),
            str(workspace),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        exec(_NATIVE_PROJECT_READY_SCRIPT, {"__name__": "__main__"})

    assert exc_info.value.code == 1


def test_project_owned_path_rejects_checkout_root_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside" / "shared"
    candidate = outside / "tests" / "test_runtime.py"
    workspace.mkdir()
    candidate.parent.mkdir(parents=True)
    candidate.write_text("def test_runtime(): pass\n", encoding="utf-8")
    (workspace / "tvm").symlink_to(outside, target_is_directory=True)

    def physical_path(logical):
        assert logical == "/workspace" or logical.startswith("/workspace/")
        suffix = logical.removeprefix("/workspace").lstrip("/")
        return workspace / suffix if suffix else workspace

    def execute(command):
        tokens = shlex.split(command)
        if tokens[:3] == ["realpath", "-m", "--"]:
            resolved = [str(physical_path(logical).resolve(strict=False)) for logical in tokens[3:]]
            return ok("\n".join(resolved) + "\n")
        if tokens[:2] == ["test", "-e"]:
            return ok("EXISTS" if physical_path(tokens[2]).exists() else "MISSING")
        raise AssertionError(f"unexpected command: {command}")

    owned, reason = verify_project_owned_path(
        execute,
        "/workspace/tvm",
        "/workspace/tvm/tests/test_runtime.py",
    )

    assert owned is False
    assert "project root real path escapes" in (reason or "")


def test_pytest_failures_are_honest_and_never_rerun():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("5 tests collected in 0.01s")),
            ("--junitxml", fail("....\n2 failed, 3 passed in 1.23s", exit_code=1)),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    runs = pytest_runs(orch)
    assert len(runs) == 1  # exit 1 with failures is an HONEST result, not an error to retry
    # Bug #13 defect 6: tests that RAN with failures are an honest green —
    # the result (stats in output) is the deliverable, not an error state.
    assert result.succeeded is True
    assert "2 failed, 3 passed" in result.output
    assert result.metadata.get("exit_code") == 1


def test_no_tests_collected_records_zero_denominator():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", fail("no tests collected in 0.01s", exit_code=5)),
            ("--junitxml", fail("no tests ran in 0.01s", exit_code=5)),
        ],
    )
    PythonTool(orch).execute("test", working_directory="/workspace/proj")
    writes = [c for c in orch.commands if COLLECTED_JSON in c and "<<" in c]
    assert writes and '"collected": 0' in writes[0]  # 0 detected, honestly — never invented


# ---------------------------------------------------------------------------
# build (wheel — evidence only, never required for green)
# ---------------------------------------------------------------------------


def test_build_failure_carries_evidence_only_metadata():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("-m build --wheel", fail("ERROR Backend subprocess exited"))],
    )
    result = PythonTool(orch).execute("build", working_directory="/workspace/proj")
    assert result.succeeded is False
    assert result.metadata.get("evidence_only") is True  # callers must not redden on this


def test_build_installs_build_into_the_venv_first():
    orch = Orch(manifest=dict(MANIFEST))
    result = PythonTool(orch).execute("build", working_directory="/workspace/proj")
    installed = next(
        i
        for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install build" in c
    )
    built = next(i for i, c in enumerate(orch.commands) if "-m build --wheel" in c)
    assert installed < built
    assert result.succeeded is True
    assert result.metadata.get("evidence_only") is True


# ---------------------------------------------------------------------------
# compile (the compileall evidence generator)
# ---------------------------------------------------------------------------


def test_compile_runs_compileall_over_package_dirs_and_reports_counts():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -d /workspace/proj/src/proj", ok("EXISTS")),
            ("importlib.util", compile_metrics(10, 8)),
        ],
    )
    result = PythonTool(orch).execute("compile", working_directory="/workspace/proj")
    compileall = [c for c in orch.commands if "-m compileall -q" in c]
    assert compileall and "/workspace/proj/src/proj" in compileall[0]
    assert result.succeeded is True
    assert "8/10" in result.output
    assert result.metadata.get("py_count") == 10
    assert result.metadata.get("pyc_count") == 8
    assert result.metadata.get("failed") == 2
    assert result.metadata.get("compileall_metric_status") == "valid"


def test_compile_foreign_pyc_is_invalid_and_never_clamped_to_full_coverage():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -d /workspace/proj/src/proj", ok("EXISTS")),
            (
                "importlib.util",
                compile_metrics(
                    10,
                    10,
                    status="invalid",
                    foreign_pyc_count=1,
                ),
            ),
        ],
    )

    result = PythonTool(orch).execute("compile", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert "invalid" in result.output
    assert result.metadata["coverage"] is None
    assert result.metadata["compileall_metric_status"] == "invalid"
    assert result.metadata["foreign_pyc_count"] == 1
    assert "metrics_conflict" in result.conflicts


# ---------------------------------------------------------------------------
# operation surface
# ---------------------------------------------------------------------------


def test_unknown_operation_is_rejected_with_the_valid_vocabulary():
    result = PythonTool(Orch(manifest=dict(MANIFEST))).execute(
        "frobnicate", working_directory="/workspace/proj"
    )
    assert result.succeeded is False
    assert result.error_code == "UNKNOWN_PYTHON_OPERATION"
    assert any("setup_env" in s for s in result.suggestions)


# ---------------------------------------------------------------------------
# Bug #13 defect 1: venv repair everywhere — an earlier phase can leave a
# pip-less/broken .venv that the pre-flight never repairs because the venv
# already exists (live evidence: /workspace/paramiko/.venv without pip,
# deps failed 3x). Probe -> ensurepip once -> recreate, narrated.
# ---------------------------------------------------------------------------


def test_setup_env_repairs_pip_less_venv_with_ensurepip():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            ("-m pip --version", FailThenOk("No module named pip", times=1)),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    ensurepip = next(i for i, c in enumerate(orch.commands) if "-m ensurepip" in c)
    first_install = next(
        i for i, c in enumerate(orch.commands) if "pip install -r requirements.txt" in c
    )
    assert ensurepip < first_install  # repaired BEFORE anything installs
    assert "[env] existing venv was missing pip — repaired" in result.output
    assert result.succeeded is True


def test_setup_env_recreates_venv_when_ensurepip_cannot_restore_pip():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            # probe fails before ensurepip AND after: only recreation restores pip
            ("-m pip --version", FailThenOk("No module named pip", times=2)),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    recreate = next(
        i
        for i, c in enumerate(orch.commands)
        if "python3 -m venv --clear /workspace/proj/.venv" in c
    )
    first_install = next(
        i for i, c in enumerate(orch.commands) if "pip install -r requirements.txt" in c
    )
    assert recreate < first_install
    assert "[env] existing venv was missing pip — recreated" in result.output
    assert result.succeeded is True


# ---------------------------------------------------------------------------
# Bug #13 defect 2: honest failure on install errors — live evidence: deps
# claimed "✅ build executed successfully" while stderr said "No module named
# pip" and nothing installed.
# ---------------------------------------------------------------------------


def test_deps_install_error_with_zero_exit_is_an_honest_failure():
    manifest = {
        **MANIFEST,
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
    }
    orch = Orch(
        manifest=manifest,
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            # The live failure shape: the wrapper reported exit 0 while the
            # output carried the fatal install error.
            ("pip install -e .", ok("/workspace/proj/.venv/bin/python: No module named pip")),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is False
    assert "No module named pip" in (result.error or "")


def test_failed_install_observation_leads_with_the_failure():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            (
                "pip install -r requirements.txt",
                fail("/workspace/proj/.venv/bin/python: No module named pip"),
            ),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is False
    # The observation LEADS with the failure — never buried under transcript.
    assert result.output.splitlines()[0].startswith("[setup] dependency install FAILED")
    assert "No module named pip" in (result.error or "")


# ---------------------------------------------------------------------------
# Bug #13 defect 3 (narration side): a manifest whose pip rung has no test
# extras must say so, so missing pytest/icecream is never silent (paramiko).
# ---------------------------------------------------------------------------


def test_setup_env_narrates_missing_test_extras_note():
    manifest = {
        **MANIFEST,
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
        "python_install_note": "no test extras declared — test deps may be missing",
    }
    orch = Orch(
        manifest=manifest,
        rules=[("test -x /workspace/proj/.venv/bin/python", ok("EXISTS"))],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is True
    assert "no test extras declared — test deps may be missing" in result.output


# ---------------------------------------------------------------------------
# Bug #13 defect 4: self-healing deps — an empty manifest (agent skipped
# project analyze) must not no-op with success; detect the ladder inline
# from the marker files sitting right there, or fail honestly.
# ---------------------------------------------------------------------------


def test_setup_env_empty_manifest_detects_installer_ladder_inline():
    orch = Orch(
        manifest=None,  # no build-requirements manifest at all
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            ("ls -A1 /workspace/proj", ok("pyproject.toml\nsrc\nREADME.md")),
            (
                "cat /workspace/proj/pyproject.toml",
                ok(
                    '[project]\nname = "proj"\n\n[project.optional-dependencies]\ntest = ["pytest"]\n'
                ),
            ),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is True
    assert "[setup] manifest empty — detected installer ladder inline" in result.output
    assert any(
        "/workspace/proj/.venv/bin/python -m pip install -e '.[test]'" in c for c in orch.commands
    )


def test_setup_env_empty_manifest_executes_pep735_dev_dependencies_inline():
    pyproject = """\
[project]
name = "paramiko"
[dependency-groups]
dev = ["pytest-relaxed>=2", "icecream>=2.1"]
"""
    orch = Orch(
        manifest=None,
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            ("ls -A1 /workspace/proj", ok("pyproject.toml\nparamiko")),
            ("cat /workspace/proj/pyproject.toml", ok(pyproject)),
        ],
    )

    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert any("pip install 'pytest-relaxed>=2' 'icecream>=2.1'" in c for c in orch.commands)
    assert "no test extras declared" not in result.output


def test_setup_env_empty_manifest_and_no_markers_fails_with_analyze_guidance():
    orch = Orch(
        manifest=None,
        rules=[
            ("test -x /workspace/proj/.venv/bin/python", ok("EXISTS")),
            ("ls -A1 /workspace/proj", ok("README.md\nLICENSE")),
        ],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.succeeded is False  # NEVER a vacuous green no-op
    assert result.error_code == "PYTHON_NO_INSTALLER_DETECTED"
    assert any("project(action='analyze')" in s for s in result.suggestions)
    assert not any("pip install" in c for c in orch.commands)


# ---------------------------------------------------------------------------
# Bug #13 defect 5: pytest bootstrap — live evidence: 5 test calls failed
# with 'No module named pytest' and still looked successful.
# ---------------------------------------------------------------------------


def test_test_bootstraps_pytest_into_the_venv_when_missing():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("-m pytest --version", fail("No module named pytest")),
            ("--collect-only", ok("3 tests collected in 0.01s")),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    probe = next(i for i, c in enumerate(orch.commands) if "-m pytest --version" in c)
    install = next(
        i
        for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install pytest" in c
    )
    collect = next(i for i, c in enumerate(orch.commands) if "--collect-only" in c)
    assert probe < install < collect  # probe -> install once -> only then run
    assert "[test] pytest not in venv — installed for the run" in result.output


# ---------------------------------------------------------------------------
# Bug #13 defect 6: honest test results — collection/usage errors and zero
# collected must never be green ("Exit code: 0" was shown for collection
# errors in the live run).
# ---------------------------------------------------------------------------


def test_collection_errors_are_never_green_even_with_exit_zero():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("2 tests collected in 0.05s")),
            (
                "--junitxml",
                ok(
                    "==== ERRORS ====\n"
                    "ERROR collecting tests/test_x.py\n"
                    "ModuleNotFoundError: No module named 'icecream'\n"
                    "!!!!! Interrupted: 1 error during collection !!!!!"
                ),
            ),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.succeeded is False
    assert result.error_code == "PYTEST_COLLECTION_ERROR"
    assert "ERROR collecting tests/test_x.py" in (result.error or "")


def test_conftest_import_error_is_never_green_when_stream_exit_is_unknown():
    # Exact live Paramiko shape: Docker streaming lost the exit status and
    # reported 0, while pytest aborted before it could emit its usual
    # "ERROR collecting" header or write JUnit XML.
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            (
                "--collect-only",
                ok(
                    "STDERR: ImportError while loading conftest "
                    "'/workspace/paramiko/tests/conftest.py'.\n"
                    "E   ModuleNotFoundError: No module named 'icecream'"
                ),
            ),
            (
                "--junitxml",
                ok(
                    "STDERR: ImportError while loading conftest "
                    "'/workspace/paramiko/tests/conftest.py'.\n"
                    "STDERR: tests/conftest.py:8: in <module>\n"
                    "    from icecream import ic\n"
                    "E   ModuleNotFoundError: No module named 'icecream'"
                ),
            ),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert result.succeeded is False
    assert result.error_code == "PYTEST_COLLECTION_ERROR"
    assert "ImportError while loading conftest" in (result.error or "")


def test_usage_errors_are_never_green():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("2 tests collected in 0.05s")),
            (
                "--junitxml",
                fail(
                    "ERROR: usage: __main__.py [options] [file_or_dir]\n"
                    "__main__.py: error: unrecognized arguments: --frobnicate",
                    exit_code=4,
                ),
            ),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.succeeded is False
    assert result.error_code == "PYTEST_USAGE_ERROR"


def test_zero_collected_is_never_green():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", fail("no tests collected in 0.01s", exit_code=5)),
            ("--junitxml", fail("no tests ran in 0.01s", exit_code=5)),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.succeeded is False
    assert result.error_code == "PYTEST_NO_TESTS"


# ---------------------------------------------------------------------------
# Reviewer-confirmed defect (criterion f): the text-only usage/collection
# signatures substring-matched ANYWHERE in the run output — including the
# captured stdout/stderr of the tests under test — and were checked BEFORE
# the exit-1 + failed-stats honest-results branch. Any CLI-heavy project
# (argparse/click) with a failing argument-handling test tripped this.
# ---------------------------------------------------------------------------

# Forensic evidence: exit 1, honest '1 failed, 5 passed' summary, a failing
# CLI test's captured stderr carrying argparse's standard error text.
_ARGPARSE_FAILURE_OUTPUT = (
    "==================================== FAILURES ====================================\n"
    "________________________________ test_cli_rejects ________________________________\n"
    "    def test_cli_rejects():\n"
    ">       main(['--bogus'])\n"
    "E       SystemExit: 2\n"
    "----------------------------- Captured stderr call -----------------------------\n"
    "usage: prog [-h] [--count COUNT]\n"
    "prog: error: unrecognized arguments: --bogus\n"
    "============================ short test summary info ============================\n"
    "FAILED tests/test_cli.py::test_cli_rejects - SystemExit: 2\n"
    "========================== 1 failed, 5 passed in 0.34s ==========================="
)


def test_failing_cli_test_with_captured_argparse_stderr_is_an_honest_result():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("6 tests collected in 0.05s")),
            ("--junitxml", fail(_ARGPARSE_FAILURE_OUTPUT, exit_code=1)),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    # The suite RAN and reported honest stats — captured argparse text from
    # the tests under test is NOT a pytest usage error.
    assert result.succeeded is True
    assert result.error_code is None
    assert "1 failed, 5 passed" in result.output


def test_passing_suite_with_captured_argparse_stderr_stays_green():
    from sag.tools.internal.python_tool import _classify_pytest_result

    # Adjacent false-red: fully PASSING suite (exit 0) whose output shows the
    # argparse text (e.g. run with -s, or log_cli on a CLI-heavy project).
    output = (
        "tests/test_cli.py::test_usage_error PASSED\n"
        "usage: prog [-h] [--count COUNT]\n"
        "prog: error: unrecognized arguments: --bogus\n"
        "=============================== 6 passed in 0.21s ==============================="
    )
    success, error, error_code = _classify_pytest_result(0, output)
    assert success is True
    assert error is None
    assert error_code is None


def test_collection_error_text_in_assertion_repr_with_failed_stats_is_honest():
    from sag.tools.internal.python_tool import _classify_pytest_result

    # 'ERROR collecting' quoted inside a failing assertion repr at exit 1
    # with honest failed stats present is a RESULT, not a collection error.
    output = (
        "==================================== FAILURES ====================================\n"
        "_______________________________ test_log_scraper ________________________________\n"
        "E       AssertionError: assert 'ERROR collecting tests/x.py' not in log\n"
        "========================== 1 failed, 3 passed in 0.11s ==========================="
    )
    success, error, error_code = _classify_pytest_result(1, output)
    assert success is True
    assert error is None
    assert error_code is None


def test_pytest_own_usage_error_line_is_still_red_when_the_exit_code_lies():
    from sag.tools.internal.python_tool import _classify_pytest_result

    # A wrapper reporting exit 0 must not mask pytest's OWN usage error —
    # the anchored line-start 'ERROR: usage:' shape, no stats line.
    output = (
        "ERROR: usage: __main__.py [options] [file_or_dir]\n"
        "__main__.py: error: unrecognized arguments: --frobnicate"
    )
    success, error, error_code = _classify_pytest_result(0, output)
    assert success is False
    assert error_code == "PYTEST_USAGE_ERROR"
    assert "ERROR: usage:" in (error or "")


# ---------------------------------------------------------------------------
# Bug #13 defect 7: arg sanitizing — 'make test' was pasted verbatim into
# the pytest command line ('pytest make test') in the live run.
# ---------------------------------------------------------------------------


def test_non_pytest_args_are_rejected_before_anything_runs():
    for bad in ("make test", "test-python", "-C /workspace/proj test"):
        orch = Orch(manifest=dict(MANIFEST))
        result = PythonTool(orch).execute("test", working_directory="/workspace/proj", args=bad)
        assert result.succeeded is False, bad
        assert result.error_code == "PYTEST_ARGS_REJECTED", bad
        assert result.failure_signature == "pytest_args_rejected:invalid_selector", bad
        # Nothing pytest ran — the bogus args never reach a command line.
        assert not any("-m pytest" in c for c in orch.commands), bad
        # The message names the correct usage.
        assert any("-k" in s for s in result.suggestions), bad


def test_pytest_plausible_args_pass_through_sanitizing():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -e /workspace/proj/tests/test_a.py", ok("EXISTS")),
            ("--collect-only", ok("1 test collected in 0.01s")),
        ],
    )
    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/proj",
        args="-k smoke -x --maxfail=2 tests/test_a.py",
    )
    run = next(c for c in orch.commands if "--junitxml" in c)
    assert "-k smoke" in run
    assert "-x" in run
    assert "--maxfail=2" in run
    assert "tests/test_a.py" in run
    assert result.succeeded is True


# ---------------------------------------------------------------------------
# Bug #13 defect 8: vacuous compile — compileall over 0 sources must say so
# instead of a misleading '0/0 sources compiled' green.
# ---------------------------------------------------------------------------


def test_compile_zero_sources_is_vacuous_and_says_so():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -d /workspace/proj/src/proj", ok("EXISTS")),
            (
                "importlib.util",
                compile_metrics(
                    0,
                    0,
                    status="unavailable",
                ),
            ),
        ],
    )
    result = PythonTool(orch).execute("compile", working_directory="/workspace/proj")
    assert result.succeeded is True  # vacuous, not a failure — but never misleading
    assert "no sources found under /workspace/proj/src/proj — nothing verified" in result.output
    assert result.metadata.get("vacuous") is True


# ---------------------------------------------------------------------------
# setup tool python branch (Task 7): the SAME shared installer ladder —
# PythonPreflight (manifest) -> venv -> detect_installer commands -> overlay.
# The ladder strings live ONLY in python_env.detect_installer.
# ---------------------------------------------------------------------------


def test_setup_tool_python_branch_issues_the_shared_ladder_commands():
    from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
    from sag.tools.internal.project_setup_tool import ProjectSetupTool
    from sag.tools.internal.python_env import detect_installer

    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("ls -A1 /workspace/proj", ok("poetry.lock\npyproject.toml\nsrc"))],
    )
    result = ProjectSetupTool(orch)._install_dependencies_for_project_type(
        {
            "type": "python",
            "build_files": ["pyproject.toml"],
            "language": "python",
            "dependencies": [],
            "suggested_tools": ["bash"],
        },
        "/workspace/proj",
    )
    assert result["success"] is True

    # The commands are the SAME ladder detect_installer declares for a
    # poetry-locked project (placeholders filled) — no duplicated strings.
    expected = [
        c.replace("{venv}", "/workspace/proj/.venv").replace("{dir}", "/workspace/proj")
        for c in detect_installer({"poetry.lock", "pyproject.toml"})["commands"]
    ]
    assert "poetry install" in expected  # the project's OWN tool is attempted
    positions = [next(i for i, c in enumerate(orch.commands) if c == e) for e in expected]

    # Order per the spec: manifest pre-flight, then venv, then the installer.
    preflight = next(i for i, c in enumerate(orch.commands) if "python3 --version" in c)
    venv_create = next(
        i for i, c in enumerate(orch.commands) if "-m venv /workspace/proj/.venv" in c
    )
    assert preflight < venv_create < positions[0]

    # The venv interpreter lands in the shared env overlay.
    assert any(DEFAULT_OVERLAY_JSON in c for c in orch.commands)

    # A python project never touches the maven/JDK machinery. (Overlay writes
    # are base64 payloads — excluded so alphabet coincidences can't match.)
    assert not any(
        ("mvn" in c or "maven" in c or "jdk" in c or "apt-get" in c)
        for c in orch.commands
        if DEFAULT_OVERLAY_JSON not in c
    )


# ---- Category-3 panel anchor: structured collected_after_deselection -------


def test_filtered_test_records_selected_count_not_total():
    """Panel spec: the TVM smoke anchor reads collected_after_deselection as
    a STRUCTURED field of the recorded result — never the summary text. A
    filtered run gets a scoped collect pass; the X of 'X/Y tests collected'
    is the selection (the naive regex would read Y, the total)."""
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            (
                "--collect-only -q -k",
                ok("tests/test_a.py::test_x\n3/357 tests collected (354 deselected)"),
            ),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj", args="-k smoke")
    # Filtered runs never launch an unfiltered collection first: that probe
    # can itself be the forbidden native full-suite sweep.
    assert result.metadata["collected"] is None
    assert result.metadata["collected_after_deselection"] == 3
    assert result.metadata["collection_scope"] == "filtered"
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert len(collects) == 1


def test_unfiltered_test_selection_equals_denominator_no_extra_collect():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("42 tests collected in 0.2s"))],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.metadata["collected_after_deselection"] == 42
    collects = [c for c in orch.commands if "--collect-only" in c]
    assert len(collects) == 1  # no scoped pass without a filter


def test_unparseable_scoped_collection_is_none_never_invented():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only -q -k", ok("some garbage the parser does not know")),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj", args="-k smoke")
    assert result.metadata["collected_after_deselection"] is None


# ---- Weak-model native smoke grounding -------------------------------------


def test_native_unready_bare_test_uses_surveyed_bounded_smoke_without_full_collect():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules(
            "tests/python/all-platform-minimal-test/test_runtime.py::test_load\n"
            "3 tests collected in 0.2s"
        ),
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert result.metadata["native_unready"] is True
    assert result.metadata["selection_mode"] == "survey_candidate"
    assert result.metadata["smoke_candidate"] == TVM_SMOKE_PATH
    assert result.metadata["collected"] is None
    assert result.metadata["collected_after_deselection"] == 3
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert collects == [
        f"/workspace/tvm/.venv/bin/python -m pytest --collect-only -q "
        f"{TVM_SMOKE_PATH} --maxfail=1"
    ]
    runs = pytest_runs(orch)
    assert len(runs) == 1
    assert f"{TVM_SMOKE_PATH} --maxfail=1" in runs[0]
    writes = [command for command in orch.commands if COLLECTED_JSON in command and "<<" in command]
    assert '"scope": "filtered"' in writes[0]
    assert '"selected": 3' in writes[0]


def test_subdir_native_test_uses_survey_root_for_smoke_and_install_root_for_origin():
    orch = Orch(
        manifest=dict(SUBDIR_NATIVE_TEST_MANIFEST),
        rules=subdir_native_smoke_rules("2 tests collected in 0.2s"),
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm/python",
    )

    assert result.succeeded is True
    assert result.metadata["smoke_candidate"] == SUBDIR_SMOKE_PATH
    collect = next(command for command in orch.commands if "--collect-only" in command)
    assert SUBDIR_SMOKE_REALPATH in collect
    ready_probe = next(
        command for command in orch.commands if "SAG_NATIVE_PROJECT_READY" in command
    )
    # The readiness script compares PEP 610 against the Python install root,
    # but resolves native artifact roots from the repository survey root.
    assert "native-pkg /workspace/tvm/python /workspace/tvm" in ready_probe
    assert '["python/_native-build", "python/src/native_pkg/lib"]' in ready_probe


@pytest.mark.parametrize(
    ("collection_output", "error_code", "selected"),
    [
        ("51 tests collected in 0.2s", "NATIVE_SMOKE_TOO_BROAD", 51),
        ("no tests collected in 0.2s", "NATIVE_SMOKE_EMPTY", 0),
        ("collection output changed shape", "NATIVE_SMOKE_COUNT_UNKNOWN", None),
    ],
)
def test_native_unready_unsafe_smoke_count_never_starts_test_run(
    collection_output, error_code, selected
):
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules(collection_output),
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert result.error_code == error_code
    assert result.metadata["collected_after_deselection"] == selected
    assert not any("--junitxml" in command for command in orch.commands)


def test_native_unready_invalid_model_path_returns_verified_replacement_without_collect():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules("3 tests collected in 0.2s"),
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args="tests/python/unittest/test_runtime.py",
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert result.metadata["replacement_args"] == f"{TVM_SMOKE_PATH} --maxfail=1"
    assert not any("--collect-only" in command for command in orch.commands)
    assert not any("--junitxml" in command for command in orch.commands)


def test_native_unready_stale_survey_candidate_refuses_to_guess_or_collect():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules(
            "3 tests collected in 0.2s",
            target_exists=False,
        ),
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert result.error_code == "NATIVE_SMOKE_UNAVAILABLE"
    assert not any("--collect-only" in command for command in orch.commands)
    assert not any("--junitxml" in command for command in orch.commands)


def test_native_ready_probe_is_informational_and_never_unlocks_the_full_suite():
    """Superseded expectation (audit 2026-07-26, Plan 4 Task 1): readiness used
    to disarm the bounded smoke, and an LLVM-less TVM build passed the probe —
    the bare test then collected the FULL suite, the exact failure the guard was
    written to prevent. Readiness is now recorded as a fact; only a capability
    receipt unlocks a full collect (tests/test_native_smoke_capability_gate.py).
    The probe's SHAPE assertions below are unchanged."""
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            *tvm_native_smoke_rules(
                "3 tests collected in 0.2s",
                native_ready=True,
            ),
            ("--collect-only", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.succeeded is True
    assert "native_unready" not in result.metadata  # the probe DID pass
    assert result.metadata["native_ready_probe"] is True
    assert result.metadata["smoke_receipt_present"] is False
    assert result.metadata["collection_scope"] == "filtered"
    assert result.metadata["collected"] is None
    assert result.metadata["collected_after_deselection"] == 3
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert len(collects) == 1
    assert TVM_SMOKE_PATH in collects[0]
    ready_probe = next(
        command for command in orch.commands if "SAG_NATIVE_PROJECT_READY" in command
    )
    assert "apache-tvm" in ready_probe
    assert "/workspace/tvm" in ready_probe
    assert '["build", "python/tvm/lib"]' in ready_probe
    assert ".venv/lib" not in ready_probe


def test_native_unready_selector_only_explicit_filter_is_rejected_before_collection():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=tvm_native_smoke_rules("3 tests collected in 0.2s"),
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args="-k runtime",
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert "concrete" in (result.error or "")
    assert result.metadata["replacement_args"] == f"{TVM_SMOKE_PATH} --maxfail=1"
    assert not any("--collect-only" in command for command in orch.commands)
    assert not any("--junitxml" in command for command in orch.commands)


def test_native_unready_explicit_project_path_with_filter_is_scoped_and_bounded():
    explicit = f"{TVM_SMOKE_PATH}/test_runtime.py"
    explicit_full = f"/workspace/tvm/{explicit}"
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (
                f"realpath -m -- /workspace /workspace/tvm {explicit_full}",
                ok(f"/workspace\n/workspace/tvm\n{explicit_full}\n"),
            ),
            (
                f"realpath -m -- {TVM_SMOKE_REALPATH} {explicit_full}",
                ok(f"{TVM_SMOKE_REALPATH}\n{explicit_full}\n"),
            ),
            (f"test -e {explicit_full}", ok("EXISTS")),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            (
                f"--collect-only -q {explicit} -k runtime --maxfail=1",
                ok("2/357 tests collected (355 deselected)"),
            ),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=f"{explicit} -k runtime",
    )

    assert result.succeeded is True
    assert result.metadata["selection_mode"] == "explicit"
    assert result.metadata["collected_after_deselection"] == 2
    collects = [command for command in orch.commands if "--collect-only" in command]
    assert len(collects) == 1
    assert f"{explicit} -k runtime --maxfail=1" in collects[0]


def test_native_unready_explicit_exact_survey_candidate_is_allowed():
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (
                f"realpath -m -- {TVM_SMOKE_REALPATH} {TVM_SMOKE_REALPATH}",
                ok(f"{TVM_SMOKE_REALPATH}\n{TVM_SMOKE_REALPATH}\n"),
            ),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=TVM_SMOKE_PATH,
    )

    assert result.succeeded is True
    assert result.metadata["selection_mode"] == "explicit"
    assert result.metadata["collected_after_deselection"] == 3


def test_native_unready_explicit_absolute_path_outside_project_is_rejected():
    outside = "/opt/shared/test_runtime.py"
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (f"test -e {outside}", ok("EXISTS")),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=outside,
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert "outside the surveyed project" in (result.error or "")
    assert not any("--collect-only" in command for command in orch.commands)


def test_native_unready_broader_project_tests_dir_is_rejected_before_collection():
    broader = "tests"
    broader_full = "/workspace/tvm/tests"
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (
                f"realpath -m -- /workspace /workspace/tvm {broader_full}",
                ok(f"/workspace\n/workspace/tvm\n{broader_full}\n"),
            ),
            (f"test -e {broader_full}", ok("EXISTS")),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
            (f"--collect-only -q {broader}", ok("357 tests collected in 1.2s")),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=broader,
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert "broader than the verified survey smoke" in (result.error or "")
    assert not any("--collect-only" in command for command in orch.commands)
    assert not any("--junitxml" in command for command in orch.commands)


def test_native_unready_explicit_symlink_escape_is_rejected():
    relative = "tests/python/escape/test_runtime.py"
    full = f"/workspace/tvm/{relative}"
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (
                f"realpath -m -- /workspace /workspace/tvm {full}",
                ok("/workspace\n/workspace/tvm\n/opt/shared/test_runtime.py\n"),
            ),
            (f"test -e {full}", ok("EXISTS")),
            *tvm_native_smoke_rules("3 tests collected in 0.2s"),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=relative,
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert "real path escapes" in (result.error or "")
    assert not any("--collect-only" in command for command in orch.commands)


def test_native_unready_subdir_dot_is_not_a_concrete_smoke_path():
    orch = Orch(
        manifest=dict(SUBDIR_NATIVE_TEST_MANIFEST),
        rules=[
            (
                "realpath -m -- /workspace /workspace/tvm /workspace/tvm/python",
                ok("/workspace\n/workspace/tvm\n/workspace/tvm/python\n"),
            ),
            ("test -e /workspace/tvm/python", ok("EXISTS")),
            *subdir_native_smoke_rules("2 tests collected in 0.2s"),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm/python",
        args=".",
    )

    assert result.succeeded is False
    assert result.error_code == "PYTEST_ARGS_REJECTED"
    assert "concrete native smoke path" in (result.error or "")
    assert not any("--collect-only" in command for command in orch.commands)


def test_native_unready_unknown_survey_source_is_not_an_executable_coordinate():
    manifest = {
        **TVM_NATIVE_TEST_MANIFEST,
        "python_smoke_candidates": [
            {
                "path": TVM_SMOKE_PATH,
                "source": "model:guessed-path",
            }
        ],
    }
    orch = Orch(
        manifest=manifest,
        rules=[
            ("SAG_NATIVE_PROJECT_READY", fail("not ready")),
            (
                f"realpath -m -- /workspace /workspace/tvm {TVM_SMOKE_REALPATH}",
                ok(f"/workspace\n/workspace/tvm\n{TVM_SMOKE_REALPATH}\n"),
            ),
            (f"test -e {TVM_SMOKE_REALPATH}", ok("EXISTS")),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/tvm")

    assert result.succeeded is False
    assert result.error_code == "NATIVE_SMOKE_UNAVAILABLE"
    assert not any("realpath -m" in command for command in orch.commands)
    assert not any("--collect-only" in command for command in orch.commands)


def test_native_unready_real_pytest_collection_accepts_owned_concrete_path(tmp_path):
    """Exercise the parser/guard against pytest's real collection output.

    The orchestrator remains a container double, but the collection transcript
    it returns comes from an actual pytest subprocess rather than a hand-written
    summary string.
    """
    test_file = tmp_path / "test_runtime.py"
    test_file.write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    collection_output = collected.stdout + collected.stderr
    assert collected.returncode == 0, collection_output

    explicit = f"{TVM_SMOKE_PATH}/test_runtime.py"
    explicit_full = f"/workspace/tvm/{explicit}"
    orch = Orch(
        manifest=dict(TVM_NATIVE_TEST_MANIFEST),
        rules=[
            (
                f"realpath -m -- /workspace /workspace/tvm {explicit_full}",
                ok(f"/workspace\n/workspace/tvm\n{explicit_full}\n"),
            ),
            (
                f"realpath -m -- {TVM_SMOKE_REALPATH} {explicit_full}",
                ok(f"{TVM_SMOKE_REALPATH}\n{explicit_full}\n"),
            ),
            (f"test -e {explicit_full}", ok("EXISTS")),
            *tvm_native_smoke_rules(collection_output),
            (f"--collect-only -q {explicit} --maxfail=1", ok(collection_output)),
        ],
    )

    result = PythonTool(orch).execute(
        "test",
        working_directory="/workspace/tvm",
        args=explicit,
    )

    assert result.succeeded is True
    assert result.metadata["selection_mode"] == "explicit"
    assert result.metadata["collected_after_deselection"] == 1
