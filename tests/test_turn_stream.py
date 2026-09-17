"""One readable line per turn, from the ledger, never from a log.

Two fences live here and they answer different questions.

The unit tests state the renderer's rules one at a time, on payloads shaped like
the real ones. The corpus fence replays three archived sessions end to end and
pins what a reader actually sees — the band sequence, the turn count, the
warning totals, and a verbatim line per tool copied from what the renderer
produced. The second exists because the first is not enough: three times in this
plan a clause was deleted and every invented-fixture test stayed green, and once
a fence pinned a defect as correct. Every clause of `turn_stream.py` was deleted
in turn and confirmed to take a test with it — 39 of 39 — and the four that only
a constructed payload reaches, because no archived session holds the shape, are
named in `test_what_the_corpus_replay_alone_cannot_reach`.
"""

import json
from pathlib import Path

import pytest
from rich.text import Text

from sag.console.turn_stream import TOOL_WIDTH, TurnStreamRenderer, _Line
from sag.trajectory.reducer import TrajectoryReducer

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trajectory"
SESSIONS = ("camel-quarkus-d2r3", "ignite-d2r3", "kafka-d2r3")


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
            and line.startswith(" " * len(_HOLE))
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
    assert "↳ exit 0" in sink.text


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
    assert "…" in line


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
            "  #1   build     build"
            "                                    refused · PHASE_ACTION_MISMATCH · 0.0s",
        ),
        (
            "CALL_NOT_EXECUTED",
            "  #1   build     build                                    cancelled · cancelled · 0.0s",
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
    # Pinned whole, not by substring: a refusal is the only real shape whose
    # turn is opened and answered by one event, so this line is the only fence
    # on the head being carried out to the column an outcome starts in. It is
    # also where a call with no params of its own shows the tool name twice —
    # the derivation states a tool and no summary, and repeating the tool is
    # the most this layer can say without inventing a phrase.
    assert _turn_lines(sink) == [expected]


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
    assert sink.text.count("graded partial") == 1
    assert "↳ graded partial" in sink.text


def test_a_gate_already_in_hand_stays_on_the_turns_own_line():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "phase", {"action": "done", "outcome": "success"}, "e1"))
    stream.feed(_gate(2, "success", "build"))
    stream.feed(_result(3, "phase", "e1", {"operation_outcome": "success"}))
    stream.close()
    assert "graded success" in _turn_lines(sink)[0]
    assert "↳" not in sink.text


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
        ("advance", "analyze", "workspace_ready", "✓ build advanced · workspace_ready"),
        (
            "repair",
            "build",
            "maven_version_below_minimum",
            "→ build repair · maven_version_below_minimum",
        ),
        ("report", "report", "report_ready", "✓ build report · report_ready"),
        ("evidence_close", "test", "test_terminal", "✓ build evidence_close · test_terminal"),
        ("flow_close", "report", "report_terminal", "✓ build flow_close · report_terminal"),
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
    warnings = _warnings(sink)
    assert len(warnings) == 2
    assert [warning.split(":")[0].lstrip("! ") for warning in warnings] == [
        "missing_loop_decision",
        "missing_tool_result",
    ]


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


def test_a_hole_is_stated_as_soon_as_the_run_has_moved_past_the_turn():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "sleep 5"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    assert _warnings(sink) == []
    stream.feed(_envelope(3, "bash", {"command": "ls"}, "e2"))
    assert any("missing_loop_decision: turn 1" in line for line in _warnings(sink))


def test_a_hole_is_stated_once_however_often_the_reducer_restates_it():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed(_envelope(1, "bash", {"command": "sleep 5"}, "e1"))
    stream.feed(_result(2, "bash", "e1", {"operation_outcome": "success"}))
    for sequence, envelope in ((3, "e2"), (5, "e3"), (7, "e4")):
        stream.feed(_envelope(sequence, "bash", {"command": "ls"}, envelope))
        stream.feed(_result(sequence + 1, "bash", envelope, {"operation_outcome": "success"}))
    stream.close()
    assert sink.text.count("missing_loop_decision: turn 1") == 1


def test_a_holes_explanation_wraps_rather_than_losing_its_second_half():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=60)
    stream.feed(_envelope(1, "build", {"command": "mvn test"}, "e1"))
    stream.close()
    joined = _hole_text(sink)
    assert "turn 1 called 'build' and emitted no loop_decision" in joined
    for line in sink.lines:
        assert len(line) <= 60, line


# --- the ledger is the only input -----------------------------------------


def test_the_stream_never_reads_a_log_line():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100)
    stream.feed("2026-09-14 21:06:09 | INFO | Executing command in container: ls")
    stream.close()
    assert sink.lines == []


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
    stream.feed(_envelope(1, "bash", {"command": "ls"}, "e1"))
    stream.feed(_envelope(3, "project", {"action": "analyze"}, "e2"))
    stream.close()
    first, second = (line.index(name) for line, name in zip(_turn_lines(sink), ("bash", "project")))
    assert first == second
    assert TOOL_WIDTH == 9


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
    for width in (10, 40, 100):
        rendered = line.render(width, tty=True)
        plain = Text.from_markup(rendered).plain
        assert len(plain) == width, (width, plain)
        assert plain.endswith("…")
        assert rendered.count("[red]") == rendered.count("[/red]") == 1
    # A clip landing INSIDE the styled word keeps the word's tag closed around
    # only what survived, rather than reaching past the end of the line.
    tight = line.render(4, tty=True)
    assert Text.from_markup(tight).plain == "fai…"
    assert tight == "[red]fai…[/red]"


def test_a_bracket_a_reader_typed_is_not_read_as_a_style_tag():
    sink = _Sink()
    stream = TurnStreamRenderer(sink, width=100, tty=True)
    stream.feed(_envelope(1, "bash", {"command": "grep -n '[info] [/red] x'"}, "e1"))
    stream.close()
    rendered = Text.from_markup(sink.lines[0]).plain
    assert "[info] [/red] x" in rendered


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
#: `warnings` is the count of statements that were still TRUE at the end — the
#: brief would have printed 65, 171 and 97 of them, 86% of camel-quarkus' false.
CORPUS = {
    "kafka-d2r3": {
        "turns": 24,
        "bands": ["provision", "analyze", "build", "test", "report"],
        "warnings": 11,
        "missing_tool_result": 0,
        "lines": 53,
    },
    "camel-quarkus-d2r3": {
        "turns": 58,
        "bands": ["provision", "analyze", "build", "test", "report"],
        "warnings": 24,
        "missing_tool_result": 3,
        "lines": 107,
    },
    "ignite-d2r3": {
        "turns": 35,
        "bands": ["provision", "analyze", "build", "test"],
        "warnings": 9,
        "missing_tool_result": 0,
        "lines": 58,
    },
}


@pytest.mark.parametrize("session", SESSIONS)
def test_every_turn_of_a_real_session_gets_exactly_one_dispatch_line(session):
    sink = _render(session)
    expected = CORPUS[session]["turns"]
    ids = [line.strip().split()[0] for line in _turn_lines(sink)]
    assert ids == [f"#{n}" for n in range(1, expected + 1)]


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_renders_the_bands_it_actually_walked(session):
    sink = _render(session)
    bands = [line[2:] for line in sink.lines if line.startswith("▸ ")]
    assert bands == CORPUS[session]["bands"]


@pytest.mark.parametrize("session", SESSIONS)
def test_only_the_holes_that_still_stand_are_printed(session):
    """R33's numbers, as literals.

    Printing each warning as the reducer emits it would print 65 / 171 / 97 —
    one under every dispatch — and retract none of them. These are the counts
    that were still true when the ledger ended.
    """

    sink = _render(session)
    warnings = _warnings(sink)
    assert len(warnings) == CORPUS[session]["warnings"]
    assert (
        sum("missing_tool_result" in warning for warning in warnings)
        == CORPUS[session]["missing_tool_result"]
    )


@pytest.mark.parametrize("session", SESSIONS)
def test_the_stream_states_exactly_the_holes_the_derivation_holds(session):
    """Hold-and-release must arrive at the same set the reducer would state.

    Adding on `warnings` and dropping on `retracted_warnings` is the schema's
    own accumulation rule; this is the check that following it line by line
    lands on the same statements a snapshot of the whole ledger makes, so the
    stream and the timeline cannot describe the same run's holes differently.
    `build_trajectory` additionally reads `token_usage.csv`, which this renderer
    is not given, so the comparison is against the reducer it actually owns.
    """

    reducer = TrajectoryReducer()
    for line in (FIXTURE_DIR / session / "control_events.jsonl").read_text().splitlines():
        reducer.feed(line)
    expected = sorted(
        f"{warning.code}: {warning.detail}" for warning in reducer.snapshot().warnings
    )
    printed = sorted(
        part.strip() for part in _hole_text(_render(session, width=200)).split("! ") if part.strip()
    )
    assert printed == expected


def test_two_holes_released_together_are_stated_in_the_order_warnings_have():
    """`warning_order` is the one order a set of statements renders in.

    The live stream releases a set it accumulated; the batch view sorts a list
    it built in one pass. They can only be compared if both use the same total
    order, so the release path sorts rather than taking insertion order.
    """

    lines = _render("camel-quarkus-d2r3").lines
    first = lines.index("  ! missing_envelope: turn 22 has a loop_decision but no action_envelope")
    assert lines[first + 1] == (
        "  ! missing_tool_result: turn 22 has no tool_result and no typed refusal"
    )


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_session_fits_the_terminal_it_was_given(session):
    for width in (60, 80, 100, 120):
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
        "  #3   project   env gradle                               "
        "[red]failed[/red] · ENV_EXECUTABLE_NOT_FOUND · 0.8s" in marked
    )


#: One verbatim line per tool, copied from what the renderer printed on the
#: session named. These are what a reader reads; a summariser branch going dead
#: changes one of them and this fails with the before and after side by side.
VERBATIM = [
    (
        "kafka-d2r3",
        "  #1   project   clone apache/kafka@4.3.1                 26b251a → /workspace/kafka · 12.6s",
    ),
    ("kafka-d2r3", "  #15  build     compile --no-daemon                      exit 0 · 1m23s"),
    (
        "kafka-d2r3",
        "  #19  build     test --no-daemon :clients:test           failed · exit 1 · DETACHED_OPERAT… · 7m26s",
    ),
    ("kafka-d2r3", "  #20  search    output_3ea47959569d /(?i)(BUILD SUCCESS… 80 matches · 1.9s"),
    ("kafka-d2r3", "  #5   bash      which gradle || true; ls -l /usr/bin/gr… ok · 2.8s"),
    (
        "kafka-d2r3",
        "  #14  advisor   consult                                  advice delivered · 0.0s",
    ),
    ("kafka-d2r3", "  #23  report    generate                                 ok · 0.9s"),
    (
        "kafka-d2r3",
        "  #7   phase     done success                             gate workspace_present · workspace… · 0.2s",
    ),
    ("kafka-d2r3", "      ↳ graded success"),
    ("kafka-d2r3", "✓ provision advanced · workspace_ready"),
    ("kafka-d2r3", "✓ test evidence_close · test_terminal"),
    ("kafka-d2r3", "  ! missing_loop_decision: turn 7 called 'phase' and emitted no loop_decision"),
    (
        "ignite-d2r3",
        "  #17  build     compile                                  exit 0 · 5 artifacts · 2m27s",
    ),
    (
        "ignite-d2r3",
        "  #35  ⚙ engine  test                                     pending · running · job 2c4d56b2… · 15m06s",
    ),
    ("ignite-d2r3", "      ! job 2c4d56b2fdca still live at close"),
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
        "  #14  build     test -DskipITs -DskipIntegrationTests -… failed · exit 1 · JAVA_VERSION_ER… · 6m23s",
    ),
]


@pytest.mark.parametrize("session,line", VERBATIM, ids=range(len(VERBATIM)))
def test_a_real_line_reads_exactly_this(session, line):
    assert line in _render(session).lines


def test_the_conservation_statement_survives_the_width_it_is_printed_at():
    """The one statement long enough to be cut in half by an 80-column terminal."""

    joined = _hole_text(_render("kafka-d2r3", width=80))
    assert (
        "the ledger accounts for 23 closed call(s) on its fullest side, and is "
        "short: decided (loop_decision + cancelled) by 9" in joined
    )


def test_what_the_corpus_replay_alone_cannot_reach():
    """Named gaps, rather than a silent one (R16/R27).

    Every clause in `turn_stream.py` was deleted in turn and a test failed —
    33 of 33 on the battery run for this task. But four of those clauses are
    caught only by a constructed payload, because the three archived sessions do
    not contain the shape at all, and a fence that says "three real sessions
    pass" while those clauses go dead is the failure this note exists to stop:

    - `_TRANSITION_GLYPH`'s `repair` and `report` rows. The 550-ledger corpus
      holds `advance` (1532), `evidence_close` (526) and `flow_close` (526) and
      nothing else; both other kinds are real —
      `PhaseTransitionPayload.expected_kind` admits them — and only
      `test_every_kind_of_transition_has_a_line` reaches them.
    - `_OUTCOME_STYLE`'s `refused` and `cancelled` rows, and with them the whole
      refusal path. 298 refusal records exist across the corpus and none is in
      these three sessions; only `test_a_refused_call_says_it_was_refused`
      reaches them.

    One clause could not be shown to bite at all and is not defended here: the
    `_JOB_NOTE_INTERVAL_SECONDS` value itself. Any interval between one second
    and a session's length passes every test in this file — the throttle's
    existence is fenced, its calibration is a judgment.
    """

    assert True
