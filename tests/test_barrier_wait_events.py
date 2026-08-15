"""Task #53 — the controller barrier says, in the record, that it is waiting.

The p7d camel forensics had to read console logs to learn that the barrier
polled: `control_events.jsonl` showed a 5,033.9-second hole while 160 probes
were in fact executing, because every job kind on that path is exceptional
(`job_stall_observed`, `job_barrier_integrity_failure`, `job_live_at_close`,
`job_settled`). A run that is waiting and a run that has stopped looked
identical in the authoritative stream — and the one thing that would have made
the hang legible as it happened, a log frozen at 186,675,667 bytes with a
1-tick CPU delta every few probes, was never written down at all.

So the wait gets one bounded, engine-written, non-terminal kind. Bounded means
the first wait for a job, any wait whose physical observation changed, and one
in every `_BARRIER_WAIT_EVERY` otherwise — never one per poll. Non-terminal
means exactly what it says: an emission failure is logged and the wait
continues, because observability never ends a run.
"""

from test_job_settlement import JOB
from test_native_loop_engine import _engine
from test_pre_close_wait import WaitingContainer
from test_pre_close_wait import _obligation as waiting_obligation
from test_settlement_triggers import _transcript_with

from sag.agent.control_events import CONTROL_EVENT_KINDS, ControlEvent
from sag.agent.job_obligations import OBLIGATION_DIR, write_obligation
from sag.agent.react_llm import NativeTurn
from sag.agent.replay import ControlReplayRunner


def _text_turn():
    return NativeTurn(text="finished", tool_calls=(), model_used="scripted-model")


def _recording_engine(orchestrator):
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.config.max_wall_clock_seconds = 7_200
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    events = []
    engine._emit_control_event = lambda kind, payload: events.append((kind, payload))
    return engine, events


def test_the_barrier_wait_is_a_control_event_kind():
    assert CONTROL_EVENT_KINDS[25] == "job_barrier_wait"
    assert len(CONTROL_EVENT_KINDS) == 26
    event = ControlEvent(
        sequence=1,
        kind="job_barrier_wait",
        payload={
            "job_id": JOB,
            "obligation_ref": f"/workspace/.setup_agent/job_obligations/{JOB}.json",
            "waits": 1,
            "waited_seconds": 30,
            "remaining_seconds": 6_570,
            "process_state": "running",
            "log_size": 186_675_667,
            "cpu_ticks_delta": 1,
            "artifact_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "progressing": False,
        },
    )
    assert event.payload["waits"] == 1


def test_a_settling_wait_polls_on_the_promised_cadence_with_no_model_turn():
    """Contract (a): the poll loop is the controller's, not the model's."""
    container = WaitingContainer(polls_until_exit=4)
    write_obligation(container.execute_command, waiting_obligation())
    engine, events = _recording_engine(container)
    engine._OBLIGATION_POLL_SECONDS = 30
    slept = []

    status = engine._drain_job_barrier(now=lambda: 0.0, sleep=slept.append)

    assert status == "cleared"
    # Three waits at the promised cadence, then the exit marker resolves it.
    assert slept == [30, 30, 30]
    assert container.terminal_checks == 4
    assert engine.llm_client.requests == []
    waits = [payload for kind, payload in events if kind == "job_barrier_wait"]
    assert [wait["waits"] for wait in waits] == [1]
    assert waits[0]["job_id"] == waiting_obligation()["job_id"]
    assert waits[0]["process_state"] == "running"
    assert waits[0]["remaining_seconds"] == 6_600
    assert waits[0]["waited_seconds"] == 0


def test_an_unchanged_observation_is_reported_on_a_bounded_cadence():
    """160 identical probes must not become 160 rows; the hole must still close.

    This is camel's exact shape: a job whose CPU ticks keep the controller's
    progress predicate true forever, so the stall clock never matures and the
    wait runs to the reserve.
    """
    container = WaitingContainer(polls_until_exit=10_000)
    write_obligation(container.execute_command, waiting_obligation())
    engine, events = _recording_engine(container)
    engine._OBLIGATION_POLL_SECONDS = 30
    engine._BARRIER_WAIT_EVERY = 5
    clock = [0.0]

    def tick(_seconds):
        clock[0] += 30.0
        if clock[0] >= 600.0:
            # The wall guard branch owns everything past the reserve; this test
            # is about the waits before it.
            raise StopIteration

    engine._hold_deadline = lambda: 6_000.0
    try:
        engine._drain_job_barrier(now=lambda: clock[0], sleep=tick)
    except StopIteration:
        pass

    waits = [payload for kind, payload in events if kind == "job_barrier_wait"]
    assert [wait["waits"] for wait in waits] == [1, 6, 11, 16]
    assert all(wait["process_state"] == "running" for wait in waits)
    assert [wait["waited_seconds"] for wait in waits] == [0, 150, 300, 450]
    # The shape that hid the hang: work claimed by one CPU tick, nothing else.
    assert all(wait["cpu_ticks_delta"] == 1 for wait in waits)
    assert len({wait["log_size"] for wait in waits}) == 1


def test_a_failing_event_sink_never_ends_the_wait():
    """Observability never ends a run: the emission is not on the wait's path."""
    container = WaitingContainer(polls_until_exit=3)
    write_obligation(container.execute_command, waiting_obligation())
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = container
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    engine._OBLIGATION_POLL_SECONDS = 0
    refusals = []

    class RefusingSink:
        def emit(self, kind, payload):
            refusals.append(kind)
            raise RuntimeError("control sink unavailable")

    engine.control_event_sink = RefusingSink()

    assert engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _s: None) == "cleared"
    assert "job_barrier_wait" in refusals


def test_replay_walks_a_transcript_that_records_its_waits(tmp_path):
    """A heartbeat moves no lifecycle state, and never invalidates a transcript."""
    transcript = _transcript_with(
        tmp_path,
        "waiting-paramiko.jsonl",
        [
            {
                "kind": "job_barrier_wait",
                "payload": {
                    "job_id": JOB,
                    "obligation_ref": f"{OBLIGATION_DIR}/{JOB}.json",
                    "waits": waits,
                    "waited_seconds": 30 * (waits - 1),
                    "remaining_seconds": 6_600 - 30 * (waits - 1),
                    "process_state": "running",
                    "log_size": 186_675_667,
                    "cpu_ticks_delta": 1,
                    "progressing": True,
                },
            }
            for waits in (1, 11)
        ],
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot is not None
    assert not any(
        conflict.startswith("job_barrier_wait") for conflict in result.snapshot.conflicts
    )
