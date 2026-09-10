import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import ContainerFS, add_published_mutable_json

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.evidence import EvidenceAssessment
from sag.evidence import InvocationStatus
from sag.tools.base import ToolResult
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.maven_versions import (
    maven_distribution_for_floor,
    nearest_installable_floor,
)
from sag.tools.internal.toolchain_manager import (
    ResolvedToolExecutable,
    ToolExecutableCandidate,
    ToolVersionRequirement,
)

pytestmark = pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")


class FakeBuildToolOrchestrator:
    def __init__(self, monitored_result=None):
        self.monitored_result = monitored_result or {
            "output": "[INFO] BUILD SUCCESS",
            "exit_code": 0,
        }
        self.commands = []
        self.monitored_commands = []
        self.project_name = None
        self.evidence = ContainerFS()
        add_published_mutable_json(
            self,
            self.evidence,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=complete_build_requirements_v1(),
        )

    def execute_control_command(self, command, workdir=None, timeout=None, **kwargs):
        # Evidence transports resolve the clean host-control channel and fail
        # closed on a bound plain executor; the scripted double exposes the
        # channel explicitly and routes it to the same observable surface.
        del kwargs
        return self.execute_command(command, workdir=workdir, timeout=timeout)

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command, workdir=workdir, timeout=timeout)

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


class FakeOutputStorage:
    def __init__(self, ref_id="output_build_log"):
        self.ref_id = ref_id
        self.stored = []
        self.outputs = {}

    def store_output(self, **kwargs):
        self.stored.append(kwargs)
        self.outputs[self.ref_id] = kwargs["output"]
        return self.ref_id

    def retrieve_output(self, ref_id):
        return self.outputs.get(ref_id)


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
        self.seen_specs = []

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_spec = spec
        self.seen_specs.append(spec)
        return None


class RequirementFreeToolchainManager:
    """Resolves nothing under a requirement and one registered Maven without it.

    jackrabbit d2r4 exactly: `project(action='provision', packages=['maven'])`
    installed 3.8.7, `project(action='env', ...)` registered and activated it,
    and the build then asserted maven_version_requirement="[3.9,)".
    """

    def __init__(self, path="/usr/share/maven/bin/mvn", version="3.8.7"):
        self.path = path
        self.version = version
        self.seen_specs = []

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_specs.append(spec)
        if spec.version_requirement is not None:
            return None
        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=self.path,
                version=self.version,
                source="env_overlay",
            ),
            reason="registered and active",
        )


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
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command, workdir=workdir, timeout=timeout)
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

    assert result.succeeded is False
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.error_code == "TIMEOUT_SILENT_TIMEOUT"
    assert result.metadata["termination_reason"] == "silent_timeout"
    assert result.metadata["execution_time"] == 1200.0
    assert result.metadata["tool_type"] == "maven"
    assert result.metadata["command"] == orchestrator.monitored_commands[0][0]


def test_maven_fail_at_end_test_reports_failures_despite_ignored_exit_code():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": "\n".join(
                [
                    "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
                    "[INFO] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0",
                    "[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0",
                    "[INFO] BUILD SUCCESS",
                ]
            ),
            "exit_code": 0,
        }
    )
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        fail_at_end=True,
        working_directory="/workspace/project",
    )

    assert result.succeeded is False
    assert result.error_code == "TEST_FAILURE"
    assert result.metadata["analysis"]["ignored_test_failures_detected"] is True
    assert result.metadata["analysis"]["test_failure_count"] == 1
    assert "-Dmaven.test.failure.ignore=true" in orchestrator.monitored_commands[0][0]


def test_maven_success_marker_with_surefire_failures_returns_partial_evidence():
    output = "\n".join(
        [
            "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
            "[INFO] Tests run: 214, Failures: 3, Errors: 0, Skipped: 5",
            "[INFO] BUILD SUCCESS",
            "[INFO] " + "x" * 900,
        ]
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 0})
    tool = MavenTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_maven_success_with_failed_tests")
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        fail_at_end=True,
        working_directory="/workspace/project",
    )

    assert result.succeeded is False
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL
    assert result.test_stats.executed == 214
    assert result.test_stats.failed == 3
    assert result.test_stats.skipped == 5
    assert result.test_stats.passed == 206
    assert result.test_stats.pass_rate == 96.3
    assert result.conflicts == ["maven_success_vs_test_failures"]
    assert result.evidence_refs == ["output_maven_success_with_failed_tests"]
    assert result.metadata["output_ref_id"] == "output_maven_success_with_failed_tests"


def test_maven_surefire_final_results_summary_does_not_double_count():
    output = "\n".join(
        [
            "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
            "[INFO] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0",
            "[INFO] Results:",
            "[INFO] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0",
            "[INFO] BUILD SUCCESS",
            "[INFO] " + "x" * 900,
        ]
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 0})
    tool = MavenTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_maven_final_results")
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        fail_at_end=True,
        working_directory="/workspace/project",
    )

    assert result.succeeded is False
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL
    assert result.test_stats.executed == 2
    assert result.test_stats.failed == 1
    assert result.test_stats.skipped == 0
    assert result.test_stats.passed == 1
    assert result.conflicts == ["maven_success_vs_test_failures"]
    assert result.evidence_refs == ["output_maven_final_results"]
    assert result.metadata["analysis"]["tests_run"] == {
        "total": 2,
        "failures": 1,
        "errors": 0,
        "skipped": 0,
    }
    assert result.metadata["analysis"]["test_failure_count"] == 1
    assert result.metadata["analysis"]["test_error_count"] == 0


def test_maven_explicit_ignore_test_failures_preserves_success_result():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": "\n".join(
                [
                    "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
                    "[INFO] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0",
                    "[INFO] BUILD SUCCESS",
                ]
            ),
            "exit_code": 0,
        }
    )
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        fail_at_end=True,
        ignore_test_failures=True,
        working_directory="/workspace/project",
    )

    assert result.succeeded is True
    assert result.metadata["analysis"]["test_failure_count"] == 1
    assert "ignored_test_failures_detected" not in result.metadata["analysis"]


def test_gradle_success_marker_with_failed_tests_returns_partial_evidence():
    output = "\n".join(
        [
            "> Task :test",
            "214 tests completed, 3 failed, 5 skipped",
            "BUILD SUCCESSFUL in 10s",
            "x" * 900,
        ]
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 0})
    tool = GradleTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_gradle_success_with_failed_tests")

    result = tool.execute(
        tasks="test",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.succeeded is True
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL
    assert result.test_stats.executed == 214
    assert result.test_stats.failed == 3
    assert result.test_stats.skipped == 5
    assert result.test_stats.passed == 206
    assert result.test_stats.pass_rate == 96.3
    assert result.conflicts == ["gradle_success_vs_test_failures"]
    assert result.evidence_refs == ["output_gradle_success_with_failed_tests"]
    assert result.metadata["output_ref_id"] == "output_gradle_success_with_failed_tests"


def test_gradle_test_run_summary_variant_returns_partial_evidence():
    output = "\n".join(
        [
            "> Task :test",
            "Test run: 12 tests, 1 failed, 2 skipped",
            "BUILD SUCCESSFUL in 10s",
            "x" * 900,
        ]
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 0})
    tool = GradleTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_gradle_test_run_summary")

    result = tool.execute(
        tasks="test",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.succeeded is True
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL
    assert result.test_stats.executed == 12
    assert result.test_stats.failed == 1
    assert result.test_stats.skipped == 2
    assert result.test_stats.passed == 9
    assert result.conflicts == ["gradle_success_vs_test_failures"]
    assert result.evidence_refs == ["output_gradle_test_run_summary"]


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

    assert result.succeeded is False
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

    assert result.succeeded is False
    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.error_code == "TIMEOUT_SILENT_TIMEOUT"
    assert result.metadata["termination_reason"] == "silent_timeout"
    assert result.metadata["execution_time"] == 1200.0
    assert result.metadata["tool_type"] == "gradle"
    assert result.metadata["task"] == "test"


def test_gradle_does_not_run_path_gradle_when_manager_cannot_resolve():
    orchestrator = FakeBuildToolOrchestrator()
    tool = GradleTool(orchestrator, toolchain_manager=EmptyToolchainManager())
    tool._install_gradle = lambda working_directory: ToolResult.completed_failure(
        output="",
        error="Gradle unavailable",
        error_code="GRADLE_INSTALLATION_FAILED",
    )

    result = tool.execute(
        tasks="build",
        working_directory="/workspace/project",
        use_wrapper=False,
    )

    assert result.succeeded is False
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

    assert result.succeeded is False
    assert result.error_code == "GRADLE_EXECUTABLE_NOT_RESOLVED"
    assert any(
        "apt-get install -y gradle" in command
        for command, _workdir, _timeout in orchestrator.commands
    )
    assert all(
        "gradle wrapper" not in command for command, _workdir, _timeout in orchestrator.commands
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

    assert result.succeeded is True
    command = orchestrator.monitored_commands[0][0]
    assert "-DskipITs=true" in command
    assert "-Dmaven.test.failure.ignore=true" in command
    assert " -D, " not in command
    assert " -Dm " not in command


def test_maven_fail_at_end_does_not_duplicate_caller_supplied_ignore_property():
    orchestrator = FakeBuildToolOrchestrator()
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        extra_args="-B -Dmaven.test.failure.ignore=true",
        fail_at_end=True,
        working_directory="/workspace/project",
    )

    assert result.succeeded is True
    command = orchestrator.monitored_commands[0][0]
    assert command.count("-Dmaven.test.failure.ignore=true") == 1


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

    assert result.succeeded is True
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

    assert result.succeeded is True
    assert orchestrator.monitored_commands[0][0].startswith("/opt/apache-maven-3.9.8/bin/mvn ")
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

    assert result.succeeded is True
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

    assert result.succeeded is False
    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    assert orchestrator.monitored_commands == []
    # Only READS are allowed before the version-resolution early-return: the
    # JDK pre-flight's manifest, `java` probe and the env overlay that states
    # which java runtime was registered (Plan 7 §B1), plus the runner choice's
    # ./mvnw probe, which decides WHICH runner the resolution is asked for
    # (Plan 7 §A1). Nothing here writes.
    preflight_reads = (
        "build_requirements.json",
        "java -version",
        "env_overlay.json",
        "/mvnw",
        "maven-wrapper.properties",
    )
    assert [
        (command, workdir, timeout)
        for (command, workdir, timeout) in orchestrator.commands
        if not any(marker in command for marker in preflight_reads)
    ] == []
    assert toolchain_manager.seen_specs[0].version_requirement.raw == "3.9.6"
    # The second question is what would resolve WITHOUT the stated requirement,
    # which is the only way the refusal can honestly name the drop-it exit. It
    # is a resolution, not a dispatch: `monitored_commands` above stays empty.
    assert toolchain_manager.seen_specs[1].version_requirement is None
    assert result.metadata["maven_version_requirement"] == {
        "raw": "3.9.6",
        "source": "tool_parameter",
        "kind": "exact",
    }


# ---------------------------------------------------------------------------
# Task #60 shape (b): a model-asserted Maven requirement that nothing satisfies
# names the registered runtime and both exits.
#
# jackrabbit d2r4 (logs/d2r4-20260815/slices/jackrabbit.md slice 9): Maven 3.8.7
# was installed, registered and ACTIVE (`measured_version: "3.8.7"`), and the
# build asserted maven_version_requirement="[3.9,)" — the model's own parameter,
# `source: "tool_parameter"`, not a project constraint. The refusal said "No
# observed executable/version pair satisfies [3.9,)" and "the same Maven
# requirement remains binding on any retry". The model searched for a wrapper,
# found none, and closed the phase blocked. Both exits existed the whole time.
# ---------------------------------------------------------------------------


def _requirement_refusal(manager, requirement="[3.9,)"):
    orchestrator = FakeBuildToolOrchestrator()
    tool = MavenTool(orchestrator, toolchain_manager=manager)
    return tool.execute(
        command="compile",
        working_directory="/workspace/jackrabbit",
        maven_version_requirement=requirement,
    )


def test_an_unsatisfied_model_asserted_requirement_names_the_registered_maven():
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager)

    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    assert result.metadata["registered_maven"] == {
        "executable": "/usr/share/maven/bin/mvn",
        "version": "3.8.7",
        "source": "env_overlay",
    }, "the runtime the container actually has is a typed fact, not only prose"
    stated = " ".join(result.suggestions or ())
    assert "3.8.7" in stated and "/usr/share/maven/bin/mvn" in stated


def test_an_unsatisfied_model_asserted_requirement_names_both_exits():
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager)

    suggestions = list(result.suggestions or ())
    drop = [s for s in suggestions if "maven_version_requirement" in s]
    assert drop, "exit one: the requirement is this call's own, so the call can omit it"
    assert "3.8.7" in drop[0], "and it names what the build would then run on"
    install = [s for s in suggestions if "action='env'" in s]
    assert install, "exit two: hold the requirement by installing a Maven that meets it"
    assert "[3.9,)" in install[0], "which stays the requirement, not a weakened one"
    assert "bin/mvn" in install[0], "named as the canonicalizer will accept it"


def test_a_model_asserted_requirement_is_not_reported_as_binding_on_any_retry():
    """The old third line said the requirement "remains binding on any retry".
    For a requirement that arrived as this call's own parameter that is false —
    and it is the sentence that makes the drop-it exit look unavailable."""
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager)

    assert not any(
        "remains binding" in s for s in (result.suggestions or ())
    ), "a parameter the next call may omit does not bind the next call"


def test_the_requirement_refusal_names_the_provision_that_holds_it():
    """Task #61. The install exit used to read "download the distribution with
    bash, then project(action='env', ...)" — a two-step the model has to
    assemble, and the reason camel's run passed `maven_version` zero times.
    `project(action='provision', maven_version=...)` is now the one call that
    installs, activates and verifies it, so the refusal names THAT."""
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager)

    provision = [s for s in (result.suggestions or ()) if "action='provision'" in s]
    assert provision, "the refusal names the provision that holds the requirement"
    assert "maven_version='3.9'" in provision[0], "with the floor the requirement states"


def test_the_provision_exit_carries_the_floor_of_an_exact_requirement():
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager, requirement="3.9.6")

    provision = [s for s in (result.suggestions or ()) if "action='provision'" in s]
    assert provision and "maven_version='3.9.6'" in provision[0]


@pytest.mark.parametrize(
    "floor, expected",
    [
        ("3.9", "3.9"),  # a line with its own distribution answers itself
        ("3.9.6", "3.9.6"),  # its line's release (3.9.9) satisfies the patch
        ("3.7", "3.8"),  # no 3.7 archive: the nearest line that satisfies it
        ("3.7.1", "3.8"),  # nor does a patch of a line that does not exist
        ("3.8.9", "3.9"),  # 3.8.8 is the last 3.8: the next line answers it
        ("3.4", "3.5"),
        ("3", "3"),  # a bare major resolves to the newest line of it
        ("4.0", None),  # nothing this harness installs satisfies it
        ("3.9.10", "3.9.11"),  # nearest explicitly supported published patch
        ("3.9.12", "3.9.16"),
        ("3.9.17", None),  # no known supported distribution satisfies this floor
        ("", None),
    ],
)
def test_the_installable_floor_is_the_nearest_line_that_satisfies_the_ask(floor, expected):
    assert nearest_installable_floor(floor) == expected


@pytest.mark.parametrize(
    "floor, distribution",
    [
        ("3.9", "3.9.9"),
        ("3.9.6", "3.9.9"),  # the line's release, which satisfies the patch
        ("3.9.11", "3.9.11"),  # preserve the official CI patch when known
        ("3.9.16", "3.9.16"),
        ("3.9.17", None),
        ("3.6.3", "3.6.3"),  # the line's release IS the patch asked for
        ("3.7.1", None),  # no 3.7 line, so no archive answers this patch
        ("3.8.9", None),  # the 3.8 line ended at 3.8.8
        ("3", "3.9.9"),
    ],
)
def test_a_patch_floor_resolves_through_the_same_line_table(floor, distribution):
    """A 3-component floor used to be returned verbatim as its own distribution,
    so `3.7.1` named `apache-maven-3.7.1-bin.tar.gz` — an archive Apache never
    published — and `nearest_installable_floor` then reported it installable.
    Every floor resolves through the lines this harness actually downloads."""
    assert maven_distribution_for_floor(floor) == distribution


def test_a_floor_no_distribution_answers_is_not_named_as_a_provision_move():
    """`floor_from_requirement` reads any lower bound a build states; the
    provision installs only the lines this harness has a distribution for. A
    requirement of `[3.7,)` produced `maven_version='3.7'`, which the provision
    refuses MAVEN_DISTRIBUTION_UNKNOWN — a move that cannot succeed. The nearest
    line that DOES satisfy the requirement is a move that can."""
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager, requirement="[3.7,)")

    provision = [s for s in (result.suggestions or ()) if "action='provision'" in s]
    assert provision, "a floor above the apt Maven still has an install route"
    assert "maven_version='3.7'" not in provision[0], "no distribution answers 3.7"
    assert "maven_version='3.8'" in provision[0], "the nearest line that satisfies it"


def test_a_patch_floor_no_line_answers_is_not_named_as_a_provision_move():
    """The same defect one component deeper: `[3.7.1,)` was handed back as
    `maven_version='3.7.1'`, a move whose only reply is
    MAVEN_DISTRIBUTION_UNKNOWN because no 3.7 archive exists to install."""
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager, requirement="[3.7.1,)")

    provision = [s for s in (result.suggestions or ()) if "action='provision'" in s]
    assert provision, "a patch floor above the apt Maven still has an install route"
    assert "maven_version='3.7.1'" not in provision[0], "no distribution answers 3.7.1"
    assert "maven_version='3.8'" in provision[0], "the nearest line that satisfies it"


def test_a_floor_beyond_every_installable_line_names_no_provision_move():
    """Nothing this harness installs satisfies `[4.0,)`, so the refusal offers
    the exits it has and invents none."""
    manager = RequirementFreeToolchainManager()

    result = _requirement_refusal(manager, requirement="[4.0,)")

    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    assert not any("action='provision'" in s for s in (result.suggestions or ()))
    assert any("action='env'" in s for s in (result.suggestions or ())), "the exits it has remain"


def test_a_project_observed_requirement_keeps_binding_and_offers_no_drop_exit():
    """The exit is honest only because the requirement is the model's own. A
    constraint the project or a build error established is not lifted by
    omitting the parameter, so that exit must not be offered for it."""

    class ObservedRequirementManager(RequirementFreeToolchainManager):
        def observed_requirements(self, name, working_directory=None):
            return [ToolVersionRequirement(raw="[3.9,)", source="build_error", kind="range")]

    result = _requirement_refusal(ObservedRequirementManager(), requirement=None)

    assert result.error_code == "MAVEN_VERSION_NOT_RESOLVED"
    assert not any(
        "maven_version_requirement" in s for s in (result.suggestions or ())
    ), "omitting a parameter cannot lift a constraint the parameter did not create"
    assert any("remains binding" in s for s in (result.suggestions or ()))


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
    tool._install_maven = lambda: install_calls.append(True) or ToolResult.completed_success(
        output="Maven installed",
    )
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
    )

    assert result.succeeded is True
    assert install_calls == [True]
    assert len(toolchain_manager.seen_specs) == 2
    assert toolchain_manager.seen_working_directories == [
        "/workspace/project",
        "/workspace/project",
    ]
    assert orchestrator.monitored_commands[0][0].startswith("/opt/apache-maven-3.9.9/bin/mvn ")
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
    tool._install_maven = lambda: install_calls.append(True) or ToolResult.completed_success(
        output="Maven installed",
    )

    result = tool.execute(
        command="test",
        working_directory="/workspace/project",
    )

    assert result.succeeded is False
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

    assert result.succeeded is True
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

    assert result.succeeded is True
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


def test_maven_tool_does_not_extract_java_enforcer_range_as_maven_requirement():
    requirement = MavenTool.extract_version_requirement_from_output(
        "[ERROR] RequireJavaVersion failed: Detected JDK Version: "
        "11.0.2 is not in the allowed range [17,)."
    )

    assert requirement is None


def test_java_enforcer_failure_does_not_persist_or_block_maven_runtime():
    output = (
        "[ERROR] BUILD FAILURE\n"
        "[ERROR] Rule 0: RequireJavaVersion failed: Detected JDK Version: "
        "11.0.2 is not in the allowed range [17,).\n"
        "[INFO] " + "x" * 900
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 1})
    tool = MavenTool(
        orchestrator,
        toolchain_manager=FakeToolchainManager(
            path="/usr/bin/mvn",
            version="3.8.7",
            source="system",
        ),
    )
    tool.output_storage = FakeOutputStorage("output_java_enforcer_failure")
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_ERROR"
    assert "maven_version_requirement" not in result.metadata
    assert "runtime_contract_persisted" not in result.metadata
    assert result.evidence_refs == ["output_java_enforcer_failure"]
    assert result.metadata["output_ref_id"] == "output_java_enforcer_failure"
    assert "recovery_actions" not in result.metadata
    assert "diagnostic_commands" not in result.metadata
    rendered = "\n".join(result.suggestions)
    assert "Observed active Java version: 11.0.2" in rendered
    assert "Project-declared Java requirement: 17" in rendered
    assert all(
        exact_call not in rendered
        for exact_call in (
            "bash(command=",
            "build(action=",
            "maven(command=",
            "project(action=",
        )
    )
    # Persistence, not traffic. The pre-flight now READS the overlay to compare
    # the java runtime this dispatch runs with the one that was registered; what
    # a JAVA version failure must never do is WRITE Maven evidence into it, so
    # every overlay command this run issues has to be a read.
    overlay_commands = [
        command for command, _workdir, _timeout in orchestrator.commands if "env_overlay" in command
    ]
    assert overlay_commands
    assert all(
        command.startswith(("if test -f ", "cat -- ")) for command in overlay_commands
    ), overlay_commands


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

    assert result.succeeded is False
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

    assert result.succeeded is False
    assert result.metadata["maven_version_requirement"]["raw"] == "[3.9,)"
    assert result.metadata["maven_runtime"] == {
        "executable": "/usr/bin/mvn",
        "version": "3.6.3",
        "source": "system",
    }


def test_maven_failure_preserves_version_facts_without_a_harness_authored_repair_call():
    output = (
        "[ERROR] BUILD FAILURE\n"
        "Rule 0: org.apache.maven.enforcer.rules.version.RequireMavenVersion failed\n"
        "Detected Maven Version: 3.8.7 is not in the allowed range [3.9,).\n"
        "[INFO] " + "x" * 900
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 1})
    tool = MavenTool(
        orchestrator,
        toolchain_manager=FakeToolchainManager(
            path="/usr/bin/mvn",
            version="3.8.7",
            source="system",
        ),
    )
    tool.output_storage = FakeOutputStorage("output_maven_version_failure")
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(
        command="compile",
        working_directory="/workspace/project",
        raw_output=True,
    )

    assert result.succeeded is False
    assert result.output == output
    assert result.raw_output == output
    assert result.error_code == "MAVEN_VERSION_ERROR"
    assert result.metadata["maven_version_requirement"] == {
        "raw": "[3.9,)",
        "source": "build_error",
        "kind": "range",
    }
    assert result.metadata["maven_runtime"] == {
        "executable": "/usr/bin/mvn",
        "version": "3.8.7",
        "source": "system",
    }
    assert result.evidence_refs == ["output_maven_version_failure"]
    assert result.metadata["output_ref_id"] == "output_maven_version_failure"
    assert "recovery_actions" not in result.metadata
    assert "diagnostic_commands" not in result.metadata
    rendered = "\n".join(result.suggestions)
    assert "[3.9,)" in rendered
    assert "/usr/bin/mvn" in rendered
    assert "3.8.7" in rendered
    for exact_call in (
        "bash(command=",
        "build(action=",
        "maven(command=",
        "project(action=",
    ):
        assert exact_call not in rendered


def test_maven_java_failure_does_not_guess_a_runtime_or_emit_a_repair_call():
    output = (
        "[ERROR] BUILD FAILURE\n"
        "java.lang.UnsupportedClassVersionError: unsupported major.minor version"
    )
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 1})
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_ERROR"
    assert result.metadata["maven_runtime"]["executable"] == "/usr/bin/mvn"
    assert "recovery_actions" not in result.metadata
    assert "diagnostic_commands" not in result.metadata
    rendered = "\n".join(result.suggestions)
    assert "does not prove the required major" in rendered
    assert "Java 17" not in rendered
    assert "Java 21" not in rendered
    for exact_call in (
        "bash(command=",
        "build(action=",
        "maven(command=",
        "project(action=",
    ):
        assert exact_call not in rendered


def test_maven_tool_runs_version_command_as_diagnostic_without_pom_validation():
    orchestrator = VersionCommandOrchestrator()
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager("/usr/bin/mvn"))

    result = tool.execute(command="-version", working_directory="/workspace/project")

    assert result.succeeded is True
    assert result.output == "Apache Maven 3.9.6"
    assert orchestrator.monitored_commands == []
    assert ("/usr/bin/mvn -version", "/workspace/project", None) in orchestrator.commands


def test_maven_tool_runs_prefixed_version_command_as_diagnostic():
    orchestrator = VersionCommandOrchestrator()
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager("/usr/bin/mvn"))

    result = tool.execute(command="mvn -version", working_directory="/workspace/project")

    assert result.succeeded is True
    assert result.output == "Apache Maven 3.9.6"
    assert orchestrator.monitored_commands == []
    assert ("/usr/bin/mvn -version", "/workspace/project", None) in orchestrator.commands
