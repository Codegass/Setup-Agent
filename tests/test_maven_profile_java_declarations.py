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


@pytest.mark.parametrize("version, expected", [("17.0.20", "8"), ("1.8", "8"), (None, "8")])
def test_curator_simple_jdk_profiles_use_the_observed_runtime(version, expected):
    # Reduced from apache/curator@88dee99a85921bd0f20955e1d7efbaf896fc17f3.
    content = (
        """<project><properties><jdk-version>1.8</jdk-version>
    <short-jdk-version>8</short-jdk-version></properties><profiles>
    <profile><id>jdk-8-minus</id><activation><jdk>(,1.8]</jdk></activation>"""
        + compiler(
            "<configuration><source>${jdk-version}</source><target>${jdk-version}</target></configuration>"
        )
        + """</profile><profile><id>jdk-9-plus</id><activation><jdk>[1.9,)</jdk></activation>"""
        + compiler("<configuration><release>${short-jdk-version}</release></configuration>")
        + "</profile></profiles></project>"
    )
    result = maven_java_requirements([(content, "pom.xml")], runtime_version=version)
    assert result["compiler_release"] == expected
    assert bool(result["unresolved"]) is (version is None)


@pytest.mark.parametrize(
    "activation, version, unknown",
    [
        ("<jdk>17</jdk>", "21.0.1", False),
        ("<jdk>17</jdk>", "17.0.20", False),
        ("<jdk>[17,)</jdk><property><name>special</name></property>", "17.0.20", True),
        ("<jdk>${range}</jdk>", "17.0.20", True),
        ("<jdk>[17,)</jdk>", None, True),
    ],
)
def test_profile_activation_does_not_guess_other_conditions(activation, version, unknown):
    content = pom(
        "<activation>"
        + activation
        + "</activation>"
        + "<properties><maven.compiler.release>21</maven.compiler.release></properties>"
    )
    result = maven_java_requirements([(content, "pom.xml")], runtime_version=version)
    assert bool(result["unresolved"]) is unknown
    if activation == "<jdk>17</jdk>":
        assert result["compiler_release"] == ("21" if version.startswith("17") else "8")


def test_known_profile_activation_does_not_resolve_arbitrary_compiler_arguments():
    content = pom(
        "<activation><jdk>[17,)</jdk></activation>"
        + compiler(
            "<configuration><compilerArgs><arg>${flags}</arg></compilerArgs></configuration>"
        )
    )
    assert maven_java_requirements([(content, "pom.xml")], runtime_version="17.0.20")["unresolved"]


# The pinned Gson POM replaces Error Prone arguments with this diagnostic flag
# under a JDK-activated profile. Its profile name has no semantic significance.
DIAGNOSTIC_PROFILE = "<activation><jdk>[,21)</jdk></activation>" + compiler(
    '<configuration><compilerArgs combine.self="override">'
    "<compilerArg>-Xlint:all,-options</compilerArg></compilerArgs>"
    '<annotationProcessorPaths combine.self="override" /></configuration>'
)


@pytest.mark.parametrize(
    "arguments",
    [
        "<compilerArgs><arg>-Xlint:all,-options</arg></compilerArgs>",
        "<compilerArgument>-Xlint:all -Werror</compilerArgument>",
        "<compilerArguments><Xlint/><deprecation/></compilerArguments>",
    ],
)
def test_profile_diagnostics_do_not_invent_java_version_uncertainty(arguments):
    body = compiler("<configuration>" + arguments + "</configuration>")
    assert maven_java_requirements([(pom(body), "pom.xml")])["unresolved"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        "<compilerArgs><arg>-Xlint</arg><arg>--release</arg><arg>21</arg></compilerArgs>",
        "<compilerArgument>--source=21 -Xlint</compilerArgument>",
        "<compilerArguments><target>21</target></compilerArguments>",
        "<compilerArgs><arg>${extra.compiler.args}</arg></compilerArgs>",
        "<compilerArgs><arg>-Xlint:${diagnostic.args}</arg></compilerArgs>",
        "<compilerArgs><arg>@compiler-options.txt</arg></compilerArgs>",
        "<compilerArgs><arg>-Xplugin:Unknown</arg></compilerArgs>",
        "<compilerArgs><arg>--enable-preview</arg></compilerArgs>",
        '<compilerArgs combine.self="override"/>',
        "<compilerArgs><arg><unknown/></arg></compilerArgs>",
        "<compilerArgument>'unterminated</compilerArgument>",
    ],
)
def test_diagnostic_precision_does_not_hide_conditional_compiler_requirements(arguments):
    body = compiler("<configuration>" + arguments + "</configuration>")
    assert maven_java_requirements([(pom(body), "pom.xml")])["unresolved"]


@pytest.mark.parametrize("java, expected", [("17", []), ("7", ["jdk_mismatch"])])
def test_diagnostic_only_jdk_profile_reaches_env_judge_without_false_unknown(java, expected):
    assert env_conflicts(DIAGNOSTIC_PROFILE, java) == expected


@pytest.mark.parametrize("java, expected", [("17", []), ("21", []), ("22", ["jdk_mismatch"])])
def test_diagnostic_profile_preserves_the_declared_runtime_upper_bound(java, expected):
    content = pom(DIAGNOSTIC_PROFILE).replace(
        "<project>",
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<build><plugins><plugin><artifactId>maven-enforcer-plugin</artifactId>"
        "<configuration><rules><requireJavaVersion><version>[17,22)</version>"
        "</requireJavaVersion></rules></configuration></plugin></plugins></build>",
    )
    requirements = maven_java_requirements([(content, "pom.xml")])
    assert requirements["unresolved"] == []
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(
        java=java,
        manifest=complete_build_requirements_v1(
            java_version="8",
            java_version_source="maven-compiler",
            java_version_enforced=False,
            java_requirements=requirements,
        ),
    )
    assert validator._collect_env_conflicts() == expected
