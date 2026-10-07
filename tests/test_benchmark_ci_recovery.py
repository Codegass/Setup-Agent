"""Tests use local synthetic responses only; no official endpoints are contacted."""
import json

import pytest

from scripts.benchmark_ci_recovery import (
    Collector, artifact_indexes, assess_case_pool, inspect_artifact, log_links,
    recover_archived_jenkins, ref, walk_local_refs, write,
)
from scripts.benchmark_test_evidence_audit import case_counts, digest


def cached_response(tmp_path, url):
    directory=tmp_path/"responses"/digest(url.encode());directory.mkdir(parents=True)
    body=directory/"body.bin";body.write_bytes(b'{"total_count":0,"artifacts":[]}')
    value={"url":url,"method":"GET","status":"available","body":ref(body,tmp_path)}
    write(directory/"receipt.json",value)
    return directory,value


@pytest.mark.parametrize("mutation",["url","method","bytes","bool_bytes","sha","body_path"])
def test_cached_response_must_bind_endpoint_path_hash_and_bytes(tmp_path,mutation):
    url="https://api.github.com/repos/org/repo/actions/runs/3/artifacts?per_page=100&page=1"
    directory,value=cached_response(tmp_path,url)
    if mutation=="url":value["url"]=url.replace("runs/3","runs/4")
    if mutation=="method":value["method"]="POST"
    if mutation=="bytes":value["body"]["bytes"]+=1
    if mutation=="bool_bytes":value["body"]["bytes"]=True
    if mutation=="sha":value["body"]["sha256"]="0"*64
    if mutation=="body_path":
        sibling=tmp_path/"other.bin";sibling.write_bytes((directory/"body.bin").read_bytes())
        value["body"]["path"]="other.bin"
    write(directory/"receipt.json",value)
    with pytest.raises(ValueError):Collector(tmp_path).get(url)


def test_valid_cached_response_reused_without_network(tmp_path):
    url="https://ci.example/job/3/testReport/api/json"
    cached_response(tmp_path,url)
    assert Collector(tmp_path).get(url)["cached"] is True


def task():
    return {"task_id":"task","repo":"org/repo","sha":"a"*40,
            "ci_identity":{"run_id":3,"run_attempt":2,"job_id":4},
            "selected_log_binding":"confirmed","artifact_uploads":[{"artifact_id":9,"job_id":4,"line":7}],
            "frozen_test_vectors":[{"counts":{"reported":2,"passed":2,"skipped":0,"failed_or_error":0}}]}


def artifact():
    return {"id":9,"name":"unit-server","size_in_bytes":80000,"expired":False,
            "workflow_run":{"id":3,"head_sha":"a"*40}}


@pytest.mark.parametrize("mutation,expected",[
    ("same","report_download_candidate"),
    ("other_commit","not_bound_to_selected_job_attempt"),
    ("other_run","not_bound_to_selected_job_attempt"),
    ("no_upload","not_bound_to_selected_job_attempt"),
    ("binary","non_report_artifact_not_downloaded"),
    ("expired","expired_report"),
    ("oversize","report_download_deferred_by_resource_limit"),
    ("archived_expired","report_already_archived"),
])
def test_only_bound_small_test_outputs_selected(mutation,expected):
    row=task();a=artifact()
    if mutation=="other_commit":a["workflow_run"]["head_sha"]="b"*40
    if mutation=="other_run":a["workflow_run"]["id"]=5
    if mutation=="no_upload":row["artifact_uploads"]=[]
    if mutation=="binary":a["name"]="maven-distributions"
    if mutation in {"expired","archived_expired"}:a["expired"]=True
    if mutation=="oversize":a["size_in_bytes"]=100001
    if mutation=="archived_expired":row["archived_artifacts"]=[{"artifact_id":9,"source":{"path":"old.zip"}}]
    assert inspect_artifact(row,a,100000)["disposition"]==expected


class MemoryCollector:
    def __init__(self,pages):self.pages=pages;self.requested=[]
    def get(self,url):self.requested.append(url);return self.pages[url]
    def json(self,response):return response.get("data")


def page(total,items,link=""):
    return {"status":"available","data":{"total_count":total,"artifacts":[{"id":i} for i in items]},"response_headers":{"link":link}}


def test_artifact_pagination_is_closed_not_just_first_page():
    url="https://api.github.com/repos/org/repo/actions/runs/3/artifacts?per_page=100&page=1"
    second=url.replace("page=1","page=2")
    pages={url:page(2,[1],f'<{second}>; rel="next"'),second:page(2,[2])}
    collector=MemoryCollector(pages)
    index,records=artifact_indexes(task(),collector,{url:pages[url]})
    assert index["pagination_complete"] is True and len(records)==2
    assert collector.requested==[second]


@pytest.mark.parametrize("kind",["missing_page","duplicate_id","changed_total"])
def test_bad_pagination_cannot_be_empty_upload_proof(kind):
    url="https://api.github.com/repos/org/repo/actions/runs/3/artifacts?per_page=100&page=1"
    second=url.replace("page=1","page=2")
    pages={url:page(2,[1],f'<{second}>; rel="next"'),second:page(2,[2])}
    if kind=="missing_page":pages[url]["response_headers"]["link"]=""
    if kind=="duplicate_id":pages[second]=page(2,[1])
    if kind=="changed_total":pages[second]=page(3,[2])
    index,_=artifact_indexes(task(),MemoryCollector(pages),{})
    assert index["pagination_complete"] is False and index["status"]=="unavailable"


def records():
    return [{"identity":{"class_name":"C","name":"dynamic"},"outcome":"passed","source_pointer":str(i)} for i in range(2)]


def test_complete_case_occurrences_can_have_ambiguous_identity():
    cases=records()
    result=assess_case_pool(task(),cases,kind="existing_archive_reinterpretation",report_ref={},binding=True,native_counts=case_counts(cases))
    assert result["complete_case_records_available"] is True
    assert result["identity_inventory"]["record_occurrences"]==2
    assert result["identity_inventory"]["collision_key_count"]==1
    assert result["cross_run_identity_equivalence"]=="not_evaluated"


def test_equal_zip_totals_alone_never_close_scope():
    result=assess_case_pool(task(),records(),kind="new_download",report_ref={},binding=True)
    assert result["frozen_vector_match"] is True
    assert result["complete_case_records_available"] is False


def test_source_binding_failure_overrides_count_parity():
    cases=records()
    result=assess_case_pool(task(),cases,kind="existing_archive_reinterpretation",report_ref={},binding=False,native_counts=case_counts(cases))
    assert result["complete_case_records_available"] is False


def test_log_sources_keep_selected_job_and_explicit_upload_path():
    text="\n".join(["2026-09-22T00:00:00Z ##[group]Run actions/upload-artifact@sha",
        "2026-09-22T00:00:00Z   name: unit-server", "2026-09-22T00:00:00Z   path: target/unit",
        "2026-09-22T00:00:00Z   TOKEN: should-not-be-copied", "2026-09-22T00:00:00Z ##[endgroup]",
        "2026-09-22T00:00:00Z Artifact ID is 9", "2026-09-22T00:00:00Z https://develocity.apache.org/s/abcd1234"])
    observed=log_links([{"job_id":4,"source":{"path":"old.log"},"text":text}])
    assert observed["artifact_uploads"][0]["job_id"]==4
    assert observed["artifact_uploads"][0]["upload_action"]["parameters"][1]["text"]=="path: target/unit"
    assert "should-not-be-copied" not in json.dumps(observed)
    assert observed["scan_links"][0]["line"]==7


def test_declared_report_hash_mismatch_cannot_fall_back_to_unchecked_string(tmp_path):
    path=tmp_path/"test-report.json";path.write_text("{}")
    with pytest.raises(ValueError):walk_local_refs({"evidence":{"file":path.name,"sha256":"0"*64}},tmp_path)


def test_archived_report_recovers_even_if_current_endpoint_was_deleted(tmp_path):
    base=tmp_path/"old";base.mkdir();out=tmp_path/"new"
    row=task();row.update(provider="apache_jenkins",official_ci_url="https://ci.example/job/3/",archived_report_candidates=[])
    report={"_class":"hudson.tasks.junit.TestResult","failCount":0,"passCount":2,"skipCount":0,
        "suites":[{"name":"C","cases":[{"className":"C","name":"dynamic","status":"PASSED","skipped":False} for _ in range(2)]}]}
    write(base/"test-report.json",report);write(base/"build.json",{"url":row["official_ci_url"],"actions":[{"lastBuiltRevision":{"SHA1":row["sha"]}}]})
    r=ref(base/"test-report.json",base)
    row["archived_report_candidates"]=[r]
    row["ci_identity"].update(metadata="build.json",test_report=r)
    row["jenkins_indexes"]={"test_report":{"status":"not_found","http_status":404}}
    result=recover_archived_jenkins(row,base,out)[0]
    assert result["complete_case_records_available"] is True
    assert result["discovery"]=="existing_archive_reinterpretation"
    assert result["identity_inventory"]["collision_key_count"]==1
