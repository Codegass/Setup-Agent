"""Bounded one-program RPC. Dispatch remains owned by the SAG engine.

The optional code tool uses this transport. An explicit process command lets
the same transport be checked without Docker or a model.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MAX_EVENT_BYTES = 96 * 1024 * 1024


def run_program(
    command: Sequence[str],
    contract: Mapping[str, Any],
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    record: Callable[[dict[str, Any]], None],
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Run once, recording requests and actual host answers even after VM exit.

    The reader can notice VM termination while a synchronous tool is running.
    That tool retains its existing owner and evidence; no later queued call is
    dispatched. No entire-program retry is performed here.
    """
    program_id = contract['program_id']
    allowed = {row['name'] for row in contract['tools']}
    deadline = time.monotonic() + contract['timeout_ms'] / 1000 + 5
    process = subprocess.Popen(
        list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=dict(env),
    )
    events: queue.Queue[dict[str, Any]] = queue.Queue()
    ended = threading.Event()
    stderr = bytearray()
    requested: dict[int, dict[str, Any]] = {}
    answered: set[int] = set()
    terminal: dict[str, Any] | None = None
    protocol_error = None

    def read_stdout():
        try:
            while True:
                line = process.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b'\n'):
                    raise ValueError('Worker event exceeded the byte limit or was incomplete')
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('program_id') != program_id:
                    raise ValueError('Worker event has the wrong program identity')
                if event.get('type') in {'done', 'error'}:
                    ended.set()
                events.put(event)
        except Exception as exc:
            events.put({'type': 'protocol_error', 'program_id': program_id, 'error': str(exc)})
        finally:
            ended.set()
            events.put({'type': 'eof', 'program_id': program_id})

    def read_stderr():
        while block := process.stderr.read(4096):
            stderr.extend(block)
            if len(stderr) > 8192:
                del stderr[:-8192]

    def send(value):
        data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        process.stdin.write(data)
        process.stdin.flush()

    threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        send(dict(contract))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Worker did not terminate within its wall budget')
            event = events.get(timeout=remaining)
            kind = event.get('type')
            record({'source': 'worker', **event})
            if kind == 'eof':
                break
            if kind == 'protocol_error':
                raise ValueError(event['error'])
            if kind == 'requested':
                ident = event.get('id')
                if (type(ident) is not int or ident != len(requested) + 1
                        or ident > contract['max_calls'] or event.get('name') not in allowed
                        or not isinstance(event.get('args'), dict)):
                    raise ValueError('Invalid child request')
                requested[ident] = event
            elif kind == 'call':
                ident = event.get('id')
                original = requested.get(ident)
                if (not original or ident in answered
                        or event.get('name') != original['name'] or event.get('args') != original['args']):
                    raise ValueError('Dispatch does not bind a unique child request')
                answered.add(ident)
                if ended.is_set():
                    record({'source': 'host', 'type': 'not_executed', 'program_id': program_id,
                            'id': ident, 'reason': 'VM already terminated'})
                    continue
                answer = dispatch(event)
                record({'source': 'host', 'type': 'result', 'program_id': program_id, 'id': ident,
                        **{k: v for k, v in answer.items() if k != 'value'}})
                reply = {'type': 'result', 'program_id': program_id, 'id': ident,
                         **{k: v for k, v in answer.items() if k != 'audit'}}
                try:
                    send(reply)
                except BrokenPipeError:
                    # The exact host result above is retained. Worker status
                    # can describe cancellation, but cannot erase that result.
                    ended.set()
            elif kind in {'done', 'error'}:
                if terminal is not None:
                    raise ValueError('Duplicate terminal event')
                terminal = event
            elif kind not in {'ready', 'cancel'}:
                raise ValueError(f'Unknown worker event: {kind!r}')
    except (Exception, KeyboardInterrupt) as exc:
        protocol_error = f'{type(exc).__name__}: {exc}'
        record({'source': 'host', 'type': 'transport_error', 'program_id': program_id,
                'error': protocol_error})
        try:
            send({'type': 'stop', 'program_id': program_id, 'reason': protocol_error})
        except (BrokenPipeError, OSError):
            pass
        if isinstance(exc, KeyboardInterrupt):
            raise
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
    return {'terminal': terminal, 'transport_error': protocol_error,
            'exit_code': process.returncode, 'stderr_tail': stderr.decode(errors='replace')}
