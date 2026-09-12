# tests/test_build_preflight.py
"""JDK pre-flight + build-requirements manifest (spec §1).

The manifest is the phase-1 -> build-tool handoff: tools only hold an
orchestrator, so requirements persist in the container next to the env
overlay (/workspace/.setup_agent/).
"""

import json
import re

import pytest
from test_container_io import FakeContainer

from sag.agent.evidence_records import frame_named_json_record_stream
from sag.tools.internal.build_preflight import (
    BUILD_REQUIREMENTS_DOMAIN_CAP,
    BUILD_REQUIREMENTS_SCHEMA_VERSION,
    REQUIREMENTS_PATH,
    read_build_requirements,
    read_live_build_requirements,
    survey_facts_fingerprint,
    validate_build_requirements_v1,
    write_build_requirements,
)
from sag.tools.internal.project_analyzer import SURVEY_FACTS_VERSION


class FakeOrch:
    """In-memory container FS supporting lossless reads and atomic writes."""

    def __init__(self, *, fail_on=None):
        self._container = FakeContainer(fail_on=fail_on)
        self.files = self._container.files
        self.commands = []

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if cmd.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in cmd:
            records = (
                [(REQUIREMENTS_PATH.rsplit("/", 1)[-1], self.files[REQUIREMENTS_PATH])]
                if REQUIREMENTS_PATH in self.files
                else []
            )
            return {"exit_code": 0, "output": frame_named_json_record_stream(records)}
        return self._execute_command(cmd, workdir)

    def _execute_command(self, cmd, workdir=None):
        return self._container.execute_command(cmd)


def current_manifest(**overrides):
    manifest = {
        "schema_version": BUILD_REQUIREMENTS_SCHEMA_VERSION,
        "survey": {
            "project_path": "/workspace/p",
            "analyzer_version": SURVEY_FACTS_VERSION,
            "config_fingerprint": None,
            "target_sha": None,
            "document_map_fingerprint": None,
            # survey_fingerprint is stamped below, AFTER overrides land, so a
            # mutated fixture still carries the pin its body earns and each
            # invalid-manifest test trips its own contract edge rather than
            # the fingerprint recompute.
        },
        "java_version": "17",
        "java_version_source": "maven-compiler",
        "java_version_enforced": False,
        "root_shape": "single_module",
        "build_root": "/workspace/p",
        "fail_at_end": False,
        "test_root": "/workspace/p",
        "test_system": "maven",
        "test_fail_at_end": False,
        "build_islands": [],
        "test_islands": [],
    }
    manifest.update(overrides)
    survey = manifest.get("survey")
    if isinstance(survey, dict) and "survey_fingerprint" not in survey:
        manifest["survey"] = {
            **survey,
            "survey_fingerprint": survey_facts_fingerprint(manifest),
        }
    return manifest


def test_write_then_read_round_trips():
    orch = FakeOrch()
    data = current_manifest(
        root_shape="healthy_reactor",
        fail_at_end=True,
        test_fail_at_end=True,
    )
    assert write_build_requirements(orch, data) is True
    assert read_build_requirements(orch) == data


def test_live_read_requires_the_exact_current_host_revision():
    orch = FakeOrch()
    data = current_manifest()
    assert write_build_requirements(orch, data) is True

    current = read_live_build_requirements(orch)
    # The tampered body is schema-valid (restamped), so the PUBLICATION check
    # is what refuses it — not a fingerprint recompute masking this test.
    orch.files[REQUIREMENTS_PATH] = json.dumps(current_manifest(java_version="11"), sort_keys=True)
    tampered = read_live_build_requirements(orch)
    del orch.files[REQUIREMENTS_PATH]
    deleted = read_live_build_requirements(orch)

    assert current.complete is True
    assert current.conflict is None
    assert current.payload == data
    assert tampered.complete is False
    assert tampered.conflict == "publication_set_mismatch"
    assert deleted.complete is False
    assert deleted.conflict == "publication_set_mismatch"


def test_unpublished_container_manifest_has_no_live_authority():
    orch = FakeOrch()
    orch.files[REQUIREMENTS_PATH] = json.dumps(current_manifest(), sort_keys=True)

    read = read_live_build_requirements(orch)

    assert read.complete is False
    assert read.conflict == "publication_set_mismatch"


def test_write_large_manifest_streams_with_bounded_commands_and_no_temp_leak():
    orch = FakeOrch()
    dependencies = [f"dep-{index}-" + "x" * 220 for index in range(400)]
    data = current_manifest(
        python_version="3.11",
        python_constraint=">=3.11",
        python_constraint_source="pyproject.toml",
        python_installer="pip",
        python_install_commands=["{venv}/bin/python -m pip install -e ."],
        python_install_note=None,
        python_install_source="pyproject.toml",
        python_packages=["project"],
        python_distribution_name="project",
        python_build_backend="setuptools.build_meta",
        python_declared_dependencies=dependencies,
        python_package_paths=[],
        python_local_providers=[],
        python_smoke_candidates=[],
        python_venv="/workspace/p/.venv",
        python_root="/workspace/p",
        has_c_extensions=False,
        has_native_build=False,
        native_build_mode=None,
        native_artifact_roots=[],
        test_hints={"pytest_args": None, "test_deps": []},
    )

    assert write_build_requirements(orch, data) is True

    assert read_build_requirements(orch) == data
    assert any(command.startswith("printf '%s'") for command in orch.commands)
    assert max(map(len, orch.commands)) <= 60_200
    assert not any(path.endswith(".tmp") for path in orch.files)


def test_failed_publish_preserves_existing_manifest():
    orch = FakeOrch(fail_on="mv -f --")
    original = '{"java_version":"11"}'
    orch.files[REQUIREMENTS_PATH] = original

    assert write_build_requirements(orch, current_manifest()) is False

    assert orch.files[REQUIREMENTS_PATH] == original
    assert not any(path.endswith(".tmp") for path in orch.files)


def test_current_schema_is_closed_strict_and_bounded():
    valid = current_manifest()
    assert validate_build_requirements_v1(valid) == valid

    # Each mutation goes THROUGH the helper so it carries a consistent stamp
    # and trips its own named edge, not the fingerprint recompute.
    invalid = [
        current_manifest(schema_version=True),
        current_manifest(schema_version=BUILD_REQUIREMENTS_SCHEMA_VERSION + 1),
        current_manifest(future_authority="yes"),
        current_manifest(fail_at_end=1),
        current_manifest(build_root="/workspace/p/../escape"),
        current_manifest(build_islands=[{"root": "/workspace/p/m", "system": "maven"}] * 2),
    ]
    for payload in invalid:
        try:
            validate_build_requirements_v1(payload)
        except ValueError:
            pass
        else:  # pragma: no cover - each mutation names a distinct contract edge
            raise AssertionError(f"accepted invalid manifest: {payload}")


def test_current_schema_requires_exact_survey_pins_and_containment():
    valid = current_manifest()
    mutations = [
        {**valid, "survey": {**valid["survey"], "analyzer_version": True}},
        {**valid, "survey": {**valid["survey"], "analyzer_version": 11}},
        {**valid, "survey": {**valid["survey"], "target_sha": "not-a-sha"}},
        {
            **valid,
            "survey": {**valid["survey"], "document_map_fingerprint": "short"},
        },
        # Restamped so the containment check, not the fingerprint recompute,
        # is the edge on trial.
        current_manifest(test_root="/workspace/other"),
    ]
    for payload in mutations:
        try:
            validate_build_requirements_v1(payload)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"accepted invalid manifest: {payload}")


def test_current_schema_rejects_nested_extras_partial_groups_and_overflow():
    valid = current_manifest()
    too_many_islands = [
        {"root": f"/workspace/p/m{index}", "system": "maven", "goal": "install"}
        for index in range(BUILD_REQUIREMENTS_DOMAIN_CAP + 1)
    ]
    partial_python = {**valid, "python_installer": "pip"}
    bad_structure = {
        **valid,
        "module_structure": {
            "schema_version": 2,
            "provenance": "inv-maven-1-0001",
            "modules": ["Core"],
            "keys": ["forged"],
        },
    }
    invalid = [
        {**valid, "survey": {**valid["survey"], "extra": None}},
        {**valid, "build_islands": too_many_islands},
        partial_python,
        bad_structure,
    ]
    for payload in invalid:
        try:
            validate_build_requirements_v1(payload)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"accepted invalid manifest: {payload}")


def test_writer_refuses_invalid_current_body_before_touching_the_container():
    orch = FakeOrch()

    assert write_build_requirements(orch, {**current_manifest(), "unknown": []}) is False

    assert REQUIREMENTS_PATH not in orch.files
    assert orch.commands == []


def test_live_reader_rejects_a_host_published_body_with_invalid_schema():
    orch = FakeOrch()
    assert write_build_requirements(orch, current_manifest()) is True
    poisoned = {**current_manifest(), "schema_version": 99}
    orch.files[REQUIREMENTS_PATH] = json.dumps(poisoned, sort_keys=True)

    read = read_live_build_requirements(orch)

    assert read.complete is False
    assert read.conflict == "record_schema_invalid"


def test_read_missing_manifest_returns_empty_dict():
    assert read_build_requirements(FakeOrch()) == {}


def test_read_corrupt_manifest_returns_empty_dict():
    orch = FakeOrch()
    orch.files[REQUIREMENTS_PATH] = "{not json"
    assert read_build_requirements(orch) == {}


from sag.tools.internal.build_preflight import JdkPreflight, active_java_major


class ProvisionOrch(FakeOrch):
    """Scriptable orchestrator: maps command substrings to canned results."""

    def __init__(
        self,
        java_version_output,
        apt_ok=True,
        temurin_ok=True,
        activate_installed_java=True,
    ):
        super().__init__()
        self.java_output = java_version_output
        self.apt_ok = apt_ok
        self.temurin_ok = temurin_ok
        self.activate_installed_java = activate_installed_java

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": self.java_output}
        if "apt-get install -y openjdk" in cmd:
            return {
                "success": self.apt_ok,
                "exit_code": 0 if self.apt_ok else 100,
                "output": "" if self.apt_ok else "E: Unable to locate package",
            }
        # Must precede the "temurin" check: the real JAVA_HOME lookup globs
        # both /usr/lib/jvm/java-N-openjdk-* and /usr/lib/jvm/temurin-N-jdk*.
        if cmd.startswith("ls -d /usr/lib/jvm"):
            return {"success": True, "exit_code": 0, "output": "/usr/lib/jvm/java-17-openjdk-arm64"}
        if "temurin" in cmd:
            return {
                "success": self.temurin_ok,
                "exit_code": 0 if self.temurin_ok else 1,
                "output": "",
            }
        if "update-alternatives --install /usr/bin/java" in cmd:
            if self.activate_installed_java:
                match = re.search(r"/java-(\d+)-", cmd)
                if match:
                    self.java_output = f'openjdk version "{match.group(1)}.0.1"'
            return {"success": True, "exit_code": 0, "output": ""}
        return self._execute_command(cmd, workdir)


def test_matching_jdk_is_a_noop():
    orch = ProvisionOrch('openjdk version "17.0.9" 2023-10-17')
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.matched is True
    assert outcome.provisioned is False
    assert outcome.narration == ""
    assert not any("apt-get" in c for c in orch.commands)


def test_active_java_major_parses_legacy_and_modern():
    assert active_java_major(ProvisionOrch('openjdk version "17.0.9"')) == "17"
    assert active_java_major(ProvisionOrch('java version "1.8.0_392"')) == "8"


def test_mismatch_provisions_and_narrates(monkeypatch):
    from test_java_provision_activation_domain import FakeJdkContainer

    orch = FakeJdkContainer(installed=("11",), linked_major="11")
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "[pre-flight] Required: Java 17" in outcome.narration
    assert "Active: Java 11" in outcome.narration


def test_jdk_install_without_durable_overlay_is_not_reported_as_provisioned(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"')
    import sag.tools.internal.build_preflight as bp

    monkeypatch.setattr(JdkPreflight, "_provision", lambda *a: "/usr/lib/jvm/java-17-openjdk-arm64")
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: False)

    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")

    assert outcome.provisioned is False
    assert outcome.mismatch is True
    assert "overlay registration failed" in outcome.narration
    assert "overlay registered" not in outcome.narration


def test_jdk_install_without_same_dispatch_postcondition_is_not_provisioned(monkeypatch):
    orch = ProvisionOrch(
        'openjdk version "11.0.2"',
        activate_installed_java=False,
    )
    import sag.tools.internal.build_preflight as bp

    monkeypatch.setattr(JdkPreflight, "_provision", lambda *a: "/usr/lib/jvm/java-17-openjdk-arm64")
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)

    outcome = JdkPreflight(orch).run("17", source="runner-observed")

    assert outcome.provisioned is False
    assert outcome.mismatch is True
    assert outcome.active_version == "11"
    assert "postcondition" in outcome.narration


def test_unprovisionable_degrades_to_mismatch_note_never_raises(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"', apt_ok=False, temurin_ok=False)
    import sag.tools.internal.build_preflight as bp

    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)
    outcome = JdkPreflight(orch).run("8", source="maven-compiler")
    assert outcome.provisioned is False
    assert outcome.mismatch is True  # verifier picks this up (Task 8)
    assert "could not provision" in outcome.narration


def test_no_requirement_is_a_noop():
    orch = ProvisionOrch('openjdk version "21.0.1"')
    outcome = JdkPreflight(orch).run(None)
    assert outcome.matched is True and outcome.narration == ""


def test_enforcer_bare_lower_bound_does_not_downgrade_current_java():
    orch = ProvisionOrch('openjdk version "17.0.9"')
    outcome = JdkPreflight(orch).run("11", source="maven-enforcer")
    assert outcome.matched is True
    assert not any("apt-get" in command for command in orch.commands)


def test_maven_java_constraints_preserve_enforcer_and_compiler_separately():
    from sag.tools.internal import java_versions

    pom = """<project><properties><maven.compiler.release>17</maven.compiler.release>
    </properties><build><plugins><plugin><artifactId>maven-enforcer-plugin</artifactId>
    <configuration><rules><requireJavaVersion><version>11</version></requireJavaVersion>
    </rules></configuration></plugin></plugins></build></project>"""
    requirements = java_versions.maven_java_requirements([(pom, "/workspace/demo/pom.xml")])
    assert requirements["runtime"] == [
        {"constraint": "11", "source": "/workspace/demo/pom.xml:requireJavaVersion"}
    ]
    assert requirements["compiler_release"] == "17"
    orch = ProvisionOrch('openjdk version "17.0.9"')
    outcome = JdkPreflight(orch).run("11", requirements=requirements)
    assert outcome.matched is True
    assert not any("apt-get" in command for command in orch.commands)


def test_literal_local_java_property_is_resolved_before_preflight():
    from sag.tools.internal.java_versions import maven_java_requirements

    pom = """<project><properties><javaVersion>17</javaVersion>
    <maven.compiler.release>${javaVersion}</maven.compiler.release>
    <runtimeRange>[11,18)</runtimeRange></properties><build><plugins><plugin>
    <artifactId>maven-enforcer-plugin</artifactId><configuration><rules>
    <requireJavaVersion><version>${runtimeRange}</version></requireJavaVersion>
    </rules></configuration></plugin></plugins></build></project>"""
    requirements = maven_java_requirements([(pom, "pom.xml")])
    assert requirements["compiler_release"] == "17"
    assert requirements["runtime"][0]["constraint"] == "[11,18)"
    assert requirements["unresolved"] == []
    assert (
        JdkPreflight(ProvisionOrch('openjdk version "17.0.9"'))
        .run("17", requirements=requirements)
        .matched
    )


@pytest.mark.parametrize(
    "properties, profiles",
    [
        ("", ""),
        ("<javaVersion>${parentVersion}</javaVersion>", ""),
        ("<javaVersion>${javaVersion}</javaVersion>", ""),
        ("<javaVersion>17</javaVersion><javaVersion>21</javaVersion>", ""),
        (
            "<javaVersion>17</javaVersion>",
            "<profiles><profile><id>other</id><properties><javaVersion>21</javaVersion></properties></profile></profiles>",
        ),
    ],
)
def test_nonliteral_or_conditional_java_property_stays_unknown(properties, profiles):
    from sag.tools.internal.java_versions import maven_java_requirements

    pom = (
        "<project><properties>"
        + properties
        + "<maven.compiler.release>${javaVersion}</maven.compiler.release></properties>"
        + profiles
        + "</project>"
    )
    requirements = maven_java_requirements([(pom, "pom.xml")])
    assert requirements["compiler_release"] is None
    assert requirements["unresolved"]


@pytest.mark.parametrize(
    "constraint, version, expected",
    [
        ("11", "17.0.9", True),
        ("[11]", "17", False),
        ("[11]", "11", True),
        ("[11]", "11.0.9", False),
        ("[11,)", "17.0.9", True),
        ("[11,17)", "17", False),
        ("(,17]", "11", True),
        ("(,17]", "21", False),
        ("[1.8,)", "1.8.0_392", True),
        ("[11,17),[21,)", "17", None),
        ("${jdk.version}", "17", None),
    ],
)
def test_enforcer_constraints_do_not_relax_exact_or_upper_bounds(constraint, version, expected):
    from sag.tools.internal import java_versions

    assert java_versions.java_constraint_matches(constraint, version) is expected


def test_independent_compiler_toolchain_does_not_change_maven_jvm():
    requirements = {
        "runtime": [{"constraint": "[11]", "source": "pom.xml:requireJavaVersion"}],
        "compiler_release": "17",
        "compiler_source": "pom.xml:maven.compiler.release",
        "compiler_toolchain": True,
    }
    orch = ProvisionOrch('openjdk version "11"')
    outcome = JdkPreflight(orch).run("11", requirements=requirements)
    assert outcome.active_version == "11"
    assert "independent compiler toolchain" in outcome.narration
    assert not any("apt-get" in command for command in orch.commands)


def test_exact_runtime_and_same_jvm_compiler_conflict_without_provisioning():
    requirements = {
        "runtime": [{"constraint": "[11]", "source": "pom.xml:requireJavaVersion"}],
        "compiler_release": "17",
        "compiler_source": "pom.xml:maven.compiler.release",
        "compiler_toolchain": False,
    }
    orch = ProvisionOrch('openjdk version "17.0.9"')
    outcome = JdkPreflight(orch).run("11", requirements=requirements)
    assert "java_constraint_conflict" in outcome.conflicts
    assert not any("apt-get" in command for command in orch.commands)


from sag.tools.internal.build_preflight import classify_version_error


def test_enforcer_message_yields_version():
    out = (
        "[ERROR] Rule 0: org.apache.maven.plugins.enforcer.RequireJavaVersion failed "
        "with message:\nDetected JDK Version: 11.0.2 is not in the allowed range [17,)."
    )
    assert classify_version_error(out) == "17"


def test_real_maven_required_java_wording_yields_version():
    assert (
        classify_version_error(
            "[ERROR] Required Java version 17 is not met by current version 11.0.27"
        )
        == "17"
    )


def test_unsupported_class_version_maps_bytecode_to_jdk():
    # class file version 61.0 = JDK 17 (44 + major)
    out = (
        "java.lang.UnsupportedClassVersionError: com/foo/Bar has been compiled by a "
        "more recent version of the Java Runtime (class file version 61.0)"
    )
    assert classify_version_error(out) == "17"


def test_invalid_target_release():
    assert (
        classify_version_error("[ERROR] Fatal error compiling: error: invalid target release: 21")
        == "21"
    )
    assert classify_version_error("error: release version 17 not supported") == "17"


def test_non_version_failures_return_none():
    assert classify_version_error("[ERROR] Failed to execute goal ... test failures") is None
    assert classify_version_error("") is None
    assert classify_version_error("BUILD SUCCESS") is None


def test_groovy_transform_typeresolver_maps_to_jdk8():
    # Live bigtop re-probe (R3): bigtop-test-framework's `mvn test` failed 27
    # attempts with the old-Groovy vs new-JDK compiler transform error. The
    # classic remediation is JDK 8. classify_version_error is pure-text, so it
    # returns the "8" sentinel; the caller's `needed != active` gate fires the
    # single re-provision when the active JDK is >= 11.
    out = (
        "[ERROR] Failed to execute goal org.codehaus.gmavenplus:gmavenplus-plugin:1.5:compile "
        "(default) on project bigtop-test-framework: Error occurred while calling a method on a "
        "Groovy class from classpath.\n"
        "Groovy:A transform used a generics containing ClassNode List <String> "
        "for the method public void install(java.util.List <String>) "
        "{ ... } directly. You are not supposed to do this. "
        "Please create a new ClassNode referring to the old ClassNode and use the new ClassNode "
        "instead of the old one. Otherwise the compiler will create wrong descriptors and a "
        "potential NullPointerException in TypeResolver in the OpenJDK. "
        "This was made with a call to public class org.apache.bigtop.itest.pmanager.PackageManager\n"
        "@ line 146, column 1."
    )
    assert classify_version_error(out) == "8"


def test_groovy_classnode_substring_alone_maps_to_jdk8():
    # The other robust substring the fix keys on — either shape is sufficient.
    out = "Groovy:A transform used a generics containing ClassNode List <String>"
    assert classify_version_error(out) == "8"


def test_groovy_error_already_on_jdk8_is_a_noop_via_the_needed_active_gate():
    # The classifier is pure-text, so it always returns "8" for this shape.
    # A build ALREADY on JDK 8 must NOT rerun: that is enforced by the call
    # sites' `needed != active` gate, not the classifier. Assert the contract
    # directly — needed == active ("8") means no re-provision fires.
    out = "Groovy:A transform used a generics containing ClassNode List <String>"
    needed = classify_version_error(out)
    active = "8"
    assert needed == "8"
    assert not (needed and needed != active)  # gate is False -> no rerun


# ---------------------------------------------------------------------------
# The steering-only set is a set of FLOORS, and a floor the running JDK already
# clears is not a requirement. The loose third wording ("<anything> requires
# Java 8") matches build chatter no provisioning pattern would ever act on, so
# without a magnitude comparison it turns an [INFO] line into an instruction to
# swap the JDK downwards under a build that was merely failing its tests.
# ---------------------------------------------------------------------------

from sag.tools.internal.build_preflight import classify_runner_java_requirement

ANIMAL_SNIFFER_INFO = "[INFO] the animal-sniffer plugin requires Java 8 to run"


def test_a_steering_floor_the_active_runtime_clears_is_not_a_requirement():
    # Pure text, no runtime named: the reader cannot compare, and says what it
    # read — this is the same answer the function has always given.
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO) == "8"
    # With the runtime named, the comparison is possible and decides it.
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO, active_version="17") is None
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO, active_version="8") is None
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO, active_version="1.8.0_362") is None
    # A runtime BELOW the floor genuinely does not meet it.
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO, active_version="7") == "8"
    # An unreadable runtime string is not a comparison; behavior is unchanged.
    assert classify_runner_java_requirement(ANIMAL_SNIFFER_INFO, active_version="unknown") == "8"


def test_the_floor_a_runtime_does_not_clear_is_read_through_the_patch_version():
    # The live geode wording, compared against the runtime as the JVM spells it.
    out = "Java version 17 or later required, but was 11.0.31"
    assert classify_runner_java_requirement(out, active_version="11.0.31") == "17"


def test_a_satisfied_floor_does_not_hide_the_one_the_runtime_misses():
    out = (
        "Java version 8 or later required\n"
        "ERROR: java version must be >= 21 and <= 24, your version: 17"
    )
    assert classify_runner_java_requirement(out, active_version="17") == "21"


def test_the_provisioning_set_keeps_its_deliberate_downgrade():
    # `classify_version_error` already authorizes an automatic re-provision in
    # both directions — the Groovy sentinel IS a downgrade to 8. The steering
    # guard is about the looser wordings only; it must not veto that set.
    out = "Groovy:A transform used a generics containing ClassNode List <String>"
    assert classify_runner_java_requirement(out, active_version="11") == "8"


def test_setup_preserves_existing_satisfying_java_instead_of_reinstalling(monkeypatch):
    from sag.tools.internal.project_setup_tool import ProjectSetupTool

    orch = ProvisionOrch('openjdk version "17.0.9"')
    tool = ProjectSetupTool(orch)
    tool._detected_java_requirements = (
        "/workspace/demo",
        {
            "runtime": [{"constraint": "11", "source": "pom.xml:requireJavaVersion"}],
            "compiler_release": "17",
            "compiler_source": "pom.xml:maven.compiler.release",
            "compiler_toolchain": False,
        },
    )
    monkeypatch.setattr(tool, "_register_maven_runtime_overlay", lambda: None)
    monkeypatch.setattr(tool, "_provision_required_maven_if_needed", lambda directory: None)
    monkeypatch.setattr(
        orch, "_execute_command", lambda *args: {"success": True, "exit_code": 0, "output": ""}
    )
    result = tool._install_dependencies_for_project_type({"type": "maven"}, "/workspace/demo", "17")
    assert result["success"]
    assert result["java_version"] == "17"
    assert result["java_requirement_status"] == "satisfied"
    assert not any("openjdk" in command or "default-jdk" in command for command in orch.commands)


def test_runner_observation_cannot_relax_declared_upper_bound():
    requirements = {
        "runtime": [{"constraint": "[11,17)", "source": "pom.xml:requireJavaVersion"}],
        "compiler_release": None,
        "compiler_source": None,
        "compiler_toolchain": False,
    }
    orch = ProvisionOrch('openjdk version "11.0.9"')
    outcome = JdkPreflight(orch).run(
        "17", source="runner_observed:receipt", requirements=requirements
    )
    assert "java_constraint_conflict" in outcome.conflicts
    assert not any("apt-get" in command for command in orch.commands)


def test_plain_fork_does_not_claim_an_independent_compiler():
    from sag.tools.internal.java_versions import maven_java_requirements

    pom = """<project><properties><maven.compiler.release>17</maven.compiler.release></properties>
    <build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration>
    <fork>true</fork></configuration></plugin></plugins></build></project>"""
    requirements = maven_java_requirements([(pom, "/workspace/p/pom.xml")])
    assert requirements["compiler_toolchain"] is False
    assert requirements["compiler_release"] == "17"


@pytest.mark.parametrize("constraint", ["[11]", "(,11]"])
def test_installing_the_right_major_does_not_prove_a_narrow_constraint(monkeypatch, constraint):
    import sag.tools.internal.build_preflight as bp

    orch = ProvisionOrch('openjdk version "17.0.9"')
    preflight = JdkPreflight(orch)

    def provision(version):
        assert version == "11"
        orch.java_output = 'openjdk version "11.0.9"'
        return "/usr/lib/jvm/java-11"

    monkeypatch.setattr(preflight, "_provision", provision)
    monkeypatch.setattr(bp, "_register_overlay", lambda *args: True)
    outcome = preflight.run(constraint, source="maven-enforcer")
    assert outcome.mismatch
    assert not outcome.provisioned
    assert "postcondition failed" in outcome.narration
