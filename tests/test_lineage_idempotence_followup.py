import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_evidence_ingestion import _action_step, _engine, _prepare_action_execution
from test_verdict_finalizer import FakeVerdictOrchestrator

from sag.agent.evidence_state import (
    EvidenceRole,
    RunEvidenceState,
    StateScope,
    ToolObservation,
)
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
from sag.evidence import (
    EvidenceAssessment,
    EvidenceFinding,
    EvidenceStatus,
    InvocationStatus,
    OperationOutcome,
    TestStats,
)
from sag.tools.base import (
    ActualToolExecution,
    BaseTool,
    OutputPersistenceError,
    ToolResult,
    UnpersistedToolResult,
    canonical_full_output_source,
)

PYTHON_312 = shutil.which("python3.12")
DRAFT_CAP_BYTES = 32 * 1024


class _ResultTool(BaseTool):
    def __init__(self, name, results):
        super().__init__(name, "scripted result tool")
        self.results = list(results)
        self.calls = []
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "command": {"type": "string"},
                "tasks": {"type": "string"},
                "working_directory": {"type": "string"},
            },
            "required": [],
        }

    def execute(self, **params):
        self.calls.append(dict(params))
        result = self.results.pop(0)
        return result() if callable(result) else result


class _BuildFacadeFailure(BaseTool):
    def __init__(self, result):
        super().__init__("build", "scripted build facade")
        self.result = result
        self._parameter_schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "working_directory": {"type": "string"},
            },
            "required": ["action"],
        }

    def execute(self, **params):
        return self.result


class _SelectivePersistenceStorage:
    """Persist the original result but fail both homes for the replacement."""

    def __init__(self):
        self.outputs = {}
        self.primary_failures = 0
        self.emergency_failures = 0

    def seed(self, result):
        self.outputs[result.output_ref] = canonical_full_output_source(
            raw_output=result.raw_output,
            output=result.output,
            error=result.error,
        )

    def store_output(self, *, output, **kwargs):
        if "replacement persistence failed" in output:
            self.primary_failures += 1
            return ""
        ref = f"output_followup_{len(self.outputs) + 1}"
        self.outputs[ref] = output
        return ref

    def store_emergency_output(self, *, output, **kwargs):
        if "replacement persistence failed" in output:
            self.emergency_failures += 1
            return ""
        ref = f"output_followup_emergency_{len(self.outputs) + 1}"
        self.outputs[ref] = output
        return ref

    def retrieve_output(self, ref_id):
        return self.outputs.get(ref_id)


def _orchestrator(engine, tools, *, successful_states=None, output_storage=None):
    return ToolOrchestrator(
        tools=tools,
        context_manager=engine.context_manager,
        recent_tool_executions=[],
        successful_states=dict(successful_states or {}),
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=output_storage,
    )


def _stats(*, passed, failed):
    return TestStats(
        discovered=5,
        executed=5,
        passed=passed,
        failed=failed,
        skipped=0,
    )


def _facade_failure(system, original, *, action="test"):
    backend_params = (
        {"command": action, "working_directory": "/workspace/bad"}
        if system == "maven"
        else {"tasks": action, "working_directory": "/workspace/bad"}
    )
    envelope = ToolResult.completed_failure(
        output=f"{system} project file not found",
        error=("pom.xml not found" if system == "maven" else "build.gradle not found"),
        error_code=("MISSING_PROJECT" if system == "maven" else "BUILD_FILE_NOT_FOUND"),
        facts={"system": system, "action": action},
        test_stats=original.test_stats,
        conflicts=["original_failure"],
    )
    return envelope.with_execution_trace(
        [ActualToolExecution(tool_name=system, params=backend_params, result=original)]
    )


def test_build_facade_recovery_persistence_error_keeps_original_and_draft(tmp_path):
    original = ToolResult.completed_failure(
        output="original test execution failed",
        error="original tests failed",
        error_code="ORIGINAL_TEST_FAILURE",
        test_stats=_stats(passed=3, failed=2),
        conflicts=["original_failure"],
    )
    storage = _SelectivePersistenceStorage()
    storage.seed(original)
    facade = _BuildFacadeFailure(_facade_failure("maven", original))

    def replacement_failure():
        return ToolResult.completed_failure(
            output="replacement persistence failed\nFINAL REPLACEMENT FAILURE",
            error="replacement tests failed",
            error_code="REPLACEMENT_TEST_FAILURE",
            test_stats=_stats(passed=2, failed=3),
            conflicts=["replacement_failure"],
            metadata={"replacement": True},
        )

    maven = _ResultTool("maven", [replacement_failure])
    engine, _ = _engine(tmp_path, phase="test")
    engine.output_storage = storage
    orchestrator = _orchestrator(
        engine,
        {"build": facade, "maven": maven},
        successful_states={"working_directory": "/workspace/good"},
        output_storage=storage,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    _prepare_action_execution(engine)

    with pytest.raises(OutputPersistenceError) as raised:
        engine._execute_steps(
            [_action_step("build", {"action": "test", "working_directory": "/workspace/bad"})]
        )

    assert storage.primary_failures == 1
    assert storage.emergency_failures == 1
    assert raised.value.tool_name == "maven"
    assert raised.value.params == {
        "command": "test",
        "working_directory": "/workspace/good",
    }
    assert raised.value.draft is not None
    assert raised.value.draft.execution_id
    assert raised.value.draft.failure_signature.startswith("REPLACEMENT_TEST_FAILURE:")
    assert raised.value.draft.test_stats == _stats(passed=2, failed=3)

    observations = engine.run_evidence_state.tool_observations
    assert len(observations) == 2
    assert [observation.tool_name for observation in observations] == ["maven", "maven"]
    assert [observation.roles for observation in observations] == [
        (EvidenceRole.TEST,),
        (EvidenceRole.TEST,),
    ]
    assert len({observation.execution_id for observation in observations}) == 2
    assert observations[0].result.test_stats == _stats(passed=3, failed=2)
    assert observations[1].result.test_stats == _stats(passed=2, failed=3)
    assert observations[1].result.output_ref is None
    assert engine.run_evidence_state.conflicts == (
        "original_failure",
        "replacement_failure",
        "output_storage_failed",
    )

    snapshot = engine.verdict_finalizer.finalize(
        engine.run_evidence_state,
        EvidenceCloseReason.ABORTED,
    )
    assert snapshot.test_stats.executed == 5
    assert snapshot.test_stats.passed == 2
    assert snapshot.test_stats.failed == 3
    assert snapshot.test_stats.raw.executed == 10


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_build_facade_recovery_uses_backend_replacement_identity(tmp_path, system):
    original = ToolResult.completed_failure(
        output=f"original {system} test failed",
        error="project file not found",
        error_code="ORIGINAL_FAILURE",
        test_stats=_stats(passed=3, failed=2),
    )
    facade = _BuildFacadeFailure(_facade_failure(system, original))
    replacement = _ResultTool(
        system,
        [
            ToolResult.completed_success(
                output="replacement passed",
                test_stats=_stats(passed=5, failed=0),
            )
        ],
    )
    engine, _ = _engine(tmp_path, phase="test")
    orchestrator = _orchestrator(
        engine,
        {"build": facade, system: replacement},
        successful_states={"working_directory": "/workspace/good"},
    )

    execution = orchestrator.execute(
        ToolCall(
            name="build",
            raw_params={"action": "test", "working_directory": "/workspace/bad"},
        )
    )

    assert execution.result.succeeded is True
    assert [actual.tool_name for actual in execution.actual_executions] == [system, system]
    expected_key = "command" if system == "maven" else "tasks"
    assert execution.actual_executions[1].params == {
        expected_key: "test",
        "working_directory": "/workspace/good",
    }


def test_recursive_and_direct_trace_duplicate_is_flattened_once():
    result = ToolResult.completed_success(
        output="five tests passed",
        test_stats=_stats(passed=5, failed=0),
    )
    leaf = ActualToolExecution(
        execution_id="execution_shared",
        tool_name="maven",
        params={"command": "test"},
        result=result,
    )
    nested = ToolResult.completed_success(output="nested facade").with_execution_trace([leaf])
    outer = ToolResult.completed_success(output="outer facade").with_execution_trace(
        [
            leaf,
            ActualToolExecution(
                execution_id="execution_nested_wrapper",
                tool_name="build",
                params={"action": "test"},
                result=nested,
            ),
        ]
    )

    flattened = ToolOrchestrator._flatten_actual_execution(
        "build",
        {"action": "test"},
        outer,
    )

    assert [actual.execution_id for actual in flattened] == ["execution_shared"]
    assert [actual.tool_name for actual in flattened] == ["maven"]


def test_state_dump_load_replay_is_idempotent_by_execution_id():
    result = ToolResult.completed_success(
        output="five tests passed",
        test_stats=_stats(passed=5, failed=0),
    )
    state = RunEvidenceState(run_id="execution-id-source")
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "maven",
        result,
        provenance="output_source",
        roles=[EvidenceRole.TEST],
        execution_id="execution_replayed",
    )
    dumped = state.model_dump(mode="json")
    loaded = ToolObservation.model_validate(dumped["tool_observations"][0])
    replayed = RunEvidenceState(run_id="execution-id-replayed")

    for _ in range(2):
        replayed.ingest_tool_result(
            loaded.scope,
            loaded.tool_name,
            loaded.result,
            loaded.provenance,
            roles=loaded.roles,
            execution_id=loaded.execution_id,
        )

    assert len(replayed.tool_observations) == 1
    assert replayed.tool_observations[0].execution_id == "execution_replayed"
    assert replayed.model_dump(mode="json")["tool_observations"][0]["execution_id"] == (
        "execution_replayed"
    )
    snapshot = VerdictFinalizer(FakeVerdictOrchestrator()).finalize(
        replayed,
        EvidenceCloseReason.TEST_TERMINATED,
    )
    assert snapshot.test_stats.executed == 5
    assert snapshot.test_stats.raw.executed == 5


def test_hostile_unpersisted_draft_has_one_small_total_budget():
    hostile = "x" * 2_000_000
    finding = EvidenceFinding(
        type="validator-conflict-" + hostile,
        reason="hostile validator payload-" + hostile,
        status=EvidenceAssessment.CONFLICT,
        refs=["finding-ref-" + hostile],
        details={"nested": [{"payload": hostile}, hostile]},
    )
    stats = _stats(passed=2, failed=3)
    draft = UnpersistedToolResult.from_failed_construction(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.CONFLICT,
        payload={
            "poll_ref": "poll-" + hostile,
            "failure_signature": "FAILURE-" + hostile,
            "error_tail_preview": hostile + "FINAL FAILURE TAIL",
            "evidence_assessment": EvidenceAssessment.CONFLICT,
            "error": "error-" + hostile,
            "error_code": "ERROR_CODE-" + hostile,
            "suggestions": [hostile] * 50,
            "documentation_links": [hostile] * 50,
            "raw_data": {f"raw-{index}-{hostile}": hostile for index in range(50)},
            "metadata": {f"meta-{index}-{hostile}": hostile for index in range(50)},
            "evidence_refs": [hostile] * 50,
            "conflicts": [hostile] * 50,
            "validator_findings": [finding],
            "test_stats": stats,
            "facts": {f"fact-{index}-{hostile}": hostile for index in range(50)},
            "refs": [hostile] * 50,
        },
    )

    encoded = draft.model_dump_json().encode("utf-8")
    assert len(encoded) <= DRAFT_CAP_BYTES
    assert draft.truncated is True
    assert draft.test_stats == stats
    assert draft.operation_outcome is OperationOutcome.FAILED
    assert draft.output_ref is None
    assert not hasattr(draft, "output")
    assert draft.failure_signature.startswith("FAILURE-")
    assert draft.error_tail_preview.endswith("FINAL FAILURE TAIL")
    assert len(draft.poll_ref) <= 256
    assert len(draft.failure_signature) <= 256
    assert len(draft.error_tail_preview) <= 400
    assert len(draft.error or "") <= 1000
    assert len(draft.error_code or "") <= 200
    bounded_finding = draft.validator_findings[0]
    assert bounded_finding.type.startswith("validator-conflict-")
    assert bounded_finding.reason.startswith("hostile validator payload-")
    assert bounded_finding.status is EvidenceAssessment.CONFLICT
    assert len(bounded_finding.type) <= 256
    assert len(bounded_finding.reason) <= 512
    assert len(bounded_finding.refs[0]) <= 500
    assert len(bounded_finding.model_dump_json().encode("utf-8")) <= 4096


def test_tools_base_annotation_contract_compiles_on_uv_python_312(tmp_path):
    source_path = Path(__file__).parents[1] / "src" / "sag" / "tools" / "base.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    future_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    assert any(alias.name == "annotations" for node in future_imports for alias in node.names)
    if PYTHON_312 is None:
        pytest.skip("python3.12 is not available on PATH")

    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    completed = subprocess.run(
        [PYTHON_312, "-m", "py_compile", str(source_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
