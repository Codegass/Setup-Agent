import ast
from pathlib import Path

import pytest
from test_evidence_ingestion import _engine
from test_lineage_idempotence_followup import _stats

from sag.agent.evidence_state import RunEvidenceState, ToolObservation
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome, TestStats
from sag.tools.base import (
    UNPERSISTED_DRAFT_MAX_BYTES,
    ActualToolExecution,
    ToolResult,
    UnpersistedToolResult,
)

ROOT = Path(__file__).parents[1]


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
