"""Task #53 — `terminal_reason` states the reason the run actually closed for.

`report_metrics._outcome_surface` read `close_reason` off the sealed verdict
snapshot, and `RunVerdictSnapshot` (frozen, `extra="forbid"`) has no such field
— so the fallback branch fired in EVERY run in `logs/`, including 20+ that
delivered a report phase. Read as forensics, `evidence_close_unavailable` looked
like a signal about the run that skipped its report. It was a signal about the
field: the reason was known and sealed, and this surface simply could not see it.

The engine writes the reason where it is authoritative — the `evidence_close`
control event — and the metrics artifact now reads it from there, in the same
single stream read the control surface already pays for. Absent basis still
states absence: an unreadable stream keeps saying `evidence_close_unavailable`,
which is then true.
"""

import json
from types import SimpleNamespace

from test_report_tool_metrics_artifact import CapturingOrchestrator

from sag.tools.report_metrics import assemble_report_metrics
from sag.tools.report_tool import ReportTool

_SNAPSHOT = {
    "verdict": "partial",
    "finalized_at": "2026-08-08T12:00:00Z",
    "build_evidence": {"observed": False, "judgment": "unknown"},
    "test_stats": {},
    "conflicts": [],
    "input_refs": [],
}


def _stream(tmp_path, rows):
    path = tmp_path / "control_events.jsonl"
    path.write_text(
        "".join(json.dumps({"sequence": index, **row}) + "\n" for index, row in enumerate(rows, 1)),
        encoding="utf-8",
    )
    return path


def test_the_sealed_close_reason_reaches_the_metrics_artifact(tmp_path):
    stream = _stream(
        tmp_path,
        [
            {"kind": "evidence_close", "payload": {"reason": "test_terminated"}},
        ],
    )
    tool = ReportTool(docker_orchestrator=CapturingOrchestrator())
    tool.control_event_sink = SimpleNamespace(path=str(stream))

    metrics = tool.finalize_metrics_v2(dict(_SNAPSHOT))

    assert metrics["outcome"]["terminal_reason"] == "test_terminated"


def test_an_unreadable_control_stream_still_says_the_reason_is_unavailable(tmp_path):
    tool = ReportTool(docker_orchestrator=CapturingOrchestrator())
    tool.control_event_sink = SimpleNamespace(path=str(tmp_path / "absent.jsonl"))

    metrics = tool.finalize_metrics_v2(dict(_SNAPSHOT))

    assert metrics["outcome"]["terminal_reason"] == "evidence_close_unavailable"


def test_the_last_close_wins_and_a_blank_reason_is_not_a_reason(tmp_path):
    stream = _stream(
        tmp_path,
        [
            {"kind": "evidence_close", "payload": {"reason": "dependents_skipped"}},
            {"kind": "evidence_close", "payload": {"reason": "aborted"}},
        ],
    )
    tool = ReportTool(docker_orchestrator=CapturingOrchestrator())
    tool.control_event_sink = SimpleNamespace(path=str(stream))

    metrics = tool.finalize_metrics_v2(dict(_SNAPSHOT))

    assert metrics["outcome"]["terminal_reason"] == "aborted"


def test_the_assembler_takes_the_reason_it_is_given():
    metrics = assemble_report_metrics(
        snapshot=dict(_SNAPSHOT),
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
        close_reason="test_terminated",
    )

    assert metrics["outcome"]["terminal_reason"] == "test_terminated"
