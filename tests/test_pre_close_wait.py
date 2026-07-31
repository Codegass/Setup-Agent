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

import json

from sag.agent.job_obligations import OBLIGATION_DIR, write_obligation
from sag.agent.react_engine import ReActEngine
from sag.agent.verdict_finalizer import EvidenceCloseReason

JOB = "373f63e5a0a4"
LOG_PATH = f"/tmp/sag_jobs/{JOB}.log"
EXIT_PATH = f"{LOG_PATH}.exit"


def _obligation():
    return {
        "schema_version": 1,
        "job_id": JOB,
        "tool": "gradle",
        "attempt": 1,
        "requested_action": "test",
        "effective_action": "test",
        "argv": "/workspace/polaris/gradlew test",
        "working_directory": "/workspace/polaris",
        "before": {},
        "log_path": LOG_PATH,
        "exit_code_path": EXIT_PATH,
        "settled_receipt_id": None,
        "dispatch_sequence": 1,
    }


class WaitingContainer:
    """A ledger with one open obligation whose exit file appears after
    `polls_until_exit` existence checks."""

    def __init__(self, polls_until_exit=3):
        self.polls_until_exit = polls_until_exit
        self.exit_checks = 0
        self.files = {}
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat ") and "*.json" in command and OBLIGATION_DIR in command:
            bodies = [
                body for path, body in sorted(self.files.items())
                if path.startswith(OBLIGATION_DIR)
            ]
            return {"success": True, "exit_code": 0, "output": "\n".join(bodies)}
        if command.startswith("cat ") and OBLIGATION_DIR in command and "<<" not in command:
            # single-file precheck: absence must be a real cat failure, or the
            # writer reads "" as an existing different body and refuses
            for path, body in self.files.items():
                if path in command:
                    return {"success": True, "exit_code": 0, "output": body}
            return {"success": False, "exit_code": 1, "output": ""}
        if EXIT_PATH in command and "<<" not in command:
            self.exit_checks += 1
            if self.exit_checks >= self.polls_until_exit:
                return {"success": True, "exit_code": 0, "output": "0\n"}
            return {"success": False, "exit_code": 1, "output": ""}
        if "<<" in command and OBLIGATION_DIR in command:
            # persist obligation writes so the ledger read sees them
            for line in command.split("\n", 1)[1].splitlines():
                if line.strip().startswith("{"):
                    try:
                        record = json.loads(line)
                        self.files[f"{OBLIGATION_DIR}/{record['job_id']}.json"] = line
                    except (ValueError, KeyError):
                        pass
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


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

    assert container.exit_checks >= 3
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
    assert container.exit_checks == 0


def test_an_unreadable_ledger_is_never_waited_on():
    """The gate already caps on an unreadable ledger (§6.8 fence 1); waiting
    on a ledger we cannot read would be waiting on a guess."""

    class Unreadable:
        def execute_command(self, command, **kwargs):
            return {
                "success": False,
                "exit_code": -1,
                "output": "Failed to execute command: transport hiccup",
                "dispatch_status": "dispatch_failed",
            }

    engine = _engine(Unreadable())

    _wait(engine)

    assert engine._sleeps == []


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
    engine._record_unsettled_job_conflicts = lambda: calls.append("conflicts")
    engine._emit_control_event = lambda *a, **k: None
    engine.run_evidence_state = SimpleNamespace(sealed=False)
    engine.verdict_finalizer = SimpleNamespace(finalize=lambda state, reason: "snapshot")

    engine._finalize_evidence(EvidenceCloseReason.TEST_TERMINATED)

    assert calls == [EvidenceCloseReason.TEST_TERMINATED, "sweep", "conflicts"]


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
