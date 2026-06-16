# tests/test_web_coverage_models.py
from sag.web.models import ModuleSummary, ModuleRollup


def test_module_summary_coverage_camelcase():
    m = ModuleSummary.model_validate({
        "name": "core", "path": "core",
        "line_covered": 80, "line_total": 100, "line_rate": 80.0,
        "branch_covered": 70, "branch_total": 100, "branch_rate": 70.0,
        "coverage_source": "jacoco-injected",
    })
    d = m.model_dump(mode="json", by_alias=True)
    assert d["lineRate"] == 80.0 and d["branchCovered"] == 70
    assert d["coverageSource"] == "jacoco-injected"


def test_rollup_coverage_defaults_null():
    r = ModuleRollup.model_validate({"modules_total": 2})
    d = r.model_dump(mode="json", by_alias=True)
    assert d["lineRate"] is None and d["branchRate"] is None and d["coverageSource"] is None
