"""Validate byte-bound passive Maven observations, separately from task verdicts."""
import hashlib
import json
from pathlib import PurePosixPath
import re


def relative(value):
    path=PurePosixPath(value)
    if not isinstance(value,str) or not value or path.is_absolute() or '..' in path.parts or str(path)!=value:
        raise ValueError('Noncanonical observer path')
    return value


def files(base, event):
    if event.get('collection_error') or not isinstance(event.get('files'),list):
        raise ValueError('Incomplete native output observation')
    result={}
    for file in event['files']:
        path=relative(file['path'])
        if path in result or not re.fullmatch('[0-9a-f]{64}',file.get('sha256','')):
            raise ValueError('Duplicate output path or invalid output digest')
        if type(file.get('bytes')) is not int or file['bytes']<0 or not isinstance(file.get('mtime'),str):
            raise ValueError('Missing output fingerprint')
        if 'archive_path' in file:
            archived=base/relative(file['archive_path'])
            if not archived.resolve().is_relative_to(base.resolve()) or archived.is_symlink():
                raise ValueError('Archived output escaped its observation')
            data=archived.read_bytes()
            if len(data)!=file['bytes'] or hashlib.sha256(data).hexdigest()!=file['sha256']:
                raise ValueError('Archived output bytes differ')
        elif path.endswith('.class') and not re.fullmatch('cafebabe[0-9a-f]{8}',file.get('class_header','')):
            raise ValueError('Missing Java class header')
        result[path]={k:v for k,v in file.items() if k!='archive_path'}
    return result


def review(path,*,run_id,root):
    """Pair native lifecycle records and verify each archived report byte."""
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    if (len(rows)<3 or rows[0].get('event')!='ObserverStarted'
            or rows[-1].get('event')!='ObserverClosed'
            or rows[0].get('run_id')!=run_id or rows[0].get('root')!=root):
        raise ValueError('Incomplete trace or observation identity mismatch')
    if [r.get('sequence') for r in rows[1:-1]]!=list(range(1,len(rows)-1)):
        raise ValueError('Native event sequence is incomplete')
    pending={}; completed=[];seen=set();fork_depth={}
    for event in rows[1:-1]:
        kind=event.get('event'); thread=event.get('thread')
        if kind=='ForkStarted':fork_depth[thread]=fork_depth.get(thread,0)+1
        if kind in ('ForkSucceeded','ForkFailed'):
            if fork_depth.get(thread,0)==0:raise ValueError('Unbalanced native fork')
            fork_depth[thread]-=1
        if kind=='MojoStarted':
            identifier=event.get('mojo_id')
            if not identifier or identifier in seen:raise ValueError('Duplicate native occurrence')
            seen.add(identifier);pending[identifier]=(event,fork_depth.get(thread,0))
        elif kind in ('MojoSucceeded','MojoFailed'):
            before,depth=pending.pop(event.get('mojo_id'),(None,None))
            if before is None:raise ValueError('Native return has no matching start')
            identity=('module','module_path','plugin','version','goal','execution','prefix','thread')
            if any(before.get(k)!=event.get(k) for k in identity):
                raise ValueError('Native occurrence identity changed')
            first,last=files(path.parent,before),files(path.parent,event)
            completed.append({'before':before,'after':event,'depth':depth,
                'fresh_outputs':[name for name,value in last.items() if first.get(name)!=value],
                'status':'returned' if kind=='MojoSucceeded' else 'failed'})
    if pending or any(fork_depth.values()):raise ValueError('Native occurrence or fork is not closed')
    sessions=[r for r in rows if r.get('event')=='SessionStarted']
    if len(sessions)!=1 or sessions[0].get('concurrency',1)!=1:
        raise ValueError('One serial native Maven session is required')
    return {'header':rows[0], 'session':sessions[0], 'closed':rows[-1],
            'runtime':{k:rows[0][k] for k in ('java_home','java_version','java_vendor')},
            'trace_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'occurrences':completed}


def compiler_reuse(producer_trace,consumer_trace,producer,consumer):
    """Four-boundary continuity; caller must additionally bind attempt/dispatch."""
    if any(p['status']!='returned' or p['depth']!=0 for p in (producer,consumer)):
        return False
    pb,pa,cb,ca=producer['before'],producer['after'],consumer['before'],consumer['after']
    if any(e.get('input_collection_error') or e.get('configuration_error') for e in (pb,pa,cb,ca)):
        return False
    if any(not isinstance(e.get('compiler_inputs'),list) or not e['compiler_inputs']
           or not re.fullmatch('[0-9a-f]{64}',e.get('resolved_configuration_sha256',''))
           for e in (pb,pa,cb,ca)):
        return False
    if any(not isinstance(v,str) or not v for trace in (producer_trace,consumer_trace)
           for v in trace.get('runtime',{}).values()):return False
    if producer_trace['runtime']!=consumer_trace['runtime'] or not producer['fresh_outputs']:
        return False
    identity=('module','module_path','plugin','version','goal','execution','resolved_configuration_sha256')
    if any(pa.get(k)!=e.get(k) for e in (pb,cb,ca) for k in identity):return False
    def inputs(event):
        values=event['compiler_inputs']
        # Maven creates an empty annotation output directory during compile.
        # Its existence alone isn't a source change; generated file bytes are.
        return [{**v, 'state':'empty'} if 'root' in v and v.get('kind')=='source'
                and v.get('state') in {'absent','directory'}
                and not any(f.get('path','').startswith(v['root']+'/') for f in values)
                else v for v in values]
    if any(inputs(pa)!=inputs(e) for e in (pb,cb,ca)):
        return False
    def content(e):return {f['path']:{k:v for k,v in f.items() if k not in ('mtime','archive_path')}
                           for f in e['files']}
    return bool(content(pa)) and content(pa)==content(cb)==content(ca)
