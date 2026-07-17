from types import SimpleNamespace

import pytest
from rich.console import Console
from test_build_tool_preflight_integration import (
    ENFORCER_FAIL,
    ScriptedBackendTool,
    ScriptedOrch,
    _patch_provision,
)
from test_evidence_ingestion import _action_step, _engine, _prepare_action_execution
from test_snapshot_surface_agreement import VERDICT_PATH, SnapshotOrchestrator

import sag.main as main_module
from sag.agent.evidence_state import StateScope
from sag.agent.tool_orchestration import (
    ToolCall,
    ToolExecutionRecord,
    ToolOrchestrator,
)
from sag.agent.verdict_finalizer import (
    EvidenceCloseReason,
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
)
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome, TestStats
from sag.tools.base import BaseTool, OutputPersistenceError, ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.report_tool import ReportTool
from sag.ui.ui_manager import UIManager


def _orchestrator(engine, tools, *, recent_tool_executions=None, successful_states=None):
    return ToolOrchestrator(
        tools=tools,
        context_manager=engine.context_manager,
        recent_tool_executions=list(recent_tool_executions or []),
        successful_states=dict(successful_states or {}),
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=engine.output_storage,
    )


def _role_values(observation):
    return {role.value for role in getattr(observation, "roles", ())}


def _advance_build_to_test(engine):
    engine.phase_machine.mark_done("build attempt complete", [])
    assert engine.phase_machine.current_phase == "test"


def test_build_phase_diagnostic_cannot_replace_incomplete_build_or_green_cli(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    engine._record_tool_execution(
        "build",
        {"action": "compile"},
        ToolResult.completed(
            output="only part of the project compiled",
            operation_outcome=OperationOutcome.PARTIAL,
            facts={"build_success": False, "build_complete": False},
        ),
    )
    engine._record_tool_execution(
        "file_io",
        {"action": "read", "path": "/workspace/pom.xml"},
        ToolResult.completed_success(output="pom.xml read successfully"),
    )
    _advance_build_to_test(engine)
    engine._record_tool_execution(
        "bash",
        {"command": "pytest"},
        ToolResult.completed_success(
            output="5 passed",
            test_stats=TestStats(
                discovered=5,
                executed=5,
                passed=5,
                failed=0,
                skipped=0,
            ),
        ),
    )

    observations = engine.run_evidence_state.tool_observations
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )
    termination = RunTermination(
        termination=RunTerminationStatus.COMPLETED,
        report_delivery_status=ReportDeliveryStatus.DELIVERED,
    )
    _, exit_code = main_module._render_setup_cli_result(snapshot, termination, "demo")

    assert observations[1].scope is StateScope.ARTIFACTS
    assert observations[1].tool_name == "file_io"
    assert _role_values(observations[1]) == set()
    assert _role_values(observations[2]) == {"test"}
    assert snapshot.build_evidence.outcome is OperationOutcome.PARTIAL
    assert snapshot.verdict != "success"
    assert exit_code == 1


@pytest.mark.parametrize(
    ("tool_name", "params"),
    [
        pytest.param("maven", {"command": "package"}, id="maven-package"),
        pytest.param(
            "gradle",
            {"tasks": "publishToMavenLocal"},
            id="gradle-install",
        ),
    ],
)
def test_package_or_install_stats_are_one_build_and_test_observation(tmp_path, tool_name, params):
    engine, _ = _engine(tmp_path, phase="build")

    engine._record_tool_execution(
        tool_name,
        params,
        ToolResult.completed_success(
            output="package completed with embedded tests",
            test_stats=TestStats(
                discovered=10,
                executed=10,
                passed=10,
                failed=0,
                skipped=0,
            ),
        ),
    )
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )

    assert len(engine.run_evidence_state.tool_observations) == 1
    observation = engine.run_evidence_state.tool_observations[0]
    assert _role_values(observation) == {"build", "test"}
    assert snapshot.build_evidence.green is True
    assert snapshot.test_stats.executed == 10
    assert snapshot.test_stats.passed == 10


def test_facade_jdk_retry_preserves_two_maven_actual_executions(tmp_path, monkeypatch):
    _patch_provision(monkeypatch)
    backend = ScriptedBackendTool(
        ToolResult.completed_failure(
            output=ENFORCER_FAIL,
            error="Java version mismatch",
            error_code="JAVA_VERSION_MISMATCH",
        ),
        ToolResult.completed_success(output="BUILD SUCCESS"),
    )
    docker = ScriptedOrch(java="11", manifest={})
    build = BuildTool(docker, maven_tool=backend)
    engine, _ = _engine(tmp_path, phase="build")
    orchestrator = _orchestrator(engine, {"build": build})
    call = ToolCall(
        name="build",
        raw_params={"action": "compile", "working_directory": "/workspace/proj"},
    )

    execution = orchestrator.execute(call)

    expected_params = {
        "command": "compile",
        "working_directory": "/workspace/proj",
        "_env_preflight": False,
        "fail_at_end": True,
    }
    assert execution.result.succeeded is True
    assert len(execution.actual_executions) == 2
    assert [getattr(actual, "tool_name", None) for actual in execution.actual_executions] == [
        "maven",
        "maven",
    ]
    assert [actual.params for actual in execution.actual_executions] == [
        expected_params,
        expected_params,
    ]
    assert [actual.result.operation_outcome for actual in execution.actual_executions] == [
        OperationOutcome.FAILED,
        OperationOutcome.SUCCESS,
    ]

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=lambda ignored: execution)
    _prepare_action_execution(engine)
    engine._execute_steps(
        [_action_step("build", {"action": "compile", "working_directory": "/workspace/proj"})]
    )

    observations = engine.run_evidence_state.tool_observations
    assert len(observations) == 2
    assert [observation.tool_name for observation in observations] == ["maven", "maven"]
    assert [observation.scope for observation in observations] == [
        StateScope.ARTIFACTS,
        StateScope.ARTIFACTS,
    ]
    assert [_role_values(observation) for observation in observations] == [
        {"build"},
        {"build"},
    ]
    assert [observation.result.operation_outcome for observation in observations] == [
        OperationOutcome.FAILED,
        OperationOutcome.SUCCESS,
    ]
    assert all(
        observation.provenance == observation.result.output_ref for observation in observations
    )


class _NeverExecutedBash(BaseTool):
    def __init__(self):
        super().__init__("bash", "must be replaced by Java auto recovery")
        self.calls = 0

    def execute(self, command: str, working_directory: str = "/workspace") -> ToolResult:
        self.calls += 1
        return ToolResult.completed_success(output="unexpected bash execution")


class _JavaSystemTool(BaseTool):
    def __init__(self):
        super().__init__("system", "records Java verification and installation")
        self.calls = []

    def execute(self, action: str, java_version: str = "") -> ToolResult:
        params = {"action": action}
        if java_version:
            params["java_version"] = java_version
        self.calls.append(params)
        return ToolResult.completed_success(output=f"system {action} complete")


def test_java_auto_fix_records_verify_and_install_as_system_executions(tmp_path):
    engine, _ = _engine(tmp_path, phase="build")
    engine.context_manager.get_current_context = lambda: "Project requires Java 17"
    bash = _NeverExecutedBash()
    system = _JavaSystemTool()
    command = "update-alternatives --config java"
    signature = (
        f"bash:{str(sorted({'command': command, 'working_directory': '/workspace'}.items()))}"
    )
    recent = [
        ToolExecutionRecord(
            signature=signature,
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.FAILED,
            timestamp=f"ts-{index}",
        )
        for index in range(5)
    ]
    orchestrator = _orchestrator(
        engine,
        {"bash": bash, "system": system},
        recent_tool_executions=recent,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)

    engine._execute_steps([_action_step("bash", {"command": command})])

    assert bash.calls == 0
    assert system.calls == [
        {"action": "verify_java"},
        {"action": "install_java", "java_version": "17"},
    ]
    observations = engine.run_evidence_state.tool_observations
    assert len(observations) == 2
    assert [observation.tool_name for observation in observations] == ["system", "system"]
    assert [observation.scope for observation in observations] == [
        StateScope.ENVIRONMENT,
        StateScope.ENVIRONMENT,
    ]
    assert [_role_values(observation) for observation in observations] == [set(), set()]
    assert [attempt.action for attempt in engine.run_evidence_state.action_attempts] == [
        "system:verify_java",
        "system:install_java",
    ]
    assert all(
        observation.provenance == observation.result.output_ref for observation in observations
    )


class _TotalFailureStorage:
    def __init__(self):
        self.primary_calls = 0
        self.emergency_calls = 0

    def store_output(self, **kwargs):
        self.primary_calls += 1
        return ""

    def store_emergency_output(self, **kwargs):
        self.emergency_calls += 1
        return ""

    def retrieve_output(self, ref_id):
        return None


class _ConstructionFailureTool(BaseTool):
    def __init__(self, tool_name, test_stats):
        super().__init__(tool_name, "fails after executing but before durable result construction")
        self.test_stats = test_stats
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": [],
        }

    def execute(self, **params) -> ToolResult:
        return ToolResult.completed_failure(
            output=("unbounded compiler output\n" * 500) + "FINAL FAILURE TAIL",
            error="actual execution failed",
            error_code="ACTUAL_EXECUTION_FAILED",
            test_stats=self.test_stats,
            facts={"build_success": False, "actual_execution": True},
            refs=["junit.xml"] if self.test_stats else ["compiler.log"],
            conflicts=["backend_reported_failure"],
            raw_data=(
                {"tests": 5, "failed_tests": 2, "error_tests": 0} if self.test_stats else None
            ),
            metadata={"source": "real execution", "attempt": 1},
        )


@pytest.mark.parametrize(
    ("tool_name", "params", "test_stats", "expected_roles"),
    [
        pytest.param("build", {"action": "compile"}, None, {"build"}, id="failed-build"),
        pytest.param(
            "maven",
            {"command": "test"},
            TestStats(discovered=5, executed=5, passed=3, failed=2, skipped=0),
            {"test"},
            id="failed-test",
        ),
    ],
)
def test_construction_persistence_failure_ingests_bounded_draft_once(
    tmp_path, tool_name, params, test_stats, expected_roles
):
    storage = _TotalFailureStorage()
    engine, _ = _engine(tmp_path, phase="build" if test_stats is None else "test")
    engine.output_storage = storage
    orchestrator = _orchestrator(
        engine,
        {tool_name: _ConstructionFailureTool(tool_name, test_stats)},
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)

    with pytest.raises(OutputPersistenceError) as raised:
        engine._execute_steps([_action_step(tool_name, params)])

    draft = getattr(raised.value, "draft", None)
    assert draft is not None
    assert storage.primary_calls == 1
    assert storage.emergency_calls == 1
    assert not hasattr(draft, "output")
    assert draft.output_ref is None
    assert draft.invocation_status is InvocationStatus.COMPLETED
    assert draft.operation_outcome is OperationOutcome.FAILED
    assert draft.evidence_status is EvidenceStatus.VERIFIED
    assert draft.failure_signature.startswith("ACTUAL_EXECUTION_FAILED:")
    assert draft.error_tail_preview.endswith("FINAL FAILURE TAIL")
    assert len(draft.error_tail_preview) <= 400

    observations = engine.run_evidence_state.tool_observations
    assert len(observations) == 1
    observation = observations[0]
    assert observation.tool_name == tool_name
    assert observation.result.output_ref is None
    assert observation.result.failure_signature == draft.failure_signature
    assert observation.result.operation_outcome is OperationOutcome.FAILED
    assert _role_values(observation) == expected_roles
    assert observation.provenance.endswith("output-persistence-failed")
    assert engine.run_evidence_state.conflicts == (
        "backend_reported_failure",
        "output_storage_failed",
    )

    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.ABORTED,
    )
    if test_stats is None:
        assert snapshot.build_evidence.observed is True
        assert snapshot.build_evidence.outcome is OperationOutcome.FAILED
    else:
        assert snapshot.test_stats.executed == 5
        assert snapshot.test_stats.passed == 3
        assert snapshot.test_stats.failed == 2


@pytest.mark.parametrize(
    (
        "build_outcome",
        "build_green",
        "passed",
        "failed",
        "expected_judgment",
        "expected_overall",
    ),
    [
        pytest.param(
            OperationOutcome.SUCCESS,
            True,
            8,
            2,
            "success",
            "success",
            id="threshold-success",
        ),
        pytest.param(
            OperationOutcome.PARTIAL,
            True,
            10,
            0,
            "success",
            "partial",
            id="partial-build-green-tests",
        ),
        pytest.param(
            OperationOutcome.SUCCESS,
            True,
            7,
            3,
            "failed",
            "failed",
            id="under-threshold",
        ),
    ],
)
def test_sealed_test_judgment_owns_report_and_terminal_ui(
    tmp_path,
    build_outcome,
    build_green,
    passed,
    failed,
    expected_judgment,
    expected_overall,
):
    engine, _ = _engine(tmp_path, phase="build")
    engine._record_tool_execution(
        "build",
        {"action": "compile"},
        ToolResult.completed(
            output="build evidence",
            operation_outcome=build_outcome,
            facts={"build_success": build_green},
        ),
    )
    _advance_build_to_test(engine)
    engine._record_tool_execution(
        "bash",
        {"command": "pytest"},
        ToolResult.completed_success(
            output=f"{passed} passed, {failed} failed",
            test_stats=TestStats(
                discovered=10,
                executed=10,
                passed=passed,
                failed=failed,
                skipped=0,
            ),
        ),
    )
    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.TEST_TERMINATED,
    )
    orchestrator = SnapshotOrchestrator({VERDICT_PATH: snapshot.model_dump_json()})
    console = Console(record=True, width=100)
    manager = UIManager(project_name="demo", console=console)
    report = ReportTool(orchestrator, workflow_mode="setup")
    report.set_ui_manager(manager)

    result = report.execute(summary="Demo setup", status=expected_overall)

    assert result.succeeded is True
    assert manager.report_data["test_success"] is (expected_judgment == "success")
    assert snapshot.test_stats.model_dump().get("judgment") == expected_judgment
    assert snapshot.verdict == expected_overall
    report_status = result.metadata["report_snapshot"]["status"]
    assert report_status["test_judgment"] == expected_judgment
    assert report_status["tests_ok"] is (expected_judgment == "success")
