import json

import pytest

from sag.runtime.env_overlay import (
    DEFAULT_OVERLAY_JSON,
    DEFAULT_OVERLAY_SCRIPT,
    EnvOverlayStore,
)
from sag.tools.internal.env_tool import EnvTool


class FakeEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("test -x "):
            return {"success": True, "output": "EXISTS\n", "exit_code": 0}
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

    assert registered.success is True
    assert activated.success is True
    assert inspected.success is True
    assert inspected.raw_data["overlay"]["tools"]["maven"]["active"] == (
        "/opt/apache-maven-3.9.9/bin/mvn"
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

    assert block_from_dict.success is True
    assert block_from_kwargs.success is True
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
