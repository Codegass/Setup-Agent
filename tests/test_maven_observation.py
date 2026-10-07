"""Passive evidence must not make missing, mixed or stale records pass."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from sag.benchmark import jvm_inputs, maven_observation
from sag.benchmark.evaluator import evaluate
from sag.benchmark.requirements import load_json, bound_file, report_scope_errors


def test_startup_approval_is_exact_and_cannot_hide_another_rc():
    names={'activation_sha256':'/etc/mavenrc','launcher_sha256':'/opt/sag-maven-witness/launch.py',
           'observer_sha256':'/opt/sag-maven-witness/witness.jar'}
    policy={'policy':'passive-maven-events-v1',**{k:hashlib.sha256(k.encode()).hexdigest() for k in names}}
    observed={p:policy[k] for k,p in names.items()}
    assert jvm_inputs.observer_rc_known({'maven_observer':policy},observed)
    assert not jvm_inputs.observer_rc_known({},observed)
    assert not jvm_inputs.observer_rc_known({'maven_observer':policy},{**observed,'/root/.mavenrc':'0'*64})
    assert not jvm_inputs.observer_rc_known({'maven_observer':policy},{**observed,'/etc/mavenrc':'0'*64})


def test_overlapping_pools_need_distinct_frozen_producer_occurrences():
    def row(name):return {'id':name,'step_id':'verify','module':'m','subtype':'unit',
        'scope':{'module_path':'.'},'validation':{'rule':'junit','report_directories':['target/surefire-reports'],
        'producer_scoped_reports':True,'native_bindings':[{'goal':'surefire:test','execution':name,'occurrence':0}]}}
    a,b=row('first'),row('second')
    assert not report_scope_errors([a,b],{'verify':'maven'})
    b['validation']['native_bindings']=deepcopy(a['validation']['native_bindings'])
    assert report_scope_errors([a,b],{'verify':'maven'})
    b=row('second');b['validation'].pop('producer_scoped_reports')
    assert report_scope_errors([a,b],{'verify':'maven'})


@pytest.fixture
def native(tmp_path):
    archive=Path(__file__).parent/'fixtures/maven-common-observation.zip'
    with zipfile.ZipFile(archive) as z:z.extractall(tmp_path)
    base=tmp_path/'records'
    task,spec,run=[load_json(base/name) for name in ('task.json','requirements.json','run.json')]
    inv=load_json(bound_file(base,run['invocations'][0]))
    return base,task,spec,run,inv


def observation(native):
    base,task,spec,run,inv=native
    return maven_observation.load(base,inv,bound_file(base,inv['log']).read_bytes(),policy=spec['maven_observer'])


def test_real_quiet_dispatch_has_separate_fresh_pools_and_reused_classes(native):
    base,task,spec,run,inv=native
    score=evaluate(task,spec,run,base)
    assert score['status']=='complete'
    rows={r['id']:r for r in score['requirements']}
    assert [rows[k]['test_counts']['assessed'] for k in ('first','second','integration')]==[2,1,1]
    assert rows['first']['native_report_producer']!=rows['second']['native_report_producer']
    assert all(rows[k]['compilation_observation']['observations'][0]['kind']=='same_attempt_class_continuity'
               for k in ('compile','test-compile'))


@pytest.mark.parametrize('key,value',[
    ('run_id','other'),('invocation_id','other'),('commit','0'*40),('project_root','/other'),
    ('effective_argv',['/opt/apache-maven-3.9.16/bin/mvn','test','-q']),
])
def test_native_observation_cannot_cross_dispatch_or_source(native,key,value):
    native[-1][key]=value
    with pytest.raises(ValueError):observation(native)


def test_corrupted_xml_cannot_be_hidden_by_successful_plugin_return(native):
    base,task,spec,run,inv=native
    obs=observation(native)
    event=next(e for e in obs['events'] if e['goal']=='surefire:test')
    row=next(r for r in spec['requirements'] if r['id']=='first')
    ref=maven_observation.reports(base,row,event)[0]
    bound_file(base,ref).write_text('<testsuite tests="0"/>')
    with pytest.raises(ValueError):observation(native)


@pytest.mark.parametrize('mutation',[
    lambda o:o.update(prior=[]),
    lambda o:o['prior'][0]['runtime'].update(java_version='changed'),
    lambda o:[p['after'].update(compiler_inputs=[]) for t in o['prior'] for p in t['occurrences']],
])
def test_missing_or_changed_producer_cannot_certify_reused_classes(native,mutation):
    obs=observation(native);mutation(obs)
    events=[e for e in obs['events'] if e['goal']=='compiler:compile']
    assert maven_observation.compiler_proof(obs,events) is None


def test_failsafe_reuse_requires_native_checksum_transition(native):
    obs=observation(native)
    events=[e for e in obs['events'] if e['goal']=='failsafe:integration-test']
    assert len(events)==2 and maven_observation.test_reuse(*events)
    events[0]['native_pair']['after']['test_execution_checksums']=[]
    assert not maven_observation.test_reuse(*events)
