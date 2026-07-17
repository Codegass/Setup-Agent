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
import subprocess
import sys
import xml.etree.ElementTree as ET

import sag.tools.internal.build_preflight as bp
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import (
    _PYTEST_ATTEMPT_TAG_SCRIPT,
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
    runs = [
        c
        for c in orch.commands
        if "-m pytest" in c and "--collect-only" not in c and "--version" not in c
    ]
    assert len(runs) == 1
    assert runs[0].startswith("/workspace/proj/.venv/bin/python -m pytest")
    assert f"--junitxml={PYTEST_REPORT_DIR}/pytest-" in runs[0]
    # collect-only denominator is recorded BEFORE the honest run
    collect = next(i for i, c in enumerate(orch.commands) if "--collect-only" in c)
    run = next(i for i, c in enumerate(orch.commands) if "--junitxml" in c)
    assert collect < run


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


def test_pytest_failures_are_honest_and_never_rerun():
    orch = Orch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("5 tests collected in 0.01s")),
            ("--junitxml", fail("....\n2 failed, 3 passed in 1.23s", exit_code=1)),
        ],
    )
    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")
    runs = [
        c
        for c in orch.commands
        if "-m pytest" in c and "--collect-only" not in c and "--version" not in c
    ]
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
            ("__pycache__", ok("8")),
            ("-name '*.py'", ok("10")),
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
            ("__pycache__", ok("0")),
            ("-name '*.py'", ok("0")),
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
