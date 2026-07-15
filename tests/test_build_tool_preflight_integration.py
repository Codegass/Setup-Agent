# tests/test_build_tool_preflight_integration.py
"""BuildTool pre-flight integration (spec §§1b-1c, 3) on the consolidated tool.

Uses a scriptable orchestrator plus scripted backend tools; asserts on the
OBSERVATION text because the narration IS the feature
(transparency-by-construction). The donor branch wired this into the internal
MavenTool/GradleTool; here the consolidated build facade is the hot path.

Single pre-flight ownership: the facade runs pre-flight/bounded-retry/[scope]
and passes _env_preflight=False to the internal tools, so exactly ONE layer
probes the container — and reruns — per build. The internal tools keep the
guarantee only for direct callers (tool_recovery's delegate path).

PR #12's orchestration layer owns working-directory injection, so the facade
adds NO workdir defaulting — only the [scope] warning when the model
EXPLICITLY narrows below a healthy reactor's recommended build root (or, for
maven, passes a -pl module selection).
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
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in cmd:  # build-marker probes
            tokens = shlex.split(cmd)
            try:
                path = tokens[tokens.index("-f") + 1]
            except (ValueError, IndexError):
                path = ""
            return {
                "success": True,
                "exit_code": 0,
                "output": "exists" if path in self.markers else "missing",
            }
        return {"success": True, "exit_code": 0, "output": ""}


class ScriptedBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, replays scripted results."""

    def __init__(self, *results):
        self.calls = []
        self._results = list(results) or [ToolResult.completed_success(output="BUILD SUCCESS")]

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        # Replay the script; hold the last result for any further calls so a
        # runaway retry loop is observable as extra `calls`, not an IndexError.
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


ENFORCER_FAIL = (
    "[ERROR] RequireJavaVersion failed ... Detected JDK Version: "
    "11.0.2 is not in the allowed range [17,). BUILD FAILURE"
)


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
        ToolResult.completed_failure(output=ENFORCER_FAIL),
        ToolResult.completed_success(output="BUILD SUCCESS"),
    )
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(action="compile", working_directory="/workspace/proj")
    assert len(maven.calls) == 2  # original + exactly one retry
    assert "[pre-flight] build error requires Java 17, re-provisioned, retry 1/1" in result.output
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}
    assert result.succeeded


def test_retry_is_bounded_to_exactly_once(monkeypatch):
    _patch_provision(monkeypatch)
    # Backend keeps failing with a version-shaped error even after retry.
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")
    assert len(maven.calls) == 2  # never a second retry
    assert result.operation_outcome.value == "failed"


def test_non_version_failure_does_not_retry():
    maven = ScriptedBackendTool(ToolResult.completed_failure(output="BUILD FAILURE: test failures"))
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")
    assert len(maven.calls) == 1


def test_no_retry_when_error_version_matches_active():
    # The error demands 17 and 17 is already active: re-provisioning can't help.
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="17", manifest={})
    result = _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")
    assert len(maven.calls) == 1
    assert "retry 1/1" not in (result.output or "")


def test_no_rerun_when_reprovision_fails(monkeypatch):
    _patch_provision(monkeypatch, ok=False)
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=ENFORCER_FAIL))
    orch = ScriptedOrch(java="11", manifest={})
    result = _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")
    assert len(maven.calls) == 1  # nothing changed, a rerun would lie
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


def test_scope_warning_for_pl_token_in_args():
    # -pl is a maven module selection: a [scope] narrowing even at the root.
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(
        action="test", args="-pl core", working_directory="/workspace/proj"
    )
    assert "[scope]" in (result.output or "")


def test_scope_warning_for_pl_equals_form():
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(
        action="test", args="-pl=core -am", working_directory="/workspace/proj"
    )
    assert "[scope]" in (result.output or "")


def test_no_scope_warning_for_pl_substring_lookalike():
    # '-pl' must be a token match, not a substring: '--no-plugin-updates'
    # contains '-pl' but selects no modules.
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(
        action="test", args="--no-plugin-updates", working_directory="/workspace/proj"
    )
    assert "[scope]" not in (result.output or "")


def test_backends_delegate_with_env_preflight_disabled():
    # Single ownership: the facade already ran the pre-flight, so the
    # backends must tell the internal tools to skip theirs.
    maven = ScriptedBackendTool()
    gradle = ScriptedBackendTool()
    orch = ScriptedOrch(
        java="17",
        manifest={"java_version": "17"},
        markers=("/workspace/proj/pom.xml",),
    )
    _tool(orch, maven=maven, gradle=gradle).execute(
        action="compile", working_directory="/workspace/proj"
    )
    assert maven.calls[0]["_env_preflight"] is False

    orch = ScriptedOrch(
        java="17",
        manifest={"java_version": "17"},
        markers=("/workspace/proj/build.gradle",),
    )
    _tool(orch, maven=maven, gradle=gradle).execute(
        action="test", working_directory="/workspace/proj"
    )
    assert gradle.calls[0]["_env_preflight"] is False


def test_deps_action_skips_preflight():
    orch = ScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _tool(orch).execute(action="deps", working_directory="/workspace/proj")
    assert "[pre-flight]" not in (result.output or "")
    assert not any("java -version" in c for c in orch.commands)


def test_gradle_version_error_single_retry(monkeypatch):
    _patch_provision(monkeypatch)
    gradle = ScriptedBackendTool(
        ToolResult.completed_failure(
            output="Unsupported class file major version 61 ... class file version 61.0",
        ),
        ToolResult.completed_success(output="BUILD SUCCESSFUL"),
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
# The same pre-flight + bounded retry guards the internal tools ONLY for
# direct callers (tool_recovery resolves the backend delegates and calls
# safe_execute directly). On the facade path the backends pass
# _env_preflight=False and the internal tools run NO probes, NO narration and
# NO retry — the facade owns all of it (single pre-flight ownership). The
# donor's workdir re-targeting ("defaulting to the recommended reactor
# root") and its auto fail_at_end/--continue mutations are NOT ported: PR
# #12's orchestration-layer injection and the BuildTool backends own those.


class MavenScriptedOrch:
    """Canned results for the internal MavenTool; records every command."""

    def __init__(
        self,
        java="17",
        manifest=None,
        build_output="BUILD SUCCESS",
        build_ok=True,
        project_name="proj",
    ):
        self.java = java
        self.manifest = manifest or {}
        self.build_output = build_output
        self.build_ok = build_ok
        self.commands = []
        self.project_name = project_name

    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if cmd.startswith("mvn") and ("test" in cmd or "install" in cmd or "compile" in cmd):
            return {
                "success": self.build_ok,
                "exit_code": 0 if self.build_ok else 1,
                "output": self.build_output,
            }
        if cmd.startswith("find") and "target/classes" in cmd:
            # post-build artifact validation for compile/package/install
            return {
                "success": True,
                "exit_code": 0,
                "output": "/workspace/proj/target/classes/Foo.class",
            }
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
    orch = MavenScriptedOrch(java="11", manifest={}, build_output=ENFORCER_FAIL, build_ok=False)
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj"
    )
    mvn_runs = _mvn_runs(orch, "compile")
    assert len(mvn_runs) == 2  # original + exactly one retry
    assert "retry 1/1" in (result.output or "")
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}


def test_maven_tool_non_version_failure_does_not_retry():
    orch = MavenScriptedOrch(
        java="17",
        manifest={"java_version": "17"},
        build_output="BUILD FAILURE: test failures",
        build_ok=False,
    )
    _internal_maven_tool(orch).execute(command="test", working_directory="/workspace/proj")
    assert len(_mvn_runs(orch, "test")) == 1


def test_maven_tool_scope_warning_when_leaf_targeted():
    orch = MavenScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
    )
    result = _internal_maven_tool(orch).execute(
        command="test", working_directory="/workspace/proj/core"
    )
    assert "[scope]" in (result.output or "")


def test_maven_tool_no_scope_warning_at_recommended_root():
    orch = MavenScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
    )
    result = _internal_maven_tool(orch).execute(command="test", working_directory="/workspace/proj")
    assert "[scope]" not in (result.output or "")


def test_maven_tool_unscoped_invocation_never_retargets_or_warns():
    # The tool's own project-name fallback (not the model) lands below the
    # manifest build_root: not an explicit narrowing, so stay quiet. The
    # donor's re-targeting default is superseded by PR #12's orchestration
    # injection and is deliberately not ported.
    orch = MavenScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace",
        },
    )
    result = _internal_maven_tool(orch).execute(command="test")  # default "/workspace"
    assert "defaulting to the recommended reactor root" not in (result.output or "")
    assert "[scope]" not in (result.output or "")


def test_maven_tool_pl_flag_warns_about_narrowed_scope():
    orch = MavenScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _internal_maven_tool(orch).execute(
        command="test", working_directory="/workspace/proj", extra_args="-pl core"
    )
    assert "[scope]" in (result.output or "")


def test_maven_tool_pl_detection_is_a_token_match():
    # '--no-plugin-updates' contains '-pl' but selects no modules: no [scope].
    orch = MavenScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _internal_maven_tool(orch).execute(
        command="test",
        working_directory="/workspace/proj",
        extra_args="--no-plugin-updates",
    )
    assert "[scope]" not in (result.output or "")


def test_maven_tool_env_preflight_false_runs_no_probes():
    # Facade path: the backend passes _env_preflight=False, so the internal
    # tool must not re-probe the container (no manifest cat, no java
    # -version) and must not narrate.
    orch = MavenScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj", _env_preflight=False
    )
    assert not any("java -version" in c for c in orch.commands)
    assert not any(REQUIREMENTS_PATH in c for c in orch.commands)
    assert "[pre-flight]" not in (result.output or "")
    assert "[scope]" not in (result.output or "")


def test_maven_tool_env_preflight_false_never_retries(monkeypatch):
    # A version-shaped failure on the facade path is the FACADE's retry to
    # make; the internal tool reruns nothing.
    _patch_provision(monkeypatch)
    orch = MavenScriptedOrch(java="11", manifest={}, build_output=ENFORCER_FAIL, build_ok=False)
    result = _internal_maven_tool(orch).execute(
        command="compile", working_directory="/workspace/proj", _env_preflight=False
    )
    assert len(_mvn_runs(orch, "compile")) == 1
    assert "retry 1/1" not in (result.output or "")
    assert "jdk_retry" not in (result.metadata or {})


def test_maven_tool_schema_does_not_expose_env_preflight():
    # _env_preflight is plumbing between the facade and its backends, never a
    # model-facing parameter.
    schema = MavenTool(MavenScriptedOrch()).get_parameter_schema()
    assert "_env_preflight" not in schema.get("properties", {})


class GradleScriptedOrch(MavenScriptedOrch):
    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        # Actual gradle invocations only: build-file probes ('test -f
        # .../build.gradle || ...') contain both 'gradle' and 'build', so a
        # bare substring match would swallow them and feed probes the canned
        # build output. Anchor on the executable at the start of the command.
        if cmd.startswith("gradle") and ("build" in cmd or "test" in cmd or "check" in cmd):
            return {
                "success": self.build_ok,
                "exit_code": 0 if self.build_ok else 1,
                "output": self.build_output,
            }
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
    assert len(runs) == 2  # original + exactly one retry
    assert "retry 1/1" in (result.output or "")


def test_gradle_tool_scope_warning_when_leaf_targeted():
    orch = GradleScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
    )
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj/core"
    )
    assert "[scope]" in (result.output or "")


def test_gradle_tool_no_auto_continue_mutation_on_healthy_reactor():
    # No auto --continue mutation on the internal tool: the BuildTool
    # backends (PR #12) own fail-at-end/--continue wiring; the internal
    # tool only honors an explicit fail_at_end=True.
    orch = GradleScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "root_shape": "healthy_reactor",
            "build_root": "/workspace/proj",
        },
    )
    _internal_gradle_tool(orch).execute(command="build", working_directory="/workspace/proj")
    runs = _gradle_runs(orch, "build")
    assert runs and "--continue" not in runs[0]


def test_gradle_tool_env_preflight_false_runs_no_probes():
    orch = GradleScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj", _env_preflight=False
    )
    assert not any("java -version" in c for c in orch.commands)
    assert not any(REQUIREMENTS_PATH in c for c in orch.commands)
    assert "[pre-flight]" not in (result.output or "")
    assert "[scope]" not in (result.output or "")


def test_gradle_tool_env_preflight_false_never_retries(monkeypatch):
    _patch_provision(monkeypatch)
    fail = "Unsupported class file major version 61 ... class file version 61.0"
    orch = GradleScriptedOrch(java="11", manifest={}, build_output=fail, build_ok=False)
    result = _internal_gradle_tool(orch).execute(
        command="build", working_directory="/workspace/proj", _env_preflight=False
    )
    assert len(_gradle_runs(orch, "build")) == 1
    assert "retry 1/1" not in (result.output or "")


# --- End-to-end: facade + REAL internal MavenTool (single-rerun bound) ------
#
# The regression this pins down: pre-flight/bounded-retry used to run on BOTH
# layers, so a version-shaped failure could trigger one rerun per layer (two
# total) plus duplicate container probes. Through the facade there must be
# exactly ONE manifest read, and a version-driven failure must produce
# exactly ONE rerun — counted on the actual mvn invocations.


class EndToEndOrch(MavenScriptedOrch):
    """MavenScriptedOrch that also answers the facade's marker probes and
    scripts per-invocation mvn results (fail first, then whatever the script
    says)."""

    def __init__(self, mvn_results, **kwargs):
        super().__init__(**kwargs)
        # mvn_results: list of (ok, output); the last entry replays forever so
        # a runaway retry loop shows up as extra counted runs, not IndexError.
        self.mvn_results = list(mvn_results)

    def execute_command(self, cmd, workdir=None, timeout=None):
        if "&& echo exists || echo missing" in cmd:  # facade marker probe
            self.commands.append(cmd)
            exists = "pom.xml" in cmd
            return {"success": True, "exit_code": 0, "output": "exists" if exists else "missing"}
        if cmd.startswith("mvn"):
            self.commands.append(cmd)
            ok, output = (
                self.mvn_results.pop(0) if len(self.mvn_results) > 1 else self.mvn_results[0]
            )
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}
        return super().execute_command(cmd, workdir, timeout)


def _e2e_build_tool(orch):
    return BuildTool(orch, maven_tool=_internal_maven_tool(orch), gradle_tool=ScriptedBackendTool())


def test_exactly_one_version_driven_rerun_end_to_end(monkeypatch):
    _patch_provision(monkeypatch)
    orch = EndToEndOrch(
        [(False, ENFORCER_FAIL), (True, "BUILD SUCCESS")],
        java="11",
        manifest={},
    )
    result = _e2e_build_tool(orch).execute(action="compile", working_directory="/workspace/proj")
    mvn_runs = [c for c in orch.commands if c.startswith("mvn")]
    assert len(mvn_runs) == 2  # original + exactly one rerun
    assert result.succeeded
    assert (result.output or "").count("retry 1/1") == 1
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}


def test_persistent_version_failure_stays_within_single_rerun_bound(monkeypatch):
    # Two successive version-shaped failures must NOT yield a second rerun
    # (the old two-layer wiring could rerun once per layer).
    _patch_provision(monkeypatch)
    orch = EndToEndOrch([(False, ENFORCER_FAIL)], java="11", manifest={})
    result = _e2e_build_tool(orch).execute(action="compile", working_directory="/workspace/proj")
    mvn_runs = [c for c in orch.commands if c.startswith("mvn")]
    assert len(mvn_runs) == 2  # bound: 1 original + 1 rerun
    assert not result.succeeded
    assert (result.output or "").count("retry 1/1") == 1


def test_end_to_end_single_manifest_probe(monkeypatch):
    # Duplicate-probe regression: only the facade reads the requirements
    # manifest; the internal MavenTool (facade path) must not re-read it.
    _patch_provision(monkeypatch)
    orch = EndToEndOrch(
        [(True, "BUILD SUCCESS")],
        java="17",
        manifest={"java_version": "17"},
    )
    result = _e2e_build_tool(orch).execute(action="compile", working_directory="/workspace/proj")
    assert result.succeeded
    manifest_reads = [c for c in orch.commands if REQUIREMENTS_PATH in c]
    assert len(manifest_reads) == 1
    java_probes = [c for c in orch.commands if "java -version" in c]
    assert len(java_probes) == 1  # facade pre-flight only
    assert (result.output or "").count("[pre-flight]") == 0  # matched: silent
