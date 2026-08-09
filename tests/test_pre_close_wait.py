# tests/test_pre_close_wait.py
"""Plan 8 — the run stopped closing its books while the worker was still out.

Live evidence, both p7d runs. camel (`session_20260729_111740_22389`) ended at
~85 minutes with the test job still running and 11,492 observed tests
unclaimable; the wall-clock cap is 7,200s, so ~35 minutes of budget went
unused while the one thing that needed time was denied it. polaris
(`session_20260729_111737_22356`), same shape at 321 tests. Settlement (§3.2)
claims a job that EXITED during the run — a job still alive at evidence-close
stays `job_unsettled` and its work stays in auxiliary, honestly and uselessly.

So evidence-close waits, bounded: while an open obligation's job is still
running, the wall clock has margin beyond the report reserve, and the close is
a working close (not an abort), poll for the exit file before sweeping. The
wait spends budget that was already allocated and otherwise evaporates; it
never extends the cap, never runs during aborts, and never waits on a ledger
it cannot read.
"""

from container_evidence_fakes import ContainerFS

from sag.agent.job_obligations import build_obligation, write_obligation
from sag.agent.react_engine import ReActEngine
from sag.agent.verdict_finalizer import EvidenceCloseReason

JOB = "373f63e5a0a4"
LOG_PATH = f"/tmp/sag_jobs/{JOB}.log"
EXIT_PATH = f"{LOG_PATH}.exit"
TERMINAL_AUTHORITY = "docker_exec_inspect_v1"
DOCKER_EXEC_ID = "e" * 64
CONTAINER_ID = "c" * 64


def _obligation():
    return build_obligation(
        job_id=JOB,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="/workspace/polaris/gradlew test",
        working_directory="/workspace/polaris",
        before={},
        log_path=LOG_PATH,
        exit_code_path=EXIT_PATH,
        terminal_authority=TERMINAL_AUTHORITY,
        docker_exec_id=DOCKER_EXEC_ID,
        container_id=CONTAINER_ID,
        start_accepted=True,
        startup_identity_verified=True,
        runner_dispatch_state="accepted",
        pid=4711,
        pgid=4711,
        pid_path=f"/tmp/sag_jobs/{JOB}.pid",
        pgid_path=f"/tmp/sag_jobs/{JOB}.pgid",
        identity_path=f"/tmp/sag_jobs/{JOB}.identity",
        process_identity_token="a" * 64,
        dispatch_sequence=1,
    )


class WaitingContainer(ContainerFS):
    """A ledger plus a host-side detached exec that eventually finishes."""

    def __init__(self, polls_until_exit=3):
        super().__init__()
        self.polls_until_exit = polls_until_exit
        self.terminal_checks = 0

    def execute_command(self, command, **kwargs):
        return self(command, **kwargs)

    def execute_control_command(self, command, **kwargs):
        return self(command, **kwargs)

    def __call__(self, command, **kwargs):
        if command.startswith("set +e; set -o pipefail; tmp=$(mktemp -d /tmp/sag-job-progress"):
            self.commands.append(command)
            digest = "b" * 64
            return {
                "success": True,
                "exit_code": 0,
                "output": "\n".join(
                    (
                        f"JOB_ID:{JOB}",
                        "PGID:4711",
                        "PROCESS_STATE:running",
                        "LOG_SIZE:1",
                        "CPU_TICKS_DELTA:1",
                        "PROCESS_COUNT:1",
                        "CHILD_COUNT:0",
                        "IDENTITY_COMPLETE:1",
                        "ARTIFACT_COMPLETE:1",
                        "REPORT_COMPLETE:1",
                        f"PROCESS_IDENTITY_SHA256:{digest}",
                        f"ARTIFACT_SHA256:{digest}",
                        f"REPORT_SHA256:{digest}",
                    )
                ),
            }
        return super().__call__(command, **kwargs)

    def inspect_detached_terminal(self, handle):
        self.terminal_checks += 1
        if (
            handle.get("terminal_authority") != TERMINAL_AUTHORITY
            or handle.get("docker_exec_id") != DOCKER_EXEC_ID
            or handle.get("container_id") != CONTAINER_ID
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "probe_error": "terminal_identity_mismatch",
            }
        if self.terminal_checks < self.polls_until_exit:
            return {
                "probe_success": True,
                "state": "running",
                "running": True,
                "finished": False,
                "exit_code": None,
            }
        return {
            "probe_success": True,
            "state": "finished",
            "running": False,
            "finished": True,
            "exit_code": 0,
        }


def _engine(container, *, started_at=1000.0, cap=7200, now=2000.0):
    engine = ReActEngine.__new__(ReActEngine)
    engine.orchestrator = container
    engine._run_started_at = started_at
    engine._wall_clock_cap = cap
    engine._sleeps = []
    engine._clock = lambda: engine._now
    engine._now = now
    return engine


def _wait(engine, reason=EvidenceCloseReason.TEST_TERMINATED):
    def sleep(seconds):
        engine._sleeps.append(seconds)
        engine._now += seconds

    return engine._await_open_obligations(reason, now=engine._clock, sleep=sleep)


def test_the_close_waits_for_a_running_job_and_the_exit_arrives():
    container = WaitingContainer(polls_until_exit=3)
    write_obligation(container.execute_command, _obligation())
    engine = _engine(container)

    _wait(engine)

    assert container.terminal_checks >= 3
    assert len(engine._sleeps) == 2  # slept between the three exit checks


def test_no_margin_means_no_wait():
    """The wait spends leftover budget; it never extends the cap. With the
    clock already inside the report reserve, close proceeds immediately."""
    container = WaitingContainer(polls_until_exit=2)
    write_obligation(container.execute_command, _obligation())
    engine = _engine(container, started_at=1000.0, cap=7200, now=1000.0 + 7200 - 60)

    _wait(engine)

    assert engine._sleeps == []


def test_an_abort_never_waits():
    container = WaitingContainer(polls_until_exit=2)
    write_obligation(container.execute_command, _obligation())
    engine = _engine(container)

    _wait(engine, reason=EvidenceCloseReason.ABORTED)
    _wait(engine, reason=EvidenceCloseReason.CANCELLED)

    assert engine._sleeps == []


def test_no_open_obligations_means_no_wait():
    container = WaitingContainer()
    engine = _engine(container)

    _wait(engine)

    assert engine._sleeps == []
    assert container.terminal_checks == 0


def test_an_unreadable_ledger_gets_one_bounded_controller_retry_not_a_model_turn():
    """Harness evidence failure is retried mechanically, then closes honestly."""

    class Unreadable:
        def execute_command(self, command, **kwargs):
            raise AssertionError("normal runner must not be used for control reads")

        def execute_control_command(self, command, **kwargs):
            return {
                "success": False,
                "exit_code": -1,
                "output": "Failed to execute command: transport hiccup",
                "dispatch_status": "dispatch_failed",
            }

    engine = _engine(Unreadable())

    _wait(engine)

    assert engine._sleeps == [1.0]


def test_a_missing_run_clock_means_no_wait():
    """Margin unknown is margin absent: engines constructed without the run
    clock (unit fixtures, replay) must close immediately."""
    container = WaitingContainer(polls_until_exit=2)
    write_obligation(container.execute_command, _obligation())
    engine = _engine(container)
    del engine._run_started_at

    _wait(engine)

    assert engine._sleeps == []


def test_finalize_evidence_waits_before_it_sweeps():
    """The wiring, pinned: deleting the wait call from _finalize_evidence must
    fail THIS test — every other test here calls the method directly, which is
    how a round-one fence once survived its own mutation."""
    from types import SimpleNamespace

    engine = ReActEngine.__new__(ReActEngine)
    calls = []
    engine._await_open_obligations = lambda reason, **kw: calls.append(reason)
    engine._sweep_job_obligations = lambda: calls.append("sweep")
    engine._record_unsettled_job_conflicts = lambda reason: calls.append(("conflicts", reason))
    engine._emit_control_event = lambda *a, **k: None
    engine.run_evidence_state = SimpleNamespace(sealed=False)
    engine.verdict_finalizer = SimpleNamespace(finalize=lambda state, reason: "snapshot")

    engine._finalize_evidence(EvidenceCloseReason.TEST_TERMINATED)

    assert calls == [
        EvidenceCloseReason.TEST_TERMINATED,
        "sweep",
        ("conflicts", EvidenceCloseReason.TEST_TERMINATED),
    ]


def test_a_sealed_state_never_reaches_the_wait():
    from types import SimpleNamespace

    engine = ReActEngine.__new__(ReActEngine)
    calls = []
    engine._await_open_obligations = lambda reason, **kw: calls.append("wait")
    engine._sweep_job_obligations = lambda: calls.append("sweep")
    engine._record_unsettled_job_conflicts = lambda: calls.append("conflicts")
    engine._emit_control_event = lambda *a, **k: None
    engine.run_evidence_state = SimpleNamespace(sealed=True)
    engine.verdict_finalizer = SimpleNamespace(finalize=lambda state, reason: "snapshot")

    engine._finalize_evidence(EvidenceCloseReason.TEST_TERMINATED)

    assert calls == []


def test_the_deadline_stops_a_job_that_never_exits():
    """A job that outlives even the waited-for margin still closes honestly:
    the wait gives up at the reserve line and the verdict keeps job_unsettled.
    The fake sleep carries its own fuse so a deadline-less mutation fails this
    test instead of hanging it."""
    container = WaitingContainer(polls_until_exit=10**6)
    write_obligation(container.execute_command, _obligation())
    engine = _engine(container, started_at=0.0, cap=1800, now=600.0)

    def fused_sleep(seconds):
        engine._sleeps.append(seconds)
        engine._now += seconds
        assert len(engine._sleeps) < 100, "the wait did not respect the deadline"

    engine._await_open_obligations(
        EvidenceCloseReason.TEST_TERMINATED, now=engine._clock, sleep=fused_sleep
    )

    assert engine._sleeps  # it did wait...
    total = sum(engine._sleeps)
    assert total <= 1800 - 600  # ...but never past the cap, reserve included
