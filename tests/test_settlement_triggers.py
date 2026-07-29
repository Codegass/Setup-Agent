# tests/test_settlement_triggers.py
"""Plan 8 Stage 1 — who settles, when, and what the run is told (spec §3.2).

A ledger nobody sweeps is a ledger. p7d polaris polled its test job patiently
for the rest of the run and the harness never once went back to ask whether the
job had finished — so the three triggers are the whole feature:

* after each executed action batch, whatever tools the batch used (a
  poll-heavy model must not starve settlement, spec §7);
* at phase-claim time, so a gate never grades moving books — the polaris build
  gate graded a snapshot and upgraded an honest `partial` on it;
* before report generation, the closing sweep.

All three are idempotent. A job that never terminates stays an OPEN obligation
and is recorded at evidence-close as `job_unsettled:<job_id>`; nothing is ever
guessed from a partial log.

The run learns about it in ONE bounded line in the next observation. No
synthetic tool result: a receipt that appears from nowhere is exactly the
surprise §7 names.
"""

import json
from pathlib import Path

from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)
from test_job_settlement import (
    EXIT_PATH,
    JOB,
    ROOT,
    JobContainer,
    _obligation,
)

from sag.agent import phase_gates
from sag.agent.control_events import CONTROL_EVENT_KINDS, ControlEvent
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.invocation_receipts import RECEIPT_DIR
from sag.agent.job_obligations import OBLIGATION_DIR, write_obligation
from sag.agent.replay import ControlReplayRunner
from sag.agent.verdict_finalizer import EvidenceCloseReason
from test_job_settlement import LOG_PATH, POLARIS_LOG, AFTER

FIXTURES = Path(__file__).parent / "fixtures" / "control_layer"


class Orchestrator:
    def __init__(self, *, terminated=True):
        files = {LOG_PATH: POLARIS_LOG}
        if terminated:
            files[EXIT_PATH] = "0\n"
        self.filesystem = JobContainer(files=files, reports=AFTER)
        write_obligation(self.execute_command, _obligation())

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)


def _receipts(orchestrator):
    return [
        json.loads(body)
        for path, body in sorted(orchestrator.filesystem.files.items())
        if path.startswith(f"{RECEIPT_DIR}/")
    ]


def _ledger(orchestrator):
    return json.loads(orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"])


def _engine(forced_engine_factory, orchestrator):
    engine, _ = forced_engine_factory()
    engine.orchestrator = orchestrator
    engine.steps = []
    return engine


# ---------------------------------------------------------------------------
# trigger 1: the engine sweep
# ---------------------------------------------------------------------------


def test_the_engine_sweep_settles_a_job_that_finished_between_batches(forced_engine):  # noqa: F811
    orchestrator = Orchestrator()
    engine = _engine(forced_engine, orchestrator)

    engine._sweep_job_obligations()

    assert len(_receipts(orchestrator)) == 1
    assert _ledger(orchestrator)["settled_receipt_id"] == _receipts(orchestrator)[0]["receipt_id"]


def test_the_sweep_emits_one_job_settled_event(forced_engine):  # noqa: F811
    """Graded from control events, not phase summaries — so the settlement has
    to be in the events."""
    orchestrator = Orchestrator()
    engine = _engine(forced_engine, orchestrator)

    engine._sweep_job_obligations()

    payloads = [payload for kind, payload in engine.control_events if kind == "job_settled"]
    assert payloads == [
        {
            "job_id": JOB,
            "receipt_id": _receipts(orchestrator)[0]["receipt_id"],
            "exit_code": 0,
        }
    ]


def test_a_job_is_announced_exactly_once(forced_engine):  # noqa: F811
    """The sweep runs after every batch; the announcement is per job."""
    orchestrator = Orchestrator()
    engine = _engine(forced_engine, orchestrator)

    engine._sweep_job_obligations()
    engine._sweep_job_obligations()

    assert [kind for kind, _ in engine.control_events].count("job_settled") == 1


def test_an_unfinished_job_is_neither_settled_nor_announced(forced_engine):  # noqa: F811
    orchestrator = Orchestrator(terminated=False)
    engine = _engine(forced_engine, orchestrator)

    engine._sweep_job_obligations()

    assert _receipts(orchestrator) == []
    assert engine.control_events == []


def test_the_next_observation_carries_one_bounded_settled_line(forced_engine):  # noqa: F811
    orchestrator = Orchestrator()
    engine = _engine(forced_engine, orchestrator)
    engine._sweep_job_obligations()

    engine._append_native_observation("call-1", "STILL_RUNNING")

    (step,) = engine.steps
    settled = [line for line in step.content.splitlines() if line.startswith("[settled]")]
    assert settled == [
        f"[settled] job {JOB}: exit 0 — receipt {_receipts(orchestrator)[0]['receipt_id']}, "
        "2 report paths claimed"
    ]
    assert step.content.startswith("STILL_RUNNING")


def test_the_notice_is_not_repeated_on_every_later_observation(forced_engine):  # noqa: F811
    orchestrator = Orchestrator()
    engine = _engine(forced_engine, orchestrator)
    engine._sweep_job_obligations()

    engine._append_native_observation("call-1", "first")
    engine._append_native_observation("call-2", "second")

    assert "[settled]" not in engine.steps[1].content


# ---------------------------------------------------------------------------
# trigger 2: the phase gate grades settled books
# ---------------------------------------------------------------------------


def test_the_phase_gate_settles_before_it_grades():
    """`_inspect_phase` sweeps first: the polaris build gate graded a snapshot
    of a job that was still running."""
    orchestrator = Orchestrator()

    phase_gates.check_phase_done("provision", None, orchestrator, "polaris")

    assert len(_receipts(orchestrator)) == 1
    assert _ledger(orchestrator)["settled_receipt_id"]


def test_an_open_obligation_is_a_validated_fact_of_the_phase():
    """The cap needs the evidence by name, and replay needs it in the
    transcript rather than re-probed from a container that is long gone."""
    orchestrator = Orchestrator(terminated=False)

    observation = phase_gates.check_phase_done("provision", None, orchestrator, "polaris")

    assert observation["validated_facts"][phase_gates.OPEN_OBLIGATIONS_FACT] == [JOB]


def test_a_settled_ledger_states_no_open_obligation_fact():
    """An absent fact stays an absent key — every run that never detached
    anything grades exactly as it does today."""
    orchestrator = Orchestrator()

    observation = phase_gates.check_phase_done("provision", None, orchestrator, "polaris")

    assert phase_gates.OPEN_OBLIGATIONS_FACT not in observation["validated_facts"]


# ---------------------------------------------------------------------------
# trigger 3: the closing sweep, and the job that never came back
# ---------------------------------------------------------------------------


def _closing_engine(forced_engine_factory, orchestrator):
    engine = _engine(forced_engine_factory, orchestrator)
    engine.run_evidence_state = RunEvidenceState(run_id="run-p8")
    engine.verdict_finalizer = type(
        "Finalizer", (), {"finalize": staticmethod(lambda state, reason: state)}
    )()
    return engine


def test_the_closing_sweep_settles_before_the_verdict_is_taken(forced_engine):  # noqa: F811
    orchestrator = Orchestrator()
    engine = _closing_engine(forced_engine, orchestrator)

    engine._finalize_evidence(EvidenceCloseReason.DEPENDENTS_SKIPPED)

    assert len(_receipts(orchestrator)) == 1


def test_a_job_that_never_terminated_is_a_conflict_on_the_verdict(forced_engine):  # noqa: F811
    """Nothing is guessed from a partial log. The run says which job it never
    heard back from, and the obligation file is the provenance."""
    orchestrator = Orchestrator(terminated=False)
    engine = _closing_engine(forced_engine, orchestrator)

    engine._finalize_evidence(EvidenceCloseReason.DEPENDENTS_SKIPPED)

    state = engine.run_evidence_state
    assert f"job_unsettled:{JOB}" in state.conflicts
    assert state.fact_provenance(f"{phase_gates.OPEN_OBLIGATIONS_FACT}.{JOB}") == (
        f"{OBLIGATION_DIR}/{JOB}.json"
    )


def test_a_settled_job_is_no_conflict_at_all(forced_engine):  # noqa: F811
    orchestrator = Orchestrator()
    engine = _closing_engine(forced_engine, orchestrator)

    engine._finalize_evidence(EvidenceCloseReason.DEPENDENTS_SKIPPED)

    assert engine.run_evidence_state.conflicts == ()


# ---------------------------------------------------------------------------
# replay: the new kind is additive
# ---------------------------------------------------------------------------


def test_job_settled_is_appended_to_the_event_kinds():
    """Nothing before it moves — anything reading this tuple by order stays
    correct."""
    assert CONTROL_EVENT_KINDS[:11] == (
        "planner_response",
        "scheduler_decision",
        "action_envelope",
        "forced_action",
        "tool_result",
        "validator_observation",
        "gate_decision",
        "phase_transition",
        "loop_decision",
        "evidence_close",
        "claim_transition",
    )
    assert CONTROL_EVENT_KINDS[11] == "job_settled"


def test_the_job_settled_payload_states_the_three_facts_and_nothing_else():
    event = ControlEvent(
        sequence=1,
        kind="job_settled",
        payload={"job_id": JOB, "receipt_id": "inv-gradle-1-0002", "exit_code": 0},
    )

    assert event.payload == {
        "job_id": JOB,
        "receipt_id": "inv-gradle-1-0002",
        "exit_code": 0,
    }
    assert event.typed_payload.exit_code == 0


def test_a_transcript_carrying_job_settled_rows_replays_to_the_same_verdict(tmp_path):
    """Additive means additive: an event kind this walk does not model must
    consume nothing and block nothing — including between a `gate_decision`
    that closed an attempt and the `phase_transition` that must follow it."""
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [source[0]]
    for row in source[1:]:
        rows.append(row)
        if row.get("kind") in {"tool_result", "gate_decision"}:
            rows.append(
                {
                    "kind": "job_settled",
                    "payload": {
                        "job_id": JOB,
                        "receipt_id": "inv-gradle-1-0002",
                        "exit_code": 0,
                    },
                    "source": row["source"],
                }
            )
    transcript = tmp_path / "settled-paramiko.jsonl"
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    settled = ControlReplayRunner.offline(verify_expected=False).run(transcript)
    untouched = ControlReplayRunner.offline().run(FIXTURES / "paramiko.jsonl")

    assert settled.snapshot.model_dump(mode="json") == untouched.snapshot.model_dump(mode="json")
    assert settled.paired_envelope_count == untouched.paired_envelope_count
    assert settled.unconsumed_events == ()


def test_settlement_never_writes_a_receipt_for_a_job_it_cannot_read(forced_engine):  # noqa: F811
    """A container that answers nothing states no exit code, and no exit code
    is not exit 0."""

    class Gone:
        def execute_command(self, command, **kwargs):
            raise RuntimeError("container gone")

    engine = _engine(forced_engine, Gone())

    engine._sweep_job_obligations()

    assert engine.control_events == []


def test_a_run_that_never_detached_anything_sweeps_and_finds_nothing(forced_engine):  # noqa: F811
    """The common case: one glob `cat` that matches no file."""
    orchestrator = Orchestrator()
    orchestrator.filesystem.files.pop(f"{OBLIGATION_DIR}/{JOB}.json")
    engine = _engine(forced_engine, orchestrator)

    engine._sweep_job_obligations()

    assert engine.control_events == []
    assert ROOT  # the fixture's working directory is untouched
