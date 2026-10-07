import base64
import copy
import json
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from test_benchmark_requirement_evaluator import fixture, write

from sag.benchmark.evaluator import evaluate
from scripts.benchmark_native_observer import expected_shell_calls, native_result


def test_native_success_failure_pending_and_timeout_are_distinct():
    base = {
        "hook_event_name": "PostToolUse",
        "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
    }
    assert native_result("claude", base)["exit_code"] == 0
    pending = copy.deepcopy(base)
    pending["tool_response"]["backgroundTaskId"] = "background-1"
    assert native_result("claude", pending)["status"] == "pending"
    failure = {
        "hook_event_name": "PostToolUseFailure",
        "error": "Exit code 3\nBUILD FAILURE",
        "is_interrupt": False,
    }
    assert native_result("claude", failure)["exit_code"] == 3
    assert native_result("claude", {**failure, "is_interrupt": True})["status"] == "unavailable"
    for code, state in [(0, "completed"), (3, "completed"), (None, "unavailable")]:
        assert (
            native_result(
                "opencode", {"tool_response": {"output": "", "metadata": {"exit": code}}}
            )["status"]
            == state
        )


def native_runtime_fixture(fixture, *, java="17.0.12"):
    task, spec, record, run, base, score = fixture
    record.update(
        recorder_version="native-tool-hooks-v1",
        execution_origin="agent",
        project_root="/workspace/project",
        effective_argv=record["argv"],
    )
    launch = {
        "argv": ["/opt/maven/bin/mvn", *record["argv"][1:]],
        "root": record["project_root"],
        "cwd": record["project_root"],
        "commit": task["sha"],
    }
    rows = [
        {
            "event": "ObserverStarted",
            "launcher_observation": json.dumps(launch),
            "java_version": java,
            "java_home": "/jdk",
            "java_vendor": "Fixture",
            "process_id": "1",
            "run_id": "fixture",
        },
        {"event": "SessionStarted", "maven_version": "3.9.16"},
        {"event": "ObserverClosed", "closed_epoch_ms": 99000},
    ]
    record["runtime"]["native_launch"] = write(
        base, "native-events.jsonl", "\n".join(json.dumps(r) for r in rows)
    )
    record["native_process"] = {
        **write(base, "process.trace", "100.000000 +++ exited with 0 +++\n"),
        "namespace": "fixture",
        "process_id": 1,
        "exit_code": 0,
    }
    record["native_launcher_event"] = write(
        base,
        "launch-event.json",
        {
            "received_at": "1970-01-01T00:01:00+00:00",
            "native": {
                "hook_event_name": "MavenLaunch",
                "namespace": "fixture",
                "launch": {**launch, "launcher_pid": 1, "argv": record["effective_argv"]},
            },
        },
    )
    record["native_hook"] = {
        key: write(
            base,
            key + ".json",
            {
                "received_at": at,
                "native": {
                    "hook_event_name": "PreToolUse" if key == "before" else "PostToolUse",
                    "tool_name": "bash",
                    "tool_use_id": "call-1",
                    "session_id": "session-1",
                },
            },
        )
        for key, at in [
            ("before", "1970-01-01T00:00:00+00:00"),
            ("after", "1970-01-01T00:03:20+00:00"),
        ]
    }
    run["execution_origin"] = "agent"
    return task, spec, record, run, base, score


def test_actual_jvm_session_overrules_a_green_parent_probe(fixture):
    *_, score = native_runtime_fixture(fixture, java="1.8.0_402")
    result = score()
    assert result["status"] == "incomplete"
    runtime = next(r for r in result["preconditions"] if r["id"] == "runtime_conformance")
    assert runtime["status"] == "failed"


def test_actual_jvm_session_is_bound_to_invocation_and_archived_bytes(fixture):
    task, spec, record, run, base, score = native_runtime_fixture(fixture)
    assert score()["status"] == "complete"
    record["effective_argv"] = ["mvn", "test"]
    assert score()["status"] == "unavailable"
    record["effective_argv"] = record["argv"]
    (base / "native-events.jsonl").write_text("changed")
    assert score()["status"] == "unavailable"


def make_observer(fixture, tmp_path):
    from scripts.benchmark_native_observer import NativeObserver

    task, spec, record, run, source, _ = fixture
    root = source.parent / "checkout"
    # The fixture records its actual checkout in the boundary observations.
    boundary = json.loads((source / run["worktree"][0]["path"]).read_text())
    root = Path(boundary["project_root"])
    requests = []

    def execute(command, **kwargs):
        args = shlex.split(command)
        request = json.loads(args[-1])
        if request.get("operation") == "probe":
            requests.append(request["argv"])
            assert request["argv"] == ["mvn", "--version"]
            result = {
                "head": task["sha"],
                "errors": [],
                "files": [],
                "configs": [],
                "probe_stdout": base64.b64encode(
                    b"Apache Maven 3.9.16\nJava version: 17.0.12\n"
                ).decode(),
                "probe_stderr": "",
                "probe_exit_code": 0,
                "probe_executable": "/opt/maven/bin/mvn",
            }
            return {"success": True, "exit_code": 0, "output": json.dumps(result)}
        answer = subprocess.run(args, capture_output=True, text=True)
        return {
            "success": answer.returncode == 0,
            "exit_code": answer.returncode,
            "output": answer.stdout,
        }

    observer = NativeObserver(
        base=tmp_path / "native",
        arm="opencode",
        run_id=run["run_id"],
        root=str(root),
        task=task,
        spec=spec,
        execute=execute,
        read_file=lambda p: Path(p).read_bytes(),
        list_traces=lambda: [],
        copy_trace=lambda *a: None,
    )

    def launch(call):
        body = [
            {
                "event": "ObserverStarted",
                "java_version": "17.0.12",
                "java_home": "/jdk17",
                "process_id": "123",
                "run_id": "fixture",
                "launcher_observation": json.dumps(
                    {
                        "argv": ["/opt/maven/bin/mvn", "verify"],
                        "cwd": str(root),
                        "root": str(root),
                        "commit": task["sha"],
                    }
                ),
            },
            {"event": "SessionStarted", "maven_version": "3.9.16"},
            {"event": "ObserverClosed", "closed_epoch_ms": 0},
        ]
        return write(
            observer.base,
            "launch-" + call["invocation_id"] + ".jsonl",
            "\n".join(json.dumps(r) for r in body),
        )

    observer._native_launch = launch
    event = {
        "tool_name": "bash",
        "tool_use_id": "native-1",
        "session_id": "session-1",
        "cwd": str(root),
        "tool_input": {"command": "mvn verify"},
        "hook_event_name": "PreToolUse",
        "observer_environment": {"MAVEN_SKIP_RC": "true"},
    }
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        json.dumps({"type": "tool_use", "part": {"tool": "bash", "callID": "native-1"}}) + "\n"
    )
    event["launcher_event"] = {
        "hook_event_name": "MavenLaunch",
        "namespace": "fixture",
        "observer_environment": {"MAVEN_SKIP_RC": "true"},
        "launch": {
            "argv": ["mvn", "verify"],
            "cwd": str(root),
            "root": str(root),
            "commit": task["sha"],
            "launcher_pid": 123,
        },
    }
    original_read = observer.read_file

    def read(p):
        if p.endswith("process.123"):
            event = json.loads((observer.base / observer.events[-1]["path"]).read_text())
            epoch = datetime.fromisoformat(event["received_at"]).timestamp() - 0.000001
            return f"{epoch:.6f} +++ exited with 0 +++\n".encode()
        return original_read(p)

    observer.read_file = read
    return observer, root, event, transcript, requests


def test_passive_observer_binds_real_snapshots_and_never_dispatches_task(fixture, tmp_path):
    observer, root, event, transcript, requests = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(event["launcher_event"])
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    log = (fixture[4] / "log.txt").read_text()
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": log, "metadata": {"exit": 0}},
        }
    )
    run = observer.close(transcript)
    assert not run["capture_errors"]
    assert len(run["invocations"]) == 1
    score = evaluate(
        observer.task, observer.spec, run, observer.base, required_execution_origin="agent"
    )
    assert score["status"] == "complete", score
    assert requests == [["mvn", "--version"]]


def test_pending_native_command_cannot_collect_late_outputs(fixture, tmp_path):
    observer, root, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": "", "metadata": {"exit": None}},
        }
    )
    run = observer.close(transcript)
    record = json.loads((observer.base / run["invocations"][0]["path"]).read_text())
    assert record["status"] == "unavailable" and record["log_complete"] is False
    assert "reports" not in record
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] != "complete"


def test_missing_hook_coverage_does_not_silently_become_no_task_failure(fixture, tmp_path):
    observer, _, _, transcript, _ = make_observer(fixture, tmp_path)
    run = observer.close(transcript)
    assert run["capture_errors"] and run["invocations"] == []
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == "unavailable"


@pytest.mark.parametrize("launch_order", ["before_ack", "after_ack", "after_wait_start"])
@pytest.mark.parametrize("terminal_evidence", ["complete", "missing_exit", "missing_wait"])
def test_native_background_wait_binds_to_original_invocation(
    fixture, tmp_path, launch_order, terminal_evidence
):
    observer, root, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.arm = "claude"
    event["tool_name"] = "Bash"
    observer.event(event)
    if launch_order == "before_ack":
        observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"stdout": "", "interrupted": False, "backgroundTaskId": "job-1"},
        }
    )
    if launch_order == "after_ack":
        observer.event(event["launcher_event"])
    assert not observer.invocations
    wait = {
        **event,
        "tool_name": "TaskOutput",
        "tool_use_id": "wait-1",
        "tool_input": {"task_id": "job-1", "block": True},
    }
    observer.event(wait)
    if launch_order == "after_wait_start":
        observer.event(event["launcher_event"])
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    completed = {
            **wait,
            "hook_event_name": "PostToolUse",
            "tool_response": {
                "retrieval_status": "success",
                "task": {
                    "task_id": "job-1",
                    "task_type": "local_bash",
                    "status": "completed",
                    "exitCode": 0,
                    "output": (fixture[4] / "log.txt").read_text(),
                },
            },
        }
    if terminal_evidence == "missing_exit":
        observer.read_file = lambda path: b"exit_group(0) = ?\n"
    if terminal_evidence != "missing_wait":
        observer.event(completed)
    transcript.write_text(
        json.dumps(
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": name, "id": ident}
                        for name, ident in [("Bash", "native-1"), ("TaskOutput", "wait-1")]
                    ]
                }
            }
        )
    )
    run = observer.close(transcript)
    assert not run["capture_errors"]
    expected = "complete" if terminal_evidence == "complete" else "unavailable"
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == expected


@pytest.mark.parametrize("prior_shell", ["completed", "two_pending", "unrelated_background_completed"])
def test_late_launch_does_not_reuse_completed_or_ambiguous_shells(fixture, tmp_path, prior_shell):
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.arm = "claude"
    event["tool_name"] = "Bash"
    observer.event(event)
    response = {"stdout": "", "interrupted": False}
    if prior_shell != "completed":
        response["backgroundTaskId"] = "job-1"
    observer.event({**event, "hook_event_name": "PostToolUse", "tool_response": response})
    if prior_shell == "two_pending":
        another = {**event, "tool_use_id": "other-shell"}
        observer.event(another)
        observer.event({**another, "hook_event_name": "PostToolUse", "tool_response": {
            **response, "backgroundTaskId": "job-2",
        }})
    elif prior_shell == "unrelated_background_completed":
        wait = {**event, "tool_name": "TaskOutput", "tool_use_id": "wait-1",
                "tool_input": {"task_id": "job-1", "block": True}}
        observer.event(wait)
        observer.event({**wait, "hook_event_name": "PostToolUse", "tool_response": {
            "retrieval_status": "success", "task": {
                "task_id": "job-1", "task_type": "local_bash", "status": "completed",
                "exitCode": 0, "output": "Dependency installation finished",
            },
        }})
        assert not observer.errors
    observer.event(event["launcher_event"])
    assert observer.errors[-1]["error"] == "Native Maven launch has no unique active shell tool"
    assert not observer.invocations


def test_compound_shell_cannot_mask_actual_maven_exit(fixture, tmp_path):
    observer, root, event, transcript, _ = make_observer(fixture, tmp_path)
    event["tool_input"]["command"] = "mkdir -p /tmp/logs; mvn verify; echo done; exit 0"
    reader = observer.read_file
    observer.read_file = lambda path: reader(path).replace(b"exited with 0", b"exited with 1")
    observer.event(event)
    observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {
                "output": (fixture[4] / "log.txt").read_text(),
                "metadata": {"exit": 0},
            },
        }
    )
    run = observer.close(transcript)
    score = evaluate(observer.task, observer.spec, run, observer.base)
    assert score["status"] == "incomplete"
    assert score["commands"][0]["status"] == "failed"


def test_tool_success_without_native_launch_is_not_build_success(fixture, tmp_path):
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": "BUILD SUCCESS", "metadata": {"exit": 0}},
        }
    )
    run = observer.close(transcript)
    assert run["invocations"] == []
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == "unavailable"


def test_incomplete_os_trace_cannot_certify_success(fixture, tmp_path):
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.read_file = lambda path: b"1790000000.000001 exit_group(0) = ?\n"
    observer.event(event)
    observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": "BUILD SUCCESS", "metadata": {"exit": 0}},
        }
    )
    run = observer.close(transcript)
    record = json.loads((observer.base / run["invocations"][0]["path"]).read_text())
    assert record["status"] == "unavailable"
    assert not record["log_complete"]
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == "unavailable"


def test_removed_native_process_evidence_cannot_improve_or_preserve_success(fixture):
    task, spec, record, run, base, score = native_runtime_fixture(fixture)
    assert score()["status"] == "complete"
    del record["native_process"]
    assert score()["status"] == "unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "another-session"),
        ("tool_use_id", "another-call"),
        ("hook_event_name", "PreToolUse"),
    ],
)
def test_native_terminal_from_another_call_cannot_certify_success(fixture, field, value):
    _, _, record, _, base, score = native_runtime_fixture(fixture)
    assert score()["status"] == "complete"
    ref = record["native_hook"]["after"]
    event = json.loads((base / ref["path"]).read_text())
    event["native"][field] = value
    record["native_hook"]["after"] = write(base, "wrong-after.json", event)
    assert score()["status"] == "unavailable"


def test_late_notifications_and_repeated_close_cannot_mutate_sealed_evidence(fixture, tmp_path):
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    original = observer.close(transcript)
    before = {p: p.read_bytes() for p in observer.base.rglob("*") if p.is_file()}
    observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": "BUILD SUCCESS", "metadata": {"exit": 0}},
        }
    )
    assert observer.close(transcript) == original
    assert before == {p: p.read_bytes() for p in observer.base.rglob("*") if p.is_file()}


def test_parallel_metadata_probes_do_not_create_a_build_capture_conflict(fixture, tmp_path):
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event({**event, "tool_use_id": "other"})
    launch = copy.deepcopy(event["launcher_event"])
    launch["launch"]["argv"] = ["mvn", "--version"]
    observer.event(launch)
    assert observer.errors == []


def test_unobserved_retry_never_uses_prior_success(fixture, tmp_path):
    observer, root, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(event["launcher_event"])
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {
                "output": (fixture[4] / "log.txt").read_text(),
                "metadata": {"exit": 0},
            },
        }
    )
    again = {**event, "tool_use_id": "native-2"}
    observer.event(again)
    observer.event(event["launcher_event"])
    # The final attempt has started, but no terminal observation exists.
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "tool_use", "part": {"tool": "bash", "callID": ident}})
            for ident in ("native-1", "native-2")
        )
    )
    run = observer.close(transcript)
    assert len(run["all_invocations"]) == 2 and len(run["invocations"]) == 1
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == "unavailable"


def test_successful_retry_retains_failure_but_scores_only_the_final_attempt(fixture, tmp_path):
    observer, root, event, transcript, _ = make_observer(fixture, tmp_path)
    reader = observer.read_file
    observer.read_file = lambda path: reader(path).replace(b"exited with 0", b"exited with 1")
    observer.event(event)
    observer.event(event["launcher_event"])
    observer.event(
        {
            **event,
            "hook_event_name": "PostToolUse",
            "tool_response": {"output": "[INFO] BUILD FAILURE\n", "metadata": {"exit": 1}},
        }
    )
    observer.read_file = reader
    again = {**event, "tool_use_id": "native-2"}
    observer.event(again)
    observer.event(event["launcher_event"])
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    observer.event(
        {
            **again,
            "hook_event_name": "PostToolUse",
            "tool_response": {
                "output": (fixture[4] / "log.txt").read_text(),
                "metadata": {"exit": 0},
            },
        }
    )
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "tool_use", "part": {"tool": "bash", "callID": identity}})
            for identity in ("native-1", "native-2")
        )
    )
    run = observer.close(transcript)
    attempts = [
        json.loads((observer.base / ref["path"]).read_text()) for ref in run["all_invocations"]
    ]
    assert [attempt["exit_code"] for attempt in attempts] == [1, 0]
    assert run["invocations"] == run["all_invocations"][-1:]
    assert evaluate(observer.task, observer.spec, run, observer.base)["status"] == "complete"
