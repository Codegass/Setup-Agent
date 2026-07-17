import json

import pytest

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_machine import PhaseAttemptRecord, PhaseOutcome, PhaseTermination
from test_verdict_finalizer import FakeVerdictOrchestrator


@pytest.fixture
def run_state():
    return RunEvidenceState(run_id="handoff-run")


@pytest.fixture
def handoff(tmp_path, run_state):
    return PhaseHandoff(
        run_state,
        storage_path=tmp_path / "phase-handoff.json",
    )


def _open_attempt(attempt_id: str, phase: str) -> PhaseAttemptRecord:
    return PhaseAttemptRecord(
        phase=phase,
        attempt_id=attempt_id,
        termination=PhaseTermination.RUNNING,
        outcome=PhaseOutcome.UNKNOWN,
    )


def test_analyze_requirement_survives_build_and_test_handoffs(handoff, run_state):
    run_state.register_fact(
        scope="project_analysis",
        key="java.required_version",
        value="17",
        source_ref="file://pom.xml#compiler-release",
        source_phase="analyze",
    )
    run_state.record_phase_attempt(_open_attempt("build-1", "build"))
    run_state.record_phase_attempt(_open_attempt("test-1", "test"))

    projection = handoff.project_for("test", char_budget=4000)

    fact = projection.fact("java.required_version")
    assert fact is not None
    assert fact.value == "17"
    assert fact.status == "verified"
    assert fact.evidence_ref.endswith("#compiler-release")
    assert fact.source_phase == "analyze"


def test_trimmed_blockers_are_explicitly_referenced(handoff, run_state):
    for index in range(100):
        run_state.record_blocker(
            f"blocker-{index}",
            evidence_ref=f"log://{index}",
        )

    projection = handoff.project_for("build", char_budget=1200)

    assert len(run_state.blockers) == 100
    assert projection.omitted_blocker_count > 0
    assert projection.full_state_ref.endswith("phase-handoff.json")
    prompt = projection.to_prompt_text()
    assert len(prompt) <= 1200
    assert "omitted" in prompt.lower()
    assert projection.full_state_ref in prompt
    assert "[BEGIN UNTRUSTED TOOL/PROJECT EVIDENCE]" in prompt
    assert "[END UNTRUSTED TOOL/PROJECT EVIDENCE]" in prompt


def test_unverified_success_claim_never_becomes_a_verified_fact(handoff, run_state):
    run_state.register_claim(
        scope="artifacts",
        key="native.build_complete",
        value=True,
        source_phase="build",
        evidence_refs=(),
    )

    projection = handoff.project_for("test", char_budget=4000)

    fact = projection.fact("native.build_complete")
    blocker = projection.blocker("unverified_claim:native.build_complete")
    assert fact is not None and fact.status == "claimed"
    assert blocker is not None and blocker.status == "active"


def test_no_active_blockers_renders_no_blocker_section(handoff, run_state):
    blocker = run_state.record_blocker("resolved-one", evidence_ref="log://1")
    run_state.resolve_blocker(blocker.blocker_id, evidence_ref="log://2")

    prompt = handoff.project_for("build", char_budget=1200).to_prompt_text()

    assert "ACTIVE BLOCKERS" not in prompt


def test_state_changes_atomically_refresh_the_full_handoff(handoff, run_state):
    run_state.register_fact(
        scope="environment",
        key="java.installed_version",
        value="17",
        source_ref="tool://java/version",
        source_phase="provision",
    )

    payload = json.loads(handoff.storage_path.read_text(encoding="utf-8"))

    assert payload["run_id"] == "handoff-run"
    assert payload["facts"][0]["key"] == "java.installed_version"
    assert not handoff.storage_path.with_suffix(".json.tmp").exists()


def test_complete_materialization_keeps_duplicate_canonical_fact_events(handoff, run_state):
    for source_ref in ("tool://java/first", "tool://java/second"):
        run_state.register_fact(
            scope="environment",
            key="java.installed_version",
            value="17",
            source_ref=source_ref,
            source_phase="provision",
        )

    payload = json.loads(handoff.storage_path.read_text(encoding="utf-8"))

    assert [fact["source_ref"] for fact in payload["facts"]] == [
        "tool://java/second",
        "tool://java/first",
    ]


def test_container_handoff_uses_atomic_workspace_replacement(run_state):
    orchestrator = FakeVerdictOrchestrator()
    PhaseHandoff(run_state, orchestrator=orchestrator)

    run_state.register_fact(
        scope="project_analysis",
        key="project.has_native_build",
        value=True,
        source_ref="file://CMakeLists.txt",
        source_phase="analyze",
    )

    path = "/workspace/.setup_agent/phase-handoff.json"
    payload = json.loads(orchestrator.files[path])
    assert payload["facts"][0]["key"] == "project.has_native_build"
    assert any(
        command.startswith(f"mv {path}.tmp {path}")
        for command in orchestrator.commands
    )
