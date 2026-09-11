"""A later invocation must not erase the exact XML proving an earlier task."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_fixed_command_verification as fixed
from test_ci_comparison import REPORT, Runner, XML, compare

from sag.agent import receipt_test_rows as reader


def parse(tmp_path, reports, *, capture=False):
    manifest = tmp_path / "claims.json"
    manifest.write_text(json.dumps(reports))
    result = subprocess.run(
        [sys.executable, "-c", reader._row_parser_program(capture_reports=capture), str(manifest)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "snapshots"
    monkeypatch.setattr(reader, "REPORT_SNAPSHOT_DIR", str(path))
    return path


def claim(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()}


def test_two_actual_xml_versions_remain_separate_and_keep_their_logical_path(tmp_path, archive):
    path = tmp_path / "target/surefire-reports/TEST-a.xml"
    first = claim(path, XML)
    assert parse(tmp_path, [first], capture=True)["execution_totals"]["passed"] == 1
    second_xml = XML.replace(
        'name="one"/>', 'name="one"><failure message="red"/></testcase>'
    ).replace('failures="0"', 'failures="1"')
    second = claim(path, second_xml)
    assert parse(tmp_path, [second], capture=True)["execution_totals"]["failed"] == 1
    old = parse(tmp_path, [first])
    new = parse(tmp_path, [second])
    assert old["execution_totals"]["passed"] == 1
    assert new["execution_totals"]["failed"] == 1
    assert old["rows"][0]["report_path"] == new["rows"][0]["report_path"] == str(path)
    assert old["rows"][0]["report_sha256"] == first["sha256"]
    assert len(list(archive.glob("*.xml"))) == 2


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
def test_unavailable_or_changed_backup_cannot_supply_evidence(tmp_path, archive, damage):
    path = tmp_path / "TEST-a.xml"
    original = claim(path, XML)
    parse(tmp_path, [original], capture=True)
    saved = archive / (original["sha256"] + ".xml")
    path.write_text(XML.replace('name="one"', 'name="other"'))
    if damage == "corrupt":
        saved.write_text("<testsuite tests='0'/>")
    else:
        saved.unlink()
        if damage == "symlink":
            saved.symlink_to(path)
    result = parse(tmp_path, [original])
    assert result["status"] == "unavailable"
    assert not result["rows"]


def test_reading_current_reports_does_not_retroactively_create_an_archive(tmp_path, archive):
    original = claim(tmp_path / "TEST-a.xml", XML)
    assert parse(tmp_path, [original])["status"] == "complete"
    assert not archive.exists()


def test_retention_has_a_disk_budget_and_never_substitutes_counts(tmp_path, archive, monkeypatch):
    monkeypatch.setattr(reader, "REPORT_SNAPSHOT_MAX_BYTES", 1)
    path = tmp_path / "TEST-a.xml"
    original = claim(path, XML)
    result = parse(tmp_path, [original], capture=True)
    assert result["status"] == "complete"  # The live report is still available.
    assert result["report_snapshot_errors"] == ["report_snapshot_budget_exceeded"]
    assert not list(archive.glob("*.xml"))
    path.unlink()
    assert parse(tmp_path, [original])["status"] == "unavailable"


def test_report_size_bound_also_limits_retention(tmp_path, archive, monkeypatch):
    monkeypatch.setattr(reader, "DELTA_REPORT_MAX_BYTES", 16)
    original = claim(tmp_path / "TEST-a.xml", XML)
    result = parse(tmp_path, [original], capture=True)
    assert result["bounds"]["unparsed_reports"] == 1
    assert not archive.exists()


def test_an_existing_corrupt_snapshot_is_not_silently_replaced(tmp_path, archive):
    original = claim(tmp_path / "TEST-a.xml", XML)
    archive.mkdir()
    saved = archive / (original["sha256"] + ".xml")
    saved.write_text("corrupt")
    result = parse(tmp_path, [original], capture=True)
    assert result["report_snapshot_errors"] == ["report_snapshot_conflict"]
    assert saved.read_text() == "corrupt"


def test_narrower_rerun_with_new_xml_does_not_erase_the_completed_ci_task(archive, monkeypatch):
    run = fixed.fixed_run()
    assert fixed.dispatch(run, run.target.execution_command, suffix="original").succeeded
    before = compare(run)
    assert before.attainment.verdict == "met"
    assert list(archive.glob("*.xml"))
    monkeypatch.setattr(
        fixed,
        "Runner",
        lambda fs: Runner(fs, xml=XML.replace("<testsuite ", '<testsuite time="0.5" ')),
    )
    assert fixed.dispatch(run, "mvn -B test", suffix="narrower").succeeded
    after = compare(run)
    assert after.attainment.verdict == "met", after
    assert after.receipt_ids == before.receipt_ids
    assert after.certificate.test_counts.reported == 1
    assert after.commands == before.commands


def test_corrupt_retained_xml_cannot_discharge_the_original_ci_command(archive, monkeypatch):
    run = fixed.fixed_run()
    assert fixed.dispatch(run, run.target.execution_command, suffix="1").succeeded
    saved = list(archive.glob("*.xml"))
    monkeypatch.setattr(
        fixed,
        "Runner",
        lambda fs: Runner(fs, xml=XML.replace("<testsuite ", '<testsuite time="0.5" ')),
    )
    assert fixed.dispatch(run, "mvn -B test", suffix="2").succeeded
    for path in saved:
        path.write_text("corrupt")
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None


def test_old_green_snapshot_cannot_hide_a_later_failed_ci_invocation(archive, monkeypatch):
    run = fixed.fixed_run()
    assert fixed.dispatch(run, run.target.execution_command, suffix="1").succeeded
    red = XML.replace('failures="0"', 'failures="1"').replace(
        "/></testsuite>", '><failure message="wrong"/></testcase></testsuite>'
    )
    monkeypatch.setattr(fixed, "Runner", lambda fs: Runner(fs, xml=red, exit_code=1))
    assert not fixed.dispatch(run, run.target.execution_command, suffix="2").succeeded
    result = compare(run)
    assert result.attainment.verdict == "not_met", result
    assert result.certificate.test_counts.failed == 1
    assert len(list(archive.glob("*.xml"))) == 2


@pytest.mark.parametrize("remove", ["all_report_bytes", "assessment", "pin", "contract"])
def test_retained_xml_does_not_replace_other_required_proof(archive, monkeypatch, remove):
    run = fixed.fixed_run()
    assert fixed.dispatch(run, run.target.execution_command).succeeded
    assert compare(run).attainment.verdict == "met"
    if remove == "all_report_bytes":
        del run.fs.files[REPORT]
        for path in archive.glob("*.xml"):
            path.unlink()
    else:
        marker = {
            "assessment": "/evidence_assessments/",
            "pin": "run-pin.json",
            "contract": "/invocation_contracts/",
        }[remove]
        for path in list(run.fs.files):
            if marker in path:
                del run.fs.files[path]
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None


def test_retention_failure_is_visible_in_the_tool_response(archive, monkeypatch):
    monkeypatch.setattr(reader, "REPORT_SNAPSHOT_MAX_BYTES", 1)
    run = fixed.fixed_run()
    result = fixed.dispatch(run, run.target.execution_command)
    assert result.succeeded
    assert result.metadata["report_snapshot_errors"] == ["report_snapshot_budget_exceeded"]
    assert "could not be retained" in result.output
