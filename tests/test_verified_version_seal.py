"""A runtime registration may not seal a version its own evidence disproves.

Two shapes of the same defect, both from the D2 battery.

**Provision (camel-quarkus, d2r3 serial, control_events seq 125/127).**
`project(action='provision', java_version='17')` returned
`operation_outcome: success` with `metadata.java_version: "17"`, and the
stored output behind that result reads, verbatim:

    Successfully installed and configured Java 17

    JAVA_HOME: /usr/lib/jvm/java-17-openjdk-arm64
    Verification:
    openjdk version "11.0.31" 2026-04-21
    ...
    ---
    javac 11.0.31

The provision path ran its own verification command and looked only at the
exit code, so a block that names Java 11 sealed a claim of Java 17 — into the
result metadata, into the overlay, and into the model's context.

**Env register (lucene, d2r2, control_events seq 86/88).** `env register
tool=java executable=/usr/bin/java requirement="[21,24]" activate=true`
returned success and persisted `"version": null` for that candidate. A
requirement was stated in the very same call and nothing was ever checked
against it, because only Maven registration probes and compares.

The rule both fences state: a version requirement present — the requested
major for provision, the `requirement` parameter or a persisted observation
for register — makes an absent or contradicting version a refusal, before
anything is persisted or activated.
"""

import json
import shlex

import pytest

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON, EnvOverlayStore
from sag.tools.internal.env_tool import EnvTool
from sag.tools.internal.java_versions import java_major
from sag.tools.internal.system_tool import SystemTool

ARCH = "arm64"
JAVA_HOME = f"/usr/lib/jvm/java-17-openjdk-{ARCH}"
JAVA_BIN = f"{JAVA_HOME}/bin/java"
JAVAC_BIN = f"{JAVA_HOME}/bin/javac"

# The stored output of camel-quarkus seq 127, verbatim below its header.
CONTRADICTING_VERIFICATION = (
    'openjdk version "11.0.31" 2026-04-21\n'
    "OpenJDK Runtime Environment (build 11.0.31+11-post-1ubuntu1-24.04.2-Ubuntu)\n"
    "OpenJDK 64-Bit Server VM (build 11.0.31+11-post-1ubuntu1-24.04.2-Ubuntu, mixed mode)\n"
    "---\n"
    "javac 11.0.31"
)
AGREEING_VERIFICATION = (
    'openjdk version "17.0.19" 2026-04-21\n'
    "OpenJDK Runtime Environment (build 17.0.19+7-Ubuntu-124.04)\n"
    "OpenJDK 64-Bit Server VM (build 17.0.19+7-Ubuntu-124.04, mixed mode)\n"
    "---\n"
    "javac 17.0.19"
)
PREINSTALLED_JAVA_11 = 'openjdk version "11.0.31" 2026-04-21\n'


class FakeProvisionOrchestrator:
    """The container camel-quarkus provisioned into: Java 11 on PATH.

    The provision now asks its question twice — once of the exact binaries it
    is about to activate, once of the domain after the switch has landed — so
    this container answers both with the same block. Whichever gate reads it
    first, the rule under test is the same: a block that names another major
    is a refusal, and the earliest gate is the one that runs before anything
    is persisted or activated.
    """

    def __init__(self, verification):
        self.verification = verification
        self.commands = []
        self.files = {}

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append(command)
        if command == "java -version 2>&1":
            return {"success": True, "output": PREINSTALLED_JAVA_11, "exit_code": 0}
        if command == "dpkg --print-architecture":
            return {"success": True, "output": f"{ARCH}\n", "exit_code": 0}
        if command.startswith("test -f ") and command.endswith("&& echo 'exists'"):
            return {"success": True, "output": "exists", "exit_code": 0}
        if "java -version" in command:
            return {"success": True, "output": self.verification, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}

    def read_file(self, path):
        if path not in self.files:
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def test_provision_does_not_seal_the_version_its_own_verification_disproves():
    orchestrator = FakeProvisionOrchestrator(CONTRADICTING_VERIFICATION)
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_VERIFICATION_MISMATCH"
    # Nothing about Java 17 may be sealed: not the overlay, not the metadata.
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    assert result.metadata.get("java_version") != "17"


def test_the_provision_refusal_names_the_claimed_and_the_verified_version():
    orchestrator = FakeProvisionOrchestrator(CONTRADICTING_VERIFICATION)
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.metadata["claimed_java_version"] == "17"
    assert result.metadata["verified_java_version"] == "11.0.31"
    assert result.metadata["verified_javac_version"] == "11.0.31"
    assert "17" in result.error and "11.0.31" in result.error
    # The block that disproved the claim travels with the refusal.
    assert 'openjdk version "11.0.31"' in result.output


def test_a_verification_block_that_names_no_version_seals_nothing():
    """Absent evidence is not agreement — the same rule the register half states."""
    orchestrator = FakeProvisionOrchestrator("---\n")
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.succeeded is False
    assert result.error_code == "JAVA_VERSION_UNVERIFIED"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_provision_still_seals_the_version_its_verification_confirms():
    orchestrator = FakeProvisionOrchestrator(AGREEING_VERIFICATION)
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("17")

    assert result.succeeded is True
    assert result.metadata["java_version"] == "17"
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert overlay["tools"]["java"]["active"] == JAVA_BIN


def test_a_legacy_1_8_runtime_verifies_as_major_8():
    """`java version "1.8.0_361"` is Java 8, not Java 1 — the gate must not misread it."""
    assert java_major("1.8.0_361") == "8"
    assert java_major("11.0.31") == "11"
    assert java_major("17") == "17"
    assert java_major("") is None


def test_a_legacy_1_8_provision_is_not_refused_by_its_own_evidence():
    orchestrator = FakeProvisionOrchestrator('java version "1.8.0_361"\n---\njavac 1.8.0_361')
    tool = SystemTool(orchestrator)

    result = tool._install_and_configure_java("8")

    assert result.succeeded is True


class FakeJavaOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("realpath -e -- "):
            return {"success": True, "output": shlex.split(command)[-1], "exit_code": 0}
        if command.startswith("test -x "):
            return {"success": True, "output": "EXISTS\n", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def test_env_register_refuses_a_null_version_against_a_stated_requirement():
    """lucene seq 86/88: `requirement="[21,24]"` and `"version": null` both sealed."""
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        requirement="[21,24]",
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_VERSION_UNVERIFIED"
    assert result.raw_data["requirement"] == "[21,24]"
    assert result.raw_data["version"] is None
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    # A refusal that cannot be acted on is a wall: the moves are named.
    named = " ".join(result.suggestions)
    assert "/usr/bin/java -version" in named
    assert "provision" in named


def test_env_register_refuses_a_claimed_version_the_requirement_disproves():
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        version="17",
        requirement="[21,24]",
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert result.raw_data["requirement"] == "[21,24]"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_env_register_refuses_against_a_requirement_the_harness_already_observed():
    """A requirement need not be restated in the call to still be in force."""
    orchestrator = FakeJavaOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_requirement_failure("java", requirement="[21,24]")
    tool = EnvTool(orchestrator, store=store)
    sealed_before = orchestrator.files[DEFAULT_OVERLAY_JSON]

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_VERSION_UNVERIFIED"
    assert result.raw_data["requirement_source"] == "registered_state"
    assert orchestrator.files[DEFAULT_OVERLAY_JSON] == sealed_before


def test_env_register_seals_a_version_that_satisfies_the_requirement():
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/opt/jdk-21/bin/java",
        version="21.0.4",
        requirement="[21,24]",
        activate=True,
    )

    assert result.succeeded is True
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    candidate = overlay["tools"]["java"]["candidates"]["/opt/jdk-21/bin/java"]
    assert candidate["version"] == "21.0.4"


def test_the_same_jdk_spelled_at_two_precisions_satisfies_the_requirement():
    """`21` and `21.0.9` are one JDK: the honest observed version is not a mismatch."""
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/lib/jvm/java-21-openjdk-arm64/bin/java",
        version="21.0.9",
        requirement="21",
        activate=True,
    )

    assert result.succeeded is True


def test_a_dotted_requirement_is_not_met_by_a_different_patch_runtime():
    """`21.0.1` and `21.0.9` are two runtimes, and the requirement named one.

    The major fallback exists for the BARE spelling only. Majoring both sides
    of a dotted requirement accepted any 21.x for `21.0.1` — a silent widening
    of the constraint the caller stated, in the one code path whose whole job
    is to refuse a version its own requirement disproves.
    """
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/lib/jvm/java-21-openjdk-arm64/bin/java",
        version="21.0.9",
        requirement="21.0.1",
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert result.raw_data["requirement"] == "21.0.1"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_a_dotted_requirement_is_met_by_the_runtime_it_names():
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/lib/jvm/java-21-openjdk-arm64/bin/java",
        version="21.0.1",
        requirement="21.0.1",
        activate=True,
    )

    assert result.succeeded is True


def test_a_legacy_bare_major_requirement_keeps_its_major_match():
    """`1.8` names major 8 and nothing narrower: `1.8.0_361` satisfies it."""
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/lib/jvm/java-8-openjdk-arm64/bin/java",
        version="1.8.0_361",
        requirement="1.8",
        activate=True,
    )

    assert result.succeeded is True


def test_env_register_without_any_requirement_is_unchanged():
    """No requirement in force means no version to disprove — the old path stands."""
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        activate=True,
    )

    assert result.succeeded is True
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert overlay["tools"]["java"]["active"] == "/usr/bin/java"


@pytest.mark.parametrize("requirement", ["~21", "preferred:21"])
def test_a_preferred_version_is_not_a_requirement_an_absence_can_fail(requirement):
    orchestrator = FakeJavaOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        requirement=requirement,
        activate=True,
    )

    assert result.succeeded is True
