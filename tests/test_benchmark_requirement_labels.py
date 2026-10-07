"""Offline label audits never promote command text to complete requirements."""
import json
from pathlib import Path
import zipfile

import pytest

from scripts.benchmark_requirement_labels import (
    audit_task, candidate_labels, canonical_digest, command_goals, file_ref,
    goal_label, native_events, old_overlap, selected_logs, verified_descriptors, sha,
)


def test_maven_verify_does_not_prove_integration_or_quality_checks():
    parsed = command_goals("mvn -Dsample.command=install -f pom.xml clean verify", "maven")
    assert parsed["goals"] == ["clean", "verify"]
    labels = candidate_labels(parsed["goals"], "maven")
    assert ("test", "unknown") in labels
    assert not any(kind in {"quality_check", "install"} for kind, subtype in labels)
    assert ("test", "integration") not in labels


def test_gradle_excluded_documentation_not_requested_by_text():
    parsed = command_goals("./gradlew -Pexample=build :module:check -x javadoc", "gradle")
    assert parsed["goals"] == [":module:check"]
    assert parsed["excluded_tasks"] == ["javadoc"]
    assert candidate_labels(parsed["goals"], "gradle") == [("test", "unknown")]


def test_report_generation_is_not_a_quality_threshold():
    assert goal_label("jacoco:0.8.13:report", "maven") is None
    assert goal_label("jacoco:0.8.13:check", "maven") == ("quality_check", "jacoco:check")
    assert goal_label("checkstyle:3.6.0:checkstyle", "maven") is None
    assert goal_label("checkstyle:3.6.0:check", "maven") == ("quality_check", "checkstyle:check")


def test_nested_and_skipped_native_goals_remain_explicit():
    text = """[INFO] >>> javadoc:3.11.2:javadoc (default-cli) > generate-sources @ tiny >>>
[INFO] --- compiler:3.15.0:compile (default-compile) @ tiny ---
[INFO] Compiling 1 source file
[INFO] <<< javadoc:3.11.2:javadoc (default-cli) < generate-sources @ tiny <<<
[INFO] --- javadoc:3.11.2:javadoc (default-cli) @ tiny ---
[INFO] --- jacoco:0.8.13:check (coverage) @ tiny ---
[INFO] Skipping JaCoCo execution due to missing execution data file.
"""
    events = native_events(text)
    assert events[0]["nested"] is True
    assert events[1]["nested"] is False
    assert events[-1]["state"] == "skipped"
    assert not any("passed" in event for event in events)


def test_selected_cell_cannot_borrow_another_jobs_install_goal(tmp_path):
    archive = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("selected.txt", "[INFO] --- compiler:3:compile (compile) @ tiny ---\n")
        out.writestr("other.txt", "[INFO] --- install:3:install (install) @ tiny ---\n")
    cells = tmp_path / "actions-cell-evidence"
    cells.mkdir()
    (cells / "example__tiny.json").write_text(json.dumps({"runs": [{"log_archive": {"file": "logs.zip"}, "cells": [
        {"job_url": "https://example.invalid/job/1", "log_selection": {"job_binding": "exact_job_name", "members": ["selected.txt"]}},
        {"job_url": "https://example.invalid/job/2", "log_selection": {"job_binding": "exact_job_name", "members": ["other.txt"]}},
    ]}]}))
    logs, errors = selected_logs(tmp_path, {"repo": "example/tiny", "official_ci_url": "https://example.invalid/job/1"}, {})
    assert not errors and len(logs) == 1
    assert [e["classification"] for e in native_events(logs[0]["text"])] == [("compile", "production")]


def test_selected_log_hash_mismatch_produces_no_observations(tmp_path):
    (tmp_path / "console.log").write_text("[INFO] --- jar:3:jar (package) @ tiny ---\n")
    row = {"ci": {"log": {"file": "console.log", "sha256": "0" * 64}}}
    logs, errors = selected_logs(tmp_path, {"repo": "example/tiny", "official_ci_url": "https://example.invalid/job/1"}, row)
    assert logs == [] and "Source hash differs" in errors[0]


def test_jenkins_timestamp_transport_does_not_hide_native_requirements():
    events = native_events("01:01:39 [2026-09-12T01:01:39.702Z] [INFO] --- maven-compiler-plugin:3.6.1:compile (default-compile) @ driver ---\n")
    assert events[0]["classification"] == ("compile", "production")
    assert events[0]["line"] == 1


@pytest.mark.parametrize("mismatch", ["revision", "bytes", "default_goal"])
def test_descriptor_claims_require_matching_commit_and_raw_default_goal(tmp_path, mismatch):
    raw = b"<project><build><defaultGoal>clean verify</defaultGoal></build></project>"
    (tmp_path / "pom.xml").write_bytes(raw)
    task = {"task_id": "one", "repo": "example/tiny", "sha": "a" * 40}
    desc = {"path": "pom.xml", "sha256": sha(raw), "bytes": len(raw), "default_goal": "clean verify"}
    source_task = {**task, "descriptors": [desc]}
    source = {"audit_base": str(tmp_path), "tasks": [source_task]}
    assert verified_descriptors(source, task)["descriptors"][0]["default_goal"] == "clean verify"
    if mismatch == "revision":
        source_task["sha"] = "b" * 40
    elif mismatch == "bytes":
        (tmp_path / "pom.xml").write_bytes(raw + b"\n")
        from scripts.benchmark_requirement_labels import file_ref
        file_ref.cache_clear()
    else:
        desc["default_goal"] = "install"
    with pytest.raises(ValueError):
        verified_descriptors(source, task)


def frozen_prior_task(base):
    task = {"repo": "example/tiny", "sha": "a" * 40,
            "steps": [{"argv": ["mvn", "verify"], "cwd": ".", "java_major": 17, "maven_version": "3.9.11"}]}
    spec = {"task_sha256": canonical_digest(task),
            "ci_alignment": {"selected_url": "https://example.invalid/job/1"},
            "annotation_completeness": {"status": "complete"}, "requirements": []}
    for name, content in [("task.json", task), ("requirements.json", spec)]:
        (base / name).write_text(json.dumps(content))
    task_ref = file_ref(str(base / "task.json"))
    spec_ref = file_ref(str(base / "requirements.json"))
    manifest = {"projects": [{"id": "tiny", "task": {**task_ref, "path": "task.json"},
                "task_sha256": canonical_digest(task),
                "requirements": {"path": "requirements.json", "sha256": canonical_digest(spec),
                                 "file_sha256": spec_ref["sha256"]}}]}
    (base / "manifest.json").write_text(json.dumps(manifest))
    return {"repo": task["repo"], "sha": task["sha"], "official_ci_url": spec["ci_alignment"]["selected_url"],
            "environment_and_commands": {"steps": [{"command": "mvn verify", "jdk": "17.0.12"}], "build_tool_versions": ["3.9.11"]}}


@pytest.mark.parametrize("change", ["sha", "ci", "jdk", "step_env", "global_config"])
def test_equal_command_cannot_transfer_readiness_across_task_context(tmp_path, change):
    task = frozen_prior_task(tmp_path)
    assert old_overlap(task, [], tmp_path)[0]["readiness_transfer"] is True
    if change == "sha":
        task["sha"] = "b" * 40
    elif change == "ci":
        task["official_ci_url"] = "https://example.invalid/job/2"
    elif change == "jdk":
        task["environment_and_commands"]["steps"][0]["jdk"] = "21.0.5"
    elif change == "step_env":
        task["environment_and_commands"]["steps"][0]["env"] = {"MAVEN_ARGS": "-DskipTests"}
    else:
        task["environment_and_commands"]["active_profiles"] = ["special"]
    assert old_overlap(task, [], tmp_path)[0]["readiness_transfer"] is False


def test_modified_prior_requirements_cannot_transfer_readiness(tmp_path):
    task = frozen_prior_task(tmp_path)
    (tmp_path / "requirements.json").write_text('{"annotation_completeness":{"status":"complete"}}')
    file_ref.cache_clear()
    with pytest.raises(ValueError, match="Source hash differs"):
        old_overlap(task, [], tmp_path)


def test_ci_job_extra_goal_is_not_a_frozen_task_requirement(tmp_path):
    (tmp_path / "job.log").write_text("[INFO] --- site:3.20.0:site (default-site) @ tiny ---\n")
    row = {"repo": "example/tiny", "sha": "a" * 40,
           "ci": {"log": {"file": "job.log", "sha256": sha((tmp_path / "job.log").read_bytes())}}}
    (tmp_path / "review.json").write_text(json.dumps({"rows": [row]}))
    task = {"task_id": "one", "repo": row["repo"], "sha": row["sha"], "tool": "maven",
            "official_ci_url": "https://example.invalid/job/1", "ci_identity": {},
            "review_source": {"file": "review.json", "row_index": 0},
            "environment_and_commands": {"command": "mvn test",
                                         "default_goals": [{"path": "pom.xml", "text": "<defaultGoal>verify javadoc:javadoc</defaultGoal>"}]}}
    result = audit_task(tmp_path, task)
    documentation = [x for x in result["labels"] if x["kind"] == "documentation"]
    assert len(documentation) == 1 and documentation[0]["subtype"] == "site:site"
    assert documentation[0]["status"] == "observed"
    assert documentation[0]["requirement_membership"] == "unresolved"
    assert documentation[0]["observations"][0]["requirement_membership"].startswith("unresolved")
    assert documentation[0]["candidates"] == []  # Explicit test never inherits defaultGoal.
    assert result["requirements_readiness"] == "review_required"
