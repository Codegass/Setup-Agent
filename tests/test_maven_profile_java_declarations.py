"""Java survey precision for the Commons CSV benchmark-profile regression."""

import pytest

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.java_versions import maven_java_requirements
from build_requirements_fakes import complete_build_requirements_v1
from test_jdk_reactor_conflicts import ConflictOrch


def pom(profile_body):
    return (
        "<project><properties><maven.compiler.target>1.8</maven.compiler.target>"
        "</properties><profiles><profile><id>benchmark</id>"
        + profile_body
        + "</profile></profiles></project>"
    )


def compiler(configuration):
    return (
        "<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>"
        "<version>${commons.compiler.version}</version>"
        + configuration
        + "</plugin></plugins></build>"
    )


CSV_PROFILE = (
    "<properties><skipTests>true</skipTests><benchmark>org.apache</benchmark></properties>"
    + compiler(
        '<configuration combine.self="override"><testIncludes><testInclude>**/*</testInclude>'
        "</testIncludes></configuration>"
    )
)


@pytest.mark.parametrize(
    "body",
    [CSV_PROFILE, compiler(""), "<properties><benchmark>org.apache</benchmark></properties>"],
)
def test_test_selection_profile_does_not_invent_a_java_constraint(body):
    requirements = maven_java_requirements([(pom(body), "pom.xml")])
    assert requirements["compiler_release"] == "8"
    assert requirements["unresolved"] == []


@pytest.mark.parametrize(
    "key", ["release", "source", "target", "testRelease", "testSource", "testTarget"]
)
@pytest.mark.parametrize("via", ["property", "plugin"])
def test_conditional_java_version_is_unknown_even_without_a_plugin(key, via):
    if via == "property":
        body = f"<properties><maven.compiler.{key}>21</maven.compiler.{key}></properties>"
    else:
        body = compiler(f"<configuration><{key}>21</{key}></configuration>")
    requirements = maven_java_requirements([(pom(body), "pom.xml")])
    assert requirements["unresolved"] == ["pom.xml:profile_activation:benchmark"]
    assert (
        maven_java_requirements(
            [(pom(body), "pom.xml")], disabled_profiles=frozenset({"benchmark"})
        )["unresolved"]
        == []
    )


@pytest.mark.parametrize(
    "config",
    [
        "<jdkToolchain><version>21</version></jdkToolchain>",
        "<executable>/opt/jdk21/bin/javac</executable><fork>true</fork>",
        "<compilerArgs><arg>--release</arg><arg>21</arg></compilerArgs>",
    ],
)
def test_conditional_compiler_execution_settings_remain_unknown(config):
    requirements = maven_java_requirements(
        [(pom(compiler("<configuration>" + config + "</configuration>")), "pom.xml")]
    )
    assert requirements["unresolved"]


@pytest.mark.parametrize(
    "body",
    [
        "<build><plugins><plugin><artifactId>maven-toolchains-plugin</artifactId></plugin></plugins></build>",
        "<build><plugins><plugin><artifactId>maven-enforcer-plugin</artifactId><configuration>"
        "<rules><requireJavaVersion><version>[21,)</version></requireJavaVersion></rules>"
        "</configuration></plugin></plugins></build>",
    ],
)
def test_conditional_toolchain_or_enforcer_is_still_a_java_requirement(body):
    assert maven_java_requirements([(pom(body), "pom.xml")])["unresolved"]


def env_conflicts(body, java):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(
        java=java,
        manifest=complete_build_requirements_v1(
            java_version="8",
            java_version_source="maven-compiler",
            java_version_enforced=False,
            java_requirements=maven_java_requirements([(pom(body), "pom.xml")]),
        ),
    )
    return validator._collect_env_conflicts()


@pytest.mark.parametrize("java, expected", [("17", []), ("7", ["jdk_mismatch"])])
def test_published_csv_requirements_preserve_actual_runtime_mismatches(java, expected):
    assert env_conflicts(CSV_PROFILE, java) == expected


def test_profile_property_uncertainty_reaches_the_physical_judge():
    assert env_conflicts(
        "<properties><maven.compiler.target>21</maven.compiler.target></properties>", "17"
    ) == ["build_requirements_unavailable"]
