from sag.tools.base import ToolResult
from sag.tools.gradle_tool import GradleTool
from sag.tools.maven_tool import MavenTool
from sag.tools.toolchain_manager import (
    ResolvedToolExecutable,
    ToolExecutableCandidate,
)


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
        if command == "command -v mvn":
            return {"success": True, "output": "/usr/bin/mvn", "exit_code": 0}
        if command == "command -v gradle":
            return {"success": True, "output": "/usr/bin/gradle", "exit_code": 0}
        if command.startswith("test -x /usr/bin/mvn"):
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if command.startswith("test -x /usr/bin/gradle"):
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if command == "/usr/bin/mvn -version":
            return {"success": True, "output": "Apache Maven 3.9.6", "exit_code": 0}
        if command == "/usr/bin/gradle -version":
            return {"success": True, "output": "Gradle 8.5", "exit_code": 0}
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


class FakeToolchainManager:
    def __init__(
        self,
        path="/tmp/apache-maven-3.9.6/bin/mvn",
        version="3.9.6",
        source="registered",
    ):
        self.path = path
        self.version = version
        self.source = source
        self.seen_spec = None
        self.seen_working_directory = None

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_spec = spec
        self.seen_working_directory = working_directory
        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=self.path,
                version=self.version,
                source=self.source,
            ),
            reason="test resolver",
        )


class WrapperBuildToolOrchestrator(FakeBuildToolOrchestrator):
    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if "gradlew" in command and command.startswith("test -f"):
            return {"success": True, "output": "exists", "exit_code": 0}
        if command.startswith("chmod +x"):
            return {"success": True, "output": "", "exit_code": 0}
        return super().execute_command(command, workdir=workdir, timeout=timeout)


class EmptyToolchainManager:
    def __init__(self):
        self.seen_spec = None

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_spec = spec
        return None


class SequencedToolchainManager:
    def __init__(self, resolutions):
        self.resolutions = list(resolutions)
        self.seen_specs = []
        self.seen_working_directories = []

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_specs.append(spec)
        self.seen_working_directories.append(working_directory)
        if not self.resolutions:
            return None

        resolution = self.resolutions.pop(0)
        if resolution is None:
            return None

        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=resolution["path"],
                version=resolution["version"],
                source=resolution["source"],
            ),
            reason="test resolver",
        )


class VersionCommandOrchestrator(FakeBuildToolOrchestrator):
    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if "pom.xml" in command:
            raise AssertionError("Maven version diagnostics must not require pom.xml")
        if command.endswith("mvn -version"):
            return {"success": True, "output": "Apache Maven 3.9.6", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


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


def test_maven_timeout_result_preserves_env_overlay_runtime_and_requested_version():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": "[INFO] downloading dependencies",
            "exit_code": 0,
            "termination_reason": "silent_timeout",
            "execution_time": 1200.0,
        }
    )
    toolchain_manager = FakeToolchainManager(
        path="/opt/apache-maven-3.9.8/bin/mvn",
        version="3.9.8",
        source="env_overlay",
    )
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
        maven_version_requirement="[3.9,4.0)",
    )

    assert result.success is False
    assert result.metadata["termination_reason"] == "silent_timeout"
    assert result.metadata["maven_runtime"] == {
        "executable": "/opt/apache-maven-3.9.8/bin/mvn",
        "version": "3.9.8",
        "source": "env_overlay",
    }
    assert result.metadata["maven_version_requirement"] == {
        "raw": "[3.9,4.0)",
        "source": "tool_parameter",
        "kind": "range",
    }


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


def test_gradle_does_not_run_path_gradle_when_manager_cannot_resolve():
    orchestrator = FakeBuildToolOrchestrator()
    tool = GradleTool(orchestrator, toolchain_manager=EmptyToolchainManager())
    tool._install_gradle = lambda working_directory: ToolResult(
        success=False,
        output="",
        error="Gradle unavailable",
        error_code="GRADLE_INSTALLATION_FAILED",
    )

    result = tool.execute(
        tasks="build",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.success is False
    assert result.error_code == "GRADLE_INSTALLATION_FAILED"
    assert all(
        not command.startswith("gradle ") for command, _kwargs in orchestrator.monitored_commands
    )


def test_gradle_real_install_path_does_not_generate_wrapper_with_unresolved_manager():
    orchestrator = FakeBuildToolOrchestrator()
    tool = GradleTool(orchestrator, toolchain_manager=EmptyToolchainManager())

    result = tool.execute(
        tasks="build",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.success is False
    assert result.error_code == "GRADLE_EXECUTABLE_NOT_RESOLVED"
    assert any(
        "apt-get install -y gradle" in command
        for command, _workdir, _timeout in orchestrator.commands
    )
    assert all(
        "gradle wrapper" not in command
        for command, _workdir, _timeout in orchestrator.commands
    )
    assert all(
        not command.startswith("gradle ") for command, _kwargs in orchestrator.monitored_commands
    )


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


def test_maven_tool_uses_resolved_toolchain_executable():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager("/tmp/apache-maven-3.9.6/bin/mvn")
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)
    tool._record_test_summary = lambda *args, **kwargs: None
    tool._validate_build_artifacts_in_container = lambda *args, **kwargs: {
        "artifacts_exist": True,
        "found_artifacts": [],
    }

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.success is True
    assert orchestrator.monitored_commands[0][0].startswith("/tmp/apache-maven-3.9.6/bin/mvn ")
    assert toolchain_manager.seen_working_directory == "/workspace/project"


def test_maven_tool_uses_active_env_overlay_candidate():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager(
        path="/opt/apache-maven-3.9.8/bin/mvn",
        version="3.9.8",
        source="env_overlay",
    )
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)
    tool._record_test_summary = lambda *args, **kwargs: None
    tool._validate_build_artifacts_in_container = lambda *args, **kwargs: {
        "artifacts_exist": True,
        "found_artifacts": [],
    }

    result = tool.execute(
        command="compile",
        working_directory="/workspace/project",
        maven_version_requirement="[3.9,4.0)",
    )

    assert result.success is True
    assert orchestrator.monitored_commands[0][0].startswith(
        "/opt/apache-maven-3.9.8/bin/mvn "
    )
    assert result.metadata["maven_runtime"] == {
        "executable": "/opt/apache-maven-3.9.8/bin/mvn",
        "version": "3.9.8",
        "source": "env_overlay",
    }
    assert result.metadata["maven_version_requirement"] == {
        "raw": "[3.9,4.0)",
        "source": "tool_parameter",
        "kind": "range",
    }
    assert toolchain_manager.seen_spec.version_requirement.raw == "[3.9,4.0)"


def test_maven_tool_schema_exposes_maven_version_requirement():
    schema = MavenTool(FakeBuildToolOrchestrator()).get_parameter_schema()

    assert "maven_version_requirement" in schema["properties"]
    assert schema["properties"]["maven_version_requirement"]["type"] == "string"


def test_maven_tool_turns_explicit_version_parameter_into_requirement():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager()
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
        maven_version_requirement="[3.9,4.0)",
    )

    assert result.success is True
    assert toolchain_manager.seen_spec.version_requirement.raw == "[3.9,4.0)"
    assert toolchain_manager.seen_spec.version_requirement.source == "tool_parameter"
    assert toolchain_manager.seen_spec.version_requirement.kind == "range"


def test_maven_tool_does_not_fallback_when_explicit_version_is_unresolved():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = EmptyToolchainManager()
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
        maven_version_requirement="3.9.6",
    )

    assert result.success is False
    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    assert orchestrator.monitored_commands == []
    assert orchestrator.commands == []
    assert toolchain_manager.seen_spec.version_requirement.raw == "3.9.6"
    assert result.metadata["maven_version_requirement"] == {
        "raw": "3.9.6",
        "source": "tool_parameter",
        "kind": "exact",
    }


def test_maven_tool_installs_then_uses_resolved_default_executable():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = SequencedToolchainManager(
        [
            None,
            {
                "path": "/opt/apache-maven-3.9.9/bin/mvn",
                "version": "3.9.9",
                "source": "env_overlay",
            },
        ]
    )
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)
    install_calls = []
    tool._install_maven = lambda: install_calls.append(True) or ToolResult(
        success=True,
        output="Maven installed",
    )
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
    )

    assert result.success is True
    assert install_calls == [True]
    assert len(toolchain_manager.seen_specs) == 2
    assert toolchain_manager.seen_working_directories == [
        "/workspace/project",
        "/workspace/project",
    ]
    assert orchestrator.monitored_commands[0][0].startswith(
        "/opt/apache-maven-3.9.9/bin/mvn "
    )
    assert not orchestrator.monitored_commands[0][0].startswith("mvn ")
    assert result.metadata["maven_runtime"] == {
        "executable": "/opt/apache-maven-3.9.9/bin/mvn",
        "version": "3.9.9",
        "source": "env_overlay",
    }


def test_maven_tool_does_not_use_raw_path_after_install_when_manager_cannot_resolve_default_version():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = EmptyToolchainManager()
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)
    install_calls = []
    tool._install_maven = lambda: install_calls.append(True) or ToolResult(
        success=True,
        output="Maven installed",
    )

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
    )

    assert result.success is False
    assert result.error_code == "MAVEN_EXECUTABLE_NOT_RESOLVED"
    assert install_calls == [True]
    assert orchestrator.monitored_commands == []
    assert toolchain_manager.seen_spec.version_requirement is None


def test_gradle_uses_active_env_overlay_candidate():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager(
        path="/opt/gradle-8.7/bin/gradle",
        version="8.7",
        source="env_overlay",
    )
    tool = GradleTool(orchestrator, toolchain_manager=toolchain_manager)

    result = tool.execute(
        tasks="build",
        working_directory="/workspace/project",
        use_wrapper=True,
    )

    assert result.success is True
    assert orchestrator.monitored_commands[0][0].startswith("/opt/gradle-8.7/bin/gradle ")
    assert toolchain_manager.seen_spec.name == "gradle"
    assert toolchain_manager.seen_spec.executable == "gradle"
    assert toolchain_manager.seen_spec.prefer_wrapper is True


def test_gradle_wrapper_keeps_priority_over_non_overlay_manager_candidate():
    orchestrator = WrapperBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager(
        path="/usr/local/bin/gradle",
        version="8.5",
        source="registered",
    )
    tool = GradleTool(orchestrator, toolchain_manager=toolchain_manager)

    result = tool.execute(
        tasks="build",
        working_directory="/workspace/project",
        use_wrapper=True,
    )

    assert result.success is True
    assert orchestrator.monitored_commands[0][0].startswith("./gradlew ")
    assert toolchain_manager.seen_spec.name == "gradle"


def test_maven_tool_extracts_version_requirement_from_enforcer_output():
    requirement = MavenTool.extract_version_requirement_from_output(
        "Detected Maven Version: 3.6.3 is not in the allowed range [3.9,)."
    )

    assert requirement is not None
    assert requirement.raw == "[3.9,)"
    assert requirement.source == "build_error"
    assert requirement.kind == "range"


def test_maven_tool_failed_result_metadata_includes_detected_maven_requirement():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": (
                "[ERROR] BUILD FAILURE\n"
                "Detected Maven Version: 3.6.3 is not in the allowed range [3.9,)."
            ),
            "exit_code": 1,
        }
    )
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager())
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="test", working_directory="/workspace/project")

    assert result.success is False
    assert result.metadata["maven_version_requirement"] == {
        "raw": "[3.9,)",
        "source": "build_error",
        "kind": "range",
    }


def test_maven_failed_result_metadata_includes_runtime_facts_for_version_error():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": (
                "[ERROR] BUILD FAILURE\n"
                "Detected Maven Version: 3.6.3 is not in the allowed range [3.9,)."
            ),
            "exit_code": 1,
        }
    )
    tool = MavenTool(
        orchestrator,
        toolchain_manager=FakeToolchainManager(
            path="/usr/bin/mvn",
            version="3.6.3",
            source="system",
        ),
    )
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.success is False
    assert result.metadata["maven_version_requirement"]["raw"] == "[3.9,)"
    assert result.metadata["maven_runtime"] == {
        "executable": "/usr/bin/mvn",
        "version": "3.6.3",
        "source": "system",
    }


def test_maven_tool_runs_version_command_as_diagnostic_without_pom_validation():
    orchestrator = VersionCommandOrchestrator()
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager("/usr/bin/mvn"))

    result = tool.execute(command="-version", working_directory="/workspace/project")

    assert result.success is True
    assert result.output == "Apache Maven 3.9.6"
    assert orchestrator.monitored_commands == []
    assert ("/usr/bin/mvn -version", "/workspace/project", None) in orchestrator.commands


def test_maven_tool_runs_prefixed_version_command_as_diagnostic():
    orchestrator = VersionCommandOrchestrator()
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager("/usr/bin/mvn"))

    result = tool.execute(command="mvn -version", working_directory="/workspace/project")

    assert result.success is True
    assert result.output == "Apache Maven 3.9.6"
    assert orchestrator.monitored_commands == []
    assert ("/usr/bin/mvn -version", "/workspace/project", None) in orchestrator.commands
