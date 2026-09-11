"""Real producer XML plus fault injection, independent of model conclusions."""

import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from test_report_format_reconciliation import parse
from sag.agent.receipt_test_rows import (
    TestcaseRowContractError as RowError,
    aggregate_testcase_execution_rows,
    seal_testcase_execution_rows,
    validate_testcase_execution_row,
)

FIXTURES = Path(__file__).parent / "fixtures/surefire_retry"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def fixture(name):
    return (FIXTURES / name).read_text()


def seal(parsed, tmp_path, *, receipt_id="inv-maven-retry-0001"):
    return seal_testcase_execution_rows(
        parsed,
        run_id="retry-producer",
        receipt_id=receipt_id,
        tool="maven",
        target_sha="a" * 40,
        domain_id=str(tmp_path),
        working_directory=str(tmp_path),
    )


@pytest.mark.parametrize("record", PROVENANCE, ids=lambda r: r["fixture"])
def test_real_surefire_retry_reports_preserve_all_cases_and_final_outcomes(tmp_path, record):
    body = fixture(record["fixture"])
    assert hashlib.sha256(body.encode()).hexdigest() == record["sha256"]
    result = parse(tmp_path, {"TEST-probe.xml": body})
    assert result["status"] == "complete", result["reasons"]
    assert result["execution_totals"]["reported"] == record["logical_cases"]
    expected = []
    for case in ET.fromstring(body).findall("testcase"):
        tags = [child.tag for child in case]
        outcome = (
            "error"
            if "error" in tags
            else "failed" if "failure" in tags else "skipped" if "skipped" in tags else "passed"
        )
        retries = sum(
            tag in {"flakyFailure", "flakyError", "rerunFailure", "rerunError"} for tag in tags
        )
        expected.append((case.get("name"), outcome, retries))
    assert sorted(
        (r["name"], r["outcome"], r.get("runner_reruns", 0)) for r in result["rows"]
    ) == sorted(expected)
    sealed = seal(result, tmp_path)
    assert sealed["status"] == "complete"
    for row in sealed["rows"]:
        assert validate_testcase_execution_row(row) == row
    counts = aggregate_testcase_execution_rows(sealed["rows"])
    assert counts["claimed"]["latest_cases"]["executed"] == record["logical_cases"]
    assert counts["claimed"]["receipt_executions"]["executed"] == record["logical_cases"]
    assert counts["retried_cases"] == sum(retries > 0 for _, _, retries in expected)
    assert counts["flaky_cases"] == sum(
        outcome == "passed" and retries > 0 for _, outcome, retries in expected
    )
    assert bool(
        counts["claimed"]["latest_cases"]["failed"] + counts["claimed"]["latest_cases"]["errors"]
    ) == bool(record["native_exit"])


@pytest.mark.parametrize("declared", ["0", "2", "6", "bad", "-1"])
def test_retry_history_does_not_excuse_unexplained_declarations(tmp_path, declared):
    body = fixture("v2-m5-split-retry-pass.xml").replace('tests="1"', f'tests="{declared}"')
    result = parse(tmp_path, {"TEST-probe.xml": body})
    assert result["status"] == "unavailable"
    assert "declared_testcase_count_mismatch" in result["reasons"]
    assert "execution_totals" not in result


def test_only_an_identified_surefire_report_can_use_retry_subset_declarations(tmp_path):
    body = fixture("v2-m5-split-retry-pass.xml").replace(
        "https://maven.apache.org/surefire/", "https://unknown.example/"
    )
    result = parse(tmp_path, {"TEST-probe.xml": body})
    assert result["status"] == "unavailable"
    assert "declared_testcase_count_mismatch" in result["reasons"]


@pytest.mark.parametrize(
    "child", ["<rerunFailure/>", "<flakyFailure/><failure/>", "<flakyError/><skipped/>"]
)
def test_impossible_retry_histories_cannot_turn_into_passed_cases(tmp_path, child):
    root = ET.fromstring(fixture("v1-m5-green.xml"))
    first = root.find("testcase")
    first.extend(ET.fromstring(f"<parts>{child}</parts>"))
    result = parse(tmp_path, {"TEST-probe.xml": ET.tostring(root, encoding="unicode")})
    assert result["status"] == "unavailable"
    assert "surefire_retry_history_invalid" in result["reasons"]
    assert "execution_totals" not in result


def test_nested_outer_declaration_still_needs_to_cover_all_children(tmp_path):
    inner = fixture("v1-m5-failure-with-retry.xml").split("?>", 1)[-1]
    good = parse(
        tmp_path / "good", {"TEST-probe.xml": f'<testsuites tests="3">{inner}</testsuites>'}
    )
    bad = parse(tmp_path / "bad", {"TEST-probe.xml": f'<testsuites tests="1">{inner}</testsuites>'})
    assert good["status"] == "complete"
    assert good["execution_totals"]["reported"] == 3
    assert bad["status"] == "unavailable"


@pytest.mark.parametrize("value", [True, 0, -1, "1", None])
def test_sealed_runner_retry_count_is_typed_and_positive(tmp_path, value):
    parsed = parse(tmp_path, {"TEST-probe.xml": fixture("v1-m5-green.xml")})
    row = seal(parsed, tmp_path)["rows"][0]
    row["runner_reruns"] = value
    with pytest.raises(RowError, match="runner_reruns"):
        validate_testcase_execution_row(row)


def test_native_retry_and_repeated_receipt_do_not_inflate_logical_counts(tmp_path):
    parsed = parse(tmp_path, {"TEST-probe.xml": fixture("v2-m5-split-retry-pass.xml")})
    envelope = seal(parsed, tmp_path)
    counts = aggregate_testcase_execution_rows(envelope["rows"] * 2)
    assert counts["claimed"]["receipt_executions"]["executed"] == 5
    assert counts["retried_cases"] == counts["flaky_cases"] == 2


def test_a_later_clean_pass_does_not_erase_prior_flaky_history(tmp_path):
    flaky = seal(
        parse(tmp_path, {"TEST-probe.xml": fixture("v1-m5-failure-with-retry.xml")}), tmp_path
    )
    clean = seal(
        parse(tmp_path, {"TEST-probe.xml": fixture("v1-m5-green.xml")}),
        tmp_path,
        receipt_id="inv-maven-retry-0002",
    )
    counts = aggregate_testcase_execution_rows(flaky["rows"] + clean["rows"])
    assert counts["claimed"]["latest_cases"]["executed"] == 3
    assert counts["claimed"]["receipt_executions"]["executed"] == 6
    assert counts["flaky_cases"] == 1


def test_a_later_red_case_is_not_reported_as_flaky_pass(tmp_path):
    flaky = seal(
        parse(tmp_path, {"TEST-probe.xml": fixture("v1-m5-failure-with-retry.xml")}), tmp_path
    )
    red = seal(
        parse(tmp_path, {"TEST-probe.xml": fixture("v1-m5-failure-no-retry.xml")}),
        tmp_path,
        receipt_id="inv-maven-retry-0002",
    )
    counts = aggregate_testcase_execution_rows(flaky["rows"] + red["rows"])
    assert counts["claimed"]["latest_cases"]["failed"] == 1
    assert counts["flaky_cases"] == 0
