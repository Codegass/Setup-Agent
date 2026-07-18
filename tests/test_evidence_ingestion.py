import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_python_tool import MANIFEST as PYTHON_MANIFEST
from test_python_tool import Orch as PythonOrchestrator
from test_python_tool import fail as command_fail
from test_python_tool import ok as command_ok
from test_verdict_finalizer import VERDICT_PATH, FakeVerdictOrchestrator

import sag.agent.agent as agent_module
import sag.agent.react_engine as react_engine_module
from sag.agent.agent import SetupAgent
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.output_storage import OutputStorageManager
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType
from sag.agent.tool_orchestration import (
    RecoveryDecision,
    ToolCall,
    ToolExecution,
    ToolOrchestrator,
)
from sag.agent.verdict_finalizer import (
    EvidenceCloseReason,
    ReportDeliveryStatus,
    RunTerminationStatus,
    VerdictFinalizer,
    read_verdict_snapshot,
)
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome, TestStats
from sag.tools.base import BaseTool, ToolError, ToolResult, bind_tool_result_output_storage
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.python_tool import PYTEST_REPORT_DIR, PythonTool


class _ContainerJUnitOrchestrator(PythonOrchestrator):
    """Run the production in-container extractor against a local report file."""

    def __init__(self, report_path, junit_xml, **kwargs):
        super().__init__(**kwargs)
        self.report_path = Path(report_path)
        self.junit_xml = junit_xml
        self.extraction_outputs = []

    def execute_command(self, cmd, workdir=None, timeout=None):
        if "SAG_ATTEMPT_TAGGED" in cmd:
            self.commands.append(cmd)
            if self.junit_xml is None:
                self.report_path.unlink(missing_ok=True)
            else:
                self.report_path.write_text(self.junit_xml, encoding="utf-8")
            argv = shlex.split(cmd)
            argv[0] = sys.executable
            argv[-2] = str(self.report_path)
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                self.junit_xml = self.report_path.read_text(encoding="utf-8")
            return {
                "success": completed.returncode == 0,
                "exit_code": completed.returncode,
                "output": completed.stdout or completed.stderr,
            }
        if "xml.etree.ElementTree" in cmd:
            self.commands.append(cmd)
            if self.junit_xml is None:
                self.report_path.unlink(missing_ok=True)
            else:
                self.report_path.write_text(self.junit_xml, encoding="utf-8")
            argv = shlex.split(cmd)
            argv[0] = sys.executable
            argv[-1] = str(self.report_path)
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
            )
            self.extraction_outputs.append(completed.stdout)
            return {
                "success": completed.returncode == 0,
                "exit_code": completed.returncode,
                "output": completed.stdout or completed.stderr,
            }
        if cmd.startswith("cat ") and PYTEST_REPORT_DIR in cmd:
            self.commands.append(cmd)
            if self.junit_xml is None:
                return {"success": False, "exit_code": 1, "output": "missing"}
            return command_ok(self.junit_xml[:30_000])
        return super().execute_command(cmd, workdir=workdir)


class _FailFirstAtomicWriteOrchestrator(FakeVerdictOrchestrator):
    def __init__(self):
        super().__init__()
        self._fail_next_trim = True

    def execute_command(self, command):
        if command.startswith("truncate -s -1 ") and self._fail_next_trim:
            self.commands.append(command)
            self._fail_next_trim = False
            return {"success": False, "exit_code": 1, "output": "transient failure"}
        return super().execute_command(command)


def _engine(tmp_path, *, phase="provision"):
    machine = PhaseMachine()
    while machine.current_phase != phase:
        machine.mark_done(f"{machine.current_phase} complete", [])

    orchestrator = FakeVerdictOrchestrator()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = machine
    engine.run_evidence_state = RunEvidenceState(run_id="session-engine")
    for record in machine.records:
        engine.run_evidence_state.record_phase_record(record)
    engine.verdict_finalizer = VerdictFinalizer(orchestrator)
    engine.output_storage = OutputStorageManager(Path(tmp_path) / "contexts")
    engine._report_attempted = False
    engine._report_delivered = False
    engine._report_failed = False
    engine.steps = [SimpleNamespace(content="old")]
    engine.recent_tool_executions = []
    engine._phase_iterations = 2
    engine.steps_since_context_switch = 2
    engine.context_manager = SimpleNamespace(
        current_task_id=None,
        update_task_status=lambda *args, **kwargs: True,
    )
    engine.prompt_builder = SimpleNamespace(invalidate_trunk_cache=lambda: None)
    engine.context_journal = None
    engine.agent_logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    engine._persist_phase_record = lambda *args, **kwargs: None
    engine._archive_window_steps = lambda: None
    engine._start_phase_branch = lambda: None
    engine._phase_intro_step = lambda: SimpleNamespace(content="phase intro")
    return engine, orchestrator


def _green_build(engine):
    return engine._record_tool_execution(
        "build",
        {"action": "compile"},
        ToolResult.completed_success(
            output="compile complete",
            facts={"build_success": True},
        ),
    )


def _green_tests(engine):
    return engine._record_tool_execution(
        "build",
        {"action": "test"},
        ToolResult.completed_success(
            output="tests complete",
            test_stats=TestStats(
                discovered=10,
                executed=10,
                passed=10,
                failed=0,
                skipped=0,
            ),
        ),
    )


def _phase_metadata(
    phase,
    *,
    signal="done",
    outcome=PhaseOutcome.SUCCESS,
    key_results="",
    reason="",
):
    outcome = PhaseOutcome(outcome)
    state = {
        PhaseOutcome.SUCCESS: ValidatorState.GREEN,
        PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
        PhaseOutcome.FAILED: ValidatorState.RED,
        PhaseOutcome.UNKNOWN: ValidatorState.UNAVAILABLE,
    }[outcome]
    facts = {}
    if phase == "provision":
        facts["provision.workspace_ready"] = outcome is PhaseOutcome.SUCCESS
    elif phase == "analyze":
        facts["analysis.build_entry_ready"] = outcome in {
            PhaseOutcome.SUCCESS,
            PhaseOutcome.PARTIAL,
        }
    elif phase == "build":
        facts["build.test_entry_ready"] = outcome in {
            PhaseOutcome.SUCCESS,
            PhaseOutcome.PARTIAL,
        }
    claim = PhaseClaim(
        phase=phase,
        signal=signal,
        claimed_outcome=outcome,
        key_results=key_results,
        reason=reason,
    )
    gate = GateResult(
        accepted=True,
        validated_outcome=outcome,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=state,
        reason=reason or "scripted gate",
        validated_facts=facts,
        claim=claim,
    )
    return {
        "phase_signal": signal,
        "phase_claim": claim.to_metadata(),
        "gate_result": gate.to_metadata(),
    }


def _phase_step(engine, signal="done", **metadata):
    return SimpleNamespace(
        tool_name="phase",
        tool_result=SimpleNamespace(
            metadata=_phase_metadata(
                engine.phase_machine.current_phase,
                signal=signal,
                **metadata,
            )
        ),
    )


def _prepare_action_execution(engine):
    engine.config = SimpleNamespace(verbose=False)
    engine.current_iteration = 1
    engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda tool_name: None)
    engine.emit = lambda *args, **kwargs: None
    engine._add_observation_step = lambda observation: None
    engine._apply_tool_execution_loop_effects = lambda execution: None


def _action_step(tool_name, params, content=None):
    return SimpleNamespace(
        step_type=StepType.ACTION,
        tool_name=tool_name,
        tool_params=params,
        tool_result=None,
        content=content or tool_name,
        model_used="test-model",
    )


def test_engine_ingests_result_once_with_full_output_ref(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    records_before = engine.phase_machine.records
    original = ToolResult.completed_success(
        output="full compile output",
        facts={"build_success": True},
    )

    recorded = engine._record_tool_execution("build", {"action": "compile"}, original)

    assert original.output_ref is None
    assert recorded is not original
    assert recorded.output_ref.startswith("output_")
    assert engine.output_storage.retrieve_output(recorded.output_ref) == original.output
    assert len(engine.run_evidence_state.tool_observations) == 1
    observation = engine.run_evidence_state.tool_observations[0]
    assert observation.result.output_ref == recorded.output_ref
    assert observation.provenance == recorded.output_ref
    assert engine.phase_machine.current_phase == "build"
    assert engine.phase_machine.current_attempt_id == "build-1"
    assert engine.phase_machine.records == records_before
    assert engine.phase_machine.current_record.termination.value == "running"
    assert engine.phase_machine.current_record.outcome.value == "unknown"


def test_execute_steps_calls_the_single_ingestion_boundary_once(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    result = ToolResult.completed_success(
        output="compile complete",
        facts={"build_success": True},
    )
    execution = ToolExecution(
        call=ToolCall(name="build", raw_params={"action": "compile"}),
        result=result,
        status="success",
        raw_params={"action": "compile"},
        validated_params={"action": "compile"},
        observation_text="compile complete",
        attempted_execution=True,
    )
    orchestrator = SimpleNamespace(execute=lambda call: execution)
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)
    step = _action_step("build", {"action": "compile"}, "compile")

    engine._execute_steps([step])

    assert len(engine.run_evidence_state.tool_observations) == 1
    assert len(engine.run_evidence_state.action_attempts) == 1
    assert step.tool_result.output_ref.startswith("output_")


def test_recovery_ingests_original_and_replacement_under_their_actual_scopes(tmp_path, monkeypatch):
    class RecoveringBuildTool(BaseTool):
        def __init__(self):
            super().__init__("build", "test recovery evidence")
            self.actions = []

        def execute(self, action: str) -> ToolResult:
            self.actions.append(action)
            if action == "test":
                return ToolResult.completed_failure(
                    output="one test failed",
                    error="one test failed",
                    error_code="TEST_FAILED",
                    test_stats=TestStats(
                        discovered=1,
                        executed=1,
                        passed=0,
                        failed=1,
                        skipped=0,
                    ),
                )
            return ToolResult.completed_success(
                output="compile recovered",
                facts={"build_success": True},
            )

    engine, _ = _engine(tmp_path, phase="test")
    tool = RecoveringBuildTool()
    orchestrator = ToolOrchestrator(
        tools={"build": tool},
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=engine.output_storage,
    )

    def recover(tool_name, params, failed_result):
        replacement = tool.safe_execute(action="compile")
        return RecoveryDecision(
            should_recover=True,
            strategy="compile_after_test_failure",
            replacement_result=replacement,
            replacement_params={"action": "compile"},
        )

    monkeypatch.setattr(orchestrator.recovery_handler, "recover", recover)
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)
    step = _action_step("build", {"action": "test"})

    engine._execute_steps([step])

    assert tool.actions == ["test", "compile"]
    assert step.tool_result.succeeded is True
    observations = engine.run_evidence_state.tool_observations
    assert [observation.scope for observation in observations] == [
        StateScope.TEST_RUNTIME,
        StateScope.ARTIFACTS,
    ]
    assert observations[0].result.test_stats.failed == 1
    assert observations[1].result.facts["build_success"] is True
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.ABORTED,
    )
    assert snapshot.test_stats.failed == 1
    assert snapshot.verdict == "failed"


@pytest.mark.parametrize(
    ("tool_name", "operation_params", "error_code"),
    [
        ("maven", {"command": "test"}, "BUILD_FAILED"),
        ("gradle", {"tasks": ["clean", ":app:test"]}, "BUILD_FILE_NOT_FOUND"),
    ],
)
def test_backend_test_recovery_keeps_both_attempts_in_test_scope(
    tmp_path, tool_name, operation_params, error_code
):
    class RecoveringBackendTool(BaseTool):
        def __init__(self):
            super().__init__(tool_name, "backend recovery evidence")
            self._parameter_schema = {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "task": {"type": "string"},
                    "tasks": {"type": ["string", "array"], "items": {"type": "string"}},
                    "working_directory": {"type": "string"},
                },
                "required": [],
            }
            self.calls = []
            self.results = [
                ToolResult.completed_failure(
                    output="tests failed because the working directory was not found",
                    error="working directory not found",
                    error_code=error_code,
                    test_stats=TestStats(
                        discovered=5,
                        executed=5,
                        passed=3,
                        failed=2,
                        skipped=0,
                    ),
                ),
                ToolResult.completed_success(
                    output="all tests passed from the recovered working directory",
                    test_stats=TestStats(
                        discovered=5,
                        executed=5,
                        passed=5,
                        failed=0,
                        skipped=0,
                    ),
                ),
            ]

        def execute(self, **params) -> ToolResult:
            self.calls.append(dict(params))
            return self.results.pop(0)

    engine, _ = _engine(tmp_path, phase="test")
    _green_build(engine)
    tool = RecoveringBackendTool()
    orchestrator = ToolOrchestrator(
        tools={tool_name: tool},
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states={"working_directory": "/workspace/app"},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=engine.output_storage,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)
    step = _action_step(tool_name, operation_params)

    engine._execute_steps([step])

    assert len(tool.calls) == 2
    for field, value in operation_params.items():
        assert [call[field] for call in tool.calls] == [value, value]
    assert tool.calls[-1]["working_directory"] == "/workspace/app"
    assert step.tool_result.succeeded is True
    observations = [
        observation
        for observation in engine.run_evidence_state.tool_observations
        if observation.tool_name == tool_name
    ]
    assert len(observations) == 2
    assert [observation.scope for observation in observations] == [
        StateScope.TEST_RUNTIME,
        StateScope.TEST_RUNTIME,
    ]
    assert [observation.result.test_stats.failed for observation in observations] == [2, 0]

    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )

    assert snapshot.verdict == "success"
    assert snapshot.test_stats.executed == 5
    assert snapshot.test_stats.passed == 5
    assert snapshot.test_stats.failed == 0
    assert snapshot.test_stats.raw.executed == 10
    assert snapshot.test_stats.raw.passed == 8
    assert snapshot.test_stats.raw.failed == 2


def test_guidance_only_recovery_does_not_fabricate_an_execution(tmp_path, monkeypatch):
    class FailingBuildTool(BaseTool):
        def __init__(self):
            super().__init__("build", "guidance-only recovery evidence")

        def execute(self, action: str) -> ToolResult:
            return ToolResult.completed_failure(
                output="compile actually failed",
                error="compile actually failed",
                error_code="ORIGINAL_COMPILE_FAILED",
            )

    engine, _ = _engine(tmp_path, phase="build")
    tool = FailingBuildTool()
    orchestrator = ToolOrchestrator(
        tools={"build": tool},
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=engine.output_storage,
    )

    def recover(tool_name, params, failed_result):
        guidance = ToolResult.completed_failure(
            output="try a different compiler next",
            error="recovery guidance only",
            error_code="GUIDANCE_ONLY",
        )
        return RecoveryDecision(
            should_recover=True,
            strategy="compile_guidance",
            replacement_result=guidance,
            replacement_params={"action": "compile"},
            metadata={"guidance_only": True},
        )

    monkeypatch.setattr(orchestrator.recovery_handler, "recover", recover)
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)
    step = _action_step("build", {"action": "compile"})

    engine._execute_steps([step])

    assert step.tool_result.error_code == "GUIDANCE_ONLY"
    observations = engine.run_evidence_state.tool_observations
    assert len(observations) == 1
    assert observations[0].result.error_code == "ORIGINAL_COMPILE_FAILED"


def test_large_pytest_junit_is_reduced_to_bounded_counts_inside_container(tmp_path):
    testcases = "\n".join(
        f'    <testcase classname="tests.generated_{index:03d}" '
        f'name="test_case_{index:03d}" time="0.001" />'
        for index in range(474)
    )
    junit = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites tests="474" failures="2" errors="1" skipped="4">\n'
        '  <testsuite name="pytest" tests="474" failures="2" errors="1" skipped="4">\n'
        f"{testcases}\n"
        "  </testsuite>\n"
        "</testsuites>"
    )
    assert len(junit.encode("utf-8")) > 31_794
    assert len(junit.splitlines()) > 474
    orchestrator = _ContainerJUnitOrchestrator(
        tmp_path / "large-pytest.xml",
        junit,
        manifest=dict(PYTHON_MANIFEST),
        rules=[
            ("--collect-only", command_ok("474 tests collected in 0.20s")),
            (
                "--junitxml",
                command_fail("2 failed, 1 error, 4 skipped, 467 passed", exit_code=1),
            ),
        ],
    )

    result = PythonTool(orchestrator).execute("test", working_directory="/workspace/proj")

    assert result.test_stats == TestStats(
        discovered=474,
        executed=474,
        passed=467,
        failed=3,
        skipped=4,
    )
    assert result.metadata["failed_tests"] == 2
    assert result.metadata["error_tests"] == 1
    assert result.raw_data["failed_tests"] == 2
    assert result.raw_data["error_tests"] == 1
    assert not any(
        command.startswith("cat ") and PYTEST_REPORT_DIR in command
        for command in orchestrator.commands
    )
    assert orchestrator.extraction_outputs
    assert max(len(output.encode("utf-8")) for output in orchestrator.extraction_outputs) < 256
    assert all(junit not in command for command in orchestrator.commands)


@pytest.mark.parametrize(
    ("junit_xml", "reason"),
    [
        (None, "missing"),
        ("<testsuites><testsuite>", "malformed"),
        (
            '<testsuite tests="9223372036854775808" failures="0" ' 'errors="0" skipped="0" />',
            "invalid_counts",
        ),
    ],
)
def test_unavailable_pytest_junit_conflict_reaches_sealed_snapshot(tmp_path, junit_xml, reason):
    python_orchestrator = _ContainerJUnitOrchestrator(
        tmp_path / f"{reason}-pytest.xml",
        junit_xml,
        manifest=dict(PYTHON_MANIFEST),
        rules=[
            ("pyproject.toml", command_ok("exists")),
            ("--collect-only", command_ok("5 tests collected in 0.02s")),
            ("--junitxml", command_ok("5 passed in 0.03s")),
        ],
    )
    build = BuildTool(
        python_orchestrator,
        python_tool=PythonTool(python_orchestrator),
    )

    result = build.execute("test", working_directory="/workspace/proj")
    engine, _ = _engine(tmp_path, phase="test")
    _green_build(engine)
    engine._record_tool_execution("build", {"action": "test"}, result)
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )

    assert result.test_stats is None
    assert result.conflicts == ["pytest_junit_unavailable"]
    assert result.metadata["junit_extraction"] == {
        "status": "unavailable",
        "reason": reason,
    }
    assert result.raw_data["junit_status"] == reason
    assert "pytest_junit_unavailable" in snapshot.conflicts
    assert snapshot.test_stats.executed == 0
    assert snapshot.verdict != "success"


def test_python_pytest_junit_stats_flow_through_build_to_sealed_verdict(tmp_path):

    junit = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests" tests="5" failures="0" errors="0" skipped="0">
  <testsuite name="pytest" tests="5" failures="0" errors="0" skipped="0">
    <testcase classname="tests.test_app" name="test_one" />
    <testcase classname="tests.test_app" name="test_two" />
    <testcase classname="tests.test_app" name="test_three" />
    <testcase classname="tests.test_app" name="test_four" />
    <testcase classname="tests.test_app" name="test_five" />
  </testsuite>
</testsuites>"""
    python_orchestrator = _ContainerJUnitOrchestrator(
        tmp_path / "five-passing.xml",
        junit,
        manifest=dict(PYTHON_MANIFEST),
        rules=[
            ("pyproject.toml", command_ok("exists")),
            ("--collect-only", command_ok("5 tests collected in 0.02s")),
            ("--junitxml", command_ok("5 passed in 0.03s")),
        ],
    )
    build = BuildTool(
        python_orchestrator,
        python_tool=PythonTool(python_orchestrator),
    )

    result = build.execute("test", working_directory="/workspace/proj")
    engine, _ = _engine(tmp_path, phase="test")
    _green_build(engine)
    engine._record_tool_execution("build", {"action": "test"}, result)
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )

    assert result.test_stats == TestStats(
        discovered=5,
        executed=5,
        passed=5,
        failed=0,
        skipped=0,
    )
    assert len(result.evidence_refs) == 1
    assert result.evidence_refs[0].startswith("/workspace/.setup_agent/pytest-reports/pytest-")
    assert snapshot.test_stats.discovered == 5
    assert snapshot.test_stats.executed == 5
    assert snapshot.test_stats.passed == 5
    assert snapshot.test_stats.failed == 0
    assert snapshot.test_stats.errors == 0
    assert snapshot.test_stats.skipped == 0
    assert result.metadata["failed_tests"] == 0
    assert result.metadata["error_tests"] == 0
    assert snapshot.verdict == "success"


def test_non_execution_is_audited_without_polluting_build_evidence(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    _green_build(engine)
    malformed = ToolResult.completed_failure(
        output="",
        error="missing required parameter",
        error_code="PARAMETER_VALIDATION_FAILED",
    )

    engine._record_tool_execution(
        "build",
        {"action": "compile"},
        malformed,
        attempted_execution=False,
    )
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state, EvidenceCloseReason.ABORTED
    )

    assert len(engine.run_evidence_state.action_attempts) == 2
    assert len(engine.run_evidence_state.tool_observations) == 1
    assert snapshot.build_evidence.green is True
    assert snapshot.build_evidence.outcome is OperationOutcome.SUCCESS


def test_failed_result_primary_repersistence_failure_uses_emergency_ref(tmp_path, monkeypatch):
    engine, _ = _engine(tmp_path, phase="build")
    records_before = engine.phase_machine.records
    origin_storage = OutputStorageManager(Path(tmp_path) / "origin")
    raw_failure = "[ERROR] complete compiler diagnostics"
    with bind_tool_result_output_storage(origin_storage, task_id="origin-build", tool_name="build"):
        failed = ToolResult.completed_failure(
            output="compile failed",
            raw_output=raw_failure,
            error="compile failed",
            error_code="MAVEN_BUILD_FAILED",
            failure_signature="MAVEN_BUILD_FAILED:canonical",
            error_tail_preview=raw_failure,
        )

    assert engine.output_storage.retrieve_output(failed.output_ref) is None
    monkeypatch.setattr(engine.output_storage, "store_output", lambda **kwargs: "")

    recorded = engine._record_tool_execution("build", {"action": "compile"}, failed)

    assert recorded.output_ref
    assert recorded.output_ref != failed.output_ref
    assert recorded.output_ref.startswith("output_emergency_")
    assert recorded.invocation_status is InvocationStatus.COMPLETED
    assert recorded.operation_outcome is OperationOutcome.FAILED
    assert recorded.error_code == failed.error_code
    assert recorded.failure_signature == failed.failure_signature
    assert recorded.error_tail_preview == failed.error_tail_preview
    reader = OutputStorageManager(engine.output_storage.storage_dir)
    assert reader.retrieve_output(recorded.output_ref) == raw_failure
    observation = engine.run_evidence_state.tool_observations[0]
    assert observation.result.output_ref == recorded.output_ref
    assert observation.provenance == recorded.output_ref
    assert engine.phase_machine.current_phase == "build"
    assert engine.phase_machine.current_attempt_id == "build-1"
    assert engine.phase_machine.records == records_before
    assert engine.phase_machine.current_record.termination.value == "running"
    assert engine.phase_machine.current_record.outcome.value == "unknown"


def test_total_output_persistence_failure_keeps_actual_failure_with_fallback_provenance(
    tmp_path,
):
    class FailedStorage:
        def store_output(self, **kwargs):
            return ""

        def store_emergency_output(self, **kwargs):
            return ""

        def retrieve_output(self, ref_id):
            return None

    engine, _ = _engine(tmp_path, phase="build")
    _green_build(engine)
    _green_tests(engine)
    origin_storage = OutputStorageManager(Path(tmp_path) / "origin")
    with bind_tool_result_output_storage(origin_storage, task_id="origin-build", tool_name="build"):
        failed = ToolResult.completed_failure(
            output="compile failed",
            error="compile failed",
            error_code="MAVEN_BUILD_FAILED",
        )
    engine.output_storage = FailedStorage()

    with pytest.raises(RuntimeError, match="primary and emergency"):
        engine._record_tool_execution(
            "build",
            {"action": "compile"},
            failed,
            attempted_execution=True,
        )
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state, EvidenceCloseReason.ABORTED
    )

    assert len(engine.run_evidence_state.action_attempts) == 3
    failed_attempt = engine.run_evidence_state.action_attempts[-1]
    assert failed_attempt.action == "build:compile"
    assert failed_attempt.outcome is OperationOutcome.FAILED
    assert failed_attempt.evidence_refs == []
    assert len(engine.run_evidence_state.tool_observations) == 3
    failed_observation = engine.run_evidence_state.tool_observations[-1]
    assert failed_observation.scope is StateScope.ARTIFACTS
    assert failed_observation.result.operation_outcome is OperationOutcome.FAILED
    assert failed_observation.provenance == ("tool:build:compile:output-persistence-failed")
    # Physical-oracle fold (2026-07-18): a later failed compile no longer
    # ERASES the green build that physically happened — mixed evidence in one
    # action group renders partial, and output_storage_failed keeps the cap.
    assert snapshot.build_evidence.outcome is OperationOutcome.PARTIAL
    assert snapshot.verdict == "partial"
    assert "output_storage_failed" in snapshot.conflicts


def test_error_only_failure_uses_one_source_through_construction_and_ingestion(
    tmp_path, monkeypatch
):
    class ErrorOnlyBuildTool(BaseTool):
        def __init__(self):
            super().__init__("build", "Error-only build tool")

        def execute(self, action: str) -> ToolResult:
            raise ToolError(
                "compiler emitted no stdout",
                error_code="COMPILER_EMPTY_OUTPUT",
            )

    engine, _ = _engine(tmp_path, phase="build")
    primary_calls = []
    monkeypatch.setattr(
        engine.output_storage,
        "store_output",
        lambda **kwargs: primary_calls.append(kwargs) or "",
    )
    constructed_results = []
    orchestrator = ToolOrchestrator(
        tools={"build": ErrorOnlyBuildTool()},
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: constructed_results.append(result),
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=engine.output_storage,
    )
    monkeypatch.setattr(
        orchestrator.recovery_handler,
        "recover",
        lambda *args: RecoveryDecision(should_recover=False),
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)
    step = _action_step("build", {"action": "compile"})

    engine._execute_steps([step])

    constructed = constructed_results[0]
    recorded = step.tool_result
    assert len(primary_calls) == 1
    assert recorded.output == ""
    assert recorded.error == "compiler emitted no stdout"
    assert recorded.output_ref == constructed.output_ref
    assert recorded.output_ref.startswith("output_emergency_")
    assert (
        engine.output_storage.retrieve_output(recorded.output_ref) == "compiler emitted no stdout"
    )
    assert recorded.error_code == constructed.error_code == "COMPILER_EMPTY_OUTPUT"
    assert recorded.failure_signature == constructed.failure_signature
    assert recorded.error_tail_preview == constructed.error_tail_preview
    assert recorded.error_tail_preview == "compiler emitted no stdout"
    observation = engine.run_evidence_state.tool_observations[-1]
    assert observation.result.output_ref == recorded.output_ref
    assert observation.provenance == recorded.output_ref


def test_failed_result_keeps_ws0_failure_identity_and_is_not_rehashed(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    with bind_tool_result_output_storage(engine.output_storage, task_id="build", tool_name="build"):
        failed = ToolResult.completed_failure(
            output="[ERROR] compilation failed",
            error="compile failed",
            error_code="MAVEN_BUILD_FAILED",
            failure_signature="MAVEN_BUILD_FAILED:canonical",
            error_tail_preview="[ERROR] compilation failed",
        )

    recorded = engine._record_tool_execution("build", {"action": "compile"}, failed)

    observation = engine.run_evidence_state.tool_observations[0]
    assert recorded.failure_signature == "MAVEN_BUILD_FAILED:canonical"
    assert observation.result.error_code == failed.error_code
    assert observation.result.failure_signature == failed.failure_signature
    assert observation.result.error_tail_preview == failed.error_tail_preview
    assert observation.result.output_ref == failed.output_ref


@pytest.mark.parametrize(
    ("phase", "tool_name", "params", "expected_scope"),
    [
        ("provision", "project", {"action": "provision"}, StateScope.ENVIRONMENT),
        ("analyze", "project", {"action": "analyze"}, StateScope.PROJECT_ANALYSIS),
        ("build", "build", {"action": "deps"}, StateScope.DEPENDENCIES),
        ("build", "build", {"action": "package"}, StateScope.ARTIFACTS),
        ("test", "build", {"action": "test"}, StateScope.TEST_RUNTIME),
        ("test", "maven", {"command": "test"}, StateScope.TEST_RUNTIME),
        (
            "test",
            "gradle",
            {"tasks": ["clean", ":app:test"]},
            StateScope.TEST_RUNTIME,
        ),
        ("build", "maven", {"command": "dependency:resolve"}, StateScope.DEPENDENCIES),
        ("build", "gradle", {"tasks": "dependencies"}, StateScope.DEPENDENCIES),
        ("build", "maven", {"command": "package"}, StateScope.ARTIFACTS),
        ("build", "gradle", {"tasks": "assemble"}, StateScope.ARTIFACTS),
        ("test", "bash", {"command": "pytest"}, StateScope.TEST_RUNTIME),
    ],
)
def test_action_and_phase_map_to_the_matching_scoped_epoch(
    tmp_path, phase, tool_name, params, expected_scope
):
    engine, _ = _engine(tmp_path, phase=phase)

    engine._record_tool_execution(
        tool_name,
        params,
        ToolResult.completed_success(output="observed", facts={"marker": phase}),
    )

    assert engine.run_evidence_state.tool_observations[0].scope is expected_scope
    assert engine.run_evidence_state.state_vector([expected_scope]) == {expected_scope.value: 1}


def test_snapshot_exists_before_report_phase_intro_is_built(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    report_intro_observations = []

    def report_intro():
        if engine.phase_machine.current_phase == "report":
            report_intro_observations.append(VERDICT_PATH in orchestrator.files)
        return SimpleNamespace(content="report phase intro")

    engine._phase_intro_step = report_intro

    engine._handle_phase_signals([_phase_step(engine, key_results="tests terminal")])

    assert report_intro_observations == [True]
    assert VERDICT_PATH in orchestrator.files
    assert engine.run_evidence_state.sealed is True
    assert engine.run_evidence_state.close_reason == EvidenceCloseReason.TEST_TERMINATED.value


def test_floor_driven_test_completion_finalizes_before_report_intro(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine.config = SimpleNamespace(
        phase_min_floors={"report": 8},
        max_iterations=10,
    )
    engine.current_iteration = 8
    engine._phase_gate_check = lambda phase: {"ok": True, "reason": "", "suggestions": []}
    report_intro_observations = []

    def report_intro():
        report_intro_observations.append(VERDICT_PATH in orchestrator.files)
        return SimpleNamespace(content="report phase intro")

    engine._phase_intro_step = report_intro

    assert engine._enforce_phase_floors() is True
    assert engine.phase_machine.current_phase == "report"
    assert report_intro_observations == [True]
    assert engine.run_evidence_state.close_reason == EvidenceCloseReason.TEST_TERMINATED.value


def test_terminal_test_signal_and_report_in_one_response_refuses_early_render(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    _prepare_action_execution(engine)
    report_calls = []

    def execute(call):
        if call.name == "phase":
            result = ToolResult.completed_success(
                output="test phase complete",
                metadata=_phase_metadata("test", key_results="tests terminal"),
            )
            return ToolExecution(
                call=call,
                result=result,
                status="success",
                raw_params=call.raw_params,
                validated_params=call.raw_params,
                observation_text=result.output,
                attempted_execution=True,
            )
        report_calls.append(call)
        result = ToolResult.completed_success(output="report rendered")
        return ToolExecution(
            call=call,
            result=result,
            status="success",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text=result.output,
            attempted_execution=True,
        )

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=execute)
    phase_step = _action_step("phase", {"action": "done", "outcome": "success"})
    early_report = _action_step("report", {"action": "generate"})

    engine._execute_steps([phase_step, early_report])

    assert report_calls == []
    assert engine._report_delivered is False
    engine._handle_phase_signals([phase_step, early_report])
    assert engine.run_evidence_state.sealed is True
    assert orchestrator.files[VERDICT_PATH]

    later_report = _action_step("report", {"action": "generate"})
    engine._execute_steps([later_report])

    assert len(report_calls) == 1
    assert engine._report_delivered is True


def test_terminal_phase_signal_stops_later_actions_until_policy_routes(tmp_path):
    engine, _ = _engine(tmp_path, phase="test")
    _prepare_action_execution(engine)
    calls = []

    def execute(call):
        calls.append(call.name)
        result = ToolResult.completed_success(
            output="test phase complete" if call.name == "phase" else "stale action ran",
            metadata=(
                _phase_metadata("test", key_results="tests terminal")
                if call.name == "phase"
                else {}
            ),
        )
        return ToolExecution(
            call=call,
            result=result,
            status="success",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text=result.output,
            attempted_execution=True,
        )

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=execute)
    phase_step = _action_step("phase", {"action": "done", "outcome": "success"})
    stale_action = _action_step("build", {"action": "test"})

    engine._execute_steps([phase_step, stale_action])

    assert calls == ["phase"]
    assert stale_action.tool_result is None


def test_sealed_run_refuses_evidence_tool_before_execution(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )
    snapshot_before = orchestrator.files[VERDICT_PATH]
    observations_before = tuple(engine.run_evidence_state.tool_observations)
    calls = []

    def execute(call):
        calls.append(call)
        result = ToolResult.completed_failure(
            output="post-close pytest failed",
            error="one test failed",
            error_code="PYTEST_FAILED",
        )
        return ToolExecution(
            call=call,
            result=result,
            status="failure",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text=result.output,
            attempted_execution=True,
        )

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=execute)
    _prepare_action_execution(engine)
    step = _action_step("build", {"action": "test"})

    engine._execute_steps([step])

    assert calls == []
    assert step.tool_result.operation_outcome is OperationOutcome.SKIPPED
    assert step.tool_result.metadata["execution_refused"] == "evidence_closed"
    assert tuple(engine.run_evidence_state.tool_observations) == observations_before
    assert orchestrator.files[VERDICT_PATH] == snapshot_before


def test_abort_mid_build_seals_available_evidence_once(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="build")
    _green_build(engine)

    first = engine.abort(reason="global_time_cap")
    commands_after_first = list(orchestrator.commands)
    second = engine.abort(reason="global_time_cap")

    assert first.termination is RunTerminationStatus.ABORTED
    assert first.snapshot_ref.endswith("verdict.json")
    assert first.model_dump_json() == second.model_dump_json()
    assert orchestrator.commands[len(commands_after_first) :] == [
        f"test -f {VERDICT_PATH} && cat {VERDICT_PATH}"
    ]
    assert read_verdict_snapshot(orchestrator).verdict in {"unknown", "partial", "failed"}
    assert len(engine.run_evidence_state.phase_records) == 3


def test_conflicting_close_termination_is_rejected_after_seal(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="build")
    _green_build(engine)
    first = engine.abort(reason="operator aborted")
    snapshot_bytes = orchestrator.files[VERDICT_PATH]

    same_reason = engine._close_flow(RunTerminationStatus.ABORTED)
    with pytest.raises(ValueError, match="conflicting evidence-close reason"):
        engine.cancel(reason="operator then requested cancellation")

    assert first.termination is RunTerminationStatus.ABORTED
    assert same_reason.model_dump_json() == first.model_dump_json()
    assert engine.run_evidence_state.close_reason == EvidenceCloseReason.ABORTED.value
    assert orchestrator.files[VERDICT_PATH] == snapshot_bytes


def test_flow_close_retries_persistence_for_an_already_sealed_state(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    orchestrator = _FailFirstAtomicWriteOrchestrator()
    engine.verdict_finalizer = VerdictFinalizer(orchestrator)
    _green_build(engine)

    with pytest.raises(OSError, match="temporary file"):
        engine._finalize_evidence(EvidenceCloseReason.ABORTED)

    assert engine.run_evidence_state.sealed is True
    assert VERDICT_PATH not in orchestrator.files

    termination = engine._close_flow(RunTerminationStatus.ABORTED)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert VERDICT_PATH in orchestrator.files


def test_explicit_cancellation_seals_snapshot_and_returns_cancelled(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="analyze")

    termination = engine.cancel(reason="operator requested cancellation")

    assert termination.termination is RunTerminationStatus.CANCELLED
    assert termination.report_delivery_status is ReportDeliveryStatus.SKIPPED
    assert read_verdict_snapshot(orchestrator).verdict == "unknown"
    assert engine.phase_machine.records[-1].reason == "operator requested cancellation"


def test_report_failure_changes_delivery_only_and_cannot_mutate_sealed_evidence(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine._handle_phase_signals([_phase_step(engine, key_results="tests green")])
    before = read_verdict_snapshot(orchestrator)
    observations_before_report = len(engine.run_evidence_state.tool_observations)

    report_failure = ToolResult.completed_failure(
        output="report renderer crashed",
        error="report renderer crashed",
        error_code="REPORT_RENDER_FAILED",
    )
    recorded = engine._record_tool_execution("report", {"action": "generate"}, report_failure)
    termination = engine._close_flow(RunTerminationStatus.COMPLETED)
    after = read_verdict_snapshot(orchestrator)

    assert recorded is report_failure
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert termination.report_delivery_status is ReportDeliveryStatus.FAILED
    assert before.model_dump_json() == after.model_dump_json()
    assert len(engine.run_evidence_state.tool_observations) == observations_before_report


def test_report_phase_abort_keeps_the_existing_evidence_close_snapshot(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine._handle_phase_signals([_phase_step(engine, key_results="tests green")])
    snapshot_before = orchestrator.files[VERDICT_PATH]
    engine._report_attempted = True
    engine._report_failed = True

    termination = engine.abort(reason="report renderer crashed")

    assert termination.termination is RunTerminationStatus.ABORTED
    assert termination.report_delivery_status is ReportDeliveryStatus.FAILED
    assert engine.run_evidence_state.close_reason == EvidenceCloseReason.TEST_TERMINATED.value
    assert orchestrator.files[VERDICT_PATH] == snapshot_before


class _PersistenceFailingReportTool(BaseTool):
    def __init__(self):
        self.renderer_calls = 0
        super().__init__("report", "render the final report")

    def execute(self, action: str) -> ToolResult:
        self.renderer_calls += 1
        return ToolResult.completed_failure(
            output="report renderer failed",
            error="report renderer failed",
            error_code="REPORT_RENDER_FAILED",
        )


class _ReportCompletionTool(BaseTool):
    def __init__(self):
        self.calls = 0
        super().__init__("phase", "complete the report phase")

    def execute(self, action: str) -> ToolResult:
        self.calls += 1
        return ToolResult.completed_success(
            output="report phase closed",
            metadata=_phase_metadata(
                "report",
                key_results="report delivery failed",
            ),
        )


def test_report_construction_persistence_failure_completes_without_mutating_verdict(
    tmp_path, monkeypatch
):
    engine, verdict_orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine._handle_phase_signals([_phase_step(engine, key_results="tests green")])
    snapshot_before = read_verdict_snapshot(verdict_orchestrator)
    snapshot_bytes_before = verdict_orchestrator.files[VERDICT_PATH]
    evidence_state_before = engine.run_evidence_state.model_dump_json()
    observations_before = tuple(engine.run_evidence_state.tool_observations)

    persistence_attempts = {"primary": 0, "emergency": 0}

    def fail_primary(**kwargs):
        persistence_attempts["primary"] += 1
        return ""

    def fail_emergency(**kwargs):
        persistence_attempts["emergency"] += 1
        return ""

    monkeypatch.setattr(engine.output_storage, "store_output", fail_primary)
    monkeypatch.setattr(engine.output_storage, "store_emergency_output", fail_emergency)

    report_tool = _PersistenceFailingReportTool()
    phase_tool = _ReportCompletionTool()
    tool_orchestrator = ToolOrchestrator(
        tools={"report": report_tool, "phase": phase_tool},
        context_manager=engine.context_manager,
        recent_tool_executions=engine.recent_tool_executions,
        successful_states={},
        repository_url="https://example.test/repo.git",
        track_tool_execution=lambda *args, **kwargs: None,
        update_successful_states=lambda *args, **kwargs: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "2026-07-17T00:00:00Z",
        output_storage=engine.output_storage,
    )
    report_step = ReActStep(
        step_type=StepType.ACTION,
        content="render report",
        tool_name="report",
        tool_params={"action": "generate"},
        timestamp="2026-07-17T00:00:00Z",
        model_used="test-model",
    )
    close_step = ReActStep(
        step_type=StepType.ACTION,
        content="close report phase",
        tool_name="phase",
        tool_params={"action": "done"},
        timestamp="2026-07-17T00:00:01Z",
        model_used="test-model",
    )
    engine.max_iterations = 1
    engine.config = SimpleNamespace(max_wall_clock_seconds=0, verbose=False)
    engine.prompt_builder = _PromptBuilder()
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _LLMClient(response="render and close report")
    engine.state_evaluator = SimpleNamespace(completion_mode="previous")
    engine.token_tracker = SimpleNamespace(
        set_iteration=lambda iteration: None,
        update_last_tool_name=lambda tool_name: None,
    )
    engine.response_parser = SimpleNamespace(
        parse=lambda response, **kwargs: [report_step, close_step]
    )
    engine._get_tool_orchestrator = lambda: tool_orchestrator
    engine._phase_intro_step = lambda: SimpleNamespace(content="report phase")
    engine._enforce_phase_floors = lambda: False
    engine._should_use_thinking_model = lambda: False
    engine._export_token_usage_csv = lambda: None
    engine._add_observation_step = lambda observation: None
    engine.emit = lambda *args, **kwargs: None

    termination = engine.run_setup_loop("deliver report", max_iterations=1)

    assert report_tool.renderer_calls == 1
    assert phase_tool.calls == 1
    assert persistence_attempts == {"primary": 1, "emergency": 1}
    assert engine._report_attempted is True
    assert engine._report_failed is True
    assert engine._report_delivered is False
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert termination.report_delivery_status is ReportDeliveryStatus.FAILED
    assert termination.snapshot_ref == VERDICT_PATH
    assert engine.phase_machine.is_complete is True
    assert engine.run_evidence_state.model_dump_json() == evidence_state_before
    assert tuple(engine.run_evidence_state.tool_observations) == observations_before
    assert verdict_orchestrator.files[VERDICT_PATH] == snapshot_bytes_before
    snapshot_after = read_verdict_snapshot(verdict_orchestrator)
    assert snapshot_after.model_dump_json() == snapshot_before.model_dump_json()
    assert report_step.tool_result is not None
    assert report_step.tool_result.operation_outcome is OperationOutcome.SKIPPED
    assert report_step.tool_result.metadata["report_delivery_failure"] == "output_persistence"


def test_successful_report_marks_delivery_without_changing_verdict(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine._handle_phase_signals([_phase_step(engine, key_results="tests green")])
    before = read_verdict_snapshot(orchestrator)

    engine._record_tool_execution(
        "report",
        {"action": "generate"},
        ToolResult.completed_success(output="report written"),
    )
    termination = engine._close_flow(RunTerminationStatus.COMPLETED)

    assert termination.report_delivery_status is ReportDeliveryStatus.DELIVERED
    assert read_verdict_snapshot(orchestrator).model_dump_json() == before.model_dump_json()


def test_normal_report_phase_flow_close_returns_completed_termination(tmp_path):
    engine, orchestrator = _engine(tmp_path, phase="test")
    _green_build(engine)
    _green_tests(engine)
    engine._handle_phase_signals([_phase_step(engine, key_results="tests green")])
    engine._record_tool_execution(
        "report",
        {"action": "generate"},
        ToolResult.completed_success(output="report written"),
    )

    engine._handle_phase_signals([_phase_step(engine, key_results="report delivered")])
    termination = engine._close_flow(RunTerminationStatus.COMPLETED)

    assert engine.phase_machine.is_complete is True
    assert termination.termination is RunTerminationStatus.COMPLETED
    assert termination.report_delivery_status is ReportDeliveryStatus.DELIVERED
    assert read_verdict_snapshot(orchestrator).verdict == "success"


class _PromptBuilder:
    def invalidate_trunk_cache(self):
        pass

    def build_initial_system_prompt(self, **kwargs):
        return "system prompt"

    def build_mode_prompt(self, prompt, mode, **kwargs):
        return prompt


class _LLMClient:
    def __init__(self, response="unparseable response", error=None):
        self.response = response
        self.error = error

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=False, model="test-model")

    def get_response(self, prompt, mode):
        if self.error is not None:
            raise self.error
        return self.response


def _loop_engine(tmp_path, *, response="unparseable response", error=None, wall_clock_cap=0):
    engine, orchestrator = _engine(tmp_path)
    engine.max_iterations = 3
    engine.config = SimpleNamespace(max_wall_clock_seconds=wall_clock_cap)
    engine.prompt_builder = _PromptBuilder()
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _LLMClient(response=response, error=error)
    engine.state_evaluator = SimpleNamespace(completion_mode="previous")
    engine.token_tracker = SimpleNamespace(set_iteration=lambda iteration: None)
    engine.response_parser = SimpleNamespace(parse=lambda response, **kwargs: [])
    engine._phase_intro_step = lambda: SimpleNamespace(content="phase intro")
    engine._enforce_phase_floors = lambda: False
    engine._should_use_thinking_model = lambda: True
    engine._export_token_usage_csv = lambda: None
    return engine, orchestrator


@pytest.mark.parametrize(
    ("response", "error", "max_iterations", "expected_reason"),
    [
        ("", None, 3, "LLM response unavailable"),
        ("unparseable response", None, 2, "iteration budget exhausted"),
        ("unused", RuntimeError("transport failed"), 3, "engine exception: RuntimeError"),
    ],
)
def test_setup_loop_abort_paths_persist_typed_termination(
    tmp_path, response, error, max_iterations, expected_reason
):
    engine, orchestrator = _loop_engine(tmp_path, response=response, error=error)

    termination = engine.run_setup_loop("set up project", max_iterations=max_iterations)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert termination.report_delivery_status is ReportDeliveryStatus.SKIPPED
    assert engine.phase_machine.records[-1].reason == expected_reason
    assert VERDICT_PATH in orchestrator.files


def test_setup_wall_clock_closure_is_aborted_and_persisted(tmp_path, monkeypatch):
    engine, orchestrator = _loop_engine(tmp_path, wall_clock_cap=1)
    clock = iter([100.0, 102.0, 102.0])
    monkeypatch.setattr(react_engine_module.time, "time", lambda: next(clock))

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.phase_machine.records[-1].reason == "wall clock cap exceeded"
    assert VERDICT_PATH in orchestrator.files


def test_setup_keyboard_interrupt_closure_is_cancelled(tmp_path):
    engine, orchestrator = _loop_engine(tmp_path, error=KeyboardInterrupt())

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.CANCELLED
    assert engine.phase_machine.records[-1].reason == "keyboard interrupt"
    assert VERDICT_PATH in orchestrator.files


def test_setup_no_progress_stop_aborts_and_seals(tmp_path):
    engine, orchestrator = _loop_engine(tmp_path, response="action")
    completed_task = SimpleNamespace(
        step_type=StepType.ACTION,
        tool_name="manage_context",
        tool_params={"action": "complete_with_results"},
        tool_result=ToolResult.completed_success(output="task complete"),
    )
    engine.response_parser = SimpleNamespace(parse=lambda response, **kwargs: [completed_task])
    engine._execute_steps = lambda steps: None
    engine._handle_phase_signals = lambda steps: None
    engine._maybe_nudge_phase_done = lambda: False
    engine._check_progress_after_task = lambda: True
    engine.state_evaluator = SimpleNamespace(
        completion_mode="previous",
        evaluate=lambda **kwargs: SimpleNamespace(
            needs_guidance=False,
            is_task_complete=False,
        ),
    )

    termination = engine.run_setup_loop("set up project", max_iterations=3)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.phase_machine.records[-1].reason == "no physical progress"
    assert VERDICT_PATH in orchestrator.files


def test_setup_evaluator_success_cannot_bypass_phase_lifecycle(tmp_path):
    engine, orchestrator = _loop_engine(tmp_path, response="thought")
    thought = SimpleNamespace(step_type=StepType.THOUGHT)
    engine.response_parser = SimpleNamespace(parse=lambda response, **kwargs: [thought])
    engine._execute_steps = lambda steps: None
    engine._handle_phase_signals = lambda steps: None
    engine._maybe_nudge_phase_done = lambda: False
    engine.state_evaluator = SimpleNamespace(
        completion_mode="previous",
        evaluate=lambda **kwargs: SimpleNamespace(
            needs_guidance=False,
            is_task_complete=True,
        ),
    )

    termination = engine.run_setup_loop("set up project", max_iterations=2)

    assert termination.termination is RunTerminationStatus.ABORTED
    assert engine.phase_machine.records[-1].reason == "iteration budget exhausted"
    assert VERDICT_PATH in orchestrator.files


def test_legacy_run_task_keeps_boolean_contract_without_evidence_finalization(tmp_path):
    engine, orchestrator = _loop_engine(tmp_path, response="")
    stale_snapshot = '{"run_id":"old-setup","verdict":"success"}'
    orchestrator.files[VERDICT_PATH] = stale_snapshot

    succeeded = engine.run_react_loop(
        "perform one task",
        max_iterations=3,
        completion_mode="run_task",
    )

    assert succeeded is False
    assert engine.phase_machine.records == ()
    assert orchestrator.files[VERDICT_PATH] == stale_snapshot


def test_setup_agent_injects_one_session_owned_state_and_finalizer(monkeypatch):
    captured = {}

    class FakeContextManager:
        def __init__(self, workspace_path, orchestrator):
            self.workspace_path = workspace_path
            self.orchestrator = orchestrator

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(agent_module, "ContextManager", FakeContextManager)
    monkeypatch.setattr(agent_module, "ReActEngine", FakeEngine)
    monkeypatch.setattr(
        "sag.agent.error_logger.ErrorLogger.get_instance",
        lambda **kwargs: SimpleNamespace(),
    )

    agent = object.__new__(SetupAgent)
    agent.config = SimpleNamespace(workspace_path="/workspace", ui_mode=False)
    agent.orchestrator = FakeVerdictOrchestrator()
    agent.context_manager = None
    agent.tools = None
    agent.react_engine = None
    agent.phase_machine = PhaseMachine()
    agent.context_journal = SimpleNamespace()
    agent.run_evidence_state = RunEvidenceState(run_id="session-owned")
    agent.verdict_finalizer = VerdictFinalizer(agent.orchestrator)
    agent.ui_manager = None
    agent.agent_logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    agent._initialize_tools = lambda workflow_mode: []

    agent._initialize_context_and_tools(workflow_mode="setup")

    assert captured["run_evidence_state"] is agent.run_evidence_state
    assert captured["verdict_finalizer"] is agent.verdict_finalizer


def test_pre_engine_exception_seals_without_fabricating_phase_evidence():
    orchestrator = FakeVerdictOrchestrator()
    agent = object.__new__(SetupAgent)
    agent.run_evidence_state = RunEvidenceState(run_id="pre-engine")
    agent.verdict_finalizer = VerdictFinalizer(orchestrator)
    agent.phase_machine = PhaseMachine()
    agent.react_engine = None
    agent.run_termination = None

    agent._close_open_setup_run("setup exception: RuntimeError")

    assert agent.run_evidence_state.sealed is True
    assert agent.run_evidence_state.phase_records == ()
    assert agent.phase_machine.records == ()
    assert agent.run_termination.termination is RunTerminationStatus.ABORTED
    assert read_verdict_snapshot(orchestrator).run_id == "pre-engine"


def test_pre_engine_closure_failure_is_surfaced_to_caller():
    class FailingFinalizer:
        def finalize(self, state, reason):
            raise OSError("verdict persistence failed")

    agent = object.__new__(SetupAgent)
    agent.run_evidence_state = RunEvidenceState(run_id="pre-engine-failure")
    agent.verdict_finalizer = FailingFinalizer()
    agent.phase_machine = PhaseMachine()
    agent.react_engine = None
    agent.run_termination = None

    with pytest.raises(OSError, match="verdict persistence failed"):
        agent._close_open_setup_run("setup exception: RuntimeError")

    assert agent.run_termination is None
    assert agent.phase_machine.records == ()
