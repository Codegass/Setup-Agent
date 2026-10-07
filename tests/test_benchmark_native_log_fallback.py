import json
from datetime import datetime

import pytest
from test_benchmark_native_observer import make_observer
from test_benchmark_requirement_evaluator import fixture

from sag.benchmark.evaluator import evaluate
from scripts.benchmark_native_observer import collect_native_log, native_result


@pytest.mark.parametrize("truncation", ["none", "flag", "marker"])
def test_missing_transient_file_uses_only_complete_native_payload(fixture, tmp_path, truncation):
    observer, root, event, transcript, probes = make_observer(fixture, tmp_path)
    observer.event(event)
    observer.event({**event["launcher_event"], "output_path": str(tmp_path / "deleted.output")})
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>'
    )
    text = (fixture[4] / "log.txt").read_text()
    if truncation == "marker":
        text += "\n... [3929 characters truncated] ...\n"
    observer.event({**event, "hook_event_name": "PostToolUse", "tool_response": {
        "output": text, "metadata": {"exit": 0, "truncated": truncation == "flag"},
    }})
    run = observer.close(transcript)
    invocation = json.loads((observer.base / run["invocations"][0]["path"]).read_text())
    assert invocation["log_complete"] is (truncation == "none")
    assert invocation["log_source"] == {"kind": "native_hook"}
    assert len(invocation["log_collection_warnings"]) == 1
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert (score["status"] == "complete") is (truncation == "none")
    assert probes == [["mvn", "--version"]]  # Collection never dispatches the task.


@pytest.mark.parametrize("hook", ["PostToolUse", "PostToolUseFailure"])
def test_claude_embedded_truncation_is_not_silently_full(hook):
    text = "BUILD FAILURE\n... [3929 characters truncated] ...\n"
    event = {"hook_event_name": hook, "error": "Exit code 1\n" + text,
             "is_interrupt": False, "tool_response": {"stdout": text, "interrupted": False}}
    native = native_result("claude", event)
    assert native["truncated"] is True
    _, complete, _, _ = collect_native_log(native, terminal=True, read_file=lambda _: b"")
    assert complete is False


def test_persisted_file_can_follow_expired_launch_file():
    requested = []

    def read_file(path):
        requested.append(path)
        if path == "/expired":
            raise FileNotFoundError(path)
        return b"full native output"

    data, complete, source, warnings = collect_native_log(
        {"status": "completed", "exit_code": 0, "text": "preview", "truncated": True,
         "output_path": "/persisted"}, terminal=True, read_file=read_file, launch_path="/expired",
    )
    assert requested == ["/expired", "/persisted"]
    assert data == b"full native output" and complete is True
    assert source == {"kind": "native_file", "path": "/persisted"}
    assert len(warnings) == 1


def test_pending_output_cannot_use_full_file_fallback():
    def never_read(_):
        pytest.fail("Pending output must not be collected as terminal evidence")

    _, complete, _, _ = collect_native_log(
        {"status": "pending", "text": "BUILD SUCCESS"}, terminal=False,
        read_file=never_read, launch_path="/output",
    )
    assert complete is False


def test_ordinary_truncated_identifier_is_not_a_transport_marker():
    native = native_result("claude", {"hook_event_name": "PostToolUse",
        "tool_response": {"stdout": "Tests run: TruncatedInputTest\n", "interrupted": False}})
    assert native["truncated"] is False


@pytest.mark.parametrize("error,expected", [
    ("Exit code 1", 1), ("Exit code 1\n", 1),
    ("Exit code 1\nactual failure", 1), ("Exit code 1not-an-exit", None),
])
def test_claude_empty_failure_output_still_has_an_exit(error, expected):
    result = native_result("claude", {"hook_event_name": "PostToolUseFailure",
        "error": error, "is_interrupt": False})
    assert result["exit_code"] == expected
    assert result["status"] == ("completed" if expected is not None else "unavailable")


def test_redirected_failure_collects_file_reports_and_os_exit(fixture, tmp_path):
    observer, root, event, transcript, probes = make_observer(fixture, tmp_path)
    observer.arm = "claude"
    event["tool_name"] = "Bash"
    log = tmp_path / "actual-maven.log"
    log.write_text((fixture[4] / "log.txt").read_text().replace("BUILD SUCCESS", "BUILD FAILURE"))
    original_read = observer.read_file

    def failed_process_read(path):
        if path.endswith("process.123"):
            latest = json.loads((observer.base / observer.events[-1]["path"]).read_text())
            epoch = datetime.fromisoformat(latest["received_at"]).timestamp() - 0.000001
            return f"{epoch:.6f} +++ exited with 1 +++\n".encode()
        return original_read(path)

    observer.read_file = failed_process_read
    observer.event(event)
    observer.event({**event["launcher_event"], "output_path": str(log)})
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-demo.xml").write_text(
        '<testsuite tests="1" failures="1" errors="0" skipped="0"><testcase name="a"><failure message="observed"/></testcase></testsuite>'
    )
    observer.event({**event, "hook_event_name": "PostToolUseFailure",
                    "error": "Exit code 1", "is_interrupt": False})
    transcript.write_text(json.dumps({"message": {"content": [
        {"type": "tool_use", "name": "Bash", "id": "native-1"},
    ]}}))
    run = observer.close(transcript)
    invocation = json.loads((observer.base / run["invocations"][0]["path"]).read_text())
    assert invocation["status"] == "completed" and invocation["exit_code"] == 1
    assert invocation["native_process"]["exit_code"] == 1
    assert invocation["log_complete"] is True
    assert invocation["reports_collection_complete"] is True
    score = evaluate(observer.task, observer.spec, run, observer.base, required_execution_origin="agent")
    assert score["status"] == "incomplete"
    assert probes == [["mvn", "--version"]]
