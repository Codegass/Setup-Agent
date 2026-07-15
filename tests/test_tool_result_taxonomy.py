import json

import pytest

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult

FAILURE_PROVENANCE = {
    "failure_signature": "maven:BUILD_FAILED:compiler",
    "error_tail_preview": "[ERROR] COMPILATION ERROR: cannot find symbol",
    "output_ref": "output_build_failed_abc123",
}


def _canonical_kwargs(invocation_status: str, operation_outcome: str) -> dict[str, object]:
    values: dict[str, object] = {
        "invocation_status": invocation_status,
        "operation_outcome": operation_outcome,
        "evidence_status": ("unknown" if operation_outcome == "unknown" else "verified"),
        "output": f"{operation_outcome} output",
    }
    if invocation_status == "pending":
        values["poll_ref"] = "job:mixed-verdict"
    if operation_outcome == "failed":
        values.update(FAILURE_PROVENANCE)
    return values


def _dump_bytes(result: ToolResult) -> bytes:
    payload = result.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


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
        **FAILURE_PROVENANCE,
    )
    assert result.is_terminal is True
    assert result.succeeded is False
    assert result.failure_signature == FAILURE_PROVENANCE["failure_signature"]
    assert result.error_tail_preview == FAILURE_PROVENANCE["error_tail_preview"]
    assert result.output_ref == FAILURE_PROVENANCE["output_ref"]


def test_legacy_and_canonical_truth_cannot_contradict():
    with pytest.raises(ValueError, match="contradict"):
        ToolResult(
            success=True,
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="failed",
        )


def test_rejected_legacy_success_assignment_is_atomic():
    result = ToolResult(
        invocation_status="pending",
        operation_outcome="unknown",
        evidence_status="unknown",
        poll_ref="job:atomic",
        output="still running",
    )
    before = _dump_bytes(result)

    with pytest.raises((TypeError, ValueError)):
        result.success = True

    assert _dump_bytes(result) == before


def test_canonical_failed_result_requires_all_failure_provenance():
    with pytest.raises(ValueError, match="failure_signature"):
        ToolResult(
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="BUILD FAILURE",
        )


@pytest.mark.parametrize("field", list(FAILURE_PROVENANCE))
def test_canonical_failed_result_rejects_blank_failure_provenance(field):
    values = _canonical_kwargs("completed", "failed")
    values[field] = " \t"

    with pytest.raises(ValueError, match=field):
        ToolResult(**values)


def test_canonical_failed_result_bounds_error_tail_preview():
    values = _canonical_kwargs("completed", "failed")
    values["error_tail_preview"] = "x" * 401

    with pytest.raises(ValueError, match="error_tail_preview.*400"):
        ToolResult(**values)


@pytest.mark.parametrize(
    ("verdict", "invocation_status", "operation_outcome"),
    [
        ("success", "completed", "success"),
        ("partial", "completed", "partial"),
        ("failed", "completed", "failed"),
        ("unknown", "completed", "unknown"),
        ("skipped", "completed", "skipped"),
        ("running", "pending", "unknown"),
    ],
)
def test_mixed_canonical_legacy_verdict_accepts_matching_state(
    verdict, invocation_status, operation_outcome
):
    result = ToolResult(
        verdict=verdict,
        **_canonical_kwargs(invocation_status, operation_outcome),
    )

    assert result.invocation_status.value == invocation_status
    assert result.operation_outcome.value == operation_outcome
    assert result.verdict == verdict


@pytest.mark.parametrize(
    ("verdict", "invocation_status", "operation_outcome"),
    [
        ("success", "completed", "partial"),
        ("partial", "completed", "success"),
        ("failed", "completed", "success"),
        ("unknown", "completed", "skipped"),
        ("skipped", "completed", "unknown"),
        ("running", "completed", "unknown"),
    ],
)
def test_mixed_canonical_legacy_verdict_rejects_contradicting_state(
    verdict, invocation_status, operation_outcome
):
    with pytest.raises(ValueError, match="verdict.*contradict"):
        ToolResult(
            verdict=verdict,
            **_canonical_kwargs(invocation_status, operation_outcome),
        )


def test_mixed_canonical_result_rejects_unknown_legacy_verdict():
    with pytest.raises(ValueError, match="verdict must be one of"):
        ToolResult(verdict="amazing", **_canonical_kwargs("completed", "success"))


def test_canonical_only_result_has_no_legacy_adapter_exemption():
    result = ToolResult(**_canonical_kwargs("completed", "success"))

    assert result.temporary_legacy_adapter is False
    assert "temporary_legacy_adapter" not in result.model_dump()
    assert "temporary_legacy_adapter_marker" not in result.model_dump()


def test_canonical_input_cannot_request_temporary_legacy_adapter_exemption():
    with pytest.raises(ValueError, match="temporary legacy adapter marker.*internal"):
        ToolResult(
            temporary_legacy_adapter_marker=True,
            **_canonical_kwargs("completed", "failed"),
        )


def test_legacy_only_failed_result_uses_excluded_temporary_adapter_exemption():
    result = ToolResult(success=False, output="legacy failure")

    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.failure_signature is None
    assert result.error_tail_preview is None
    assert result.output_ref is None
    assert result.temporary_legacy_adapter is True
    assert "temporary_legacy_adapter" not in result.model_dump()
    assert "temporary_legacy_adapter_marker" not in result.model_dump()


@pytest.mark.parametrize(
    ("verdict", "invocation_status", "operation_outcome"),
    [
        ("success", "completed", "success"),
        ("partial", "completed", "partial"),
        ("failed", "completed", "failed"),
        ("unknown", "completed", "unknown"),
        ("skipped", "completed", "skipped"),
        ("running", "pending", "unknown"),
    ],
)
def test_legacy_only_verdict_uses_shared_compatibility_mapping(
    verdict, invocation_status, operation_outcome
):
    values = {"verdict": verdict, "output": f"legacy {verdict}"}
    if verdict == "running":
        values["poll_ref"] = "job:legacy-running"

    result = ToolResult(**values)

    assert result.invocation_status.value == invocation_status
    assert result.operation_outcome.value == operation_outcome
    assert result.temporary_legacy_adapter is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("success", False),
        ("status", "blocked"),
        ("verdict", "failed"),
        ("invocation_status", "crashed"),
        ("operation_outcome", "failed"),
        ("evidence_status", "conflict"),
        ("poll_ref", "job:changed"),
        ("failure_signature", "changed-signature"),
        ("error_tail_preview", "changed tail"),
        ("output_ref", "output_changed"),
    ],
)
def test_result_truth_fields_are_read_only_after_construction(field, value):
    result = ToolResult(**_canonical_kwargs("completed", "success"))
    before = _dump_bytes(result)

    with pytest.raises((TypeError, ValueError), match="read.only"):
        setattr(result, field, value)

    assert _dump_bytes(result) == before


def test_mutable_payload_assignment_preserves_canonical_truth():
    result = ToolResult(**_canonical_kwargs("completed", "partial"))
    truth_before = (
        result.invocation_status,
        result.operation_outcome,
        result.evidence_status,
        result.success,
        result.status,
        result.verdict,
    )

    result.output = "updated partial output"

    assert result.output == "updated partial output"
    assert (
        result.invocation_status,
        result.operation_outcome,
        result.evidence_status,
        result.success,
        result.status,
        result.verdict,
    ) == truth_before
