"""Preserve SAG's prospective checkout observation for common external acceptance.

This adapter never takes a late snapshot and calls it task_start. SAG captures
the source boundary in its clone callback before returning control to the actor.
"""
import json
import shutil
from pathlib import Path

from sag.benchmark.recorder import reference, write_json
from sag.benchmark.requirements import bound_file, canonical_digest, evaluation_identity, load_json


def freeze_source(root, destination):
    import subprocess
    destination.mkdir(parents=True, exist_ok=False)
    names=set(subprocess.check_output(['git','ls-files','-z'],cwd=root,text=True).split('\0'))
    names.update(subprocess.check_output(['git','ls-files','--others','--exclude-standard','-z','--','src','scripts'],cwd=root,text=True).split('\0'))
    from sag.benchmark.requirements import file_digest
    files={}
    for name in sorted(names):
        if not name: continue
        source=root/name
        if source.is_symlink(): raise ValueError('Source symlink requires review: '+name)
        if not source.is_file(): continue
        if source.name=='.env': raise ValueError('Credentials cannot enter the source archive')
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
        files[name]={'sha256':file_digest(target),'mode':target.stat().st_mode&0o777}
    return {'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
            'files':files,'snapshot_sha256':canonical_digest(files),
            'identity_note':'Exact working-tree bytes and modes, including approved uncommitted implementation.'}


def initial_from_sag(session, destination, task, spec, *, image_id):
    session, destination=Path(session).resolve(),Path(destination).resolve()
    pin=load_json(session/'run-pin.json')
    identity=evaluation_identity(spec)
    actual=pin.get('sanitized_config',{}).get('evaluation_protocol',{})
    if (pin.get('target_repo_sha')!=task['sha'] or pin.get('container_image_digest')!=image_id
            or any(actual.get(k)!=v for k,v in identity.items())):
        raise ValueError('SAG source/image/task pin does not bind this attempt')
    rows=[(p,load_json(p)) for p in (session/'worktree-evidence').glob('*.json')]
    starts=[(p,s) for p,s in rows if s.get('boundary')=='task_start' and s.get('run_id')==pin['run_id']]
    if len(starts)!=1: raise ValueError('Expected one prospective SAG checkout boundary')
    path,snapshot=starts[0]
    project_root='/workspace/'+task['repo'].split('/')[1]
    if (snapshot.get('project_root')!=project_root or snapshot.get('status')!='observed'
            or snapshot.get('head')!=task['sha'] or snapshot.get('tracked_clean') is not True):
        raise ValueError('Initial SAG checkout boundary is not ready')
    probes={'head':['rev-parse','HEAD'],'tracked':['diff','--name-only','-z','HEAD','--'],
            'untracked':['ls-files','--others','--exclude-standard','-z']}
    copied=[path]
    for name,argv in probes.items():
        probe=snapshot['probes'][name]
        if probe['argv']!=['git','-C',project_root,*argv] or type(probe['exit_code']) is not int or probe['exit_code']!=0:
            raise ValueError('SAG checkout probe identity is invalid')
        data=bound_file(session,probe['stdout']).read_bytes()
        if name=='head' and data.decode().strip()!=task['sha'] or name=='tracked' and data:
            raise ValueError('Initial raw Git observations contradict ready source')
        if name=='untracked' and data and not data.endswith(b'\0'):
            raise ValueError('Initial untracked listing is incomplete')
        copied.extend(bound_file(session,probe[k]) for k in ('stdout','stderr'))
    destination.mkdir(parents=True,exist_ok=False)
    for source in copied:
        target=destination/source.relative_to(session);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    write_json(destination/'task.json',task);write_json(destination/'requirements.json',spec)
    state={'schema_version':2,'recorder_version':'portable-requirements-v2','run_id':pin['run_id'],
           'evaluation_identity':identity,'repo':task['repo'],'commit':task['sha'],'project_root':project_root,
           'agent':'sag','started_at':snapshot['observed_at'],'admission':'ready',
           'worktree':[reference(destination,destination/path.relative_to(session))],'invocations':[],
           'input_policy':{'step_stdin':'DEVNULL','between_commands':'not_observed'},
           'initial_boundary_source':'SAG host recorder in clone callback; raw observations preserved unchanged'}
    write_json(destination/'run-open.json',state)
    return state,pin
