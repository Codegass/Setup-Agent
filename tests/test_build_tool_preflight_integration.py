# tests/test_build_tool_preflight_integration.py
"""BuildTool pre-flight integration (spec §§1b-1c, 3) on the consolidated tool.

Uses a scriptable orchestrator plus scripted backend tools; asserts on the
OBSERVATION text because the narration IS the feature
(transparency-by-construction). The donor branch wired this into the internal
MavenTool/GradleTool; here the consolidated build facade is the hot path.

Single pre-flight ownership: the facade runs pre-flight/bounded-retry/[scope]
and passes _env_preflight=False to the internal tools, so exactly ONE layer
probes the container — and reruns — per build. The internal tools keep the
guarantee only for callers that invoke them directly.

PR #12's orchestration layer owns working-directory injection, so the facade
adds NO workdir defaulting — only the [scope] warning when the model
EXPLICITLY narrows below a healthy reactor's recommended build root (or, for
maven, passes a -pl module selection).
"""

import hashlib
import json
import shlex
from pathlib import Path

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import ContainerFS, add_published_mutable_json

from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.invocation_contracts import action_context, current_contract
from sag.runtime.env_overlay import (
    DEFAULT_OVERLAY_JSON,
    RUNTIME_REQUIREMENT_CONFLICT,
    EnvOverlayStore,
)
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.gradle_tool import GradleTool

pytestmark = pytest.mark.usefixtures(
    "facade_contract_authority",
    "exact_build_facade_authority",
    "exact_internal_runner_authority",
)
from sag.tools.internal.maven_tool import MavenTool


def _requirements(**overrides):
    return complete_build_requirements_v1(
        project_root="/workspace/proj",
        **overrides,
    )


def _published_requirements(manifest):
    """Expand the terse behavior-specific fixture fields into strict live v1."""

    overrides = dict(manifest or {})
    project_root = (
        "/workspace" if overrides.get("build_root") == "/workspace" else "/workspace/proj"
    )
    if overrides.get("root_shape") == "healthy_reactor":
        overrides.setdefault("fail_at_end", True)
        overrides.setdefault("test_fail_at_end", True)
    return complete_build_requirements_v1(project_root=project_root, **overrides)


def _domain_id(root):
    return "dom-" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]


def _domain_requirements(*roots, java_version="11", java_version_source=None):
    domains = [{"root": root, "system": "maven"} for root in roots]
    facts = [
        {
            "domain_id": _domain_id(root),
            "root": root,
            "role": "unknown",
            "environment": "unknown",
            "fact_epoch": 1,
            "system": "maven",
        }
        for root in roots
    ]
    return _requirements(
        java_version=java_version,
        java_version_source=java_version_source,
        root_shape=(
            "single_module" if roots[0] == "/workspace/proj" else "pathological_aggregator"
        ),
        build_root=roots[0],
        build_islands=domains,
        build_domains=domains,
        domain_facts=facts,
    )


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
        self.manifest = _published_requirements(manifest)
        self.markers = set(markers)
        self.project_name = project_name
        self.commands = []
        self.evidence = ContainerFS()
        add_published_mutable_json(
            self,
            self.evidence,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    def read_file(self, path):
        self.commands.append(f"read_file {path}")
        if path == REQUIREMENTS_PATH and self.manifest:
            return {
                "success": True,
                "exit_code": 0,
                "content": json.dumps(self.manifest),
            }
        return {"success": False, "exit_code": 1, "content": ""}

    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if REQUIREMENTS_PATH in cmd and "SAG_NAMED_JSON_RECORD_V1" in cmd:
            return self.evidence(cmd)
        if cmd.startswith("cat ") and "/invocation_contracts/" in cmd:
            return {"success": False, "exit_code": 1, "output": ""}
        if cmd.startswith("test ! -e ") and "/invocation_contracts/" in cmd:
            return {"success": True, "exit_code": 0, "output": ""}
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in cmd:  # build-marker probes
            return self._marker_probe(cmd)
        return {"success": True, "exit_code": 0, "output": ""}

    def execute_control_command(self, cmd, workdir=None, timeout=None, **kwargs):
        # Contract/evidence writers resolve the clean host-control channel and
        # fail closed on a bound plain executor; expose it explicitly.
        del kwargs
        return self.execute_command(cmd, workdir=workdir, timeout=timeout)

    def _marker_probe(self, cmd):
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


class RuntimeScriptedOrch(ScriptedOrch):
    """Adds exact file persistence and a real target SHA for dynamic JDK tests."""

    # This double persists overlay/JDK state in ``files``/``write_file``; the
    # env overlay store selects that explicit in-memory transport only when no
    # callable clean control channel is present, so the inherited one is
    # masked here.
    execute_control_command = None

    def __init__(self, *args, sha="a" * 40, **kwargs):
        super().__init__(*args, **kwargs)
        self.sha = sha
        self.files = {}

    def read_file(self, path):
        self.commands.append(f"read_file {path}")
        if path == REQUIREMENTS_PATH and self.manifest:
            return {
                "success": True,
                "exit_code": 0,
                "content": json.dumps(self.manifest),
            }
        if path in self.files:
            return {"success": True, "exit_code": 0, "content": self.files[path]}
        return None

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "exit_code": 0, "output": ""}

    def execute_command(self, cmd, workdir=None, timeout=None):
        if "rev-parse HEAD" in cmd:
            self.commands.append(cmd)
            return {"success": True, "exit_code": 0, "output": self.sha}
        return super().execute_command(cmd, workdir=workdir, timeout=timeout)


class ContractCapturingBackendTool(ScriptedBackendTool):
    def __init__(self, *results):
        super().__init__(*results)
        self.contracts = []

    def execute(self, **kwargs):
        self.contracts.append(dict(current_contract() or {}))
        return super().execute(**kwargs)


ENFORCER_FAIL = (
    "[ERROR] RequireJavaVersion failed ... Detected JDK Version: "
    "11.0.2 is not in the allowed range [17,). BUILD FAILURE"
)
REAL_MAVEN_JAVA_FAIL = (
    "[ERROR] Required Java version 17 is not met by current version 11.0.27. " "BUILD FAILURE"
)


def _tool(orch, maven=None, gradle=None):
    return BuildTool(
        orch,
        maven_tool=maven or ScriptedBackendTool(),
        gradle_tool=gradle or ScriptedBackendTool(),
    )


def _patch_provision(monkeypatch, ok=True):
    import sag.tools.internal.build_preflight as bp

    def provision(self, version):
        if not ok:
            return None
        self.orchestrator.java = version
        return f"/usr/lib/jvm/java-{version}-openjdk-arm64"

    monkeypatch.setattr(bp.JdkPreflight, "_provision", provision)
    monkeypatch.setattr(bp, "_register_overlay", lambda orchestrator, home, version: True)


def test_matching_jdk_no_narration():
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(action="compile", working_directory="/workspace/proj")
    assert "[pre-flight]" not in (result.output or "")


def _structured_java(runtime="11", release="17", toolchain=False):
    return {
        "runtime": [{"constraint": runtime, "source": "pom.xml:requireJavaVersion"}],
        "compiler_release": release,
        "compiler_source": "pom.xml:maven.compiler.release",
        "compiler_toolchain": toolchain,
    }


def test_facade_keeps_satisfying_jdk_and_freezes_the_observed_runtime():
    orch = ScriptedOrch(
        java="17",
        manifest={
            "java_version": "11",
            "java_version_source": "maven-enforcer",
            "java_requirements": _structured_java(),
        },
    )
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))
    result = _tool(orch, maven=backend).execute(
        action="compile", working_directory="/workspace/proj"
    )
    assert result.succeeded
    assert backend.contracts[0]["effective_jdk"]["major"] == "17"
    assert "requirement_major" not in backend.contracts[0]["effective_jdk"]
    assert not any("apt-get" in command for command in orch.commands)


def test_facade_refuses_exact_runtime_compiler_conflict_before_dispatch():
    orch = ScriptedOrch(
        java="17", manifest={"java_version": "11", "java_requirements": _structured_java("[11]")}
    )
    backend = ScriptedBackendTool()
    result = _tool(orch, maven=backend).execute(
        action="compile", working_directory="/workspace/proj"
    )
    assert result.error_code == "JAVA_CONSTRAINT_CONFLICT"
    assert backend.calls == []
    assert not any("apt-get" in command for command in orch.commands)


def test_facade_keeps_unresolved_constraints_visible_without_installing():
    orch = ScriptedOrch(
        java="17",
        manifest={"java_version": "11", "java_requirements": _structured_java("${supported.java}")},
    )
    result = _tool(orch).execute(action="compile", working_directory="/workspace/proj")
    assert "constraint resolution is unknown" in result.output
    assert not any("apt-get" in command for command in orch.commands)


def test_contract_records_probe_provenance_when_survey_states_no_jdk_requirement():
    orch = ScriptedOrch(java="17", manifest={})
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))

    result = _tool(orch, maven=backend).execute(
        action="compile",
        working_directory="/workspace/proj",
    )

    assert result.succeeded is True
    assert backend.contracts[0]["effective_jdk"] == {
        "major": "17",
        "runtime_authority": "dispatch_probe",
        "provenance": {"source": "java_runtime_probe"},
    }


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


def test_initial_and_jdk_retry_contracts_preserve_repair_context_digest(monkeypatch):
    _patch_provision(monkeypatch)
    backend = ContractCapturingBackendTool(
        ToolResult.completed_failure(output=ENFORCER_FAIL),
        ToolResult.completed_success(output="BUILD SUCCESS"),
    )
    orch = ScriptedOrch(java="11", manifest={})
    params = {"action": "compile", "working_directory": "/workspace/proj"}
    domain_id = "test:/workspace/proj"

    with action_context(
        envelope_id="envelope-repair-jdk",
        intent_source="model",
        intent_id="intent-repair-jdk",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
        trigger_assessment_id="asm-gate-jdk",
        repair_context_id="rcx-123456789abc",
        repair_context_sha256="e" * 64,
    ):
        result = _tool(orch, maven=backend).execute(
            action="compile",
            working_directory="/workspace/proj",
        )

    assert result.succeeded is True
    assert len(backend.contracts) == 2
    assert {contract["repair_context_sha256"] for contract in backend.contracts} == {"e" * 64}
    assert {contract["repair_context_id"] for contract in backend.contracts} == {"rcx-123456789abc"}
    assert backend.contracts[1]["predecessor_contract_id"] == backend.contracts[0]["contract_id"]


def test_real_maven_required_java_wording_triggers_one_runtime_retry(monkeypatch):
    _patch_provision(monkeypatch)
    maven = ScriptedBackendTool(
        ToolResult.completed_failure(output=REAL_MAVEN_JAVA_FAIL, error="failed"),
        ToolResult.completed_success(output="BUILD SUCCESS"),
    )
    orch = ScriptedOrch(java="11", manifest={})

    result = _tool(orch, maven=maven).execute(
        action="compile",
        working_directory="/workspace/proj",
    )

    assert result.succeeded is True
    assert len(maven.calls) == 2
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}


def test_runner_observed_jdk_persists_and_next_invocation_outranks_static_survey(
    monkeypatch,
):
    _patch_provision(monkeypatch)
    manifest = _domain_requirements(
        "/workspace/proj",
        "/workspace/proj/auxiliary",
        java_version_source="maven-compiler",
    )
    orch = RuntimeScriptedOrch(java="11", manifest=manifest)
    first_backend = ContractCapturingBackendTool(
        ToolResult.completed_failure(
            output=REAL_MAVEN_JAVA_FAIL,
            error="failed",
            metadata={"receipt_id": "inv-maven-compile-0001"},
        ),
        ToolResult.completed_success(output="BUILD SUCCESS"),
    )

    first = _tool(orch, maven=first_backend).execute(
        action="compile",
        working_directory="/workspace/proj",
    )

    assert first.succeeded is True
    assert [
        contract["effective_jdk"]["requirement_authority"] for contract in first_backend.contracts
    ] == ["static_survey", "runner_observed"]
    assert first_backend.contracts[0]["effective_jdk"]["major"] == "11"
    assert first_backend.contracts[1]["effective_jdk"]["major"] == "17"
    assert (
        first_backend.contracts[1]["predecessor_contract_id"]
        == first_backend.contracts[0]["contract_id"]
    )
    stored = json.loads(orch.files[DEFAULT_OVERLAY_JSON])
    (runtime_fact,) = stored["tools"]["java"]["runtime_requirements"]
    assert runtime_fact["target_sha"] == orch.sha
    assert runtime_fact["domain_id"] == _domain_id("/workspace/proj")
    assert runtime_fact["domain_root"] == "/workspace/proj"
    assert runtime_fact["required_major"] == "17"
    assert runtime_fact["source_ref"] == "inv-maven-compile-0001"
    assert runtime_fact["active_runtime"]["major"] == "17"

    next_backend = ContractCapturingBackendTool(
        ToolResult.completed_success(output="BUILD SUCCESS")
    )
    next_result = _tool(orch, maven=next_backend).execute(
        action="compile",
        working_directory="/workspace/proj",
    )

    assert next_result.succeeded is True
    assert next_backend.contracts[0]["effective_jdk"]["major"] == "17"
    assert (
        next_backend.contracts[0]["effective_jdk"]["requirement_authority"] == "persisted_dynamic"
    )


def test_same_scope_runtime_conflict_refuses_dispatch_without_latest_wins():
    manifest = _domain_requirements(
        "/workspace/proj",
        "/workspace/proj/auxiliary",
    )
    orch = RuntimeScriptedOrch(java="17", manifest=manifest)
    store = EnvOverlayStore(orch)
    scope = {
        "target_sha": orch.sha,
        "domain_id": _domain_id("/workspace/proj"),
        "domain_root": "/workspace/proj",
    }
    store.record_runtime_requirement(
        "java",
        **scope,
        required_major="17",
        source_ref="inv-1",
        observed_at="2026-08-08T12:00:00Z",
    )
    store.record_runtime_requirement(
        "java",
        **scope,
        required_major="21",
        source_ref="inv-2",
        observed_at="2026-08-08T12:01:00Z",
    )
    backend = ContractCapturingBackendTool()

    result = _tool(orch, maven=backend).execute(
        action="compile",
        working_directory="/workspace/proj",
    )

    assert result.succeeded is False
    assert result.error_code == RUNTIME_REQUIREMENT_CONFLICT
    assert result.metadata["runner_dispatched"] is False
    assert backend.calls == []


def test_sibling_domain_does_not_inherit_dynamic_jdk_requirement(monkeypatch):
    _patch_provision(monkeypatch)
    manifest = _domain_requirements(
        "/workspace/proj/a",
        "/workspace/proj/b",
        java_version_source="maven-compiler",
    )
    orch = RuntimeScriptedOrch(
        java="17",
        manifest=manifest,
        markers=("/workspace/proj/a/pom.xml", "/workspace/proj/b/pom.xml"),
    )
    EnvOverlayStore(orch).record_runtime_requirement(
        "java",
        target_sha=orch.sha,
        domain_id=_domain_id("/workspace/proj/a"),
        domain_root="/workspace/proj/a",
        required_major="17",
        source_ref="inv-a",
        observed_at="2026-08-08T12:00:00Z",
    )
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))

    result = _tool(orch, maven=backend).execute(
        action="compile",
        working_directory="/workspace/proj/b",
    )

    assert result.succeeded is True
    assert backend.contracts[0]["effective_jdk"]["major"] == "11"
    assert backend.contracts[0]["effective_jdk"]["requirement_authority"] == "static_survey"
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


# ---------------------------------------------------------------------------
# Task #60 shape (c): a runner that diagnosed its own JDK need, and an engine
# that did not act on it, still names the move that ends it.
#
# geode d2r4 (logs/d2r4-20260815/slices/geode.md): the Gradle plugin answered
# "Java version 17 or later required, but was 11.0.31" twice — slice 3 via bash
# and slice 8 through build(action='test') — and `classify_version_error` knows
# none of that wording, so nothing was re-provisioned and nothing was said. The
# model's answer was `project(action='env', tool='gradle',
# executable='/usr/lib/jvm/java-17-openjdk-amd64/bin/java')`: a guessed amd64
# path on an arm64 host (slice 5, ENV_EXECUTABLE_NOT_FOUND), while
# `project(action='provision', java_version='17')` — the call that had already
# installed Java 11 in the same run — was never tried. lucene d2r4 is the same
# family in the Gradle launcher's wording: "ERROR: java version must be >= 21
# and <= 24, your version: 17".
# ---------------------------------------------------------------------------

GEODE_PLUGIN_FAIL = (
    "Caused by: org.gradle.api.GradleException: Java version 17 or later required, "
    "but was 11.0.31\n\t... 191 more\n\nBUILD FAILED in 17s"
)
LUCENE_LAUNCHER_FAIL = "ERROR: java version must be >= 21 and <= 24, your version: 17"


def _steering_lines(result):
    return [line for line in (result.output or "").splitlines() if "[toolchain]" in line]


def test_a_runner_that_names_the_java_it_needs_gets_the_provision_call_named():
    gradle = ScriptedBackendTool(ToolResult.completed_failure(output=GEODE_PLUGIN_FAIL))
    orch = ScriptedOrch(
        java="11",
        manifest={"test_system": "gradle"},
        markers=("/workspace/proj/build.gradle",),
    )

    result = _tool(orch, gradle=gradle).execute(action="test", working_directory="/workspace/proj")

    assert len(gradle.calls) == 1, "the sentence is steering, never a second dispatch"
    lines = _steering_lines(result)
    assert len(lines) == 1, "exactly one sentence"
    assert "project(action='provision', java_version='17')" in lines[0]
    assert "action='env'" not in lines[0], "never the registration that failed live"
    assert result.metadata["runner_java_requirement"] == {
        "required_major": "17",
        "source": "runner_output",
    }


def test_the_launcher_wording_is_read_as_the_same_requirement():
    gradle = ScriptedBackendTool(ToolResult.completed_failure(output=LUCENE_LAUNCHER_FAIL))
    orch = ScriptedOrch(java="17", manifest={}, markers=("/workspace/proj/build.gradle",))

    result = _tool(orch, gradle=gradle).execute(
        action="compile", working_directory="/workspace/proj"
    )

    assert "project(action='provision', java_version='21')" in _steering_lines(result)[0]


def test_the_engine_that_switched_the_runtime_itself_does_not_also_steer(monkeypatch):
    """A retry that already moved to the named major has done the thing the
    sentence would ask for. Saying it anyway would send the model to re-provision
    what it is now running on."""
    _patch_provision(monkeypatch)
    maven = ScriptedBackendTool(
        ToolResult.completed_failure(output=REAL_MAVEN_JAVA_FAIL),
        ToolResult.completed_failure(output=REAL_MAVEN_JAVA_FAIL),
    )
    orch = ScriptedOrch(java="11", manifest={})

    result = _tool(orch, maven=maven).execute(action="compile", working_directory="/workspace/proj")

    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}
    assert _steering_lines(result) == []


def test_a_reprovision_that_could_not_happen_still_names_the_call(monkeypatch):
    """The retry is the engine's own move and it failed; the model's move is
    still available and is now the only one left."""
    _patch_provision(monkeypatch, ok=False)
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=REAL_MAVEN_JAVA_FAIL))
    orch = ScriptedOrch(java="11", manifest={})

    result = _tool(orch, maven=maven).execute(action="compile", working_directory="/workspace/proj")

    assert "jdk_retry" not in (result.metadata or {})
    assert "project(action='provision', java_version='17')" in _steering_lines(result)[0]


def test_a_build_that_succeeded_is_never_steered():
    maven = ScriptedBackendTool(
        ToolResult.completed_success(output="BUILD SUCCESS on Java 17 or later required")
    )
    orch = ScriptedOrch(java="17", manifest={})

    result = _tool(orch, maven=maven).execute(action="compile", working_directory="/workspace/proj")

    assert _steering_lines(result) == []


def test_a_runner_asking_for_the_major_already_active_is_not_steered():
    """The runtime already IS 17; whatever failed, provisioning 17 is not it."""
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=REAL_MAVEN_JAVA_FAIL))
    orch = ScriptedOrch(java="17", manifest={})

    result = _tool(orch, maven=maven).execute(action="compile", working_directory="/workspace/proj")

    assert _steering_lines(result) == []


# A plugin banner naming the Java IT needs is not the build naming the Java the
# BUILD needs. The steering wordings are floors; a floor the running JDK already
# clears cannot be the reason this build failed, and a sentence that names it
# would send the model to swap the JDK downwards under a test failure.
ANIMAL_SNIFFER_NOISE = (
    "[INFO] --- animal-sniffer-maven-plugin:1.23:check (default) @ core ---\n"
    "[INFO] the animal-sniffer plugin requires Java 8 to run\n"
    "[ERROR] Failed to execute goal surefire:test (default-test) on project core: "
    "There are test failures.\nBUILD FAILURE"
)


def test_a_floor_the_runtime_already_clears_never_steers_a_downgrade():
    maven = ScriptedBackendTool(ToolResult.completed_failure(output=ANIMAL_SNIFFER_NOISE))
    orch = ScriptedOrch(java="17", manifest={})

    result = _tool(orch, maven=maven).execute(action="test", working_directory="/workspace/proj")

    assert len(maven.calls) == 1
    assert _steering_lines(result) == []
    assert "runner_java_requirement" not in (result.metadata or {})


def test_the_generic_wording_still_names_the_provision_when_it_asks_for_more():
    """Same loose wording, the other direction: 11 does not clear a Java 17
    floor, so the sentence and the typed fact are both earned."""
    gradle = ScriptedBackendTool(
        ToolResult.completed_failure(
            output="FAILURE: Build failed.\n> the shadow plugin requires Java 17 to run"
        )
    )
    orch = ScriptedOrch(
        java="11",
        manifest={"test_system": "gradle"},
        markers=("/workspace/proj/build.gradle",),
    )

    result = _tool(orch, gradle=gradle).execute(action="test", working_directory="/workspace/proj")

    assert len(gradle.calls) == 1
    assert "project(action='provision', java_version='17')" in _steering_lines(result)[0]
    assert result.metadata["runner_java_requirement"]["required_major"] == "17"


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


def test_default_workdir_is_not_retargeted_from_project_name():
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
    result = _tool(orch).execute(action="compile")  # default "/workspace"
    assert result.operation_outcome.value == "unknown"
    assert result.error_code == "BUILD_SYSTEM_NOT_DETECTED"
    assert result.metadata == {
        "runner_dispatched": False,
        "working_directory": "/workspace",
    }
    assert result.facts["working_directory"] == "/workspace"
    assert not any("/workspace/proj/" in command for command in orch.commands)


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


@pytest.mark.parametrize(
    ("marker", "action", "backend_name"),
    (
        ("/workspace/proj/pom.xml", "compile", "maven"),
        ("/workspace/proj/build.gradle", "test", "gradle"),
    ),
)
def test_backends_delegate_with_env_preflight_disabled(marker, action, backend_name):
    # Single ownership: the facade already ran the pre-flight, so the
    # backends must tell the internal tools to skip theirs.
    maven = ScriptedBackendTool()
    gradle = ScriptedBackendTool()
    orch = ScriptedOrch(
        java="17",
        manifest={
            "java_version": "17",
            "test_system": backend_name,
        },
        markers=(marker,),
    )
    _tool(orch, maven=maven, gradle=gradle).execute(
        action=action, working_directory="/workspace/proj"
    )
    selected = maven if backend_name == "maven" else gradle
    assert selected.calls[0]["_env_preflight"] is False


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
# direct callers. On the facade path the backends pass
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
        *,
        publish_manifest=True,
    ):
        self.java = java
        self.manifest = _published_requirements(manifest)
        self.build_output = build_output
        self.build_ok = build_ok
        self.commands = []
        self.project_name = project_name
        self.evidence = ContainerFS()
        if publish_manifest:
            add_published_mutable_json(
                self,
                self.evidence,
                path=REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=self.manifest,
            )

    def read_file(self, path):
        self.commands.append(f"read_file {path}")
        if path == REQUIREMENTS_PATH and self.manifest:
            return {
                "success": True,
                "exit_code": 0,
                "content": json.dumps(self.manifest),
            }
        return {"success": False, "exit_code": 1, "content": ""}

    def execute_command(self, cmd, workdir=None, timeout=None):
        self.commands.append(cmd)
        if REQUIREMENTS_PATH in cmd and "SAG_NAMED_JSON_RECORD_V1" in cmd:
            return self.evidence(cmd)
        if cmd.startswith("cat ") and "/invocation_contracts/" in cmd:
            return {"success": False, "exit_code": 1, "output": ""}
        if cmd.startswith("test ! -e ") and "/invocation_contracts/" in cmd:
            return {"success": True, "exit_code": 0, "output": ""}
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
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
    # Internal backends receive the facade's frozen cwd and cannot use the
    # Docker project name to replace it.
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
    assert not any("/workspace/proj/pom.xml" in command for command in orch.commands)


def test_gradle_tool_unscoped_invocation_never_retargets_from_project_name():
    orch = GradleScriptedOrch(java="17", project_name="proj")

    result = _internal_gradle_tool(orch).execute(command="build")

    assert not any("/workspace/proj/build.gradle" in command for command in orch.commands)
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
        if REQUIREMENTS_PATH in cmd and "SAG_NAMED_JSON_RECORD_V1" in cmd:
            return self.evidence(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if cmd in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
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


@pytest.mark.parametrize("runner", ("maven", "gradle"))
def test_direct_internal_runner_rejects_an_unpublished_manifest_before_probing(runner):
    if runner == "maven":
        orch = MavenScriptedOrch(publish_manifest=False)
        result = _internal_maven_tool(orch).execute(
            command="compile", working_directory="/workspace/proj"
        )
    else:
        orch = GradleScriptedOrch(publish_manifest=False)
        result = _internal_gradle_tool(orch).execute(
            command="build", working_directory="/workspace/proj"
        )

    assert result.error_code == "BUILD_REQUIREMENTS_UNAVAILABLE"
    assert result.metadata["runner_dispatched"] is False
    assert result.metadata["blocker_owner"] == "harness"
    assert not any("java -version" in command for command in orch.commands)
    assert not any(command.startswith(("mvn", "gradle")) for command in orch.commands)


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


class RequirementRecordingManager:
    def __init__(self):
        self.specs = []

    def resolve(self, spec, working_directory="/workspace"):
        self.specs.append((spec, working_directory))
        return None


def test_real_build_and_maven_classes_preserve_requirement_at_resolution_seam():
    orch = EndToEndOrch(
        [(True, "BUILD SUCCESS")],
        java="17",
        manifest={},
    )
    manager = RequirementRecordingManager()
    internal = MavenTool(orch, toolchain_manager=manager)
    build = BuildTool(orch, maven_tool=internal)

    result = build.execute(
        action="compile",
        working_directory="/workspace/proj",
        maven_version_requirement="[3.9,4.0)",
    )

    assert result.succeeded is False
    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    spec, workdir = manager.specs[0]
    assert workdir == "/workspace/proj"
    assert spec.version_requirement is not None
    assert spec.version_requirement.raw == "[3.9,4.0)"
    assert spec.version_requirement.kind == "range"
    # The refusal then asks one further question — what resolves WITHOUT the
    # stated requirement — because that is the only honest basis for naming the
    # registered runtime and the omit-it exit (task #60 shape (b), jackrabbit
    # d2r4). It is a resolution, never a dispatch, and the requirement itself
    # crosses the seam unweakened above.
    assert [spec.version_requirement for spec, _ in manager.specs[1:]] == [None]


def test_explicit_executor_never_falls_back_to_a_different_root_marker():
    orch = ScriptedOrch(manifest={"test_system": "gradle"}, markers=("/workspace/proj/pom.xml",))
    maven = ScriptedBackendTool()
    gradle = ScriptedBackendTool()
    result = _tool(orch, maven=maven, gradle=gradle).execute(
        action="test",
        system="gradle",
        source_command="./gradlew test",
        working_directory="/workspace/proj",
    )
    assert not result.succeeded
    assert not maven.calls and not gradle.calls


def test_make_command_is_rejected_before_any_backend_dispatch():
    orch = ScriptedOrch(
        manifest={"test_system": "gradle"}, markers=("/workspace/proj/build.gradle",)
    )
    gradle = ScriptedBackendTool()
    result = _tool(orch, gradle=gradle).execute(
        action="test",
        args="client-unit-test",
        system="gradle",
        source_command="make client-unit-test",
        working_directory="/workspace/proj",
    )
    assert result.error_code == "BUILD_PARAMETER_INVALID"
    assert gradle.calls == []


def test_explicit_maven_command_preserves_clean_before_install():
    orch = ScriptedOrch()
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))
    result = _tool(orch, maven=backend).execute(
        action="install",
        args="clean -DskipTests",
        system="maven",
        source_command="./mvnw clean install -DskipTests",
        working_directory="/workspace/proj",
    )
    assert result.succeeded
    assert backend.contracts[0]["expected_argv"].endswith("clean install -DskipTests")
    assert backend.calls[0]["command"] == "install"
    assert backend.calls[0]["_source_argv"] == ["clean", "install", "-DskipTests"]


def test_real_source_mismatch_refuses_dispatch_with_actionable_feedback():
    path = Path(__file__).parent / "fixtures/d3r1_remediation/commons-cli-plan-params.json"
    params = json.loads(path.read_text())["build_params"]
    orch = ScriptedOrch()
    backend = ScriptedBackendTool()
    result = _tool(orch, maven=backend).execute(**params)
    assert result.error_code == "BUILD_PARAMETER_INVALID"
    assert (
        "expected args='--errors --show-version --batch-mode --no-transfer-progress clean'"
        in result.output
    )
    assert "extra tokens=['verify']" in result.output
    assert backend.calls == []


def test_explicit_failsafe_verify_is_not_narrowed_to_test():
    orch = ScriptedOrch()
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))
    result = _tool(orch, maven=backend).execute(
        action="verify",
        args="-Dit.test=SmokeIT",
        system="maven",
        source_command="./mvnw verify -Dit.test=SmokeIT",
        working_directory="/workspace/proj",
    )
    assert result.succeeded
    assert "verify -Dit.test=SmokeIT" in backend.contracts[0]["expected_argv"]


def test_official_maven_multiphase_source_matches_real_runner_and_contract():
    source = "mvn -B -f pom.xml clean verify install -P-use-toolchains,nodoclint"
    orch = EndToEndOrch([(True, "BUILD SUCCESS")], java="17", manifest={})
    internal = _internal_maven_tool(orch)
    captured = []
    original = internal.execute

    def capture(**kwargs):
        captured.append(dict(current_contract() or {}))
        return original(**kwargs)

    internal.execute = capture
    result = BuildTool(orch, maven_tool=internal).execute(
        action="verify",
        system="maven",
        source_command=source,
        args="-B -f pom.xml clean install -P-use-toolchains,nodoclint",
        working_directory="/workspace/proj",
    )
    assert result.succeeded
    actual = next(command for command in orch.commands if command.startswith("mvn"))
    assert shlex.split(actual)[1:] == shlex.split(captured[0]["expected_argv"])
    assert shlex.split(actual)[1:] == shlex.split(source)[1:]
    assert captured[0]["effective_action"] == "verify"
    assert captured[0]["requested_call"]["params"]["source_command"] == source


def test_explicit_full_default_goal_and_flags_reach_maven_runner_and_contract_unchanged():
    flags = "--errors --show-version --batch-mode --no-transfer-progress"
    quality_goals = (
        "apache-rat:check japicmp:cmp checkstyle:check spotbugs:check pmd:check javadoc:javadoc"
    )
    source = f"mvn {flags} clean verify {quality_goals}"
    args = f"{flags} clean {quality_goals}"
    orch = EndToEndOrch([(True, "BUILD SUCCESS")], java="17", manifest={})
    internal = _internal_maven_tool(orch)
    captured = []
    original = internal.execute

    def capture(**kwargs):
        captured.append(dict(current_contract() or {}))
        return original(**kwargs)

    internal.execute = capture
    result = BuildTool(orch, maven_tool=internal).execute(
        action="verify",
        system="maven",
        source_command=source,
        args=args,
        working_directory="/workspace/proj",
    )
    assert result.succeeded
    actual = next(command for command in orch.commands if command.startswith("mvn"))
    assert shlex.split(actual)[1:] == shlex.split(captured[0]["expected_argv"])
    assert shlex.split(actual)[1:] == shlex.split(source)[1:]
    assert captured[0]["effective_action"] == "verify"
    assert captured[0]["requested_call"]["params"]["source_command"] == source
    assert captured[0]["requested_call"]["params"]["args"] == args


def test_explicit_gradle_scoped_source_records_the_actual_task():
    orch = ScriptedOrch(
        manifest={"test_system": "gradle"}, markers=("/workspace/proj/build.gradle",)
    )
    backend = ContractCapturingBackendTool(ToolResult.completed_success(output="BUILD SUCCESS"))
    result = _tool(orch, gradle=backend).execute(
        action="test",
        system="gradle",
        source_command="./gradlew :client:test --tests SmokeTest",
        args=":client:test --tests SmokeTest",
        working_directory="/workspace/proj",
    )
    assert result.succeeded
    assert backend.contracts[0]["effective_action"] == ":client:test"
    assert backend.calls[0]["tasks"] == ":client:test"
    assert shlex.split(backend.contracts[0]["expected_argv"]) == [
        ":client:test",
        "--tests",
        "SmokeTest",
    ]


def test_explicit_python_client_root_is_not_overridden_by_jvm_root_declaration():
    orch = ScriptedOrch(
        manifest={"test_system": "gradle", "test_root": "/workspace/proj"},
        markers=("/workspace/proj/build.gradle", "/workspace/proj/client/pyproject.toml"),
    )
    python = ContractCapturingBackendTool(ToolResult.completed_success(output="1 passed"))
    gradle = ScriptedBackendTool()
    result = BuildTool(orch, python_tool=python, gradle_tool=gradle).execute(
        action="test",
        system="python",
        source_command="uv run --active pytest tests/",
        args="tests/",
        working_directory="/workspace/proj/client",
    )
    assert result.succeeded
    assert not gradle.calls
    assert python.calls[0]["working_directory"] == "/workspace/proj/client"


def test_explicit_gradle_source_matches_real_runner_without_hidden_test_flags():
    class SourceGradleOrch(GradleScriptedOrch):
        def execute_command(self, cmd, workdir=None, timeout=None):
            if "&& echo exists || echo missing" in cmd:
                self.commands.append(cmd)
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": "exists" if "build.gradle" in cmd else "missing",
                }
            return super().execute_command(cmd, workdir, timeout)

    orch = SourceGradleOrch(
        java="17",
        manifest={"test_system": "gradle"},
        build_output="> Task :client:test\nBUILD SUCCESSFUL",
    )
    internal = _internal_gradle_tool(orch)
    captured = []
    original = internal.execute

    def capture(**kwargs):
        captured.append(dict(current_contract() or {}))
        return original(**kwargs)

    internal.execute = capture
    result = BuildTool(orch, gradle_tool=internal).execute(
        action="test",
        system="gradle",
        source_command="./gradlew :client:test --tests SmokeTest",
        args=":client:test --tests SmokeTest",
        working_directory="/workspace/proj",
    )
    actual = next(command for command in orch.commands if command.startswith("gradle "))
    assert shlex.split(actual)[1:] == shlex.split(captured[0]["expected_argv"])
    assert "ignoreFailures" not in actual
    assert captured[0]["effective_action"] == ":client:test"
    assert result.metadata["receipt_id"].startswith("inv-gradle-")
