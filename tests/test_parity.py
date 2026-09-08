# tests/test_parity.py
"""Lifecycle parity: the CI command is the build's specification.

Its own axis — never folded into alpha — stating whether SAG's dispatches
reached what the CI command runs, and naming what they did not.
"""

from sag.metrics.build_scope import parse_ci_command
from sag.metrics.parity import command_parity


def _maven(text):
    return parse_ci_command(text)


def test_reaching_the_ci_phase_is_equivalent():
    parity = command_parity(
        _maven("mvn -V test --file pom.xml"),
        [_maven("mvn --fail-at-end compile"), _maven("mvn test")],
    )

    assert parity.status == "equivalent"
    assert parity.form == "maven_phases"
    assert parity.ci_reach == "test"
    assert parity.sag_reach == "test"
    assert parity.missing == ()


def test_stopping_short_names_the_missing_phases_and_plugin_goals():
    parity = command_parity(
        _maven("mvn -B verify japicmp:cmp checkstyle:check"),
        [_maven("mvn compile"), _maven("mvn test")],
    )

    assert parity.status == "not_equivalent"
    assert parity.missing == (
        "package",
        "integration-test",
        "verify",
        "checkstyle:check",
        "japicmp:cmp",
    )


def test_a_bare_mvn_resolves_through_the_pom_default_goal_or_stays_unknown():
    assert command_parity(_maven("mvn"), [_maven("mvn test")]).status == "unknown"
    resolved = command_parity(_maven("mvn"), [_maven("mvn test")], ci_default_goal="clean verify")
    assert resolved.status == "not_equivalent"
    assert resolved.ci_reach == "verify"


def test_gradle_tasks_compare_as_sets_after_exclusions():
    ci = parse_ci_command("./gradlew build -x javadoc")
    assert command_parity(ci, [parse_ci_command("./gradlew build")]).status == "equivalent"
    short = command_parity(ci, [parse_ci_command("./gradlew test")])
    assert short.status == "not_equivalent"
    assert short.missing == ("build",)


def test_different_tools_or_no_sag_commands_are_unknown():
    assert (
        command_parity(_maven("mvn test"), [parse_ci_command("./gradlew test")]).status == "unknown"
    )
    assert command_parity(_maven("mvn test"), []).status == "unknown"


def test_gradle_sag_exclusions_do_not_claim_the_excluded_task_was_run():
    result = command_parity(
        parse_ci_command("gradle test"), [parse_ci_command("gradle test -x test")]
    )
    assert result.status == "not_equivalent"
    assert result.missing == ("test",)


def test_maven_unrecognized_goal_does_not_claim_equivalence():
    assert command_parity(_maven("mvn mystery"), [_maven("mvn test")]).status == "unknown"


def test_maven_nonmilestone_phase_still_requires_reaching_that_phase():
    result = command_parity(_maven("mvn test-compile"), [_maven("mvn compile")])
    assert result.status == "not_equivalent"
    assert result.missing == ("test-compile",)


def test_profile_mismatch_does_not_claim_lifecycle_equivalence():
    assert command_parity(_maven("mvn -Pci test"), [_maven("mvn test")]).status == "unknown"


def test_a_new_gradle_exclusion_can_remove_transitive_work():
    result = command_parity(
        parse_ci_command("gradle build"), [parse_ci_command("gradle build -x test")]
    )
    assert result.status == "unknown"
