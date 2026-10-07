"""Inspect selected prefix job report indexes and small bound testcase artifacts."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from zipfile import BadZipFile

from scripts.benchmark_ci_recovery import Collector, artifact_indexes, inspect_artifact, log_links, ref, write
from scripts.benchmark_org_ci_audit import native_observations
from scripts.benchmark_test_evidence_audit import case_counts, digest, identity_inventory, write_case_archive, zip_xml


ROOT=Path(__file__).resolve().parents[1]


def audit(candidate,collector,collect_reports):
    jobs_response=collector.get(candidate['jobs_response']['url']);jobs=collector.json(jobs_response)
    matches=[j for j in jobs.get('jobs',[]) if j.get('id')==candidate['job_id']] if isinstance(jobs,dict) else []
    log_response=collector.get(candidate['log_response']['url'])
    bound=(len(matches)==1 and all(matches[0].get(key)==candidate[key] for key in ('run_id','run_attempt'))
           and matches[0].get('head_sha')==candidate['sha'] and log_response['status']=='available'
           and log_response['url']==f"https://api.github.com/repos/{candidate['repo']}/actions/jobs/{candidate['job_id']}/logs")
    row={key:candidate[key] for key in ('repo','repository_id','sha','official_ci_url','conclusion','job_name')}
    row.update({'ci_identity':{key:candidate[key] for key in ('run_id','run_attempt','job_id','selected_job_ids')},
        'selected_log_binding':'confirmed' if bound else 'unavailable','jobs_source':jobs_response,'log_source':log_response,
        'task_admitted':False,'selection_status':candidate['selection_status'],
        'complete_case_records_available':False,'cross_run_identity_equivalence':'not_evaluated',
        'scope':'Exact provisional job only; whole task native producer closure and source applicability remain unreviewed'})
    logs=[]
    if bound:logs=[{'job_id':candidate['job_id'],'source':log_response,
                   'text':(collector.out/log_response['body']['path']).read_text(errors='replace')}]
    row.update(log_links(logs));row['native_execution_observations']=native_observations(logs[0]['text']) if logs else []
    index,artifacts=artifact_indexes(row,collector,{})
    row['artifact_index']=index;row['artifacts']=[inspect_artifact(row,a,collector.max_report_bytes) for a in artifacts]
    for a in row['artifacts']:
        if a['disposition']!='report_download_candidate':continue
        response=collector.get(a['url']);a['download']=response
        if response['status']!='available':continue
        try:
            records,members,errors=zip_xml(collector.out/response['body']['path'])
            key=digest(f"{row['repo']}:{candidate['run_id']}:{candidate['run_attempt']}:{a['artifact_id']}".encode())[:20]
            a['parsed_report']={'case_records':write_case_archive(collector.out,{'task_id':key},records),
                'xml_members':members,'errors':errors,'observed_counts':case_counts(records),
                'identity_inventory':identity_inventory(records),'complete_producer_scope':False}
        except (ValueError,OSError,BadZipFile) as exc:a['parse_error']=str(exc)
    if not bound:status='selected_log_unavailable_or_unbound'
    elif index['status']!='confirmed':status='artifact_index_unavailable'
    elif any(a.get('parsed_report',{}).get('observed_counts',{}).get('reported_count',0)>0 for a in row['artifacts']):status='partial_records_observed_scope_review_pending'
    elif any(a['job_attempt_commit_bound'] and a['report_hint'] for a in row['artifacts']):status='selected_report_requires_scope_review'
    else:status='no_selected_testcase_report_upload_observed'
    row['case_evidence_status']=status
    return row


def main():
    b=ROOT/'output/java-benchmark-org-expansion-20260922';p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=b/'ci');p.add_argument('--collect',action='store_true');p.add_argument('--collect-reports',action='store_true')
    a=p.parse_args();input_path=a.output/'eclipse-prefix-review.json';raw=input_path.read_bytes();review=json.loads(raw)
    snapshot=a.output/'source-snapshots'/(digest(raw)+'.json');snapshot.parent.mkdir(parents=True,exist_ok=True);snapshot.write_bytes(raw)
    collector=Collector(a.output.resolve(),a.collect or a.collect_reports)
    rows=[audit(r['selected_candidate'],collector,a.collect_reports) for r in review['rows'] if r['selected_candidate']]
    parsed=[artifact['parsed_report'] for row in rows for artifact in row['artifacts'] if artifact.get('parsed_report')]
    result={'schema':'eclipse-prefix-test-evidence-v1','review_source':ref(snapshot.resolve(),ROOT),
        'generation_script':ref(Path(__file__).resolve(),ROOT),'collector_script':ref(ROOT/'scripts/benchmark_ci_recovery.py',ROOT),
        'entrypoint_source':ref(a.output/'eclipse-entrypoints.json',ROOT),
        'workflow_index_source':ref(a.output/'eclipse-github-actions.json',ROOT),
        'source_applicability_review':ref(b/'eclipse/source-applicability/review.json',ROOT),
        'fixed_population_size':review['fixed_population_size'],'workload_batch_size':review['batch_size'],
        'summary':{'provisional_job_candidates':len(rows),'complete_case_records':0,
            'partial_report_cases':sum(p['observed_counts']['reported_count'] for p in parsed),
            'partial_report_skipped':sum(p['observed_counts']['skipped_count'] for p in parsed),
            'partial_report_assessed':sum(p['observed_counts']['assessed_count'] for p in parsed),
            'partial_report_xml_files':sum(len(p['xml_members']) for p in parsed),
            'case_evidence_status':dict(Counter(r['case_evidence_status'] for r in rows))},
        'rows':rows,'remaining_deeper_review':review['remaining_queue'],
        'unselected_prefix_repositories':[{'repo':r['repo'],'gap':r.get('gap'),'search_extent':r.get('search_extent')} for r in review['rows'] if not r['selected_candidate']]}
    write(a.output/'eclipse-prefix-evidence.json',result)
    lines=['# Eclipse CI evidence: fixed first review batch','',
        f"All {review['fixed_population_size']} metadata candidates remain in the queue. This batch is the first {review['batch_size']} repository names alphabetically: an explicit workload boundary, not an eligibility criterion or representative availability sample.",'',
        'The official project directory mapped 41 repositories to 39 Jenkins instances. Their archived instance indexes require authentication; anonymous read was denied. All 60 GitHub Actions workflow indexes were captured with complete pagination. Neither entrypoint mapping establishes task scope or test availability.','',
        '| Provisional selected repository | Exact-job evidence | Report records |','|---|---|---|']
    for row in rows:
        counts=[f"{p['observed_counts']['reported_count']} reported / {p['observed_counts']['skipped_count']} skipped / {p['observed_counts']['assessed_count']} assessed ({len(p['xml_members'])} XML)" for artifact in row['artifacts'] if (p:=artifact.get('parsed_report'))]
        lines.append(f"| {row['repo']} | {row['case_evidence_status']} | {'; '.join(counts) or 'not obtained'} |")
    lines.extend(['','CDT records are bound to the exact job upload and preserve original labels and occurrences. They have no within-run collisions. Its 28 native Tycho producer summaries reconcile descriptively to 4,719 reported / 9 skipped, but the complete planned producer scope, task applicability and cross-agent identity mapping are not certified. The same-pin source review shows native production recompilation is opt-in via -Dnative=all/docker; the selected command does not set that property. GCC/GDB are test-environment tools. Whether the selected Xvfb/ptrace setup requires adaptations in the benchmark container remains pending. These observations do not automatically exclude the repository.','',
        'BIRT uploads large application packages, not selected testcase reports. They were not downloaded. The parser preserves 20 distinct native producer summaries, including unprefixed Tycho output; these are not a certified whole-project test denominator. Eclipse Collections has no selected testcase-report upload. AspectJ log HTTP 410 and the cancelled DL4J examples log HTTP 404 are retained as distinct unavailable states; no replacement cell was chosen for evidence convenience.','',
        '## Unresolved repositories within the fixed batch','',
        '| Repository | Inspected retained default-branch completed runs | Disposition |','|---|---:|---|'])
    for row in review['rows']:
        if row['selected_candidate']:continue
        extent=row.get('search_extent',{})
        lines.append(f"| {row['repo']} | {extent.get('first_page_rows','unavailable')} / {extent.get('api_total_count','unavailable')} listed; {len(row['inspected_runs'])} source candidates inspected | {row.get('gap')} |")
    lines.extend(['','Run discovery captured the first 100 retained completed default-branch runs per repository. Where the API total exceeds that page, older created runs may have later attempts. Pins therefore remain provisional, even when their exact job/report binding is valid. Unsupported native commands, no literal lifecycle goal and reusable actions remain review gaps, not evidence that no task exists.','',
        f"The remaining {len(review['remaining_queue'])} repositories retain explicit deeper-review queue entries. No new complete eligible testcase reference, cross-run equivalence, requirements-v2 readiness or campaign success is claimed by this batch. Full raw responses, URL/time/status/hash/byte receipts, selected command sources and parsed case records are linked by the JSON artifacts.",''])
    (a.output/'ECLIPSE_REPORT.md').write_text('\n'.join(lines))
    print(result['summary'])


if __name__=='__main__':main()
