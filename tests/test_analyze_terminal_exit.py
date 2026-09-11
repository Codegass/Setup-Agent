"""No-plan Analyze termination through the production gate and engine routing.

The survey response and container transport are fixtures. Claim grading,
plan sealing/reading, phase signal handling, transitions, and verdict folding
use production code; no model loop or external project process is exercised.
"""

from types import SimpleNamespace

import pytest
from test_container_io import FakeContainer
from test_phase_tool import _authored_execution_plan
from test_react_engine_phase_wiring import _engine_with_machine

from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.phase_machine import PhaseOutcome
from sag.agent.project_execution_plan import (
    PROJECT_EXECUTION_PLAN_PATH,
    seal_and_write_project_execution_plan,
)
from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
from sag.tools.phase_tool import PhaseTool


class AnalysisContainer(FakeContainer):
    def execute_command(self, command, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in command:
            self.commands.append(command)
            return {"exit_code": 0, "output": frame_named_json_record_stream([])}
        return super().execute_command(command, **kwargs)


def _analyze(*, old_plan=False, survey=None):
    engine = _engine_with_machine(start_phase="analyze")
    container = AnalysisContainer()
    if old_plan:
        artifact = seal_and_write_project_execution_plan(
            _authored_execution_plan(),
            container,
            source_attempt_id="analyze-0",
            claim_sha256="a" * 64,
        )
        for key, value in (
            ("analysis.build_entry_ready", True),
            ("analysis.execution_plan_sealed", True),
            ("analysis.execution_plan_sha256", artifact.authored_plan_sha256),
            ("analysis.execution_plan_source_attempt_id", "analyze-0"),
        ):
            engine.run_evidence_state.set_fact(
                key,
                value,
                evidence_ref=PROJECT_EXECUTION_PLAN_PATH,
                source_phase="analyze",
                source_attempt_id="analyze-0",
            )
    validator = SimpleNamespace(
        validate_project_analysis_status=lambda _: (
            survey
            if survey is not None
            else {"analyzed": True, "has_static_test_count": True, "static_test_count": 12}
        )
    )
    tool = PhaseTool(
        machine=engine.phase_machine,
        validator=validator,
        orchestrator=container,
        project_name="demo",
        run_evidence_state=engine.run_evidence_state,
    )
    engine.orchestrator = container
    engine.tools["phase"] = tool
    return engine, tool, container


def _deliver(engine, result):
    return engine._handle_phase_signals(
        [
            SimpleNamespace(
                step_type=SimpleNamespace(value="action"),
                tool_name="phase",
                tool_result=result,
            )
        ]
    )


@pytest.mark.parametrize("outcome", ["unknown", "failed"])
@pytest.mark.parametrize("old_plan", [False, True])
def test_no_plan_close_advances_a_surveyed_project_without_granting_completion(outcome, old_plan):
    engine, tool, container = _analyze(old_plan=old_plan)
    original_plan = container.files.get(PROJECT_EXECUTION_PLAN_PATH)

    result = tool.execute(
        action="done",
        outcome=outcome,
        key_results="Survey exists, but no valid execution strategy was established.",
    )

    assert result.succeeded is True  # The terminal call, not the project, succeeded.
    assert result.metadata["gate_result"]["validated_outcome"] == "success"
    facts = result.metadata["gate_result"]["validated_facts"]
    assert facts["analysis.build_entry_ready"] is True
    assert facts["analysis.execution_plan_sealed"] is False
    assert facts["analysis.execution_plan_artifact_present"] is old_plan
    if old_plan:
        assert facts["analysis.execution_plan_ref"] == PROJECT_EXECUTION_PLAN_PATH
        assert facts["analysis.execution_plan_source_attempt_id"] == "analyze-0"
    assert not result.metadata["phase_claim"].get("execution_plan_sha256")
    assert "execution_plan_candidate" not in result.metadata
    assert engine.phase_machine.current_phase == "analyze"
    assert _deliver(engine, result) == "done"
    assert engine.phase_machine.current_phase == "build"
    assert [(record.phase, record.outcome.value) for record in engine.phase_machine.records] == [
        ("analyze", "success"),
    ]
    assert engine.finalized_reasons == []
    assert engine.run_evidence_state.fact_value("analysis.build_entry_ready") is True
    assert container.files.get(PROJECT_EXECUTION_PLAN_PATH) == original_plan
    assert engine._read_sealed_execution_plan() is None
    assert engine._system_prompt_for_current_phase("BASE") == "BASE"
    snapshot = VerdictFinalizer(container)._snapshot_for_state(engine.run_evidence_state)
    assert snapshot.verdict != "success"
    assert engine.run_evidence_state.phase_records == engine.phase_machine.records


@pytest.mark.parametrize("outcome", ["success", "partial"])
@pytest.mark.parametrize("old_plan", [False, True])
def test_no_plan_optimistic_close_cannot_use_old_authority(outcome, old_plan):
    engine, tool, container = _analyze(old_plan=old_plan)
    original_plan = container.files.get(PROJECT_EXECUTION_PLAN_PATH)
    result = tool.execute(action="done", outcome=outcome, key_results="Survey complete.")
    assert result.succeeded is True
    assert "execution_plan_candidate" not in result.metadata
    assert not result.metadata["phase_claim"].get("execution_plan_sha256")
    assert _deliver(engine, result) == "done"
    assert engine.phase_machine.current_phase == "build"
    assert container.files.get(PROJECT_EXECUTION_PLAN_PATH) == original_plan


@pytest.mark.parametrize("outcome", ["unknown", "failed", "success"])
def test_supplied_invalid_plan_is_not_a_no_plan_exit(outcome):
    engine, tool, _ = _analyze()
    result = tool.execute(action="done", outcome=outcome, execution_plan={})
    assert result.succeeded
    assert result.metadata["plan_warning"]
    assert "execution_plan_candidate" not in result.metadata
    assert _deliver(engine, result) == "done"
    assert engine.phase_machine.current_phase == "build"


@pytest.mark.parametrize("outcome", ["unknown", "failed"])
def test_missing_survey_still_requires_harness_recovery(outcome):
    engine, tool, _ = _analyze(
        survey={"analyzed": False, "analysis_status_code": "analysis_facts_missing"}
    )
    result = tool.execute(action="done", outcome=outcome)
    assert result.succeeded is False
    assert result.metadata["control_disposition"] == "harness_recovery_required"
    assert _deliver(engine, result) is None
    assert engine.phase_machine.records == ()


def test_blocked_remains_contradicted_by_green_analyze_evidence():
    engine, tool, _ = _analyze()
    result = tool.execute(action="blocked", outcome="unknown", reason="Plan not established.")
    assert result.error_code == "blocked_contradicted_by_green_evidence"
    assert "analyze" in result.error.lower()
    assert "green build" not in result.error.lower()
    assert _deliver(engine, result) is None
    assert engine.phase_machine.records == ()


def test_no_plan_exit_preserves_an_observed_failed_analysis():
    engine, tool, _ = _analyze(survey={"success": False, "reason": "No build system found."})
    result = tool.execute(action="done", outcome="failed")
    assert result.succeeded is True
    assert _deliver(engine, result) == "done"
    assert engine.phase_machine.records[0].outcome is PhaseOutcome.FAILED
    assert engine.phase_machine.current_phase == "report"


@pytest.mark.parametrize("outcome", ["unknown", "failed", "success"])
def test_other_attempt_document_read_cannot_supply_plan_authority(tmp_path, outcome):
    from sag.agent.evidence_state import StateScope
    from sag.agent.output_storage import OutputStorageManager
    from sag.tools.base import ToolResult

    engine, tool, _ = _analyze()
    storage = OutputStorageManager(tmp_path / "contexts")
    ref = storage.store_output(
        task_id="old-analyze", tool_name="search", output="Build with ./mvnw compile -DskipTests."
    )
    plan = _authored_execution_plan()
    plan["documents_reviewed"][0]["evidence_refs"] = [ref]
    plan["test_disposition"]["evidence_refs"] = [ref]
    plan["test_disposition"]["definition_evidence_refs"] = [ref]
    engine.run_evidence_state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "search",
        ToolResult.completed_success(output="document read", output_ref=ref),
        params={"target": "file:/workspace/demo/DEVNOTES.txt", "pattern": "."},
        source_phase="analyze",
        source_attempt_id="analyze-0",
    )
    tool.bind_execution_plan_evidence(storage)
    result = tool.execute(action="done", outcome=outcome, execution_plan=plan)
    assert result.succeeded
    assert "current Analyze attempt" in result.metadata["plan_warning"]
    assert "execution_plan_candidate" not in result.metadata
    assert _deliver(engine, result) == "done"
    assert engine.phase_machine.current_phase == "build"
