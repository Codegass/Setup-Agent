import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_container_io import FakeContainer
from test_invocation_receipts import HASH_A, FakeExecute, ok, receipts_written

from sag.agent import invocation_receipts
from sag.agent.invocation_receipts import (
    REPORT_TAG_MARKER,
    assemble_gradle_test_rows,
    parse_report_tag_rows,
)
from sag.agent.receipt_test_rows import (
    DELTA_PER_FILE_ROW_CAP,
    DELTA_REPORT_MAX_BYTES,
    DELTA_ROW_MAX_JSON_BYTES,
    DELTA_TESTCASE_ROW_CAP,
    SEALED_ROW_OVERHEAD_MAX_BYTES,
    _row_parser_program,
    bound_testcase_rows,
    diagnostic_testcase_outcomes,
    read_delta_testcase_rows,
    read_gradle_project_map,
    seal_testcase_execution_rows,
)
from sag.agent.receipt_test_rows import testcase_execution_id as _testcase_execution_id
from sag.agent.receipt_test_rows import (
    validate_testcase_execution_row,
)

RUN_ID = "run-pytest"


def _surefire_claims(count, root="/workspace/proj"):
    """`{path: sha}` for `count` more claimed reports under one reactor.

    A disclosure's `dropped_files` counts reports THIS receipt claims, so a
    stated loss at reactor scale needs a claim set at reactor scale beside it
    (plan r2 T3).
    """

    return {
        f"{root}/m{index // 100}/target/surefire-reports/TEST-c{index:04d}.xml": f"{index:064x}"
        for index in range(count)
    }


def _parsed(path, *, classname="com.acme.SharedTest", name="roundTrip[size=1]"):
    return {
        "schema_version": 2,
        "status": "complete",
        "report_count": 1,
        "rows": [
            {
                "report_path": path,
                "report_sha256": HASH_A,
                "classname": classname,
                "name": name,
                "source_file": None,
                "outcome": "passed",
                "execution_ordinal": 1,
            }
        ],
    }


def test_maven_rows_use_the_hash_bound_relative_module_root_and_parameter_identity():
    path = "/workspace/proj/alpha/target/surefire-reports/TEST-com.acme.SharedTest.xml"

    envelope = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0007",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )

    assert envelope["status"] == "complete"
    assert envelope["report_count"] == 1
    assert envelope["rows"] == [
        {
            "run_id": RUN_ID,
            "receipt_id": "inv-maven-test-0007",
            "execution_index": 7,
            "execution_ordinal": 1,
            "target_sha": "a" * 40,
            "domain_id": "/workspace/proj",
            "module_coordinate": "alpha",
            "framework": "junit-xml",
            "owner": "com.acme.SharedTest",
            "test_name": "roundTrip",
            "parameter_id": "size=1",
            "outcome": "passed",
            "report_path": path,
            "report_sha256": HASH_A,
            "disposition": "claimed",
            "qualifying_invocation": True,
            "execution_id": envelope["rows"][0]["execution_id"],
        }
    ]
    assert envelope["rows"][0]["execution_id"].startswith("inv-maven-test-0007#execution-")


def test_gradle_requires_its_task_stream_to_prove_the_project_path():
    path = "/workspace/proj/alpha/build/test-results/test/TEST-a.xml"

    unavailable = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="inv-gradle-test-0008",
        tool="gradle",
        target_sha="b" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )
    proven = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="inv-gradle-test-0008",
        tool="gradle",
        target_sha="b" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
        module_outcomes=[{"module": "alpha", "status": "attempted"}],
        gradle_project_map={"/workspace/proj": ":root", "/workspace/proj/alpha": ":alpha"},
    )

    assert unavailable["status"] == "unavailable"
    assert unavailable["rows"] == []
    assert proven["status"] == "complete"
    assert proven["rows"][0]["module_coordinate"] == ":alpha"


def test_python_uses_the_invocation_workspace_and_source_owner_not_the_central_report_dir():
    path = "/workspace/.setup_agent/pytest-reports/pytest-attempt-000001.xml"
    parsed = _parsed(
        path,
        classname="tests.test_api.ApiTests",
        name="test_request[https-proxy]",
    )
    parsed["rows"][0]["source_file"] = "tests/test_api.py"

    envelope = seal_testcase_execution_rows(
        parsed,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0009",
        tool="python",
        target_sha="c" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj/python",
    )

    row = envelope["rows"][0]
    assert row["module_coordinate"] == "pytest:tests/test_api.py"
    assert row["framework"] == "pytest"
    assert row["owner"] == "tests/test_api.py::tests.test_api.ApiTests"
    assert row["test_name"] == "test_request"
    assert row["parameter_id"] == "https-proxy"


def test_one_missing_identity_fact_makes_the_whole_envelope_unavailable():
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"

    envelope = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="receipt-without-sequence",
        tool="maven",
        target_sha=None,
        domain_id=None,
        working_directory="/workspace/proj",
    )

    assert envelope["status"] == "unavailable"
    assert envelope["rows"] == []
    assert set(envelope["reasons"]) >= {
        "target_sha_unavailable",
        "domain_id_unavailable",
        "invocation_sequence_unavailable",
    }


def test_python_source_outside_the_surveyed_domain_is_not_identity_proof():
    path = "/workspace/.setup_agent/pytest-reports/pytest-attempt.xml"
    escaped = _parsed(path, classname="", name="works")
    escaped["rows"][0]["source_file"] = "../outside/test_api.py"

    envelope = seal_testcase_execution_rows(
        escaped,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0020",
        tool="python",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )

    assert envelope["status"] == "unavailable"
    assert envelope["rows"] == []


def test_delta_row_reader_is_exact_across_new_changed_and_cached_reports_under_the_bound():
    class ExactParserContainer(FakeContainer):
        def __init__(self):
            super().__init__()
            self.parser_kwargs = None
            self.parser_input = None

        def execute_command(self, command, **kwargs):
            if "def declarations_agree" in command:
                self.commands.append(command)
                self.parser_kwargs = dict(kwargs)
                input_path = shlex.split(command)[-1]
                self.parser_input = json.loads(self.files[input_path])
                rows = [
                    {
                        "report_path": report["path"],
                        "report_sha256": report["sha256"],
                        "classname": "com.acme.ExactTest",
                        "name": f"case[{index}]",
                        "source_file": None,
                        "outcome": "passed",
                        "execution_ordinal": 1,
                    }
                    for index, report in enumerate(self.parser_input)
                ]
                return {
                    "exit_code": 0,
                    "output": json.dumps(
                        {
                            "status": "complete",
                            "report_count": len(rows),
                            "rows": rows,
                            "reasons": [],
                        }
                    ),
                }
            return super().execute_command(command, **kwargs)

    container = ExactParserContainer()
    entries = [
        {
            "path": f"/workspace/proj/module-{index}/target/surefire-reports/TEST-{index}.xml",
            "sha256": f"{index + 1:064x}",
        }
        for index in range(120)
    ]

    parsed = read_delta_testcase_rows(
        container.execute_command,
        receipt_id="inv-maven-test-0010",
        delta={
            "new": entries[:40],
            "changed": entries[40:80],
            "cached": entries[80:],
        },
    )

    assert parsed["status"] == "complete"
    assert parsed["report_count"] == 120
    assert len(parsed["rows"]) == 120
    assert len(container.parser_input) == 120
    assert container.parser_kwargs == {"truncate_output": False}
    assert not any(".testcase-row-input" in path for path in container.files)
    # A bound that did not bite states a bound that did not bite: every row
    # this read observed is a row it carried.
    assert parsed["row_bounds"]["observed_rows"] == parsed["row_bounds"]["kept_rows"] == 120
    assert parsed["row_bounds"]["dropped_green"] == 0
    assert parsed["row_bounds"]["dropped_files"] == 0


def test_delta_row_reader_refuses_a_path_with_conflicting_receipt_hashes():
    container = FakeContainer()
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"

    parsed = read_delta_testcase_rows(
        container.execute_command,
        receipt_id="inv-maven-test-0010",
        delta={
            "new": [{"path": path, "sha256": "a" * 64}],
            "changed": [{"path": path, "sha256": "b" * 64}],
        },
    )

    assert parsed == {
        "schema_version": 2,
        "status": "unavailable",
        "rows": [],
        "reasons": ["report_delta_conflict"],
    }
    assert not any("def declarations_agree" in command for command in container.commands)


def test_container_parser_verifies_hash_and_inherits_the_nearest_suite_source_file(tmp_path):
    report = tmp_path / "TEST-exact.xml"
    body = (
        '<testsuites><testsuite tests="2" file="tests/test_api.py">'
        '<testcase classname="tests.Api" name="works[x]"/>'
        '<testcase name="collection"><error message="collection failure"/></testcase>'
        "</testsuite></testsuites>"
    )
    report.write_text(body)
    manifest = tmp_path / "reports.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "path": str(report),
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            ]
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", _row_parser_program(), str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(completed.stdout)

    assert parsed["status"] == "complete"
    assert parsed["report_count"] == 1
    assert parsed["rows"] == [
        {
            "report_path": str(report),
            "report_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "classname": "tests.Api",
            "name": "works[x]",
            "source_file": "tests/test_api.py",
            "outcome": "passed",
            "reason": None,
            "execution_ordinal": 1,
        }
    ]


def test_container_parser_prefers_the_nearest_nested_suite_source_file(tmp_path):
    report = tmp_path / "TEST-nested.xml"
    body = (
        '<testsuites><testsuite tests="1" file="outer.py">'
        '<testsuite tests="1" file="inner.py">'
        '<testcase classname="tests.Api" name="works"/>'
        "</testsuite></testsuite></testsuites>"
    )
    report.write_text(body)
    manifest = tmp_path / "reports.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "path": str(report),
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            ]
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", _row_parser_program(), str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["rows"][0]["source_file"] == "inner.py"


def test_an_incomplete_parse_never_exposes_partially_sealed_rows():
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    parsed = _parsed(path)
    parsed["status"] = "unavailable"
    parsed["reasons"] = ["report_unreadable"]

    envelope = seal_testcase_execution_rows(
        parsed,
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0012",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )

    assert envelope["status"] == "unavailable"
    assert envelope["rows"] == []


def test_exact_parse_projects_the_existing_bounded_failure_first_diagnostic_view():
    parsed = {
        "status": "complete",
        "rows": [
            {
                "classname": "a.Suite",
                "name": f"case_{index}",
                "outcome": "passed",
            }
            for index in range(55)
        ]
        + [
            {
                "classname": "a.Suite",
                "name": "broken",
                "outcome": "failed",
                "reason": "  assertion   failed  ",
            }
        ],
    }

    diagnostic = diagnostic_testcase_outcomes(parsed)

    assert len(diagnostic["nodes"]) == 50
    assert diagnostic["truncated"] is True
    assert diagnostic["nodes"][0] == {
        "node_id": "a.Suite#broken",
        "status": "failed",
        "reason": "assertion failed",
    }


def test_record_invocation_atomically_embeds_the_exact_row_envelope(monkeypatch):
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    execute = FakeExecute(
        rules=[
            ("rev-parse HEAD", ok("d" * 40)),
            ("command -v", ok("/usr/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9\n")),
        ]
    )
    monkeypatch.setattr(
        invocation_receipts,
        "read_delta_testcase_rows",
        lambda *_args, **_kwargs: _parsed(path),
    )

    metadata = invocation_receipts.record_invocation(
        execute,
        receipt_id="inv-maven-test-0011",
        run_id=RUN_ID,
        tool="maven",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={path: HASH_A},
        requirements={"build_domains": [{"root": "/workspace/proj", "system": "maven"}]},
    )

    assert metadata == {"receipt_id": "inv-maven-test-0011"}
    (receipt,) = receipts_written(execute.commands)
    envelope = receipt["testcase_execution_rows"]
    assert envelope["status"] == "complete"
    assert envelope["rows"][0]["target_sha"] == "d" * 40
    assert envelope["rows"][0]["domain_id"] == "/workspace/proj"
    assert json.loads(json.dumps(receipt))["testcase_execution_rows"] == envelope


def test_declared_count_mismatch_invalidates_the_entire_report_envelope(tmp_path):
    report = tmp_path / "TEST-truncated.xml"
    body = '<testsuite tests="2"><testcase classname="a.T" name="only"/></testsuite>'
    report.write_text(body)
    manifest = tmp_path / "reports.json"
    manifest.write_text(
        json.dumps([{"path": str(report), "sha256": hashlib.sha256(body.encode()).hexdigest()}])
    )

    completed = subprocess.run(
        [sys.executable, "-c", _row_parser_program(), str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )

    parsed = json.loads(completed.stdout)
    assert parsed["status"] == "unavailable"
    assert parsed["reasons"] == ["declared_testcase_count_mismatch"]
    sealed = seal_testcase_execution_rows(
        parsed,
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0013",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )
    assert sealed["status"] == "unavailable"
    assert sealed["rows"] == []


# --- the universal row bounds (plan r2 T1) ----------------------------------


def _report(tmp_path, name, *, cases):
    """One report on disk plus the manifest entry that claims its bytes."""

    body = "\n".join(['<testsuite tests="%d">' % len(cases), *cases, "</testsuite>"])
    path = tmp_path / name
    path.write_text(body)
    return {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()}


def _case(index, *, outcome="passed", classname="a.Suite", name=None):
    label = name if name is not None else f"case{index:05d}"
    opening = f'<testcase classname="{classname}" name="{label}"'
    if outcome == "failed":
        return f'{opening}><failure message="boom"/></testcase>'
    if outcome == "error":
        return f'{opening}><error message="boom"/></testcase>'
    if outcome == "skipped":
        return f"{opening}><skipped/></testcase>"
    return f"{opening}/>"


def _parse_reports(tmp_path, entries):
    manifest = tmp_path / "reports.json"
    manifest.write_text(json.dumps(entries))
    completed = subprocess.run(
        [sys.executable, "-c", _row_parser_program(), str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_a_single_report_over_the_per_file_cap_still_keeps_what_the_budget_holds(tmp_path):
    """The per-file bound orders the sample; it does not throw rows away.

    One pytest run is one report file. Cutting it at the per-file cap would
    drop 600 identities the receipt had room for and disclose a loss that never
    had to happen — a bound has to be the smallest one that holds.
    """
    over = DELTA_PER_FILE_ROW_CAP + 600
    entries = [
        _report(
            tmp_path,
            "TEST-one.xml",
            cases=[_case(0, outcome="failed", name="theRed")]
            + [_case(index) for index in range(1, over)],
        )
    ]

    parsed = _parse_reports(tmp_path, entries)
    bounds = parsed["bounds"]

    assert bounds["observed_rows"] == bounds["kept_rows"] == over
    assert bounds["dropped_green"] == bounds["dropped_red"] == 0
    assert bounds["per_file_cap_drops"] == 0
    assert parsed["rows"][0]["name"] == "theRed"


def test_the_container_read_keeps_every_red_and_states_what_each_cap_dropped(tmp_path):
    """One report far past the per-file cap, one small; reds in both.

    Two laws at once. No green anywhere outranks a red — both reds are carried
    though one arrives 2,400 rows deep. And the rows that lose their slot are
    the ones the per-file bound demoted, counted under that bound, with
    kept + dropped equal to what the read observed: the receipt schema calls a
    cap that cannot say what it cost a silent cap, and refuses to carry one.
    """
    over = DELTA_TESTCASE_ROW_CAP + 400
    entries = [
        _report(
            tmp_path,
            "TEST-big.xml",
            cases=[_case(index) for index in range(over)]
            + [_case(over, outcome="failed", name="bigRed")],
        ),
        _report(
            tmp_path,
            "TEST-small.xml",
            cases=[_case(0, outcome="error", name="smallRed"), _case(1, name="smallGreen")],
        ),
    ]
    observed = over + 3

    parsed = _parse_reports(tmp_path, entries)
    bounds = parsed["bounds"]

    # The parse itself is complete: a cap is a disclosure, never a failure.
    assert parsed["status"] == "complete"
    assert parsed["reasons"] == []
    assert parsed["report_count"] == 2
    assert bounds["observed_rows"] == observed
    assert bounds["kept_rows"] == len(parsed["rows"]) == DELTA_TESTCASE_ROW_CAP
    assert bounds["dropped_red"] == 0
    assert bounds["dropped_green"] == observed - DELTA_TESTCASE_ROW_CAP
    assert bounds["per_file_cap_drops"] == bounds["dropped_green"]
    assert bounds["total_cap_drops"] == 0
    assert bounds["kept_rows"] + bounds["dropped_green"] == bounds["observed_rows"]
    assert {row["name"] for row in parsed["rows"] if row["outcome"] != "passed"} == {
        "bigRed",
        "smallRed",
    }
    # The small file kept its slot: that is what the per-file bound is for.
    assert bounds["dropped_files"] == 0
    assert "smallGreen" in {row["name"] for row in parsed["rows"]}


def test_the_total_cap_spends_the_sample_on_reds_before_any_green(tmp_path):
    """Reds in the LAST file read still outrank greens from the first.

    A cap applied in file order would have spent the whole sample on the first
    report's greens and dropped the reds that arrived after it — which is how
    kafka's 8 failures disappear into 27,219 passes.
    """
    # Six full files of greens — each exactly at the per-file cap, so only the
    # TOTAL cap can bite — and the red arrives in the seventh, last.
    green_files = 6
    entries = [
        _report(
            tmp_path,
            f"TEST-green-{number}.xml",
            cases=[_case(index) for index in range(DELTA_PER_FILE_ROW_CAP)],
        )
        for number in range(green_files)
    ]
    entries.append(
        _report(
            tmp_path,
            "TEST-late.xml",
            cases=[_case(0, outcome="failed", name="lateRed"), _case(1, name="lateGreen")],
        )
    )
    observed = green_files * DELTA_PER_FILE_ROW_CAP + 2

    parsed = _parse_reports(tmp_path, entries)
    bounds = parsed["bounds"]

    assert bounds["observed_rows"] == observed
    assert bounds["kept_rows"] == DELTA_TESTCASE_ROW_CAP
    assert [row["name"] for row in parsed["rows"] if row["outcome"] == "failed"] == ["lateRed"]
    assert bounds["dropped_red"] == 0
    assert bounds["per_file_cap_drops"] == 0
    assert bounds["dropped_green"] == observed - DELTA_TESTCASE_ROW_CAP
    assert bounds["total_cap_drops"] == bounds["dropped_green"]
    # Every file still contributed a carried row, so none is a dropped file.
    assert bounds["dropped_files"] == 0
    assert bounds["kept_rows"] + bounds["dropped_green"] == observed


def test_a_report_too_large_to_parse_is_still_hash_bound_and_disclosed(tmp_path):
    """kafka's 137.8 MB report, in miniature: read whole, parsed never.

    The digest is streamed over the WHOLE file, so what the receipt says about
    this report is still bound to the bytes the delta claims. What it says is
    that the sample does not speak for it — a dropped file, and no claim of
    red completeness — rather than that the report was unreadable, which would
    empty the envelope of every OTHER report's identities too.
    """
    padding = " " * (DELTA_REPORT_MAX_BYTES + 1024)
    huge = _report(
        tmp_path,
        "TEST-huge.xml",
        cases=[_case(0, outcome="failed", name="hidden"), f"<!--{padding}-->"],
    )
    # The comment is not a testcase, so the root's declared count still holds.
    body = (tmp_path / "TEST-huge.xml").read_text().replace('tests="2"', 'tests="1"')
    (tmp_path / "TEST-huge.xml").write_text(body)
    huge["sha256"] = hashlib.sha256(body.encode()).hexdigest()
    small = _report(tmp_path, "TEST-small.xml", cases=[_case(0, name="survivor")])

    parsed = _parse_reports(tmp_path, [huge, small])
    bounds = parsed["bounds"]

    assert parsed["status"] == "complete"
    assert parsed["report_count"] == 2
    assert [row["name"] for row in parsed["rows"]] == ["survivor"]
    assert bounds["unparsed_reports"] == 1
    assert bounds["dropped_files"] == 1
    assert bounds["observed_rows"] == 1
    disclosure = invocation_receipts.disclose_row_bounds({**parsed, "row_bounds": bounds})
    assert disclosure == {
        "rows_source": "delta_xml",
        "red_rows_complete": False,
        "rows_truncated": {"dropped_green": 0, "dropped_files": 1},
    }


def test_a_row_too_wide_for_the_transport_is_dropped_and_counted(tmp_path):
    """An identity is dropped whole or carried whole; it is never clipped.

    A clipped classname is a different test, and a receipt may not invent one.
    """
    entries = [
        _report(
            tmp_path,
            "TEST-wide.xml",
            cases=[
                _case(0, name="x" * (DELTA_ROW_MAX_JSON_BYTES + 64)),
                _case(1, name="ordinary"),
            ],
        )
    ]

    parsed = _parse_reports(tmp_path, entries)

    assert [row["name"] for row in parsed["rows"]] == ["ordinary"]
    assert parsed["bounds"]["oversize_row_drops"] == 1
    assert parsed["bounds"]["dropped_green"] == 1
    assert parsed["bounds"]["observed_rows"] == 2


def test_the_host_holds_the_bound_whatever_the_parser_hands_back():
    """The guarantee, not the optimization.

    The container applies these caps at the source. This is what makes them a
    property of the receipt rather than of one program: a parser that ignored
    them — an older image, a doubled transport, a bug — still cannot put an
    unbounded list into a receipt, and the accounting it did not do is done
    here.
    """
    rows = [
        {"report_path": "/workspace/proj/a.xml", "outcome": "passed", "name": f"g{index}"}
        for index in range(DELTA_TESTCASE_ROW_CAP + 500)
    ]
    rows.append({"report_path": "/workspace/proj/b.xml", "outcome": "failed", "name": "red"})

    kept, bounds = bound_testcase_rows(rows)

    assert len(kept) == DELTA_TESTCASE_ROW_CAP
    assert kept[0]["name"] == "red"
    assert bounds["dropped_red"] == 0
    assert bounds["dropped_green"] == 501
    assert bounds["total_cap_drops"] == 501
    assert bounds["kept_rows"] + bounds["dropped_green"] == bounds["observed_rows"] == len(rows)


def test_host_bounds_add_to_the_container_accounting_and_never_replace_it():
    """Two caps, one arithmetic: what each side dropped is stated once."""

    rows = [
        {"report_path": "/workspace/proj/a.xml", "outcome": "passed", "name": f"g{index}"}
        for index in range(DELTA_TESTCASE_ROW_CAP + 10)
    ]

    # What the container says it saw and dropped: 27,219 observed, of which
    # these rows are what it kept.
    kept, bounds = bound_testcase_rows(
        rows,
        {
            "observed_rows": 27_219,
            "dropped_green": 27_219 - len(rows),
            "dropped_files": 900,
        },
    )

    assert len(kept) == DELTA_TESTCASE_ROW_CAP
    assert bounds["observed_rows"] == 27_219
    assert bounds["dropped_green"] == 27_219 - DELTA_TESTCASE_ROW_CAP
    # Every row came from one file, and that file is still represented.
    assert bounds["dropped_files"] == 900
    assert bounds["kept_rows"] + bounds["dropped_green"] == bounds["observed_rows"]


def test_the_rows_section_worst_case_is_a_constant_far_under_the_receipt_budget():
    """The structural guard, recomputed rather than trusted.

    `invocation_receipts` asserts this relation at import time; if the caps or
    the budget ever move apart, the assert fires there and this states why it
    exists. A sealed row cannot exceed one parsed row plus the receipt-scoped
    material sealing adds, and the reader cannot return more rows than the cap.
    """
    assert invocation_receipts.SEALED_ROW_MAX_CANONICAL_BYTES == (
        DELTA_ROW_MAX_JSON_BYTES + SEALED_ROW_OVERHEAD_MAX_BYTES
    )
    assert invocation_receipts.ROW_SECTION_MAX_CANONICAL_BYTES == (
        DELTA_TESTCASE_ROW_CAP * invocation_receipts.SEALED_ROW_MAX_CANONICAL_BYTES
    )
    assert (
        invocation_receipts.ROW_SECTION_MAX_CANONICAL_BYTES
        * invocation_receipts.ROW_SECTION_BUDGET_SHARE
        < invocation_receipts.RECEIPT_MAX_CANONICAL_BYTES
    )
    # And the bounds stay the harvest's own, which is what makes them one cap
    # over every runner rather than two that drift.
    assert DELTA_TESTCASE_ROW_CAP == invocation_receipts.GRADLE_TESTCASE_ROW_CAP
    assert DELTA_PER_FILE_ROW_CAP <= invocation_receipts.TESTCASE_TAG_CAP


def test_a_maven_receipt_discloses_its_own_row_bounds_and_keeps_its_totals(monkeypatch):
    """P-A on a runner that has no Gradle harvest at all.

    Nothing about the exact read is Gradle's: `record_invocation` parses the
    delta the same way for maven, and the bound it applies there needs the same
    disclosure. The receipt keeps its exit code, its argv and its delta; the
    identity list says it is a sample; the disclosure says by how much.
    """
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    parsed = _parsed(path)
    parsed["rows"] = [
        {**parsed["rows"][0], "name": f"case{index}", "execution_ordinal": index + 1}
        for index in range(3)
    ]
    parsed["row_bounds"] = {
        "observed_rows": 27_219,
        "kept_rows": 3,
        "dropped_red": 0,
        "dropped_green": 27_216,
        "dropped_files": 1_100,
        "per_file_cap_drops": 0,
        "total_cap_drops": 27_216,
        "oversize_row_drops": 0,
        "unparsed_reports": 0,
    }
    execute = FakeExecute(
        rules=[
            ("rev-parse HEAD", ok("d" * 40)),
            ("command -v", ok("/usr/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9\n")),
        ]
    )
    monkeypatch.setattr(invocation_receipts, "read_delta_testcase_rows", lambda *_a, **_k: parsed)

    invocation_receipts.record_invocation(
        execute,
        receipt_id="inv-maven-test-0043",
        run_id=RUN_ID,
        tool="maven",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        # A reactor's worth of claimed reports, because the disclosure below
        # states 1,100 of them dropped: a sample can only drop files this
        # receipt claims, and the arithmetic is checked at build time now.
        after={path: HASH_A, **_surefire_claims(1_099)},
        requirements={"build_domains": [{"root": "/workspace/proj", "system": "maven"}]},
    )

    (receipt,) = receipts_written(execute.commands)
    assert receipt["exit_code"] == 0
    assert receipt["argv"] == "mvn test"
    assert len(receipt["report_delta"]["new"]) == 1_100
    assert {"path": path, "sha256": HASH_A} in receipt["report_delta"]["new"]
    assert len(receipt["testcase_execution_rows"]["rows"]) == 3
    assert receipt["testcase_row_disclosure"] == {
        "rows_source": "delta_xml",
        "red_rows_complete": True,
        "rows_truncated": {"dropped_green": 27_216, "dropped_files": 1_100},
    }
    # The list a reader sees says it is a sample, in its own field.
    assert receipt["testcase_outcomes"]["truncated"] is True
    assert invocation_receipts.validate_receipt_v2(receipt) == receipt


def test_a_row_disclosure_never_rides_a_receipt_that_carries_no_sample():
    """A truncation record over a list the receipt does not hold is evidence
    against itself, so it is not carried at all."""

    receipt = invocation_receipts.build_receipt(
        receipt_id="inv-maven-test-0045",
        run_id=RUN_ID,
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        testcase_row_disclosure={
            "rows_source": "delta_xml",
            "red_rows_complete": False,
            "rows_truncated": {"dropped_green": 12, "dropped_files": 3},
        },
    )

    assert "testcase_row_disclosure" not in receipt
    assert "evidence_omissions" not in receipt
    assert invocation_receipts.validate_receipt_v2(receipt) == receipt


def test_a_sample_that_lost_nothing_discloses_nothing(monkeypatch):
    """A disclosure states a loss. With no loss there is nothing to state, and
    an all-zero truncation record beside a complete sample is noise a reader
    would have to learn to ignore."""

    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    parsed = _parsed(path)
    parsed["row_bounds"] = {"observed_rows": 1, "kept_rows": 1, "dropped_green": 0}
    execute = FakeExecute(
        rules=[
            ("rev-parse HEAD", ok("d" * 40)),
            ("command -v", ok("/usr/bin/mvn\nSAGTOOLCHAIN\nApache Maven 3.9\n")),
        ]
    )
    monkeypatch.setattr(invocation_receipts, "read_delta_testcase_rows", lambda *_a, **_k: parsed)

    invocation_receipts.record_invocation(
        execute,
        receipt_id="inv-maven-test-0044",
        run_id=RUN_ID,
        tool="maven",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={path: HASH_A},
        requirements={"build_domains": [{"root": "/workspace/proj", "system": "maven"}]},
    )

    (receipt,) = receipts_written(execute.commands)
    assert "testcase_row_disclosure" not in receipt
    assert "truncated" not in receipt["testcase_outcomes"]


def test_duplicate_testcase_elements_in_one_report_keep_distinct_execution_ids():
    path = "/workspace/proj/target/surefire-reports/TEST-duplicate.xml"
    parsed = _parsed(path, name="same")
    parsed["rows"].append({**parsed["rows"][0], "execution_ordinal": 2})

    envelope = seal_testcase_execution_rows(
        parsed,
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0014",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )

    assert envelope["status"] == "complete"
    assert len(envelope["rows"]) == 2
    assert len({row["execution_id"] for row in envelope["rows"]}) == 2


def test_full_nested_classname_prevents_source_file_owner_collision():
    path = "/workspace/proj/target/surefire-reports/TEST-nested.xml"
    parsed = _parsed(path, classname="pkg.Outer$First", name="same")
    parsed["rows"][0]["source_file"] = "src/test/java/pkg/Outer.java"
    parsed["rows"].append(
        {
            **parsed["rows"][0],
            "classname": "pkg.Outer$Second",
            "execution_ordinal": 2,
        }
    )

    envelope = seal_testcase_execution_rows(
        parsed,
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0015",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )

    assert {row["owner"] for row in envelope["rows"]} == {
        "src/test/java/pkg/Outer.java::pkg.Outer.First",
        "src/test/java/pkg/Outer.java::pkg.Outer.Second",
    }


def test_python_subject_identity_is_cwd_independent_and_requires_source_proof():
    path = "/workspace/.setup_agent/pytest-reports/pytest-attempt.xml"
    parsed = _parsed(path, classname="tests.test_api.ApiTests", name="works")
    parsed["rows"][0]["source_file"] = "tests/test_api.py"

    rows = []
    for receipt_id, cwd in (
        ("inv-python-test-0016", "/workspace/proj"),
        ("inv-python-test-0017", "/workspace/proj/deep/workdir"),
    ):
        envelope = seal_testcase_execution_rows(
            parsed,
            run_id=RUN_ID,
            receipt_id=receipt_id,
            tool="python",
            target_sha="a" * 40,
            domain_id="/workspace/proj",
            working_directory=cwd,
        )
        rows.append(envelope["rows"][0])
    assert rows[0]["module_coordinate"] == rows[1]["module_coordinate"]
    assert rows[0]["owner"] == rows[1]["owner"]

    unproved = _parsed(path, classname="", name="works")
    envelope = seal_testcase_execution_rows(
        unproved,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0018",
        tool="python",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )
    assert envelope["status"] == "unavailable"


def test_python_absolute_source_must_be_inside_the_surveyed_domain():
    path = "/workspace/.setup_agent/pytest-reports/pytest-attempt.xml"
    relative = _parsed(path, classname="tests.test_api.ApiTests", name="works")
    relative["rows"][0]["source_file"] = "tests/test_api.py"
    inside = _parsed(path, classname="tests.test_api.ApiTests", name="works")
    inside["rows"][0]["source_file"] = "/workspace/proj/tests/test_api.py"
    outside = _parsed(path, classname="tests.test_api.ApiTests", name="works")
    outside["rows"][0]["source_file"] = "/workspace/other/tests/test_api.py"

    relative_envelope = seal_testcase_execution_rows(
        relative,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0021",
        tool="python",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj/deep",
    )
    inside_envelope = seal_testcase_execution_rows(
        inside,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0022",
        tool="python",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )
    outside_envelope = seal_testcase_execution_rows(
        outside,
        run_id=RUN_ID,
        receipt_id="inv-python-test-0023",
        tool="python",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/other",
    )

    assert relative_envelope["status"] == "complete"
    assert inside_envelope["status"] == "complete"
    assert relative_envelope["rows"][0]["module_coordinate"] == "pytest:tests/test_api.py"
    assert inside_envelope["rows"][0]["module_coordinate"] == "pytest:tests/test_api.py"
    assert relative_envelope["rows"][0]["owner"] == inside_envelope["rows"][0]["owner"]
    assert outside_envelope["status"] == "unavailable"
    assert outside_envelope["rows"] == []


def test_consumer_recomputes_execution_id_instead_of_accepting_a_claimed_id():
    path = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    envelope = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="inv-maven-test-0019",
        tool="maven",
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        working_directory="/workspace/proj",
    )
    row = dict(envelope["rows"][0])
    assert _testcase_execution_id(row) == row["execution_id"]
    row["execution_id"] = "invented"
    try:
        validate_testcase_execution_row(row)
    except ValueError as exc:
        assert "execution_id" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("arbitrary execution id was accepted")


def test_gradle_project_dir_mapping_requires_static_settings_proof():
    class SettingsExecute:
        def __init__(self, source):
            self.source = source

        def __call__(self, command, **_kwargs):
            if command.endswith("settings.gradle"):
                return {"exit_code": 0, "output": self.source}
            return {"exit_code": 1, "output": ""}

    proven = read_gradle_project_map(
        SettingsExecute('include(":api")\nproject(":api").projectDir = file("modules/public-api")'),
        "/workspace/proj",
    )
    dynamic = read_gradle_project_map(
        SettingsExecute('include(":api")\nproject(":api").projectDir = file(provider.get())'),
        "/workspace/proj",
    )

    assert proven == {
        "/workspace/proj": ":root",
        "/workspace/proj/modules/public-api": ":api",
    }
    assert dynamic is None


# ---------------------------------------------------------------------------
# Gradle test evidence (evidence study 2026-08-30, docs/superpowers/reports/
# gradle-evidence-20260830.md). The measured kafka run left 27,219 tests' worth
# of XML on disk and reported none of it. These cover the pure fold that turns
# such a harvest into the two receipt facts it may state: complete per
# (project, task-dir) totals, and a bounded, red-first identity sample that
# discloses its own truncation.
# ---------------------------------------------------------------------------

GRADLE_ROOT = "/workspace/proj"


def _suite(module, task, *, tests=1, failures=0, errors=0, skipped=0):
    return {
        "module": module,
        "task": task,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def _gradle_row(module, name, outcome, *, ordinal=1, task="test", digest=HASH_A):
    return {
        "report_path": f"{GRADLE_ROOT}/{module}/build/test-results/{task}/TEST-{name}.xml",
        "report_sha256": digest,
        "classname": "com.acme.SuiteTest",
        "name": name,
        "source_file": None,
        "outcome": outcome,
        "reason": None,
        "execution_ordinal": ordinal,
    }


def test_gradle_summaries_fold_per_module_and_task_dir_not_per_module():
    """geode writes `test-results/test` AND `test-results/distributedTest`.

    Folding on the module alone would silently merge two different task runs
    into one row and lose which task produced which count.
    """
    section, rows, disclosures = assemble_gradle_test_rows(
        [
            _suite(":core", "test", tests=10, failures=1, skipped=2),
            _suite(":core", "test", tests=5, errors=1),
            _suite(":core", "distributedTest", tests=7),
        ],
        [],
    )

    assert section["suites"] == [
        {
            "module": ":core",
            "task": "distributedTest",
            "xml_files": 1,
            "tests": 7,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        {
            "module": ":core",
            "task": "test",
            "xml_files": 2,
            "tests": 15,
            "failures": 1,
            "errors": 1,
            "skipped": 2,
        },
    ]
    assert "truncated" not in section and "unreadable_suites" not in section
    assert rows == []
    # The summaries witness two reds that no row carries: nothing may claim the
    # red identities are complete.
    assert disclosures == {"rows_source": "gradle_xml", "red_rows_complete": False}


def test_a_gradle_harvest_that_found_nothing_states_absence_and_never_a_zero():
    """ofbiz-plugins left no XML at all. Absence is missing, not zero."""

    assert assemble_gradle_test_rows([], []) == (None, [], None)
    assert assemble_gradle_test_rows(None, None) == (None, [], None)
    # A harvest whose every entry was unreadable is equally not a zero count:
    # no suite survives, so the section is absent and the caller must record an
    # evidence omission rather than publish an empty summary.
    section, rows, disclosures = assemble_gradle_test_rows([{"module": ":a"}], [])
    assert section is None and rows == []
    assert disclosures is None


def test_gradle_row_cap_drops_greens_before_reds_and_counts_every_drop():
    reds = [_gradle_row("clients", f"red{index}", "failed", ordinal=index) for index in range(1, 3)]
    greens = [
        _gradle_row("streams", f"green{index}", "passed", ordinal=index) for index in range(1, 5)
    ]

    section, rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=6, failures=2)],
        [*greens, *reds],
        row_cap=3,
    )

    assert [row["name"] for row in rows] == ["red1", "red2", "green1"]
    assert disclosures == {
        "rows_source": "gradle_xml",
        # Every red the summaries account for survived the cap.
        "red_rows_complete": True,
        "rows_truncated": {"dropped_green": 3, "dropped_files": 3},
    }
    assert section["suites"][0]["failures"] == 2


def test_a_cap_that_cannot_hold_the_reds_withdraws_the_completeness_claim():
    reds = [_gradle_row("clients", f"red{index}", "error", ordinal=index) for index in range(1, 4)]

    _section, rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=3, errors=3)],
        [*reds, _gradle_row("clients", "green", "passed", ordinal=9)],
        row_cap=2,
    )

    assert [row["name"] for row in rows] == ["red1", "red2"]
    assert disclosures["red_rows_complete"] is False
    # A dropped red is disclosed as a count, not only as a withdrawn claim.
    # Two reports (red3's and the green's) lost every row they contributed.
    assert disclosures["rows_truncated"] == {
        "dropped_green": 1,
        "dropped_files": 2,
        "dropped_red": 1,
    }


def test_dropped_files_counts_only_reports_that_lost_every_row():
    shared = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-shared.xml"
    kept = dict(_gradle_row("clients", "red", "failed"), report_path=shared)
    also_shared = dict(_gradle_row("clients", "green", "passed"), report_path=shared, ordinal=2)
    elsewhere = _gradle_row("streams", "other", "passed", ordinal=1)

    _section, rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=3, failures=1)],
        [kept, also_shared, elsewhere],
        row_cap=1,
    )

    assert [row["name"] for row in rows] == ["red"]
    # Two rows were dropped but only ONE report lost its whole representation.
    assert disclosures["rows_truncated"] == {"dropped_green": 2, "dropped_files": 1}


def test_gradle_summary_cap_keeps_red_bearing_pairs_and_states_what_it_dropped():
    summaries = [_suite(f":green{index:03d}", "test", tests=1) for index in range(20)]
    summaries.append(_suite(":zzz-red", "test", tests=1, failures=1))

    section, _rows, disclosures = assemble_gradle_test_rows(summaries, [], summary_cap=3)

    modules = [suite["module"] for suite in section["suites"]]
    assert ":zzz-red" in modules
    assert len(section["suites"]) == 3
    assert section["truncated"] is True
    assert section["dropped_suites"] == 18
    # The cap that dropped summaries did not touch the rows, so no row
    # truncation is claimed — but the unseen red keeps completeness withdrawn.
    assert "rows_truncated" not in disclosures
    assert disclosures["red_rows_complete"] is False


@pytest.mark.parametrize(
    "malformed",
    [
        {"module": ":a", "task": "test", "tests": 1, "failures": 0, "errors": 0},
        {"module": "", "task": "test", "tests": 1, "failures": 0, "errors": 0, "skipped": 0},
        {"module": ":a", "task": "", "tests": 1, "failures": 0, "errors": 0, "skipped": 0},
        {"module": ":a", "task": "test", "tests": -1, "failures": 0, "errors": 0, "skipped": 0},
        {"module": ":a", "task": "test", "tests": True, "failures": 0, "errors": 0, "skipped": 0},
        {"module": ":a", "task": "test", "tests": "4", "failures": 0, "errors": 0, "skipped": 0},
        "not a mapping",
    ],
)
def test_an_unreadable_summary_is_counted_and_never_summed_into_a_smaller_total(malformed):
    section, _rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=4), malformed],
        [_gradle_row("clients", "green", "passed")],
    )

    assert section["suites"] == [
        {
            "module": ":clients",
            "task": "test",
            "xml_files": 1,
            "tests": 4,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        }
    ]
    assert section["unreadable_suites"] == 1
    # Part of the witness is missing, so the red count it would have declared
    # is unknown and completeness cannot be claimed.
    assert disclosures["red_rows_complete"] is False


def test_red_completeness_needs_a_summary_witness_that_the_rows_satisfy():
    row = _gradle_row("clients", "red", "failed")

    _s, _r, agreeing = assemble_gradle_test_rows([_suite(":c", "test", tests=1, failures=1)], [row])
    _s, _r, short = assemble_gradle_test_rows([_suite(":c", "test", tests=2, failures=2)], [row])
    _s, _r, unwitnessed = assemble_gradle_test_rows([], [row])

    assert agreeing["red_rows_complete"] is True
    # The reports declared two failures and only one identity was harvested.
    assert short["red_rows_complete"] is False
    # No summary at all is no witness at all — never a vacuous claim.
    assert unwitnessed == {"rows_source": "gradle_xml", "red_rows_complete": False}


def test_harvested_module_and_task_text_is_collapsed_before_a_receipt_sees_it():
    """The kafka lesson, applied one field earlier.

    A receipt identifier may not carry control text, and a field that carries
    it is dropped to an omission at assembly. Collapsing here keeps the
    evidence instead of losing the section to its own whitespace.
    """
    section, _rows, _disclosures = assemble_gradle_test_rows(
        [_suite(" :clients\n ", "test\ttest", tests=1)],
        [],
    )

    assert section["suites"] == [
        {
            "module": ":clients",
            "task": "test test",
            "xml_files": 1,
            "tests": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        }
    ]


def test_rows_a_parser_could_not_classify_lose_their_slot_before_any_red_does():
    unclassified = dict(_gradle_row("clients", "mystery", "passed"), outcome="")

    _section, rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=2, failures=1)],
        [unclassified, _gradle_row("clients", "red", "failed", ordinal=2)],
        row_cap=1,
    )

    assert [row["name"] for row in rows] == ["red"]
    assert disclosures["red_rows_complete"] is True
    assert disclosures["rows_truncated"] == {"dropped_green": 1, "dropped_files": 1}


def test_capped_gradle_rows_still_seal_through_the_one_maven_row_contract():
    """The rows this fold returns are the SAME rows, only ordered and bounded.

    Downstream must not learn a second row shape: identity sealing, the
    execution-id material and `validate_testcase_execution_row` are unchanged.
    """
    harvest = [
        _gradle_row("clients", "greenOne", "passed", ordinal=1),
        _gradle_row("clients", "redOne", "failed", ordinal=2),
    ]
    _section, rows, disclosures = assemble_gradle_test_rows(
        [_suite(":clients", "test", tests=2, failures=1)],
        harvest,
        row_cap=1,
    )
    for row in rows:
        row["report_path"] = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-c.xml"

    envelope = seal_testcase_execution_rows(
        {"schema_version": 2, "status": "complete", "report_count": 1, "rows": rows},
        run_id=RUN_ID,
        receipt_id="inv-gradle-test-0031",
        tool="gradle",
        target_sha="a" * 40,
        domain_id=GRADLE_ROOT,
        working_directory=GRADLE_ROOT,
        module_outcomes=[{"module": ":clients", "status": "attempted"}],
        gradle_project_map={
            GRADLE_ROOT: ":root",
            f"{GRADLE_ROOT}/clients": ":clients",
        },
    )

    assert envelope["status"] == "complete"
    assert len(envelope["rows"]) == 1
    sealed = envelope["rows"][0]
    assert sealed["module_coordinate"] == ":clients"
    assert sealed["framework"] == "junit-xml"
    assert sealed["outcome"] == "failed"
    assert (
        validate_testcase_execution_row(
            sealed,
            receipt_id="inv-gradle-test-0031",
            run_id=RUN_ID,
            target_sha="a" * 40,
            domain_id=GRADLE_ROOT,
            report_claims={(sealed["report_path"], sealed["report_sha256"])},
        )
        == sealed
    )
    assert disclosures["rows_truncated"]["dropped_green"] == 1


_RED_FIXTURE = Path(__file__).parent / "fixtures" / "gradle_receipts" / "kafka-red-suite.xml"


def _cut_red_stream(path):
    """kafka's red report, its tag stream cut exactly where `head -n` cuts.

    Line 20 is `testFileUnreadable()`'s open tag and line 21 is its `<failure>`
    child, so the first 20 lines are a report whose last node opened and never
    stated an outcome — the ordinary shape of a bounded read against a report
    that (in the measured container) runs to 137.8 MB.
    """
    lines = _RED_FIXTURE.read_text().splitlines()
    cut = "\n".join(lines[:20]) + "\n"
    assert cut.rstrip().endswith('time="0.019">')
    digest = hashlib.sha256(_RED_FIXTURE.read_bytes()).hexdigest()
    return f"{REPORT_TAG_MARKER}{digest}  {path}\n{cut}", digest


def test_a_report_cut_before_a_failure_child_yields_no_row_rather_than_a_green_one():
    """The per-file bound cuts mid-node; the parser's DEFAULT must not ship.

    The trailing node's outcome is on the other side of the cut, so closing it
    at stream end publishes `testFileUnreadable()` — one of the eight measured
    kafka reds — as a PASSING row. A red identity reported green is worse than
    a red identity not reported: fewer rows, never a wrong one.
    """
    path = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-red.xml"
    stream, digest = _cut_red_stream(path)

    rows = parse_report_tag_rows(stream, report_claims={path: digest})

    assert "testFileUnreadable()" not in [row["name"] for row in rows]
    # The 16 nodes that did close still stand.
    assert len(rows) == 16
    assert {row["outcome"] for row in rows} == {"passed"}


def test_a_partly_read_report_states_the_identities_its_own_bound_never_delivered():
    """The same cut, folded: 19 declared by the file, 16 delivered by the read.

    The row cap never fires and the report keeps its slot in the sample, so
    neither `dropped_green` nor `dropped_files` can carry this loss — without a
    count of its own it is a silent cap, and the red that the cut swallowed
    would leave no trace at all.
    """
    path = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-red.xml"
    stream, digest = _cut_red_stream(path)
    rows = parse_report_tag_rows(stream, report_claims={path: digest})

    _section, kept, disclosures = assemble_gradle_test_rows(
        # The head read of the same file: its `<testsuite>` root declares 19
        # tests and one failure, and those counts stay complete.
        [{**_suite(":clients", "test", tests=19, failures=1), "path": path}],
        rows,
    )

    assert len(kept) == 16
    assert disclosures["rows_truncated"] == {
        "dropped_green": 0,
        "dropped_files": 0,
        "unread_rows": 3,
    }
    # The declared failure is not in the sample, and nothing claims it is.
    assert disclosures["red_rows_complete"] is False


def test_identities_lost_inside_a_partly_read_report_are_counted_not_silent():
    """kafka's 400-token per-file bound against a report declaring 1,000 tests.

    The rows that bound never built can be neither a dropped green (nothing
    built them) nor a dropped file (the report IS represented in the sample),
    so before this they were a cap that applied and recorded nothing — while
    the suite totals beside them declared 1,000. Measured against the file's
    own `<testsuite>` total, they are a stated loss.
    """
    report = f"{GRADLE_ROOT}/streams/build/test-results/test/TEST-giant.xml"
    delivered = [
        dict(
            _gradle_row("streams", f"green{index}", "passed", ordinal=index),
            report_path=report,
        )
        for index in range(1, 131)
    ]

    _section, rows, disclosures = assemble_gradle_test_rows(
        [{**_suite(":streams", "test", tests=1000), "path": report}],
        delivered,
        row_cap=200,
    )

    assert len(rows) == 130
    # The row cap never fired and the report kept its slot: without the in-file
    # count this disclosure would have been absent entirely.
    assert disclosures["rows_truncated"] == {
        "dropped_green": 0,
        "dropped_files": 0,
        "unread_rows": 870,
    }


def test_a_file_that_delivered_no_row_at_all_stays_a_dropped_file_and_not_unread_rows():
    """One loss, counted once, in the unit that describes it.

    A report the sample never spoke for is wholly disclosed by `dropped_files`;
    re-counting its declared tests as unread identities would inflate a second
    field with the same fact.
    """
    read = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-read.xml"
    unread = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-unread.xml"

    _section, _rows, disclosures = assemble_gradle_test_rows(
        [
            {**_suite(":clients", "test", tests=1), "path": read},
            {**_suite(":clients", "test", tests=500), "path": unread},
        ],
        [dict(_gradle_row("clients", "green", "passed"), report_path=read)],
        harvested_files=[read, unread],
    )

    assert disclosures["rows_truncated"] == {"dropped_green": 0, "dropped_files": 1}


def test_a_stated_row_disclosure_binds_the_diagnostic_list_to_its_own_harvest(monkeypatch):
    """A disclosure and the list beside it are ONE sample, or they are evidence
    against each other.

    A harvest can state a disclosure and still fold to no usable row — a tag
    transport that returned nothing, claims that matched nothing, rows with no
    name. Letting a different transport fill the list there leaves the receipt
    disclosing dropped files and truncated rows of a sample it does not carry,
    and a reader reconciling the two gets contradictory evidence. Absent is the
    honest answer.
    """
    path = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-a.xml"
    execute = FakeExecute(
        rules=[
            ("rev-parse HEAD", ok("d" * 40)),
            ("command -v", ok("/usr/bin/gradle\nSAGTOOLCHAIN\nGradle 8.5\n")),
            # Both weaker transports are armed, and neither may win.
            ("grep -oE", ok('<testcase classname="a.S" name="fromTheTagRead">\n</testcase>\n')),
        ]
    )
    monkeypatch.setattr(
        invocation_receipts,
        "read_delta_testcase_rows",
        lambda *_args, **_kwargs: _parsed(path),
    )

    invocation_receipts.record_invocation(
        execute,
        receipt_id="inv-gradle-test-0041",
        run_id=RUN_ID,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory=GRADLE_ROOT,
        exit_code=0,
        before={},
        # Three claimed reports, because the harvest's disclosure below states
        # three of them dropped and a sample may only drop what it claims.
        after={path: HASH_A, **_surefire_claims(2, root=GRADLE_ROOT)},
        requirements={"build_domains": [{"root": GRADLE_ROOT, "system": "gradle"}]},
        gradle_row_disclosure={
            "rows_source": "gradle_xml",
            "red_rows_complete": False,
            "rows_truncated": {"dropped_green": 0, "dropped_files": 3},
        },
        harvested_testcase_outcomes=None,
    )

    (receipt,) = receipts_written(execute.commands)
    assert receipt["gradle_row_disclosure"]["rows_truncated"]["dropped_files"] == 3
    assert "testcase_outcomes" not in receipt
    # The weakest transport is not even probed once the harvest has spoken.
    assert not any("grep -oE" in command for command in execute.commands)


def test_without_a_harvest_disclosure_the_receipt_still_falls_back_to_the_exact_parse(monkeypatch):
    """The binding is the disclosure's, not gradle's: nothing else narrows."""
    path = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-a.xml"
    execute = FakeExecute(
        rules=[
            ("rev-parse HEAD", ok("d" * 40)),
            ("command -v", ok("/usr/bin/gradle\nSAGTOOLCHAIN\nGradle 8.5\n")),
        ]
    )
    monkeypatch.setattr(
        invocation_receipts,
        "read_delta_testcase_rows",
        lambda *_args, **_kwargs: _parsed(path),
    )

    invocation_receipts.record_invocation(
        execute,
        receipt_id="inv-gradle-test-0042",
        run_id=RUN_ID,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory=GRADLE_ROOT,
        exit_code=0,
        before={},
        after={path: HASH_A},
        requirements={"build_domains": [{"root": GRADLE_ROOT, "system": "gradle"}]},
    )

    (receipt,) = receipts_written(execute.commands)
    assert "gradle_row_disclosure" not in receipt
    assert receipt["testcase_outcomes"]["nodes"] == [
        {"node_id": "com.acme.SharedTest#roundTrip[size=1]", "status": "passed"}
    ]


# --- the cap arithmetic itself (plan r2 T3.4) -------------------------------


class _StatedBoundsContainer(FakeContainer):
    """A parser that hands back rows plus an accounting of its own choosing."""

    def __init__(self, bounds):
        super().__init__()
        self.bounds = bounds

    def execute_command(self, command, **kwargs):
        if "def declarations_agree" in command:
            self.commands.append(command)
            reports = json.loads(self.files[shlex.split(command)[-1]])
            rows = [
                {
                    "report_path": report["path"],
                    "report_sha256": report["sha256"],
                    "classname": "com.acme.ExactTest",
                    "name": f"case[{index}]",
                    "source_file": None,
                    "outcome": "passed",
                    "execution_ordinal": 1,
                }
                for index, report in enumerate(reports)
            ]
            return {
                "exit_code": 0,
                "output": json.dumps(
                    {
                        "status": "complete",
                        "report_count": len(rows),
                        "rows": rows,
                        "reasons": [],
                        "bounds": {**self.bounds, "kept_rows": len(rows)},
                    }
                ),
            }
        return super().execute_command(command, **kwargs)


def _read_with_bounds(bounds, *, reports=3):
    container = _StatedBoundsContainer(bounds)
    entries = [
        {
            "path": f"/workspace/proj/m{index}/target/surefire-reports/TEST-{index}.xml",
            "sha256": f"{index + 1:064x}",
        }
        for index in range(reports)
    ]
    return read_delta_testcase_rows(
        container.execute_command,
        receipt_id="inv-maven-test-0050",
        delta={"new": entries, "changed": []},
    )


def test_a_read_whose_own_accounting_does_not_add_up_yields_no_sample():
    """kept + dropped == observed, or there is no sample to disclose.

    The accounting a disclosure is built from is half the container's — it saw
    rows this side never received — and half this side's. A parser stating
    27,219 observed, 3 kept and 12 dropped has not described a bound that fired;
    it has described a read that cannot have happened, and a disclosure built
    from it would state a loss against a number nobody measured.
    """
    honest = _read_with_bounds(
        {"observed_rows": 27_219, "dropped_green": 27_216, "total_cap_drops": 27_216}
    )
    assert honest["status"] == "complete"
    assert honest["row_bounds"]["kept_rows"] + honest["row_bounds"]["dropped_green"] == 27_219

    impossible = _read_with_bounds(
        {"observed_rows": 27_219, "dropped_green": 12, "total_cap_drops": 12}
    )
    assert impossible["status"] == "unavailable"
    assert impossible["rows"] == []
    assert impossible["reasons"] == ["row_bounds_inconsistent"]
    assert "row_bounds" not in impossible
    # And with no bounds there is nothing to disclose: the receipt states no
    # sample rather than a sample nobody can reconcile.
    assert invocation_receipts.disclose_row_bounds(impossible) is None


def test_a_drop_that_answers_to_no_cap_is_refused_the_same_way():
    """Every dropped row names the bound that took it.

    `dropped_red`/`dropped_green` say WHAT was lost and the three cap counters
    say WHY. A parser whose two halves disagree is stating a loss with no cause,
    which is the silent cap wearing a disclosure's clothes.
    """
    uncaused = _read_with_bounds(
        {"observed_rows": 27_219, "dropped_green": 27_216, "total_cap_drops": 0}
    )

    assert uncaused["status"] == "unavailable"
    assert uncaused["reasons"] == ["row_bounds_inconsistent"]


def test_a_report_declaring_more_outcomes_than_tests_is_unreadable_not_a_total():
    """Conservation at the file that stated the numbers (plan r2 T3.1).

    One report root claiming two failures over one test has measured nothing
    the receipt can sum. Folding it in would carry `tests=1, failures=2` into
    the totals, where the summary validator would refuse the WHOLE section and
    every other report's counts would go with it. So the file is unreadable —
    counted, disclosed, and left out of the totals it cannot join.
    """
    section, _rows, _disclosures = assemble_gradle_test_rows(
        [
            _suite(":clients", "test", tests=20, failures=1, skipped=2),
            {**_suite(":clients", "test", tests=1), "failures": 2},
        ],
        [],
    )

    assert section["suites"] == [
        {
            "module": ":clients",
            "task": "test",
            "xml_files": 1,
            "tests": 20,
            "failures": 1,
            "errors": 0,
            "skipped": 2,
        }
    ]
    assert section["unreadable_suites"] == 1
    # And what survived is a section the receipt can actually carry.
    assert (
        invocation_receipts.validate_receipt_v2(
            {
                "schema_version": invocation_receipts.RECEIPT_SCHEMA_VERSION,
                "receipt_id": "inv-gradle-test-0051",
                "run_id": RUN_ID,
                "tool": "gradle",
                "requested_action": "test",
                "effective_action": "test",
                "argv": "./gradlew test",
                "working_directory": GRADLE_ROOT,
                "actual_cwd": GRADLE_ROOT,
                "exit_code": 0,
                "outcome": "completed",
                "report_delta": {"new": [], "changed": []},
                "gradle_suite_summaries": section,
            }
        )["gradle_suite_summaries"]["unreadable_suites"]
        == 1
    )


def test_a_sealed_row_from_a_report_the_receipt_never_claimed_is_unconstructible():
    """Identity rows are a subset of the claimed report set.

    A row is this invocation's evidence because the bytes it was parsed from
    are bytes this receipt's own delta claims. Re-point the delta and the row
    is somebody else's — so the receipt carrying it cannot be written.
    """
    path = f"{GRADLE_ROOT}/clients/build/test-results/test/TEST-a.xml"
    envelope = seal_testcase_execution_rows(
        _parsed(path),
        run_id=RUN_ID,
        receipt_id="inv-gradle-test-0052",
        tool="gradle",
        target_sha="a" * 40,
        domain_id=GRADLE_ROOT,
        working_directory=GRADLE_ROOT,
        module_outcomes=[{"module": ":clients", "status": "attempted"}],
        gradle_project_map={GRADLE_ROOT: ":root", f"{GRADLE_ROOT}/clients": ":clients"},
    )
    assert envelope["rows"]
    receipt = {
        "schema_version": invocation_receipts.RECEIPT_SCHEMA_VERSION,
        "receipt_id": "inv-gradle-test-0052",
        "run_id": RUN_ID,
        "tool": "gradle",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "./gradlew test",
        "working_directory": GRADLE_ROOT,
        "actual_cwd": GRADLE_ROOT,
        "target_sha": "a" * 40,
        "domain_id": GRADLE_ROOT,
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [{"path": path, "sha256": HASH_A}], "changed": []},
        "testcase_execution_rows": envelope,
    }

    assert invocation_receipts.validate_receipt_v2(receipt)["testcase_execution_rows"] == envelope

    stranger = json.loads(json.dumps(receipt))
    stranger["report_delta"]["new"][0]["path"] = f"{GRADLE_ROOT}/clients/build/other/TEST-a.xml"
    with pytest.raises(ValueError, match="not bound to its report delta"):
        invocation_receipts.validate_receipt_v2(stranger)
