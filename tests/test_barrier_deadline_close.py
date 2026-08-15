"""Task #53 — the wait that reaches the report reserve closes the phase, not the run.

The p7d camel finding corrected the premise: the controller barrier DID poll.
It polled `2c4d56b2fdca` 160 times across 5,033.9s, verified it live at the
deadline, and returned the honest status `live_at_deadline`. What it could not
do was hand that status to a call site that knew the difference: the loop top
treated a BUDGET outcome exactly like an integrity failure and called
`self.abort(...)`, which threw away the 600s report reserve `_hold_deadline`
had just spent 84 minutes defending, recorded the test phase as
`termination: aborted / claim: null`, and skipped the report phase entirely.

So the deadline becomes an honest blocked close — the job is named, the phase
closes BLOCKED, dependents skip, evidence closes and the report delivers, the
same containment #45 built for a control-persist exhaustion. Integrity
families keep abort semantics untouched.

A disclosure is also the controller's LAST word about that job: `job_settled`
after `job_live_at_close` is not a lifecycle any replay can walk, and a job
still counted as a live barrier makes every later gate return
`job_controller_barrier`/WAIT_REQUIRED — an ungradable report phase.
"""

import pytest
from test_job_barrier import PROCESS_PROVENANCE
from test_job_settlement import JOB, _orchestrator, _with_obligation
from test_native_loop_engine import _engine, _phase_turn
from test_settlement_triggers import JOB as SETTLEMENT_JOB
from test_settlement_triggers import Orchestrator as SettlementOrchestrator

from sag.agent import phase_gates
from sag.agent.job_obligations import OBLIGATION_DIR
from sag.agent.phase_gates import (
    JOB_BARRIER_FACT,
    OPEN_OBLIGATIONS_FACT,
    ValidatorState,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome, PhaseTermination
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.verdict_finalizer import ReportDeliveryStatus, RunTerminationStatus
from sag.tools.base import BaseTool, ToolResult


class _ReportTool(BaseTool):
    """A report tool that delivers, so delivery status is the loop's answer."""

    def __init__(self):
        super().__init__("report", "Deliver the run report")
        self.calls = 0

    def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult.completed_success(output="report delivered")


def _report_turn(index):
    return NativeTurn(
        text="Delivering the report.",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name="report",
                arguments={},
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )


def _loop_engine(turns, *, barrier_status, disclose=True):
    """A phase-mode engine whose barrier answers `barrier_status` in `test`."""
    engine = _engine(turns)
    report_tool = _ReportTool()
    engine.tools["report"] = report_tool
    engine._get_tool_orchestrator().tools["report"] = report_tool
    engine.report_tool = report_tool

    def barrier(*_args, **_kwargs):
        if engine.phase_machine.current_phase != "test":
            return "cleared"
        if disclose and not engine.run_evidence_state.sealed:
            engine.run_evidence_state.record_conflict(f"job_live_at_close:{JOB}")
        return barrier_status

    engine._drain_job_barrier = barrier
    return engine


def test_a_job_live_at_the_report_reserve_closes_the_phase_and_delivers_the_report():
    engine = _loop_engine(
        [
            _phase_turn(1),  # provision
            _phase_turn(2),  # analyze
            _phase_turn(3),  # build
            _report_turn(4),  # report phase: the deliverable the abort discarded
            _phase_turn(5),  # report closes
        ],
        barrier_status="live_at_deadline",
    )

    termination = engine.run_setup_loop("set up the project", max_iterations=12)

    records = engine.phase_machine.records
    test_record = next(record for record in records if record.phase == "test")
    assert test_record.termination is PhaseTermination.BLOCKED
    assert test_record.claim is not None
    assert JOB in test_record.reason
    assert all(record.termination is not PhaseTermination.ABORTED for record in records)
    assert any(record.phase == "report" for record in records)
    assert engine.report_tool.calls == 1
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert termination.report_delivery_status is ReportDeliveryStatus.DELIVERED
    # The reserve was spent on the report, and the close states a known reason.
    assert engine.run_evidence_state.close_reason == "test_terminated"


@pytest.mark.parametrize("status", ["integrity_failure", "evidence_unpersisted"])
def test_an_integrity_family_barrier_status_still_aborts_the_run(status):
    engine = _loop_engine(
        [_phase_turn(index) for index in range(1, 6)],
        barrier_status=status,
        disclose=False,
    )

    termination = engine.run_setup_loop("set up the project", max_iterations=12)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert termination.report_delivery_status is ReportDeliveryStatus.SKIPPED
    assert engine.phase_machine.records[-1].termination is PhaseTermination.ABORTED


def test_the_deadline_close_needs_a_disclosed_job_or_it_stays_an_abort():
    """No disclosure means no honest sentence to close on — the abort stands."""
    engine = _loop_engine(
        [_phase_turn(index) for index in range(1, 6)],
        barrier_status="live_at_deadline",
        disclose=False,
    )

    termination = engine.run_setup_loop("set up the project", max_iterations=12)

    assert termination.termination is RunTerminationStatus.ABORTED


def test_a_disclosed_job_leaves_the_gate_barrier_but_keeps_the_settlement_cap():
    """The gate may grade again; it still may not call the phase a success."""
    orchestrator = SettlementOrchestrator(terminated=False)

    probe = phase_gates.check_phase_done(
        "provision",
        None,
        orchestrator,
        "polaris",
        disclosed_job_ids=(SETTLEMENT_JOB,),
    )

    facts = probe["validated_facts"]
    assert JOB_BARRIER_FACT not in facts
    assert facts[OPEN_OBLIGATIONS_FACT] == [SETTLEMENT_JOB]
    assert probe["code"] != "job_controller_barrier"

    gate = validate_phase_claim(
        PhaseClaim(phase="provision", claimed_outcome=PhaseOutcome.UNKNOWN),
        ValidatorState.UNAVAILABLE,
        validated_facts=facts,
    )
    assert gate.accepted
    assert gate.code != "job_controller_barrier"


def test_the_undisclosed_job_still_holds_the_gate_barrier():
    orchestrator = SettlementOrchestrator(terminated=False)

    probe = phase_gates.check_phase_done("provision", None, orchestrator, "polaris")

    assert probe["code"] == "job_controller_barrier"
    assert probe["validated_facts"][JOB_BARRIER_FACT] == [
        {"job_id": SETTLEMENT_JOB, "state": "running"}
    ]


def test_the_barrier_stops_waiting_on_a_job_it_already_disclosed():
    orchestrator = _with_obligation(_orchestrator(exit_code=None), **PROCESS_PROVENANCE)
    engine = _engine([], max_iterations=1)
    engine.orchestrator = orchestrator
    engine._run_started_at = 0.0
    engine._wall_clock_cap = 7_200.0
    engine.run_evidence_state.record_conflict(f"job_live_at_close:{JOB}")

    status = engine._drain_job_barrier(now=lambda: 0.0, sleep=lambda _seconds: None)

    assert status == "cleared"


def test_a_disclosed_job_speaks_no_further_lifecycle_word():
    """`job_settled` after `job_live_at_close` is not a lifecycle replay walks."""
    orchestrator = _with_obligation(_orchestrator(exit_code="0"))
    engine = _engine([], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.steps = []
    events = []
    engine._emit_control_event = lambda kind, payload: events.append(kind)
    engine.run_evidence_state.record_conflict(f"job_live_at_close:{JOB}")

    engine._sweep_job_obligations()

    assert events == []
    body = orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"]
    assert '"settlement_state": "settled"' not in body


def test_an_undisclosed_job_still_settles_after_an_action_batch():
    orchestrator = _with_obligation(_orchestrator(exit_code="0"))
    engine = _engine([], max_iterations=1)
    engine.orchestrator = orchestrator
    engine.steps = []
    events = []
    engine._emit_control_event = lambda kind, payload: events.append(kind)

    engine._sweep_job_obligations()

    assert "job_settled" in events
    body = orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"]
    assert '"settlement_state": "settled"' in body
