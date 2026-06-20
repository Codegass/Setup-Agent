"""Task 3 of the workbench-detail redesign: ExecutionSessionDetail gains a
server-composed verdict plus run metadata (model, steps, stepBudget). All
fields are nullable so older sessions degrade gracefully."""

from sag.web.models import (
    BuildSummary,
    ExecutionSessionDetail,
    ModuleRollup,
    TestSummary,
    VerdictSummary,
)
from sag.web.verdict import compose_verdict


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


def test_compose_verdict_reads_serialized_model_aliases():
    """_session_detail feeds compose_verdict the serialized (by_alias) Pydantic
    dicts, so compose_verdict's reads (pass/fail/total, singleModule/modulesTotal/
    modulesBuilt) must line up with the models' serialization_alias values. A rename
    of any alias would silently drop a clause / mis-tone the verdict — lock it here."""
    verdict = compose_verdict(
        build=BuildSummary(state="partial", tool="Maven").model_dump(
            mode="json", by_alias=True
        ),
        test=TestSummary(pass_count=1186, fail_count=7, total=1205).model_dump(
            mode="json", by_alias=True
        ),
        module_summary=ModuleRollup(
            modules_total=4, modules_built=3, modules_failed=1, single_module=False
        ).model_dump(mode="json", by_alias=True),
        outcome="PARTIAL",
        blocker=None,
    )
    assert verdict is not None
    assert verdict["tone"] == "attention"
    assert verdict["headline"] == (
        "Build passed on 3 of 4 modules. 7 of 1,205 tests failing — review before promoting"
    )


def test_verdict_summary_model_roundtrips():
    v = VerdictSummary.model_validate({"tone": "success", "headline": "Build passed"})
    out = v.model_dump(mode="json", by_alias=True)
    assert out == {"tone": "success", "headline": "Build passed", "detail": None}
