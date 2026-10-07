import json

from scripts.benchmark_gateway_usage import read_usage


def ledger(path):
    path.mkdir()
    def save(name, value):
        target = path/name
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(value))
    save('start.json', {'run_id': 'r1', 'started_at': '2026-09-23T10:00:00+00:00'})
    save('close.json', {'run_id': 'r1', 'closed_at': '2026-09-23T11:00:00+00:00', 'unfinished_requests': []})
    save('call1/started.json', {'id': 'call1', 'run_id': 'r1', 'at': '2026-09-23T10:01:00+00:00'})
    save('call1/finished.json', {'at': '2026-09-23T10:02:00+00:00', 'usage': {
        'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120, 'resolved_model': 'gpt-5.4-mini-2026-03-17'}})
    return save


def test_complete_actual_usage_inside_declared_process_window(tmp_path):
    directory=tmp_path/'gateway'; ledger(directory)
    result=read_usage(directory, 'r1', inference_started_at='2026-09-23T10:00:01+00:00',
                      inference_finished_at='2026-09-23T10:03:00+00:00')
    assert result['model_calls_complete'] is True
    assert result['known_tokens']==120


def test_child_request_after_premature_parent_exit_invalidates_accounting(tmp_path):
    directory=tmp_path/'gateway'; ledger(directory)
    result=read_usage(directory, 'r1', inference_started_at='2026-09-23T10:00:01+00:00',
                      inference_finished_at='2026-09-23T10:00:02+00:00')
    assert result['model_calls_complete'] is False
    assert result['missing_usage_request_ids']==[]
    assert result['outside_inference_window_request_ids']==['call1']
    assert result['known_tokens']==120  # Retain observed bills, even for an invalid run interval.
    assert 'inference window' in result['usage_errors'][0]


def test_missing_provider_usage_never_becomes_free_retry(tmp_path):
    directory=tmp_path/'gateway';save=ledger(directory)
    save('call2/started.json', {'id': 'call2', 'run_id': 'r1', 'at': '2026-09-23T10:04:00+00:00'})
    save('call2/finished.json', {'at': '2026-09-23T10:05:00+00:00', 'usage': None, 'status': 'transport_error'})
    result=read_usage(directory,'r1')
    assert result['model_calls_complete'] is False
    assert result['known_tokens']==120
    assert result['request_counts']=={'started':2,'with_usage':1,'missing_usage':1}


def test_unclosed_empty_or_foreign_ledger_is_not_complete(tmp_path):
    assert read_usage(tmp_path/'absent','r1')['model_calls_complete'] is False
    directory=tmp_path/'gateway';ledger(directory)
    assert read_usage(directory,'r2')['model_calls_complete'] is False
    (directory/'close.json').unlink()
    assert read_usage(directory,'r1')['model_calls_complete'] is False
