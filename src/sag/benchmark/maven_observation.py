"""Common, passive Maven evidence for all portable harness arms (stdlib only).

The recorder binds the observer's frozen bytes and the current dispatch. Native
returns never bypass physical outputs, test freshness, runtime or Git checks.
An untrusted agent can rewrite its whole container; this is an audit boundary,
not a security boundary against an adversarial benchmark participant.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from . import maven_trace
from .requirements import bound_file, load_json, report_directories

INSTALL = Path('/opt/sag-maven-witness')
OBSERVATIONS = Path('/opt/sag-maven-observations')


def _ref(base, path, **extra):
    data = path.read_bytes()
    return {'path': str(path.relative_to(base)), 'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data), **extra}


def prepare(base, root, run_id, invocation_id, commit, log):
    policy = load_json(INSTALL/'policy.json')
    expected = {'observer_sha256': INSTALL/'witness.jar', 'activation_sha256': Path('/etc/mavenrc'),
                'launcher_sha256': INSTALL/'launch.py'}
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != policy[k] for k,p in expected.items()):
        raise ValueError('Frozen Maven observer installation differs')
    return {'schema_version': 1, 'run_id': run_id, 'invocation_id': invocation_id,
            'commit': commit, 'root': str(root), 'namespace': os.environ['HOSTNAME'],
            'policy': policy, 'prior': sorted(p.name for p in OBSERVATIONS.iterdir()) if OBSERVATIONS.exists() else [],
            'environment': {'BENCH_MAVEN_WITNESS_DISPATCH': invocation_id,
                            'BENCH_MAVEN_WITNESS_LOG': str(log)}}


def collect(base, out, request):
    """Copy raw events/reports; unknown/unfinished producers remain archived."""
    result = {k:v for k,v in request.items() if k!='environment'}
    result['traces'] = []
    for directory in sorted(OBSERVATIONS.iterdir()) if OBSERVATIONS.exists() else []:
        if not re.fullmatch(r'[a-f0-9-]{36}', directory.name) or directory.is_symlink():
            raise ValueError('Unexpected observer directory')
        target = base/'maven-observations'/directory.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(directory, target, symlinks=True)
        refs=[]
        for p in sorted(target.rglob('*')):
            if p.is_symlink():
                raise ValueError('Observer archive contains symlink')
            if p.is_file():
                if p.read_bytes() != (directory/p.relative_to(target)).read_bytes():
                    raise ValueError('Observer bytes changed during archival')
                refs.append(_ref(base,p))
        result['traces'].append({'id':directory.name, 'prior':directory.name in request['prior'], 'files':refs})
    path=out/'maven-observation.json'
    path.write_text(json.dumps(result,indent=2)+'\n')
    return _ref(base,path)


def _verified_traces(base, document):
    for trace in document['traces']:
        paths=[bound_file(base,r) for r in trace['files']]
        events=[p for p in paths if p.name=='events.jsonl']
        if len(events)!=1: continue
        try:
            reviewed=maven_trace.review(events[0],run_id=document['namespace'],root=document['root'])
            launch=json.loads(reviewed['header']['launcher_observation'])
            if (launch['commit']!=document['commit'] or launch['root']!=document['root']
                    or launch['tracked_clean'] is not True
                    or any(launch.get(k)!=document['policy'][k] for k in ('observer_sha256','activation_sha256'))):
                continue
            reviewed.update(path=events[0],launch=launch,prior=trace['prior'])
            yield reviewed
        except (ValueError,KeyError,OSError,TypeError):
            continue  # It cannot establish a producer or replace a native return.


def load(base, invocation, raw_log, *, policy):
    document=load_json(bound_file(base,invocation['maven_observation']))
    if (policy.get('policy') != 'passive-maven-events-v1'
            or document['policy'] != {k:v for k,v in policy.items() if k!='policy'}):
        raise ValueError('Native observer differs from frozen measurement policy')
    for key in ('run_id','invocation_id','commit'):
        if document[key]!=invocation[key]:raise ValueError('Maven observer belongs to another dispatch')
    if document['root']!=invocation['project_root']:raise ValueError('Observer checkout differs')
    traces=list(_verified_traces(base,document))
    argv=invocation.get('effective_argv',invocation['argv'])
    # Invoker integration projects can launch other Maven processes in the
    # same window. They are retained, but are not the controller's root command.
    selected=[t for t in traces if not t['prior'] and t['header'].get('dispatch')==invocation['invocation_id']
              and t['launch'].get('argv',[None])[1:]==argv[1:]
              and t['launch'].get('cwd')==str(Path(document['root'])/invocation['cwd'])]
    if len(selected)!=1:raise ValueError('Exactly one complete observed Maven dispatch is required')
    current=selected[0]
    observed=current['launch']['argv']
    if (observed[1:]!=argv[1:] or not Path(observed[0]).is_absolute()
            or (Path(argv[0]).is_absolute() and observed[0]!=argv[0])
            or current['launch']['cwd']!=str(Path(document['root'])/invocation['cwd'])):
        raise ValueError('Observer launcher does not bind the frozen command')
    # Cross-check the independently observed launcher, not configured JAVA_HOME.
    probe=bound_file(base,invocation['runtime']['launcher_probe']).read_text(errors='replace')
    from .native_evidence import launcher_java_home
    home=launcher_java_home(probe)
    if home and current['runtime']['java_home']!=home:
        raise ValueError('Observer JVM differs from launcher probe')
    counts={}; events=[]
    for pair in sorted(current['occurrences'],key=lambda p:p['before']['sequence']):
        if pair['depth']!=0: continue
        a,b=pair['before'],pair['after']
        goal=a['prefix']+':'+a['goal']
        key=(a['module'],goal,a['execution'])
        occurrence=counts.get(key,0);counts[key]=occurrence+1
        first,last=a.get('log_offset'),b.get('log_offset')
        if type(first) is not int or type(last) is not int or not 0<=first<=last<=len(raw_log):
            raise ValueError('Native log segment has no valid byte boundary')
        skipped=any(v is True or v=='true' for v in a.get('skip_parameters',{}).values())
        events.append({'module':a['module'],'module_path':a['module_path'] or '.',
            'goal':goal,'execution':a['execution'],'version':a['version'],
            'occurrence':occurrence,'position':a['sequence'],
            'status':'failed' if pair['status']=='failed' else 'not_run' if skipped else 'passed',
            'returned':pair['status']=='returned', 'segment':raw_log[first:last].decode(errors='replace'),
            'native_pair':pair,'native_trace':current,'evidence_ref':invocation['maven_observation']})
    return {'events':events,'current':current,'prior':[t for t in traces if t['prior']],
            'evidence_ref':invocation['maven_observation']}


def reports(base, row, event):
    """Select only reports freshly produced by this exact goal occurrence."""
    pair=event['native_pair'];trace=event['native_trace']
    directories=report_directories(row)
    if directories is None:
        # Preserve the existing single-pool Maven default; repeated pools require
        # explicit roots in validate_requirements, independently of this reader.
        module=row.get('scope',{}).get('module_path')
        if not isinstance(module,str):raise ValueError('Native report module root unavailable')
        directories=[str(Path(module)/'target'/('failsafe-reports' if row.get('subtype')=='integration' else 'surefire-reports'))]
    result=[]
    for file in pair['after']['files']:
        path=file['path']
        if (path not in pair['fresh_outputs'] or not any(path.startswith(d+'/') for d in directories)
                or not Path(path).name.startswith('TEST-') or not path.endswith('.xml')): continue
        archived=trace['path'].parent/file['archive_path']
        result.append(_ref(base,archived,producer_relative_path=path, module=row['module'],
                           test_kind=row.get('subtype','unit'),fresh=True))
    return result


def compiler_proof(observation, matched):
    if not matched or any('native_pair' not in e for e in matched): return None
    proofs=[]
    for event in matched:
        consumer=event['native_pair'];current=observation['current']
        if (consumer['status']!='returned' or not consumer['after']['files']
                or consumer['after'].get('collection_error')): return None
        if consumer['fresh_outputs']:
            proofs.append({'kind':'fresh_class_files','goal':event['goal'],
                           'outputs':len(consumer['after']['files'])});continue
        candidates=[]
        for prior in observation['prior']:
            if prior['closed'].get('closed_epoch_ms',float('inf'))>=current['header'].get('started_epoch_ms',0):continue
            for producer in prior['occurrences']:
                if not all(e.get('compiler_runtime_observed') is True for e in (
                    producer['before'],producer['after'],consumer['before'],consumer['after'])): continue
                if maven_trace.compiler_reuse(prior,current,producer,consumer):
                    candidates.append({'trace_sha256':prior['trace_sha256'],
                                       'mojo_id':producer['before']['mojo_id']})
        if not candidates:return None
        proofs.append({'kind':'same_attempt_class_continuity','goal':event['goal'],
                       'producers':candidates,'outputs':len(consumer['after']['files'])})
    return {'observations':proofs,'evidence_refs':[observation['evidence_ref']]}


def test_reuse(producer, reused):
    """Reviewed Surefire/Failsafe 3.5.3 hasExecutedBefore context protocol.

    Require the first occurrence to introduce the native configuration checksum,
    and the second to retain that exact checksum, configuration and report bytes.
    A generic successful plugin return, or same-path report, is insufficient.
    """
    if any('native_pair' not in e or e['status']!='passed' or e['version']!='3.5.3'
           for e in (producer,reused)): return False
    p,c=producer['native_pair'],reused['native_pair']
    pb,pa,cb,ca=p['before'],p['after'],c['before'],c['after']
    if (producer['native_trace']['trace_sha256']!=reused['native_trace']['trace_sha256']
            or pa['sequence']>=cb['sequence'] or not p['fresh_outputs'] or c['fresh_outputs']
            or any(pa[k]!=e[k] for e in (pb,cb,ca) for k in ('module','module_path','plugin','version','goal','configuration_sha256'))):
        return False
    marker=set(pa.get('test_execution_checksums',[]))-set(pb.get('test_execution_checksums',[]))
    if len(marker)!=1 or not marker<=set(cb.get('test_execution_checksums',[])):
        return False
    if cb.get('test_execution_checksums')!=ca.get('test_execution_checksums'): return False
    def content(e):return {f['path']:{k:v for k,v in f.items() if k!='archive_path'} for f in e['files']}
    return bool(content(pa)) and content(pa)==content(cb)==content(ca)
