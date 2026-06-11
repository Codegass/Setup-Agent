"""Engine-owned phase skeleton (spec §3.1): fixed order, evidence-gated
advancement, always-available escape valve, honest records."""

import pytest

from sag.agent.phase_machine import PHASE_NAMES, PhaseMachine


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
    assert rec.name == "provision"
    assert rec.status == "done"
    assert rec.key_results == "cloned repo; JDK 17 installed"
    assert rec.evidence == ["overlay:java"]


def test_blocked_advances_with_honest_record():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("gradle kts detected", evidence=[])
    m.mark_blocked("develocity plugin unresolvable", evidence=["job:abc"])
    assert m.current_phase == "test"
    assert m.records[2].status == "blocked"
    assert m.records[2].reason == "develocity plugin unresolvable"


def test_complete_after_report_phase():
    m = PhaseMachine()
    for _ in PHASE_NAMES:
        m.mark_done("ok", evidence=[])
    assert m.is_complete is True
    with pytest.raises(RuntimeError):
        m.mark_done("again", evidence=[])


def test_overall_outcome_degrades_on_blocks():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_blocked("no tests runnable", evidence=[])
    m.mark_done("report written", evidence=[])
    assert m.overall_outcome() == "partial"   # core phase blocked


def test_outcome_failed_when_build_blocked():
    m = PhaseMachine()
    m.mark_done("ok", evidence=[])
    m.mark_done("ok", evidence=[])
    m.mark_blocked("cannot compile", evidence=[])
    m.mark_blocked("no build, no tests", evidence=[])
    m.mark_done("report written", evidence=[])
    assert m.overall_outcome() == "failed"


def test_digest_lines_for_prompt():
    m = PhaseMachine()
    m.mark_done("JDK 17 + repo at /workspace/p", evidence=[])
    lines = m.digest_lines()
    assert any("provision" in l and "JDK 17" in l for l in lines)
    assert any("analyze" in l and ("current" in l.lower() or "→" in l) for l in lines)
