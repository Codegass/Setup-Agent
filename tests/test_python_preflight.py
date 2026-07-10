# tests/test_python_preflight.py
"""PythonPreflight (uv -> apt ladder) + pip Requires-Python classifier
(spec 2026-07-07 Component 2).

Mirrors tests/test_build_preflight.py's scripted-orchestrator style: canned
results per command substring, every command recorded. The pre-flight is
check-and-fix: satisfied -> no-op; mismatch -> provision + narrate; ladder
exhausted -> mismatch=True for the verifier (python_version_mismatch), and
it NEVER raises or blocks.
"""

import json

import sag.tools.internal.build_preflight as bp
from sag.tools.internal.build_preflight import (
    REQUIREMENTS_PATH,
    PythonPreflight,
    active_python_version,
    classify_python_version_error,
)
from sag.tools.internal.python_env import SUPPORTED_PYTHONS


class PyOrch:
    """Scriptable orchestrator for the python provision ladder."""

    def __init__(self, python_output, uv_present=True, uv_install_ok=True,
                 uv_python_ok=True, uv_venv_ok=True, apt_ok=True,
                 apt_venv_ok=True, manifest=None, venv_pip_ok=True,
                 ensurepip_ok=True):
        self.python_output = python_output
        self.uv_present = uv_present
        self.uv_install_ok = uv_install_ok
        self.uv_python_ok = uv_python_ok
        self.uv_venv_ok = uv_venv_ok
        self.apt_ok = apt_ok
        self.apt_venv_ok = apt_venv_ok
        self.manifest = manifest
        self.venv_pip_ok = venv_pip_ok      # `python -m pip --version` probe
        self.ensurepip_ok = ensurepip_ok    # `python -m ensurepip` repair
        self.commands = []

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)

        def result(ok, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        if "python3 --version" in cmd:
            return result(True, self.python_output)
        if cmd.startswith("cat ") and REQUIREMENTS_PATH in cmd:
            if self.manifest is None:
                return result(False, "No such file")
            return result(True, json.dumps(self.manifest))
        if "astral.sh/uv/install.sh" in cmd:
            if self.uv_install_ok:
                self.uv_present = True
            return result(self.uv_install_ok)
        if "command -v uv" in cmd:
            return result(self.uv_present)
        if "uv python install" in cmd:
            return result(self.uv_python_ok)
        if "uv venv" in cmd:
            return result(self.uv_venv_ok)
        if "apt-get" in cmd:
            return result(self.apt_ok)
        if "-m venv" in cmd:
            return result(self.apt_venv_ok)
        if "-m pip --version" in cmd:
            return result(self.venv_pip_ok, "pip 25.0" if self.venv_pip_ok else
                          "No module named pip")
        if "-m ensurepip" in cmd:
            if self.ensurepip_ok:
                self.venv_pip_ok = True  # the repair takes: re-probe succeeds
            return result(self.ensurepip_ok)
        return result(True)


def test_matching_python_is_a_noop():
    orch = PyOrch("Python 3.11.7")
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    assert outcome.matched is True
    assert outcome.provisioned is False
    assert outcome.narration == ""
    # No provisioning traffic on the happy path.
    assert not any("uv" in c or "apt-get" in c or "curl" in c for c in orch.commands)


def test_constraint_satisfied_by_active_is_a_noop():
    # Active 3.12 satisfies >=3.9 even though the resolved newest is 3.13:
    # the pre-flight guarantees requirements, it does not chase the newest.
    orch = PyOrch("Python 3.12.4")
    outcome = PythonPreflight(orch).run("3.13", constraint=">=3.9", source="requires-python")
    assert outcome.matched is True
    assert not any("uv" in c or "apt-get" in c for c in orch.commands)


def test_no_requirement_is_a_noop():
    orch = PyOrch("Python 3.12.4")
    outcome = PythonPreflight(orch).run(None)
    assert outcome.matched is True and outcome.narration == ""


def test_active_python_version_parses_major_minor():
    assert active_python_version(PyOrch("Python 3.11.7")) == "3.11"
    assert active_python_version(PyOrch("bash: python3: command not found")) is None


def test_mismatch_provisions_via_uv_and_narrates(monkeypatch):
    orch = PyOrch("Python 3.8.10", manifest={"python_venv": "/workspace/proj/.venv"})
    # Overlay registration talks to the container too; stub it out.
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", constraint=">=3.11", source="requires-python")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "[pre-flight] Required: Python 3.11 (source: requires-python)" in outcome.narration
    assert "Active: 3.8" in outcome.narration
    assert "uv-provisioned 3.11" in outcome.narration
    assert "/workspace/proj/.venv" in outcome.narration  # manifest venv, not a default
    assert any("uv python install 3.11" in c for c in orch.commands)
    # --seed: plain `uv venv` ships NO pip, which broke every later
    # `python -m pip ...` rung (bug #12).
    assert any(
        "uv venv --seed --python 3.11 /workspace/proj/.venv" in c
        for c in orch.commands
    )


def test_uv_unavailable_falls_back_to_apt(monkeypatch):
    orch = PyOrch("Python 3.8.10", uv_present=False, uv_install_ok=False)
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "apt-provisioned 3.11" in outcome.narration
    assert any("apt-get install" in c and "python3.11-venv" in c for c in orch.commands)
    assert any("python3.11 -m venv" in c for c in orch.commands)
    # The apt path verifies pip inside the fresh venv too (bug #12).
    assert any("-m pip --version" in c for c in orch.commands)


def test_ladder_exhausted_degrades_to_mismatch_never_raises(monkeypatch):
    orch = PyOrch("Python 3.8.10", uv_present=False, uv_install_ok=False, apt_ok=False)
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    assert outcome.provisioned is False
    assert outcome.mismatch is True  # verifier maps this to python_version_mismatch
    assert "could not provision" in outcome.narration


# ---------------------------------------------------------------------------
# Venv pip guarantee (bug #12): seeded venvs are verified, pip-less venvs are
# repaired with ensurepip, and a still-missing pip is narrated — never a block
# ---------------------------------------------------------------------------


def test_provisioned_venv_pip_is_verified_no_repair_when_present(monkeypatch):
    orch = PyOrch("Python 3.8.10", venv_pip_ok=True)
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    assert outcome.provisioned is True
    # The venv pip probe ran; a healthy venv needs no ensurepip repair.
    assert any("-m pip --version" in c for c in orch.commands)
    assert not any("-m ensurepip" in c for c in orch.commands)
    assert "ensurepip" not in outcome.narration


def test_missing_pip_is_repaired_with_ensurepip(monkeypatch):
    orch = PyOrch("Python 3.8.10", venv_pip_ok=False, ensurepip_ok=True)
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert any("-m ensurepip --upgrade" in c for c in orch.commands)
    # The repair is narrated so the setup docs reflect what actually ran.
    assert "ensurepip" in outcome.narration
    # Probe -> repair -> re-probe: two pip probes bracket the ensurepip.
    probes = [i for i, c in enumerate(orch.commands) if "-m pip --version" in c]
    repair = next(i for i, c in enumerate(orch.commands) if "-m ensurepip" in c)
    assert len(probes) == 2 and probes[0] < repair < probes[1]


def test_pip_still_missing_after_ensurepip_narrates_never_blocks(monkeypatch):
    orch = PyOrch("Python 3.8.10", venv_pip_ok=False, ensurepip_ok=False)
    monkeypatch.setattr(bp, "_register_python_overlay", lambda *a, **k: True)
    outcome = PythonPreflight(orch).run("3.11", source="requires-python")
    # Never a block: the interpreter IS provisioned, the hole is narrated.
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "pip still missing" in outcome.narration


# ---------------------------------------------------------------------------
# classify_python_version_error: pip's Requires-Python rejections only
# ---------------------------------------------------------------------------


def test_pip_requires_python_rejection_resolves_needed_version():
    out = ("ERROR: Package 'foo' requires a different Python: "
           "3.8.10 not in '>=3.10,<3.11'")
    assert classify_python_version_error(out) == "3.10"


def test_open_lower_bound_resolves_to_newest_supported():
    # The constraint is resolved with resolve_python_version, i.e. the spec's
    # interpreter policy: newest supported CPython satisfying it.
    out = "ERROR: Package 'foo' requires a different Python: 3.8.10 not in '>=3.10'"
    assert classify_python_version_error(out) == SUPPORTED_PYTHONS[-1]


def test_pip_ignored_versions_metadata_line():
    out = ("ERROR: Ignored the following versions that require a different python "
           "version: 24.2 Requires-Python >=3.10.0; 24.3 Requires-Python >=3.10.0\n"
           "ERROR: Could not find a version that satisfies the requirement black")
    assert classify_python_version_error(out) == SUPPORTED_PYTHONS[-1]


def test_unrelated_pip_error_returns_none():
    assert classify_python_version_error(
        "ERROR: Could not find a version that satisfies the requirement nosuchpkg"
    ) is None
    assert classify_python_version_error("") is None


def test_bare_syntax_error_returns_none():
    # A SyntaxError alone cannot distinguish "source needs a newer interpreter"
    # from "the source is simply broken" -- acting on it would re-provision on
    # broken code. Too ambiguous to act on; the classifier stays silent and
    # the bounded retry never fires.
    assert classify_python_version_error(
        'File "setup.py", line 12\n    match x:\nSyntaxError: invalid syntax'
    ) is None
