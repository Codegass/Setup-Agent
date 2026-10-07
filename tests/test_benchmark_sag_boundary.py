import copy
import json
import pytest

from scripts.benchmark_sag_boundary import initial_from_sag
from sag.benchmark.recorder import reference, write_json
from sag.benchmark.requirements import evaluation_identity


def fixture(root):
    task={'repo':'apache/example','sha':'a'*40}
    spec={'task_sha256':'b'*64,'policy_version':'ci-requirements-v2'}
    # evaluation_identity also binds all requirement bytes, not the display name.
    pin={'run_id':'native-r1','target_repo_sha':task['sha'],'container_image_digest':'image',
         'sanitized_config':{'evaluation_protocol':evaluation_identity(spec)}}
    write_json(root/'run-pin.json',pin)
    s={'run_id':'native-r1','boundary':'task_start','observed_at':'2026-09-23T10:00:00Z',
       'project_root':'/workspace/example','status':'observed','head':task['sha'],
       'tracked_clean':True,'probes':{}}
    for name,argv in {'head':['rev-parse','HEAD'],'tracked':['diff','--name-only','-z','HEAD','--'],
                      'untracked':['ls-files','--others','--exclude-standard','-z']}.items():
        out=root/('worktree-evidence/initial.'+name+'.stdout');out.parent.mkdir(exist_ok=True)
        out.write_text(task['sha']+'\n' if name=='head' else '')
        err=out.with_suffix('.stderr');err.write_text('')
        s['probes'][name]={'argv':['git','-C','/workspace/example',*argv],'exit_code':0,
                           'stdout':reference(root,out),'stderr':reference(root,err)}
    write_json(root/'worktree-evidence/initial.json',s)
    return task,spec


def test_initial_import_keeps_original_run_and_raw_boundary(tmp_path):
    source=tmp_path/'source';source.mkdir();task,spec=fixture(source)
    state,pin=initial_from_sag(source,tmp_path/'dest',task,spec,image_id='image')
    assert state['run_id']=='native-r1'
    assert state['started_at']=='2026-09-23T10:00:00Z'
    assert (tmp_path/'dest/worktree-evidence/initial.json').read_bytes()==(source/'worktree-evidence/initial.json').read_bytes()
    assert state['invocations']==[]


@pytest.mark.parametrize('mutation',['wrong_image','wrong_task','late_boundary','raw_head_drift'])
def test_import_cannot_relabel_a_late_or_foreign_observation(tmp_path,mutation):
    source=tmp_path/'source';source.mkdir();task,spec=fixture(source)
    image='image'
    if mutation=='wrong_image':image='other-image'
    elif mutation=='wrong_task':task=copy.deepcopy(task);task['sha']='c'*40
    elif mutation=='late_boundary':
        path=source/'worktree-evidence/initial.json';s=json.loads(path.read_text());s['boundary']='evidence_close';write_json(path,s)
    else:(source/'worktree-evidence/initial.head.stdout').write_text('c'*40+'\n')
    with pytest.raises(ValueError):initial_from_sag(source,tmp_path/'dest',task,spec,image_id=image)
