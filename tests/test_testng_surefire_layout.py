"""TestNG under surefire writes one run three ways; the receipt counts it once.

casbin-jcasbin-r2 (benchmark-fifty-20261021) ran 266 TestNG tests under
surefire 3.2.5. Its report delta held TestNG's native `testng-results.xml`,
TestNG's two JUnit-style copies (`Surefire suite/Surefire test.xml` and one
`junitreports/TEST-<class>.xml` per class), and surefire's own copy of the same
run, `TEST-TestSuite.xml`. Surefire's copy carries every test's logged output
(the project's CI log holds ~19 MB of it), so it was over the reader's parse
bound (`DELTA_REPORT_MAX_BYTES`): the reader hashed it, never parsed it, and
disclosed it as a dropped file with no claim of red completeness. The test
gate then could not grade a green run whose every execution was already
carried by the files it did read.

The trimmed TestNG files under `fixtures/casbin_testng` are that run's
retained bytes (`sources.json` says how they were cut). Surefire's copy was
never retained — only parsed reports are — so `surefire_copy` below writes
surefire 3.2.5's format for the same identities.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sag.agent import invocation_receipts
from sag.agent.receipt_test_rows import DELTA_REPORT_MAX_BYTES, _row_parser_program

FIXTURES = Path(__file__).parent / "fixtures/casbin_testng"
REPORTS = "workspace/casbin-jcasbin/target/surefire-reports"
NATIVE = "testng-results.xml"
TESTNG_SUITE = "Surefire suite/Surefire test.xml"
SUREFIRE = "TEST-TestSuite.xml"
JUNIT_REPORTS = sorted(
    str(path.relative_to(FIXTURES)) for path in (FIXTURES / "junitreports").glob("*.xml")
)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def identities(suite_xml):
    return re.findall(r'<testcase name="([^"]+)" time="[^"]+" classname="([^"]+)"/>', suite_xml)


def surefire_copy(cases, *, logged_bytes=0, red=None):
    """Surefire 3.2.5's `TEST-TestSuite.xml` for the TestNG run it hosted.

    `cases` are (name, classname) pairs. Each testcase carries its logged
    output in `system-out`, which is where the casbin file's size lived.
    `red` names one case that failed, with surefire's failure element.
    """

    output = "x" * (logged_bytes // max(len(cases), 1) + 1)
    failures = sum(name == red for name, _ in cases)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<testsuite xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="https://maven.apache.org/surefire/'
        'maven-surefire-plugin/xsd/surefire-test-report-3.0.xsd" version="3.0"'
        f' name="TestSuite" time="54.997" tests="{len(cases)}" errors="0"'
        f' skipped="0" failures="{failures}">',
        "  <properties>",
        '    <property name="surefire.version" value="3.2.5"/>',
        "  </properties>",
    ]
    for name, classname in cases:
        lines.append(f'  <testcase name="{name}" classname="{classname}" time="0.001">')
        if name == red:
            lines.append(
                '    <failure message="expected [true] but found [false]"'
                ' type="java.lang.AssertionError">java.lang.AssertionError</failure>'
            )
        lines.append(f"    <system-out><![CDATA[{output}]]></system-out>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def casbin_files(**surefire):
    files = {NATIVE: fixture(NATIVE), TESTNG_SUITE: fixture(TESTNG_SUITE)}
    files.update({name: fixture(name) for name in JUNIT_REPORTS})
    files[SUREFIRE] = surefire_copy(identities(files[TESTNG_SUITE]), **surefire)
    return files


def parse(tmp_path, files):
    """The shipped container program over these files, in delta order."""

    claims = []
    for name, body in sorted(files.items()):
        path = tmp_path / REPORTS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        claims.append({"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()})
    manifest = tmp_path / "claims.json"
    manifest.write_text(json.dumps(claims))
    completed = subprocess.run(
        [sys.executable, "-c", _row_parser_program(), str(manifest)],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def disclosure(parsed):
    return invocation_receipts.disclose_row_bounds({**parsed, "row_bounds": parsed["bounds"]})


def carried(parsed):
    return sorted((row["classname"], row["name"], row["outcome"]) for row in parsed["rows"])


def test_the_fixture_is_the_run_s_own_testng_layout():
    native = fixture(NATIVE)
    assert 'total="10" passed="10"' in native
    # Six configuration methods (`setUp`) ride beside the ten tests.
    assert native.count('is-config="true"') == 6
    assert len(identities(fixture(TESTNG_SUITE))) == 10
    assert len(JUNIT_REPORTS) == 3


def test_an_oversize_surefire_copy_of_the_testng_run_is_not_a_dropped_file(tmp_path):
    files = casbin_files(logged_bytes=DELTA_REPORT_MAX_BYTES)
    # The rule that dropped it in the run: over the parse bound.
    assert len(files[SUREFIRE].encode()) > DELTA_REPORT_MAX_BYTES

    parsed = parse(tmp_path, files)

    assert parsed["status"] == "complete" and parsed["reasons"] == []
    assert parsed["report_count"] == len(files)
    assert len(parsed["rows"]) == 10
    assert len(set(carried(parsed))) == 10
    assert parsed["bounds"]["unparsed_reports"] == 0
    assert parsed["bounds"]["dropped_files"] == 0
    assert parsed["execution_totals"] == {
        "reported": 10,
        "passed": 10,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    # Nothing was lost, so there is no loss to disclose.
    assert disclosure(parsed) is None


def test_a_surefire_copy_inside_the_bound_is_not_a_second_run(tmp_path):
    parsed = parse(tmp_path, casbin_files())

    assert parsed["status"] == "complete"
    assert len(parsed["rows"]) == 10
    assert parsed["execution_totals"]["reported"] == 10
    assert parsed["bounds"]["observed_rows"] == 10


@pytest.mark.parametrize("logged_bytes", [0, DELTA_REPORT_MAX_BYTES])
def test_a_red_in_the_testng_run_is_carried_once(tmp_path, logged_bytes):
    red = "testEval"
    message = 'message="expected [true] but found [false]"'
    files = casbin_files(logged_bytes=logged_bytes, red=red)
    files[NATIVE] = (
        files[NATIVE]
        .replace(
            'failed="0" ignored="0" total="10" passed="10"',
            'failed="1" ignored="0" total="10" passed="9"',
        )
        .replace(f'status="PASS" signature="{red}()', f'status="FAIL" signature="{red}()', 1)
    )
    files[TESTNG_SUITE] = (
        files[TESTNG_SUITE]
        .replace('failures="0"', 'failures="1"', 1)
        .replace(
            f'<testcase name="{red}" time="0.023" classname="org.casbin.jcasbin.main.AbacAPIUnitTest"/>',
            f'<testcase name="{red}" time="0.023" classname="org.casbin.jcasbin.main.AbacAPIUnitTest">'
            f'<failure {message} type="java.lang.AssertionError"/></testcase>',
        )
    )
    # TestNG's per-class copy of the red class would restate the red too; the
    # two copies above are enough to show it is carried once.
    for name in JUNIT_REPORTS:
        if "AbacAPIUnitTest" in name:
            del files[name]

    parsed = parse(tmp_path, files)

    assert parsed["status"] == "complete" and parsed["reasons"] == []
    assert [row for row in carried(parsed) if row[2] == "failed"] == [
        ("org.casbin.jcasbin.main.AbacAPIUnitTest", red, "failed")
    ]
    assert len(parsed["rows"]) == 10
    assert parsed["execution_totals"]["failed"] == 1
    assert parsed["execution_totals"]["reported"] == 10
    assert disclosure(parsed) is None


def test_an_oversize_surefire_report_with_no_testng_record_stays_a_dropped_file(tmp_path):
    """With TestNG's own listeners off, surefire's copy is the only record."""

    cases = identities(fixture(TESTNG_SUITE))
    parsed = parse(tmp_path, {SUREFIRE: surefire_copy(cases, logged_bytes=DELTA_REPORT_MAX_BYTES)})

    assert parsed["bounds"]["unparsed_reports"] == 1
    assert parsed["bounds"]["dropped_files"] == 1
    assert "execution_totals" not in parsed
    assert disclosure(parsed)["red_rows_complete"] is False


@pytest.mark.parametrize("shape", ["one more testcase", "one testcase replaced"])
def test_an_oversize_surefire_report_stating_a_red_testng_did_not_record_is_not_dropped_silently(
    tmp_path, shape
):
    """A configuration failure surefire reports as a test, beside a green record.

    The copy states an execution the native record does not hold, so it is
    not a restatement of that record. It stays what the parse bound makes it:
    a dropped file, with no claim that every red is carried. With one more
    testcase than the record the markup read stops at it; with the same count
    the identities do not match.
    """

    files = casbin_files()
    cases = identities(files[TESTNG_SUITE])
    if shape == "one testcase replaced":
        cases = cases[:-1]
    cases.append(("setUp", "org.casbin.jcasbin.main.LogUtilTest"))
    files[SUREFIRE] = surefire_copy(cases, logged_bytes=DELTA_REPORT_MAX_BYTES, red="setUp")

    parsed = parse(tmp_path, files)

    assert parsed["status"] == "complete"
    assert parsed["bounds"]["unparsed_reports"] == 1
    assert parsed["bounds"]["dropped_files"] == 1
    assert "execution_totals" not in parsed
    assert disclosure(parsed)["red_rows_complete"] is False


def test_an_oversize_surefire_copy_contradicting_its_own_header_stays_a_dropped_file(tmp_path):
    """The markup read only ever turns a dropped file into a verified copy."""

    files = casbin_files(logged_bytes=DELTA_REPORT_MAX_BYTES)
    files[SUREFIRE] = files[SUREFIRE].replace('tests="10"', 'tests="9"', 1)

    parsed = parse(tmp_path, files)

    assert parsed["status"] == "complete" and parsed["reasons"] == []
    assert parsed["bounds"]["unparsed_reports"] == 1
    assert parsed["bounds"]["dropped_files"] == 1
    assert disclosure(parsed)["red_rows_complete"] is False


def test_a_surefire_report_inside_the_bound_stating_a_red_testng_did_not_record_keeps_it(
    tmp_path,
):
    files = casbin_files()
    cases = identities(files[TESTNG_SUITE]) + [("setUp", "org.casbin.jcasbin.main.LogUtilTest")]
    files[SUREFIRE] = surefire_copy(cases, red="setUp")

    parsed = parse(tmp_path, files)

    assert parsed["status"] == "complete"
    assert ("org.casbin.jcasbin.main.LogUtilTest", "setUp", "failed") in carried(parsed)
    assert parsed["execution_totals"]["failed"] == 1
