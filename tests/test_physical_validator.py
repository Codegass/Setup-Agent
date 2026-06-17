"""Tests for Phase 2: Gradle build validation + single test-verdict policy.

Covers:
- TASK 2.1: Gradle build validation requires REAL compiled outputs
  (build/classes/**/*.class, build/libs/*.jar), excludes the wrapper jar, and
  treats the bare .gradle cache dir as a non-deciding hint.
- TASK 2.2: One documented test-verdict policy (evaluate_run_verdict) shared by
  the report verdict and the run/test success path, plus failing_test_names
  enumeration.
"""

import fnmatch
import json
import os
import re

import pytest

from sag.agent.physical_validator import (
    PhysicalValidator,
    _format_build_duration,
    evaluate_run_verdict,
)
from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD, Config
from sag.tools.report_tool import ReportTool


# ---------------------------------------------------------------------------
# Fake orchestrators
# ---------------------------------------------------------------------------
class FakeBuildOrchestrator:
    """Simulates a container filesystem for the build-validation shell commands.

    Supports ``test -d/-f/-e <path>`` and a small subset of ``find`` with
    ``-name``, ``-path``, ``-not -path``, ``-type f/d``, ``-mindepth`` and
    ``-maxdepth`` predicates, piped to ``wc -l`` (count) or ``head -1`` (first),
    or returning newline-joined paths. ``*`` matches across ``/`` (find/-path
    semantics), matching Python's fnmatch.
    """

    def __init__(self, files=(), dirs=()):
        self.files = set(files)
        self.dirs = set(dirs)
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        for op, pool in (("test -d ", self.dirs), ("test -f ", self.files)):
            if c.startswith(op):
                path = c[len(op) :].split()[0]
                return {"exit_code": 0 if path in pool else 1, "output": ""}
        if c.startswith("test -e "):
            path = c[len("test -e ") :].split()[0]
            exists = path in self.files or path in self.dirs
            return {"exit_code": 0 if exists else 1, "output": ""}
        if c.startswith("find "):
            matches = self._run_find(c)
            if "| wc -l" in c:
                return {"exit_code": 0, "output": str(len(matches))}
            if "| head -1" in c:
                return {"exit_code": 0, "output": matches[0] if matches else ""}
            return {"exit_code": 0, "output": "\n".join(matches)}
        return {"exit_code": 1, "output": ""}

    def _run_find(self, command):
        cmd = command.split("|")[0]
        root = cmd.split()[1]
        search_dirs = "-type d" in cmd
        name_pats = re.findall(r"-name '([^']+)'", cmd)
        not_path_pats = re.findall(r"-not -path '([^']+)'", cmd)
        cmd_wo_not = re.sub(r"-not -path '[^']+'", "", cmd)
        path_pats = re.findall(r"-path '([^']+)'", cmd_wo_not)
        mindepth = self._int_opt(cmd, "-mindepth")
        maxdepth = self._int_opt(cmd, "-maxdepth")
        root_norm = root.rstrip("/")
        pool = self.dirs if search_dirs else self.files
        results = []
        for entry in sorted(pool):
            if entry != root_norm and not entry.startswith(root_norm + "/"):
                continue
            rel = entry[len(root_norm) :].lstrip("/")
            depth = len(rel.split("/")) if rel else 0
            if mindepth is not None and depth < mindepth:
                continue
            if maxdepth is not None and depth > maxdepth:
                continue
            if name_pats and not any(
                fnmatch.fnmatch(os.path.basename(entry), p) for p in name_pats
            ):
                continue
            if path_pats and not any(fnmatch.fnmatch(entry, p) for p in path_pats):
                continue
            if any(fnmatch.fnmatch(entry, p) for p in not_path_pats):
                continue
            results.append(entry)
        return results

    @staticmethod
    def _int_opt(cmd, opt):
        m = re.search(rf"{opt}\s+(\d+)", cmd)
        return int(m.group(1)) if m else None


class FakeReportOrchestrator:
    """Substring-driven orchestrator for parse_test_reports discovery + cat."""

    def __init__(self, report_dir, xml_files):
        self.report_dir = report_dir
        self.xml_files = dict(xml_files)
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        if "-type d" in c and "surefire-reports" in c:
            return {"exit_code": 0, "output": self.report_dir}
        if "src/test/groovy" in c:
            return {"exit_code": 0, "output": ""}
        if "-name '*.xml'" in c and self.report_dir in c:
            return {"exit_code": 0, "output": "\n".join(self.xml_files.keys())}
        if c.startswith("cat "):
            m = re.search(r"cat '([^']+)'", c)
            if m and m.group(1) in self.xml_files:
                return {"exit_code": 0, "output": self.xml_files[m.group(1)]}
            return {"exit_code": 1, "output": ""}
        return {"exit_code": 1, "output": ""}


class FakeCompactReportOrchestrator(FakeReportOrchestrator):
    """Simulates truncated shell output plus a compact in-container parser."""

    def __init__(self, report_dir, xml_files, compact_result):
        super().__init__(report_dir, xml_files)
        self.compact_result = compact_result

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        if "SAG_COMPACT_TEST_REPORT_PARSER" in c:
            return {"exit_code": 0, "output": json.dumps(self.compact_result)}
        if "-name '*.xml'" in c and self.report_dir in c:
            # Old path would lose files when orchestrator truncates large find output.
            return {"exit_code": 0, "output": "\n".join(list(self.xml_files.keys())[:1])}
        if c.startswith("cat "):
            # Old path would parse a truncated XML body and undercount testcases.
            m = re.search(r"cat '([^']+)'", c)
            if m and m.group(1) in self.xml_files:
                return {"exit_code": 0, "output": self.xml_files[m.group(1)][:120]}
            return {"exit_code": 1, "output": ""}
        return super().execute_command(command)


WRAPPER_JAR = "/workspace/demo/gradle/wrapper/gradle-wrapper.jar"
APP_CLASS = "/workspace/demo/build/classes/java/main/com/example/App.class"
APP_JAR = "/workspace/demo/build/libs/app.jar"
BUILD_GRADLE = "/workspace/demo/build.gradle"


# ===========================================================================
# TASK 2.1 - Gradle build validation requires real compiled outputs
# ===========================================================================
def test_gradle_cache_only_dir_is_not_valid():
    """A bare .gradle/ dir (no compiled outputs) must NOT validate."""
    orch = FakeBuildOrchestrator(files={BUILD_GRADLE}, dirs={"/workspace/demo/.gradle"})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    cache = validator._validate_gradle_cache("/workspace/demo")

    assert cache["valid"] is False
    # The .gradle dir is recorded only as a non-deciding hint.
    assert cache["details"].get("gradle_cache_dir") is True
    assert "class_count" not in cache["details"]
    assert "jar_count" not in cache["details"]


def test_gradle_cache_with_real_outputs_is_valid():
    """Compiled classes + a real build/libs jar must validate."""
    orch = FakeBuildOrchestrator(
        files={BUILD_GRADLE, APP_CLASS, APP_JAR, WRAPPER_JAR},
        dirs={"/workspace/demo/.gradle"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    cache = validator._validate_gradle_cache("/workspace/demo")

    assert cache["valid"] is True
    assert cache["details"].get("class_count") == 1
    assert cache["details"].get("jar_count") == 1


def test_gradle_cache_wrapper_jar_only_is_not_valid():
    """Only the gradle-wrapper.jar present -> not a build output -> not valid."""
    orch = FakeBuildOrchestrator(files={BUILD_GRADLE, WRAPPER_JAR})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    cache = validator._validate_gradle_cache("/workspace/demo")

    assert cache["valid"] is False


def test_validate_build_status_gradle_cache_only_fails():
    """End-to-end: gradle workspace with only .gradle/ -> build NOT valid."""
    orch = FakeBuildOrchestrator(files={BUILD_GRADLE}, dirs={"/workspace/demo/.gradle"})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_build_status("demo")

    assert result["evidence"]["build_system"] == "gradle"
    assert result["success"] is False


def test_validate_build_status_gradle_real_outputs_succeeds():
    """End-to-end: build/classes/**/*.class + build/libs/app.jar -> valid.

    Mirrors the eval's beam project (real .class + JARs) which MUST still pass.
    """
    orch = FakeBuildOrchestrator(
        files={BUILD_GRADLE, APP_CLASS, APP_JAR, WRAPPER_JAR},
        dirs={"/workspace/demo/.gradle"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_build_status("demo")

    assert result["evidence"]["build_system"] == "gradle"
    assert result["success"] is True


def test_validate_build_status_gradle_wrapper_jar_only_fails():
    """End-to-end: only the wrapper jar present -> build NOT valid."""
    orch = FakeBuildOrchestrator(files={BUILD_GRADLE, WRAPPER_JAR})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_build_status("demo")

    assert result["evidence"]["build_system"] == "gradle"
    assert result["success"] is False


def test_build_artifacts_complete_excludes_wrapper_jar():
    """The artifact count must not credit the gradle wrapper jar."""
    orch = FakeBuildOrchestrator(files={BUILD_GRADLE, WRAPPER_JAR})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    artifacts = validator._check_build_artifacts_complete("/workspace/demo")

    assert artifacts["jar_count"] == 0
    assert artifacts["exist"] is False


def test_validate_build_status_maven_unaffected():
    """Regression: Maven build validation (commons-cli-style) still succeeds."""
    orch = FakeBuildOrchestrator(
        files={"/workspace/mvn/pom.xml", "/workspace/mvn/target/foo-1.0.jar"},
        dirs={"/workspace/mvn/target/classes"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.validate_build_status("mvn")

    assert result["evidence"]["build_system"] == "maven"
    assert result["success"] is True


# ===========================================================================
# Build evidence surfaced for the metrics read model (report_metrics contract)
# ===========================================================================
def test_validate_build_status_evidence_populates_metrics_fields_gradle():
    """validate_build_status must surface tool/module_output_count/artifact_samples/
    warnings so report_metrics.assemble does not read keys the producer never sets.

    Gradle multi-project: two subprojects with real compiled outputs.
    """
    orch = FakeBuildOrchestrator(
        files={
            BUILD_GRADLE,
            "/workspace/demo/settings.gradle",
            APP_CLASS,
            APP_JAR,
            WRAPPER_JAR,
            "/workspace/demo/core/build.gradle",
            "/workspace/demo/core/build/classes/java/main/com/example/Core.class",
            "/workspace/demo/web/build.gradle",
            "/workspace/demo/web/build/classes/java/main/com/example/Web.class",
            "/workspace/demo/web/build/libs/web.jar",
        },
        dirs={"/workspace/demo/.gradle"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    evidence = validator.validate_build_status("demo")["evidence"]

    # tool mirrors the detected build system (no longer name-drifts to a never-set key)
    assert evidence["tool"] == "gradle"
    # module_output_count reflects subprojects that produced output
    assert evidence["module_output_count"] == 2
    # artifact_samples lists real build outputs (never the wrapper jar)
    assert evidence["artifact_samples"], "expected at least one artifact sample"
    assert all("gradle/wrapper" not in sample for sample in evidence["artifact_samples"])
    assert any(sample.endswith(".jar") or sample.endswith(".class")
               for sample in evidence["artifact_samples"])
    # warnings is an explicit list (honest empty when nothing was collected)
    assert evidence["warnings"] == []


def test_validate_build_status_evidence_populates_metrics_fields_maven():
    """Maven single-module: tool + artifact_samples surfaced; no module fan-out."""
    orch = FakeBuildOrchestrator(
        files={"/workspace/mvn/pom.xml", "/workspace/mvn/target/foo-1.0.jar"},
        dirs={"/workspace/mvn/target/classes"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    evidence = validator.validate_build_status("mvn")["evidence"]

    assert evidence["tool"] == "maven"
    assert "module_output_count" in evidence
    assert any(sample.endswith(".jar") for sample in evidence["artifact_samples"])
    assert evidence["warnings"] == []


# ===========================================================================
# Build time / command / artifact surfaced in evidence (Plan 3 - Task 3)
# ===========================================================================
class _StubCommandTracker:
    """Minimal CommandTracker stand-in returning a recorded last build command."""

    def __init__(self, last_build):
        self._last_build = last_build

    def get_last_build_command(self):
        return self._last_build


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (47.2, "47.2s"),
        (0.0, "0.0s"),
        (59.94, "59.9s"),
        (60, "1m 00s"),
        (192, "3m 12s"),
        (192.4, "3m 12s"),
        (3600, "60m 00s"),
    ],
)
def test_format_build_duration(seconds, expected):
    assert _format_build_duration(seconds) == expected


def test_validate_build_status_surfaces_build_time_command_artifact():
    """When a command_tracker with a timed build is attached, validate_build_status
    threads build_command + a formatted build_time into evidence, and exposes the
    primary artifact (first artifact sample)."""
    orch = FakeBuildOrchestrator(
        files={"/workspace/mvn/pom.xml", "/workspace/mvn/target/foo-1.0.jar"},
        dirs={"/workspace/mvn/target/classes"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    validator.command_tracker = _StubCommandTracker(
        {"command": "mvn -q clean package", "duration": 47.2}
    )

    evidence = validator.validate_build_status("mvn")["evidence"]

    assert evidence["build_command"] == "mvn -q clean package"
    assert evidence["build_time"] == "47.2s"
    # Primary artifact is the first collected sample.
    assert evidence["artifact"] == evidence["artifact_samples"][0]


def test_validate_build_status_artifact_without_tracker():
    """No command_tracker: build_time/build_command stay absent, but the primary
    artifact is still surfaced from artifact_samples (no regression)."""
    orch = FakeBuildOrchestrator(
        files={"/workspace/mvn/pom.xml", "/workspace/mvn/target/foo-1.0.jar"},
        dirs={"/workspace/mvn/target/classes"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    evidence = validator.validate_build_status("mvn")["evidence"]

    assert "build_time" not in evidence
    assert "build_command" not in evidence
    assert evidence["artifact"] == evidence["artifact_samples"][0]


def test_validate_build_status_command_without_duration():
    """A tracked build command with no duration surfaces the command but no time."""
    orch = FakeBuildOrchestrator(
        files={"/workspace/mvn/pom.xml", "/workspace/mvn/target/foo-1.0.jar"},
        dirs={"/workspace/mvn/target/classes"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    validator.command_tracker = _StubCommandTracker(
        {"command": "mvn install", "duration": None}
    )

    evidence = validator.validate_build_status("mvn")["evidence"]

    assert evidence["build_command"] == "mvn install"
    assert "build_time" not in evidence


# ===========================================================================
# TASK 2.2 - Single test-verdict policy (evaluate_run_verdict)
# ===========================================================================
def test_settings_test_pass_threshold_default():
    assert DEFAULT_TEST_PASS_THRESHOLD == 0.8
    assert Config().test_pass_threshold == 0.8


def test_settings_test_pass_threshold_from_env(monkeypatch):
    """SAG_TEST_PASS_THRESHOLD must override the default in Config.from_env."""
    monkeypatch.setenv("SAG_TEST_PASS_THRESHOLD", "0.95")
    assert Config.from_env().test_pass_threshold == 0.95


@pytest.mark.parametrize(
    "build_green,pass_rate,expected",
    [
        (False, 100.0, "failed"),  # build not green -> always failed
        (False, 0.0, "failed"),
        (True, 100.0, "success"),  # perfect pass
        (True, 96.2, "success"),  # commons-vfs: build green, >=80%
        (True, 80.0, "success"),  # boundary: >= threshold is success
        (True, 79.9, "failed"),  # just below threshold
        (True, 0.0, "failed"),
    ],
)
def test_evaluate_run_verdict_policy(build_green, pass_rate, expected):
    assert evaluate_run_verdict(build_green, pass_rate) == expected


def test_evaluate_run_verdict_custom_threshold():
    assert evaluate_run_verdict(True, 85.0, test_pass_threshold=0.9) == "failed"
    assert evaluate_run_verdict(True, 95.0, test_pass_threshold=0.9) == "success"


def _metrics(total, passed, failed=0, error=0, failing_names=None):
    return {
        "valid": True,
        "total_tests": total,
        "passed_tests": passed,
        "failed_tests": failed,
        "error_tests": error,
        "skipped_tests": 0,
        "failing_test_names": list(failing_names or []),
        "report_files": ["/workspace/demo/target/surefire-reports/TEST-Foo.xml"],
        "parsing_errors": [],
        "test_exclusions": [],
        "modules_without_tests": [],
    }


def test_validate_test_status_partial_pass_above_threshold(monkeypatch):
    """commons-vfs: 177/184 (96.2%) build-green -> PARTIAL (a pass), NOT FAILED.

    failing_test_names must be propagated for callers to enumerate failures.
    """
    failing = [f"com.example.VfsTest::case{i}" for i in range(7)]
    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: _metrics(184, 177, failed=5, error=2, failing_names=failing),
    )

    result = validator.validate_test_status("demo")

    assert result["status"] == "PARTIAL"
    assert result["evidence_status"] == "partial"
    assert result["pass_rate"] == pytest.approx(96.2, abs=0.05)
    assert result["failing_test_names"] == failing


def test_validate_test_status_below_threshold_fails(monkeypatch):
    """<80% build-green -> FAILED, with failing_test_names populated."""
    failing = [f"com.example.Bad::t{i}" for i in range(50)]
    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: _metrics(100, 50, failed=50, failing_names=failing),
    )

    result = validator.validate_test_status("demo")

    assert result["status"] == "FAILED"
    assert result["evidence_status"] == "blocked"
    assert result["failing_test_names"] == failing


def test_validate_test_status_all_pass_success(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: _metrics(184, 184),
    )

    result = validator.validate_test_status("demo")

    assert result["status"] == "SUCCESS"
    assert result["evidence_status"] == "success"
    assert result["failing_test_names"] == []


def test_validate_test_status_exposes_raw_unique_and_report_file_counts(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")
    metrics = _metrics(10, 9, failed=1)
    metrics.update(
        {
            "raw_total_tests": 18,
            "raw_passed_tests": 17,
            "raw_failed_tests": 1,
            "raw_error_tests": 0,
            "raw_skipped_tests": 0,
            "unique_tests": 10,
            "unique_passed_tests": 9,
            "unique_failed_tests": 1,
            "unique_error_tests": 0,
            "unique_skipped_tests": 0,
            "report_file_count": 3,
        }
    )
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: metrics,
    )

    result = validator.validate_test_status("demo")

    assert result["raw_total_tests"] == 18
    assert result["unique_tests"] == 10
    assert result["report_file_count"] == 3
    assert result["test_stats"]["executed"] == 10


# ---------------------------------------------------------------------------
# TASK 2.2(a) - failing_test_names enumerated from parsed records
# ---------------------------------------------------------------------------
def test_parse_test_reports_enumerates_failing_test_names():
    xml = (
        "<testsuites>"
        '<testsuite name="s1" tests="2" failures="1" errors="0">'
        '<testcase classname="com.example.FooTest" name="testA" time="0.01"/>'
        '<testcase classname="com.example.FooTest" name="testB" time="0.02">'
        '<failure message="boom">trace</failure></testcase>'
        "</testsuite>"
        '<testsuite name="s2" tests="2" failures="0" errors="1">'
        '<testcase classname="com.example.BarTest" name="testC" time="0.01"/>'
        '<testcase classname="com.example.BazTest" name="testD" time="0.03">'
        '<error message="ex">trace</error></testcase>'
        "</testsuite>"
        "</testsuites>"
    )
    report_dir = "/tmp/demo/target/surefire-reports"
    xml_path = f"{report_dir}/TEST-all.xml"
    orch = FakeReportOrchestrator(report_dir, {xml_path: xml})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    # project_dir outside /workspace skips static-catalog building.
    result = validator.parse_test_reports("/tmp/demo")

    assert result["valid"] is True
    assert result["total_tests"] == 4
    assert result["passed_tests"] == 2
    assert result["failed_tests"] == 1
    assert result["error_tests"] == 1
    assert result["failing_test_names"] == [
        "com.example.BazTest::testD",
        "com.example.FooTest::testB",
    ]


def test_parse_test_reports_no_failures_has_empty_failing_names():
    xml = (
        '<testsuite name="s" tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="com.example.OkTest" name="t1" time="0.01"/>'
        '<testcase classname="com.example.OkTest" name="t2" time="0.01"/>'
        "</testsuite>"
    )
    report_dir = "/tmp/ok/target/surefire-reports"
    xml_path = f"{report_dir}/TEST-ok.xml"
    orch = FakeReportOrchestrator(report_dir, {xml_path: xml})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.parse_test_reports("/tmp/ok")

    assert result["failed_tests"] == 0
    assert result["error_tests"] == 0
    assert result["failing_test_names"] == []


def test_parse_test_reports_uses_compact_parser_for_large_maven_xml():
    """Maven XML parsing must not depend on truncated cat output."""
    report_dir = "/workspace/demo/module-a/target/surefire-reports"
    xml_path = f"{report_dir}/TEST-com.example.BigTest.xml"
    xml = (
        '<testsuite name="big" tests="3" failures="0" errors="0" skipped="1">'
        '<testcase classname="com.example.BigTest" name="testA"/>'
        '<testcase classname="com.example.BigTest" name="testB"/>'
        '<testcase classname="com.example.BigTest" name="testC">'
        "<skipped/></testcase>"
        "</testsuite>"
    )
    compact = {
        "valid": True,
        "total_tests": 3,
        "passed_tests": 2,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 1,
        "raw_total_tests": 3,
        "raw_passed_tests": 2,
        "raw_failed_tests": 0,
        "raw_error_tests": 0,
        "raw_skipped_tests": 1,
        "unique_tests": 3,
        "unique_passed_tests": 2,
        "unique_failed_tests": 0,
        "unique_error_tests": 0,
        "unique_skipped_tests": 1,
        "test_success": True,
        "failing_test_names": [],
        "report_files": [xml_path],
        "report_dirs": [report_dir],
        "parsing_errors": [],
    }
    orch = FakeCompactReportOrchestrator(report_dir, {xml_path: xml}, compact)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.parse_test_reports("/workspace/demo")

    assert result["valid"] is True
    assert result["total_tests"] == 3
    assert result["passed_tests"] == 2
    assert result["skipped_tests"] == 1
    assert result["raw_total_tests"] == 3
    assert result["unique_tests"] == 3
    assert any("SAG_COMPACT_TEST_REPORT_PARSER" in c for c in orch.commands)


def test_parse_test_reports_uses_compact_parser_for_many_gradle_xml_files():
    """Gradle report discovery must not lose files when find output is truncated."""
    report_dir = "/workspace/demo/lib/build/test-results/test"
    xml_paths = [f"{report_dir}/TEST-com.example.Test{i}.xml" for i in range(3)]
    xml_files = {
        path: (
            f'<testsuite name="s{i}" tests="1" failures="{1 if i == 2 else 0}" '
            'errors="0" skipped="0">'
            f'<testcase classname="com.example.Test{i}" name="case{i}">'
            + ("<failure/>" if i == 2 else "")
            + "</testcase></testsuite>"
        )
        for i, path in enumerate(xml_paths)
    }
    compact = {
        "valid": True,
        "total_tests": 3,
        "passed_tests": 2,
        "failed_tests": 1,
        "error_tests": 0,
        "skipped_tests": 0,
        "raw_total_tests": 3,
        "raw_passed_tests": 2,
        "raw_failed_tests": 1,
        "raw_error_tests": 0,
        "raw_skipped_tests": 0,
        "unique_tests": 3,
        "unique_passed_tests": 2,
        "unique_failed_tests": 1,
        "unique_error_tests": 0,
        "unique_skipped_tests": 0,
        "test_success": False,
        "failing_test_names": ["com.example.Test2::case2"],
        "report_files": xml_paths,
        "report_dirs": [report_dir],
        "parsing_errors": [],
    }
    orch = FakeCompactReportOrchestrator(report_dir, xml_files, compact)
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    result = validator.parse_test_reports("/workspace/demo")

    assert result["total_tests"] == 3
    assert result["passed_tests"] == 2
    assert result["failed_tests"] == 1
    assert result["failing_test_names"] == ["com.example.Test2::case2"]


# ===========================================================================
# TASK 2.2(c) - report verdict reads the SAME single policy (no divergence)
# ===========================================================================
def _report_tool():
    # docker_orchestrator=None so the verdict path skips the physical re-check
    # branch; physical_validator supplies the pass-rate threshold + calculator.
    return ReportTool(
        docker_orchestrator=None,
        physical_validator=PhysicalValidator(project_path="/workspace"),
    )


def _accomplishments(total, passed, build_success=True):
    return {
        "repository_cloned": True,
        "build_success": build_success,
        "physical_validation": {"test_analysis": {"total_tests": total, "passed_tests": passed}},
    }


@pytest.mark.parametrize(
    "total,passed,expected",
    [
        (184, 177, "success"),  # commons-vfs 96.2% -> success (was failed pre-fix)
        (977, 916, "success"),  # commons-cli 93.8% -> remains success
        (184, 184, "success"),  # 100% -> success
        (100, 80, "success"),  # boundary >= 80%
        (100, 50, "fail"),  # < 80% -> fail
    ],
)
def test_report_determine_actual_status_uses_policy(total, passed, expected):
    tool = _report_tool()
    assert tool._determine_actual_status(_accomplishments(total, passed)) == expected


def test_report_determine_actual_status_build_failed_is_fail():
    tool = _report_tool()
    accomplishments = _accomplishments(184, 184, build_success=False)
    assert tool._determine_actual_status(accomplishments) == "fail"


def test_reconcile_status_partial_pass_is_success():
    """The fallback reconcile path must NOT collapse a >=80% partial to fail."""
    tool = _report_tool()
    status = tool._reconcile_status("fail", "partial", _accomplishments(184, 177))
    assert status == "success"


def test_reconcile_status_below_threshold_is_fail():
    tool = _report_tool()
    status = tool._reconcile_status("success", "success", _accomplishments(100, 50))
    assert status == "fail"


def test_report_and_validator_verdicts_agree_for_partial_pass(monkeypatch):
    """Single source of truth: report verdict and run/test verdict don't diverge."""
    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: _metrics(184, 177, failed=7),
    )
    test_status = validator.validate_test_status("demo")

    tool = ReportTool(docker_orchestrator=None, physical_validator=validator)
    report_verdict = tool._determine_actual_status(_accomplishments(184, 177))

    # Build green + 96.2% pass rate: report says success, validator says PARTIAL
    # (a pass) with a non-blocked evidence status -> consistent verdict.
    assert report_verdict == "success"
    assert test_status["status"] == "PARTIAL"
    assert test_status["evidence_status"] != "blocked"


def test_test_pass_threshold_feeds_both_report_and_run_verdict(monkeypatch):
    """A non-default test_pass_threshold must change BOTH the test-status verdict
    and the report verdict (they read the same physical_validator.test_pass_threshold).

    Proves the setting is wired end-to-end rather than always resolving to the
    hardcoded 0.8 default: the identical 85% build-green run is a PASS under 0.8
    but a FAIL under 0.9 on both gates.
    """
    metrics = lambda project_dir: _metrics(100, 85, failed=15)  # noqa: E731
    accomplishments = _accomplishments(100, 85)

    # Default threshold (0.8): 85% is a partial pass / success on both gates.
    default_validator = PhysicalValidator(project_path="/workspace")
    assert default_validator.test_pass_threshold == DEFAULT_TEST_PASS_THRESHOLD
    monkeypatch.setattr(default_validator, "parse_test_reports_with_catalog", metrics)
    assert default_validator.validate_test_status("demo")["status"] == "PARTIAL"
    default_report = ReportTool(docker_orchestrator=None, physical_validator=default_validator)
    assert default_report._determine_actual_status(accomplishments) == "success"

    # Stricter threshold (0.9): the same 85% now fails on both gates.
    strict_validator = PhysicalValidator(project_path="/workspace", test_pass_threshold=0.9)
    assert strict_validator.test_pass_threshold == 0.9
    monkeypatch.setattr(strict_validator, "parse_test_reports_with_catalog", metrics)
    assert strict_validator.validate_test_status("demo")["status"] == "FAILED"
    strict_report = ReportTool(docker_orchestrator=None, physical_validator=strict_validator)
    assert strict_report._determine_actual_status(accomplishments) == "fail"
