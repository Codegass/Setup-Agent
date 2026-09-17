"""The run, as it happens, one line per turn.

The input is the control ledger and nothing else: this renderer folds raw
event lines through the same `TrajectoryReducer` the Workbench timeline reads,
so the terminal and the browser cannot describe the same run differently. A
console log line fed here produces no output at all, because it is not an
event and this layer has no other source.

A turn's dispatch line is written without a newline and completed in place when
the turn settles. If anything else reached the terminal in between — a job
progress note, a ledger warning, a phase band — the outcome takes a fresh
indented line instead, so no outcome is ever lost to an interleaving.

Two things a live renderer has to get right that a batch one does not:

- **A turn in flight is not a hole.** The reducer states `missing_tool_result`
  the moment an envelope lands, because at that moment it is true, and
  withdraws it when the result arrives. Printing each statement as it is made
  would put three exclamations on every turn of a real session and take none of
  them back. Statements are HELD here, added and withdrawn as the reducer says,
  and printed only once the run has moved past the turn they name — or, for a
  statement about the run as a whole, at `close()`.
- **Width is measured against what a reader sees.** Styling is Rich markup, and
  markup is not characters on a screen: a line clipped by the length of its
  tags clips early and can cut a tag in half. Every line is laid out and
  clipped as plain text, and the markup is spliced in afterwards.
"""

from __future__ import annotations

import json
import textwrap
import time
from typing import Callable, Iterable

from rich.markup import escape as rich_escape

from sag.trajectory.reducer import UNKNOWN_PHASE, TrajectoryReducer, elapsed
from sag.trajectory.schema import Turn, Warning, warning_order

#: How much of the line the tool's name gets. Public because the turn stream and
#: anything that lines up beneath it have to agree on one column.
TOOL_WIDTH = 9
_ID_WIDTH = 5
#: The widest the call column ever gets. It is a CAP, not a fixed column: on a
#: narrow terminal the call yields so that what came back still has room, since
#: a line that says what was asked and clips the answer has said nothing.
_SUMMARY_WIDTH = 40
_MIN_SUMMARY_WIDTH = 20
#: What the outcome is guaranteed, before the call column is allowed to grow.
_OUTCOME_ROOM = 38
_MIN_WIDTH = 60
_JOB_NOTE_INTERVAL_SECONDS = 60.0
_ENGINE_ACTOR = "⚙ engine"
_NO_CALL = "no call"
_INDENT = "  "
#: Everything before the call column: indent, turn id, tool name and its gutter.
_CHROME = len(_INDENT) + _ID_WIDTH + TOOL_WIDTH + 1
_CONTINUATION = "      ↳ "
_NOTE = "      … "
#: The word a gate delivered, said in a way a reader cannot confuse with the
#: gate's own name. A phase result already reads `gate analysis_green · …`,
#: which is what the gate was; this is what it decided.
_GRADED = "graded"

_OUTCOME_STYLE = {
    "ok": "green",
    "failed": "red",
    "refused": "yellow",
    "cancelled": "yellow",
    "pending": "cyan",
}

#: `ok` is the unmarked case. When the derivation has a line for what came back,
#: that line already says it came back — `ok · exit 0 · 994 tests` says "fine"
#: twice and spends five columns doing it. Every other word is printed always,
#: because a reader scanning a stream is scanning for exactly those.
_UNMARKED_OUTCOME = "ok"

#: Two of these five are real but unarchived: the corpus holds only `advance`
#: (1532), `evidence_close` (526) and `flow_close` (526), while
#: `PhaseTransitionPayload.expected_kind` also admits `repair` and `report`.
_TRANSITION_GLYPH = {
    "advance": "✓",
    "evidence_close": "✓",
    "report": "✓",
    "flow_close": "✓",
    "repair": "→",
}


def _duration(turn: Turn) -> str | None:
    """How long the turn took, or nothing when the ledger has not said yet."""

    seconds = elapsed(turn.t0, turn.t1) if turn.t0 and turn.t1 else None
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if width == 1:
        return text[:1]
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _escape(text: str) -> str:
    r"""Keep a bracket a reader typed from being read as a style tag.

    Real calls carry brackets — a search pattern, a `[INFO]` prefix in an error
    line, a Java generic. Rich would eat them and the reader would never know a
    character had gone missing. Rich's own escape is used rather than a local
    one: the rule is "what Rich would have read as a tag", and only Rich knows
    it — a blanket backslash-doubling corrupts the `\s` of every search pattern
    in the corpus.
    """

    return rich_escape(text)


class _Line:
    """Plain text plus the styles to paint on ranges of it.

    Layout and clipping happen on `text` — what the terminal actually shows —
    and the markup is spliced in only at render time, so a style can never
    spend the budget a reader's characters need and a clip can never cut a tag
    in half.
    """

    __slots__ = ("_text", "_spans")

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._spans: list[tuple[int, int, str]] = []

    def add(self, chunk: str, style: str | None = None) -> "_Line":
        start = len(self._text)
        self._text += chunk
        if style and chunk:
            self._spans.append((start, len(self._text), style))
        return self

    def extend(self, other: "_Line") -> "_Line":
        offset = len(self._text)
        self._text += other._text
        self._spans.extend((s + offset, e + offset, style) for s, e, style in other._spans)
        return self

    def truncated(self, width: int) -> "_Line":
        """A copy holding only what fits, carrying the styles that survived."""

        visible = _clip(self._text, width)
        out = _Line(visible)
        for start, end, style in self._spans:
            if start >= len(visible):
                break
            out._spans.append((start, min(end, len(visible)), style))
        return out

    @property
    def text(self) -> str:
        return self._text

    def render(self, width: int, tty: bool) -> str:
        visible = _clip(self._text, width)
        if not tty:
            return visible
        out: list[str] = []
        cursor = 0
        for start, end, style in self._spans:
            if start >= len(visible):
                break
            stop = min(end, len(visible))
            out.append(_escape(visible[cursor:start]))
            out.append(f"[{style}]{_escape(visible[start:stop])}[/{style}]")
            cursor = stop
        out.append(_escape(visible[cursor:]))
        return "".join(out)


class TurnStreamRenderer:
    """Folds control-event lines into terminal lines. Writes; never reads files."""

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        width: int = 100,
        tty: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._write = write
        self._width = max(_MIN_WIDTH, width)
        self._tty = tty
        self._clock = clock or time.monotonic
        self._reducer = TrajectoryReducer()
        self._summary_width = min(
            _SUMMARY_WIDTH,
            max(_MIN_SUMMARY_WIDTH, self._width - _CHROME - 1 - _OUTCOME_ROOM),
        )
        #: Where the head of a turn line ends and its outcome begins.
        self._tail_at = _CHROME + self._summary_width + 1
        self._phase: str | None = None
        self._printed: set[int] = set()
        #: What each turn has already said, so a restatement that carries
        #: nothing new says nothing. The reducer restates a settled turn on
        #: every event that touches it — three or four times each — and a
        #: renderer that prints each restatement prints the run four times.
        self._said: set[int] = set()
        self._gated: set[int] = set()
        self._open_turn: int | None = None
        self._open_cost = 0
        self._line_open = False
        #: Statements currently true about the ledger, and the ones already
        #: shown. A statement is shown at most once and only while it stands.
        self._held: dict[Warning, None] = {}
        self._shown: set[Warning] = set()
        self._high_turn = 0
        self._job_notes: dict[str, float] = {}

    # -- writing -------------------------------------------------------

    def _emit(self, line: _Line | str) -> None:
        if isinstance(line, str):
            line = _Line(line)
        self._close_line()
        self._write(line.render(self._width, self._tty) + "\n")

    def _open_line(self, line: _Line) -> None:
        self._close_line()
        rendered = _clip(line.text, self._width)
        self._open_cost = len(rendered)
        self._write(line.render(self._width, self._tty))
        self._line_open = True

    def _close_line(self) -> None:
        if self._line_open:
            self._write("\n")
            self._line_open = False
            self._open_turn = None
            self._open_cost = 0

    # -- turns ---------------------------------------------------------

    def _head(self, turn: Turn) -> _Line:
        actor = (
            _ENGINE_ACTOR if turn.actor == "controller" else (turn.call.tool if turn.call else "—")
        )
        summary = (turn.call.summary if turn.call else None) or (
            _NO_CALL if turn.call is None else turn.call.tool
        )
        # Unpadded: a turn still in flight leaves its line open, and padding it
        # out to the outcome column would leave a row of trailing blanks on the
        # screen of anyone whose turn never came back.
        return _Line(
            f"{_INDENT}{('#' + str(turn.turn_id)):<{_ID_WIDTH}}"
            f"{_clip(actor, TOOL_WIDTH):<{TOOL_WIDTH + 1}}"
            f"{_clip(summary, self._summary_width)}"
        )

    def _padded(self, head: _Line) -> _Line:
        """The head carried out to the column every outcome starts in.

        Padding the `_Line` rather than its text keeps any style the head
        carries; it carries none today, and a renderer that silently dropped
        one the day it did would be a hard thing to notice.
        """

        return head.add(" " * max(1, self._tail_at - len(head.text)))

    def _outcome_line(self, turn: Turn, *, gate: bool) -> _Line | None:
        """What the turn's settling says, as one styled line's worth of text.

        `gate` asks for the word a gate delivered to be part of it, which it is
        only when the grading arrived before this line was written.
        """

        observation = turn.observation
        outcome = observation.outcome if observation is not None else None
        summary = observation.summary if observation is not None else None
        word = None if outcome == _UNMARKED_OUTCOME and summary else outcome
        gate_word = turn.gate.word if (gate and turn.gate is not None) else None
        if word is None and summary is None and gate_word is None:
            return None
        line = _Line()
        if word is not None:
            line.add(word, _OUTCOME_STYLE.get(word))
        if summary:
            line.add(" · " if line.text else "").add(summary)
        if gate_word:
            line.add(" · " if line.text else "").add(f"{_GRADED} {gate_word}")
        return line

    def _settle(self, turn: Turn, room: int) -> _Line | None:
        """The outcome, with the turn's timing reserved out of its room.

        The timing is held back rather than left to take its chances at the end
        of a long line: a clipped summary still says most of what it knew, and a
        clipped duration says nothing at all.
        """

        line = self._outcome_line(turn, gate=turn.turn_id not in self._gated)
        if line is None:
            return None
        duration = _duration(turn)
        if duration is None:
            return line.truncated(room)
        tail = f" · {duration}"
        return line.truncated(max(0, room - len(tail))).add(tail)

    def render_turn(self, turn: Turn) -> None:
        """Write whatever this turn now says that it had not said before."""

        if turn.turn_id not in self._printed:
            self._band(turn)
            self._printed.add(turn.turn_id)
            self._high_turn = max(self._high_turn, turn.turn_id)
            head = self._head(turn)
            settled = self._settle(turn, self._width - self._tail_at)
            if settled is None:
                self._open_line(head)
                self._open_turn = turn.turn_id
                return
            self._said.add(turn.turn_id)
            if turn.gate is not None:
                self._gated.add(turn.turn_id)
            self._emit(self._padded(head).extend(settled))
            return

        if turn.turn_id not in self._said:
            settled = self._settle(turn, self._width - self._tail_at)
            if settled is not None:
                self._said.add(turn.turn_id)
                if turn.gate is not None:
                    self._gated.add(turn.turn_id)
                self._complete(turn, settled)
        elif turn.gate is not None and turn.turn_id not in self._gated:
            self._gated.add(turn.turn_id)
            self._emit(_Line(_CONTINUATION).add(f"{_GRADED} {turn.gate.word}"))
        self._band(turn)

    def _complete(self, turn: Turn, settled: _Line) -> None:
        """Finish the turn's line in place, or give the outcome its own line."""

        if self._open_turn == turn.turn_id and self._line_open:
            pad = " " * max(1, self._tail_at - self._open_cost)
            room = self._width - self._open_cost - len(pad)
            self._write(_Line(pad).extend(settled.truncated(room)).render(self._width, self._tty))
            self._close_line()
            return
        room = self._width - len(_CONTINUATION)
        self._emit(_Line(_CONTINUATION).extend(settled.truncated(room)))

    def _band(self, turn: Turn) -> None:
        """Open a phase band when the run enters one.

        A band is never opened for `unknown`: that is the reducer saying the
        ledger has not placed the run yet, not the name of a phase. The first
        turn of a run is dispatched before anything states a phase, so its band
        opens once its own result names one — after that turn's line, which is
        the only place a live stream can honestly put it.
        """

        if not turn.phase or turn.phase == UNKNOWN_PHASE or turn.phase == self._phase:
            return
        self._phase = turn.phase
        self._emit(f"▸ {turn.phase}")

    # -- events --------------------------------------------------------

    def _note_transition(self, payload: dict) -> None:
        kind = str(payload.get("expected_kind") or "")
        target = payload.get("expected_target")
        reason = payload.get("expected_reason_code")
        glyph = _TRANSITION_GLYPH.get(kind, "·")
        name = self._phase or target or "phase"
        if kind == "repair":
            text = f"{glyph} {target or name} repair"
        elif kind == "advance":
            text = f"{glyph} {name} advanced"
        else:
            text = f"{glyph} {name} {kind}"
        if reason:
            text = f"{text} · {reason}"
        self._emit(text)

    def _note_job(self, payload: dict) -> None:
        job = str(payload.get("job_id") or "")
        now = self._clock()
        last = self._job_notes.get(job)
        if last is not None and now - last < _JOB_NOTE_INTERVAL_SECONDS:
            return
        self._job_notes[job] = now
        waited = payload.get("waited_seconds")
        parts = ["still running"]
        if isinstance(waited, (int, float)):
            parts.append(f"{int(waited) // 60}m{int(waited) % 60:02d}s")
        size = payload.get("log_size")
        if isinstance(size, int) and size > 0:
            parts.append(f"log {size / 1_048_576:.1f} MB")
        if payload.get("progressing") is True:
            parts.append("progressing")
        self._emit(_NOTE + " · ".join(parts))

    # -- the ledger's own statements -----------------------------------

    def _hold(self, added: Iterable[Warning], withdrawn: Iterable[Warning]) -> None:
        for warning in added:
            self._held[warning] = None
        for warning in withdrawn:
            self._held.pop(warning, None)

    def _release(self, high: int) -> None:
        """Show every held statement about a turn the run has already left.

        A statement about the OPEN turn is withheld, not suppressed: it says
        the turn has no result yet, which is true of every turn between its
        dispatch and its answer. Once a later turn has opened, the same
        statement is a hole and is stated.
        """

        for warning in sorted(self._held, key=warning_order):
            if warning.turn_id is not None and warning.turn_id < high:
                self._show(warning)

    def _show(self, warning: Warning) -> None:
        """State one hole, whole.

        This is the one line in the stream that WRAPS instead of clipping. Every
        other line has a shape a reader can predict, so a clipped one still says
        which turn it is about and roughly what happened; a hole is nothing but
        its explanation, and half of one is not a shorter statement, it is a
        different one.
        """

        if warning in self._shown:
            return
        self._shown.add(warning)
        head = f"{_INDENT}! {warning.code}: "
        body = textwrap.wrap(
            warning.detail,
            width=self._width,
            initial_indent=head,
            subsequent_indent=" " * len(head),
        ) or [head.rstrip()]
        for line in body:
            self._emit(_Line(line))

    def feed(self, raw_line: str) -> None:
        """Fold one control-event line and write whatever it produced.

        A line that is not an event is not input. The terminal this renders to
        is the same one the old log firehose used, and a stream that answered a
        stray `Executing command in container` line with a warning about it
        would have taken the log back in through the door this layer closed.
        Only a JSON object naming a `kind` reaches the reducer.
        """

        event = self._event(raw_line)
        if event is None:
            return
        kind, body = event
        delta = self._reducer.feed(raw_line)
        if kind == "phase_transition":
            self._note_transition(body)
        elif kind == "job_barrier_wait":
            self._note_job(body)
        elif kind == "job_live_at_close":
            self._emit(f"      ! job {body.get('job_id')} still live at close")
        self._release(max([self._high_turn, *(t.turn_id for t in delta.turns)], default=0))
        for turn in delta.turns:
            self.render_turn(turn)
        self._hold(delta.warnings, delta.retracted_warnings)

    @staticmethod
    def _event(raw_line: str) -> tuple[str, dict] | None:
        """The line's kind and payload, or nothing when it is not an event."""

        try:
            event = json.loads(raw_line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        body = event.get("payload")
        return kind, body if isinstance(body, dict) else {}

    def close(self) -> None:
        """Flush the open line and state every hole the ledger left standing."""

        self._close_line()
        for warning in sorted(self._held, key=warning_order):
            self._show(warning)
        self._close_line()


__all__ = ["TurnStreamRenderer", "TOOL_WIDTH"]
