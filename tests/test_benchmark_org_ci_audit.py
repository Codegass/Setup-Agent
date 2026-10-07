import json
from pathlib import Path

import pytest

from scripts.benchmark_org_ci_audit import bind_selected_log, native_observations
from scripts.benchmark_ci_recovery import ref


@pytest.fixture(autouse=True)
def isolated_root(tmp_path,monkeypatch):
    monkeypatch.setattr('scripts.benchmark_org_ci_audit.ROOT',tmp_path)


def test_summaries_keep_repeated_invocations_and_never_sum_class_lines():
    text='''2026-09-22T00:00:00Z ##[group]Run ./mvnw verify
2026-09-22T00:00:00Z ##[endgroup]
[INFO] --- surefire:3.5.1:test (default-test) @ core ---
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 1 s -- in ExampleTest
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 1
[INFO] BUILD SUCCESS
2026-09-22T00:00:01Z ##[group]Run ./mvnw -q test jacoco:report
2026-09-22T00:00:01Z ##[endgroup]
'''
    groups=native_observations(text)
    assert len(groups)==2
    assert len(groups[0]['native_test_summaries'])==1
    assert groups[0]['native_test_summaries'][0]['assessed_count']==2
    assert groups[0]['native_test_summaries'][0]['producer']['module']=='core'
    assert groups[1]['native_test_summaries']==[]


def test_tycho_unprefixed_summary_preserves_producer_without_class_double_count():
    text='''##[group]Run mvn verify
[INFO] --- tycho-surefire:5.0.1:test (default-test) @ sample.tests ---
Tests run: 3, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 1 s -- in Example
\x1b[0mTests run: 3, Failures: 0, Errors: 0, Skipped: 1\x1b[0m
'''
    values=native_observations(text)[0]['native_test_summaries']
    assert len(values)==1
    assert values[0]['producer']=={'plugin':'tycho-surefire','goal':'test','module':'sample.tests'}
    assert values[0]['reported_count']==3


def fixture(tmp_path):
    def save(name,value):
        path=tmp_path/name
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        path.write_bytes(raw)
        value=ref(path,tmp_path);value['path']=str(path)
        return value
    task={'repo':'Org/Repo','sha':'abc','run_id':12,'run_attempt':2,'job_id':9,'selected_job_ids':[9]}
    job={'id':9,'run_id':12,'run_attempt':2,'head_sha':'abc'}
    jobs=save('jobs.json',{'jobs':[job]})
    jobs_url='https://api.github.com/repos/Org/Repo/actions/runs/12/attempts/2/jobs?per_page=100&page=1'
    jobs_receipt=save('jobs-receipt.json',{'url':jobs_url,'http_status':200,'exit_code':0,'sha256':jobs['sha256'],'bytes':jobs['bytes']})
    task['jobs_refs']=[{**jobs,'url':jobs_url,'receipt':jobs_receipt}]
    log=save('job.log',b'log')
    url='https://api.github.com/repos/Org/Repo/actions/jobs/9/logs'
    receipt=save('receipt.json',{'url':url,'http_status':200,'exit_code':0,'sha256':log['sha256'],'bytes':3})
    task['job_log_ref']={**log,'receipt':receipt,'url':url}
    return task,save


def test_same_job_attempt_commit_and_response_required(tmp_path):
    task,save=fixture(tmp_path)
    assert bind_selected_log(task)[0]['status']=='confirmed'
    task['run_attempt']=3
    assert bind_selected_log(task)[0]['status']=='unavailable'


def test_changed_source_bytes_refused(tmp_path):
    task,save=fixture(tmp_path)
    Path(task['job_log_ref']['path']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='mismatch'):
        bind_selected_log(task)


def test_wrong_official_log_endpoint_refused(tmp_path):
    task,save=fixture(tmp_path)
    task['job_log_ref']['url']='https://api.github.com/repos/Org/Repo/actions/jobs/10/logs'
    assert bind_selected_log(task)[0]['status']=='unavailable'
