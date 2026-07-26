# tests/test_evidence_check_nudge.py
"""The mid-phase EVIDENCE CHECK obeys the §3.3 rejection-message standard.

The nudge exists because a model can sit on green evidence for dozens of
iterations. What it may say is the machine-derived state: the gate result, the
validator's own reason, the refs it rests on, and the one thing the phase
record still lacks. What it may NOT do is dictate the closure call — a message
that spells out `phase(action='done', outcome='success', ...)` teaches the
model to type the words rather than to judge the evidence, which is exactly the
rejected pattern.
"""

from types import SimpleNamespace

from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import StepType


def _engine(gate):
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine()
    engine.steps = []
    engine._phase_iterations = ReActEngine.NUDGE_EVERY
    engine._get_timestamp = lambda: "2026-07-26 00:00:00"
    engine.agent_logger = SimpleNamespace(info=lambda *a, **k: None)
    engine._phase_gate_check = lambda phase: gate
    return engine


GREEN = {
    "ok": True,
    "reason": "surefire reports present for 4 modules",
    "suggestions": [],
    "validator_state": "green",
    "evidence_refs": ["output_17", "output_18"],
}


def _nudge_text(engine):
    guidance = [s for s in engine.steps if s.step_type == StepType.SYSTEM_GUIDANCE]
    assert len(guidance) == 1
    return guidance[0].content


def test_nudge_states_the_gate_result_and_the_validator_reason():
    engine = _engine(GREEN)

    assert engine._maybe_nudge_phase_done() is True

    text = _nudge_text(engine)
    assert "EVIDENCE CHECK" in text
    assert "provision" in text
    assert "surefire reports present for 4 modules" in text


def test_nudge_names_the_concrete_missing_item():
    engine = _engine(GREEN)
    engine._maybe_nudge_phase_done()

    text = _nudge_text(engine)
    # The one thing the machine can point at: this attempt has no recorded
    # outcome. Naming the attempt makes it checkable rather than rhetorical.
    assert str(engine.phase_machine.current_attempt_id) in text
    assert "outcome" in text


def test_nudge_cites_the_evidence_refs_the_gate_used():
    engine = _engine(GREEN)
    engine._maybe_nudge_phase_done()

    text = _nudge_text(engine)
    assert "output_17" in text


def test_nudge_never_spells_out_the_closure_call():
    engine = _engine(GREEN)
    engine._maybe_nudge_phase_done()

    text = _nudge_text(engine)
    assert "phase(action='done'" not in text
    assert 'phase(action="done"' not in text
    assert "outcome='success'" not in text


def test_no_nudge_when_the_gate_does_not_pass():
    engine = _engine({"ok": False, "reason": "no artifacts", "suggestions": []})

    assert engine._maybe_nudge_phase_done() is False
    assert engine.steps == []
