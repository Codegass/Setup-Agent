"""Native task evidence uses the actual Bash call and immutable publications."""

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import MethodType

import pytest
from test_acceptance_task import completion, retain, task_run
from test_ci_comparison import ROOT

from sag.agent.action_intents import action_fingerprint
from sag.agent.invocation_contracts import action_context
from sag.agent.invocation_receipts import output_content_hash
from sag.agent.native_task_receipt import executable_sha256
from sag.tools.bash import BashTool


def native_call(
    run,
    monkeypatch,
    *,
    exit_code=0,
    change=None,
    command="./build/native/app",
    full_output=None,
    dispatch_status=None,
):
    run.fs.acceptance_task = run.task
    run.fs.acceptance_task_root = ROOT
    original_execute = run.fs.execute_command
    digest = hashlib.sha256(b"native executable fixture").hexdigest()
    probes = []
    calls = []

    def execute(self, cmd, **kwargs):
        if cmd.startswith("python3 -c ") and "is_relative_to" in cmd:
            probes.append(cmd)
            value = "b" * 64 if change == "binary" and len(probes) > 1 else digest
            if change == "script":
                return {"success": False, "exit_code": 1, "output": "not ELF"}
            return {"success": True, "exit_code": 0, "output": value}
        if cmd == command:
            calls.append((cmd, kwargs))
            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "output": "native check",
                "stdout": "native check",
                "stderr": "",
                **({"full_output": full_output} if full_output is not None else {}),
                **(
                    {"dispatch_status": dispatch_status, "dispatch": {"job_id": "native-job"}}
                    if dispatch_status
                    else {}
                ),
                **({"termination_reason": "absolute_timeout"} if change == "timeout" else {}),
            }
        return original_execute(cmd, **kwargs)

    monkeypatch.setattr(run.fs, "execute_command", MethodType(execute, run.fs))
    monkeypatch.setattr(
        run.fs,
        "execute_command_with_soft_timeout",
        lambda **params: run.fs.execute_command(
            params["command"], **{k: v for k, v in params.items() if k != "command"}
        ),
        raising=False,
    )
    tool = BashTool(run.fs)
    monkeypatch.setattr(tool, "_working_directory_available", lambda _: (True, ""))
    params = {"command": command, "working_directory": ROOT, "timeout": 20}
    if change == "environment":
        params["environment"] = {"TASK_FLAG": "1"}
    intent = dict(params)
    if change == "intent":
        intent["command"] = "./build/native/other"
    domain = "test:" + ROOT
    with action_context(
        envelope_id="envelope-native-task",
        intent_id="intent-native-task",
        intent_domain_id=domain,
        intent_exact_params=intent,
        action_fingerprint=action_fingerprint(domain_id=domain, tool="bash", params=intent),
    ):
        result = tool.execute(**params)
    return result, calls, probes


def test_native_call_is_run_once_and_binds_the_executable_bytes(tmp_path, monkeypatch):
    run = task_run(tmp_path, "./build/native/app", native=True)
    result, calls, probes = native_call(run, monkeypatch)
    retain(run, result, tool="bash")
    assert result.succeeded
    assert len(calls) == 1
    assert len(probes) == 2
    assert completion(run).status == "complete"
    receipt = json.loads(
        next(
            raw
            for path, raw in run.fs.files.items()
            if path.endswith(result.metadata["receipt_id"] + ".json")
        )
    )
    assert receipt["argv"] == "./build/native/app"
    assert receipt["report_delta"] == {"new": [], "changed": []}
    assert "testcase_outcomes" not in receipt


@pytest.mark.parametrize("dispatch_status", [None, "completed_detached"])
def test_native_receipt_and_search_ref_bind_the_same_full_output(
    tmp_path, monkeypatch, dispatch_status
):
    run = task_run(tmp_path, "./build/native/app", native=True)
    full_output = "start\n" + "diagnostic\n" * 5000 + "MIDDLE_RESULT\n" + "tail\n" * 5000
    result, calls, _ = native_call(
        run,
        monkeypatch,
        full_output=full_output,
        dispatch_status=dispatch_status,
    )
    retain(run, result, tool="bash")

    assert len(calls) == 1
    assert result.raw_output == full_output
    assert completion(run).status == "complete"
    receipt = json.loads(
        next(
            raw
            for path, raw in run.fs.files.items()
            if path.endswith(result.metadata["receipt_id"] + ".json")
        )
    )
    assert receipt["output_content_hash"] == output_content_hash(full_output)


@pytest.mark.parametrize("change", ["binary", "script", "environment", "intent", "timeout"])
def test_unproven_native_run_does_not_gain_completion(tmp_path, monkeypatch, change):
    run = task_run(tmp_path, "./build/native/app", native=True)
    result, calls, _ = native_call(run, monkeypatch, change=change)
    retain(run, result, tool="bash")
    assert len(calls) == 1  # Evidence limits never refuse the actual Bash attempt.
    assert completion(run).status != "complete"


def test_native_nonzero_exit_is_a_failed_step_not_a_junit_case(tmp_path, monkeypatch):
    run = task_run(tmp_path, "./build/native/app", native=True)
    result, calls, _ = native_call(run, monkeypatch, exit_code=1)
    retain(run, result, tool="bash")
    closed = completion(run)
    assert len(calls) == 1
    assert closed.steps[0].status == "failed"
    assert result.test_stats is None


def test_shell_suffix_cannot_reuse_the_native_task_receipt(tmp_path, monkeypatch):
    run = task_run(tmp_path, "./build/native/app", native=True)
    result, calls, probes = native_call(run, monkeypatch, command="./build/native/app; true")
    retain(run, result, tool="bash")
    assert len(calls) == 1 and not probes
    assert completion(run).steps[0].status == "missing"


def test_executable_probe_reads_bytes_without_invoking_the_program(tmp_path):
    path = tmp_path / "app"
    body = b"\x7fELFfixture bytes, not an executable program"
    path.write_bytes(body)
    path.chmod(0o700)
    calls = []

    def execute(command):
        calls.append(command)
        argv = shlex.split(command)
        assert argv[:2] == ["python3", "-c"]
        result = subprocess.run(argv, capture_output=True, text=True)
        return {"exit_code": result.returncode, "output": result.stdout}

    assert (
        executable_sha256(execute, str(tmp_path), "./app", str(tmp_path))
        == hashlib.sha256(body).hexdigest()
    )
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    assert executable_sha256(execute, str(tmp_path), "./app", str(tmp_path)) is None
    assert len(calls) == 2
