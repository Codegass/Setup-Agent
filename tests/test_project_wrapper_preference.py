# tests/test_project_wrapper_preference.py
"""The project's own build wrapper is the runner (Plan 7 Task A1).

Live evidence (camel, `logs/session_20260727_054707_94153`): the build died
before compiling with `NoSuchMethodError` in
`org.eclipse.aether.SessionData.computeIfAbsent`. The checkout ships an
executable `mvnw` and a `.mvn/wrapper/maven-wrapper.properties` pinning Maven
3.9.11 (verified read-only against the still-running container:
`docker exec sag-c23-camel sh -c 'ls -l /workspace/camel/mvnw; cat
/workspace/camel/.mvn/wrapper/maven-wrapper.properties'`), and we ran a
registered Maven instead — `GradleTool.execute` defaults `use_wrapper=True`
while `MavenTool.execute` defaulted to False.

Scripted-orchestrator style (house pattern, shared with
tests/test_maven_gradle_tool_contracts.py).
"""

from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.toolchain_manager import (
    ResolvedToolExecutable,
    ToolExecutableCandidate,
)

WORKDIR = "/workspace/project"
WRAPPER = f"{WORKDIR}/mvnw"
REGISTERED = "/usr/bin/mvn"

# Verbatim from the live camel checkout (licence header elided).
CAMEL_WRAPPER_PROPERTIES = (
    "wrapperVersion=3.3.2\n"
    "distributionType=bin\n"
    "distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/"
    "apache-maven/3.9.11/apache-maven-3.9.11-bin.zip\n"
)


class WrapperOrchestrator:
    """A checkout whose ./mvnw state and wrapper properties are scripted."""

    def __init__(self, wrapper="executable", properties=CAMEL_WRAPPER_PROPERTIES, builds=None):
        self.wrapper = wrapper  # "executable" | "present" | "absent"
        self.properties = properties
        self.builds = list(builds or [{"output": "[INFO] BUILD SUCCESS", "exit_code": 0}])
        self.commands = []
        self.monitored_commands = []
        self.project_name = None

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if "/mvnw" in command and "EXECUTABLE" in command:
            marker = {
                "executable": "EXECUTABLE",
                "present": "PRESENT",
                "absent": "MISSING",
            }[self.wrapper]
            return {"success": True, "output": marker, "exit_code": 0}
        if "maven-wrapper.properties" in command:
            return {"success": True, "output": self.properties or "", "exit_code": 0}
        if "pom.xml && echo 'EXISTS'" in command:
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if "grep -q '<modules>'" in command:
            return {"success": False, "output": "NO_MODULES", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        return dict(self.builds[min(len(self.monitored_commands), len(self.builds)) - 1])

    @property
    def runners(self):
        """The head token of every physically dispatched Maven command."""
        return [command.split()[0] for command, _kwargs in self.monitored_commands]


class WrapperAwareToolchainManager:
    """Resolves the checkout wrapper only when the spec asks to prefer it."""

    def __init__(self):
        self.seen_specs = []

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_specs.append(spec)
        if spec.prefer_wrapper:
            return ResolvedToolExecutable(
                candidate=ToolExecutableCandidate(
                    name=spec.name,
                    executable=spec.executable,
                    path=WRAPPER,
                    version="3.9.11",
                    source="wrapper",
                ),
                reason="checkout wrapper",
            )
        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=REGISTERED,
                version="3.8.7",
                source="registered",
            ),
            reason="registered maven",
        )


class VersionPinnedToolchainManager(WrapperAwareToolchainManager):
    """A pinned requirement the checkout wrapper cannot satisfy: the resolver
    returns the registered Maven even when the spec prefers the wrapper."""

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_specs.append(spec)
        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=REGISTERED,
                version="3.9.9",
                source="registered",
            ),
            reason="pinned to 3.9.9",
        )


def _run(orchestrator, manager=None, **kwargs):
    manager = manager or WrapperAwareToolchainManager()
    tool = MavenTool(orchestrator, toolchain_manager=manager)
    result = tool.execute(command="test", working_directory=WORKDIR, **kwargs)
    return manager, result


# ---------------------------------------------------------------------------
# which runner
# ---------------------------------------------------------------------------


def test_the_checkout_wrapper_is_the_runner_when_it_exists_and_is_executable():
    orchestrator = WrapperOrchestrator()

    manager, result = _run(orchestrator)

    assert manager.seen_specs[0].prefer_wrapper is True
    assert orchestrator.runners == [WRAPPER]
    assert result.metadata["maven_runner_choice"]["runner"] == "wrapper"
    assert result.metadata["maven_runner_choice"]["wrapper_path"] == WRAPPER


def test_a_checkout_without_a_wrapper_leaves_the_registered_maven_in_charge():
    orchestrator = WrapperOrchestrator(wrapper="absent")

    manager, result = _run(orchestrator)

    assert manager.seen_specs[0].prefer_wrapper is False
    assert orchestrator.runners == [REGISTERED]
    assert "./mvnw" not in (result.output or "")
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["reason"] == f"no ./mvnw in {WORKDIR}"
    assert "wrapper_path" not in choice


def test_an_explicit_use_wrapper_false_from_the_caller_still_wins():
    orchestrator = WrapperOrchestrator()

    manager, result = _run(orchestrator, use_wrapper=False)

    assert manager.seen_specs[0].prefer_wrapper is False
    assert orchestrator.runners == [REGISTERED]
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["reason"] == "the caller passed use_wrapper=False"


# ---------------------------------------------------------------------------
# the visible line
# ---------------------------------------------------------------------------


def test_the_choice_is_visible_and_names_the_pinned_version():
    orchestrator = WrapperOrchestrator()

    _manager, result = _run(orchestrator)

    assert "[toolchain] using the project's own ./mvnw (pins Maven 3.9.11)" in result.output


def test_a_wrapper_that_pins_no_version_is_named_without_a_parenthetical():
    orchestrator = WrapperOrchestrator(properties="distributionType=bin\n")

    _manager, result = _run(orchestrator)

    assert "[toolchain] using the project's own ./mvnw\n" in result.output
    assert "pins Maven" not in result.output


def test_the_pinned_version_is_parsed_out_of_the_distribution_url():
    assert MavenTool._maven_wrapper_pinned_version(CAMEL_WRAPPER_PROPERTIES) == "3.9.11"


def test_an_escaped_distribution_url_still_yields_its_version():
    properties = (
        "distributionUrl=https\\://repo.maven.apache.org/maven2/org/apache/maven/"
        "apache-maven/3.8.8/apache-maven-3.8.8-bin.zip\n"
    )

    assert MavenTool._maven_wrapper_pinned_version(properties) == "3.8.8"


def test_a_distribution_url_stating_no_version_pins_nothing():
    assert (
        MavenTool._maven_wrapper_pinned_version(
            "distributionUrl=https://example.invalid/maven/latest.zip\n"
        )
        is None
    )


def test_a_commented_out_distribution_url_pins_nothing():
    assert (
        MavenTool._maven_wrapper_pinned_version(
            "#distributionUrl=https://repo1/apache-maven-3.9.11-bin.zip\n"
        )
        is None
    )


# ---------------------------------------------------------------------------
# falling back, with the reason on the record
# ---------------------------------------------------------------------------


def test_a_wrapper_that_is_not_executable_falls_back_with_a_recorded_reason():
    orchestrator = WrapperOrchestrator(wrapper="present")

    manager, result = _run(orchestrator)

    assert manager.seen_specs[0].prefer_wrapper is False
    assert orchestrator.runners == [REGISTERED]
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["reason"] == f"{WRAPPER} is not executable"
    assert choice["wrapper_path"] == WRAPPER
    assert "[toolchain] ./mvnw is present but not executable" in result.output


def test_a_resolver_that_rejects_the_wrapper_gets_the_last_word_on_the_record():
    """A version pin can exclude the wrapper the checkout ships; the recorded
    runner is then what the toolchain actually resolved."""
    orchestrator = WrapperOrchestrator()

    _manager, result = _run(orchestrator, manager=VersionPinnedToolchainManager())

    assert orchestrator.runners == [REGISTERED]
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["reason"].endswith(f"; the toolchain resolved {REGISTERED} (registered)")
    assert choice["wrapper_path"] == WRAPPER


def test_a_wrapper_whose_first_invocation_never_starts_falls_back_and_reruns():
    """A wrapper downloads its distribution; losing the network must not lose
    the build."""
    orchestrator = WrapperOrchestrator(
        builds=[
            {
                "output": (
                    "Error: Could not find or load main class "
                    "org.apache.maven.wrapper.MavenWrapperMain"
                ),
                "exit_code": 1,
            },
            {"output": "[INFO] BUILD SUCCESS", "exit_code": 0},
        ]
    )

    _manager, result = _run(orchestrator)

    assert orchestrator.runners == [WRAPPER, REGISTERED]
    assert result.succeeded is True
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["reason"].startswith(f"{WRAPPER} failed to start")
    assert "[toolchain] ./mvnw failed to start" in result.output
    assert result.metadata["maven_runtime"]["executable"] == REGISTERED


def test_a_wrapper_whose_build_merely_fails_is_not_second_guessed():
    """A build error is Maven speaking, not a wrapper that never booted — even
    when its text carries a phrase the launcher markers also match."""
    orchestrator = WrapperOrchestrator(
        builds=[
            {
                "output": (
                    "[INFO] Scanning for projects...\n"
                    "[ERROR] Failed to execute goal on project core: "
                    "/workspace/project/src/gone.java: No such file or directory\n"
                    "[INFO] BUILD FAILURE"
                ),
                "exit_code": 1,
            },
            {"output": "[INFO] BUILD SUCCESS", "exit_code": 0},
        ]
    )

    _manager, result = _run(orchestrator)

    assert orchestrator.runners == [WRAPPER]
    assert result.succeeded is False
    assert result.metadata["maven_runner_choice"]["runner"] == "wrapper"
