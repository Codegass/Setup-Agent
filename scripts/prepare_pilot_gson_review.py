#!/usr/bin/env python3
"""Explicit offline Gson review of the unchanged selected CI command.

This script consumes captured metadata and archived CI; it never invokes Maven,
Docker, network APIs or models. Its per-goal decisions are review data, not a
general lifecycle inference engine.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile
import xml.etree.ElementTree as ET

from scripts.build_benchmark_requirements import (
    apply_reviewed_plan, archive_source, build_project, ci_binding_inventory,
    ci_native_text, pom_execution_bindings, task_digest, text_at, xml_root,
)
from scripts.requirements_metadata_inventory import attach_preparation_inventory
from sag.benchmark.ci_count_semantics import unavailable
from sag.benchmark.requirements import bound_file, canonical_digest, load_requirements

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'output/java-benchmark-pilot-20260922'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare():
    sealed = PILOT / 'preparation/gson'
    capture_root = PILOT / 'metadata-captured'
    capture = capture_root / 'gson'
    out = PILOT / 'prepared/gson'
    inputs = out / 'import-inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    task_bytes = (sealed / 'task.json').read_bytes()
    task = json.loads(task_bytes)
    assert task['steps'][0]['argv'] == ['mvn', 'verify', 'javadoc:jar']
    assert len(task['steps']) == 1
    state = json.loads((capture / 'metadata-status.json').read_text())
    index = json.loads((capture / 'source-poms/index.json').read_text())
    inv = json.loads((sealed / 'native-review-inventory.json').read_text())['steps'][0]
    selection = json.loads((sealed / 'sources/prior-reference-task.json').read_text())
    origin = json.loads((sealed / 'sources/prior-ci-job-inventory.json').read_text())[0]['raw_source']
    for name in ['tracked-diff-before.txt', 'tracked-diff.txt', 'untracked-before.txt', 'untracked.txt']:
        assert (capture / name).read_bytes() == b''
    assert index['commit'] == task['sha'] == state['commit']
    raw_zip = Path(origin['path'])
    assert sha(raw_zip) == origin['sha256']
    with zipfile.ZipFile(raw_zip) as archive:
        assert archive.read(origin['zip_member']) == (sealed / 'sources/selected-job.log').read_bytes()
    for target, source in [('task.json', sealed/'task.json'), ('source-pom.xml', capture/'source-poms/pom.xml'),
                           ('official-ci.log', sealed/'sources/selected-job.log')]:
        (inputs / target).write_bytes(source.read_bytes())
    url = selection['official_ci_url']
    official = {key: archive_source(path, out, basis=key, official_url=url) for key, path in {
        'job_log': sealed/'sources/selected-job.log', 'run_log_zip': raw_zip,
        'selected_cell_index': Path(origin['selection_ref']['path']),
        'task_reference': sealed/'sources/prior-reference-task.json',
    }.items()}
    write(inputs/'ci/gson/index.json', {'schema_version':1, 'repo':task['repo'], 'sha':task['sha'],
          'ci_identity':selection['ci_identity'], 'selected_url':url, 'sources':official,
          'zip_member':origin['zip_member']})
    project = {'id':'gson', 'repo':task['repo'], 'commit':task['sha'],
        'task':{'path':'task.json','sha256':sha(inputs/'task.json')},
        'steps':[dict(task['steps'][0],stages=['build','test'])],
        'scope_note':selection['scope'], 'ci':{'selected_url':url,
            'selected_cell':selection['ci_identity']['job_name'], 'comparison_admitted':True,
            'modules':[m['id'] for m in json.loads((sealed/'ci-step-1-review-plan.draft.json').read_text())['modules']],
            'evidence_status':'exact_official_invocation_and_native_suite_counts',
            'evidence':[{'path':p,'sha256':sha(inputs/p)} for p in ('source-pom.xml','official-ci.log')]}}
    write(inputs/'manifest.json',{'projects':[project]})
    spec = build_project(project, inputs, out, ROOT)
    source_paths = {
        'effective_pom':capture/'effective-pom.xml', 'effective_settings':capture/'effective-settings.xml',
        'pom_log':capture/'effective-pom.log', 'settings_log':capture/'effective-settings.log',
        'runtime':capture/'runtime.txt', 'head':capture/'HEAD.txt',
        'tracked_diff':capture/'tracked-diff.txt', 'untracked':capture/'untracked.txt',
        'preparation_request':capture_root/'request-gson.json',
        'preparation_script':capture_root/state['preparation_script']['path'],
        'preparation_status':capture/'metadata-status.json', 'source_pom_index':capture/'source-poms/index.json',
        'official_ci':sealed/'sources/ci-step-1-ci-command.log',
    }
    sources = {name:archive_source(path,out,basis='selected_runtime_'+name) for name,path in source_paths.items()}
    discovery=out/'discovery-review'
    source_index=json.loads((sealed/'source-config-index.json').read_text())
    discovery_sources={key:archive_source(path,out,basis='reviewed_test_discovery') for key,path in {
        'git_tree':Path(source_index['tree_ref']['path']),
        'git_commit':ROOT/'output/java-benchmark-20260916/raw/repos/google/gson/head.json',
        'surefire_sources':discovery/'maven-surefire-plugin-3.5.6-sources.jar',
        'test_source':discovery/'ShrinkingIT.java',
        'compiler_bundle':discovery/'error_prone_core-2.50.0-with-dependencies.jar',
    }.items()}
    models = list(xml_root((capture/'effective-pom.xml').read_bytes()))
    workspace = state['steps'][0]['runtime_probe']['cwd']
    modules = []
    for model in models:
        module = text_at(model,'artifactId')
        directory = PurePosixPath(text_at(model,'build/directory')).relative_to(workspace).parent
        path = str(directory)
        source_path = str(directory/'pom.xml')
        indexed = [r for r in index['files'] if r['source_path'] == source_path]
        assert len(indexed) == 1 and indexed[0]['pinned_bytes_equal'] is True
        pinned = bound_file(capture_root,indexed[0])
        assert pinned.read_bytes() == (sealed/'sources/source-config'/source_path).read_bytes()
        modules.append({'id':module,'path':path,'packaging':text_at(model,'packaging') or 'jar',
                        'coordinates':{k:text_at(model,p) for k,p in [('group_id','groupId'),('artifact_id','artifactId'),('version','version')]},
                        'source_pom':archive_source(pinned,out,basis='pinned_source_pom')})
    assert len(modules) == 8
    module_map = {r['id']:r for r in modules}
    model_map = {text_at(m,'artifactId'):m for m in models}
    text = ci_native_text(source_paths['official_ci'].read_text())
    top,nested = ci_binding_inventory(text)
    assert len(top)==88 and nested==[]
    lines = text.splitlines()
    rows, by_key, bindings, counts, exceptions = [], {}, [], [], []

    def artifact(module, relative, role='main', classifier=None):
        return {'module':module,'path':relative,'role':role,'classifier':classifier,
                'format':'jar','extension':'jar','coordinates':module_map[module]['coordinates'],
                'evidence_tier':'declared'}

    def requirement(b,kind,subtype,rule='native_goal',artifacts=None):
        key = (b['module'],kind,subtype)
        if key not in by_key:
            row = {'id':'ci-step-1-'+b['module']+'-'+subtype.replace(':','-'),
                   'module':b['module'],'kind':kind,'subtype':subtype,
                   'depends_on':[], 'dependencies_complete':True,
                   'validation':{'rule':rule},'expectations':{}}
            if artifacts: row['expectations']['artifacts']=artifacts
            if rule=='junit':
                row['validation']['report_directories']=[str(PurePosixPath(module_map[b['module']]['path'])/'target'/('failsafe-reports' if subtype=='integration' else 'surefire-reports'))]
            by_key[key]=row; rows.append(row)
        elif artifacts:
            for a in artifacts:
                if a not in by_key[key]['expectations'].setdefault('artifacts',[]):
                    by_key[key]['expectations']['artifacts'].append(a)
        b.update(disposition='requirement',requirement_id=by_key[key]['id'],
                 reason='Mandatory selected invocation outcome, with module-specific native execution and declared output/report scope.')

    support = {'resources:resources','resources:testResources','resources:copy-resources',
               'templating:filter-sources','bnd:bnd-process','protobuf:generate-test'}
    noops = ['[INFO] No sources to compile','[INFO] No tests to run.',
             '[INFO] Auto-skipping goal because module skips install and/or deploy',
             '[INFO] Not executing Javadoc as the project is not a Java classpath-capable package']
    for i, original in enumerate(top):
        b = deepcopy(original); module=b['module']; goal=b['goal']
        end=top[i+1]['line']-1 if i+1<len(top) else len(lines)
        segment='\n'.join(lines[b['line']-1:end])
        disabled=next((x for x in segment.splitlines() if x.startswith('[INFO] Skipping') or x in noops),None)
        if disabled:
            b.update(disposition='ci_disabled',reason='The exact selected CI occurrence explicitly has no applicable output; it does not contribute a successful requirement.',
                     disabled_witness={'type':'native_log','text':disabled})
            exceptions.append({'module':module,'goal':goal,'witness':disabled,'line':b['line']})
        elif goal in {'enforcer:enforce','spotless:check'}:
            requirement(b,'quality_check',goal)
        elif goal in {'compiler:compile','compiler:testCompile'}:
            requirement(b,'compile','production_compile' if goal=='compiler:compile' else 'test_compile')
        elif goal=='surefire:test' and module=='test-shrinker':
            compiler=next(p for p in model_map[module].findall('build/plugins/plugin') if text_at(p,'artifactId')=='maven-compiler-plugin')
            b.update(disposition='ci_disabled',reason='Explicit source-bound default selection is empty; this is not a passed unit-test pool.',
                disabled_witness={'type':'source_empty_surefire_selection',
                    'effective_pom_sha256':sources['effective_pom']['sha256'],
                    'reviewed_by':'SAG offline source/CI selection review 2026-09-22',
                    'reason':'The complete exact-SHA test tree contains one source. Its public ShrinkingIT and nested TestAction binary names match none of Surefire3.5.6 source-defined defaults; actual CI compiles precisely one test source. No custom Surefire discovery, test selectors or lifecycle-generated source roots are configured.',
                    **{k:discovery_sources[k] for k in ('git_tree','git_commit','surefire_sources')},
                    'official_ci':sources['official_ci'],
                    'test_sources':[{**discovery_sources['test_source'],'source_path':'test-shrinker/src/test/java/com/google/gson/it/ShrinkingIT.java',
                        'declared_types':['ShrinkingIT','TestAction'],'reviewed_binary_names':['ShrinkingIT','ShrinkingIT$TestAction']}],
                    'compiler_discovery_review':{
                        'reason':'The bound compiler config uses only Error Prone2.50.0 for diagnostics on its annotationProcessorPath. Its official dependency bundle registers ErrorProneJavacPlugin and no javax.annotation.processing.Processor; no source-producing lifecycle goal precedes this native one-source compilation. This is a bounded review of this exact config, not a general Java source generator proof.',
                        'effective_plugin_sha256':hashlib.sha256(ET.tostring(compiler)).hexdigest(),
                        'coordinates':['com.google.errorprone','error_prone_core','2.50.0'],
                        'javac_plugin_class':'com.google.errorprone.ErrorProneJavacPlugin',
                        'dependency_bundle':discovery_sources['compiler_bundle']}})
            exceptions.append({'module':module,'goal':goal,'reason':b['reason'],'line':b['line']})
        elif goal=='failsafe:verify':
            requirement(b,'quality_check','integration_result_check','native_goal')
        elif goal in {'surefire:test','failsafe:integration-test'}:
            subtype='integration' if goal.startswith('failsafe:') else 'unit'
            requirement(b,'test',subtype,'junit')
            totals=re.findall(r'^\[(?:INFO|WARNING)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$',segment,re.M)
            assert len(totals)<=1
            if totals:
                reported,failed,errors,skipped=map(int,totals[0]);counts.append({'module':module,'goal':goal,'position':b['position'],
                    'reported':reported,'failed':failed,'errors':errors,'skipped':skipped,'assessed':reported-skipped,'passed':reported-skipped-failed-errors})
            else:
                raise AssertionError(('Selected test goal lacks its single official pool',b))
        elif goal in {'jar:jar','moditect:add-module-info','shade:shade'}:
            relative=str(PurePosixPath(module_map[module]['path'])/'target'/(text_at(model_map[module],'build/finalName')+'.jar'))
            requirement(b,'package','main_jar','artifact',[artifact(module,relative)])
        elif goal=='javadoc:jar':
            relative=str(PurePosixPath(module_map[module]['path'])/'target'/(text_at(model_map[module],'build/finalName')+'-javadoc.jar'))
            assert '/'+relative in segment
            requirement(b,'package','javadoc_jar','artifact',[artifact(module,relative,'javadoc','javadoc')])
            package_id=b.pop('requirement_id')
            requirement(b,'documentation','javadoc_generation','native_goal')
            b['requirement_ids']=[b.pop('requirement_id'),package_id]
        elif goal=='proguard:proguard' and module=='gson':
            b.update(disposition='support',reason='Transforms the explicitly selected enum test fixture before the mandatory Gson test pool; not a distribution artifact.')
        elif goal in {'proguard:proguard','exec:java'} and module=='test-shrinker':
            name='proguard-output' if goal=='proguard:proguard' else 'r8-output'
            relative='test-shrinker/target/'+name+'.jar'
            requirement(b,'package',name,'artifact',[artifact(module,relative,'test_fixture')])
        elif goal in support:
            b.update(disposition='support',reason='Upstream lifecycle input/resource preparation retained by the exact command; its consumer compilation, package or test is separately mandatory.')
        else:
            raise AssertionError(('Unreviewed native goal',b,segment))
        bindings.append(b)
    assert sum(c['reported'] for c in counts)==4906 and sum(c['skipped'] for c in counts)==22 and len(counts)==7
    # Dependencies follow each module's selected lifecycle, not reactor-wide
    # linearization. Repeated native events remain in the same logical row.
    for row in rows:
        module=row['module']; subtype=row['subtype']; deps=[]
        def depend(kind,sub):
            dep=by_key.get((module,kind,sub))
            if dep and dep is not row and dep['id'] not in deps: deps.append(dep['id'])
        if subtype!='enforcer:enforce': depend('quality_check','enforcer:enforce')
        if row['kind'] in {'compile','package','documentation'}: depend('compile','production_compile')
        if row['kind']=='test': depend('compile','test_compile')
        if subtype=='integration':
            for sub in ('main_jar','proguard-output','r8-output'):depend('package',sub)
        if subtype=='integration_result_check':depend('test','integration')
        row['depends_on']=deps
    declarations=[]
    observed={(b['module'],b['goal'],b['execution'],b['version']) for b in top}
    for module,model in model_map.items():
        for row in pom_execution_bindings(model):
            row['module']=module
            if (module,row['goal'],row['execution'],row['version']) in observed:
                row.update(disposition='observed',reason='Exact effective model version and execution occurs in the selected official command.')
            else:
                assert row['phase'] in {'clean','install','deploy','site','site-deploy'},row
                row.update(disposition='outside_task_lifecycle',reason='Separate clean/install/deploy/site lifecycle not requested by verify javadoc:jar.')
            declarations.append(row)
    review={'schema_version':1,'review_protocol':'multi-module-maven-v1','project_id':'gson',
        'commit':task['sha'],'task_sha256':task_digest(task),'source_paths_relative_to':'requirements_bundle_root',
        'metadata_workspace':workspace,'reviewed_by':'SAG pilot source-bound offline review 2026-09-22',
        'review_notes':[
            'One original Maven command, eight effective models, all 88 top-level selected CI bindings and all model execution declarations; no module or goal removed.',
            'Gson contains separate Surefire unit and Failsafe integration pools. Shrinker has only the Failsafe report pool; complete pinned source/default-selector/one-source-native-compilation review establishes its empty Surefire selection, without a passed unit-test obligation. Failsafe verify is a separate native result check, not a second JUnit pool.',
            'Native-image profile is not active. The Graal-named module uses ordinary JVM tests and explicitly has no main sources/JAR. R8 is invoked with --classfile and its JAR is consumed by integration tests; no Android SDK/DEX task is introduced.',
            'ModiTect and Shade rewrite their existing main JAR. Their native bindings remain attached to the same artifact obligation. ProGuard/R8 custom test JARs and the three actual Javadoc JARs are explicitly required.',
            'Buildinfo is explicitly auto-skipped upstream for all modules. Parent/test-module Javadoc and no-input compiler/test goals are recorded as inapplicable, never passed.',
            'Metadata uses selected Java21 and Maven3.9.16; OpenJDK metadata vendor differs from official Temurin. MAVEN_ARGS formatting-only switches in official CI do not change task scope. No runtime/vendor parity or actual replay is claimed.',
            'Definition readiness is distinct from replay, testcase identity comparability and shared CI-score availability. No CI cache is shared with an agent.',
        ],'unresolved_obligations':[], 'sources':sources,'modules':modules,
        'ci_source':{'kind':'github-actions-command-v1','raw_job':official['job_log'],
                     'start_line':inv['full_job_start_line'],'end_line_exclusive':inv['full_job_end_line_exclusive']},
        'ci_bindings':bindings,'nested_ci_bindings':[],'pom_bindings':declarations,'requirements':rows}
    write(out/'review.json',review)
    spec=apply_reviewed_plan(spec,task,out/'review.json',out)
    spec=attach_preparation_inventory(spec,task,capture_root,out)
    supplemental={name:archive_source(path,out,basis=name) for name,path in {
        'source_config_index':sealed/'source-config-index.json','native_review_inventory':sealed/'native-review-inventory.json',
        'launcher_inputs_index':capture/'launcher-inputs/index.json',
        'tool_source_download_receipts':discovery/'download-receipts.json',
        'surefire_download_headers':discovery/'surefire-sources.headers',
        'error_prone_download_headers':discovery/'error-prone.headers',
    }.items()}
    spec['pilot_review']={'official_sources':official,'supplemental_sources':supplemental,
        'native_test_pools':counts,'inapplicable_native_goals':exceptions,'runtime_vendor_equality_claimed':False,
        'source_configuration_files':[archive_source(p,out,basis='pinned_build_configuration') for p in sorted((sealed/'sources/source-config').rglob('*')) if p.is_file()]}
    spec['ci_alignment']['test_count_semantics']=unavailable('Archived GitHub Actions native suite totals are descriptive. Shared typed CI-score import currently supports Jenkins testReport only; no untyped legacy denominator is promoted.',sources=official,selected_url=url,selected_cell=selection['ci_identity']['job_name'])
    write(out/'requirements.json',spec)
    load_requirements(out/'requirements.json',task)
    assert (out/'task.json').read_bytes()==task_bytes
    report={'project_id':'gson','status':'definition_ready_for_reference_replay',
        'task_sha256':task_digest(task),'requirements_sha256':canonical_digest(spec),
        'requirements_file_sha256':sha(out/'requirements.json'),'modules':len(modules),'requirements':len(rows),
        'ci_top_bindings':len(top),'pom_bindings':len(declarations),'requirement_categories':dict(Counter(r['kind'] for r in rows)),
        'native_test_pools':counts,'native_reported':4906,'native_assessed':4884,'native_skipped':22,
        'reference_replay_performed':False,'agent_run_performed':False,'ci_score_reference_status':'unavailable'}
    write(out/'verification.json',report)
    (out/'README.md').write_text('# Gson selected-task requirements review\n\n'+ '\n\n'.join(review['review_notes'])+'\n\n'+
        f'{len(rows)} mandatory requirements; 8 modules; 88 top-level CI goal occurrences. Seven distinct test pools report 4,906 records, 22 skipped and 4,884 assessed; these counts do not establish testcase identity equality.\n')
    return report


if __name__=='__main__':
    print(json.dumps(prepare(),indent=2))
