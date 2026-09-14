"""Regressions from the 20-project mini-high / terra-high paired experiment."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag.agent import job_obligations
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.invocation_receipts import toolchain_fingerprint
from sag.agent.output_storage import OutputStorageManager
from sag.agent.react_engine import ReActEngine
from sag.agent.receipt_test_rows import receipt_failure_summary
from sag.agent.tool_orchestration import ToolCall, ToolExecution, format_tool_result
from sag.tools.base import (
    ActualToolExecution,
    ToolResult,
    bind_tool_result_output_storage,
)
from sag.tools.build.build_tool import BuildTool


@pytest.mark.parametrize(
    "tool,banner", [("maven", "Apache Maven 3.9.4"), ("gradle", "Gradle 8.7")]
)
@pytest.mark.parametrize(
    "prefix", ["", "Warning: JAVA_HOME environment variable is not set.\n", "\n\n"]
)
def test_version_banner_survives_launcher_noise(tool, banner, prefix):
    observed = toolchain_fingerprint(
        lambda command: {
            "exit_code": 0,
            "output": f"/bin/runner\nSAGTOOLCHAIN\n{prefix}\x1b[1m{banner}\x1b[0m\n",
        },
        executable="runner",
        version_flag="--version",
        tool=tool,
    )
    assert observed == {"executable": "/bin/runner", "version": banner}


@pytest.mark.parametrize(
    "output",
    [
        "Warning: JAVA_HOME environment variable is not set.",
        "Apache Maven 3.9.4\nApache Maven 3.9.16",
        "Maven home: /opt/maven-3.9.4\nJava version: 17",
        "noise\n" * 64 + "Apache Maven 3.9.4",
    ],
)
def test_missing_or_conflicting_banners_never_supply_a_version(output):
    observed = toolchain_fingerprint(
        lambda command: {"exit_code": 0, "output": "/bin/mvn\nSAGTOOLCHAIN\n" + output},
        executable="mvn",
        version_flag="--version",
        tool="maven",
    )
    assert observed == {"executable": "/bin/mvn"}


@pytest.mark.parametrize("exit_code", [1, 127])
def test_unsuccessful_probe_does_not_certify_its_output(exit_code):
    assert (
        toolchain_fingerprint(
            lambda command: {
                "exit_code": exit_code,
                "output": "/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9.4",
            },
            executable="mvn",
            version_flag="--version",
            tool="maven",
        )
        is None
    )


@pytest.mark.parametrize(
    "observed",
    [{"executable": "/opt/maven/bin/mvn", "version": "Apache Maven 3.9.4"}, {}, None],
)
def test_async_settlement_never_uses_the_later_control_environment(observed):
    from test_job_settlement import _obligation, _orchestrator, _settled_receipt

    orchestrator = _orchestrator()
    obligation = _obligation(tool="maven", argv="mvn verify")
    if observed is None:
        obligation.pop(
            "toolchain_fingerprint", None
        )  # Historical obligation without a probe.
    else:
        obligation["toolchain_fingerprint"] = observed
    assert job_obligations.write_obligation(orchestrator, obligation)
    # This fake's later shell advertises Gradle 8.7. It cannot supply Maven's
    # dispatch version, or fill an absent dispatch-time observation.
    assert len(job_obligations.reconcile_job_obligations(orchestrator).settlements) == 1
    receipt = _settled_receipt(orchestrator)
    assert receipt.get("toolchain_fingerprint") == (observed or None)


def test_dispatch_fingerprint_is_immutable_and_bounded():
    from test_job_settlement import _obligation, _orchestrator

    orchestrator = _orchestrator()
    obligation = _obligation(
        toolchain_fingerprint={"executable": "/bin/gradle", "version": "Gradle 8.7"}
    )
    assert job_obligations.write_obligation(orchestrator, obligation)
    revised = copy.deepcopy(obligation)
    revised["toolchain_fingerprint"]["version"] = "Gradle 9.0"
    assert not job_obligations.write_obligation(orchestrator, revised)
    for invalid in ({"version": "x" * 4097}, {"version": 123}, {"unknown": "value"}):
        with pytest.raises(ValueError, match="toolchain_fingerprint"):
            job_obligations.validate_obligation_v3(
                {**obligation, "toolchain_fingerprint": invalid}
            )


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("failed", [True, False])
def test_visible_build_envelope_retains_the_leaf_file_without_double_ingestion(
    tmp_path, wrapped, failed
):
    storage = OutputStorageManager(tmp_path)
    raw = "normal build output\n" * 2000 + "ROOT_FAILURE\n"
    with bind_tool_result_output_storage(storage):
        ref = storage.store_output("build", "maven", raw)
        inner = (
            ToolResult.completed_failure if failed else ToolResult.completed_success
        )(
            output="short summary",
            raw_output=raw,
            output_ref=ref,
        )
        original = (
            BuildTool._envelope(
                SimpleNamespace(),
                inner,
                "maven",
                "install",
                "install",
                "/workspace/demo",
            )
            if wrapped
            else inner
        )
        call = ToolCall(name="build", raw_params={"command": "mvn install"})
        execution = ToolExecution(
            call=call,
            result=original,
            status="failure" if failed else "success",
            raw_params=call.raw_params,
            observation_text=format_tool_result("build", original),
            attempted_execution=True,
            actual_executions=[
                ActualToolExecution("maven", {"command": "install"}, inner)
            ],
        )
        engine = ReActEngine.__new__(ReActEngine)
        engine.run_evidence_state = RunEvidenceState(run_id="envelope")
        engine.phase_machine = None
        engine.context_manager = SimpleNamespace(current_task_id="build")
        engine.output_storage = storage
        returned, _, leaves = engine._record_execution_bundle(execution, call)
    assert returned.metadata["output_path"] in execution.observation_text
    assert Path(returned.metadata["output_path"]).read_text() == raw
    assert returned.output_ref == original.output_ref == leaves[0].result.output_ref
    assert returned.operation_outcome == original.operation_outcome
    assert len(engine.run_evidence_state.tool_observations) == 1
    assert len(engine.run_evidence_state.action_attempts) == 1
    assert "output_path" not in original.metadata


def failure_receipt():
    return {
        "receipt_id": "inv-maven-test-001",
        "argv": "mvn verify",
        "exit_code": 1,
        "testcase_outcomes": {
            "nodes": [
                {
                    "node_id": "example.SpaceTest#cleansFiles",
                    "status": "failed",
                    "reason": "expected:<45> but was:<55>",
                }
            ]
        },
        "testcase_execution_rows": {
            "rows": [
                {
                    "owner": "example.SpaceTest",
                    "test_name": "cleansFiles",
                    "outcome": "failed",
                    "module_coordinate": "store",
                    "report_path": "/workspace/demo/target/surefire-reports/TEST-Space.xml",
                    "report_sha256": "a" * 64,
                }
            ]
        },
    }


def test_failure_summary_preserves_assertion_and_unambiguous_receipt_links():
    receipt = failure_receipt()
    original = copy.deepcopy(receipt)
    summary = receipt_failure_summary(receipt)
    assert "expected:<45> but was:<55>" in summary
    assert "example.SpaceTest#cleansFiles" in summary
    assert '"module": "store"' in summary and "a" * 64 in summary
    assert "receipt_file=" in summary and "exit_code=1" in summary
    assert receipt == original


def test_duplicate_case_names_do_not_acquire_a_guessed_report_binding():
    receipt = failure_receipt()
    rows = receipt["testcase_execution_rows"]["rows"]
    rows.append(
        {
            **rows[0],
            "module_coordinate": "other",
            "report_path": "/workspace/other/TEST-Space.xml",
        }
    )
    summary = receipt_failure_summary(receipt)
    assert "expected:<45> but was:<55>" in summary
    assert '"module"' not in summary and '"report_path"' not in summary


def test_diagnostic_sample_states_omissions_and_does_not_enumerate_green_results():
    receipt = failure_receipt()
    node = receipt["testcase_outcomes"]["nodes"][0]
    receipt["testcase_outcomes"]["nodes"] = [
        {**node, "node_id": f"test-{i}"} for i in range(50)
    ]
    summary = receipt_failure_summary(receipt)
    assert len(summary.encode()) < 8192 and "omitted" in summary
    assert '"test": "test-7"' in summary and '"test": "test-8"' not in summary
    receipt["testcase_outcomes"]["nodes"] = [{**node, "status": "passed"}]
    assert receipt_failure_summary(receipt) == ""


@pytest.mark.parametrize("window", [4096, 6000, 65536])
def test_actual_advisor_request_keeps_failure_facts_under_log_pressure(
    tmp_path, window
):
    from test_advisor_tool import _advisor_engine

    summary = receipt_failure_summary(failure_receipt())
    engine = _advisor_engine()
    engine.config.advisor_context_window = window
    engine.control_event_sink = SimpleNamespace(path=tmp_path / "control_events.jsonl")
    engine.run_evidence_state = RunEvidenceState(run_id="failure-context")
    result = ToolResult.completed_failure(
        output="verbose output is summarized",
        raw_output="\n".join(f"ERROR cache noise {i}" for i in range(30000)),
        metadata={
            "receipt_id": "inv-maven-test-001",
            "command": "mvn verify",
            "test_failure_summary": summary,
        },
    )
    engine.run_evidence_state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "maven",
        result,
        params={"command": "verify"},
        execution_id="exec-failed",
    )
    assert summary in format_tool_result("build", result)
    before = engine.run_evidence_state.model_dump_json()
    response = engine.consult_advisor()
    assert response.metadata["advisor"] == "advice"
    text = "\n".join(
        m["content"] for call in engine.llm_client.calls for m in call["messages"]
    )
    # A tiny window may use several consultations, but the structured failure
    # row must fit in one request with its test, assertion, and source intact.
    assert next(line for line in summary.splitlines() if line.startswith("{")) in text
    assert (
        "receipt_file=/workspace/.setup_agent/invocation_receipts/inv-maven-test-001.json"
        in text
    )
    assert engine.run_evidence_state.model_dump_json() == before
    for audit in engine.advisor_telemetry["calls"]:
        assert (
            audit["context"]["estimated_input_tokens"]
            <= audit["context"]["input_token_budget"]
        )


def test_latest_success_does_not_keep_old_failure_as_current():
    from test_advisor_jvm_test_observations import digest

    assert "expected:<45>" not in digest(
        {
            "system": "maven",
            "receipt_id": "old",
            "test_failure_summary": receipt_failure_summary(failure_receipt()),
        },
        {"system": "maven", "receipt_id": "new"},
    )


@pytest.mark.parametrize("snapshot_current", [True, False])
def test_report_recovery_intersects_the_sealed_execution_policy(
    tmp_path, snapshot_current
):
    from test_terminal_claim_convergence import _engine

    from sag.agent.phase_machine import PhaseMachine
    from sag.tools.report_tool import ReportTool

    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="report")
    engine.tools["report"] = ReportTool()
    engine.run_evidence_state.seal(finalized_at="2026-09-14T00:00:00Z")
    engine.verdict_finalizer = SimpleNamespace(
        has_current_snapshot=lambda state: snapshot_current
    )
    affordances = engine._repair_tool_affordances(report_recovery=True)
    assert {a.tool for a in affordances} == ({"report"} if snapshot_current else set())
    assert engine._repair_tool_affordances() == ()
    assert (
        engine._refusal_for_call(ToolCall(name="build", raw_params={"action": "test"}))
        is not None
    )


def test_missing_report_can_be_submitted_through_a_real_repair_intent(
    tmp_path, monkeypatch
):
    from test_terminal_claim_convergence import _engine
    from test_tool_orchestration_parameters import _orchestrator

    from sag.agent.phase_gates import ValidatorState, validate_phase_claim
    from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
    from sag.tools.report_tool import ReportTool

    engine = _engine()
    engine.phase_machine = PhaseMachine(start_phase="report")
    engine.run_evidence_state.seal(finalized_at="2026-09-14T00:00:00Z")
    engine.verdict_finalizer = SimpleNamespace(has_current_snapshot=lambda state: True)
    report = ReportTool()
    engine.tools["report"] = report
    engine._active_native_tool_call_id = "call-report-recovery"
    claim = PhaseClaim(
        phase="report", signal="done", claimed_outcome=PhaseOutcome.SUCCESS
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="report artifact missing",
        code="report_missing",
    )
    context, code = engine._install_repair_context(claim, gate)
    assert code == "persisted" and context is not None
    engine._pending_repair_context = context
    assert {a.tool for a in context.allowed_tool_affordances} == {"report"}

    # The formatter is outside this policy experiment; write a small artifact
    # through the real report tool, normalizer, and pre-dispatch repair gate.
    path = tmp_path / "setup-report-recovered.md"

    def render(*args, **kwargs):
        path.write_text("# Recorded setup result\n")
        return path.read_text(), "partial", str(path), {}, {}

    monkeypatch.setattr(report, "_generate_comprehensive_report", render)
    monkeypatch.setattr(
        report, "_generate_condensed_log_output", lambda *args: str(path)
    )
    call = ToolCall(
        name="report",
        raw_params={"action": "generate", "status": "partial"},
        repair_intent_submission={
            "blocking_fact_refs": [context.trigger_assessment_id],
            "repair_hypothesis": "The missing delivery artifact can be generated from sealed results.",
            "next_action_kind": "generate",
            "expected_observation": ["artifact_or_report_delta"],
            "stop_condition": "Stop after the report artifact is saved.",
        },
    )
    assert engine._refusal_for_call(call) is None
    dispatcher, _, _, _ = _orchestrator(
        tools={"report": report},
        before_tool_execute=engine._prepare_control_action,
    )
    before = engine.run_evidence_state.model_dump_json()
    execution = dispatcher.execute(call)
    assert execution.attempted_execution and execution.result.succeeded, (
        execution.observation_text
    )
    engine._record_execution_bundle(execution, call)
    assert path.exists() and engine._report_delivered
    assert call.action_intent.repair_context_id == context.repair_context_id
    assert engine.run_evidence_state.model_dump_json() == before
