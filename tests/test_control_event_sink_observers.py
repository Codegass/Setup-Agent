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

import io
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

import sag.config as config_module
import sag.config.logger as logger_module
import sag.main as main_module
from sag.agent.control_events import ControlEventSink
from sag.config import Config
from sag.config.logger import SessionLogger

FIXTURE = Path(__file__).parent / "fixtures" / "trajectory" / "kafka-d2r3" / "control_events.jsonl"

#: Every markup spelling the renderer can emit, open and close. A terminal that
#: receives any of these verbatim is a terminal the user reads tags on.
MARKUP_TAGS = re.compile(r"\[/?(?:bold|green|red|yellow|cyan|dim)\]")


def _sink(tmp_path, **kwargs) -> ControlEventSink:
    return ControlEventSink(tmp_path / "control_events.jsonl", run_id="run-1", **kwargs)


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

    def close(self) -> None:
        self.closed = True


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
