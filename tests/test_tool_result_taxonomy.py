import json

import pytest

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult, bind_tool_result_output_storage

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


@pytest.mark.parametrize("field", ["success", "status", "verdict"])
def test_legacy_truth_inputs_are_rejected(field):
    values = _canonical_kwargs("completed", "success")
    values[field] = True if field == "success" else "success"

    with pytest.raises(ValueError, match=field):
        ToolResult(**values)


def test_canonical_failed_result_requires_all_failure_provenance():
    with pytest.raises(ValueError, match="failure_signature"):
        ToolResult(
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="BUILD FAILURE",
        )


@pytest.mark.parametrize("error_code", [None, " \t"])
def test_canonical_failed_result_requires_nonblank_error_code(error_code):
    with pytest.raises(ValueError, match="error_code"):
        ToolResult(
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="BUILD FAILURE",
            error_code=error_code,
            **FAILURE_PROVENANCE,
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


def test_failure_factory_refuses_to_fabricate_output_storage_ref():
    with bind_tool_result_output_storage(None):
        with pytest.raises(ValueError, match="durable.*output"):
            ToolResult.completed_failure(
                output="short failure",
                error="failed",
                error_code="SHORT_FAILURE",
            )


def test_failure_factory_promotes_existing_output_storage_evidence_ref(tmp_path):
    from sag.agent.output_storage import OutputStorageManager

    storage = OutputStorageManager(tmp_path)
    ref = storage.store_output(
        task_id="maven",
        tool_name="maven",
        output="maven failed",
    )
    with bind_tool_result_output_storage(storage):
        result = ToolResult.completed_failure(
            output="maven failed",
            error="failed",
            error_code="MAVEN_BUILD_FAILED",
            evidence_refs=[ref],
        )

    assert result.output_ref == ref
    assert storage.retrieve_output(result.output_ref) == "maven failed"


def test_failure_factory_rejects_unresolvable_output_ref():
    with pytest.raises(ValueError, match="resolvable.*OutputStorage"):
        ToolResult.completed_failure(
            output="failed",
            error="failed",
            error_code="FAILED",
            output_ref="tool-result:fabricated",
        )


def test_canonical_result_has_no_legacy_adapter_or_truth_fields():
    result = ToolResult(**_canonical_kwargs("completed", "success"))

    dumped = result.model_dump()
    assert {"success", "status", "verdict", "temporary_legacy_adapter_marker"}.isdisjoint(dumped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
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
        result.evidence_assessment,
    )

    result.output = "updated partial output"

    assert result.output == "updated partial output"
    assert (
        result.invocation_status,
        result.operation_outcome,
        result.evidence_status,
        result.evidence_assessment,
    ) == truth_before
