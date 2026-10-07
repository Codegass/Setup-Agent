"""Archive complete GitHub Actions workflow indexes for every Eclipse candidate."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re

from scripts.benchmark_ci_recovery import Collector, ref, write
from scripts.benchmark_test_evidence_audit import digest


ROOT=Path(__file__).resolve().parents[1]


def workflow_index(repo,collector):
    prefix=f'https://api.github.com/repos/{repo}/actions/workflows?'
    url=prefix+'per_page=100&page=1';pages=[];rows=[];total=None;seen=set();issues=[]
    while url:
        if url in seen:issues.append('Repeated pagination URL');break
        seen.add(url);response=collector.get(url);pages.append(response);data=collector.json(response)
        if not isinstance(data,dict) or not isinstance(data.get('workflows'),list):
            issues.append('Official workflow index unavailable or malformed');break
        if type(data.get('total_count')) is not int or data['total_count']<0:
            issues.append('Missing valid workflow total_count');break
        if total is not None and total!=data['total_count']:issues.append('Workflow total_count changed across pages')
        total=data['total_count'];rows.extend(data['workflows'])
        link=response.get('response_headers',{}).get('link','')
        match=re.search(r'<([^>]+)>;\s*rel="next"',link)
        url=match.group(1) if match else None
        if url and not url.startswith(prefix):issues.append('Unexpected next-page endpoint');break
    if total is not None and total!=len(rows):issues.append('Workflow count and listed rows differ')
    if len({r.get('id') for r in rows})!=len(rows):issues.append('Duplicate workflow ID')
    return {'status':'confirmed' if not issues else 'unavailable','total_count':total,'listed_count':len(rows),
            'pagination_complete':not issues,'pages':pages,'issues':issues,'workflows':rows}


def main():
    base=ROOT/'output/java-benchmark-org-expansion-20260922'
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',type=Path,default=base/'eclipse/candidates.json')
    p.add_argument('--output',type=Path,default=base/'ci');p.add_argument('--collect',action='store_true')
    a=p.parse_args();raw=a.candidates.read_bytes();data=json.loads(raw)
    snapshot=a.output/'source-snapshots'/(digest(raw)+'.json');snapshot.parent.mkdir(parents=True,exist_ok=True);snapshot.write_bytes(raw)
    candidates=[r for r in data['repositories'] if r.get('new_candidate')]
    collector=Collector(a.output.resolve(),a.collect)
    with ThreadPoolExecutor(max_workers=4) as pool:indexes=list(pool.map(lambda r:workflow_index(r['repo'],collector),candidates))
    rows=[{'repo':r['repo'],'repository_id':r['repository_id'],'project_ids':r['project_ids'],
           'default_branch':r['default_branch'],'default_branch_sha':r['default_branch_sha'],
           'workflow_index':index,'task_selection_status':'not_reviewed','task_admitted':False}
          for r,index in zip(candidates,indexes)]
    result={'schema':'eclipse-github-actions-inventory-v1','candidate_source':ref(snapshot.resolve(),ROOT),
        'generation_script':ref(Path(__file__).resolve(),ROOT),'collector_script':ref(ROOT/'scripts/benchmark_ci_recovery.py',ROOT),
        'population_enumeration_complete':data['complete'],'candidate_count':len(rows),
        'confirmed_indexes':sum(i['status']=='confirmed' for i in indexes),'rows':rows,
        'scope':'Current official workflow entrypoints only. This does not bind a workflow version to a CI task commit, select a run/job, qualify Maven/Gradle/Android/Docker constraints or establish test-report capability.'}
    write(a.output/'eclipse-github-actions.json',result)
    print({'candidate_count':len(rows),'confirmed_indexes':result['confirmed_indexes']})


if __name__=='__main__':main()
