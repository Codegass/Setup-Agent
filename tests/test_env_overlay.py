import json

from sag.runtime.env_overlay import (
    DEFAULT_OVERLAY_JSON,
    DEFAULT_OVERLAY_SCRIPT,
    EnvOverlayStore,
)
from sag.tools.env_tool import EnvTool


class FakeEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
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
