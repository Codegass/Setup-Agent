"""Large report sets must not overflow the controller's repair record."""

from dataclasses import replace

import pytest
from container_evidence_fakes import ScriptedOrchestrator
from test_gate_one_word_fence import _claim, _engine, _gate

from sag.agent.evidence_assessments import validate_assessment_v2
from sag.agent.phase_gates import GateControlDisposition, PhaseOutcome
from sag.agent.repair_contexts import build_repair_context, repair_context_sha256
from sag.agent.replay import (
    ReplayValidationError, _repair_assessment_id_for_gate, _validated_opened_repair_context,
)


def repair_case(tmp_path, refs):
    engine = _engine(tmp_path)
    engine.orchestrator = ScriptedOrchestrator()
    engine.successful_states = {"working_directory": "/workspace/proj"}
    claim = _claim(PhaseOutcome.SUCCESS)
    gate = replace(
        _gate(PhaseOutcome.FAILED, claim=claim, accepted=False, code="build_red"),
        control_disposition=GateControlDisposition.REPAIR_REQUIRED,
        evidence_refs=tuple(refs),
    )
    return engine, claim, gate


@pytest.mark.parametrize("report_count", [0, 1, 64, 121, 1000])
def test_report_set_size_does_not_close_an_otherwise_repairable_phase(tmp_path, report_count):
    refs = [f"/workspace/proj/target/surefire-reports/TEST-case{i}.xml" for i in range(report_count)]
    engine, claim, gate = repair_case(tmp_path, refs)
    assessment = engine._gate_control_assessment(claim, gate)
    validate_assessment_v2(assessment.payload())

    context, status = engine._install_repair_context(claim, gate)
    assert status == "persisted"
    assert context is not None
    assert set(context.observed_fact_refs) == {assessment.assessment_id, gate.decision_id}
    assert assessment.evidence_refs == (gate.decision_id,)
    assert gate.evidence_refs == tuple(refs)
    assert gate.accepted is False
    assert gate.validated_outcome == PhaseOutcome.FAILED

    event = engine._emit_control_gate(claim, gate)
    assert event.payload["evidence_refs"] == refs
    payload = {
        "source_phase_attempt_id": engine.phase_machine.current_attempt_id,
        "context": context.model_dump(mode="json"),
        "context_sha256": repair_context_sha256(context),
    }
    assert _validated_opened_repair_context(payload, source_gate=event.payload) == context


def test_repair_link_does_not_admit_an_unrelated_gate(tmp_path):
    engine, claim, gate = repair_case(tmp_path, ["output-current"])
    context, status = engine._install_repair_context(claim, gate)
    assert status == "persisted"
    context = context.model_copy(update={"observed_fact_refs": (context.trigger_assessment_id, "gate-unrelated")})
    event = engine._emit_control_gate(claim, gate)
    with pytest.raises(ReplayValidationError, match="observed refs|source gate"):
        _validated_opened_repair_context(
            {
                "source_phase_attempt_id": engine.phase_machine.current_attempt_id,
                "context": context.model_dump(mode="json"),
                "context_sha256": repair_context_sha256(context),
            },
            source_gate=event.payload,
        )


def test_historical_direct_evidence_context_remains_replayable(tmp_path):
    engine, claim, gate = repair_case(tmp_path, ["output-current"])
    context, status = engine._install_repair_context(claim, gate)
    assert status == "persisted"
    event = engine._emit_control_gate(claim, gate)
    trigger = _repair_assessment_id_for_gate(
        event.payload, phase_attempt_id=engine.phase_machine.current_attempt_id, decision_linked=False,
    )
    context = build_repair_context(
        trigger_assessment_id=trigger, typed_blocker=gate.code,
        blocker_owner=gate.blocker_owner.value, observed_fact_refs=(trigger, *gate.evidence_refs),
    )
    assert _validated_opened_repair_context(
        {
            "source_phase_attempt_id": engine.phase_machine.current_attempt_id,
            "context": context.model_dump(mode="json"),
            "context_sha256": repair_context_sha256(context),
        },
        source_gate=event.payload,
    ) == context


def test_distinct_linked_gate_decisions_cannot_collide_in_append_only_assessments(tmp_path):
    engine, claim, gate = repair_case(tmp_path, ["output-current"])
    first, status = engine._install_repair_context(claim, gate)
    assert status == "persisted"
    revised = replace(gate, reason="same evidence, more precise explanation")
    second, status = engine._install_repair_context(claim, revised)
    assert status == "persisted"
    assert first.trigger_assessment_id != second.trigger_assessment_id
