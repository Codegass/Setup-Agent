# tests/test_web_metrics_read_model.py
from sag.web.session_registry import (
    _build_payload_from_metrics,
    _test_payload_from_metrics,
)

METRICS = {
    "version": 1,
    "build": {"state": "success", "system": "maven", "tool": "/usr/bin/mvn",
              "class_count": 115, "jar_count": 0, "module_output_count": 3,
              "artifact_samples": ["target/classes/Foo.class"], "warnings": [],
              "evidence_refs": ["output_x"]},
    "test": {"state": "partial", "total": 18839, "passed": 18805, "failed": 5,
             "errors": 0, "skipped": 29, "pass_rate": 99.8, "report_file_count": 760,
             "unique_total": 9497, "unique_passed": 9480, "unique_failed": 5,
             "unique_errors": 0, "unique_skipped": 12, "declared_total": 20500,
             "method_execution_rate": 46.3, "failing_names": ["com.x.FooTest.testA"],
             "conflicts": ["test_report_parse_error"], "evidence_refs": ["output_5b9a"]},
}


def test_build_payload_from_metrics_maps_fields():
    p = _build_payload_from_metrics(METRICS)
    assert p["state"] == "success" and p["system"] == "maven"
    assert p["class_count"] == 115 and p["jar_count"] == 0
    assert p["module_output_count"] == 3
    assert p["artifact_samples"] == ["target/classes/Foo.class"]
    assert p["evidence_refs"] == ["output_x"]


def test_test_payload_from_metrics_maps_runner_and_unique():
    p = _test_payload_from_metrics(METRICS)
    assert p["total"] == 18839 and p["pass"] == 18805 and p["fail"] == 5
    assert p["skip"] == 29 and p["errors"] == 0
    assert p["pass_rate"] == 99.8
    assert p["report_file_count"] == 760
    assert p["unique_total"] == 9497 and p["unique_passed"] == 9480
    assert p["declared_total"] == 20500 and p["method_execution_rate"] == 46.3
    assert p["failing_names"] == ["com.x.FooTest.testA"]
    assert p["conflicts"] == ["test_report_parse_error"]
    # execution_rate is kept as the legacy field too (method coverage)
    assert p["execution_rate"] == 46.3


def test_metrics_payloads_none_when_absent():
    assert _test_payload_from_metrics(None) is None
    assert _build_payload_from_metrics({}) is None
    assert _test_payload_from_metrics({"build": {}}) is None  # no test block


from sag.web.session_registry import _build_summary, _session_summary


def test_session_summary_test_carries_new_fields():
    item = {"id": "s", "test": _test_payload_from_metrics(METRICS)}
    summ = _session_summary(item, "sag-demo")
    d = summ.test.model_dump(mode="json", by_alias=True)
    assert d["total"] == 18839 and d["uniqueTotal"] == 9497
    assert d["reportFileCount"] == 760 and d["declaredTotal"] == 20500
    assert d["failingNames"] == ["com.x.FooTest.testA"]
    assert d["conflicts"] == ["test_report_parse_error"]


def test_build_summary_carries_new_fields():
    b = _build_summary(_build_payload_from_metrics(METRICS))
    d = b.model_dump(mode="json", by_alias=True)
    assert d["system"] == "maven" and d["classCount"] == 115 and d["jarCount"] == 0
    assert d["artifactSamples"] == ["target/classes/Foo.class"]
    assert d["evidenceRefs"] == ["output_x"]
