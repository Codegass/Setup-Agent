# tests/test_collection_error_semantics.py
"""Plan 4 Task 2 — pytest collection failures are NOT executed tests.

The 2026-07-26 post-acceptance audit (CORRECTION block in
``docs/superpowers/reports/2026-07-26-sagv2-final-acceptance.md``) falsified
the claim that TVM "executed 56 tests": all 56 testcase nodes in
``pytest-attempt-000001.xml`` are pytest *collection* nodes — empty
``classname``, ``<error message="collection failure">`` or
``<skipped message="collection skipped">``. Nothing ran; ``executed`` is 0.

Fixture shape is copied from the real artifact
(``logs/session_20260726_132903_18116/.setup_agent/pytest-reports/pytest-attempt-000001.xml``):
``<testsuite ... errors="28" failures="0" skipped="28" tests="56">``.
"""

import contextlib
import io
import json
import re

import pytest

from sag.agent.physical_validator import _COMPACT_REPORT_PARSER_BODY, PhysicalValidator

PYTEST_REPORT_DIR = "/workspace/.setup_agent/pytest-reports"

# --- verbatim-shaped collection-error bodies from the live TVM artifact ------
# The dominant message (16 nodes). Its structured tail is the `E   ` line.
_TARGET_ERROR_BODY = """tests/nightly/python/test_nnapi/test_from_exported_to_cuda.py:73: in &lt;module&gt;
    @pytest.mark.skipif(not tvm.testing.device_enabled("cuda"), reason="cuda not enabled")
                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
python/tvm/testing/utils.py:438: in _get_targets
    raise RuntimeError(
E   RuntimeError: None of the following targets are supported by this build of TVM: ['llvm', 'cuda', 'nvptx', 'metal', 'rocm', 'hexagon']. Try setting TVM_TEST_TARGETS to a supported target. Cannot default to llvm, as it is not enabled."""

# The runner-up (12 nodes). Two `E   ` lines: the LAST one is the real cause,
# the first is the swallowed AttributeError.
_LLVM_ERROR_BODY = """python/tvm/target/codegen.py:265: in llvm_version_major
    return _ffi_api.llvm_version_major()
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   AttributeError: module 'tvm.target._ffi_api' has no attribute 'llvm_version_major'

During handling of the above exception, another exception occurred:
tests/python/codegen/test_target_codegen_aarch64.py:33: in &lt;module&gt;
    llvm_version_major() &lt; 15, reason="Test requires an LLVM version of at least 15"
    ^^^^^^^^^^^^^^^^^^^^
E   RuntimeError: LLVM version is not available, please check if you built TVM with LLVM"""

_SKIP_BODY = (
    "('/workspace/tvm/tests/nightly/python/test_nnapi/test_network.py', 22, "
    "\"Skipped: could not import 'onnx': No module named 'onnx'\")"
)

DOMINANT_SUMMARY_PREFIX = "RuntimeError: None of the following targets"


def _tvm_collection_xml(target_errors: int = 16, llvm_errors: int = 12, skips: int = 28) -> str:
    """Rebuild the live TVM attempt XML shape at a chosen node census."""
    errors = target_errors + llvm_errors
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<testsuites name="pytest tests">',
        f'<testsuite name="pytest" errors="{errors}" failures="0" skipped="{skips}" '
        f'tests="{errors + skips}" time="150.304" hostname="58940470bc7c">',
        '<properties><property name="sag.attempt_id" value="1" /></properties>',
    ]
    for i in range(target_errors):
        parts.append(
            f'<testcase classname="" name="tests.python.target.test_target_{i}" time="0.000">'
            f'<error message="collection failure">{_TARGET_ERROR_BODY}</error></testcase>'
        )
    for i in range(llvm_errors):
        parts.append(
            f'<testcase classname="" name="tests.python.codegen.test_llvm_{i}" time="0.000">'
            f'<error message="collection failure">{_LLVM_ERROR_BODY}</error></testcase>'
        )
    for i in range(skips):
        parts.append(
            f'<testcase classname="" name="tests.nightly.python.test_nnapi.test_net_{i}" '
            f'time="0.000"><skipped message="collection skipped">{_SKIP_BODY}</skipped>'
            "</testcase>"
        )
    parts.append("</testsuite></testsuites>")
    return "".join(parts)


TVM_COLLECTION_XML = _tvm_collection_xml()

# Regression lock: the Plan-1 Groovy/bigtop fixture shape must be counted
# exactly as before (real classnames -> real executed tests).
BIGTOP_SUITE_XML = """<testsuite name="org.apache.bigtop.itest.pmanager.PackageManagerTest" tests="3" failures="1" errors="1" skipped="0">
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDeps" time="0.1"><failure message="boom"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDocs" time="0.1"><error message="crash"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="installBash" time="0.1"/>
</testsuite>"""

# A named test that really was skipped keeps counting as skipped even when it
# shares the file with collection failures (plan: "ONLY when they carry a
# non-empty classname").
MIXED_XML = """<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="0" skipped="2" tests="4">
<properties><property name="sag.attempt_id" value="1" /></properties>
<testcase classname="" name="tests.python.test_broken" time="0.0"><error message="collection failure">E   RuntimeError: LLVM version is not available, please check if you built TVM with LLVM</error></testcase>
<testcase classname="" name="tests.python.test_absent" time="0.0"><skipped message="collection skipped">no module</skipped></testcase>
<testcase classname="tests.python.test_ok" name="test_real_skip" time="0.0"><skipped message="collection skipped">needs gpu</skipped></testcase>
<testcase classname="tests.python.test_ok" name="test_real_pass" time="0.01"/>
</testsuite></testsuites>"""


def _validator():
    return PhysicalValidator.__new__(PhysicalValidator)


class _PytestReportOrchestrator:
    """Shell-path orchestrator: no compact parser, one pytest report dir."""

    def __init__(self, xml_files):
        self.xml_files = dict(xml_files)
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        if "SAG_COMPACT_TEST_REPORT_PARSER" in c:
            return {"exit_code": 1, "output": ""}
        if f"test -d {PYTEST_REPORT_DIR}" in c:
            return {"exit_code": 0, "output": "EXISTS"}
        if "-type d" in c:
            return {"exit_code": 0, "output": ""}
        if "-name '*.xml'" in c and PYTEST_REPORT_DIR in c:
            return {"exit_code": 0, "output": "\n".join(self.xml_files)}
        if c.startswith("cat "):
            m = re.search(r"cat '([^']+)'", c)
            if m and m.group(1) in self.xml_files:
                return {"exit_code": 0, "output": self.xml_files[m.group(1)]}
            return {"exit_code": 1, "output": ""}
        return {"exit_code": 1, "output": ""}


def _run_compact_parser(project_dir: str, pytest_reports_dir: str) -> dict:
    """Execute the in-container parser body locally (same source string).

    Plan 5 Task B2 added two prepended coordinates (``receipts_dir``,
    ``primary_root``). These sessions have no invocation receipts, so the
    parser stays on its legacy global-scan basis.
    """
    namespace = {
        "project_dir": project_dir,
        "pytest_reports_dir": pytest_reports_dir,
        "receipts_dir": "/workspace/.setup_agent/invocation_receipts",
        "primary_root": None,
    }
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(_COMPACT_REPORT_PARSER_BODY, "<compact-parser>", "exec"), namespace)
    return json.loads(buffer.getvalue())


# ---------------------------------------------------------------------------
# Per-file parse: collection nodes are counted apart from executed tests
# ---------------------------------------------------------------------------
def test_collection_only_attempt_executes_nothing():
    stats = _validator()._parse_single_test_xml(TVM_COLLECTION_XML, "pytest-attempt-000001.xml")

    assert stats is not None
    assert stats["total"] == 0
    assert stats["passed"] == 0
    assert stats["failed"] == 0
    assert stats["errors"] == 0
    assert stats["skipped"] == 0
    assert stats["collection_errors"] == 28
    assert stats["collection_errors_skipped"] == 28
    # Collection nodes never become test identities.
    assert stats["testcases"] == []


def test_collection_error_summary_quotes_the_dominant_structured_error():
    stats = _validator()._parse_single_test_xml(TVM_COLLECTION_XML, "pytest-attempt-000001.xml")

    assert stats["collection_error_summary"].startswith(DOMINANT_SUMMARY_PREFIX)
    # One line only, and never the swallowed intermediate exception.
    assert "\n" not in stats["collection_error_summary"]
    assert "AttributeError" not in stats["collection_error_summary"]


def test_collection_error_summary_follows_the_majority_message():
    """Flip the census: the summary tracks the most frequent message."""
    xml = _tvm_collection_xml(target_errors=3, llvm_errors=9, skips=0)
    stats = _validator()._parse_single_test_xml(xml, "pytest-attempt-000002.xml")

    assert stats["collection_errors"] == 12
    assert stats["collection_error_summary"] == (
        "RuntimeError: LLVM version is not available, please check if you built TVM with LLVM"
    )


def test_named_testcases_are_unaffected_by_collection_semantics():
    """Regression lock — the Plan-1 bigtop/Groovy shape counts exactly as before."""
    stats = _validator()._parse_single_test_xml(BIGTOP_SUITE_XML, "TEST-PackageManagerTest.xml")

    assert stats["total"] == 3
    assert stats["failed"] == 1
    assert stats["errors"] == 1
    assert stats["passed"] == 1
    assert stats["collection_errors"] == 0
    assert stats["collection_errors_skipped"] == 0
    assert stats["collection_error_summary"] is None


def test_named_skip_survives_alongside_collection_nodes():
    stats = _validator()._parse_single_test_xml(MIXED_XML, "pytest-attempt-000003.xml")

    assert stats["total"] == 2
    assert stats["passed"] == 1
    assert stats["skipped"] == 1
    assert stats["errors"] == 0
    assert stats["collection_errors"] == 1
    assert stats["collection_errors_skipped"] == 1
    assert {tc["classname"] for tc in stats["testcases"]} == {"tests.python.test_ok"}


# ---------------------------------------------------------------------------
# Aggregation: shell path, compact in-container path, sealed verdict
# ---------------------------------------------------------------------------
def test_parse_test_reports_reports_zero_executed_for_collection_only_run():
    xml_path = f"{PYTEST_REPORT_DIR}/pytest-attempt-000001.xml"
    orch = _PytestReportOrchestrator({xml_path: TVM_COLLECTION_XML})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.parse_test_reports("/tmp/tvm")

    assert result["valid"] is True
    assert result["total_tests"] == 0
    assert result["raw_total_tests"] == 0
    assert result["unique_tests"] == 0
    assert result["error_tests"] == 0
    assert result["skipped_tests"] == 0
    assert result["collection_errors"] == 28
    assert result["collection_errors_skipped"] == 28
    assert result["collection_error_summary"].startswith(DOMINANT_SUMMARY_PREFIX)
    assert result["test_success"] is False


def test_compact_in_container_parser_agrees_with_the_shell_path(tmp_path):
    """The live TVM runs used the compact parser — it must count the same."""
    project_dir = tmp_path / "tvm"
    project_dir.mkdir()
    reports = tmp_path / "pytest-reports"
    reports.mkdir()
    (reports / "pytest-attempt-000001.xml").write_text(TVM_COLLECTION_XML, encoding="utf-8")

    parsed = _run_compact_parser(str(project_dir), str(reports))

    assert parsed["valid"] is True
    assert parsed["total_tests"] == 0
    assert parsed["raw_total_tests"] == 0
    assert parsed["unique_tests"] == 0
    assert parsed["error_tests"] == 0
    assert parsed["skipped_tests"] == 0
    assert parsed["collection_errors"] == 28
    assert parsed["collection_errors_skipped"] == 28
    assert parsed["collection_error_summary"].startswith(DOMINANT_SUMMARY_PREFIX)
    assert parsed["test_success"] is False
    assert parsed["test_histories"] == []


def test_compact_parser_keeps_named_testcases(tmp_path):
    project_dir = tmp_path / "proj"
    reports_dir = project_dir / "target" / "surefire-reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "TEST-PackageManagerTest.xml").write_text(BIGTOP_SUITE_XML, encoding="utf-8")

    parsed = _run_compact_parser(str(project_dir), str(tmp_path / "absent-pytest-reports"))

    assert parsed["total_tests"] == 3
    assert parsed["failed_tests"] == 1
    assert parsed["error_tests"] == 1
    assert parsed["passed_tests"] == 1
    assert parsed["collection_errors"] == 0
    assert parsed["collection_errors_skipped"] == 0
    assert parsed["collection_error_summary"] is None


def test_sealed_test_stats_carry_collection_errors_with_zero_executed(monkeypatch):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.test_pass_threshold = 0.8
    validator.project_path = "/workspace"

    metrics = {
        "valid": True,
        "total_tests": 0,
        "passed_tests": 0,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "flaky_count": 0,
        "collection_errors": 28,
        "collection_errors_skipped": 28,
        "collection_error_summary": (
            "RuntimeError: None of the following targets are supported by this build of TVM"
        ),
        "report_files": [f"{PYTEST_REPORT_DIR}/pytest-attempt-000001.xml"],
        "metrics_conflicts": [],
        "parsing_errors": [],
    }
    monkeypatch.setattr(
        PhysicalValidator, "parse_test_reports_with_catalog", lambda self, project_dir: metrics
    )
    monkeypatch.setattr(PhysicalValidator, "_python_collected_count", lambda self, name: None)

    status = validator.validate_test_status("tvm")

    assert status["total_tests"] == 0
    assert status["collection_errors"] == 28
    assert status["collection_errors_skipped"] == 28
    assert status["collection_error_summary"].startswith(DOMINANT_SUMMARY_PREFIX)
    assert status["test_stats"]["executed"] == 0
    assert status["test_stats"]["collection_errors"] == 28
    # The verdict cap must not evaporate with the (wrong) 28 "errors".
    assert "test_collection_failed" in status["conflicts"]
    assert status["evidence_status"] == "blocked"
    assert "0 tests executed" in status["reason"]


@pytest.mark.parametrize("collection_errors", [0, 28])
def test_conflict_marker_only_fires_on_collection_failures(monkeypatch, collection_errors):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.test_pass_threshold = 0.8
    validator.project_path = "/workspace"

    metrics = {
        "valid": True,
        "total_tests": 4,
        "passed_tests": 4,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "flaky_count": 0,
        "collection_errors": collection_errors,
        "collection_errors_skipped": 0,
        "collection_error_summary": "RuntimeError: boom" if collection_errors else None,
        "report_files": ["/tmp/x.xml"],
        "metrics_conflicts": [],
        "parsing_errors": [],
    }
    monkeypatch.setattr(
        PhysicalValidator, "parse_test_reports_with_catalog", lambda self, project_dir: metrics
    )
    monkeypatch.setattr(PhysicalValidator, "_python_collected_count", lambda self, name: None)

    status = validator.validate_test_status("demo")

    assert status["total_tests"] == 4
    assert ("test_collection_failed" in status["conflicts"]) is bool(collection_errors)
