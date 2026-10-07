"""M1: the next Actor window keeps observations without replaying actions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_machine import PhaseAttemptRecord, PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.tools.base import ToolResult


def observe(state, *, text, path="/workspace/demo/pom.xml", ref="output_read", action="read"):
    state.ingest_tool_result(
        "project_analysis", "file_io", ToolResult.completed_success(output=text, output_ref=ref),
        params={"action": action, "path": path}, source_phase="analyze")


def fixture_state():
    fixture = json.loads((Path(__file__).parent / "fixtures/phase_handoff/commons-csv-analyze-reads.json").read_text())
    state = RunEvidenceState(run_id="csv-regression")
    for row in fixture["reads"]:
        assert hashlib.sha256(row["text"].encode()).hexdigest() == row["sha256"]
        state.ingest_tool_result(
            "project_analysis", row["tool"],
            ToolResult.completed_success(output=row["text"], output_ref=row["ref"],
                                         facts={"target": row["params"]["target"], "matched": True}),
            params=row["params"], source_phase="analyze")
    return state


def test_actual_csv_configuration_survives_with_hash_and_ranges(tmp_path):
    state = fixture_state()
    handoff = PhaseHandoff(state, storage_path=tmp_path / "handoff.json", retain_context=True)
    before = len(state.tool_observations)
    projected = handoff.project_for("build", char_budget=6000)
    text = projected.to_prompt_text()
    assert "<defaultGoal>clean verify apache-rat:check japicmp:cmp spotbugs:check" in text
    assert "<maven.compiler.source>1.8</maven.compiler.source>" in text
    assert "UTF-8" in text
    assert "output_38b32df7081c" in text
    assert "observed-output-sha256=" in text and "output-lines=" in text
    assert len(text) <= 6000 and len(state.tool_observations) == before
    # Control condition stays byte-compatible and carries no source text.
    legacy = PhaseHandoff(state).project_for("build", char_budget=6000).to_prompt_text()
    assert "<defaultGoal>" not in legacy


def test_claim_is_whole_or_explicitly_omitted_and_full_claim_is_persisted(tmp_path):
    state = RunEvidenceState(run_id="claims")
    claim = "The complete command is mvn --show-version --batch-mode " + "verify " * 500
    record = PhaseAttemptRecord(phase="analyze", attempt_id="analyze-1", termination="completed",
                                outcome="partial", key_results=claim)
    state.record_phase_attempt(record)
    handoff = PhaseHandoff(state, storage_path=tmp_path / "handoff.json", retain_context=True)
    text = handoff.project_for("build", char_budget=1200).to_prompt_text()
    assert claim[:200] not in text
    assert "omitted" in text and "handoff.json" in text
    assert json.loads(handoff.storage_path.read_text())["attempts"][0]["claim_text"] == claim
    machine = PhaseMachine(start_phase="build")
    machine._records.append(record)
    assert claim[:200] not in "\n".join(machine.digest_lines(include_claims=False))


def test_budget_retains_configuration_lines_and_makes_gaps_explicit(tmp_path):
    state = RunEvidenceState(run_id="bounded")
    text = ("<!-- copyright filler -->\n" * 100 + "<defaultGoal>clean verify</defaultGoal>\n"
            + "<maven.compiler.release>17</maven.compiler.release>\n" + "<!-- more -->\n" * 200)
    observe(state, text=text)
    handoff = PhaseHandoff(state, storage_path=tmp_path / "handoff.json", retain_context=True)
    p = handoff.project_for("build", char_budget=1400)
    visible = p.to_prompt_text()
    assert len(visible) <= 1400
    assert "<defaultGoal>clean verify</defaultGoal>" in visible
    assert "<maven.compiler.release>17</maven.compiler.release>" in visible
    assert p.excerpts[0].abbreviated
    assert p.excerpts[0].observed_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert "full observed output" in visible


@pytest.mark.parametrize("invalid_ref", ["file:///tmp/invented", "job:wrong", "claim://success"])
def test_non_output_refs_never_become_source_excerpts(invalid_ref):
    state = RunEvidenceState(run_id="bad-ref")
    observe(state, text="<defaultGoal>test</defaultGoal>", ref=invalid_ref)
    assert not PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000).excerpts


def test_written_source_and_prior_checkout_reads_are_not_reused():
    state = RunEvidenceState(run_id="writes")
    observe(state, text="<defaultGoal>test</defaultGoal>")
    observe(state, text="wrote new POM", action="write")
    assert not PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000).excerpts
    observe(state, text="<defaultGoal>verify</defaultGoal>", ref="output_new")
    p = PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000)
    assert len(p.excerpts) == 1 and "verify" in p.excerpts[0].text
    state.ingest_tool_result("environment", "project", ToolResult.completed_success(output="cloned"),
                             params={"action": "clone"}, source_phase="provision")
    assert not PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000).excerpts


@pytest.mark.parametrize("command, retained", [
    ("rg -n defaultGoal pom.xml", True),
    ("rg -n 'compiler|defaultGoal' pom.xml", True),
    ("sed -n '100,145p' pom.xml", True),
    ("sed -n -i 's/test/verify/' pom.xml", False),
    ("cat pom.xml; mvn test", False),
])
def test_simple_bash_reads_keep_output_with_command_provenance(command, retained):
    state = RunEvidenceState(run_id="bash-reads")
    state.ingest_tool_result("project_analysis", "bash", ToolResult.completed_success(
        output="139:<defaultGoal>verify</defaultGoal>", output_ref="output_bash"),
        params={"command": command, "working_directory": "/workspace/demo"}, source_phase="analyze")
    projection = PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000)
    assert bool(projection.excerpts) == retained
    if retained:
        assert command in projection.excerpts[0].path
        assert "139:<defaultGoal>verify</defaultGoal>" in projection.to_prompt_text()


def test_unverified_claim_and_conflict_cannot_override_failed_observation():
    state = RunEvidenceState(run_id="conflict")
    state.record_conflict("changed_source_fingerprint")
    state.register_claim("artifacts", "build.complete", True, source_phase="analyze")
    state.ingest_tool_result("artifacts", "maven", ToolResult.completed_failure(
        output="BUILD FAILURE", error_code="FAILED"),
        params={"command": "mvn test"}, source_phase="analyze")
    p = PhaseHandoff(state, retain_context=True).project_for("build", char_budget=6000)
    assert p.fact("build.complete").status == "claimed"
    assert p.conflicts == ("changed_source_fingerprint",)
    assert "BUILD FAILURE" in p.to_prompt_text() and "conflicts=1" in p.to_prompt_text()


def test_actor_and_advisor_share_current_facts_without_replaying_steps():
    engine = ReActEngine.__new__(ReActEngine)
    engine.config = SimpleNamespace(phase_context_policy="retained", phase_handoff_char_budget=6000)
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.phase_handoff = PhaseHandoff(fixture_state(), retain_context=True)
    engine._phase_budget_numbers = lambda phase: (100, 0, 20)
    engine._detected_build_system = lambda: "maven"
    engine._ensure_project_facts = lambda: "present"
    engine._recommended_build_line = lambda phase: ""
    engine._native_smoke_guidance = lambda phase: ""
    engine._toolchain_state_line = lambda: "Current env overlay: Maven 3.9.16 active"
    engine._required_task_progress_lines = lambda: ["Required Maven 3.9.16; receipt runtime 3.9.9; unavailable"]
    engine._last_test_attempt_line = lambda: "Terminal receipt supersedes pending; 974 passed, 11 skipped"
    engine._native_state_line = lambda: ""
    engine.steps = []
    actor = engine._phase_intro_step().content
    advisor = engine._advisor_evidence_digest()
    for line in engine._current_phase_evidence_lines():
        assert line in actor and line in advisor
    assert "not a dispatch observation" in actor
    assert "<defaultGoal>" in actor and "<defaultGoal>" in advisor
    assert engine.steps == []
    assert engine._phase_handoff_audit["intro_sha256"] == hashlib.sha256(actor.encode()).hexdigest()


def test_native_phase_switches_retain_reads_without_extra_requests_or_actions():
    from test_native_loop_engine import _engine, _phase_turn
    from sag.agent.verdict_finalizer import RunTerminationStatus

    engine = _engine([_phase_turn(i) for i in range(1, 6)])
    engine.config.phase_context_policy = "retained"
    engine.phase_handoff = PhaseHandoff(engine.run_evidence_state, retain_context=True)
    observe(engine.run_evidence_state, text="<defaultGoal>clean verify</defaultGoal>")
    engine._phase_intro_step = ReActEngine._phase_intro_step.__get__(engine)
    engine._ensure_project_facts = lambda: "present"
    engine._detected_build_system = lambda: "maven"
    engine._recommended_build_line = lambda phase: ""
    engine._native_smoke_guidance = lambda phase: ""
    termination = engine.run_setup_loop("set up the project", max_iterations=12)
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.current_iteration == len(engine.llm_client.requests) == 5
    assert len(engine.phase_machine.records) == 5
    assert sum(o.tool_name == "file_io" for o in engine.run_evidence_state.tool_observations) == 1
    for messages in engine.llm_client.requests[1:]:
        assert any("<defaultGoal>clean verify</defaultGoal>" in str(m.get("content")) for m in messages)


@pytest.mark.parametrize("policy", ["facts", "relevant"])
def test_new_policies_reserve_complete_decisions_before_read_quotes(policy):
    state = fixture_state()
    claim = "Use Maven 3.9.11 and Java 17. Run the pinned root defaultGoal, then inspect its receipt."
    state.record_phase_attempt(PhaseAttemptRecord(
        phase="analyze", attempt_id="analyze-1", termination="completed", outcome="success", key_results=claim))
    handoff = PhaseHandoff(state, retain_context=True, context_policy=policy)
    projection = handoff.project_for("build", char_budget=2400)
    assert claim in projection.to_prompt_text()
    assert len(projection.to_prompt_text()) <= 2400
    if policy == "facts":
        assert not projection.excerpts
    else:
        assert projection.excerpts


def test_relevant_policy_does_not_repeat_configuration_after_successful_build():
    state = fixture_state()
    state.record_phase_attempt(PhaseAttemptRecord(
        phase="build", attempt_id="build-1", termination="completed", outcome="success",
        key_results="Pinned command completed; inspect recorded tests."))
    projection = PhaseHandoff(state, retain_context=True, context_policy="relevant").project_for("test", char_budget=6000)
    assert not projection.excerpts
    assert projection.omitted_excerpt_count > 0
    assert "Pinned command completed" in projection.to_prompt_text()


@pytest.mark.parametrize("policy", ["facts", "relevant"])
def test_new_intro_budget_covers_contract_and_handoff_together(policy):
    engine = ReActEngine.__new__(ReActEngine)
    engine.config = SimpleNamespace(phase_context_policy=policy, phase_handoff_char_budget=6000)
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.phase_handoff = PhaseHandoff(fixture_state(), retain_context=True, context_policy=policy)
    engine._phase_budget_numbers = lambda phase: (150, 10, 100)
    engine._detected_build_system = lambda: "maven"
    engine._ensure_project_facts = lambda: "present"
    engine._recommended_build_line = lambda phase: "Build coordinates are task.json."
    engine._native_smoke_guidance = lambda phase: ""
    engine._current_phase_evidence_lines = lambda: ["Required Java 17; actual dispatch Java 17."]
    intro = engine._phase_intro_step().content
    assert len(intro) <= 6000
    assert "Required Java 17; actual dispatch Java 17." in intro
    assert engine._phase_handoff_audit["budget_scope"] == "whole_intro"


def test_oversized_contract_keeps_full_source_and_never_cuts_command(tmp_path):
    from sag.agent.output_storage import OutputStorageManager
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.output_storage = OutputStorageManager(tmp_path)
    command = "Task ci-step-1: mvn clean verify " + "-Dproperty=value " * 200
    contract = "=== PHASE: BUILD ===\nObjective: execute pinned task\n" + command + "\n" + "detail\n" * 500
    text, ref = engine._bounded_phase_contract(contract, 1000)
    assert len(text) <= 1000
    assert command[:50] not in text
    assert "=== PHASE: BUILD ===" in text
    assert "omitted lines" in text and ref in text
    assert engine.output_storage.retrieve_output(ref) == contract
