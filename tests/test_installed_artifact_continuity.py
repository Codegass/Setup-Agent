"""An unchanged installed file needs this invocation's byte-bound fresh source."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from sag.benchmark.evaluator import evaluate, native_requirement
from sag.benchmark.installed_evidence import validate_source, verified_copies
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.requirements import bound_file, canonical_digest
from test_benchmark_recorder import setup
from test_sag_requirement_observer import setup as observer_setup, contract, receipt
from sag.agent.worktree_evidence import record_contract_worktree, record_receipt_worktree


@pytest.fixture
def installed(setup, tmp_path):
    root, make = setup
    repository = tmp_path / 'repository'
    target = repository / 'org/example/tiny/1-SNAPSHOT/tiny-1-SNAPSHOT-site.xml'
    target.parent.mkdir(parents=True); target.write_text('<site/>')
    source = root / 'target/tiny-1-SNAPSHOT-site.xml'
    source.parent.mkdir(); source.write_text('<site/>')
    launcher = tmp_path / 'mvn'
    launcher.write_text(f'#!{sys.executable}\n' + f'''
import sys,pathlib
if '--version' in sys.argv:
    print('Apache Maven 3.9.16\\nJava version: 17.0.12\\nJava home: /jdk')
elif '-Dexpression=settings.localRepository' in sys.argv:
    print({str(repository)!r})
else:
    source=pathlib.Path({str(source)!r})
    temporary=source.with_suffix('.new')
    temporary.write_text('<site/>');temporary.replace(source)
    print('[INFO] --- site:1.0:attach-descriptor (attach) @ tiny ---')
    print('[INFO] --- install:3.1.3:install (default-install) @ tiny ---')
    print({('[INFO] Installing '+str(source)+' to '+str(target))!r})
    print('[INFO] BUILD SUCCESS')
''')
    launcher.chmod(0o755)
    def task_change(task):
        task['steps'][0].update(runner='maven', argv=['mvn','clean','install'], java_major=17)
    def spec_change(spec):
        spec['requirements'] = [{'id':'install', 'step_id':'test', 'kind':'install', 'module':'tiny',
            'scope':{'status':'declared','module_path':'.'},
            'validation':{'rule':'install','goals':['install:install']},
            'expectations':{'artifacts':[{'role':'installed_attached','module':'tiny',
                'coordinates':{'group_id':'org.example','artifact_id':'tiny','version':'1-SNAPSHOT'},
                'extension':'xml','format':'xml','classifier':'site',
                'repository_relative_path':'org/example/tiny/1-SNAPSHOT/tiny-1-SNAPSHOT-site.xml',
                'install_source_path':'target/tiny-1-SNAPSHOT-site.xml',
                'install_source_basis':'Fixture site producer explicitly regenerates this file.'}]}}]
    recorder = make(task_change=task_change, spec_change=spec_change)
    recorder.start()
    record = recorder.step('test', timeout=5, maven_bin=launcher)
    run = recorder.close()
    return recorder, json.loads(json.dumps(record)), run


def test_real_recorder_keeps_unchanged_destination_and_fresh_source(installed):
    recorder, record, run = installed
    destination = next(a for a in record['artifacts'] if 'repository_relative_path' in a)
    source = next(a for a in record['artifacts'] if 'installation_source_for' in a)
    assert destination['fresh'] is False and source['fresh'] is True
    assert source['sha256'] == destination['sha256']
    # Keep the actual observations unchanged; the verifier adds a separate proof.
    score = evaluate(recorder.task, recorder.spec, run, recorder.base)
    row = score['requirements'][0]
    assert row['status'] == 'passed'
    assert row['installation_continuity']
    assert destination['fresh'] is False


@pytest.mark.parametrize('fresh_source', [False, True])
def test_sag_observer_keeps_the_same_install_source_proof(observer_setup, tmp_path, fresh_source):
    root, control, make = observer_setup
    repository = tmp_path / 'repository'; control.repository = repository
    relative = 'org/example/tiny/1/tiny-1-site.xml'
    target = repository / relative; target.parent.mkdir(parents=True); target.write_text('<site/>')
    source = root / 'target/tiny-1-site.xml'; source.parent.mkdir(); source.write_text('<site/>')
    def change(task, spec):
        task['steps'][0]['argv'] = ['mvn', 'clean', 'install']
        spec['task_sha256'] = canonical_digest(task)
        spec['requirements'] = [{'id': 'install', 'step_id': 'verify', 'kind': 'install', 'module': 'tiny',
            'scope': {'status': 'declared', 'module_path': '.'},
            'validation': {'rule': 'install', 'goals': ['install:install']},
            'expectations': {'artifacts': [{'role': 'installed_attached', 'module': 'tiny',
                'coordinates': {'group_id': 'org.example', 'artifact_id': 'tiny', 'version': '1'},
                'extension': 'xml', 'format': 'xml', 'classifier': 'site',
                'repository_relative_path': relative, 'install_source_path': str(source.relative_to(root)),
                'install_source_basis': 'Fixture fresh source; unchanged destination.'}]}}]
    observer = make(change); c = contract(observer)
    record_contract_worktree(control.execute, c)
    if fresh_source:
        fresh = source.with_suffix('.new'); fresh.write_text('<site/>'); fresh.replace(source)
    record_receipt_worktree(control.execute, receipt(c))
    record = observer.export_invocation(c['contract_id'])
    assert len(record['artifacts']) == 2
    record.update(runner='maven', status='completed', log_complete=True, exit_code=0)
    text = f'[INFO] --- install:3.1.3:install (default-install) @ tiny ---\n[INFO] Installing {source} to {target}\n[INFO] BUILD SUCCESS\n'
    events = maven_events(text, terminal=True, serial=True)
    proof = verified_copies(observer.base, observer.spec['requirements'][0]['expectations']['artifacts'], record, events)
    assert bool(proof) is fresh_source


@pytest.mark.parametrize('mutation', ['stale_source', 'wrong_source_path', 'wrong_coordinate',
    'wrong_root', 'no_copy_log', 'duplicate_copy_log', 'copy_in_wrong_goal', 'failed_install',
    'timeout', 'missing_destination', 'different_bytes', 'unknown_before', 'corrupt_archive'])
def test_missing_or_incompatible_proof_never_certifies_install(installed, mutation):
    recorder, original, _ = installed
    record = deepcopy(original)
    row = recorder.spec['requirements'][0]
    text = bound_file(recorder.base, record['log']).read_text()
    events = maven_events(text, terminal=True, serial=True)
    source = next(a for a in record['artifacts'] if 'installation_source_for' in a)
    dest = next(a for a in record['artifacts'] if 'repository_relative_path' in a)
    if mutation == 'stale_source': source.update(fresh=False)
    elif mutation == 'wrong_source_path': source['producer_relative_path'] = 'other/source.xml'
    elif mutation == 'wrong_coordinate': source['coordinates']['artifact_id'] = 'other'
    elif mutation == 'wrong_root': record['project_root'] = '/unrelated'
    elif mutation == 'no_copy_log': text = text.replace('Installing ', 'Not installing ')
    elif mutation == 'duplicate_copy_log': text = text.replace('[INFO] BUILD SUCCESS', text.split('--- install:')[1].split('\n',1)[1])
    elif mutation == 'copy_in_wrong_goal': text = text.replace('install:3.1.3:install', 'site:1.0:attach-descriptor')
    elif mutation == 'failed_install': text = text.replace('[INFO] BUILD SUCCESS', '[ERROR] Failed to execute goal org.apache.maven.plugins:maven-install-plugin:3.1.3:install (default-install) on project tiny: failure\n[INFO] BUILD FAILURE'); record['exit_code'] = 1
    elif mutation == 'timeout': record['status'] = 'timeout'
    elif mutation == 'missing_destination': record['artifacts'].remove(dest)
    elif mutation == 'different_bytes': source['sha256'] = '0' * 64
    elif mutation == 'unknown_before': dest['freshness']['before'] = None
    elif mutation == 'corrupt_archive': (recorder.base / source['path']).write_text('tampered')
    events = maven_events(text, terminal=mutation!='timeout', serial=True)
    if mutation == 'corrupt_archive':
        with pytest.raises(ValueError): native_requirement(row, record, recorder.base, text, events)
    else:
        assert native_requirement(row, record, recorder.base, text, events)['status'] != 'passed'


@pytest.mark.parametrize('path', ['../elsewhere', '/absolute/site.xml', '.', 'a/../b', '.git/config', 1])
def test_source_scope_cannot_escape_the_reviewed_checkout(path):
    with pytest.raises(ValueError, match='reviewed checkout-relative'):
        validate_source({'install_source_path':path, 'install_source_basis':'review', 'repository_relative_path':'a'})
