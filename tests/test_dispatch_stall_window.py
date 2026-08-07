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


@pytest.mark.parametrize(
    "argv",
    [
        # `=false` is the standard way to force tests ON over a pom that sets
        # the property — reading it as a skip hands a TEST run the unbounded tier.
        "/usr/bin/mvn -DskipTests=false verify",
        "/usr/bin/mvn -Dmaven.test.skip=false test",
        # A different property that merely starts the same way.
        "/usr/bin/mvn test -DskipTestsPhase=integration",
    ],
)
def test_a_property_that_turns_tests_ON_is_not_a_skip(argv):
    assert dispatch_hold_policy("maven", argv) == "windowed"


@pytest.mark.parametrize(
    "argv",
    ["/usr/bin/mvn install -DskipTests", "/usr/bin/mvn install -DskipTests=true"],
)
def test_a_real_skip_flag_still_reaches_the_progress_tier(argv):
    assert dispatch_hold_policy("maven", argv) == "progress"


@pytest.mark.parametrize(
    "argv",
    [
        # `test` still runs in every one of these: only an exact `-x test` /
        # `-x check` may admit `build` to the safe set.
        "/usr/bin/gradle --build-cache build -x checkstyleMain",
        "/usr/bin/gradle build -x testFixturesJar",
        "/usr/bin/gradle build --exclude-task checkstyleMain",
    ],
)
def test_excluding_some_other_task_does_not_make_build_test_free(argv):
    assert dispatch_hold_policy("gradle", argv) == "windowed"


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
    # The container clock comes back even when the tree scan is skipped: it is
    # what establishes the NEXT poll's `progress_since`.
    assert "date +%s" in orch.command_log[0]
    assert poll["now_epoch"] == 1754500000


def test_the_bootstrap_cycle_still_gets_the_container_clock():
    """Poll #1 has a workdir but no `since` yet (S1-only by design). If NOW:
    were emitted only alongside the tree scan, `since` would never be
    established and S2 would never run for the life of the process."""
    orch = _bare_orchestrator("STATE:RUNNING\nSIZE:10\nNOW:1754500000\n---TAIL---\nx")
    poll = orch.poll_detached_command(_HANDLE, progress_workdir="/workspace/proj")
    assert "find" not in orch.command_log[0]
    assert poll["progress_fresh"] is None
    assert poll["now_epoch"] == 1754500000


def test_poll_probe_reports_no_fresh_writes():
    orch = _bare_orchestrator(
        "STATE:RUNNING\nSIZE:10\nNOW:1754500060\nPROGRESS:NONE\n---TAIL---\nquiet"
    )
    poll = orch.poll_detached_command(
        _HANDLE, progress_workdir="/workspace/proj", progress_since=1754500000
    )
    assert poll["progress_fresh"] is False


def test_progress_markers_in_the_tail_are_not_trusted():
    # The tail carries BARE marker lines — byte-for-byte what the head emits.
    # Only the head/tail boundary can reject them; an exact-equality check
    # alone would accept both.
    orch = _bare_orchestrator(
        "STATE:RUNNING\nSIZE:10\nNOW:1754500060\nPROGRESS:NONE\n---TAIL---\n"
        "PROGRESS:FRESH\nNOW:9999999999"
    )
    poll = orch.poll_detached_command(
        _HANDLE, progress_workdir="/workspace/proj", progress_since=1754500000
    )
    assert poll["progress_fresh"] is False
    assert poll["now_epoch"] == 1754500060


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


def test_the_run_loop_installs_the_provider_on_its_orchestrator():
    """Wiring pin on the CALL SITE, not the installer: without this call the
    progress tier is permanently inert in production, and a dead feature is
    indistinguishable from the correctly-degraded one fence 4 asserts."""
    engine = ReActEngine.__new__(ReActEngine)
    engine.orchestrator = SimpleNamespace()
    # A cap this small is already exceeded on the loop's first check, so the
    # loop exits immediately — through the real run-start wiring.
    engine.config = SimpleNamespace(max_wall_clock_seconds=1e-9)
    engine.phase_machine = None
    engine.max_iterations = 1
    engine.agent_logger = SimpleNamespace(info=lambda *a, **k: None)
    engine.repository_url = "https://example.invalid/repo"
    engine.repository_ref = None
    engine.prompt_builder = SimpleNamespace(build_initial_system_prompt=lambda **k: "SYSTEM")
    engine._reset_advisor_run_state = lambda: None
    engine._export_token_usage_csv = lambda: None
    engine.current_iteration = 0
    engine._phase_iterations = 0
    engine.steps = []

    engine._run_native_loop("go", max_iterations=1, completion_mode="build")

    provider = engine.orchestrator.hold_deadline_provider
    assert provider.__func__ is ReActEngine._hold_deadline
    assert provider() == engine._hold_deadline()


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
    # BOTH command channels are recorded: a kill could be issued down either
    # one, and a no-kill assertion over a log the double never writes to is an
    # assertion over an empty list.
    orch.detached_log = []

    def dispatch(command, **kwargs):
        orch.detached_log.append(command)
        return {
            "started": True,
            "pid": 4242,
            "log_path": "/tmp/sag_jobs/stall.log",
            "exit_code_path": "/tmp/sag_jobs/stall.log.exit",
            "pid_path": "/tmp/sag_jobs/stall.log.pid",
        }

    orch.execute_command_detached = dispatch
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


def assert_the_handoff_killed_nothing(orch):
    """Spec §4.1: no kill semantics anywhere — on ANY channel, in ANY branch."""
    channels = list(orch.command_log) + list(orch.detached_log)
    assert channels, "the double recorded nothing; an empty log proves nothing"
    assert not any("kill" in command for command in channels)


def final_probe_orchestrator(loop_poll, final_poll, log_content="BUILD SUCCESS"):
    """A double that answers the post-loop probe differently from the in-loop
    ones. The final probe is the only poll the hold makes with no progress
    workdir, so that is what distinguishes it."""
    orch = stall_orchestrator([loop_poll], log_content=log_content)
    record = orch.poll_detached_command

    def poll(handle, tail_lines=40, progress_workdir=None, progress_since=None):
        record(
            handle,
            tail_lines=tail_lines,
            progress_workdir=progress_workdir,
            progress_since=progress_since,
        )
        return dict(final_poll if progress_workdir is None else loop_poll)

    orch.poll_detached_command = poll
    return orch


UNANSWERED = {
    "finished": False,
    "running": False,
    "exit_code": None,
    "tail": "",
    "log_size": 0,
    "probe_success": False,
    "state": "unknown",
}


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


# Fence 1b: the MIRROR of fence 1 — stdout growth alone (S1) also holds a
# prerequisite past the total window. Mutation "drop S1's progress effect" ->
# the stall fires at 600 and the build is handed off -> red.
def test_growing_stdout_alone_holds_past_the_total_window():
    clock = Fuse()
    polls = [running(100 + 10 * i, epoch=1754500000 + i) for i in range(14)]
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
    assert clock.t - 1000.0 > 900  # a quiet build tree did not shorten the hold


# Fence 1c (spec §3, headline promise): the progress tier HOLDS while it
# progresses and HANDS OFF when it stalls. Mutation "stall clock off for the
# progress tier" -> the hold runs to the wall deadline -> red.
def test_a_stalled_prerequisite_still_hands_off_at_the_stall_window():
    clock = Fuse()
    # Progress through poll #3 (t=1017), then quiet: the stall is due at 1617,
    # ~39_000s before the wall deadline.
    polls = [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(3)]
    polls.append(running(102))
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

    assert result["handoff_reason"] == "stalled"
    held = clock.t - 1000.0
    assert 600 <= held <= 720, "the stall clock, not the wall clock, ended this hold"


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


# Fence 5b (spec §8.7, the converse of fence 1): a WINDOWED dispatch keeps its
# total window even when a wall-clock budget is installed. Mutation "tier
# collapse the other way" (every dispatch treated as prerequisite) -> red.
def test_windowed_tier_keeps_its_window_when_a_budget_is_installed():
    clock = Fuse()
    polls = [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(60)]
    orch = stall_orchestrator(polls)
    orch.hold_deadline_provider = lambda: 1000.0 + 40000.0  # plenty of margin

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="windowed",
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "window"
    assert clock.t - 1000.0 <= 960  # continuous progress did NOT drop the window


# Fence 5c (spec §4.2): the wall guard bounds how far a hold may EXTEND — it
# never cancels the dispatch's own short early polls. Inside the report reserve
# the deadline is already past; a command that finishes in seconds must still
# come back completed instead of opening an obligation nothing can settle.
def test_an_expired_wall_deadline_still_gets_the_early_polls():
    clock = Fuse()
    orch = stall_orchestrator([running(100), FINISHED])
    orch.hold_deadline_provider = lambda: 900.0  # 100s in the PAST

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=15,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["dispatch_status"] == "completed_detached"
    assert result["exit_code"] == 0
    assert clock.t - 1000.0 <= 17, "the floor is the early-poll budget, not a window"


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

    assert result["handoff_reason"] == "stalled"
    assert_the_handoff_killed_nothing(orch)
    assert result["dispatch"]["pid"] == 4242
    assert result["dispatch"]["log_path"] == "/tmp/sag_jobs/stall.log"


# Fence 6b: the other two handoff branches must not kill either — "no kill
# semantics anywhere" is a claim about every exit, not just the stall one.
def test_the_window_and_wall_clock_handoffs_never_kill_either():
    clock = Fuse()
    windowed = stall_orchestrator(
        [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(60)]
    )
    result = windowed.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert result["handoff_reason"] == "window"
    assert_the_handoff_killed_nothing(windowed)

    clock = Fuse()
    reserved = stall_orchestrator(
        [running(100 + i, fresh=True, epoch=1754500000 + i) for i in range(60)]
    )
    reserved.hold_deadline_provider = lambda: 2400.0
    result = reserved.execute_command_with_soft_timeout(
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
    assert_the_handoff_killed_nothing(reserved)


# Fence 6c (spec §6): the config field is what the hold actually reads. Every
# other fence names stall_seconds explicitly, so deleting the config read would
# turn the stall clock off in production while the suite stayed green.
def test_the_stall_window_comes_from_config_when_the_caller_names_none():
    clock = Fuse()
    orch = stall_orchestrator([running(100)])
    orch.config = Config(
        dispatch_stall_seconds=120,
        dispatch_soft_timeout_seconds=900,
        dispatch_poll_interval_seconds=30,
    )

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result["handoff_reason"] == "stalled"
    assert 120 <= clock.t - 1000.0 <= 150  # the configured window, not the 900s one


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


# Fence 8 (spec §8.8 / §5): observations, never the conclusion "hung". The
# NUMBERS are part of the claim — an observation that reports the wrong second
# or the wrong byte count is not an observation.
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

    # Poll #1 lands at t=1002 and grows the log to 100 bytes — the last progress
    # there is. The stall fires 600s later, at t=1602: held 602s, quiet 600s.
    out = result["output"]
    assert "handed off after 602s" in out
    assert "no observable progress for 600s" in out
    assert "stdout last grew 600s ago (log size 100 bytes)" in out
    assert "no build-tree writes observed since dispatch" in out
    assert "NOT killed" in out
    assert "do NOT start the same build again" in out
    assert "hung" not in out.lower()


# Fence 8b: the build-tree signal reports its OWN last observation, not the
# stdout one — the two signals are timed separately.
def test_stall_handoff_text_dates_each_signal_separately():
    clock = Fuse()
    # Poll #1 (t=1002) grows stdout only; poll #2 (t=1007) sees a tree write;
    # everything after is quiet, so the stall fires at 1007 + 600 = 1607.
    polls = [running(100), running(100, fresh=True, epoch=1754500000), running(100)]
    orch = stall_orchestrator(polls)

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=3600,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    out = result["output"]
    assert result["handoff_reason"] == "stalled"
    assert "handed off after 607s" in out
    assert "no observable progress for 600s" in out
    assert "stdout last grew 605s ago (log size 100 bytes)" in out
    assert "last build-tree write observed 600s ago" in out


# Fence 8c: a stall handoff whose LAST probe came back inconclusive still
# carries its observations — the reason travels in the result and the tool
# metadata, so the text may not silently become the generic liveness message.
def test_a_stall_handoff_states_its_observations_even_when_liveness_is_unknown():
    clock = Fuse()
    orch = final_probe_orchestrator(running(100), UNANSWERED)

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
    assert result["handoff_reason"] == "stalled"
    assert result["dispatch_status"] == "liveness_unknown_detached"
    assert "liveness could not be established" in out
    assert "no observable progress for 600s" in out.lower()
    assert "stdout last grew 600s ago (log size 100 bytes)" in out
    assert "hung" not in out.lower()


# Fence 8d: a probe that did not answer is not an observation of quiet. The
# handoff may not report a log size it never measured.
def test_unanswered_probes_are_reported_as_unanswered_not_as_a_measured_log():
    clock = Fuse()
    # Every in-loop probe fails; the final one answers: the process is alive
    # and its log is 900_000 bytes — nothing the failed probes could have seen.
    orch = final_probe_orchestrator(UNANSWERED, running(900000, fresh=True, epoch=1754500000))

    result = orch.execute_command_with_soft_timeout(
        "mvn test",
        workdir="/workspace/proj",
        soft_timeout=3600,
        poll_interval=30,
        stall_seconds=600,
        now=clock.now,
        sleep=clock.sleep,
    )

    out = result["output"]
    assert result["handoff_reason"] == "stalled"
    assert "did not answer" in out
    assert "could not be observed for 600s" in out
    assert "log size 0 bytes" not in out, "a log we never read has no size"
    assert "stdout last grew" not in out


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

    # The third completion path: the command finishes exactly as the hold ends,
    # so the result is collected by the POST-loop probe, not by the in-loop
    # return. Comparing only the first two compares one statement with itself.
    final_clock = Fuse()
    last = final_probe_orchestrator(running(100), FINISHED)
    at_the_end = last.execute_command_with_soft_timeout(
        "mvn install -DskipTests",
        workdir="/workspace/proj",
        soft_timeout=900,
        poll_interval=100,
        hold="progress",
        stall_seconds=600,
        now=final_clock.now,
        sleep=final_clock.sleep,
    )

    assert sorted(within.keys()) == sorted(held.keys()) == sorted(at_the_end.keys())
    for key in ("success", "exit_code", "dispatch_status", "output"):
        assert within[key] == held[key] == at_the_end[key]


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


from sag.tools.internal.build_utils import detached_handoff_tool_result


def test_handoff_tool_result_carries_the_reason():
    raw = {
        "output": "handed off",
        "dispatch_status": "running_detached",
        "runner_dispatched": True,
        "handoff_reason": "stalled",
        "dispatch": {
            "job_id": "j123",
            "pid": 4242,
            "log_path": "/tmp/sag_jobs/j123.log",
            "exit_code_path": "/tmp/sag_jobs/j123.log.exit",
            "pid_path": "/tmp/sag_jobs/j123.log.pid",
            "soft_timeout": 900,
        },
    }
    result = detached_handoff_tool_result("maven", "mvn test", raw)
    assert result.metadata["handoff_reason"] == "stalled"


class _HoldRecordingOrchestrator:
    """Records what the build tools hand to the dispatch path.

    Answers the tools' executable/wrapper probes the way
    tests/test_dispatch_and_poll.py's RoutingOrchestrator does, so the argv
    the tier is computed from is the real resolved one.
    """

    def __init__(self):
        self.dispatches = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
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

    def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
        self.dispatches.append({"command": command, **kwargs})
        return {
            "success": True,
            "runner_dispatched": True,
            "exit_code": None,
            "output": "still running; poll /tmp/sag_jobs/abc.log",
            "termination_reason": None,
            "dispatch_status": "running_detached",
            "handoff_reason": "stalled",
            "dispatch": {
                "job_id": "abc",
                "pid": 1,
                "log_path": "/tmp/sag_jobs/abc.log",
                "exit_code_path": "/tmp/sag_jobs/abc.log.exit",
            },
        }


def test_hold_policy_reads_a_resolved_launcher_path():
    """What the tools dispatch is a resolved path, not a bare `mvn`. Reading
    that path as a goal would make every real build unclassifiable."""
    assert dispatch_hold_policy("maven", "/usr/bin/mvn clean compile") == "progress"
    assert dispatch_hold_policy("maven", "/usr/bin/mvn test") == "windowed"
    assert dispatch_hold_policy("gradle", "/opt/gradle/bin/gradle assemble") == "progress"
    assert dispatch_hold_policy("gradle", "/opt/gradle/bin/gradle build") == "windowed"


def test_maven_passes_its_hold_tier_to_the_dispatch():
    from sag.tools.internal.maven_tool import MavenTool

    orch = _HoldRecordingOrchestrator()
    MavenTool(orch).execute(command="clean compile", working_directory="/workspace/p")
    MavenTool(orch).execute(command="test", working_directory="/workspace/p")

    compiled, tested = orch.dispatches
    assert compiled["hold"] == dispatch_hold_policy("maven", compiled["command"]) == "progress"
    assert tested["hold"] == dispatch_hold_policy("maven", tested["command"]) == "windowed"


def test_gradle_passes_its_hold_tier_to_the_dispatch():
    from sag.tools.internal.gradle_tool import GradleTool

    orch = _HoldRecordingOrchestrator()
    GradleTool(orch).execute(tasks="assemble", working_directory="/workspace/p")
    GradleTool(orch).execute(tasks="build", working_directory="/workspace/p")

    assembled, built = orch.dispatches
    assert assembled["hold"] == dispatch_hold_policy("gradle", assembled["command"]) == "progress"
    assert built["hold"] == dispatch_hold_policy("gradle", built["command"]) == "windowed"
