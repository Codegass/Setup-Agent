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
2026-08-08 HTTPComponents exposed the prerequisite boundary: Maven Wrapper
3.3.2 changes its download from ``.zip`` to ``.tar.gz`` when ``unzip`` is
missing, but the repository SHA remains the SHA of the ZIP.  Wrapper archive
readiness therefore has to be closed *before* the first runner dispatch.
"""

import json
from types import SimpleNamespace

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import ContainerFS, add_published_mutable_json

import sag.tools.internal.maven_tool as maven_module
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.toolchain_manager import (
    ResolvedToolExecutable,
    ToolchainManager,
    ToolExecutableCandidate,
)
from sag.utils.container_io import WRITE_PERSISTED, ContainerWriteResult

pytestmark = pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")

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

# Verbatim facts from the 2026-08-08 HTTPComponents checkout.  The checksum is
# deliberately part of the fixture: no recovery is allowed to rewrite either
# it or the ZIP URL.
HTTP_WRAPPER_PROPERTIES = (
    "distributionSha256Sum=0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb\n"
    "distributionType=only-script\n"
    "distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/"
    "apache-maven/3.9.11/apache-maven-3.9.11-bin.zip\n"
    "wrapperVersion=3.3.2\n"
)

HTTP_CHECKSUM_FAILURE = (
    "Error: Failed to validate Maven distribution SHA-256, your Maven "
    "distribution might be compromised.\n"
    "If you updated your Maven version, you need to update the specified "
    "distributionSha256Sum property."
)


class WrapperOrchestrator:
    """A checkout whose ./mvnw state and wrapper properties are scripted."""

    def __init__(
        self,
        wrapper="executable",
        properties=CAMEL_WRAPPER_PROPERTIES,
        builds=None,
        *,
        unzip_available=True,
        unzip_install_success=True,
        unzip_install_activates=True,
        properties_read_success=True,
        pom_text="",
    ):
        self.wrapper = wrapper  # "executable" | "present" | "absent"
        self.properties = properties
        self.builds = list(builds or [{"output": "[INFO] BUILD SUCCESS", "exit_code": 0}])
        self.unzip_available = unzip_available
        self.unzip_install_success = unzip_install_success
        self.unzip_install_activates = unzip_install_activates
        self.properties_read_success = properties_read_success
        self.pom_text = pom_text
        self.commands = []
        self.monitored_commands = []
        self.files = {}
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

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command, workdir=workdir, timeout=timeout)
        if "/mvnw" in command and "EXECUTABLE" in command:
            marker = {
                "executable": "EXECUTABLE",
                "present": "PRESENT",
                "absent": "MISSING",
            }[self.wrapper]
            return {"success": True, "output": marker, "exit_code": 0}
        if "maven-wrapper.properties" in command:
            return {
                "success": self.properties_read_success,
                "output": self.properties if self.properties_read_success else "read failed",
                "exit_code": 0 if self.properties_read_success else -1,
            }
        if command == "command -v unzip":
            if self.unzip_available:
                return {"success": True, "output": "/usr/bin/unzip\n", "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 127}
        if command == "command -v tar":
            return {"success": True, "output": "/usr/bin/tar\n", "exit_code": 0}
        if command.startswith("DEBIAN_FRONTEND=noninteractive apt-get update"):
            return {"success": True, "output": "", "exit_code": 0}
        if command == "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip":
            if self.unzip_install_success:
                if self.unzip_install_activates:
                    self.unzip_available = True
                return {"success": True, "output": "installed", "exit_code": 0}
            return {"success": False, "output": "install failed", "exit_code": 100}
        if command == f"cat {WORKDIR}/pom.xml":
            return {
                "success": bool(self.pom_text),
                "output": self.pom_text,
                "exit_code": 0 if self.pom_text else 1,
            }
        if "pom.xml && echo 'EXISTS'" in command:
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if "grep -q '<modules>'" in command:
            return {"success": False, "output": "NO_MODULES", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        if (
            command.split()[0] == WRAPPER
            and not self.unzip_available
            and "-bin.zip" in self.properties
        ):
            return {"output": HTTP_CHECKSUM_FAILURE, "exit_code": 1}
        return dict(self.builds[min(len(self.monitored_commands), len(self.builds)) - 1])

    @property
    def runners(self):
        """The head token of every physically dispatched Maven command."""
        return [command.split()[0] for command, _kwargs in self.monitored_commands]


class WrapperAwareToolchainManager:
    """Resolves the checkout wrapper only when the spec asks to prefer it."""

    def __init__(self, *, registered_version="3.8.7"):
        self.seen_specs = []
        self.registered_version = registered_version

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
                version=self.registered_version,
                source="registered",
            ),
            reason="registered maven",
        )

    def matches_requirement(self, version, requirement):
        return ToolchainManager(None).matches_requirement(version, requirement)


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


def _persist_prerequisite_state(monkeypatch):
    """Make the shared atomic writer observable to the in-memory orchestrator."""

    def write(orchestrator, path, content, **_kwargs):
        orchestrator.files[path] = content
        return ContainerWriteResult(
            persisted=True,
            code=WRITE_PERSISTED,
            bytes_written=len(content.encode("utf-8")),
        )

    monkeypatch.setattr(maven_module, "write_container_text_atomic", write)


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
# wrapper archive readiness precedes every runner dispatch (Plan WS5)
# ---------------------------------------------------------------------------


def test_http_zip_archive_facts_keep_the_repository_checksum_domain():
    facts = MavenTool._maven_wrapper_distribution_facts(HTTP_WRAPPER_PROPERTIES)

    assert facts == {
        "archive_type": "zip",
        "checksum_domain": "distribution_url:zip",
        "distribution_sha256": ("0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb"),
        "distribution_url": (
            "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
            "3.9.11/apache-maven-3.9.11-bin.zip"
        ),
    }


def test_tar_gz_distribution_requires_tar_without_touching_unzip():
    properties = (
        "distributionUrl=https://example.invalid/apache-maven-3.9.11-bin.tar.gz\n"
        "distributionSha256Sum=tar-domain-sha\n"
    )
    orchestrator = WrapperOrchestrator(properties=properties, unzip_available=False)

    _manager, result = _run(orchestrator)

    assert result.succeeded is True
    choice = result.metadata["maven_runner_choice"]
    assert choice["archive_type"] == "tar.gz"
    assert choice["checksum_domain"] == "distribution_url:tar.gz"
    assert choice["prerequisite"] == {
        "executable": "tar",
        "package": "tar",
        "provision_attempted": False,
        "status": "available",
    }
    assert not any(
        "apt-get install" in command for command, _workdir, _timeout in orchestrator.commands
    )


def test_missing_unzip_is_provisioned_before_http_wrapper_dispatch(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
    )

    _manager, result = _run(orchestrator)

    assert result.succeeded is True
    assert orchestrator.runners == [WRAPPER]
    install_index = next(
        index
        for index, (command, _workdir, _timeout) in enumerate(orchestrator.commands)
        if command == "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip"
    )
    final_probe_index = max(
        index
        for index, (command, _workdir, _timeout) in enumerate(orchestrator.commands)
        if command == "command -v unzip"
    )
    assert install_index < final_probe_index
    choice = result.metadata["maven_runner_choice"]
    assert choice["archive_type"] == "zip"
    assert choice["distribution_url"].endswith("apache-maven-3.9.11-bin.zip")
    assert choice["distribution_sha256"] == (
        "0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb"
    )
    assert choice["prerequisite"] == {
        "executable": "unzip",
        "package": "unzip",
        "provision_attempted": True,
        "status": "provisioned",
    }
    assert choice["fallback_reason"] is None


def test_unpersisted_provision_attempt_never_runs_apt_or_wrapper(monkeypatch):
    def refuse_write(*_args, **_kwargs):
        return ContainerWriteResult(False, "transport_write_failed")

    monkeypatch.setattr(maven_module, "write_container_text_atomic", refuse_write)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.8.7")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.error_code == "prerequisite_executable_missing:unzip"
    assert orchestrator.runners == []
    assert result.metadata["maven_runner_choice"]["prerequisite"]["status"] == ("state_unpersisted")
    assert not any("apt-get" in command for command, _workdir, _timeout in orchestrator.commands)


def test_unreadable_wrapper_properties_dispatches_no_runner(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        properties_read_success=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.8.7")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.error_code == "wrapper_properties_unreadable"
    assert orchestrator.runners == []
    choice = result.metadata["maven_runner_choice"]
    assert choice["properties_status"] == "unreadable"
    assert choice["prerequisite"]["status"] == "properties_unreadable"


def test_claimed_install_without_post_probe_executable_dispatches_no_wrapper(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=True,
        unzip_install_activates=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.8.7")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.error_code == "prerequisite_executable_missing:unzip"
    assert orchestrator.runners == []
    assert result.metadata["maven_runner_choice"]["prerequisite"]["status"] == ("install_failed")


def test_wrapper_prerequisite_provision_is_one_shot(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.8.7")
    tool = MavenTool(orchestrator, toolchain_manager=manager)

    first = tool.execute(command="test", working_directory=WORKDIR)
    second = tool.execute(command="test", working_directory=WORKDIR)

    assert first.error_code == "prerequisite_executable_missing:unzip"
    assert second.error_code == "prerequisite_executable_missing:unzip"
    assert orchestrator.runners == []
    assert (
        sum(
            command == "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip"
            for command, _workdir, _timeout in orchestrator.commands
        )
        == 1
    )
    state = json.loads(orchestrator.files[MavenTool.WRAPPER_PREREQUISITE_STATE["unzip"]])
    assert state["install_attempted"] is True
    assert state["status"] == "failed"


def test_failed_unzip_provision_falls_back_only_to_exact_wrapper_version(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.9.11")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.succeeded is True
    assert orchestrator.runners == [REGISTERED]
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "registered"
    assert choice["fallback_reason"] == (
        "unzip provisioning failed; registered Maven 3.9.11 exactly matches " "wrapper Maven 3.9.11"
    )
    assert choice["prerequisite"]["status"] == "install_failed"


def test_failed_unzip_provision_uses_explicit_project_range_as_compatibility_proof(
    monkeypatch,
):
    _persist_prerequisite_state(monkeypatch)
    pom = """
    <project><build><plugins><plugin><configuration><rules>
      <requireMavenVersion><version>[3.9,4.0)</version></requireMavenVersion>
    </rules></configuration></plugin></plugins></build></project>
    """
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=False,
        pom_text=pom,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.9.9")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.succeeded is True
    assert orchestrator.runners == [REGISTERED]
    choice = result.metadata["maven_runner_choice"]
    assert choice["fallback_reason"] == (
        "unzip provisioning failed; registered Maven 3.9.9 satisfies "
        "project-declared range [3.9,4.0)"
    )


def test_failed_unzip_provision_without_compatibility_proof_dispatches_no_runner(
    monkeypatch,
):
    _persist_prerequisite_state(monkeypatch)
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=False,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.8.7")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.succeeded is False
    assert result.error_code == "prerequisite_executable_missing:unzip"
    assert orchestrator.runners == []
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "unavailable"
    assert choice["fallback_reason"] == (
        "registered Maven 3.8.7 neither exactly matches wrapper Maven 3.9.11 "
        "nor satisfies an explicit project Maven range"
    )
    assert HTTP_WRAPPER_PROPERTIES.splitlines()[0] in orchestrator.properties
    assert "apache-maven-3.9.11-bin.zip" in orchestrator.properties
    assert "checksum" not in result.output.lower()


def test_commented_project_range_cannot_authorize_registered_fallback(monkeypatch):
    _persist_prerequisite_state(monkeypatch)
    pom = """
    <project><build><plugins><plugin><configuration><rules>
      <!-- <requireMavenVersion><version>[3.8,4.0)</version></requireMavenVersion> -->
    </rules></configuration></plugin></plugins></build></project>
    """
    orchestrator = WrapperOrchestrator(
        properties=HTTP_WRAPPER_PROPERTIES,
        unzip_available=False,
        unzip_install_success=False,
        pom_text=pom,
    )
    manager = WrapperAwareToolchainManager(registered_version="3.9.9")

    _manager, result = _run(orchestrator, manager=manager)

    assert result.error_code == "prerequisite_executable_missing:unzip"
    assert orchestrator.runners == []


def test_standard_container_environment_includes_unzip_before_any_project_run():
    orchestrator = object.__new__(DockerOrchestrator)
    orchestrator.config = SimpleNamespace(workspace_path="/workspace")
    commands = []

    # Environment setup now runs on the clean control transport; a fake that
    # cannot accept its call shape (``_clean_control_path`` et al.) fails
    # closed, so the double takes the full production signature.
    def execute(command, workdir=None, **kwargs):
        commands.append((command, workdir))
        return {"success": True, "output": "ok", "exit_code": 0}

    orchestrator.execute_command = execute

    assert orchestrator._setup_container_environment() is True
    essential_install = next(
        command for command, _workdir in commands if command.startswith("apt-get install -y -qq")
    )
    assert "unzip" in essential_install.split()
    assert {"procps", "util-linux", "coreutils", "iproute2"}.issubset(
        set(essential_install.split())
    )
    assert any(
        command == "command -v setsid ps sha256sum timeout base64 >/dev/null"
        for command, _workdir in commands
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


def test_a_wrapper_failure_after_dispatch_never_switches_runners():
    """Fallback proof is a pre-dispatch policy, never a second build attempt."""
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

    assert orchestrator.runners == [WRAPPER]
    assert result.succeeded is False
    choice = result.metadata["maven_runner_choice"]
    assert choice["runner"] == "wrapper"
    assert choice["fallback_reason"] is None
    assert result.metadata["maven_runtime"]["executable"] == WRAPPER


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
