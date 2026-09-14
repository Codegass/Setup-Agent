import json

import pytest

from sag.agent.tool_orchestration import format_tool_result
from sag.runtime.env_overlay import EnvOverlayStore
from sag.tools.internal.build_preflight import (
    JdkPreflight,
    active_java_runtime,
    classify_java_constraint,
)
from sag.tools.internal.env_tool import EnvTool
from sag.tools.project_tool import ProjectTool


class JavaHost:
    """Executable facts are separate from both the overlay and caller claims."""

    def __init__(
        self, version="21.0.9", *, stale=False, broken=False, distribution="openjdk", native=False
    ):
        self.version, self.stale, self.broken = version, stale, broken
        self.files, self.commands = {}, []
        self.distribution, self.native = distribution, native

    def read_file(self, path):
        return self.files.get(path)

    def write_file(self, path, content):
        self.files[path] = content
        return {"exit_code": 0, "success": True, "output": ""}

    def execute_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("readlink -f"):
            output = "/opt/jdk/bin/java"
        elif cmd.startswith("test -x"):
            output = "EXISTS"
        elif "native-image" in cmd:
            return {
                "exit_code": 0 if self.native else 127,
                "success": self.native,
                "output": (
                    f"/opt/jdk/bin/native-image\nnative-image {self.version}"
                    if self.native
                    else "native-image: not found"
                ),
            }
        elif "javac -version" in cmd:
            output = f"/opt/jdk/bin/javac\njavac {self.version}"
        elif cmd.startswith("command -v java"):
            output = (
                '/usr/bin/java\nopenjdk version "8.0.1"'
                if self.stale
                else f'/opt/jdk/bin/java\nopenjdk version "{self.version}"'
            )
        elif cmd == "/opt/jdk/bin/java -version":
            output = f'openjdk version "{self.version}"'
        else:
            output = ""
        if "java -version" in cmd:
            if self.distribution == "graalvm":
                output += "\nGraalVM Runtime Environment"
            elif self.distribution == "unknown":
                output = output.replace("openjdk version", "java version")
        return {"exit_code": 1 if self.broken else 0, "success": not self.broken, "output": output}


def test_java_registration_measures_version_and_aligns_java_home():
    host = JavaHost()
    result = ProjectTool(env_tool=EnvTool(host)).execute(
        action="env",
        tool="java",
        executable="/usr/bin/java",
        requirement="[21,)",
        env={"JAVA_HOME": "/opt/old-jdk"},
    )
    assert result.succeeded, result.error
    assert result.raw_data["measured_version"] == "21.0.9"
    active = EnvOverlayStore(host).active_candidate("java")
    assert active["version"] == "21.0.9"
    assert active["env"]["JAVA_HOME"] == "/opt/jdk"


def test_java_caller_version_cannot_override_executable_fact():
    host = JavaHost(version="17.0.1")
    result = EnvTool(host).execute(
        action="register",
        tool="java",
        executable="/opt/jdk/bin/java",
        version="21",
        requirement="[21,)",
        activate=True,
    )
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert not EnvOverlayStore(host).active_candidate("java")


def test_java_activation_failure_is_visible_and_not_certified():
    host = JavaHost(stale=True)
    result = EnvTool(host).execute(
        action="register", tool="java", executable="/opt/jdk/bin/java", activate=True
    )
    assert result.error_code == "ENV_ACTIVATION_NOT_CONFIRMED"
    assert "dispatch_runtime" in format_tool_result("project", result)
    assert not result.metadata["activation_confirmed"]


def test_java_reactivation_reprobes_replaced_executable():
    host = JavaHost()
    tool = EnvTool(host)
    assert tool.execute(action="register", tool="java", executable="/opt/jdk/bin/java").succeeded
    host.version = "17.0.1"
    result = tool.execute(
        action="activate", tool="java", executable="/opt/jdk/bin/java", requirement="[21,)"
    )
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert not EnvOverlayStore(host).active_candidate("java")


def test_nonzero_java_probe_cannot_supply_runtime_evidence():
    assert active_java_runtime(JavaHost(broken=True)) == {}


@pytest.mark.parametrize(
    "message,bound",
    [
        ("Dependency requires at least JVM runtime version 17.", "[17,)"),
        ("class file version 61.0", "[17,)"),
        ("release version 17 not supported", "[17,)"),
        ("Build requires exactly Java 17", "[17,18)"),
        ("RequireJavaVersion failed: allowed range [11,17)", "[11,17)"),
        ("RequireJavaVersion failed: allowed range [11,17),[21,)", None),
        ("Groovy:A transform used a generics containing ClassNode", None),
    ],
)
def test_auto_retry_uses_stated_bounds_not_guessed_exact_major(message, bound):
    assert (classify_java_constraint(message) or {}).get("constraint") == bound


def test_floor_keeps_newer_jdk_and_real_upper_bound_still_applies(monkeypatch):
    host = JavaHost()
    installs = []
    monkeypatch.setattr(JdkPreflight, "_provision", lambda self, major: installs.append(major))
    assert JdkPreflight(host).run("17", runtime_constraints=["[17,)"]).matched
    assert installs == []
    assert JdkPreflight(host).run("17", runtime_constraints=["[17,18)"]).mismatch
    assert installs == ["17"]


def test_command_scoped_floors_intersect_and_do_not_leak_to_native_step():
    host = JavaHost()
    store = EnvOverlayStore(host)
    scope = dict(target_sha="a" * 40, domain_root="/workspace/project")
    for major in ("17", "21"):
        store.record_runtime_requirement(
            "java",
            **scope,
            required_major=major,
            source_ref="inv-" + major,
            command_key="b" * 64,
            constraint=f"[{major},)",
        )
    resolution = store.resolve_runtime_requirement(
        "java", **scope, static_major=None, static_source=None, command_key="b" * 64
    )
    assert resolution["runtime_constraints"] == ["[17,)", "[21,)"]
    assert (
        JdkPreflight(host)
        .run(resolution["required_major"], runtime_constraints=resolution["runtime_constraints"])
        .matched
    )
    assert (
        store.resolve_runtime_requirement(
            "java", **scope, static_major=None, static_source=None, command_key="c" * 64
        )
        == {}
    )
    # Persistence round trip retains bounds rather than reducing them to majors.
    assert (
        json.loads(host.files["/workspace/.setup_agent/env_overlay.json"])["tools"]["java"][
            "runtime_requirements"
        ][0]["constraint"]
        == "[17,)"
    )


def test_current_public_parameters_visible_but_legacy_suggestions_stay_out():
    tool = ProjectTool(env_tool=EnvTool(JavaHost()))
    result = tool.execute(
        action="env", tool="java", executable="/opt/jdk/bin/java", JAVA_HOME="/old"
    )
    result.suggestions = ["stale internal env(action='set') advice"]
    text = format_tool_result("project", result)
    assert "accepted_parameters" in text and "env" in text
    assert "stale internal" not in text


@pytest.mark.parametrize("distribution", ["openjdk", "graalvm"])
@pytest.mark.parametrize("native", [False, True])
def test_jvm_capability_verification_is_independent_of_distribution(distribution, native):
    host = JavaHost(distribution=distribution, native=native)
    tool = EnvTool(host)
    plain = tool.execute(
        action="register",
        tool="java",
        executable="/opt/jdk/bin/java",
        java_distribution=distribution,
        activate=True,
    )
    assert plain.succeeded, plain.error
    assert not any("native-image" in command for command in host.commands)
    required = tool.execute(
        action="register",
        tool="java",
        executable="/opt/jdk/bin/java",
        java_distribution=distribution,
        java_capabilities=["native-image"],
        activate=True,
    )
    assert required.succeeded is native
    if not native:
        assert required.error_code == "ENV_CAPABILITY_UNAVAILABLE"


def test_distribution_is_measured_not_inferred_from_the_install_path():
    from sag.tools.internal.java_versions import java_distribution

    assert java_distribution('/opt/graalvm/bin/java\nopenjdk version "21.0.9"') == "openjdk"
    host = JavaHost()
    result = EnvTool(host).execute(
        action="register",
        tool="java",
        executable="/opt/graalvm/bin/java",
        java_distribution="graalvm",
        activate=True,
    )
    assert result.error_code == "ENV_RUNTIME_DISTRIBUTION_MISMATCH"
    assert not host.files


class EmbeddedJreHost(JavaHost):
    """RocketMQ's measured layout; the launcher symlink and compiler are distinct."""

    def __init__(self, *, compiler=True, enclosing_launcher=True):
        super().__init__(version="1.8.0_502")
        self.compiler = compiler
        self.enclosing_launcher = enclosing_launcher

    def execute_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("test -x /opt/jdk/bin/java && readlink"):
            return {
                "exit_code": 0 if self.enclosing_launcher else 1,
                "output": "/opt/jdk/jre/bin/java" if self.enclosing_launcher else "",
            }
        if "jre/bin/javac" in cmd or ("javac" in cmd and not self.compiler):
            return {"exit_code": 127, "success": False, "output": "javac: not found"}
        if cmd.startswith("command -v javac"):
            environment = EnvOverlayStore(self).authorized_environment({})
            assert "/opt/jdk/bin" in environment["PATH"].split(":")
        result = super().execute_command(cmd.replace("/jre/bin/java", "/bin/java"), **kwargs)
        if cmd.startswith("readlink") or cmd.startswith("command -v java "):
            result["output"] = result["output"].replace(
                "/opt/jdk/bin/java", "/opt/jdk/jre/bin/java"
            )
        return result


@pytest.mark.parametrize("compiler", [True, False])
def test_embedded_jre_uses_jdk_home_and_still_requires_real_compiler(compiler):
    host = EmbeddedJreHost(compiler=compiler)
    tool = EnvTool(host)
    result = tool.execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        java_capabilities=["javac"],
        requirement="8",
        activate=True,
    )
    assert result.succeeded is compiler, result.error
    if compiler:
        environment = EnvOverlayStore(host).authorized_environment({})
        assert environment["JAVA_HOME"] == "/opt/jdk"
        assert "/opt/jdk/bin" in environment["PATH"].split(":")
        again = tool.execute(action="activate", tool="java", executable="/opt/jdk/jre/bin/java")
        assert again.succeeded, again.error
    else:
        assert result.error_code == "ENV_CAPABILITY_UNAVAILABLE"
        assert not host.files
    assert not any("jre/bin/javac" in c for c in host.commands)


def test_standalone_jre_does_not_borrow_tools_from_unrelated_parent():
    host = EmbeddedJreHost(enclosing_launcher=False)
    result = EnvTool(host).execute(
        action="register",
        tool="java",
        executable="/usr/bin/java",
        java_capabilities=["javac"],
        activate=True,
    )
    assert result.error_code == "ENV_CAPABILITY_UNAVAILABLE"
    assert not host.files


def test_new_unknown_banner_cannot_keep_a_previous_distribution_claim():
    host = JavaHost(distribution="graalvm")
    tool = EnvTool(host)
    assert tool.execute(
        action="register", tool="java", executable="/opt/jdk/bin/java", activate=True
    ).succeeded
    host.distribution = "unknown"
    result = tool.execute(
        action="register", tool="java", executable="/opt/jdk/bin/java", activate=True
    )
    assert result.succeeded, result.error
    assert EnvOverlayStore(host).active_candidate("java")["distribution"] == "unknown"


def test_automatic_version_repair_keeps_the_selected_jvm_family_and_capabilities(monkeypatch):
    from sag.tools.internal.system_tool import SystemTool

    host = JavaHost(distribution="graalvm", native=True)
    env = EnvTool(host)
    assert env.execute(
        action="register",
        tool="java",
        executable="/opt/jdk/bin/java",
        java_distribution="graalvm",
        java_capabilities=["native-image"],
        activate=True,
    ).succeeded
    calls = []

    def acquire(self, **kwargs):
        calls.append(kwargs)
        host.version = kwargs["java_version"] + ".0.1"
        return env.execute(
            action="register",
            tool="java",
            executable="/opt/jdk/bin/java",
            java_distribution=kwargs["java_distribution"],
            java_capabilities=kwargs["java_capabilities"],
            activate=True,
        )

    monkeypatch.setattr(SystemTool, "execute", acquire)
    outcome = JdkPreflight(host).run("25", runtime_constraints=["[25,)"])
    assert outcome.provisioned, outcome.narration
    assert calls == [
        {
            "action": "install_java",
            "java_version": "25",
            "java_distribution": "graalvm",
            "java_capabilities": ["native-image"],
        }
    ]
    assert EnvOverlayStore(host).active_candidate("java")["distribution"] == "graalvm"


def test_unavailable_selected_distribution_never_falls_back_to_openjdk():
    host = JavaHost(version="17.0.1", distribution="unknown")
    assert (
        EnvTool(host)
        .execute(action="register", tool="java", executable="/opt/jdk/bin/java", activate=True)
        .succeeded
    )
    outcome = JdkPreflight(host).run("21", runtime_constraints=["[21,)"])
    assert not outcome.provisioned
    assert "JAVA_DISTRIBUTION_UNSUPPORTED" in outcome.narration
    assert not any("apt-get" in command for command in host.commands)


@pytest.mark.parametrize(
    "field,value",
    [
        ("distribution", ["graalvm"]),
        ("capabilities", "native-image"),
        ("capabilities", ["../java"]),
        ("capabilities", ["javac"] * 33),
    ],
)
def test_jvm_selection_persistence_rejects_invalid_identity_fields(field, value):
    host = JavaHost()
    with pytest.raises(ValueError):
        EnvOverlayStore(host).register("java", "/opt/jdk/bin/java", **{field: value})
    assert not host.files


def test_version_only_provision_keeps_family_until_explicitly_overridden(monkeypatch):
    from sag.tools.base import ToolResult
    from sag.tools.internal.system_tool import SystemTool

    host = JavaHost(distribution="graalvm")
    assert (
        EnvTool(host)
        .execute(action="register", tool="java", executable="/opt/jdk/bin/java", activate=True)
        .succeeded
    )
    calls = []

    def provider(family):
        def acquire(self, version, *, capabilities):
            calls.append((family, version, capabilities))
            return ToolResult.completed_success(output="acquisition delegated")

        return acquire

    monkeypatch.setattr(SystemTool, "_install_graalvm", provider("graalvm"))
    monkeypatch.setattr(SystemTool, "_install_and_configure_java", provider("openjdk"))
    system = SystemTool(host)
    assert system.execute(action="install_java", java_version="25").succeeded
    assert system.execute(
        action="install_java", java_version="25", java_distribution="openjdk"
    ).succeeded
    assert calls == [("graalvm", "25", []), ("openjdk", "25", [])]


def test_maven_enforcer_failure_delivers_a_current_callable_provision_route(monkeypatch):
    from sag.tools.base import ToolResult
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.internal.maven_tool import MavenTool
    from sag.tools.internal.system_tool import SystemTool

    host = JavaHost()
    maven = MavenTool(host)
    output = (
        "Rule 0: org.apache.maven.enforcer.rules.version.RequireMavenVersion failed\n"
        "Detected Maven Version: 3.8.7 is not in the allowed range [3.9,).\nBUILD FAILURE"
    )
    result = maven._handle_maven_error(
        output,
        1,
        "mvn clean verify",
        {"maven_version_requirement": {"raw": "[3.9,)", "source": "build_error", "kind": "range"}},
        maven_runtime={"executable": "/usr/local/bin/mvn", "version": "3.8.7"},
        runner_dispatched=True,
        final_runner_dispatched=True,
    )
    # The public build envelope previously discarded all backend facts.
    result.facts["system"] = "stale_backend_label"
    result = BuildTool(host)._envelope(result, "maven", "verify", "verify", "/workspace/p")
    assert result.facts["system"] == "maven"
    rendered = format_tool_result("build", result)
    assert "available_setup_call" in rendered and "maven_version" in rendered
    assert "3.8.7" in rendered and "[3.9,)" in rendered
    assert result.facts["runner_dispatched"] is True
    call = result.facts["available_setup_call"]
    observed = []

    def install(self, floor, *, requirement=None):
        assert requirement is None
        observed.append(floor)
        return ToolResult.completed_success(output="installer delegated")

    monkeypatch.setattr(SystemTool, "_install_and_configure_maven", install)
    actual = ProjectTool(system_tool=SystemTool(host)).execute(**call["params"])
    assert actual.succeeded and observed == ["3.9"]


@pytest.mark.parametrize("requirement", ["[3.8,3.8.8)", "[4,)", "[3.9.20,)"])
def test_maven_hint_never_offers_a_distribution_outside_the_full_requirement(requirement):
    from sag.tools.internal.maven_tool import MavenTool
    from sag.tools.internal.toolchain_manager import ToolVersionRequirement

    result = MavenTool(JavaHost())._maven_version_not_resolved_result(
        ToolVersionRequirement.from_raw(requirement, source="build_error"), "/workspace/p"
    )
    assert "available_setup_call" not in result.facts
    assert result.facts["maven_requirement"]["raw"] == requirement
