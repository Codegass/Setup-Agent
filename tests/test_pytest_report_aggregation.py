# tests/test_pytest_report_aggregation.py
"""Aggregate pytest JUnit XMLs per test across invocations (live 2026-07-10).

python_tool writes ONE cumulative JUnit XML per pytest invocation into
PYTEST_REPORT_DIR with an explicit sag.attempt_id. The requests run produced a full-suite
XML and then a diagnostic subset re-run XML; the validator's final read scored
ONLY the subset (0/8 -> verdict FAILED) while the truth was ~619/635 passing.

Contract under test (WS7):
- ALL *.xml files in the pytest-reports dir are parsed;
- canonical (module_or_file, class, name, param_id) identity is shared;
- latest means max explicit attempt_id, never filename or mtime;
- first/latest/worst/retried/flaky history remains visible;
- primary counts use canonical latest status while raw counts remain diagnostics.

And FIX B: with no static_test_count in the env summary, a python project's
pytest --collect-only denominator (COLLECTED_JSON) must feed BOTH
test_stats.discovered and the static_test_count the execution-coverage gate
consumes.

The compact in-container parser is exercised FOR REAL: a local orchestrator
executes the emitted `python3 - <<'PY'` command with a subprocess against
tmp-dir fixtures (same code path as the live container run).
"""

import json
import os
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


def _junit_xml(cases, suite="pytest", attempt_id=1):
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
        f'<properties><property name="sag.attempt_id" value="{attempt_id}"/></properties>'
        + "".join(rows)
        + "</testsuite></testsuites>"
    )


def _full_suite(broken_status, attempt_id=1):
    """10 tests: 9 always pass, test_broken carries broken_status."""
    cases = [("tests.test_api", f"test_ok_{i}", "passed") for i in range(9)]
    cases.append(("tests.test_api", "test_broken", broken_status))
    return _junit_xml(cases, attempt_id=attempt_id)


def test_explicit_attempt_id_drives_latest_worst_retry_and_flaky_history(workspace):
    project, reports = workspace
    paths = [reports / "pytest-zeta.xml", reports / "pytest-alpha.xml", reports / "pytest-mid.xml"]
    statuses = ((2, "failed"), (1, "failed"), (3, "passed"))
    for path, (attempt_id, status) in zip(paths, statuses, strict=True):
        path.write_text(
            _junit_xml(
                [("tests.test_api", "test_flaky", status)],
                attempt_id=attempt_id,
            )
        )
    # Deliberately make mtime order disagree with attempt order too.
    for timestamp, path in enumerate(reversed(paths), start=100):
        os.utime(path, (timestamp, timestamp))

    result = _parse(project)

    assert result["total_tests"] == 1
    assert result["passed_tests"] == 1
    assert result["failed_tests"] == 0
    assert result["flaky_count"] == 1
    assert result["retried_count"] == 2
    history = result["test_histories"][0]
    assert history["first"] == "failed"
    assert history["latest"] == "passed"
    assert history["worst"] == "failed"
    assert history["retried_count"] == 2
    assert history["attempt_ids"] == [1, 2, 3]


def test_missing_attempt_id_is_a_conflict_and_never_uses_filename_order(workspace):
    project, reports = workspace
    without_attempt = _junit_xml([("tests.test_api", "test_case", "failed")]).replace(
        '<properties><property name="sag.attempt_id" value="1"/></properties>', ""
    )
    later_named_pass = _junit_xml([("tests.test_api", "test_case", "passed")]).replace(
        '<properties><property name="sag.attempt_id" value="1"/></properties>', ""
    )
    (reports / "pytest-1.xml").write_text(without_attempt)
    (reports / "pytest-999999.xml").write_text(later_named_pass)

    result = _parse(project)

    # Both observations stay in the same conservative attempt and merge to
    # worst. The epoch-looking filename is never treated as execution order.
    assert result["total_tests"] == 1
    assert result["failed_tests"] == 1
    assert result["passed_tests"] == 0
    assert result["retried_count"] == 0
    assert "test_attempt_id_invalid" in result["metrics_conflicts"]


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
        _junit_xml(
            [("tests.test_api", "test_broken", "passed")],
            attempt_id=2,
        )
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
        _junit_xml(
            [("tests.test_api", "test_broken", "failed")],
            attempt_id=2,
        )
    )

    result = _parse(project)

    assert result["total_tests"] == 10
    assert result["passed_tests"] == 9
    assert result["failed_tests"] == 1
    assert result["test_success"] is False
    assert result["failing_test_names"] == ["tests.test_api::test_broken"]


def test_later_invocation_wins_per_test_by_explicit_attempt_id(workspace):
    """The explicit attempt id, never the report filename, selects latest."""
    project, reports = workspace
    (reports / "pytest-z-last.xml").write_text(
        _junit_xml(
            [("tests.test_api", "test_flaky", "failed")],
            attempt_id=1,
        )
    )
    (reports / "pytest-100.xml").write_text(
        _junit_xml(
            [("tests.test_api", "test_flaky", "passed")],
            attempt_id=2,
        )
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
    assert result["unique_tests"] == 8
    assert result["unique_methods"] == 1


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
            ],
            attempt_id=2,
        )
    )

    result = _parse(project)

    assert result["total_tests"] == 12
    assert result["passed_tests"] == 12
    assert result["failed_tests"] == 0
    assert result["failing_test_names"] == []


# ---------------------------------------------------------------------------
# Canonical/raw basis also applies to Maven surefire reports
# ---------------------------------------------------------------------------


def test_maven_surefire_raw_is_diagnostic_and_primary_is_canonical(workspace):
    """Without explicit attempt metadata, surefire files share conservative
    attempt 1: primary counts dedupe canonically while raw keeps every row."""
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
    # Primary counts use the canonical latest/worst-safe attempt-1 basis;
    # raw diagnostics retain all 4 testcase observations.
    assert result["total_tests"] == 3
    assert result["passed_tests"] == 2
    assert result["failed_tests"] == 1
    assert result["raw_total_tests"] == 4
    assert result["raw_passed_tests"] == 3
    assert result["test_success"] is False
    # Severity merge (NOT latest-wins) for JVM reports: testC stays failed.
    assert result["unique_tests"] == 3
    assert result["unique_failed_tests"] == 1
    assert "com.example.FooTest::testC" in result["failing_test_names"]


# ---------------------------------------------------------------------------
# FIX A, shell fallback path: when the compact in-container parser is
# unavailable, the find/cat fallback must apply the SAME pytest per-test
# aggregation instead of inferring retry order from transport filenames.
# ---------------------------------------------------------------------------


class CompactParserDownOrch(LocalExecOrch):
    """Compact in-container parser fails (exit 1) -> the shell find/cat
    fallback executes for real against the tmp-dir fixtures."""

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "SAG_COMPACT_TEST_REPORT_PARSER" in cmd:
            self.commands.append(cmd)
            return {"exit_code": 1, "success": False, "output": ""}
        return super().execute_command(cmd, workdir=workdir, **kwargs)


def _parse_fallback(project):
    validator = PhysicalValidator(
        docker_orchestrator=CompactParserDownOrch(), project_path=str(project.parent)
    )
    return validator.parse_test_reports(str(project))


def test_fallback_shell_path_subset_rerun_aggregates_per_test(workspace):
    """Full run 9/10 + subset re-run of the 1 failure now passing -> 10/10
    through the shell fallback too: no double-counted total (11), no
    never-overridable failure from the severity merge."""
    project, reports = workspace
    (reports / "pytest-1000.xml").write_text(_full_suite("failed"))
    (reports / "pytest-2000.xml").write_text(
        _junit_xml(
            [("tests.test_api", "test_broken", "passed")],
            attempt_id=2,
        )
    )

    result = _parse_fallback(project)

    assert result["valid"] is True
    assert result["total_tests"] == 10
    assert result["passed_tests"] == 10
    assert result["failed_tests"] == 0
    assert result["test_success"] is True
    assert result["failing_test_names"] == []
    # Raw executions stay honest: 11 runs happened, 1 of them failed.
    assert result["raw_total_tests"] == 11
    assert result["raw_failed_tests"] == 1


def test_fallback_shell_path_latest_invocation_uses_explicit_attempt_id(workspace):
    """The fallback path also ignores filename order for latest."""
    project, reports = workspace
    (reports / "pytest-z-last.xml").write_text(
        _junit_xml(
            [("tests.test_api", "test_flaky", "failed")],
            attempt_id=1,
        )
    )
    (reports / "pytest-100.xml").write_text(
        _junit_xml(
            [("tests.test_api", "test_flaky", "passed")],
            attempt_id=2,
        )
    )

    result = _parse_fallback(project)

    assert result["total_tests"] == 1
    assert result["passed_tests"] == 1
    assert result["failed_tests"] == 0
    assert result["test_success"] is True
    assert result["failing_test_names"] == []


def test_fallback_shell_path_parameterized_union(workspace):
    """Parameterized invocations stay distinct canonical test identities."""
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

    result = _parse_fallback(project)

    assert result["total_tests"] == 8
    assert result["failed_tests"] == 8
    assert result["unique_tests"] == 8
    assert result["unique_methods"] == 1


def test_fallback_shell_path_maven_uses_the_same_canonical_basis(workspace):
    """The fallback gives surefire the same canonical/raw split."""
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

    result = _parse_fallback(project)

    assert result["valid"] is True
    assert result["total_tests"] == 3
    assert result["passed_tests"] == 2
    assert result["failed_tests"] == 1
    assert result["raw_total_tests"] == 4
    assert result["raw_passed_tests"] == 3
    assert result["test_success"] is False
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
    subset XML (total 8, canonical unique 8), so the gate is exercised
    with numbers the live pipeline can actually produce. The collect-only
    denominator is param-EXPANDED, so the coverage numerator must be the
    param-expanded executed union (8), never the stripped method count (1)."""
    project, reports = workspace
    (reports / "pytest-1783661384.xml").write_text(_SUBSET_XML)
    parsed = _parse(project)
    assert parsed["total_tests"] == 8
    assert parsed["unique_tests"] == 8
    assert parsed["unique_methods"] == 1

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

    snapshot = tool._build_legacy_report_snapshot(
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
    """One full 100%-green pytest run: 50 collected, 50 canonical executions,
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
    assert parsed["unique_tests"] == 50
    assert parsed["unique_methods"] == 21

    tool = ReportTool()
    accomplishments = {
        "physical_validation": {
            "test_status": {"static_test_count": 50},
            "test_analysis": parsed,
        },
    }

    snapshot = tool._build_legacy_report_snapshot(
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

    snapshot = tool._build_legacy_report_snapshot(
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


def test_collect_only_denominator_outranks_env_summary_on_python():
    """python denominator priority (bug #7, click live probe): the pytest
    --collect-only count is ground truth from the actual runner and OVERRIDES
    an env-summary static count when both exist — static scans can be polluted
    by the venv the setup plants inside the project dir (click: 32927 static
    vs 1927 collected capped a 98.7% run at PARTIAL). The static count is kept
    as evidence and stays the fallback when no collected count exists; java
    priority order is unchanged (see tests/test_static_scan_exclusions.py)."""

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

    assert analysis["static_test_count"] == 635
    assert analysis["static_test_count_source"] == "pytest_collect_only"
    assert analysis["static_test_count_static_scan"] == 700
