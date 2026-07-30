import base64
import json
import shlex

import pytest

from sag.runtime.env_overlay import (
    DEFAULT_OVERLAY_JSON,
    DEFAULT_OVERLAY_SCRIPT,
    EnvOverlayStore,
)
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.env_tool import EnvTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.toolchain_manager import ToolchainManager, ToolchainSpec
from sag.tools.project_tool import ProjectTool


class FakeEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("realpath -e -- "):
            return {
                "success": True,
                "output": shlex.split(command)[-1],
                "exit_code": 0,
            }
        if command.startswith("test -x "):
            return {"success": True, "output": "EXISTS\n", "exit_code": 0}
        if command.endswith(" -version"):
            if "apache-maven-3.9.9" in command:
                version = "3.9.9"
            elif "/usr/bin/mvn" in command:
                version = "3.8.7"
            else:
                version = "3.9.6"
            return {
                "success": True,
                "output": f"Apache Maven {version}\nMaven home: /opt/maven",
                "exit_code": 0,
            }
        return {"success": True, "output": "", "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def test_register_activate_writes_json_and_shell_script():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")
    overlay = store.activate("maven", "/opt/apache-maven-3.9.9/bin/mvn")

    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert overlay["tools"]["maven"]["active"] == "/opt/apache-maven-3.9.9/bin/mvn"
    assert stored["tools"]["maven"]["active"] == "/opt/apache-maven-3.9.9/bin/mvn"
    assert (
        stored["tools"]["maven"]["candidates"]["/opt/apache-maven-3.9.9/bin/mvn"]["version"]
        == "3.9.9"
    )
    assert (
        "export PATH=/opt/apache-maven-3.9.9/bin:$PATH"
        in orchestrator.files[DEFAULT_OVERLAY_SCRIPT]
    )


def test_block_records_exact_executable_without_blocking_other_versions():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register("maven", "/usr/bin/mvn", version="3.6.3")
    store.block(
        "maven",
        "/usr/bin/mvn",
        version="3.6.3",
        requirement="[3.9,)",
        reason="Project requires Maven 3.9+",
    )
    store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")

    assert store.is_blocked("maven", "/usr/bin/mvn") is True
    assert store.is_blocked("maven", "/opt/apache-maven-3.9.9/bin/mvn") is False


def test_invalid_overlay_json_recovers_to_empty_state():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = "{not valid json"
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()

    assert inspected["tools"] == {}
    assert inspected["warnings"]

    store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")

    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert stored["version"] == 1
    assert (
        stored["tools"]["maven"]["candidates"]["/opt/apache-maven-3.9.9/bin/mvn"]["version"]
        == "3.9.9"
    )
    assert "warnings" not in stored


def test_env_tool_register_activate_inspect():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    registered = tool.execute(
        {
            "action": "register",
            "tool": "maven",
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
            "version": "3.9.9",
        }
    )
    activated = tool.execute(
        {
            "action": "activate",
            "tool": "maven",
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
        }
    )
    inspected = tool.execute({"action": "inspect"})

    assert registered.succeeded is True
    assert activated.succeeded is True
    assert inspected.succeeded is True
    assert inspected.raw_data["overlay"]["tools"]["maven"]["active"] == (
        "/opt/apache-maven-3.9.9/bin/mvn"
    )


def test_env_tool_register_with_activate_confirms_exact_active_candidate():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="3.9.9",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"] == {
        "executable": "/opt/apache-maven-3.9.9/bin/mvn",
        "version": "3.9.9",
        "source": "agent_registered",
        "env": {},
        "path_prepend": ["/opt/apache-maven-3.9.9/bin"],
    }
    assert result.raw_data["measured_version"] == "3.9.9"


class ReadLimitedEnvOverlayOrchestrator(FakeEnvOverlayOrchestrator):
    """Fail any read after the register transaction's own verified readback."""

    def __init__(self):
        super().__init__()
        self.read_calls = 0

    def read_file(self, path):
        self.read_calls += 1
        if self.read_calls > 6:
            # The tripwire, deliberately a FAILURE and not absence: under the
            # §3.9 contract a seventh read raises out of the exact path, so a
            # transaction that re-reads past its own verified snapshot cannot
            # quietly see an empty overlay — it fails loudly.
            return {
                "success": False,
                "content": "",
                "exit_code": 1,
            }
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure.
            return None
        return {
            "success": True,
            "content": self.files[path],
            "exit_code": 0,
        }


def test_register_activation_uses_same_transaction_readback_snapshot():
    orchestrator = ReadLimitedEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"]["executable"] == ("/opt/apache-maven-3.9.9/bin/mvn")
    assert orchestrator.read_calls == 6


def test_project_env_facade_replaces_stale_active_runtime_atomically():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)
    project = ProjectTool(env_tool=EnvTool(orchestrator, store=store))

    result = project.safe_execute(
        action="env",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="3.9.9",
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"]["executable"] == ("/opt/apache-maven-3.9.9/bin/mvn")
    assert store.active_candidate("maven")["version"] == "3.9.9"
    assert orchestrator.files[DEFAULT_OVERLAY_SCRIPT].splitlines()[-1] == (
        "export PATH=/opt/apache-maven-3.9.9/bin:$PATH"
    )


def test_env_tool_block_defaults_to_build_error_without_source():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    block_from_dict = tool.execute(
        {
            "action": "block",
            "tool": "maven",
            "executable": "/usr/bin/mvn",
        }
    )
    block_from_kwargs = tool.execute("block", tool="gradle", executable="/usr/bin/gradle")

    assert block_from_dict.succeeded is True
    assert block_from_kwargs.succeeded is True
    assert (
        block_from_dict.raw_data["overlay"]["tools"]["maven"]["blocked"][0]["source"]
        == "build_error"
    )
    assert (
        block_from_kwargs.raw_data["overlay"]["tools"]["gradle"]["blocked"][0]["source"]
        == "build_error"
    )


def test_register_activate_rejects_blocked_executable():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.block("maven", "/usr/bin/mvn", reason="Project requires Maven 3.9+")

    with pytest.raises(ValueError, match="blocked"):
        store.register("maven", "/usr/bin/mvn", version="3.6.3", activate=True)

    inspected = store.inspect()
    assert "active" not in inspected["tools"]["maven"]


class FallbackWriteEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            self.files[tokens[-1]] = base64.b64decode(tokens[2]).decode("utf-8")
            return {"success": True, "output": "", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def test_fallback_writer_uses_base64_decode_not_raw_heredoc():
    orchestrator = FallbackWriteEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register(
        "maven",
        "/opt/apache-maven-3.9.9/bin/mvn",
        env={"SAG_MARKER": "line one\nSAG_ENV_OVERLAY_EOF\nline three"},
    )

    commands = [command for command, _workdir, _timeout in orchestrator.commands]
    write_commands = [command for command in commands if DEFAULT_OVERLAY_JSON in command]
    assert write_commands
    assert all("SAG_ENV_OVERLAY_EOF" not in command for command in write_commands)
    assert any("base64 -d" in command for command in write_commands)


def test_inspect_skips_malformed_persisted_candidate_fields():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "candidates": {
                        "/usr/bin/mvn": {
                            "version": "3.6.3",
                            "env": ["JAVA_HOME=/bad"],
                            "path_prepend": ["/usr/bin"],
                        },
                        "/opt/apache-maven-3.9.9/bin/mvn": {
                            "version": "3.9.9",
                            "env": {"JAVA_HOME": "/opt/jdk"},
                            "path_prepend": {"0": "/opt/apache-maven-3.9.9/bin"},
                        },
                    },
                }
            },
        }
    )
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()

    assert inspected["tools"]["maven"]["candidates"] == {}
    assert inspected["warnings"]


def test_env_tool_schema_allows_string_or_array_path_prepend():
    schema = EnvTool(FakeEnvOverlayOrchestrator()).get_parameter_schema()

    path_schema = schema["properties"]["path_prepend"]

    assert path_schema["oneOf"] == [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}},
    ]


def test_maven_registration_uses_measured_version_not_caller_claim():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="99.0-caller-claim",
        requirement="[3.9,4.0)",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["measured_version"] == "3.9.9"
    assert result.raw_data["active_candidate"]["version"] == "3.9.9"
    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert (
        stored["tools"]["maven"]["candidates"]["/opt/apache-maven-3.9.9/bin/mvn"]["version"]
        == "3.9.9"
    )


def test_maven_registration_accepts_ansi_decorated_identity_line():
    orchestrator = ScriptedMavenProbeOrchestrator(
        {
            "success": True,
            "output": "\x1b[1mApache Maven 3.9.9\x1b[0m\nMaven home: /opt/maven",
            "exit_code": 0,
        }
    )
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        requirement="[3.9,4.0)",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["measured_version"] == "3.9.9"


class ScriptedMavenProbeOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self, probe):
        super().__init__()
        self.probe = probe

    def execute_command(self, command, workdir=None, timeout=None):
        if command.endswith(" -version"):
            self.commands.append((command, workdir, timeout))
            return dict(self.probe)
        return super().execute_command(command, workdir=workdir, timeout=timeout)


@pytest.mark.parametrize(
    ("probe", "requirement", "error_code"),
    [
        (
            {"success": True, "output": "not Maven 3.9.9", "exit_code": 0},
            None,
            "ENV_RUNTIME_IDENTITY_MISMATCH",
        ),
        (
            {"success": True, "output": "Apache Maven 3.8.7", "exit_code": 0},
            "[3.9,)",
            "ENV_RUNTIME_REQUIREMENT_MISMATCH",
        ),
        (
            {"success": False, "output": "missing libjansi", "exit_code": 1},
            None,
            "ENV_RUNTIME_PROBE_FAILED",
        ),
    ],
)
def test_maven_registration_rejects_unproven_runtime_without_mutating_overlay(
    probe,
    requirement,
    error_code,
):
    orchestrator = ScriptedMavenProbeOrchestrator(probe)
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/candidate/bin/mvn",
        version="3.9.9",
        requirement=requirement,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == error_code
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    assert DEFAULT_OVERLAY_SCRIPT not in orchestrator.files


class FailAfterMutatingWriteOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self):
        super().__init__()
        self.fail_path = None
        self.failed = False

    def write_file(self, path, content):
        self.files[path] = content
        if path == self.fail_path and not self.failed:
            self.failed = True
            return {"success": False, "output": "injected write failure", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}


def test_overlay_second_write_failure_rolls_back_json_and_shell_as_one_pair():
    orchestrator = FailAfterMutatingWriteOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)
    before = dict(orchestrator.files)
    orchestrator.fail_path = DEFAULT_OVERLAY_JSON

    with pytest.raises(RuntimeError, match="prior JSON/shell pair restored"):
        store.register(
            "maven",
            "/opt/apache-maven-3.9.9/bin/mvn",
            version="3.9.9",
            activate=True,
        )

    assert orchestrator.files[DEFAULT_OVERLAY_JSON] == before[DEFAULT_OVERLAY_JSON]
    assert orchestrator.files[DEFAULT_OVERLAY_SCRIPT] == before[DEFAULT_OVERLAY_SCRIPT]
    assert store.active_candidate("maven")["executable"] == "/usr/bin/mvn"


class CommandOnlyFailingOverlayOrchestrator:
    """Exercise the production fallback, which has no read_file/write_file API."""

    def __init__(self):
        self.storage = {}
        self.fail_path = DEFAULT_OVERLAY_JSON
        self.failed = False

    def execute_command(self, command, workdir=None, timeout=None):
        del workdir, timeout
        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("if test -f ") and "base64 -w 0 --" in command:
            encoded_path = command.split("base64 -w 0 --", 1)[1].split(";", 1)[0].strip()
            path = shlex.split(encoded_path)[0]
            if path not in self.storage:
                return {
                    "success": False,
                    "output": "__SAG_FILE_MISSING__",
                    "exit_code": 44,
                }
            payload = base64.b64encode(self.storage[path].encode("utf-8")).decode("ascii")
            return {
                "success": True,
                "output": f"__SAG_FILE_BASE64__{payload}",
                "exit_code": 0,
            }
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            if path not in self.storage:
                return {"success": False, "output": "", "exit_code": 1}
            return {
                "success": True,
                "output": self.storage[path],
                "exit_code": 0,
            }
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            path = tokens[-1]
            self.storage[path] = base64.b64decode(tokens[2]).decode("utf-8")
            if path == self.fail_path and not self.failed:
                self.failed = True
                return {
                    "success": False,
                    "output": "injected initial write failure",
                    "exit_code": 1,
                }
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("rm -f "):
            path = shlex.split(command)[2]
            self.storage.pop(path, None)
            return {"success": True, "output": "", "exit_code": 0}
        raise AssertionError(f"unexpected command: {command}")


def test_initial_fallback_write_failure_restores_missing_overlay_pair():
    orchestrator = CommandOnlyFailingOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    with pytest.raises(RuntimeError, match="prior JSON/shell pair restored"):
        store.register(
            "maven",
            "/opt/apache-maven-3.9.9/bin/mvn",
            version="3.9.9",
            activate=True,
        )

    assert orchestrator.storage == {}


class ProductionStrippingOverlayOrchestrator:
    """Mirror DockerOrchestrator's text `.strip()` while supporting raw transport."""

    def __init__(self):
        self.storage = {}

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        del workdir, timeout, truncate_output
        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            if path not in self.storage:
                return {"success": False, "output": "", "exit_code": 1}
            # This is the production defect: ordinary command output loses the
            # shell script's terminal LF.
            return {
                "success": True,
                "output": self.storage[path].strip(),
                "exit_code": 0,
            }
        if command.startswith("if test -f ") and "base64 -w 0 --" in command:
            encoded_path = command.split("base64 -w 0 --", 1)[1].split(";", 1)[0].strip()
            path = shlex.split(encoded_path)[0]
            if path not in self.storage:
                return {
                    "success": False,
                    "output": "__SAG_FILE_MISSING__",
                    "exit_code": 44,
                }
            payload = base64.b64encode(self.storage[path].encode("utf-8")).decode("ascii")
            return {
                "success": True,
                "output": f"__SAG_FILE_BASE64__{payload}",
                "exit_code": 0,
            }
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            path = tokens[-1]
            self.storage[path] = base64.b64decode(tokens[2]).decode("utf-8")
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("rm -f "):
            path = shlex.split(command)[2]
            self.storage.pop(path, None)
            return {"success": True, "output": "", "exit_code": 0}
        raise AssertionError(f"unexpected command: {command}")


def test_command_fallback_readback_preserves_terminal_newline_byte_for_byte():
    orchestrator = ProductionStrippingOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    overlay = store.register(
        "java",
        "/usr/lib/jvm/java-8/bin/java",
        version="8",
        env={"JAVA_HOME": "/usr/lib/jvm/java-8"},
        activate=True,
    )

    assert overlay["tools"]["java"]["active"] == "/usr/lib/jvm/java-8/bin/java"
    assert orchestrator.storage[DEFAULT_OVERLAY_SCRIPT].endswith("\n")
    assert json.loads(orchestrator.storage[DEFAULT_OVERLAY_JSON])["tools"]["java"]["active"] == (
        "/usr/lib/jvm/java-8/bin/java"
    )


def test_requirement_failure_atomically_records_constraint_and_exact_block():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)

    overlay = store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
        reason="Maven Enforcer rejected this runtime",
        working_directory="/workspace/project",
    )

    entry = overlay["tools"]["maven"]
    assert entry["requirements"] == [
        {
            "raw": "[3.9,)",
            "source": "build_error",
            "working_directory": "/workspace/project",
        }
    ]
    assert "active" not in entry
    assert entry["blocked"][-1] == {
        "executable": "/usr/bin/mvn",
        "version": "3.8.7",
        "requirement": "[3.9,)",
        "reason": "Maven Enforcer rejected this runtime",
        "source": "build_error",
    }
    assert store.observed_requirement("maven")["raw"] == "[3.9,)"


def test_scoped_requirement_history_is_append_only_and_cannot_be_weakened():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.record_requirement_failure(
        "maven",
        requirement=">=3.9",
        working_directory="/workspace/project/island-a",
    )
    store.record_requirement_failure(
        "maven",
        requirement=">=3.8",
        working_directory="/workspace/project/island-a",
    )
    store.record_requirement_failure(
        "maven",
        requirement="[3.8,3.9)",
        working_directory="/workspace/project/island-b",
    )

    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project/island-a",
        )
    ] == [">=3.9", ">=3.8"]
    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project/island-b",
        )
    ] == ["[3.8,3.9)"]
    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project",
        )
    ] == [">=3.9", ">=3.8", "[3.8,3.9)"]
    assert (
        store.observed_requirements(
            "maven",
            working_directory="/workspace/other-project",
        )
        == []
    )


def test_maven_registration_cannot_narrow_persisted_constraints_by_path():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        working_directory="/workspace/project/module",
    )
    tool = EnvTool(orchestrator, store=store)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/usr/bin/mvn",
        activate=True,
        working_directory="/workspace/unrelated",
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert result.raw_data["requirement"] == "[3.9,)"


def test_omitted_maven_requirement_inherits_harness_observation():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
    )
    tool = EnvTool(orchestrator, store=store)

    rejected = tool.execute(
        action="register",
        tool="maven",
        executable="/usr/bin/mvn",
        activate=True,
    )
    accepted = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=True,
    )

    assert rejected.succeeded is False
    assert rejected.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert rejected.raw_data["requirement"] == "[3.9,)"
    assert rejected.raw_data["requirement_source"] == "registered_state"
    assert accepted.succeeded is True
    assert store.active_candidate("maven")["version"] == "3.9.9"


class MavenRealpathOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self, realpaths):
        super().__init__()
        self.realpaths = realpaths

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            self.commands.append((command, workdir, timeout))
            requested = shlex.split(command)[-1]
            resolved = self.realpaths.get(requested)
            return {
                "success": resolved is not None,
                "exit_code": 0 if resolved is not None else 1,
                "output": resolved or "",
            }
        return super().execute_command(command, workdir=workdir, timeout=timeout)


def test_public_maven_registration_rejects_relative_executable_before_probe():
    orchestrator = FakeEnvOverlayOrchestrator()
    project = ProjectTool(env_tool=EnvTool(orchestrator))

    result = project.execute(
        action="env",
        tool="mvn",
        executable="downloads/apache-maven/bin/mvn",
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_PATH_NOT_ABSOLUTE"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    assert not any(
        command.startswith("realpath ") or command.endswith(" -version")
        for command, _workdir, _timeout in orchestrator.commands
    )


def test_public_maven_registration_fails_closed_when_realpath_cannot_be_proven():
    requested = "/opt/apache-maven-current/bin/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: None})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_REALPATH_FAILED"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_public_maven_registration_rejects_workspace_symlink_escape():
    requested = "/workspace/project/tools/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: "/root/private/bin/mvn"})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_REALPATH_ESCAPE"
    assert result.raw_data["resolved_executable"] == "/root/private/bin/mvn"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_public_maven_registration_rejects_canonical_target_not_named_mvn():
    requested = "/opt/apache-maven-current/bin/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: "/opt/apache-maven-3.9.9/bin/mvn-real"})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_MAVEN_EXECUTABLE_NAME_MISMATCH"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_legacy_mvn_overlay_normalizes_to_one_maven_key_and_remains_resolvable():
    executable = "/opt/apache-maven-3.9.9/bin/mvn"
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "mvn": {
                    "active": executable,
                    "candidates": {
                        executable: {
                            "version": "3.9.9",
                            "source": "agent_registered",
                            "env": {},
                            "path_prepend": ["/stale/bin"],
                        }
                    },
                    "requirements": [
                        {
                            "raw": "[3.9,)",
                            "source": "build_error",
                            "working_directory": None,
                        }
                    ],
                }
            },
        }
    )
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()
    resolved = ToolchainManager(orchestrator).resolve(
        ToolchainSpec(name="maven", executable="mvn", prefer_wrapper=False),
        working_directory="/workspace/other-project",
    )

    assert set(inspected["tools"]) == {"maven"}
    assert inspected["tools"]["maven"]["active"] == executable
    assert inspected["tools"]["maven"]["candidates"][executable]["path_prepend"] == [
        "/opt/apache-maven-3.9.9/bin",
        "/stale/bin",
    ]
    assert store.observed_requirements("mvn")[0]["raw"] == "[3.9,)"
    assert resolved is not None
    assert resolved.candidate.path == executable
    assert resolved.candidate.source == "env_overlay"


def test_mvn_alias_covers_negative_evidence_requirements_and_clear_without_double_key():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    recorded = store.record_requirement_failure(
        "mvn",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
    )

    assert set(recorded["tools"]) == {"maven"}
    assert store.observed_requirements("mvn")[0]["raw"] == "[3.9,)"
    assert store.is_blocked("maven", "/usr/bin/mvn", version="3.8.7") is True
    cleared = store.clear("mvn")
    assert cleared["tools"] == {}


class MavenContractE2EOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []
        self.monitored_commands = []
        self.project_name = "project"
        self.executables = {
            "/usr/bin/mvn": "Apache Maven 3.8.7",
        }
        self.build_results = [
            {
                "success": False,
                "exit_code": 1,
                "output": (
                    "[ERROR] BUILD FAILURE\n"
                    "Detected Maven Version: 3.8.7 is not in the allowed range [3.9,)."
                ),
            },
            {"success": True, "exit_code": 0, "output": "[INFO] BUILD SUCCESS"},
        ]

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("realpath -e -- "):
            requested = shlex.split(command)[-1]
            return {
                "success": requested in self.executables,
                "exit_code": 0 if requested in self.executables else 1,
                "output": requested if requested in self.executables else "",
            }
        if "java -version" in command:
            return {"success": True, "exit_code": 0, "output": 'openjdk version "17.0.1"'}
        if "build_requirements.json" in command and command.startswith("cat "):
            return {"success": False, "exit_code": 1, "output": ""}
        if command.startswith("test -f ") and "pom.xml" in command:
            marker = "EXISTS" if "'EXISTS'" in command else "exists"
            return {"success": True, "exit_code": 0, "output": marker}
        if command.startswith("test -x "):
            path = shlex.split(command)[2]
            exists = path in self.executables
            return {
                "success": True,
                "exit_code": 0,
                "output": "EXISTS" if exists else "MISSING",
            }
        if command.endswith(" -version"):
            path = shlex.split(command)[0]
            output = self.executables.get(path)
            return {
                "success": bool(output),
                "exit_code": 0 if output else 1,
                "output": output or "",
            }
        if command == "command -v mvn":
            return {"success": True, "exit_code": 0, "output": "/usr/bin/mvn"}
        if "apache-maven-*/bin/mvn" in command:
            standalone = [
                path
                for path in self.executables
                if "/apache-maven-" in path and path.endswith("/bin/mvn")
            ]
            return {"success": True, "exit_code": 0, "output": "\n".join(standalone)}
        if command.startswith("find ") and "target/classes" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": "/workspace/project/target/classes/Example.class",
            }
        return {"success": True, "exit_code": 0, "output": ""}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        result = self.build_results.pop(0)
        return dict(result)


class CanonicalMavenFacadeE2EOrchestrator(MavenContractE2EOrchestrator):
    alias = "/opt/apache-maven-current/bin/mvn"
    canonical = "/opt/apache-maven-3.9.9/bin/mvn"

    def __init__(self):
        super().__init__()
        self.executables[self.alias] = "Apache Maven 3.9.9"
        self.executables[self.canonical] = "Apache Maven 3.9.9"
        self.build_results = [{"success": True, "exit_code": 0, "output": "[INFO] BUILD SUCCESS"}]

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            self.commands.append((command, workdir, timeout))
            requested = shlex.split(command)[-1]
            resolved = self.canonical if requested == self.alias else requested
            return {
                "success": requested in self.executables,
                "exit_code": 0 if requested in self.executables else 1,
                "output": resolved if requested in self.executables else "",
            }
        if command == "command -v mvn" and DEFAULT_OVERLAY_SCRIPT in self.files:
            self.commands.append((command, workdir, timeout))
            path_line = next(
                line
                for line in self.files[DEFAULT_OVERLAY_SCRIPT].splitlines()
                if line.startswith("export PATH=")
            )
            path_prefix = path_line.removeprefix("export PATH=").removesuffix(":$PATH")
            for directory in path_prefix.split(":"):
                candidate = f"{directory}/mvn"
                if candidate in self.executables:
                    return {"success": True, "exit_code": 0, "output": candidate}
        return super().execute_command(command, workdir=workdir, timeout=timeout)


def test_public_mvn_env_pins_canonical_maven_for_shell_and_cross_workdir_build():
    orchestrator = CanonicalMavenFacadeE2EOrchestrator()
    orchestrator.executables["/stale/bin/mvn"] = "Apache Maven 3.8.7"
    store = EnvOverlayStore(orchestrator)
    store.register(
        "aardvark",
        "/stale/bin/helper",
        path_prepend=["/stale/bin"],
        activate=True,
    )
    project = ProjectTool(env_tool=EnvTool(orchestrator, store=store))
    build = BuildTool(orchestrator, maven_tool=MavenTool(orchestrator))

    registered = project.safe_execute(
        action="env",
        tool="mvn",
        executable=orchestrator.alias,
        path_prepend=["/stale/bin"],
        working_directory="/workspace/bootstrap",
    )
    shell_resolution = orchestrator.execute_command(
        "command -v mvn",
        workdir="/workspace/unrelated/module",
    )
    built = build.execute(
        action="compile",
        working_directory="/workspace/unrelated/module",
    )

    assert registered.succeeded is True
    assert registered.raw_data["active_candidate"]["executable"] == orchestrator.canonical
    assert set(registered.raw_data["overlay"]["tools"]) == {"aardvark", "maven"}
    assert "mvn" not in registered.raw_data["overlay"]["tools"]
    candidate = registered.raw_data["active_candidate"]
    assert candidate["path_prepend"] == [
        "/opt/apache-maven-3.9.9/bin",
        "/stale/bin",
    ]
    assert shell_resolution["output"] == orchestrator.canonical
    assert built.succeeded is True
    assert orchestrator.monitored_commands[-1][0].startswith(f"{orchestrator.canonical} ")
    assert orchestrator.monitored_commands[-1][1]["workdir"] == ("/workspace/unrelated/module")


def test_maven_failure_contract_survives_weak_model_omissions_end_to_end():
    orchestrator = MavenContractE2EOrchestrator()
    maven = MavenTool(orchestrator)
    build = BuildTool(orchestrator, maven_tool=maven)
    project = ProjectTool(env_tool=EnvTool(orchestrator))

    first = build.execute(action="compile", working_directory="/workspace/project")

    assert first.succeeded is False
    assert first.error_code == "MAVEN_VERSION_ERROR"
    assert first.metadata["runtime_contract_persisted"] is True
    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert stored["tools"]["maven"]["requirements"][0]["raw"] == "[3.9,)"
    assert stored["tools"]["maven"]["blocked"][-1]["executable"] == "/usr/bin/mvn"
    assert stored["tools"]["maven"]["blocked"][-1]["version"] == "3.8.7"

    stale = project.execute(
        action="env",
        tool="maven",
        executable="/usr/bin/mvn",
    )
    assert stale.succeeded is False
    assert stale.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"

    compatible_path = "/opt/apache-maven-3.9.9/bin/mvn"
    orchestrator.executables[compatible_path] = "Apache Maven 3.9.9"
    registered = project.execute(
        action="env",
        tool="maven",
        executable=compatible_path,
    )
    assert registered.succeeded is True
    assert registered.raw_data["measured_version"] == "3.9.9"

    retried = build.execute(action="compile", working_directory="/workspace/project")

    assert retried.succeeded is True
    assert orchestrator.monitored_commands[-1][0].startswith(f"{compatible_path} ")
    assert retried.metadata["maven_version_requirement"] == {
        "raw": "[3.9,)",
        "source": "registered_state",
        "kind": "range",
    }
