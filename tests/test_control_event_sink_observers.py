"""A second reader may watch the ledger without becoming part of it.

The renderer that shows a live run as turns reads the control ledger by
standing beside it, never by being asked first: the append is complete and
fsynced before any observer hears about it, and an observer that raises is
logged and dropped exactly as the container mirror is.

Three clauses here are fences against defects that no offline replay and no
plain-callable sink can see, so each says in its own docstring what it holds:

* the session sink is CACHED, so the mirror the agent supplies second has to
  reach the sink the CLI built first, or the container-side ledger -- the copy
  `--record` archives -- is silently never written;
* the CLI's write callable has to be `console.print`, not `console.file.write`,
  or every styled token reaches a real terminal as literal `[red]failed[/red]`;
* `close()` has to run from a `finally`, or a crashing run loses its last line.

Every clause this file claims was proved by deleting or inverting it in the
source and watching a named test go red: 25 edits over `ControlEventSink.emit`,
`add_observer`, `attach_mirror`, `SessionLogger.get_control_event_sink` and
`main._attach_turn_stream` and its two call sites, ranging from one keyword
(`soft_wrap=True`) to a whole block (the observer loop, the `finally`), each run
with `PYTHONDONTWRITEBYTECODE=1`. 25 of 25 were caught. Two earlier survivors
are the reason two of these tests exist at all: `soft_wrap` only bites on a
console narrower than the renderer's 60-column floor, and the observer snapshot
only bites when an observer registers another mid-delivery.
"""

import ast
import inspect
import io
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from loguru import logger as loguru_logger
from rich.console import Console

import sag.config as config_module
import sag.config.logger as logger_module
import sag.main as main_module
from sag.agent.agent import SetupAgent
from sag.agent.control_events import ControlEventSink
from sag.config import Config
from sag.config.logger import SessionLogger

FIXTURE = Path(__file__).parent / "fixtures" / "trajectory" / "kafka-d2r3" / "control_events.jsonl"

#: Every markup spelling the renderer can emit, open and close. A terminal that
#: receives any of these verbatim is a terminal the user reads tags on.
MARKUP_TAGS = re.compile(r"\[/?(?:bold|green|red|yellow|cyan|dim)\]")


def _sink(tmp_path, **kwargs) -> ControlEventSink:
    return ControlEventSink(tmp_path / "control_events.jsonl", run_id="run-1", **kwargs)


@pytest.fixture
def warnings_said():
    """Everything the run said to its user at WARNING or above, as text."""

    said: list[str] = []
    sink_id = loguru_logger.add(said.append, level="WARNING", format="{message}")
    try:
        yield said
    finally:
        loguru_logger.remove(sink_id)


def _session_logger(tmp_path, monkeypatch) -> SessionLogger:
    """A real `SessionLogger` whose `get_control_event_sink` is the real one.

    Only `_setup_loggers` is stubbed: it calls `logger.remove()`, which would
    strip loguru's handlers for the whole pytest process. Everything this file
    exercises -- the caching branch, the sink it builds, the directory it
    builds it in -- is the shipped code.
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SessionLogger, "_setup_loggers", lambda self: None)
    return SessionLogger(Config())


# --- the observer contract ------------------------------------------------


def test_an_observer_sees_every_emitted_line(tmp_path):
    seen: list[str] = []
    sink = _sink(tmp_path, observers=(seen.append,))
    sink.emit("evidence_close", {"reason": "test_terminated"})
    assert len(seen) == 1
    assert json.loads(seen[0])["kind"] == "evidence_close"


def test_an_observer_added_later_sees_later_lines(tmp_path):
    seen: list[str] = []
    sink = _sink(tmp_path)
    sink.emit("evidence_close", {"reason": "aborted"})
    sink.add_observer(seen.append)
    sink.emit("evidence_close", {"reason": "cancelled"})
    assert len(seen) == 1
    assert json.loads(seen[0])["payload"]["reason"] == "cancelled"


def test_a_failing_observer_never_breaks_the_ledger(tmp_path):
    """The append-only guarantee outranks any renderer.

    Asserts the line is ON DISK, not merely that no exception escaped: an
    observer raised inside the lock, after the write, and the sequence still
    advanced for the next caller.
    """

    def explode(_: str) -> None:
        raise RuntimeError("renderer is down")

    sink = _sink(tmp_path, observers=(explode,))
    event = sink.emit("evidence_close", {"reason": "aborted"})
    assert event.sequence == 1
    written = (tmp_path / "control_events.jsonl").read_text().strip().splitlines()
    assert len(written) == 1
    assert sink.emit("evidence_close", {"reason": "cancelled"}).sequence == 2


def test_observers_run_after_the_line_is_on_disk(tmp_path):
    path = tmp_path / "control_events.jsonl"
    observed: list[int] = []

    def count_lines(_: str) -> None:
        observed.append(len(path.read_text().strip().splitlines()))

    sink = _sink(tmp_path, observers=(count_lines,))
    sink.emit("evidence_close", {"reason": "aborted"})
    assert observed == [1]


def test_an_observer_registered_mid_delivery_starts_at_the_next_line(tmp_path):
    """`add_observer` means from here on, and `here` is a line already going out.

    `emit` delivers to a snapshot of the observers, so a renderer that attaches
    a second reader while the first is being fed does not hand the newcomer the
    line it is standing in the middle of. (It also proves the lock is
    reentrant: `add_observer` takes it from inside `emit`.)
    """

    seen: list[str] = []
    sink = _sink(tmp_path)

    def register_another(_: str) -> None:
        sink.add_observer(seen.append)

    sink.add_observer(register_another)
    sink.emit("evidence_close", {"reason": "aborted"})
    assert seen == []
    sink.emit("evidence_close", {"reason": "cancelled"})
    assert len(seen) == 1
    assert json.loads(seen[0])["payload"]["reason"] == "cancelled"


def test_an_observer_hears_the_mirror_s_line_byte_for_byte(tmp_path):
    """One line, one text. The renderer parses what the container stores."""

    mirrored: list[str] = []
    seen: list[str] = []
    sink = _sink(tmp_path, mirror=mirrored.append, observers=(seen.append,))
    sink.emit("evidence_close", {"reason": "aborted"})
    assert seen == mirrored


# --- the cached session sink ----------------------------------------------


def test_the_container_mirror_still_fires_after_a_no_argument_sink_request(tmp_path, monkeypatch):
    """The CLI asks for the sink first; the agent brings the mirror second.

    `get_control_event_sink` caches, so before this fence the agent's
    `mirror=mirror_event` was dropped on the floor and
    `/workspace/.setup_agent/control_events.jsonl` was never created for the
    rest of the run -- no exception, no warning, and only discoverable once the
    container is gone. That file is the copy `--record` archives.
    """

    session_logger = _session_logger(tmp_path, monkeypatch)
    first = session_logger.get_control_event_sink()

    mirrored: list[str] = []
    second = session_logger.get_control_event_sink(mirror=mirrored.append)

    assert second is first
    second.emit("evidence_close", {"reason": "aborted"})
    assert len(mirrored) == 1
    assert json.loads(mirrored[0])["kind"] == "evidence_close"


def test_a_second_mirror_never_displaces_the_one_already_writing(tmp_path):
    """Two writers appending to one container file would interleave.

    So `attach_mirror` fills an absence and reports whether it did; it does not
    swap a live mirror out from under the run.
    """

    first: list[str] = []
    second: list[str] = []
    sink = _sink(tmp_path, mirror=first.append)

    assert sink.attach_mirror(second.append) is False
    sink.emit("evidence_close", {"reason": "aborted"})
    assert len(first) == 1
    assert second == []


def test_attach_mirror_reports_that_it_filled_an_absence(tmp_path):
    mirrored: list[str] = []
    sink = _sink(tmp_path)
    assert sink.attach_mirror(mirrored.append) is True
    sink.emit("evidence_close", {"reason": "aborted"})
    assert len(mirrored) == 1


def test_a_mirror_that_arrives_late_is_given_what_it_missed(tmp_path):
    """The container copy starts where the host copy starts.

    Nothing emits between the CLI's early sink request and the agent's mirror
    today, but that is an ordering held by luck, not by anything that would
    complain if it changed. An archived ledger beginning at sequence 3 is the
    same after-the-container-is-gone failure in a smaller size.
    """

    mirrored: list[str] = []
    sink = _sink(tmp_path)
    sink.emit("evidence_close", {"reason": "aborted"})
    sink.emit("evidence_close", {"reason": "cancelled"})

    assert sink.attach_mirror(mirrored.append) is True
    sink.emit("evidence_close", {"reason": "test_terminated"})

    sequences = [json.loads(line)["sequence"] for line in mirrored]
    assert sequences == [1, 2, 3]
    assert (tmp_path / "control_events.jsonl").read_text().splitlines() == [
        line.rstrip("\n") for line in mirrored
    ]


def test_a_sink_opened_on_an_existing_ledger_replays_none_of_it(tmp_path):
    """Backfill re-sends this sink's own appends, not an earlier process's."""

    first = _sink(tmp_path)
    first.emit("evidence_close", {"reason": "aborted"})
    first.emit("evidence_close", {"reason": "cancelled"})

    mirrored: list[str] = []
    resumed = _sink(tmp_path)
    # Its own append comes first, so the backfill has something to replay and
    # the choice of where to start is the thing under test -- not an early
    # return that would make any starting point look right.
    resumed.emit("evidence_close", {"reason": "test_terminated"})
    resumed.attach_mirror(mirrored.append)

    assert [json.loads(line)["sequence"] for line in mirrored] == [3]

    resumed.emit("evidence_close", {"reason": "aborted"})
    assert [json.loads(line)["sequence"] for line in mirrored] == [3, 4]


def test_a_failing_backfill_never_breaks_the_ledger(tmp_path):
    def explode(_: str) -> None:
        raise RuntimeError("container is gone")

    sink = _sink(tmp_path)
    sink.emit("evidence_close", {"reason": "aborted"})
    assert sink.attach_mirror(explode) is True
    assert sink.emit("evidence_close", {"reason": "cancelled"}).sequence == 2
    assert len((tmp_path / "control_events.jsonl").read_text().strip().splitlines()) == 2


def test_a_declined_mirror_says_so_out_loud(tmp_path, monkeypatch, warnings_said):
    """Silence is the shape of the defect this whole task exists to end."""

    session_logger = _session_logger(tmp_path, monkeypatch)
    session_logger.get_control_event_sink(mirror=lambda line: None)
    session_logger.get_control_event_sink(mirror=lambda line: None)

    assert any("declined" in said for said in warnings_said), warnings_said


def test_remove_observer_reports_whether_it_was_watching(tmp_path):
    seen: list[str] = []
    sink = _sink(tmp_path, observers=(seen.append,))

    assert sink.remove_observer(seen.append) is True
    assert sink.remove_observer(seen.append) is False
    sink.emit("evidence_close", {"reason": "aborted"})
    assert seen == []


def test_observers_offered_to_a_cached_sink_still_reach_it(tmp_path, monkeypatch):
    """Whatever you hand the session's one sink is applied to the sink it has."""

    session_logger = _session_logger(tmp_path, monkeypatch)
    session_logger.get_control_event_sink()

    seen: list[str] = []
    sink = session_logger.get_control_event_sink(observers=(seen.append,))
    sink.emit("evidence_close", {"reason": "aborted"})
    assert len(seen) == 1


def test_a_sink_built_by_the_session_carries_its_observers_from_the_start(tmp_path, monkeypatch):
    session_logger = _session_logger(tmp_path, monkeypatch)
    seen: list[str] = []
    sink = session_logger.get_control_event_sink(observers=(seen.append,))
    sink.emit("evidence_close", {"reason": "aborted"})
    assert len(seen) == 1


# --- the CLI's own sink ---------------------------------------------------


def _terminal(monkeypatch, width: int = 100) -> io.StringIO:
    """Point the CLI's console at a real Rich terminal writing into memory."""

    buffer = io.StringIO()
    monkeypatch.setattr(
        main_module, "console", Console(file=buffer, force_terminal=True, width=width)
    )
    return buffer


def test_the_run_stream_reaches_a_real_terminal_as_colour_not_as_markup(tmp_path, monkeypatch):
    """`console.file.write` is a raw stream write and interprets no markup.

    Driven through `main._attach_turn_stream` itself, so it fences the CLI's
    CHOICE of write callable, not a callable a test wrote. A sink that merely
    stores strings cannot tell `[red]failed[/red]` from red `failed`.
    """

    buffer = _terminal(monkeypatch)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    for line in FIXTURE.open(encoding="utf-8"):
        renderer.feed(line)
    renderer.close()

    written = buffer.getvalue()
    assert "\x1b[" in written, "a real terminal got no ANSI at all"
    leaked = sorted(set(MARKUP_TAGS.findall(written)))
    assert leaked == [], f"markup tags reached the terminal verbatim: {leaked}"


@pytest.mark.parametrize("width", [40, 60, 100])
def test_the_renderer_owns_its_own_line_discipline(tmp_path, monkeypatch, width):
    """`soft_wrap=True` and `end=""` keep Rich from re-wrapping a finished line.

    The renderer will not render below 60 columns, so a console narrower than
    that is the case where Rich has something to fold: without `soft_wrap` it
    breaks the renderer's already-fitted line into console-width pieces and
    inserts newlines of its own. The count of lines on screen must be the count
    of lines the renderer wrote, at any width.
    """

    buffer = _terminal(monkeypatch, width=width)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    written: list[str] = []
    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    renderer._write = _tee(renderer._write, written)
    for line in FIXTURE.open(encoding="utf-8"):
        renderer.feed(line)
    renderer.close()

    assert buffer.getvalue().count("\n") == "".join(written).count("\n")


def _tee(write, sink: list[str]):
    def tee(text: str) -> None:
        sink.append(text)
        write(text)

    return tee


def test_nothing_but_the_renderer_colours_the_stream(tmp_path, monkeypatch):
    """`highlight=False`, or Rich paints numbers and paths of its own.

    The renderer styles exactly what it means to; Rich's auto-highlighter would
    reach inside a summary it is already styling and colour the numbers there,
    which shows up as escape sequences nobody asked for. Every styled span the
    renderer emits costs one escape to set and one to reset, so the count is
    exact.
    """

    buffer = _terminal(monkeypatch)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    written: list[str] = []
    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    renderer._write = _tee(renderer._write, written)
    for line in FIXTURE.open(encoding="utf-8"):
        renderer.feed(line)
    renderer.close()

    asked_for = len(re.findall(r"\[(?:bold|green|red|yellow|cyan|dim)\]", "".join(written)))
    assert asked_for > 0
    assert buffer.getvalue().count("\x1b[") == asked_for * 2


def test_the_stream_is_cut_to_the_width_of_the_console_it_writes_to(tmp_path, monkeypatch):
    """`width=console.width`, or a narrow terminal gets lines it cannot hold."""

    buffer = _terminal(monkeypatch, width=60)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    for line in FIXTURE.open(encoding="utf-8"):
        renderer.feed(line)
    renderer.close()

    for line in re.sub(r"\x1b\[[0-9;]*m", "", buffer.getvalue()).splitlines():
        assert len(line) <= 60, line


def test_no_session_means_no_stream(monkeypatch):
    """Nothing to observe, nothing to close."""

    monkeypatch.setattr(logger_module, "_session_logger", None)
    assert main_module._attach_turn_stream() is None


def test_the_stream_is_attached_to_the_session_s_own_ledger(tmp_path, monkeypatch):
    """A real emit on the session sink reaches the terminal as a turn band."""

    buffer = _terminal(monkeypatch)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    session_logger.get_control_event_sink().emit(
        "phase_transition",
        {
            "expected_kind": "advance",
            "expected_reason_code": "workspace_ready",
            "expected_target": "analyze",
            "repair_request": None,
        },
    )
    renderer.close()
    assert "analyze" in buffer.getvalue()


# --- the stream survives a crashing run -----------------------------------


class _RecordingRenderer:
    """Stands in for the renderer so a test can see `close()` happen."""

    instances: list["_RecordingRenderer"] = []

    def __init__(self, write, *, width=100, tty=False, clock=None):
        self.write = write
        self.width = width
        self.tty = tty
        self.closed = False
        self.lines: list[str] = []
        _RecordingRenderer.instances.append(self)

    def feed(self, raw_line: str) -> None:
        self.lines.append(raw_line)

    def give_way(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_a_closed_stream_stops_watching_the_ledger(tmp_path, monkeypatch, caplog):
    """A finished renderer must come off the sink, not sit on it refusing.

    `feed()` after `close()` raises, the sink catches it and logs through
    stdlib `logging` — which nothing in this repo routes, so it reaches stderr
    through `lastResort` and lands on the console this layer quieted.
    """

    _terminal(monkeypatch)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)
    renderer = main_module._attach_turn_stream()
    sink = session_logger.get_control_event_sink()

    main_module._close_turn_stream(renderer)

    with caplog.at_level(logging.WARNING, logger="sag.agent.control_events"):
        sink.emit("evidence_close", {"reason": "aborted"})
    assert [record.getMessage() for record in caplog.records] == []


class _CrashingAgent:
    def __init__(self, config, orchestrator, **kwargs):
        self.config = config
        self.orchestrator = orchestrator

    def setup_project(self, **kwargs):
        raise RuntimeError("the run died mid-build")

    def run_task(self, **kwargs):
        raise RuntimeError("the task died mid-build")


class _FakeOrchestrator:
    exists = False

    def __init__(self, project_name=None):
        self.project_name = project_name

    def container_exists(self):
        return self.exists

    def is_container_running(self):
        return True

    def execute_command(self, command, **kwargs):
        return {"success": True, "output": ""}


class _ExistingContainerOrchestrator(_FakeOrchestrator):
    exists = True


@pytest.fixture
def recording_renderer(monkeypatch):
    _RecordingRenderer.instances = []
    monkeypatch.setattr(main_module, "TurnStreamRenderer", _RecordingRenderer)
    return _RecordingRenderer


def test_a_crashing_run_still_closes_its_stream(monkeypatch, tmp_path, recording_renderer):
    """`project`'s trailing `except Exception` would swallow the crash first.

    Without a `finally` the last turn line stays open and every held warning is
    lost -- on exactly the runs a reader most needs to read.
    """

    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.setattr(SessionLogger, "_setup_loggers", lambda self: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", _CrashingAgent)

    result = CliRunner().invoke(
        main_module.cli, ["project", "https://github.com/apache/commons-cli.git"]
    )

    assert result.exit_code == 1
    assert len(recording_renderer.instances) == 1
    assert recording_renderer.instances[0].closed is True


def test_a_renderer_that_dies_closing_never_replaces_the_run_s_own_error(
    monkeypatch, tmp_path, recording_renderer
):
    """The run's exception outranks any renderer, as the ledger's append does.

    `close()` runs from a `finally` inside a command that ends in a broad
    `except Exception`, and it writes to the terminal — so a closed terminal or
    a `BrokenPipeError` from `sag project ... | head` would otherwise be
    reported to the user as the reason the run failed.
    """

    class _CloseFails(_RecordingRenderer):
        def close(self) -> None:
            raise RuntimeError("the terminal went away")

    monkeypatch.setattr(main_module, "TurnStreamRenderer", _CloseFails)
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.setattr(SessionLogger, "_setup_loggers", lambda self: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", _CrashingAgent)

    result = CliRunner().invoke(
        main_module.cli, ["project", "https://github.com/apache/commons-cli.git"]
    )

    assert result.exit_code == 1
    assert "Setup failed: the run died mid-build" in result.output
    assert "Setup failed: the terminal went away" not in result.output
    # The renderer's own failure is still stated -- as a warning, in its place.
    assert "turn stream failed to finish its last line: the terminal went away" in result.output


# --- the stream survives the loop it is narrating ------------------------


class _Screen:
    """What a terminal SHOWS, as opposed to what was written to it.

    A live region does not delete its bytes from the stream — it writes them,
    then returns the cursor and erases the row. So `buffer.getvalue()` still
    contains every turn line even when a user can read none of them, and any
    assertion over the raw stream is blind to this entire class of defect. This
    replays the control bytes Rich actually emits — `\\r`, `\\n`, `ESC[2K`,
    `ESC[K`, `ESC[nA` — and reports the rows that are left.
    """

    _CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

    def __init__(self) -> None:
        self.rows: list[str] = [""]
        self.row = 0
        self.col = 0

    def _put(self, text: str) -> None:
        row = self.rows[self.row].ljust(self.col)
        self.rows[self.row] = row[: self.col] + text + row[self.col + len(text) :]
        self.col += len(text)

    def _newline(self) -> None:
        self.row += 1
        self.col = 0
        while len(self.rows) <= self.row:
            self.rows.append("")

    def write(self, stream: str) -> "_Screen":
        index = 0
        while index < len(stream):
            match = self._CSI.match(stream, index)
            if match:
                params, final = match.group(1), match.group(2)
                if final == "K":
                    mode = params or "0"
                    if mode == "2":
                        self.rows[self.row] = ""
                    elif mode == "1":
                        self.rows[self.row] = " " * self.col + self.rows[self.row][self.col :]
                    else:
                        self.rows[self.row] = self.rows[self.row][: self.col]
                elif final == "A":
                    self.row = max(0, self.row - int(params or 1))
                elif final == "B":
                    for _ in range(int(params or 1)):
                        self._newline()
                index = match.end()
                continue
            char = stream[index]
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self._newline()
            elif char == "\x1b":  # a control sequence this screen does not model
                index += 1
                continue
            else:
                self._put(char)
            index += 1
        return self

    def text(self) -> str:
        return "\n".join(row.rstrip() for row in self.rows)


def test_a_screen_shows_what_a_live_region_left_behind():
    """The emulator itself, or the fence below proves nothing.

    A row written and then cleared is gone; a row above the cleared one stays.
    """

    assert _Screen().write("gone\r\x1b[2Kspinner").text() == "spinner"
    assert _Screen().write("kept\ngone\r\x1b[2Kspinner").text() == "kept\nspinner"
    assert _Screen().write("  #1   bash   clone").text() == "  #1   bash   clone"


def test_every_turn_of_a_real_run_is_still_on_screen_when_the_loop_ends(tmp_path, monkeypatch):
    """The whole point of this task, on the terminal it was built for.

    `_run_unified_setup` is driven for real; only the engine it calls is a
    stand-in, and that stand-in does what the live engine does — control events
    land while the loop runs, and the renderer writes them out. The agent's
    console and the CLI's console are two `Console` instances on one stdout,
    which is the live arrangement: Rich hoists writes above a live region only
    for the console that owns it, so a live region held across the loop repaints
    over every turn line the stream just wrote.

    Offline replays and `StringIO` assertions cannot see this — Rich does no
    cursor work on a non-TTY, and the erased bytes are still in the buffer.
    """

    buffer = io.StringIO()
    monkeypatch.setattr(
        main_module, "console", Console(file=buffer, force_terminal=True, width=100)
    )
    agent_console = Console(file=buffer, force_terminal=True, width=100)

    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)
    renderer = main_module._attach_turn_stream()
    assert renderer is not None

    # A live region erases the row the cursor is on, and a dispatch line sits
    # open on that row for the whole duration of a tool call — 15m08s on one
    # recorded run. Replaying a fixture takes milliseconds, so the wait that a
    # real tool call supplies is supplied here instead, at exactly the moments
    # it happens live: a line is open and the next refresh is about to land.
    pending: list[str] = []
    renderer._write = _tee(renderer._write, pending)

    def run_setup_loop(*, initial_prompt, max_iterations):
        naps = 0
        for line in FIXTURE.open(encoding="utf-8"):
            before = len(pending)
            renderer.feed(line)
            if naps < 3 and len(pending) > before and not pending[-1].endswith("\n"):
                naps += 1
                time.sleep(0.15)
        return "termination"

    stand_in = SimpleNamespace(
        console=agent_console,
        react_engine=SimpleNamespace(run_setup_loop=run_setup_loop),
        max_iterations=5,
        run_termination=None,
        _finalize_run_pin=lambda: None,
    )
    SetupAgent._run_unified_setup(
        stand_in, project_url="https://example.invalid/x.git", project_name="x", goal="build it"
    )
    renderer.close()

    # What the renderer WROTE is the claim; what the screen SHOWS is the test.
    # Taking the expected ids from the renderer's own chunks rather than from a
    # ledger kind keeps this from going vacuous if a fixture does not carry
    # that kind — an empty expectation is a test that cannot fail.
    written = sorted({int(turn) for turn in re.findall(r"^  #(\d+) ", "".join(pending), re.M)})
    assert len(written) > 10, "the stand-in loop rendered almost nothing"

    screen = _Screen().write(buffer.getvalue()).text()
    shown = sorted({int(turn) for turn in re.findall(r"^  #(\d+) ", screen, re.M)})
    erased = [turn for turn in written if turn not in shown]
    assert erased == [], f"turns erased from the screen: {erased}\n--- screen ---\n{screen}"


def test_the_setup_loop_runs_under_no_live_region():
    """The backstop: the turn stream IS the progress display now.

    A source check as well as the behavioural one above, because the defect is
    a `with` block that a future edit could reintroduce anywhere in the method.
    """

    module = ast.parse(Path(inspect.getfile(SetupAgent)).read_text(encoding="utf-8"))
    bodies = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_unified_setup"
    ]
    assert len(bodies) == 1, "located the wrong method"
    body = bodies[0]

    def calls(node) -> set[str]:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
        return names

    assert "run_setup_loop" in calls(body), "the method stopped running the loop"
    for node in ast.walk(body):
        if isinstance(node, ast.With) and "run_setup_loop" in calls(node):
            opened = {
                item.context_expr.func.id
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
            }
            assert "Progress" not in opened, "the ReAct loop is inside a live region again"


def test_a_crashing_task_still_closes_its_stream(monkeypatch, tmp_path, recording_renderer):
    """`sag run` ends the same way `sag project` does, and closes the same way."""

    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.setattr(SessionLogger, "_setup_loggers", lambda self: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", _ExistingContainerOrchestrator)
    monkeypatch.setattr(main_module, "detect_project_directory_in_container", lambda _: None)
    monkeypatch.setattr(main_module, "SetupAgent", _CrashingAgent)

    result = CliRunner().invoke(main_module.cli, ["run", "sag-commons-cli", "--task", "build it"])

    assert result.exit_code == 1
    assert len(recording_renderer.instances) == 1
    assert recording_renderer.instances[0].closed is True


# --- the console shares the screen with the stream --------------------------


def _ledger_line(sequence: int, kind: str, payload: dict) -> str:
    return json.dumps(
        {
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
            "source": None,
            "timestamp": f"2026-09-15T01:00:{sequence:02d}Z",
            "event_id": f"control-{sequence:06d}",
            "run_id": "run-1",
        }
    )


def test_a_console_line_written_while_a_turn_is_open_lands_on_its_own_line(
    tmp_path, monkeypatch, capsys
):
    """The CLI hands the log's console sink to the renderer it attached.

    `_attach_turn_stream` registers `renderer.give_way` with the logger, so a
    warning logged while a dispatch line is open finishes that line first and
    the answer comes back under `↳ #N` — when the engine's record of the turn
    lands, which is what the stream writes the outcome on. `_close_turn_stream`
    takes the hook back before the renderer is closed, so a warning logged
    during shutdown has no renderer to ask and reaches stderr all the same.
    """

    buffer = _terminal(monkeypatch)
    session_logger = _session_logger(tmp_path, monkeypatch)
    monkeypatch.setattr(logger_module, "_session_logger", session_logger)

    renderer = main_module._attach_turn_stream()
    assert renderer is not None
    renderer.feed(
        _ledger_line(
            1,
            "loop_decision",
            {
                "event": {"tool_name": "project", "phase": "provision", "iteration": 1},
                "expected_decision": "continue",
                "expected_reason_code": "ok",
            },
        )
    )
    renderer.feed(
        _ledger_line(
            2,
            "action_envelope",
            {
                "envelope_id": "e1",
                "tool_call_id": "call-2",
                "tool": "project",
                "exact_params": {"action": "provision", "tool": "openjdk", "version": "8"},
                "envelope_sha256": "a" * 64,
            },
        )
    )
    assert not buffer.getvalue().endswith("\n"), "the dispatch line should be open"

    logger_module._console_sink("18:32:56 | WARNING  | something\n")

    assert buffer.getvalue().endswith("\n"), "the open line was not finished first"
    renderer.feed(
        _ledger_line(
            3,
            "tool_result",
            {
                "envelope_id": "e1",
                "execution_id": "x3",
                "tool": "project",
                "params": {},
                "scope": "environment",
                "result": {"operation_outcome": "success"},
            },
        )
    )
    # The answer alone does not write the outcome: the stream waits for the
    # engine to write the turn down, because that record carries the span the
    # trajectory and the report state.
    assert "↳ #2" not in buffer.getvalue()
    renderer.feed(
        _ledger_line(
            4,
            "turn_record",
            {
                "actor": "model",
                "envelope_ref": "e1",
                "phase": "provision",
                "iteration": 1,
                "t0": "2026-09-15T01:00:01Z",
                "t1": "2026-09-15T01:00:03Z",
            },
        )
    )
    # The turn is #2: a `loop_decision` ahead of its envelope opens a call-less
    # #1, which is how these fixtures state the phase before the dispatch.
    assert "↳ #2" in buffer.getvalue()

    main_module._close_turn_stream(renderer)
    logger_module._console_sink("18:32:57 | WARNING  | after\n")

    assert capsys.readouterr().err == (
        "18:32:56 | WARNING  | something\n18:32:57 | WARNING  | after\n"
    )
