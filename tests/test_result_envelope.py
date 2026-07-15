"""Envelope contract: every ToolResult carries verdict/facts/refs.

Spec §5: verdict ∈ success|partial|failed|running|unknown|skipped;
facts is structured; refs are retrieval handles for the search tool.
Backward compatible: legacy ToolResult(success=..., output=...) still works.
"""

import pytest

from sag.agent.tool_orchestration import format_tool_result
from sag.tools.base import ToolResult, VERDICTS


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == {"success", "partial", "failed", "running", "unknown", "skipped"}


def test_verdict_derived_from_success_when_absent():
    assert ToolResult(success=True, output="ok").verdict == "success"
    assert ToolResult(success=False, output="no").verdict == "failed"


def test_explicit_verdict_kept():
    r = ToolResult(success=True, output="still going", verdict="running", poll_ref="job:still-going")
    assert r.verdict == "running"


def test_invalid_verdict_rejected():
    with pytest.raises(ValueError):
        ToolResult(success=True, output="x", verdict="amazing")


def test_facts_and_refs_default_empty():
    r = ToolResult(success=True, output="x")
    assert r.facts == {}
    assert r.refs == []


def test_envelope_fields_round_trip():
    r = ToolResult(
        success=True,
        output="206/214 passed",
        verdict="partial",
        facts={"executed": 214, "passed": 206, "pass_rate": 96.3},
        refs=["output_5b9a"],
    )
    assert r.facts["pass_rate"] == 96.3
    assert r.refs == ["output_5b9a"]


def test_observation_shows_verdict_and_facts():
    r = ToolResult(
        success=True, output="206/214 passed", verdict="partial",
        facts={"executed": 214, "passed": 206}, refs=["output_5b9a"],
    )
    obs = format_tool_result("build", r)
    assert "partial" in obs.lower()
    assert "executed" in obs and "214" in obs
    assert "output_5b9a" in obs


def test_observation_running_keeps_dispatch_wording():
    r = ToolResult(
        success=True, output="still running; poll later", verdict="running",
        poll_ref="job:background-build", metadata={"dispatch_status": "running_detached"},
    )
    obs = format_tool_result("build", r)
    assert "still running" in obs.lower()
    assert "✅" not in obs.split("\n")[0]


def test_observation_legacy_result_unchanged_shape():
    obs = format_tool_result("bash", ToolResult(success=True, output="hello"))
    assert "✅ bash executed successfully" in obs
