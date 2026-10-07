import json
import pytest
from fastapi.testclient import TestClient

from scripts.benchmark_model_gateway import create_app, prepare_request, usage_from_response


def test_model_cannot_fall_back_or_escape_frozen_arm():
    for model in ('claude-sonnet-5', 'gpt-4o', None):
        with pytest.raises(ValueError, match='only'):
            prepare_request('/v1/responses', {'model': model}, 'claude')
    _, body = prepare_request('/v1/responses', {'model': 'gpt-5.4-mini', 'reasoning': {'effort': 'low'}}, 'opencode')
    assert body['reasoning']['effort'] == 'none'
    assert body['store'] is False


def test_anthropic_tool_round_trip_to_provider_request():
    path, body = prepare_request('/v1/messages', {
        'model': 'gpt-5.4-mini', 'max_tokens': 8192,
        'system': 'set up this Java project',
        'messages': [
            {'role': 'user', 'content': 'run build'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'mvn test'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'BUILD FAILURE'}]},
        ],
        'tools': [{'name': 'Bash', 'description': 'run shell', 'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}],
        'thinking': {'type': 'enabled', 'budget_tokens': 20000}, 'stream': True,
    }, 'claude')
    assert path == '/responses'
    assert body['reasoning'] == {'effort': 'none'}
    assert body['stream'] is False
    assert any(x.get('type') == 'function_call_output' and x['output'] == 'BUILD FAILURE' for x in body['input'])
    assert body['tools'][0]['name'] == 'Bash'


def test_usage_does_not_subtract_cached_input_or_double_count_reasoning():
    obj = {'id': 'r1', 'model': 'gpt-5.4-mini', 'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
          'input_tokens_details': {'cached_tokens': 90}, 'output_tokens_details': {'reasoning_tokens': 15}}}
    usage = usage_from_response(json.dumps(obj).encode())
    assert usage['total_tokens'] == 120
    sse = ('data: ' + json.dumps({'type': 'response.completed', 'response': obj}) + '\n\n').encode()
    assert usage_from_response(sse, streaming=True) == usage
    obj['usage']['total_tokens'] = 99
    assert usage_from_response(json.dumps(obj).encode()) is None
    assert usage_from_response(b'{}') is None


def test_gateway_does_not_invent_native_sag_tool_reasoning():
    original={'model':'gpt-5.4-mini','temperature':0.0,'tools':[{'type':'function','function':{'name':'x'}}]}
    path,body=prepare_request('/v1/chat/completions',original,'sag')
    assert path=='/chat/completions'
    assert 'reasoning_effort' not in body
    assert body['temperature']==0.0
    _,advisor=prepare_request('/v1/chat/completions',{'model':'gpt-5.4-mini','reasoning_effort':'high'},'sag')
    assert advisor['reasoning_effort']=='high'


def test_route_rejects_wrong_credentials_and_model_before_provider(tmp_path):
    app = create_app(directory=tmp_path/'ledger', run_id='qualification', arm='claude',
                     token='test-token', api_key='test-key-not-real', api_base='https://invalid.example/v1')
    with TestClient(app) as client:
        assert client.get('/health').json()['ready'] is True
        assert client.post('/v1/messages', json={'model': 'gpt-5.4-mini'}).status_code == 401
        assert client.post('/v1/messages', headers={'x-api-key': 'test-token'}, json={'model': 'gpt-4o'}).status_code == 400
    assert not list((tmp_path/'ledger').glob('*/started.json'))
    assert json.loads((tmp_path/'ledger/close.json').read_text())['unfinished_requests'] == []
