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

import pytest
from container_evidence_fakes import (
    add_published_mutable_json,
    strict_published_evidence,
)

from sag.agent.action_intents import action_fingerprint
from sag.agent.control_events import canonical_json
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    publish_evidence_revision,
    evidence_publication_authority_for,
)
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    PYTHON_FACADE_EXECUTION_BINDING,
    build_contract,
)
from sag.agent.invocation_receipts import build_receipt
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_test_rows import seal_testcase_execution_rows
from sag.reporting.utils import render_condensed_summary
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import COLLECTED_JSON, PYTEST_REPORT_DIR


def _manifest(**overrides):
    data = {
        "python_version": "3.12",
        "python_constraint": ">=3.9",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
        "python_packages": ["foo"],
        "python_venv": "/workspace/proj/.venv",
        "has_c_extensions": False,
    }
    data.update(overrides)
    return data


class LadderOrch:
    """Scriptable python evidence-ladder container: flat-layout package foo."""

    def __init__(
        self,
        *,
        venv=True,
        pip_clean=True,
        pip_output=None,
        import_ok=True,
        py_count=10,
        pyc_count=10,
        so_present=False,
        foreign_pyc_count=0,
        manifest=None,
        active="3.12",
    ):
        self.venv = venv
        self.pip_clean = pip_clean
        self.pip_output = pip_output  # forces a FAILED pip check with this output
        self.import_ok = import_ok
        self.py_count = py_count
        self.pyc_count = pyc_count
        self.foreign_pyc_count = foreign_pyc_count
        self.so_present = so_present
        self.manifest = manifest if manifest is not None else _manifest()
        self.active = active
        self.commands = []
        publication = publish_evidence_revision(
            self,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=canonical_json(self.manifest).encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        assert publication.published

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)

        def res(ok, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        c = cmd.strip()
        if "SAG_NAMED_JSON_RECORD_V1" in c and REQUIREMENTS_PATH in c:
            return res(
                True,
                frame_named_json_record_stream(
                    [("build-requirements.json", canonical_json(self.manifest))]
                ),
            )
        if "SAG_NAMED_JSON_RECORD_V1" in c:
            return res(True, frame_named_json_record_stream([]))
        if c in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            return res(True, canonical_json(self.manifest))
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
            if self.pip_output is not None:
                return res(False, self.pip_output)
            return res(
                self.pip_clean,
                (
                    "No broken requirements found."
                    if self.pip_clean
                    else "foo 1.0 requires bar, which is not installed."
                ),
            )
        if "import foo" in c:
            return res(
                self.import_ok,
                "" if self.import_ok else "ModuleNotFoundError: No module named 'foo'",
            )
        if "importlib.util" in c:
            invalid = self.foreign_pyc_count > 0
            return res(
                True,
                json.dumps(
                    {
                        "status": (
                            "invalid" if invalid else ("valid" if self.py_count else "unavailable")
                        ),
                        "source_count": self.py_count,
                        "compiled_source_count": self.pyc_count,
                        "missing_source_count": max(self.py_count - self.pyc_count, 0),
                        "foreign_pyc_count": self.foreign_pyc_count,
                        "coverage": (
                            self.pyc_count / self.py_count
                            if self.py_count and not invalid
                            else None
                        ),
                        "cache_tag": "cpython-312",
                        "conflicts": ["metrics_conflict"] if invalid else [],
                        "missing_sources": [],
                        "foreign_pycs": (
                            ["/workspace/proj/foo/__pycache__/old.cpython-311.pyc"]
                            if invalid
                            else []
                        ),
                    }
                ),
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


def test_unpersisted_import_failure_is_unknown_not_blocked():
    orch = LadderOrch(import_ok=False)
    result = _validate(orch)
    assert result["success"] is True
    assert result["evidence_status"] == "partial"
    assert "package importability unknown: no producer receipt" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert result["evidence"]["fingerprint_details"]["import_failures"] == []
    assert not any("import foo" in command for command in orch.commands)


def test_unpersisted_pip_result_is_unknown_and_partial():
    orch = LadderOrch(pip_clean=False)
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "pip dependency integrity unknown: no producer receipt" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["pip_check_clean"] is None
    assert not any("pip check" in command for command in orch.commands)


def test_missing_pip_module_is_unverifiable_not_breakage():
    """Bug #12 reproduction: plain `uv venv` seeds NO pip, so the pip-check
    rung fails with 'No module named pip' — a MISSING TOOL, not dependency
    breakage. The rung must go UNVERIFIABLE (pip_check_clean None) with a
    visible skip warning and the honest PARTIAL reason 'pip check unverified'
    — never the phantom 'pip check reported dependency breakage'."""
    orch = LadderOrch(pip_output="/workspace/proj/.venv/bin/python: No module named pip")
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["pip_check_clean"] is None  # UNVERIFIABLE, not False
    assert result["success"] is True
    assert result["build_complete"] is False  # bug-#9 semantics: never silent green
    assert result["evidence_status"] == "partial"
    assert "pip dependency integrity unknown: no producer receipt" in result["reason"]
    assert "breakage" not in result["reason"]
    assert any(
        "pip dependency integrity unknown: no producer receipt" in w
        for w in result["evidence"]["warnings"]
    )


def test_missing_python_binary_is_unverifiable_not_breakage():
    """Same bug, other shell shape: the venv dir exists but its python binary
    does not, so the shell reports 'No such file or directory' — also a
    missing tool, never dependency breakage."""
    orch = LadderOrch(
        pip_output=("bash: /workspace/proj/.venv/bin/python: No such file or directory")
    )
    result = _validate(orch)
    assert result["evidence"]["fingerprint_details"]["pip_check_clean"] is None
    assert result["evidence_status"] == "partial"
    assert "pip dependency integrity unknown: no producer receipt" in result["reason"]
    assert any(
        "pip dependency integrity unknown: no producer receipt" in w
        for w in result["evidence"]["warnings"]
    )


def test_verifier_never_runs_pip_or_project_python():
    orch = LadderOrch()
    _validate(orch)
    assert not any("pip check" in command for command in orch.commands)
    assert not any("/workspace/proj/.venv/bin/python" in command for command in orch.commands)


def test_unpersisted_compileall_coverage_is_unknown_and_partial():
    orch = LadderOrch(py_count=10, pyc_count=5)  # 0.5 < the 1.0 threshold
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "bytecode compilation unknown: no producer receipt" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["compileall_coverage"] is None
    assert not any("compileall" in command for command in orch.commands)


def test_unpersisted_foreign_pyc_claim_cannot_enter_judgment():
    orch = LadderOrch(py_count=10, pyc_count=10, foreign_pyc_count=1)

    result = _validate(orch)

    details = result["evidence"]["fingerprint_details"]
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert details["compileall_metric_status"] == "unavailable"
    assert details["compileall_coverage"] is None
    assert details["compileall_foreign_pyc_count"] == 0
    assert "metrics_conflict" not in result["conflicts"]
    assert "bytecode compilation unknown: no producer receipt" in result["reason"]

    rendered = render_condensed_summary(
        {
            "status": {"verdict": "partial"},
            "project": {"build_system": "python"},
            "phases": {"build": True},
            "physical_evidence": result["evidence"],
        }
    )
    build_line = next(
        line for line in rendered.splitlines() if line.startswith("🧾 Build evidence:")
    )
    assert "compileall invalid" not in build_line
    assert "compileall 100%" not in build_line


def test_missing_declared_c_extension_is_partial():
    orch = LadderOrch(manifest=_manifest(has_c_extensions=True), so_present=False)
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "C-extension" in result["reason"]
    assert result["evidence"]["fingerprint_details"]["ext_modules_ok"] is False


def test_unreceipted_green_fakes_cannot_make_build_complete():
    orch = LadderOrch()
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]
    details = result["evidence"]["fingerprint_details"]
    assert details["venv_exists"] is True
    assert details["pip_check_clean"] is None
    assert details["imports_ok"] is None
    assert details["compileall_coverage"] is None


# ---------------------------------------------------------------------------
# Unverified imports rung (pyyaml live probe bug #9): unknown evidence caps
# the build at PARTIAL, never silent green
# ---------------------------------------------------------------------------


def test_unverified_imports_cap_build_at_partial():
    """imports_ok None (no importable package detected anywhere: manifest
    empty AND no project-owned installed record in the venv) must cap the
    build at PARTIAL. venv + compileall are real evidence, so success stays
    True — but unknown import evidence is never green (complete False)."""
    orch = LadderOrch(manifest=_manifest(python_packages=[]))
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    # The PARTIAL reason names the skipped rung.
    assert "package importability unknown: no producer receipt" in result["reason"]
    # The existing visible evidence warning stays.
    assert any(
        "package importability unknown: no producer receipt" in w
        for w in result["evidence"]["warnings"]
    )
    # No import was ever probed — the rung is genuinely unknown, not failed.
    assert not any('-c "import' in c for c in orch.commands)


def test_unpersisted_successful_import_is_not_judgment_evidence():
    """Regression: the bug #9 cap must not touch imports_ok=True — every
    rung green stays SUCCESS/complete, with no unverified-imports reason."""
    orch = LadderOrch()
    result = _validate(orch)
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "package importability unknown: no producer receipt" in result["reason"]


def test_unpersisted_failed_import_is_not_judgment_evidence():
    """Regression: the bug #9 cap must not touch imports_ok=False — a failed
    import stays BLOCKED (success False), never softened to PARTIAL."""
    orch = LadderOrch(import_ok=False)
    result = _validate(orch)
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"


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
        if "SAG_NAMED_JSON_RECORD_V1" in c:
            # No invocation receipt or assessment exists for this parser-only
            # fixture; the host authority's expected set is likewise empty.
            return {
                "exit_code": 0,
                "success": True,
                "output": frame_named_json_record_stream([]),
            }
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
        publication = publish_evidence_revision(
            self,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=canonical_json(self.manifest).encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        assert publication.published

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in cmd and REQUIREMENTS_PATH in cmd:
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(
                    [("build-requirements.json", canonical_json(self.manifest))]
                ),
            }
        if "python3 --version" in cmd:
            return {"success": True, "exit_code": 0, "output": f"Python {self.python}.10"}
        if "java -version" in cmd:
            if self.java:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"',
                }
            return {"success": False, "exit_code": 127, "output": "java: command not found"}
        if cmd in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": canonical_json(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def _conflicts(orch):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = orch
    return validator._collect_env_conflicts()


def test_python_version_mismatch_is_not_probed_with_an_ambient_interpreter():
    orch = EnvOrch(python="3.8", manifest={"python_version": "3.11"})
    assert _conflicts(orch) == []


@pytest.mark.parametrize(
    ("python", "manifest"),
    [
        ("3.11", {"python_version": "3.11"}),
        ("3.8", {}),
    ],
)
def test_no_python_conflict_when_matching_or_unknown(python, manifest):
    assert _conflicts(EnvOrch(python=python, manifest=manifest)) == []


def test_no_python_conflict_when_constraint_satisfied():
    # Active 3.12 satisfies >=3.9 even though the resolved newest is 3.13 —
    # same honesty rule as PythonPreflight: the requirement is the constraint,
    # not the newest interpreter.
    orch = EnvOrch(python="3.12", manifest={"python_version": "3.13", "python_constraint": ">=3.9"})
    assert _conflicts(orch) == []


def test_jdk_and_python_mismatches_are_both_collected():
    orch = EnvOrch(
        python="3.8", java="11", manifest={"java_version": "17", "python_version": "3.11"}
    )
    assert _conflicts(orch) == ["jdk_mismatch"]


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
            return {"exit_code": 0, "output": json.dumps({"environment_summary": self.trunk_env})}
        if c.startswith("test -f "):
            return {"exit_code": 0 if c.endswith(self.build_marker) else 1, "output": ""}
        if c == f"cat {COLLECTED_JSON}":
            return {"exit_code": 0, "output": json.dumps({"collected": self.collected})}
        return {"exit_code": 1, "output": ""}


class PublishedDenominatorOrch:
    """One current Python test receipt with three complete typed rows."""

    run_id = "run-pytest"
    target_sha = "a" * 40
    project_root = "/workspace/proj"

    def __init__(self):
        report_path = "/workspace/.setup_agent/pytest-reports/pytest-attempt-000001.xml"
        report_sha = "f" * 64
        receipt_id = "inv-python-test-0001"
        parsed_rows = {
            "schema_version": 2,
            "status": "complete",
            "report_count": 1,
            "rows": [
                {
                    "report_path": report_path,
                    "report_sha256": report_sha,
                    "classname": "tests.test_api",
                    "name": f"test_case[{index}]",
                    "source_file": "tests/test_api.py",
                    "outcome": "passed",
                    "execution_ordinal": index,
                }
                for index in range(1, 4)
            ],
        }
        rows = seal_testcase_execution_rows(
            parsed_rows,
            run_id=self.run_id,
            receipt_id=receipt_id,
            tool="python",
            target_sha=self.target_sha,
            domain_id=self.project_root,
            working_directory=self.project_root,
        )
        params = {
            "action": "test",
            "args": None,
            "working_directory": self.project_root,
            "timeout": None,
            "maven_version_requirement": None,
            "features": None,
            "definitions": None,
        }
        contract = build_contract(
            run_id=self.run_id,
            envelope_id="envelope-python-denominator",
            tool="build",
            params=params,
            effective_tool="python",
            effective_action="test",
            expected_cwd=self.project_root,
            expected_argv=None,
            execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
            intent_source="controller",
            intent_id="intent-python-denominator",
            intent_domain_id=self.project_root,
            intent_exact_params=params,
            action_fingerprint=action_fingerprint(
                domain_id=self.project_root,
                tool="build",
                params=params,
            ),
            target_sha=self.target_sha,
            survey_fingerprint="survey-current",
            config_fingerprint="config-current",
            document_map_fingerprint="documents-current",
            domain_id=self.project_root,
            fact_epoch=1,
        )
        receipt = build_receipt(
            receipt_id=receipt_id,
            run_id=self.run_id,
            tool="python",
            requested_action="test",
            effective_action="test",
            argv=f"{self.project_root}/.venv/bin/python -m pytest",
            working_directory=self.project_root,
            actual_cwd=self.project_root,
            exit_code=0,
            before={},
            after={report_path: report_sha},
            target_sha=self.target_sha,
            survey_fingerprint="survey-current",
            config_fingerprint="config-current",
            document_map_fingerprint="documents-current",
            domain_id=self.project_root,
            fact_epoch=1,
            testcase_execution_rows=rows,
            contract_id=contract["contract_id"],
            contract_hash=contract["contract_hash"],
            execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
        )
        self.filesystem = strict_published_evidence(
            self,
            run_id=self.run_id,
            target_sha=self.target_sha,
            receipts=[],
        )
        receipt_raw = canonical_json(receipt)
        self.filesystem.files[
            f"/workspace/.setup_agent/invocation_receipts/{receipt_id}.json"
        ] = receipt_raw
        evidence_publication_authority_for(self).publish_bytes(
            record_kind="invocation_receipt",
            record_id=receipt_id,
            raw=receipt_raw.encode("utf-8"),
            contract_id=contract["contract_id"],
            contract_hash=contract["contract_hash"],
        )
        contract_raw = canonical_json(contract)
        self.filesystem.files[f"{CONTRACT_DIR}/{contract['contract_id']}.json"] = contract_raw
        evidence_publication_authority_for(self).publish_bytes(
            record_kind="invocation_contract",
            record_id=contract["contract_id"],
            raw=contract_raw.encode("utf-8"),
            contract_id=contract["contract_id"],
            contract_hash=contract["contract_hash"],
        )
        self.manifest = {
            "survey": {
                "project_path": self.project_root,
                "target_sha": self.target_sha,
                "survey_fingerprint": "survey-current",
                "config_fingerprint": "config-current",
                "document_map_fingerprint": "documents-current",
            },
            "build_domains": [{"root": self.project_root}],
            "domain_facts": [{"root": self.project_root, "fact_epoch": 1}],
        }
        add_published_mutable_json(
            self,
            self.filesystem,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    @property
    def commands(self):
        return self.filesystem.commands

    def execute_command(self, command, **kwargs):
        if command.strip().startswith("test -f "):
            return {
                "success": command.strip().endswith("pyproject.toml"),
                "exit_code": 0 if command.strip().endswith("pyproject.toml") else 1,
                "output": "",
            }
        return self.filesystem(command, **kwargs)


def test_unpublished_collected_json_cannot_feed_static_test_count():
    orch = CollectedOrch(collected=42)  # no trunk context at all
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["has_static_test_count"] is False
    assert result["static_test_count"] is None
    assert result["analysis_status_code"] == "analysis_trunk_missing"
    assert "missing_analysis_prompt" not in result
    assert "project_analyzer" not in str(result)
    assert "execution plan" not in str(result).lower()
    assert not any(COLLECTED_JSON in command for command in orch.commands)


def test_published_complete_python_test_rows_feed_the_denominator():
    orch = PublishedDenominatorOrch()
    validator = PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
        receipt_run_id=orch.run_id,
    )

    result = validator.validate_project_analysis_status("proj")

    assert result["static_test_count"] == 3
    assert result["static_test_count_source"] == "published_testcase_receipt"
    assert not any(COLLECTED_JSON in command for command in orch.commands)


def test_survey_facts_mark_analysis_ready_before_python_test_collection():
    # dim (c) deleted (Category-3 analyzer diet): the project brief is gone, so
    # the persisted SURVEY FACTS (build recommendation / survey stamp) are the
    # analysis-ready markers — no project_brief_ref/fingerprint is written.
    orch = CollectedOrch(
        collected=None,
        trunk_env={
            "build_recommendation": {
                "build_system": "python",
                "build_root": "/workspace/proj",
            },
            "survey": {"project_path": "/workspace/proj", "analyzer_version": 7},
        },
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_project_analysis_status("proj")

    assert result["analyzed"] is True
    assert result["has_static_test_count"] is False


def test_unpublished_collect_only_mirror_cannot_override_env_summary():
    orch = CollectedOrch(collected=42, trunk_env={"static_test_count": 100})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["static_test_count"] == 100
    assert "static_test_count_source" not in result
    assert "static_test_count_static_scan" not in result
    assert not any(COLLECTED_JSON in command for command in orch.commands)


def test_fallback_only_applies_to_python_projects():
    orch = CollectedOrch(collected=42, build_marker="pom.xml")
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    result = validator.validate_project_analysis_status("proj")
    assert result["has_static_test_count"] is False
    assert result["static_test_count"] is None
    assert not any(COLLECTED_JSON in c for c in orch.commands)
