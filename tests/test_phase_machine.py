"""Engine-owned phase skeleton (spec §3.1): fixed order, evidence-gated
advancement, always-available escape valve, honest records."""

import pytest

from sag.agent.phase_machine import (
    PHASE_NAMES,
    PhaseAttemptRecord,
    PhaseMachine,
    PhaseOutcome,
    PhaseTermination,
)


def test_phase_order_is_fixed():
    assert PHASE_NAMES == ["provision", "analyze", "build", "test", "report"]


def test_starts_in_provision():
    m = PhaseMachine()
    assert m.current_phase == "provision"
    assert m.is_complete is False


def test_done_advances_and_records_key_results():
    m = PhaseMachine()
    m.mark_done("cloned repo; JDK 17 installed", evidence=["overlay:java"])
    assert m.current_phase == "analyze"
    rec = m.records[0]
    assert isinstance(rec, PhaseAttemptRecord)
    assert rec.phase == "provision"
    assert rec.termination is PhaseTermination.COMPLETED
    assert rec.outcome is PhaseOutcome.UNKNOWN
    assert rec.legacy_claim is True
    assert rec.key_results == "cloned repo; JDK 17 installed"
    assert rec.evidence == ("overlay:java",)


def test_blocked_advances_with_honest_record():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("gradle kts detected", evidence=[])
    m.mark_blocked("develocity plugin unresolvable", evidence=["job:abc"])
    assert m.current_phase == "test"
    assert m.records[2].termination is PhaseTermination.BLOCKED
    assert m.records[2].outcome is PhaseOutcome.UNKNOWN
    assert m.records[2].reason == "develocity plugin unresolvable"


def test_complete_after_report_phase():
    m = PhaseMachine()
    for _ in PHASE_NAMES:
        m.mark_done("ok", evidence=[])
    assert m.is_complete is True
    with pytest.raises(RuntimeError):
        m.mark_done("again", evidence=[])


def test_termination_state_reports_completed_after_all_phases_terminate():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_blocked("no tests runnable", evidence=[])
    m.mark_done("report written", evidence=[])
    assert m.termination_state() == "completed"


def test_termination_state_does_not_leak_phase_outcomes():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_blocked("cannot compile", evidence=[])
    m.mark_blocked("no build, no tests", evidence=[])
    m.mark_done("report written", evidence=[])
    assert m.termination_state() == "completed"


def test_abort_records_current_attempt_without_advancing_or_duplicating_cleanup():
    m = PhaseMachine()

    first = m.record_abort("wall clock exceeded", evidence=["job:build"])
    second = m.record_abort("wall clock exceeded", evidence=["job:build"])

    assert first is second
    assert len(m.records) == 1
    assert m.current_phase == "provision"
    assert m.termination_state() == "aborted"
    assert m.records[0].reason == "wall clock exceeded"
    assert "ABORTED" in m.digest_lines()[0]


def test_digest_lines_for_prompt():
    m = PhaseMachine()
    m.mark_done("JDK 17 + repo at /workspace/p", evidence=[])
    lines = m.digest_lines()
    assert any("provision" in l and "JDK 17" in l for l in lines)
    assert any("analyze" in l and ("current" in l.lower() or "→" in l) for l in lines)
