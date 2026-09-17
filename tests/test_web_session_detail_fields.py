"""What `ExecutionSessionDetail` carries for the Workbench detail view.

The detail used to hold a one-sentence verdict the server composed from module
rollups — a third derivation that could disagree with the CLI block and the
report table. It now carries the same result card those two surfaces print.
"""

from sag.web.models import ExecutionSessionDetail


def test_detail_serializes_run_metadata_camelcase():
    d = ExecutionSessionDetail.model_validate(
        {
            "id": "S1",
            "workspace": "w",
            "title": "t",
            "status": "partial",
            "entry": "e",
            "start": "now",
            "duration": "1s",
            "outcome": "⚠️ PARTIAL",
            "report": "ready",
            "build": {"state": "success", "tool": "maven", "time": "2m", "note": ""},
            "test": {"state": "partial", "pass": 1, "fail": 1, "skip": 0, "total": 2},
            "evidence": [],
            "logs": [],
            "model": "claude-sonnet-4.5",
            "steps": 6,
            "stepBudget": 40,
        }
    )
    out = d.model_dump(mode="json", by_alias=True)
    assert out["model"] == "claude-sonnet-4.5"
    assert out["stepBudget"] == 40


def test_new_fields_default_none():
    d = ExecutionSessionDetail.model_validate(
        {
            "id": "S1",
            "workspace": "w",
            "title": "t",
            "status": "ok",
            "entry": "e",
            "start": "now",
            "duration": "1s",
            "outcome": "",
            "report": "none",
            "build": {"state": "success", "tool": "maven", "time": "", "note": ""},
            "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
            "evidence": [],
            "logs": [],
        }
    )
    out = d.model_dump(mode="json", by_alias=True)
    assert out["resultCard"] is None and out["model"] is None and out["stepBudget"] is None


def test_detail_serves_the_result_card_not_a_composed_sentence():
    from sag.web.models import ExecutionSessionDetail

    fields = ExecutionSessionDetail.model_fields
    assert "result_card" in fields
    for gone in ("verdict", "ci_comparison_lines", "task_completion_lines", "files"):
        assert gone not in fields, f"{gone} should have been removed with the composed verdict"


def test_result_card_serializes_under_its_camel_case_alias():
    from sag.result_card.build import build_result_card
    from sag.web.models import ExecutionSessionDetail

    from result_card_fakes import snapshot_dict

    card = build_result_card(snapshot_dict())
    detail = ExecutionSessionDetail.model_construct(result_card=card)
    dumped = detail.model_dump(mode="json", by_alias=True, include={"result_card"})
    assert "resultCard" in dumped
    assert dumped["resultCard"]["rows"][0]["key"] == "setup"
    # The envelope key is not the only camelCase thing here: the body speaks the
    # same convention, so a reader never switches halfway through the object.
    assert dumped["resultCard"]["runId"] == card.run_id
    assert [key for key in dumped["resultCard"] if "_" in key] == []


def test_the_composed_verdict_module_is_gone():
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sag.web.verdict")
