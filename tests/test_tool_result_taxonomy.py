import pytest

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult


def test_pending_result_cannot_claim_success():
    with pytest.raises(ValueError, match="pending.*unknown"):
        ToolResult(
            invocation_status=InvocationStatus.PENDING,
            operation_outcome=OperationOutcome.SUCCESS,
            evidence_status=EvidenceStatus.UNKNOWN,
            output="started",
        )


def test_pending_result_requires_unknown_evidence_and_poll_ref():
    with pytest.raises(ValueError, match="pending.*poll_ref"):
        ToolResult(
            invocation_status="pending",
            operation_outcome="unknown",
            evidence_status="unknown",
            output="started",
        )
    with pytest.raises(ValueError, match="pending.*evidence"):
        ToolResult(
            invocation_status="pending",
            operation_outcome="unknown",
            evidence_status="verified",
            poll_ref="job:abc",
            output="started",
        )


def test_terminal_failure_is_orthogonal_to_invocation_completion():
    result = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.VERIFIED,
        output="BUILD FAILURE",
        error_code="BUILD_FAILED",
    )
    assert result.is_terminal is True
    assert result.succeeded is False


def test_legacy_and_canonical_truth_cannot_contradict():
    with pytest.raises(ValueError, match="contradict"):
        ToolResult(
            success=True,
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="failed",
        )
