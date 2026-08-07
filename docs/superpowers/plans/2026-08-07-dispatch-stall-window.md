# Dispatch Stall Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 900s dispatch hold with a stall window — hold while the build shows progress, hand off (never kill) when it stalls — per spec `docs/superpowers/specs/2026-08-06-dispatch-stall-window-design.md`.

**Architecture:** The poll probe grows two progress signals (stdout growth, build-tree writes). `execute_command_with_soft_timeout` gets a stall clock and two hold tiers (prerequisite = progress-bounded, test = windowed). The engine exposes its ONE wall-clock margin computation (`_hold_deadline`) to the orchestrator through an installed provider; `_await_open_obligations` consumes the same helper (P3).

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

## Global Constraints

- **No kill semantics anywhere** — every hold ends in a handoff or a collected completion (spec §4.1).
- **A missing budget basis degrades to the bounded old behavior, never to an unbounded hold** (spec §4.2, P2).
- **One deadline computation** — the dispatch hold and the evidence-close wait must consume the same helper (spec §4.2, P3).
- **Trusted-marker rule**: only probe output before `---TAIL---` carries markers (build output in the tail can contain anything).
- **Unclassifiable dispatches hold LESS, never more** — default tier is `windowed` (spec §3).
- **No real sleeps in tests** — inject `now`/`sleep`; clocks carry a fuse that raises instead of hanging when a mutation removes an exit condition (the M9 lesson).
- **Handoff text states observations, never the conclusion "hung"** (spec §5).
- Config default `dispatch_stall_seconds=600`, env `SAG_DISPATCH_STALL_SECONDS`, `0` disables the stall clock.
- Commit messages: lowercase `feat:`/`fix:`/`test:`/`docs:` prefix, single line, **no Co-Authored-By trailer**.
- `docs/` is gitignored: plan/spec commits need `git add -f`. Source and tests add normally.
- After each task: `python -m pytest tests/ -x -q` must be green before committing.

---

### Task 1: Config field `dispatch_stall_seconds`

**Files:**
- Modify: `src/sag/config/settings.py:103` (field block) and `:174` (env mapping)
- Test: `tests/test_dispatch_stall_window.py` (new file)

**Interfaces:**
- Produces: `config.dispatch_stall_seconds: int` (default 600), consumed by Task 5.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch_stall_window.py`:

```python
"""Dispatch stall window (spec 2026-08-06): hold while it progresses,
hand off when it stalls. Time is always injected — never a real sleep."""

import pytest

from sag.config.settings import SAGConfig


def test_stall_window_config_defaults_to_600():
    assert SAGConfig().dispatch_stall_seconds == 600


def test_stall_window_config_reads_env(monkeypatch):
    monkeypatch.setenv("SAG_DISPATCH_STALL_SECONDS", "120")
    assert SAGConfig.from_env().dispatch_stall_seconds == 120
```

Note: check the actual config class name and env-constructor name at the top
of `src/sag/config/settings.py` (the class holding
`dispatch_soft_timeout_seconds` at line 103 and the `os.getenv` block at line
174) and use those exact names in both tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v`
Expected: FAIL — `dispatch_stall_seconds` attribute does not exist.

- [ ] **Step 3: Implement**

In `src/sag/config/settings.py`, directly under
`dispatch_poll_interval_seconds: int = Field(default=15)` (line 104), add:

```python
    # Stall window (spec 2026-08-06): hand off a held dispatch after this many
    # seconds without observable progress (stdout growth or build-tree writes).
    # 0 disables the stall clock — fixed-window behavior only.
    dispatch_stall_seconds: int = Field(default=600)
```

In the env-constructor block (after the `dispatch_poll_interval_seconds` entry
around line 177-179), add:

```python
            dispatch_stall_seconds=int(os.getenv("SAG_DISPATCH_STALL_SECONDS", "600")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/config/settings.py tests/test_dispatch_stall_window.py
git commit -m "feat: dispatch_stall_seconds config — the stall window's one knob"
```

---

### Task 2: `dispatch_hold_policy` classifier

**Files:**
- Modify: `src/sag/tools/internal/build_utils.py` (append after `detached_poll_ref`, line ~31)
- Test: `tests/test_dispatch_stall_window.py`

**Interfaces:**
- Produces: `dispatch_hold_policy(system: str, argv: str) -> str` returning `"progress"` or `"windowed"`. Consumed by Task 6 (maven/gradle tools) and honored by Task 5 (`hold=` parameter).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatch_stall_window.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v -k hold_policy`
Expected: FAIL — ImportError, `dispatch_hold_policy` not defined.

- [ ] **Step 3: Implement**

Append to `src/sag/tools/internal/build_utils.py` (after `detached_poll_ref`):

```python
_HOLD_LAUNCHERS = frozenset({"mvn", "./mvnw", "mvnw", "gradle", "./gradlew", "gradlew"})
_MAVEN_SKIP_FLAGS = ("-DskipTests", "-Dmaven.test.skip")
# Goals that provably run no tests. Anything else — including plugin goals we
# have never seen — stays on the bounded window: refusing to guess must hold
# LESS, never more (spec §3).
_MAVEN_SAFE_GOALS = frozenset(
    {
        "clean",
        "validate",
        "initialize",
        "generate-sources",
        "process-resources",
        "compile",
        "process-test-resources",
        "test-compile",
        "dependency:resolve",
        "dependency:go-offline",
        "dependency:tree",
    }
)
_GRADLE_SAFE_TASKS = frozenset(
    {
        "clean",
        "classes",
        "testClasses",
        "compileJava",
        "compileTestJava",
        "processResources",
        "assemble",
        "jar",
        "dependencies",
    }
)
_GRADLE_EXCLUDE_FLAGS = ("-x", "--exclude-task")


def dispatch_hold_policy(system: str, argv: str) -> str:
    """'progress' only when the argv provably runs no tests; else 'windowed'.

    'progress' marks a PREREQUISITE dispatch (spec §3): nothing downstream can
    proceed without it, so the harness holds while it shows progress. Every
    test-running or unclassifiable dispatch keeps the bounded window — the
    obligation/settlement path accounts for those after a handoff.
    """
    text = str(argv or "")
    if system == "maven":
        if any(flag in text for flag in _MAVEN_SKIP_FLAGS):
            return "progress"
        goals = [
            token
            for token in text.split()
            if token not in _HOLD_LAUNCHERS and not token.startswith("-")
        ]
        if goals and all(goal in _MAVEN_SAFE_GOALS for goal in goals):
            return "progress"
        return "windowed"
    if system == "gradle":
        excluded_tests = any(
            f"{flag} test" in text or f"{flag} check" in text
            for flag in _GRADLE_EXCLUDE_FLAGS
        )
        tasks = []
        skip_next = False
        for token in text.split():
            if skip_next:
                skip_next = False
                continue
            if token in _GRADLE_EXCLUDE_FLAGS:
                skip_next = True
                continue
            if token in _HOLD_LAUNCHERS or token.startswith("-"):
                continue
            tasks.append(token)
        safe = _GRADLE_SAFE_TASKS | ({"build"} if excluded_tests else frozenset())
        if tasks and all(task in safe for task in tasks):
            return "progress"
        return "windowed"
    return "windowed"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/build_utils.py tests/test_dispatch_stall_window.py
git commit -m "feat: dispatch_hold_policy — progress tier only for provably test-free argv"
```

---

### Task 3: Progress probe in `poll_detached_command`

**Files:**
- Modify: `src/sag/docker_orch/orch.py:1039` (`poll_detached_command`)
- Test: `tests/test_dispatch_stall_window.py`

**Interfaces:**
- Consumes: existing probe transport (STATE:/SIZE:/---TAIL--- shell probe).
- Produces: two new keyword params `progress_workdir: Optional[str] = None`, `progress_since: Optional[int] = None`; two new result keys `now_epoch: Optional[int]` (container clock, always emitted) and `progress_fresh: Optional[bool]` (`True` = a build-tree file is newer than `progress_since`; `False` = probed, nothing newer; `None` = not probed). Consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatch_stall_window.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v -k probe`
Expected: FAIL — unexpected keyword `progress_workdir` / missing keys.

- [ ] **Step 3: Implement**

In `src/sag/docker_orch/orch.py`, change the signature at line 1039:

```python
    def poll_detached_command(
        self,
        handle: Dict[str, Any],
        tail_lines: int = 40,
        progress_workdir: Optional[str] = None,
        progress_since: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Poll a detached command: completion state, exit code, and log tail.

        With ``progress_workdir`` + ``progress_since`` the probe also answers
        (spec 2026-08-06 §2, S2): has ANY file under the workdir's build-output
        subtrees been written since ``progress_since`` (container clock, 1s
        slack)? ``NOW:`` always carries the container clock so the caller's
        next ``progress_since`` never mixes host and container time.
        """
```

Build the probe: after the existing `SIZE:` echo and before the `---TAIL---`
echo, insert into the probe string:

```python
        progress_probe = 'echo "NOW:$(date +%s)"; '
        if progress_workdir and progress_since is not None:
            quoted_dir = shlex.quote(progress_workdir)
            progress_probe += (
                f"if [ -d {quoted_dir} ]; then "
                f"fresh=$(find {quoted_dir} "
                f"\\( -path '*/target/*' -o -path '*/build/*' "
                f"-o -path '*/.setup_agent/pytest-reports/*' \\) "
                f"-type f -newermt @{int(progress_since) - 1} -print -quit 2>/dev/null); "
                f'if [ -n "$fresh" ]; then echo "PROGRESS:FRESH"; '
                f'else echo "PROGRESS:NONE"; fi; fi; '
            )
```

and splice `progress_probe` into the existing `probe` assembly between the
`SIZE:` echo and `echo "---TAIL---"`.

In the head-parsing loop add:

```python
            elif stripped.startswith("NOW:"):
                try:
                    now_epoch = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    now_epoch = None
            elif stripped == "PROGRESS:FRESH":
                progress_fresh = True
            elif stripped == "PROGRESS:NONE":
                progress_fresh = False
```

initializing `now_epoch = None` and `progress_fresh = None` before the loop,
and add both to the returned dict:

```python
            "now_epoch": now_epoch,
            "progress_fresh": progress_fresh,
```

- [ ] **Step 4: Run tests + the existing dispatch suite**

Run: `python -m pytest tests/test_dispatch_stall_window.py tests/test_dispatch_and_poll.py -v`
Expected: all PASS (existing polls omit the new params — behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/sag/docker_orch/orch.py tests/test_dispatch_stall_window.py
git commit -m "feat: poll probe carries container clock and build-tree progress (S2)"
```

---

### Task 4: One deadline computation — `_hold_deadline` + provider install

**Files:**
- Modify: `src/sag/agent/react_engine.py:996` (`_await_open_obligations`) and `:2403` (run-loop stash)
- Test: `tests/test_dispatch_stall_window.py`; keep `tests/test_pre_close_wait.py` green

**Interfaces:**
- Produces: `ReactEngine._hold_deadline(self) -> Optional[float]` (absolute epoch; None = margin unknown), `ReactEngine._install_hold_deadline_provider(self) -> None` (sets `orchestrator.hold_deadline_provider` to the bound `_hold_deadline`). Consumed by Task 5 via `getattr(self, "hold_deadline_provider", None)` on the orchestrator.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatch_stall_window.py`:

```python
from types import SimpleNamespace

from sag.agent.react_engine import ReactEngine


class _EngineStub:
    _REPORT_RESERVE_SECONDS = ReactEngine._REPORT_RESERVE_SECONDS
    _hold_deadline = ReactEngine._hold_deadline
    _install_hold_deadline_provider = ReactEngine._install_hold_deadline_provider


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
    assert provider.__func__ is ReactEngine._hold_deadline


def test_the_close_wait_consults_the_shared_deadline():
    """Wiring pin: _await_open_obligations must read _hold_deadline, not run a
    second lookalike computation. A patched helper returning an expired
    deadline must stop the wait before any container poll."""
    calls = []

    class Stub(_EngineStub):
        _await_open_obligations = ReactEngine._await_open_obligations

    stub = Stub()
    stub._run_started_at = 1000.0
    stub._wall_clock_cap = 7200
    stub._hold_deadline = lambda: 0.0  # expired: no margin at all
    stub.orchestrator = SimpleNamespace(
        execute_command=lambda *a, **k: calls.append(a) or {"exit_code": 0, "output": ""}
    )
    from sag.agent.react_engine import EvidenceCloseReason

    stub._await_open_obligations(
        EvidenceCloseReason.WALL_CLOCK, now=lambda: 5000.0, sleep=lambda s: None
    )
    assert calls == []  # deadline already passed: the wait never polled
```

Note: check the real member name on `EvidenceCloseReason` (open
`src/sag/agent/react_engine.py` and use any non-ABORTED, non-CANCELLED member
that exists — e.g. the one `_finalize_evidence` passes for a working close).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v -k deadline`
Expected: FAIL — `_hold_deadline` does not exist.

- [ ] **Step 3: Implement**

In `src/sag/agent/react_engine.py`, directly above `_await_open_obligations`
(line ~996), add two methods:

```python
    def _hold_deadline(self) -> Optional[float]:
        """When must any hold stop — ONE computation (P3, spec §4.2).

        Consumed by the evidence-close wait below and, through the provider
        installed at run start, by the orchestrator's dispatch hold. None
        means the margin is unknown — and margin unknown is margin absent:
        every consumer degrades to its bounded behavior, never to an
        unbounded hold.
        """
        started_at = getattr(self, "_run_started_at", None)
        if started_at is None:
            return None
        cap = getattr(self, "_wall_clock_cap", None) or getattr(
            getattr(self, "config", None), "max_wall_clock_seconds", 7200
        )
        return float(started_at) + float(cap) - self._REPORT_RESERVE_SECONDS

    def _install_hold_deadline_provider(self) -> None:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is not None:
            orchestrator.hold_deadline_provider = self._hold_deadline
```

In `_await_open_obligations`, replace this block:

```python
        started_at = getattr(self, "_run_started_at", None)
        if started_at is None:
            return  # margin unknown is margin absent
        cap = getattr(self, "_wall_clock_cap", None) or getattr(
            getattr(self, "config", None), "max_wall_clock_seconds", 7200
        )
        deadline = float(started_at) + float(cap) - self._REPORT_RESERVE_SECONDS
```

with:

```python
        deadline = self._hold_deadline()
        if deadline is None:
            return  # margin unknown is margin absent
```

In `run_react_loop` (line ~2407), directly after:

```python
        self._run_started_at = run_started_at
        self._wall_clock_cap = wall_clock_cap
```

add:

```python
        # The dispatch hold (stall window) must stop at the same line the
        # evidence-close wait stops at; both consume _hold_deadline (P3).
        self._install_hold_deadline_provider()
```

- [ ] **Step 4: Run the new tests AND the wait-policy suite**

Run: `python -m pytest tests/test_dispatch_stall_window.py tests/test_pre_close_wait.py -v`
Expected: all PASS. If a `test_pre_close_wait.py` stub fails with
`AttributeError: _hold_deadline`, add
`_hold_deadline = ReactEngine._hold_deadline` to that stub with a one-line
comment (`# the wait now consumes the shared deadline helper (P3)`), and
nothing else — the tests' assertions must not change.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/react_engine.py tests/test_dispatch_stall_window.py tests/test_pre_close_wait.py
git commit -m "feat: one hold-deadline computation, installed as the orchestrator's provider"
```

---

### Task 5: The stall-window loop in `execute_command_with_soft_timeout`

**Files:**
- Modify: `src/sag/docker_orch/orch.py:1114`
- Test: `tests/test_dispatch_stall_window.py`

**Interfaces:**
- Consumes: Task 3's poll keys, Task 4's `hold_deadline_provider`, Task 1's config.
- Produces: keyword-only params `hold: str = "windowed"`, `stall_seconds: Optional[int] = None`, `now=None`, `sleep=None`; handoff results gain `handoff_reason: "stalled"|"window"|"wall_clock"` (top level AND inside `dispatch`). Positional signature `(command, workdir, environment, soft_timeout, poll_interval, tail_lines)` is unchanged.

- [ ] **Step 1: Write the failing fences**

Append to `tests/test_dispatch_stall_window.py`:

```python
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
```

- [ ] **Step 2: Run fences to verify they fail**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v`
Expected: every fence FAILS (unexpected keyword `hold` / missing
`handoff_reason`); Tasks 1–4 tests still PASS.

- [ ] **Step 3: Implement**

Rewrite `execute_command_with_soft_timeout` (orch.py:1114). Signature — the
six existing positionals unchanged, new params keyword-only:

```python
    def execute_command_with_soft_timeout(
        self,
        command: str,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        soft_timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
        tail_lines: int = 40,
        *,
        hold: str = "windowed",
        stall_seconds: Optional[int] = None,
        now=None,
        sleep=None,
    ) -> Dict[str, Any]:
```

Keep the existing docstring and extend it with:

```
        Stall window (spec 2026-08-06): progress on either signal — stdout
        growth or a build-tree write — resets a stall clock; when it reaches
        ``stall_seconds`` the command is handed off, never killed.
        ``hold="progress"`` (prerequisite dispatches) drops the total window
        and holds while progress continues, bounded ONLY by the engine's
        installed ``hold_deadline_provider``; without that provider the total
        window applies — a missing budget basis degrades to the bounded old
        behavior, never to an unbounded hold. ``hold="windowed"`` (default:
        test-running and unclassifiable dispatches) keeps the total window
        and hands off at min(stall, window).
```

Body:

```python
        import time as _time

        now = now or _time.time
        sleep = sleep or _time.sleep
        config = getattr(self, "config", None)
        if soft_timeout is None:
            soft_timeout = getattr(config, "dispatch_soft_timeout_seconds", 900) or 900
        if poll_interval is None:
            poll_interval = getattr(config, "dispatch_poll_interval_seconds", 15) or 15
        if stall_seconds is None:
            stall_seconds = getattr(config, "dispatch_stall_seconds", 600)
        stall_seconds = max(0, int(stall_seconds or 0))

        handle = self.execute_command_detached(command, workdir=workdir, environment=environment)
        if not handle.get("started"):
            return {
                "success": False,
                "exit_code": 1,
                "output": f"Failed to dispatch command: {handle.get('launch_output', '')}",
                "termination_reason": None,
                "dispatch_status": "dispatch_failed",
                "runner_dispatched": False,
                "dispatch": handle,
            }

        provider = getattr(self, "hold_deadline_provider", None)
        wall_deadline: Optional[float] = None
        if callable(provider):
            try:
                raw = provider()
                wall_deadline = float(raw) if raw is not None else None
            except Exception:
                wall_deadline = None

        start = now()
        # Spec §3/§4.2: the total window drops ONLY when a stall clock AND a
        # wall-clock budget both exist (P2: a missing basis is not permission).
        unbounded = hold == "progress" and stall_seconds > 0 and wall_deadline is not None
        window_deadline = None if unbounded else start + max(1, int(soft_timeout))

        last_progress = start
        last_stdout_growth = start
        last_tree_write: Optional[float] = None
        max_log_size = 0
        since_epoch: Optional[int] = None

        def _next_deadline() -> float:
            candidates = []
            if window_deadline is not None:
                candidates.append(window_deadline)
            if wall_deadline is not None:
                candidates.append(wall_deadline)
            if stall_seconds > 0:
                candidates.append(last_progress + stall_seconds)
            return min(candidates)

        def _poll(with_progress: bool) -> Dict[str, Any]:
            probe_workdir = workdir if (with_progress and stall_seconds > 0) else None
            try:
                return self.poll_detached_command(
                    handle,
                    tail_lines=tail_lines,
                    progress_workdir=probe_workdir,
                    progress_since=since_epoch if with_progress else None,
                )
            except TypeError as exc:
                # Small test orchestrators predate the progress params.
                if "progress_workdir" not in str(exc):
                    raise
                return self.poll_detached_command(handle, tail_lines=tail_lines)

        delays = [2, 5, 10]
        poll_count = 0
        while True:
            ts = now()
            if ts >= _next_deadline():
                break
            delay = delays[poll_count] if poll_count < len(delays) else poll_interval
            sleep(max(0.05, min(delay, _next_deadline() - ts)))
            poll_count += 1
            poll = _poll(with_progress=True)
            if self._detached_poll_state(poll) in {"finished", "vanished"}:
                return self.collect_detached_result(handle, poll)
            ts = now()
            size = int(poll.get("log_size") or 0)
            if size > max_log_size:
                max_log_size = size
                last_stdout_growth = ts
                last_progress = ts
            if poll.get("progress_fresh"):
                last_tree_write = ts
                last_progress = ts
            if poll.get("now_epoch"):
                since_epoch = int(poll["now_epoch"])

        final_poll = _poll(with_progress=False)
        final_state = self._detached_poll_state(final_poll)
        if final_state in {"finished", "vanished"}:
            return self.collect_detached_result(handle, final_poll)

        ts = now()
        if stall_seconds > 0 and ts >= last_progress + stall_seconds:
            handoff_reason = "stalled"
        elif window_deadline is not None and ts >= window_deadline:
            handoff_reason = "window"
        else:
            handoff_reason = "wall_clock"

        held = int(ts - start)
        liveness_unknown = final_state == "unknown"
        dispatch_status = "liveness_unknown_detached" if liveness_unknown else "running_detached"
        if liveness_unknown:
            logger.warning(
                f"Hold ended ({handoff_reason}) without a conclusive liveness "
                f"probe; preserving detached command handle (pid {handle['pid']}, "
                f"log {handle['log_path']})"
            )
            handoff_summary = (
                "Command liveness could not be established when the hold ended. "
                "Its detached handle was preserved and the operation remains pending."
            )
        elif handoff_reason == "stalled":
            quiet = int(ts - last_progress)
            stdout_ago = int(ts - last_stdout_growth)
            tree_line = (
                f"last build-tree write observed {int(ts - last_tree_write)}s ago"
                if last_tree_write is not None
                else "no build-tree writes observed since dispatch"
            )
            logger.info(
                f"⏳ Stall window ({stall_seconds}s) reached after {held}s; handing off "
                f"still-running command (pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command handed off after {held}s: no observable progress for "
                f"{quiet}s — it was left running in the background (NOT killed).\n"
                f"Observations: stdout last grew {stdout_ago}s ago "
                f"(log size {max_log_size} bytes); {tree_line}."
            )
        elif handoff_reason == "wall_clock":
            logger.info(
                f"⏳ Report reserve reached after {held}s of holding; handing off "
                f"still-running command (pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command still running after {held}s — the run's report reserve "
                "was reached, so the harness stopped holding. It was left running "
                "in the background (NOT killed)."
            )
        else:
            logger.info(
                f"⏳ Soft window of {soft_timeout}s expired; handing off still-running command "
                f"(pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command still running after the {soft_timeout}s soft window — it was left "
                "running in the background (NOT killed)."
            )
        handoff_output = (
            f"{handoff_summary}\n"
            f"Background job: pid {handle['pid']}, log file {handle['log_path']}\n"
            f"Last output:\n{final_poll.get('tail') or '(no output yet)'}\n\n"
            f"NEXT STEPS — poll the log instead of re-running the command:\n"
            f"  1. Progress: bash(command=\"tail -n 50 {handle['log_path']}\")\n"
            f"  2. Completion: bash(command=\"cat {handle['exit_code_path']} 2>/dev/null || echo STILL_RUNNING\") "
            f"— prints the exit code once the command finishes\n"
            f"  3. Do other useful work between polls; do NOT start the same build again."
        )
        return {
            "success": True,
            "exit_code": None,
            "output": handoff_output,
            "termination_reason": None,
            "dispatch_status": dispatch_status,
            "runner_dispatched": True,
            "lifecycle_state": "pending",
            "liveness_state": final_state,
            "handoff_reason": handoff_reason,
            "dispatch": {
                **handle,
                "last_tail": final_poll.get("tail", ""),
                "log_size": final_poll.get("log_size", 0),
                "soft_timeout": soft_timeout,
                "handoff_reason": handoff_reason,
            },
        }
```

Note for the completion paths: `collect_detached_result` is shared by the
within-window and the held path — fence 9 passes by construction. Do not add
any held-only key to a completed result.

- [ ] **Step 4: Run the full dispatch suites**

Run: `python -m pytest tests/test_dispatch_stall_window.py tests/test_dispatch_and_poll.py tests/test_job_obligations.py tests/test_receipt_proven_structure.py -v`
Expected: all PASS. The pre-existing handoff tests now see
`handoff_reason == "window"` — their assertions don't check it, so they stay
green unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/sag/docker_orch/orch.py tests/test_dispatch_stall_window.py
git commit -m "feat: stall-window hold — progress keeps holding, stalling hands off, nothing kills"
```

---

### Task 6: Tool plumbing — maven/gradle pass their tier; handoff metadata carries the reason

**Files:**
- Modify: `src/sag/tools/internal/maven_tool.py:32` (import), `:502` (call site)
- Modify: `src/sag/tools/internal/gradle_tool.py:354` (call site + matching import)
- Modify: `src/sag/tools/internal/build_utils.py:34` (`detached_handoff_tool_result` metadata)
- Test: `tests/test_dispatch_stall_window.py`

**Interfaces:**
- Consumes: `dispatch_hold_policy` (Task 2), `hold=` parameter (Task 5).
- Produces: `ToolResult.metadata["handoff_reason"]` on handoffs. `bash.py` is deliberately untouched: it passes its own `soft_timeout` and stays windowed — an arbitrary shell command is unclassifiable by definition.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatch_stall_window.py`:

```python
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


def test_maven_passes_its_hold_tier_to_the_dispatch(monkeypatch):
    """The maven tool must hand the classifier's verdict to the orchestrator.
    Import MavenTool, build it around a recording orchestrator whose
    execute_command_with_soft_timeout captures kwargs and returns FINISHED-
    shaped data, then run a compile action and assert the recorded
    hold == "progress" and a test action records hold == "windowed"."""
    from sag.tools.internal.maven_tool import MavenTool  # noqa: F401
    # Follow the existing MavenTool construction pattern used in
    # tests/test_dispatch_and_poll.py / tests/test_job_obligations.py for the
    # orchestrator double; assert on the captured `hold` kwarg for
    # "mvn clean compile" (progress) vs "mvn test" (windowed).
```

Write the second test fully against the real `MavenTool` constructor — open
`tests/test_job_obligations.py` for the smallest working MavenTool double
setup and copy its shape. The assertion that matters: the captured
`hold` kwarg equals `dispatch_hold_policy("maven", <the same argv>)` for one
progress case and one windowed case. If constructing MavenTool inline proves
heavier than one screen of setup, assert instead at the `_run_build` seam by
extracting the call into a small helper — but the fence must fail when the
`hold=` argument is dropped from the call site (that is its mutation).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dispatch_stall_window.py -v -k "handoff_tool_result or maven_passes"`
Expected: FAIL — `handoff_reason` missing from metadata; `hold` never captured.

- [ ] **Step 3: Implement**

`build_utils.py`, in `detached_handoff_tool_result`'s metadata dict (after
`"soft_timeout": dispatch.get("soft_timeout"),`), add:

```python
            "handoff_reason": result.get("handoff_reason"),
```

`maven_tool.py` — extend the existing build_utils import (lines 31-36) with
`dispatch_hold_policy`, then replace the soft-timeout call at line ~502:

```python
                    try:
                        return self.orchestrator.execute_command_with_soft_timeout(
                            maven_cmd,
                            workdir=working_directory,
                            hold=dispatch_hold_policy("maven", maven_cmd),
                        )
                    except TypeError as exc:
                        # Small test orchestrators predate the hold parameter.
                        if "hold" not in str(exc):
                            raise
                        return self.orchestrator.execute_command_with_soft_timeout(
                            maven_cmd,
                            workdir=working_directory,
                        )
```

`gradle_tool.py` — same shape at line ~354 with
`hold=dispatch_hold_policy("gradle", gradle_cmd)` (add the import next to the
existing build_utils imports in that file).

- [ ] **Step 4: Run the tool suites**

Run: `python -m pytest tests/test_dispatch_stall_window.py tests/test_dispatch_and_poll.py tests/test_job_obligations.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest tests/ -q`
Expected: green (same skip count as main).

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/internal/maven_tool.py src/sag/tools/internal/gradle_tool.py src/sag/tools/internal/build_utils.py tests/test_dispatch_stall_window.py
git commit -m "feat: build tools declare their hold tier; handoffs carry their reason"
```

---

### Task 7: Mutation pass — every fence red under exactly its mutation

Each mutation is applied by hand, the named test must go RED, then the
mutation is reverted (`git checkout -- <file>`). Record each result in the
commit message body of the final commit. No mutation may hang a test — the
Fuse clock raises instead (verify this on M4 especially).

- [ ] **M1 — drop S2**: in Task 5's loop, delete the `if poll.get("progress_fresh"):` block → `test_growing_tree_holds_past_the_total_window` red.
- [ ] **M2 — stall clock never fires**: remove `candidates.append(last_progress + stall_seconds)` → `test_fully_stalled_hands_off_at_the_stall_window` red (reason becomes "window" at 900).
- [ ] **M3 — no reset**: in the S1/S2 blocks, stop updating `last_progress` → `test_progress_resets_the_stall_clock` red.
- [ ] **M4 — default unbounded**: change `unbounded = ...` to drop the `wall_deadline is not None` conjunct → `test_progress_tier_without_budget_stays_windowed` red **via the Fuse raising, not a hang**.
- [ ] **M5 — second deadline computation**: in `_await_open_obligations`, reinstate the inline `started_at/cap` computation instead of `self._hold_deadline()` → `test_the_close_wait_consults_the_shared_deadline` red.
- [ ] **M6 — kill on stall**: in the stalled branch, add `self.execute_command(f"kill {handle['pid']}")` → `test_stall_handoff_never_kills` red.
- [ ] **M7 — tier collapse**: ignore `hold` and always set `window_deadline` → `test_growing_tree_holds_past_the_total_window` red; always drop it → `test_progress_tier_without_budget_stays_windowed` red.
- [ ] **M8 — conclusion wording**: replace the stalled summary with "Command appears hung" → `test_stall_handoff_text_states_observations_not_conclusions` red.
- [ ] **M9 — held-only field**: add `"held_for": held` to a COMPLETED result path (inside `collect_detached_result`'s caller branch for the held case — simulate by special-casing) → `test_held_to_completion_equals_within_window_completion` red.
- [ ] **M10 — hold argument dropped**: remove `hold=` from the maven call site → the Task 6 maven fence red.
- [ ] **Full suite + locked profiles**: `python -m pytest tests/ -q` green, then re-verify the four locked replay profiles (cli 9, bigtop 13, tvm 18, tvm 15) exactly as done for Plan 8 commits.
- [ ] **Commit** (mutation log in body):

```bash
git commit --allow-empty -m "test: stall-window mutation pass — M1-M10 each red under its own fence"
```

---

### Task 8: Live anchor (operator-gated — coordinate with the repo owner)

Per spec §8: one rerun where a prerequisite build that previously handed off
at 900s is held to completion, and one induced stall producing the §5 text.
This task needs Docker, the OPENAI key from `.env`, and the owner's go-ahead
(evidence-preservation rules apply: archive `--record` sessions into repo
`logs/` BEFORE any container is removed).

- [ ] Launch a polaris (or camel) run on this branch with `--record`; confirm in the session log a `hold="progress"` dispatch that runs past 900s without a handoff and completes (grep the main log for the dispatch and the absence of the soft-window handoff text between dispatch and completion).
- [ ] Induce a stall in a probe container (dispatch `bash -c 'sleep 1200'` through a maven-shaped argv with `-DskipTests`, stall window lowered via `SAG_DISPATCH_STALL_SECONDS=60`): confirm the handoff text carries "no observable progress for" and per-signal observations, and `docker exec` shows the process still alive afterwards.
- [ ] Archive both sessions into repo `logs/`, then grade the anchor in a short report under `docs/superpowers/reports/`.

---

## Self-review notes

- Spec coverage: §2 signals → Tasks 3+5; §3 tiers → Tasks 2+5+6; §4.1 no-kill → F6/M6; §4.2 shared deadline + degrade → Task 4 + F4/F5/M4/M5; §4.3 field-for-field → F9/M9; §5 wording → F8/M8; §6 config → Task 1; §8 fences → Tasks 5/7; live anchor → Task 8.
- Type consistency: `hold_deadline_provider` returns `Optional[float]` absolute epoch everywhere; `progress_since`/`now_epoch` are container-clock ints; `dispatch_hold_policy` returns the same two strings Task 5 branches on.
- Known judgment calls an implementer must NOT "fix" silently: bash stays windowed; unknown argv stays windowed; the first poll cycle is S1-only (`progress_since=None`); `-newermt` gets 1s slack toward extra hold.
