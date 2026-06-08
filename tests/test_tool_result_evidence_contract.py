from sag.evidence import EvidenceStatus
from sag.tools.base import ToolResult


def test_tool_result_defaults_status_from_success_boolean():
    success = ToolResult(success=True, output="ok")
    failure = ToolResult(success=False, output="", error="bad")

    assert success.status == EvidenceStatus.SUCCESS
    assert failure.status == EvidenceStatus.BLOCKED
    assert success.success is True
    assert failure.success is False


def test_tool_result_status_can_represent_partial_without_losing_legacy_success():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Build command exited 0 but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
    )

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert result.evidence_refs == ["output_abc"]
    assert result.conflicts == ["maven_success_vs_surefire_failures"]
    assert result.test_stats.pass_rate == 96.3
