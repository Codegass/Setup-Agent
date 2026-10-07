"""Bounded read-only CI discovery for a predeclared lexicographic candidate prefix.

The batch size limits work, not admission. The first retained run-index page is
not an exhaustive completion-time search; every candidate pin stays provisional.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from scripts.benchmark_ci_recovery import Collector, ref, write
from scripts.collect_fasterxml_expansion import eligible_jobs, workflow_commands, workflow_unknowns


ROOT=Path(__file__).resolve().parents[1]


def direct_test_candidate(command):
    """Explicit skip settings cannot make a quality-only job a test reference."""
    if not command['contains_build_tool'] or not command['contains_test_lifecycle_token']:return False
    text=command['command']
    properties={}
    for match in re.finditer(r'(?:^|\s)-D(skipTests|maven\.test\.skip)(?:=([^\s]+))?(?=\s|$)',text):
        properties[match.group(1)]=match.group(2)
    if any(value is None or value.lower()=='true' for value in properties.values()):return False
    if re.search(r'(?:^|\s)(?:-x|--exclude-task)(?:=|\s+)(?:test|check)(?:\s|$)',text):return False
    return True


def content(collector,repo,sha,path):
    url=f'https://api.github.com/repos/{repo}/contents/{quote(path,safe="/")}?ref={sha}'
    response=collector.get(url);data=collector.json(response)
    if not isinstance(data,dict) or data.get('type')!='file' or data.get('encoding')!='base64':
        return None,{'response':response,'status':'unavailable'}
    raw=base64.b64decode(data['content'])
    blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    if blob!=data['sha']:raise ValueError('Pinned source Git blob mismatch')
    return raw.decode(errors='replace'),{'response':response,'repository_path':path,'commit':sha,
        'git_blob_sha':blob,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'status':'available'}


def review(row,collector):
    repo=row['repo'];branch=row['default_branch']
    endpoint=f'https://api.github.com/repos/{repo}/actions/runs?branch={quote(branch,safe="")}&status=completed&per_page=100&page=1'
    response=collector.get(endpoint);data=collector.json(response)
    result={'repo':repo,'repository_id':row['repository_id'],'default_branch':branch,
        'head_at_population':row['default_branch_sha'],'run_index':response,'inspected_runs':[],
        'selected_candidate':None,'task_admitted':False,'selection_status':'provisional_bounded_search'}
    if not isinstance(data,dict) or not isinstance(data.get('workflow_runs'),list):
        result['gap']='Retained first-page run index unavailable';return result
    runs=data['workflow_runs']
    result['search_extent']={'api_total_count':data.get('total_count'),'first_page_rows':len(runs),
        'all_pages_scanned':len(runs)==data.get('total_count'),
        'unsearched_older_created_runs_may_have_later_attempts':len(runs)!=data.get('total_count'),
        'ordering':'Inspect captured runs by updated_at upper bound, then exact eligible job completed_at; source parse failures remain unresolved'}
    runs=sorted(runs,key=lambda r:(r.get('updated_at',''),r.get('html_url','')),reverse=True)
    best=None
    for run in runs:
        if run.get('head_branch')!=branch or run.get('event') not in {'push','schedule','workflow_dispatch','workflow_call'}:continue
        if best and run.get('updated_at','')<best['completed_at']:continue
        item={k:run.get(k) for k in ('id','run_attempt','head_sha','head_branch','event','name','path','conclusion','html_url','created_at','updated_at')}
        result['inspected_runs'].append(item)
        path=run.get('path','').split('@',1)[0]
        if not path.startswith('.github/workflows/'):
            item['gap']='Dynamic or non-repository workflow source; not treated as setup absence';continue
        text,source=content(collector,repo,run['head_sha'],path);item['workflow_source']=source
        if text is None: item['gap']='Pinned workflow source unavailable';continue
        try:commands=workflow_commands(text);unknowns=workflow_unknowns(text)
        except Exception as exc:item['gap']='Workflow parse unavailable: '+str(exc);continue
        item['ordered_source_commands']=commands;item['unreviewed_scopes']=unknowns
        candidates=[c for c in commands if direct_test_candidate(c)]
        if not candidates:
            item['gap']='No direct supported build/test command candidate; defaultGoal/action/native-tool scope requires source review';continue
        jobs_endpoint=f'https://api.github.com/repos/{repo}/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs?per_page=100&page=1'
        jobs_response=collector.get(jobs_endpoint);jobs=collector.json(jobs_response);item['jobs_response']=jobs_response
        if not isinstance(jobs,dict) or not isinstance(jobs.get('jobs'),list):item['gap']='Jobs unavailable';continue
        if jobs.get('total_count')!=len(jobs['jobs']):item['gap']='Job index pagination pending; no cell selected';continue
        if any(j.get('run_id')!=run['id'] or j.get('run_attempt')!=run['run_attempt'] or j.get('head_sha')!=run['head_sha'] for j in jobs['jobs']):
            item['gap']='Job metadata differs from exact run attempt/commit';continue
        matches=eligible_jobs({'ordinary_build_test_command_candidates':candidates},jobs['jobs'])
        item['eligible_job_ids']=[j['id'] for j,c in matches]
        if not matches:item['gap']='No ordinary Linux direct-command cell was unambiguously mapped; not task absence';continue
        job,mapping=matches[0]
        if best and (job['completed_at']<best['completed_at'] or job['completed_at']==best['completed_at'] and job['html_url']>best['official_ci_url']):continue
        log_url=f'https://api.github.com/repos/{repo}/actions/jobs/{job["id"]}/logs'
        log_response=collector.get(log_url)
        best={'repo':repo,'repository_id':row['repository_id'],'sha':run['head_sha'],
            'run_id':run['id'],'run_attempt':run['run_attempt'],'job_id':job['id'],'selected_job_ids':[job['id']],
            'job_name':job['name'],'completed_at':job['completed_at'],'conclusion':job['conclusion'],
            'official_ci_url':job['html_url'],'workflow_source':source,'jobs_response':jobs_response,
            'log_response':log_response,'ordered_source_commands':commands,'mapping_candidates':mapping,
            'admitted':False,'selection_status':'provisional_bounded_search_and_task_scope_review_required'}
    result['selected_candidate']=best
    if best is None:result['gap']='No candidate established in inspected extent; source/action/defaultGoal review or wider run search may resolve this'
    return result


def main():
    b=ROOT/'output/java-benchmark-org-expansion-20260922';p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=b/'ci');p.add_argument('--collect',action='store_true')
    p.add_argument('--batch-size',type=int,default=10)
    a=p.parse_args();candidates_path=b/'eclipse/candidates.json';population=json.loads(candidates_path.read_text())
    all_rows=sorted((r for r in population['repositories'] if r.get('new_candidate')),key=lambda r:r['repo'])
    if a.batch_size<1 or a.batch_size>len(all_rows):raise ValueError('Invalid work batch size')
    selected=all_rows[:a.batch_size];collector=Collector(a.output.resolve(),a.collect)
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda r:review(r,collector),selected))
    result={'schema':'eclipse-prefix-ci-review-v1','generation_script':ref(Path(__file__).resolve(),ROOT),
        'selection_helper_script':ref(ROOT/'scripts/collect_fasterxml_expansion.py',ROOT),'candidate_source':ref(candidates_path,ROOT),
        'batch_definition':'First N metadata candidates in repository lexicographic order; workload boundary, not selection criterion or a representative evidence-availability sample',
        'batch_size':len(results),'fixed_population_size':len(all_rows),'rows':results,
        'remaining_queue':[{'repo':r['repo'],'repository_id':r['repository_id'],'status':'deeper_task_review_pending'} for r in all_rows[a.batch_size:]]}
    write(a.output/'eclipse-prefix-review.json',result)
    print({'reviewed_prefix':len(results),'provisional_candidate_jobs':sum(r['selected_candidate'] is not None for r in results),'remaining':len(result['remaining_queue'])})


if __name__=='__main__':main()
