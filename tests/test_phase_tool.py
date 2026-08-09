# tests/test_phase_tool.py
"""phase(action: done|blocked|note) lifecycle surface."""

from types import SimpleNamespace

import pytest

import sag.tools.phase_tool as phase_tool_module
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_gates import (
    ClaimDisposition,
    GateControlDisposition,
    GateResult,
    ValidatorState,
)
from sag.agent.phase_machine import PhaseOutcome
from sag.tools.phase_tool import PhaseTool


class GateRecorder:
    def __init__(
        self,
        ok=True,
        reason="",
        suggestions=None,
        *,
        validated_outcome=PhaseOutcome.SUCCESS,
        validator_state=ValidatorState.GREEN,
    ):
        self.calls = []
        self.result = GateResult(
            accepted=ok,
            validated_outcome=validated_outcome,
            claim_disposition=(ClaimDisposition.CONFIRMED if ok else ClaimDisposition.CONTRADICTED),
            validator_state=validator_state,
            reason=reason,
            suggestions=tuple(suggestions or ()),
        )

    def __call__(self, phase, claim, validator, orchestrator, project_name, *, sealed=False):
        self.calls.append(phase)
        self.sealed = sealed
        return self.result.with_claim(claim)


class EmptyPublishedEvidence:
    """A readable empty container ledger for the autouse host authority."""

    @staticmethod
    def execute_command(command, **_kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream([]),
            }
        return {"success": False, "exit_code": 1, "output": ""}


def _tool(gate, phase="build"):
    machine = SimpleNamespace(current_phase=phase, is_complete=False)
    return PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=EmptyPublishedEvidence(),
        project_name="demo",
        gate_fn=gate,
    )


def test_done_passes_gate_and_signals_engine():
    gate = GateRecorder(ok=True)
    tool = _tool(gate)

    result = tool.execute(
        action="done",
        outcome="success",
        key_results="compiled 115 classes",
        evidence=["output_x"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "done"
    assert result.metadata["phase_claim"]["key_results"] == "compiled 115 classes"
    assert gate.calls == ["build"]


def test_a_claim_carries_the_evidence_seal_to_the_gate():
    """The gate settles the job ledger before it grades (spec §3.2 trigger 2),
    and a SEALED run accepts no further evidence: the report phase can still
    claim after evidence-close, and settling for it would write a receipt the
    sealed verdict has already recorded as `job_unsettled`."""
    gate = GateRecorder(ok=True)
    tool = _tool(gate)
    tool.run_evidence_state = RunEvidenceState(run_id="phase-tool-seal")

    tool.execute(action="done", outcome="success", key_results="115 classes", evidence=["ref"])
    assert gate.sealed is False

    tool.run_evidence_state.seal(
        finalized_at="2026-07-29T11:17:37Z", close_reason="test_terminated"
    )
    tool.execute(action="done", outcome="success", key_results="115 classes", evidence=["ref"])
    assert gate.sealed is True


def test_model_owned_repair_gate_returns_facts_without_a_project_prescription():
    gate = GateRecorder(ok=False, reason="no artifacts", suggestions=["build(action='compile')"])
    gate.result = GateResult(
        accepted=False,
        validated_outcome=PhaseOutcome.FAILED,
        claim_disposition=ClaimDisposition.CONTRADICTED,
        validator_state=ValidatorState.RED,
        reason="no artifacts",
        suggestions=("build(action='compile')",),
        code="artifact_missing",
        validated_facts={"artifact_count": 0},
        control_disposition=GateControlDisposition.REPAIR_REQUIRED,
        blocker_owner="project",
    )
    tool = _tool(gate)

    result = tool.execute(action="done", outcome="success", key_results="done!", evidence=[])

    assert result.succeeded is False
    assert "phase_signal" not in result.metadata
    assert result.operation_outcome.value == "failed"
    assert result.suggestions == []
    assert result.metadata["control_disposition"] == "repair_required"
    assert result.metadata["blocker_owner"] == "project"
    assert result.metadata["gate_result"]["validated_facts"] == {"artifact_count": 0}
    assert result.metadata["gate_result"]["suggestions"] == []
    assert result.metadata["gate_result"]["claim_disposition"] == "contradicted"


def test_controller_barrier_rejection_keeps_its_control_disposition():
    gate = GateRecorder(
        ok=False,
        reason="controller job barrier remains active",
        validated_outcome=PhaseOutcome.UNKNOWN,
        validator_state=ValidatorState.UNAVAILABLE,
    )
    gate.result = GateResult(
        accepted=False,
        validated_outcome=PhaseOutcome.UNKNOWN,
        claim_disposition=ClaimDisposition.CONTRADICTED,
        validator_state=ValidatorState.UNAVAILABLE,
        control_disposition=GateControlDisposition.WAIT_REQUIRED,
        reason="controller job barrier remains active",
        code="job_controller_barrier",
    )

    result = _tool(gate).execute(action="done", outcome="partial", key_results="job still running")

    assert result.succeeded is False
    assert result.error_code == "job_controller_barrier"
    assert result.metadata["control_disposition"] == "wait_required"
    assert result.metadata["gate_result"]["control_disposition"] == "wait_required"
    assert "phase_signal" not in result.metadata


def test_unreadable_job_ledger_precedes_attempt_and_island_policy(monkeypatch):
    gate = GateRecorder(ok=True)
    tool = _tool(gate)

    class UnreadableLedger:
        @staticmethod
        def execute_command(_command, **_kwargs):
            return {
                "success": False,
                "exit_code": -1,
                "dispatch_status": "container_unavailable",
                "output": "ledger transport unavailable",
            }

    def policy_must_not_run(*_args, **_kwargs):
        raise AssertionError("project policy ran before the controller integrity gate")

    tool.orchestrator = UnreadableLedger()
    monkeypatch.setattr(phase_tool_module, "required_test_attempt", policy_must_not_run)
    monkeypatch.setattr(phase_tool_module, "build_attempt_requirement", policy_must_not_run)
    monkeypatch.setattr(phase_tool_module, "untried_islands_requirement", policy_must_not_run)

    result = tool.execute(action="done", outcome="failed", key_results="cannot grade")

    assert result.succeeded is False
    assert result.error_code == "job_evidence_integrity"
    assert result.metadata["control_disposition"] == "harness_recovery_required"
    assert result.metadata["blocker_owner"] == "harness"
    assert result.facts["run.job_integrity_failures"] == ["ledger_unreadable"]
    assert gate.calls == []


def test_required_test_floor_is_controller_owned_and_non_prescriptive(monkeypatch):
    requirement = SimpleNamespace(
        to_metadata=lambda: {"system": "maven", "root": "/workspace/demo"}
    )
    monkeypatch.setattr(phase_tool_module, "required_test_attempt", lambda *a, **k: requirement)

    result = _tool(GateRecorder(), phase="test").execute(
        action="done",
        outcome="success",
        key_results="tests complete",
    )

    assert result.error_code == "TEST_ATTEMPT_REQUIRED"
    assert result.suggestions == []
    assert result.metadata["control_disposition"] == "harness_recovery_required"
    assert result.metadata["blocker_owner"] == "harness"
    assert result.metadata["gate_result"]["suggestions"] == []


def test_missing_build_attempt_is_model_owned_fact_not_a_command(monkeypatch):
    monkeypatch.setattr(phase_tool_module, "required_test_attempt", lambda *a, **k: None)
    monkeypatch.setattr(
        phase_tool_module,
        "build_attempt_requirement",
        lambda *a, **k: SimpleNamespace(
            to_metadata=lambda: {
                "domain_id": "maven:/workspace/demo",
                "terminal_build_receipts": 0,
            }
        ),
    )

    result = _tool(GateRecorder()).execute(
        action="blocked",
        outcome="failed",
        reason="build did not run",
    )

    assert result.error_code == "BUILD_ATTEMPT_REQUIRED"
    assert result.suggestions == []
    assert result.metadata["control_disposition"] == "repair_required"
    assert result.metadata["blocker_owner"] == "unknown"
    assert "build(action" not in result.output


def test_untried_islands_are_facts_without_a_selected_next_island(monkeypatch):
    requirement = SimpleNamespace(
        to_metadata=lambda: {
            "untried": [
                {"root": "/workspace/demo/module-a", "system": "maven"},
                {"root": "/workspace/demo/module-b", "system": "gradle"},
            ]
        }
    )
    monkeypatch.setattr(phase_tool_module, "required_test_attempt", lambda *a, **k: None)
    monkeypatch.setattr(phase_tool_module, "build_attempt_requirement", lambda *a, **k: None)
    monkeypatch.setattr(
        phase_tool_module, "untried_islands_requirement", lambda *a, **k: requirement
    )

    result = _tool(GateRecorder()).execute(
        action="done",
        outcome="failed",
        key_results="some islands remain",
    )

    assert result.error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert result.suggestions == []
    assert result.metadata["control_disposition"] == "repair_required"
    assert result.metadata["blocker_owner"] == "unknown"
    assert "must choose" in result.output
    assert "build(action" not in result.output


def test_honest_terminal_unpersisted_close_preserves_recovery_disposition():
    gate = GateRecorder(
        ok=True,
        validated_outcome=PhaseOutcome.PARTIAL,
        validator_state=ValidatorState.PARTIAL,
    )
    gate.result = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.PARTIAL,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.PARTIAL,
        control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
        reason="terminal receipt was not persisted",
        code="evidence_unpersisted",
    )

    result = _tool(gate).execute(
        action="done", outcome="partial", key_results="durable evidence is partial"
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "done"
    assert result.metadata["control_disposition"] == "harness_recovery_required"


def test_analysis_facts_missing_gets_one_controller_survey_and_one_final_gate():
    class RecoveringGate:
        def __init__(self):
            self.calls = 0

        def __call__(self, phase, claim, validator, orchestrator, project_name, *, sealed=False):
            self.calls += 1
            if self.calls == 1:
                return GateResult(
                    accepted=False,
                    validated_outcome=PhaseOutcome.UNKNOWN,
                    claim_disposition=ClaimDisposition.CONTRADICTED,
                    validator_state=ValidatorState.UNAVAILABLE,
                    control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
                    blocker_owner="harness",
                    code="analysis_facts_missing",
                    validated_facts={"analysis.status_code": "analysis_facts_missing"},
                    claim=claim,
                )
            return GateResult(
                accepted=True,
                validated_outcome=PhaseOutcome.SUCCESS,
                claim_disposition=ClaimDisposition.PESSIMISTIC,
                validator_state=ValidatorState.GREEN,
                code="analysis_green",
                validated_facts={"analysis.build_entry_ready": True},
                claim=claim,
            )

    gate = RecoveringGate()
    surveys = []
    tool = _tool(gate, phase="analyze")
    tool.bind_analysis_facts_recovery(lambda: surveys.append("survey") or "created")

    result = tool.execute(action="done", outcome="failed", key_results="survey complete")

    assert result.succeeded is True
    assert gate.calls == 2
    assert surveys == ["survey"]
    audit = result.metadata["gate_result"]["validated_facts"]["run.analysis_recovery"]
    assert audit == {
        "kind": "framework_survey",
        "attempt": 1,
        "before_code": "analysis_facts_missing",
        "survey_status": "created",
        "after_code": "analysis_green",
        "resolved": True,
    }


def test_analysis_facts_recovery_is_one_shot_and_failed_gate_has_no_phase_signal():
    class MissingGate:
        def __init__(self):
            self.calls = 0

        def __call__(self, phase, claim, validator, orchestrator, project_name, *, sealed=False):
            self.calls += 1
            return GateResult(
                accepted=False,
                validated_outcome=PhaseOutcome.UNKNOWN,
                claim_disposition=ClaimDisposition.CONTRADICTED,
                validator_state=ValidatorState.UNAVAILABLE,
                control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
                blocker_owner="harness",
                code="analysis_facts_missing",
                claim=claim,
            )

    gate = MissingGate()
    surveys = []
    tool = _tool(gate, phase="analyze")
    tool.bind_analysis_facts_recovery(lambda: surveys.append("survey") or "failed")

    first = tool.execute(action="done", outcome="failed", key_results="missing")
    second = tool.execute(action="done", outcome="failed", key_results="still missing")

    assert first.succeeded is False
    assert "phase_signal" not in first.metadata
    assert second.succeeded is False
    assert "phase_signal" not in second.metadata
    assert surveys == ["survey"]
    assert gate.calls == 3  # first claim initial+regrade; second claim initial only


def test_analysis_recovery_callback_exception_is_one_failed_controller_attempt():
    gate = GateRecorder(
        ok=False,
        validated_outcome=PhaseOutcome.UNKNOWN,
        validator_state=ValidatorState.UNAVAILABLE,
    )
    gate.result = GateResult(
        accepted=False,
        validated_outcome=PhaseOutcome.UNKNOWN,
        claim_disposition=ClaimDisposition.CONTRADICTED,
        validator_state=ValidatorState.UNAVAILABLE,
        control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
        blocker_owner="harness",
        code="analysis_facts_missing",
    )
    tool = _tool(gate, phase="analyze")

    def unavailable():
        raise RuntimeError("survey transport unavailable")

    tool.bind_analysis_facts_recovery(unavailable)

    result = tool.execute(action="done", outcome="failed", key_results="missing")

    assert result.succeeded is False
    assert gate.calls == ["analyze", "analyze"]
    audit = result.metadata["gate_result"]["validated_facts"]["run.analysis_recovery"]
    assert audit["survey_status"] == "failed"
    assert audit["resolved"] is False


@pytest.mark.parametrize(
    ("sealed", "code"),
    [(False, "analysis_unavailable"), (True, "analysis_facts_missing")],
)
def test_analysis_recovery_is_not_run_when_sealed_or_unavailable(sealed, code):
    gate = GateRecorder(
        ok=False,
        validated_outcome=PhaseOutcome.UNKNOWN,
        validator_state=ValidatorState.UNAVAILABLE,
    )
    gate.result = GateResult(
        accepted=False,
        validated_outcome=PhaseOutcome.UNKNOWN,
        claim_disposition=ClaimDisposition.CONTRADICTED,
        validator_state=ValidatorState.UNAVAILABLE,
        control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
        blocker_owner="harness",
        code=code,
    )
    surveys = []
    tool = _tool(gate, phase="analyze")
    tool.bind_analysis_facts_recovery(lambda: surveys.append("survey") or "created")
    if sealed:
        tool.run_evidence_state = RunEvidenceState(run_id="sealed-analysis")
        tool.run_evidence_state.seal(
            finalized_at="2026-08-08T00:00:00Z", close_reason="test"
        )

    result = tool.execute(action="done", outcome="failed", key_results="cannot inspect")

    assert result.succeeded is False
    assert surveys == []
    assert gate.calls == ["analyze"]


def test_external_blocked_claim_is_accepted_when_evidence_is_unavailable():
    gate = GateRecorder(
        ok=True,
        reason="external repository unavailable",
        validated_outcome=PhaseOutcome.UNKNOWN,
        validator_state=ValidatorState.UNAVAILABLE,
    )
    tool = _tool(gate)

    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="develocity plugin unresolvable",
        evidence=["job:a"],
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "blocked"
    assert result.metadata["phase_claim"]["reason"] == "develocity plugin unresolvable"
    assert result.metadata["gate_result"]["validated_outcome"] == "unknown"
    assert gate.calls == ["build"]


def test_green_evidence_refuses_blocked_without_selecting_a_terminal_call(monkeypatch):
    monkeypatch.setattr(phase_tool_module, "required_test_attempt", lambda *a, **k: None)
    monkeypatch.setattr(phase_tool_module, "build_attempt_requirement", lambda *a, **k: None)
    monkeypatch.setattr(
        phase_tool_module, "untried_islands_requirement", lambda *a, **k: None
    )

    result = _tool(GateRecorder()).execute(
        action="blocked",
        outcome="failed",
        reason="model chose to stop",
    )

    assert result.succeeded is False
    assert result.error_code == "blocked_contradicted_by_green_evidence"
    rendered = f"{result.error}\n{result.output}\n" + "\n".join(result.suggestions)
    assert "phase(action=" not in rendered
    assert "build(action=" not in rendered
    assert "terminal outcome is bounded" in rendered


def test_blocked_requires_reason():
    result = _tool(GateRecorder()).execute(action="blocked", outcome="failed", reason="")
    assert result.succeeded is False


def test_note_signals_engine_for_durable_phase_notes():
    result = _tool(GateRecorder()).execute(action="note", text="trying maven 3.9.9 next")
    assert result.succeeded is True
    assert result.metadata == {
        "phase_signal": "note",
        "text": "trying maven 3.9.9 next",
    }


def test_machine_complete_rejects_actions():
    machine = SimpleNamespace(current_phase=None, is_complete=True)
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=None,
        project_name="demo",
        gate_fn=GateRecorder(),
    )
    result = tool.execute(action="done", outcome="unknown", key_results="x")
    assert result.succeeded is False
