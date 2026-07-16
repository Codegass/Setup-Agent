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


def test_terminal_failure_is_orthogonal_to_invocation_completion(
    durable_tool_result_storage,
):
    output_ref = durable_tool_result_storage.store_output(
        task_id="taxonomy",
        tool_name="build",
        output="BUILD FAILURE",
    )
    result = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        evidence_status=EvidenceStatus.VERIFIED,
        output="BUILD FAILURE",
        error_code="BUILD_FAILED",
        failure_signature=FAILURE_PROVENANCE["failure_signature"],
        error_tail_preview=FAILURE_PROVENANCE["error_tail_preview"],
        output_ref=output_ref,
    )
    assert result.is_terminal is True
    assert result.succeeded is False
    assert result.failure_signature == FAILURE_PROVENANCE["failure_signature"]
    assert result.error_tail_preview == FAILURE_PROVENANCE["error_tail_preview"]
    assert result.output_ref == output_ref


def test_terminal_failure_factory_preserves_explicit_timeout_status():
    result = ToolResult.terminal_failure(
        invocation_status=InvocationStatus.TIMEOUT,
        output="command exceeded the configured timeout",
        error="command timed out",
        error_code="TIMEOUT_WALL_CLOCK",
    )

    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.evidence_status is EvidenceStatus.VERIFIED
    assert result.succeeded is False


def test_failure_factory_signature_ignores_volatile_runtime_evidence():
    first = ToolResult.completed_failure(
        output=(
            "2026-07-16T17:48:00Z pid=1234 job:build-a1b2 "
            "/tmp/sag-a1b2/progress.log progress 2/10: compiler cannot find symbol Widget"
        ),
        error="build failed",
        error_code="BUILD_FAILED",
    )
    second = ToolResult.completed_failure(
        output=(
            "2026-07-16T18:03:59Z pid=9876 job:build-z9y8 "
            "/tmp/sag-z9y8/progress.log progress 9/10: compiler cannot find symbol Widget"
        ),
        error="build failed",
        error_code="BUILD_FAILED",
    )

    assert first.failure_signature == second.failure_signature


def test_failure_signature_normalizes_temp_runs_without_erasing_semantic_basename():
    first_pom = ToolResult.completed_failure(
        output="/tmp/pytest-of-alice/pytest-41/run-a1b2/pom.xml: malformed project model",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    second_pom = ToolResult.completed_failure(
        output="/var/tmp/pytest-of-bob/pytest-99/run-z9y8/pom.xml: malformed project model",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    settings = ToolResult.completed_failure(
        output="/tmp/pytest-of-alice/pytest-41/run-a1b2/settings.xml: malformed project model",
        error="build failed",
        error_code="BUILD_FAILED",
    )

    assert first_pom.failure_signature == second_pom.failure_signature
    assert first_pom.failure_signature != settings.failure_signature


def test_failure_signature_retains_stable_suffix_for_entropy_bearing_temp_file():
    first_log = ToolResult.completed_failure(
        output="/tmp/sag-output-a1b2c3.log: parser failed",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    second_log = ToolResult.completed_failure(
        output="/var/tmp/sag-output-z9y8x7.log: parser failed",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    json_output = ToolResult.completed_failure(
        output="/tmp/sag-output-a1b2c3.json: parser failed",
        error="build failed",
        error_code="BUILD_FAILED",
    )

    assert first_log.failure_signature == second_log.failure_signature
    assert first_log.failure_signature != json_output.failure_signature


def test_failure_signature_keeps_short_semantic_temp_numbers_distinct():
    not_found = ToolResult.completed_failure(
        output="/tmp/result-404.json: request failed",
        error="request failed",
        error_code="REQUEST_FAILED",
    )
    server_error = ToolResult.completed_failure(
        output="/tmp/result-500.json: request failed",
        error="request failed",
        error_code="REQUEST_FAILED",
    )

    assert not_found.failure_signature != server_error.failure_signature


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        (
            "/tmp/550e8400-e29b-41d4-a716-446655440000.json",
            "/var/tmp/123e4567-e89b-12d3-a456-426614174000.json",
        ),
        (
            "/tmp/output-deadbeefcafebabe.log",
            "/var/tmp/output-c001d00dcafed00d.log",
        ),
        (
            "/tmp/sag-output-a1b2c3.log",
            "/var/tmp/sag-output-z9y8x7.log",
        ),
    ],
)
def test_failure_signature_normalizes_genuine_temp_entry_entropy(first_path, second_path):
    first = ToolResult.completed_failure(
        output=f"{first_path}: parser failed",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    second = ToolResult.completed_failure(
        output=f"{second_path}: parser failed",
        error="build failed",
        error_code="BUILD_FAILED",
    )

    assert first.failure_signature == second.failure_signature


def test_failure_factory_keeps_explicit_domain_signature_authoritative():
    result = ToolResult.completed_failure(
        output="/tmp/run-a1b2/pom.xml: malformed project model",
        error="build failed",
        error_code="BUILD_FAILED",
        failure_signature="maven:model:malformed-pom",
    )

    assert result.failure_signature == "maven:model:malformed-pom"


def test_failure_factory_signature_keeps_distinct_root_causes_distinct():
    compiler_failure = ToolResult.completed_failure(
        output="pid=1234: compiler cannot find symbol Widget",
        error="build failed",
        error_code="BUILD_FAILED",
    )
    dependency_failure = ToolResult.completed_failure(
        output="pid=9876: dependency org.example:widget:1.0 could not be resolved",
        error="build failed",
        error_code="BUILD_FAILED",
    )

    assert compiler_failure.failure_signature != dependency_failure.failure_signature


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


def test_failure_factory_rejects_shape_valid_but_unpersisted_output_ref(tmp_path):
    from sag.agent.output_storage import OutputStorageManager

    storage = OutputStorageManager(tmp_path)
    with bind_tool_result_output_storage(storage):
        with pytest.raises(ValueError, match="persisted.*OutputStorage"):
            ToolResult.completed_failure(
                output="failed",
                error="failed",
                error_code="FAILED",
                output_ref="output_missing",
            )


def test_direct_failure_rejects_shape_valid_but_unpersisted_output_ref():
    with pytest.raises(ValueError, match="persisted.*OutputStorage"):
        ToolResult(
            invocation_status="completed",
            operation_outcome="failed",
            evidence_status="verified",
            output="failed",
            error_code="FAILED",
            failure_signature="FAILED:missing",
            error_tail_preview="failed",
            output_ref="output_missing",
        )


def test_failure_factory_accepts_ref_from_explicit_origin_storage(tmp_path):
    from sag.agent.output_storage import OutputStorageManager

    outer_storage = OutputStorageManager(tmp_path / "outer")
    origin_storage = OutputStorageManager(tmp_path / "origin")
    output_ref = origin_storage.store_output(
        task_id="maven",
        tool_name="maven",
        output="maven failed",
    )

    with bind_tool_result_output_storage(outer_storage):
        result = ToolResult.completed_failure(
            output="maven failed",
            error="failed",
            error_code="MAVEN_BUILD_FAILED",
            evidence_refs=[output_ref],
            output_ref_storage=origin_storage,
        )
        outer_result = ToolResult.completed_failure(
            output="outer failure",
            error="failed",
            error_code="OUTER_FAILED",
        )

    assert result.output_ref == output_ref
    assert origin_storage.retrieve_output(output_ref) == "maven failed"
    assert outer_storage.retrieve_output(outer_result.output_ref) == "outer failure"
    dumped = result.model_dump()
    assert "output_ref_storage" not in dumped
    assert "_output_ref_verified" not in dumped


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
