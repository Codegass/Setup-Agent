# tests/test_coverage_merge.py
from sag.coverage.merge import merge_coverage_into_metrics


def _metrics():
    return {
        "version": 1,
        "module_summary": {"modules_total": 3, "build_systems": ["gradle"]},
        "modules": [
            {"name": "core", "path": "core", "build_status": "success"},
            {"name": "io", "path": "io", "build_status": "success"},
            {"name": "examples", "path": "examples", "build_status": "unknown"},
        ],
    }


def test_merges_by_path_and_weights_rollup():
    cov_map = {
        "core": {"line_covered": 80, "line_total": 100, "line_rate": 80.0,
                 "branch_covered": 70, "branch_total": 100, "branch_rate": 70.0,
                 "coverage_source": "jacoco-injected"},
        "io": {"line_covered": 30, "line_total": 100, "line_rate": 30.0,
               "branch_covered": 10, "branch_total": 50, "branch_rate": 20.0,
               "coverage_source": "jacoco-injected"},
    }
    out = merge_coverage_into_metrics(_metrics(), cov_map)
    by_path = {m["path"]: m for m in out["modules"]}
    assert by_path["core"]["line_rate"] == 80.0
    assert by_path["io"]["branch_covered"] == 10
    # examples had no coverage -> fields stay absent/None
    assert by_path["examples"].get("line_rate") is None
    s = out["module_summary"]
    # lines-weighted: (80+30)/(100+100) = 55.0 ; branch (70+10)/(100+50)=53.3
    assert s["line_covered"] == 110 and s["line_total"] == 200 and s["line_rate"] == 55.0
    assert s["branch_total"] == 150 and s["branch_rate"] == 53.3
    assert s["coverage_source"] == "jacoco-injected"


def test_no_coverage_leaves_rollup_null():
    out = merge_coverage_into_metrics(_metrics(), {})
    s = out["module_summary"]
    assert s["line_rate"] is None and s["branch_rate"] is None
    assert s["coverage_source"] is None
