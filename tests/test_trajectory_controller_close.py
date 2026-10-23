"""A report action followed by its own controller gate, as in Commons CLI."""

import json

import pytest

from sag.trajectory.reducer import DeltaAccumulator, TrajectoryReducer


def event(sequence, kind, **payload):
    return json.dumps({"sequence": sequence, "kind": kind, "payload": payload})


@pytest.mark.parametrize("revision", [None, "before_record", "after_record"])
def test_controller_gate_moves_to_its_stated_turn_and_keeps_revisions(revision):
    reducer = TrajectoryReducer()
    accumulated = DeltaAccumulator()

    def feed(line):
        accumulated.feed(reducer.feed(line))
        assert accumulated.snapshot() == reducer.snapshot()

    feed(event(1, "action_envelope", envelope_id="report-call", tool="report",
               intent_source="controller", exact_params={"action": "generate"}))
    feed(event(2, "turn_record", turn_id=1, actor="controller", phase="report",
               envelope_ref="report-call", iteration=8))
    feed(event(3, "gate_decision", decision_id="report-gate", phase="report",
               expected_outcome="success", expected_accepted=True, code="report_present",
               reason="persisted report bytes match the current sealed snapshot"))
    revised = dict(delivered_decision_id="report-gate", revised_decision_id="revised-gate",
                   phase="report", revised_outcome="failed", revised_accepted=True,
                   reason="report bytes changed", code="report_changed")
    if revision == "before_record":
        feed(event(4, "gate_outcome_revised", **revised))
    feed(event(5, "turn_record", turn_id=2, actor="controller", phase="report",
               gate_decision_id="report-gate", iteration=8, observation_ref="output_close"))
    if revision == "after_record":
        feed(event(6, "gate_outcome_revised", **revised))

    action, close = reducer.snapshot().turns
    assert action.call.tool == "report" and action.gate is None
    assert action.control_seq == [1, 2]
    assert close.call is None and close.actor == "controller"
    assert close.observation.ref == "output_close"
    assert close.gate.accepted is True
    assert close.gate.word == ("failed" if revision else "success")
    assert close.gate.code == ("report_changed" if revision else "report_present")
    assert 3 in close.control_seq and 5 in close.control_seq
    assert close.tokens is None and close.advisor_tokens is None


def test_unresolved_gate_id_does_not_borrow_a_previous_success():
    reducer = TrajectoryReducer()
    reducer.feed(event(1, "turn_record", turn_id=1, actor="model", phase="report"))
    reducer.feed(event(2, "gate_decision", decision_id="previous-gate", phase="report",
                       expected_outcome="success", expected_accepted=True))
    reducer.feed(event(3, "turn_record", turn_id=2, actor="controller", phase="report",
                       gate_decision_id="missing-gate"))
    assert reducer.snapshot().turns[-1].gate is None
