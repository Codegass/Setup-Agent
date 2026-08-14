import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_verdict_finalizer import FakeVerdictOrchestrator, bind_verdict_authority

from sag.agent.evidence_state import (
    EvidenceRole,
    RunEvidenceState,
    StateScope,
    ToolObservation,
)
from sag.agent.tool_orchestration import ToolOrchestrator
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
    ToolResult,
    UnpersistedToolResult,
)

PYTHON_312 = shutil.which("python3.12")
DRAFT_CAP_BYTES = 32 * 1024


def _stats(*, passed, failed):
    return TestStats(
        discovered=5,
        executed=5,
        passed=passed,
        failed=failed,
        skipped=0,
    )


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
        source_phase="test",
        source_attempt_id="test-1",
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
            source_phase=loaded.source_phase,
            source_attempt_id=loaded.source_attempt_id,
        )

    with pytest.raises(ValueError, match="conflicting observation"):
        replayed.ingest_tool_result(
            loaded.scope,
            loaded.tool_name,
            loaded.result,
            loaded.provenance,
            roles=loaded.roles,
            execution_id=loaded.execution_id,
            source_phase="test",
            source_attempt_id="test-2",
        )

    assert len(replayed.tool_observations) == 1
    assert replayed.tool_observations[0].execution_id == "execution_replayed"
    assert replayed.tool_observations[0].source_phase == "test"
    assert replayed.tool_observations[0].source_attempt_id == "test-1"
    assert replayed.model_dump(mode="json")["tool_observations"][0]["execution_id"] == (
        "execution_replayed"
    )
    verdict_orchestrator = FakeVerdictOrchestrator()
    bind_verdict_authority(verdict_orchestrator, replayed.run_id)
    snapshot = VerdictFinalizer(verdict_orchestrator).finalize(
        replayed,
        EvidenceCloseReason.TEST_TERMINATED,
    )
    # Rebased 2026-08-14 (spec amendment item 9): idempotence is the subject —
    # ingesting the same execution twice contributes its volume ONCE. The
    # destination moved; the arithmetic under test did not.
    assert snapshot.test_stats.executed == 0
    assert snapshot.test_stats.auxiliary_test_stats["executed"] == 5


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
