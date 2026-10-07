"""Reports must not improve results by dropping unknowns or combining boundaries."""
from copy import deepcopy

import pytest

from sag.benchmark.artifact_inventory import capture, inventory_plan
from sag.benchmark.measurement_summary import module_measurements, output_counts, test_record_counts as record_counts
from sag.benchmark.requirements import evaluation_identity
from test_benchmark_artifact_inventory import spec as inventory_spec, output
from test_benchmark_requirement_evaluator import fixture


def test_agent_and_acceptance_outputs_are_distinct_observations(tmp_path):
    plan = inventory_plan(inventory_spec())
    output(tmp_path, 'target/classes/Agent.class')
    agent = capture(tmp_path, plan, run_id='one', boundary='agent_exit_before_acceptance')
    output(tmp_path, 'target/classes/Verifier.class')
    verifier = capture(tmp_path, plan, run_id='one', boundary='after', invocation_id='inv')
    assert output_counts(agent, run_id='one', boundary='agent_exit_before_acceptance')['counts']['class:main'] == 1
    assert output_counts(verifier, run_id='one', boundary='after')['counts']['class:main'] == 2
    with pytest.raises(ValueError, match='boundary'):
        output_counts(verifier, run_id='one', boundary='agent_exit_before_acceptance')


def test_missing_or_partial_inventory_is_not_zero(tmp_path):
    assert output_counts(None, run_id='one', boundary='after')['counts'] is None
    inv = capture(tmp_path, inventory_plan(inventory_spec()), run_id='one', boundary='after')
    assert output_counts(inv, run_id='one', boundary='after')['counts']['jar:main'] == 0
    inv.update(status='partial', errors=[{'reason': 'unreadable'}])
    summary = output_counts(inv, run_id='one', boundary='after')
    assert summary['counts'] is None and summary['observed_counts']['jar:main'] == 0


def test_duplicate_output_rows_rejected(tmp_path):
    output(tmp_path, 'target/classes/Agent.class')
    inv = capture(tmp_path, inventory_plan(inventory_spec()), run_id='one', boundary='after')
    inv['files'] *= 2
    with pytest.raises(ValueError, match='duplicates'):
        output_counts(inv, run_id='one', boundary='after')


def test_ran_excludes_skips_and_unknown_pool_keeps_total_unknown():
    spec = {'requirements': [
        {'id': 'a', 'kind': 'test', 'validation': {'rule': 'junit', 'report_directories': ['a/reports']}},
        {'id': 'b', 'kind': 'test', 'validation': {'rule': 'junit', 'report_directories': ['b/reports']}},
    ]}
    score = {'requirements': [{'id': 'a', 'invocation_id': 'inv', 'test_counts':
                              dict(reported=10, passed=5, failed=1, errors=1, skipped=3, assessed=7)}]}
    summary = record_counts(spec, score)
    assert summary['counts'] is None and summary['observed_counts']['ran'] == 7
    assert summary['missing_requirements'] == ['b']
    score['requirements'].append({'id': 'b', 'invocation_id': 'inv', 'test_counts':
                                  dict(reported=1, passed=1, failed=0, errors=0, skipped=0, assessed=1)})
    assert record_counts(spec, score)['counts']['ran'] == 8
    spec['requirements'][1]['validation']['report_directories'] = ['a/reports']
    with pytest.raises(ValueError, match='more than once'):
        record_counts(spec, score)


def test_native_smoke_check_does_not_invent_a_test_case():
    spec = {'requirements': [{'id': 'native', 'kind': 'test', 'validation': {'rule': 'native_exit'}}]}
    summary = record_counts(spec, {'requirements': [{'id': 'native', 'status': 'passed'}]})
    assert summary['status'] == 'not_applicable' and summary['counts'] is None
    assert summary['non_counted_test_requirements'] == ['native']


def test_maven_default_pool_and_other_test_units_remain_separate():
    spec = {'steps': [{'step_id': 'verify', 'runner': 'maven'}], 'requirements': [
        {'id': 'unit', 'kind': 'test', 'step_id': 'verify', 'scope': {'module_path': 'child'},
         'validation': {'rule': 'junit'}},
        {'id': 'it-project', 'kind': 'test', 'validation': {'rule': 'maven_invoker'}},
    ]}
    score = {'requirements': [
        {'id': 'unit', 'invocation_id': 'one', 'test_counts':
         dict(reported=2, passed=2, failed=0, errors=0, skipped=0, assessed=2)},
        {'id': 'it-project', 'status': 'passed', 'native_test_counts':
         dict(unit='integration_project', reported=10, passed=10, failed=0, errors=0, skipped=0)},
    ]}
    summary = record_counts(spec, score)
    assert summary['counts']['ran'] == 2
    assert summary['other_test_observations'][0]['counts']['passed'] == 10


def test_module_counts_preserve_declared_parent_and_runtime_gate(fixture):
    _, spec, _, _, _, score = fixture
    original = score()
    spec['steps'] = [{'step_id': 'verify', 'modules': [{'id': 'demo', 'path': '.'}, {'id': 'parent', 'path': 'parent'}]}]
    original['evaluation_identity'] = evaluation_identity(spec)
    result = module_measurements(spec, original)
    assert result['declared'] == 2 and result['without_declared_obligations'] == 1
    assert result['required_outcomes_complete'] == 1
    original['preconditions'][0]['status'] = 'unavailable'
    assert module_measurements(spec, original)['required_outcomes_complete'] == 0
    original['evaluation_identity']['requirements_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='another frozen'):
        module_measurements(spec, original)


def test_module_missing_result_and_incomplete_definition_cannot_be_complete(fixture):
    _, spec, _, _, _, score = fixture
    original = score()
    spec['steps'] = [{'step_id': 'verify', 'modules': [{'id': 'demo', 'path': '.'}]}]
    original['evaluation_identity'] = evaluation_identity(spec)
    missing = deepcopy(original); missing['requirements'].pop()
    result = module_measurements(spec, missing)
    assert result['modules'][0]['groups']['test']['unavailable'] == 1
    assert result['required_outcomes_complete'] == 0
    spec['annotation_completeness']['status'] = 'review_required'
    original['evaluation_identity'] = evaluation_identity(spec)
    assert module_measurements(spec, original)['required_outcomes_complete'] == 0
