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


class Fuse:
    """Injected clock with a fuse: a mutation that removes an exit condition
    raises instead of hanging (the M9 lesson)."""

    def __init__(self, start=1000.0, fuse=50000.0):
        self.t = float(start)
        self._limit = self.t + fuse

    def now(self):
        if self.t > self._limit:
            raise AssertionError("clock fuse blown — an exit condition is gone")
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))


def running(size, fresh=None, epoch=None):
    return {
        "finished": False,
        "running": True,
        "exit_code": None,
        "tail": "> compiling",
        "log_size": size,
        "probe_success": True,
        "state": "running",
        "progress_fresh": fresh,
        "now_epoch": epoch,
    }


FINISHED = {
    "finished": True,
    "running": False,
    "exit_code": 0,
    "tail": "BUILD SUCCESS",
    "log_size": 500,
    "probe_success": True,
    "state": "finished",
}


def stall_orchestrator(polls, log_content="BUILD SUCCESS"):
    orch = DockerOrchestrator.__new__(DockerOrchestrator)
    orch.container_name = "sag-demo"
    orch.command_log = []
    orch.execute_command_detached = lambda command, **kwargs: {
        "started": True,
        "pid": 4242,
        "log_path": "/tmp/sag_jobs/stall.log",
        "exit_code_path": "/tmp/sag_jobs/stall.log.exit",
        "pid_path": "/tmp/sag_jobs/stall.log.pid",
    }
    seq = iter(polls)
    last = polls[-1]
    orch.poll_calls = []

    def poll(handle, tail_lines=40, progress_workdir=None, progress_since=None):
        orch.poll_calls.append({"workdir": progress_workdir, "since": progress_since})
        return next(seq, last)

    orch.poll_detached_command = poll

    def execute(command, **kwargs):
        orch.command_log.append(command)
        return {"exit_code": 0, "output": log_content}

    orch.execute_command = execute
    return orch


# Fence 1 (spec §8.1): quiet stdout + growing tree -> no handoff before the
# wall guard. Mutation "drop S2" -> the stall clock fires at 600 -> red.
def test_growing_tree_holds_past_the_total_window():
    clock = Fuse()
    polls = [running(100, fresh=True, epoch=1754500000 + i) for i in range(14)]
    polls.append(FINISHED)
    orch = stall_orchestrator(polls)
    orch.hold_deadline_provider = lambda: 1000.0 + 40000.0

    result = orch.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="progress",
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["dispatch_status"] == "completed_detached"
    assert clock.t - 1000.0 > 900  # held well past the old fixed window


# Fence 2 (spec §8.2): fully stalled -> handoff at stall_seconds ± one poll.
# Mutations: stall clock never fires -> reason "window" at 900 -> red;
# fixed-900 restored -> held ~900 -> red.
def test_fully_stalled_hands_off_at_the_stall_window():
    clock = Fuse()
    orch = stall_orchestrator([running(100)])

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "stalled"
    assert result["dispatch_status"] == "running_detached"
    assert result["success"] is True, "a handoff is not a failure"
    assert result["exit_code"] is None
    held = clock.t - 1000.0
    assert 600 <= held <= 660  # stall window ± one poll interval


# Fence 3 (spec §8.3): progress resets the clock — the stall is measured from
# the last progress, not from dispatch. Mutation "no reset" -> fires at ~602 -> red.
def test_progress_resets_the_stall_clock():
    clock = Fuse()
    # Poll times with delays [2,5,10] then 100: 1002,1007,1017,1117,1217,...
    # Fresh through the 5th poll (t=1217) -> stall must fire near 1817.
    polls = [running(100, fresh=True) for _ in range(5)] + [running(100)]
    orch = stall_orchestrator(polls)

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=3600,
        poll_interval=100,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "stalled"
    held = clock.t - 1000.0
    assert held >= 780, "the clock must restart at the last progress"
    assert held <= 900


# Fence 4 (spec §8.5): progress tier WITHOUT a budget basis stays windowed.
# Mutation "default unbounded" -> the Fuse raises -> red, no hang.
def test_progress_tier_without_budget_stays_windowed():
    clock = Fuse()
    polls = [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(60)]
    orch = stall_orchestrator(polls)  # NO hold_deadline_provider installed

    result = orch.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="progress",
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "window"
    assert clock.t - 1000.0 <= 1000


# Fence 5 (spec §8.7 / §4.2): the wall deadline bounds even a progressing hold.
def test_wall_deadline_bounds_a_progressing_hold():
    clock = Fuse()
    polls = [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(60)]
    orch = stall_orchestrator(polls)
    orch.hold_deadline_provider = lambda: 2400.0  # 1400s of margin

    result = orch.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="progress",
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "wall_clock"
    assert 1400 <= clock.t - 1000.0 <= 1500
    assert "report reserve" in result["output"]


# Fence 6 (spec §8.6): a stall handoff never kills; the handle survives.
def test_stall_handoff_never_kills():
    clock = Fuse()
    orch = stall_orchestrator([running(100)])

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert not any("kill" in c for c in orch.command_log)
    assert result["dispatch"]["pid"] == 4242
    assert result["dispatch"]["log_path"] == "/tmp/sag_jobs/stall.log"


# Fence 7 (spec §8.7): stall_seconds=0 disables the stall clock entirely.
def test_stall_zero_is_the_fixed_window_escape_hatch():
    clock = Fuse()
    orch = stall_orchestrator([running(100)])

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        stall_seconds=0,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "window"
    assert 900 <= clock.t - 1000.0 <= 960


# Fence 8 (spec §8.8 / §5): observations, never the conclusion "hung".
def test_stall_handoff_text_states_observations_not_conclusions():
    clock = Fuse()
    orch = stall_orchestrator([running(100)])

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    out = result["output"]
    assert "no observable progress for" in out
    assert "stdout last grew" in out
    assert "NOT killed" in out
    assert "do NOT start the same build again" in out
    assert "hung" not in out.lower()


# Fence 9 (spec §8.9): held-to-completion == within-window completion,
# field for field. Mutation "held path adds/loses a field" -> red.
def test_held_to_completion_equals_within_window_completion():
    quick_clock = Fuse()
    quick = stall_orchestrator([running(100), FINISHED])
    within = quick.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        hold="progress",
        stall_seconds=600,
        now=quick_clock.now,
        sleep=quick_clock.sleep,
    )

    held_clock = Fuse()
    long_polls = [running(100, fresh=True) for _ in range(14)] + [FINISHED]
    slow = stall_orchestrator(long_polls)
    slow.hold_deadline_provider = lambda: 1000.0 + 40000.0
    held = slow.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="progress",
        stall_seconds=600,
        now=held_clock.now,
        sleep=held_clock.sleep,
    )

    assert sorted(within.keys()) == sorted(held.keys())
    for key in ("success", "exit_code", "dispatch_status", "output"):
        assert within[key] == held[key]


# The stall probe hands the poll its workdir and last container clock.
def test_hold_loop_threads_workdir_and_container_clock_into_the_probe():
    clock = Fuse()
    polls = [
        running(100, fresh=True, epoch=1754500000),
        running(100, fresh=True, epoch=1754500100),
        FINISHED,
    ]
    orch = stall_orchestrator(polls)

    orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert orch.poll_calls[0]["workdir"] == "/workspace/proj"
    assert orch.poll_calls[0]["since"] is None  # first cycle: S1 only
    assert orch.poll_calls[1]["since"] == 1754500000  # container clock, not host
