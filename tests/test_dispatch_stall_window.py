"""Dispatch stall window (spec 2026-08-06): hold while it progresses,
hand off when it stalls. Time is always injected — never a real sleep."""

import pytest

from sag.config.settings import Config


def test_stall_window_config_defaults_to_600():
    assert Config().dispatch_stall_seconds == 600


def test_stall_window_config_reads_env(monkeypatch):
    monkeypatch.setenv("SAG_DISPATCH_STALL_SECONDS", "120")
    assert Config.from_env().dispatch_stall_seconds == 120


from sag.tools.internal.build_utils import dispatch_hold_policy


@pytest.mark.parametrize(
    "argv,expected",
    [
        ("mvn clean compile", "progress"),
        ("./mvnw dependency:go-offline", "progress"),
        ("mvn clean install -DskipTests", "progress"),
        ("mvn verify -Dmaven.test.skip=true", "progress"),
        ("mvn clean install", "windowed"),          # runs tests
        ("mvn test", "windowed"),
        ("mvn spotless:apply compile", "windowed"),  # unknown goal -> refuse to guess
        ("mvn", "windowed"),                         # no goals -> refuse to guess
    ],
)
def test_maven_hold_policy(argv, expected):
    assert dispatch_hold_policy("maven", argv) == expected


@pytest.mark.parametrize(
    "argv,expected",
    [
        ("./gradlew clean assemble", "progress"),
        ("./gradlew compileJava --no-daemon", "progress"),
        ("./gradlew build -x test", "progress"),
        ("./gradlew build", "windowed"),             # runs tests
        ("./gradlew test", "windowed"),
        ("./gradlew build -x test integrationTest", "windowed"),  # unknown task
    ],
)
def test_gradle_hold_policy(argv, expected):
    assert dispatch_hold_policy("gradle", argv) == expected


def test_gradle_exclusion_argument_is_not_read_as_a_task():
    # "-x test": "test" is the flag's argument, not a task to run.
    assert dispatch_hold_policy("gradle", "./gradlew assemble -x test") == "progress"


def test_unknown_system_refuses_to_guess():
    assert dispatch_hold_policy("bash", "sleep 100") == "windowed"


from sag.docker_orch.orch import DockerOrchestrator


def _bare_orchestrator(probe_output):
    orch = DockerOrchestrator.__new__(DockerOrchestrator)
    orch.container_name = "sag-demo"
    orch.command_log = []

    def fake_execute(command, **kwargs):
        orch.command_log.append(command)
        return {"exit_code": 0, "output": probe_output}

    orch.execute_command = fake_execute
    return orch


_HANDLE = {
    "log_path": "/tmp/sag_jobs/abc.log",
    "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
    "pid": 4242,
}


def test_poll_probe_emits_container_clock_and_progress_markers():
    orch = _bare_orchestrator(
        "STATE:RUNNING\nSIZE:2048\nNOW:1754500000\nPROGRESS:FRESH\n---TAIL---\ncompiling"
    )
    poll = orch.poll_detached_command(
        _HANDLE, progress_workdir="/workspace/proj", progress_since=1754499000
    )
    assert poll["state"] == "running"
    assert poll["now_epoch"] == 1754500000
    assert poll["progress_fresh"] is True
    probe = orch.command_log[0]
    # The probe asks the container, in ONE command, with 1s mtime slack.
    assert "date +%s" in probe
    assert "-newermt @1754498999" in probe
    assert "*/target/*" in probe and "*/build/*" in probe
    assert "/.setup_agent/pytest-reports/" in probe
    # Trusted markers stay in the head, before the tail separator.
    assert probe.index("PROGRESS") < probe.index("---TAIL---")


def test_poll_probe_without_workdir_skips_the_tree_scan():
    orch = _bare_orchestrator("STATE:RUNNING\nSIZE:10\nNOW:1754500000\n---TAIL---\nx")
    poll = orch.poll_detached_command(_HANDLE)
    assert poll["progress_fresh"] is None
    assert "find" not in orch.command_log[0]


def test_poll_probe_reports_no_fresh_writes():
    orch = _bare_orchestrator(
        "STATE:RUNNING\nSIZE:10\nNOW:1754500060\nPROGRESS:NONE\n---TAIL---\nquiet"
    )
    poll = orch.poll_detached_command(
        _HANDLE, progress_workdir="/workspace/proj", progress_since=1754500000
    )
    assert poll["progress_fresh"] is False


def test_progress_markers_in_the_tail_are_not_trusted():
    orch = _bare_orchestrator(
        "STATE:RUNNING\nSIZE:10\nNOW:1754500060\nPROGRESS:NONE\n---TAIL---\n"
        "echo PROGRESS:FRESH from build output"
    )
    poll = orch.poll_detached_command(
        _HANDLE, progress_workdir="/workspace/proj", progress_since=1754500000
    )
    assert poll["progress_fresh"] is False


from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine


class _EngineStub:
    _REPORT_RESERVE_SECONDS = ReActEngine._REPORT_RESERVE_SECONDS
    _hold_deadline = ReActEngine._hold_deadline
    _install_hold_deadline_provider = ReActEngine._install_hold_deadline_provider


def test_hold_deadline_is_start_plus_cap_minus_reserve():
    stub = _EngineStub()
    stub._run_started_at = 1000.0
    stub._wall_clock_cap = 7200
    assert stub._hold_deadline() == 1000.0 + 7200 - 600


def test_hold_deadline_without_a_start_time_is_none():
    # Margin unknown is margin absent — consumers degrade to bounded behavior.
    assert _EngineStub()._hold_deadline() is None


def test_installed_provider_IS_the_shared_computation():
    stub = _EngineStub()
    stub.orchestrator = SimpleNamespace()
    stub._install_hold_deadline_provider()
    provider = stub.orchestrator.hold_deadline_provider
    assert provider.__func__ is ReActEngine._hold_deadline


def test_the_close_wait_consults_the_shared_deadline():
    """Wiring pin: _await_open_obligations must read _hold_deadline, not run a
    second lookalike computation. A patched helper returning an expired
    deadline must stop the wait before any container poll."""
    calls = []

    class Stub(_EngineStub):
        _await_open_obligations = ReActEngine._await_open_obligations

    stub = Stub()
    stub._run_started_at = 1000.0
    stub._wall_clock_cap = 7200
    stub._hold_deadline = lambda: 0.0  # expired: no margin at all
    stub.orchestrator = SimpleNamespace(
        execute_command=lambda *a, **k: calls.append(a) or {"exit_code": 0, "output": ""}
    )
    from sag.agent.react_engine import EvidenceCloseReason

    stub._await_open_obligations(
        EvidenceCloseReason.TEST_TERMINATED, now=lambda: 5000.0, sleep=lambda s: None
    )
    assert calls == []  # deadline already passed: the wait never polled
