"""A version-targeted Maven provision installs, activates, and verifies.

Live evidence, d2r4.

camel (`logs/d2r4-20260815/slices/camel.md`): the repo pins the build
extensions `eu.maveniverse.maven.nisse` 0.8.4, the container ran the apt Maven
(receipt `inv-maven-1-0420ee7725d8-0002`, `toolchain_fingerprint.version`
"Apache Maven 3.8.7"), and the reactor printed `BUILD SUCCESS` and then died —

    Exception in thread "main" java.lang.NoSuchMethodError:
    'java.lang.Object org.eclipse.aether.SessionData.computeIfAbsent(...)'
        at eu.maveniverse.maven.nisse.extension3.internal.NissePropertyInliner...
        at org.apache.maven.DefaultMaven.afterSessionEnd(DefaultMaven.java:360)

exit 1, zero report paths, `compiled_classes 0`. Nothing in the run named a
Maven version: `maven_version` occurs 0 times in the whole control-events file,
and `action_envelope` with `tool == "bash"` is 0 too. There was no way to ask
for a different Maven, so nobody asked.

jackrabbit (`logs/d2r4-20260815/slices/jackrabbit.md`) is the same hole from
the other side: apt gave 3.8.7, the model held `[3.9,)`, and the only route the
harness could name was "download it yourself with bash".

So `project(action='provision', maven_version='3.9')` exists now, and it holds
the discipline task #59 established for the JDK: prove the exact binary by
path before anything moves, land the switch in the domain every later dispatch
resolves, and verify with a bare `mvn` resolved exactly as a dispatch resolves
it. A provision that cannot show the domain answering for its claim is a
refusal, not a configured runtime.
"""

import json
import posixpath
import re

import sag.agent.evidence_assessments as assessments_module
from sag.agent.control_ownership import BlockerOwner, blocker_owner_for_assessment
from sag.agent.evidence_assessments import (
    assess_dispatch,
    maven_extension_incompatibility,
    validate_assessment_v2,
)
from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON, EnvOverlayStore
from sag.tools.internal.system_tool import PROVISION_ACTIVATION_STATE_MARKER, SystemTool
from sag.tools.project_tool import ProjectTool

SYSTEM_MVN = "/usr/bin/mvn"
APT_VERSION = "3.8.7"
DISTRIBUTION = "3.9.9"
DISTRIBUTION_HOME = f"/opt/apache-maven-{DISTRIBUTION}"
DISTRIBUTION_BIN = f"{DISTRIBUTION_HOME}/bin/mvn"

_EXACT_MVN_RE = re.compile(r"(/\S+)/bin/mvn -version")
_TAR_RE = re.compile(r"tar -xzf \S*apache-maven-([0-9][0-9.]*)-bin\.tar\.gz -C (\S+)")


def _banner(version: str, home: str) -> str:
    return (
        f"Apache Maven {version} (8e8579a9e76f7d015ee5ec7bfcdc97d260186937)\n"
        f"Maven home: {home}\n"
        "Java version: 21.0.9, vendor: Ubuntu\n"
        "Default locale: en, platform encoding: UTF-8"
    )


class FakeMavenContainer:
    """A container that resolves `mvn` the way a dispatch resolves it.

    PATH resolution is not this double's invention: `execute_command` builds
    every project command's environment from the persisted overlay
    (`_default_exec_environment` -> `EnvOverlayStore.authorized_environment`),
    so the ACTIVE candidate's bin directory fronts /usr/bin. The download is
    mocked at the one seam the tool has for it — the shell command that fetches
    and untars the Apache distribution.
    """

    def __init__(
        self,
        *,
        system_version=APT_VERSION,
        download_succeeds=True,
        extracted_answers=None,
        frozen=False,
        overlay_writable=True,
    ):
        self.system_version = system_version
        self.download_succeeds = download_succeeds
        # A distribution directory whose binary answers for another version
        # than its name promises — the one thing an exact-path probe catches
        # and a PATH lookup cannot.
        self.extracted_answers = dict(extracted_answers or {})
        # A container whose resolution refuses to move, whatever is written:
        # the lucene shape from task #59, held fixed so a seal over it would
        # be observable.
        self.frozen = frozen
        self.overlay_writable = overlay_writable
        self.installed: dict[str, str] = {}
        self.downloads: list[str] = []
        self.commands: list[str] = []
        self.removed: list[str] = []
        self.files: dict[str, str] = {}

    # -- the container's own resolution -------------------------------------
    def resolved_mvn(self):
        if self.frozen:
            return SYSTEM_MVN
        raw = self.files.get(DEFAULT_OVERLAY_JSON)
        if raw:
            active = json.loads(raw).get("tools", {}).get("maven", {}).get("active")
            if active:
                return active
        return SYSTEM_MVN

    def version_of(self, executable):
        home = posixpath.dirname(posixpath.dirname(executable or ""))
        if executable == SYSTEM_MVN:
            return self.system_version
        return self.installed.get(home)

    def resolved_version(self):
        return self.version_of(self.resolved_mvn())

    # -- command surface ----------------------------------------------------
    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        self.commands.append(command)

        if command.startswith("rm -rf "):
            for path in command.split()[2:]:
                self.removed.append(path)
                self.installed.pop(path, None)
            return {"success": True, "output": "", "exit_code": 0}

        tar = _TAR_RE.search(command)
        if tar:
            version, root = tar.groups()
            self.downloads.append(version)
            if not self.download_succeeds:
                return {
                    "success": False,
                    "output": f"curl: (22) The requested URL returned error: 404\ntar: Error",
                    "exit_code": 2,
                }
            home = f"{root}/apache-maven-{version}"
            self.installed[home] = self.extracted_answers.get(version, version)
            return {"success": True, "output": "", "exit_code": 0}

        executable = re.fullmatch(r"test -x (\S+) && echo 'present'", command)
        if executable:
            path = executable.group(1)
            present = self.version_of(path) is not None
            return {
                "success": present,
                "output": "present" if present else "",
                "exit_code": 0 if present else 1,
            }

        if "mvn -version" in command:
            return self._version_block(command)

        return {"success": True, "output": "", "exit_code": 0}

    def _version_block(self, command):
        """Answer a version probe exactly as the container would.

        An absolute path names one exact distribution; a bare `mvn` is whatever
        this container's PATH resolves right now.
        """
        exact = _EXACT_MVN_RE.search(command)
        executable = f"{exact.group(1)}/bin/mvn" if exact else self.resolved_mvn()
        version = self.version_of(executable)
        if version is None:
            return {"success": False, "output": "mvn: command not found", "exit_code": 127}
        lines = []
        if "command -v mvn" in command:
            lines.append(executable)
        lines.append(_banner(version, posixpath.dirname(posixpath.dirname(executable))))
        return {"success": True, "output": "\n".join(lines), "exit_code": 0}

    def write_file(self, path, content):
        if not self.overlay_writable:
            return {"success": False, "output": "read-only file system", "exit_code": 1}
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def _provision(container, floor="3.9"):
    return SystemTool(container).execute(action="install_maven", maven_version=floor)


# ---------------------------------------------------------------------------
# (a) the provision itself
# ---------------------------------------------------------------------------


def test_a_maven_provision_installs_activates_and_verifies_the_floor():
    """The jackrabbit shape: apt's 3.8.7 is active and `[3.9,)` is wanted."""
    container = FakeMavenContainer()

    result = _provision(container)

    assert result.succeeded is True
    assert container.downloads == [DISTRIBUTION]
    assert result.metadata["maven_version"] == DISTRIBUTION
    assert result.metadata["maven_version_floor"] == "3.9"
    assert result.metadata["maven_home"] == DISTRIBUTION_HOME
    assert result.metadata["verified_maven_version"] == DISTRIBUTION
    assert result.metadata["resolved_maven_executable"] == DISTRIBUTION_BIN
    # The block the model reads names the Maven that will now run, and only it.
    assert DISTRIBUTION in result.output
    assert APT_VERSION not in result.output


def test_a_subsequent_dispatch_resolves_the_provisioned_maven():
    container = FakeMavenContainer()

    _provision(container)

    environment = EnvOverlayStore(container).authorized_environment({})
    assert environment["PATH"].split(":")[0] == f"{DISTRIBUTION_HOME}/bin"
    assert environment["MAVEN_HOME"] == DISTRIBUTION_HOME
    # And the container itself: bare `mvn`, resolved the dispatch way.
    assert container.resolved_mvn() == DISTRIBUTION_BIN
    assert container.resolved_version() == DISTRIBUTION


def test_a_domain_that_did_not_take_the_switch_is_not_sealed_as_one_that_did():
    """#59's law, for Maven: an activation the domain does not answer for is
    a refusal, never a configured runtime."""
    container = FakeMavenContainer(frozen=True)

    result = _provision(container)

    assert result.succeeded is False
    assert result.error_code == "MAVEN_VERSION_VERIFICATION_MISMATCH"
    assert result.metadata["verified_maven_version"] == APT_VERSION
    assert result.metadata.get("maven_version") != DISTRIBUTION


def test_the_post_activation_refusal_states_the_container_it_actually_left():
    """The switch landed and the domain still answered for the old Maven. A
    refusal is right; describing the container as unswitched is not."""
    container = FakeMavenContainer(frozen=True)

    result = _provision(container)

    assert result.succeeded is False
    stated = " ".join([result.error or "", *(result.suggestions or ())])
    assert "activation persisted; verification refused the seal" in stated


def test_the_post_activation_refusal_leaves_a_typed_activation_marker():
    container = FakeMavenContainer(frozen=True)

    result = _provision(container)

    assert result.metadata[PROVISION_ACTIVATION_STATE_MARKER] == {
        "tool": "maven",
        "state": "activated",
        "executable": DISTRIBUTION_BIN,
        "home": DISTRIBUTION_HOME,
        "requested": "3.9",
        "verified": APT_VERSION,
    }
    assert (
        json.loads(container.files[DEFAULT_OVERLAY_JSON])["tools"]["maven"]["active"]
        == DISTRIBUTION_BIN
    )


def test_a_pre_activation_refusal_carries_no_activation_marker():
    container = FakeMavenContainer(extracted_answers={DISTRIBUTION: APT_VERSION})

    result = _provision(container)

    assert result.succeeded is False
    stated = " ".join([result.error or "", *(result.suggestions or ())])
    assert "activation persisted" not in stated
    assert PROVISION_ACTIVATION_STATE_MARKER not in (result.metadata or {})


# ---------------------------------------------------------------------------
# A failed install leaves nothing behind for the resolver to prefer
#
# `ToolchainManager` discovers Maven with
# `find /workspace /tmp /opt /usr/local -path '*/apache-maven-*/bin/mvn'`. An
# extraction that produced no runnable mvn, or one whose own probe failed, is
# exactly the tree that find will hand back to the next resolution — a Maven
# this provision already refused, now arriving as a discovered candidate.
# ---------------------------------------------------------------------------


class _BrokenExtractionContainer(FakeMavenContainer):
    """The tar lands the directory; the binary inside it is not usable."""

    def __init__(self, *, binary_present, **kwargs):
        super().__init__(**kwargs)
        self.binary_present = binary_present

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        if not self.binary_present and re.fullmatch(r"test -x (\S+) && echo 'present'", command):
            self.commands.append(command)
            return {"success": False, "output": "", "exit_code": 1}
        if self.binary_present and f"{DISTRIBUTION_BIN} -version" in command:
            self.commands.append(command)
            return {"success": False, "output": "Error: JAVA_HOME is not set", "exit_code": 1}
        return super().execute_command(command, workdir, timeout, truncate_output)


def test_an_extraction_with_no_runnable_binary_is_removed_from_opt():
    container = _BrokenExtractionContainer(binary_present=False)

    result = _provision(container)

    assert result.error_code == "MAVEN_BINARY_NOT_FOUND"
    assert DISTRIBUTION_HOME in container.removed


def test_a_distribution_that_failed_its_own_probe_is_removed_from_opt():
    container = _BrokenExtractionContainer(binary_present=True)

    result = _provision(container)

    assert result.error_code == "MAVEN_CONFIG_FAILED"
    assert DISTRIBUTION_HOME in container.removed


def test_a_successful_provision_removes_nothing():
    container = FakeMavenContainer()

    result = _provision(container)

    assert result.succeeded is True
    assert container.removed == []


def test_a_distribution_whose_binary_answers_another_version_moves_nothing():
    """The refusal that precedes the switch leaves the container as it was."""
    container = FakeMavenContainer(extracted_answers={DISTRIBUTION: APT_VERSION})

    result = _provision(container)

    assert result.succeeded is False
    assert result.error_code == "MAVEN_VERSION_VERIFICATION_MISMATCH"
    assert result.metadata["verified_maven_version"] == APT_VERSION
    assert DEFAULT_OVERLAY_JSON not in container.files
    assert container.resolved_mvn() == SYSTEM_MVN


def test_an_activation_that_did_not_persist_is_not_sealed_as_success():
    container = FakeMavenContainer(overlay_writable=False)

    result = _provision(container)

    assert result.succeeded is False
    assert result.error_code == "MAVEN_RUNTIME_ACTIVATION_FAILED"
    assert DISTRIBUTION in result.error


def test_a_download_that_failed_is_a_refusal_and_registers_nothing():
    container = FakeMavenContainer(download_succeeds=False)

    result = _provision(container)

    assert result.succeeded is False
    assert result.error_code == "MAVEN_INSTALL_FAILED"
    assert DEFAULT_OVERLAY_JSON not in container.files


def test_a_resolved_maven_that_already_satisfies_the_floor_downloads_nothing():
    container = FakeMavenContainer(system_version="3.9.11")

    result = _provision(container)

    assert result.succeeded is True
    assert container.downloads == []
    assert result.metadata["verified_maven_version"] == "3.9.11"
    assert result.metadata["already_active"] is True


def test_a_floor_no_distribution_is_known_for_is_refused_before_any_download():
    container = FakeMavenContainer()

    result = _provision(container, floor="9.9")

    assert result.succeeded is False
    assert result.error_code == "MAVEN_DISTRIBUTION_UNKNOWN"
    assert container.downloads == []
    # The refusal names what this call CAN install, so the next one can ask.
    assert "3.9" in " ".join(result.suggestions or ())


def test_a_patch_level_floor_installs_exactly_that_distribution():
    """A floor naming a patch names its own distribution: the lowest release
    that satisfies `3.6.3` is 3.6.3."""
    container = FakeMavenContainer(system_version="3.5.4")

    result = _provision(container, floor="3.6.3")

    assert result.succeeded is True
    assert container.downloads == ["3.6.3"]
    assert result.metadata["maven_version"] == "3.6.3"


# ---------------------------------------------------------------------------
# the facade: project(action='provision', maven_version=...)
# ---------------------------------------------------------------------------


def test_the_project_facade_routes_maven_version_to_the_maven_provision():
    container = FakeMavenContainer()
    facade = ProjectTool(system_tool=SystemTool(container))

    result = facade.execute(action="provision", maven_version="3.9")

    assert result.succeeded is True
    assert container.downloads == [DISTRIBUTION]


def test_the_facade_schema_names_maven_version():
    """A parameter the model cannot see is a parameter the model cannot use:
    camel's whole run passed `maven_version` exactly zero times."""
    schema = ProjectTool()._get_parameters_schema()

    assert "maven_version" in schema["properties"]


def test_a_provision_asked_for_two_toolchains_at_once_drops_neither():
    """One call routes to one delegate action. Silently ignoring the other
    parameter would seal a toolchain that was never installed."""
    container = FakeMavenContainer()
    facade = ProjectTool(system_tool=SystemTool(container))

    result = facade.execute(action="provision", java_version="17", maven_version="3.9")

    assert result.succeeded is False
    assert result.error_code == "PROJECT_PROVISION_AMBIGUOUS"
    assert container.downloads == []
    stated = " ".join(result.suggestions or ())
    assert "java_version" in stated and "maven_version" in stated


def test_a_provision_asked_for_maven_and_apt_packages_at_once_drops_neither():
    """The same law for the other pair the router silently resolved: maven_version
    routed to install_maven and `packages` went nowhere — a call whose apt half
    was never run and never refused."""
    container = FakeMavenContainer()
    facade = ProjectTool(system_tool=SystemTool(container))

    result = facade.execute(action="provision", packages=["git"], maven_version="3.9")

    assert result.succeeded is False
    assert result.error_code == "PROJECT_PROVISION_AMBIGUOUS"
    assert container.downloads == []
    stated = " ".join(result.suggestions or ())
    assert "packages" in stated and "maven_version" in stated


def test_maven_version_alone_still_routes_to_the_maven_provision():
    container = FakeMavenContainer()
    facade = ProjectTool(system_tool=SystemTool(container))

    assert facade.execute(action="provision", maven_version="3.9").succeeded is True


# ---------------------------------------------------------------------------
# (b) the typed assessment for the failure that motivated the provision
# ---------------------------------------------------------------------------

CAMEL_NISSE_CRASH = (
    "[INFO] BUILD SUCCESS\n"
    "[INFO] Total time:  22:24 min\n"
    "---------------------------------------------------\n"
    'Exception in thread "main" java.lang.NoSuchMethodError: \'java.lang.Object '
    "org.eclipse.aether.SessionData.computeIfAbsent(java.lang.Object, "
    "java.util.function.Supplier)'\n"
    "\tat eu.maveniverse.maven.nisse.extension3.internal.NissePropertyInliner"
    ".inlinedPoms(NissePropertyInliner.java:109)\n"
    "\tat eu.maveniverse.maven.nisse.extension3.internal.NisseLifecycleParticipant"
    ".afterSessionEnd(NisseLifecycleParticipant.java:80)\n"
    "\tat org.apache.maven.DefaultMaven.afterSessionEnd(DefaultMaven.java:360)\n"
    "\tat org.apache.maven.cli.MavenCli.main(MavenCli.java:196)\n"
)

MAVEN_RECEIPT = {
    "receipt_id": "inv-maven-1-0420ee7725d8-0002",
    "outcome": "failed",
    "exit_code": 1,
}


def test_the_nisse_shape_is_typed_and_names_the_provision_remedy():
    (assessment,) = maven_extension_incompatibility(MAVEN_RECEIPT, CAMEL_NISSE_CRASH)

    assert assessment.typed_code == "maven_extension_incompatible"
    assert assessment.receipt_id == "inv-maven-1-0420ee7725d8-0002"
    assert "eu.maveniverse.maven.nisse" in assessment.detail
    assert "org.eclipse.aether.SessionData" in assessment.detail
    # The remedy, named as the one call that performs it.
    assert "project(action='provision', maven_version='3.9')" in assessment.detail


def test_the_finding_survives_the_reader_that_grades_persisted_assessments():
    """An assessment the ledger reader rejects is not evidence of anything."""
    (assessment,) = maven_extension_incompatibility(MAVEN_RECEIPT, CAMEL_NISSE_CRASH)
    payload = assessment.payload()

    validated = validate_assessment_v2(payload, expected_id=payload["assessment_id"])

    assert validated["typed_code"] == "maven_extension_incompatible"
    assert validated["blocker_owner"] == "project"


def test_the_typed_code_is_project_owned():
    """Ownership answers who may act next, and the next actor here is the
    model's own provision call."""
    assert blocker_owner_for_assessment("maven_extension_incompatible") is BlockerOwner.PROJECT


def test_a_maven_that_already_satisfies_the_floor_is_not_told_to_provision():
    """The remedy is a version move. Against a Maven that already has the API
    the extension wants, the same crash means something else, and naming the
    provision would be inventing a diagnosis."""
    output = f"Apache Maven 3.9.11 (abcdef)\n{CAMEL_NISSE_CRASH}"

    assert maven_extension_incompatibility(MAVEN_RECEIPT, output) == []


def test_the_extension_named_is_the_one_under_the_crash_not_an_earlier_stack():
    """The frame search started at offset 0, so any non-core `at` line printed
    EARLIER in a long reactor log named the extension. A stack trace above the
    NoSuchMethodError belongs to a different failure; the caller this
    assessment is about is the frame beneath the match."""
    output = (
        "[ERROR] Failed to execute goal on project widget\n"
        "\tat com.example.unrelated.EarlierFailure.run(EarlierFailure.java:31)\n"
        f"{CAMEL_NISSE_CRASH}"
    )

    (assessment,) = maven_extension_incompatibility(MAVEN_RECEIPT, output)

    assert "eu.maveniverse.maven.nisse" in assessment.detail
    assert "com.example.unrelated" not in assessment.detail


def test_a_resolver_crash_with_no_frame_beneath_it_states_nothing():
    """Whatever precedes the match cannot supply the caller."""
    output = (
        "\tat com.example.unrelated.EarlierFailure.run(EarlierFailure.java:31)\n"
        'Exception in thread "main" java.lang.NoSuchMethodError: \'java.lang.Object '
        "org.eclipse.aether.SessionData.computeIfAbsent(java.lang.Object)'\n"
    )

    assert maven_extension_incompatibility(MAVEN_RECEIPT, output) == []


def test_a_no_such_method_error_outside_the_resolver_api_states_nothing():
    output = (
        "java.lang.NoSuchMethodError: 'void com.example.Widget.paint()'\n"
        "\tat com.example.App.main(App.java:12)\n"
    )

    assert maven_extension_incompatibility(MAVEN_RECEIPT, output) == []


def test_no_output_states_nothing():
    assert maven_extension_incompatibility(MAVEN_RECEIPT, None) == []
    assert maven_extension_incompatibility(MAVEN_RECEIPT, "") == []
    assert maven_extension_incompatibility({}, CAMEL_NISSE_CRASH) == []


def test_the_dispatch_assessor_carries_the_finding(monkeypatch):
    """A rider nothing calls is a rider nothing records."""
    monkeypatch.setattr(assessments_module, "write_assessment", lambda _execute, _finding: True)

    landed = assess_dispatch(
        lambda _command: {},
        contract=None,
        receipt=MAVEN_RECEIPT,
        output=CAMEL_NISSE_CRASH,
    )

    assert "maven_extension_incompatible" in [finding.typed_code for finding in landed]
