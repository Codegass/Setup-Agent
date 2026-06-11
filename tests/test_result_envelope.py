"""Envelope contract: every ToolResult carries verdict/facts/refs.

Spec §5: verdict ∈ success|partial|failed|running|unknown|skipped;
facts is structured; refs are retrieval handles for the search tool.
Backward compatible: legacy ToolResult(success=..., output=...) still works.
"""

import pytest

from sag.tools.base import ToolResult, VERDICTS


def test_verdict_vocabulary_is_closed():
    assert VERDICTS == {"success", "partial", "failed", "running", "unknown", "skipped"}


def test_verdict_derived_from_success_when_absent():
    assert ToolResult(success=True, output="ok").verdict == "success"
    assert ToolResult(success=False, output="no").verdict == "failed"


def test_explicit_verdict_kept():
    r = ToolResult(success=True, output="still going", verdict="running")
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
