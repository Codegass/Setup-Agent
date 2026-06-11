import json

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
from sag.tools.internal.system_tool import SystemTool

JAVA_HOME = "/usr/lib/jvm/java-17-openjdk-amd64"
JAVA_BIN = f"{JAVA_HOME}/bin/java"
JAVAC_BIN = f"{JAVA_HOME}/bin/javac"


class FakeSystemOrchestrator:
    def __init__(self, *, install_success=True, verification_success=True):
        self.install_success = install_success
        self.verification_success = verification_success
        self.commands = []
        self.files = {}

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command == "java -version 2>&1":
            return {"success": False, "output": "java: command not found", "exit_code": 127}

        if command == "apt-get update":
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("apt-get install -y openjdk-17"):
            if self.install_success:
                return {"success": True, "output": "installed", "exit_code": 0}
            return {"success": False, "output": "install failed", "exit_code": 100}

        if command == "apt-get install -y java-17-openjdk":
            return {"success": False, "output": "not found", "exit_code": 100}

        if command == "apt-get install -y java-17-openjdk-devel":
            return {"success": False, "output": "not found", "exit_code": 100}

        if command == "dpkg --print-architecture":
            return {"success": True, "output": "amd64\n", "exit_code": 0}

        if command == f"test -f {JAVA_BIN} && echo 'exists'":
            return {"success": True, "output": "exists", "exit_code": 0}

        if command in {
            "update-alternatives --list java 2>/dev/null",
            "update-alternatives --list javac 2>/dev/null",
        }:
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("echo 'export JAVA_HOME="):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("echo 'export PATH=$JAVA_HOME/bin:$PATH'"):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("update-alternatives"):
            return {"success": True, "output": "", "exit_code": 0}

        if command == (
            f"export JAVA_HOME={JAVA_HOME} && java -version 2>&1 && echo '---' "
            "&& javac -version 2>&1"
        ):
            if self.verification_success:
                return {
                    "success": True,
                    "output": 'openjdk version "17.0.10"\n---\njavac 17.0.10',
                    "exit_code": 0,
                }
            return {"success": False, "output": "verification failed", "exit_code": 1}

        if command == "mkdir -p /workspace/.setup_agent":
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}

    def read_file(self, path):
        if path not in self.files:
            return {"success": False, "content": "", "exit_code": 1}
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def test_install_java_registers_active_java_overlay_runtime():
    orchestrator = FakeSystemOrchestrator()
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.success is True
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    java_entry = overlay["tools"]["java"]
    assert java_entry["active"] == JAVA_BIN
    assert java_entry["candidates"][JAVA_BIN]["version"] == "17"
    assert java_entry["candidates"][JAVA_BIN]["env"] == {"JAVA_HOME": JAVA_HOME}
    assert java_entry["candidates"][JAVA_BIN]["path_prepend"] == [f"{JAVA_HOME}/bin"]


def test_install_java_verification_failure_does_not_activate_overlay_runtime():
    orchestrator = FakeSystemOrchestrator(verification_success=False)
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.success is False
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_install_java_install_failure_does_not_activate_overlay_runtime():
    orchestrator = FakeSystemOrchestrator(install_success=False)
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.success is False
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
