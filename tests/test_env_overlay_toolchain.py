import json

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


def test_a_refused_executable_names_what_is_already_registered():
    """Live p7b-camel-quarkus: the model asked for
    /usr/lib/jvm/java-17-openjdk-amd64/bin/java on an arm64 machine while the
    arm64 path for the same JDK was already registered, and the refusal said
    only that the path does not exist. What the overlay already knows is the
    cheapest correction available, so it is stated."""
    orchestrator = FakeOverlayOrchestrator()
    orchestrator.files["/workspace/.setup_agent/env_overlay.json"] = json.dumps(
        {
            "tools": {
                "java": {
                    "active": "/usr/lib/jvm/java-11-openjdk-arm64/bin/java",
                    "blocked": [],
                    "candidates": {
                        "/usr/lib/jvm/java-11-openjdk-arm64/bin/java": {"version": "11"},
                        "/usr/lib/jvm/java-17-openjdk-arm64/bin/java": {"version": "17"},
                    },
                }
            }
        }
    )
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/lib/jvm/java-17-openjdk-amd64/bin/java",
        activate=True,
    )

    assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    named = " ".join(result.suggestions)
    assert "/usr/lib/jvm/java-17-openjdk-arm64/bin/java" in named
    assert result.raw_data["registered_candidates"]


def test_a_blocked_candidate_is_never_offered_as_a_correction():
    orchestrator = FakeOverlayOrchestrator()
    orchestrator.files["/workspace/.setup_agent/env_overlay.json"] = json.dumps(
        {
            "tools": {
                "maven": {
                    "active": None,
                    "blocked": [{"executable": "/usr/share/maven/bin/mvn", "version": "3.8.7"}],
                    "candidates": {
                        "/usr/share/maven/bin/mvn": {"version": "3.8.7"},
                        "/opt/maven/bin/mvn": {"version": "3.9.9"},
                    },
                }
            }
        }
    )
    tool = EnvTool(orchestrator)

    assert tool._registered_candidates("maven") == ["/opt/maven/bin/mvn"]


def test_an_unknown_tool_offers_nothing_and_still_refuses_cleanly():
    tool = EnvTool(FakeOverlayOrchestrator())

    result = tool.execute(
        action="register", tool="rustc", executable="/nowhere/rustc", activate=True
    )

    assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert "registered_candidates" not in result.raw_data
