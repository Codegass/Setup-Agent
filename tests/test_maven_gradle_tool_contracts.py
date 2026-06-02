from sag.tools.gradle_tool import GradleTool
from sag.tools.maven_tool import MavenTool


class FakeBuildToolOrchestrator:
    def __init__(self, monitored_result=None):
        self.monitored_result = monitored_result or {
            "output": "[INFO] BUILD SUCCESS",
            "exit_code": 0,
        }
        self.commands = []
        self.monitored_commands = []
        self.project_name = None

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command == "which mvn":
            return {"success": True, "output": "/usr/bin/mvn", "exit_code": 0}
        if command == "which gradle":
            return {"success": True, "output": "/usr/bin/gradle", "exit_code": 0}
        if "pom.xml && echo 'EXISTS'" in command:
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if "build.gradle" in command and command.startswith("test -f"):
            return {"success": True, "output": "", "exit_code": 0}
        if "grep -q '<modules>'" in command:
            return {"success": False, "output": "NO_MODULES", "exit_code": 1}
        if "settings.gradle" in command and "grep -q 'include'" in command:
            return {"success": False, "output": "", "exit_code": 1}
        if command.startswith("find "):
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        return dict(self.monitored_result)


def test_maven_tool_converts_monitored_silent_timeout_to_timeout_result():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": "[INFO] BUILD SUCCESS",
            "exit_code": 0,
            "termination_reason": "silent_timeout",
            "execution_time": 1200.0,
        }
    )
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="test", working_directory="/workspace/project")

    assert result.success is False
    assert result.error_code == "TIMEOUT_SILENT_TIMEOUT"
    assert result.metadata["termination_reason"] == "silent_timeout"
    assert result.metadata["execution_time"] == 1200.0
    assert result.metadata["tool_type"] == "maven"
    assert result.metadata["command"] == orchestrator.monitored_commands[0][0]


def test_gradle_tool_converts_monitored_silent_timeout_to_timeout_result():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": "BUILD SUCCESSFUL",
            "exit_code": 0,
            "termination_reason": "silent_timeout",
            "execution_time": 1200.0,
        }
    )
    tool = GradleTool(orchestrator)

    result = tool.execute(
        tasks="test",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.success is False
    assert result.error_code == "TIMEOUT_SILENT_TIMEOUT"
    assert result.metadata["termination_reason"] == "silent_timeout"
    assert result.metadata["execution_time"] == 1200.0
    assert result.metadata["tool_type"] == "gradle"
    assert result.metadata["task"] == "test"


def test_maven_tool_preserves_list_properties_when_fail_at_end_adds_ignore():
    orchestrator = FakeBuildToolOrchestrator()
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        properties=["skipITs=true"],
        fail_at_end=True,
        working_directory="/workspace/project",
    )

    assert result.success is True
    command = orchestrator.monitored_commands[0][0]
    assert "-DskipITs=true" in command
    assert "-Dmaven.test.failure.ignore=true" in command
    assert " -D, " not in command
    assert " -Dm " not in command
