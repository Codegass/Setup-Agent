"""Python runner bytes close real facade assessments through durable output refs.

The test replaces the container transport and runner output. Receipt writers,
XML tag/extract/row programs, publication, output storage and assess_dispatch
are production code; no assessment or digest acceptance is injected.
"""

import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from build_requirements_fakes import complete_python_build_requirements_v1
from container_evidence_fakes import add_published_mutable_json, complete_run_pin
from test_receipt_assessor import SHA, ContainerFS, build_action_context

from sag.agent.evidence_assessments import read_assessments, read_receipt
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    RUN_PIN_LOGICAL_ARTIFACT_ID,
)
from sag.agent.output_storage import attach_durable_output_ref
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_tool import (
    _PYTEST_ATTEMPT_TAG_SCRIPT,
    _PYTEST_JUNIT_EXTRACT_SCRIPT,
    _PYTEST_JUNIT_SKIP_REASONS_SCRIPT,
    PYTEST_REPORT_DIR,
    PythonTool,
)

ROOT = "/workspace/proj"
PYTHON = ROOT + "/.venv/bin/python"
XML = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="tests.test_one" name="test_one" file="tests/test_one.py"/></testsuite>'


def ok(output=""):
    return {"success": True, "exit_code": 0, "output": output}


class PythonReceiptFS(ContainerFS):
    def __init__(self, raw, display, assertion_failed=False):
        super().__init__(markers={"pyproject.toml"})
        self.raw, self.display = raw, display
        self.assertion_failed = assertion_failed
        self.test_commands = []

    def execute_command(self, command, **kwargs):
        if command.startswith("find ") and "-exec sha256sum" in command:
            self.commands.append(command)
            return ok(
                "".join(
                    f"{hashlib.sha256(body.encode()).hexdigest()}  {path}\n"
                    for path, body in self.files.items()
                    if path.endswith(".xml")
                )
            )
        if command == PYTHON + " -m pytest --version":
            self.commands.append(command)
            return ok("pytest 8.4.1")
        if command.startswith(PYTHON + " -m pytest"):
            self.commands.append(command)
            if "--collect-only" in command:
                return ok("tests/test_one.py::test_one\n1 test collected in 0.01s")
            report = next(
                token.split("=", 1)[1]
                for token in shlex.split(command)
                if token.startswith("--junitxml=")
            )
            self.files[report] = (
                XML.replace('failures="0"', 'failures="1"').replace(
                    "/></testsuite>", '><failure message="wrong"/></testcase></testsuite>'
                )
                if self.assertion_failed
                else XML
            )
            self.test_commands.append(command)
            return {
                **ok(self.display),
                "success": not self.assertion_failed,
                "exit_code": int(self.assertion_failed),
                "full_output": self.raw,
                "runner_dispatched": True,
            }
        if command.startswith(PYTHON + " -c "):
            tokens = shlex.split(command)
            if tokens[2] in {
                _PYTEST_ATTEMPT_TAG_SCRIPT,
                _PYTEST_JUNIT_EXTRACT_SCRIPT,
                _PYTEST_JUNIT_SKIP_REASONS_SCRIPT,
            }:
                self.commands.append(command)
                report = tokens[3]
                with tempfile.TemporaryDirectory() as folder:
                    local = Path(folder) / "report.xml"
                    if report in self.files:
                        local.write_text(self.files[report])
                    process = subprocess.run(
                        [sys.executable, "-c", tokens[2], str(local), *tokens[4:]],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if local.exists():
                        self.files[report] = local.read_text()
                return {
                    "success": process.returncode == 0,
                    "exit_code": process.returncode,
                    "output": process.stdout + process.stderr,
                }
        result = super().execute_command(command, **kwargs)
        # This file-layer double predates explicit terminal exit codes. State
        # the transport result for its supported metadata reads and writes.
        return {"exit_code": 0 if result.get("success") else 1, **result}


@pytest.mark.parametrize(
    "mode,assertion_failed",
    [("normal", False), ("truncated", False), ("empty", False), ("normal", True)],
)
def test_python_raw_bytes_close_facade_assessments_and_round_trip_output_ref(
    mode, assertion_failed, durable_tool_result_storage, tmp_path, monkeypatch
):
    from sag.agent import receipt_test_rows

    # The fixture executes the real row parser on the host; its retained XML
    # belongs in this test's writable directory, not Docker's /workspace.
    monkeypatch.setattr(
        receipt_test_rows, "REPORT_SNAPSHOT_DIR", str(tmp_path.resolve() / "report-snapshots")
    )
    raw = {
        "normal": "1 passed in 0.01s\n",
        "truncated": ("runner diagnostic line\n" * 10000) + "1 passed in 0.01s\n",
        "empty": "",
    }[mode]
    if assertion_failed:
        raw = raw.replace("passed", "failed")
    display = raw if mode == "normal" else "[monitor summary]\n1 passed in 0.01s\n"
    fs = PythonReceiptFS(raw, display, assertion_failed)
    requirements = complete_python_build_requirements_v1(project_root=ROOT, target_sha=SHA)
    add_published_mutable_json(
        fs,
        fs,
        path=REQUIREMENTS_PATH,
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        payload=requirements,
    )
    add_published_mutable_json(
        fs,
        fs,
        path="/workspace/.setup_agent/run-pin.json",
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        payload=complete_run_pin("run-pytest", SHA),
    )
    facade = BuildTool(fs, python_tool=PythonTool(fs))
    with build_action_context("envelope-python-binding", action="test"):
        result = facade.execute(action="test", working_directory=ROOT)
    # Completed assertion-red execution is a tool result; its receipt stays red.
    assert result.succeeded, result
    assert result.metadata["exit_code"] == int(assertion_failed)
    assert len(fs.test_commands) == 1
    assert result.output.startswith("Collection:")
    assert result.raw_output == raw
    result = attach_durable_output_ref(
        result, durable_tool_result_storage, task_id="pytest", tool_name="build", action="test"
    )
    assert durable_tool_result_storage.retrieve_output(result.output_ref) == raw
    receipt = read_receipt(fs.execute_command, result.metadata["receipt_id"])
    assert receipt is not None
    assert receipt["output_content_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert receipt["report_delta"]["new"][0]["path"].startswith(PYTEST_REPORT_DIR)
    rows = receipt["testcase_execution_rows"]
    assert rows["status"] == "complete" and len(rows["rows"]) == 1, rows
    assessments = [
        row for row in read_assessments(fs) if row.get("receipt_id") == receipt["receipt_id"]
    ]
    codes = {row["typed_code"] for row in assessments}
    assert ("expectation_unmet" if assertion_failed else "expectation_met") in codes, [
        (row["typed_code"], row.get("detail")) for row in assessments
    ]
    assert "assessment_bundle_complete" in codes, [
        (row["typed_code"], row.get("detail")) for row in assessments
    ]
    assert "assessment_output_unavailable" not in codes
    completion = next(
        row for row in assessments if row["typed_code"] == "assessment_bundle_complete"
    )
    assert completion["scope"] == receipt["output_content_hash"]
    if assertion_failed:
        assert "test_failure_exit" in codes, [
            (row["typed_code"], row.get("detail")) for row in assessments
        ]
