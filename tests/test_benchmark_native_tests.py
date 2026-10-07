import pytest

from sag.benchmark import native_tests
from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.requirements import validate_requirements
from test_benchmark_requirement_evaluator import fixture, requirement, write
from test_benchmark_recorder import setup as recorder_setup
from test_sag_requirement_observer import setup as observer_setup


def row():
    value = requirement("integration", "test", "invoker:run")
    value.update(subtype="integration_projects")
    value["validation"].update(rule="maven_invoker", report_directories=["target/invoker-reports"])
    value["expectations"] = {"integration_projects": ["alpha/pom.xml", "beta/pom.xml"]}
    return value


def invoker_refs(base, outcomes=("success", "success")):
    return [{**write(base, f"{name}.xml", f'<build-job project="{name}/pom.xml" result="{state}" executionCount="1"/>'),
             "fresh": True, "module": "demo", "test_kind": "integration_projects",
             "producer_relative_path": f"target/invoker-reports/BUILD-{name}.xml"}
            for name, state in zip(("alpha", "beta"), outcomes)]


@pytest.mark.parametrize("outcomes,status", [(('success','success'),'passed'), (('success','skipped'),'not_run'), (('success','failure-post-hook'),'failed')])
def test_invoker_native_completion_keeps_project_counts_separate(fixture, outcomes, status):
    _, _, invocation, _, base, _ = fixture
    invocation['reports'] = invoker_refs(base, outcomes)
    log = '[INFO] --- invoker:3.10.1:run (integration-test) @ demo ---\n[INFO] BUILD SUCCESS\n'
    result = native_requirement(row(), invocation, base, log, maven_events(log, terminal=True, serial=True))
    assert result['status'] == status
    assert result['native_test_counts']['reported'] == 2
    assert result['native_test_counts']['unit'] == 'integration_project'
    assert 'test_counts' not in result


@pytest.mark.parametrize('problem', ['stale','missing','duplicate','wrong_identity','wrong_bytes','wrong_format'])
def test_invoker_cannot_borrow_or_trust_incomplete_reports(tmp_path, problem):
    refs = invoker_refs(tmp_path)
    if problem == 'stale': refs[0]['fresh'] = False
    elif problem == 'missing': refs.pop()
    elif problem == 'duplicate': refs.append(refs[0])
    elif problem == 'wrong_bytes': (tmp_path/refs[0]['path']).write_text('<changed/>')
    else:
        refs[0] = {**refs[0], **write(tmp_path, 'bad.xml', '<build-job project="other/pom.xml" result="success"/>' if problem=='wrong_identity' else '<testsuite tests="1"/>')}
    with pytest.raises(ValueError):native_tests.invoker(tmp_path, refs, ['alpha/pom.xml','beta/pom.xml'])


def ant_log():
    return ('[INFO] [au:antunit] Build File: /workspace/demo/src/it/tests.xml\n'
            '[INFO] [au:antunit] Tests run: 2, Failures: 0, Errors: 0, Time elapsed: 1 sec\n'
            '[INFO] [au:antunit] Target: testOne took 0.1 sec\n'
            '[INFO] [au:antunit] Target: testTwo took 0.9 sec\n')


@pytest.mark.parametrize('change', [None,'target','count','suite','duplicate','missing'])
def test_antunit_requires_source_scoped_suite_and_target_identities(change):
    suites = [{'path':'src/it/tests.xml','targets':['testOne','testTwo']}]
    log = ant_log()
    if change == 'target':log=log.replace('testTwo','testUnknown')
    elif change == 'count':log=log.replace('Tests run: 2','Tests run: 3')
    elif change == 'suite':log=log.replace('/tests.xml','/other.xml')
    elif change == 'duplicate':log+=log
    elif change == 'missing':log=''
    if change:
        with pytest.raises(ValueError):native_tests.antunit(log,suites)
    else:
        counts,proof=native_tests.antunit(log,suites)
        assert counts['passed']==2 and proof['unit']=='antunit_target'


def test_invoker_pool_overlap_fails_closed(fixture):
    task,spec,*_=fixture
    first,second=row(),row();second['id']='duplicate'
    spec['requirements']=[first,second]
    with pytest.raises(ValueError,match='overlap'):validate_requirements(spec,task)


def test_portable_inventory_collects_build_job_xml_only(recorder_setup):
    root, make = recorder_setup
    recorder = make()
    task = recorder.task
    # Reuse its real filesystem and frozen module name, without launching Maven.
    value=row(); value['step_id']=task['steps'][0]['id'];value['scope']['module_path']='.'
    recorder.spec['requirements']=[value]
    directory=root/'target/invoker-reports';directory.mkdir(parents=True)
    (directory/'BUILD-alpha.xml').write_text('<build-job/>')
    (directory/'TEST-alias.xml').write_text('<testsuite/>')
    files=recorder._files(task['steps'][0])
    assert [key[1] for key in files]==['target/invoker-reports/BUILD-alpha.xml']


def test_sag_inventory_uses_same_report_format(observer_setup):
    root, control, make = observer_setup
    observer = make()
    value=row();value['step_id']=observer.task['steps'][0]['id'];value['scope']['module_path']='.'
    observer.spec['requirements']=[value]
    request,errors=observer._scope(observer.task['steps'][0])
    assert not errors
    assert request['report_dirs'][0]['filename_prefix']=='BUILD-'
