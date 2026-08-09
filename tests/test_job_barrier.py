"""WS2: a registered job is controller-owned until lifecycle settlement resolves."""

import json

from test_job_settlement import (
    CONTAINER_ID,
    DOCKER_EXEC_ID,
    EXIT_PATH,
    JOB,
    _obligation,
    _orchestrator,
    _receipt_failing_orchestrator,
    _with_obligation,
)
from container_evidence_fakes import ScriptedOrchestrator
from test_native_loop_engine import _engine
from test_pre_close_wait import WaitingContainer
from test_pre_close_wait import _obligation as waiting_obligation

import sag.agent.react_engine as react_engine_module
from sag.agent.job_obligations import (
    OBLIGATION_DIR,
    reconcile_job_obligations,
    write_obligation,
)
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.output_storage import OutputStorageManager
from sag.agent.stall_diagnostics import (
    CleanupResult,
    DiagnosticBundle,
    EvidenceSeal,
    JobProgressSnapshot,
    ProgressObservation,
    StallControlResult,
)
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolResult


def _text_turn():
    return NativeTurn(text="finished", tool_calls=(), model_used="scripted-model")


PROCESS_PROVENANCE = {
    "pid": 4711,
    "pgid": 4711,
    "pid_path": "/tmp/sag_jobs/job.pid",
    "pgid_path": "/tmp/sag_jobs/job.pgid",
    "identity_path": "/tmp/sag_jobs/job.identity",
    "process_identity_token": "a" * 64,
}


def _confirmed_stall_cleanup_result(*, group_live=False):
    diagnostic = DiagnosticBundle(
        job_id=JOB,
        pid=4711,
        pgid=4711,
        process_identity_token="a" * 64,
        evidence_ref=f"/workspace/.setup_agent/job_diagnostics/{JOB}/bundle.json",
        diagnostic_fingerprint="b" * 64,
        observation="thread_join_wait",
        snapshot=JobProgressSnapshot(process_state="running"),
        persisted=True,
        code="captured",
    )
    seal = EvidenceSeal(
        job_id=JOB,
        pgid=4711,
        process_identity_token="a" * 64,
        evidence_ref=f"/workspace/.setup_agent/job_diagnostics/{JOB}/seal.json",
        diagnostic_ref=diagnostic.evidence_ref,
        diagnostic_fingerprint=diagnostic.diagnostic_fingerprint,
        persisted=True,
        code="sealed",
    )
    return StallControlResult(
        job_id=JOB,
        code="job_live_at_close" if group_live else "killed",
        diagnostic=diagnostic,
        progress=ProgressObservation(diagnostic.snapshot),
        seal=seal,
        cleanup=CleanupResult(
            job_id=JOB,
            pgid=4711,
            code="job_live_at_close" if group_live else "killed",
            term_sent=True,
            kill_sent=True,
            group_live=group_live,
        ),
    )


def test_running_job_is_polled_to_terminal_before_any_model_iteration():
    container = WaitingContainer(polls_until_exit=3)
    write_obligation(container.execute_command, waiting_obligation())
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = container
    engine.config.max_wall_clock_seconds = 7_200
    engine._OBLIGATION_POLL_SECONDS = 0
    observed = []
    original = engine.llm_client.get_native_turn

    def model_turn(messages, **kwargs):
        observed.append((container.terminal_checks, engine.current_iteration))
        return original(messages, **kwargs)

    engine.llm_client.get_native_turn = model_turn

    assert engine._run_native_loop("go", max_iterations=1, completion_mode="build") is True
    assert observed == [(3, 1)]
    assert container.terminal_checks == 3


def test_terminal_receipt_retry_resolves_before_the_model_sees_the_job():
    orchestrator = _with_obligation(_receipt_failing_orchestrator(receipt_failures=1))
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.config.max_wall_clock_seconds = 7_200
    states_at_model = []
    original = engine.llm_client.get_native_turn

    def model_turn(messages, **kwargs):
        body = orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"]
        states_at_model.append(body)
        return original(messages, **kwargs)

    engine.llm_client.get_native_turn = model_turn

    assert engine._run_native_loop("go", max_iterations=1, completion_mode="build") is True
    assert len(states_at_model) == 1
    assert '"process_state": "terminal"' in states_at_model[0]
    assert '"settlement_state": "settled"' in states_at_model[0]
    assert orchestrator.filesystem.receipt_failures == 0


def test_terminal_settlement_does_not_require_a_process_wait_deadline():
    orchestrator = _with_obligation(_receipt_failing_orchestrator(receipt_failures=0))
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.__dict__.pop("_run_started_at", None)

    assert engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _seconds: None) == "cleared"
    body = orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"]
    assert '"process_state": "terminal"' in body
    assert '"settlement_state": "settled"' in body


def test_tampered_settlement_attempts_fail_after_bounded_integrity_retries():
    orchestrator = _orchestrator(exit_code="0")
    obligation = _obligation(
        process_state="terminal",
        settlement_state="pending",
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=0,
        attempted_receipt_id="inv-gradle-invalid-attempts",
    )
    assert write_obligation(orchestrator.execute_command, obligation)
    path = f"{OBLIGATION_DIR}/{JOB}.json"
    tampered = json.loads(orchestrator.filesystem.files[path])
    tampered["settlement_attempts"] = -3
    orchestrator.filesystem.files[path] = json.dumps(tampered, sort_keys=True)
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 600.0
    engine._REPORT_RESERVE_SECONDS = 0
    sleeps = []

    status = engine._drain_job_barrier(
        now=lambda: 600.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert status == "integrity_failure"
    assert len(sleeps) == 1
    assert "ledger_unreadable" in engine._fatal_harness_control_failure


def test_exhausted_harness_persistence_failure_closes_without_a_model_turn():
    orchestrator = _with_obligation(_receipt_failing_orchestrator(receipt_failures=2))
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.config.max_wall_clock_seconds = 7_200

    assert engine._run_native_loop("go", max_iterations=1, completion_mode="build") is False
    assert engine.llm_client.requests == []
    assert f"job_terminal_unpersisted:{JOB}" in engine.run_evidence_state.conflicts


def test_restart_recovers_terminal_unpersisted_as_an_integrity_barrier():
    orchestrator = _with_obligation(_receipt_failing_orchestrator(receipt_failures=2))
    reconcile_job_obligations(orchestrator)
    reconcile_job_obligations(orchestrator)

    # This is a fresh controller instance: no in-memory announcement/barrier
    # flag survives.  The durable lifecycle must still prevent a model turn.
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.config.max_wall_clock_seconds = 7_200

    assert engine._run_native_loop("go", max_iterations=1, completion_mode="build") is False
    assert engine.llm_client.requests == []
    assert f"job_terminal_unpersisted:{JOB}" in engine.run_evidence_state.conflicts


def test_malformed_detached_handoff_aborts_before_a_second_model_turn(tmp_path):
    class MalformedDetachedTool(BaseTool):
        def __init__(self):
            super().__init__("build", "malformed detached fixture")

        def execute(self, action: str) -> ToolResult:
            return ToolResult(
                invocation_status=InvocationStatus.PENDING,
                operation_outcome=OperationOutcome.UNKNOWN,
                evidence_status=EvidenceStatus.UNKNOWN,
                poll_ref="job:missing-persistence",
                output="",
                metadata={
                    "dispatch_status": "running_detached",
                    "job_id": "missing-persistence",
                    "exit_code_path": "/tmp/sag_jobs/missing-persistence.exit",
                },
            )

        def _get_parameters_schema(self):
            return {
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"],
            }

    turn = NativeTurn(
        text="dispatching",
        tool_calls=(
            NativeToolCall(
                id="call-malformed",
                name="build",
                arguments={"action": "compile"},
                raw_arguments='{"action":"compile"}',
            ),
        ),
        model_used="scripted-model",
    )
    engine = _engine([turn, _text_turn()], max_iterations=2)
    engine.tools["build"] = MalformedDetachedTool()
    storage = OutputStorageManager(tmp_path)
    engine.output_storage = storage
    engine._get_tool_orchestrator().output_storage = storage

    termination = engine.run_setup_loop("go", max_iterations=2)

    assert termination.termination.value == "aborted"
    assert len(engine.llm_client.requests) == 1
    assert (
        "detached_result_persistence_invalid"
        in engine.run_evidence_state.fact_value("job_barrier_integrity_failure")["failures"]
    )


def test_ambiguous_start_cannot_be_laundered_by_finished_zero_before_next_turn(tmp_path):
    class AmbiguousDetachedTool(BaseTool):
        def __init__(self):
            super().__init__("build", "ambiguous detached fixture")

        def execute(self, action: str) -> ToolResult:
            del action
            return ToolResult(
                invocation_status=InvocationStatus.PENDING,
                operation_outcome=OperationOutcome.UNKNOWN,
                evidence_status=EvidenceStatus.UNKNOWN,
                poll_ref="job:ambiguous-start",
                output="Docker start response was ambiguous",
                metadata={
                    "dispatch_status": "dispatch_unknown",
                    "runner_dispatched": None,
                    "runner_dispatch_state": "unknown",
                    "started": False,
                    "start_accepted": False,
                    "startup_identity_verified": False,
                    "job_id": "ambiguous-start",
                    "job_obligation_persisted": False,
                    "job_obligation_persistence_code": "invalid_dispatch_handle",
                    "terminal_authority": "docker_exec_inspect_v1",
                    "docker_exec_id": DOCKER_EXEC_ID,
                    "container_id": CONTAINER_ID,
                    "log_path": "/tmp/sag_jobs/ambiguous-start.log",
                    "exit_code_path": "/tmp/sag_jobs/ambiguous-start.log.exit",
                },
            )

        def _get_parameters_schema(self):
            return {
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"],
            }

    class FinishedOnlyOrchestrator(ScriptedOrchestrator):
        def inspect_detached_terminal(self, handle):
            assert handle["start_accepted"] is False
            return {
                "probe_success": True,
                "state": "finished",
                "running": False,
                "finished": True,
                "exit_code": 0,
            }

    turn = NativeTurn(
        text="dispatching",
        tool_calls=(
            NativeToolCall(
                id="call-ambiguous",
                name="build",
                arguments={"action": "compile"},
                raw_arguments='{"action":"compile"}',
            ),
        ),
        model_used="scripted-model",
    )
    engine = _engine([turn, _text_turn()], max_iterations=2)
    engine.tools["build"] = AmbiguousDetachedTool()
    engine.orchestrator = FinishedOnlyOrchestrator()
    storage = OutputStorageManager(tmp_path)
    engine.output_storage = storage
    engine._get_tool_orchestrator().output_storage = storage

    termination = engine.run_setup_loop("go", max_iterations=2)

    assert termination.termination.value == "aborted"
    assert len(engine.llm_client.requests) == 1
    assert "ambiguous-start" in engine._ephemeral_job_handles()
    failures = engine.run_evidence_state.fact_value("job_barrier_integrity_failure")["failures"]
    assert "ambiguous-start:terminal_authority_unreadable" in failures


def test_daemon_running_promotes_ambiguous_ephemeral_before_terminal_settlement():
    class RunningThenFinishedOrchestrator(ScriptedOrchestrator):
        def __init__(self):
            super().__init__()
            self.observations = iter(("running", "finished"))

        def inspect_detached_terminal(self, handle):
            state = next(self.observations)
            if state == "running":
                return {
                    "probe_success": True,
                    "state": "running",
                    "running": True,
                    "finished": False,
                    "exit_code": None,
                }
            assert handle["start_accepted"] is True
            return {
                "probe_success": True,
                "state": "finished",
                "running": False,
                "finished": True,
                "exit_code": 0,
            }

    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = RunningThenFinishedOrchestrator()
    engine._job_barrier_ephemeral_handles = {
        "ambiguous-start": {
            "job_id": "ambiguous-start",
            "terminal_authority": "docker_exec_inspect_v1",
            "docker_exec_id": DOCKER_EXEC_ID,
            "container_id": CONTAINER_ID,
            "start_accepted": False,
            "startup_identity_verified": False,
            "started": False,
            "runner_dispatched": None,
            "runner_dispatch_state": "unknown",
            "log_path": "/tmp/sag_jobs/ambiguous-start.log",
            "job_obligation_persistence_code": "invalid_dispatch_handle",
        }
    }

    running, failures = engine._reconcile_ephemeral_jobs()

    assert running == ("ambiguous-start",)
    assert failures == ()
    promoted = engine._ephemeral_job_handles()["ambiguous-start"]
    assert promoted["start_accepted"] is True
    assert promoted["runner_dispatched"] is True
    assert promoted["runner_dispatch_state"] == "accepted"
    assert promoted["startup_identity_verified"] is False

    running, failures = engine._reconcile_ephemeral_jobs()

    assert running == ()
    assert failures == ()
    assert "ambiguous-start" not in engine._ephemeral_job_handles()
    assert "job_terminal_unpersisted:ambiguous-start" in engine.run_evidence_state.conflicts


def test_multiple_jobs_form_one_set_barrier():
    orchestrator = _receipt_failing_orchestrator(receipt_failures=0)
    first = _obligation()
    second = {
        **_obligation(),
        "job_id": "second-job",
        "log_path": "/tmp/sag_jobs/second-job.log",
        "exit_code_path": "/tmp/sag_jobs/second-job.log.exit",
        "docker_exec_id": "f" * 64,
    }
    orchestrator.set_detached_terminal_state("f" * 64, "running")
    orchestrator.filesystem.files[second["log_path"]] = "BUILD SUCCESSFUL\n"
    # Only the first has an exit marker. The second holds the global barrier.
    write_obligation(orchestrator.execute_command, first)
    write_obligation(orchestrator.execute_command, second)

    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 600.0
    engine._REPORT_RESERVE_SECONDS = 0

    status = engine._drain_job_barrier(now=lambda: 600.0, sleep=lambda _seconds: None)

    assert status == "integrity_failure"
    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    assert engine.llm_client.requests == []


def test_confirmed_stall_cleanup_stays_in_controller_until_marker_settlement(monkeypatch):
    orchestrator = _with_obligation(
        _orchestrator(exit_code=None),
        **PROCESS_PROVENANCE,
        handoff_reason="stalled",
    )
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.config.max_wall_clock_seconds = 7_200
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    calls = []

    def cleanup(_execute, _job, **kwargs):
        calls.append(kwargs["trigger"])
        orchestrator.set_detached_terminal_state(DOCKER_EXEC_ID, "finished", 137)
        return _confirmed_stall_cleanup_result()

    monkeypatch.setattr("sag.agent.react_engine.control_stalled_job", cleanup)

    assert engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _seconds: None) == "cleared"
    assert calls == ["stall_confirmation"]
    body = orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"]
    assert '"process_state": "terminal"' in body
    assert '"settlement_state": "settled"' in body
    assert engine.llm_client.requests == []
    assert any(
        fact.key.startswith(f"job_stall_observed.{JOB}.")
        for fact in engine.run_evidence_state.facts
    )


def test_dead_group_without_supervisor_marker_is_integrity_not_live(monkeypatch):
    orchestrator = _with_obligation(
        _orchestrator(exit_code=None),
        **PROCESS_PROVENANCE,
        handoff_reason="stalled",
    )
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    engine._JOB_MARKER_RECONCILE_ATTEMPTS = 2
    monkeypatch.setattr(
        "sag.agent.react_engine.control_stalled_job",
        lambda *_args, **_kwargs: _confirmed_stall_cleanup_result(),
    )

    status = engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _seconds: None)

    assert status == "integrity_failure"
    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    assert not any(
        fact.key == f"job_live_at_close.{JOB}" for fact in engine.run_evidence_state.facts
    )
    assert engine.llm_client.requests == []


def test_progressing_job_reaches_wall_guard_without_stall_cleanup(monkeypatch):
    orchestrator = _with_obligation(
        _orchestrator(exit_code=None),
        **PROCESS_PROVENANCE,
        handoff_reason="window",
    )
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    clock = [0.0]
    triggers = []
    progress = ProgressObservation(
        JobProgressSnapshot(process_state="running", cpu_ticks_delta=10),
        ("cpu_active",),
    )
    monkeypatch.setattr(
        "sag.agent.react_engine.probe_job_progress",
        lambda *_args, **_kwargs: progress,
    )

    def wall_observation(_execute, _job, **kwargs):
        triggers.append(kwargs["trigger"])
        return StallControlResult(
            job_id=JOB,
            code="wall_guard_progressing",
            progress=progress,
            trigger=kwargs["trigger"],
        )

    monkeypatch.setattr("sag.agent.react_engine.control_stalled_job", wall_observation)

    status = engine._drain_job_barrier(
        now=lambda: clock[0],
        sleep=lambda _seconds: clock.__setitem__(0, 6_600.0),
    )

    assert status == "live_at_deadline"
    assert triggers == ["wall_guard"]
    assert not any(
        fact.key.startswith(f"job_stall_observed.{JOB}.")
        for fact in engine.run_evidence_state.facts
    )
    assert engine.llm_client.requests == []


def test_wall_guard_unobservable_progress_is_integrity_not_live(monkeypatch):
    orchestrator = _with_obligation(
        _orchestrator(exit_code=None),
        **PROCESS_PROVENANCE,
        handoff_reason="window",
    )
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    unobservable = ProgressObservation(
        JobProgressSnapshot(),
        code="progress_probe_failed",
    )
    monkeypatch.setattr(
        "sag.agent.react_engine.control_stalled_job",
        lambda *_args, **kwargs: StallControlResult(
            job_id=JOB,
            code="progress_unobservable",
            progress=unobservable,
            trigger=kwargs["trigger"],
        ),
    )

    status = engine._drain_job_barrier(now=lambda: 6_600.0, sleep=lambda _seconds: None)

    assert status == "integrity_failure"
    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    assert not any(
        fact.key == f"job_live_at_close.{JOB}" for fact in engine.run_evidence_state.facts
    )


def test_barrier_integrity_failure_is_emitted_at_most_once_before_close():
    engine = _engine([_text_turn()], max_iterations=1)
    events = []
    engine._emit_control_event = lambda kind, payload: events.append((kind, payload))

    engine._record_job_barrier_integrity_failure(("ledger_unreadable",))
    engine._record_job_barrier_integrity_failure(("ledger_unreadable",))

    assert events == [
        (
            "job_barrier_integrity_failure",
            {"failures": ["ledger_unreadable"]},
        )
    ]
    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts


def test_after_batch_unreadable_ledger_records_integrity_instead_of_empty(monkeypatch):
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = object()
    monkeypatch.setattr(react_engine_module, "read_obligations", lambda _orchestrator: None)

    engine._sweep_job_obligations()

    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    fact = next(
        fact
        for fact in engine.run_evidence_state.facts
        if fact.key == "job_barrier_integrity_failure"
    )
    assert fact.value["failures"] == ["ledger_unreadable_after_action_batch"]


def test_close_live_projection_does_not_claim_live_when_ledger_is_unknown(monkeypatch):
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = object()
    monkeypatch.setattr(react_engine_module, "read_obligations", lambda _orchestrator: None)

    engine._record_live_jobs_at_close((JOB,), "deadline")

    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    assert not any(
        fact.key == f"job_live_at_close.{JOB}" for fact in engine.run_evidence_state.facts
    )


def test_close_live_projection_drops_a_job_that_settled_after_wall_observation(monkeypatch):
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = object()
    settled = {
        "job_id": JOB,
        "process_state": "terminal",
        "settlement_state": "settled",
        "settled_receipt_id": "inv-gradle-1-0001",
    }
    monkeypatch.setattr(
        react_engine_module,
        "read_obligations",
        lambda _orchestrator: [settled],
    )

    engine._record_live_jobs_at_close((JOB,), "deadline")

    assert not any(
        fact.key == f"job_live_at_close.{JOB}" for fact in engine.run_evidence_state.facts
    )
    assert f"job_live_at_close:{JOB}" not in engine.run_evidence_state.conflicts


def test_close_unsettled_scan_does_not_treat_unknown_ledger_as_empty(monkeypatch):
    engine = _engine([_text_turn()], max_iterations=1)
    engine.orchestrator = object()
    monkeypatch.setattr(react_engine_module, "read_obligations", lambda _orchestrator: None)

    engine._record_unsettled_job_conflicts("deadline")

    assert "job_barrier_integrity_failure" in engine.run_evidence_state.conflicts
    assert not any(
        fact.key.startswith("job_live_at_close.") for fact in engine.run_evidence_state.facts
    )
