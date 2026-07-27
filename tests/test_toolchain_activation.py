"""The registered runtime must reach the build, and an ambiguous overlay must resolve.

Both faults are recorded, not imagined.

B1 — polaris, `logs/session_20260727_065557_97847`: Java 21 was provisioned,
registered and activated (`project(action='env', activate=True)`), and the
compile that would have run under it was refused three times as
`RETRY_WITHOUT_DELTA` with the SAME `retry_key` (`5b057768a1d3b320`,
`runner_dispatched: false`).  The retry identity's toolchain component hashes
`/workspace/.setup_agent/toolchains.json`; no session in the 23-project
campaign ever wrote that file, because every real registration lands in
`env_overlay.json` instead.  Registry and overlay were two disconnected
stores, so a registration was invisible to the dispatch that was supposed to
inherit it.

B2 — camel-quarkus, `logs/session_20260727_063915_96714`: the overlay listed
more than one Maven for one tool and no active candidate, and the phase had no
rule with which to choose.  The shapes below are that session's real
`env_overlay.json`.
"""

import json
import shlex

from sag.agent.retry_authority import (
    TOOLCHAIN_REGISTRY_PATH as RETRY_TOOLCHAIN_REGISTRY_PATH,
)
from sag.agent.retry_authority import compute_retry_key, toolchain_state_fingerprint
from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
from sag.tools.internal.build_preflight import (
    JAVA_RUNTIME_CONFLICT,
    REQUIREMENTS_PATH,
    JdkPreflight,
)
from sag.tools.internal.env_tool import EnvTool
from sag.tools.internal.toolchain_manager import (
    OVERLAY_RULE_ACTIVE,
    OVERLAY_RULE_HIGHEST_VERSION,
    OVERLAY_RULE_PROJECT_REQUIREMENT,
    TOOLCHAIN_REGISTRY_PATH,
    ToolchainManager,
    ToolchainSpec,
    ToolVersionRequirement,
)

JAVA_21 = "/usr/lib/jvm/java-21-openjdk-arm64/bin/java"
JAVA_17 = "/usr/lib/jvm/java-17-openjdk-arm64/bin/java"
MAVEN_398 = "/usr/local/bin/mvn"
MAVEN_399 = "/workspace/tools/apache-maven-3.9.9/bin/mvn"


class FakeContainer:
    """The command shapes the registry, the overlay and the pre-flight use."""

    def __init__(
        self,
        *,
        files=None,
        executables=None,
        java_executable=None,
        java_version_banner=None,
        path_executables=None,
    ):
        self.files = dict(files or {})
        self.executables = dict(executables or {})
        self.java_executable = java_executable
        self.java_version_banner = java_version_banner
        self.path_executables = dict(path_executables or {})
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=None):
        self.commands.append(command)

        if "java -version" in command:
            lines = []
            if self.java_executable:
                lines.append(self.java_executable)
            if self.java_version_banner:
                lines.append(self.java_version_banner)
            return {"success": True, "output": "\n".join(lines), "exit_code": 0}

        if command.startswith("realpath -e -- "):
            path = shlex.split(command)[-1]
            if path in self.executables:
                return {"success": True, "output": path, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command.startswith("test -x "):
            path = shlex.split(command)[2]
            exists = path in self.executables
            return {
                "success": True,
                "output": "EXISTS" if exists else "MISSING",
                "exit_code": 0,
            }

        if command.startswith("test -f "):
            path = shlex.split(command)[2]
            return {
                "success": True,
                "output": "EXISTS" if path in self.files else "MISSING",
                "exit_code": 0,
            }

        if command.endswith(" -version"):
            path = shlex.split(command)[0]
            banner = self.executables.get(path)
            if banner:
                return {"success": True, "output": banner, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command.startswith("command -v "):
            name = shlex.split(command)[2]
            path = self.path_executables.get(name)
            if path:
                return {"success": True, "output": path, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command.startswith("find "):
            paths = [
                path
                for path in self.executables
                if "/apache-maven-" in path and path.endswith("/bin/mvn")
            ]
            return {"success": True, "output": "\n".join(paths), "exit_code": 0}

        if command.startswith("mkdir -p"):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("cat > "):
            path = shlex.split(command.split("\n", 1)[0])[2]
            body = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.files[path] = body
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("cat "):
            path = shlex.split(command)[1]
            if path in self.files:
                return {"success": True, "output": self.files[path], "exit_code": 0}
            if "echo '{}'" in command:
                return {"success": True, "output": "{}", "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        return {"success": True, "output": "", "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def _polaris_container():
    """polaris at the moment Java 21 was installed but not yet registered."""
    return FakeContainer(
        executables={
            JAVA_21: 'openjdk version "21.0.9" 2026-01-20',
            JAVA_17: 'openjdk version "17.0.19" 2026-01-20',
        },
        java_executable=JAVA_17,
        java_version_banner='openjdk version "17.0.19" 2026-01-20',
        files={REQUIREMENTS_PATH: json.dumps({"build_root": "/workspace/polaris"})},
    )


def _dispatch_contract():
    """The retry-relevant view of the polaris compile that was refused."""
    return {
        "requested_call": {"tool": "build"},
        "effective_action": "compile",
        "expected_argv": "/workspace/polaris/gradlew --continue --build-cache compileJava",
        "target_sha": "da95233805815b1d6a8576c5b527143193e7d7e5",
        "config_fingerprint": "2151062651 52941 L0",
    }


# ---------------------------------------------------------------------------
# B1 — the registered runtime reaches the dispatch
# ---------------------------------------------------------------------------


def test_env_registration_reaches_the_dispatch_toolchain_state():
    """polaris regression: registry -> overlay -> dispatch carries the runtime."""
    container = _polaris_container()
    contract = _dispatch_contract()
    before = compute_retry_key(
        contract,
        "expectation_unmet",
        toolchain_state=toolchain_state_fingerprint(container.execute_command),
    )

    result = EnvTool(container).execute(
        action="register",
        tool="java",
        executable=JAVA_21,
        requirement="21",
        env={
            "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-arm64",
            "PATH": "/usr/lib/jvm/java-21-openjdk-arm64/bin:/usr/bin:/bin",
        },
        activate=True,
    )

    assert result.succeeded
    overlay = json.loads(container.files[DEFAULT_OVERLAY_JSON])
    assert overlay["tools"]["java"]["active"] == JAVA_21

    registry = json.loads(container.files[TOOLCHAIN_REGISTRY_PATH])
    assert registry["java"]["java"][0]["path"] == JAVA_21

    after = compute_retry_key(
        contract,
        "expectation_unmet",
        toolchain_state=toolchain_state_fingerprint(container.execute_command),
    )
    assert after != before


def test_the_registry_the_dispatch_reads_is_the_registry_registration_writes():
    """One path, stated once on each side, asserted to be the same path.

    The whole B1 fault was two stores that never met. A drift between these
    two constants would silently recreate it.
    """
    assert TOOLCHAIN_REGISTRY_PATH == RETRY_TOOLCHAIN_REGISTRY_PATH


def test_identical_re_registration_leaves_the_dispatch_identity_alone():
    """A repeat of the same registration is not new material progress.

    camel-quarkus registered the same Maven three times.  The registry records
    the runtime, not the number of times a model asked for it, so a re-register
    that states nothing new must not hand the retry law a fresh identity.
    """
    container = _polaris_container()
    tool = EnvTool(container)
    tool.execute(action="register", tool="java", executable=JAVA_21, activate=True)
    first = container.files[TOOLCHAIN_REGISTRY_PATH]

    tool.execute(action="register", tool="java", executable=JAVA_21, activate=True)

    assert container.files[TOOLCHAIN_REGISTRY_PATH] == first


def test_preflight_registration_reaches_the_registry():
    """The pre-flight's own activation is a registration like any other."""
    from sag.tools.internal.build_preflight import _register_overlay

    container = _polaris_container()

    assert _register_overlay(container, "/usr/lib/jvm/java-21-openjdk-arm64", "21") is True

    registry = json.loads(container.files[TOOLCHAIN_REGISTRY_PATH])
    assert registry["java"]["java"][0]["path"] == JAVA_21
    assert registry["java"]["java"][0]["version"] == "21"


# ---------------------------------------------------------------------------
# B1 — a runtime the dispatch does not run under is stated, not accepted
# ---------------------------------------------------------------------------


def test_registered_java_the_dispatch_does_not_run_is_a_named_conflict():
    container = _polaris_container()
    EnvTool(container).execute(action="register", tool="java", executable=JAVA_21, activate=True)

    outcome = JdkPreflight(container).run(None, source="unknown")

    assert JAVA_RUNTIME_CONFLICT in outcome.conflicts
    assert JAVA_RUNTIME_CONFLICT in outcome.narration
    assert JAVA_21 in outcome.narration
    assert JAVA_17 in outcome.narration


def test_registered_java_the_dispatch_does_run_states_no_conflict():
    container = _polaris_container()
    container.java_executable = JAVA_21
    container.java_version_banner = 'openjdk version "21.0.9" 2026-01-20'
    EnvTool(container).execute(action="register", tool="java", executable=JAVA_21, activate=True)

    outcome = JdkPreflight(container).run("21", source="maven-enforcer")

    assert outcome.conflicts == ()
    assert outcome.matched is True


def test_version_mismatch_against_the_registered_runtime_is_named():
    """The registered runtime states a version: compare on it, not only paths."""
    container = _polaris_container()
    container.java_executable = JAVA_21
    EnvTool(container).execute(
        action="register",
        tool="java",
        executable=JAVA_21,
        version="21",
        activate=True,
    )

    outcome = JdkPreflight(container).run(None, source="unknown")

    assert JAVA_RUNTIME_CONFLICT in outcome.conflicts
    assert "Java 21" in outcome.narration


def test_the_same_runtime_spelled_differently_is_not_a_conflict():
    """`21.0.9` and `21` are one JDK; only a real difference is a fact."""
    container = _polaris_container()
    container.java_executable = JAVA_21
    container.java_version_banner = 'openjdk version "21.0.9" 2026-01-20'
    EnvTool(container).execute(
        action="register",
        tool="java",
        executable=JAVA_21,
        version="21.0.9",
        activate=True,
    )

    outcome = JdkPreflight(container).run(None, source="unknown")

    assert outcome.conflicts == ()
    assert outcome.narration == ""


def test_without_a_registered_runtime_the_preflight_probes_exactly_as_before():
    container = FakeContainer(
        java_executable=JAVA_17,
        java_version_banner='openjdk version "17.0.19" 2026-01-20',
    )

    outcome = JdkPreflight(container).run(None, source="unknown")

    assert outcome.matched is True
    assert outcome.conflicts == ()
    assert outcome.narration == ""
    assert not [command for command in container.commands if "java -version" in command]


def test_matched_requirement_without_a_registered_runtime_probes_once():
    container = FakeContainer(
        java_executable=JAVA_17,
        java_version_banner='openjdk version "17.0.19" 2026-01-20',
    )

    outcome = JdkPreflight(container).run("17", source="maven-enforcer")

    assert outcome.matched is True
    assert outcome.narration == ""
    assert len([command for command in container.commands if "java -version" in command]) == 1


# ---------------------------------------------------------------------------
# B2 — an ambiguous overlay resolves, deterministically and recorded
# ---------------------------------------------------------------------------


def _camel_quarkus_container(*, requirements=None):
    """camel-quarkus' recorded overlay shape, before either Maven was blocked."""
    overlay = {
        "version": 1,
        "tools": {
            "maven": {
                "blocked": [],
                "candidates": {
                    MAVEN_398: {
                        "version": "3.8.7",
                        "source": "system_install",
                        "env": {},
                        "path_prepend": ["/usr/local/bin"],
                    },
                    MAVEN_399: {
                        "version": "3.9.9",
                        "source": "agent_registered",
                        "env": {},
                        "path_prepend": ["/workspace/tools/apache-maven-3.9.9/bin"],
                    },
                },
            }
        },
    }
    if requirements:
        overlay["tools"]["maven"]["requirements"] = requirements
    return FakeContainer(
        executables={
            MAVEN_398: "Apache Maven 3.8.7",
            MAVEN_399: "Apache Maven 3.9.9",
        },
        files={DEFAULT_OVERLAY_JSON: json.dumps(overlay)},
    )


def test_two_registered_mavens_resolve_to_the_version_the_project_requires():
    container = _camel_quarkus_container(
        requirements=[
            {
                "raw": "[3.9.0,)",
                "source": "build_error",
                "working_directory": "/workspace/camel-quarkus",
            }
        ]
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_399
    assert resolved.selection_rule == OVERLAY_RULE_PROJECT_REQUIREMENT


def test_two_registered_mavens_without_a_requirement_resolve_to_the_highest():
    container = _camel_quarkus_container()

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_399
    assert resolved.selection_rule == OVERLAY_RULE_HIGHEST_VERSION


def test_the_rule_that_resolved_the_overlay_is_recorded():
    container = _camel_quarkus_container(
        requirements=[
            {
                "raw": "[3.9.0,)",
                "source": "build_error",
                "working_directory": "/workspace/camel-quarkus",
            }
        ]
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert OVERLAY_RULE_PROJECT_REQUIREMENT in resolved.reason
    assert MAVEN_399 in resolved.reason
    assert "2 registered candidates" in resolved.reason


def test_a_requirement_the_highest_version_fails_still_resolves():
    """Rule order, not version order: the project's requirement decides first."""
    container = _camel_quarkus_container(
        requirements=[
            {
                "raw": "[3.8,3.9)",
                "source": "build_error",
                "working_directory": "/workspace/camel-quarkus",
            }
        ]
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_398
    assert resolved.selection_rule == OVERLAY_RULE_PROJECT_REQUIREMENT


def test_single_active_overlay_entry_resolves_exactly_as_today():
    overlay = {
        "version": 1,
        "tools": {
            "maven": {
                "active": MAVEN_399,
                "blocked": [],
                "candidates": {
                    MAVEN_399: {
                        "version": "3.9.9",
                        "source": "agent_registered",
                        "env": {},
                        "path_prepend": ["/workspace/tools/apache-maven-3.9.9/bin"],
                    }
                },
            }
        },
    }
    container = FakeContainer(
        executables={MAVEN_399: "Apache Maven 3.9.9"},
        files={DEFAULT_OVERLAY_JSON: json.dumps(overlay)},
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement(
                raw="[3.9.0,)", source="tool_parameter", kind="range"
            ),
        ),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_399
    assert resolved.candidate.source == "env_overlay"
    assert resolved.reason == (
        f"selected {MAVEN_399} from env_overlay because version 3.9.9 satisfies [3.9.0,)"
    )
    assert resolved.selection_rule == OVERLAY_RULE_ACTIVE


def test_a_single_unactivated_overlay_entry_behaves_exactly_as_today():
    """One executable is not a question, so it does not get a new answer.

    Ambiguity resolution applies to an overlay that LISTS more than one
    executable for a tool. A single registered-but-not-activated candidate
    keeps the pre-existing contract: the overlay contributes nothing and
    ordinary discovery decides, at the source it decided at before.
    """
    overlay = {
        "version": 1,
        "tools": {
            "maven": {
                "blocked": [],
                "candidates": {
                    MAVEN_399: {
                        "version": "3.9.9",
                        "source": "agent_registered",
                        "env": {},
                        "path_prepend": ["/workspace/tools/apache-maven-3.9.9/bin"],
                    }
                },
            }
        },
    }
    container = FakeContainer(
        executables={MAVEN_399: "Apache Maven 3.9.9"},
        files={DEFAULT_OVERLAY_JSON: json.dumps(overlay)},
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_399
    assert resolved.candidate.source == "standalone"
    assert resolved.selection_rule is None


def test_an_active_overlay_entry_outranks_a_higher_registered_version():
    """Activation is a recorded decision; resolution does not overrule it.

    The dispatch environment exports the ACTIVE candidate's PATH, so choosing a
    different executable here would make the resolver and the shell disagree.
    """
    overlay = {
        "version": 1,
        "tools": {
            "maven": {
                "active": MAVEN_398,
                "blocked": [],
                "candidates": {
                    MAVEN_398: {
                        "version": "3.8.7",
                        "source": "system_install",
                        "env": {},
                        "path_prepend": ["/usr/local/bin"],
                    },
                    MAVEN_399: {
                        "version": "3.9.9",
                        "source": "agent_registered",
                        "env": {},
                        "path_prepend": ["/workspace/tools/apache-maven-3.9.9/bin"],
                    },
                },
            }
        },
    }
    container = FakeContainer(
        executables={MAVEN_398: "Apache Maven 3.8.7", MAVEN_399: "Apache Maven 3.9.9"},
        files={DEFAULT_OVERLAY_JSON: json.dumps(overlay)},
    )

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_398
    assert resolved.selection_rule == OVERLAY_RULE_ACTIVE


def test_a_blocked_registered_candidate_is_never_the_resolution():
    container = _camel_quarkus_container()
    overlay = json.loads(container.files[DEFAULT_OVERLAY_JSON])
    overlay["tools"]["maven"]["blocked"] = [
        {
            "executable": MAVEN_399,
            "version": "3.9.9",
            "requirement": None,
            "reason": "Maven Enforcer rejected this runtime",
            "source": "build_error",
        }
    ]
    container.files[DEFAULT_OVERLAY_JSON] = json.dumps(overlay)

    resolved = ToolchainManager(container).resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/camel-quarkus",
    )

    assert resolved is not None
    assert resolved.candidate.path == MAVEN_398
