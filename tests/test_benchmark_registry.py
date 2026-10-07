"""Synthetic integration checks: no archived datasets or network are required."""
from copy import deepcopy
import json

import pytest

from scripts.assemble_benchmark_registry import (
    assemble,
    band,
    file_ref,
    locate,
    resolve_locator,
    validate_locators,
)


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


@pytest.fixture
def inputs():
    old_ref = {"repository_id": 1, "repo": "old/library", "canonical_sha": OLD_SHA,
               "canonical_task_id": "old-task", "canonical_ci_url": "https://ci.example/old/1",
               "canonical_evidence_level": "complete_case_results", "canonical_tool": "maven"}
    return {
        "old_dataset": {"projects": [old_ref], "reference_tasks": [
            {"task_id": "old-task", "repo": "old/library", "sha": OLD_SHA}]},
        "old_ledger": {"rows": [
            {"repository_id": 1, "repo": "old/library", "organization": "old", "stars": 300,
             "state": "static_qualified"},
            {"repository_id": 2, "repo": "old/pending", "organization": "old", "stars": 250,
             "state": "needs_review"}]},
        "cohort": {"candidates": [
            {"repository_id": rid, "repo": repo,
             "population": {"status": "eligible", "cutoff": "2025-09-16"}}
            for rid, repo in [(1, "old/library"), (2, "old/pending")]]},
        "rescreen": {"projects": [{"repo": "old/library", "ci_count_audit": "confirmed"}]},
        "acquisition": {
            "size_policy": {"q1": 100, "q3": 1000, "method": "frozen_original_quartiles"},
            "projects": [{"repo": "old/library", "sha": OLD_SHA, "canonical_task_id": "old-task",
                          "source_after": "java_content_verified", "java_files": 3,
                          "java_physical_lines": 100, "case_records_after": True,
                          "within_run_labels_unambiguous": True, "requirements_readiness": "complete"}]},
        "eclipse": {"repositories": [
            {"repository_id": 3, "repo": "eclipse/example", "new_candidate": True,
             "stars": 501, "project_ids": ["tools.example"]}]},
        "fasterxml": {"rows": [
            {"repository_id": 4, "repo": "FasterXML/example", "status": "metadata_candidate",
             "stars": 701, "family": "Jackson"}]},
        "faster_tasks": {"tasks": [{"repo": "FasterXML/example", "sha": NEW_SHA,
                                     "run_id": 41, "run_attempt": 2, "job_id": 43,
                                     "official_ci_url": "https://ci.example/new/41/43",
                                     "selection_status": "reviewed"}]},
        "faster_evidence": {"tasks": []},
        "eclipse_evidence": {"rows": [
            {"repo": "eclipse/example", "artifacts": [
                {"parsed_report": {"observed_counts": {"reported_count": 7}}},
                {"parsed_report": {"observed_counts": {"reported_count": 4}}}]}]},
        "new_source": {"rows": [
            {"repo": "FasterXML/example", "sha": NEW_SHA, "status": "java_content_verified",
             "scan": {"measured_size": {"java_files": 5, "java_physical_lines": 100}}}]},
        "applicability": {"repositories": []},
    }


@pytest.fixture
def decision():
    checks = ("population", "task_applicability", "official_ci_binding", "successful_required_scope",
              "complete_build_test_counts", "runtime_disclosed", "source_and_license", "selection_disclosed")
    return {
        "repository_id": 4, "repo": "FasterXML/example", "decision": "static_qualified",
        "checks": dict.fromkeys(checks, "passed"),
        "reason": "Complete native test totals and the original reference contract are reviewed.",
        "evidence_locators": [locate("faster_evidence", "/tasks/0")],
        "reference_task": {
            "task_id": "new-task", "repository_id": 4, "repo": "FasterXML/example", "sha": NEW_SHA,
            "evidence_level": "complete_suite_counts",
            "ci": {"run_id": 41, "run_attempt": 2, "job_id": 43},
            "build": {"status": "success"},
            "tests": {"reported_count": 8, "passed_count": 6, "failed_count": 0,
                      "error_count": 0, "skipped_count": 2, "assessed_count": 6}},
    }


def admitted(data, decision):
    return assemble(data, {"decisions": [decision]})


def candidate(registry, repo):
    return next(row for row in registry["candidates"] if row["repo"] == repo)


def test_registration_preserves_candidate_reference_and_campaign_boundaries(inputs):
    result = assemble(inputs)
    assert result["summary"]["registered_candidate_records"] == 4
    assert result["summary"]["historical_reference_projects"] == 1
    assert result["summary"]["new_static_reference_projects"] == 0
    assert result["summary"]["reference_projects"] == 1
    assert result["summary"]["reference_tasks"] == 1
    assert candidate(result, "old/pending")["canonical_task_id"] is None
    assert candidate(result, "old/library")["requirements_readiness"] == "complete"
    assert all(row["campaign_ready"] is False for row in result["candidates"] + result["reference_tasks"])
    assert result["reference_tasks"][0]["evidence_root_source"] == "original"
    assert result["reference_tasks"][0]["task"] == locate("old_dataset", "/reference_tasks/0")


def test_count_only_reference_admission_does_not_require_v2_replay_or_testcase_rows(inputs, decision):
    original = deepcopy(inputs)
    result = admitted(inputs, decision)
    row = candidate(result, "FasterXML/example")
    assert row["reference_status"] == "static_qualified"
    assert row["canonical_task_id"] == "new-task"
    assert row["requirements_readiness"] == "review_required"
    assert row["reference_replay"] == "not_verified"
    assert row["campaign_ready"] is False
    assert row["complete_case_records"] is False
    assert row["within_run_labels_unambiguous"] is False
    assert result["summary"]["reference_projects"] == 2
    assert result["summary"]["canonical_complete_case_record_projects"] == 1
    assert inputs == original


def test_partial_report_pool_is_not_promoted_to_whole_task_evidence(inputs):
    result = assemble(inputs)
    row = candidate(result, "eclipse/example")
    assert row["partial_case_records"]["reported"] == 11
    assert row["canonical_task_id"] is None
    assert row["reference_status"] == "needs_review"
    assert row["complete_case_records"] is False
    assert result["summary"]["canonical_complete_case_record_projects"] == 1


@pytest.mark.parametrize("duplicate", ["repository_id", "repo", "task_id"])
def test_duplicate_identity_is_rejected(inputs, duplicate):
    if duplicate == "task_id":
        inputs["old_dataset"]["reference_tasks"].append(deepcopy(inputs["old_dataset"]["reference_tasks"][0]))
    elif duplicate == "repository_id":
        inputs["fasterxml"]["rows"][0]["repository_id"] = 1
    else:
        inputs["eclipse"]["repositories"][0]["repo"] = "old/pending"
    with pytest.raises(ValueError, match="Duplicate identity"):
        assemble(inputs)


@pytest.mark.parametrize("field,value", [("sha", "c" * 40), ("run_attempt", 1), ("job_id", 99)])
def test_admission_rejects_another_revision_or_ci_invocation(inputs, decision, field, value):
    task = decision["reference_task"]
    (task if field == "sha" else task["ci"])[field] = value
    with pytest.raises(ValueError, match="revision|run/attempt/job"):
        admitted(inputs, decision)


def test_source_size_must_describe_the_reviewed_ci_revision(inputs):
    inputs["new_source"]["rows"][0]["sha"] = "c" * 40
    with pytest.raises(ValueError, match="Source size and candidate CI SHA disagree"):
        assemble(inputs)


@pytest.mark.parametrize("count,expected", [(100, "small"), (101, "medium"), (1000, "medium"), (1001, "large")])
def test_frozen_size_bands_apply_identically_across_waves(inputs, count, expected):
    inputs["acquisition"]["projects"][0]["java_physical_lines"] = count
    inputs["new_source"]["rows"][0]["scan"]["measured_size"]["java_physical_lines"] = count
    result = assemble(inputs)
    assert candidate(result, "old/library")["descriptive_size_band"] == expected
    assert candidate(result, "FasterXML/example")["descriptive_size_band"] == expected
    assert result["policy"]["size"]["q1"] == 100
    assert result["policy"]["size"]["q3"] == 1000


def test_unknown_source_size_is_not_a_small_project():
    assert band({"status": "not_measured", "java_physical_lines": 0}, {"q1": 100, "q3": 1000}) == "size_unknown"


@pytest.mark.parametrize("field", ["run_id", "run_attempt", "job_id"])
@pytest.mark.parametrize("value", [None, 0, -1, True])
def test_missing_or_invalid_ci_identity_cannot_pass_by_matching_on_both_sides(inputs, decision, field, value):
    inputs["faster_tasks"]["tasks"][0][field] = value
    decision["reference_task"]["ci"][field] = value
    with pytest.raises(ValueError):
        admitted(inputs, decision)


def test_all_skipped_reference_is_not_a_successful_test_baseline(inputs, decision):
    decision["reference_task"]["tests"].update(passed_count=0, assessed_count=0, skipped_count=8)
    with pytest.raises(ValueError):
        admitted(inputs, decision)


@pytest.mark.parametrize("scope_review", [None, "pending", "failed"])
def test_complete_case_capability_requires_complete_scope_review(inputs, decision, scope_review):
    decision["reference_task"]["evidence_level"] = "complete_case_results"
    if scope_review is not None:
        decision["complete_case_scope_review"] = scope_review
    with pytest.raises(ValueError):
        admitted(inputs, decision)


def test_count_only_reference_cannot_claim_a_complete_case_scope(inputs, decision):
    decision["complete_case_scope_review"] = "passed"
    with pytest.raises(ValueError, match="Complete testcase capability"):
        admitted(inputs, decision)


def test_reviewed_complete_cases_do_not_imply_collision_free_or_cross_run_identity(inputs, decision):
    decision["reference_task"]["evidence_level"] = "complete_case_results"
    decision["complete_case_scope_review"] = "passed"
    result = admitted(inputs, decision)
    row = candidate(result, "FasterXML/example")
    assert row["complete_case_records"] is True
    assert row["within_run_labels_unambiguous"] is False
    assert row["cross_run_identity"] == "not_evaluated"
    assert row["campaign_ready"] is False


@pytest.fixture
def locator_fixture(tmp_path):
    source = tmp_path / "frozen"
    source.mkdir()
    path = source / "dataset.json"
    path.write_text(json.dumps({"reference_tasks": [{"repo": "old/library", "evidence": "raw/log.txt"}]}))
    integrated = tmp_path / "integrated"
    integrated.mkdir()
    # A plausible same-named file in the new directory must never shadow the source.
    (integrated / "dataset.json").write_text('{"reference_tasks": [{"repo": "wrong/root"}]}')
    registry = {"sources": {"original": {"root": "../frozen"}},
                "documents": {"old_dataset": {"source": "original", "path": "dataset.json", **file_ref(path)}}}
    return registry, integrated / "registry.json", source


def test_locator_uses_declared_legacy_source_root(locator_fixture):
    registry, path, source = locator_fixture
    row, root = resolve_locator(registry, path, locate("old_dataset", "/reference_tasks/0"))
    assert root == source
    assert row == {"repo": "old/library", "evidence": "raw/log.txt"}
    assert root / row["evidence"] == source / "raw/log.txt"


def test_locator_rejects_changed_source_document(locator_fixture):
    registry, path, source = locator_fixture
    (source / "dataset.json").write_text('{"reference_tasks": []}')
    with pytest.raises(ValueError, match="hash|length|changed"):
        resolve_locator(registry, path, locate("old_dataset", "/reference_tasks/0"))


@pytest.mark.parametrize("document_path", ["../integrated/dataset.json", "/tmp/dataset.json", "raw/../../dataset.json"])
def test_locator_rejects_document_path_traversal(locator_fixture, document_path):
    registry, path, _ = locator_fixture
    registry["documents"]["old_dataset"]["path"] = document_path
    with pytest.raises(ValueError):
        resolve_locator(registry, path, locate("old_dataset", "/reference_tasks/0"))


def test_locator_rejects_symlink_escape_even_when_hash_matches(locator_fixture, tmp_path):
    registry, path, source = locator_fixture
    outside = tmp_path / "outside.json"
    outside.write_text('{"reference_tasks": []}')
    (source / "redirect.json").symlink_to(outside)
    registry["documents"]["old_dataset"].update(path="redirect.json", **file_ref(outside))
    with pytest.raises(ValueError):
        resolve_locator(registry, path, locate("old_dataset", "/reference_tasks"))


@pytest.mark.parametrize("pointer", ["reference_tasks/0", "/reference_tasks/-1", "/reference_tasks/00"])
def test_locator_rejects_ambiguous_or_malformed_array_pointer(locator_fixture, pointer):
    registry, path, _ = locator_fixture
    with pytest.raises(ValueError):
        resolve_locator(registry, path, locate("old_dataset", pointer))


@pytest.fixture
def reviewed_admission(tmp_path, decision):
    source = tmp_path / "archive"
    source.mkdir()
    raw = source / "raw.log"
    raw.write_text("BUILD SUCCESS\nTests run: 8, Failures: 0, Errors: 0, Skipped: 2\n")
    task = decision["reference_task"]
    task["commands"] = ["mvn verify"]
    task["evidence"] = {"native": {"path": "raw.log", **file_ref(raw)}}
    review = {"recommendation": "static_qualified",
              **{key: deepcopy(task[key]) for key in ("repo", "repository_id", "sha", "ci")}}
    task_file = source / "tasks.json"
    task_file.write_text(json.dumps({"tasks": [task]}))
    review_file = source / "reviews.json"
    review_file.write_text(json.dumps({"rows": [review]}))
    integrated = tmp_path / "integrated"
    integrated.mkdir()
    registry = {
        "sources": {"expansion": {"root": "../archive", "read_only": True}},
        "documents": {
            "reviewed_task": {"source": "expansion", "path": "tasks.json", **file_ref(task_file)},
            "reviewed_decision": {"source": "expansion", "path": "reviews.json", **file_ref(review_file)},
        },
    }
    decision["evidence_locators"] = [locate("reviewed_task", "/tasks/0"), locate("reviewed_decision", "/rows/0")]
    return registry, integrated / "registry.json", {"decisions": [decision]}, source


def test_admission_locator_validation_checks_review_and_nested_raw_bytes(reviewed_admission):
    registry, path, admissions, _ = reviewed_admission
    result = validate_locators(registry, path, admissions)
    assert result["resolved_locators"] == 2
    assert result["verified_documents"] == 2
    assert result["verified_admission_raw_files"] == 1


@pytest.mark.parametrize("alteration", ["counts", "commands"])
def test_admission_must_equal_entire_archived_task_not_only_identity(reviewed_admission, alteration):
    registry, path, admissions, _ = reviewed_admission
    task = admissions["decisions"][0]["reference_task"]
    if alteration == "counts":
        task["tests"].update(reported_count=999, assessed_count=999, passed_count=999, skipped_count=0)
    else:
        task["commands"] = ["mvn -DskipTests verify"]
    with pytest.raises(ValueError, match="differs from the archived reviewed task"):
        validate_locators(registry, path, admissions)


@pytest.mark.parametrize("field,value", [
    ("recommendation", "needs_review"), ("repo", "FasterXML/other"), ("repository_id", 99),
    ("sha", "c" * 40), ("run_id", 99), ("run_attempt", 1), ("job_id", 99),
])
def test_admission_requires_matching_qualified_review(reviewed_admission, field, value):
    registry, path, admissions, source = reviewed_admission
    review_file = source / "reviews.json"
    rows = json.loads(review_file.read_text())
    row = rows["rows"][0]
    (row["ci"] if field in ("run_id", "run_attempt", "job_id") else row)[field] = value
    review_file.write_text(json.dumps(rows))
    # The document is intact and correctly pinned: its semantic identity is wrong.
    registry["documents"]["reviewed_decision"].update(file_ref(review_file))
    with pytest.raises(ValueError, match="matching qualified review"):
        validate_locators(registry, path, admissions)


def test_nested_raw_evidence_hash_mismatch_blocks_admission(reviewed_admission):
    registry, path, admissions, source = reviewed_admission
    (source / "raw.log").write_text("BUILD FAILURE\n")
    with pytest.raises(ValueError, match="Raw evidence hash/length changed"):
        validate_locators(registry, path, admissions)


def test_admission_raw_evidence_cannot_come_from_mutable_integration_root(reviewed_admission):
    registry, path, admissions, _ = reviewed_admission
    registry["sources"]["expansion"]["read_only"] = False
    with pytest.raises(ValueError, match="outside the sealed sources"):
        validate_locators(registry, path, admissions)
