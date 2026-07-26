"""§3.5 cross-phase corrections: a later phase supersedes an earlier phase's
verified fact, and the replacement carries an auditable trace of what it
invalidated. Earlier records — facts and claims alike — are never edited."""

from sag.agent.evidence_state import FactStatus, RunEvidenceState, StateScope
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult


def _verified(facts, ref):
    return ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.SUCCESS,
        evidence_status=EvidenceStatus.VERIFIED,
        output="probe",
        facts=facts,
        refs=[ref],
    )


def test_later_phase_fact_supersedes_and_names_what_it_invalidated():
    state = RunEvidenceState(run_id="r-supersede")
    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_provision_gate",
        source_phase="provision",
        source_attempt_id="provision-1",
    )

    state.set_fact(
        "provision.venv_ready",
        False,
        evidence_ref="output_build_probe",
        source_phase="build",
        source_attempt_id="build-1",
    )

    assert state.fact_value("provision.venv_ready") is False
    assert state.fact_provenance("provision.venv_ready") == "output_build_probe"
    correction = state.facts[-1]
    assert correction.source_phase == "build"
    assert len(correction.superseded) == 1
    replaced = correction.superseded[0]
    assert replaced.value is True
    assert replaced.canonical_value == "true"
    assert replaced.provenance == "output_provision_gate"
    assert replaced.source_phase == "provision"
    assert replaced.source_attempt_id == "provision-1"


def test_superseded_earlier_records_are_never_edited():
    state = RunEvidenceState(run_id="r-append-only")
    state.register_claim(
        StateScope.ENVIRONMENT,
        "provision.venv_ready",
        True,
        "model_step_3",
        source_phase="provision",
    )
    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_provision_gate",
        source_phase="provision",
    )

    state.set_fact(
        "provision.venv_ready",
        False,
        evidence_ref="output_build_probe",
        source_phase="build",
    )

    claim, original, correction = state.facts
    assert len(state.facts) == 3
    assert claim.status is FactStatus.CLAIMED
    assert claim.value is True
    assert claim.provenance == "model_step_3"
    assert claim.superseded == ()
    assert original.status is FactStatus.VERIFIED
    assert original.value is True
    assert original.provenance == "output_provision_gate"
    assert original.source_phase == "provision"
    assert original.superseded == ()
    assert correction.superseded[0].provenance == "output_provision_gate"


def test_a_later_phase_claim_neither_supersedes_nor_traces():
    state = RunEvidenceState(run_id="r-claim-only")
    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_provision_gate",
        source_phase="provision",
    )

    state.register_claim(
        StateScope.ENVIRONMENT,
        "provision.venv_ready",
        False,
        "model_step_9",
        source_phase="build",
    )

    assert state.fact_value("provision.venv_ready") is True
    assert state.fact_provenance("provision.venv_ready") == "output_provision_gate"
    assert state.facts[-1].superseded == ()


def test_same_phase_reverification_is_not_a_cross_phase_correction():
    state = RunEvidenceState(run_id="r-same-phase")
    state.set_fact(
        "build.test_entry_ready",
        False,
        evidence_ref="artifact://missing",
        source_phase="build",
    )

    state.set_fact(
        "build.test_entry_ready",
        True,
        evidence_ref="artifact://classpath",
        source_phase="build",
    )

    assert state.fact_value("build.test_entry_ready") is True
    assert [fact.superseded for fact in state.facts] == [(), ()]


def test_reverifying_the_same_value_later_invalidates_nothing():
    state = RunEvidenceState(run_id="r-confirmation")
    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_provision_gate",
        source_phase="provision",
    )

    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_build_recheck",
        source_phase="build",
    )

    assert state.facts[-1].superseded == ()
    assert state.state_vector([StateScope.ENVIRONMENT]) == {"environment": 1}


def test_in_place_repair_flow_ends_on_the_build_phase_value():
    state = RunEvidenceState(run_id="r-repair")
    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_provision_gate",
        source_phase="provision",
        source_attempt_id="provision-1",
    )
    state.set_fact(
        "provision.venv_ready",
        False,
        evidence_ref="output_build_probe",
        source_phase="build",
        source_attempt_id="build-1",
    )

    state.set_fact(
        "provision.venv_ready",
        True,
        evidence_ref="output_build_repair",
        source_phase="build",
        source_attempt_id="build-1",
    )

    assert state.fact_value("provision.venv_ready") is True
    assert state.fact_provenance("provision.venv_ready") == "output_build_repair"
    assert state.facts[-1].source_phase == "build"
    assert [fact.provenance for fact in state.facts] == [
        "output_provision_gate",
        "output_build_probe",
        "output_build_repair",
    ]
    traced = [
        (fact.provenance, entry.provenance, entry.value)
        for fact in state.facts
        for entry in fact.superseded
    ]
    assert traced == [("output_build_probe", "output_provision_gate", True)]


def test_tool_ingested_facts_carry_the_cross_phase_trace():
    state = RunEvidenceState(run_id="r-ingest")
    state.ingest_tool_result(
        StateScope.ENVIRONMENT,
        "system",
        _verified({"java.version": "8"}, "output_1"),
        source_phase="provision",
        source_attempt_id="provision-1",
    )

    state.ingest_tool_result(
        StateScope.ENVIRONMENT,
        "system",
        _verified({"java.version": "17"}, "output_2"),
        source_phase="build",
        source_attempt_id="build-1",
    )

    assert state.fact_value("java.version") == "17"
    replaced = state.facts[-1].superseded[0]
    assert replaced.value == "8"
    assert replaced.source_phase == "provision"
    assert replaced.provenance == "output_1"


def test_supersession_trace_survives_serialization():
    state = RunEvidenceState(run_id="r-dump")
    state.set_fact(
        "dependencies.resolved",
        {"pip": "23.0"},
        evidence_ref="output_provision_gate",
        source_phase="provision",
    )
    state.set_fact(
        "dependencies.resolved",
        {"pip": "24.2"},
        evidence_ref="output_build_repair",
        source_phase="build",
    )
    state.seal(finalized_at="2026-07-26T12:00:00Z")

    dumped = state.model_dump()

    assert dumped["facts"][-1]["superseded"] == (
        {
            "value": {"pip": "23.0"},
            "canonical_value": '{"pip":"23.0"}',
            "provenance": "output_provision_gate",
            "source_phase": "provision",
            "source_attempt_id": None,
        },
    )
    assert state.model_dump_json() == state.model_dump_json()
