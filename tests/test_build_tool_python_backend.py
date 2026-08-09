# tests/test_build_tool_python_backend.py
"""PythonBackend on the consolidated BuildTool (spec §4 growth law + plan Task 5).

Contract:
- marker priority: a JVM repo with a stray requirements.txt stays JVM
  (python markers probe AFTER maven/gradle); a pyproject-only dir is python;
- verb delegation: deps->setup_env, compile->compile, test->test,
  package->build, install->build all reach python_tool.execute;
- the envelope reports facts["system"] == "python";
- the facade's JDK pre-flight is SKIPPED for python (PythonPreflight already
  runs inside python_tool.setup_env — running it twice would double-provision).

Agent wiring (agent.py constructing PythonTool and passing it to BuildTool)
is verified by reading, per the plan; the direct-construction tests below
cover the registration path itself.
"""

import json
import shlex

import pytest

from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.invocation_contracts import (
    action_context,
    current_contract,
    python_facade_dispatch_matches,
)
from sag.tools.base import ToolResult
from sag.tools.build.backends import PythonBackend
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from tests.build_requirements_fakes import complete_build_requirements_v1
from tests.container_evidence_fakes import ContainerFS, add_published_mutable_json


_UNSET = object()


class FakePythonTool:
    """Stands in for the internal PythonTool: records calls, scripted result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult.completed_success(output="ok")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class ContractCheckingPythonTool(FakePythonTool):
    """Emulate PythonTool's public-to-operation authority gate exactly."""

    def execute(
        self,
        operation,
        working_directory="/workspace",
        args=None,
        timeout=600,
        native=None,
    ):
        kwargs = {
            "operation": operation,
            "working_directory": working_directory,
            "args": args,
            "timeout": timeout,
            "native": native,
        }
        self.calls.append(kwargs)
        if not python_facade_dispatch_matches(
            current_contract(),
            operation=operation,
            working_directory=working_directory,
            internal_params=kwargs,
        ):
            return ToolResult.completed_failure(
                output="semantic authority mismatch",
                error="semantic authority mismatch",
                error_code="CONTRACT_AUTHORITY_MISSING",
                metadata={"runner_dispatched": False},
            )
        return self.result


class FakeBackendTool:
    """Stands in for MavenTool/GradleTool: records calls, scripted result."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class ScriptedOrch:
    """Answers `test -f <path>` marker probes from a set of existing paths,
    the manifest read, and `java -version`; records every command so
    pre-flight activity is observable."""

    def __init__(self, existing_paths, manifest=None, java="17"):
        self.existing_paths = set(existing_paths)
        # The strict live reader validates the published head, so legacy
        # partial fixtures ({} or {"java_version": ...}) become overrides on a
        # complete v1 manifest for the probed project root.
        self.manifest = complete_build_requirements_v1(
            project_root="/workspace/p",
            **(manifest or {}),
        )
        self.java = java
        self.commands = []
        self.filesystem = ContainerFS()
        add_published_mutable_json(
            self,
            self.filesystem,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append(command)
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.filesystem(command)
        if "java -version" in command:
            return {"success": True, "exit_code": 0, "output": f'openjdk version "{self.java}.0.1"'}
        if command in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "test -f" in command:
            tokens = shlex.split(command)
            try:
                path = tokens[tokens.index("-f") + 1]
            except (ValueError, IndexError):
                path = ""
            output = "exists" if path in self.existing_paths else "missing"
            return {"success": True, "exit_code": 0, "output": output}
        return self.filesystem(command)

    def execute_control_command(self, command, **kwargs):
        """Host-owned control channel (production surface for evidence I/O)."""
        return self.execute_command(command, **kwargs)


def _tool(existing_paths, python=None, maven=None, gradle=None, manifest=None):
    orch = ScriptedOrch(existing_paths, manifest=manifest)
    tool = BuildTool(
        orch,
        maven_tool=maven,
        gradle_tool=gradle,
        python_tool=python,
    )
    return tool, orch


def _execute(
    tool,
    *,
    action,
    working_directory="/workspace/p",
    args=None,
    timeout=None,
    exact_timeout=_UNSET,
):
    """Run one facade call under the exact engine-owned public intent."""

    params = {"action": action, "working_directory": working_directory}
    if args is not None:
        params["args"] = args
    if exact_timeout is not _UNSET:
        params["timeout"] = exact_timeout
    elif timeout is not None:
        params["timeout"] = timeout
    domain_id = f"test:{working_directory}"
    with action_context(
        envelope_id=f"envelope-python-{action}-{len(params)}",
        intent_source="model",
        intent_id=f"intent-python-{action}-{len(params)}",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    ):
        return tool.execute(
            action=action,
            args=args,
            working_directory=working_directory,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Marker priority
# ---------------------------------------------------------------------------


def test_jvm_repo_with_stray_requirements_txt_stays_maven():
    maven = FakeBackendTool()
    python = FakePythonTool()
    tool, _ = _tool(
        {"/workspace/p/pom.xml", "/workspace/p/requirements.txt"},
        python=python,
        maven=maven,
    )

    result = _execute(tool, action="compile")

    assert result.facts["system"] == "maven"
    assert maven.calls, "maven backend must run"
    assert not python.calls, "python backend must NOT run on a JVM repo"


def test_pyproject_only_selects_python():
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action="compile")

    assert result.facts["system"] == "python"
    assert python.calls


# ---------------------------------------------------------------------------
# Verb delegation table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb,operation",
    [
        ("deps", "setup_env"),
        ("compile", "compile"),
        ("test", "test"),
        ("package", "build"),
        ("install", "build"),
    ],
)
def test_verb_reaches_python_tool_with_mapped_operation(verb, operation):
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action=verb)

    assert python.calls, f"{verb} must delegate to python_tool"
    call = python.calls[0]
    assert call["operation"] == operation
    assert call["working_directory"] == "/workspace/p"
    assert result.facts["system"] == "python"
    assert result.facts["action"] == verb


def test_args_and_timeout_pass_through():
    python = FakePythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    _execute(tool, action="test", args="-k smoke", timeout=120)

    call = python.calls[0]
    assert call["args"] == "-k smoke"
    assert call["timeout"] == 120


def test_python_materializer_preserves_explicit_values_without_truthiness_rewrite():
    params = PythonBackend(FakePythonTool()).materialize(
        "test",
        "",
        "/workspace/p",
        0,
    )

    assert params["args"] == ""
    assert params["timeout"] == 0


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_python_test_envelope_reports_system_python():
    python = FakePythonTool(ToolResult.completed_success(output="3 passed"))
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action="test")

    assert result.succeeded
    assert result.operation_outcome.value == "success"
    assert result.facts["system"] == "python"


def test_no_python_backend_registered_is_an_honest_failure():
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=None)

    result = _execute(tool, action="test")

    assert not result.succeeded
    assert result.operation_outcome.value == "failed"


# ---------------------------------------------------------------------------
# Pre-flight routing: python skips the JDK pre-flight
# ---------------------------------------------------------------------------


def test_python_system_skips_jdk_preflight():
    # Even a manifest that declares a java_version (mixed repo) must not
    # trigger the JDK pre-flight when the selected system is python:
    # PythonPreflight runs inside python_tool.setup_env instead.
    python = FakePythonTool()
    tool, orch = _tool(
        {"/workspace/p/pyproject.toml"},
        python=python,
        manifest={"java_version": "17"},
    )

    result = _execute(tool, action="test")

    assert not any(
        "java -version" in c for c in orch.commands
    ), "JdkPreflight must not probe the JVM on a python project"
    assert "[pre-flight]" not in (result.output or "")


def test_maven_system_still_runs_jdk_preflight():
    maven = FakeBackendTool()
    tool, orch = _tool({"/workspace/p/pom.xml"}, maven=maven, manifest={"java_version": "17"})

    _execute(tool, action="test")

    assert any("java -version" in c for c in orch.commands)


def test_python_default_timeout_null_materializes_to_internal_600_authority():
    python = ContractCheckingPythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action="test", exact_timeout=None)

    assert result.succeeded
    assert python.calls[0]["timeout"] == 600


def test_python_explicit_timeout_is_exact_semantic_authority():
    python = ContractCheckingPythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action="test", timeout=37)

    assert result.succeeded
    assert python.calls[0]["timeout"] == 37


def test_python_changed_explicit_timeout_has_zero_runner_authority():
    class TimeoutChangingPythonTool(ContractCheckingPythonTool):
        def execute(self, **kwargs):
            kwargs["timeout"] = 38
            return super().execute(**kwargs)

    python = TimeoutChangingPythonTool()
    tool, _ = _tool({"/workspace/p/pyproject.toml"}, python=python)

    result = _execute(tool, action="test", timeout=37)

    assert not result.succeeded
    assert result.error_code == "CONTRACT_AUTHORITY_MISSING"
    assert result.metadata["runner_dispatched"] is False
