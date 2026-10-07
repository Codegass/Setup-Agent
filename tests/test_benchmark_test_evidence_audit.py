"""Offline evidence audit guards; no provider, CI, build or original dataset writes."""
import json
from zipfile import ZipFile

import pytest

from scripts.benchmark_test_evidence_audit import (
    audit_task,
    capture_request_ref,
    case_counts,
    digest,
    identity_inventory,
    jenkins_rows,
    normalize_counts,
    scan_rows,
    zip_xml,
)


def test_reported_and_assessed_are_distinct():
    counts = normalize_counts({"reported": 557, "skipped": 2, "passed": 555, "failed_or_error": 0})
    assert counts == {"reported_count": 557, "skipped_count": 2, "assessed_count": 555,
        "passed_count": 555, "failed_count": None, "error_count": None, "failed_or_error_count": 0}


@pytest.mark.parametrize("patch", [
    {"reported": True}, {"skipped": -1}, {"passed": False},
    {"failures": -1}, {"errors": True}, {"failed_or_error": True},
    {"failures": 1, "errors": 0, "failed_or_error": 0},
    {"failures": 2, "errors": None, "failed_or_error": 1},
    {"reported": 3}, {"errors": None, "failed_or_error": None},
])
def test_count_conflicts_are_rejected(patch):
    counts = {"reported": 2, "skipped": 1, "passed": 1, "failures": 0, "errors": 0, "failed_or_error": 0}
    with pytest.raises(ValueError):
        normalize_counts(counts | patch)


def test_consistent_split_and_combined_counts_preserve_native_split():
    result = normalize_counts({"reported": 5, "skipped": 1, "passed": 1,
                               "failures": 1, "errors": 2, "failed_or_error": 3})
    assert result["failed_count"] == 1
    assert result["error_count"] == 2
    assert result["failed_or_error_count"] is None


def jenkins_suite(node="12", state="PASSED"):
    return {"name": "pkg.Test", "nodeId": node, "enclosingBlockNames": ["Java 17", "Test"],
            "cases": [{"className": "pkg.Test", "name": "parameterized", "status": state, "skipped": state == "SKIPPED"}]}


def test_duplicates_are_complete_occurrences_not_invented_ordinal_ids():
    rows = jenkins_rows({"suites": [jenkins_suite(), jenkins_suite(state="SKIPPED")]})
    inv = identity_inventory(rows)
    assert case_counts(rows)["reported_count"] == 2
    assert case_counts(rows)["assessed_count"] == 1
    assert inv["distinct_observed_identity_keys"] == 1
    assert inv["collisions"][0]["outcomes"] == {"passed": 1, "skipped": 1}
    assert inv["duplicates_removed"] is False and inv["ordinal_is_identity"] is False
    assert len(set(inv["collisions"][0]["source_occurrences"])) == 2
    assert identity_inventory(list(reversed(rows)))["identity_outcome_multiset_sha256"] == inv["identity_outcome_multiset_sha256"]


def test_explicit_publisher_context_distinguishes_within_run_occurrences():
    rows = jenkins_rows({"suites": [jenkins_suite("12"), jenkins_suite("13")]})
    assert identity_inventory(rows)["collision_key_count"] == 0
    assert len(jenkins_rows({"suites": [jenkins_suite("12"), jenkins_suite("13")]}, publisher="12")) == 1
    assert not jenkins_rows({"suites": [jenkins_suite()]}, blocks=["Test"])


def scan():
    return {"status": "COMPLETED", "data": {
        "workUnits": [{"name": ":test", "suites": [0]}],
        "suites": [{"name": "pkg.Test", "workUnitName": ":test", "parentWorkUnit": 0, "tests": [0, 1]}],
        "tests": [{"name": "parameterized", "displayName": "parameterized", "testId": "same-label",
                   "outcome": value, "parentSuite": 0, "suiteName": "pkg.Test", "workUnitName": ":test"} for value in [0, 1]],
    }}


def test_scan_duplicate_labels_and_native_ids_preserved():
    rows = scan_rows(scan())
    inv = identity_inventory(rows)
    assert inv["record_occurrences"] == 2
    assert inv["collision_key_count"] == 1
    assert inv["native_record_id_collisions"] == {"same-label": 2}


@pytest.mark.parametrize("mutation", ["duplicate_member", "missing_member", "wrong_parent", "bool_outcome"])
def test_scan_scope_parent_closure_must_hold(mutation):
    value = scan()
    if mutation == "duplicate_member": value["data"]["suites"][0]["tests"] = [0, 0, 1]
    if mutation == "missing_member": value["data"]["suites"][0]["tests"] = [0]
    if mutation == "wrong_parent": value["data"]["tests"][0]["suiteName"] = "wrong"
    if mutation == "bool_outcome": value["data"]["tests"][0]["outcome"] = False
    with pytest.raises(ValueError): scan_rows(value)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"file": path.name, "sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}


def test_request_url_and_response_bytes_both_required(tmp_path):
    response = write_json(tmp_path / "response.json", {"suites": []})
    record = {"url": "https://ci.example/42/testReport/api/json", "status": 200,
              "sha256": response["sha256"], "bytes": response["bytes"]}
    write_json(tmp_path / "capture.json", [record])
    assert capture_request_ref(tmp_path, "capture.json", record["url"], response)[1]["sha256"] == response["sha256"]
    write_json(tmp_path / "capture.json", [record | {"sha256": "0" * 64}])
    with pytest.raises(ValueError): capture_request_ref(tmp_path, "capture.json", record["url"], response)
    write_json(tmp_path / "capture.json", [record, record])
    with pytest.raises(ValueError): capture_request_ref(tmp_path, "capture.json", record["url"], response)


def test_malformed_report_and_suite_conflict_not_silently_complete(tmp_path):
    path = tmp_path / "reports.zip"
    with ZipFile(path, "w") as z:
        z.writestr("target/surefire-reports/TEST-broken.xml", "<testsuite")
        z.writestr("TEST-other.xml", '<testsuite name="X" tests="2" failures="0" errors="0" skipped="0"><testcase classname="X" name="x"/></testsuite>')
    rows, reports, problems = zip_xml(str(path))
    assert len(rows) == 1 and len(reports) == 1
    assert any("malformed" in p for p in problems)
    assert any("suite/case count conflict" in p for p in problems)


def artifact_fixture(tmp_path, *, wrong_artifact_sha=False, xml=True):
    base = tmp_path / "dataset"; base.mkdir()
    root = base / "raw/actions/org/repo/runs/3/attempt-1"; root.mkdir(parents=True)
    with ZipFile(root / "logs.zip", "w") as z:
        z.writestr("0_job.txt", "Complete job name: job\nArtifact ID is 9\n")
    with ZipFile(root / "artifact.zip", "w") as z:
        z.writestr("TEST-X.xml" if xml else "notes.txt", '<testsuite name="X" tests="1" failures="0" errors="0" skipped="0"><testcase classname="X" name="x"/></testsuite>' if xml else "not test evidence")
    def ref(name):
        p=root/name
        return {"file":str(p.relative_to(base)), "sha256":digest(p.read_bytes()), "bytes":p.stat().st_size}
    commit = "a"*40
    capture = {"id": 3, "run_attempt": 1, "head_sha": commit,
        "jobs": [{"id": 4, "name": "job", "head_sha": commit, "run_attempt": 1}],
        "logs": ref("logs.zip"), "artifacts": [{"id": 9, "name": "reports", "download": ref("artifact.zip"),
            "workflow_run": {"id": 3, "head_sha": "b"*40 if wrong_artifact_sha else commit}}]}
    write_json(root/"capture.json", capture)
    write_json(base/"review.json", {"rows": [{"repo": "org/repo"}]})
    task={"task_id":"task", "repo":"org/repo", "sha":commit, "scope":"one invocation",
        "review_source":{"file":"review.json", "row_index":0}, "evidence_level":"complete_suite_counts",
        "ci_identity":{"run_id":3,"run_attempt":1,"job_id":4},
        "official_ci_url":"https://github.com/org/repo/actions/runs/3/job/4",
        "reported_test_results_by_invocation":[{"scope":"test", "counts":{"reported":1,"passed":1,"skipped":0,"failed_or_error":0}}]}
    return base, task


def test_equal_artifact_count_and_upload_binding_do_not_promote_identity(tmp_path):
    base, task = artifact_fixture(tmp_path)
    result=audit_task(base,tmp_path/"out",task,True)
    artifact=result["artifact_recovery"][0]
    assert artifact["job_attempt_bound"] is True and artifact["counts_equal_selected_vector"] is True
    assert artifact["promotable"] is False
    assert result["identity_audit"]["complete_case_records_available"] is False
    assert result["status"] == "partial"


def test_uploaded_artifact_with_other_checkout_sha_is_unbound(tmp_path):
    base, task = artifact_fixture(tmp_path, wrong_artifact_sha=True)
    result=audit_task(base,tmp_path/"out",task,True)
    assert result["artifact_recovery"][0]["job_attempt_bound"] is False


def test_no_xml_does_not_mean_zero_tests(tmp_path):
    base, task = artifact_fixture(tmp_path, xml=False)
    result=audit_task(base,tmp_path/"out",task,True)
    artifact=result["artifact_recovery"][0]
    assert artifact["counts"] is None
    assert artifact["counts_equal_selected_vector"] is None
    assert artifact["parser_status"] == "unavailable"
