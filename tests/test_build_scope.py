# tests/test_build_scope.py
"""The reactor a CI command selects on one commit — exact for the declared scope.

`settings.gradle` includes name Gradle project paths and may remap a project
directory (kafka: `:storage:storage-api` lives in `storage/api`); a Maven
reactor is the `<modules>` tree, widened by the profiles the command
activates and narrowed by its `-pl`.  Maven modules are stated by the display
name the Reactor Summary prints, so the log rung and this rung agree.
"""

import pytest

from sag.metrics.build_scope import (
    gradle_declared_projects,
    maven_declared_modules,
    maven_default_goal,
    parse_ci_command,
)

KAFKA_SETTINGS = """
rootProject.name = 'kafka'
include 'clients',
    'connect:api',
    'connect:runtime',
    'core',
    'storage:storage-api'
include("streams", "streams:examples")
project(":storage:storage-api").projectDir = file("storage/api")
"""

ROOT_POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>parent</artifactId>
  <name>Ignite Parent</name>
  <build><defaultGoal>clean verify</defaultGoal></build>
  <modules>
    <module>modules/core</module>
    <module>modules/tools</module>
  </modules>
  <profiles>
    <profile>
      <id>checkstyle</id>
      <modules><module>modules/checkstyle</module></modules>
    </profile>
  </profiles>
</project>
"""
CHILD_POMS = {
    "modules/core": "<project><artifactId>ignite-core</artifactId><name>ignite-core</name></project>",
    "modules/tools": "<project><artifactId>ignite-tools</artifactId></project>",
    "modules/checkstyle": "<project><artifactId>ignite-checkstyle</artifactId><name>Ignite Checkstyle</name></project>",
}


def test_gradle_settings_name_every_included_project_by_directory():
    assert gradle_declared_projects(KAFKA_SETTINGS) == (
        ".",
        "clients",
        "connect",
        "connect/api",
        "connect/runtime",
        "core",
        "storage",
        "storage/api",
        "streams",
        "streams/examples",
    )


def test_maven_reactor_uses_display_names_and_profiles_widen_it():
    read = CHILD_POMS.get

    assert maven_declared_modules(ROOT_POM, read) == (
        "Ignite Parent",
        "ignite-core",
        "ignite-tools",
    )
    assert maven_declared_modules(ROOT_POM, read, active_profiles=("checkstyle",)) == (
        "Ignite Checkstyle",
        "Ignite Parent",
        "ignite-core",
        "ignite-tools",
    )


def test_maven_pl_narrows_by_directory_or_artifact_id():
    read = CHILD_POMS.get

    assert maven_declared_modules(ROOT_POM, read, projects=("modules/core",)) == ("ignite-core",)
    assert maven_declared_modules(ROOT_POM, read, projects=("ignite-tools",)) == ("ignite-tools",)


def test_maven_default_goal_is_read_or_absent():
    assert maven_default_goal(ROOT_POM) == "clean verify"
    assert maven_default_goal("<project><artifactId>x</artifactId></project>") is None


@pytest.mark.parametrize(
    "text, tool, goals, profiles, projects, excluded",
    [
        ("mvn -V test --file pom.xml --no-transfer-progress", "maven", ("test",), (), (), ()),
        (
            "./mvnw -B -Pfast,ci -pl core,tools -am verify checkstyle:check",
            "maven",
            ("verify", "checkstyle:check"),
            ("fast", "ci"),
            ("core", "tools"),
            (),
        ),
        (
            "./gradlew --info build -x test -x javadoc --scan",
            "gradle",
            ("build",),
            (),
            (),
            ("test", "javadoc"),
        ),
        ("mvn", "maven", (), (), (), ()),
        ("echo hi && npm test", "unknown", (), (), (), ()),
    ],
)
def test_parse_ci_command(text, tool, goals, profiles, projects, excluded):
    command = parse_ci_command(text)

    assert command.tool == tool
    assert command.goals == goals
    assert command.profiles == profiles
    assert command.projects == projects
    assert command.excluded_tasks == excluded


def test_a_multi_line_run_script_takes_its_build_line():
    command = parse_ci_command("set -e\nexport JAVA_HOME=/x\n./gradlew test\necho done\n")

    assert command.tool == "gradle"
    assert command.goals == ("test",)
    assert command.text == "set -e\nexport JAVA_HOME=/x\n./gradlew test\necho done"
    assert command.unsupported_scope_reason == "script contains unresolved shell context"


@pytest.mark.parametrize("text", ["echo mvn test", "echo './gradlew build'"])
def test_mentions_are_not_build_invocations(text):
    assert parse_ci_command(text).tool == "unknown"


def test_command_selectors_are_retained_and_unsupported_scope_is_disclosed():
    cmd = parse_ci_command("mvn --file=child/pom.xml --activate-profiles=ci --projects=:core test")
    assert cmd.build_file == "child/pom.xml"
    assert cmd.profiles == ("ci",)
    assert cmd.projects == (":core",)
    assert cmd.unsupported_scope_reason is None
    assert parse_ci_command("gradle -p child test").project_dir == "child"
    assert parse_ci_command("mvn -pl core -am test").unsupported_scope_reason
    assert parse_ci_command("mvn -P${PROFILE} test").unsupported_scope_reason
    assert parse_ci_command("mvn test && mvn verify").unsupported_scope_reason


def test_a_missing_or_dynamic_pom_never_shrinks_the_declared_reactor():
    assert maven_declared_modules(ROOT_POM, lambda _: None) == ()
    dynamic = ROOT_POM.replace("modules/core", "${module.path}")
    assert maven_declared_modules(dynamic, CHILD_POMS.get) == ()


def test_unknown_project_selector_never_returns_a_partial_selection():
    assert (
        maven_declared_modules(ROOT_POM, CHILD_POMS.get, projects=("modules/core", "missing")) == ()
    )
    assert maven_declared_modules(ROOT_POM, CHILD_POMS.get, projects=(":ignite-core",)) == (
        "ignite-core",
    )


def test_single_module_maven_root_uses_the_same_key_as_log_and_receipt():
    assert maven_declared_modules(
        "<project><artifactId>x</artifactId></project>", lambda _: None
    ) == (".",)


def test_gradle_comments_do_not_invent_modules_and_dynamic_includes_are_unknown():
    assert gradle_declared_projects("// include 'ghost'\ninclude 'real'") == (".", "real")
    assert gradle_declared_projects("include 'real'\ninclude(projectNames)") == ()
    assert gradle_declared_projects("if (enableExtra) { include 'extra' }") == ()


def test_profile_activation_without_a_known_environment_is_unknown():
    pom = ROOT_POM.replace(
        "<id>checkstyle</id>", "<id>checkstyle</id><activation><jdk>17</jdk></activation>"
    )
    assert maven_declared_modules(pom, CHILD_POMS.get) == ()


def test_fail_at_end_is_a_flag_and_never_a_build_file():
    assert parse_ci_command("mvn -fae test").build_file is None


def test_property_controlled_execution_is_not_assumed_equivalent():
    assert parse_ci_command("mvn test -DskipTests").unsupported_scope_reason


def test_a_mixed_literal_dynamic_gradle_include_is_unresolved():
    assert gradle_declared_projects("include 'real', extraProject") == ()


def test_gradle_directory_mapping_preserves_project_keys():
    from sag.metrics.build_scope import gradle_project_directories

    assert gradle_project_directories(KAFKA_SETTINGS)["storage/storage-api"] == "storage/api"


def test_active_by_default_profile_is_included_when_no_explicit_profile_applies():
    pom = ROOT_POM.replace(
        "<id>checkstyle</id>",
        "<id>checkstyle</id><activation><activeByDefault>true</activeByDefault></activation>",
    )
    assert "Ignite Checkstyle" in maven_declared_modules(pom, CHILD_POMS.get)


def test_nested_gradle_include_enumerates_parent_and_preserves_explicit_remaps():
    from sag.metrics.build_scope import gradle_project_directories

    assert gradle_project_directories('include(":parent:child")\n') == {
        ".": ".",
        "parent": "parent",
        "parent/child": "parent/child",
    }
    settings = """include(":parent:child")
project(":parent").projectDir = file("parent-with-build")
project(":parent:child").projectDir = file("child-with-build")
"""
    assert gradle_declared_projects(settings) == (".", "child-with-build", "parent-with-build")


def test_shell_directory_preamble_is_not_discarded_for_parity():
    from sag.metrics.parity import parity_from_texts

    command = "cd subproject\nmvn test"
    assert parse_ci_command(command).text == command
    assert parity_from_texts(command, ["mvn test"]).status == "unknown"
