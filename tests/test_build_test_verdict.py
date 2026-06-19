"""Build/test verdict + module-reporting fixes (PR #9).

All of *our* net-new tests live here so they don't convolute the original suites
and are easy to review in one place. They cover:
  1. Tri-state build verdict + the JVM phantom-green gate
  2. Profile-gated / active-module set (modules disabled in the pom don't count)
  3. Reactor-authoritative module metrics + root-inclusive scan
  4. `--fail-at-end` backend wiring (compile/package, not just test)
  5. Maven Reactor Summary capture on build commands + ANSI-coloured parsing
  6. Verdict capping: incomplete modules AND low test-execution coverage -> PARTIAL
  7. Module count keeps submodules that actually built (no `0/N` despite artifacts)

Reusable fakes are imported from the original test modules (pytest's prepend import
mode puts the tests dir on sys.path); the small fakes our tests introduced live here.
"""

import json as _json
import re

from sag.agent.physical_validator import PhysicalValidator
from sag.config.settings import (
    DEFAULT_TEST_EXECUTION_THRESHOLD,
    Config,
)
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.module_metrics import assemble_module_metrics
from sag.tools.report_tool import ReportTool
from sag.verdict import run_verdict

# Reusable fakes/helpers from the original suites.
from test_physical_validator import FakeBuildOrchestrator, _coverage_validator
from test_build_tool import FakeBackendTool, _tool
from test_physical_validator_modules import FakeOrch
from test_agent_final_status import FakePhysicalValidator, _agent_with_validator


# ===========================================================================
# Local fakes introduced by our tests
# ===========================================================================
class FakeMavenPomOrchestrator:
    """Serves pom.xml content for `cat` and denies every other probe.

    Records commands so a test can assert WHICH module poms were visited.
    """

    def __init__(self, poms):
        self.poms = dict(poms)
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        if c.startswith("cat "):
            m = re.search(r"cat (\S+)", c)
            path = m.group(1) if m else ""
            if path in self.poms:
                return {"exit_code": 0, "output": self.poms[path]}
            return {"exit_code": 1, "output": ""}
        return {"exit_code": 1, "output": ""}


class _CapturingOrch:
    """Captures executed commands so we can inspect the recorded summary entry."""

    def __init__(self):
        self.cmds = []

    def execute_command(self, command, **kwargs):
        self.cmds.append(command)
        return {"success": True, "output": "", "exit_code": 0}

    def _last_entry(self):
        for c in reversed(self.cmds):
            if "test_summary.jsonl" in c and "<<'EOF'" in c:
                body = c.split("<<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
                return _json.loads(body)
        return None


def _all_present_validator(threshold=1.0):
    """A validator whose expected-artifact check reports every module present."""
    validator = _coverage_validator(1.0, found=["a", "b"], missing=[], threshold=threshold)
    validator._verify_expected_artifacts = lambda *a, **k: {
        "all_present": True,
        "found": ["a", "b"],
        "missing": [],
        "classes_expected": 4,
        "classes_found": 4,
        "class_coverage": 1.0,
    }
    return validator


# ===========================================================================
# 1. Tri-state build verdict + JVM phantom-green gate
# ===========================================================================
def test_validate_build_status_full_success_when_all_modules_built():
    """Every expected/active module produced output -> clean SUCCESS, no conflict."""
    result = _all_present_validator(threshold=1.0).validate_build_status("m")

    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["conflicts"] == []


def test_validate_build_status_full_success_at_or_above_loosened_threshold():
    """An env-loosened threshold lets a >= threshold build reach full SUCCESS."""
    validator = _coverage_validator(0.75, found=["a", "b", "c"], missing=["d"], threshold=0.75)

    result = validator.validate_build_status("m")

    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["conflicts"] == []
    assert "75%" in result["reason"]


def test_validate_build_status_partial_below_coverage_threshold():
    """Real build output below the threshold is PARTIAL (build happened) — capped
    at partial by build_modules_incomplete, never a clean success, never a hard fail."""
    validator = _coverage_validator(0.5, found=["a"], missing=["b", "c", "d"], threshold=0.75)

    result = validator.validate_build_status("m")

    assert result["success"] is True  # build is real -> phase happened
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]
    assert "50%" in result["reason"]
    assert "incomplete" in result["reason"].lower()


def test_validate_build_status_strict_default_partial_when_not_all_modules():
    """Default strict threshold (1.0): a near-complete build (0.99) is PARTIAL,
    never a full success — every active module must compile for SUCCESS."""
    validator = _coverage_validator(0.99, found=["a", "b", "c"], missing=["d"], threshold=1.0)

    result = validator.validate_build_status("m")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_modules_incomplete" in result["conflicts"]


def test_validate_build_status_blocked_when_no_real_output():
    """Zero coverage and no compiled evidence -> BLOCKED (not a real build)."""
    validator = _coverage_validator(0.0, found=[], missing=["a", "b"], threshold=0.75)

    result = validator.validate_build_status("m")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "build_validation_failed" in result["conflicts"]


def test_validate_build_status_zero_classes_no_artifacts_blocked_despite_trivial_coverage():
    """commons-chain regression: 0 compiled classes + no artifacts must be BLOCKED
    even when no class-based expectation exists (class_coverage defaults to 1.0) or
    an empty target/classes fingerprint is present. The build verdict must agree
    with the module scan (0 built), never report a phantom 'Built 100%'."""
    orch = FakeBuildOrchestrator(files={"/workspace/cc/pom.xml"})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    validator._get_expected_artifacts = lambda *a, **k: [
        {"type": "jar", "path": "/workspace/cc/target/cc.jar", "artifact": "cc.jar"}
    ]
    validator._verify_expected_artifacts = lambda *a, **k: {
        "all_present": False, "found": [], "missing": ["cc.jar"],
        "classes_expected": 0, "classes_found": 0, "class_coverage": 1.0,
    }

    result = validator.validate_build_status("cc")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "compiled" in result["reason"].lower()


# ===========================================================================
# 2. Active-module set: profile-gated modules are NOT counted
# ===========================================================================
def test_parse_maven_expected_artifacts_excludes_profile_gated_modules():
    """A module declared only inside a <profiles> block is disabled in the build
    config and must NOT be counted as an active/expected module."""
    root_pom = """
    <project>
      <artifactId>root</artifactId>
      <version>1.0</version>
      <packaging>pom</packaging>
      <modules><module>active-mod</module></modules>
      <profiles><profile><id>extras</id>
        <modules><module>profiled-mod</module></modules>
      </profile></profiles>
    </project>
    """
    leaf_pom = "<project><artifactId>active-mod</artifactId><version>1.0</version></project>"
    orch = FakeMavenPomOrchestrator(
        {
            "/workspace/proj/pom.xml": root_pom,
            "/workspace/proj/active-mod/pom.xml": leaf_pom,
        }
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    validator._parse_maven_expected_artifacts("/workspace/proj")

    cats = [c for c in orch.commands if c.startswith("cat ")]
    assert any("active-mod/pom.xml" in c for c in cats), "active module must be visited"
    assert not any("profiled-mod" in c for c in cats), "profile-gated module must be skipped"


def test_active_maven_module_dirs_excludes_profile_gated():
    """The detected-module fallback set = root + active <modules> only; profile-
    gated modules (disabled in the pom) are not part of the build and excluded."""
    root_pom = """
    <project>
      <modules><module>core</module></modules>
      <profiles><profile><id>x</id>
        <modules><module>profiled</module></modules>
      </profile></profiles>
    </project>
    """
    core_pom = "<project><artifactId>core</artifactId></project>"
    orch = FakeMavenPomOrchestrator(
        {"/w/p/pom.xml": root_pom, "/w/p/core/pom.xml": core_pom}
    )
    v = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    dirs = v._active_maven_module_dirs("/w/p")

    assert "/w/p" in dirs and "/w/p/core" in dirs
    assert not any("profiled" in d for d in dirs)


# ===========================================================================
# 3. Reactor-authoritative module metrics + root-inclusive scan
# ===========================================================================
def test_reactor_authoritative_excludes_non_reactor_scanned_modules():
    """The reactor built api+runtime; a scanned dir not in the reactor (a standalone
    example pom) is NOT part of the build and must not be counted as detected."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": "api", "name": "api", "class_count": 10, "jar_count": 1, "report_dirs": []},
            {"path": "runtime", "name": "runtime", "class_count": 0, "jar_count": 0, "report_dirs": []},
            {"path": "examples", "name": "examples", "class_count": 0, "jar_count": 0, "report_dirs": []},
        ],
        reactor_status={"api": "success", "runtime": "failure"},
        tests={}, build_systems=["maven"], build_error_samples={}, generated_at="t",
    )
    s = metrics["module_summary"]
    assert s["modules_total"] == 2  # detected = reactor modules; examples excluded
    assert s["modules_built"] == 1 and s["modules_failed"] == 1
    assert "examples" not in {m["path"] for m in metrics["modules"]}


def test_reactor_only_module_counted_when_no_scan_match():
    """A reactor module no disk scan found is still counted (detected == reactor count)."""
    metrics = assemble_module_metrics(
        modules=[{"path": "api", "name": "api", "class_count": 5, "jar_count": 0, "report_dirs": []}],
        reactor_status={"api": "success", "ghost": "success"},
        tests={}, build_systems=["maven"], build_error_samples={}, generated_at="t",
    )
    s = metrics["module_summary"]
    assert s["modules_total"] == 2 and s["modules_built"] == 2
    assert "ghost" in {m["name"] for m in metrics["modules"]}


def test_scan_modules_includes_root_in_multi_module():
    """The submodule find runs at mindepth 2, so the depth-1 root pom is excluded;
    the root module that actually compiled must still be scanned (path ".")."""
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/apps/example1/pom.xml\n/w/p/apps/example2/pom.xml"},
        "/w/p/target/classes": {"output": "33"},  # root compiled 33 classes
        "/apps/example1/target/classes": {"output": "0"},
        "/apps/example2/target/classes": {"output": "0"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    by_path = {m["path"]: m for m in v.scan_modules("/w/p", "maven")}
    assert "." in by_path, "root module must be scanned, not invisible"
    assert by_path["."]["class_count"] == 33
    assert "apps/example1" in by_path and "apps/example2" in by_path


# ===========================================================================
# 4. --fail-at-end backend wiring (compile/package, not just test)
# ===========================================================================
def test_compile_and_package_pass_fail_at_end_for_whole_reactor():
    for action in ("compile", "package"):
        maven, gradle = FakeBackendTool(), FakeBackendTool()
        _tool({"pom.xml"}, maven=maven).execute(action=action, working_directory="/w")
        _tool({"build.gradle"}, gradle=gradle).execute(action=action, working_directory="/w")
        assert maven.calls[0].get("fail_at_end") is True, action
        assert gradle.calls[0].get("fail_at_end") is True, action


def test_deps_does_not_pass_fail_at_end():
    maven = FakeBackendTool()
    _tool({"pom.xml"}, maven=maven).execute(action="deps", working_directory="/w")
    assert "fail_at_end" not in maven.calls[0]


# ===========================================================================
# 5. Reactor-summary capture on build commands + ANSI parsing
# ===========================================================================
def test_record_summary_tags_build_vs_test_and_keeps_reactor_summary():
    """The reactor summary is recorded for BUILD commands too, tagged
    'build_summary' so its empty test counts aren't mistaken for a test run."""
    analysis = {
        "tests_run": {},
        "failed_modules": [],
        "skipped_modules": [],
        "reactor_summary": [
            {"module": "brooklyn-server", "status": "SUCCESS"},
            {"module": "brooklyn-ui", "status": "FAILURE"},
        ],
    }

    build_orch = _CapturingOrch()
    MavenTool(build_orch)._record_test_summary("/workspace/p", analysis, 0, "clean install")
    build_entry = build_orch._last_entry()
    assert build_entry is not None
    assert build_entry["event"] == "build_summary"
    assert len(build_entry["reactor_summary"]) == 2

    test_orch = _CapturingOrch()
    MavenTool(test_orch)._record_test_summary("/workspace/p", analysis, 0, "test")
    assert test_orch._last_entry()["event"] == "test_session_end"


def test_analyze_maven_output_parses_ansi_colored_reactor_summary():
    """Maven emits ANSI-coloured output; the Reactor Summary parser must still
    capture per-module SUCCESS/FAILURE/SKIPPED (regression: coloured [INFO]
    silently yielded zero reactor modules)."""
    e = "\x1b"
    out = "\n".join(
        [
            f"[{e}[1;34mINFO{e}[m] {e}[1mReactor Summary:{e}[m",
            f"[{e}[1;34mINFO{e}[m] Apache Brooklyn Server ......... {e}[1;32mSUCCESS{e}[m [ 12.3 s]",
            f"[{e}[1;34mINFO{e}[m] Apache Brooklyn UI ............. {e}[1;31mFAILURE{e}[m [  1.1 s]",
            f"[{e}[1;34mINFO{e}[m] Apache Brooklyn Karaf ......... {e}[1;33mSKIPPED{e}[m",
            f"[{e}[1;31mERROR{e}[m] BUILD FAILURE",
        ]
    )

    analysis = MavenTool(_CapturingOrch())._analyze_maven_output(out, 1)

    rs = {r["module"]: r["status"] for r in analysis["reactor_summary"]}
    assert rs == {
        "Apache Brooklyn Server": "SUCCESS",
        "Apache Brooklyn UI": "FAILURE",
        "Apache Brooklyn Karaf": "SKIPPED",
    }
    assert analysis["has_build_failure_marker"] is True


# ===========================================================================
# 6. Verdict capping: incomplete modules AND low test-execution coverage
# ===========================================================================
def test_run_verdict_incomplete_modules_cap_at_partial():
    """build_modules_incomplete is genuine (not threshold-adjudicated) and must cap
    an otherwise-clean run at PARTIAL, never SUCCESS."""
    assert run_verdict("success", "success", ["build_modules_incomplete"]) == "partial"


def test_run_verdict_tests_not_fully_executed_caps_at_partial():
    """A detected test suite that barely executed caps the run at PARTIAL."""
    assert run_verdict("success", "success", ["tests_not_fully_executed"]) == "partial"


def test_settings_test_execution_threshold_default_and_env(monkeypatch):
    assert DEFAULT_TEST_EXECUTION_THRESHOLD == 0.8
    assert Config().test_execution_threshold == 0.8
    monkeypatch.setenv("SAG_TEST_EXECUTION_THRESHOLD", "0.5")
    assert Config.from_env().test_execution_threshold == 0.5


def test_agent_caps_at_partial_when_modules_incomplete():
    """CLI parity: build real but incomplete modules -> PARTIAL even if tests pass."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={
                "success": True,
                "build_complete": False,
                "reason": "Built 60% of expected classes; 2 module(s) incomplete",
                "conflicts": ["build_modules_incomplete"],
            },
            test_status={
                "has_test_reports": True, "status": "SUCCESS", "reason": "All tests passed",
                "pass_rate": 100.0, "total_tests": 50, "passed_tests": 50,
                "failed_tests": 0, "error_tests": 0, "skipped_tests": 0,
                "test_exclusions": [], "modules_without_tests": [],
            },
            analysis_status={"analyzed": True, "has_static_test_count": True, "static_test_count": 50},
        )
    )
    assert agent._get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "partial"


def test_agent_caps_at_partial_on_low_test_execution():
    """CLI parity: a detected suite that barely ran (1 of 1122) -> PARTIAL even
    though the one test that ran passed (mirrors tests_not_fully_executed)."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "build_complete": True, "reason": "Built 100%"},
            test_status={
                "has_test_reports": True, "status": "SUCCESS", "reason": "1/1 passed",
                "pass_rate": 100.0, "total_tests": 1, "passed_tests": 1,
                "failed_tests": 0, "error_tests": 0, "skipped_tests": 0,
                "test_exclusions": [], "modules_without_tests": [],
            },
            analysis_status={"analyzed": True, "has_static_test_count": True, "static_test_count": 1122},
        )
    )
    assert agent._get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "partial"


# ===========================================================================
# 7. Module count keeps submodules that actually built (no 0/N despite artifacts)
# ===========================================================================
class _ModuleScanValidator:
    """Minimal validator stub for ReportTool._compute_module_metrics: a no-reactor
    project whose root declares no active modules but whose submodules compiled."""

    def __init__(self, project_dir, scanned, active_dirs):
        self._project_dir = project_dir
        self._scanned = scanned
        self._active_dirs = active_dirs

    def _detect_build_system(self, project_dir):
        return "maven"

    def scan_modules(self, project_dir, build_system):
        return self._scanned

    def _active_maven_module_dirs(self, project_dir):
        return self._active_dirs

    def parse_module_test_reports(self, module_dir, report_dirs):
        return {}


def test_module_metrics_keeps_built_submodules_without_reactor():
    """No reactor summary + root declares no modules: a submodule that produced
    artifacts must still be counted as built (not collapsed to 0/1), while an
    artifact-less, undeclared module stays excluded (commons-chain shape)."""
    project_dir = "/workspace/p"
    scanned = [
        {"path": ".", "name": ".", "class_count": 0, "jar_count": 0, "report_dirs": []},
        {"path": "tools/cli", "name": "tools:cli", "class_count": 27, "jar_count": 0, "report_dirs": []},
        {"path": "examples", "name": "examples", "class_count": 0, "jar_count": 0, "report_dirs": []},
    ]
    tool = ReportTool()
    tool.physical_validator = _ModuleScanValidator(project_dir, scanned, active_dirs=[project_dir])
    tool._get_project_info = lambda: {"directory": project_dir}

    metrics = tool._compute_module_metrics({}, generated_at="t")

    paths = {m["path"] for m in metrics["modules"]}
    assert "tools/cli" in paths  # built submodule kept despite no reactor / not declared
    assert "examples" not in paths  # artifact-less + undeclared stays excluded
    assert metrics["module_summary"]["modules_built"] >= 1
