# tests/test_web_module_read_model.py
import json
from sag.web.session_registry import _modules_payload_from_metrics, _module_rollup_from_metrics


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
