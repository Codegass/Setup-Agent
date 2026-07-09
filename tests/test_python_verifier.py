# tests/test_python_verifier.py
"""Validator python honesty layer (spec 2026-07-07 Component 4).

Evidence ladder tri-state (venv -> pip check -> imports -> compileall
coverage -> declared C-extension .so), pytest JUnit-XML round-trip through
the EXISTING report parser (no parser changes), the python_version_mismatch
conflict (exact mirror of jdk_mismatch), and the COLLECTED_JSON fallback
feeding static_test_count (the tests_not_fully_executed denominator).

Scripted-orchestrator style mirrors tests/test_jdk_reactor_conflicts.py:
canned results per command shape, every command recorded.
"""

import json

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import COLLECTED_JSON, PYTEST_REPORT_DIR


def _manifest(**overrides):
    data = {
        "python_version": "3.12",
        "python_constraint": ">=3.9",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/pip install -e ."],
        "python_packages": ["foo"],
        "python_venv": "/workspace/proj/.venv",
        "has_c_extensions": False,
    }
    data.update(overrides)
    return data


class LadderOrch:
    """Scriptable python evidence-ladder container: flat-layout package foo."""

    def __init__(self, *, venv=True, pip_clean=True, import_ok=True,
                 py_count=10, pyc_count=10, so_present=False,
                 manifest=None, active="3.12"):
        self.venv = venv
        self.pip_clean = pip_clean
        self.import_ok = import_ok
        self.py_count = py_count
        self.pyc_count = pyc_count
        self.so_present = so_present
        self.manifest = manifest if manifest is not None else _manifest()
        self.active = active
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)

        def res(ok, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        c = cmd.strip()
        if c == f"cat {REQUIREMENTS_PATH}":
            return res(True, json.dumps(self.manifest))
        if "python3 --version" in c:
            return res(True, f"Python {self.active}.0")
        if "java -version" in c:
            return res(False, "java: command not found")
        if c.startswith("test -f "):
            # Build-system detection: this project is python-only.
            return res("pyproject.toml" in c)
        if c.startswith("test -d "):
            path = c.split()[2]
            if path.endswith("/.venv"):
                return res(self.venv)
            if path.endswith("/src/foo"):
                return res(False)  # flat layout
            if path.endswith("/foo"):
                return res(True)
            return res(False)
        if "pip check" in c:
            return res(
                self.pip_clean,
                "No broken requirements found." if self.pip_clean
                else "foo 1.0 requires bar, which is not installed.",
            )
        if "import foo" in c:
            return res(
                self.import_ok,
                "" if self.import_ok else "ModuleNotFoundError: No module named 'foo'",
            )
        if "compileall" in c:
            return res(True)
        if "__pycache__" in c and "wc -l" in c:
            return res(True, str(self.pyc_count))
        if "'*.py'" in c and "wc -l" in c:
            return res(True, str(self.py_count))
        if "'*.so'" in c:
            return res(True, "/workspace/proj/foo/_ext.so" if self.so_present else "")
        if "'*.jar'" in c or "'*.class'" in c:
            return res(True, "0")
        return res(True, "")


def _validate(orch):
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    return validator.validate_build_status("proj")


# ---------------------------------------------------------------------------
# Evidence ladder tri-state (spec Component 4 table)
# ---------------------------------------------------------------------------


def test_no_venv_is_blocked():
    orch = LadderOrch(venv=False)
    result = _validate(orch)
    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "venv" in result["reason"].lower()
    assert result["evidence"]["fingerprint_details"]["venv_exists"] is False
    # The ladder stops at the missing venv: no later rung is even probed.
    assert not any("pip check" in c for c in orch.commands)


def test_failed_import_is_blocked():
    orch = LadderOrch(import_ok=False)
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    assert "import" in result["reason"].lower()
    assert "foo" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["import_failures"] == ["foo"]


def test_dirty_pip_check_is_partial():
    orch = LadderOrch(pip_clean=False)
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "pip check" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["pip_check_clean"] is False


def test_low_compileall_coverage_is_partial():
    orch = LadderOrch(py_count=10, pyc_count=5)  # 0.5 < the 1.0 threshold
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "compileall coverage" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["compileall_coverage"] == 0.5


def test_missing_declared_c_extension_is_partial():
    orch = LadderOrch(manifest=_manifest(has_c_extensions=True), so_present=False)
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "C-extension" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["ext_modules_ok"] is False


def test_all_rungs_green_is_success():
    orch = LadderOrch()
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["conflicts"] == []
    details = result["evidence"]["fingerprint_details"]
    assert details["venv_exists"] is True
    assert details["pip_check_clean"] is True
    assert details["imports_ok"] is True
    assert details["compileall_coverage"] == 1.0


# ---------------------------------------------------------------------------
# pytest JUnit XML round-trips through the EXISTING parser unchanged
# ---------------------------------------------------------------------------

_PYTEST_XML_FILE = f"{PYTEST_REPORT_DIR}/pytest-1720000000.xml"

# Real pytest --junitxml shape: <testsuites> wrapping one <testsuite>.
_PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="1" skipped="0" tests="3"
             time="0.123" timestamp="2026-07-09T12:00:00" hostname="sag">
    <testcase classname="tests.test_api" name="test_ok" time="0.010" />
    <testcase classname="tests.test_api" name="test_also_ok" time="0.011" />
    <testcase classname="tests.test_api" name="test_broken" time="0.020">
      <failure message="assert 1 == 2">AssertionError</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


class PytestReportOrch:
    """find/cat script for the pytest-reports dir; no JVM reports anywhere."""

    def __init__(self, xml=_PYTEST_XML):
        self.xml = xml
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        c = cmd.strip()
        if "SAG_COMPACT_TEST_REPORT_PARSER" in c:
            # In-container compact parser unavailable -> shell discovery path.
            return {"exit_code": 1, "success": False, "output": ""}
        if c.startswith("test -f ") and "pom.xml" in c:
            return {"exit_code": 0, "output": "MISSING"}
        if c.startswith(f"test -d {PYTEST_REPORT_DIR}"):
            return {"exit_code": 0, "output": "EXISTS"}
        if c.startswith("find") and "-type d" in c and "surefire-reports" in c:
            return {"exit_code": 0, "output": ""}
        if c.startswith("find") and PYTEST_REPORT_DIR in c and "'*.xml'" in c:
            return {"exit_code": 0, "output": _PYTEST_XML_FILE}
        if "src/test/groovy" in c:
            return {"exit_code": 0, "output": ""}
        if c == f"cat '{_PYTEST_XML_FILE}'":
            return {"exit_code": 0, "output": self.xml}
        return {"exit_code": 0, "output": ""}


def test_pytest_junitxml_roundtrips_through_existing_parser():
    orch = PytestReportOrch()
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.parse_test_reports("/workspace/proj")
    assert result["valid"] is True
    assert result["total_tests"] == 3
    assert result["passed_tests"] == 2
    assert result["failed_tests"] == 1
    assert result["test_success"] is False
    assert _PYTEST_XML_FILE in result["report_files"]
    assert "tests.test_api::test_broken" in result["failing_test_names"]
    # Discovery actually probed the pytest-reports dir (outside project_dir).
    assert any(c.startswith(f"test -d {PYTEST_REPORT_DIR}") for c in orch.commands)


# ---------------------------------------------------------------------------
# python_version_mismatch (exact mirror of jdk_mismatch, same collection site)
# ---------------------------------------------------------------------------


class EnvOrch:
    """Manifest + interpreter/JDK probes + benign answers elsewhere."""

    def __init__(self, python="3.8", java=None, manifest=None):
        self.python = python
        self.java = java
        self.manifest = manifest or {}

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "python3 --version" in cmd:
            return {"success": True, "exit_code": 0, "output": f"Python {self.python}.10"}
        if "java -version" in cmd:
            if self.java:
                return {"success": True, "exit_code": 0,
                        "output": f'openjdk version "{self.java}.0.1"'}
            return {"success": False, "exit_code": 127, "output": "java: command not found"}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def _conflicts(orch):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = orch
    return validator._collect_env_conflicts()


def test_python_version_mismatch_on_manifest_vs_active():
    orch = EnvOrch(python="3.8", manifest={"python_version": "3.11"})
    assert _conflicts(orch) == ["python_version_mismatch"]


def test_no_python_conflict_when_matching_or_unknown():
    assert _conflicts(EnvOrch(python="3.11", manifest={"python_version": "3.11"})) == []
    assert _conflicts(EnvOrch(python="3.8", manifest={})) == []  # no requirement


def test_no_python_conflict_when_constraint_satisfied():
    # Active 3.12 satisfies >=3.9 even though the resolved newest is 3.13 —
    # same honesty rule as PythonPreflight: the requirement is the constraint,
    # not the newest interpreter.
    orch = EnvOrch(python="3.12",
                   manifest={"python_version": "3.13", "python_constraint": ">=3.9"})
    assert _conflicts(orch) == []


def test_jdk_and_python_mismatches_are_both_collected():
    orch = EnvOrch(python="3.8", java="11",
                   manifest={"java_version": "17", "python_version": "3.11"})
    assert _conflicts(orch) == ["jdk_mismatch", "python_version_mismatch"]


# ---------------------------------------------------------------------------
# COLLECTED_JSON fallback -> static_test_count (tests_not_fully_executed gate)
# ---------------------------------------------------------------------------


class CollectedOrch:
    """Optional trunk context; a build marker file; python_tool's COLLECTED_JSON."""

    def __init__(self, collected=42, build_marker="pyproject.toml", trunk_env=None):
        self.collected = collected
        self.build_marker = build_marker
        self.trunk_env = trunk_env
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        c = cmd.strip()
        if c.startswith("ls ") and "trunk_" in c:
            if self.trunk_env is None:
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": "/workspace/.setup_agent/contexts/trunk_1.json"}
        if c.startswith("cat ") and "trunk_" in c:
            return {"exit_code": 0,
                    "output": json.dumps({"environment_summary": self.trunk_env})}
        if c.startswith("test -f "):
            return {"exit_code": 0 if c.endswith(self.build_marker) else 1, "output": ""}
        if c == f"cat {COLLECTED_JSON}":
            return {"exit_code": 0, "output": json.dumps({"collected": self.collected})}
        return {"exit_code": 1, "output": ""}


def test_collected_json_fallback_feeds_static_test_count():
    orch = CollectedOrch(collected=42)  # no trunk context at all
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["has_static_test_count"] is True
    assert result["static_test_count"] == 42


def test_env_summary_static_count_wins_over_fallback():
    orch = CollectedOrch(collected=42, trunk_env={"static_test_count": 100})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["static_test_count"] == 100


def test_fallback_only_applies_to_python_projects():
    orch = CollectedOrch(collected=42, build_marker="pom.xml")
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["has_static_test_count"] is False
    assert result["static_test_count"] is None
    assert not any(COLLECTED_JSON in c for c in orch.commands)
