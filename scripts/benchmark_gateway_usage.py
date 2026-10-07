"""Read complete or partial provider usage from the experiment gateway archive."""
import json
from datetime import datetime
from pathlib import Path


def timestamp(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError('Ledger timestamp has no timezone')
    return value


def read_usage(directory, run_id, *, inference_started_at=None, inference_finished_at=None):
    directory = Path(directory)
    calls, missing, errors, outside_window = [], [], [], []
    read = lambda path: json.loads(path.read_bytes())
    lower = upper = None
    try:
        start, closed = read(directory / 'start.json'), read(directory / 'close.json')
        if start['run_id'] != run_id or closed['run_id'] != run_id:
            raise ValueError('Gateway ledger belongs to another run')
        lower, upper = timestamp(start['started_at']), timestamp(closed['closed_at'])
        if lower > upper or not isinstance(closed.get('unfinished_requests'), list):
            raise ValueError('Gateway lifecycle is invalid')
        if inference_started_at is not None or inference_finished_at is not None:
            begin, end = timestamp(inference_started_at), timestamp(inference_finished_at)
            if not lower <= begin <= end <= upper:
                raise ValueError('Inference window is outside the gateway lifecycle')
            lower, upper = begin, end
        complete = not closed['unfinished_requests']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc)); complete = False
    folders = sorted(path for path in directory.iterdir() if path.is_dir()) if directory.is_dir() else []
    for path in folders:
        try:
            requested = read(path / 'started.json')
            if requested['run_id'] != run_id or requested['id'] != path.name:
                raise ValueError('Request identity mismatch')
            response = read(path / 'finished.json')
            requested_at, returned_at = timestamp(requested['at']), timestamp(response['at'])
            if requested_at > returned_at or lower is not None and requested_at < lower or upper is not None and returned_at > upper:
                complete = False
                outside_window.append(path.name)
                errors.append('Provider request lies outside the declared inference window')
            usage = response.get('usage')
            if not isinstance(usage, dict):
                raise ValueError('Provider usage is unavailable')
            a, b = usage['input_tokens'], usage['output_tokens']
            if type(a) is not int or type(b) is not int or min(a, b) < 0 or usage['total_tokens'] != a + b:
                raise ValueError('Provider token counts disagree')
            if not str(usage.get('resolved_model', '')).startswith('gpt-5.4-mini'):
                raise ValueError('Unexpected resolved provider model')
            calls.append({'role': requested.get('role', 'actor'), 'request_id': path.name, 'input_tokens': a, 'output_tokens': b,
                          'model': usage['resolved_model'], 'source': str(path.relative_to(directory) / 'finished.json')})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            complete = False; missing.append(path.name); errors.append(str(exc))
    return {'model_calls': calls, 'model_calls_complete': complete,
            'known_tokens': sum(c['input_tokens'] + c['output_tokens'] for c in calls),
            'request_counts': {'started': len(folders), 'with_usage': len(calls), 'missing_usage': len(missing)},
            'missing_usage_request_ids': missing, 'usage_errors': errors,
            'outside_inference_window_request_ids': outside_window,
            'usage_limitations': [] if complete else ['At least one provider request or ledger boundary lacks complete usage.'],
            'token_accounting': 'provider input plus output; cached input and reasoning each included once'}
