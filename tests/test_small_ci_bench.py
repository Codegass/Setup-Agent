"""A badge, a partial pool, or another matrix cell cannot enter the benchmark."""

import json
import re

import pytest

from scripts import small_ci_bench as bench


STAGE_LOG = "[INFO] Scanning for projects...\n[INFO] Building demo 1\n[INFO] BUILD SUCCESS\n"


def test_goal_preserves_command_tokens_without_sentence_punctuation():
    entry = dict(repo="apache/demo", sha="a" * 40, jdk_major=17, maven_version="3.9.16",
                 build_url="https://ci-builds.apache.org/job/demo/1/",
                 official_command="mvn -B clean deploy -Pci",
                 local_command="mvn -B clean install -Pci", scope_note="Local install replaces deploy.")
    blocks = re.findall(r"```sh\n(.*?)\n```", bench.project_goal(entry), re.S)
    assert blocks == [entry["official_command"], entry["local_command"]]
    assert all(command.split()[-1] != "." for command in blocks)


def pool():
    return {
        "passCount": 2, "failCount": 0, "skipCount": 1,
        "suites": [{
            "name": "A", "enclosingBlockNames": ["Build"], "nodeId": "12",
            "cases": [
                {"className": "A", "name": "one", "status": "PASSED"},
                {"className": "A", "name": "one", "status": "FIXED"},
                {"className": "A", "name": "skip", "status": "SKIPPED", "skipped": True},
            ],
        }],
    }


def test_new_campaign_task_binds_measured_ci_maven_instead_of_only_prose():
    entry = dict(repo="apache/demo", sha="a" * 40, jdk_major="17", maven_version="3.9.16",
                 local_command="mvn -B clean install -Pci")
    task = bench.project_task(entry)
    assert task.steps[0].maven_version == "3.9.16"
    assert task.steps[0].java_major == 17
    assert task.steps[0].command == entry["local_command"]
    assert "Apache Maven exactly 3.9.16" in task.prompt("/workspace/demo")


def parse(report, **kwargs):
    return bench.flat_jenkins_cell(
        report, stage="Build", stage_log=STAGE_LOG, cell_id="linux17",
        command="mvn clean install", refs=("official-json",), **kwargs,
    )


def test_final_rows_keep_duplicate_names_and_skips_without_inventing_flakes():
    cell = parse(pool())
    assert cell.executed_count == len(cell.executed_ids) == 3
    assert cell.skipped == 1 and cell.red_count == cell.flaky_count == 0
    assert cell.modules == (".",) and cell.modules_basis == "log"


@pytest.mark.parametrize("damage", ["aggregate", "missing_cases", "wrong_cell", "no_node", "contradictory_skip", "status"])
def test_incomplete_or_other_cell_pool_is_rejected(damage):
    report = pool()
    suite = report["suites"][0]
    if damage == "aggregate":
        report["passCount"] = 3
    elif damage == "missing_cases":
        suite["cases"].pop()
    elif damage == "wrong_cell":
        suite["enclosingBlockNames"] = ["Build", "Windows"]
    elif damage == "no_node":
        suite.pop("nodeId")
    elif damage == "contradictory_skip":
        suite["cases"][-1]["skipped"] = False
    else:
        suite["cases"][0]["status"] = "UNKNOWN"
    with pytest.raises(bench.HarvestError):
        parse(report)


def test_nested_matrix_context_is_exact_not_just_the_first_stage_name():
    report = pool()
    context = ["Build", "Java 17 on linux"]
    report["suites"][0]["enclosingBlockNames"] = context
    assert parse(report, stage_context=context).executed_count == 3
    with pytest.raises(bench.HarvestError):
        parse(report, stage_context=["Build", "Java 21 on linux"])


def test_identity_bound_drops_all_ids_but_retains_full_count(monkeypatch):
    monkeypatch.setattr(bench, "IDENTITY_COUNT_BOUND", 1)
    assert parse(pool()).executed_count == 3
    assert parse(pool()).executed_ids == ()


def test_stage_selection_excludes_the_later_site_rerun():
    text = "[Pipeline] { (Build)\nfirst\n[Pipeline] }\n[Pipeline] // stage\n[Pipeline] { (Site)\nsecond\n[Pipeline] // stage\n"
    selected, first, last = bench.pipeline_stage(text, "Build")
    assert "first" in selected and "second" not in selected
    assert (first, last) == (1, 4)
    with pytest.raises(bench.HarvestError):
        bench.pipeline_stage(text + text, "Build")
    with pytest.raises(bench.HarvestError):
        bench.pipeline_stage(text.split("[Pipeline] // stage")[0], "Build")


def test_node_console_needs_a_complete_console_body():
    parser = bench.NodeConsole()
    parser.feed('<html><pre class="console-output">a &amp; b\n<span>BUILD SUCCESS</span></pre></html>')
    assert parser.result() == "a & b\nBUILD SUCCESS"
    truncated = bench.NodeConsole()
    truncated.feed('<pre class="console-output">partial')
    with pytest.raises(bench.HarvestError):
        truncated.result()


def test_freeze_does_not_create_an_output_when_size_is_not_ten(tmp_path):
    proposal = tmp_path / "candidates.json"
    proposal.write_text(json.dumps({"projects": [{"repo": "apache/a"}]}))
    output = tmp_path / "battery"
    with pytest.raises(ValueError, match="10 distinct"):
        bench.freeze(proposal, tmp_path, output)
    assert not output.exists()


def test_existing_benchmark_is_not_replaced(tmp_path):
    output = tmp_path / "battery"
    output.mkdir()
    with pytest.raises(FileExistsError):
        bench.freeze(tmp_path / "does-not-exist", tmp_path, output)


def test_mixed_revision_or_missing_scope_cannot_be_frozen(tmp_path, monkeypatch):
    entries = [{"repo": f"apache/a{i}", "seat": f"a{i}", "sha": "a" * 40} for i in range(10)]
    proposal = tmp_path / "candidates.json"
    proposal.write_text(json.dumps({"projects": entries}))
    monkeypatch.setattr(bench, "read_official", lambda *_: (_ for _ in ()).throw(bench.HarvestError("revision mismatch")))
    output = tmp_path / "battery"
    with pytest.raises(bench.HarvestError, match="revision mismatch"):
        bench.freeze(proposal, tmp_path, output)
    assert not output.exists()


@pytest.mark.parametrize("damage", [None, "revision", "url", "version", "scope", "bytes"])
def test_official_subject_toolchain_and_bytes_are_checked_together(tmp_path, damage):
    entry = dict(repo="apache/demo", sha="a" * 40,
                 build_url="https://ci-builds.apache.org/job/demo/1/", cell_id="demo17",
                 stage="Build", official_command="mvn clean install", jdk_major=17,
                 maven_version="3.9.16")
    metadata = dict(url=entry["build_url"], building=False, result="SUCCESS", duration=1000,
                    mavenVersionUsed="3.9.16", actions=[{
                        "remoteUrls": ["https://github.com/apache/demo.git"],
                        "lastBuiltRevision": {"SHA1": entry["sha"]},
                    }])
    if damage == "revision":
        metadata["actions"][0]["lastBuiltRevision"]["SHA1"] = "b" * 40
    elif damage == "url":
        metadata["url"] = "https://ci-builds.apache.org/job/demo/2/"
    elif damage == "version":
        metadata["mavenVersionUsed"] = "3.9.11"
    scope = "[INFO] BUILD SUCCESS\n" if damage == "scope" else STAGE_LOG
    console = (
        f"Checking out Revision {entry['sha']}\n[Pipeline] {{ (Build)\n"
        "+ mvn clean install\nJava version: 17.0.12, vendor: example\n"
        + scope + "[Pipeline] }\n[Pipeline] // stage\nFinished: SUCCESS\n"
    )
    (tmp_path / "build.json").write_text(json.dumps(metadata))
    (tmp_path / "console.log").write_text(console)
    (tmp_path / "test-report.json").write_text(json.dumps(pool()))
    (tmp_path / "source-pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><version>1</version></project>')
    files = {p.name: {"sha256": bench.runner.digest(p)} for p in tmp_path.iterdir()}
    (tmp_path / "provenance.json").write_text(json.dumps(dict(
        build_url=entry["build_url"], fetched_at="2026-09-10T00:00:00Z", files=files,
    )))
    if damage == "bytes":
        (tmp_path / "console.log").write_text(console + "changed bytes\n")
    if damage is None:
        record, counts = bench.read_official(entry, tmp_path)
        assert record.sha == entry["sha"] and counts["reported"] == 3
        assert counts["executed_non_skipped"] == counts["passed"] == 2
    else:
        with pytest.raises(bench.HarvestError):
            bench.read_official(entry, tmp_path)
