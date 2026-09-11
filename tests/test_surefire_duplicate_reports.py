"""Surefire counts distinct cases while retaining duplicate physical runs."""

import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from test_report_format_reconciliation import parse
from test_surefire_retry_reports import seal
from sag.agent.receipt_test_rows import aggregate_testcase_execution_rows

FIXTURES = Path(__file__).parent / "fixtures/surefire_duplicates"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def fixture(name="duplicate-pass.xml"):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("record", PROVENANCE, ids=lambda r: r["fixture"])
def test_native_duplicate_reports_keep_physical_runs_and_worst_logical_outcome(tmp_path, record):
    body = fixture(record["fixture"])
    assert hashlib.sha256(body.encode()).hexdigest() == record["sha256"]
    parsed = parse(tmp_path, {"TEST-probe.xml": body})
    assert parsed["status"] == "complete", parsed["reasons"]
    assert parsed["execution_totals"]["reported"] == record["physical_executions"]
    counts = aggregate_testcase_execution_rows(seal(parsed, tmp_path)["rows"])
    assert counts["claimed"]["receipt_executions"]["executed"] == record["physical_executions"]
    latest = counts["claimed"]["latest_cases"]
    assert latest["executed"] == record["logical_cases"]
    # A later pass of the same test in this receipt never erases an earlier red.
    assert latest["failed"] + latest["errors"] == record["native_exit"]
    assert latest["passed"] == record["logical_cases"] - record["native_exit"]


@pytest.mark.parametrize("declared", ["0", "1", "4", "-1", "bad"])
def test_duplicate_cases_do_not_excuse_unexplained_header_counts(tmp_path, declared):
    body = fixture().replace('tests="2"', f'tests="{declared}"')
    parsed = parse(tmp_path, {"TEST-probe.xml": body})
    assert parsed["status"] == "unavailable"
    assert "declared_testcase_count_mismatch" in parsed["reasons"]
    assert "execution_totals" not in parsed


@pytest.mark.parametrize(
    "fault", ["unknown_schema", "missing_class", "missing_name", "different_class"]
)
def test_unique_case_header_needs_official_schema_and_exact_complete_identities(tmp_path, fault):
    root = ET.fromstring(fixture())
    if fault == "unknown_schema":
        root.set("{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation", "unknown")
    elif fault == "missing_class":
        root.find("testcase").set("classname", "")
    elif fault == "missing_name":
        root.find("testcase").set("name", " ")
    else:
        root.find("testcase").set("classname", "DifferentClass")
    parsed = parse(tmp_path, {"TEST-probe.xml": ET.tostring(root, encoding="unicode")})
    assert parsed["status"] == "unavailable"
    assert "declared_testcase_count_mismatch" in parsed["reasons"]
    assert "execution_totals" not in parsed


def test_outer_total_must_still_cover_duplicate_physical_executions(tmp_path):
    inner = fixture().split("?>", 1)[-1]
    good = parse(
        tmp_path / "good", {"TEST-probe.xml": f'<testsuites tests="3">{inner}</testsuites>'}
    )
    bad = parse(tmp_path / "bad", {"TEST-probe.xml": f'<testsuites tests="2">{inner}</testsuites>'})
    assert good["status"] == "complete"
    assert good["execution_totals"]["reported"] == 3
    assert bad["status"] == "unavailable"
    assert "execution_totals" not in bad
