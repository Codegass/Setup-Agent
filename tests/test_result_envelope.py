"""Canonical ToolResult envelope contract for outcomes, facts, and refs."""

import pytest

from sag.agent.tool_orchestration import format_tool_result
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult


def test_result_vocabularies_are_closed_and_orthogonal():
    assert {item.value for item in InvocationStatus} == {
        "pending",
        "completed",
        "timeout",
        "crashed",
        "cancelled",
    }
    assert {item.value for item in OperationOutcome} == {
        "unknown",
        "success",
        "partial",
        "failed",
        "skipped",
    }


def test_tool_result_has_no_run_level_verdict():
    result = ToolResult.completed_success(output="ok")

    assert not hasattr(result, "verdict")


def test_explicit_verdict_is_rejected():
    with pytest.raises(ValueError, match="verdict"):
        ToolResult.completed_success(output="x", verdict="partial")


def test_facts_and_refs_default_empty():
    result = ToolResult.completed_success(output="x")

    assert result.facts == {}
    assert result.refs == []


def test_envelope_fields_round_trip():
    result = ToolResult.completed(
        output="206/214 passed",
        operation_outcome="partial",
        facts={"executed": 214, "passed": 206, "pass_rate": 96.3},
        refs=["output_5b9a"],
    )

    assert result.operation_outcome is OperationOutcome.PARTIAL
    assert result.facts["pass_rate"] == 96.3
    assert result.refs == ["output_5b9a"]


def test_observation_shows_outcome_and_facts():
    result = ToolResult.completed(
        output="206/214 passed",
        operation_outcome="partial",
        facts={"executed": 214, "passed": 206},
        refs=["output_5b9a"],
    )

    observation = format_tool_result("build", result)

    assert "partial" in observation.lower()
    assert "executed" in observation and "214" in observation
    assert "output_5b9a" in observation


def test_observation_pending_keeps_dispatch_wording():
    result = ToolResult(
        invocation_status="pending",
        operation_outcome="unknown",
        evidence_status=EvidenceStatus.UNKNOWN,
        poll_ref="job:background-build",
        output="still running; poll later",
        metadata={"dispatch_status": "running_detached"},
    )

    observation = format_tool_result("build", result)

    assert "still running" in observation.lower()
    assert "✅" not in observation.split("\n")[0]


def test_success_observation_shape_is_stable():
    observation = format_tool_result("bash", ToolResult.completed_success(output="hello"))

    assert "✅ bash executed successfully" in observation
