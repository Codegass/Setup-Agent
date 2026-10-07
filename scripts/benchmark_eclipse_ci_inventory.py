"""Join the fixed Eclipse metadata frame to official CI entrypoints, then index.

Instance job listings are discovery observations only, never repository binding,
canonical job selection or evidence that an unlisted repository lacks CI.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from urllib.parse import urlencode

from scripts.benchmark_ci_recovery import Collector, ref, write
from scripts.benchmark_test_evidence_audit import digest


ROOT=Path(__file__).resolve().parents[1]
TREE='jobs[name,url,_class,jobs[name,url,_class,jobs[name,url,_class]]]'


def inventory(candidates,directory):
    instances=directory['instances']
    rows=[]
    for candidate in candidates['repositories']:
        if not candidate.get('new_candidate'):continue
        matches=[i for i in instances if i['project_id'] in candidate['project_ids']]
        rows.append({'repo':candidate['repo'],'repository_id':candidate['repository_id'],
            'project_ids':candidate['project_ids'],'population_status':candidate['population_status'],
            'default_branch':candidate['default_branch'],'default_branch_sha':candidate['default_branch_sha'],
            'official_jenkins_instances':matches,
            'official_github_repository_url':'https://github.com/'+candidate['repo'],
            'github_actions_index_url':'https://api.github.com/repos/'+candidate['repo']+'/actions/workflows?per_page=100',
            'status':'jenkins_entrypoint_available_task_not_selected' if matches else 'official_directory_has_no_matching_instance_github_or_other_ci_review_pending',
            'task_admitted':False,'complete_case_records_available':False})
    return rows


def main():
    base=ROOT/'output/java-benchmark-org-expansion-20260922'
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',type=Path,default=base/'eclipse/metadata-candidates.partial.json')
    p.add_argument('--output',type=Path,default=base/'ci')
    p.add_argument('--collect-instances',action='store_true')
    a=p.parse_args();directory_path=a.output/'eclipse-directory.json'
    raw=a.candidates.read_bytes(); candidates=json.loads(raw);directory=json.loads(directory_path.read_text())
    snapshot=a.output/'source-snapshots'/(digest(raw)+'.json');snapshot.parent.mkdir(parents=True,exist_ok=True);snapshot.write_bytes(raw)
    rows=inventory(candidates,directory);collector=Collector(a.output.resolve(),a.collect_instances)
    urls=sorted({item['url'].rstrip('/')+'/api/json?'+urlencode({'tree':TREE}) for row in rows for item in row['official_jenkins_instances']})
    with ThreadPoolExecutor(max_workers=4) as pool:responses=list(pool.map(collector.get,urls))
    result={'schema':'eclipse-ci-entrypoint-inventory-v1','generation_script':ref(Path(__file__).resolve(),ROOT),
        'candidate_source':ref(snapshot.resolve(),ROOT),'directory_source':ref(directory_path.resolve(),ROOT),
        'population_enumeration_complete':candidates['complete'],'candidate_count':len(rows),
        'unique_official_instances':len(urls),'rows':rows,'instance_indexes':responses,
        'job_depth_limit':3,'limits':['Project/JIPP mapping does not establish job repository or task scope.',
            'Only fixed official instance first three job/folder levels indexed. Deeper folders remain pending.',
            'No instance in this directory is not proof of no CI; GitHub Actions and other officially linked CI remain to inspect.',
            'No canonical task/reference or complete test record is certified by this discovery inventory.']}
    write(a.output/'eclipse-entrypoints.json',result)
    print({'candidate_count':len(rows),'instances':len(urls),'available_indexes':sum(r['status']=='available' for r in responses),'population_enumeration_complete':candidates['complete']})


if __name__=='__main__':main()
