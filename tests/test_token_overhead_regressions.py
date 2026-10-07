"""Regression paths from the source-bound 2026-09-23 token investigation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_loop_engine import _engine
from test_verdict_finalizer import _RateValidator, _set_rate_test_rollup

from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.control_events import ControlEvent, ControlEventSink, action_envelope_sha256
from sag.agent.phase_gates import ValidatorState, check_phase_claim
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import ReActStep, StepType
from sag.agent.report_delivery import report_delivery_binding
from sag.agent.replay import (
    ReplayValidationError,
    recover_active_repair_context,
    recover_active_repair_context_from_path,
)
from sag.agent.verdict_finalizer import (
    EvidenceCloseReason,
    RunTerminationStatus,
    read_live_verdict_snapshot,
)
from sag.tools.base import ToolResult
from sag.tools.report_tool import ReportTool

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/token_overhead/archived_excerpts.json").read_text()
)


def test_setup_kickoff_and_real_system_prompt_preserve_one_operational_contract():
    """Catch stale rules appended after the builder, not just YAML in isolation."""
    from io import StringIO

    from rich.console import Console

    from sag.agent.agent import SetupAgent
    from sag.agent.react_prompt_builder import ReActPromptBuilder
    from sag.config.prompt_loader import load_react_engine_prompts

    engine = _engine([_no_tool_turn()] * 2)
    engine.repository_url = "https://example.test/build-demo.git"
    engine.repository_ref = "pinned-ref"
    engine.config.compact_setup_prompt = True
    engine.prompt_builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=SimpleNamespace(
            get_current_context_info=lambda: {
                "context_type": "branch", "context_id": "unknown",
                "task": None, "focus": None,
            }
        ),
        tools={},
    )
    agent = object.__new__(SetupAgent)
    agent.react_engine = engine
    agent.console = Console(file=StringIO())
    agent.max_iterations = 2
    agent._finalize_run_pin = lambda: None
    goal = (
        "Run mvn -B clean verify with Java 17.\n"
        "Never modify tracked files or add test skips.\n"
        "Write /work/toolpaths.json; preserve failed evidence."
    )
    agent._run_unified_setup(engine.repository_url, "build-demo", goal,
                             project_ref=engine.repository_ref)
    system = engine.llm_client.requests[0][0]["content"]
    assert system.count(goal) == 1
    assert engine.repository_url in system and "Repository ref: pinned-ref" in system
    assert "execution_plan is optional" in system
    assert "Submit a model-authored execution_plan before Analyze can enter Build" not in system
    assert "the Analyze plan is sealed before routing" not in system
    assert "CURRENT CONTEXT:" not in system
    assert system.count("AVAILABLE TOOLS:") == 1
    for boundary in (
        "build/test environment", "packaging, documentation or quality checks",
        "BUILD SUCCESS cannot override validator findings", "raw shell",
        "Do not add skips", "actual receipt and reports", "project",
        "java_distribution", "java_capabilities", "registered", "env",
        "repair_intent", "durable report artifact", "checked against physical evidence",
    ):
        assert boundary.casefold() in system.casefold()


def _no_tool_turn():
    text = next(
        message["content"]
        for message in FIXTURES["report_tail"]["messages"]
        if message["role"] == "assistant" and not message.get("tool_calls")
    )
    return NativeTurn(text=text, tool_calls=(), model_used="scripted-model")


def test_same_state_gets_one_protocol_correction_then_honest_abort():
    engine = _engine([_no_tool_turn()] * 150, max_iterations=150)
    result = engine.run_setup_loop("Set up the project.")
    assert result.termination is RunTerminationStatus.ABORTED
    assert engine.current_iteration == 2
    assert len(engine.llm_client.requests) == 2
    assert "No tool was called" in engine.llm_client.requests[1][-1]["content"]
    assert "no tool progress" in engine.phase_machine.records[-1].reason
    assert len(engine.context_journal.records) == 2


def test_no_tool_rounds_use_the_shared_compaction_path(monkeypatch):
    engine = _engine([_no_tool_turn()] * 2, max_iterations=2)
    compacted = []
    monkeypatch.setattr(
        engine, "_compact_window_if_needed", lambda phase: (compacted.append(phase), 0)
    )
    engine.run_setup_loop("Set up the project.")
    assert compacted == [True, True]


def test_new_evidence_allows_a_new_protocol_correction():
    engine = _engine([_no_tool_turn()] * 3)
    native = engine.llm_client.get_native_turn

    def get_turn(messages, **kwargs):
        if len(engine.llm_client.requests) == 1:
            engine.run_evidence_state.set_fact(
                "environment.ready", True, evidence_ref="probe://new"
            )
        return native(messages, **kwargs)

    engine.llm_client.get_native_turn = get_turn
    result = engine.run_setup_loop("Set up the project.")
    assert result.termination is RunTerminationStatus.ABORTED
    assert engine.current_iteration == 3


def test_no_tool_guard_preserves_the_required_test_attempt():
    engine = _engine([_no_tool_turn()] * 3)
    forced = []
    requirement = SimpleNamespace(action_text=lambda: "build(action=test)")
    engine._missing_required_test_attempt = lambda: requirement if not forced else None
    engine._force_required_test_attempt = lambda *args, **kwargs: forced.append(kwargs) or True
    result = engine.run_setup_loop("Set up the project.")
    assert result.termination is RunTerminationStatus.ABORTED
    assert len(forced) == 1
    assert engine.current_iteration == 3


def _report_engine():
    engine = _engine([])
    engine.phase_machine = PhaseMachine(start_phase="report")
    engine._phase_intro_step = lambda: ReActStep(
        step_type=StepType.SYSTEM_GUIDANCE, content="=== PHASE: REPORT ===", timestamp="test"
    )
    engine.orchestrator = engine.verdict_finalizer.orchestrator
    engine.verdict_finalizer.validator = _RateValidator()
    engine.verdict_finalizer.project_name = "demo"
    _set_rate_test_rollup(
        engine.run_evidence_state,
        passed=90,
        failed=10,
        errors=0,
        driven_modules=["core", "io"],
        execution_state="partial",
    )
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state, EvidenceCloseReason.TEST_TERMINATED
    )
    engine.steps = []
    engine._phase_iterations = 0
    return engine, snapshot


def test_real_report_gate_closes_delivery_without_upgrading_partial(monkeypatch, tmp_path):
    engine, snapshot = _report_engine()
    assert snapshot.verdict == "partial"
    control_path = tmp_path / "control_events.jsonl"
    engine.control_event_sink = ControlEventSink(control_path, run_id=snapshot.run_id)
    engine.control_event_sink.emit("evidence_close", {"reason": "test_terminated"})
    tool = ReportTool(docker_orchestrator=engine.orchestrator, workflow_mode="setup")
    # Rendering decoration is irrelevant; persistence, sealing and grading are real.
    monkeypatch.setattr(tool, "_get_project_info", lambda: {})
    monkeypatch.setattr(tool, "_collect_execution_metrics", lambda: {})
    monkeypatch.setattr(tool, "_assemble_report_metrics_artifact", lambda **kw: {})
    monkeypatch.setattr(tool, "_persist_report_metrics", lambda data: True)
    monkeypatch.setattr(tool, "_generate_console_report", lambda *args: "partial")
    monkeypatch.setattr(tool, "_generate_markdown_report", lambda *args: "# Result: partial\n")
    result = tool.execute(action="generate", status="success", summary="Model claims success")
    assert result.succeeded, result.error
    assert result.metadata["verified_status"] == "partial"
    assert result.metadata.get("completion_signal") is None
    assert "REPORT GENERATED" in result.output and "SETUP COMPLETED" not in result.output
    assert not engine.phase_machine.is_complete  # ReportTool does not own transitions.
    engine.tools["report"] = tool
    engine.llm_client.turns = [
        NativeTurn(
            text="Generate the partial report.",
            tool_calls=(
                NativeToolCall(
                    id="report-1",
                    name="report",
                    arguments={
                        "action": "generate",
                        "status": "partial",
                        "summary": "Partial setup",
                    },
                    raw_arguments="{}",
                ),
            ),
            model_used="scripted-model",
        )
    ]
    termination = engine.run_setup_loop("Generate the final report.")
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert engine.current_iteration == 1
    assert engine.phase_machine.is_complete
    assert not engine._close_delivered_report()  # Idempotent controller close.
    assert len(engine.phase_machine.records) == 1
    assert read_live_verdict_snapshot(engine.orchestrator) == snapshot
    # A new reader must accept the delivery tail without reopening evidence or
    # creating another transition. The projection is a read-only restart seam.
    original = control_path.read_bytes()
    for _ in range(2):
        recovered = recover_active_repair_context_from_path(control_path, run_id=snapshot.run_id)
        assert recovered.context is None
    assert control_path.read_bytes() == original


@pytest.mark.parametrize("defect", ["missing", "stale_snapshot", "changed_bytes", "no_binding"])
def test_report_gate_requires_current_snapshot_and_exact_persisted_bytes(defect):
    engine, snapshot = _report_engine()
    path = "/workspace/setup-report-test.md"
    content = "# Report\n"
    delivery = report_delivery_binding(snapshot, path, content)
    engine.orchestrator.files[path] = content
    if defect == "missing":
        del engine.orchestrator.files[path]
    elif defect == "stale_snapshot":
        delivery["snapshot_sha256"] = "0" * 64
    elif defect == "changed_bytes":
        engine.orchestrator.files[path] = "wrong report"
    else:
        delivery = None
    claim = PhaseClaim(phase="report", signal="done", claimed_outcome=PhaseOutcome.SUCCESS)
    gate = check_phase_claim(
        "report", claim, None, engine.orchestrator, "demo", sealed=True, report_delivery=delivery
    )
    assert not gate.accepted
    assert gate.validator_state is not ValidatorState.GREEN
    assert read_live_verdict_snapshot(engine.orchestrator) == snapshot


def test_sealed_report_no_tool_abort_preserves_the_setup_verdict():
    engine, snapshot = _report_engine()
    engine.llm_client.turns = [_no_tool_turn()] * 150
    termination = engine.run_setup_loop("Finish reporting.", max_iterations=150)
    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.current_iteration == 2
    assert "report(action='generate'" in engine.llm_client.requests[1][-1]["content"]
    assert read_live_verdict_snapshot(engine.orchestrator) == snapshot


def test_unbound_successful_report_cannot_mark_delivery_complete():
    engine, snapshot = _report_engine()
    result = ToolResult.completed_success(
        output="SETUP COMPLETED", metadata={"completion_signal": True, "task_completed": True}
    )
    engine._record_tool_execution("report", {}, result)
    assert not engine._report_delivered
    assert not engine._close_delivered_report()
    assert read_live_verdict_snapshot(engine.orchestrator) == snapshot


def _report_tail_events():
    params = {"action": "generate"}
    rows = [
        ("evidence_close", {"reason": "test_terminated"}),
        (
            "action_envelope",
            {
                "envelope_id": "report-action",
                "tool_call_id": "report-call",
                "tool": "report",
                "exact_params": params,
                "envelope_sha256": action_envelope_sha256(
                    tool_call_id="report-call",
                    tool="report",
                    exact_params=params,
                ),
            },
        ),
        (
            "tool_result",
            {
                "envelope_id": "report-action",
                "execution_id": "report-execution",
                "tool": "report",
                "params": params,
                "scope": "project_analysis",
                "source_phase": "report",
                "result": {},
            },
        ),
        (
            "gate_decision",
            {
                "phase": "report",
                "claimed_outcome": "success",
                "validator_state": "green",
                "expected_accepted": True,
                "expected_outcome": "success",
                "validated_facts": {"report.present": True},
            },
        ),
        (
            "phase_transition",
            {
                "expected_kind": "flow_close",
                "expected_reason_code": "report_terminal",
            },
        ),
    ]
    return [
        ControlEvent(sequence=i, kind=kind, payload=payload).model_dump(mode="json")
        for i, (kind, payload) in enumerate(rows, 1)
    ]


@pytest.mark.parametrize(
    "defect",
    [
        "new_build",
        "nested_build",
        "wrong_phase",
        "new_build_fact",
        "new_build_transition",
        "reseal",
        "duplicate_result",
        "missing_result",
        "missing_transition",
        "after_flow_close",
    ],
)
def test_report_restart_does_not_reopen_evidence_or_relax_pairing(defect):
    events = _report_tail_events()
    assert recover_active_repair_context(events).context is None
    if defect == "new_build":
        events[1]["payload"]["tool"] = "maven"
        events[1]["payload"]["envelope_sha256"] = action_envelope_sha256(
            tool_call_id="report-call",
            tool="maven",
            exact_params={"action": "generate"},
        )
    elif defect == "nested_build":
        events[2]["payload"]["actual_executions"] = [
            {
                "execution_id": "hidden-build",
                "tool": "maven",
                "params": {},
                "result": {},
                "scope": "artifacts",
                "roles": ["build"],
            }
        ]
    elif defect == "wrong_phase":
        events[3]["payload"]["phase"] = "build"
    elif defect == "new_build_fact":
        events[3]["payload"]["validated_facts"]["build.success"] = True
    elif defect == "new_build_transition":
        events[4]["payload"].update(expected_kind="advance", expected_target="build")
    elif defect == "reseal":
        events.insert(1, dict(events[0]))
    elif defect == "duplicate_result":
        events.insert(3, dict(events[2]))
    elif defect == "missing_result":
        del events[2]
    elif defect == "missing_transition":
        events.pop()
    else:
        events.append(dict(events[1]))
    for i, event in enumerate(events, 1):
        event["sequence"] = i
    with pytest.raises(ReplayValidationError):
        recover_active_repair_context(events)


def _output_observation(state, tool, result, execution_id, params=None):
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        tool,
        result,
        execution_id=execution_id,
        params=params or {},
        source_phase="build",
    )


def test_advisor_preserves_failed_producer_after_successful_ref_read():
    engine = ReActEngine.__new__(ReActEngine)
    engine.run_evidence_state = RunEvidenceState(run_id="producer")
    failure = ToolResult.completed_failure(output="[ERROR] Javadoc failed", error="Javadoc failed")
    ref = failure.output_ref
    read = ToolResult.completed_success(
        output="Found error",
        raw_output="[ERROR] Javadoc failed",
        output_ref=ref,
        metadata={"source_ref": ref},
    )
    _output_observation(engine.run_evidence_state, "maven", failure, "producer")
    _output_observation(engine.run_evidence_state, "search", read, "reader", {"target": ref})
    sections = engine._advisor_output_sections()
    assert len(sections) == 1
    header = json.loads(sections[0].text.splitlines()[0])
    assert header["tool"] == "maven"
    assert header["operation_outcome"] == "failed"
    assert header["producer_execution_id"] == "producer"
    assert sections[0].priority == 30


@pytest.mark.parametrize("conflict", [False, True])
def test_advisor_never_inferrs_producer_success_from_missing_or_conflicting_lineage(conflict):
    engine = ReActEngine.__new__(ReActEngine)
    engine.run_evidence_state = RunEvidenceState(run_id="producer")
    ref = "output_shared"
    if conflict:
        for index in range(2):
            result = ToolResult.completed_success(output=f"output {index}", output_ref=ref)
            _output_observation(engine.run_evidence_state, "maven", result, f"producer-{index}")
    else:
        result = ToolResult.completed_success(
            output="read", output_ref=ref, metadata={"source_ref": ref}
        )
        _output_observation(engine.run_evidence_state, "search", result, "reader", {"target": ref})
    header = json.loads(engine._advisor_output_sections()[0].text.splitlines()[0])
    assert header["producer_status"] == ("conflict" if conflict else "unknown")
    assert header["operation_outcome"] == "unknown"
