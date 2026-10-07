"""Read-only native hook evidence checks shared with the standalone scorer."""

import json
import posixpath
import re
from datetime import datetime
from pathlib import PurePosixPath

from .requirements import bound_file


def process_terminal(raw):
    matches = re.findall(rb"(?m)^(\d+\.\d+) \+\+\+ exited with (\d+) \+\+\+$", raw)
    if len(matches) != 1:
        raise ValueError("Native process has no unique normal OS exit")
    return {"exit_epoch": float(matches[0][0]), "exit_code": int(matches[0][1])}


def validate_native_process(base, invocation):
    """Cross-bind launch callback, OS exit and native tool return timestamps."""
    if invocation.get("status") != "completed":
        return
    proof = invocation["native_process"]
    ended = process_terminal(bound_file(base, proof).read_bytes())
    launch_event = json.loads(bound_file(base, invocation["native_launcher_event"]).read_text())
    before = json.loads(bound_file(base, invocation["native_hook"]["before"]).read_text())
    after = json.loads(bound_file(base, invocation["native_hook"]["after"]).read_text())
    launch = launch_event["native"]["launch"]
    started, finished = before["native"], after["native"]
    if (
        started["hook_event_name"] != "PreToolUse"
        or started["tool_name"] not in {"Bash", "bash"}
        or not started.get("tool_use_id")
        or not started.get("session_id")
        or finished.get("session_id") != started["session_id"]
        or finished["hook_event_name"] not in {"PostToolUse", "PostToolUseFailure"}
        or launch_event["native"]["hook_event_name"] != "MavenLaunch"
    ):
        raise ValueError("Native process has no matching shell tool boundary")
    acknowledgement = invocation["native_hook"].get("background_acknowledgement")
    if acknowledgement:
        ack = json.loads(bound_file(base, acknowledgement).read_text())["native"]
        task = finished.get("tool_response", {}).get("task", {})
        task_id = ack.get("tool_response", {}).get("backgroundTaskId")
        if (
            ack.get("tool_use_id") != started["tool_use_id"]
            or ack.get("session_id") != started["session_id"]
            or ack.get("hook_event_name") != "PostToolUse"
            or ack.get("tool_name") != "Bash"
            or not task_id
            or finished.get("tool_name") != "TaskOutput"
            or finished.get("tool_input", {}).get("task_id") != task_id
            or finished.get("tool_response", {}).get("retrieval_status") != "success"
            or task.get("task_id") != task_id
            or task.get("task_type") != "local_bash"
            or task.get("status") not in {"completed", "failed"}
            or type(task.get("exitCode")) is not int
        ):
            raise ValueError("Native background completion belongs to another task")
    elif (
        finished.get("tool_use_id") != started["tool_use_id"]
        or finished.get("tool_name") != started["tool_name"]
    ):
        raise ValueError("Native tool completion belongs to another call")
    epoch = lambda event: datetime.fromisoformat(event["received_at"]).timestamp()
    if (
        launch_event["native"]["namespace"] != proof["namespace"]
        or launch["launcher_pid"] != proof["process_id"]
        or launch["argv"] != invocation["effective_argv"]
        or launch["commit"] != invocation["commit"]
        or launch["root"] != invocation["project_root"]
        or ended["exit_code"] != invocation["exit_code"]
        or ended["exit_code"] != proof["exit_code"]
        or not epoch(before) <= epoch(launch_event) <= ended["exit_epoch"] <= epoch(after)
    ):
        raise ValueError("Native launch, process exit and tool return do not bind one execution")


def launcher_runtime(base, invocation):
    rows = [
        json.loads(line)
        for line in bound_file(base, invocation["runtime"]["native_launch"])
        .read_text()
        .splitlines()
    ]
    if (
        not rows
        or rows[0].get("event") != "ObserverStarted"
        or rows[-1].get("event") != "ObserverClosed"
    ):
        raise ValueError("Native launcher did not close")
    launch = json.loads(rows[0]["launcher_observation"])
    if invocation.get("native_process"):
        proof = invocation["native_process"]
        ended = process_terminal(bound_file(base, proof).read_bytes())
        if (
            ended["exit_code"] != invocation["exit_code"]
            or ended["exit_code"] != proof["exit_code"]
            or str(proof["process_id"]) != rows[0].get("process_id")
            or proof["namespace"] != rows[0].get("run_id")
            or rows[-1].get("closed_epoch_ms", 0) / 1000 > ended["exit_epoch"]
        ):
            raise ValueError("Native process exit and Maven session differ")
    argv = invocation["effective_argv"]
    observed = launch.get("argv", [])
    if (
        len(observed) != len(argv)
        or observed[1:] != argv[1:]
        or not PurePosixPath(observed[0]).is_absolute()
        or (PurePosixPath(argv[0]).is_absolute() and observed[0] != argv[0])
        or launch.get("root") != invocation["project_root"]
        or launch.get("commit") != invocation["commit"]
        or launch.get("cwd")
        != posixpath.normpath(posixpath.join(invocation["project_root"], invocation["cwd"]))
    ):
        raise ValueError("Native launcher command binding differs")
    sessions = [r for r in rows if r.get("event") == "SessionStarted"]
    if len(sessions) != 1:
        raise ValueError("Native launcher session is not unique")
    version = rows[0].get("java_version", "")
    match = re.match(r"(\d+)(?:\.(\d+))?", version)
    major = (int(match[2]) if match[1] == "1" and match[2] else int(match[1])) if match else None
    return {
        "java_major": major,
        "java_version": version,
        "java_home": rows[0].get("java_home"),
        "java_vendor": rows[0].get("java_vendor"),
        "maven_version": sessions[0].get("maven_version"),
        "executable": observed[0],
    }
