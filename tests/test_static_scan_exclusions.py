"""Bug #7 (click live probe): env dirs polluted the static test scan, and the
static heuristic outranked the pytest collect-only denominator.

Live 2026-07-10 click run: the setup created a `.venv` INSIDE the project dir;
the static test scan swept it (site-packages ships thousands of vendored test
functions) and recorded 32927 "static tests detected" while pytest actually
collected 1927. The execution-coverage gate then capped a 98.7% run (1902/1927
executed) at PARTIAL: "only 1927/32927 detected tests executed".

Two fixes under test here:

FIX A — the static test scans prune environment/vendor dirs at the scan
        level, never post-hoc. Unambiguous names (.venv, site-packages,
        node_modules, .git, .tox, .nox, __pycache__, .eggs) are pruned
        unconditionally; bare 'env'/'venv' are REAL java names (Spring ships
        an org.springframework.core.env test package, modules get named
        env/), so those are pruned ONLY when the directory carries a
        virtualenv signature (pyvenv.cfg, bin/activate, Scripts/activate,
        conda-meta).
FIX B — on python projects the pytest --collect-only count (COLLECTED_JSON,
        ground truth from the actual runner) takes PRIORITY over any static
        heuristic; the static count remains the fallback when no collected
        count exists. Java priority order unchanged (env summary wins).
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

# Reusable fakes from the original suites (pytest prepend import mode).
from test_agent_final_status import FakePhysicalValidator, _agent_with_validator

from sag.agent.physical_validator import PhysicalValidator
from sag.testcases.catalog import build_java_test_catalog
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool
from sag.tools.internal.python_tool import COLLECTED_JSON
from sag.tools.report_tool import ReportTool


# ---------------------------------------------------------------------------
# Harness: run the scan commands locally (they are self-contained `cd .. &&
# python3 - <<'PY'` scripts) so the exclusion behavior is exercised for real.
# ---------------------------------------------------------------------------
class LocalShellOrch:
    """Executes orchestrator commands on the local shell."""

    def __init__(self):
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=120)
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": proc.stdout,
            "error": proc.stderr,
        }


def _java_test_class(package, class_name, methods):
    body = "\n".join(f"    @Test\n    public void {m}() {{ }}\n" for m in methods)
    return (
        f"package {package};\n\nimport org.junit.jupiter.api.Test;\n\n"
        f"public class {class_name} {{\n{body}}}\n"
    )


def _make_click_shaped_tree(tmp_path, project_files=12, vendored_files=60):
    """Click's shape: the project's own tests plus a `.venv` the setup planted
    inside the project dir, whose site-packages carries hundreds of test
    functions/methods that must NEVER be counted."""
    project = tmp_path / "click"

    # The project's OWN tests (12 files, one test method each).
    own = project / "src" / "test" / "java" / "com" / "example"
    own.mkdir(parents=True)
    for i in range(project_files):
        (own / f"Own{i}Test.java").write_text(
            _java_test_class("com.example", f"Own{i}Test", ["testOne"])
        )

    # The environment the setup created inside the project: site-packages
    # ships vendored suites (pytest's own tests, bundled fixtures, ...).
    site = project / ".venv" / "lib" / "python3.12" / "site-packages" / "vendorpkg"
    vendored_java = site / "src" / "test" / "java" / "com" / "vendor"
    vendored_java.mkdir(parents=True)
    for i in range(vendored_files):
        (vendored_java / f"Vendor{i}Test.java").write_text(
            _java_test_class("com.vendor", f"Vendor{i}Test", [f"testV{j}" for j in range(5)])
        )
    # Python-style vendored test functions too (click's actual pollution).
    vendored_py = site / "tests"
    vendored_py.mkdir(parents=True)
    for i in range(vendored_files):
        (vendored_py / f"test_vendor_{i}.py").write_text(
            "\n".join(f"def test_v{j}():\n    pass\n" for j in range(5))
        )

    # Other env/vendor dirs from the exclusion list must be pruned as well.
    for env_dir in ("node_modules/dep", ".tox/py312", "venv/lib"):
        extra = project / env_dir / "src" / "test" / "java"
        extra.mkdir(parents=True)
        (extra / "ExtraTest.java").write_text(
            _java_test_class("com.extra", "ExtraTest", ["testExtra"])
        )
    # `venv` is only pruned on a virtualenv SIGNATURE (a bare name is a real
    # java module/package candidate); a planted env always carries one.
    (project / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")

    return project


# ===========================================================================
# FIX A (a): the static scan counts ONLY the project's own tests
# ===========================================================================
def test_static_catalog_scan_prunes_env_dirs(tmp_path):
    project = _make_click_shaped_tree(tmp_path)

    catalog = build_java_test_catalog(str(project), LocalShellOrch())

    assert catalog.count() == 12
    files = {d.file_path for d in catalog.get_all().values()}
    assert not any(".venv" in f or "site-packages" in f for f in files)
    assert not any("node_modules" in f or ".tox" in f for f in files)


def test_annotation_counter_prunes_env_dirs(tmp_path):
    project = _make_click_shaped_tree(tmp_path)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=LocalShellOrch())

    counts = analyzer._get_java_test_annotation_counts(str(project))

    assert counts is not None
    assert counts["Test"] == 12  # 12 own tests; 300+ vendored ones pruned


# ===========================================================================
# REJECT (b): bare 'env'/'venv' are REAL java names — the scan must keep them
# unless the directory carries a virtualenv signature.
# ===========================================================================
def _make_env_named_java_tree(tmp_path):
    """Reviewer repro: 'env' as a java package segment (a real convention —
    Spring's org.springframework.core.env test package) AND 'env' as a module
    name with its own src/test/java. Neither is a virtualenv."""
    project = tmp_path / "envproj"

    plain = project / "src" / "test" / "java" / "com" / "example"
    plain.mkdir(parents=True)
    (plain / "PlainTest.java").write_text(
        _java_test_class("com.example", "PlainTest", ["testPlain"])
    )

    # 1) java package segment named 'env'
    env_pkg = plain / "env"
    env_pkg.mkdir()
    (env_pkg / "EnvConfigTest.java").write_text(
        _java_test_class("com.example.env", "EnvConfigTest", ["testEnvConfig"])
    )

    # 2) module literally named 'env' containing src/test/java
    env_mod = project / "env" / "src" / "test" / "java" / "com" / "example" / "mod"
    env_mod.mkdir(parents=True)
    (env_mod / "EnvModuleTest.java").write_text(
        _java_test_class("com.example.mod", "EnvModuleTest", ["testEnvModule"])
    )

    return project


def test_catalog_keeps_env_package_and_env_module_tests(tmp_path):
    """All 3 real tests must land in the catalog (the catalog feeds
    parse_test_reports_with_catalog's unexecuted-test matching, so a pruned
    real test gets misreported as nonexistent)."""
    project = _make_env_named_java_tree(tmp_path)

    catalog = build_java_test_catalog(str(project), LocalShellOrch())

    assert catalog.count() == 3
    files = {d.file_path for d in catalog.get_all().values()}
    assert "src/test/java/com/example/env/EnvConfigTest.java" in files
    assert "env/src/test/java/com/example/mod/EnvModuleTest.java" in files


def test_annotation_counter_keeps_env_package_and_env_module_tests(tmp_path):
    project = _make_env_named_java_tree(tmp_path)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=LocalShellOrch())

    counts = analyzer._get_java_test_annotation_counts(str(project))

    assert counts is not None
    assert counts["Test"] == 3


def test_env_dir_with_virtualenv_signature_is_pruned(tmp_path):
    """A dir literally named env/ that IS a virtualenv (pyvenv.cfg) must stay
    pruned — that is the click-run pollution the exclusion exists for."""
    project = tmp_path / "sigproj"
    own = project / "src" / "test" / "java" / "com" / "example"
    own.mkdir(parents=True)
    (own / "OwnTest.java").write_text(_java_test_class("com.example", "OwnTest", ["testOwn"]))

    planted = project / "env"
    vendored = planted / "src" / "test" / "java" / "com" / "vendor"
    vendored.mkdir(parents=True)
    (vendored / "VendorTest.java").write_text(
        _java_test_class("com.vendor", "VendorTest", ["testVendor"])
    )
    (planted / "pyvenv.cfg").write_text("home = /usr/bin\n")

    catalog = build_java_test_catalog(str(project), LocalShellOrch())
    assert catalog.count() == 1
    files = {d.file_path for d in catalog.get_all().values()}
    assert not any(f.startswith("env/") for f in files)

    analyzer = ProjectAnalyzerTool(docker_orchestrator=LocalShellOrch())
    counts = analyzer._get_java_test_annotation_counts(str(project))
    assert counts is not None
    assert counts["Test"] == 1


def test_venv_dir_with_bin_activate_signature_is_pruned(tmp_path):
    """Old-style virtualenvs may lack pyvenv.cfg; bin/activate is a
    sufficient signature."""
    project = tmp_path / "actproj"
    own = project / "src" / "test" / "java" / "com" / "example"
    own.mkdir(parents=True)
    (own / "OwnTest.java").write_text(_java_test_class("com.example", "OwnTest", ["testOwn"]))

    planted = project / "venv"
    (planted / "bin").mkdir(parents=True)
    (planted / "bin" / "activate").write_text("# activate\n")
    vendored = planted / "src" / "test" / "java"
    vendored.mkdir(parents=True)
    (vendored / "VendorTest.java").write_text(
        _java_test_class("com.vendor", "VendorTest", ["testVendor"])
    )

    catalog = build_java_test_catalog(str(project), LocalShellOrch())

    assert catalog.count() == 1


def test_dist_named_module_tests_are_kept(tmp_path):
    """'dist' is NOT unconditionally excluded: it is a plausible java
    package/module segment, and python dist/ dirs hold archives, not loose
    test sources."""
    project = tmp_path / "distproj"
    mod = project / "dist" / "src" / "test" / "java" / "com" / "example"
    mod.mkdir(parents=True)
    (mod / "DistTest.java").write_text(_java_test_class("com.example", "DistTest", ["testDist"]))

    catalog = build_java_test_catalog(str(project), LocalShellOrch())

    assert catalog.count() == 1


# ===========================================================================
# FIX B: denominator priority — collect-only wins on python, static falls back
# ===========================================================================
class _DenominatorOrch:
    """Click's live shape: trunk env summary carries the polluted static count
    (32927) while COLLECTED_JSON has the runner's ground truth (1927)."""

    def __init__(self, build_marker="pyproject.toml", static=32927, collected=1927):
        self.build_marker = build_marker
        self.static = static
        self.collected = collected
        self.commands = []

    def execute_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        c = cmd.strip()
        if c.startswith("ls ") and "trunk_" in c:
            return {
                "exit_code": 0,
                "output": "/workspace/.setup_agent/contexts/trunk_1.json",
            }
        if c.startswith("cat ") and "trunk_" in c:
            return {
                "exit_code": 0,
                "output": json.dumps({"environment_summary": {"static_test_count": self.static}}),
            }
        if c == f"cat {COLLECTED_JSON}":
            if self.collected is None:
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": json.dumps({"collected": self.collected})}
        if c.startswith("test -f "):
            return {
                "exit_code": 0 if c.endswith(self.build_marker) else 1,
                "output": "",
            }
        return {"exit_code": 0, "output": ""}


def test_collect_only_denominator_outranks_static_count_on_python():
    """python: env-summary static=32927 present AND collected=1927 -> the gate
    denominator is 1927 (ground truth), the static heuristic is preserved as
    evidence only."""
    validator = PhysicalValidator(docker_orchestrator=_DenominatorOrch(), project_path="/workspace")

    result = validator.validate_project_analysis_status("click")

    assert result["static_test_count"] == 1927
    assert result["static_test_count_source"] == "pytest_collect_only"
    assert result["static_test_count_static_scan"] == 32927


def test_java_env_summary_static_count_priority_unchanged():
    """java: the env-summary static count stays authoritative even when a stale
    COLLECTED_JSON exists on disk."""
    orch = _DenominatorOrch(build_marker="pom.xml", static=1122, collected=999)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_project_analysis_status("javaproj")

    assert result["static_test_count"] == 1122
    assert "static_test_count_source" not in result


def test_static_count_still_used_when_collected_json_absent():
    """python without COLLECTED_JSON: the (post-exclusion) static count remains
    the denominator fallback."""
    orch = _DenominatorOrch(static=1927, collected=None)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_project_analysis_status("click")

    assert result["static_test_count"] == 1927
    assert "static_test_count_source" not in result


def test_agent_gate_no_partial_cap_with_collected_denominator():
    """click's numbers through the CLI gate: 1902 executed of 1927 collected
    (98.7% >= 80%) -> SUCCESS, no tests_not_fully_executed PARTIAL cap."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "build_complete": True, "reason": "ok"},
            test_status={
                "has_test_reports": True,
                "status": "SUCCESS",
                "reason": "98.7%",
                "pass_rate": 100.0,
                "total_tests": 1902,
                "passed_tests": 1902,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 1927,
                "static_test_count_source": "pytest_collect_only",
            },
        )
    )

    assert agent._legacy_get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "success"


def test_report_snapshot_prefers_collected_denominator_on_python():
    """The report snapshot's execution-coverage gate must use the collect-only
    denominator on python even when the trunk carries a polluted static count:
    1902 executed / 1927 collected -> no tests_not_fully_executed conflict."""
    tool = ReportTool(
        context_manager=SimpleNamespace(
            load_trunk_context=lambda: SimpleNamespace(
                environment_summary={"static_test_count": 32927}
            )
        )
    )
    accomplishments = {
        "physical_validation": {
            "test_status": {
                "static_test_count": 1927,
                "test_stats": {
                    "discovered": 1927,
                    "executed": 1902,
                    "passed": 1902,
                    "failed": 0,
                    "skipped": 0,
                    "pass_rate": 100.0,
                },
            },
            "test_analysis": {
                "total_tests": 1902,
                "passed_tests": 1902,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "pass_rate": 100.0,
            },
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
    assert status["static_test_count"] == 1927
    assert status["execution_rate"] == pytest.approx(1902 / 1927 * 100, abs=0.01)
    assert "tests_not_fully_executed" not in snapshot["evidence_result"].get("conflicts", [])


def test_report_snapshot_java_trunk_static_count_unchanged():
    """java keeps the trunk static count even when test_status carries a
    stray discovered value (priority inversion is python-only)."""
    tool = ReportTool(
        context_manager=SimpleNamespace(
            load_trunk_context=lambda: SimpleNamespace(
                environment_summary={"static_test_count": 1122}
            )
        )
    )
    accomplishments = {
        "physical_validation": {
            "test_status": {"static_test_count": 999},
            "test_analysis": {
                "total_tests": 1122,
                "passed_tests": 1122,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "pass_rate": 100.0,
                "unique_tests": 1122,
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

    assert snapshot["status"]["static_test_count"] == 1122
