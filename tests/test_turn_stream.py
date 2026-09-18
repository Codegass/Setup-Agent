"""One readable line per turn, from the ledger, never from a log.

Two fences live here and they answer different questions.

The unit tests state the renderer's rules one at a time, on payloads shaped like
the real ones. The corpus fence replays three archived sessions end to end and
pins what a reader actually sees — the band sequence, the turn count, the
warning totals, and a verbatim line per tool copied from what the renderer
produced. The second exists because the first is not enough: three times in this
plan a clause was deleted and every invented-fixture test stayed green, and once
a fence pinned a defect as correct.

**On the mutation numbers in this file.** The first round of this task reported
"40 of 40 caught" and a reviewer sampling 20 mutations found 6 survivors. The
cause was not the tooling — `__pycache__` was cleared and bytecode disabled on
every run, and the battery correctly reported SURVIVED the first time it saw
one. The cause was that a survivor was treated as a prompt to write a STRONGER
mutation rather than as a finding, and that a hand-picked list of 40 was then
described as "every clause". The list is now enumerated from the source file
clause by clause, a survivor is reported as a survivor, and the number here, in
the report and in `progress.md` is one number. See
`test_what_this_fence_does_not_catch` for what the three archived
sessions cannot reach on their own. The number for this round is **106 of 110**,
and the four that survived are each named there.

**And on the sessions themselves.** For two rounds this file replayed three
archived sessions and called that the corpus. They are the three of 275 that
predate the engine's per-turn seal, and building a fence on them is how a rule
the live engine can take back got through a review, a re-review and a hundred
mutations. `sling-commons-osgi-v4` is here so that the next such blind spot has
somewhere to show itself.
"""

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from sag.console.turn_stream import TOOL_WIDTH, TurnStreamRenderer, _escape, _Line
from sag.trajectory.reducer import TrajectoryReducer
from sag.trajectory.schema import SUMMARY_MAX_CHARS, CallInfo, GateInfo, ObservationInfo, Turn

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trajectory"
#: Four archived sessions, and the fourth is not a fourth of the same thing.
#:
#: The first three predate the engine's per-turn seal: between them they carry
#: no `turn_record` and no `refusal_record`. The `logs/` corpus has 275
#: sessions and **272 of them carry `turn_record`** — 15,996 events, the second
#: commonest kind there is — so a fence built on those three alone replays the
#: three outliers and is blind to the shape of 99% of real runs. That blindness
#: is not hypothetical: it is how a settling rule that a `turn_record` can
#: retract got through a review, a re-review and a hundred mutations.
#:
#: `sling-commons-osgi-v4` is the fourth. 21 `turn_record`, 2 `refusal_record`,
#: a `file_io` call, a `pending` job, an `evidence_close` and a ledger that
#: closes with zero holes standing — every one of those a path the other three
#: cannot reach.
SESSIONS = ("camel-quarkus-d2r3", "ignite-d2r3", "kafka-d2r3", "sling-commons-osgi-v4")


class _Sink:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def __call__(self, text: str) -> None:
        self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line.strip()]


def _event(sequence: int, kind: str, payload: dict) -> str:
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


def _envelope(sequence: int, tool: str, params: dict, envelope_id: str) -> str:
    return _event(
        sequence,
        "action_envelope",
        {
            "envelope_id": envelope_id,
            "tool_call_id": f"call-{sequence}",
            "tool": tool,
            "exact_params": params,
            "envelope_sha256": "a" * 64,
        },
    )


def _result(sequence: int, tool: str, envelope_id: str, result: dict) -> str:
    return _event(
        sequence,
        "tool_result",
        {
            "envelope_id": envelope_id,
            "execution_id": f"x{sequence}",
            "tool": tool,
            "params": {},
            "scope": "environment",
            "result": result,
        },
    )


def _decision(sequence: int, tool: str, phase: str) -> str:
    return _event(
        sequence,
        "loop_decision",
        {
            "event": {"tool_name": tool, "phase": phase, "iteration": 1},
            "expected_decision": "continue",
            "expected_reason_code": "ok",
        },
    )


def _build_ok() -> dict:
    return {
        "operation_outcome": "success",
        "invocation_status": "completed",
        "facts": {"executed": 994, "failed": 0, "skipped": 61, "errors": 0},
        "metadata": {"analysis": {"exit_code": 0, "artifacts_created": ["a.jar", "b.jar"]}},
    }


def _turn_lines(sink: _Sink) -> list[str]:
    return [line for line in sink.lines if line.lstrip().startswith("#")]


#: A hole the ledger left, as this renderer writes one: two spaces, a bang. The
#: six-space `!` of a job still live at close is a note about a job, not a
#: statement about the ledger, and is counted separately.
_HOLE = "  ! "
_HOLE_HANG = "    "


def _warnings(sink: _Sink) -> list[str]:
    return [line for line in sink.lines if line.startswith(_HOLE)]


def _hole_text(sink: _Sink) -> str:
    """Every hole statement, wrapped lines rejoined."""

    out: list[str] = []
    for line in sink.lines:
        if line.startswith(_HOLE):
            out.append(line.strip())
        elif (
            out
            and line.startswith(_HOLE_HANG)
            and not line.lstrip().startswith(("#", "↳", "…", "!"))
        ):
            out[-1] += " " + line.strip()
    return " ".join(out)


# --- the shape of one turn -------------------------------------------------


def test_one_turn_is_one_line_with_tool_summary_and_outcome():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn clean verify", "action": "verify"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    line = _turn_lines(sink)[0]
    assert line.lstrip().startswith("#1")
    assert "build" in line
    assert "verify mvn clean verify" in line
    assert "exit 0 · 994 tests" in line


def test_the_outcome_lands_on_the_same_line_when_nothing_intervened():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    assert len([line for line in sink.lines if line.lstrip().startswith("#1")]) == 1
    assert "↳" not in sink.text


def test_the_outcome_gets_its_own_line_when_something_was_printed_between():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.feed(
        _event(
            2,
            "job_barrier_wait",
            {
                "job_id": "c523e63040aa",
                "obligation_ref": "o1",
                "waits": 1,
                "waited_seconds": 300.0,
                "remaining_seconds": 900.0,
                "process_state": "running",
                "log_size": 1_258_291,
                "progressing": True,
            },
        )
    )
    stream.feed(_result(3, "build", "e1", _build_ok()))
    stream.close()
    assert "still running" in sink.text
    assert "↳ #1 exit 0" in sink.text


def test_a_line_from_outside_the_ledger_closes_the_open_line_first():
    """A warning arriving while a turn is in flight lands on a line of its own.

    In the first real run loguru wrote `18:32:56 | WARNING | …` to stderr while
    the renderer was holding `#3 project provision openjdk 8` open on stdout.
    The two met on one screen line, and the answer, arriving after, landed on
    the next line with no turn id. The renderer cannot see another writer, so
    the other writer tells it first: `give_way()` finishes the open line, and
    the answer then comes back under its own `↳ #N`.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    assert not sink.text.endswith("\n")
    stream.give_way()
    assert sink.text.endswith("\n")
    written = sink.text
    # Nothing is open now: a second caller gets the screen with no blank line.
    stream.give_way()
    assert sink.text == written
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    assert "↳ #1 exit 0" in sink.text


def test_give_way_after_close_is_a_no_op_because_a_log_sink_must_never_raise():
    """A warning logged during shutdown finds a finished renderer and moves on."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.close()
    stream.give_way()
    assert sink.text == ""


def test_a_turn_still_in_flight_leaves_no_trailing_blanks_on_the_screen():
    """An open line is not padded out to the outcome column it may never reach."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "sleep 900"}, "e1"))
    stream.close()
    head = _turn_lines(sink)[0]
    assert head == head.rstrip()
    assert head.endswith("sleep 900")


def test_a_settled_turn_is_not_restated_when_the_reducer_restates_it():
    """The reducer hands back the whole turn on every event that touches it.

    A real session touches a settled turn three or four more times — the
    `loop_decision` that follows its result, then every later envelope. Printing
    each restatement prints the run four times over.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.feed(_decision(3, "build", "build"))
    stream.feed(_envelope(4, "bash", {"command": "ls"}, "e2"))
    stream.close()
    assert sink.text.count("exit 0") == 1
    assert len([line for line in sink.lines if line.lstrip().startswith("#1")]) == 1


# --- what the outcome says -------------------------------------------------


def test_ok_is_not_said_twice_when_the_result_has_a_line_of_its_own():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "project", {"action": "clone", "repo_url": "a/b"}, "e1"))
    stream.feed(
        _result(
            2,
            "project",
            "e1",
            {
                "operation_outcome": "success",
                "invocation_status": "completed",
                "metadata": {"resolved_commit": "26b251a" + "0" * 33, "clone_path": "/w/b"},
            },
        )
    )
    stream.close()
    line = _turn_lines(sink)[0]
    assert "26b251a → /w/b" in line
    assert "ok" not in line


def test_ok_is_said_when_the_derivation_has_nothing_else_to_say():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.close()
    assert "ok" in _turn_lines(sink)[0]


def test_a_failure_always_says_the_word():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "project", {"action": "env", "tool": "gradle"}, "e1"))
    stream.feed(
        _result(2, "project", "e1", {"operation_outcome": "failed", "error_code": "ENV_MISSING"})
    )
    stream.close()
    assert "failed · ENV_MISSING" in _turn_lines(sink)[0]


def test_the_turns_timing_is_never_the_thing_that_gets_clipped():
    """A long result yields its last characters so the duration survives."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn verify"}, "e1"))
    stream.feed(_result(59, "build", "e1", _build_ok()))
    stream.close()
    line = _turn_lines(sink)[0]
    assert line.endswith("58.0s")
    assert "exit 0 · 994 tests · 0 F · 61 S · 2 artifacts" in line


def test_a_turn_the_ledger_never_timed_says_nothing_about_timing():
    """Absence is stated, never implied — and never defaulted to a zero.

    A turn settles with no timing when neither of its events carried a stamp.
    `0.0s` would be a measurement nobody took, and the one number a reader
    scanning for the slow turn would most like to trust.
    """

    untimed = [
        json.loads(line)
        for line in (
            _envelope(1, "bash", {"command": "ls"}, "e1"),
            _result(2, "bash", "e1", {"operation_outcome": "success"}),
        )
    ]
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    for event in untimed:
        event["timestamp"] = None
        stream.feed(json.dumps(event))
    stream.close()
    line = _turn_lines(sink)[0]
    assert line.rstrip().endswith("ok")
    assert "0.0s" not in sink.text


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            "PHASE_ACTION_MISMATCH",
            "  #1   build     build                       "
            "refused · PHASE_ACTION_MISMATCH                    0.0s",
        ),
        (
            "CALL_NOT_EXECUTED",
            "  #1   build     build                       "
            "cancelled · cancelled                              0.0s",
        ),
    ],
)
def test_a_refused_call_says_it_was_refused(code, expected):
    """No archived fixture holds a refusal, so only this fences the two words.

    298 refusal records exist across the 550-ledger corpus and none of them is
    in the three sessions committed here, so the corpus fence below cannot reach
    `refused` or `cancelled` — they are pinned by shape instead.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "refusal_record",
            {
                "tool": "build",
                "tool_call_id": "call-1",
                "refusal_code": code,
                "exact_params_sha256": "b" * 64,
            },
        )
    )
    stream.close()
    styled = _Sink()
    marked = TurnStreamRenderer(styled, width=100, tty=True)
    marked.feed(
        _event(
            1,
            "refusal_record",
            {
                "tool": "build",
                "tool_call_id": "call-1",
                "refusal_code": code,
                "exact_params_sha256": "b" * 64,
            },
        )
    )
    marked.close()
    word = "cancelled" if code == "CALL_NOT_EXECUTED" else "refused"
    assert f"[yellow]{word}[/yellow]" in styled.text
    # Pinned whole, not by substring: a refusal is the only real shape whose
    # turn is opened and answered by one event, so this line is the only fence
    # on the head being carried out to the column an outcome starts in. It is
    # also where a call with no params of its own shows the tool name twice —
    # the derivation states a tool and no summary, and repeating the tool is
    # the most this layer can say without inventing a phrase.
    assert _turn_lines(sink) == [expected]


def test_an_untimed_outcome_is_still_clipped_to_the_room_it_has():
    """The branch that has no timing to reserve still has a width to obey."""

    untimed = [
        json.loads(_envelope(1, "bash", {"command": "ls"}, "e1")),
        json.loads(
            _result(2, "bash", "e1", {"operation_outcome": "failed", "error_code": "E" * 90})
        ),
    ]
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    for event in untimed:
        event["timestamp"] = None
        stream.feed(json.dumps(event))
    stream.close()
    for line in sink.lines:
        assert len(line) <= 100, line
    assert _turn_lines(sink)[0].endswith("…")


def test_a_controller_turn_is_marked_as_the_engine():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "forced_action",
            {
                "envelope_id": "e1",
                "tool": "phase",
                "phase": "report",
                "exact_params": {"signal": "done", "phase": "report"},
                "policy": "p",
                "trigger": "t",
                "reason_code": "r",
            },
        )
    )
    stream.close()
    assert any("engine" in line for line in sink.lines)


# --- the gate --------------------------------------------------------------


def _gate(sequence: int, word: str, phase: str) -> str:
    return _event(
        sequence,
        "gate_decision",
        {
            "decision_id": f"d{sequence}",
            "phase": phase,
            "expected_outcome": word,
            "expected_signal": "done",
            "reason": "r",
            "key_results": "k",
            "validator_state": "green",
        },
    )


def test_a_gate_that_arrives_after_the_result_gets_its_own_line_once():
    """The grading always lands on a later event than the call it grades.

    So the turn's line is already finished when the word arrives, and the word
    is new information rather than a restatement — it takes a continuation line,
    and takes it exactly once however many times the turn is restated after.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_result(2, "phase", "e1", {"operation_outcome": "success"}))
    stream.feed(_gate(3, "partial", "build"))
    stream.feed(_decision(4, "phase", "build"))
    stream.close()
    assert sink.text.count("gate: partial") == 1
    assert "↳ #1 gate: partial" in sink.text


def test_a_gate_on_a_dispatched_turn_waits_for_the_answer_rather_than_taking_it():
    """The gate lands while the turn's line is still open.

    Only an answer finishes a turn's line. A gate completing it instead would
    mark the turn as having spoken, and the result — landing after — would find
    the turn already spoken for and never render its outcome at all.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "phase", "build"))
    stream.feed(_envelope(2, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_gate(3, "partial", "build"))
    stream.feed(_result(4, "phase", "e1", {"operation_outcome": "success"}))
    # And the restatement that follows every result in a real ledger must not
    # say the gate again: the line it went on has to be recorded as having it.
    stream.feed(_decision(5, "phase", "build"))
    stream.close()
    line = [row for row in _turn_lines(sink) if row.lstrip().startswith("#2")][0]
    assert "ok" in line
    assert "gate: partial" in line
    assert sink.text.count("gate: partial") == 1
    assert "↳" not in sink.text


def test_an_escaped_bracket_does_not_move_the_outcome_column_of_an_open_line():
    """The in-place completion of a line that was left open, with markup on.

    `_open_cost` is how many columns the head took. Measured on the MARKUP it
    counts the backslashes Rich adds and never shows, so the outcome of any turn
    whose call held a bracket lands in a different column from every other turn.
    """

    plain, marked = _Sink(), _Sink()
    for sink, tty in ((plain, False), (marked, True)):
        stream = TurnStreamRenderer(sink, width=100, tty=tty)
        stream.feed(_decision(1, "bash", "build"))
        stream.feed(_envelope(2, "bash", {"command": "grep -n '[info] [warn] x'"}, "e1"))
        stream.feed(_result(3, "bash", "e1", {"operation_outcome": "success"}))
        stream.close()
    assert [Text.from_markup(line).plain for line in marked.lines] == plain.lines
    bracketed = [line for line in marked.lines if "info" in line][0]
    rendered = Text.from_markup(bracketed).plain
    assert "[info] [warn] x" in rendered
    # The outcome lands in the column it would have landed in with no brackets
    # in the call at all: markup the reader never sees does not move columns.
    without = _Sink()
    stream = TurnStreamRenderer(without, width=100, tty=True)
    stream.feed(_decision(1, "bash", "build"))
    stream.feed(_envelope(2, "bash", {"command": "grep -n  info   warn  x "}, "e1"))
    stream.feed(_result(3, "bash", "e1", {"operation_outcome": "success"}))
    stream.close()
    other = Text.from_markup([line for line in without.lines if "warn" in line][0]).plain
    assert rendered.index("ok") == other.index("ok")


def test_a_gate_already_in_hand_stays_on_the_turns_own_line():
    """And does not finish the line on its own, taking the outcome with it.

    Only an answer finishes a turn. A gate arriving first used to complete the
    line by itself, which marked the turn as having spoken; the result then
    landed on a turn already spoken for and its outcome was never rendered at
    all. Gates follow results in every archived session, so only this reaches it.
    The gate disagrees with the claim here: an agreeing one says nothing (R49).
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_gate(2, "partial", "build"))
    stream.feed(_result(3, "phase", "e1", {"operation_outcome": "success"}))
    stream.close()
    line = _turn_lines(sink)[0]
    assert "gate: partial" in line
    assert "ok" in line
    assert "↳" not in sink.text


def test_a_gate_that_agrees_with_the_claim_adds_no_line():
    """R49: `done success` graded `success` is one fact, and is said once.

    Five of the nineteen turns of the first real run carried `↳ #N gate:
    success` under a line already reading `done success`. Where the gate
    disagrees — `done success` graded `partial`, kafka turn 16 — the
    continuation is the most valuable line in the stream, and suppressing
    agreement is what lets that one stand out.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_result(2, "phase", "e1", {"operation_outcome": "success"}))
    stream.feed(_gate(3, "success", "build"))
    stream.feed(_decision(4, "phase", "build"))
    stream.close()
    assert "gate:" not in sink.text
    assert len(_turn_lines(sink)) == 1


def test_a_gate_in_hand_that_agrees_with_the_claim_is_not_repeated_on_the_line():
    """The same rule when the grading arrived before the line was written."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "blocked", "outcome": "failed"}, "e1"))
    stream.feed(_gate(2, "failed", "build"))
    stream.feed(_result(3, "phase", "e1", {"operation_outcome": "success"}))
    stream.close()
    assert "gate:" not in sink.text
    assert "blocked failed" in _turn_lines(sink)[0]


# --- phases ----------------------------------------------------------------


def test_a_phase_opens_a_band_and_a_transition_closes_it():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "project", {"action": "clone", "repo_url": "x/y"}, "e1"))
    stream.feed(_result(2, "project", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(3, "project", "provision"))
    stream.feed(
        _event(
            4,
            "phase_transition",
            {
                "expected_kind": "advance",
                "expected_target": "analyze",
                "expected_reason_code": "workspace_ready",
            },
        )
    )
    stream.close()
    assert any(line.startswith("▸ provision") for line in sink.lines)
    assert any(line.startswith("✓ provision advanced") for line in sink.lines)


def test_a_band_is_never_opened_for_a_run_the_ledger_has_not_placed():
    """`unknown` is the reducer saying nothing has stated a phase, not a name."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "project", {"action": "clone", "repo_url": "x/y"}, "e1"))
    stream.close()
    assert "unknown" not in sink.text
    assert not [line for line in sink.lines if line.startswith("▸")]


def test_a_band_opens_once_per_entry_however_many_turns_it_holds():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    for index, (envelope, sequence) in enumerate(((f"e{n}", n) for n in (1, 3, 5)), start=1):
        stream.feed(_envelope(sequence, "bash", {"command": f"echo {index}"}, envelope))
        stream.feed(_result(sequence + 1, "bash", envelope, {"operation_outcome": "success"}))
        stream.feed(_decision(sequence + 1, "bash", "build"))
    stream.close()
    assert sink.text.count("▸ build") == 1


def test_a_blocked_phase_says_why():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "phase_transition",
            {
                "expected_kind": "repair",
                "expected_target": "build",
                "expected_reason_code": "maven_version_below_minimum",
            },
        )
    )
    stream.close()
    assert sink.lines == ["→ build repair · maven_version_below_minimum"]


@pytest.mark.parametrize(
    "kind,target,reason,expected",
    [
        ("advance", "analyze", "workspace_ready", "✓ build advanced"),
        # The target, not the band the run is in: a repair names the phase it is
        # sending the run back to, which is the one fact the line exists for.
        (
            "repair",
            "analyze",
            "maven_version_below_minimum",
            "→ analyze repair · maven_version_below_minimum",
        ),
        ("report", "report", "report_ready", "✓ build reported"),
        ("evidence_close", "test", "test_terminal", "✓ build finished"),
        # `flow_close` ends the run, so it names no phase.
        ("flow_close", "report", "report_terminal", "✓ run ended"),
        ("something_new", "x", "y", "· build something_new · y"),
    ],
)
def test_every_kind_of_transition_has_a_line(kind, target, reason, expected):
    """`repair` and `report` are real but unarchived, so only this fences them.

    `PhaseTransitionPayload.expected_kind` admits five kinds; the 550-ledger
    corpus holds three (advance 1532, evidence_close 526, flow_close 526). The
    last row is an unknown kind, which gets a neutral glyph rather than a
    KeyError, because a schema this layer does not yet know is not a crash.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(3, "bash", "build"))
    stream.feed(
        _event(
            4,
            "phase_transition",
            {
                "expected_kind": kind,
                "expected_target": target,
                "expected_reason_code": reason,
            },
        )
    )
    stream.close()
    assert expected in sink.lines


def _transition(sequence: int, kind: str, target: str, reason: str) -> str:
    return _event(
        sequence,
        "phase_transition",
        {
            "expected_kind": kind,
            "expected_target": target,
            "expected_reason_code": reason,
        },
    )


def _run_to_close(kind: str, reason: str, *, gate: str | None = None) -> list[str]:
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(3, "bash", "test"))
    if gate is not None:
        stream.feed(_gate(4, gate, "test"))
    stream.feed(_transition(5, kind, "test", reason))
    stream.close()
    return sink.lines


def test_a_band_close_does_not_print_the_ledgers_own_event_kind():
    """Spec 4.2 prescribes advanced / blocked / repair, and nothing else.

    Every normal run closed two of its five bands with `evidence_close` and
    `flow_close` — the ledger's `expected_kind` values — beside a raw
    `expected_reason_code`. Measured over 120 ledgers: advance 338,
    evidence_close 110, flow_close 109.
    """

    closes = [line for line in _run_to_close("evidence_close", "test_terminal") if line[0] in "✓✗→"]

    assert closes, "the transition still prints a line"
    for line in closes:
        assert "evidence_close" not in line
        assert "test_terminal" not in line


def test_the_close_that_ends_the_run_is_not_attributed_to_a_phase():
    """`flow_close` is the run ending, not the phase the run happened to be in."""

    closes = [line for line in _run_to_close("flow_close", "report_terminal") if line[0] in "✓✗→"]

    assert closes == ["✓ run ended"]


def test_an_advance_states_the_phase_and_nothing_machine_made():
    closes = [line for line in _run_to_close("advance", "workspace_ready") if line[0] in "✓✗→"]

    assert closes == ["✓ test advanced"]


def test_a_phase_its_gate_failed_does_not_close_with_a_tick():
    """storm's test phase was graded `failed` and the band still closed green.

    A reader scanning the left edge for the run's shape reads a column of
    ticks and never learns that one of them sat over a phase that did not
    finish.
    """

    failed = [line for line in _run_to_close("evidence_close", "test_terminal", gate="failed")
              if line[0] in "✓✗→"]
    passed = [line for line in _run_to_close("evidence_close", "test_terminal", gate="success")
              if line[0] in "✓✗→"]

    assert failed == ["✗ test blocked"]
    assert passed == ["✓ test finished"]


# --- jobs ------------------------------------------------------------------


def test_job_progress_is_throttled_to_once_a_minute_per_job():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100, clock=iter([0.0, 10.0, 70.0]).__next__)
    payload = {
        "job_id": "j1",
        "obligation_ref": "o1",
        "waits": 1,
        "waited_seconds": 10.0,
        "remaining_seconds": 10.0,
        "process_state": "running",
        "progressing": True,
    }
    for sequence in (1, 2, 3):
        stream.feed(_event(sequence, "job_barrier_wait", dict(payload)))
    stream.close()
    assert sink.text.count("still running") == 2


def test_a_job_note_says_how_long_it_has_waited_and_how_big_the_log_is():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "job_barrier_wait",
            {
                "job_id": "c523e63040aa",
                "obligation_ref": "o1",
                "waits": 1,
                "waited_seconds": 300.0,
                "remaining_seconds": 900.0,
                "process_state": "running",
                "log_size": 1_258_291,
                "progressing": True,
            },
        )
    )
    stream.close()
    assert sink.lines[0] == "      … still running · 5m00s · log 1.2 MB · progressing"


def test_a_job_note_states_only_what_the_payload_carries():
    """No log size and no progress flag is two clauses absent, not two zeros."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(
        _event(
            1,
            "job_barrier_wait",
            {
                "job_id": "j1",
                "obligation_ref": "o1",
                "waits": 1,
                "waited_seconds": 61.0,
                "remaining_seconds": 900.0,
                "process_state": "running",
            },
        )
    )
    stream.close()
    assert sink.lines[0] == "      … still running · 1m01s"


def test_two_jobs_are_throttled_apart():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100, clock=iter([0.0, 1.0]).__next__)
    for sequence, job in ((1, "j1"), (2, "j2")):
        stream.feed(
            _event(
                sequence,
                "job_barrier_wait",
                {
                    "job_id": job,
                    "obligation_ref": "o1",
                    "waits": 1,
                    "waited_seconds": 10.0,
                    "remaining_seconds": 10.0,
                    "process_state": "running",
                },
            )
        )
    stream.close()
    assert sink.text.count("still running") == 2


# --- holes -----------------------------------------------------------------


def test_a_dangling_envelope_states_both_of_the_holes_it_leaves():
    """One envelope with nothing after it leaves TWO statements, not one.

    The turn has no result and it emitted no decision, and the reducer states
    both — they are different holes and a reader chasing one is not told about
    the other by being told about the first. The brief asserted one warning
    here; it asserted the count it had written the code to produce rather than
    the count the derivation makes, and two is the honest answer.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.close()
    joined = _hole_text(sink)
    assert "missing_tool_result: turn 1 has no tool_result and no typed refusal" in joined
    # The second hole is real and is not dropped — it is one of the two codes
    # that are counted rather than listed, so the count states it.
    assert "more note" in joined and "missing_loop_decision ×1" in joined


def test_a_turn_in_flight_is_not_yet_a_hole():
    """`missing_tool_result` is true of every turn between dispatch and answer.

    Printing it as the reducer states it would put an exclamation under every
    dispatch of the run and take none of them back. It is held until the run has
    moved past the turn it names.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "sleep 5"}, "e1"))
    assert _warnings(sink) == []
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(3, "bash", "build"))
    stream.feed(_envelope(4, "bash", {"command": "ls"}, "e2"))
    stream.close()
    assert "missing_tool_result: turn 1" not in sink.text


def test_a_neighbour_opening_is_not_evidence_that_this_turn_settled():
    """The counter-example the first round's release rule got wrong.

    Two calls in flight at once: B opens and answers while A is still running.
    Treating "a later turn exists" as "this turn settled" states a hole on A the
    derivation withdraws two events later, which is exactly the firehose R33
    exists to end — with the added insult that it cannot be un-printed. The
    three archived sessions are strictly sequential, so only a constructed case
    reaches it.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "sleep 900"}, "e1"))
    stream.feed(_envelope(2, "build", {"command": "mvn test"}, "e2"))
    stream.feed(_result(3, "build", "e2", _build_ok()))
    assert "turn 1" not in sink.text
    stream.feed(_result(4, "bash", "e1", {"operation_outcome": "success"}))
    stream.close()
    assert "missing_tool_result" not in sink.text
    assert "↳ #1 ok" in sink.text


def _turn_record(sequence: int, *, envelope_ref: str | None = None, actor: str = "model") -> str:
    return _event(
        sequence,
        "turn_record",
        {
            "actor": actor,
            "envelope_ref": envelope_ref,
            "phase": "build",
            "iteration": 1,
            "t0": f"2026-09-15T01:00:{sequence:02d}Z",
            "t1": f"2026-09-15T01:00:{sequence:02d}Z",
        },
    )


def test_a_turn_record_can_take_back_a_hole_so_having_no_call_does_not_settle_one():
    """The second door into R33, and the one a live run walks through.

    A turn that arrives with no call looks final — nothing more can arrive for
    it — until a `turn_record` seals it, at which point `_sealed_turn_warnings`
    states a DIFFERENT set and both holes standing on it are withdrawn. Treating
    "it has no call" as proof it had settled printed two statements the
    derivation then took back, with no un-print.

    None of the first three fixtures can show this: they carry no `turn_record`
    at all. 272 of the corpus's 275 sessions do.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "bash", "build"))
    assert _inline_holes(sink) == [], "nothing the ledger can still withdraw"
    stream.feed(_turn_record(2))
    stream.close()
    assert "missing_tool_result" not in sink.text
    assert "missing_envelope: turn 1 has a loop_decision" not in sink.text


def test_a_hole_on_an_unsealed_turn_is_still_stated_at_the_close():
    """Held, not dropped: the same ledger with no record still states both."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "bash", "build"))
    stream.close()
    joined = _hole_text(sink)
    assert "missing_envelope: turn 1 has a loop_decision but no action_envelope" in joined
    assert "missing_tool_result: turn 1 has no tool_result and no typed refusal" in joined


def test_a_hole_is_stated_once_however_often_the_reducer_restates_it():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "bash", "build"))
    for sequence, envelope in ((3, "e2"), (5, "e3"), (7, "e4")):
        stream.feed(_envelope(sequence, "bash", {"command": "ls"}, envelope))
        stream.feed(_result(sequence + 1, "bash", envelope, {"operation_outcome": "success"}))
    stream.close()
    assert sink.text.count("missing_envelope: turn 1") == 1


def test_a_holes_explanation_wraps_rather_than_losing_its_second_half():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=60)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.close()
    joined = _hole_text(sink)
    assert "turn 1 has no tool_result and no typed refusal" in joined
    for line in sink.lines:
        assert len(line) <= 60, line


# --- the ledger is the only input -----------------------------------------


def test_a_wrapped_hole_hangs_four_columns_in_and_not_under_its_own_code():
    """A 28-column hanging indent eats a third of an 80-column line."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=60)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.close()
    wrapped = [line for line in sink.lines if line.startswith(_HOLE_HANG) and _HOLE not in line]
    assert wrapped
    for line in wrapped:
        assert len(line) - len(line.lstrip()) == len(_HOLE_HANG), line


def test_the_stream_never_reads_a_log_line():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed("2026-09-14 21:06:09 | INFO | Executing command in container: ls")
    stream.close()
    assert sink.lines == []


def test_a_torn_event_line_is_stated_and_a_log_line_is_not():
    """A log line and a half-written event line are not the same thing.

    A process killed mid-write leaves a truncated last line, which is exactly
    what a live reader needs told. The old rule dropped both in silence, which
    made `malformed_event_line` unreachable from this renderer entirely.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed("2026-09-14 21:06:09 | INFO | Executing command in container: ls")
    stream.feed('{"sequence": 2, "kind": "tool_result", "payload": {"envelope')
    assert "malformed_event_line" in _hole_text(sink), "stated where it happened"
    stream.close()
    joined = _hole_text(sink)
    assert "malformed_event_line" in joined
    assert "Executing command" not in sink.text
    assert len(_inline_holes(sink)) == 1


def test_an_event_whose_payload_is_not_a_mapping_does_not_crash_the_run():
    """The note handlers read keys off the payload; `None.get` is a crash."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_event(1, "job_live_at_close", {}).replace('"payload": {}', '"payload": 5'))
    stream.feed(_event(2, "job_barrier_wait", {}).replace('"payload": {}', '"payload": null'))
    stream.feed(_event(3, "phase_transition", {}).replace('"payload": {}', '"payload": []'))
    stream.close()
    assert "still live at close" in sink.text


def test_a_line_that_names_no_kind_is_a_torn_event_and_says_so():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed('{"sequence": 2, "payload": {}}')
    stream.close()
    assert "malformed_event_line" in _hole_text(sink)


def test_a_job_with_no_name_is_not_given_the_name_none():
    """`None` on a user's terminal is a value nobody stated."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_event(1, "job_live_at_close", {}))
    stream.close()
    assert "      ! a job still live at close" in sink.lines
    assert "job None" not in "".join(line for line in sink.lines if line.startswith("    "))


def test_close_is_terminal_and_a_second_session_cannot_continue_the_first():
    """One renderer renders one session.

    Feeding a closed renderer used to be accepted and silently continue: the
    already-shown holes stayed suppressed, the old band carried over so a new
    run's first band was dropped, and turn ids ran on from the finished run.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.close()
    with pytest.raises(RuntimeError):
        stream.feed(_envelope(3, "bash", {"command": "pwd"}, "e2"))
    # Including a line that would have produced no turn at all: the refusal is
    # `feed`'s own, not one it happens to inherit from rendering something.
    with pytest.raises(RuntimeError):
        stream.feed("2026-09-14 21:06:09 | INFO | Executing command in container: ls")


def test_the_two_accounting_codes_are_counted_and_everything_else_is_stated():
    """R34's split, on constructed events rather than only on the corpus."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "bash", "build"))
    stream.feed(_envelope(2, "bash", {"command": "ls"}, "e1"))
    stream.close()
    inline = _inline_holes(sink)
    assert [hole.split(":")[0].strip("! ") for hole in inline] == [
        "missing_envelope",
        "missing_tool_result",
        "missing_tool_result",
    ]
    assert "missing_loop_decision" not in " ".join(inline)
    assert (
        "2 more notes about the ledger itself: conservation_violation ×1, "
        "missing_loop_decision ×1" in _hole_text(sink)
    )


def test_a_session_with_nothing_to_count_says_nothing_about_counting():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(3, "bash", "build"))
    stream.close()
    assert "more note" not in sink.text
    assert _inline_holes(sink) == []


def test_the_call_column_grows_with_the_terminal_and_stops_where_the_derivation_does():
    """Upward as well as downward, and no further than the longest line it gets.

    `CallInfo.summary` is capped at `SUMMARY_MAX_CHARS`, so past that the room
    belongs to what came back rather than to blank columns after a call.
    """

    command = "x" * 300
    widths = {}
    for width in (80, 100, 120, 200, 400):
        sink = _Sink()
        stream = TurnStreamRenderer(sink, width=width)
        stream.feed(_envelope(1, "bash", {"command": command}, "e1"))
        stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
        stream.close()
        widths[width] = len(_call_of(_turn_lines(sink)[0]))
    assert widths[80] < widths[100] < widths[120] < widths[200]
    assert widths[200] == widths[400] == SUMMARY_MAX_CHARS


def test_the_call_and_what_came_back_are_two_columns_not_one_string():
    """A call clipped to its full column keeps two blank columns after it."""

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "y" * 300}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    stream.close()
    assert "…  ok" in _turn_lines(sink)[0]


def test_nothing_is_buffered_across_a_close():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.close()
    before = sink.text
    assert before.endswith("\n")
    stream.close()
    assert sink.text == before


# --- width -----------------------------------------------------------------


def test_no_line_exceeds_the_configured_width():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=80)
    stream.feed(_envelope(1, "bash", {"command": "echo " + "x" * 300}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "failed", "error_code": "E" * 90}))
    stream.close()
    for line in sink.lines:
        assert len(line) <= 80, line


def test_a_terminal_too_narrow_to_hold_a_turn_is_overrun_rather_than_obeyed():
    """Below sixty columns there is no layout left, only a column of ellipses.

    So the width floor wins and the lines run past the edge, where the terminal
    wraps them and a reader still gets the words. Obeying a twenty-column
    request would produce rows that say `#1  bas… …` and nothing else.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=20)
    stream.feed(_envelope(1, "build", {"command": "mvn verify"}, "e1"))
    stream.feed(_result(2, "build", "e1", _build_ok()))
    stream.close()
    line = _turn_lines(sink)[0]
    assert len(line) > 20
    assert len(line) <= 60
    assert "verify mvn verify" in line
    assert "exit 0" in line


def test_a_narrow_terminal_takes_the_room_from_the_call_not_the_answer():
    """A line that says what was asked and clips the answer has said nothing."""

    wide, narrow = _Sink(), _Sink()
    for sink, width in ((wide, 120), (narrow, 80)):
        stream = TurnStreamRenderer(sink, width=width)
        stream.feed(_envelope(1, "build", {"command": "mvn -pl core verify"}, "e1"))
        stream.feed(
            _result(
                2,
                "build",
                "e1",
                {
                    "operation_outcome": "failed",
                    "invocation_status": "completed",
                    "error_code": "TEST_FAILURE",
                    "metadata": {"analysis": {"exit_code": 1}},
                },
            )
        )
        stream.close()
    assert "exit 1 · TEST_FAILURE" in _turn_lines(narrow)[0]
    assert "verify mvn -pl core verify" in _turn_lines(wide)[0]
    assert "verify mvn -pl core verify" not in _turn_lines(narrow)[0]


def test_the_tool_column_is_the_width_this_module_publishes():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    for sequence, tool, params, envelope in (
        (1, "bash", {"command": "ls"}, "e1"),
        (3, "project", {"action": "analyze"}, "e2"),
    ):
        stream.feed(_envelope(sequence, tool, params, envelope))
        stream.feed(_result(sequence + 1, tool, envelope, {"operation_outcome": "success"}))
        stream.feed(_decision(sequence + 1, tool, "build"))
    stream.close()
    first, second = (line.index(name) for line, name in zip(_turn_lines(sink), ("bash", "project")))
    assert first == second
    assert TOOL_WIDTH == 9


def test_a_held_first_line_is_placed_before_the_next_turn_takes_the_terminal():
    """Two turns dispatched before the ledger states a phase, not one lost.

    The first line is held for its band; a second turn arriving is the signal
    that it has waited long enough. Overwriting the held turn instead would
    print the second and lose the first entirely.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "first"}, "e1"))
    stream.feed(_envelope(2, "bash", {"command": "second"}, "e2"))
    stream.close()
    assert [line.split()[0] for line in _turn_lines(sink)] == ["#1", "#2"]
    assert "first" in _turn_lines(sink)[0]


# --- styling ---------------------------------------------------------------


def _styled(width: int, lines: list[str]) -> tuple[list[str], list[str]]:
    plain, marked = _Sink(), _Sink()
    for sink, tty in ((plain, False), (marked, True)):
        stream = TurnStreamRenderer(sink, width=width, tty=tty)
        for line in lines:
            stream.feed(line)
        stream.close()
    return plain.lines, marked.lines


def test_a_styled_line_is_measured_by_what_a_reader_sees():
    """Markup is not characters on a screen.

    Clipping a marked-up string by its own length clips the visible line early
    and can cut a tag in half, leaking `[/gre` to the terminal. Layout happens
    on the plain text and the markup is spliced in afterwards, so the two paths
    render the same characters at every width.
    """

    events = [
        _envelope(1, "bash", {"command": "echo " + "x" * 300}, "e1"),
        _result(2, "bash", "e1", {"operation_outcome": "failed", "error_code": "E" * 90}),
    ]
    for width in (60, 80, 100, 120):
        plain, marked = _styled(width, events)
        assert [Text.from_markup(line).plain for line in marked] == plain
        for line in marked:
            assert Text.from_markup(line).plain == Text.from_markup(line).plain.rstrip("[/]")


def test_a_clip_that_lands_inside_a_styled_word_still_closes_its_tag():
    events = [
        _envelope(1, "bash", {"command": "x" * 200}, "e1"),
        _result(2, "bash", "e1", {"operation_outcome": "cancelled"}),
    ]
    _, marked = _styled(60, events)
    for line in marked:
        assert line.count("[") == line.count("]")
        Text.from_markup(line)


def test_a_styled_line_is_clipped_by_its_characters_and_not_by_its_tags():
    """The layout unit itself, because this is where the decision lives.

    `_Line` is private, and it is what the decision about styling and width was
    made in: text accumulates plain, styles are recorded as ranges over it, and
    markup is spliced in only at render. Asserting it here means the property is
    pinned where it is implemented rather than only where it happens to show.
    """

    line = _Line().add("failed", "red").add(" · " + "E" * 200)
    # The style covers the word it was given and stops there: a span that ran on
    # to the end of the clipped line would paint the whole row red and no
    # plain-text assertion would ever see it.
    assert line.render(100, tty=True).startswith("[red]failed[/red] · EEE")
    for width in (4, 10, 40, 100):
        rendered = line.render(width, tty=True)
        plain = Text.from_markup(rendered).plain
        assert 0 < len(plain) <= width, (width, plain)
        assert plain.endswith("…")
        assert rendered.count("[red]") == rendered.count("[/red]") == 1
    # A clip landing INSIDE the styled word keeps the word's tag closed around
    # only what survived, rather than reaching past the end of the line.
    assert line.render(4, tty=True) == "[red]fai…[/red]"
    # And a clip landing on the separator drops the separator with it, rather
    # than leaving `failed ·…` — a join with nothing on the far side of it.
    assert line.render(10, tty=True) == "[red]failed[/red]…"
    assert line.render(12, tty=True) == "[red]failed[/red] · EE…"


def test_a_truncated_span_does_not_paint_what_is_appended_after_it():
    """`_settle` truncates the outcome and then appends the turn's timing.

    A span that kept the end it had before the cut would reach past the text it
    was clipped to, and the appended timing would land inside it.
    """

    grown = _Line().add("cancelled", "yellow").truncated(4).add(" · 1.0s")
    assert grown.render(100, tty=True) == "[yellow]can…[/yellow] · 1.0s"


def test_a_style_over_no_text_records_nothing():
    """`[red][/red]` around nothing is markup a terminal parses for no reason."""

    assert _Line().add("", "red").add("x").render(10, tty=True) == "x"


def test_a_span_dropped_by_a_truncation_cannot_come_back_when_text_is_appended():
    """`truncated()` then `add()` is what `_settle` does to every outcome.

    A span kept past the cut would be dormant until the append made the text
    long enough to reach it again, and would then paint characters it was never
    given.
    """

    grown = _Line().add("ab").add("cd", "red").truncated(2).add("xyz")
    assert grown.render(100, tty=True) == "a…xyz"


def test_a_style_wholly_past_the_cut_is_dropped_rather_than_emitted_empty():
    line = _Line().add("ab").add("cd", "red")
    # Clipped to two columns the styled half is gone entirely; emitting an empty
    # `[red][/red]` around nothing is markup a reader's terminal parses for no
    # reason, and a pair of them is how a stray tag gets noticed.
    assert line.render(2, tty=True) == "a…"
    assert line.render(4, tty=True) == "ab[red]cd[/red]"


def test_a_bracket_a_reader_typed_is_not_read_as_a_style_tag():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100, tty=True)
    stream.feed(_envelope(1, "bash", {"command": "grep -n '[info] [/red] x'"}, "e1"))
    stream.close()
    rendered = Text.from_markup(sink.lines[0]).plain
    assert "[info] [/red] x" in rendered


def test_an_escaped_bracket_does_not_move_the_outcome_column():
    """The escape Rich needs adds characters a reader never sees.

    An outcome column placed by the length of the MARKUP lands in a different
    place on every line whose call happened to contain a bracket — and real
    calls contain brackets.
    """

    plain, marked = _Sink(), _Sink()
    for sink, tty in ((plain, False), (marked, True)):
        stream = TurnStreamRenderer(sink, width=100, tty=tty)
        stream.feed(_envelope(1, "bash", {"command": "grep -n '[info] x'"}, "e1"))
        stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
        stream.close()
    assert [Text.from_markup(line).plain for line in marked.lines] == plain.lines


def test_the_plain_path_carries_no_markup_at_all():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "failed", "error_code": "BOOM"}))
    stream.close()
    assert "[red]" not in sink.text
    assert "\\[" not in sink.text


# --- the corpus fence: three real sessions, replayed line by line ----------


def _render(session: str, width: int = 100, tty: bool = False) -> _Sink:
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=width, tty=tty)
    for line in (FIXTURE_DIR / session / "control_events.jsonl").read_text().splitlines():
        stream.feed(line)
    stream.close()
    return sink


#: What each archived session renders as. Every number was read off the output
#: of the shipped renderer and checked against the ledger it came from.
#:
#: `standing` is the count of statements that were still TRUE at the end — the
#: brief would have printed 65, 171 and 97 of them, 86% of camel-quarkus' false.
#: `inline` is how many of those a reader is shown where they happen; the rest
#: are the two accounting codes, which are counted and stated once at the close.
#: inline + counted == standing, and the fence asserts that rather than trusting
#: the three numbers to have been written down consistently.
CORPUS = {
    "kafka-d2r3": {
        "turns": 24,
        "bands": ["provision", "analyze", "build", "test", "report"],
        "standing": 11,
        "inline": 0,
        "counted": {"conservation_violation": 1, "missing_loop_decision": 10},
        "lines": 39,
    },
    "camel-quarkus-d2r3": {
        "turns": 58,
        "bands": ["provision", "analyze", "build", "test", "report"],
        "standing": 24,
        "inline": 6,
        "counted": {"conservation_violation": 1, "missing_loop_decision": 17},
        "lines": 86,
    },
    "ignite-d2r3": {
        "turns": 35,
        "bands": ["provision", "analyze", "build", "test"],
        "standing": 9,
        "inline": 0,
        "counted": {"conservation_violation": 1, "missing_loop_decision": 8},
        "lines": 49,
    },
    # The only one of the four whose ledger closes with nothing standing: 21
    # turns, every one sealed by a `turn_record`, no hole anywhere. It is what a
    # healthy run looks like in this stream, and the only fixture that reaches
    # the closing tally's silent path, the refusal path and a `file_io` call on
    # real bytes.
    "sling-commons-osgi-v4": {
        "turns": 21,
        "bands": ["provision", "analyze", "build", "test"],
        "standing": 0,
        "inline": 0,
        "counted": {},
        "lines": 28,
    },
}


def _call_of(line: str) -> str:
    """The call column of a turn line, whatever width it was rendered at.

    The renderer keeps at least two blank columns between the call and what came
    back, so the boundary is findable without the test knowing the layout.
    """

    fields = [field for field in line.split("  ") if field]
    return fields[2] if len(fields) > 2 else ""


def _inline_holes(sink: _Sink) -> list[str]:
    """The holes stated where they happened, without the closing tally."""

    return [line for line in _warnings(sink) if "more note" not in line]


@pytest.mark.parametrize("session", SESSIONS)
def test_every_turn_of_a_real_session_gets_exactly_one_dispatch_line(session):
    sink = _render(session)
    expected = CORPUS[session]["turns"]
    ids = [line.strip().split()[0] for line in _turn_lines(sink)]
    assert ids == [f"#{n}" for n in range(1, expected + 1)]


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_renders_the_bands_it_actually_walked(session):
    sink = _render(session)
    assert [line[2:] for line in sink.lines if line.startswith("▸ ")] == CORPUS[session]["bands"]


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_opens_with_its_band_and_not_with_a_turn(session):
    """The run's first line is the phase it is in, not the call it made there.

    Nothing states a phase until the first turn's result, so the first line is
    held for its band rather than printed above it.
    """

    assert _render(session).lines[0].startswith("▸ ")


def test_a_held_line_is_placed_as_soon_as_a_phase_is_stated_not_when_it_answers():
    """A long first call should not leave the terminal empty until it returns.

    The line is held for its BAND, so anything that states the phase places it —
    here a `loop_decision`, before the call has come back at all.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "build", {"command": "mvn verify"}, "e1"))
    assert sink.lines == []
    stream.feed(_decision(2, "build", "build"))
    assert sink.lines[0] == "▸ build"
    assert sink.lines[1].lstrip().startswith("#1")
    assert "verify mvn verify" in sink.lines[1]


def test_a_hole_about_a_call_less_turn_waits_for_the_close():
    """Because a `turn_record` can still withdraw it, and 99% of runs write one.

    camel-quarkus has no records, so all six statements are true at the end and
    all six are made — after the last turn, in `warning_order`, not under the
    turns they name. That is the price of the predicate having no proxy in it:
    six statements move to the foot of one session, and none of them can be a
    statement the ledger takes back.
    """

    lines = _render("camel-quarkus-d2r3").lines
    holes = [index for index, line in enumerate(lines) if line.startswith(_HOLE)]
    last_turn = max(index for index, line in enumerate(lines) if line.lstrip().startswith("#"))
    assert holes and min(holes) > last_turn
    assert lines[min(holes)] == (
        "  ! missing_envelope: turn 22 has a loop_decision but no action_envelope"
    )


@pytest.mark.parametrize("session", SESSIONS)
def test_every_band_opens_above_the_first_turn_it_holds(session):
    """A band under its own first turn reads as though the turn preceded it.

    It happens because the ledger does not state a phase until the turn's result
    lands — twice per session, at the run's first turn and again after an
    evidence close. The first line is held for its band rather than printed
    above it.
    """

    lines = _render(session).lines
    for index, line in enumerate(lines):
        if line.startswith("▸ "):
            assert lines[index + 1].lstrip().startswith("#"), line


@pytest.mark.parametrize("session", SESSIONS)
def test_only_the_holes_that_still_stand_are_stated_and_the_rest_are_counted(session):
    """R33's numbers, as literals, and R34's split of them.

    Printing each warning as the reducer emits it would print 65 / 171 / 97 —
    one under every dispatch — and retract none of them. These are the counts
    that were still true when the ledger ended, and how they are divided between
    what a reader is shown and what is counted for them. The fourth session's
    zeroes are the useful ones: a healthy closed ledger says nothing at all.
    """

    expected = CORPUS[session]
    sink = _render(session, width=200)
    inline = _inline_holes(sink)
    assert len(inline) == expected["inline"]
    counted = expected["counted"]
    total = sum(counted.values())
    named = ", ".join(f"{code} ×{n}" for code, n in sorted(counted.items()))
    if total:
        assert f"{total} more notes about the ledger itself: {named}" in _hole_text(sink)
    else:
        assert "more note" not in sink.text
    assert len(inline) + total == expected["standing"]
    for code in counted:
        assert not [hole for hole in inline if hole.startswith(f"{_HOLE}{code}:")]


@pytest.mark.parametrize("session", SESSIONS)
def test_the_stream_states_exactly_the_holes_the_derivation_holds(session):
    """Hold-and-release must arrive at the same set the reducer would state.

    Adding on `warnings` and dropping on `retracted_warnings` is the schema's
    own accumulation rule; this is the check that following it line by line
    lands on the same statements a snapshot of the whole ledger makes, so the
    stream and the timeline cannot describe the same run's holes differently.
    The two accounting codes are checked by their counts rather than their text,
    because the stream states those as a number.
    """

    reducer = TrajectoryReducer()
    for line in (FIXTURE_DIR / session / "control_events.jsonl").read_text().splitlines():
        reducer.feed(line)
    held = reducer.snapshot().warnings
    counted = CORPUS[session]["counted"]
    expected = sorted(
        f"{warning.code}: {warning.detail}" for warning in held if warning.code not in counted
    )
    sink = _render(session, width=200)
    printed = sorted(hole.strip()[len("! ") :] for hole in _inline_holes(sink))
    assert printed == expected
    for code, count in counted.items():
        assert sum(warning.code == code for warning in held) == count


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_fits_the_terminal_it_was_given(session):
    for width in (60, 80, 100, 120, 200):
        sink = _render(session, width=width)
        assert sink.lines
        for line in sink.lines:
            assert len(line) <= width, (width, line)


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_is_the_length_it_is(session):
    """A count, so that a clause going dead takes a number with it."""

    assert len(_render(session).lines) == CORPUS[session]["lines"]


@pytest.mark.parametrize("session", SESSIONS)
def test_the_styled_and_plain_renderings_of_a_real_session_agree(session):
    plain, marked = _render(session).lines, _render(session, tty=True).lines
    assert [Text.from_markup(line).plain for line in marked] == plain


def test_a_real_line_paints_the_outcome_word_and_nothing_else():
    marked = _render("kafka-d2r3", tty=True).lines
    assert (
        "  #3   project   env gradle                  "
        "[red]failed[/red] · ENV_EXECUTABLE_NOT_FOUND                  0.8s" in marked
    )


def test_a_job_still_running_is_styled_as_the_unfinished_thing_it_is():
    """The fourth session dispatches a job and answers the turn while it runs."""

    marked = _render("sling-commons-osgi-v4", tty=True).lines
    assert [line for line in marked if "[cyan]pending[/cyan] · running" in line]


def test_the_lines_a_watcher_is_scanning_for_carry_a_style():
    """Bands, closes and holes, not only the outcome word.

    On a real terminal the lines a watcher most needs to catch were the ones
    with no colour on them.
    """

    marked = _render("camel-quarkus-d2r3", tty=True).lines
    assert "[bold]▸ build[/bold]" in marked
    assert "[green]✓ build advanced[/green]" in marked
    assert (
        "[yellow]  ! missing_envelope: turn 22 has a loop_decision "
        "but no action_envelope[/yellow]" in marked
    )


def test_a_five_way_collision_is_five_different_lines_when_the_terminal_has_the_room():
    """The one place the reviewer could not follow the run.

    camel-quarkus calls `bash` five times with commands that share their first
    46 characters and differ by the 57th. A call column that stops at 40 renders
    all five as the same string on a terminal that had the room to tell them
    apart.
    """

    collisions = [
        line
        for line in _turn_lines(_render("camel-quarkus-d2r3", width=120))
        if "JAVA_HOME=" in line
    ]
    assert len(collisions) == 5
    assert len({_call_of(line) for line in collisions}) == 5
    # 120 and not 110: the five diverge at the 57th character and the call
    # column is 53 at width 110, so three of them collapse into one there. The
    # fence says where the property starts holding rather than implying it holds
    # everywhere.
    at_110 = [
        line
        for line in _turn_lines(_render("camel-quarkus-d2r3", width=110))
        if "JAVA_HOME=" in line
    ]
    assert len({_call_of(line) for line in at_110}) == 3
    # And at 80 columns there is genuinely no room, which the fence states
    # rather than pretending otherwise.
    narrow = [
        line
        for line in _turn_lines(_render("camel-quarkus-d2r3", width=80))
        if "JAVA_HOME=" in line
    ]
    assert len({_call_of(line) for line in narrow}) == 1


#: One verbatim line per tool, copied from what the renderer printed on the
#: session named. These are what a reader reads; a summariser branch going dead
#: changes one of them and this fails with the before and after side by side.
#: The `sling-commons-osgi-v4` rows are the four shapes no other fixture has.
VERBATIM = [
    (
        "kafka-d2r3",
        "  #1   project   clone apache/kafka@4.3.1    26b251a → /workspace/kafka                        12.6s",
    ),
    (
        "kafka-d2r3",
        "  #15  build     compile --no-daemon         exit 0                                            1m23s",
    ),
    (
        "kafka-d2r3",
        "  #19  build     test --no-daemon :clients:test  failed · exit 1 · DETACHED_OPERATION_FAILED   7m26s",
    ),
    (
        "kafka-d2r3",
        "  #20  search    output_3ea47959569d /(?i)(BUILD SUCCESSFUL…  80 matches                        1.9s",
    ),
    (
        "kafka-d2r3",
        "  #5   bash      which gradle || true; ls -l /usr/bin/gradl…  ok                                2.8s",
    ),
    (
        "kafka-d2r3",
        "  #14  advisor   consult                     advice delivered                                   0.0s",
    ),
    (
        "kafka-d2r3",
        "  #23  report    generate                    ok                                                 0.9s",
    ),
    (
        "kafka-d2r3",
        "  #7   phase     done success                workspace_present · workspace /workspace/kafka e…  0.2s",
    ),
    # The gate here DISAGREES with the claim (`done success`), which is the only
    # kind of gate continuation that prints (R49).
    ("kafka-d2r3", "      ↳ #16 gate: partial"),
    ("kafka-d2r3", "▸ provision"),
    ("kafka-d2r3", "✓ provision advanced"),
    # This one used to read `✓ test evidence_close · test_terminal`: a green
    # tick over a phase whose gate said failed, in the ledger's own words.
    ("kafka-d2r3", "✗ test blocked"),
    ("kafka-d2r3", "✓ run ended"),
    (
        "ignite-d2r3",
        "  #17  build     compile                     exit 0 · 5 artifacts                              2m27s",
    ),
    (
        "ignite-d2r3",
        "  #35  ⚙ engine  test                        pending · running · job 2c4d56b2fdca             15m06s",
    ),
    ("ignite-d2r3", "      ! job 2c4d56b2fdca still live at close"),
    ("ignite-d2r3", "      ↳ #34 gate: unknown"),
    ("camel-quarkus-d2r3", "  #22  —         no call"),
    (
        "camel-quarkus-d2r3",
        "  ! missing_envelope: turn 22 has a loop_decision but no action_envelope",
    ),
    (
        "camel-quarkus-d2r3",
        "  ! missing_tool_result: turn 22 has no tool_result and no typed refusal",
    ),
    (
        "camel-quarkus-d2r3",
        "  #14  build     test -DskipITs -DskipIntegrationTests -Dsk…  failed · exit 1 · JAVA_VERSION…  6m23s",
    ),
    # A `file_io` call. R21 recorded "no archived fixture holds a `file_io`
    # call, so that branch is pinned by unit tests only"; this one does.
    (
        "sling-commons-osgi-v4",
        "  #6   file_io   read /workspace/sling-org-apache-sling-com…  ok                                1.8s",
    ),
    # A refusal, on real bytes. R21 said the same of refusal records.
    (
        "sling-commons-osgi-v4",
        "  #20  search    search                      cancelled · cancelled                              0.0s",
    ),
    # A job dispatched and still running when its turn was answered.
    (
        "sling-commons-osgi-v4",
        "  #19  search    job:output_a93bbaebcf50     pending · running                                  1.7s",
    ),
    (
        "sling-commons-osgi-v4",
        "  #14  bash      mvn --show-version -U -B -e clean install…  ok                                1m04s",
    ),
]


@pytest.mark.parametrize("session,line", VERBATIM, ids=range(len(VERBATIM)))
def test_a_real_line_reads_exactly_this(session, line):
    assert line in _render(session).lines


def test_a_real_gate_that_said_unknown_says_so():
    """`gate_decision.expected_outcome` is literally `unknown` once in ignite.

    The gate DID state something, so the stream states it. This is the one token
    that means absence elsewhere in the schema — `operation_outcome: unknown` is
    `None`, and an `unknown` phase opens no band — and the difference is that a
    gate saying `unknown` is a gate that answered.
    """

    assert "      ↳ #34 gate: unknown" in _render("ignite-d2r3").lines


def test_a_phase_reason_code_is_rendered_bare_like_every_other_reason_code():
    """One word, one fact.

    `summaries._phase_observation` used to label a gate's reason code `gate
    <code>`, and this layer says `gate:` for the word a gate DELIVERED, so the
    two sat on adjacent lines under one word and neither could be read. Fixed at
    its source now, so nothing here takes a label off a neighbour's string.
    """

    lines = _render("kafka-d2r3").lines
    turn = [line for line in lines if line.lstrip().startswith("#7 ")][0]
    assert "workspace_present · workspace" in turn
    assert "gate workspace_present" not in turn
    assert "      ↳ #16 gate: partial" in lines


def test_an_archived_gate_that_agrees_with_its_claim_is_not_restated():
    """R49 on the archive: kafka's four agreeing gates say nothing, its three
    disagreeing ones (#16 partial, #21 failed, #24 success) each take a line."""

    lines = _render("kafka-d2r3").lines
    continuations = [line for line in lines if "gate:" in line]
    assert continuations == [
        "      ↳ #16 gate: partial",
        "      ↳ #21 gate: failed",
        "      ↳ #24 gate: success",
    ]


def test_the_counted_notes_say_where_to_read_them_in_full():
    joined = _hole_text(_render("kafka-d2r3", width=80))
    assert (
        "11 more notes about the ledger itself: conservation_violation ×1, "
        "missing_loop_decision ×10 — read them in full with: "
        "uv run sag trajectory <session> --format json" in joined
    )


def test_a_clip_at_eighty_columns_never_ends_on_a_dangling_separator():
    for session in SESSIONS:
        for line in _render(session, width=80).lines:
            assert not line.endswith("·…"), line
            assert " ·…" not in line, line


# --- the sink a real terminal actually is --------------------------------


def _through_rich(markup: str, width: int) -> str:
    """What a `rich.console.Console` puts on the screen for this markup.

    R40 pinned the Task 5 sink as `console.print(text, end="", markup=True,
    highlight=False, soft_wrap=True)`. A callable that stores strings cannot
    tell markup from rendered output, which is the gap every styled assertion in
    this file used to sit in — so the styled path is checked through the real
    thing, with colour off so what comes back is the characters a reader sees.
    """

    buffer = io.StringIO()
    Console(file=buffer, force_terminal=False, no_color=True, width=max(width, 10_000)).print(
        markup, end="", markup=True, highlight=False, soft_wrap=True
    )
    return buffer.getvalue()


@pytest.mark.parametrize("width", [60, 80, 100, 110, 120, 140, 200, 300])
@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_reaches_a_real_terminal_unchanged(session, width):
    r"""Every character of every line, at every width, through a Rich sink.

    This ran at one width before, and one width was enough to hide that
    `rich.markup.escape` does not round-trip: ignite `#18`'s search pattern
    carries `\[INFO\]`, and at width 100 that line clipped before the first
    backslash. At 110 and above it does not, and two characters of a regex the
    reader would copy were being dropped on the way to the screen.
    """

    plain = _render(session, width=width).lines
    styled = _render(session, width=width, tty=True).lines
    assert [_through_rich(line, width) for line in styled] == plain


def test_a_backslash_before_a_bracket_survives_the_sink():
    r"""`rich.markup.escape` escapes for one of Rich's two rules, not both.

    A TAG-shaped bracket is literal behind an odd number of backslashes, and the
    run is halved; every OTHER `\[` simply loses its backslash. Rich's own
    escape implements the first rule only, so it hands `\[INFO\]` straight
    through and the sink prints `[INFO\]`.
    """

    awkward = [
        r"/(BUILD SUCCESS|ERROR|FAILURE|\[INFO\] Building|\[INFO\] Re",
        r"x\[INFO\]y",
        r"a\[b\]c",
        r"\s+\[",
        r"sed -e 's/\[0-9\]//'",
        "[INFO] Building",
        "[red]x[/red]",
        "[/]",
        "[]",
        "\\\\",
        "\\",
        r"C:\path\to",
    ]
    for text in awkward:
        assert _through_rich(_escape(text), 200) == text, text


def test_the_pattern_ignite_really_ran_reaches_the_screen_whole():
    line = [
        row for row in _render("ignite-d2r3", width=200, tty=True).lines if "BUILD SUCCESS" in row
    ][0]
    assert r"\[INFO\] Building" in _through_rich(line, 200)


# --- the other public entry point ----------------------------------------


def _turn(**fields) -> Turn:
    """A `Turn` built by hand, for the paths no ledger produces.

    `render_turn` is public (R6) and takes any `Turn`. Five clauses this file
    once called unfenceable are reachable through it: they are unreachable from
    what the reducer emits, which is not the same as unreachable from the
    module's own contract.
    """

    base = {"turn_id": 1, "phase": "build", "actor": "model"}
    return Turn.model_validate({**base, **fields})


def _observation(**fields) -> ObservationInfo:
    return ObservationInfo.model_validate(fields)


def test_render_turn_is_public_and_renders_a_turn_no_ledger_would_produce():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.render_turn(
        _turn(
            call=CallInfo(tool="bash", summary="ls -l"),
            observation=_observation(outcome="ok", summary="two files"),
            t0="2026-09-15T01:00:00Z",
            t1="2026-09-15T01:00:03Z",
        )
    )
    stream.close()
    assert sink.lines == [
        "▸ build",
        "  #1   bash      ls -l                       two files                                          3.0s",
    ]


def test_a_turn_that_arrives_already_answered_is_not_held_for_a_band():
    """Holding it would be holding for a band its phase can never name.

    Nothing more will arrive for an answered turn, so the band it goes under is
    already decided; holding it only delays the line.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.render_turn(
        _turn(
            phase="unknown",
            call=CallInfo(tool="bash", summary="ls"),
            observation=_observation(outcome="ok"),
        )
    )
    assert _turn_lines(sink), "written at once, not held for a phase that will never come"
    assert not [line for line in sink.lines if line.startswith("▸")]


def test_a_band_opens_after_a_late_outcome_and_not_before_it():
    """A turn that moves from one KNOWN phase to another.

    No archived ledger does this — the only phase moves in all four sessions are
    `unknown` to a real phase — so only a hand-built pair of turns reaches the
    ordering. The outcome belongs to the band the turn was dispatched in, so it
    is written before the new band opens, not under it.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.render_turn(_turn(phase="build", call=CallInfo(tool="bash", summary="ls")))
    stream.render_turn(
        _turn(
            phase="test",
            call=CallInfo(tool="bash", summary="ls"),
            observation=_observation(outcome="ok"),
        )
    )
    stream.close()
    assert [line.strip()[:9] for line in sink.lines] == ["▸ build", "#1   bash", "▸ test"]


def test_an_outcome_never_lands_on_another_turns_open_line():
    """Two calls in flight and the EARLIER one answers first.

    Without the guard the outcome is written onto whatever line happens to be
    open — #2's — and #1 never gets one at all. The existing two-in-flight test
    feeds the results the other way round, so it cannot see this.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_decision(1, "bash", "build"))
    stream.feed(_envelope(2, "bash", {"command": "first"}, "e1"))
    stream.feed(_envelope(3, "bash", {"command": "second"}, "e2"))
    stream.feed(_result(4, "bash", "e1", {"operation_outcome": "success"}))
    stream.close()
    second = [line for line in _turn_lines(sink) if line.lstrip().startswith("#3")][0]
    assert second.rstrip().endswith("second"), "still in flight, and says nothing else"
    assert "↳ #2 ok" in sink.text


def test_render_turn_refuses_after_close_the_way_feed_does():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.close()
    with pytest.raises(RuntimeError):
        stream.render_turn(_turn(call=CallInfo(tool="bash", summary="ls")))


def test_a_gate_in_hand_when_a_held_line_is_finally_written_is_not_said_twice():
    """R36's hold defers a write past events that already happened.

    So a turn dispatched late can arrive with its grading already on the line,
    and the next restatement of that turn will say it again unless the write
    records it. Event order is not render order.
    """

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_gate(2, "partial", "build"))
    stream.feed(_result(3, "phase", "e1", {"operation_outcome": "success"}))
    stream.feed(_decision(4, "phase", "build"))
    stream.close()
    assert sink.text.count("gate: partial") == 1
    assert "↳" not in sink.text


def test_held_statements_are_released_in_the_order_warnings_have():
    """Driven directly, because `feed()` cannot produce the disagreement.

    Insertion order into the held set follows the reducer's emission order, and
    `warning_order` keys on `control_seq` first, so on everything a ledger can
    produce the two coincide. Two statements held out of sequence order tell
    them apart.
    """

    from sag.trajectory.schema import Warning as LedgerWarning

    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=200)
    later = LedgerWarning(code="unknown_event_kind", detail="second", control_seq=9)
    earlier = LedgerWarning(code="unknown_event_kind", detail="first", control_seq=2)
    stream._hold([later, earlier], [])
    stream._release()
    stream.close()
    assert [line.split(": ")[1] for line in _inline_holes(sink)] == ["first", "second"]


def test_what_this_fence_does_not_catch():
    """The named gaps, rather than a silent one (R16/R27).

    110 mutations enumerated by walking `turn_stream.py` clause by clause, all
    110 applied, **106 caught**. The four that did not fail a test are listed
    here so the next reader knows which parts of the module are held up by
    reading rather than by this file. All four are equivalent mutants: no input
    the module can be given distinguishes them, through `feed()` or through
    `render_turn()`.

    - `min(SUMMARY_MAX_CHARS, …)` on the call column. `CallInfo.summary` is
      capped at `SUMMARY_MAX_CHARS` by the schema, so a renderer column wider
      than that has nothing to put in it. The clause states the coupling; the
      schema is what enforces it.
    - `_event`'s `isinstance(kind, str)` guard. A non-string kind matches none
      of the `elif` arms, so returning it changes nothing.
    - Rendering a delta's turns before releasing its statements, rather than
      after. Every statement a delta releases is about a turn settled in an
      EARLIER delta, and every turn a delta prints is one no statement is
      released for yet, so no ledger produces a line that lands in a different
      place under the two orders.
    - `close()`'s explicit `_flush()`. A close that still holds a turn's line
      also still holds a statement about that turn — an unanswered turn always
      leaves `missing_tool_result` standing — and stating one flushes.

    And one value, not a clause: `_JOB_NOTE_INTERVAL_SECONDS` itself. Any
    interval between one second and a session's length passes every test here.
    The throttle's existence is fenced; its calibration is a judgment.

    **What the fourth fixture changed.** Exactly one clause is caught by
    `sling-commons-osgi-v4` and by nothing else — the `pending` style, which no
    other session reaches. Seventeen test cases replay it, and it is among the
    failures of 35 of the 106. That is the honest mutation number, and it
    understates the point: the fixture's value is not that it catches more of
    today's clauses but that it is the only one of the four that can catch a
    tomorrow's. The first three carry no `turn_record` between them, and the
    live engine writes one per sealed turn; a settling rule that a `turn_record`
    silently retracted survived a review, a re-review and a hundred mutations
    against those three, because nothing in them could produce the event.

    The two claims above that can be checked rather than read are checked
    below, so this stops being a green test that asserts nothing: the first
    mutant is equivalent only while the schema caps the summary, and the
    throttle's existence is what the docstring says is fenced.
    """

    import pydantic

    from sag.console.turn_stream import _JOB_NOTE_INTERVAL_SECONDS
    from sag.trajectory.schema import SUMMARY_MAX_CHARS, CallInfo, ObservationInfo

    # Why `min(SUMMARY_MAX_CHARS, …)` is an equivalent mutant: nothing longer
    # can reach the column. If this cap is ever lifted, the clause starts doing
    # work and this docstring starts being wrong. The legal summary is built
    # first so the refusal below cannot be a missing field instead of a cap.
    for model, required in ((CallInfo, {"tool": "bash"}), (ObservationInfo, {})):
        assert model(**required, summary="x" * SUMMARY_MAX_CHARS).summary
        with pytest.raises(pydantic.ValidationError, match="at most"):
            model(**required, summary="x" * (SUMMARY_MAX_CHARS + 1))

    assert _JOB_NOTE_INTERVAL_SECONDS > 0, "the throttle exists; its value is a judgment"
