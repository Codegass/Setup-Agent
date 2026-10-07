"""A consult may react to facts, but cannot authorize execution or success."""

from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.tools.base import ToolResult
from test_advisor_tool import _advisor_engine


def engine_with_state():
    engine = _advisor_engine(phase="build")
    engine.config.advisor_trigger_policy = "problems"
    engine.run_evidence_state = RunEvidenceState(run_id="problem-policy")
    engine.consults = []
    engine._append_entry_consult_pair = lambda: engine.consults.append(
        engine._active_advisor_trigger
    )
    return engine


def observe(engine, tool, result, identity, **params):
    engine.run_evidence_state.ingest_tool_result(
        StateScope.ARTIFACTS,
        tool,
        result,
        execution_id=identity,
        source_phase="build",
        params=params,
    )


def test_reads_do_not_rearm_an_identical_failure_but_a_changed_command_does():
    engine = engine_with_state()
    assert engine._maybe_consult_advisor_at_phase_entry() is False
    failure = ToolResult.completed_failure(
        output="compile failed", error_code="BUILD_FAILED", error="compile failed"
    )
    observe(engine, "maven", failure, "build-1", command="mvn test")
    assert engine._maybe_consult_advisor_for_problem() is True
    observe(engine, "search", ToolResult.completed_success(output="found log"), "read-1")
    observe(engine, "maven", failure, "build-2", command="mvn test")
    assert engine._maybe_consult_advisor_for_problem() is False
    observe(engine, "maven", failure, "build-3", command="mvn -Pci test")
    assert engine._maybe_consult_advisor_for_problem() is True
    observe(
        engine,
        "maven",
        ToolResult.completed_success(output="done"),
        "build-4",
        command="mvn -Pci test",
    )
    assert engine._maybe_consult_advisor_for_problem() is False
    assert len(engine.consults) == 2


def test_new_conflict_triggers_and_provider_failure_does_not_loop_or_mutate_evidence():
    engine = engine_with_state()
    engine.run_evidence_state.record_conflict("receipt and command scope differ")
    before = engine.run_evidence_state.model_dump_json()
    engine._append_entry_consult_pair = lambda: (_ for _ in ()).throw(
        RuntimeError("provider offline")
    )
    assert engine._maybe_consult_advisor_for_problem() is False
    engine._append_entry_consult_pair = lambda: engine.consults.append(
        engine._active_advisor_trigger
    )
    assert engine._maybe_consult_advisor_for_problem() is False
    assert engine.run_evidence_state.model_dump_json() == before
    engine.run_evidence_state.record_conflict("runtime identity differs")
    assert engine._maybe_consult_advisor_for_problem() is True


def test_changed_runtime_rearms_the_same_problem_but_reobserving_it_does_not():
    engine = engine_with_state()
    state = engine.run_evidence_state
    state.record_conflict("compiler runtime unresolved")
    state.set_fact("environment.java_version", "1.8.0_504", evidence_ref="probe-1")
    assert engine._maybe_consult_advisor_for_problem() is True
    state.set_fact("environment.java_version", "1.8.0_504", evidence_ref="probe-2")
    assert engine._maybe_consult_advisor_for_problem() is False
    state.set_fact("environment.java_version", "17.0.9", evidence_ref="probe-3")
    assert engine._maybe_consult_advisor_for_problem() is True
