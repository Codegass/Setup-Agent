"""Review-required benchmark definitions frozen from the acquired dataset.

The environment_and_commands shapes below are copied from
output/java-benchmark-20260916/dataset.json (reference tasks named in each
test). They are inlined so that the tests do not depend on that directory.
"""

import hashlib
import json
import shlex
import zipfile
from pathlib import Path

import pytest

from scripts import freeze_benchmark_definitions as freeze
from scripts.build_benchmark_requirements import task_digest
from scripts.requirements_metadata_inventory import _metadata_options, attach_preparation_inventory
from sag.benchmark.requirements import normalize_task
from sag.metrics.target_record import TargetRecord

POOL_SHA = "37b9363f4eda29dd1d47fa6cad546a30fdca4ad3"
POOL_ARGV = ["mvn", "--show-version", "--batch-mode", "--no-transfer-progress"]

# apache/commons-pool, task b7697ed58bb57ce8d524 ("command" shape)
POOL_CONFIG = {
    "command": "mvn --show-version --batch-mode --no-transfer-progress",
    "environment": [
        {"zip_member": "3_build (21, false).txt", "line": 167, "text": "distribution: temurin"},
        {"zip_member": "3_build (21, false).txt", "line": 168, "text": "java-version: 21"},
        {"zip_member": "3_build (21, false).txt", "line": 202,
         "text": "JAVA_HOME: /opt/hostedtoolcache/Java_Temurin-Hotspot_jdk/21.0.12-1/x64"},
        {"zip_member": "3_build (21, false).txt", "line": 204, "text": "MAVEN_ARGS: -ntp"},
        {"zip_member": "3_build (21, false).txt", "line": 206,
         "text": "Apache Maven 3.9.16 (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)"},
        {"zip_member": "3_build (21, false).txt", "line": 207, "text": "Maven home: /usr/share/apache-maven-3.9.16"},
        {"zip_member": "3_build (21, false).txt", "line": 208,
         "text": "Java version: 21.0.12.1, vendor: Eclipse Adoptium, runtime: /usr/lib/jvm/temurin-21-jdk-amd64"},
    ],
    "default_goals": [{"path": "pom.xml", "line": 123,
                       "text": "<defaultGoal>clean verify apache-rat:check checkstyle:check japicmp:cmp javadoc:javadoc spotbugs:check pmd:check pmd:cpd-check</defaultGoal>"}],
}

# microsoft/gctoolkit, task e6a04abafbc200018e16 ("actual_commands" shape, no Maven version)
GCTOOLKIT_CONFIG = {
    "jdk": "/opt/hostedtoolcache/Java_Microsoft_jdk/21.0.11/x64",
    "actual_commands": [{"zip_member": "2_build (21).txt", "line": 180, "kind": "runner_echoed_script_line",
                         "text": "./mvnw -B verify -Dmaven.compiler.release=21 --file pom.xml",
                         "java_tool_invocation_signal": True}],
    "working_directory": ".",
    "environment_clues": [
        {"zip_member": "2_build (21).txt", "line": 122, "text": "java-version: 21"},
        {"zip_member": "2_build (21).txt", "line": 123, "text": "distribution: microsoft"},
        {"zip_member": "2_build (21).txt", "line": 183, "text": "JAVA_HOME: /opt/hostedtoolcache/Java_Microsoft_jdk/21.0.11/x64"},
        {"zip_member": "2_build (21).txt", "line": 185, "text": "MAVEN_ARGS: -ntp"},
    ],
    "environment": None,
}

# google/compile-testing, task bc2e4b1966f59a17a75d ("commands" shape, two steps, build_tool key)
COMPILE_TESTING_CONFIG = {
    "job_name": "JDK 21",
    "selection_comparison": [{"job_id": 1, "job_name": "JDK 8"}],
    "commands": [
        {"zip_member": "0_JDK 21.txt", "line": 194, "kind": "runner_step_heading",
         "text": "mvn -B -U install clean --fail-never --quiet -DskipTests=true -Dinvoker.skip=true"},
        {"zip_member": "0_JDK 21.txt", "line": 202, "kind": "runner_step_heading", "text": "mvn -B verify"},
    ],
    "build_tool": {"name": "Maven", "version": "3.9.16",
                   "version_basis": "actual -V log or runner-image manifest explicitly linked by log"},
    "jdk": {"build_and_test": "Zulu21.0.12+8"},
    "environment_evidence": [
        {"archive": "raw/actions/google/compile-testing/runs/34238982885/attempt-1/logs.zip",
         "zip_member": "0_JDK 21.txt", "line": 198, "text": "JAVA_HOME: /opt/hostedtoolcache/Java_Zulu_jdk/21.0.12-8/x64"},
    ],
}

# apache/commons-dbutils, task 2f1ae1ca232dd52a258f (Jenkins "steps" shape)
DBUTILS_CONFIG = {
    "working_directory": "repository root at pinned SHA",
    "steps": [{"command": "mvn -B -f pom.xml -V clean test --batch-mode", "jdk": "17.0.12",
               "role": "build_and_test", "command_evidence": None}],
    "os": "Linux", "build_tool": "maven", "build_tool_versions": ["3.9.16"], "preparation": [],
}

# apache/casbin-jcasbin, task 1eea705b785c933392b2 (java-version: 1.8)
CASBIN_CONFIG = {
    "commands": ["mvn -B -ntp clean verify cobertura:cobertura -Dgpg.skip=true"],
    "environment_evidence": [
        {"zip_member": "0_build.txt", "line": 726, "text": "java-version: 1.8"},
        {"zip_member": "0_build.txt", "line": 752, "text": "JAVA_HOME: /opt/hostedtoolcache/jdk/8.0.504/x64"},
        {"zip_member": "0_build.txt", "line": 753, "text": "JAVA_HOME_8_0_504_X64: /opt/hostedtoolcache/jdk/8.0.504/x64"},
    ],
    "build_tool": "maven",
}

# apache/incubator-seata-samples, task 55e221e084cbecbc75e0 (a shell loop, not a literal command)
SEATA_CONFIG = {
    "command": "directories=(\"at-sample\" \"saga-sample\" \"tcc-sample\" \"xa-sample\")\nfor dir in \"${directories[@]}\"; do\n  for subdir in \"$dir\"/*; do\n    if [ -d \"$subdir\" ]; then\n      echo \"Entering directory $subdir\"\n      cd \"$subdir\"\n      mvn -B test\n      cd - > /dev/null\n    fi\n  done\ndone",
    "jdk": "11",
}

# google/truth, task ab99d5c999176383939f (two jobs on two JDKs)
TRUTH_CONFIG = {
    "commands": [{"text": "mvn -B verify"}],
    "jdk": {"build_job": "Temurin26.0.2+101", "test_job": "Temurin17.0.20+1"},
    "build_tool": {"name": "Maven", "version": "3.9.16"},
}


# ---------------------------------------------------------------- extraction

def test_command_shape_is_one_literal_step_with_both_versions_and_their_evidence():
    steps = freeze.literal_steps(POOL_CONFIG)
    assert [s["argv"] for s in steps] == [POOL_ARGV]
    assert steps[0]["cwd"] == "."
    versions = freeze.extract_versions(POOL_CONFIG)
    assert versions["java_major"] == 21
    assert versions["maven_version"] == "3.9.16"
    assert versions["problems"] == []
    java_text = [e["text"] for e in versions["java_evidence"]]
    assert "java-version: 21" in java_text
    assert "Java version: 21.0.12.1, vendor: Eclipse Adoptium, runtime: /usr/lib/jvm/temurin-21-jdk-amd64" in java_text
    assert [e["text"] for e in versions["maven_evidence"]] == [
        "Apache Maven 3.9.16 (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)"]
    # "Maven home: /usr/share/apache-maven-3.9.16" is a path, not a version statement.
    assert all("Maven home" not in e["text"] for e in versions["maven_evidence"])


def test_actual_commands_shape_keeps_wrapper_argv_and_has_no_maven_version():
    steps = freeze.literal_steps(GCTOOLKIT_CONFIG)
    assert [s["argv"] for s in steps] == [["./mvnw", "-B", "verify", "-Dmaven.compiler.release=21", "--file", "pom.xml"]]
    assert steps[0]["cwd"] == "."
    versions = freeze.extract_versions(GCTOOLKIT_CONFIG)
    assert versions["java_major"] == 21
    assert versions["maven_version"] is None
    assert versions["maven_evidence"] == []
    assert versions["problems"] == []
    task = freeze.frozen_task("microsoft/gctoolkit", "a" * 40, steps, versions["java_major"], versions["maven_version"])
    assert "maven_version" not in task["steps"][0]
    assert normalize_task(task) == task


def test_commands_shape_reads_text_rows_job_name_and_build_tool_but_not_rejected_cells():
    steps = freeze.literal_steps(COMPILE_TESTING_CONFIG)
    assert [s["argv"][:3] for s in steps] == [["mvn", "-B", "-U"], ["mvn", "-B", "verify"]]
    versions = freeze.extract_versions(COMPILE_TESTING_CONFIG)
    assert versions["java_major"] == 21  # "JDK 8" belongs to a rejected comparison cell
    assert versions["maven_version"] == "3.9.16"
    assert any(e["text"] == "JDK 21" for e in versions["java_evidence"])
    assert all("selection_comparison" not in e["source"] for e in versions["java_evidence"])


def test_steps_shape_reads_the_step_jdk():
    steps = freeze.literal_steps(DBUTILS_CONFIG)
    assert [s["argv"] for s in steps] == [["mvn", "-B", "-f", "pom.xml", "-V", "clean", "test", "--batch-mode"]]
    versions = freeze.extract_versions(DBUTILS_CONFIG)
    assert versions["java_major"] == 17
    assert [e["text"] for e in versions["java_evidence"]] == ["17.0.12"]


def test_java_1_8_is_major_8():
    versions = freeze.extract_versions(CASBIN_CONFIG)
    assert versions["java_major"] == 8
    assert {e["text"] for e in versions["java_evidence"]} >= {"java-version: 1.8", "JAVA_HOME: /opt/hostedtoolcache/jdk/8.0.504/x64"}
    assert freeze.normalize_major("1.8") == 8
    assert freeze.normalize_major("17.0.19") == 17


@pytest.mark.parametrize("text", [
    SEATA_CONFIG["command"],
    "mvn -B install && mvn -B verify",
    "mvn -B verify | tee build.log",
    "mvn -B verify; mvn site",
    "mvn -B verify -Drevision=$REVISION",
    "mvn -B verify -Dsha=`git rev-parse HEAD`",
    "mvn -B verify > build.log",
])
def test_shell_constructs_are_rejected(text):
    with pytest.raises(ValueError, match="literal"):
        freeze.literal_steps({"command": text})


def test_conflicting_or_out_of_range_java_majors_are_problems_not_guesses():
    truth = freeze.extract_versions(TRUTH_CONFIG)
    assert truth["java_major"] is None
    assert truth["problems"] == ["java_major_conflict"]
    old = freeze.extract_versions({"command": "mvn verify", "environment": [{"text": "java-version: 7"}]})
    assert old["java_major"] is None and old["problems"] == ["java_major_out_of_range"]
    missing = freeze.extract_versions({"command": "mvn verify"})
    assert missing["java_major"] is None and missing["problems"] == ["java_major_not_found"]


def test_ambiguous_or_prerelease_maven_versions_are_problems():
    two = freeze.extract_versions({"command": "mvn verify", "environment": [{"text": "java-version: 21"}],
                                   "maven": "3.9.16 host -> wrapper4.0.0-rc-6 bootstrap"})
    assert two["maven_version"] is None and "maven_version_conflict" in two["problems"]
    rc = freeze.extract_versions({"command": "mvn verify", "environment": [{"text": "java-version: 21"}],
                                  "maven": "3.10.0-rc-1"})
    assert rc["maven_version"] is None and "maven_version_unsupported" in rc["problems"]
    wrapper = freeze.extract_versions({"command": "./mvnw verify", "jdk": "Zulu 11.0.32-9",
                                       "maven": "wrapper Maven3.9.9 (pinned distributionUrl)"})
    assert (wrapper["java_major"], wrapper["maven_version"], wrapper["problems"]) == (11, "3.9.9", [])


def test_frozen_task_has_ordered_ci_step_ids_and_matches_the_canonical_digest():
    steps = freeze.literal_steps(COMPILE_TESTING_CONFIG)
    task = freeze.frozen_task("google/compile-testing", "b" * 40, steps, 21, "3.9.16")
    assert task["schema_version"] == 1
    assert [s["id"] for s in task["steps"]] == ["ci-step-1", "ci-step-2"]
    assert {s["runner"] for s in task["steps"]} == {"maven"}
    assert all(s["cwd"] == "." and s["java_major"] == 21 and s["maven_version"] == "3.9.16" for s in task["steps"])
    assert task_digest(task) == freeze.canonical_task_digest(task)


# ------------------------------------------------------ preparation script

def pool_task():
    return freeze.frozen_task("apache/commons-pool", POOL_SHA, [{"argv": POOL_ARGV, "cwd": "."}], 21, "3.9.16")


def test_prepare_script_mirrors_the_surviving_one_with_the_exact_metadata_argv():
    task = pool_task()
    plan = freeze.metadata_plan("commons-pool", task, batch="smoke-java21")
    step = plan["steps"][0]
    launcher = "/opt/apache-maven-3.9.16/bin/mvn"
    assert step["launcher"] == launcher
    assert step["metadata_options"] == _metadata_options(POOL_ARGV)
    help_prefix = "org.apache.maven.plugins:maven-help-plugin:3.5.2:"
    expected = [
        [launcher, "--show-version", "--batch-mode", "--no-transfer-progress", help_prefix + "effective-pom",
         "-Dverbose", "-Doutput=/metadata/commons-pool/effective-pom.xml"],
        [launcher, "--show-version", "--batch-mode", "--no-transfer-progress", help_prefix + "effective-settings",
         "-Dverbose", "-Doutput=/metadata/commons-pool/effective-settings.xml"],
    ]
    assert [c["argv"] for c in step["commands"]] == expected
    script = freeze.preparation_script("smoke-java21", [plan])
    lines = script.splitlines()
    assert lines[0] == "#!/bin/sh"
    for argv in expected:
        assert any(line.startswith("timeout ") and line.endswith(shlex.join(argv) + " >/metadata/commons-pool/"
                                                                   + argv[4].rsplit(":", 1)[1] + ".log 2>&1")
                   for line in lines), argv
    assert "git init -q /worktrees/commons-pool" in lines
    assert "git -C /worktrees/commons-pool remote add origin https://github.com/apache/commons-pool.git" in lines
    assert any("git -C /worktrees/commons-pool fetch --depth 1 origin " + POOL_SHA in line for line in lines)
    assert "git -C /worktrees/commons-pool rev-parse HEAD >/metadata/commons-pool/HEAD.txt" in lines
    assert launcher + " --version >/metadata/commons-pool/runtime.txt 2>&1" in lines
    assert "git diff --name-only HEAD -- >/metadata/commons-pool/tracked-diff.txt" in lines
    assert "git ls-files --others --exclude-standard >/metadata/commons-pool/untracked.txt" in lines
    # GNU tar 1.35 cannot extract under amd64 emulation in the Ubuntu 26.04
    # image ("Cannot open: Function not implemented"); the zip and unzip can.
    url = "https://archive.apache.org/dist/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip"
    assert url in script and url + ".sha512" in script
    assert "unzip -q -o /opt/apache-maven-3.9.16-bin.zip -d /opt" in script
    assert "tar -x" not in script
    # The probe runs in the checkout root, so its recorded cwd is truthful.
    probe_at = lines.index(launcher + " --version >/metadata/commons-pool/runtime.txt 2>&1")
    assert "cd /worktrees/commons-pool" in lines[:probe_at]
    # Metadata only: no lifecycle phase is ever requested.
    for argv in expected:
        assert not set(argv) & {"clean", "test", "verify", "install", "package", "deploy"}


def test_null_maven_version_uses_the_default_tarball_and_a_wrapper_uses_the_checkout():
    task = freeze.frozen_task("microsoft/gctoolkit", "a" * 40, freeze.literal_steps(GCTOOLKIT_CONFIG), 21, None)
    plan = freeze.metadata_plan("gctoolkit", task, batch="b-java21")
    assert plan["steps"][0]["launcher"] == "/worktrees/gctoolkit/mvnw"
    assert plan["maven_versions"] == []
    plain = freeze.frozen_task("org/x", "a" * 40, [{"argv": ["mvn", "verify"], "cwd": "."}], 17, None)
    plain_plan = freeze.metadata_plan("x", plain, batch="b-java17")
    assert plain_plan["steps"][0]["launcher"] == "/opt/apache-maven-3.9.16/bin/mvn"
    assert plain_plan["maven_versions"] == ["3.9.16"]
    # Goals are not metadata options; only frozen configuration options remain.
    assert plain_plan["steps"][0]["metadata_options"] == []


# ----------------------------------------------- metadata status and import

RUNTIME_21 = """Apache Maven 3.9.16 (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)
Maven home: /opt/apache-maven-3.9.16
Java version: 21.0.8, vendor: Eclipse Adoptium, runtime: /opt/java/openjdk
Default locale: en_US, platform encoding: UTF-8
OS name: "linux", version: "6.17.0", arch: "amd64", family: "unix"
"""

EFFECTIVE_POM = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by Maven Help Plugin -->
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>org.apache.commons</groupId>
  <artifactId>commons-pool3</artifactId>
  <version>3.0.0-SNAPSHOT</version>
  <build>
    <directory>/worktrees/commons-pool/target</directory>
    <outputDirectory>/worktrees/commons-pool/target/classes</outputDirectory>
    <finalName>commons-pool3-3.0.0-SNAPSHOT</finalName>
    <defaultGoal>clean verify apache-rat:check</defaultGoal>
  </build>
</project>
"""

SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <localRepository>/root/.m2/repository</localRepository>
  <interactiveMode>false</interactiveMode>
</settings>
"""


def simulate_container(preparation, plan, *, runtime=RUNTIME_21, pom_exit=0, head=None):
    """Write what the generated shell script leaves behind, without Docker."""
    project = preparation / plan["directory"]
    project.mkdir(parents=True, exist_ok=True)
    (project / "HEAD.txt").write_text((head or plan["commit"]) + "\n")
    (project / "tracked-diff.txt").write_text("")
    (project / "untracked.txt").write_text("")
    (project / "metadata-environment.txt").write_text("JAVA_HOME=/opt/java/openjdk\n")
    receipts = []
    for command in plan["source_commands"]:
        (preparation / command["log"]).write_text(command["name"] + " ok\n")
        receipts.append(f"{command['name']}\t0\t100.0\t101.5")
    for step in plan["steps"]:
        (preparation / step["runtime_probe"]["log"]).write_text(runtime)
        receipts.append(f"{step['id']}:runtime_probe\t0\t102.0\t102.5")
        for command in step["commands"]:
            (preparation / command["log"]).write_text("[INFO] BUILD SUCCESS\n")
            code = pom_exit if command["goal"] == "effective-pom" else 0
            if code == 0:
                body = EFFECTIVE_POM if command["goal"] == "effective-pom" else SETTINGS
                (preparation / command["output"]).write_text(body)
            receipts.append(f"{step['id']}:{command['goal']}\t{code}\t103.0\t110.25")
    (preparation / plan["receipts"]).write_text("\n".join(receipts) + "\n")


def prepared(tmp_path, **simulation):
    task = pool_task()
    preparation = tmp_path / "preparation"
    preparation.mkdir()
    plan = freeze.metadata_plan("commons-pool", task, batch="smoke-java21")
    script = preparation / "prepare-smoke-java21.sh"
    script.write_text(freeze.preparation_script("smoke-java21", [plan]))
    manifest_steps = freeze.manifest_steps(task, environment={})
    request = freeze.preparation_request("commons-pool", task, manifest_steps)
    (preparation / "request-commons-pool.json").write_text(json.dumps(request, indent=2))
    simulate_container(preparation, plan, **simulation)
    status = freeze.metadata_status(preparation, plan, script_name=script.name,
                                    image={"name": "eclipse-temurin:21-jdk", "id": "sha256:" + "0" * 64})
    spec = {"project_id": "commons-pool", "task_sha256": task_digest(task), "steps": []}
    return task, preparation, plan, status, spec


def test_written_status_and_request_are_accepted_by_attach_preparation_inventory(tmp_path):
    task, preparation, plan, status, spec = prepared(tmp_path)
    on_disk = json.loads((preparation / "commons-pool/metadata-status.json").read_text())
    assert on_disk == status
    assert status["status"] == "captured_review_required"
    assert status["metadata_only"] is True
    step = status["steps"][0]
    assert step["status"] == "captured_review_required"
    assert (step["observed_java_major"], step["observed_maven_version"]) == (21, "3.9.16")
    assert step["runtime_probe"]["cwd"] == "/worktrees/commons-pool"
    assert step["commands"][0]["elapsed_seconds"] == 7.25
    result = attach_preparation_inventory(spec, task, preparation, tmp_path / "definition")
    audit = result["metadata_preparation"]
    assert audit["status"] == "captured_for_review"
    assert audit["authorizes_complete"] is False
    inventory = audit["steps"][0]["inventory"]
    assert inventory["model_count"] == 1
    assert inventory["models"][0]["module"] == "commons-pool3"
    assert {"status", "request", "preparation_script", "HEAD.txt", "ci-step-1:effective_pom"} <= set(audit["sources"])
    # The same structured receipts also satisfy the reviewed-plan checker, so a
    # later review can promote the project without recapturing metadata.
    from scripts.build_benchmark_requirements import reviewed_preparation

    def ref(path):
        return {"sha256": hashlib.sha256((preparation / path).read_bytes()).hexdigest()}
    sources = {"preparation_script": ref("prepare-smoke-java21.sh"), "runtime": ref("commons-pool/runtime.txt"),
               "effective_pom": ref("commons-pool/effective-pom.xml"),
               "effective_settings": ref("commons-pool/effective-settings.xml"),
               "pom_log": ref("commons-pool/effective-pom.log"), "settings_log": ref("commons-pool/effective-settings.log")}
    reviewed_preparation(status, sources, task, task["steps"][0], "/worktrees/commons-pool")


def test_a_failed_effective_pom_is_a_recorded_gap_not_a_crash(tmp_path):
    task, preparation, _plan, status, spec = prepared(tmp_path, pom_exit=1)
    assert status["status"] == "incomplete_metadata"
    assert status["steps"][0]["status"] == "metadata_capture_failed"
    assert status["steps"][0]["commands"][0]["exit_code"] == 1
    assert "outputs" not in status["steps"][0]
    result = attach_preparation_inventory(spec, task, preparation, tmp_path / "definition")
    audit = result["metadata_preparation"]
    assert audit["status"] == "partial_or_unavailable"
    assert audit["steps"][0]["gaps"] == ["metadata_not_successfully_captured_at_clean_pinned_source"]


def test_a_runtime_that_differs_from_the_task_is_not_presented_as_captured(tmp_path):
    task, preparation, _plan, status, spec = prepared(
        tmp_path, runtime=RUNTIME_21.replace("Java version: 21.0.8", "Java version: 17.0.16"))
    assert status["steps"][0]["status"] == "runtime_probe_failed_or_mismatch"
    assert status["steps"][0]["observed_java_major"] == 17
    result = attach_preparation_inventory(spec, task, preparation, tmp_path / "definition")
    assert result["metadata_preparation"]["status"] == "partial_or_unavailable"


# ------------------------------------------------------------ CI target

COUNTS = {"reported_total": 381, "passed": 370, "failures": 0, "errors": 0, "skipped": 11}


def pool_target(log_counts):
    return freeze.ci_target(pool_task(), cell_id="build (21, false)",
                            ci_url="https://github.com/apache/commons-pool/actions/runs/33386416050/job/99469944619",
                            logs_url="https://api.github.com/repos/apache/commons-pool/actions/runs/33386416050/attempts/1/logs",
                            build="ok", dataset_counts=COUNTS, log_counts=log_counts, module_count=1,
                            harvested_at="2026-10-21T00:00:00+00:00")


def test_ci_target_uses_dataset_counts_when_the_archived_log_agrees():
    raw = ("2026-08-31T11:50:00.0000000Z [INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.044 s -- in org.A\n"
           "2026-08-31T11:50:01.0000000Z [INFO] Tests run: 381, Failures: 0, Errors: 0, Skipped: 11\n"
           "2026-08-31T11:50:02.0000000Z [INFO] BUILD SUCCESS\n")
    log_counts = freeze.log_test_totals(raw)
    assert log_counts == {"summary_lines": 1, "reported_total": 381, "failures": 0, "errors": 0, "skipped": 11}
    target, agreement = pool_target(log_counts)
    record = TargetRecord.model_validate(target)
    cell = record.cells[0]
    assert agreement == "agree"
    assert (cell.executed_count, cell.red_count, cell.skipped, cell.grade) == (381, 0, 11, "B")
    assert cell.modules == (".",) and cell.modules_basis == "declared"
    assert cell.command == shlex.join(POOL_ARGV)
    assert record.matched_cell == "build (21, false)"
    assert (record.repo, record.sha) == ("apache/commons-pool", POOL_SHA)


def test_older_surefire_summaries_without_a_log_level_are_counted():
    # apache/xmlgraphics-fop job 96208253680: Surefire 2.x prints its Results lines bare.
    raw = ("2026-08-19T20:03:40.1480528Z Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n"
           "2026-08-19T20:04:00.0000000Z Tests run: 26, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.043 sec"
           " - in org.apache.fop.SomeTestCase\n"
           "2026-08-19T20:05:14.8530956Z Tests run: 3655, Failures: 0, Errors: 0, Skipped: 4\n")
    assert freeze.log_test_totals(raw) == {"summary_lines": 2, "reported_total": 3657, "failures": 0, "errors": 0,
                                           "skipped": 4}


@pytest.mark.parametrize("log_counts", [
    {"summary_lines": 1, "reported_total": 380, "failures": 0, "errors": 0, "skipped": 11},
    None,
])
def test_ci_target_records_both_counts_and_asserts_none_when_they_disagree(log_counts):
    target, agreement = pool_target(log_counts)
    record = TargetRecord.model_validate(target)
    cell = record.cells[0]
    assert agreement == "disagree"
    assert (cell.executed_count, cell.red_count, cell.skipped) == (0, 0, 0)
    notes = " ".join(record.notes)
    assert "381" in notes and "unavailable" in notes
    if log_counts:
        assert "380" in notes


# ----------------------------------------------------- end to end, offline

def synthetic_dataset(root):
    """A miniature acquired dataset: one GitHub task with its raw evidence."""
    repo, sha, run, job = "apache/commons-pool", POOL_SHA, 33386416050, 99469944619
    member = "3_build (21, false).txt"
    log = ("2026-08-31T11:44:08.0000000Z ##[group]Run mvn --show-version --batch-mode --no-transfer-progress\n"
           "2026-08-31T11:44:09.0000000Z Apache Maven 3.9.16 (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)\n"
           "2026-08-31T11:45:00.0000000Z [INFO] --- surefire:3.5.6:test (default-test) @ commons-pool3 ---\n"
           "2026-08-31T11:50:01.0000000Z [INFO] Tests run: 381, Failures: 0, Errors: 0, Skipped: 11\n"
           "2026-08-31T11:51:00.0000000Z [INFO] --- apache-rat:0.16.1:check (default-cli) @ commons-pool3 ---\n"
           "2026-08-31T11:51:30.0000000Z [INFO] BUILD SUCCESS\n")
    zip_path = root / f"raw/actions/{repo}/runs/{run}/attempt-1/logs.zip"
    zip_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(member, log)
        archive.writestr("build (21, false)/system.txt", "system")
    raw_zip = zip_path.read_bytes()
    logs_url = f"https://api.github.com/repos/{repo}/actions/runs/{run}/attempts/1/logs"
    zip_path.with_name("logs.zip.meta.json").write_text(json.dumps(
        {"url": logs_url, "sha256": hashlib.sha256(raw_zip).hexdigest(), "bytes": len(raw_zip)}))
    pom = root / f"raw/configs/{repo}/{sha}/pom.xml"
    pom.parent.mkdir(parents=True)
    pom.write_text("<project><artifactId>commons-pool3</artifactId><build>"
                   "<defaultGoal>clean verify apache-rat:check</defaultGoal></build></project>\n")
    pom.with_name("pom.xml.meta.json").write_text(json.dumps(
        {"url": f"https://raw.githubusercontent.com/{repo}/{sha}/pom.xml",
         "sha256": hashlib.sha256(pom.read_bytes()).hexdigest(), "bytes": pom.stat().st_size}))
    cells = root / "actions-cell-evidence/apache__commons-pool.json"
    cells.parent.mkdir(parents=True)
    cells.write_text(json.dumps({"repo": repo, "runs": [{
        "run_id": run, "run_attempt": 1, "head_sha": sha,
        "log_archive": {"file": str(zip_path.relative_to(root)), "url": logs_url},
        "cells": [{"job_id": job, "job_name": "build (21, false)", "job_conclusion": "success",
                   "log_selection": {"members": [member], "selection": "combined_job_member"},
                   "source_revision": {"run_head_sha": sha, "run_sha_match": "match"}}]}]}))
    task = {
        "repo": repo, "task_id": "b7697ed58bb57ce8d524", "sha": sha, "tool": "maven",
        "official_ci_url": f"https://github.com/{repo}/actions/runs/{run}/job/{job}",
        "ci_identity": {"run_id": run, "run_attempt": 1, "job_id": job, "job_name": "build (21, false)",
                        "ci_url": f"https://github.com/{repo}/actions/runs/{run}/job/{job}",
                        "run_url": f"https://github.com/{repo}/actions/runs/{run}"},
        "environment_and_commands": POOL_CONFIG, "module_count": 1,
        "source_size": {"java_files": 125, "java_physical_lines": 31031},
        "reported_test_results_by_invocation": [{"scope": "selected Maven invocation", "counts": COUNTS}],
        "scope": "Entire selected official Maven CI cell.", "evidence_level": "complete_suite_counts",
    }
    project = {"repo": repo, "canonical_task_id": task["task_id"], "canonical_tool": "maven"}
    (root / "dataset.json").write_text(json.dumps({"projects": [project], "reference_tasks": [task]}))
    return task, project


OBSERVER = {"policy": "passive-maven-events-v1", "observer_sha256": "1" * 64,
            "launcher_sha256": "2" * 64, "activation_sha256": "3" * 64}


def test_select_prepare_build_and_verify_end_to_end_without_docker(tmp_path):
    dataset = tmp_path / "dataset"
    task, project = synthetic_dataset(dataset)
    row = freeze.candidate(task, project, dataset_root=dataset, twenty_repos=set(),
                           allow_two_steps=False, max_java_lines=None, max_modules=None)
    assert row["eligible"] is True, row["reasons"]
    assert (row["project_id"], row["java_major"], row["maven_version"]) == ("commons-pool", 21, "3.9.16")
    excluded = freeze.candidate(task, project, dataset_root=dataset, twenty_repos={"apache/commons-pool"},
                                allow_two_steps=False, max_java_lines=None, max_modules=None)
    assert excluded["eligible"] is False and "in_frozen_twenty" in excluded["reasons"]
    small = freeze.candidate(task, project, dataset_root=dataset, twenty_repos=set(),
                             allow_two_steps=False, max_java_lines=1000, max_modules=None)
    assert "java_lines_above_cap" in small["reasons"]

    out = tmp_path / "out"
    selection = {"schema_version": 1, "dataset": {"path": str(dataset)}, "projects": [row]}
    (tmp_path / "selection.json").write_text(json.dumps(selection))
    summary = freeze.prepare(tmp_path / "selection.json", out, ["commons-pool"], dataset, batch_label="smoke")
    assert summary["batches"] == {"smoke-java21": ["commons-pool"]}
    definition = out / "definitions/commons-pool"
    frozen = json.loads((definition / "task.json").read_text())
    assert frozen == freeze.frozen_task("apache/commons-pool", POOL_SHA, [{"argv": POOL_ARGV, "cwd": "."}], 21, "3.9.16")
    manifest = json.loads((out / "bundle/manifest.json").read_text())
    entry = manifest["projects"][0]
    assert entry["id"] == "commons-pool" and entry["ci"]["selected_cell"] == "build (21, false)"
    assert {p.name for p in (out / "bundle/ci/commons-pool").iterdir()} >= {"index.json", "source-pom.xml"}
    assert (out / "preparation/prepare-smoke-java21.sh").is_file()
    assert (out / "preparation/request-commons-pool.json").is_file()

    plan = json.loads((out / "preparation/plan-smoke-java21.json").read_text())["projects"][0]
    simulate_container(out / "preparation", plan)
    freeze.metadata_status(out / "preparation", plan, script_name="prepare-smoke-java21.sh",
                           image={"name": "eclipse-temurin:21-jdk", "id": "sha256:" + "0" * 64})

    built = freeze.build(out, ["commons-pool"], repo_root=tmp_path, observer=OBSERVER)
    assert built["commons-pool"]["status"] == "built", built
    assert {p.name for p in definition.iterdir()} == {
        "task.json", "requirements.json", "ci-target.json", "ci", "sources", "freeze.json"}
    spec = json.loads((definition / "requirements.json").read_text())
    assert spec["annotation_completeness"]["status"] == "review_required"
    assert spec["maven_observer"] == OBSERVER
    assert spec["metadata_preparation"]["status"] == "captured_for_review"
    counts = spec["ci_alignment"]["test_count_semantics"]
    assert counts["status"] == "unavailable" and set(counts["sources"]) == {"job_log"}
    assert {r["kind"] for r in spec["requirements"]} >= {"compile", "test", "package", "quality_check"}
    target = TargetRecord.model_validate_json((definition / "ci-target.json").read_bytes())
    assert target.cells[0].executed_count == 381
    record = json.loads((definition / "freeze.json").read_text())
    assert record["tier"] == "review_required"
    assert record["dataset_task_id"] == "b7697ed58bb57ce8d524"
    assert record["ci_counts"]["agreement"] == "agree"
    assert record["extraction"]["java_major"] == 21

    rows = freeze.verify(out, observer=OBSERVER)
    assert [(r["project"], r["result"]) for r in rows] == [("commons-pool", "ok")], rows


def git_blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def eligibility(dataset, task, project, **kwargs):
    return freeze.candidate(task, project, dataset_root=dataset, twenty_repos=set(), allow_two_steps=False,
                            max_java_lines=None, max_modules=None, **kwargs)


def test_contents_api_source_pom_is_accepted_only_when_its_git_blob_matches(tmp_path):
    task, project = synthetic_dataset(tmp_path)
    pom = tmp_path / f"raw/configs/{task['repo']}/{task['sha']}/pom.xml"
    meta = {"url": f"https://github.com/{task['repo']}/blob/{task['sha']}/pom.xml",
            "api_url": f"https://api.github.com/repos/{task['repo']}/contents/pom.xml?ref={task['sha']}",
            "source_sha": task["sha"], "blob_sha": git_blob_sha(pom.read_bytes()),
            "sha256": hashlib.sha256(pom.read_bytes()).hexdigest(), "bytes": pom.stat().st_size}
    pom.with_name("pom.xml.meta.json").write_text(json.dumps(meta))
    row = eligibility(tmp_path, task, project)
    assert row["eligible"] is True, row["reasons"]
    assert row["source_pom"]["provenance"] == "github_contents_api_blob"
    meta["blob_sha"] = "0" * 40
    pom.with_name("pom.xml.meta.json").write_text(json.dumps(meta))
    assert "source_pom_provenance_mismatch" in eligibility(tmp_path, task, project)["reasons"]


def test_jobs_page_and_dataset_log_binding_stand_in_for_a_missing_cell_summary(tmp_path):
    task, project = synthetic_dataset(tmp_path)
    ci = task["ci_identity"]
    (tmp_path / "actions-cell-evidence/apache__commons-pool.json").write_text(json.dumps({"runs": []}))
    assert "ci_cell_evidence_missing" in eligibility(tmp_path, task, project)["reasons"]
    task["environment_and_commands"] = {**POOL_CONFIG, "job_id": ci["job_id"], "log_job_binding": {
        "members": ["3_build (21, false).txt"], "selection": "combined_job_member", "job_binding": "exact_job_name"}}
    jobs = tmp_path / f"raw/actions/{task['repo']}/runs/{ci['run_id']}/attempt-1/jobs/page-1.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"total_count": 1, "jobs": [
        {"id": ci["job_id"], "run_id": ci["run_id"], "run_attempt": 1, "name": ci["job_name"],
         "conclusion": "success", "status": "completed", "head_sha": task["sha"]}]}))
    jobs.with_name("page-1.json.meta.json").write_text(json.dumps(
        {"sha256": hashlib.sha256(jobs.read_bytes()).hexdigest(), "bytes": jobs.stat().st_size}))
    row = eligibility(tmp_path, task, project)
    assert row["eligible"] is True, row["reasons"]
    assert row["ci"]["zip_member"] == "3_build (21, false).txt"
    assert row["ci"]["cell_evidence"].endswith("jobs/page-1.json")
    assert row["ci"]["cell_evidence_basis"] == "run_attempt_jobs_page_with_dataset_log_binding"
    assert row["ci"]["job_conclusion"] == "success"


# spring-projects/spring-grpc, task 513042eab1274d17a493 (the dataset's reviewed non-publishing projection)
GRPC_CONFIG = {
    "jdk": "/opt/hostedtoolcache/Java_Liberica_jdk/25.0.4-9/x64",
    "actual_commands": ["./mvnw -B deploy -s .mvn/settings.xml"],
    "working_directory": ".",
    "environment_clues": [{"zip_member": "0_build.txt", "line": 120, "text": "java-version: 25"}],
    "projection": {"official_command": "./mvnw -B deploy -s .mvn/settings.xml",
                   "benchmark_command": "./mvnw -B install -s .mvn/settings.xml",
                   "removed_effect": "standard Maven remote artifact deployment only"},
}


def test_the_dataset_projection_replaces_a_publishing_command():
    steps = freeze.literal_steps(GRPC_CONFIG)
    assert [s["argv"] for s in steps] == [["./mvnw", "-B", "install", "-s", ".mvn/settings.xml"]]
    assert steps[0]["projected_from"] == "./mvnw -B deploy -s .mvn/settings.xml"
    assert freeze.publishing_goals(["./mvnw", "-B", "deploy", "-s", "settings.xml"]) == ["deploy"]
    assert freeze.publishing_goals(steps[0]["argv"]) == []


def test_a_command_that_publishes_is_not_eligible(tmp_path):
    task, project = synthetic_dataset(tmp_path)
    task["environment_and_commands"] = {**POOL_CONFIG, "command": "mvn -B clean deploy -Dgpg.skip"}
    assert "publishing_goal" in eligibility(tmp_path, task, project)["reasons"]


def frozen_synthetic_definition(tmp_path, *, runtime=RUNTIME_21, image=None):
    """select -> prepare -> (simulated container) -> build for the miniature dataset."""
    dataset, twenty, out = tmp_path / "dataset", tmp_path / "twenty", tmp_path / "out"
    synthetic_dataset(dataset)
    twenty.mkdir()
    freeze.select(dataset, twenty, out, projects=["commons-pool"])
    freeze.prepare(out / "selection.json", out, ["commons-pool"], dataset, batch_label="t")
    plan = json.loads((out / "preparation/plan-t-java21.json").read_text())["projects"][0]
    simulate_container(out / "preparation", plan, runtime=runtime)
    freeze.metadata_status(out / "preparation", plan, script_name="prepare-t-java21.sh",
                           image=image or {"name": "eclipse-temurin:21-jdk", "id": "sha256:" + "0" * 64})
    built = freeze.build(out, ["commons-pool"], repo_root=tmp_path, observer=OBSERVER)
    assert built["commons-pool"]["status"] == "built", built
    return out, out / "definitions/commons-pool"


def test_select_can_be_restricted_to_a_decided_project_list(tmp_path):
    dataset, twenty = tmp_path / "dataset", tmp_path / "twenty"
    task, project = synthetic_dataset(dataset)
    twenty.mkdir()
    data = json.loads((dataset / "dataset.json").read_text())
    other = {**task, "repo": "apache/commons-other", "task_id": "c" * 20}
    data["reference_tasks"].append(other)
    data["projects"].append({"repo": other["repo"], "canonical_task_id": other["task_id"]})
    (dataset / "dataset.json").write_text(json.dumps(data))
    selection = freeze.select(dataset, twenty, tmp_path / "out", projects=["commons-pool"])
    rows = {r["project_id"]: r for r in selection["projects"]}
    assert rows["commons-pool"]["eligible"] is True
    assert "not_in_requested_set" in rows["commons-other"]["reasons"]
    assert selection["criteria"]["requested_projects"] == ["commons-pool"]
    assert json.loads((tmp_path / "out/selection.json").read_text())["counts"]["eligible"] == 1
    with pytest.raises(ValueError, match="not candidates"):
        freeze.select(dataset, twenty, tmp_path / "out2", projects=["commons-pool", "no-such-project"])


def test_image_resolution_prefers_the_jdk_tag_then_alternatives_then_a_newer_lts():
    def available(*tags):
        return lambda tag: tag in tags
    assert freeze.resolve_image(23, available("eclipse-temurin:23-jdk")) == {
        "java_major": 23, "requested": "eclipse-temurin:23-jdk", "used": "eclipse-temurin:23-jdk",
        "substitute": False, "tried": ["eclipse-temurin:23-jdk"]}
    fallback = freeze.resolve_image(23, available("eclipse-temurin:23-jdk-noble"))
    assert (fallback["used"], fallback["substitute"]) == ("eclipse-temurin:23-jdk-noble", False)
    assert fallback["tried"][:2] == ["eclipse-temurin:23-jdk", "eclipse-temurin:23"]
    newer = freeze.resolve_image(23, available("eclipse-temurin:25-jdk"))
    assert (newer["used"], newer["substitute"], newer["used_java_major"]) == ("eclipse-temurin:25-jdk", True, 25)
    assert "23" in newer["reason"] and "25" in newer["reason"]
    assert freeze.resolve_image(26, available())["used"] is None


RUNTIME_25 = RUNTIME_21.replace("Java version: 21.0.8", "Java version: 25.0.4")


def test_a_substitute_jdk_capture_is_flagged_and_never_counts_as_captured(tmp_path):
    image = {"name": "eclipse-temurin:25-jdk", "id": "sha256:" + "1" * 64, "substitute": True,
             "requested": "eclipse-temurin:21-jdk", "used_java_major": 25,
             "reason": "No eclipse-temurin image for Java 21; nearest newer LTS 25 used."}
    out, definition = frozen_synthetic_definition(tmp_path, runtime=RUNTIME_25, image=image)
    status = json.loads((out / "preparation/commons-pool/metadata-status.json").read_text())
    assert status["steps"][0]["status"] == "captured_with_substitute_jdk"
    assert status["status"] == "incomplete_metadata"
    spec = json.loads((definition / "requirements.json").read_text())
    assert spec["metadata_preparation"]["status"] == "partial_or_unavailable"
    record = json.loads((definition / "freeze.json").read_text())
    assert record["metadata_capture"]["image_substitute"] == {
        "requested": "eclipse-temurin:21-jdk", "used": "eclipse-temurin:25-jdk", "used_java_major": 25,
        "reason": image["reason"]}
    assert [(r["project"], r["result"]) for r in freeze.verify(out, observer=OBSERVER)] == [("commons-pool", "ok")]


REVISED_OBSERVER = {"policy": "passive-maven-events-v1", "observer_sha256": "1" * 64,
                    "launcher_sha256": "4" * 64, "activation_sha256": "5" * 64}


def test_the_observer_block_comes_from_a_named_revised_definition(tmp_path):
    source = tmp_path / "requirements.json"
    source.write_text(json.dumps({"schema_version": 2, "maven_observer": REVISED_OBSERVER}))
    block, ref = freeze.observer_policy(source)
    assert block == REVISED_OBSERVER
    assert ref == {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                   "field": "maven_observer"}
    source.write_text(json.dumps({"maven_observer": {**REVISED_OBSERVER, "launcher_sha256": "stale"}}))
    with pytest.raises(ValueError, match="observer"):
        freeze.observer_policy(source)


def test_a_revised_copy_changes_only_the_observer_block_and_records_both(tmp_path):
    _out, definition = frozen_synthetic_definition(tmp_path)
    original = (definition / "requirements.json").read_bytes()
    dest = tmp_path / "revised"
    observer_ref = {"path": "/x/requirements.json", "sha256": "6" * 64, "field": "maven_observer"}
    results = freeze.revise_definitions(definition.parent, ["commons-pool"], dest, REVISED_OBSERVER, observer_ref)
    assert results["commons-pool"]["status"] == "revised"
    copy = dest / "commons-pool"
    revised = (copy / "requirements.json").read_bytes()
    before, after = json.loads(original), json.loads(revised)
    assert after["maven_observer"] == REVISED_OBSERVER
    assert {k: v for k, v in after.items() if k != "maven_observer"} == {
        k: v for k, v in before.items() if k != "maven_observer"}
    for path in definition.rglob("*"):
        if path.is_file() and path.name != "requirements.json":
            assert (copy / path.relative_to(definition)).read_bytes() == path.read_bytes()
    record = json.loads((copy / "revision.json").read_text())
    assert record["old_maven_observer"] == OBSERVER and record["new_maven_observer"] == REVISED_OBSERVER
    assert record["requirements_sha256_before"] == hashlib.sha256(original).hexdigest()
    assert record["requirements_sha256_after"] == hashlib.sha256(revised).hexdigest()
    assert record["observer_source"] == observer_ref
    assert record["source_definition"] == str(definition)
    rows = freeze.verify(dest, observer=REVISED_OBSERVER, kind="revised")
    assert [(r["project"], r["result"]) for r in rows] == [("commons-pool", "ok")], rows
    with pytest.raises(ValueError, match="already exists"):
        freeze.revise_definitions(definition.parent, ["commons-pool"], dest, REVISED_OBSERVER, observer_ref)
    assert [r["result"] for r in freeze.verify(dest, observer=OBSERVER, kind="revised")] == ["failed"]
    # A copied definition without a matched CI cell (twenty commons-cli) is reported, not failed:
    # the harness and the SAG CLI accept a target without one.
    target = json.loads((copy / "ci-target.json").read_text())
    (copy / "ci-target.json").write_text(json.dumps({**target, "matched_cell": None}))
    [row] = freeze.verify(dest, observer=REVISED_OBSERVER, kind="revised")
    assert (row["result"], row["target"]) == ("ok", "no matched cell (1 cells)"), row


# ------------------------------------------------------ harness and CLI

def test_harness_admits_review_required_only_with_the_flag():
    from scripts.run_portable_harness import REVIEW_ENV, admit_definition, build_parser, review_environment

    pending = {"annotation_completeness": {"status": "review_required", "gaps": ["x"]}}
    with pytest.raises(ValueError, match="review must be complete"):
        admit_definition(pending, allow_review_required=False)
    admission = admit_definition(pending, allow_review_required=True)
    assert admission == {"status": "review_required", "admission": "allowed_by_review_required_flag"}
    assert review_environment(admission) == {REVIEW_ENV: "1"} == {"SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED": "1"}
    complete = admit_definition({"annotation_completeness": {"status": "complete", "gaps": []}},
                                allow_review_required=True)
    assert complete == {"status": "complete", "admission": "complete"}
    assert review_environment(complete) == {}
    required = ["--project", "p", "--output", "o", "--run-id", "r", "--arm", "sag", "--image", "i"]
    assert build_parser().parse_args(required).allow_review_required is False
    assert build_parser().parse_args(required + ["--allow-review-required"]).allow_review_required is True


@pytest.mark.parametrize("value,accepted", [("1", True), ("0", False), (None, False)])
def test_sag_cli_admits_review_required_only_when_the_harness_sets_the_switch(tmp_path, monkeypatch, value, accepted):
    from test_requirements_protocol_surfaces import RecordingSetupAgent, forbid_container_work, inputs, invoke

    _task, spec, _target, paths, flags = inputs(tmp_path)
    spec["annotation_completeness"] = {"status": "review_required", "gaps": ["unreviewed"]}
    paths["requirements"].write_text(json.dumps(spec))
    if value is None:
        monkeypatch.delenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", value)
    if not accepted:
        forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    if accepted:
        assert result.exit_code == 0, result.output
        assert len(RecordingSetupAgent.calls) == 1
    else:
        assert result.exit_code != 0
        assert "annotation still needs review" in result.output
        assert RecordingSetupAgent.calls == []


@pytest.mark.parametrize("allowed", [True, False])
def test_sag_cli_accepts_a_frozen_definition_only_with_the_switch(tmp_path, monkeypatch, allowed):
    from click.testing import CliRunner

    import sag.main as main_module
    import test_cli_project_exit_codes as fakes
    from test_requirements_protocol_surfaces import RecordingSetupAgent, forbid_container_work

    _out, definition = frozen_synthetic_definition(tmp_path)
    task = json.loads((definition / "task.json").read_text())
    if allowed:
        monkeypatch.setenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", "1")
    else:
        monkeypatch.delenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", raising=False)
        forbid_container_work(monkeypatch)
    RecordingSetupAgent.calls = []
    fakes.reset_config_state(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", fakes.FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", RecordingSetupAgent)
    result = CliRunner().invoke(main_module.cli, [
        "project", "https://github.com/" + task["repo"] + ".git", "--ref", task["sha"],
        "--acceptance-task-file", str(definition / "task.json"),
        "--ci-target-file", str(definition / "ci-target.json"),
        "--requirements-file", str(definition / "requirements.json")])
    if allowed:
        assert result.exit_code == 0, result.output
        call = RecordingSetupAgent.calls[0]
        assert Path(call["requirements_source_root"]).resolve() == definition.resolve()
        assert call["requirements_definition"]["annotation_completeness"]["status"] == "review_required"
    else:
        assert result.exit_code != 0
        assert "annotation still needs review" in result.output
        assert RecordingSetupAgent.calls == []
