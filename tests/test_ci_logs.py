# tests/test_ci_logs.py
"""A CI job log names every module the build ran — the exact build universe.

GitHub prefixes every line with a timestamp; the readers search within the
line.  Gradle prints one `> Task :path:compileJava` per compiled project;
Maven prints a Reactor Summary once, with the display name it also prints in
SAG's own Maven receipts.
"""

import zipfile

import pytest

from sag.metrics.ci_logs import job_logs, modules_from_log

GRADLE_LOG = """
2026-06-17T21:40:01.1Z > Task :clients:compileJava
2026-06-17T21:40:02.1Z > Task :connect:runtime:compileJava FROM-CACHE
2026-06-17T21:40:03.1Z > Task :compileJava NO-SOURCE
2026-06-17T21:40:04.1Z > Task :clients:test
2026-06-17T21:40:05.1Z > Task :storage:storage-api:classes UP-TO-DATE
2026-06-17T21:40:06.1Z > Task :docs:javadoc
"""

MAVEN_REACTOR_LOG = """
2026-06-01T07:25:22.4Z [INFO] Reactor Summary for Apache Commons Parent 1.0:
2026-06-01T07:25:22.4Z [INFO]
2026-06-01T07:25:22.4Z [INFO] Apache Commons Parent ............................ SUCCESS [  1.2 s]
2026-06-01T07:25:22.4Z [INFO] Apache Camel :: Core ............................. SUCCESS [ 12.3 s]
2026-06-01T07:25:22.4Z [INFO] ignite-tools ..................................... FAILURE [  0.1 s]
2026-06-01T07:25:22.4Z [INFO] ignite-checkstyle ................................ SKIPPED
2026-06-01T07:25:22.4Z [INFO] ------------------------------------------------------------------------
2026-06-01T07:25:22.4Z [INFO] BUILD FAILURE
"""

MAVEN_SINGLE_LOG = """
2026-06-01T07:25:24.1Z [INFO] Building Apache Tomcat Migration Tool for Jakarta EE 1.0.12
2026-06-01T07:25:31.8Z [INFO]       [jar] Building jar: D:\\a\\x\\target\\test-classes\\cgi-api.jar
2026-06-01T07:25:37.2Z [INFO] Tests run: 52, Failures: 0, Errors: 0, Skipped: 0
2026-06-01T07:25:40.0Z [INFO] BUILD SUCCESS
"""


def test_gradle_compile_tasks_name_the_built_projects():
    result = modules_from_log(GRADLE_LOG)

    assert result.tool == "gradle"
    # Root `:compileJava` is the root project; `:docs:javadoc` is not a compile task.
    assert result.modules == (".", "clients", "connect/runtime", "storage/storage-api")


def test_maven_reactor_summary_counts_success_rows_only():
    result = modules_from_log(MAVEN_REACTOR_LOG)

    assert result.tool == "maven"
    assert result.modules == ("Apache Camel :: Core", "Apache Commons Parent")
    assert result.failed == 1
    assert result.skipped == 1


def test_a_single_module_maven_build_is_the_root():
    result = modules_from_log(MAVEN_SINGLE_LOG)

    assert result.tool == "maven"
    assert result.modules == (".",)


def test_single_module_packaging_messages_do_not_add_projects():
    text = MAVEN_SINGLE_LOG.replace(
        "[INFO] BUILD SUCCESS",
        "[INFO] Building jar: /workspace/target/commons-cli.jar\n"
        "[INFO] Building jar: /workspace/target/commons-cli-tests.jar\n"
        "[INFO] BUILD SUCCESS",
    )
    assert modules_from_log(text).modules == (".",)


def test_a_log_that_names_no_build_states_nothing():
    result = modules_from_log("2026-06-01T07:25:24.1Z Run actions/checkout@v4\n")

    assert result.tool is None
    assert result.modules == ()


def test_job_logs_map_the_zip_members_to_job_names(tmp_path):
    path = tmp_path / "run-1-logs.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0_JDK11 windows-latest.txt", "a")
        archive.writestr("1_JDK17 ubuntu-latest.txt", "b")
        archive.writestr("JDK17 ubuntu-latest/system.txt", "ignored")
        archive.writestr("JDK17 ubuntu-latest/3_Run tests.txt", "ignored step log")

    assert job_logs(path) == {"JDK11 windows-latest": "a", "JDK17 ubuntu-latest": "b"}


def test_failed_and_skipped_gradle_tasks_do_not_prove_a_module_built():
    result = modules_from_log(
        "> Task :ok:compileJava\n> Task :bad:compileJava FAILED\n> Task :skip:jar SKIPPED\n"
    )
    assert result.modules == ("ok",)
    assert result.failed == result.skipped == 1


def test_ansi_maven_reactor_is_read_without_crossing_blank_info_lines():
    result = modules_from_log(MAVEN_REACTOR_LOG.replace("[INFO]", "\x1b[34m[INFO]\x1b[0m"))
    assert result.modules == ("Apache Camel :: Core", "Apache Commons Parent")


def test_duplicate_job_names_are_ambiguous_instead_of_last_write_wins(tmp_path):
    path = tmp_path / "run-1-logs.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0_build.txt", "one")
        archive.writestr("1_build.txt", "two")
    assert job_logs(path) == {}


@pytest.mark.parametrize("padding", ["", ". "])
def test_maven_names_with_versions_and_short_padding(padding):
    text = f"[INFO] Reactor Summary:\n[INFO] RocketMQ 5.5.1 {padding}SUCCESS\n[INFO] BUILD SUCCESS"
    assert modules_from_log(text).modules == ("RocketMQ 5.5.1",)


@pytest.mark.parametrize("second_status", ["SUCCESS", "FAILURE", "SKIPPED"])
def test_duplicate_maven_labels_cannot_define_an_exact_universe(second_status):
    text = (
        "[INFO] Reactor Summary:\n[INFO] duplicate ... SUCCESS\n"
        f"[INFO] duplicate ... {second_status}\n[INFO] BUILD SUCCESS"
    )
    result = modules_from_log(text)
    assert result.modules == ()
    assert result.ambiguous_labels == ("duplicate",)
    assert result.notes


def test_multiple_reactors_are_not_merged_by_display_name():
    result = modules_from_log(MAVEN_REACTOR_LOG + MAVEN_REACTOR_LOG)
    assert result.modules == ()
    assert result.notes


@pytest.mark.parametrize(
    "text",
    [
        "[INFO] BUILD SUCCESS\n[INFO] Building app 1.0\n[INFO] BUILD FAILURE",
        "[INFO] BUILD SUCCESS\n[INFO] Building app 1.0",
        MAVEN_SINGLE_LOG + "\n[INFO] BUILD FAILURE",
        MAVEN_SINGLE_LOG + "\n[INFO] Scanning for projects...",
        "[INFO] Reactor Summary:\n[INFO] only ... SUCCESS",
        MAVEN_REACTOR_LOG + "\n[INFO] Building unfinished 1.0",
        "[INFO] Scanning for projects...\n[INFO] Building unfinished\n"
        "[INFO] Scanning for projects...\n" + MAVEN_REACTOR_LOG,
        "##[group]Run mvn test\n[INFO] Building unfinished 1.0\n##[endgroup]\n"
        "##[group]Run another script\n[INFO] BUILD SUCCESS\n##[endgroup]",
    ],
)
def test_mixed_or_unfinished_maven_logs_do_not_prove_root_scope(text):
    assert modules_from_log(text).modules == ()


def test_frozen_camel_examples_block_proves_all_83_names_but_two_blocks_do_not():
    from pathlib import Path

    from sag.metrics.maven_reactor import parse_maven_reactor

    text = (
        Path(__file__).parent / "fixtures/d3r1_remediation/maven_reactor/camel-examples.txt"
    ).read_text()
    block = parse_maven_reactor(text)[0]
    one_build = "\n".join(text.splitlines()[block.start_line - 1 : block.end_line])
    assert len(modules_from_log(one_build).modules) == 83
    assert modules_from_log(text).modules == ()


def test_frozen_camel_duplicate_names_do_not_hide_failed_or_skipped_modules():
    from pathlib import Path

    text = (Path(__file__).parent / "fixtures/d3r1_remediation/maven_reactor/camel.txt").read_text()
    result = modules_from_log(text)
    assert result.modules == ()
    assert len(result.ambiguous_labels) == 2
    assert result.failed == 2
    assert result.skipped == 36
