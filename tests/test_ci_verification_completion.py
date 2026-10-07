"""Independent archived Jenkins/Gson fixtures and counterfactual evidence tests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from sag.benchmark.ci_sources import archive_sources, read_source, reviewed_steps
from sag.benchmark.ci_verification import compare_verified, verify_reference
from sag.benchmark.ci_jenkins import selected_jenkins
from sag.benchmark.requirements import normalize_task
from test_selected_ci_verification import summary


@pytest.fixture
def archived(tmp_path):
    def load(project):
        base = tmp_path / project
        with zipfile.ZipFile(
            Path(__file__).parent / "fixtures" / f"selected-ci-{project}.zip"
        ) as archive:
            assert all((base / p).resolve().is_relative_to(base) for p in archive.namelist())
            archive.extractall(base)
        return (
            base,
            normalize_task(json.loads((base / "task.json").read_text())),
            json.loads((base / "requirements.json").read_text()),
        )

    return load


def replace(base, ref, value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    (base / ref["path"]).write_bytes(raw)
    ref.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def test_real_jenkins_command_runtime_and_native_report_are_one_task(archived):
    base, task, spec = archived("commons-dbutils")
    reference = verify_reference(task, spec, base)
    assert reference["identity"]["build_number"] == 455
    assert reference["identity"]["sha"] == task["sha"]
    assert reference["runtimes"]["ci-step-1"]["java_major"] == 17
    assert reference["counts"]["reported"] == reference["counts"]["assessed"] == 523
    result = compare_verified(task, spec, summary(spec, reference["counts"]), source_base=base)
    assert result["scope"]["status"] == "met"
    assert result["test_counts"]["status"] == "equal_counts"
    assert result["case_identity"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "mutation",
    [
        "commit",
        "repository",
        "build_url",
        "building",
        "failure",
        "api_counts",
        "command",
        "pom_path",
        "java",
        "maven",
        "truncated",
        "duplicate",
        "checkout",
    ],
)
def test_jenkins_cannot_join_different_builds_commands_or_environments(archived, mutation):
    base, task, spec = archived("commons-dbutils")
    local = summary(spec, verify_reference(task, spec, base)["counts"])
    index_ref = spec["ci_alignment"]["archived_ci_index"]
    index = json.loads(read_source(base, index_ref))
    build_ref, log_ref = index["evidence"][:2]
    build = json.loads(read_source(base, build_ref))
    git = next(
        a for a in build["actions"] if a.get("_class") == "hudson.plugins.git.util.BuildData"
    )
    if mutation == "commit":
        git["lastBuiltRevision"]["SHA1"] = "f" * 40
    elif mutation == "repository":
        git["remoteUrls"] = ["https://github.com/other/project.git"]
    elif mutation == "build_url":
        build["url"] = build["url"].replace("455", "456")
    elif mutation == "building":
        build["building"] = True
    elif mutation == "failure":
        build["result"] = "FAILURE"
    elif mutation == "api_counts":
        next(
            a
            for a in build["actions"]
            if a.get("_class") == "hudson.maven.reporters.SurefireAggregatedReport"
        )["totalCount"] += 1
    else:
        raw = read_source(base, log_ref)
        if mutation == "command":
            raw = raw.replace(b"-V clean test --batch-mode", b"-V test --batch-mode")
        elif mutation == "pom_path":
            raw = raw.replace(
                b"-f /home/jenkins/workspace/Commons/commons-dbutils/pom.xml", b"-f /other/pom.xml"
            )
        elif mutation == "java":
            raw = raw.replace(b"Java version: 17.0.12", b"Java version: 21.0.12")
        elif mutation == "maven":
            raw = raw.replace(b"Apache Maven 3.9.16", b"Apache Maven 3.8.16")
        elif mutation == "truncated":
            raw = raw.replace(b"Finished: SUCCESS", b"")
        elif mutation == "duplicate":
            raw += raw
        elif mutation == "checkout":
            raw = raw.replace(
                b"Checking out Revision " + task["sha"].encode(),
                b"Checking out Revision " + b"a" * 40,
            )
        replace(base, log_ref, raw)
    replace(base, build_ref, build)
    replace(base, index_ref, index)
    result = compare_verified(task, spec, local, source_base=base)
    assert result["scope"]["status"] == result["test_counts"]["status"] == "unavailable"
    assert result["scope"]["rate"] is None


def test_gson_empty_pool_is_source_verified_and_never_counted_as_pass(archived):
    base, task, spec = archived("gson")
    reference = verify_reference(task, spec, base)
    assert reference["counts"] == dict(
        reported=4906, skipped=22, assessed=4884, passed=4884, failed=0, errors=0
    )
    assert len(reference["pools"]) == 7
    assert len(reference["requirement_ids"]) == 51
    shrinker = next(
        r for r in reference["excluded_test_invocations"] if r["module"] == "test-shrinker"
    )
    assert shrinker["reason"] == "source_verified_empty_default_selection"
    assert not any(
        r["module"] == "test-shrinker" and r["goal"] == "surefire:test" for r in reference["pools"]
    )
    relocated = base.parent / "relocated"
    archive_sources(spec, base, relocated)
    assert verify_reference(task, spec, relocated) == reference


@pytest.mark.parametrize("mutation", ["number", "url", "module"])
def test_jenkins_child_report_must_belong_to_the_selected_build(archived, monkeypatch, mutation):
    base, task, spec = archived("commons-dbutils")
    semantics = deepcopy(spec["ci_alignment"]["test_count_semantics"])
    ref = semantics["sources"]["test_report"]
    report = json.loads(read_source(base, ref))
    child = report["childReports"][0]["child"]
    if mutation == "number":
        child["number"] += 1
    elif mutation == "url":
        child["url"] = child["url"].replace("/455/", "/456/")
    else:
        child["url"] = child["url"].replace("commons-dbutils/455/", "other-module/455/")
    replace(base, ref, report)
    # Isolate the child-to-build join after successful count-semantics validation.
    monkeypatch.setattr(
        "sag.benchmark.ci_jenkins.validate_ci_count_metadata", lambda *a, **kw: semantics
    )
    with pytest.raises(ValueError, match="another build|frozen test module"):
        selected_jenkins(task, spec, base)


@pytest.mark.parametrize(
    "part",
    [
        "git_tree",
        "git_commit",
        "surefire_sources",
        "test_source",
        "compiler_bundle",
        "reviewer",
        "model",
    ],
)
def test_empty_pool_requires_its_whole_proof_in_the_verifier(archived, part):
    base, task, spec = archived("gson")
    review = reviewed_steps(spec)[0]
    binding = next(
        b
        for b in review["ci_bindings"]
        if b.get("disabled_witness", {}).get("type") == "source_empty_surefire_selection"
    )
    witness = binding["disabled_witness"]
    if part == "reviewer":
        witness.pop("reviewed_by")
    elif part == "model":
        witness["effective_pom_sha256"] = "0" * 64
    else:
        ref = (
            witness["test_sources"][0]
            if part == "test_source"
            else (
                witness["compiler_discovery_review"]["dependency_bundle"]
                if part == "compiler_bundle"
                else witness[part]
            )
        )
        (base / ref["path"]).unlink()
    result = compare_verified(task, spec, {"status": "complete"}, source_base=base)
    assert result["reference"]["status"] == "unavailable"
    assert result["scope"]["rate"] is None


def test_dual_obligation_goal_does_not_allow_duplicate_test_pool_credit(archived):
    base, task, spec = archived("gson")
    binding = next(
        b
        for b in reviewed_steps(spec)[0]["ci_bindings"]
        if b["goal"] == "surefire:test" and b.get("requirement_id")
    )
    binding["requirement_ids"] = [binding["requirement_id"], binding["requirement_id"]]
    with pytest.raises(ValueError, match="duplicated"):
        verify_reference(task, spec, base)
