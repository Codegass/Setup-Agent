import hashlib
import json
import shlex
import subprocess
import sys

from test_container_io import FakeContainer
from test_invocation_receipts import HASH_A, FakeExecute, ok, receipts_written

from sag.agent import invocation_receipts
from sag.agent.receipt_test_rows import (
    _CONTAINER_REPORT_ROW_PARSER,
    diagnostic_testcase_outcomes,
    read_delta_testcase_rows,
    read_gradle_project_map,
    seal_testcase_execution_rows,
    testcase_execution_id as _testcase_execution_id,
    validate_testcase_execution_row,
)

RUN_ID = "run-pytest"


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


def test_delta_row_reader_is_exact_across_new_changed_and_cached_reports_without_a_cap():
    class ExactParserContainer(FakeContainer):
        def __init__(self):
            super().__init__()
            self.parser_kwargs = None
            self.parser_input = None

        def execute_command(self, command, **kwargs):
            if "def declared_count" in command:
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
    assert not any("def declared_count" in command for command in container.commands)


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
        [sys.executable, "-c", _CONTAINER_REPORT_ROW_PARSER, str(manifest)],
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
        [sys.executable, "-c", _CONTAINER_REPORT_ROW_PARSER, str(manifest)],
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
        [sys.executable, "-c", _CONTAINER_REPORT_ROW_PARSER, str(manifest)],
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
