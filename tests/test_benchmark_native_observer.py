import base64
import copy
import json
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from test_benchmark_requirement_evaluator import banner, fixture, write

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


def test_parallel_probe_shell_does_not_hide_the_shell_that_runs_the_step(fixture, tmp_path):
    """OpenCode opens `mvn -version` and `mvn verify` shells at once; the launch binds to the latter."""
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    observer.event(event)
    probe = {**event, "tool_use_id": "probe", "tool_input": {"command": "mvn -version"}}
    observer.event(probe)
    observer.event(event["launcher_event"])
    assert observer.errors == []
    assert observer.calls["native-1"]["selected_step"] is not None
    assert observer.calls["probe"]["selected_step"] is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("mvn verify", True),
        ("/opt/maven/bin/mvn verify", True),
        ("cd /workspace/project && mvn verify 2>&1 | tail -50", True),
        ("mvn -version", False),
        ("mvn verify -DskipTests", False),
        ("mvn clean verify", False),
        ("echo mvn verify", False),
        ("mvn verify > build.log 2>&1", True),
        ("(cd sub; mvn verify)", True),
        ("", False),
        # Leading variable assignments belong to the same simple command.
        ("PATH=/tmp/opencode/tools/apache-maven-3.9.11/bin:$PATH mvn verify", True),
        ("JAVA_HOME=/jdk MAVEN_OPTS='-Xmx4g -Xms1g' mvn verify > build.log 2>&1", True),
        ("cd /workspace/project && LC_ALL=C.UTF-8 PATH=/opt/maven/bin:$PATH mvn verify", True),
        ("env PATH=/opt/maven/bin:$PATH mvn verify", True),
        ("JAVA_HOME=/jdk env PATH=/opt/maven/bin:$PATH mvn verify", True),
        ("PATH=/opt/maven/bin:$PATH mvn verify -DskipTests", False),
        ("PATH=/opt/maven/bin:$PATH mvn -version", False),
        ("echo PATH=/opt/maven/bin mvn verify", False),
        ("echo env mvn verify", False),
        ("-DPATH=/opt mvn verify", False),
        # A launcher spelled with a version suffix, as launcher_matches reads it.
        ("PATH=/tmp/opencode:$PATH /tmp/opencode/mvn-3.9.16 verify > /tmp/opencode/ci-step-1.log 2>&1", True),
        ("mvn-3.9.16 verify", True),
        ("/tmp/opencode/mvn-latest verify", False),
        ("/tmp/opencode/mvnw-3.9.16 verify", False),
    ],
)
def test_shell_names_step_matches_the_frozen_argv_as_contiguous_tokens(command, expected):
    from scripts.benchmark_native_observer import shell_names_step

    steps = [{"argv": ["mvn", "verify"], "cwd": "."}]
    assert shell_names_step({"command": command}, steps) is expected


def test_shell_names_step_reads_past_a_leading_path_assignment_commons_net():
    """commons-net-opencode, events 000010/000011: the version check and the pinned build
    were open together, each command prefixed with PATH=...; only the build names the step."""
    from scripts.benchmark_native_observer import shell_names_step

    steps = [{"argv": ["mvn", "-B", "-f", "pom.xml", "-V", "clean", "install",
                       "--no-transfer-progress", "--batch-mode"], "cwd": "."}]
    probe = ("PATH=/tmp/opencode/tools/apache-maven-3.9.11/bin:$PATH java -version && "
             "PATH=/tmp/opencode/tools/apache-maven-3.9.11/bin:$PATH mvn -version")
    build = ("PATH=/tmp/opencode/tools/apache-maven-3.9.11/bin:$PATH mvn -B -f pom.xml -V "
             "clean install --no-transfer-progress --batch-mode")
    assert shell_names_step({"command": probe}, steps) is False
    assert shell_names_step({"command": build}, steps) is True


def test_parallel_probe_with_path_prefix_does_not_hide_the_prefixed_build_shell(fixture, tmp_path):
    """Both shells carry a PATH= prefix (commons-net-opencode); the launch binds to the build."""
    observer, _, event, transcript, _ = make_observer(fixture, tmp_path)
    prefix = "PATH=/tmp/opencode/tools/apache-maven-3.9.11/bin:$PATH "
    probe = {**event, "tool_use_id": "probe", "tool_input": {
        "command": prefix + "java -version && " + prefix + "mvn -version"}}
    build = {**event, "tool_input": {"command": prefix + "mvn verify"}}
    observer.event(probe)
    observer.event(build)
    observer.event(event["launcher_event"])
    assert observer.errors == []
    assert observer.calls["native-1"]["selected_step"] is not None
    assert observer.calls["probe"]["selected_step"] is None


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


def test_expected_shell_calls_skip_claude_system_notices_with_string_messages(tmp_path):
    """A permission denial is a system event whose message is a string; it must not crash
    the transcript scan, and its denied tool call still counts as an expected call because
    Claude Code fires the PreToolUse hook before the permission decision."""
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init", "cwd": "/workspace/project"}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_denied", "name": "Bash", "input": {"command": "rm -rf x"}}]}}),
        json.dumps({"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
                    "tool_use_id": "call_denied", "message": "Permission to use Bash has been denied"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "plain string content"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I am blocked."}]}}),
        json.dumps({"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]) + "\n")
    assert expected_shell_calls("claude", transcript) == {"call_denied"}


def test_launcher_matches_binds_wrapper_distribution_only_to_mvnw_steps():
    from scripts.benchmark_native_observer import launcher_matches

    dist = "/root/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f/bin/mvn"
    assert launcher_matches("./mvnw", "./mvnw")
    assert launcher_matches("./mvnw", dist)
    assert launcher_matches("mvn", dist)  # the wrapper's Maven is still a Maven on an absolute path
    assert launcher_matches("mvn", "/usr/bin/mvn")
    assert not launcher_matches("./mvnw", "/usr/bin/mvn")
    assert not launcher_matches("./mvnw", "/opt/apache-maven-3.9.16/bin/mvn")
    assert not launcher_matches("./mvnw", "/root/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f/mvn")
    assert not launcher_matches("./mvnw", "/root/.m2/wrapper/dists/x/bin/mvn")
    assert not launcher_matches("./mvnw", "/root/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f/bin/mvnDebug")
    assert not launcher_matches("mvn", "mvnw")
    assert not launcher_matches("./gradlew", "/root/.gradle/wrapper/dists/gradle-8.5-bin/abc/gradle-8.5/bin/gradle")


def test_launcher_matches_reads_a_version_suffix_on_a_mvn_launcher_name():
    """struts-examples-opencode ran Maven through a symlink named /tmp/opencode/mvn-3.9.16.
    The witness records the launcher as invoked ($0), not its resolved target, so the
    rule reads the name: `mvn-<version>` is a Maven launcher for a `mvn` step only."""
    from scripts.benchmark_native_observer import launcher_matches

    assert launcher_matches("mvn", "/tmp/opencode/mvn-3.9.16")
    assert launcher_matches("mvn", "/opt/bin/mvn-3")
    assert not launcher_matches("mvn", "mvn-3.9.16")  # a launcher the witness saw resolved is absolute
    assert not launcher_matches("mvn", "/tmp/opencode/mvn-latest")
    assert not launcher_matches("mvn", "/tmp/opencode/mvn-3.9.16-wrapper")
    assert not launcher_matches("mvn", "/tmp/opencode/mvn3.9.16")
    assert not launcher_matches("mvn", "/tmp/opencode/mvnw-3.9.16")
    assert not launcher_matches("mvn", "/tmp/opencode/mvnDebug-3.9.16")
    assert not launcher_matches("./mvnw", "/tmp/opencode/mvn-3.9.16")
    assert not launcher_matches("./mvnw", "/root/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f/bin/mvn-3.9.16")


def _mvnw_observer(fixture, tmp_path, observed_launcher):
    observer, root, event, transcript, requests = make_observer(fixture, tmp_path)
    observer.task = copy.deepcopy(observer.task)
    step = observer.task["steps"][0]
    step["argv"] = ["./mvnw", *step["argv"][1:]]
    observer.observer.task = observer.task

    def execute(command, **kwargs):
        args = shlex.split(command)
        request = json.loads(args[-1])
        if request.get("operation") == "probe":
            requests.append(request["argv"])
            result = {"head": observer.task["sha"], "errors": [], "files": [], "configs": [],
                      "probe_stdout": base64.b64encode(b"Apache Maven 3.9.16\nJava version: 17.0.12\n").decode(),
                      "probe_stderr": "", "probe_exit_code": 0, "probe_executable": request["argv"][0]}
            return {"success": True, "exit_code": 0, "output": json.dumps(result)}
        answer = subprocess.run(args, capture_output=True, text=True)
        return {"success": answer.returncode == 0, "exit_code": answer.returncode, "output": answer.stdout}

    observer.execute_raw = execute
    event = copy.deepcopy(event)
    event["tool_input"] = {"command": "./mvnw " + " ".join(step["argv"][1:])}
    event["launcher_event"]["launch"]["argv"] = [observed_launcher, *step["argv"][1:]]
    return observer, root, event, transcript, requests


def test_wrapper_distribution_launch_binds_to_the_mvnw_step(fixture, tmp_path):
    dist = "/root/.m2/wrapper/dists/apache-maven-3.9.16/56ba1f9f/bin/mvn"
    observer, root, event, transcript, requests = _mvnw_observer(fixture, tmp_path, dist)
    observer.event(event)
    observer.event(event["launcher_event"])
    call = observer.calls["native-1"]
    assert call["selected_step"] is observer.task["steps"][0]
    assert call["contract"]["observed_launcher"] == dist
    assert requests[-1] == [dist, "--version"]
    assert observer.errors == []


def test_system_maven_launch_never_binds_to_a_mvnw_step(fixture, tmp_path):
    observer, root, event, transcript, requests = _mvnw_observer(fixture, tmp_path, "/usr/bin/mvn")
    observer.event(event)
    observer.event(event["launcher_event"])
    assert observer.calls["native-1"]["selected_step"] is None
    assert observer.errors == []
    run = observer.close(transcript)
    assert run["invocations"] == []


def _witness_from_launch(observer):
    """Stand-in for the Maven witness trace: the launch exactly as the callback reported it."""

    def native_launch(call):
        body = [
            {
                "event": "ObserverStarted",
                "java_version": "17.0.12",
                "java_home": "/jdk17",
                "process_id": str(call["process_id"]),
                "run_id": call["namespace"],
                "launcher_observation": json.dumps(
                    {"argv": call["parsed"]["argv"], "cwd": call["parsed"]["cwd"],
                     "root": observer.root, "commit": observer.task["sha"]}
                ),
            },
            {"event": "SessionStarted", "maven_version": "3.9.16"},
            {"event": "ObserverClosed", "closed_epoch_ms": 0},
        ]
        return write(observer.base, "launch-" + call["invocation_id"] + ".jsonl",
                     "\n".join(json.dumps(r) for r in body))

    return native_launch


def _probe_any_launcher(observer, requests):
    task = observer.task

    def execute(command, **kwargs):
        args = shlex.split(command)
        request = json.loads(args[-1])
        if request.get("operation") == "probe":
            requests.append(request["argv"])
            result = {"head": task["sha"], "errors": [], "files": [], "configs": [],
                      "probe_stdout": base64.b64encode(b"Apache Maven 3.9.16\nJava version: 17.0.12\n").decode(),
                      "probe_stderr": "", "probe_exit_code": 0, "probe_executable": request["argv"][0]}
            return {"success": True, "exit_code": 0, "output": json.dumps(result)}
        answer = subprocess.run(args, capture_output=True, text=True)
        return {"success": answer.returncode == 0, "exit_code": answer.returncode, "output": answer.stdout}

    observer.execute_raw = execute


def test_versioned_launcher_symlink_binds_and_scores_struts_examples(fixture, tmp_path):
    """struts-examples-opencode, events 000019/000020: the pinned arguments through
    /tmp/opencode/mvn-3.9.16 in the pinned directory were treated as exploration."""
    observer, root, event, transcript, requests = make_observer(fixture, tmp_path)
    _probe_any_launcher(observer, requests)
    observer._native_launch = _witness_from_launch(observer)
    launcher = "/tmp/opencode/mvn-3.9.16"
    event["tool_input"] = {"command": (
        "PATH=/tmp/opencode:$PATH JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 "
        "MAVEN_OPTS='-Dmaven.repo.local=/tmp/opencode/m2repo' " + launcher + " verify")}
    event["launcher_event"]["launch"]["argv"] = [launcher, "verify"]
    observer.event(event)
    observer.event(event["launcher_event"])
    assert observer.errors == []
    assert observer.calls["native-1"]["contract"]["observed_launcher"] == launcher
    assert requests == [[launcher, "--version"]]
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    observer.event({**event, "hook_event_name": "PostToolUse", "tool_response": {
        "output": (fixture[4] / "log.txt").read_text(), "metadata": {"exit": 0}}})
    run = observer.close(transcript)
    assert not run["capture_errors"] and len(run["invocations"]) == 1
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert score["status"] == "complete", score


def _two_step_observer(fixture, tmp_path):
    """google-java-format-opencode's shape: `mvn install` then `mvn verify`, one shell call."""
    from sag.benchmark.requirements import canonical_digest, normalize_task

    from test_benchmark_requirement_evaluator import requirement

    task, spec = fixture[0], fixture[1]
    task["steps"].insert(0, {"id": "install", "runner": "maven", "argv": ["mvn", "install"], "cwd": "."})
    spec["task_sha256"] = canonical_digest(normalize_task(task))
    spec["requirements"].insert(0, requirement(
        "install-compile", "compile", "compiler:compile", step_id="install"))
    observer, root, event, transcript, requests = make_observer(fixture, tmp_path)
    _probe_any_launcher(observer, requests)
    observer._native_launch = _witness_from_launch(observer)
    logs = tmp_path / "agent-logs"
    logs.mkdir()
    event["tool_input"] = {"command": (
        "set -euo pipefail\n"
        f"PATH=/opt/maven/bin:$PATH mvn install > {logs}/ci-step-1.log 2>&1\n"
        f"PATH=/opt/maven/bin:$PATH mvn verify > {logs}/ci-step-2.log 2>&1")}
    launches = []
    for pid, argv, name in [(123, ["mvn", "install"], "ci-step-1.log"), (456, ["mvn", "verify"], "ci-step-2.log")]:
        launch = copy.deepcopy(event["launcher_event"])
        launch["launch"].update(argv=["/opt/maven/bin/" + argv[0], *argv[1:]], launcher_pid=pid)
        launch["output_path"] = str(logs / name)
        launches.append(launch)
    exits = {}  # pid -> (exit code, seconds after its launch callback, or None while running)

    def launch_epoch(pid):
        for ref in observer.events:
            row = json.loads((observer.base / ref["path"]).read_text())
            if row["native"].get("launch", {}).get("launcher_pid") == pid:
                return datetime.fromisoformat(row["received_at"]).timestamp()
        raise FileNotFoundError(pid)

    def read(path):
        if "/opt/benchmark-process-traces/process." in path:
            pid = int(path.rsplit(".", 1)[1])
            code, after = exits.get(pid, (None, None))
            if after is None:
                return f"{launch_epoch(pid):.6f} execve(...) = 0\n".encode()
            return f"{launch_epoch(pid) + after:.6f} +++ exited with {code} +++\n".encode()
        return Path(path).read_bytes()

    observer.read_file = read
    return observer, root, event, transcript, launches, exits, logs


def _write_reports(root):
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )


def test_sequential_pinned_builds_in_one_shell_call_are_two_invocations(fixture, tmp_path):
    """google-java-format-opencode, events 000044-000048: steps 1 and 2 one after the other
    in one shell call. Each launch gets its own invocation and physical boundaries; the
    first launch's after boundary is taken while the second waits in its launcher callback."""
    import time

    observer, root, event, transcript, launches, exits, logs = _two_step_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(launches[0])
    (logs / "ci-step-1.log").write_text(
        banner("compiler:compile") + "[INFO] Compiling 2 source files\n[INFO] BUILD SUCCESS\n")
    time.sleep(0.05)
    exits[123] = (0, 0.01)
    observer.event(launches[1])
    assert observer.errors == []
    _write_reports(root)
    (logs / "ci-step-2.log").write_text((fixture[4] / "log.txt").read_text())
    time.sleep(0.05)
    exits[456] = (0, 0.01)
    observer.event({**event, "hook_event_name": "PostToolUse",
                    "tool_response": {"output": "", "metadata": {"exit": 0}}})
    run = observer.close(transcript)
    assert not run["capture_errors"]
    records = [json.loads((observer.base / ref["path"]).read_text()) for ref in run["invocations"]]
    assert [r["step_id"] for r in records] == ["install", "verify"]
    assert records[0]["invocation_id"] != records[1]["invocation_id"]
    assert records[0]["sequence"] < records[1]["sequence"]
    assert [r["exit_code"] for r in records] == [0, 0]
    assert [r["log_source"] for r in records] == [
        {"kind": "native_file", "path": str(logs / "ci-step-1.log")},
        {"kind": "native_file", "path": str(logs / "ci-step-2.log")},
    ]
    assert all(r["log_complete"] for r in records)
    assert records[0]["native_hook"] == records[1]["native_hook"]  # one shell call
    boundaries = [
        (snapshot["boundary"], snapshot.get("invocation_id"))
        for snapshot in (json.loads((observer.base / ref["path"]).read_text()) for ref in run["worktree"])
    ]
    first, second = records[0]["invocation_id"], records[1]["invocation_id"]
    assert boundaries == [
        ("task_start", None),
        ("acceptance_before", first),
        ("acceptance_after", first),
        ("acceptance_before", second),
        ("acceptance_after", second),
        ("evidence_close", None),
    ]
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert [c["status"] for c in score["commands"]] == ["passed", "passed"], score["commands"]
    assert score["status"] == "complete", score


@pytest.mark.parametrize("earlier", ["still_running", "exited_after_second_launch"])
def test_overlapping_pinned_builds_in_one_shell_call_stay_refused(fixture, tmp_path, earlier):
    """`mvn install & mvn verify`: the first build had not ended when the second started."""
    observer, _, event, transcript, launches, exits, _ = _two_step_observer(fixture, tmp_path)
    observer.event(event)
    observer.event(launches[0])
    if earlier == "exited_after_second_launch":
        exits[123] = (0, 60.0)
    observer.event(launches[1])
    assert observer.errors[-1]["error"] == "Multiple build launches in one tool need separate physical boundaries"
    assert observer.close(transcript)["invocations"] == []


@pytest.mark.parametrize("order", [("verify", "install"), ("install", "install")])
def test_pinned_builds_in_one_shell_call_must_follow_the_step_order(fixture, tmp_path, order):
    import time

    observer, _, event, transcript, launches, exits, _ = _two_step_observer(fixture, tmp_path)
    by_step = {"install": launches[0], "verify": launches[1]}
    first, second = copy.deepcopy(by_step[order[0]]), copy.deepcopy(by_step[order[1]])
    first["launch"]["launcher_pid"], second["launch"]["launcher_pid"] = 123, 456
    observer.event(event)
    observer.event(first)
    time.sleep(0.05)
    exits[123] = (0, 0.01)
    observer.event(second)
    assert observer.errors[-1]["error"] == "Multiple build launches in one tool need separate physical boundaries"


def test_sequential_builds_sharing_the_shell_output_have_no_complete_log(fixture, tmp_path):
    """Without a file of its own, a launch's output is mixed with the other pinned build's
    in the shell's output, so neither launch can claim that text as its log."""
    import time

    observer, root, event, transcript, launches, exits, _ = _two_step_observer(fixture, tmp_path)
    for launch in launches:
        launch["output_path"] = None
    observer.event(event)
    observer.event(launches[0])
    time.sleep(0.05)
    exits[123] = (0, 0.01)
    observer.event(launches[1])
    _write_reports(root)
    time.sleep(0.05)
    exits[456] = (0, 0.01)
    observer.event({**event, "hook_event_name": "PostToolUse", "tool_response": {
        "output": "[INFO] BUILD SUCCESS\n" + (fixture[4] / "log.txt").read_text(), "metadata": {"exit": 0}}})
    run = observer.close(transcript)
    assert not run["capture_errors"] and len(run["invocations"]) == 2
    records = [json.loads((observer.base / ref["path"]).read_text()) for ref in run["invocations"]]
    assert [r["log_complete"] for r in records] == [False, False]
    assert [r["exit_code"] for r in records] == [0, 0]
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert [c["status"] for c in score["commands"]] == ["unavailable", "unavailable"]


def test_sequential_builds_in_a_backgrounded_claude_shell_close_on_the_task_wait(fixture, tmp_path):
    """Claude Code moves a long shell call to the background; its later TaskOutput
    completion closes every pinned launch of that call."""
    import time

    observer, root, event, transcript, launches, exits, logs = _two_step_observer(fixture, tmp_path)
    observer.arm = "claude"
    event["tool_name"] = "Bash"
    observer.event(event)
    observer.event(launches[0])
    (logs / "ci-step-1.log").write_text(
        banner("compiler:compile") + "[INFO] Compiling 2 source files\n[INFO] BUILD SUCCESS\n")
    observer.event({**event, "hook_event_name": "PostToolUse", "tool_response": {
        "stdout": "", "interrupted": False, "backgroundTaskId": "job-1"}})
    time.sleep(0.05)
    exits[123] = (0, 0.01)
    observer.event(launches[1])
    assert observer.errors == []
    _write_reports(root)
    (logs / "ci-step-2.log").write_text((fixture[4] / "log.txt").read_text())
    wait = {**event, "tool_name": "TaskOutput", "tool_use_id": "wait-1",
            "tool_input": {"task_id": "job-1", "block": True}}
    observer.event(wait)
    time.sleep(0.05)
    exits[456] = (0, 0.01)
    observer.event({**wait, "hook_event_name": "PostToolUse", "tool_response": {
        "retrieval_status": "success", "task": {
            "task_id": "job-1", "task_type": "local_bash", "status": "completed",
            "exitCode": 0, "output": ""}}})
    transcript.write_text(json.dumps({"message": {"content": [
        {"type": "tool_use", "name": name, "id": ident}
        for name, ident in [("Bash", "native-1"), ("TaskOutput", "wait-1")]]}}))
    run = observer.close(transcript)
    assert not run["capture_errors"] and len(run["invocations"]) == 2
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert [c["status"] for c in score["commands"]] == ["passed", "passed"], score["commands"]


def _claude_transcript(tmp_path, tool_input, result):
    transcript = tmp_path / "agent.jsonl"
    lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_x", "name": "Bash", "input": tool_input}]}})]
    if result is not None:
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_x", **result}]}}))
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


def test_bash_call_rejected_by_input_validation_is_not_expected_to_reach_the_hooks(tmp_path):
    """curator-claude-r5: Claude Code rejected `Bash` with an unknown `workdir` parameter before
    PreToolUse fires. The call ran nothing, and its own result says so."""
    transcript = _claude_transcript(
        tmp_path,
        {"command": "java -version && ./mvnw -version", "timeout": 7200000, "workdir": "/workspace/project"},
        {"is_error": True, "content": "<tool_use_error>InputValidationError: Bash failed due to the following "
                                      "issue:\nAn unexpected parameter `workdir` was provided</tool_use_error>"},
    )
    assert expected_shell_calls("claude", transcript) == set()


def test_bash_call_with_any_other_error_is_still_expected(tmp_path):
    transcript = _claude_transcript(
        tmp_path, {"command": "mvn -B verify"}, {"is_error": True, "content": "Exit code 1\n[ERROR] BUILD FAILURE"}
    )
    assert expected_shell_calls("claude", transcript) == {"call_x"}


def test_bash_call_without_a_result_is_still_expected(tmp_path):
    transcript = _claude_transcript(tmp_path, {"command": "mvn -B verify"}, None)
    assert expected_shell_calls("claude", transcript) == {"call_x"}


def test_validation_wording_inside_a_successful_result_does_not_exempt_the_call(tmp_path):
    transcript = _claude_transcript(
        tmp_path, {"command": "cat notes.txt"},
        {"is_error": False, "content": "<tool_use_error>InputValidationError: copied text</tool_use_error>"},
    )
    assert expected_shell_calls("claude", transcript) == {"call_x"}
