"""Top-level Maven completion survives later, separately scoped fork failures."""

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import fail_fast_observable, maven_events
from test_benchmark_requirement_evaluator import banner, fixture, requirement, write


def marker(direction, *, goal="javadoc", execution="default", module="demo"):
    delimiter, relation = (">>>", ">") if direction == "enter" else ("<<<", "<")
    return f"[INFO] {delimiter} maven-{goal}-plugin:1.0:{goal} ({execution}) {relation} generate-sources @ {module} {delimiter}\n"


def test_javadoc_fork_failure_preserves_prior_compile_and_test(fixture):
    _, spec, record, _, base, score = fixture
    spec["requirements"].append(requirement("docs", "documentation", "javadoc:javadoc"))
    prefix = (base / "log.txt").read_text().replace("[INFO] BUILD SUCCESS\n", "")
    log = (
        prefix
        + marker("enter")
        + banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + marker("exit")
        + banner("javadoc:javadoc")
        + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-javadoc-plugin:1.0:javadoc (default) on project demo: bad docs\n[INFO] BUILD FAILURE\n"
    )
    record.update(exit_code=1, reports_collection_complete=True, log=write(base, "log.txt", log))
    output = score()
    assert [row["status"] for row in output["requirements"]] == ["passed", "passed", "failed"]
    assert output["known_blockers"] == ["docs"]
    events = maven_events(log, terminal=True, serial=True)
    assert [e["occurrence"] for e in events if e["goal"] == "compiler:compile"] == [0]
    assert fail_fast_observable(record, log) is False


def test_nested_only_compile_cannot_satisfy_top_level_compile(fixture):
    _, _, record, _, base, _ = fixture
    log = (
        marker("enter")
        + banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + marker("exit")
        + "[INFO] BUILD SUCCESS\n"
    )
    events = maven_events(log, terminal=True, serial=True)
    assert events == []
    assert (
        native_requirement(
            requirement("compile", "compile", "compiler:compile"), record, base, log, events
        )["status"]
        == "unavailable"
    )


def test_balanced_fork_allows_later_independent_top_level_goals():
    log = (
        marker("enter")
        + banner("compiler:compile")
        + marker("enter", goal="jar")
        + banner("resources:resources")
        + marker("exit", goal="jar")
        + marker("exit")
        + banner("javadoc:javadoc")
        + "[INFO] Generated documentation\n"
        + banner("checkstyle:check")
        + "[INFO] BUILD SUCCESS\n"
    )
    assert [(e["goal"], e["status"]) for e in maven_events(log, terminal=True, serial=True)] == [
        ("javadoc:javadoc", "passed"),
        ("checkstyle:check", "passed"),
    ]


@pytest.mark.parametrize(
    "prefix",
    [
        marker("enter"),
        marker("exit"),
        marker("enter") + marker("exit", module="other"),
        marker("enter") + marker("exit", execution="other"),
        "[INFO] >>> malformed boundary >>>\n",
    ],
)
def test_unbalanced_or_malformed_fork_does_not_upgrade_later_goals(prefix):
    log = (
        prefix
        + banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n[INFO] BUILD SUCCESS\n"
    )
    assert maven_events(log, terminal=True, serial=True) == []


def test_nested_failure_does_not_rewrite_same_named_previous_top_level_goal():
    log = (
        banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + marker("enter")
        + banner("compiler:compile")
        + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: nested failure\n[INFO] BUILD FAILURE\n"
    )
    events = maven_events(log, terminal=True, serial=True)
    assert [(e["goal"], e["status"]) for e in events] == [("compiler:compile", "passed")]
    assert "nested failure" not in events[0]["segment"]


def test_fork_entry_is_no_completion_witness_without_serial_execution():
    log = (
        banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + marker("enter")
        + marker("exit")
        + "[INFO] BUILD SUCCESS\n"
    )
    assert maven_events(log, terminal=True, serial=False)[0]["status"] == "unavailable"


@pytest.mark.parametrize("goal,kind", [("report", "documentation"), ("check", "quality_check")])
@pytest.mark.parametrize(
    "message",
    [
        "Skipping JaCoCo execution due to missing execution data file.",
        "Skipping JaCoCo execution because property jacoco.skip is set.",
        "Skipping JaCoCo execution due to missing execution data file:/tmp/project/target/jacoco.exec",
    ],
)
def test_jacoco_skips_never_become_success_from_exit_zero(fixture, goal, kind, message):
    _, _, record, _, base, _ = fixture
    row = requirement("coverage-" + goal, kind, "jacoco:" + goal)
    log = f"[INFO] --- jacoco:0.8.11:{goal} (default) @ demo ---\n[INFO] {message}\n[INFO] BUILD SUCCESS\n"
    events = maven_events(log, terminal=True, serial=True)
    assert events[0]["status"] == "not_run"
    result = native_requirement(row, record, base, log, events)
    assert result["status"] == "not_run" and result["reason"] == "explicit_native_skip"


def test_jacoco_report_success_does_not_satisfy_check(fixture):
    _, _, record, _, base, _ = fixture
    log = "[INFO] --- jacoco:0.8.11:report (default) @ demo ---\n[INFO] Analyzed bundle 'demo' with 2 classes\n[INFO] BUILD SUCCESS\n"
    events = maven_events(log, terminal=True, serial=True)
    assert (
        native_requirement(
            requirement("report", "documentation", "jacoco:report"), record, base, log, events
        )["status"]
        == "passed"
    )
    assert (
        native_requirement(
            requirement("check", "quality_check", "jacoco:check"), record, base, log, events
        )["status"]
        == "unavailable"
    )


@pytest.mark.parametrize("goal", ["surefire:test", "failsafe:integration-test"])
def test_explicit_no_tests_cannot_reuse_another_report(fixture, goal):
    _, _, record, _, base, _ = fixture
    row = requirement("tests", "test", goal)
    plugin, name = goal.split(":")
    log = f"[INFO] --- maven-{plugin}-plugin:3.5.0:{name} (default) @ demo ---\n[INFO] No tests to run.\n[INFO] BUILD SUCCESS\n"
    events = maven_events(log, terminal=True, serial=True)
    assert events[0]["status"] == "not_run"
    # The fixture retains a green XML report; explicit native no-tests wins.
    assert native_requirement(row, record, base, log, events)["status"] == "not_run"


def test_absent_test_summary_is_not_an_explicit_skip(fixture):
    _, _, record, _, base, _ = fixture
    log = "[INFO] --- maven-surefire-plugin:3.5.0:test (default) @ demo ---\n[INFO] BUILD SUCCESS\n"
    events = maven_events(log, terminal=True, serial=True)
    assert events[0]["status"] == "passed"
    assert (
        native_requirement(
            requirement("tests", "test", "surefire:test"), record, base, log, events
        )["status"]
        == "passed"
    )
