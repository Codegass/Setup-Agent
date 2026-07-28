# tests/test_enforcer_attribution.py
"""Plan 7 round two — only a Maven-version failure the BUILD states may block
a Maven runtime, and a runtime that satisfies the requirement is never blocked.

Live evidence (p7-camel-quarkus, `logs/session_20260727_182220_41788`): the
env overlay held

    BLOCKED 3.8.7   /usr/share/maven/bin/mvn            req: [3.9.0,)
    BLOCKED 3.9.15  /workspace/apache-maven-3.9.15/bin/mvn  req: [3.9.0,)
    active: None

3.9.15 satisfies `[3.9.0,)`. The requirement had arrived as the caller's own
tool parameter — `build(action='test', maven_version_requirement='[3.9.0,)')` —
and the guard only asked whether SOME requirement existed. The project's real
problem was Java 11 against a build needing 17+, so each retry installed a
newer Maven and condemned that one too, until no candidate was left and the
test phase became unreachable (64 `MAVEN_VERSION_NOT_RESOLVED` refusals).
"""

from sag.tools.internal.maven_tool import MavenTool


class RecordingOverlayOrchestrator:
    """Container double with a file store, so an overlay write round-trips.

    The overlay store writes base64 and reads the file back byte-for-byte; a
    double that answers every read with "" makes the write look like a failure
    and would hide whether the guards or the transport refused the block.
    """

    def __init__(self):
        self.commands = []
        self.files = {}

    def write_file(self, path, content):
        self.files[path] = content
        self.commands.append(f"write_file {path}")
        return {"success": True, "exit_code": 0}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("command -v"):
            return {"success": True, "output": "/usr/share/maven/bin/mvn", "exit_code": 0}
        for path, body in self.files.items():
            if path in command and ("cat" in command or "base64" in command):
                return {"success": True, "output": body, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}

    def overlay_writes(self):
        return [
            command
            for command in self.commands
            if "env_overlay" in command and command.startswith("write_file")
        ]


def maven_tool():
    return MavenTool(RecordingOverlayOrchestrator())


ENFORCER_OUTPUT = (
    "[INFO] --- maven-enforcer-plugin:3.4.1:enforce (enforce-maven) @ demo ---\n"
    "[WARNING] Rule 0: org.apache.maven.enforcer.rules.version.RequireMavenVersion failed "
    "with message:\n"
    "Detected Maven Version: 3.8.7 is not in the allowed range [3.9.0,).\n"
)

JAVA_ENFORCER_OUTPUT = (
    "[INFO] --- maven-enforcer-plugin:3.4.1:enforce (enforce-java) @ demo ---\n"
    "[WARNING] Rule 0: RequireJavaVersion failed with message:\n"
    "Detected JDK Version: 11.0.22 is not in the allowed range [17,).\n"
)


def test_a_caller_supplied_requirement_never_blocks_a_runtime():
    """The camel-quarkus fault: the requirement was the caller's parameter."""
    tool = maven_tool()

    blocked = tool._persist_maven_requirement_failure(
        requirement={"raw": "[3.9.0,)", "source": "tool_parameter", "kind": "range"},
        maven_runtime={"executable": "/workspace/apache-maven-3.9.15/bin/mvn", "version": "3.9.15"},
        output=JAVA_ENFORCER_OUTPUT,
        working_directory="/workspace/demo",
    )

    assert blocked is False
    assert tool.orchestrator.overlay_writes() == []


def test_a_satisfying_runtime_is_never_blocked_even_with_enforcer_evidence():
    """3.9.15 against [3.9.0,) — the exact pair the live overlay condemned."""
    tool = maven_tool()

    blocked = tool._persist_maven_requirement_failure(
        requirement={"raw": "[3.9.0,)", "source": "build_error", "kind": "range"},
        maven_runtime={"executable": "/workspace/apache-maven-3.9.15/bin/mvn", "version": "3.9.15"},
        output=ENFORCER_OUTPUT.replace("3.8.7", "3.9.15"),
        working_directory="/workspace/demo",
    )

    assert blocked is False
    assert tool.orchestrator.overlay_writes() == []


def test_a_genuine_maven_version_failure_still_blocks():
    """The behaviour the guards must not cost us: 3.8.7 really is too old."""
    tool = maven_tool()

    blocked = tool._persist_maven_requirement_failure(
        requirement={"raw": "[3.9.0,)", "source": "build_error", "kind": "range"},
        maven_runtime={"executable": "mvn", "version": "3.8.7"},
        output=ENFORCER_OUTPUT,
        working_directory="/workspace/demo",
    )

    assert blocked is True
    assert tool.orchestrator.overlay_writes()


def test_an_undecidable_version_does_not_block():
    """No detected version is not evidence of rejection."""
    tool = maven_tool()

    blocked = tool._persist_maven_requirement_failure(
        requirement={"raw": "[3.9.0,)", "source": "build_error", "kind": "range"},
        maven_runtime={"executable": "mvn"},
        output="[ERROR] something else went wrong\n",
        working_directory="/workspace/demo",
    )

    assert blocked is True  # unknown version cannot be shown to satisfy the range
    assert tool.orchestrator.overlay_writes()


def test_an_unparseable_range_does_not_satisfy_and_still_blocks():
    tool = maven_tool()

    assert tool._version_satisfies_requirement("3.9.15", "not-a-range", "range") is False


def test_the_satisfaction_check_is_the_toolchain_managers_own():
    """One comparator decides both usability and condemnation."""
    tool = maven_tool()

    assert tool._version_satisfies_requirement("3.9.15", "[3.9.0,)", "range") is True
    assert tool._version_satisfies_requirement("3.8.7", "[3.9.0,)", "range") is False
    assert tool._version_satisfies_requirement(None, "[3.9.0,)", "range") is False
