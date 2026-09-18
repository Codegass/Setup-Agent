import base64
import shlex
import subprocess
import sys

from loguru import logger as loguru_logger

from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import EvidencePublicationAuthority
from sag.docker_orch import orch
from sag.docker_orch.orch import DockerOrchestrator
from sag.runtime.env_overlay import EnvOverlayStore, EnvOverlayUnavailableError


def test_docker_orchestrator_import_is_order_independent_in_a_fresh_interpreter():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from sag.docker_orch.orch import DockerOrchestrator; "
                "from sag.runtime import EnvOverlayStore; "
                "from sag.agent import SetupAgent, ReActEngine"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0, probe.stderr


class FakeExecResult:
    def __init__(self, exit_code=0, output=(b"ok", b"")):
        self.exit_code = exit_code
        self.output = output


class FakeStreamingExecResult:
    def __init__(self, exit_code=0, output=()):
        self.exit_code = exit_code
        self.output = iter(output)


class FakeContainer:
    id = "c" * 64

    def __init__(self, exec_result=None):
        self.exec_calls = []
        self.exec_result = exec_result or FakeExecResult()

    def exec_run(self, exec_command, **kwargs):
        self.exec_calls.append({"exec_command": exec_command, "kwargs": kwargs})
        return self.exec_result


class FailingExecContainer(FakeContainer):
    def exec_run(self, exec_command, **kwargs):
        self.exec_calls.append({"exec_command": exec_command, "kwargs": kwargs})
        raise RuntimeError("docker exec was never accepted")


class FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, _container_name):
        return self.container


class FakeClient:
    def __init__(self, container):
        self.containers = FakeContainers(container)
        self.api = FakeDaemonAPI(container.id, container)


class FakeDaemonAPI:
    def __init__(self, container_id, container=None):
        self.container_id = container_id
        self.container = container
        self.exec_id = "d" * 64
        self.create_calls = []
        self.start_calls = []

    def exec_create(self, container, cmd, **kwargs):
        self.create_calls.append({"container": container, "cmd": cmd, "kwargs": kwargs})
        return {"Id": self.exec_id}

    def exec_start(self, exec_id, **kwargs):
        self.start_calls.append({"exec_id": exec_id, "kwargs": kwargs})
        if kwargs.get("stream"):
            call = self.create_calls[-1]
            return self.container.exec_run(call["cmd"], **call["kwargs"], **kwargs).output

    def exec_inspect(self, exec_id):
        return {
            "ID": exec_id,
            "ContainerID": self.container_id,
            "Running": False,
            "ExitCode": self.container.exec_result.exit_code if self.container else 0,
        }


def build_orchestrator(container, *, authorize_runtime=True):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.client = FakeClient(container)
    orchestrator.container_name = "sag-demo"
    orchestrator.is_container_running = lambda: True
    if authorize_runtime:
        orchestrator._sag_evidence_publication_authority = EvidencePublicationAuthority(
            run_id=f"run-orchestrator-{id(orchestrator)}"
        )

        def runtime_environment(environment=None):
            runtime = orchestrator._control_exec_environment()
            for key, value in (environment or {}).items():
                if key not in {"PATH", "BASH_ENV", "ENV", "CDPATH"}:
                    runtime[key] = value
            return runtime

        orchestrator._default_exec_environment = runtime_environment
    return orchestrator


def test_normal_runner_without_host_authority_is_not_dispatched():
    container = FakeContainer()
    orchestrator = build_orchestrator(container, authorize_runtime=False)

    result = orchestrator.execute_command("mvn test")

    assert result["success"] is False
    assert result["dispatch_status"] == "environment_overlay_unavailable"
    assert result["runner_dispatched"] is False
    assert container.exec_calls == []


def test_runtime_profile_prefix_exports_utf8_locale_before_tools_run():
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)

    prefix = orchestrator._runtime_profile_prefix()

    assert "export LANG=${LANG:-C.UTF-8}" in prefix
    assert "export LC_ALL=${LC_ALL:-C.UTF-8}" in prefix
    assert prefix.index("export LANG=${LANG:-C.UTF-8}") < prefix.index(
        "export LC_ALL=${LC_ALL:-C.UTF-8}"
    )


def test_execute_command_passes_utf8_environment_to_docker_exec():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command("locale")

    assert result["success"] is True
    assert result["runner_dispatched"] is True
    exec_env = container.exec_calls[-1]["kwargs"]["environment"]
    assert exec_env["LANG"] == "C.UTF-8"
    assert exec_env["LC_ALL"] == "C.UTF-8"
    assert "LANG=C.UTF-8" in container.exec_calls[-1]["exec_command"]


def test_execute_command_preserves_explicit_environment_overrides():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command(
        "locale",
        environment={
            "LANG": "en_US.UTF-8",
            "CUSTOM_FLAG": "1",
            "PATH": "/workspace/attacker",
            "BASH_ENV": "/workspace/.bashrc",
        },
    )

    assert result["success"] is True
    exec_env = container.exec_calls[-1]["kwargs"]["environment"]
    assert exec_env["LANG"] == "C.UTF-8"
    assert exec_env["LC_ALL"] == "C.UTF-8"
    assert exec_env["PATH"] == orch.CONTROL_EXEC_PATH
    assert exec_env["BASH_ENV"] == ""
    runtime_argv = container.exec_calls[-1]["exec_command"]
    assert "LANG=en_US.UTF-8" in runtime_argv
    assert "CUSTOM_FLAG=1" in runtime_argv
    assert f"PATH={orch.CONTROL_EXEC_PATH}" in runtime_argv
    assert "PATH=/workspace/attacker" not in runtime_argv
    assert "BASH_ENV=/workspace/.bashrc" not in runtime_argv


def test_execute_command_with_monitoring_passes_utf8_environment_to_docker_exec():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    assert result["runner_dispatched"] is True
    exec_env = container.exec_calls[-1]["kwargs"]["environment"]
    assert exec_env["LANG"] == "C.UTF-8"
    assert exec_env["LC_ALL"] == "C.UTF-8"


def test_execute_command_exec_exception_is_not_a_runner_dispatch():
    orchestrator = build_orchestrator(FailingExecContainer())

    result = orchestrator.execute_command("mvn test")

    assert result["success"] is False
    assert result["dispatch_status"] == "dispatch_failed"
    assert result["runner_dispatched"] is False


def test_monitored_start_exception_retains_unknown_dispatch():
    orchestrator = build_orchestrator(FailingExecContainer())

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["dispatch_status"] == "dispatch_unknown"
    assert result["runner_dispatched"] is None


def test_monitoring_failure_after_exec_preserves_runner_dispatch():
    orchestrator = build_orchestrator(FakeContainer(FakeStreamingExecResult()))

    def fail_after_dispatch(*_args, **_kwargs):
        raise RuntimeError("stream observation failed")

    orchestrator._monitor_execution_with_timeouts = fail_after_dispatch
    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["dispatch_status"] == "execution_observation_failed"
    assert result["runner_dispatched"] is True


def test_connect_to_container_passes_utf8_environment_to_docker_exec(monkeypatch):
    container = FakeContainer()
    orchestrator = build_orchestrator(container)
    calls = []

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("subprocess.call", lambda cmd: calls.append(cmd) or 0)

    orchestrator.connect_to_container("/bin/sh")

    assert calls == [
        [
            "docker",
            "exec",
            "-e",
            "LANG=C.UTF-8",
            "-e",
            "LC_ALL=C.UTF-8",
            "-i",
            "sag-demo",
            "/bin/sh",
        ]
    ]


def test_runtime_profile_prefix_never_sources_project_writable_profiles_or_overlay():
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)

    prefix = orchestrator._runtime_profile_prefix()

    assert orch.ENV_OVERLAY_SCRIPT_PATH == "/workspace/.setup_agent/env_overlay.sh"
    assert "source /etc/profile" not in prefix
    assert "source ~/.bashrc" not in prefix
    assert orch.ENV_OVERLAY_SCRIPT_PATH not in prefix


def test_project_writable_profile_and_overlay_cannot_change_runtime_prefix(tmp_path):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    old_bin = tmp_path / "old-bin"
    new_bin = tmp_path / "new-bin"
    old_bin.mkdir()
    new_bin.mkdir()
    old_maven = old_bin / "mvn"
    new_maven = new_bin / "mvn"
    old_maven.write_text("#!/bin/sh\nexit 0\n")
    new_maven.write_text("#!/bin/sh\nexit 0\n")
    old_maven.chmod(0o755)
    new_maven.chmod(0o755)
    profile = tmp_path / "profile"
    bashrc = tmp_path / "bashrc"
    overlay = tmp_path / "env_overlay.sh"
    profile.write_text(f"export PATH={shlex.quote(str(old_bin))}:$PATH\n")
    bashrc.write_text(f"export PATH={shlex.quote(str(old_bin))}:$PATH\n")
    overlay.write_text(f"export PATH={shlex.quote(str(new_bin))}:$PATH\n")

    prefix = orchestrator._runtime_profile_prefix()
    completed = subprocess.run(
        ["/bin/bash", "-c", f"{prefix}; command -v mvn"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": f"{old_bin}:/usr/bin:/bin"},
    )

    assert completed.stdout.strip() == str(old_maven)


def test_execute_command_never_sources_profiles_or_overlay_before_command():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project"

    result = orchestrator.execute_command("echo hi", workdir=workdir)

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][-1]
    assert "source /etc/profile" not in wrapped_command
    assert "source ~/.bashrc" not in wrapped_command
    assert orch.ENV_OVERLAY_SCRIPT_PATH not in wrapped_command
    assert wrapped_command.endswith(f"cd {shlex.quote(workdir)} && echo hi")


def test_clean_control_command_uses_fixed_path_and_no_runtime_sources():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_control_command(
        "command -v base64",
        environment={"PATH": "/workspace/attacker", "BASH_ENV": "/workspace/.bashrc"},
    )

    assert result["success"] is True
    call = container.exec_calls[-1]
    wrapped_command = call["exec_command"][-1]
    assert "source /etc/profile" not in wrapped_command
    assert "source ~/.bashrc" not in wrapped_command
    assert orch.ENV_OVERLAY_SCRIPT_PATH not in wrapped_command
    assert call["kwargs"]["environment"]["PATH"] == orch.CONTROL_EXEC_PATH
    assert call["kwargs"]["environment"]["BASH_ENV"] == ""


def test_clean_control_command_never_invokes_runtime_overlay_resolution(monkeypatch):
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    def poisoned_runtime_environment(_environment=None):
        raise AssertionError("project overlay resolver must not run")

    monkeypatch.setattr(orchestrator, "_default_exec_environment", poisoned_runtime_environment)

    result = orchestrator.execute_control_command("printf safe")

    assert result["success"] is True
    assert len(container.exec_calls) == 1


def test_normal_runner_refuses_typed_unavailable_overlay_before_dispatch(monkeypatch):
    container = FakeContainer()
    orchestrator = build_orchestrator(container)

    def unavailable(_environment=None):
        raise EnvOverlayUnavailableError("published overlay was tampered")

    monkeypatch.setattr(orchestrator, "_default_exec_environment", unavailable)

    result = orchestrator.execute_command("mvn test", workdir="/workspace/project")

    assert result["success"] is False
    assert result["dispatch_status"] == "environment_overlay_unavailable"
    assert result["runner_dispatched"] is False
    assert container.exec_calls == []


def test_normal_runner_environment_is_derived_from_authorized_overlay(monkeypatch, tmp_path):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.evidence_store_identity = lambda: "docker:test-overlay-environment"
    orchestrator._sag_evidence_publication_authority = EvidencePublicationAuthority(
        run_id="run-overlay-env",
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )

    def authorized(_store, base):
        assert base["CUSTOM"] == "1"
        return {**base, "JAVA_HOME": "/opt/jdk-21", "PATH": "/opt/jdk-21/bin:/usr/bin"}

    monkeypatch.setattr(EnvOverlayStore, "authorized_environment", authorized)

    environment = orchestrator._default_exec_environment({"CUSTOM": "1"})

    assert environment["JAVA_HOME"] == "/opt/jdk-21"
    assert environment["PATH"] == "/opt/jdk-21/bin:/usr/bin"


def test_execute_command_shell_quotes_workdir_with_space_and_single_quote():
    container = FakeContainer()
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project with ' quote"

    result = orchestrator.execute_command("echo hi", workdir=workdir)

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][-1]
    assert f"cd {shlex.quote(workdir)} && echo hi" in wrapped_command
    assert "cd /workspace/project with" not in wrapped_command


def test_execute_command_with_monitoring_never_sources_profiles_or_overlay():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project"

    result = orchestrator.execute_command_with_monitoring(
        "echo hi",
        workdir=workdir,
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    wrapped_command = container.exec_calls[-1]["exec_command"][-1]
    assert "source /etc/profile" not in wrapped_command
    assert "source ~/.bashrc" not in wrapped_command
    assert orch.ENV_OVERLAY_SCRIPT_PATH not in wrapped_command
    assert wrapped_command.endswith(f"cd {shlex.quote(workdir)} && echo hi")


def test_clean_control_stream_uses_fixed_environment(monkeypatch):
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)

    def poisoned_runtime_environment(_environment=None):
        raise AssertionError("runtime overlay resolver must not run")

    monkeypatch.setattr(orchestrator, "_default_exec_environment", poisoned_runtime_environment)

    result = orchestrator.execute_control_command_with_monitoring(
        "printf safe",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    assert container.exec_calls[-1]["kwargs"]["environment"]["PATH"] == orch.CONTROL_EXEC_PATH


def test_clean_control_detached_launcher_never_uses_normal_runner(monkeypatch):
    orchestrator = build_orchestrator(FakeContainer())
    calls = []

    def clean(command, **kwargs):
        calls.append((command, kwargs))
        return {
            "success": True,
            "exit_code": 0,
            "output": f"PID:42\nPGID:42\nIDENTITY:{'a' * 64}\n",
        }

    def normal(*_args, **_kwargs):
        raise AssertionError("normal runner must not launch control work")

    monkeypatch.setattr(orchestrator, "execute_control_command", clean)
    monkeypatch.setattr(orchestrator, "execute_command", normal)

    result = orchestrator.execute_control_command_detached(
        "printf safe",
        workdir="/workspace/project",
    )

    assert result["started"] is True
    assert len(calls) == 1
    launcher = calls[0][0]
    assert "source /etc/profile" not in launcher
    assert "source ~/.bashrc" not in launcher
    assert orch.ENV_OVERLAY_SCRIPT_PATH not in launcher


def test_detached_evidence_poll_and_log_collection_use_clean_control_path(monkeypatch):
    orchestrator = build_orchestrator(FakeContainer())
    clean_commands = []

    def clean(command, **_kwargs):
        clean_commands.append(command)
        if command.startswith("cat "):
            return {"success": True, "exit_code": 0, "output": "BUILD SUCCESS"}
        return {
            "success": True,
            "exit_code": 0,
            "output": (
                "SIZE:13\nNOW:1\n---TAIL---\n" + base64.b64encode(b"BUILD SUCCESS").decode("ascii")
            ),
        }

    def normal(*_args, **_kwargs):
        raise AssertionError("runtime PATH must not observe detached evidence")

    monkeypatch.setattr(orchestrator, "execute_control_command", clean)
    monkeypatch.setattr(orchestrator, "execute_command", normal)
    handle = {
        "log_path": "/tmp/sag_jobs/job.log",
        "exit_code_path": "/tmp/sag_jobs/job.log.exit",
        "pid": 42,
        "process_identity_token": "a" * 64,
        "terminal_authority": "docker_exec_inspect_v1",
        "docker_exec_id": "d" * 64,
        "container_id": "c" * 64,
        "start_accepted": True,
        "startup_identity_verified": True,
        "runner_dispatched": True,
        "runner_dispatch_state": "accepted",
    }

    poll = orchestrator.poll_detached_command(handle)
    result = orchestrator.collect_detached_result(handle, poll)

    assert poll["finished"] is True
    assert result["success"] is True
    assert len(clean_commands) == 2


def test_execute_command_with_monitoring_treats_unknown_exit_build_failure_as_failure():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[(b"[ERROR] BUILD FAILURE\nCould not resolve dependency\n", b"")],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] is None


def test_execute_command_with_monitoring_treats_unknown_exit_ordinary_output_as_unavailable():
    container = FakeContainer(
        FakeStreamingExecResult(exit_code=None, output=[(b"[INFO] BUILD SUCCESS\n", b"")])
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "mvn test",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["observed_exit_code"] is None
    assert result["exit_code_inferred"] is False


def test_execute_command_with_monitoring_treats_unknown_exit_pip_terminal_failure_as_failure():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[
                (
                    b"ERROR: No matching distribution found for " b"apache-tvm-ffi>=0.1.13\n",
                    b"",
                )
            ],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "/workspace/tvm/.venv/bin/python -m pip install -e .",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] is None
    assert result["observed_exit_code"] is None
    assert result["exit_code_inferred"] is False


def test_execute_command_with_monitoring_keeps_unknown_exit_could_not_resolve_narrative_unavailable():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[
                (
                    b"Diagnostic note: Could not resolve whether optional docs are installed.\n",
                    b"",
                )
            ],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "bash -lc 'printf diagnostics'",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] is None


def test_execute_command_with_monitoring_keeps_unknown_exit_allowed_range_narrative_unavailable():
    container = FakeContainer(
        FakeStreamingExecResult(
            exit_code=None,
            output=[
                (
                    b"Release note: values not in the allowed range are normalized later.\n",
                    b"",
                )
            ],
        )
    )
    orchestrator = build_orchestrator(container)

    result = orchestrator.execute_command_with_monitoring(
        "bash -lc 'printf diagnostics'",
        use_timeout_wrapper=False,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is False
    assert result["exit_code"] is None


def test_execute_command_with_monitoring_preserves_quoted_workdir_in_timeout_wrapper():
    container = FakeContainer(FakeStreamingExecResult())
    orchestrator = build_orchestrator(container)
    workdir = "/workspace/project with ' quote"

    result = orchestrator.execute_command_with_monitoring(
        "echo hi",
        workdir=workdir,
        enable_cpu_monitoring=False,
    )

    assert result["success"] is True
    timeout_args = container.exec_calls[-1]["exec_command"]
    assert timeout_args[:4] == [
        "/usr/bin/timeout",
        "--preserve-status",
        "2400",
        "/usr/bin/env",
    ]
    assert timeout_args[-3:-1] == ["/bin/bash", "-c"]
    base_command = timeout_args[-1]
    assert f"cd {shlex.quote(workdir)} && echo hi" in base_command
    assert "cd /workspace/project with" not in base_command


def test_a_long_output_is_cut_quietly_and_the_log_says_what_was_kept():
    """The 25+25 cut is bookkeeping, not an alarm.

    Any output over 100 lines and 10,000 characters is cut to its first and
    last 25 lines. In the first real commons-cli run this fired five times —
    twice on `apt-get install`, three times on a `find` listing — and none of
    the five was ever read: every consumer on this path checks the exit code,
    and the model-facing tools opt out of the cut (`bash` passes
    `truncate_output=False`; the detached build log is read whole). The old
    line said `🚨 … to prevent context pollution` at WARNING, naming a danger
    the output was never headed for. The fact belongs at DEBUG in plain words;
    the marker inside the output stays, because it is true.
    """

    body = "\n".join(
        f"Unpacking package-{n:03d} ({n}.0.0-1ubuntu0.6_amd64.deb) over ({n}.0.0-1ubuntu0.4) ..."
        for n in range(200)
    )
    assert len(body) > 10_000
    container = FakeContainer(FakeExecResult(exit_code=0, output=(body.encode(), b"")))
    orchestrator = build_orchestrator(container)
    seen: list[tuple[str, str]] = []
    handle = loguru_logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    try:
        result = orchestrator.execute_command("apt-get install -y -qq curl wget git")
    finally:
        loguru_logger.remove(handle)

    assert "[ORCHESTRATOR TRUNCATED: 200 lines" in result["output"]
    assert result["output"].count("\n") == 50  # 25 kept, the marker line, 25 kept
    assert [message for level, message in seen if level == "WARNING"] == []
    said = [
        message
        for level, message in seen
        if level == "DEBUG" and "kept the first 25 and last 25" in message
    ]
    assert said, seen
    assert said[0].startswith("apt-get install -y -qq curl wget git: ")
    assert "200 lines" in said[0]
    assert "pollution" not in said[0] and "🚨" not in said[0]
