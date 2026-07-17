import pytest

from sag.agent.evidence_state import FactStatus, RunEvidenceState, StateScope
from sag.evidence import EvidenceFinding, EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult


def test_duplicate_fact_does_not_advance_epoch():
    state = RunEvidenceState(run_id="r1")

    first = state.register_fact(StateScope.ENVIRONMENT, "java.version", "17", "output_1")
    duplicate = state.register_fact(StateScope.ENVIRONMENT, "java.version", "17", "output_2")

    assert first.changed is True
    assert duplicate.changed is False
    assert state.state_vector([StateScope.ENVIRONMENT]) == {"environment": 1}
    assert [fact.provenance for fact in state.facts] == ["output_1", "output_2"]


def test_unrelated_scope_does_not_change_build_vector():
    state = RunEvidenceState(run_id="r1")
    scopes = [StateScope.ENVIRONMENT, StateScope.DEPENDENCIES, StateScope.ARTIFACTS]

    before = state.state_vector(scopes)
    state.register_fact(StateScope.PROJECT_ANALYSIS, "docs.digest", "abc", "output_1")

    assert state.state_vector(scopes) == before


def test_fact_values_use_stable_compact_json_for_dicts_and_lists():
    state = RunEvidenceState(run_id="r1")

    first = state.register_fact(
        StateScope.DEPENDENCIES,
        "resolved",
        {"z": [2, 1], "a": True},
        "output_1",
    )
    duplicate = state.register_fact(
        StateScope.DEPENDENCIES,
        "resolved",
        {"a": True, "z": [2, 1]},
        "output_2",
    )

    assert first.fact.canonical_value == '{"a":true,"z":[2,1]}'
    assert duplicate.changed is False
    assert state.state_vector([StateScope.DEPENDENCIES]) == {"dependencies": 1}


def test_claim_is_recorded_without_advancing_verified_epoch():
    state = RunEvidenceState(run_id="r1")

    delta = state.register_claim(
        StateScope.ARTIFACTS,
        "wheel.path",
        "dist/project.whl",
        "model_step_1",
    )

    assert delta.changed is False
    assert delta.fact.status is FactStatus.CLAIMED
    assert state.state_vector([StateScope.ARTIFACTS]) == {"artifacts": 0}


def test_blocker_resolution_keeps_an_append_only_event_history():
    state = RunEvidenceState(run_id="r1")

    blocker = state.record_blocker(
        category="build",
        error_code="E1",
        failure_signature="sig",
    )
    resolution = state.resolve_blocker(blocker.blocker_id, resolution="installed JDK 17")

    assert len(state.blockers) == 1
    assert state.blockers[0].status == "resolved"
    assert [event.event for event in state.blocker_events] == ["recorded", "resolved"]
    assert resolution.status == "resolved"


def test_record_attempt_appends_a_state_vector_snapshot():
    state = RunEvidenceState(run_id="r1")
    state.register_fact(StateScope.ENVIRONMENT, "java.version", "17", "output_1")

    attempt = state.record_attempt(
        action="mvn test",
        relevant_scopes=[StateScope.ENVIRONMENT, StateScope.TEST_RUNTIME],
        evidence_refs=["output_2"],
    )

    assert state.action_attempts == (attempt,)
    assert attempt.state_vector == {"environment": 1, "test_runtime": 0}


def test_ingest_tool_result_preserves_the_observation_and_only_registers_verified_facts():
    state = RunEvidenceState(run_id="r1")
    verified = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.SUCCESS,
        evidence_status=EvidenceStatus.VERIFIED,
        output="Java 17",
        facts={"java.version": "17"},
        refs=["output_1"],
    )
    unknown = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.UNKNOWN,
        evidence_status=EvidenceStatus.UNKNOWN,
        output="maybe Java 21",
        facts={"java.version": "21"},
        refs=["output_2"],
    )

    verified_delta = state.ingest_tool_result(StateScope.ENVIRONMENT, "system", verified)
    unknown_delta = state.ingest_tool_result(StateScope.ENVIRONMENT, "system", unknown)

    assert verified_delta.changed is True
    assert unknown_delta.changed is False
    assert len(state.tool_observations) == 2
    assert state.tool_observations[0].result is not verified
    assert state.state_vector([StateScope.ENVIRONMENT]) == {"environment": 1}


def test_ingest_failed_tool_result_preserves_failure_provenance():
    state = RunEvidenceState(run_id="r1")
    failed = ToolResult.completed_failure(
        output="[ERROR] COMPILATION ERROR: cannot find symbol Widget",
        error="maven build failed",
        error_code="MAVEN_BUILD_FAILED",
        failure_signature="maven:compiler:missing-symbol",
        error_tail_preview="[ERROR] COMPILATION ERROR: cannot find symbol Widget",
    )

    state.ingest_tool_result(StateScope.TEST_RUNTIME, "maven", failed)

    observed = state.tool_observations[0].result
    assert observed.error_code == "MAVEN_BUILD_FAILED"
    assert observed.failure_signature == "maven:compiler:missing-symbol"
    assert observed.error_tail_preview == "[ERROR] COMPILATION ERROR: cannot find symbol Widget"
    assert observed.output_ref == failed.output_ref


def test_public_evidence_histories_are_read_only_and_detached_before_and_after_seal():
    state = RunEvidenceState(run_id="r1")
    fact_delta = state.register_fact(
        StateScope.ENVIRONMENT,
        "java.versions",
        {"installed": ["17"]},
        "output_1",
    )
    blocker = state.record_blocker(
        category="build",
        error_code="E1",
        failure_signature="sig",
        evidence_refs=["output_1"],
    )
    resolution = state.resolve_blocker(blocker.blocker_id, resolution="installed JDK 17")
    attempt = state.record_attempt(
        action="mvn test",
        relevant_scopes=[StateScope.ENVIRONMENT],
        evidence_refs=["output_2"],
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "maven",
        ToolResult(
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.UNKNOWN,
            evidence_status=EvidenceStatus.UNKNOWN,
            output="unknown",
            facts={"result": {"lines": ["before"]}},
            conflicts=["tool-conflict"],
            validator_findings=[
                EvidenceFinding(
                    type="validator",
                    reason="incomplete evidence",
                    details={"source": "tool"},
                )
            ],
        ),
    )

    for history in (
        state.facts,
        state.blockers,
        state.blocker_events,
        state.action_attempts,
        state.tool_observations,
        state.validator_findings,
        state.conflicts,
    ):
        assert isinstance(history, tuple)
        with pytest.raises(AttributeError):
            history.append("tampered")

    fact_delta.fact.value["installed"].append("21")
    blocker.category = "tampered"
    resolution.resolution = "tampered"
    attempt.state_vector["environment"] = 999
    state.facts[0].value["installed"].append("22")
    state.blockers[0].status = "tampered"
    state.blocker_events[1].resolution = "tampered"
    state.action_attempts[0].evidence_refs.append("output_tampered")
    state.tool_observations[0].result.facts["result"]["lines"].append("tampered")
    state.validator_findings[0].details["source"] = "tampered"

    assert state.facts[0].value == {"installed": ["17"]}
    assert state.blockers[0].category == "build"
    assert state.blockers[0].status == "resolved"
    assert state.blocker_events[1].resolution == "installed JDK 17"
    assert state.action_attempts[0].state_vector == {"environment": 1}
    assert state.action_attempts[0].evidence_refs == ["output_2"]
    assert state.tool_observations[0].result.facts == {"result": {"lines": ["before"]}}
    assert state.validator_findings[0].details == {"source": "tool"}

    state.seal(finalized_at="2026-07-17T00:00:00Z")
    with pytest.raises(AttributeError):
        state.facts.append("tampered after seal")
    state.facts[0].value["installed"].append("23")
    state.blockers[0].status = "tampered after seal"
    state.action_attempts[0].state_vector["environment"] = 1000
    state.tool_observations[0].result.facts["result"]["lines"].append("after seal")

    assert state.facts[0].value == {"installed": ["17"]}
    assert state.blockers[0].status == "resolved"
    assert state.action_attempts[0].state_vector == {"environment": 1}
    assert state.tool_observations[0].result.facts == {"result": {"lines": ["before"]}}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.register_fact(StateScope.ENVIRONMENT, "java.version", "17", "output_1"),
        lambda state: state.register_claim(StateScope.ENVIRONMENT, "java.version", "17", "model_1"),
        lambda state: state.record_blocker(
            category="build", error_code="E1", failure_signature="sig"
        ),
        lambda state: state.record_attempt(action="mvn test"),
        lambda state: state.ingest_tool_result(
            StateScope.ENVIRONMENT,
            "system",
            ToolResult(
                invocation_status=InvocationStatus.COMPLETED,
                operation_outcome=OperationOutcome.UNKNOWN,
                evidence_status=EvidenceStatus.UNKNOWN,
                output="unknown",
            ),
        ),
    ],
)
def test_sealed_state_rejects_mutation(mutation):
    state = RunEvidenceState(run_id="r1")
    state.seal(finalized_at="2026-07-15T00:00:00Z")

    with pytest.raises(RuntimeError, match="sealed"):
        mutation(state)


def test_seal_records_finalized_at_once():
    state = RunEvidenceState(run_id="r1")
    state.seal(finalized_at="2026-07-15T00:00:00Z")

    assert state.finalized_at == "2026-07-15T00:00:00Z"
    with pytest.raises(RuntimeError, match="sealed"):
        state.seal(finalized_at="2026-07-16T00:00:00Z")


def test_sealed_state_rejects_blocker_resolution():
    state = RunEvidenceState(run_id="r1")
    blocker = state.record_blocker(category="build", error_code="E1", failure_signature="sig")
    state.seal(finalized_at="2026-07-15T00:00:00Z")

    with pytest.raises(RuntimeError, match="sealed"):
        state.resolve_blocker(blocker.blocker_id)
