import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_ingestion import _engine
from test_lineage_idempotence_followup import _ResultTool, _stats

from sag.agent.evidence_state import RunEvidenceState, ToolObservation
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome, TestStats
from sag.tools.base import (
    UNPERSISTED_DRAFT_MAX_BYTES,
    ActualToolExecution,
    OutputPersistenceError,
    ToolResult,
    UnpersistedToolResult,
)

ROOT = Path(__file__).parents[1]


class _PomLocator:
    project_name = "sample"

    def execute_command(self, command):
        return {
            "success": True,
            "output": "/workspace/sample/pom.xml",
            "exit_code": 0,
        }


def _tool_orchestrator(tools, context_manager):
    return ToolOrchestrator(
        tools=tools,
        context_manager=context_manager,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
    )


def _draft(*, error_code="REPLACEMENT_PERSISTENCE_FAILED"):
    return UnpersistedToolResult.from_failed_construction(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.CONFLICT,
        payload={
            "error": "replacement output could not be persisted",
            "error_code": error_code,
            "failure_signature": f"{error_code}:signature",
            "error_tail_preview": "replacement persistence failed",
            "conflicts": ["replacement_persistence_failed"],
            "test_stats": _stats(passed=2, failed=3),
        },
    )


def test_maven_pom_discovery_reraises_replacement_persistence_error_with_lineage():
    original = ToolResult.completed_failure(
        output="no pom at requested root",
        error="No pom.xml found at /workspace",
        error_code="NO_POM_XML",
        test_stats=_stats(passed=3, failed=2),
    )

    def fail_replacement_construction():
        raise OutputPersistenceError(
            "primary and emergency persistence failed",
            draft=_draft(),
        )

    maven = _ResultTool("maven", [original, fail_replacement_construction])
    maven._parameter_schema["properties"]["pom_file"] = {"type": "string"}
    orchestrator = _tool_orchestrator(
        {"maven": maven},
        SimpleNamespace(orchestrator=_PomLocator()),
    )

    with pytest.raises(OutputPersistenceError) as raised:
        orchestrator.execute(
            ToolCall(
                name="maven",
                raw_params={"command": "test"},
                validated_params={"command": "test"},
            )
        )

    error = raised.value
    replacement_params = {
        "command": "test",
        "pom_file": "/workspace/sample/pom.xml",
        "working_directory": "/workspace/sample",
    }
    assert error.tool_name == "maven"
    assert error.params == replacement_params
    assert error.execution_id
    assert error.draft is not None
    assert error.draft.execution_id == error.execution_id
    assert len(error.actual_executions) == 1
    assert error.actual_executions[0].tool_name == "maven"
    assert error.actual_executions[0].params == {"command": "test"}
    assert error.actual_executions[0].result is original


def test_recovery_broad_catches_reraise_output_persistence_errors_first():
    source_path = ROOT / "src" / "sag" / "agent" / "tool_recovery.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body = ast.Module(body=node.body, type_ignores=[])
        calls_safe_execute = any(
            isinstance(child, ast.Attribute) and child.attr == "safe_execute"
            for child in ast.walk(body)
        )
        if not calls_safe_execute:
            continue
        exception_index = next(
            (
                index
                for index, handler in enumerate(node.handlers)
                if isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
            ),
            None,
        )
        if exception_index is None:
            continue
        persistence_index = next(
            (
                index
                for index, handler in enumerate(node.handlers)
                if isinstance(handler.type, ast.Name)
                and handler.type.id == "OutputPersistenceError"
            ),
            None,
        )
        if persistence_index is None or persistence_index > exception_index:
            violations.append(node.lineno)

    assert violations == []


@pytest.mark.parametrize("difference", ["tool_name", "params", "result"])
def test_flatten_rejects_conflicting_duplicate_execution_id(difference):
    result = ToolResult.completed_success(
        output="five tests passed",
        test_stats=_stats(passed=5, failed=0),
    )
    original = ActualToolExecution(
        execution_id="execution_collision",
        tool_name="maven",
        params={"command": "test"},
        result=result,
    )
    conflicting = ActualToolExecution(
        execution_id="execution_collision",
        tool_name="gradle" if difference == "tool_name" else "maven",
        params={"command": "verify"} if difference == "params" else {"command": "test"},
        result=(
            ToolResult.completed_success(output="different result")
            if difference == "result"
            else result
        ),
    )
    envelope = ToolResult.completed_success(output="facade").with_execution_trace(
        [original, conflicting]
    )

    with pytest.raises(ValueError, match="conflicting execution_id execution_collision"):
        ToolOrchestrator._flatten_actual_execution("build", {"action": "test"}, envelope)


@pytest.mark.parametrize("difference", ["params", "result"])
def test_engine_replay_validates_duplicate_execution_id(tmp_path, difference):
    engine, _ = _engine(tmp_path, phase="test")
    params = {"command": "test", "working_directory": "/workspace/app"}
    result = ToolResult.completed_success(
        output="five tests passed",
        test_stats=_stats(passed=5, failed=0),
    )
    recorded = engine._record_tool_execution(
        "maven",
        params,
        result,
        execution_id="execution_engine_replay",
    )
    observation_count = len(engine.run_evidence_state.tool_observations)
    attempt_count = len(engine.run_evidence_state.action_attempts)

    assert (
        engine._record_tool_execution(
            "maven",
            params,
            recorded,
            execution_id="execution_engine_replay",
        )
        is recorded
    )
    assert len(engine.run_evidence_state.tool_observations) == observation_count
    assert len(engine.run_evidence_state.action_attempts) == attempt_count

    conflicting_params = (
        {**params, "working_directory": "/workspace/other"} if difference == "params" else params
    )
    conflicting_result = (
        recorded.model_copy(update={"output": "different result"})
        if difference == "result"
        else recorded
    )
    with pytest.raises(ValueError, match="conflicting observation for execution_id"):
        engine._record_tool_execution(
            "maven",
            conflicting_params,
            conflicting_result,
            execution_id="execution_engine_replay",
        )


def test_observation_params_survive_dump_load_and_idempotent_replay(tmp_path):
    engine, _ = _engine(tmp_path, phase="test")
    params = {"command": "test", "working_directory": "/workspace/app"}
    result = ToolResult.completed_success(
        output="five tests passed",
        test_stats=_stats(passed=5, failed=0),
    )
    engine._record_tool_execution(
        "maven",
        params,
        result,
        execution_id="execution_dumped_params",
    )
    dumped = engine.run_evidence_state.model_dump(mode="json")
    loaded = ToolObservation.model_validate(dumped["tool_observations"][0])
    replayed = RunEvidenceState(run_id="replayed-observation")

    for _ in range(2):
        replayed.ingest_tool_result(
            loaded.scope,
            loaded.tool_name,
            loaded.result,
            loaded.provenance,
            roles=loaded.roles,
            execution_id=loaded.execution_id,
            params=loaded.params,
        )

    assert loaded.params == params
    assert len(replayed.tool_observations) == 1
    assert replayed.tool_observations[0].params == params


def test_unpersisted_draft_hard_cap_handles_300k_digit_test_count():
    huge_count = 10**299_999
    stats = TestStats(
        discovered=huge_count,
        executed=huge_count,
        passed=huge_count,
        failed=0,
        skipped=0,
    )

    draft = UnpersistedToolResult.from_failed_construction(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.CONFLICT,
        payload={
            "error": "failed execution",
            "error_code": "FAILED",
            "failure_signature": "FAILED:signature",
            "error_tail_preview": "failed execution",
            "test_stats": stats,
        },
    )

    assert len(draft.model_dump_json().encode("utf-8")) <= UNPERSISTED_DRAFT_MAX_BYTES
    assert draft.test_stats is None
    assert draft.truncated is True


def test_unpersisted_draft_direct_construction_enforces_hard_cap():
    huge_count = 10**299_999

    with pytest.raises(ValueError, match="serialized size limit"):
        UnpersistedToolResult(
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.FAILED,
            evidence_status=EvidenceStatus.CONFLICT,
            test_stats=TestStats(
                discovered=huge_count,
                executed=huge_count,
                passed=huge_count,
            ),
        )


def test_unpersisted_draft_copy_cannot_bypass_hard_cap():
    draft = UnpersistedToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.CONFLICT,
    )

    with pytest.raises(ValueError, match="serialized size limit"):
        draft.model_copy(update={"metadata": {"payload": "x" * 40_000}})


def test_python_compatibility_smoke_has_no_user_specific_interpreter_path():
    source_path = ROOT / "tests" / "test_lineage_idempotence_followup.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    absolute_user_paths = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/Users/")
    ]
    portable_discovery = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "shutil"
        and node.func.attr == "which"
        for node in ast.walk(tree)
    )

    assert absolute_user_paths == []
    assert portable_discovery is True
