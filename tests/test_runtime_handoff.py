"""Runtime delivery uses measured command identity without granting success."""
from copy import deepcopy
import json

import pytest

from sag.agent.acceptance_task import AcceptanceStep
from sag.agent.runtime_handoff import observed_step_paths, deliver_runtime_handoff, RUNTIME_HANDOFF_PATH


def step(argv=("mvn", "test"), java_major=17, maven_version="3.9.16"):
    return AcceptanceStep(id="s",runner="maven",argv=argv,java_major=java_major,maven_version=maven_version)


def receipt():
    return {"effective_jdk":{"major":"17","runtime_authority":"dispatch_probe",
            "provenance":{"dispatch_runtime":{"executable":"/opt/jdk17/bin/java"}}},
            "toolchain_fingerprint":{"executable":"/opt/maven/bin/mvn","version":"Apache Maven 3.9.16"}}


def test_exported_hints_do_not_depend_on_exit_success_or_mutate_evidence():
    record=receipt();record['exit_code']=1;original=deepcopy(record)
    assert observed_step_paths(step(),record) == ({'java_home':'/opt/jdk17','maven_bin':'/opt/maven/bin/mvn'},None)
    assert record == original


@pytest.mark.parametrize('mutation', [
    lambda r:r['effective_jdk'].update(major='21'),
    lambda r:r['effective_jdk'].update(runtime_authority='registry'),
    lambda r:r['toolchain_fingerprint'].update(version='Apache Maven 3.8.7'),
    lambda r:r['effective_jdk']['provenance']['dispatch_runtime'].update(executable='/usr/bin/java'),
])
def test_wrong_version_unmeasured_registration_and_shim_paths_are_not_guessed(mutation):
    record=receipt();mutation(record)
    hints,reason=observed_step_paths(step(),record)
    assert hints == {} and reason


def test_wrapper_keeps_its_own_maven_and_only_delivers_launcher_jdk():
    hints,reason=observed_step_paths(step(('./mvnw','test'),maven_version=None),receipt())
    assert hints == {'java_home':'/opt/jdk17'} and reason is None


def test_real_published_receipt_missing_runtime_remains_unavailable(tmp_path):
    from test_acceptance_task import task_run,retain,dispatch
    run=task_run(tmp_path,'mvn test');retain(run,dispatch(run,'mvn test'))
    original=len(run.state.tool_observations)
    result=deliver_runtime_handoff(run.fs,run.state,validator=run.validator,task=run.task,
        project_root=run.fs.acceptance_task_root,repository=run.task.repo,output_storage=run.storage)
    assert result is not None and result['run_id'] == run.state.run_id
    assert result['steps'] == {} and result['unavailable']
    assert len(run.state.tool_observations) == original and not run.state.sealed
    assert 'No task completion authority' in result['semantics']


def test_real_publication_and_command_match_deliver_paths_once(tmp_path,monkeypatch):
    import test_ci_comparison
    from test_acceptance_task import task_run,retain,dispatch
    original_record=test_ci_comparison.record_invocation
    def measured(*args,**kwargs):
        observation=receipt()
        kwargs['effective_jdk']=observation['effective_jdk']
        kwargs['toolchain_observation']=observation['toolchain_fingerprint']
        return original_record(*args,**kwargs)
    monkeypatch.setattr(test_ci_comparison,'record_invocation',measured)
    run=task_run(tmp_path,'mvn test');retain(run,dispatch(run,'mvn test'))
    def deliver():
        return deliver_runtime_handoff(run.fs,run.state,validator=run.validator,task=run.task,
            project_root=run.fs.acceptance_task_root,repository=run.task.repo,output_storage=run.storage)
    result=deliver()
    assert result['steps']['step-0'] == {'java_home':'/opt/jdk17','maven_bin':'/opt/maven/bin/mvn'}
    assert result['sources']['step-0']['receipt_id']
    assert json.loads(run.fs.files[RUNTIME_HANDOFF_PATH]) == result
    count=len(run.fs.commands)
    assert deliver() == result
    # The second read may verify source/receipts again but must not republish.
    assert not any('base64' in call and RUNTIME_HANDOFF_PATH in call for call in run.fs.commands[count:])


def test_engine_option_is_off_by_default_and_environment_setting_is_explicit(monkeypatch):
    from sag.config.settings import Config
    assert Config().export_runtime_handoff is False
    monkeypatch.setenv('SAG_EXPORT_RUNTIME_HANDOFF','true')
    assert Config.from_env().export_runtime_handoff is True


def test_primary_execution_protocol_no_longer_requires_replay_handoff():
    from scripts.run_portable_harness import prompt
    task={'sha':'a'*40,'repo':'example/project','steps':[{'id':'s','cwd':'.','argv':['mvn','clean','verify'],'java_major':17}]}
    control=prompt(task,'/workspace/project',7200)
    candidate=prompt(task,'/workspace/project',7200,runtime_handoff=True)
    assert 'toolpaths.json' not in control
    assert control == candidate
    for text in [control,candidate]:
        assert 'skip tests, or weaken checks' in text and '7200-second deadline' in text
        assert 'will not run missing commands' in text
