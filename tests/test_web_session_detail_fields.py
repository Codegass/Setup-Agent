"""Task 3 of the workbench-detail redesign: ExecutionSessionDetail gains a
server-composed verdict plus run metadata (model, steps, stepBudget). All
fields are nullable so older sessions degrade gracefully."""

from sag.web.models import ExecutionSessionDetail, VerdictSummary


def test_detail_serializes_new_fields_camelcase():
    d = ExecutionSessionDetail.model_validate({
        "id": "S1", "workspace": "w", "title": "t", "status": "partial", "entry": "e",
        "start": "now", "duration": "1s", "outcome": "⚠️ PARTIAL", "report": "ready",
        "build": {"state": "success", "tool": "maven", "time": "2m", "note": ""},
        "test": {"state": "partial", "pass": 1, "fail": 1, "skip": 0, "total": 2},
        "evidence": [], "logs": [],
        "verdict": {"tone": "attention", "headline": "x", "detail": None},
        "model": "claude-sonnet-4.5", "steps": 6, "stepBudget": 40,
    })
    out = d.model_dump(mode="json", by_alias=True)
    assert out["verdict"]["tone"] == "attention"
    assert out["model"] == "claude-sonnet-4.5"
    assert out["stepBudget"] == 40


def test_new_fields_default_none():
    d = ExecutionSessionDetail.model_validate({
        "id": "S1", "workspace": "w", "title": "t", "status": "ok", "entry": "e",
        "start": "now", "duration": "1s", "outcome": "", "report": "none",
        "build": {"state": "success", "tool": "maven", "time": "", "note": ""},
        "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
        "evidence": [], "logs": [],
    })
    out = d.model_dump(mode="json", by_alias=True)
    assert out["verdict"] is None and out["model"] is None and out["stepBudget"] is None


def test_verdict_summary_model_roundtrips():
    v = VerdictSummary.model_validate({"tone": "success", "headline": "Build passed"})
    out = v.model_dump(mode="json", by_alias=True)
    assert out == {"tone": "success", "headline": "Build passed", "detail": None}
