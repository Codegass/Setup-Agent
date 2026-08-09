"""Phase 4.3 — detached dispatch + log-tail polling (soft-timeout handoff).

Long build/test commands run detached with output in a container log file.
If they finish inside the soft window the tool gets a normal result; if not,
the agent gets the log tail + poll instructions and the process keeps running.
"""

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")

from build_requirements_fakes import complete_build_requirements_v1
from sag.agent.control_events import canonical_json
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    evidence_publication_authority_for,
)
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.output_storage import OutputStorageManager
from sag.docker_orch.orch import DockerOrchestrator
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import bind_tool_result_output_storage
from sag.tools.internal.build_utils import (
    BuildAnalyzer,
    classify_detached_completion,
    detached_handoff_tool_result,
    detached_runner_from_command,
)
from sag.tools.internal.command_tracker import CommandTracker
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.search_tool import SearchTool

CONTAINER_ID = "c" * 64
DOCKER_EXEC_ID = "d" * 64


class FakeDetachedAPI:
    def __init__(self):
        self.create_calls = []
        self.start_calls = []
        self.inspect_calls = []
        self.inspect_result = {
            "ID": DOCKER_EXEC_ID,
            "ContainerID": CONTAINER_ID,
            "Running": True,
            "ExitCode": 0,
        }

    def exec_create(self, container, cmd, **kwargs):
        self.create_calls.append({"container": container, "cmd": cmd, "kwargs": kwargs})
        return {"Id": DOCKER_EXEC_ID}

    def exec_start(self, exec_id, **kwargs):
        self.start_calls.append({"exec_id": exec_id, "kwargs": kwargs})

    def exec_inspect(self, exec_id):
        self.inspect_calls.append(exec_id)
        return dict(self.inspect_result)


class FakeDetachedContainer:
    id = CONTAINER_ID


class FakeDetachedContainers:
    def get(self, _name):
        return FakeDetachedContainer()


class FakeDetachedClient:
    def __init__(self, api):
        self.api = api
        self.containers = FakeDetachedContainers()


def _framed_poll_output(tail="", *, size=0, now=1, progress=None, extra_head=()):
    lines = [*extra_head, f"SIZE:{size}", f"NOW:{now}"]
    if progress is not None:
        lines.append(f"PROGRESS:{progress}")
    encoded = base64.b64encode(tail.encode("utf-8")).decode("ascii")
    return "\n".join([*lines, "---TAIL---", encoded])


def build_orchestrator(execute_command=None):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.container_name = "sag-demo"
    orchestrator.command_log = []
    orchestrator.detached_api = FakeDetachedAPI()
    orchestrator.client = FakeDetachedClient(orchestrator.detached_api)
    if execute_command is not None:
        orchestrator.execute_command = execute_command
        orchestrator.execute_control_command = execute_command
    return orchestrator


# --- execute_command_detached ----------------------------------------------


def test_detached_dispatch_uses_daemon_owned_exec_and_returns_bound_handle():
    orchestrator = build_orchestrator()

    def fake_execute(command, **kwargs):
        orchestrator.command_log.append(command)
        return {
            "exit_code": 0,
            "output": f"PID:12345\nPGID:12345\nIDENTITY:{'a' * 64}",
        }

    orchestrator.execute_command = fake_execute
    orchestrator.execute_control_command = fake_execute
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})
    orchestrator._runtime_profile_prefix = lambda: "true"

    handle = orchestrator.execute_command_detached(
        "./gradlew compileJava --no-daemon", workdir="/workspace/beam"
    )

    assert handle["started"] is True
    assert handle["pid"] == 12345
    assert handle["pgid"] == 12345
    assert handle["process_identity_token"] == "a" * 64
    assert handle["docker_exec_id"] == DOCKER_EXEC_ID
    assert handle["container_id"] == CONTAINER_ID
    assert handle["terminal_authority"] == "docker_exec_inspect_v1"
    assert handle["start_accepted"] is True
    assert handle["startup_identity_verified"] is True
    assert handle["log_path"].startswith("/tmp/sag_jobs/")
    assert handle["exit_code_path"] == handle["log_path"] + ".exit"
    assert handle["pid_path"].endswith(".pid")
    assert handle["pgid_path"].endswith(".pgid")
    assert handle["identity_path"].endswith(".identity")
    assert len(orchestrator.detached_api.create_calls) == 1
    assert orchestrator.detached_api.start_calls == [
        {"exec_id": DOCKER_EXEC_ID, "kwargs": {"detach": True}}
    ]
    created = orchestrator.detached_api.create_calls[0]
    assert created["container"] == CONTAINER_ID
    launcher = created["cmd"][-1]
    assert "nohup bash -c" not in launcher
    assert "setsid --fork --wait" in launcher
    assert "job_pid=$$" in launcher
    assert handle["pid_path"] in launcher
    assert handle["pgid_path"] in launcher
    assert handle["identity_path"] in launcher
    assert "/proc/sys/kernel/random/boot_id" in launcher
    assert "/proc/$job_pid/stat" in launcher
    assert "job_start_ticks" in launcher
    assert "/workspace/beam" in launcher
    assert "./gradlew compileJava --no-daemon" in launcher
    assert "pkill" not in launcher and "killall" not in launcher
    assert handle["exit_code_path"] not in launcher
    syntax = subprocess.run(["bash", "-n", "-c", launcher], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_detached_dispatch_failure_reported():
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 1, "output": "boom"}
    )
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is False
    assert handle["runner_dispatched"] is True
    assert handle["dispatch_status"] == "execution_observation_failed"
    assert handle["docker_exec_id"] == DOCKER_EXEC_ID
    assert handle["container_id"] == CONTAINER_ID


def test_detached_dispatch_rejects_duplicate_startup_identity_markers():
    orchestrator = build_orchestrator()
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})
    orchestrator.execute_control_command = lambda command, **kwargs: {
        "success": True,
        "exit_code": 0,
        "output": f"PID:123\nPID:123\nPGID:123\nIDENTITY:{'a' * 64}\n",
    }

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is False
    assert handle["runner_dispatched"] is True
    assert handle["start_accepted"] is True
    assert handle["startup_identity_verified"] is False
    assert handle["pid"] is None
    assert handle["pgid"] is None
    assert handle["process_identity_token"] == ""


def test_detached_start_response_loss_uses_daemon_inspect_receipt():
    orchestrator = build_orchestrator()
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})
    orchestrator.execute_control_command = lambda command, **kwargs: {
        "success": True,
        "exit_code": 0,
        "output": f"PID:123\nPGID:123\nIDENTITY:{'a' * 64}\n",
    }

    def response_lost(_exec_id, **_kwargs):
        raise RuntimeError("connection reset after daemon accepted start")

    orchestrator.detached_api.exec_start = response_lost

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is True
    assert handle["runner_dispatched"] is True
    assert handle["runner_dispatch_state"] == "accepted"
    assert orchestrator.detached_api.inspect_calls == [DOCKER_EXEC_ID]


def test_detached_start_and_inspect_ambiguity_is_not_zero_dispatch():
    orchestrator = build_orchestrator()
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("daemon response unavailable")

    orchestrator.detached_api.exec_start = unavailable
    orchestrator.detached_api.exec_inspect = unavailable

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is False
    assert handle["runner_dispatched"] is None
    assert handle["runner_dispatch_state"] == "unknown"
    assert handle["start_accepted"] is False
    assert handle["startup_identity_verified"] is False
    assert handle["dispatch_status"] == "dispatch_unknown"
    assert handle["docker_exec_id"] == DOCKER_EXEC_ID


def test_created_but_never_started_exec_cannot_be_laundered_as_exit_zero():
    orchestrator = build_orchestrator()
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})

    def start_failed_before_request(*_args, **_kwargs):
        raise RuntimeError("request was never sent")

    orchestrator.detached_api.exec_start = start_failed_before_request
    orchestrator.detached_api.inspect_result = {
        "ID": DOCKER_EXEC_ID,
        "ContainerID": CONTAINER_ID,
        "Running": False,
        "ExitCode": 0,
    }
    orchestrator.execute_control_command = lambda command, **kwargs: {
        "success": False,
        "exit_code": 70,
        "output": "",
    }

    handle = orchestrator.execute_command_detached("mvn package")

    assert handle["started"] is False
    assert handle["runner_dispatched"] is None
    assert handle["runner_dispatch_state"] == "unknown"
    assert handle["start_accepted"] is False
    assert handle["startup_identity_verified"] is False
    assert handle["dispatch_status"] == "dispatch_unknown"


def test_soft_timeout_never_collects_a_created_but_unstarted_exit_zero_exec():
    orchestrator = build_orchestrator()
    orchestrator._runtime_profile_prefix = lambda: "true"
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})

    def start_failed_before_request(*_args, **_kwargs):
        raise RuntimeError("request was never sent")

    orchestrator.detached_api.exec_start = start_failed_before_request
    orchestrator.detached_api.inspect_result = {
        "ID": DOCKER_EXEC_ID,
        "ContainerID": CONTAINER_ID,
        "Running": False,
        "ExitCode": 0,
    }
    orchestrator.execute_control_command = lambda command, **kwargs: {
        "success": True,
        "exit_code": 0,
        "output": f"PID:123\nPGID:123\nIDENTITY:{'a' * 64}\n",
    }

    result = orchestrator.execute_command_with_soft_timeout("mvn package")

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["dispatch_status"] == "dispatch_unknown"
    assert result["runner_dispatched"] is None
    assert result["lifecycle_state"] == "pending"


def test_finished_exec_without_start_acceptance_is_not_terminal_authority():
    orchestrator = build_orchestrator()
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=0)
    handle = {
        **_handle(),
        "started": False,
        "start_accepted": False,
        "runner_dispatch_state": "unknown",
    }

    observation = orchestrator.inspect_detached_terminal(handle)

    assert observation["probe_success"] is False
    assert observation["state"] == "unknown"
    assert observation["exit_code"] is None
    assert observation["start_accepted"] is False
    assert observation["probe_error"] == "detached_start_acceptance_unproven"


def test_running_exec_can_promote_ambiguous_start_before_later_terminal():
    orchestrator = build_orchestrator()
    handle = {
        **_handle(),
        "started": False,
        "start_accepted": False,
        "runner_dispatch_state": "unknown",
    }

    running = orchestrator.inspect_detached_terminal(handle)

    assert running["probe_success"] is True
    assert running["state"] == "running"
    assert running["start_accepted"] is True

    handle["start_accepted"] = True
    handle["runner_dispatch_state"] = "accepted"
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=7)
    terminal = orchestrator.inspect_detached_terminal(handle)

    assert terminal["probe_success"] is True
    assert terminal["state"] == "finished"
    assert terminal["exit_code"] == 7
    assert terminal["start_accepted"] is True


# --- poll_detached_command ---------------------------------------------------


def _handle():
    return {
        "started": True,
        "job_id": "abc",
        "pid": 12345,
        "pid_path": "/tmp/sag_jobs/abc.pid",
        "log_path": "/tmp/sag_jobs/abc.log",
        "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
        "process_identity_token": "a" * 64,
        "docker_exec_id": DOCKER_EXEC_ID,
        "container_id": CONTAINER_ID,
        "terminal_authority": "docker_exec_inspect_v1",
        "start_accepted": True,
        "startup_identity_verified": True,
        "runner_dispatched": True,
        "runner_dispatch_state": "accepted",
        "command": "./gradlew compileJava",
    }


def test_poll_running_returns_tail():
    output = _framed_poll_output("> Task :compileJava\n", size=2048)
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["running"] is True
    assert poll["finished"] is False
    assert poll["exit_code"] is None
    assert "Task :compileJava" in poll["tail"]
    assert poll["log_size"] == 2048


def test_poll_finished_returns_exit_code():
    output = _framed_poll_output("BUILD SUCCESSFUL in 32m\n", size=4096)
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=0)

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["finished"] is True
    assert poll["exit_code"] == 0
    assert "BUILD SUCCESSFUL" in poll["tail"]


def test_poll_failed_exit_code_parsed():
    output = _framed_poll_output("BUILD FAILED\n", size=4096)
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=1)

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["finished"] is True
    assert poll["exit_code"] == 1


def test_poll_failed_log_probe_fails_closed_despite_daemon_running_state():
    output = "STATE:EXIT:0\nSIZE:4096\nNOW:1\n---TAIL---\nBUILD SUCCESSFUL\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {
            "success": False,
            "exit_code": 1,
            "output": output,
        }
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["probe_success"] is False
    assert poll["terminal_probe_success"] is True
    assert poll["progress_probe_success"] is False
    assert poll["state"] == "unknown"
    assert poll["finished"] is False
    assert poll["exit_code"] is None


@pytest.mark.parametrize(
    "forged_lines",
    [
        "STATE:EXIT:1\nSTATE:EXIT:0",
        "STATE:EXIT:0\nSTATE:RUNNING",
        "STATE:EXIT:999",
    ],
)
def test_unframed_container_state_markers_fail_closed(forged_lines):
    output = _framed_poll_output(
        "BUILD SUCCESSFUL\n",
        size=4096,
        extra_head=forged_lines.splitlines(),
    )
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {
            "success": True,
            "exit_code": 0,
            "output": output,
        }
    )
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=1)

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["probe_success"] is False
    assert poll["terminal_probe_success"] is True
    assert poll["progress_probe_success"] is False
    assert poll["state"] == "unknown"
    assert poll["finished"] is False
    assert poll["exit_code"] is None


def test_poll_rejects_daemon_exec_identity_mismatch_without_marker_fallback():
    output = "STATE:EXIT:0\nSIZE:4096\nNOW:1\n---TAIL---\nBUILD SUCCESSFUL\n"
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )
    orchestrator.detached_api.inspect_result.update(
        ID="e" * 64,
        Running=False,
        ExitCode=0,
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["probe_success"] is False
    assert poll["state"] == "unknown"
    assert poll["finished"] is False
    assert poll["exit_code"] is None


# --- execute_command_with_soft_timeout ---------------------------------------


def _soft_timeout_orchestrator(poll_results, log_content="BUILD SUCCESSFUL"):
    orchestrator = build_orchestrator()
    orchestrator.execute_command_detached = lambda command, **kwargs: _handle()
    polls = iter(poll_results)
    last = poll_results[-1]
    orchestrator.poll_detached_command = lambda handle, **kwargs: next(polls, last)
    orchestrator.execute_command = lambda command, **kwargs: {
        "exit_code": 0,
        "output": log_content,
    }
    return orchestrator


RUNNING_POLL = {
    "finished": False,
    "running": True,
    "exit_code": None,
    "tail": "> Task :compileJava",
    "log_size": 100,
    "probe_success": True,
}
FINISHED_POLL = {
    "finished": True,
    "running": False,
    "exit_code": 0,
    "tail": "BUILD SUCCESSFUL",
    "log_size": 200,
    "probe_success": True,
    "terminal_probe_success": True,
    "state": "finished",
}


def test_soft_timeout_returns_full_result_when_finished_in_window():
    orchestrator = _soft_timeout_orchestrator([RUNNING_POLL, FINISHED_POLL])

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=30, poll_interval=0.01
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["dispatch_status"] == "completed_detached"
    assert "BUILD SUCCESSFUL" in result["output"]
    assert result["termination_reason"] is None


def test_collect_detached_result_preserves_full_log_untruncated_for_storage():
    """A finished detached build log must be read WITHOUT the orchestrator's
    emergency truncation and handed back complete under `full_output`, so the
    build tools can persist the real error to the output store. The inline
    `output` stays bounded so it never floods the model context (Brooklyn: the
    truncated cat hid the compile error and the agent looped blind)."""
    orchestrator = build_orchestrator()
    middle_marker = "[ERROR] COMPILATION ERROR in BrooklynModule.java"
    big_log = (
        "\n".join(f"[INFO] downloading dep {i}" for i in range(1500))
        + f"\n{middle_marker}\n"
        + "\n".join(f"[INFO] trailing line {i}" for i in range(1500))
        + "\nBUILD FAILURE\n"
    )
    assert len(big_log) > 10000  # large enough to trigger inline bounding

    seen = {}

    def fake_execute(command, **kwargs):
        seen["truncate_output"] = kwargs.get("truncate_output", True)
        seen["command"] = command
        return {"exit_code": 0, "output": big_log}

    orchestrator.execute_control_command = fake_execute

    result = orchestrator._collect_detached_result(
        _handle(),
        {
            "finished": True,
            "running": False,
            "state": "finished",
            "probe_success": True,
            "terminal_probe_success": True,
            "exit_code": 1,
            "tail": "BUILD FAILURE",
        },
    )

    # The log was read with truncation explicitly disabled...
    assert seen["truncate_output"] is False
    assert seen["command"].startswith("cat ")
    # ...so the complete log (including the mid-stream error) is preserved.
    assert result["full_output"] == big_log
    assert middle_marker in result["full_output"]
    # The inline output is bounded for context safety.
    assert len(result["output"]) < len(result["full_output"])
    assert len(result["output"]) <= 10000
    assert result["exit_code"] == 1
    assert result["dispatch_status"] == "completed_detached"
    assert result["execution_observation_complete"] is True


def test_collect_detached_result_refuses_success_when_terminal_log_is_unreadable():
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {
            "success": False,
            "exit_code": -1,
            "dispatch_status": "control_transport_unavailable",
            "output": "",
        }
    )

    result = orchestrator.collect_detached_result(
        _handle(),
        {
            "finished": True,
            "state": "finished",
            "probe_success": True,
            "terminal_probe_success": True,
            "exit_code": 0,
            "tail": "BUILD SUCCESSFUL",
        },
    )

    assert result["success"] is False
    assert result["dispatch_status"] == "execution_observation_failed"
    assert result["runner_dispatched"] is True
    assert result["execution_observation_complete"] is False


def test_collect_detached_result_cannot_bypass_unproven_start_acceptance():
    calls = []
    orchestrator = build_orchestrator()
    orchestrator.execute_control_command = lambda command, **kwargs: calls.append(command)
    handle = {
        **_handle(),
        "started": False,
        "start_accepted": False,
        "startup_identity_verified": False,
        "runner_dispatched": None,
        "runner_dispatch_state": "unknown",
    }

    result = orchestrator.collect_detached_result(
        handle,
        {
            "finished": True,
            "running": False,
            "state": "finished",
            "probe_success": True,
            "terminal_probe_success": True,
            "exit_code": 0,
            "tail": "BUILD SUCCESSFUL",
        },
    )

    assert result["success"] is False
    assert result["dispatch_status"] == "dispatch_unknown"
    assert result["runner_dispatched"] is None
    assert result["execution_observation_complete"] is False
    assert calls == []


def test_soft_timeout_hands_off_still_running_command():
    orchestrator = _soft_timeout_orchestrator([RUNNING_POLL])

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=1, poll_interval=0.01
    )

    assert result["dispatch_status"] == "running_detached"
    assert result["success"] is True, "a handoff is not a failure"
    assert result["exit_code"] is None
    assert result["termination_reason"] is None
    assert "/tmp/sag_jobs/abc.log" in result["output"]
    assert "Controller-owned job barrier" in result["output"]
    assert "No model action is requested" in result["output"]
    assert "do not poll or dispatch this job again" in result["output"]
    assert "tail -n" not in result["output"]
    assert "NEXT STEPS" not in result["output"]


def test_soft_timeout_vanished_process_fails_safe():
    vanished = {
        "finished": False,
        "running": False,
        "exit_code": None,
        "tail": "killed",
        "log_size": 10,
        "probe_success": True,
    }
    orchestrator = _soft_timeout_orchestrator([vanished], log_content="killed")

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=30, poll_interval=0.01
    )

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["dispatch_status"] == "execution_observation_failed"
    assert result["lifecycle_state"] == "pending"


def test_soft_timeout_unknown_liveness_returns_distinct_pending_handoff():
    unknown = {
        "finished": False,
        "running": False,
        "exit_code": None,
        "tail": "last known output",
        "log_size": 17,
        "probe_success": False,
        "state": "unknown",
    }
    orchestrator = _soft_timeout_orchestrator([unknown], log_content="last known output")
    collect_calls = []
    orchestrator.collect_detached_result = lambda *args: collect_calls.append(args) or {}

    result = orchestrator.execute_command_with_soft_timeout(
        "./gradlew compileJava", soft_timeout=1, poll_interval=0.01
    )

    assert result["success"] is True
    assert result["exit_code"] is None
    assert result["dispatch_status"] == "liveness_unknown_detached"
    assert result["lifecycle_state"] == "pending"
    assert result["liveness_state"] == "unknown"
    assert result["dispatch"]["job_id"] == "abc"
    assert result["dispatch"]["last_tail"] == "last known output"
    assert "last known output" in result["output"]
    assert collect_calls == []


def test_soft_timeout_dispatch_failure_is_failure_result():
    orchestrator = build_orchestrator()
    orchestrator.execute_command_detached = lambda command, **kwargs: {
        "started": False,
        "launch_output": "no shell",
        "pid": None,
        "log_path": "x",
        "exit_code_path": "x.exit",
        "job_id": "x",
        "command": command,
    }

    result = orchestrator.execute_command_with_soft_timeout("mvn package", soft_timeout=1)

    assert result["success"] is False
    assert result["dispatch_status"] == "dispatch_failed"


def test_post_start_identity_failure_returns_traceable_pending_handle():
    orchestrator = build_orchestrator()
    partial = {
        **_handle(),
        "started": False,
        "pid": None,
        "pgid": None,
        "process_identity_token": "",
        "runner_dispatched": True,
        "dispatch_status": "execution_observation_failed",
        "launch_output": "identity probe unavailable",
    }
    orchestrator.execute_command_detached = lambda command, **kwargs: dict(partial)
    orchestrator.poll_detached_command = lambda handle, **kwargs: {
        "finished": False,
        "running": False,
        "exit_code": None,
        "tail": "",
        "log_size": 0,
        "probe_success": False,
        "state": "unknown",
    }

    result = orchestrator.execute_command_with_soft_timeout("mvn package", soft_timeout=1)

    assert result["success"] is True
    assert result["exit_code"] is None
    assert result["runner_dispatched"] is True
    assert result["dispatch_status"] == "liveness_unknown_detached"
    assert result["lifecycle_state"] == "pending"
    assert result["dispatch"]["docker_exec_id"] == DOCKER_EXEC_ID
    assert result["dispatch"]["container_id"] == CONTAINER_ID


def test_soft_timeout_preserves_typed_pre_dispatch_authority_failure():
    orchestrator = build_orchestrator()
    orchestrator.execute_command_detached = lambda command, **kwargs: {
        "started": False,
        "success": False,
        "exit_code": -1,
        "launch_output": "published runtime overlay is unavailable",
        "dispatch_status": "environment_overlay_unavailable",
        "runner_dispatched": False,
        "command": command,
    }

    result = orchestrator.execute_command_with_soft_timeout("mvn package", soft_timeout=1)

    assert result["success"] is False
    assert result["exit_code"] == -1
    assert result["dispatch_status"] == "environment_overlay_unavailable"
    assert result["runner_dispatched"] is False


# --- tool-level handoff -------------------------------------------------------


def test_detached_handoff_is_a_promise_not_success():
    result = {
        "output": "still running; tail -n 50 /tmp/sag_jobs/abc.log",
        "dispatch_status": "running_detached",
        "dispatch": {
            "job_id": "abc",
            "pid": 12345,
            "log_path": "/tmp/sag_jobs/abc.log",
            "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            "soft_timeout": 900,
        },
    }

    tool_result = detached_handoff_tool_result("gradle", "./gradlew build", result)

    assert tool_result.invocation_status is InvocationStatus.PENDING
    assert tool_result.operation_outcome is OperationOutcome.UNKNOWN
    assert tool_result.evidence_status is EvidenceStatus.UNKNOWN
    assert tool_result.poll_ref == "job:abc"
    assert "tail -n 50" in tool_result.output
    assert tool_result.metadata["dispatch_status"] == "running_detached"
    assert tool_result.metadata["pid"] == 12345
    assert tool_result.metadata["log_path"] == "/tmp/sag_jobs/abc.log"


def test_fatal_tail_overrides_zero_exit_plumbing(tmp_path):
    outer_storage = OutputStorageManager(tmp_path / "outer")
    origin_storage = OutputStorageManager(tmp_path / "origin")
    output_ref = origin_storage.store_output(
        task_id="detached",
        tool_name="build",
        output="CMake Error: configuration failed",
    )
    with bind_tool_result_output_storage(outer_storage):
        result = classify_detached_completion(
            exit_code=0,
            tail="CMake Error: configuration failed",
            full_output_ref=output_ref,
            output_ref_storage=origin_storage,
        )

    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.error_code == "DETACHED_OPERATION_FAILED"
    assert result.failure_signature
    assert result.error_tail_preview == "CMake Error: configuration failed"
    assert result.output_ref == output_ref
    assert origin_storage.retrieve_output(output_ref) == "CMake Error: configuration failed"


def test_saved_gradle_success_is_not_classified_by_make_vocabulary():
    tail = (
        Path(__file__).parent
        / "fixtures/battery_20260808/sha256-49884ada42c5ae31f09d496facc947a098e5aca2ee2106094df2339eb6e0688e.txt"
    ).read_text(encoding="utf-8")
    assert "checkKotlinGradlePluginConfigurationErrors" in tail

    result = classify_detached_completion(0, tail, runner="gradle")

    assert result.operation_outcome is OperationOutcome.SUCCESS
    assert result.metadata["runner"] == "gradle"


def test_make_requires_a_native_failure_shape_not_an_arbitrary_error_word():
    unrelated = BuildAnalyzer.detect_build_status(
        "> Task :checkKotlinGradlePluginConfigurationErrors\nBUILD SUCCESSFUL",
        "make",
    )
    native = BuildAnalyzer.detect_build_status(
        "make[2]: *** [CMakeFiles/core.dir/build.make:76: core.o] Error 1",
        "make",
    )

    assert unrelated["success"] is None
    assert native["success"] is False
    assert (
        classify_detached_completion(
            0,
            "make[2]: *** [CMakeFiles/core.dir/build.make:76: core.o] Error 1",
            runner="make",
        ).operation_outcome
        is OperationOutcome.FAILED
    )


def test_nonzero_exit_outranks_a_native_success_marker():
    result = classify_detached_completion(
        137,
        "BUILD SUCCESSFUL in 30s",
        runner="gradle",
    )

    assert result.operation_outcome is OperationOutcome.FAILED


@pytest.mark.parametrize(
    "command,expected",
    [
        ("./mvnw verify", "maven"),
        ("/opt/gradle/bin/gradle test", "gradle"),
        ("python3 -m pytest -q", "python"),
        ("gmake all", "make"),
        ("bash -lc './gradlew test'", None),
    ],
)
def test_direct_detached_runner_identity_is_mechanical(command, expected):
    assert detached_runner_from_command(command) == expected


def test_missing_exit_code_preserves_pending_unknown_with_poll_ref(tmp_path):
    storage = OutputStorageManager(tmp_path)
    output_ref = storage.store_output(
        task_id="detached",
        tool_name="build",
        output="detached process state could not be established",
    )
    with bind_tool_result_output_storage(storage):
        result = classify_detached_completion(
            exit_code=None,
            tail="detached process state could not be established",
            full_output_ref=output_ref,
            poll_ref="job:detached",
        )

    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.poll_ref == "job:detached"
    assert result.succeeded is False


@pytest.mark.parametrize(
    "invocation_status",
    [InvocationStatus.COMPLETED, InvocationStatus.CRASHED],
)
def test_terminal_detached_observation_without_exit_status_does_not_repoll(
    invocation_status,
):
    result = classify_detached_completion(
        exit_code=None,
        tail="the process ended but no exit file was recorded",
        poll_ref="job:detached",
        invocation_status=invocation_status,
        terminal_observation=True,
    )

    assert result.invocation_status is invocation_status
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.error_code == "DETACHED_EXIT_STATUS_MISSING"
    assert result.poll_ref == "job:detached"
    assert result.is_terminal


def test_detached_completion_rejects_shape_valid_unpersisted_output_ref():
    with pytest.raises(ValueError, match="persisted.*OutputStorage"):
        classify_detached_completion(
            exit_code=None,
            tail="detached process state could not be established",
            full_output_ref="output_missing",
            poll_ref="job:missing",
        )


def test_detached_failure_checks_origin_index_once_without_loading_output():
    class IndexedOutputStorage:
        def __init__(self):
            self.lookups = 0

        def has_output_ref(self, ref_id):
            self.lookups += 1
            return ref_id == "output_detached_failure"

        def retrieve_output(self, ref_id):
            raise AssertionError("ref validation must not load full output")

    storage = IndexedOutputStorage()
    result = classify_detached_completion(
        exit_code=0,
        tail="CMake Error: configuration failed",
        full_output_ref="output_detached_failure",
        output_ref_storage=storage,
    )

    assert result.operation_outcome is OperationOutcome.FAILED
    assert storage.lookups == 1


class PollingJobOrchestrator:
    def __init__(self, poll, collected=None):
        self.poll = poll
        self.collected = collected or {}
        self.handles = []

    def detached_handle(self, job_id):
        return {
            "job_id": job_id,
            "log_path": f"/tmp/sag_jobs/{job_id}.log",
            "exit_code_path": f"/tmp/sag_jobs/{job_id}.log.exit",
            "pid_path": f"/tmp/sag_jobs/{job_id}.pid",
        }

    def poll_detached_command(self, handle, **kwargs):
        self.handles.append(handle)
        return dict(self.poll)

    def collect_detached_result(self, handle, poll):
        return dict(self.collected)

    def execute_command(self, command, **kwargs):
        # The old implementation greps the log and reports that grep invocation
        # as the original operation's successful completion.
        return {"exit_code": 0, "output": self.poll.get("tail", "")}


def test_search_job_poll_preserves_pending_for_active_original_operation():
    orchestrator = PollingJobOrchestrator(
        {
            "finished": False,
            "running": True,
            "exit_code": None,
            "tail": "compiling module 3/10",
            "log_size": 200,
            "probe_success": True,
            "state": "running",
        }
    )

    result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.poll_ref == "job:abc"
    assert "compiling module 3/10" in result.output


def test_search_job_poll_keeps_same_ref_when_probe_cannot_establish_liveness():
    orchestrator = PollingJobOrchestrator(
        {
            "finished": False,
            "running": False,
            "exit_code": None,
            "tail": "last known output",
            "log_size": 17,
            "probe_success": False,
            "state": "unknown",
        }
    )

    first = SearchTool(orchestrator).execute(target="job:abc", pattern=".")
    second = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    for result in (first, second):
        assert result.invocation_status is InvocationStatus.PENDING
        assert result.operation_outcome is OperationOutcome.UNKNOWN
        assert result.evidence_status is EvidenceStatus.UNKNOWN
        assert result.poll_ref == "job:abc"
    assert len(orchestrator.handles) == 2


def test_search_job_poll_classifies_terminal_fatal_tail_for_original_operation(tmp_path):
    orchestrator = PollingJobOrchestrator(
        {
            "finished": True,
            "running": False,
            "exit_code": 0,
            "tail": "CMake Error: configuration failed",
            "log_size": 42,
            "probe_success": True,
            "state": "finished",
        },
        {
            "exit_code": 0,
            "output": "CMake Error: configuration failed",
            "full_output": "CMake Error: configuration failed",
            "dispatch_status": "completed_detached",
        },
    )
    storage = OutputStorageManager(tmp_path)

    with bind_tool_result_output_storage(storage, task_id="detached", tool_name="search"):
        result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is InvocationStatus.COMPLETED
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.poll_ref == "job:abc"
    assert result.output_ref.startswith("output_")
    assert storage.retrieve_output(result.output_ref) == "CMake Error: configuration failed"
    assert result.error_code == "DETACHED_OPERATION_FAILED"


def test_search_job_poll_reports_observed_vanish_as_crashed(tmp_path):
    orchestrator = PollingJobOrchestrator(
        {
            "finished": False,
            "running": False,
            "exit_code": None,
            "tail": "process disappeared",
            "log_size": 19,
            "probe_success": True,
            "state": "vanished",
        },
        {
            "exit_code": 1,
            "output": "process disappeared",
            "full_output": "process disappeared",
            "dispatch_status": "completed_detached",
        },
    )
    storage = OutputStorageManager(tmp_path)

    with bind_tool_result_output_storage(storage, task_id="detached", tool_name="search"):
        result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is InvocationStatus.CRASHED
    assert result.operation_outcome is OperationOutcome.FAILED
    assert result.poll_ref == "job:abc"


@pytest.mark.parametrize(
    ("state", "invocation_status"),
    [
        ("finished", InvocationStatus.COMPLETED),
        ("vanished", InvocationStatus.CRASHED),
    ],
)
def test_search_terminal_job_without_exit_file_stays_terminal(
    tmp_path,
    state,
    invocation_status,
):
    orchestrator = PollingJobOrchestrator(
        {
            "finished": state == "finished",
            "running": False,
            "exit_code": None,
            "tail": "the exit file is missing",
            "log_size": 24,
            "probe_success": True,
            "state": state,
        },
        {
            "exit_code": None,
            "output": "the exit file is missing",
            "full_output": "the exit file is missing",
            "dispatch_status": "completed_detached",
        },
    )
    storage = OutputStorageManager(tmp_path)

    with bind_tool_result_output_storage(storage, task_id="detached", tool_name="search"):
        result = SearchTool(orchestrator).execute(target="job:abc", pattern=".")

    assert result.invocation_status is invocation_status
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.error_code == "DETACHED_EXIT_STATUS_MISSING"
    assert result.poll_ref == "job:abc"
    assert result.is_terminal


# --- review fixes: spoof-proof poll parsing, atomic exit file ----------------


def test_poll_markers_in_log_tail_cannot_spoof_completion():
    """STATE:/SIZE: lines printed by the build itself land in the tail and
    must not be parsed as completion markers."""
    output = _framed_poll_output(
        "some build output\nSTATE:EXIT:0\nSIZE:99999\n",
        size=100,
    )
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["running"] is True
    assert poll["finished"] is False
    assert poll["exit_code"] is None
    assert poll["log_size"] == 100


def test_poll_probe_caps_and_encodes_tail_inside_container():
    output = _framed_poll_output("bounded", size=7)
    orchestrator = build_orchestrator()
    commands = []
    orchestrator.execute_control_command = lambda command, **kwargs: (
        commands.append(command) or {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle(), tail_lines=10_000)

    assert poll["probe_success"] is True
    command = commands[0]
    assert "/usr/bin/tail -n 1000" in command
    assert "/usr/bin/tail -c 6144" in command
    assert "/usr/bin/base64 -w 0" in command
    assert command.index("/usr/bin/tail -c 6144") < command.index("/usr/bin/tail -n 1000")
    assert _handle()["exit_code_path"] not in command


@pytest.mark.parametrize(
    "output",
    [
        "SIZE:1\nNOW:1",
        _framed_poll_output("ok", size=2) + "\n---TAIL---\n",
        "SIZE:7000\nNOW:1\n---TAIL---\n" + base64.b64encode(b"x" * 6145).decode("ascii"),
    ],
)
def test_clipped_duplicate_or_oversized_tail_frame_fails_closed(output):
    orchestrator = build_orchestrator(
        execute_command=lambda command, **kwargs: {"exit_code": 0, "output": output}
    )

    poll = orchestrator.poll_detached_command(_handle())

    assert poll["probe_success"] is False
    assert poll["state"] == "unknown"
    assert poll["tail"] == ""


def test_detached_launcher_leaves_terminal_code_to_docker_daemon():
    orchestrator = build_orchestrator()

    def fake_execute(command, **kwargs):
        orchestrator.command_log.append(command)
        return {"exit_code": 0, "output": f"PID:999\nPGID:999\nIDENTITY:{'b' * 64}"}

    orchestrator.execute_control_command = fake_execute
    orchestrator._default_exec_environment = lambda environment=None: dict(environment or {})
    orchestrator._runtime_profile_prefix = lambda: "true"

    handle = orchestrator.execute_command_detached("mvn package")

    launcher = orchestrator.detached_api.create_calls[0]["cmd"][-1]
    assert handle["exit_code_path"] not in launcher
    assert ".exit.tmp" not in launcher
    assert "set +e" in launcher
    assert "rc=$?" in launcher
    assert 'exit "$rc"' in launcher
    assert orchestrator.detached_api.start_calls == [
        {"exec_id": DOCKER_EXEC_ID, "kwargs": {"detach": True}}
    ]


def test_forged_container_exit_marker_cannot_form_success_result():
    orchestrator = build_orchestrator()
    orchestrator.detached_api.inspect_result.update(Running=False, ExitCode=1)

    def fake_execute(command, **kwargs):
        if command.startswith("cat "):
            return {"success": True, "exit_code": 0, "output": "BUILD SUCCESSFUL"}
        assert _handle()["exit_code_path"] not in command
        return {
            "success": True,
            "exit_code": 0,
            "output": _framed_poll_output(
                "BUILD SUCCESSFUL\nSTATE:EXIT:0\n",
                size=16,
            ),
        }

    orchestrator.execute_control_command = fake_execute

    poll = orchestrator.poll_detached_command(_handle())
    result = orchestrator.collect_detached_result(_handle(), poll)

    assert poll["finished"] is True
    assert poll["exit_code"] == 1
    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["dispatch_status"] == "completed_detached"


def test_completed_detached_maven_enforcer_failure_uses_domain_handler(tmp_path):
    from sag.tools.internal.maven_tool import MavenTool

    output = (
        "[ERROR] BUILD FAILURE\n"
        "Rule 0: org.apache.maven.enforcer.rules.version.RequireMavenVersion failed\n"
        "Detected Maven Version: 3.8.7 is not in the allowed range [3.9,)."
    )

    class CompletedVersionFailureOrchestrator(RoutingOrchestrator):
        def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
            self.soft_timeout_calls.append(command)
            return {
                "success": False,
                "runner_dispatched": True,
                "exit_code": 1,
                "output": output,
                "full_output": output,
                "termination_reason": None,
                "dispatch_status": "completed_detached",
                "lifecycle_state": "finished",
                "dispatch": {
                    "job_id": "maven-version",
                    "log_path": "/tmp/sag_jobs/maven-version.log",
                    "exit_code_path": "/tmp/sag_jobs/maven-version.log.exit",
                },
            }

    persisted = []
    tool = MavenTool(CompletedVersionFailureOrchestrator())
    tool.output_storage = OutputStorageManager(tmp_path)
    tool._persist_maven_requirement_failure = lambda **kwargs: persisted.append(kwargs) or True

    result = tool.execute(command="compile", working_directory="/workspace/p")

    assert result.succeeded is False
    assert result.error_code == "MAVEN_VERSION_ERROR"
    assert result.metadata["dispatch_status"] == "completed_detached"
    assert result.metadata["maven_version_requirement"]["raw"] == "[3.9,)"
    assert result.metadata["runtime_contract_persisted"] is True
    assert result.poll_ref == "job:maven-version"
    assert persisted and persisted[0]["working_directory"] == "/workspace/p"


# --- review fixes: tools actually route long commands through dispatch ------


class RoutingOrchestrator:
    """Fake with both execution APIs to prove the dispatch path is preferred.

    execute_command answers the tools' executable/wrapper probes the same way
    tests/test_maven_gradle_tool_contracts.py's fake does.
    """

    def __init__(self, handoff=True, handoff_status="running_detached"):
        self.soft_timeout_calls = []
        self.monitoring_calls = []
        self.project_name = None
        self._handoff = handoff
        self._handoff_status = handoff_status
        self.manifest_raw = canonical_json(
            complete_build_requirements_v1(project_root="/workspace/p")
        )

    def execute_control_command(self, command, **kwargs):
        return self.execute_command(command, **kwargs)

    def _published_manifest_stream(self):
        authority = evidence_publication_authority_for(self)
        if authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID) is None:
            authority.publish_revision(
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                raw=self.manifest_raw.encode("utf-8"),
                expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
            )
        return {
            "success": True,
            "exit_code": 0,
            "output": frame_named_json_record_stream(
                [("build_requirements.json", self.manifest_raw)]
            ),
        }

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in command and REQUIREMENTS_PATH in command:
            return self._published_manifest_stream()
        if command in ("which mvn", "command -v mvn"):
            return {"success": True, "output": "/usr/bin/mvn", "exit_code": 0}
        if command in ("which gradle", "command -v gradle"):
            return {"success": True, "output": "/usr/bin/gradle", "exit_code": 0}
        if command.startswith("test -x /usr/bin/mvn") or command.startswith(
            "test -x /usr/bin/gradle"
        ):
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if command == "/usr/bin/mvn -version":
            return {"success": True, "output": "Apache Maven 3.9.6", "exit_code": 0}
        if command == "/usr/bin/gradle -version":
            return {"success": True, "output": "Gradle 8.5", "exit_code": 0}
        if "pom.xml && echo 'EXISTS'" in command:
            return {"success": True, "output": "EXISTS", "exit_code": 0}
        if "grep -q '<modules>'" in command:
            return {"success": False, "output": "NO_MODULES", "exit_code": 1}
        if "settings.gradle" in command and "grep -q 'include'" in command:
            return {"success": False, "output": "", "exit_code": 1}
        return {"exit_code": 0, "output": "", "success": True}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitoring_calls.append(command)
        return {"exit_code": 0, "output": "BUILD SUCCESSFUL", "success": True}

    def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
        self.soft_timeout_calls.append(command)
        if self._handoff:
            return {
                "success": True,
                "runner_dispatched": True,
                "exit_code": None,
                "output": "still running; poll /tmp/sag_jobs/abc.log",
                "termination_reason": None,
                "dispatch_status": self._handoff_status,
                "dispatch": {
                    "pid": 1,
                    "log_path": "/tmp/sag_jobs/abc.log",
                    "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
                },
            }
        return {
            "success": True,
            "runner_dispatched": True,
            "exit_code": 0,
            "output": "BUILD SUCCESSFUL",
            "termination_reason": None,
            "dispatch_status": "completed_detached",
            "dispatch": {
                "job_id": "abc",
                "log_path": "/tmp/sag_jobs/abc.log",
                "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            },
        }


def test_gradle_tool_routes_build_through_dispatch_and_returns_handoff():
    from sag.tools.internal.gradle_tool import GradleTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = GradleTool(orchestrator)

    result = tool.execute(tasks="build", working_directory="/workspace/p")

    assert orchestrator.soft_timeout_calls, "gradle build must use dispatch-and-poll"
    assert orchestrator.monitoring_calls == []
    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.metadata["dispatch_status"] == "running_detached"
    assert "/tmp/sag_jobs/abc.log" in result.output


def test_maven_tool_routes_test_through_dispatch():
    from sag.tools.internal.maven_tool import MavenTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = MavenTool(orchestrator)

    result = tool.execute(command="test", working_directory="/workspace/p")

    assert orchestrator.soft_timeout_calls, "maven test must use dispatch-and-poll"
    assert result.metadata["dispatch_status"] == "running_detached"
    assert result.metadata["runner_dispatched"] is True


def test_maven_pending_profile_receipt_survives_terminal_search_poll():
    from sag.tools.internal.maven_tool import MavenTool

    tracker = CommandTracker()
    tool = MavenTool(
        RoutingOrchestrator(handoff=True),
        command_tracker=tracker,
    )

    pending = tool.execute(
        command="package",
        profiles="foo",
        working_directory="/workspace/p",
    )

    assert pending.invocation_status is InvocationStatus.PENDING
    assert tracker.get_all_build_commands() == []
    receipts = tracker.get_all_execution_receipts()
    assert len(receipts) == 1
    assert receipts[0]["command"] == "/usr/bin/mvn -Pfoo package"
    assert receipts[0]["working_dir"] == "/workspace/p"
    assert receipts[0]["dispatch_status"] == "running_detached"
    assert receipts[0]["poll_ref"] == "job:abc"
    assert receipts[0]["invocation_status"] == "pending"

    poll_orchestrator = PollingJobOrchestrator(
        {
            "finished": True,
            "running": False,
            "exit_code": 0,
            "tail": "BUILD SUCCESS",
            "log_size": 13,
            "probe_success": True,
            "state": "finished",
        },
        {
            "exit_code": 0,
            "output": "BUILD SUCCESS",
            "full_output": "BUILD SUCCESS",
            "dispatch_status": "completed_detached",
        },
    )
    completed = SearchTool(
        poll_orchestrator,
        command_tracker=tracker,
    ).execute(target="job:abc", pattern=".")

    assert completed.invocation_status is InvocationStatus.COMPLETED
    assert receipts[0]["invocation_status"] == "completed"
    assert receipts[0]["dispatch_status"] == "completed_detached"
    assert receipts[0]["operation_outcome"] == "success"


def test_maven_timeout_still_records_profile_graph_receipt():
    from sag.tools.internal.maven_tool import MavenTool

    class TimeoutOrchestrator(RoutingOrchestrator):
        def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
            self.soft_timeout_calls.append(command)
            return {
                "success": False,
                "runner_dispatched": True,
                "exit_code": 124,
                "output": "timed out while packaging",
                "termination_reason": "absolute_timeout",
                "execution_time": 300,
            }

    tracker = CommandTracker()
    result = MavenTool(
        TimeoutOrchestrator(),
        command_tracker=tracker,
    ).execute(
        command="package",
        profiles="foo",
        working_directory="/workspace/p",
    )

    assert result.invocation_status is InvocationStatus.TIMEOUT
    assert result.metadata["runner_dispatched"] is True
    receipts = tracker.get_all_execution_receipts()
    assert len(receipts) == 1
    assert receipts[0]["command"] == "/usr/bin/mvn -Pfoo package"
    assert receipts[0]["termination_reason"] == "absolute_timeout"
    assert receipts[0]["invocation_status"] == "timeout"


def test_maven_preflight_refusal_does_not_record_execution_receipt():
    from sag.tools.internal.maven_tool import MavenTool

    class MissingPomOrchestrator(RoutingOrchestrator):
        def execute_command(self, command, workdir=None, timeout=None, **kwargs):
            if "pom.xml && echo 'EXISTS'" in command:
                return {
                    "success": True,
                    "output": "MISSING",
                    "exit_code": 0,
                }
            return super().execute_command(
                command,
                workdir=workdir,
                timeout=timeout,
                **kwargs,
            )

    tracker = CommandTracker()
    result = MavenTool(
        MissingPomOrchestrator(),
        command_tracker=tracker,
    ).execute(
        command="package",
        profiles="foo",
        working_directory="/workspace/p",
    )

    assert tracker.get_all_execution_receipts() == []
    assert tracker.get_all_build_commands() == []
    assert result.metadata.get("runner_dispatched") is not True


def test_maven_dispatch_failure_does_not_record_execution_receipt():
    from sag.tools.internal.maven_tool import MavenTool

    class DispatchFailedOrchestrator(RoutingOrchestrator):
        def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
            self.soft_timeout_calls.append(command)
            return {
                "success": False,
                "runner_dispatched": False,
                "exit_code": 1,
                "output": "detached launcher failed",
                "termination_reason": None,
                "dispatch_status": "dispatch_failed",
            }

    tracker = CommandTracker()
    result = MavenTool(
        DispatchFailedOrchestrator(),
        command_tracker=tracker,
    ).execute(
        command="package",
        profiles="foo",
        working_directory="/workspace/p",
    )

    assert tracker.get_all_execution_receipts() == []
    assert tracker.get_all_build_commands() == []
    assert result.metadata["dispatch_status"] == "dispatch_failed"
    assert result.metadata["runner_dispatched"] is False


@pytest.mark.parametrize(
    ("exit_code", "output", "expected_outcome"),
    [
        (0, "[INFO] BUILD SUCCESS", OperationOutcome.SUCCESS),
        (1, "[INFO] BUILD FAILURE", OperationOutcome.FAILED),
    ],
)
def test_maven_terminal_result_propagates_real_runner_dispatch(
    exit_code,
    output,
    expected_outcome,
):
    from sag.tools.internal.maven_tool import MavenTool

    class TerminalOrchestrator(RoutingOrchestrator):
        def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
            self.soft_timeout_calls.append(command)
            return {
                "success": exit_code == 0,
                "runner_dispatched": True,
                "exit_code": exit_code,
                "output": output,
                "termination_reason": None,
            }

    tracker = CommandTracker()
    result = MavenTool(
        TerminalOrchestrator(),
        command_tracker=tracker,
    ).execute(
        command="package",
        profiles="foo",
        working_directory="/workspace/p",
        raw_output=True,
    )

    assert result.operation_outcome is expected_outcome
    assert result.metadata["runner_dispatched"] is True
    assert len(tracker.get_all_execution_receipts()) == 1
    assert len(tracker.get_all_build_commands()) == 1


def test_maven_retry_dispatch_failure_preserves_first_real_dispatch(
    monkeypatch,
):
    from sag.tools.internal.maven_tool import MavenTool

    class RetryOrchestrator(RoutingOrchestrator):
        def __init__(self):
            super().__init__()
            self.results = [
                {
                    "success": False,
                    "runner_dispatched": True,
                    "exit_code": 1,
                    "output": (
                        "RequireJavaVersion failed: version 11 is not in "
                        "the allowed range [17,) BUILD FAILURE"
                    ),
                    "termination_reason": None,
                },
                {
                    "success": False,
                    "runner_dispatched": False,
                    "exit_code": 1,
                    "output": "detached launcher failed",
                    "termination_reason": None,
                    "dispatch_status": "dispatch_failed",
                },
            ]

        def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
            self.soft_timeout_calls.append(command)
            return self.results.pop(0)

    def preflight(_self, required, source):
        if source == "build-error":
            return SimpleNamespace(
                provisioned=True,
                active_version="17",
                narration="",
            )
        return SimpleNamespace(
            provisioned=False,
            active_version="11",
            narration="",
        )

    monkeypatch.setattr(
        "sag.tools.internal.maven_tool.JdkPreflight.run",
        preflight,
    )
    tracker = CommandTracker()
    result = MavenTool(
        RetryOrchestrator(),
        command_tracker=tracker,
    ).execute(
        command="compile",
        profiles="foo",
        working_directory="/workspace/p",
    )

    assert result.metadata["runner_dispatched"] is True
    assert result.metadata["final_runner_dispatched"] is False
    assert result.metadata["dispatch_status"] == "dispatch_failed"
    assert result.metadata["jdk_retry"] == {"from": "11", "to": "17"}
    assert len(tracker.get_all_execution_receipts()) == 1
    assert tracker.get_all_build_commands() == []


def test_bash_tool_routes_long_command_through_dispatch():
    from sag.tools.bash import BashTool

    orchestrator = RoutingOrchestrator(handoff=True)
    tool = BashTool(orchestrator)

    result = tool.execute(command="mvn verify", timeout=1200)

    assert orchestrator.soft_timeout_calls, "mvn verify must use dispatch-and-poll"
    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.metadata["dispatch_status"] == "running_detached"


@pytest.mark.parametrize("tool_name", ["bash", "maven", "gradle"])
def test_build_tools_preserve_unknown_liveness_as_pending_handoff(tool_name):
    orchestrator = RoutingOrchestrator(
        handoff=True,
        handoff_status="liveness_unknown_detached",
    )
    if tool_name == "bash":
        from sag.tools.bash import BashTool

        result = BashTool(orchestrator).execute(command="mvn verify", timeout=1200)
    elif tool_name == "maven":
        from sag.tools.internal.maven_tool import MavenTool

        result = MavenTool(orchestrator).execute(command="test", working_directory="/workspace/p")
    else:
        from sag.tools.internal.gradle_tool import GradleTool

        result = GradleTool(orchestrator).execute(tasks="build", working_directory="/workspace/p")

    assert result.invocation_status is InvocationStatus.PENDING
    assert result.operation_outcome is OperationOutcome.UNKNOWN
    assert result.evidence_status is EvidenceStatus.UNKNOWN
    assert result.poll_ref == "job:abc"
    assert result.metadata["dispatch_status"] == "liveness_unknown_detached"


def test_bash_quick_inspection_commands_not_dispatched():
    from sag.tools.bash import BashTool

    orchestrator = RoutingOrchestrator()
    tool = BashTool(orchestrator)

    for command in (
        "cat build.gradle",
        "ls src/test/java",
        "tail -n 50 /tmp/sag_jobs/abc.log",
        "test -d /workspace/p",
    ):
        tool.execute(command=command, timeout=60)

    assert orchestrator.soft_timeout_calls == []


# --- review fixes: repetition detector must not fight polling ---------------


def test_repetition_detector_exempts_dispatch_poll_commands():
    from sag.agent.tool_orchestration import ToolOrchestrator

    poll_signature = (
        "bash:[('command', 'tail -n 50 /tmp/sag_jobs/abc.log'), "
        "('working_directory', '/workspace')]"
    )
    recent = [
        {
            "signature": poll_signature,
            "invocation_status": "completed",
            "operation_outcome": "success",
            "timestamp": f"ts-{i}",
        }
        for i in range(9)
    ]
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=recent,
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    # The poll command itself is never treated as a loop...
    assert orchestrator._get_repetition_level(poll_signature) == 0
    # ...and 9 polls must not inflate the bash flood count for other commands.
    other_signature = "bash:[('command', 'ls /workspace')]"
    assert orchestrator._get_repetition_level(other_signature) == 0


# --- round-3 fix: dispatch routing scans the command line, not heredoc bodies


def test_heredoc_body_keywords_do_not_trigger_dispatch():
    """Round-3 finding: `python - <<'PY' ... import unittest ...` was dispatched
    because keywords in the heredoc BODY matched. Only the command line before
    the heredoc marker may be scanned."""
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert (
        tool._is_long_running_command(
            "python - <<'PY'\nimport subprocess\nsubprocess.run(['mvn','test'])\nPY"
        )
        is False
    )


def test_version_probe_flags_are_quick():
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert tool._is_long_running_command("./mvnw -v") is False
    assert tool._is_long_running_command("mvn --version") is False
    assert tool._is_long_running_command("./gradlew --version") is False


def test_real_builds_still_dispatch():
    from sag.tools.bash import BashTool

    tool = BashTool(RoutingOrchestrator())
    assert tool._is_long_running_command("mvn clean install") is True
    assert tool._is_long_running_command("./gradlew compileJava --no-daemon") is True
