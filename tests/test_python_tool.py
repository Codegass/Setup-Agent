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

import json

import sag.tools.internal.build_preflight as bp
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import (
    COLLECTED_JSON,
    PYTEST_REPORT_DIR,
    PythonTool,
)


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

    def execute_command(self, cmd, workdir=None):
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


# ---------------------------------------------------------------------------
# setup_env
# ---------------------------------------------------------------------------


def test_setup_env_runs_preflight_then_install_commands_in_ladder_order():
    orch = Orch(manifest=dict(MANIFEST))
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    assert result.success is True
    preflight = next(i for i, c in enumerate(orch.commands) if "python3 --version" in c)
    first = next(
        i for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -r requirements.txt" in c
    )
    second = next(
        i for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -r requirements-dev.txt" in c
    )
    # Pre-flight first, then the manifest commands in ladder order, with the
    # {venv} placeholder filled from the manifest venv.
    assert preflight < first < second


def test_setup_env_creates_missing_venv_before_installing():
    orch = Orch(manifest=dict(MANIFEST))  # no EXISTS rule -> venv missing
    PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    venv_create = next(
        i for i, c in enumerate(orch.commands)
        if "-m venv /workspace/proj/.venv" in c
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
        i for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install -e ." in c
    )
    assert attempted < fallback  # the project's own tool was tried FIRST
    # The deviation is narrated in the observation — the generated setup docs
    # must reflect what actually ran (spec Component 3).
    assert (
        "[deviation] poetry install failed; fell back to pip install -e . "
        "— setup docs must list the fallback"
    ) in result.output
    assert result.success is True


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
        rules=[(
            "pip install -e .",
            FailThenOk("ERROR: Package 'proj' requires a different Python: "
                       "3.12.4 not in '>=3.13'", times=1),
        )],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    attempts = [c for c in orch.commands if "pip install -e ." in c]
    assert len(attempts) == 2  # initial + exactly one retry
    assert any("uv python install 3.13" in c for c in orch.commands)  # re-provisioned
    assert "retry 1/1" in result.output
    assert result.success is True


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
        rules=[(
            "pip install -e .",
            FailThenOk("ERROR: Package 'proj' requires a different Python: "
                       "3.12.4 not in '>=3.13'", times=99),
        )],
    )
    result = PythonTool(orch).execute("setup_env", working_directory="/workspace/proj")
    attempts = [c for c in orch.commands if "pip install -e ." in c]
    assert len(attempts) == 2  # never more than one retry, even on repeat failure
    assert result.success is False


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_test_writes_collected_denominator_and_junitxml_report():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("tests/test_a.py::test_x\n42 tests collected in 0.12s"))],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    assert result.success is True
    writes = [c for c in orch.commands if COLLECTED_JSON in c and "<<" in c]
    assert writes and '"collected": 42' in writes[0]
    runs = [c for c in orch.commands if "-m pytest" in c and "--collect-only" not in c]
    assert len(runs) == 1
    assert runs[0].startswith("/workspace/proj/.venv/bin/python -m pytest")
    assert f"--junitxml={PYTEST_REPORT_DIR}/pytest-" in runs[0]
    # collect-only denominator is recorded BEFORE the honest run
    collect = next(i for i, c in enumerate(orch.commands) if "--collect-only" in c)
    run = next(i for i, c in enumerate(orch.commands) if "--junitxml" in c)
    assert collect < run


def test_pytest_failures_are_honest_and_never_rerun():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("5 tests collected in 0.01s")),
            ("--junitxml", fail("....\n2 failed, 3 passed in 1.23s", exit_code=1)),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    runs = [c for c in orch.commands if "-m pytest" in c and "--collect-only" not in c]
    assert len(runs) == 1  # exit 1 with failures is an HONEST result, not an error to retry
    assert result.success is False
    assert "2 failed, 3 passed" in result.output


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
    assert result.success is False
    assert result.metadata.get("evidence_only") is True  # callers must not redden on this


def test_build_installs_build_into_the_venv_first():
    orch = Orch(manifest=dict(MANIFEST))
    result = PythonTool(orch).execute("build", working_directory="/workspace/proj")
    installed = next(
        i for i, c in enumerate(orch.commands)
        if "/workspace/proj/.venv/bin/python -m pip install build" in c
    )
    built = next(i for i, c in enumerate(orch.commands) if "-m build --wheel" in c)
    assert installed < built
    assert result.success is True
    assert result.metadata.get("evidence_only") is True


# ---------------------------------------------------------------------------
# compile (the compileall evidence generator)
# ---------------------------------------------------------------------------


def test_compile_runs_compileall_over_package_dirs_and_reports_counts():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("test -d /workspace/proj/src/proj", ok("EXISTS")),
            ("__pycache__", ok("8")),
            ("-name '*.py'", ok("10")),
        ],
    )
    result = PythonTool(orch).execute("compile", working_directory="/workspace/proj")
    compileall = [c for c in orch.commands if "-m compileall -q" in c]
    assert compileall and "/workspace/proj/src/proj" in compileall[0]
    assert result.success is True
    assert "8/10" in result.output
    assert result.metadata.get("py_count") == 10
    assert result.metadata.get("pyc_count") == 8
    assert result.metadata.get("failed") == 2


# ---------------------------------------------------------------------------
# operation surface
# ---------------------------------------------------------------------------


def test_unknown_operation_is_rejected_with_the_valid_vocabulary():
    result = PythonTool(Orch(manifest=dict(MANIFEST))).execute(
        "frobnicate", working_directory="/workspace/proj"
    )
    assert result.success is False
    assert result.error_code == "UNKNOWN_PYTHON_OPERATION"
    assert any("setup_env" in s for s in result.suggestions)


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
