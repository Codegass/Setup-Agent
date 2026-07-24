"""Flat CSV mirror of module_metrics: header + the two derived columns."""

import csv
import io

from sag.tools.module_metrics import _CSV_COLUMNS, module_metrics_to_csv


def _rows(metrics):
    return list(csv.DictReader(io.StringIO(module_metrics_to_csv(metrics))))


def test_header_and_derived_columns():
    metrics = {
        "module_summary": {"build_systems": ["maven"]},
        "modules": [
            # green module: tests passed -> test_success True, validator True
            {"path": "a", "build_status": "success", "class_count": 5,
             "java_file_count": 3, "report_file_count": 2,
             "tests_total": 10, "tests_passed": 10, "failing_count": 0},
            # failing tests -> test_success False even though build succeeded
            {"path": "b", "build_status": "success", "class_count": 1,
             "tests_total": 4, "failing_count": 1},
            # no tests, no build -> both False
            {"path": "c", "build_status": "unknown"},
        ],
    }
    out = module_metrics_to_csv(metrics)
    assert out.splitlines()[0].split(",") == _CSV_COLUMNS

    a, b, c = _rows(metrics)
    assert a["build_system"] == "maven"
    assert (a["test_success"], a["physical_validator_passed"]) == ("True", "True")
    assert a["report_file_count"] == "2" and a["java_file_count"] == "3"
    assert b["test_success"] == "False" and b["physical_validator_passed"] == "True"
    assert c["test_success"] == "False" and c["physical_validator_passed"] == "False"
    assert c["parse_errors"] == "0"  # constant until SAG tracks it per module
