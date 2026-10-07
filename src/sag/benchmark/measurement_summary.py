"""Descriptive measurements from one sealed task, without changing its verdict.

The caller verifies archive hashes before loading these objects. Agent-exit and
acceptance inventories must be passed separately; they are never added together.
"""
from collections import Counter
from pathlib import PurePosixPath

from .requirements import BUILD_KINDS, evaluation_identity, report_directories, task_status


OUTPUT_KEYS = (
    "class:main", "class:test", "class:unclassified", "class:invalid",
    "jar:main", "jar:tests", "jar:sources", "jar:test_sources",
    "jar:javadoc", "jar:unclassified", "jar:invalid", "war:main",
    "war:unclassified", "war:invalid",
)
TEST_KEYS = ("reported", "passed", "failed", "errors", "skipped", "assessed")
COUNTED_TEST_RULES = {"junit"}


def output_counts(inventory, *, run_id, boundary, module_path=None):
    """Zero requires a complete, matching observation. Partial counts are lower bounds."""
    result = {"status": "unavailable", "counts": None, "observed_counts": None}
    if not inventory:
        return {**result, "reason": "output_inventory_missing"}
    if inventory.get("run_id") != run_id or inventory.get("boundary") != boundary:
        raise ValueError("Output inventory belongs to another run or boundary")
    if inventory.get("status") not in {"complete", "partial"}:
        return {**result, "reason": "output_inventory_unavailable"}
    files = inventory.get("files")
    if not isinstance(files, list) or len({f['path'] for f in files}) != len(files):
        raise ValueError("Output inventory is malformed or duplicates filesystem paths")
    if module_path is not None:
        declared = {m['path'] for m in inventory.get('scope', {}).get('modules', [])}
        if module_path not in declared:
            return {**result, "reason": "module_not_in_inventory_scope"}
        files = [f for f in files if f.get('module_path') == module_path]
    counts = Counter(f["kind"] + ":" + (f["role"] if f["format_valid"] else "invalid")
                     for f in files)
    counts = {k: counts[k] for k in sorted(set(OUTPUT_KEYS) | counts.keys())}
    complete = inventory['status'] == 'complete' and inventory.get('errors') == []
    return {'status': 'complete' if complete else 'partial',
            'counts': counts if complete else None, 'observed_counts': counts,
            'errors': inventory.get('errors'),
            'semantics': 'Files present at this boundary; not source coverage or fresh-build proof.'}


def test_record_counts(spec, score):
    """Count records in distinct required executions; never call this unique cases."""
    rows = {r['id']: r for r in score.get('requirements', [])}
    if len(rows) != len(score.get('requirements', [])):
        raise ValueError('Duplicate requirement results')
    runners = {s['step_id']: s['runner'] for s in spec.get('steps', [])}
    countable = [r for r in spec['requirements'] if r['kind'] == 'test'
                 and r.get('validation', {}).get('rule') in COUNTED_TEST_RULES]
    total = Counter({k: 0 for k in TEST_KEYS})
    observed, missing, seen = [], [], set()
    for requirement in countable:
        row = rows.get(requirement['id'], {})
        counts = row.get('test_counts')
        if counts is None:
            missing.append(requirement['id'])
            continue
        if (not isinstance(counts, dict)
                or any(type(counts.get(k)) is not int or counts[k] < 0 for k in TEST_KEYS)
                or counts['reported'] != sum(counts[k] for k in ('passed', 'failed', 'errors', 'skipped'))
                or counts['assessed'] != counts['passed'] + counts['failed'] + counts['errors']):
            raise ValueError('Test record counts are inconsistent')
        # Requirements referring to the same report pool cannot each add its
        # total. Multiple goals combined in one requirement are counted once.
        directories = report_directories(requirement)
        if directories is None and runners.get(requirement.get('step_id')) == 'maven':
            module_path = requirement.get('scope', {}).get('module_path')
            subtype = requirement.get('subtype', 'unit')
            if module_path and subtype in {'unit', 'integration'}:
                directory = 'surefire-reports' if subtype == 'unit' else 'failsafe-reports'
                directories = [str(PurePosixPath(module_path) / 'target' / directory)]
        if not row.get('invocation_id') or not directories:
            missing.append(requirement['id'])
            continue
        for directory in directories:
            producer=row.get('native_report_producer', {})
            identity=(producer.get('trace_sha256'),producer.get('mojo_id')) if producer else None
            key = (row['invocation_id'], directory, identity)
            if any(previous == key or previous[0] == key[0] and previous[2] == key[2] and
                   (previous[1].startswith(directory + '/') or directory.startswith(previous[1] + '/'))
                   for previous in seen):
                raise ValueError('Test report pool would be counted more than once')
            seen.add(key)
        total.update({k: counts[k] for k in TEST_KEYS})
        observed.append(requirement['id'])
    result = dict(total)
    result['ran'] = result['passed'] + result['failed'] + result['errors']
    return {'status': 'not_applicable' if not countable else 'complete' if not missing else 'partial' if observed else 'unavailable',
            'counts': result if countable and not missing else None,
            'observed_counts': result if observed else None,
            'countable_requirements': len(countable), 'observed_requirements': observed,
            'missing_requirements': missing,
            'non_counted_test_requirements': [r['id'] for r in spec['requirements'] if r['kind'] == 'test' and r not in countable],
            'other_test_observations': [{'requirement_id': r['id'],
                'status': rows.get(r['id'], {}).get('status', 'unavailable'),
                'counts': rows.get(r['id'], {}).get('native_test_counts')}
                for r in spec['requirements'] if r['kind'] == 'test' and r not in countable],
            'semantics': 'Required execution records; skipped excluded from ran. Not unique test identities across executions.'}


def module_measurements(spec, score):
    """One module/path row across task steps, retaining the whole denominator."""
    expected = evaluation_identity(spec)
    if any(score.get('evaluation_identity', {}).get(k) != v for k, v in expected.items()):
        raise ValueError('Score belongs to another frozen definition')
    observed = {r['id']: r for r in score.get('requirements', [])}
    if len(observed) != len(score.get('requirements', [])):
        raise ValueError('Duplicate requirement results')
    if not set(observed) <= {r['id'] for r in spec['requirements']}:
        raise ValueError('Unplanned requirement results')
    modules = {(m['id'], m['path']) for s in spec['steps'] for m in s['modules']}
    preconditions = {r['id']: r['status'] for r in score.get('preconditions', [])}
    gates = [preconditions.get(r['id'], 'unavailable') for r in spec['preconditions']]
    complete_definition = spec.get('annotation_completeness', {}).get('status') == 'complete'
    result = []
    for module, path in sorted(modules, key=lambda m: (m[1], m[0])):
        definitions = [r for r in spec['requirements'] if r.get('module') == module
                       and r.get('scope', {}).get('module_path') == path]
        statuses = [observed.get(r['id'], {}).get('status', 'unavailable') for r in definitions]
        state = task_status(statuses + gates) if definitions else 'not_applicable'
        if not complete_definition and state in {'complete', 'not_applicable'}:
            state = 'unavailable'
        grouped = {}
        for name, kinds in [('build', BUILD_KINDS), ('test', {'test'})]:
            ids = [r['id'] for r in definitions if r['kind'] in kinds]
            counts = Counter(observed.get(i, {}).get('status', 'unavailable') for i in ids)
            grouped[name] = {'declared': len(ids), **{s: counts[s] for s in ('passed', 'failed', 'not_run', 'unavailable')}}
        result.append({'module': module, 'module_path': path,
            'required_outcomes_status': state, 'declared_requirements': len(definitions),
            'passed_requirements': sum(s == 'passed' for s in statuses),
            'requirement_ids': [r['id'] for r in definitions], 'groups': grouped})
    return {'declared': len(modules),
            'required_outcomes_complete': sum(m['required_outcomes_status'] == 'complete' for m in result),
            'without_declared_obligations': sum(m['declared_requirements'] == 0 for m in result),
            'modules': result,
            'semantics': 'Module requirement outcomes with task preconditions. Overall command completion and task verdict remain separate.'}
