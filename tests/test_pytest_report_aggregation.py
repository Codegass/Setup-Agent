# tests/test_pytest_report_aggregation.py
"""Aggregate pytest JUnit XMLs per test across invocations (live 2026-07-10).

python_tool writes ONE cumulative JUnit XML per pytest invocation into
PYTEST_REPORT_DIR (pytest-<epoch>.xml). The requests run produced a full-suite
XML and then a diagnostic subset re-run XML; the validator's final read scored
ONLY the subset (0/8 -> verdict FAILED) while the truth was ~619/635 passing.

Contract under test (FIX A):
- ALL *.xml files in the pytest-reports dir are parsed;
- per-test dedupe with the LATEST invocation winning (epoch in the filename,
  mtime fallback) — a subset re-run updates the tests it ran but never erases
  tests it did not run;
- executed/passed/failed = the deduped union;
- Maven/Gradle per-dir behavior stays byte-identical (raw accumulation,
  severity-merged statuses).

And FIX B: with no static_test_count in the env summary, a python project's
pytest --collect-only denominator (COLLECTED_JSON) must feed BOTH
test_stats.discovered and the static_test_count the execution-coverage gate
consumes.

The compact in-container parser is exercised FOR REAL: a local orchestrator
executes the emitted `python3 - <<'PY'` command with a subprocess against
tmp-dir fixtures (same code path as the live container run).
"""

import json
import subprocess

import pytest

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.python_tool import COLLECTED_JSON, PYTEST_REPORT_DIR
from sag.tools.report_tool import ReportTool

# ---------------------------------------------------------------------------
# Harness: run validator commands with a real local shell (tmp-dir sandbox)
# ---------------------------------------------------------------------------


class LocalExecOrch:
    """Executes every validator command via bash so the compact in-container
    parser runs exactly as it would inside the container."""

    def __init__(self):
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        return {
            "exit_code": proc.returncode,
            "success": proc.returncode == 0,
            "output": proc.stdout if proc.stdout else proc.stderr,
        }


def _junit_xml(cases, suite="pytest"):
    """Real pytest --junitxml shape: <testsuites> wrapping one <testsuite>."""
    rows = []
    failures = errors = skips = 0
    for classname, name, status in cases:
        if status == "failed":
            failures += 1
            rows.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01">'
                '<failure message="assert 1 == 2">AssertionError</failure></testcase>'
            )
        elif status == "error":
            errors += 1
            rows.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01">'
                '<error message="boom">RuntimeError</error></testcase>'
            )
        elif status == "skipped":
            skips += 1
            rows.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01">'
                "<skipped/></testcase>"
            )
        else:
            rows.append(f'<testcase classname="{classname}" name="{name}" time="0.01" />')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites>'
        f'<testsuite name="{suite}" errors="{errors}" failures="{failures}" '
        f'skipped="{skips}" tests="{len(cases)}" time="1.0">'
        + "".join(rows)
        + "</testsuite></testsuites>"
    )


def _full_suite(broken_status):
    """10 tests: 9 always pass, test_broken carries broken_status."""
    cases = [("tests.test_api", f"test_ok_{i}", "passed") for i in range(9)]
    cases.append(("tests.test_api", "test_broken", broken_status))
    return _junit_xml(cases)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """tmp project dir + injected pytest-reports dir (mirrors PYTEST_REPORT_DIR)."""
    project = tmp_path / "proj"
    project.mkdir()
    reports = tmp_path / "pytest-reports"
    reports.mkdir()
    monkeypatch.setattr("sag.tools.internal.python_tool.PYTEST_REPORT_DIR", str(reports))
    return project, reports


def _parse(project):
    validator = PhysicalValidator(
        docker_orchestrator=LocalExecOrch(), project_path=str(project.parent)
    )
    return validator.parse_test_reports(str(project))


# ---------------------------------------------------------------------------
# FIX A: the two-XML live scenario (full run + diagnostic subset re-run)
# ---------------------------------------------------------------------------


def test_subset_rerun_that_now_passes_updates_only_that_test(workspace):
    """Full run 9/10 + subset re-run of the 1 failure now passing -> 10/10."""
    project, reports = workspace
    (reports / "pytest-1000.xml").write_text(_full_suite("failed"))
    (reports / "pytest-2000.xml").write_text(
        _junit_xml([("tests.test_api", "test_broken", "passed")])
    )

    result = _parse(project)

    assert result["valid"] is True
    assert result["report_file_count"] == 2
    assert result["total_tests"] == 10
    assert result["passed_tests"] == 10
    assert result["failed_tests"] == 0
    assert result["test_success"] is True
    assert result["failing_test_names"] == []
    # Raw executions stay honest: 11 runs happened, 1 of them failed.
    assert result["raw_total_tests"] == 11
    assert result["raw_failed_tests"] == 1


def test_subset_rerun_still_failing_keeps_full_denominator(workspace):
    """Subset still failing -> 10 executed, 9 passed, 1 failed — NOT 0/1."""
    project, reports = workspace
    (reports / "pytest-1000.xml").write_text(_full_suite("failed"))
    (reports / "pytest-2000.xml").write_text(
        _junit_xml([("tests.test_api", "test_broken", "failed")])
    )

    result = _parse(project)

    assert result["total_tests"] == 10
    assert result["passed_tests"] == 9
    assert result["failed_tests"] == 1
    assert result["test_success"] is False
    assert result["failing_test_names"] == ["tests.test_api::test_broken"]


def test_later_invocation_wins_per_test_by_filename_epoch(workspace):
    """Ordering follows the epoch in the filename, not lexicographic sorting:
    pytest-100.xml (epoch 100) is LATER than pytest-99.xml even though it
    sorts first as a string, so its passing result must win."""
    project, reports = workspace
    (reports / "pytest-99.xml").write_text(_junit_xml([("tests.test_api", "test_flaky", "failed")]))
    (reports / "pytest-100.xml").write_text(
        _junit_xml([("tests.test_api", "test_flaky", "passed")])
    )

    result = _parse(project)

    assert result["total_tests"] == 1
    assert result["passed_tests"] == 1
    assert result["failed_tests"] == 0
    assert result["test_success"] is True


def test_parameterized_invocations_stay_distinct_in_the_union(workspace):
    """8 parameterized executions of one test are 8 union entries (the executed
    count must stay comparable to pytest's own totals and --collect-only)."""
    project, reports = workspace
    params = ["http", "https", "all", "mixed", "socks", "env", "noproxy", "cidr"]
    (reports / "pytest-1000.xml").write_text(
        _junit_xml(
            [
                ("tests.test_lowlevel", f"test_use_proxy_from_environment[{p}]", "failed")
                for p in params
            ]
        )
    )

    result = _parse(project)

    assert result["total_tests"] == 8
    assert result["failed_tests"] == 8
    assert result["unique_tests"] == 1  # method-level dedupe unchanged


def test_subset_rerun_updates_all_its_parameterized_executions(workspace):
    """A re-run fixing the parameterized test flips all 8 union entries."""
    project, reports = workspace
    params = ["http", "https", "all", "mixed", "socks", "env", "noproxy", "cidr"]
    cases = [("tests.test_api", f"test_ok_{i}", "passed") for i in range(4)]
    cases += [
        ("tests.test_lowlevel", f"test_use_proxy_from_environment[{p}]", "failed") for p in params
    ]
    (reports / "pytest-1000.xml").write_text(_junit_xml(cases))
    (reports / "pytest-2000.xml").write_text(
        _junit_xml(
            [
                ("tests.test_lowlevel", f"test_use_proxy_from_environment[{p}]", "passed")
                for p in params
            ]
        )
    )

    result = _parse(project)

    assert result["total_tests"] == 12
    assert result["passed_tests"] == 12
    assert result["failed_tests"] == 0
    assert result["failing_test_names"] == []


# ---------------------------------------------------------------------------
# FIX A guard: Maven surefire per-dir behavior stays byte-identical
# ---------------------------------------------------------------------------


def test_maven_surefire_raw_accumulation_unchanged(workspace):
    """Two surefire XMLs (a re-run passing a previously failed test) keep the
    legacy semantics: primary counts are RAW sums across files and the unique
    status merge stays severity-based (failed wins over a later pass)."""
    project, _reports = workspace
    surefire = project / "target" / "surefire-reports"
    surefire.mkdir(parents=True)
    first = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="com.example.FooTest" tests="3" failures="1" errors="0" skipped="0">'
        '<testcase classname="com.example.FooTest" name="testA" time="0.01" />'
        '<testcase classname="com.example.FooTest" name="testB" time="0.01" />'
        '<testcase classname="com.example.FooTest" name="testC" time="0.01">'
        '<failure message="nope">AssertionError</failure></testcase>'
        "</testsuite>"
    )
    rerun = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="com.example.FooTest" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="com.example.FooTest" name="testC" time="0.01" />'
        "</testsuite>"
    )
    (surefire / "TEST-com.example.FooTest.xml").write_text(first)
    (surefire / "TEST-com.example.FooTest.RERUN.xml").write_text(rerun)

    result = _parse(project)

    assert result["valid"] is True
    # Raw accumulation: 4 executions across the two files, 1 failure recorded.
    assert result["total_tests"] == 4
    assert result["passed_tests"] == 3
    assert result["failed_tests"] == 1
    assert result["test_success"] is False
    # Severity merge (NOT latest-wins) for JVM reports: testC stays failed.
    assert result["unique_tests"] == 3
    assert result["unique_failed_tests"] == 1
    assert "com.example.FooTest::testC" in result["failing_test_names"]


# ---------------------------------------------------------------------------
# FIX B: pytest --collect-only denominator feeds discovered + the coverage gate
# ---------------------------------------------------------------------------

_SUBSET_XML_FILE = f"{PYTEST_REPORT_DIR}/pytest-1783661384.xml"

_SUBSET_XML = _junit_xml(
    [
        (
            "tests.test_lowlevel",
            f"test_use_proxy_from_environment[{p}]",
            "failed",
        )
        for p in ("http", "https", "all", "mixed", "socks", "env", "noproxy", "cidr")
    ]
)


class CollectedDenominatorOrch:
    """Live requests-run shape: only the subset XML on disk, no trunk static
    count, python build system, COLLECTED_JSON present with the real total."""

    def __init__(self, collected=635):
        self.collected = collected
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        c = cmd.strip()
        if "SAG_COMPACT_TEST_REPORT_PARSER" in c:
            # Force the shell discovery path (canned commands below).
            return {"exit_code": 1, "success": False, "output": ""}
        if "pom.xml && echo" in c:
            return {"exit_code": 0, "output": "MISSING"}
        if c.startswith(f"test -d {PYTEST_REPORT_DIR}"):
            return {"exit_code": 0, "output": "EXISTS"}
        if c.startswith("find") and "-type d" in c:
            return {"exit_code": 0, "output": ""}
        if c.startswith("find") and PYTEST_REPORT_DIR in c and "'*.xml'" in c:
            return {"exit_code": 0, "output": _SUBSET_XML_FILE}
        if c == f"cat '{_SUBSET_XML_FILE}'":
            return {"exit_code": 0, "output": _SUBSET_XML}
        if c == f"cat {COLLECTED_JSON}":
            return {"exit_code": 0, "output": json.dumps({"collected": self.collected})}
        if c.startswith("test -f "):
            return {"exit_code": 0 if c.endswith("pyproject.toml") else 1, "output": ""}
        return {"exit_code": 0, "output": ""}


def test_collected_json_fallback_populates_test_stats_discovered():
    orch = CollectedDenominatorOrch(collected=635)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_test_status("proj")

    assert result["has_test_reports"] is True
    assert result["test_stats"] is not None
    assert result["test_stats"]["discovered"] == 635
    assert result["static_test_count"] == 635
    assert any(COLLECTED_JSON in c for c in orch.commands)


def test_execution_coverage_gate_consumes_collected_fallback(workspace):
    """The report snapshot's static_test_count (the number the
    tests_not_fully_executed gate reads) must come from the collect-only
    denominator when trunk/catalog have nothing: 8 of 635 executed -> the
    conflict fires and caps the run, instead of a silent 0/8 false red.

    The test_analysis fed to the snapshot is the REAL parser's output for the
    subset XML (total 8, unique 1 — FIX A contract), so the gate is exercised
    with numbers the live pipeline can actually produce. The collect-only
    denominator is param-EXPANDED, so the coverage numerator must be the
    param-expanded executed union (8), never the stripped method count (1)."""
    project, reports = workspace
    (reports / "pytest-1783661384.xml").write_text(_SUBSET_XML)
    parsed = _parse(project)
    assert parsed["total_tests"] == 8
    assert parsed["unique_tests"] == 1  # parser-faithful: 8 params of ONE method

    tool = ReportTool()
    accomplishments = {
        "physical_validation": {
            "test_status": {
                "test_stats": {
                    "discovered": 635,
                    "executed": 8,
                    "passed": 0,
                    "failed": 8,
                    "skipped": 0,
                    "pass_rate": 0.0,
                },
            },
            "test_analysis": parsed,
        },
    }

    snapshot = tool._build_report_snapshot(
        verified_status="partial",
        report_filename="setup-report-test.md",
        project_info={"build_system": "pip/poetry"},
        actual_accomplishments=accomplishments,
        execution_metrics={},
    )

    status = snapshot["status"]
    assert status["static_test_count"] == 635
    assert status["execution_rate"] == pytest.approx(8 / 635 * 100, abs=0.01)
    assert "tests_not_fully_executed" in snapshot["evidence_result"]["conflicts"]


def test_full_green_parameterized_run_keeps_coverage_gate_quiet(workspace):
    """One full 100%-green pytest run: 50 collected, 50 executed union entries,
    21 unique methods. The pytest --collect-only denominator counts
    parameterized invocations ([param] expanded), so the coverage numerator
    must share that basis — comparing the param-STRIPPED unique method count
    (21) against the expanded denominator (50) read a genuinely full green run
    as 42% coverage, falsely fired tests_not_fully_executed and capped it at
    PARTIAL."""
    project, reports = workspace
    cases = [("tests.test_api", f"test_ok_{i}", "passed") for i in range(20)]
    cases += [("tests.test_params", f"test_matrix[case{i}]", "passed") for i in range(30)]
    (reports / "pytest-1000.xml").write_text(_junit_xml(cases))

    parsed = _parse(project)  # REAL compact parser
    assert parsed["total_tests"] == 50
    assert parsed["passed_tests"] == 50
    assert parsed["unique_tests"] == 21

    tool = ReportTool()
    accomplishments = {
        "physical_validation": {
            "test_status": {"static_test_count": 50},
            "test_analysis": parsed,
        },
    }

    snapshot = tool._build_report_snapshot(
        verified_status="success",
        report_filename="setup-report-test.md",
        project_info={"build_system": "pip/poetry"},
        actual_accomplishments=accomplishments,
        execution_metrics={},
    )

    status = snapshot["status"]
    assert status["static_test_count"] == 50
    assert status["execution_rate"] == pytest.approx(100.0, abs=0.01)
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get("conflicts", [])


def test_java_method_denominator_keeps_unique_numerator():
    """Maven/Gradle guard: static_test_count counts declared test METHODS
    (catalog scan), so the unique method count stays the numerator there — a
    surefire run with parameterized expansions (50 raw executions of 21
    methods) is 21/21 = 100% coverage, not 238% and not 42%."""
    tool = ReportTool()
    accomplishments = {
        "physical_validation": {
            "test_analysis": {
                "total_tests": 50,
                "passed_tests": 50,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "raw_total_tests": 50,
                "unique_tests": 21,
                "catalog_test_count": 21,
            },
        },
    }

    snapshot = tool._build_report_snapshot(
        verified_status="success",
        report_filename="setup-report-test.md",
        project_info={"build_system": "Maven"},
        actual_accomplishments=accomplishments,
        execution_metrics={},
    )

    status = snapshot["status"]
    assert status["static_test_count"] == 21
    assert status["execution_rate"] == pytest.approx(100.0, abs=0.01)
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get("conflicts", [])


def test_env_summary_static_count_still_wins_when_present():
    """The fallback NEVER overrides a count the analyzer already recorded
    (validate_project_analysis_status contract stays intact)."""

    class TrunkOrch(CollectedDenominatorOrch):
        def execute_command(self, cmd, workdir=None, **kwargs):
            c = cmd.strip()
            if c.startswith("ls ") and "trunk_" in c:
                self.commands.append(cmd)
                return {
                    "exit_code": 0,
                    "output": "/workspace/.setup_agent/contexts/trunk_1.json",
                }
            if c.startswith("cat ") and "trunk_" in c:
                self.commands.append(cmd)
                return {
                    "exit_code": 0,
                    "output": json.dumps({"environment_summary": {"static_test_count": 700}}),
                }
            return super().execute_command(cmd, workdir=workdir, **kwargs)

    orch = TrunkOrch(collected=635)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    analysis = validator.validate_project_analysis_status("proj")

    assert analysis["static_test_count"] == 700
