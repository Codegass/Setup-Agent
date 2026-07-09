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
