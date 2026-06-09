"""Phase 4.2 — monitor-thread timeout enforcement survives stream read timeouts.

The docker exec output stream raising a socket read timeout used to jump to
the generic exception handler, abandoning the absolute/silent timeout
enforcement entirely while the container process kept running (a 1200s-capped
gradle build ran ~20000s). These tests pin the fixed behavior.
"""

import time
from types import SimpleNamespace

from sag.docker_orch.orch import DockerOrchestrator


class ReadTimeoutLike(Exception):
    """Mimics requests/urllib3 read-timeout error text."""

    def __init__(self):
        super().__init__("UnixHTTPConnectionPool: Read timed out. (read timeout=60)")


class ScriptedStream:
    """Resumable iterator: yields chunks or raises scripted exceptions."""

    def __init__(self, events):
        self._events = list(events)
        self._index = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._events):
            raise StopIteration
        event = self._events[self._index]
        self._index += 1
        if isinstance(event, Exception):
            raise event
        return event


class AlwaysReadTimeoutStream:
    def __iter__(self):
        return self

    def __next__(self):
        raise ReadTimeoutLike()


def build_orchestrator():
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.container_name = "sag-demo"
    orchestrator.terminate_calls = []
    orchestrator._terminate_container_processes = lambda: orchestrator.terminate_calls.append(1)
    return orchestrator


def fresh_state(start_offset=0.0, **extra):
    now = time.time()
    state = {
        "last_output_time": now,
        "start_time": now - start_offset,
        "total_output": "",
        "process_terminated": False,
        "termination_reason": None,
        "cpu_warnings": 0,
        "blind_poll_interval": 0.01,
    }
    state.update(extra)
    return state


def test_read_timeout_then_more_output_continues_monitoring():
    orchestrator = build_orchestrator()
    stream = ScriptedStream(
        [
            (b"hello ", None),
            ReadTimeoutLike(),
            (b"world", None),
        ]
    )
    exec_result = SimpleNamespace(output=stream, exit_code=0)

    result = orchestrator._monitor_execution_with_timeouts(
        exec_result, fresh_state(), silent_timeout=60, absolute_timeout=60
    )

    assert result["success"] is True
    assert "hello" in result["output"] and "world" in result["output"]
    assert result["termination_reason"] is None
    assert orchestrator.terminate_calls == []


def test_repeated_read_timeouts_still_hit_absolute_timeout():
    orchestrator = build_orchestrator()
    exec_result = SimpleNamespace(output=AlwaysReadTimeoutStream(), exit_code=None)

    result = orchestrator._monitor_execution_with_timeouts(
        exec_result,
        fresh_state(start_offset=10.0),
        silent_timeout=100,
        absolute_timeout=5,
    )

    assert result["termination_reason"] == "absolute_timeout"
    assert result["success"] is False
    assert orchestrator.terminate_calls, "runaway process must be terminated"


def test_repeated_read_timeouts_still_hit_silent_timeout():
    orchestrator = build_orchestrator()
    exec_result = SimpleNamespace(output=AlwaysReadTimeoutStream(), exit_code=None)

    result = orchestrator._monitor_execution_with_timeouts(
        exec_result,
        fresh_state(),
        silent_timeout=0,
        absolute_timeout=100,
    )

    assert result["termination_reason"] == "silent_timeout"
    assert orchestrator.terminate_calls


def test_stream_death_after_read_timeout_enters_blind_enforcement():
    """Generator dies (StopIteration) right after a read timeout: process may
    still be running, so liveness is polled until the process exits."""
    orchestrator = build_orchestrator()
    liveness_answers = iter([True, True, False])
    liveness_calls = []

    def fake_liveness(fragment):
        liveness_calls.append(fragment)
        return next(liveness_answers)

    orchestrator._command_still_running = fake_liveness

    stream = ScriptedStream([(b"partial output", None), ReadTimeoutLike()])
    exec_result = SimpleNamespace(output=stream, exit_code=None)

    result = orchestrator._monitor_execution_with_timeouts(
        exec_result,
        fresh_state(command_fragment="./gradlew compileJava"),
        silent_timeout=60,
        absolute_timeout=60,
    )

    assert len(liveness_calls) == 3
    assert liveness_calls[0] == "./gradlew compileJava"
    assert result["termination_reason"] is None
    assert "partial output" in result["output"]
    assert orchestrator.terminate_calls == []


def test_blind_enforcement_terminates_on_absolute_timeout():
    """The absolute deadline passes while in the blind (stream-lost) phase:
    start_offset is just under the cap so the streaming loop lets it through
    and the blind loop must catch the breach."""
    orchestrator = build_orchestrator()
    orchestrator._command_still_running = lambda fragment: True

    stream = ScriptedStream([ReadTimeoutLike()])
    exec_result = SimpleNamespace(output=stream, exit_code=None)

    state = fresh_state(start_offset=0.95, command_fragment="./gradlew compileJava")
    result = orchestrator._monitor_execution_with_timeouts(
        exec_result, state, silent_timeout=100, absolute_timeout=1
    )

    assert result["termination_reason"] == "absolute_timeout"
    assert orchestrator.terminate_calls


def test_normal_stream_completion_unchanged():
    orchestrator = build_orchestrator()
    stream = ScriptedStream([(b"BUILD SUCCESSFUL", None)])
    exec_result = SimpleNamespace(output=stream, exit_code=0)

    result = orchestrator._monitor_execution_with_timeouts(
        exec_result, fresh_state(), silent_timeout=60, absolute_timeout=60
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "BUILD SUCCESSFUL" in result["output"]
