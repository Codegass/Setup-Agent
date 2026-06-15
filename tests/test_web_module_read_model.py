# tests/test_web_module_read_model.py
import json
from sag.web.session_registry import (
    _modules_payload_from_metrics,
    _module_rollup_from_metrics,
    _session_detail,
)


def _metrics():
    return {
        "version": 1,
        "module_summary": {"modules_total": 2, "modules_built": 1, "modules_failed": 1,
                           "modules_skipped": 0, "modules_with_test_failures": 1,
                           "build_systems": ["maven"], "single_module": False},
        "modules": [
            {"name": "core", "path": "core", "build_status": "success",
             "build_source": "reactor", "class_count": 50, "jar_count": 1,
             "tests_total": 10, "tests_passed": 9, "tests_failed": 1,
             "failing_names": ["core.FooTest.bad"], "failing_count": 1,
             "evidence_refs": ["/w/core/target/surefire-reports"]},
            {"name": "api", "path": "api", "build_status": "failure",
             "build_source": "reactor", "class_count": 0, "jar_count": 0,
             "tests_total": None, "failing_names": [], "failing_count": None,
             "evidence_refs": []},
        ],
    }


def test_modules_payload_maps_records():
    payload = _modules_payload_from_metrics(_metrics())
    assert len(payload) == 2
    assert payload[0]["build_status"] == "success"
    assert payload[0]["failing_count"] == 1


def test_module_rollup_maps_summary():
    rollup = _module_rollup_from_metrics(_metrics())
    assert rollup["modules_total"] == 2 and rollup["modules_failed"] == 1


def test_payload_none_when_absent():
    assert _modules_payload_from_metrics(None) == []
    assert _module_rollup_from_metrics(None) is None


def _base_item():
    return {
        "id": "SETUP-x-1",
        "workspace": "sag-x",
        "title": "Project setup",
        "status": "completed",
        "start": "2026-06-15 00:00:00",
    }


def test_session_detail_degrades_on_malformed_module_record():
    # A present, valid-JSON module_metrics.json with a bad-typed field (e.g.
    # class_count as a non-numeric string, or modules_total='oops') must not take
    # down the whole detail endpoint -- it should degrade to modules=[] / None.
    item = _base_item()
    item["modules"] = [
        {"name": "ok", "path": "ok", "class_count": 5},
        {"name": "bad", "path": "bad", "class_count": "oops"},
    ]
    item["module_summary"] = {"modules_total": "oops"}

    detail = _session_detail(item, "sag-x", None)

    assert detail is not None
    # Invalid records dropped; valid one kept (or all dropped -> []). Either way
    # no ValidationError escapes.
    assert all(m.name != "bad" for m in detail.modules)
    assert any(m.name == "ok" for m in detail.modules)
    assert detail.module_summary is None


def test_session_detail_keeps_valid_modules():
    item = _base_item()
    item["modules"] = [{"name": "core", "path": "core", "class_count": 50}]
    item["module_summary"] = {"modules_total": 1, "modules_built": 1}

    detail = _session_detail(item, "sag-x", None)

    assert [m.name for m in detail.modules] == ["core"]
    assert detail.module_summary is not None
    assert detail.module_summary.modules_total == 1
