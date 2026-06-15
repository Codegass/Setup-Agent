# tests/test_web_module_models.py
from sag.web.models import ModuleSummary, ModuleRollup


def test_module_summary_round_trips_camelcase():
    m = ModuleSummary.model_validate({
        "name": "connect:api", "path": "connect/api",
        "build_status": "success", "build_source": "reactor",
        "class_count": 180, "jar_count": 3,
        "tests_total": 198, "tests_passed": 198, "tests_failed": 0,
        "failing_names": [], "failing_count": 0,
        "evidence_refs": ["/w/connect/api/target/surefire-reports"],
    })
    d = m.model_dump(mode="json", by_alias=True)
    assert d["buildStatus"] == "success"
    assert d["classCount"] == 180
    assert d["testsTotal"] == 198
    assert d["failingCount"] == 0
    assert d["evidenceRefs"] == ["/w/connect/api/target/surefire-reports"]


def test_module_rollup_camelcase():
    r = ModuleRollup.model_validate({
        "modules_total": 24, "modules_built": 21, "modules_failed": 1,
        "modules_skipped": 2, "modules_with_test_failures": 2,
        "build_systems": ["maven"], "single_module": False,
    })
    d = r.model_dump(mode="json", by_alias=True)
    assert d["modulesTotal"] == 24 and d["modulesWithTestFailures"] == 2
    assert d["buildSystems"] == ["maven"] and d["singleModule"] is False
