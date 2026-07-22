from sag.tools.internal.env_tool import EnvTool
from sag.tools.internal.toolchain_manager import (
    ToolchainManager,
    ToolchainSpec,
    ToolVersionRequirement,
)


class FakeOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command.startswith("cat /workspace/.setup_agent/env_overlay.json"):
            return {
                "exit_code": 0,
                "output": self.files.get("/workspace/.setup_agent/env_overlay.json", ""),
            }
        if command.startswith("mkdir -p"):
            return {"exit_code": 0, "output": ""}
        if command.startswith("printf ") and " > /workspace/.setup_agent/env_overlay" in command:
            return {"exit_code": 0, "output": ""}
        if command.startswith("test -x /opt/missing-maven/bin/mvn"):
            return {"exit_code": 0, "output": "MISSING"}
        return {"exit_code": 0, "output": ""}


class FakeToolchainOrchestrator:
    def __init__(self):
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command.startswith("cat /workspace/.setup_agent/env_overlay.json"):
            return {"exit_code": 0, "output": "{}"}
        if command.startswith("test -x /workspace/apache-maven-3.9.9/bin/mvn"):
            return {"exit_code": 0, "output": "EXISTS"}
        if command == "/workspace/apache-maven-3.9.9/bin/mvn -version":
            return {"exit_code": 0, "output": "Apache Maven 3.9.9"}
        if command.startswith("test -x /usr/bin/mvn"):
            return {"exit_code": 0, "output": "EXISTS"}
        if command == "/usr/bin/mvn -version":
            return {"exit_code": 0, "output": "Apache Maven 3.8.7"}
        if command.startswith("test -x /workspace/project/mvnw"):
            return {"exit_code": 0, "output": "MISSING"}
        if command.startswith("find /workspace /tmp /opt /usr/local"):
            return {
                "exit_code": 0,
                "output": "/workspace/apache-maven-3.9.9/bin/mvn\n",
            }
        if command.startswith("find /tmp /opt /usr/local"):
            return {"exit_code": 0, "output": ""}
        if command == "command -v mvn":
            return {"exit_code": 0, "output": "/usr/bin/mvn\n"}
        return {"exit_code": 0, "output": ""}


def test_env_register_rejects_missing_executable():
    tool = EnvTool(FakeOverlayOrchestrator())

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/missing-maven/bin/mvn",
        version="3.9.9",
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert "not executable" in result.error


def test_toolchain_manager_discovers_workspace_maven_for_version_requirement():
    manager = ToolchainManager(FakeToolchainOrchestrator())

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement.from_raw(
                ">=3.9,<4.0", source="tool_parameter"
            ),
        ),
        working_directory="/workspace/project",
    )

    assert resolved is not None
    assert resolved.candidate.path == "/workspace/apache-maven-3.9.9/bin/mvn"
    assert resolved.candidate.version == "3.9.9"
