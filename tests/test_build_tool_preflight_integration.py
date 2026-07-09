# tests/test_build_tool_preflight_integration.py
"""BuildTool pre-flight integration (spec §§1b-1c, 3) on the consolidated tool.

Uses a scriptable orchestrator plus scripted backend tools; asserts on the
OBSERVATION text because the narration IS the feature
(transparency-by-construction). The donor branch wired this into the internal
MavenTool/GradleTool; here the consolidated build facade is the hot path.

PR #12's orchestration layer owns working-directory injection, so the facade
adds NO workdir defaulting — only the [scope] warning when the model
EXPLICITLY narrows below a healthy reactor's recommended build root.
"""

import json
import shlex

from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool


class ScriptedOrch:
    """Answers marker probes, manifest reads, java -version; records commands."""

    def __init__(
        self,
        java="17",
        manifest=None,
        markers=("/workspace/proj/pom.xml",),
        project_name="proj",
    ):
        self.java = java
        self.manifest = manifest or {}
        self.markers = set(markers)
        self.project_name = project_name
        self.commands = []

    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0,
                        "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in cmd:  # build-marker probes
            tokens = shlex.split(cmd)
            try:
                path = tokens[tokens.index("-f") + 1]
            except (ValueError, IndexError):
                path = ""
            return {"success": True, "exit_code": 0,
                    "output": "exists" if path in self.markers else "missing"}
        return {"success": True, "exit_code": 0, "output": ""}


class ScriptedBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, replays scripted results."""

    def __init__(self, *results):
        self.calls = []
        self._results = list(results) or [ToolResult(success=True, output="BUILD SUCCESS")]

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        # Replay the script; hold the last result for any further calls so a
        # runaway retry loop is observable as extra `calls`, not an IndexError.
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


ENFORCER_FAIL = ("[ERROR] RequireJavaVersion failed ... Detected JDK Version: "
                 "11.0.2 is not in the allowed range [17,). BUILD FAILURE")


def _tool(orch, maven=None, gradle=None):
    return BuildTool(
        orch,
        maven_tool=maven or ScriptedBackendTool(),
        gradle_tool=gradle or ScriptedBackendTool(),
    )


def _patch_provision(monkeypatch, ok=True):
    import sag.tools.internal.build_preflight as bp

    monkeypatch.setattr(
        bp.JdkPreflight,
        "_provision",
        (lambda self, v: f"/usr/lib/jvm/java-{v}-openjdk-arm64") if ok else (lambda self, v: None),
    )


def test_matching_jdk_no_narration():
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(action="compile", working_directory="/workspace/proj")
    assert "[pre-flight]" not in (result.output or "")


def test_mismatch_narrated_in_observation(monkeypatch):
    _patch_provision(monkeypatch)
    orch = ScriptedOrch(
        java="11",
        manifest={"java_version": "17", "java_version_source": "maven-enforcer"},
    )
    result = _tool(orch).execute(action="compile", working_directory="/workspace/proj")
    assert "[pre-flight] Required: Java 17" in (result.output or "")


def test_version_error_triggers_single_retry_then_success(monkeypatch):
    _patch_provision(monkeypatch)
    maven = ScriptedBackendTool(
        ToolResult(success=False, output=ENFORCER_FAIL),
        ToolResult(success=True, output="BUILD SUCCESS"),
    )
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(
        action="compile", working_directory="/workspace/proj"
    )
    assert len(maven.calls) == 2          # original + exactly one retry
    assert "[pre-flight] build error requires Java 17, re-provisioned, retry 1/1" in result.output
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}
    assert result.success


def test_retry_is_bounded_to_exactly_once(monkeypatch):
    _patch_provision(monkeypatch)
    # Backend keeps failing with a version-shaped error even after retry.
    maven = ScriptedBackendTool(ToolResult(success=False, output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(
        action="test", working_directory="/workspace/proj"
    )
    assert len(maven.calls) == 2          # never a second retry
    assert result.verdict == "failed"     # honest failure after the bounded retry


def test_non_version_failure_does_not_retry():
    maven = ScriptedBackendTool(
        ToolResult(success=False, output="BUILD FAILURE: test failures")
    )
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")
    assert len(maven.calls) == 1


def test_no_retry_when_error_version_matches_active():
    # The error demands 17 and 17 is already active: re-provisioning can't help.
    maven = ScriptedBackendTool(ToolResult(success=False, output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="17", manifest={})
    result = _tool(orch, maven=maven).execute(
        action="test", working_directory="/workspace/proj"
    )
    assert len(maven.calls) == 1
    assert "retry 1/1" not in (result.output or "")


def test_no_rerun_when_reprovision_fails(monkeypatch):
    _patch_provision(monkeypatch, ok=False)
    maven = ScriptedBackendTool(ToolResult(success=False, output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(
        action="test", working_directory="/workspace/proj"
    )
    assert len(maven.calls) == 1          # nothing changed, a rerun would lie
    assert "retry 1/1" not in (result.output or "")


def test_scope_warning_when_explicit_workdir_deeper_than_build_root():
    orch = ScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
        markers=("/workspace/proj/pom.xml", "/workspace/proj/core/pom.xml"),
    )
    result = _tool(orch).execute(action="test", working_directory="/workspace/proj/core")
    assert "[scope]" in (result.output or "")


def test_no_scope_warning_at_recommended_root():
    orch = ScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
    )
    result = _tool(orch).execute(action="test", working_directory="/workspace/proj")
    assert "[scope]" not in (result.output or "")


def test_no_scope_warning_when_workdir_resolved_by_detection_fallback():
    # The facade's own project-name fallback (not the model) lands below the
    # manifest build_root: not an explicit narrowing, so stay quiet.
    orch = ScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace",
        },
        markers=("/workspace/proj/pom.xml",),
        project_name="proj",
    )
    result = _tool(orch).execute(action="test")  # default "/workspace"
    assert "[scope]" not in (result.output or "")


def test_deps_action_skips_preflight():
    orch = ScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _tool(orch).execute(action="deps", working_directory="/workspace/proj")
    assert "[pre-flight]" not in (result.output or "")
    assert not any("java -version" in c for c in orch.commands)


def test_gradle_version_error_single_retry(monkeypatch):
    _patch_provision(monkeypatch)
    gradle = ScriptedBackendTool(
        ToolResult(
            success=False,
            output="Unsupported class file major version 61 ... class file version 61.0",
        ),
        ToolResult(success=True, output="BUILD SUCCESSFUL"),
    )
    orch = ScriptedOrch(java="11", manifest={}, markers=("/workspace/proj/build.gradle",))
    result = _tool(orch, gradle=gradle).execute(
        action="package", working_directory="/workspace/proj"
    )
    assert len(gradle.calls) == 2
    assert "retry 1/1" in (result.output or "")
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}


# --- Internal MavenTool/GradleTool wiring (donor port) ----------------------
#
# The same pre-flight + bounded retry guards the internal tools for agents
# that reach maven/gradle directly instead of through the consolidated
# facade. The donor's workdir re-targeting ("defaulting to the recommended
# reactor root") and its auto fail_at_end/--continue mutations are NOT
# ported: PR #12's orchestration-layer injection and the BuildTool backends
# own those. Kept: narration prepend, bounded retry, and the [scope] warning
# for EXPLICITLY narrowed working directories.


class MavenScriptedOrch:
    """Canned results for the internal MavenTool; records every command."""

    def __init__(self, java="17", manifest=None, build_output="BUILD SUCCESS",
                 build_ok=True, project_name="proj"):
        self.java = java
        self.manifest = manifest or {}
        self.build_output = build_output
        self.build_ok = build_ok
        self.commands = []
        self.project_name = project_name

    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if cmd.startswith("mvn") and ("test" in cmd or "install" in cmd or "compile" in cmd):
            return {"success": self.build_ok, "exit_code": 0 if self.build_ok else 1,
                    "output": self.build_output}
        if cmd.startswith("find") and "target/classes" in cmd:
            # post-build artifact validation for compile/package/install
            return {"success": True, "exit_code": 0,
                    "output": "/workspace/proj/target/classes/Foo.class"}
        if "test -f" in cmd or "test -e" in cmd:  # pom probes
            return {"success": True, "exit_code": 0, "output": "EXISTS"}
        if "command -v mvn" in cmd or "which mvn" in cmd:
            return {"success": True, "exit_code": 0, "output": "/usr/bin/mvn"}
        return {"success": True, "exit_code": 0, "output": ""}


def _internal_maven_tool(orch):
    tool = MavenTool.__new__(MavenTool)  # skip full __init__; wire minimum
    tool.orchestrator = orch
    tool.toolchain_manager = None
    tool.command_tracker = None
    tool.output_storage = None
    return tool


def _mvn_runs(orch, phase):
    # Actual mvn invocations only: the test-summary heredoc embeds the mvn
    # command string, so a bare '"mvn" in c' substring filter would overcount.
    return [c for c in orch.commands if c.startswith("mvn") and phase in c]


def test_maven_tool_matching_jdk_no_narration():
    orch = MavenScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj"
    )
    assert "[pre-flight]" not in (result.output or "")


def test_maven_tool_mismatch_narrated_in_observation(monkeypatch):
    _patch_provision(monkeypatch)
    orch = MavenScriptedOrch(
        java="11", manifest={"java_version": "17", "java_version_source": "maven-enforcer"}
    )
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj"
    )
    assert "[pre-flight] Required: Java 17" in (result.output or "")


def test_maven_tool_version_error_triggers_single_retry(monkeypatch):
    _patch_provision(monkeypatch)
    orch = MavenScriptedOrch(java="11", manifest={},
                             build_output=ENFORCER_FAIL, build_ok=False)
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj"
    )
    mvn_runs = _mvn_runs(orch, "compile")
    assert len(mvn_runs) == 2          # original + exactly one retry
    assert "retry 1/1" in (result.output or "")
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}


def test_maven_tool_non_version_failure_does_not_retry():
    orch = MavenScriptedOrch(java="17", manifest={"java_version": "17"},
                             build_output="BUILD FAILURE: test failures", build_ok=False)
    _internal_maven_tool(orch).execute(command="test", working_directory="/workspace/proj")
    assert len(_mvn_runs(orch, "test")) == 1


def test_maven_tool_scope_warning_when_leaf_targeted():
    orch = MavenScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _internal_maven_tool(orch).execute(
        command="test", working_directory="/workspace/proj/core"
    )
    assert "[scope]" in (result.output or "")


def test_maven_tool_no_scope_warning_at_recommended_root():
    orch = MavenScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _internal_maven_tool(orch).execute(
        command="test", working_directory="/workspace/proj"
    )
    assert "[scope]" not in (result.output or "")


def test_maven_tool_unscoped_invocation_never_retargets_or_warns():
    # The tool's own project-name fallback (not the model) lands below the
    # manifest build_root: not an explicit narrowing, so stay quiet. The
    # donor's re-targeting default is superseded by PR #12's orchestration
    # injection and is deliberately not ported.
    orch = MavenScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace",
    })
    result = _internal_maven_tool(orch).execute(command="test")  # default "/workspace"
    assert "defaulting to the recommended reactor root" not in (result.output or "")
    assert "[scope]" not in (result.output or "")


def test_maven_tool_pl_flag_warns_about_narrowed_scope():
    orch = MavenScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _internal_maven_tool(orch).execute(
        command="test", working_directory="/workspace/proj", extra_args="-pl core"
    )
    assert "[scope]" in (result.output or "")


class GradleScriptedOrch(MavenScriptedOrch):
    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        # Actual gradle invocations only: build-file probes ('test -f
        # .../build.gradle || ...') contain both 'gradle' and 'build', so a
        # bare substring match would swallow them and feed probes the canned
        # build output. Anchor on the executable at the start of the command.
        if cmd.startswith("gradle") and ("build" in cmd or "test" in cmd or "check" in cmd):
            return {"success": self.build_ok, "exit_code": 0 if self.build_ok else 1,
                    "output": self.build_output}
        if "test -f" in cmd:  # wrapper detection, build-file/settings probes
            return {"success": True, "exit_code": 0, "output": "yes"}
        if "which gradle" in cmd:
            return {"success": True, "exit_code": 0, "output": "/usr/bin/gradle"}
        return {"success": True, "exit_code": 0, "output": "yes"}


def _internal_gradle_tool(orch):
    tool = GradleTool.__new__(GradleTool)  # skip full __init__; wire minimum
    tool.orchestrator = orch
    tool.toolchain_manager = None
    tool.output_storage = None
    return tool


def _gradle_runs(orch, task):
    # Same overcount hazard as _mvn_runs: only count real gradle invocations.
    return [c for c in orch.commands if c.startswith("gradle") and task in c]


def test_gradle_tool_mismatch_narrated(monkeypatch):
    _patch_provision(monkeypatch)
    orch = GradleScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj"
    )
    assert "[pre-flight] Required: Java 17" in (result.output or "")


def test_gradle_tool_version_error_single_retry(monkeypatch):
    _patch_provision(monkeypatch)
    fail = "Unsupported class file major version 61 ... class file version 61.0"
    orch = GradleScriptedOrch(java="11", manifest={}, build_output=fail, build_ok=False)
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj"
    )
    runs = _gradle_runs(orch, "build")
    assert len(runs) == 2          # original + exactly one retry
    assert "retry 1/1" in (result.output or "")


def test_gradle_tool_scope_warning_when_leaf_targeted():
    orch = GradleScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj/core"
    )
    assert "[scope]" in (result.output or "")


def test_gradle_tool_no_auto_continue_mutation_on_healthy_reactor():
    # No auto --continue mutation on the internal tool: the BuildTool
    # backends (PR #12) own fail-at-end/--continue wiring; the internal
    # tool only honors an explicit fail_at_end=True.
    orch = GradleScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    _internal_gradle_tool(orch).execute(command="build", working_directory="/workspace/proj")
    runs = _gradle_runs(orch, "build")
    assert runs and "--continue" not in runs[0]
