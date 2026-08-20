# tests/test_react_engine_phase_wiring.py
"""Phase-machine wiring seams: signal handling, window reset, budget forcing.

These test the helper methods the loop calls, with a minimal fake engine
state — not the full LLM loop."""

from types import SimpleNamespace

import pytest
from test_container_io import FakeContainer

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState, claim_identity
from sag.agent.phase_machine import (
    PhaseAttemptRecord,
    PhaseClaim,
    PhaseMachine,
    PhaseOutcome,
    PhaseTermination,
)
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.project_execution_plan import (
    PROJECT_EXECUTION_PLAN_PATH,
    canonical_authored_plan_sha256,
    read_sealed_project_execution_plan,
    seal_project_execution_plan,
    validate_authored_plan,
)
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import StepType
from sag.agent.verdict_finalizer import EvidenceCloseReason
from sag.evidence import OperationOutcome


def _engine_with_machine(*, start_phase="provision"):
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase=start_phase)
    engine.run_evidence_state = RunEvidenceState(run_id="phase-wiring")
    engine.transition_policy = PhaseTransitionPolicy()
    engine._repair_global_remaining = 2
    engine._repair_phase_remaining = {"test": 1, "build": 1}
    engine.steps = [SimpleNamespace(step_type=None, content="old")] * 7
    engine.context_journal = None
    engine._phase_iterations = 12
    engine.config = SimpleNamespace(
        phase_min_floors={"analyze": 4, "build": 10, "test": 12, "report": 8},
        max_iterations=150,
    )
    engine.current_iteration = 10
    engine.context_manager = SimpleNamespace(
        update_task_status=lambda *a, **k: True,
        current_task_id=None,
    )
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine.finalized_reasons = []
    engine._finalize_evidence = lambda reason: engine.finalized_reasons.append(reason)
    engine.plan_evidence_validations = []

    def validate_plan_evidence(plan, *, source_attempt_id):
        engine.plan_evidence_validations.append(source_attempt_id)
        return validate_authored_plan(plan)

    engine.tools = {
        "phase": SimpleNamespace(validate_execution_plan_evidence=validate_plan_evidence)
    }
    return engine


def test_analysis_facts_controller_recovery_spends_one_run_local_survey_attempt():
    engine = _engine_with_machine(start_phase="analyze")
    engine._analysis_facts_recovery_attempted = False
    calls = []
    engine._ensure_project_facts = lambda: calls.append("survey") or "created"

    assert engine._recover_analysis_facts_once() == "created"
    assert engine._recover_analysis_facts_once() is None
    assert calls == ["survey"]


def _terminal_step(
    engine,
    *,
    outcome="success",
    signal="done",
    key_results="ok",
    reason="",
    validated_facts=None,
):
    phase = engine.phase_machine.current_phase
    claimed = PhaseOutcome(outcome)
    state = {
        PhaseOutcome.SUCCESS: ValidatorState.GREEN,
        PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
        PhaseOutcome.FAILED: ValidatorState.RED,
        PhaseOutcome.UNKNOWN: ValidatorState.UNAVAILABLE,
    }[claimed]
    facts = dict(validated_facts or {})
    if validated_facts is None:
        if phase == "provision":
            facts["provision.workspace_ready"] = claimed is PhaseOutcome.SUCCESS
        elif phase == "analyze":
            facts["analysis.build_entry_ready"] = claimed in {
                PhaseOutcome.SUCCESS,
                PhaseOutcome.PARTIAL,
            }
        elif phase == "build":
            facts["build.test_entry_ready"] = claimed in {
                PhaseOutcome.SUCCESS,
                PhaseOutcome.PARTIAL,
            }
    claim = PhaseClaim(
        phase=phase,
        signal=signal,
        claimed_outcome=claimed,
        key_results=key_results,
        reason=reason,
    )
    gate = GateResult(
        accepted=True,
        validated_outcome=claimed,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=state,
        reason=reason or "scripted gate",
        validated_facts=facts,
        claim=claim,
    )
    return SimpleNamespace(
        step_type=SimpleNamespace(value="action"),
        tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={
                "phase_signal": signal,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        ),
    )


def _execution_plan_payload():
    return {
        "summary": "Build and test through the project-specific wrapper lanes.",
        "documents_reviewed": [
            {
                "path": "/workspace/demo/DEVNOTES.txt",
                "reason": "Defines the supported build and test entry points.",
                "evidence_refs": ["output_devnotes"],
            }
        ],
        "build_steps": [
            {
                "tool": "build",
                "params": {
                    "action": "compile",
                    "working_directory": "/workspace/demo",
                    "args": "-DskipTests",
                },
                "purpose": "Compile before entering the separate test lane.",
                "evidence_refs": ["output_devnotes"],
            }
        ],
        "build_success_criteria": ["A terminal receipt covers the reactor."],
        "test_steps": [
            {
                "tool": "build",
                "params": {
                    "action": "test",
                    "working_directory": "/workspace/demo",
                    "args": "-Dtest=FocusedSuite",
                },
                "purpose": "Run the documented bounded test lane.",
                "evidence_refs": ["output_devnotes"],
            }
        ],
        "test_success_criteria": ["The focused lane has a terminal test receipt."],
        "environment_constraints": ["Use the repository wrapper."],
        "risks": [],
        "unresolved_questions": [],
    }


def _analyze_plan_step(plan):
    plan_sha256 = canonical_authored_plan_sha256(plan)
    claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome=PhaseOutcome.SUCCESS,
        key_results="reviewed the project execution contract",
        evidence_refs=("output_devnotes",),
        execution_plan_sha256=plan_sha256,
        execution_plan_ref=PROJECT_EXECUTION_PLAN_PATH,
    )
    gate = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        reason="analysis evidence is ready",
        validated_facts={
            # The ordinary Analyze observation runs before the plan artifact
            # exists, so its read-only readiness projection is still false.
            "analysis.build_entry_ready": False,
            "analysis.execution_plan_candidate_valid": True,
            "analysis.execution_plan_sha256": plan_sha256,
        },
        claim=claim,
    )
    return SimpleNamespace(
        step_type=SimpleNamespace(value="action"),
        tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={
                "phase_signal": "done",
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
                "execution_plan_candidate": {"authored_plan": plan},
            },
        ),
    )


def test_phase_done_signal_advances_and_resets_window():
    engine = _engine_with_machine()
    step = _terminal_step(engine, key_results="cloned + JDK")

    engine._handle_phase_signals([step])

    assert engine.phase_machine.current_phase == "analyze"
    assert len(engine.steps) == 1, "window must reset to the phase intro"
    intro = engine.steps[0].content
    assert "analyze" in intro.lower()
    assert "cloned + JDK" in intro, "prior key results carried into the digest"
    assert engine._phase_iterations == 0


def test_analyze_plan_is_persisted_before_gate_facts_are_recorded_and_build_opens(
    monkeypatch,
):
    engine = _engine_with_machine(start_phase="analyze")
    container = FakeContainer()
    engine.orchestrator = container
    monkeypatch.setattr(
        "sag.agent.document_map.read_live_document_map",
        lambda _source: SimpleNamespace(
            complete=True,
            conflict=None,
            detail="",
            payload=None,
        ),
    )
    recorded = []
    original_record = engine._record_gate_facts

    def record_after_publish(phase, gate):
        assert PROJECT_EXECUTION_PLAN_PATH in container.files
        recorded.append((phase, gate.validated_facts["analysis.execution_plan_sealed"]))
        return original_record(phase, gate)

    engine._record_gate_facts = record_after_publish

    signal = engine._handle_phase_signals([_analyze_plan_step(_execution_plan_payload())])

    artifact = read_sealed_project_execution_plan(container)
    assert signal == "done"
    assert artifact is not None
    assert artifact.authored_plan_sha256 == canonical_authored_plan_sha256(
        _execution_plan_payload()
    )
    assert recorded == [("analyze", True)]
    assert engine.plan_evidence_validations == ["analyze-1"]
    assert engine.run_evidence_state.fact_value("analysis.execution_plan_sealed") is True
    assert (
        engine.run_evidence_state.fact_value("analysis.execution_plan_source_attempt_id")
        == "analyze-1"
    )
    assert engine.phase_machine.current_phase == "build"


def test_engine_rechecks_document_read_evidence_immediately_before_sealing(monkeypatch):
    engine = _engine_with_machine(start_phase="analyze")
    container = FakeContainer()
    engine.orchestrator = container
    calls = []

    def reject_stale_evidence(_plan, *, source_attempt_id):
        calls.append(source_attempt_id)
        raise ValueError("document output became unreadable")

    engine.tools["phase"].validate_execution_plan_evidence = reject_stale_evidence
    monkeypatch.setattr(
        "sag.agent.document_map.read_live_document_map",
        lambda _source: SimpleNamespace(
            complete=True,
            conflict=None,
            detail="",
            payload=None,
        ),
    )

    signal = engine._handle_phase_signals([_analyze_plan_step(_execution_plan_payload())])

    assert signal is None
    assert calls == ["analyze-1"]
    assert PROJECT_EXECUTION_PLAN_PATH not in container.files
    assert engine.phase_machine.current_phase == "analyze"


def test_analyze_plan_publish_failure_supersedes_acceptance_and_keeps_analyze_open(
    monkeypatch,
):
    engine = _engine_with_machine(start_phase="analyze")
    container = FakeContainer(fail_on="mv -f --")
    engine.orchestrator = container
    monkeypatch.setattr(
        "sag.agent.document_map.read_live_document_map",
        lambda _source: SimpleNamespace(
            complete=True,
            conflict=None,
            detail="",
            payload=None,
        ),
    )

    signal = engine._handle_phase_signals([_analyze_plan_step(_execution_plan_payload())])

    assert signal is None
    assert PROJECT_EXECUTION_PLAN_PATH not in container.files
    assert engine.phase_machine.current_phase == "analyze"
    assert engine.phase_machine.records == ()


def test_unavailable_inventory_records_a_gap_but_does_not_become_a_document_allowlist(
    monkeypatch,
):
    engine = _engine_with_machine(start_phase="analyze")
    container = FakeContainer()
    engine.orchestrator = container
    monkeypatch.setattr(
        "sag.agent.document_map.read_live_document_map",
        lambda _source: SimpleNamespace(
            complete=False,
            conflict="publication_missing",
            detail="document map was not published",
            payload=None,
        ),
    )

    signal = engine._handle_phase_signals([_analyze_plan_step(_execution_plan_payload())])

    artifact = read_sealed_project_execution_plan(container)
    assert signal == "done"
    assert artifact is not None
    assert any(
        warning.startswith("document_map_unavailable:") for warning in artifact.inventory_warnings
    )
    assert engine.phase_machine.current_phase == "build"


def test_analyze_auto_close_does_not_reuse_plan_facts_from_an_older_attempt():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(
        is_complete=False,
        current_phase="analyze",
        current_attempt_id="analyze-2",
    )
    engine.run_evidence_state = RunEvidenceState(run_id="analyze-reentry")
    for key, value in (
        ("analysis.execution_plan_sealed", True),
        ("analysis.execution_plan_sha256", "a" * 64),
        ("analysis.execution_plan_source_attempt_id", "analyze-1"),
    ):
        engine.run_evidence_state.set_fact(
            key,
            value,
            evidence_ref="artifact://analyze-1-plan",
            source_phase="analyze",
            source_attempt_id="analyze-1",
        )
    guidance = []
    engine._add_system_guidance = lambda text, priority=0: guidance.append((text, priority))

    assert engine._keep_analyze_open_for_execution_plan("phase_floor") is True
    assert guidance and "ANALYSIS_EXECUTION_PLAN_REQUIRED" in guidance[0][0]


def test_build_prompt_rejects_plan_bound_to_an_older_analyze_record(monkeypatch):
    plan = _execution_plan_payload()
    plan_sha256 = canonical_authored_plan_sha256(plan)

    def accepted_record(attempt_id: str, key_results: str) -> PhaseAttemptRecord:
        claim = PhaseClaim(
            phase="analyze",
            signal="done",
            claimed_outcome=PhaseOutcome.SUCCESS,
            key_results=key_results,
            execution_plan_sha256=plan_sha256,
            execution_plan_ref=PROJECT_EXECUTION_PLAN_PATH,
        )
        return PhaseAttemptRecord(
            phase="analyze",
            attempt_id=attempt_id,
            termination=PhaseTermination.COMPLETED,
            outcome=PhaseOutcome.SUCCESS,
            transition="advance",
            claim=claim,
        )

    analyze_one = accepted_record("analyze-1", "first strategy")
    artifact = seal_project_execution_plan(
        plan,
        source_attempt_id="analyze-1",
        claim_sha256=claim_identity(analyze_one.claim),
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(current_phase="build", records=(analyze_one,))
    engine._sealed_execution_plan_cache = artifact
    engine.orchestrator = object()
    assert "MODEL-AUTHORED IN ANALYZE" in engine._system_prompt_for_current_phase("BASE")

    analyze_two = accepted_record("analyze-2", "revised strategy")
    engine.phase_machine = SimpleNamespace(
        current_phase="build",
        records=(analyze_one, analyze_two),
    )
    monkeypatch.setattr(
        "sag.agent.project_execution_plan.read_sealed_project_execution_plan",
        lambda _orchestrator: artifact,
    )

    with pytest.raises(RuntimeError, match="sealed Analyze execution plan"):
        engine._system_prompt_for_current_phase("BASE")
    assert engine._sealed_execution_plan_cache is None


def test_report_does_not_inject_analyze_one_plan_after_analyze_two_blocks(monkeypatch):
    plan = _execution_plan_payload()
    plan_sha256 = canonical_authored_plan_sha256(plan)
    first_claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome=PhaseOutcome.SUCCESS,
        execution_plan_sha256=plan_sha256,
        execution_plan_ref=PROJECT_EXECUTION_PLAN_PATH,
    )
    first_record = PhaseAttemptRecord(
        phase="analyze",
        attempt_id="analyze-1",
        termination=PhaseTermination.COMPLETED,
        outcome=PhaseOutcome.SUCCESS,
        transition="advance",
        claim=first_claim,
    )
    stale_artifact = seal_project_execution_plan(
        plan,
        source_attempt_id="analyze-1",
        claim_sha256=claim_identity(first_claim),
    )
    blocked_claim = PhaseClaim(
        phase="analyze",
        signal="blocked",
        claimed_outcome=PhaseOutcome.PARTIAL,
        reason="documentation remains unresolved",
    )
    blocked_record = PhaseAttemptRecord(
        phase="analyze",
        attempt_id="analyze-2",
        termination=PhaseTermination.BLOCKED,
        outcome=PhaseOutcome.PARTIAL,
        transition="evidence_close",
        claim=blocked_claim,
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = SimpleNamespace(
        current_phase="report",
        records=(first_record, blocked_record),
    )
    engine._sealed_execution_plan_cache = stale_artifact
    engine.orchestrator = object()
    monkeypatch.setattr(
        "sag.agent.project_execution_plan.read_sealed_project_execution_plan",
        lambda _orchestrator: stale_artifact,
    )

    assert engine._system_prompt_for_current_phase("BASE") == "BASE"
    assert engine._sealed_execution_plan_cache is None


def test_plan_reader_rejects_each_record_binding_mismatch(monkeypatch):
    plan = _execution_plan_payload()
    plan_sha256 = canonical_authored_plan_sha256(plan)
    claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome=PhaseOutcome.SUCCESS,
        key_results="current strategy",
        execution_plan_sha256=plan_sha256,
        execution_plan_ref=PROJECT_EXECUTION_PLAN_PATH,
    )
    record = PhaseAttemptRecord(
        phase="analyze",
        attempt_id="analyze-2",
        termination=PhaseTermination.COMPLETED,
        outcome=PhaseOutcome.SUCCESS,
        transition="advance",
        claim=claim,
    )
    revised_plan = {**plan, "summary": "A different authored execution strategy."}
    mismatches = {
        "source_attempt": seal_project_execution_plan(
            plan,
            source_attempt_id="analyze-1",
            claim_sha256=claim_identity(claim),
        ),
        "claim_sha": seal_project_execution_plan(
            plan,
            source_attempt_id="analyze-2",
            claim_sha256="f" * 64,
        ),
        "authored_plan": seal_project_execution_plan(
            revised_plan,
            source_attempt_id="analyze-2",
            claim_sha256=claim_identity(claim),
        ),
    }
    holder = {"artifact": None}
    monkeypatch.setattr(
        "sag.agent.project_execution_plan.read_sealed_project_execution_plan",
        lambda _orchestrator: holder["artifact"],
    )

    for name, artifact in mismatches.items():
        engine = ReActEngine.__new__(ReActEngine)
        engine.phase_machine = SimpleNamespace(current_phase="build", records=(record,))
        engine._sealed_execution_plan_cache = artifact
        engine.orchestrator = object()
        holder["artifact"] = artifact

        assert engine._read_sealed_execution_plan() is None, name
        assert engine._sealed_execution_plan_cache is None, name


def test_phase_blocked_signal_routes_from_prerequisites_not_linear_order():
    engine = _engine_with_machine()
    step = _terminal_step(
        engine,
        signal="blocked",
        outcome="failed",
        reason="no network",
        validated_facts={"provision.workspace_ready": False},
    )

    engine._handle_phase_signals([step])

    assert engine.phase_machine.records[0].termination.value == "blocked"
    assert engine.phase_machine.current_phase == "report"
    assert [record.phase for record in engine.phase_machine.records[1:]] == [
        "analyze",
        "build",
        "test",
    ]
    assert all(record.outcome.value == "skipped" for record in engine.phase_machine.records[1:])


def test_failed_build_signal_records_test_skip_before_evidence_close():
    engine = _engine_with_machine(start_phase="build")
    step = _terminal_step(
        engine,
        outcome="failed",
        key_results="compiler failed",
        validated_facts={"build.test_entry_ready": False},
    )

    engine._handle_phase_signals([step])

    assert [(record.phase, record.outcome.value) for record in engine.phase_machine.records] == [
        ("build", "failed"),
        ("test", "skipped"),
    ]
    assert engine.run_evidence_state.phase_records == engine.phase_machine.records
    assert engine.finalized_reasons == [EvidenceCloseReason.DEPENDENTS_SKIPPED]
    assert engine.phase_machine.current_phase == "report"


def test_partial_build_with_validated_test_entry_advances_to_test():
    engine = _engine_with_machine(start_phase="build")
    step = _terminal_step(
        engine,
        outcome="partial",
        validated_facts={"build.test_entry_ready": True},
    )

    engine._handle_phase_signals([step])

    assert engine.phase_machine.current_phase == "test"
    assert engine.phase_machine.current_attempt_id == "test-1"
    assert engine.finalized_reasons == []


def test_retired_repair_signal_cannot_reopen_a_dependency():
    """WS3 requires the model to emit a new ordinary ActionIntent.  A stale
    phase-repair envelope cannot mutate the phase machine behind that path."""
    engine = _engine_with_machine(start_phase="test")
    step = SimpleNamespace(
        tool_result=SimpleNamespace(
            metadata={
                "phase_signal": "repair",
                "repair_request": {"target_phase": "build"},
            }
        )
    )

    signal = engine._handle_phase_signals([step])

    assert signal is None
    assert engine.phase_machine.current_phase == "test"
    assert engine.phase_machine.current_attempt_id == "test-1"
    assert engine.phase_machine.records == ()


def test_phase_note_signal_persists_without_advancing():
    from sag.agent.context_manager import Task, TrunkContext

    class _CM:
        current_task_id = "phase_provision"

        def __init__(self, trunk):
            self.trunk = trunk
            self.saved = False

        def load_trunk_context(self):
            return self.trunk

        def _save_trunk_context(self, trunk):
            self.saved = True

    trunk = TrunkContext(context_id="t", goal="g", project_url="u", project_name="p")
    trunk.todo_list.append(Task(id="phase_provision", description="Provision"))
    engine = _engine_with_machine()
    engine.context_manager = _CM(trunk)
    engine.steps_since_context_switch = 9
    step = SimpleNamespace(
        step_type=SimpleNamespace(value="action"),
        tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={"phase_signal": "note", "text": "Maven 3.8 is incompatible; use 3.9"},
        ),
    )

    signal = engine._handle_phase_signals([step])

    assert signal == "note"
    assert engine.phase_machine.current_phase == "provision"
    assert len(engine.steps) == 7, "note should not reset the phase window"
    assert engine.steps_since_context_switch == 9
    assert trunk.todo_list[0].notes == "Maven 3.8 is incompatible; use 3.9"
    assert engine.context_manager.saved is True


def test_persist_phase_record_preserves_existing_phase_notes():
    from sag.agent.context_manager import Task, TrunkContext

    class _CM:
        current_task_id = "phase_provision"

        def __init__(self, trunk):
            self.trunk = trunk

        def load_trunk_context(self):
            return self.trunk

        def _save_trunk_context(self, trunk):
            pass

    trunk = TrunkContext(context_id="t", goal="g", project_url="u", project_name="p")
    trunk.todo_list.append(
        Task(id="phase_provision", description="Provision", notes="Downloaded Maven 3.9.9")
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_manager = _CM(trunk)
    engine.prompt_builder = None

    engine._persist_phase_record("provision", "completed", "Repository cloned and analyzed")

    task = trunk.todo_list[0]
    assert task.notes == "Downloaded Maven 3.9.9"
    assert task.key_results == "Repository cloned and analyzed"
    assert task.status.value == "completed"


def test_floor_starvation_closes_unknown_and_skips_unready_dependents():
    engine = _engine_with_machine()
    # 150-iteration run, still in provision, but only 29 iterations remain:
    # analyze+build+test+report floors (4+10+12+8=34) would starve.
    engine.current_iteration = 121

    forced = engine._enforce_phase_floors()

    assert forced is True
    assert engine.phase_machine.records[0].termination.value == "completed"
    assert engine.phase_machine.records[0].outcome.value == "unknown"
    assert "reserved" in engine.phase_machine.records[0].key_results.lower()
    assert engine.phase_machine.current_phase == "report"


def test_hard_phase_may_consume_savings():
    engine = _engine_with_machine()
    # Deep into the run but only test+report remain after build: floors 12+8=20.
    engine.phase_machine.mark_done("ok", [])
    engine.phase_machine.mark_done("ok", [])
    assert engine.phase_machine.current_phase == "build"
    engine.current_iteration = 100  # 50 remain > 20 reserved -> build keeps going

    assert engine._enforce_phase_floors() is False


def test_no_machine_means_no_phase_behavior():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = None
    assert engine._handle_phase_signals([]) is None
    assert engine._enforce_phase_floors() is False


def test_phase_transition_resets_journal_ledger_memory():
    """The window reset restarts the ledger; the journal's text-dedupe memory
    must reset alongside _journal_intro_dirty or the next phase's first ledger
    could be wrongly suppressed (round-6 review)."""
    engine = _engine_with_machine()
    engine._journal_last_ledger = "ATTEMPT LEDGER (older work, compacted):\n✗ x"
    step = _terminal_step(engine)

    engine._handle_phase_signals([step])

    assert engine._journal_last_ledger is None
    assert engine._journal_intro_dirty is True


def test_phase_transition_resets_context_switch_counter():
    """No manage_context actions exist in phase mode, so the legacy reset
    never fires; phase transitions are the context switches now."""
    engine = _engine_with_machine()
    engine.steps_since_context_switch = 23
    step = _terminal_step(engine)

    engine._handle_phase_signals([step])

    assert engine.steps_since_context_switch == 0

    forced = _engine_with_machine()
    forced.steps_since_context_switch = 23
    forced.current_iteration = 121  # floors force-block (see test above)

    assert forced._enforce_phase_floors() is True
    assert forced.steps_since_context_switch == 0


def test_persist_phase_record_warns_when_trunk_task_missing():
    """A missing phase_<name> trunk task must at least warn — silent False
    returns hid the analyzer-rewrite defect for an entire run."""
    from loguru import logger as loguru_logger

    from sag.agent.context_manager import TrunkContext

    class _CM:
        current_task_id = None

        def __init__(self, trunk):
            self.trunk = trunk

        def load_trunk_context(self):
            return self.trunk

        def _save_trunk_context(self, trunk):
            pass

    trunk = TrunkContext(context_id="t", goal="g", project_url="u", project_name="p")
    trunk.add_task("some unrelated task")  # no phase_build entry
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_manager = _CM(trunk)

    messages = []
    handler_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        engine._persist_phase_record("build", "completed", "compiled fine")
    finally:
        loguru_logger.remove(handler_id)

    assert any(
        "phase_build" in m for m in messages
    ), "missing trunk phase task must produce a warning, not a silent no-op"


# --- round-5 gate fixes ------------------------------------------------------


def test_floor_exhaustion_auto_completes_when_gate_passes():
    """vfs round 5: the build phase was force-BLOCKED at floor exhaustion while
    the physical evidence was green (BUILD SUCCESS + 177/184 tests on disk),
    failing a healthy run. Floor exhaustion must consult the evidence gate:
    gate passes -> auto-done, only gate-fail -> blocked."""
    engine = _engine_with_machine()
    engine.current_iteration = 121  # forces floor starvation in provision
    engine._phase_gate_check = lambda phase: {
        "ok": True,
        "reason": "workspace exists",
        "suggestions": [],
        "validator_state": "green",
        "validated_facts": {"provision.workspace_ready": True},
        "evidence_refs": ["workspace:///demo"],
    }

    forced = engine._enforce_phase_floors()

    assert forced is True
    rec = engine.phase_machine.records[0]
    assert rec.termination.value == "completed"
    assert "floor exhaustion" in rec.key_results.lower()
    assert engine.phase_machine.current_phase == "analyze"


def test_floor_exhaustion_records_failed_outcome_when_evidence_is_red():
    engine = _engine_with_machine()
    engine.current_iteration = 121
    engine._phase_gate_check = lambda phase: {
        "ok": False,
        "reason": "no artifacts",
        "suggestions": [],
        "validator_state": "red",
        "validated_facts": {"provision.workspace_ready": False},
        "evidence_refs": ["workspace:///missing"],
    }

    forced = engine._enforce_phase_floors()

    assert forced is True
    assert engine.phase_machine.records[0].termination.value == "completed"
    assert engine.phase_machine.records[0].outcome.value == "failed"
    assert engine.phase_machine.current_phase == "report"


def test_mid_phase_nudge_when_evidence_green():
    """vfs round 5: the model held green evidence for ~100 iterations without
    claiming done. Every NUDGE_EVERY phase-iterations the engine checks the
    gate and, when green, injects guidance suggesting a done-claim."""
    engine = _engine_with_machine()
    engine._phase_iterations = 15
    engine._phase_gate_check = lambda phase: {
        "ok": True,
        "reason": "workspace ready",
        "suggestions": [],
    }

    nudged = engine._maybe_nudge_phase_done()

    assert nudged is True
    assert any("EVIDENCE CHECK" in getattr(s, "content", "") for s in engine.steps)


def test_no_nudge_when_evidence_not_green():
    engine = _engine_with_machine()
    engine._phase_iterations = 15
    engine._phase_gate_check = lambda phase: {"ok": False, "reason": "x", "suggestions": []}

    assert engine._maybe_nudge_phase_done() is False


def test_no_nudge_off_cycle():
    engine = _engine_with_machine()
    engine._phase_iterations = 7
    engine._phase_gate_check = lambda phase: {"ok": True, "reason": "", "suggestions": []}

    assert engine._maybe_nudge_phase_done() is False


def test_build_objective_names_closure_evidence_not_a_selected_call():
    """A weak model chooses the next ordinary action from facts and schema;
    the phase objective defines only the evidence needed to close."""
    from sag.agent.react_engine import PHASE_OBJECTIVES

    build_obj = PHASE_OBJECTIVES["build"]
    assert "terminal build evidence" in build_obj
    assert "required surveyed build coordinate" in build_obj
    assert "current receipt and artifact/coverage evidence" in build_obj
    assert "build(action=" not in build_obj
    assert " then " not in build_obj.lower()


def test_summary_counts_survive_window_resets():
    engine = _engine_with_machine()
    engine.current_iteration = 3
    engine.token_tracker = None
    # Simulate: 7 steps in window, then a phase transition archives them.
    engine._archive_window_steps()
    assert engine._archived_counts["total_steps"] == 7


def _action(tool_name, success=True, model="gpt-action"):
    return SimpleNamespace(
        step_type=StepType.ACTION,
        tool_name=tool_name,
        tool_result=SimpleNamespace(
            succeeded=success,
            operation_outcome=(OperationOutcome.SUCCESS if success else OperationOutcome.FAILED),
        ),
        model_used=model,
        content="",
    )


def test_archive_accumulates_per_tool_usage_across_windows():
    """Per-tool usage must survive window resets so the end-of-run report shows
    every tool, not just the last phase's. Regression: archived counts tracked
    aggregate actions but dropped the tool breakdown -> 'Tool Usage: Report (1)'
    for a 19-action run."""
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_journal = None

    # Window 1 (e.g. build phase): bash x2 (one failed), build x1
    engine.steps = [_action("bash"), _action("bash", success=False), _action("build")]
    engine._archive_window_steps()
    # Window 2 (test phase): bash x1, test_runner x1
    engine.steps = [_action("bash"), _action("test_runner")]
    engine._archive_window_steps()

    counts = engine._archived_counts
    assert counts["tools_used"] == {"bash": 3, "build": 1, "test_runner": 1}
    assert counts["tool_failures"] == {"bash": 1}


def test_execution_summary_merges_archived_and_live_tool_usage():
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_journal = None
    engine.current_iteration = 26
    # Two archived windows, then the live (report) window holds just `report`.
    engine.steps = [_action("bash"), _action("build", success=False)]
    engine._archive_window_steps()
    engine.steps = [_action("search"), _action("search")]
    engine._archive_window_steps()
    engine.steps = [_action("report")]

    summary = engine.get_execution_summary()

    assert summary["actions"] == 5
    assert summary["tools_used"] == {"bash": 1, "build": 1, "search": 2, "report": 1}
    assert summary["tool_failures"] == {"build": 1}


# Plan 2 Task 8: old protocol removed
