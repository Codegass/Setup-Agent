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
import shlex

import pytest

from test_agent_final_status import FakePhysicalValidator, _agent_with_validator
from test_build_tool import FakeBackendTool, _tool

# Reusable fakes/helpers from the original suites.
from test_physical_validator import FakeBuildOrchestrator, _coverage_validator
from test_physical_validator_modules import FakeOrch

import sag.agent.physical_validator as physical_validator_module
from sag.agent.physical_validator import PhysicalValidator
from sag.config.settings import (
    DEFAULT_TEST_EXECUTION_THRESHOLD,
    Config,
)
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.command_tracker import CommandTracker
from sag.tools.module_metrics import assemble_module_metrics
from sag.tools.report_tool import ReportTool
from sag.verdict import run_verdict


# ===========================================================================
# Local fakes introduced by our tests
# ===========================================================================
class FakeMavenPomOrchestrator:
    """Serves pom.xml content for `cat` and denies every other probe.

    Records commands so a test can assert WHICH module poms were visited.
    """

    def __init__(self, poms, realpaths=None, files=None, dirs=None):
        self.poms = dict(poms)
        self.files = {**self.poms, **dict(files or {})}
        self.dirs = set(dirs or ())
        self.realpaths = dict(realpaths or {})
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()
        if c.startswith("realpath -e -- "):
            path = shlex.split(c)[-1]
            resolved = self.realpaths.get(path, path)
            if resolved is None:
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": resolved}
        if c.startswith("test -d "):
            path = shlex.split(c)[2]
            return {"exit_code": 0 if path in self.dirs else 1, "output": ""}
        if c.startswith("test -f "):
            path = shlex.split(c)[2]
            return {"exit_code": 0 if path in self.files else 1, "output": ""}
        if c.startswith("cat "):
            path = shlex.split(c)[1] if len(shlex.split(c)) > 1 else ""
            if path in self.files:
                return {"exit_code": 0, "output": self.files[path]}
            return {"exit_code": 1, "output": ""}
        return {"exit_code": 1, "output": ""}


class FakeReceiptTracker:
    def __init__(self, build=(), test=()):
        self.build = list(build)
        self.test = list(test)

    def get_all_build_commands(self):
        return self.build

    def get_all_test_commands(self):
        return self.test

    def get_last_build_command(self):
        return self.build[-1] if self.build else None

    def get_last_test_command(self):
        return self.test[-1] if self.test else None


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
        "all_present": False,
        "found": [],
        "missing": ["cc.jar"],
        "classes_expected": 0,
        "classes_found": 0,
        "class_coverage": 1.0,
    }

    result = validator.validate_build_status("cc")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "compiled" in result["reason"].lower()


# ===========================================================================
# 2. Active-module set: profile-gated modules are NOT counted
# ===========================================================================
def _receipt(
    command,
    *,
    working_dir="/w/p",
    tool="maven",
    timestamp="2026-07-23T12:00:00",
):
    return {
        "command": command,
        "working_dir": working_dir,
        "tool": tool,
        "timestamp": timestamp,
        "duration": 1.0,
    }


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

    assert validator._bounded_maven_reactor("/workspace/proj").complete is True
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
    orch = FakeMavenPomOrchestrator({"/w/p/pom.xml": root_pom, "/w/p/core/pom.xml": core_pom})
    v = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    dirs = v._active_maven_module_dirs("/w/p")

    assert v._bounded_maven_reactor("/w/p").complete is True
    assert "/w/p" in dirs and "/w/p/core" in dirs
    assert not any("profiled" in d for d in dirs)


def test_active_by_default_profile_modules_enter_reactor_and_artifacts():
    root_pom = """
    <project><packaging>pom</packaging><profiles>
      <profile><id>default-modules</id>
        <activation><activeByDefault>true</activeByDefault></activation>
        <modules><module>defaulted</module></modules>
      </profile>
      <profile><id>manual-only</id>
        <modules><module>manual</module></modules>
      </profile>
    </profiles></project>
    """
    leaf_pom = "<project><artifactId>defaulted</artifactId><version>1</version></project>"
    orch = FakeMavenPomOrchestrator(
        {
            "/w/p/pom.xml": root_pom,
            "/w/p/defaulted/pom.xml": leaf_pom,
            "/w/p/manual/pom.xml": (
                "<project><artifactId>manual</artifactId><version>1</version></project>"
            ),
        }
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    snapshot = validator._bounded_maven_reactor("/w/p")
    artifacts = validator._parse_maven_expected_artifacts(
        "/w/p",
        snapshot=snapshot,
    )

    assert snapshot.complete is True
    assert [record.module_dir for record in snapshot.records] == [
        "/w/p",
        "/w/p/defaulted",
    ]
    assert [item["path"] for item in artifacts] == ["/w/p/defaulted/target/defaulted-1.jar"]
    assert not any(
        command.startswith("cat ") and "/manual/pom.xml" in command for command in orch.commands
    )


@pytest.mark.parametrize(
    ("command", "enabled", "disabled"),
    [
        ("mvn -Pfoo,bar,!old,-legacy package", {"foo", "bar"}, {"old", "legacy"}),
        ("mvn -P foo,!old package", {"foo"}, {"old"}),
        ("mvn --activate-profiles foo,bar package", {"foo", "bar"}, set()),
        ("mvn --activate-profiles=foo,!old package", {"foo"}, {"old"}),
    ],
)
def test_recorded_maven_profile_syntax(command, enabled, disabled):
    validator = PhysicalValidator(project_path="/w")
    validator.command_tracker = FakeReceiptTracker(build=[_receipt(command)])

    selection = validator._recorded_maven_profile_selection("/w/p")

    assert selection.conflicts == ()
    assert selection.enabled == frozenset(enabled)
    assert selection.disabled == frozenset(disabled)


def test_profile_receipt_selection_ignores_later_non_maven_and_other_reactor():
    validator = PhysicalValidator(project_path="/w")
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt("mvn -Pfoo package", timestamp="2026-07-23T12:00:00"),
            _receipt(
                "mvn -Pother package",
                working_dir="/w/other",
                timestamp="2026-07-23T12:01:00",
            ),
            _receipt(
                "gradle build",
                tool="gradle",
                timestamp="2026-07-23T12:02:00",
            ),
        ]
    )

    selection = validator._recorded_maven_profile_selection("/w/p")

    assert selection.enabled == frozenset({"foo"})
    assert selection.conflicts == ()


def test_newer_relevant_maven_test_receipt_supersedes_build_receipt():
    validator = PhysicalValidator(project_path="/w")
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt("mvn package", timestamp="2026-07-23T12:00:00"),
        ],
        test=[
            _receipt("mvn -Pfoo verify", timestamp="2026-07-23T12:01:00"),
        ],
    )

    selection = validator._recorded_maven_profile_selection("/w/p")

    assert selection.enabled == frozenset({"foo"})
    assert selection.conflicts == ()


def test_profile_receipt_file_selector_binds_only_exact_reactor():
    validator = PhysicalValidator(project_path="/w")
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn -f /w/p/pom.xml -Pfoo package",
                working_dir="/w",
                timestamp="2026-07-23T12:00:00",
            ),
            _receipt(
                "mvn --file ../other/pom.xml -Pother package",
                working_dir="/w/p",
                timestamp="2026-07-23T12:01:00",
            ),
        ]
    )

    selection = validator._recorded_maven_profile_selection("/w/p")

    assert selection.enabled == frozenset({"foo"})
    assert selection.working_dir == "/w/p"
    assert selection.conflicts == ()


@pytest.mark.parametrize(
    ("command", "expected_launcher_basedir"),
    [
        ("mvn -f /w/p/pom.xml -Pfoo package", "/w/p"),
        ("mvn --file /w/p/pom.xml -Pfoo package", "/w/p"),
        ("mvn -f/w/p/pom.xml -Pfoo package", "/w"),
        ("mvn --file=/w/p/pom.xml -Pfoo package", "/w"),
    ],
)
def test_profile_receipt_file_forms_keep_exact_launcher_basedir_semantics(
    command,
    expected_launcher_basedir,
):
    validator = PhysicalValidator(project_path="/w")
    validator.command_tracker = FakeReceiptTracker(build=[_receipt(command, working_dir="/w")])

    selection = validator._recorded_maven_profile_selection("/w/p")

    assert selection.enabled == frozenset({"foo"})
    assert selection.working_dir == expected_launcher_basedir
    assert selection.conflicts == ()


def test_maven_reactor_snapshot_excludes_lexical_and_symlink_escapes():
    root_pom = """
    <project>
      <artifactId>root</artifactId><version>1</version><packaging>pom</packaging>
      <modules>
        <module>safe</module>
        <module>../outside</module>
        <module>linked-outside</module>
      </modules>
    </project>
    """
    safe_pom = """
    <project>
      <artifactId>safe</artifactId><version>1</version><packaging>pom</packaging>
      <modules><module>nested</module></modules>
    </project>
    """
    nested_pom = "<project><artifactId>nested</artifactId><version>1</version></project>"
    orch = FakeMavenPomOrchestrator(
        {
            "/w/p/pom.xml": root_pom,
            "/w/p/safe/pom.xml": safe_pom,
            "/w/p/safe/nested/pom.xml": nested_pom,
            # These must remain unread even though the fake can serve them.
            "/w/outside/pom.xml": "<project><artifactId>outside</artifactId></project>",
            "/w/p/linked-outside/pom.xml": ("<project><artifactId>linked</artifactId></project>"),
        },
        realpaths={"/w/p/linked-outside": "/w/outside"},
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    snapshot = validator._bounded_maven_reactor("/w/p")
    assert snapshot.complete is False
    assert set(snapshot.conflicts) == {
        "maven_module_outside_project",
        "maven_module_unresolved",
    }
    assert validator._active_maven_module_dirs("/w/p") == [
        "/w/p",
        "/w/p/safe",
        "/w/p/safe/nested",
    ]
    artifacts = validator._parse_maven_expected_artifacts("/w/p")

    assert [item["path"] for item in artifacts] == ["/w/p/safe/nested/target/nested-1.jar"]
    cat_commands = [command for command in orch.commands if command.startswith("cat ")]
    assert not any("/w/outside/pom.xml" in command for command in cat_commands)
    assert not any("/linked-outside/pom.xml" in command for command in cat_commands)


def test_maven_reactor_snapshot_drops_two_node_cycle_branch():
    root_pom = """
    <project><packaging>pom</packaging><modules>
      <module>a</module><module>safe</module>
    </modules></project>
    """
    a_pom = """
    <project><packaging>pom</packaging>
      <modules><module>b</module></modules>
    </project>
    """
    b_pom = """
    <project><packaging>pom</packaging>
      <modules><module>..</module></modules>
    </project>
    """
    safe_pom = "<project><artifactId>safe</artifactId><version>1</version></project>"
    orch = FakeMavenPomOrchestrator(
        {
            "/w/p/pom.xml": root_pom,
            "/w/p/a/pom.xml": a_pom,
            "/w/p/a/b/pom.xml": b_pom,
            "/w/p/safe/pom.xml": safe_pom,
        }
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    snapshot = validator._bounded_maven_reactor("/w/p")
    assert snapshot.complete is False
    assert snapshot.conflicts == ("maven_module_cycle",)
    assert validator._active_maven_module_dirs("/w/p") == ["/w/p", "/w/p/safe"]
    assert [item["path"] for item in validator._parse_maven_expected_artifacts("/w/p")] == [
        "/w/p/safe/target/safe-1.jar"
    ]
    assert len(orch.commands) < 75


def test_maven_reactor_snapshot_self_cycle_and_caps_fail_closed(monkeypatch):
    root_pom = """
    <project><packaging>pom</packaging>
      <modules><module>.</module><module>safe</module></modules>
    </project>
    """
    safe_pom = "<project><artifactId>safe</artifactId><version>1</version></project>"
    orch = FakeMavenPomOrchestrator({"/w/p/pom.xml": root_pom, "/w/p/safe/pom.xml": safe_pom})
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/w")

    assert validator._bounded_maven_reactor("/w/p").conflicts == ("maven_module_cycle",)
    assert validator._active_maven_module_dirs("/w/p") == ["/w/p"]
    assert validator._parse_maven_expected_artifacts("/w/p") == []
    assert len(orch.commands) < 40

    capped_pom = """
    <project><packaging>pom</packaging><modules>
      <module>one</module><module>two</module>
    </modules></project>
    """
    capped = FakeMavenPomOrchestrator(
        {
            "/w/capped/pom.xml": capped_pom,
            "/w/capped/one/pom.xml": safe_pom,
            "/w/capped/two/pom.xml": safe_pom,
        }
    )
    capped_validator = PhysicalValidator(docker_orchestrator=capped, project_path="/w")
    monkeypatch.setattr(physical_validator_module, "_MAVEN_REACTOR_MAX_NODES", 2)

    assert capped_validator._bounded_maven_reactor("/w/capped").conflicts == (
        "maven_module_cap_exceeded",
    )
    assert capped_validator._active_maven_module_dirs("/w/capped") == ["/w/capped"]

    depth_root = """
    <project><packaging>pom</packaging>
      <modules><module>one</module></modules>
    </project>
    """
    depth_one = """
    <project><packaging>pom</packaging>
      <modules><module>two</module></modules>
    </project>
    """
    depth_limited = FakeMavenPomOrchestrator(
        {
            "/w/depth/pom.xml": depth_root,
            "/w/depth/one/pom.xml": depth_one,
            "/w/depth/one/two/pom.xml": safe_pom,
        }
    )
    depth_validator = PhysicalValidator(
        docker_orchestrator=depth_limited,
        project_path="/w",
    )
    monkeypatch.setattr(physical_validator_module, "_MAVEN_REACTOR_MAX_NODES", 256)
    monkeypatch.setattr(physical_validator_module, "_MAVEN_REACTOR_MAX_DEPTH", 1)

    assert depth_validator._bounded_maven_reactor("/w/depth").conflicts == (
        "maven_module_depth_exceeded",
    )
    assert depth_validator._active_maven_module_dirs("/w/depth") == ["/w/depth"]


@pytest.mark.parametrize(
    ("module_value", "expected_conflict"),
    [
        ("${dynamic.module}", "maven_module_unresolved"),
        ("missing", "maven_module_pom_unreadable"),
    ],
)
def test_maven_reactor_unverifiable_declared_child_is_incomplete(
    module_value,
    expected_conflict,
):
    root_pom = f"""
    <project><packaging>pom</packaging>
      <modules><module>{module_value}</module></modules>
    </project>
    """
    validator = PhysicalValidator(
        docker_orchestrator=FakeMavenPomOrchestrator({"/w/p/pom.xml": root_pom}),
        project_path="/w",
    )

    snapshot = validator._bounded_maven_reactor("/w/p")

    assert [record.module_dir for record in snapshot.records] == ["/w/p"]
    assert snapshot.complete is False
    assert expected_conflict in snapshot.conflicts


def _maven_reactor_verdict_validator(root_pom, extra_poms):
    safe_pom = "<project><artifactId>safe</artifactId><version>1</version></project>"
    orch = FakeMavenPomOrchestrator(
        {
            "/w/p/pom.xml": root_pom,
            "/w/p/safe/pom.xml": safe_pom,
            **extra_poms,
        }
    )
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/w")
    validator._detect_build_system = lambda _project_dir: "maven"
    validator._check_build_artifacts_complete = lambda _project_dir: {
        "exist": True,
        "count": 11,
        "class_count": 10,
        "jar_count": 1,
    }
    validator._collect_artifact_samples = lambda *_args, **_kwargs: ["safe/target/safe-1.jar"]
    validator._validate_maven_fingerprints = lambda _project_dir: {
        "valid": True,
        "details": {"classes": True},
        "modules": ["safe"],
    }
    validator._verify_expected_artifacts = lambda _project_dir, expected: {
        "all_present": bool(expected),
        "found": [item["path"] for item in expected],
        "missing": [],
        "classes_expected": 10,
        "classes_found": 10,
        "class_coverage": 1.0,
    }
    validator._check_class_files = lambda _project_dir: {
        "paths": ["/w/p/safe/target/classes/Foo.class"]
    }
    validator._check_jar_files = lambda _project_dir: {"paths": ["/w/p/safe/target/safe-1.jar"]}
    validator._collect_env_conflicts = lambda: []
    return validator


def test_unverified_maven_reactor_caps_otherwise_complete_build():
    root_pom = """
    <project><packaging>pom</packaging><modules>
      <module>safe</module><module>../outside</module>
    </modules></project>
    """
    validator = _maven_reactor_verdict_validator(
        root_pom,
        {
            "/w/outside/pom.xml": (
                "<project><artifactId>outside</artifactId><version>1</version></project>"
            )
        },
    )
    original_resolver = validator._bounded_maven_reactor
    resolver_calls = 0

    def counted_resolver(project_dir):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(project_dir)

    validator._bounded_maven_reactor = counted_resolver

    result = validator.validate_build_status("p")

    assert resolver_calls == 1
    assert result["success"] is True  # real safe-module build evidence exists
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "maven_reactor_unverified" in result["conflicts"]
    assert result["evidence"]["maven_reactor_complete"] is False
    assert "maven_module_outside_project" in result["evidence"]["maven_reactor_conflicts"]
    assert "could not be fully verified" in result["reason"]


def test_active_by_default_maven_reactor_keeps_complete_build_green():
    root_pom = """
    <project><packaging>pom</packaging><profiles><profile>
      <id>default</id>
      <activation><activeByDefault>true</activeByDefault></activation>
      <modules><module>safe</module></modules>
    </profile></profiles>
    </project>
    """
    validator = _maven_reactor_verdict_validator(root_pom, {})

    result = validator.validate_build_status("p")

    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "maven_reactor_unverified" not in result["conflicts"]
    assert result["evidence"]["maven_reactor_complete"] is True
    assert result["evidence"]["maven_reactor_modules"] == ["/w/p", "/w/p/safe"]


def test_dynamic_profile_module_caps_otherwise_complete_build():
    root_pom = """
    <project><packaging>pom</packaging>
      <modules><module>safe</module></modules>
      <profiles><profile>
        <id>dynamic</id>
        <activation><property><name>with.extra</name></property></activation>
        <modules><module>dynamic</module></modules>
      </profile></profiles>
    </project>
    """
    validator = _maven_reactor_verdict_validator(
        root_pom,
        {
            "/w/p/dynamic/pom.xml": (
                "<project><artifactId>dynamic</artifactId><version>1</version></project>"
            )
        },
    )

    result = validator.validate_build_status("p")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "maven_reactor_unverified" in result["conflicts"]
    assert result["evidence"]["maven_reactor_modules"] == ["/w/p", "/w/p/safe"]
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_profile_activation_unresolved"]


def test_inherited_version_resource_leaf_cannot_disappear_from_denominator():
    root_pom = """
    <project><packaging>pom</packaging><modules>
      <module>safe</module><module>resources-only</module>
    </modules></project>
    """
    inherited_leaf = """
    <project>
      <parent>
        <groupId>example</groupId><artifactId>parent</artifactId><version>1</version>
      </parent>
      <artifactId>resources-only</artifactId>
      <build><resources><resource><directory>src/main/resources</directory></resource></resources></build>
    </project>
    """
    validator = _maven_reactor_verdict_validator(
        root_pom,
        {"/w/p/resources-only/pom.xml": inherited_leaf},
    )

    result = validator.validate_build_status("p")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/resources-only",
    ]
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_module_artifact_unresolved"]


def _explicit_profile_root():
    return """
    <project><packaging>pom</packaging>
      <modules><module>safe</module></modules>
      <profiles><profile><id>foo</id>
        <modules><module>foo</module></modules>
      </profile></profiles>
    </project>
    """


def test_explicit_profile_missing_artifact_cannot_green():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = FakeReceiptTracker(build=[_receipt("mvn -Pfoo package")])

    def missing_foo(_project_dir, expected):
        paths = [item["path"] for item in expected]
        foo_path = "/w/p/foo/target/foo-1.jar"
        assert foo_path in paths
        return {
            "all_present": False,
            "found": [path for path in paths if path != foo_path],
            "missing": [foo_path],
            "classes_expected": 20,
            "classes_found": 10,
            "class_coverage": 0.5,
        }

    validator._verify_expected_artifacts = missing_foo

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_profiles_enabled"] == ["foo"]


def test_pending_profile_dispatch_is_conservative_and_terminal_poll_keeps_denominator():
    tracker = CommandTracker()
    receipt = tracker.track_execution_receipt(
        command="mvn -Pfoo package",
        tool="maven",
        working_dir="/w/p",
        command_kind="build",
        dispatch_status="running_detached",
        poll_ref="job:profile-build",
        invocation_status="pending",
    )
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = tracker

    pending = validator.validate_build_status("p")

    assert pending["build_complete"] is False
    assert pending["evidence_status"] == "partial"
    assert pending["evidence"]["maven_profile_selection_conservative"] is True
    assert pending["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/foo",
    ]
    assert pending["evidence"]["maven_reactor_conflicts"] == ["maven_profile_execution_unfinished"]

    assert tracker.update_execution_receipt(
        receipt["poll_ref"],
        invocation_status="completed",
        dispatch_status="completed_detached",
        exit_code=0,
        operation_outcome="success",
        lifecycle_state="finished",
    )

    def missing_foo(_project_dir, expected):
        paths = [item["path"] for item in expected]
        foo_path = "/w/p/foo/target/foo-1.jar"
        assert foo_path in paths
        return {
            "all_present": False,
            "found": [path for path in paths if path != foo_path],
            "missing": [foo_path],
            "classes_expected": 20,
            "classes_found": 10,
            "class_coverage": 0.5,
        }

    validator._verify_expected_artifacts = missing_foo
    completed = validator.validate_build_status("p")

    assert completed["build_complete"] is False
    assert completed["evidence_status"] == "partial"
    assert completed["evidence"]["maven_profiles_enabled"] == ["foo"]
    assert completed["evidence"]["maven_reactor_conflicts"] == []


def test_pending_receipt_without_artifacts_is_not_build_success_evidence():
    tracker = CommandTracker()
    tracker.track_execution_receipt(
        command="mvn -Pfoo package",
        tool="maven",
        working_dir="/w/p",
        command_kind="build",
        dispatch_status="running_detached",
        poll_ref="job:no-artifacts",
        invocation_status="pending",
    )
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = tracker
    validator._check_build_artifacts_complete = lambda _project_dir: {
        "exist": False,
        "count": 0,
        "class_count": 0,
        "jar_count": 0,
    }
    validator._validate_maven_fingerprints = lambda _project_dir: {
        "valid": False,
        "details": {},
        "modules": [],
    }
    validator._verify_expected_artifacts = lambda _project_dir, expected: {
        "all_present": False,
        "found": [],
        "missing": [item["path"] for item in expected],
        "classes_expected": 0,
        "classes_found": 0,
        "class_coverage": 0.0,
    }
    validator._check_class_files = lambda _project_dir: {"paths": []}
    validator._check_jar_files = lambda _project_dir: {"paths": []}

    result = validator.validate_build_status("p")

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"


def test_timed_out_profile_dispatch_still_enters_expected_denominator():
    tracker = CommandTracker()
    tracker.track_execution_receipt(
        command="mvn -Pfoo package",
        tool="maven",
        working_dir="/w/p",
        command_kind="build",
        invocation_status="timeout",
        exit_code=124,
        termination_reason="absolute_timeout",
    )
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = tracker

    def missing_foo(_project_dir, expected):
        paths = [item["path"] for item in expected]
        assert "/w/p/foo/target/foo-1.jar" in paths
        return {
            "all_present": False,
            "found": ["/w/p/safe/target/safe-1.jar"],
            "missing": ["/w/p/foo/target/foo-1.jar"],
            "classes_expected": 20,
            "classes_found": 10,
            "class_coverage": 0.5,
        }

    validator._verify_expected_artifacts = missing_foo
    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_profiles_enabled"] == ["foo"]
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/foo",
    ]


def test_symlinked_receipt_workdir_still_binds_profile_to_current_reactor():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.docker_orchestrator.realpaths["/w/current"] = "/w/p"
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn -Pfoo package",
                working_dir="/w/current",
            )
        ]
    )

    def missing_foo(_project_dir, expected):
        paths = [item["path"] for item in expected]
        assert "/w/p/foo/target/foo-1.jar" in paths
        return {
            "all_present": False,
            "found": ["/w/p/safe/target/safe-1.jar"],
            "missing": ["/w/p/foo/target/foo-1.jar"],
            "classes_expected": 20,
            "classes_found": 10,
            "class_coverage": 0.5,
        }

    validator._verify_expected_artifacts = missing_foo
    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_profiles_enabled"] == ["foo"]
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/foo",
    ]


def test_unprovable_file_selected_reactor_fails_closed_without_profile_flag():
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    validator.docker_orchestrator.realpaths["/w/alias-pom.xml"] = None
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn -f /w/alias-pom.xml package",
                working_dir="/w",
            )
        ]
    )

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_profile_selection_unresolved"]


def test_explicit_profile_complete_artifacts_can_green():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = FakeReceiptTracker(
        build=[_receipt("mvn --activate-profiles foo package")]
    )

    result = validator.validate_build_status("p")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/foo",
    ]


def test_file_selected_profile_receipt_from_parent_can_verify_exact_reactor():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn -f /w/p/pom.xml -Pfoo package",
                working_dir="/w",
            )
        ]
    )

    result = validator.validate_build_status("p")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_profiles_enabled"] == ["foo"]
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/safe",
        "/w/p/foo",
    ]


def test_explicit_profile_disables_active_by_default_in_same_pom():
    root_pom = """
    <project><packaging>pom</packaging><profiles>
      <profile><id>default</id>
        <activation><activeByDefault>true</activeByDefault></activation>
        <modules><module>defaulted</module></modules>
      </profile>
      <profile><id>foo</id><modules><module>foo</module></modules></profile>
    </profiles></project>
    """
    validator = _maven_reactor_verdict_validator(
        root_pom,
        {
            "/w/p/defaulted/pom.xml": (
                "<project><artifactId>defaulted</artifactId><version>1</version></project>"
            ),
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            ),
        },
    )
    validator.command_tracker = FakeReceiptTracker(build=[_receipt("mvn -Pfoo package")])

    result = validator.validate_build_status("p")

    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_reactor_modules"] == ["/w/p", "/w/p/foo"]
    assert not any(
        command.startswith("cat ") and "/defaulted/pom.xml" in command
        for command in validator.docker_orchestrator.commands
    )


def test_explicit_profile_disable_keeps_unrelated_active_by_default_module():
    root_pom = """
    <project><packaging>pom</packaging><profiles>
      <profile><id>default</id>
        <activation><activeByDefault>true</activeByDefault></activation>
        <modules><module>defaulted</module></modules>
      </profile>
      <profile><id>foo</id><modules><module>foo</module></modules></profile>
    </profiles></project>
    """
    validator = _maven_reactor_verdict_validator(
        root_pom,
        {
            "/w/p/defaulted/pom.xml": (
                "<project><artifactId>defaulted</artifactId><version>1</version></project>"
            ),
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            ),
        },
    )
    validator.command_tracker = FakeReceiptTracker(build=[_receipt("mvn -P!foo package")])

    result = validator.validate_build_status("p")

    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_profiles_disabled"] == ["foo"]
    assert result["evidence"]["maven_reactor_modules"] == [
        "/w/p",
        "/w/p/defaulted",
    ]


def test_ambiguous_explicit_profile_selection_cannot_green():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = FakeReceiptTracker(build=[_receipt("mvn -Pfoo,!foo package")])

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_profile_selection_unresolved"]


def test_maven_config_profile_selection_cannot_silently_green():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    orch = validator.docker_orchestrator
    orch.dirs.add("/w/p/.mvn")
    orch.files["/w/p/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_changes_graph"]


def test_non_graph_maven_config_keeps_complete_reactor_green():
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    orch = validator.docker_orchestrator
    orch.dirs.add("/w/p/.mvn")
    orch.files["/w/p/.mvn/maven.config"] = "-DskipTests"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_reactor_conflicts"] == []


def test_empty_maven_directory_above_project_cannot_silently_green():
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    validator.docker_orchestrator.dirs.add("/w/.mvn")

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_outside_project"]


def test_profile_maven_config_above_project_cannot_shrink_reactor_green():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    orch = validator.docker_orchestrator
    orch.dirs.add("/w/.mvn")
    orch.files["/w/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_outside_project"]


def test_empty_project_maven_directory_shadows_unsafe_parent_config():
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    orch = validator.docker_orchestrator
    orch.dirs.update({"/w/p/.mvn", "/w/.mvn"})
    orch.files["/w/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_reactor_conflicts"] == []
    assert not any(
        command.startswith("cat ") and "/w/.mvn/maven.config" in command
        for command in orch.commands
    )


def test_separated_file_selector_uses_child_maven_config_seed():
    validator = _maven_reactor_verdict_validator(
        _explicit_profile_root(),
        {
            "/w/p/foo/pom.xml": (
                "<project><artifactId>foo</artifactId><version>1</version></project>"
            )
        },
    )
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn -f /w/p/pom.xml package",
                working_dir="/w",
            )
        ]
    )
    orch = validator.docker_orchestrator
    orch.dirs.add("/w/p/.mvn")
    orch.files["/w/p/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_changes_graph"]


def test_separated_file_selector_child_empty_maven_dir_shadows_parent():
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    validator.command_tracker = FakeReceiptTracker(
        build=[
            _receipt(
                "mvn --file /w/p/pom.xml package",
                working_dir="/w",
            )
        ]
    )
    orch = validator.docker_orchestrator
    orch.dirs.update({"/w/p/.mvn", "/w/.mvn"})
    orch.files["/w/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert result["evidence"]["maven_reactor_conflicts"] == []


@pytest.mark.parametrize(
    "command",
    [
        "mvn -f/w/p/pom.xml package",
        "mvn --file=/w/p/pom.xml package",
    ],
)
def test_attached_file_selector_keeps_cwd_maven_config_seed(command):
    validator = _maven_reactor_verdict_validator(
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>",
        {},
    )
    validator.command_tracker = FakeReceiptTracker(build=[_receipt(command, working_dir="/w")])
    orch = validator.docker_orchestrator
    orch.dirs.update({"/w/p/.mvn", "/w/.mvn"})
    orch.files["/w/.mvn/maven.config"] = "-Pfoo"

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_outside_project"]


def test_maven_launcher_probe_error_fails_closed_instead_of_claiming_absence():
    class BrokenMavenProbeOrchestrator(FakeMavenPomOrchestrator):
        def execute_command(self, command):
            if command.strip() == "test -d /w/.mvn":
                self.commands.append(command)
                return {"exit_code": 2, "output": "probe unavailable"}
            return super().execute_command(command)

    root_pom = (
        "<project><packaging>pom</packaging><modules>" "<module>safe</module></modules></project>"
    )
    orch = BrokenMavenProbeOrchestrator(
        {
            "/w/p/pom.xml": root_pom,
            "/w/p/safe/pom.xml": (
                "<project><artifactId>safe</artifactId><version>1</version></project>"
            ),
        }
    )
    validator = _maven_reactor_verdict_validator(root_pom, {})
    validator.docker_orchestrator = orch

    result = validator.validate_build_status("p")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_config_unreadable"]


def test_module_pom_symlink_outside_caps_build_without_reading_target():
    root_pom = """
    <project><packaging>pom</packaging><modules>
      <module>safe</module><module>escaped</module>
    </modules></project>
    """
    validator = _maven_reactor_verdict_validator(root_pom, {})
    orch = validator.docker_orchestrator
    orch.realpaths["/w/p/escaped/pom.xml"] = "/outside/escaped.xml"
    orch.files["/outside/escaped.xml"] = (
        "<project><artifactId>escaped</artifactId><version>1</version></project>"
    )

    result = validator.validate_build_status("p")

    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_module_pom_outside_project"]
    assert not any(
        command.startswith("cat ") and "/outside/escaped.xml" in command
        for command in orch.commands
    )


def test_root_pom_symlink_outside_caps_build_without_reading_target():
    validator = _maven_reactor_verdict_validator(
        "<project><artifactId>root</artifactId><version>1</version></project>",
        {},
    )
    orch = validator.docker_orchestrator
    orch.realpaths["/w/p/pom.xml"] = "/outside/root.xml"
    orch.files["/outside/root.xml"] = (
        "<project><artifactId>outside</artifactId><version>1</version></project>"
    )

    result = validator.validate_build_status("p")

    assert result["evidence_status"] == "partial"
    assert result["evidence"]["maven_reactor_conflicts"] == ["maven_module_pom_outside_project"]
    assert not any(
        command.startswith("cat ") and "/outside/root.xml" in command for command in orch.commands
    )


# ===========================================================================
# 3. Reactor-authoritative module metrics + root-inclusive scan
# ===========================================================================
def test_reactor_authoritative_excludes_non_reactor_scanned_modules():
    """The reactor built api+runtime; a scanned dir not in the reactor (a standalone
    example pom) is NOT part of the build and must not be counted as detected."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": "api", "name": "api", "class_count": 10, "jar_count": 1, "report_dirs": []},
            {
                "path": "runtime",
                "name": "runtime",
                "class_count": 0,
                "jar_count": 0,
                "report_dirs": [],
            },
            {
                "path": "examples",
                "name": "examples",
                "class_count": 0,
                "jar_count": 0,
                "report_dirs": [],
            },
        ],
        reactor_status={"api": "success", "runtime": "failure"},
        tests={},
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )
    s = metrics["module_summary"]
    assert s["modules_total"] == 2  # detected = reactor modules; examples excluded
    assert s["modules_built"] == 1 and s["modules_failed"] == 1
    assert "examples" not in {m["path"] for m in metrics["modules"]}


def test_reactor_only_module_counted_when_no_scan_match():
    """A reactor module no disk scan found is still counted (detected == reactor count)."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": "api", "name": "api", "class_count": 5, "jar_count": 0, "report_dirs": []}
        ],
        reactor_status={"api": "success", "ghost": "success"},
        tests={},
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )
    s = metrics["module_summary"]
    assert s["modules_total"] == 2 and s["modules_built"] == 2
    assert "ghost" in {m["name"] for m in metrics["modules"]}


def test_no_reactor_jar_without_classes_is_not_built():
    # commons-vfs shape: no reactor summary, a module left a stale jar but
    # compiled no fresh .class files (its build failed dependency resolution).
    # It must read detected-but-not-built, not an optimistic "success".
    metrics = assemble_module_metrics(
        modules=[
            {"path": "core", "name": "core", "class_count": 12, "jar_count": 1, "report_dirs": []},
            {
                "path": "examples",
                "name": "examples",
                "class_count": 0,
                "jar_count": 1,
                "report_dirs": [],
            },
        ],
        reactor_status={},
        tests={},
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["core"]["build_status"] == "success"  # has classes
    assert by_path["examples"]["build_status"] != "success"  # jar only -> not built
    assert metrics["module_summary"]["modules_total"] == 2
    assert metrics["module_summary"]["modules_built"] == 1


def test_module_summary_counts_tested_and_not_tested():
    """tested = modules that ran >=1 test (tests_total>0); not_tested = detected - tested."""
    metrics = assemble_module_metrics(
        modules=[
            {"path": "api", "name": "api", "class_count": 10, "jar_count": 1, "report_dirs": []},
            {"path": "core", "name": "core", "class_count": 8, "jar_count": 1, "report_dirs": []},
            {"path": "util", "name": "util", "class_count": 4, "jar_count": 0, "report_dirs": []},
        ],
        reactor_status={"api": "success", "core": "success", "util": "success"},
        tests={
            "api": {"tests_total": 12, "tests_passed": 12, "failing_count": 0},
            "core": {
                "tests_total": 0,
                "tests_passed": 0,
                "failing_count": 0,
            },  # built, no tests run
        },
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )
    s = metrics["module_summary"]
    assert s["modules_total"] == 3
    assert s["modules_tested"] == 1  # only api ran tests
    assert s["modules_not_tested"] == 2  # core (0 tests) + util (no entry)
    assert s["modules_tested"] + s["modules_not_tested"] == s["modules_total"]


def test_submodule_breakdown_header_shows_tested_not_tested():
    """The markdown breakdown header surfaces tested / not-tested alongside built/detected."""
    metrics = {
        "module_summary": {
            "modules_total": 3,
            "modules_built": 2,
            "modules_failed": 0,
            "modules_skipped": 0,
            "modules_tested": 1,
            "modules_not_tested": 2,
            "modules_with_test_failures": 0,
            "build_systems": ["maven"],
            "single_module": False,
        },
        "modules": [
            {
                "name": "api",
                "path": "api",
                "build_status": "success",
                "tests_total": 12,
                "tests_passed": 12,
                "tests_failed": 0,
                "failing_count": 0,
            },
            {
                "name": "core",
                "path": "core",
                "build_status": "success",
                "tests_total": 0,
                "failing_count": 0,
            },
            {
                "name": "util",
                "path": "util",
                "build_status": "unknown",
                "tests_total": None,
                "failing_count": None,
            },
        ],
    }
    body = "\n".join(ReportTool()._render_submodule_breakdown(metrics))
    assert "2 built / 3 detected" in body
    assert "1 tested · 2 not tested" in body


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
                "has_test_reports": True,
                "status": "SUCCESS",
                "reason": "All tests passed",
                "pass_rate": 100.0,
                "total_tests": 50,
                "passed_tests": 50,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 50,
            },
        )
    )
    assert agent._legacy_get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "partial"


def test_agent_caps_at_partial_on_low_test_execution():
    """CLI parity: a detected suite that barely ran (1 of 1122) -> PARTIAL even
    though the one test that ran passed (mirrors tests_not_fully_executed)."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "build_complete": True, "reason": "Built 100%"},
            test_status={
                "has_test_reports": True,
                "status": "SUCCESS",
                "reason": "1/1 passed",
                "pass_rate": 100.0,
                "total_tests": 1,
                "passed_tests": 1,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 1122,
            },
        )
    )
    assert agent._legacy_get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "partial"


def test_agent_zero_executed_tests_is_partial_not_failed():
    """CLI parity (carbondata regression): build green but the detected suite did
    NOT run (0 of 1122 executed) -> PARTIAL, not a 0% pass-rate FAILURE. The CLI
    verdict must match the report's tests_not_fully_executed -> PARTIAL."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "build_complete": True, "reason": "Built 100%"},
            test_status={
                "has_test_reports": True,
                "status": "WARNING",
                "reason": "no tests ran",
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed_tests": 0,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 1122,
            },
        )
    )
    agent._legacy_get_verified_final_status(react_engine_success=True)
    assert agent.final_verdict == "partial"  # not "failed"


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
        {
            "path": "tools/cli",
            "name": "tools:cli",
            "class_count": 27,
            "jar_count": 0,
            "report_dirs": [],
        },
        {
            "path": "examples",
            "name": "examples",
            "class_count": 0,
            "jar_count": 0,
            "report_dirs": [],
        },
    ]
    tool = ReportTool()
    tool.physical_validator = _ModuleScanValidator(project_dir, scanned, active_dirs=[project_dir])
    tool._get_project_info = lambda: {"directory": project_dir}

    metrics = tool._compute_module_metrics({}, generated_at="t")

    paths = {m["path"] for m in metrics["modules"]}
    assert "tools/cli" in paths  # built submodule kept despite no reactor / not declared
    assert "examples" not in paths  # artifact-less + undeclared stays excluded
    assert metrics["module_summary"]["modules_built"] >= 1
