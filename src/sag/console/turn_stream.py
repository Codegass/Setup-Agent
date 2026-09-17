"""The run, as it happens, one line per turn.

The input is the control ledger and nothing else: this renderer folds raw
event lines through the same `TrajectoryReducer` the Workbench timeline reads,
so the terminal and the browser cannot describe the same run differently. A
console log line fed here produces no output at all, because it is not an
event and this layer has no other source. A TORN event line is a different
thing and is stated, not dropped — see `feed`.

A turn's dispatch line is written without a newline and completed in place when
the turn settles. If anything else reached the terminal in between — a job
progress note, a hole in the ledger, a phase band — the outcome takes a fresh
line carrying its own turn id, so an outcome is never lost to an interleaving
and never read as the outcome of the line above it.

Three things a live renderer has to get right that a batch one does not:

- **A turn in flight is not a hole.** The reducer states `missing_tool_result`
  the moment an envelope lands, because at that moment it is true, and
  withdraws it when the result arrives. Printing each statement as it is made
  would put three exclamations on every turn of a real session and take none of
  them back. Statements are HELD here, added and withdrawn as the reducer says,
  and a statement naming a turn is shown only once THAT TURN has settled on its
  own evidence — its answer landed, or it never had a call to answer. A
  neighbour opening is not evidence about this turn: with two calls in flight
  it would state a hole the derivation is about to withdraw.
- **A hole a reader can act on is not the same as the ledger's own bookkeeping.**
  Two codes — `missing_loop_decision` and `conservation_violation` — are true of
  most turns of most real sessions and say nothing about the run. They are
  counted, and the count is stated once at the end with where to read them in
  full. Every other code is stated where it happens. Nothing leaves the
  derivation; what changes is where it is said.
- **Width is measured against what a reader sees.** Styling is Rich markup, and
  markup is not characters on a screen: a line clipped by the length of its
  tags clips early and can cut a tag in half. Every line is laid out and
  clipped as plain text, and the markup is spliced in afterwards.
"""

from __future__ import annotations

import json
import re
import textwrap
import time
from collections import Counter
from typing import Callable, Iterable

from sag.trajectory.reducer import UNKNOWN_PHASE, TrajectoryReducer, elapsed
from sag.trajectory.schema import (
    SUMMARY_MAX_CHARS,
    TrajectoryDelta,
    Turn,
    Warning,
    warning_order,
)

#: How much of the line the tool's name gets. Public because the turn stream and
#: anything that lines up beneath it have to agree on one column.
TOOL_WIDTH = 9
_ID_WIDTH = 5
#: The narrowest the call column is ever squeezed to, and the room the outcome
#: keeps before the call is allowed to grow into what is left. Above that floor
#: the call takes the terminal's whole surplus, up to the longest line the
#: derivation will ever hand it, and anything beyond that goes back to the
#: outcome. Five real camel-quarkus turns share the first 46 characters of their
#: command and differ by the 57th: a column that stops at 40 renders five
#: different things as one string on a terminal that had the room to tell them
#: apart.
_MIN_SUMMARY_WIDTH = 20
_OUTCOME_ROOM = 38
#: Blank columns between a call and what came back. Two, not one: a call clipped
#: to the full width of its column would otherwise sit one space from the
#: outcome and read as a single run-on string.
_GUTTER = 2
#: The column a short call's outcome still lines up at. A fixed column the whole
#: width of the terminal freezes the outcome's room and pads ten-character calls
#: across forty blanks; no column at all makes every row ragged and there is
#: nothing left to scan down. This is the floor: calls shorter than it align,
#: longer ones run on and take the space with them.
_ALIGN_CALL = 26
_MIN_WIDTH = 60
_JOB_NOTE_INTERVAL_SECONDS = 60.0
_ENGINE_ACTOR = "⚙ engine"
_NO_CALL = "no call"
_INDENT = "  "
#: Everything before the call column: indent, turn id, tool name and its gutter.
_CHROME = len(_INDENT) + _ID_WIDTH + TOOL_WIDTH + 1
_NOTE = "      … "
_HOLE = "  ! "
#: Wrapped hole text hangs four columns in, not under its own code: a 28-column
#: hanging indent eats a third of an 80-column line.
_HOLE_HANG = "    "
#: The word a gate delivered, marked as the gate's own answer. The reason code
#: it sits beside used to be labelled `gate <code>` too; that is fixed at its
#: source now (`summaries._phase_observation`), so nothing here has to take a
#: label off a neighbour's string.
_GATE = "gate:"
#: Where to read the counted notes in full. The format is named: the command's
#: own default prints this very stream, so a bare `sag trajectory <session>`
#: would answer the note by repeating it.
_TRAJECTORY_COMMAND = "uv run sag trajectory <session> --format json"

#: Codes that are true of most turns of most healthy sessions. They are the
#: ledger's bookkeeping about itself, not something a reader can act on, and
#: inline they are 10 of kafka's 11 statements. Counted, and stated once at the
#: close with where to read them whole.
_ACCOUNTING_CODES = frozenset({"missing_loop_decision", "conservation_violation"})

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

#: What Rich reads as a style tag, and a bracket with whatever backslashes run
#: up to it. `_escape` needs to tell the two apart because Rich's parser does.
_TAG_SHAPED = re.compile(r"\[[a-z#/@][^\[]*?]")
_BRACKET_RUN = re.compile(r"(\\*)(\[)")

_BAND_STYLE = "bold"
_HOLE_STYLE = "yellow"
_JOB_STYLE = "dim"
_REPAIR_STYLE = "yellow"
_CLOSE_STYLE = "green"


def _duration(turn: Turn) -> str | None:
    """How long the turn took, or nothing when the ledger has not said yet."""

    seconds = elapsed(turn.t0, turn.t1) if turn.t0 and turn.t1 else None
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _clip(text: str, width: int) -> str:
    """Cut to `width`, marking the cut, and never on a dangling separator.

    A clip that lands right after a ` · ` renders `gate build_partial ·…`, which
    reads as a separator with nothing on the far side of it. The separator is
    dropped with the text it was separating.
    """

    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1].rstrip().rstrip("·").rstrip() + "…"


def _escape(text: str) -> str:
    r"""Keep a bracket, or a backslash before one, from being eaten as markup.

    Rich's parser has two rules and `rich.markup.escape` implements one of them.
    A TAG-shaped bracket — `[` then one of `a-z # / @` — is literal when an ODD
    number of backslashes precedes it, and the run is halved. Every OTHER `\[`
    simply loses its backslash. So `rich.markup.escape` leaves
    `\[INFO\] Building` untouched and the sink prints `[INFO\] Building`: two
    characters gone from a regex the reader would copy, which is exactly what
    this function exists to prevent. ignite `#18` carries that pattern.

    Both rules, applied to the text as it will be read: a run of N backslashes
    before a tag-shaped bracket becomes 2N+1, and before any other bracket, N+1.
    Verified to round-trip through a real `rich.console.Console` for every call
    and observation summary in all four archived sessions, and for the awkward
    shapes around them (`[/]`, `[]`, a trailing backslash, `\\\[`).
    """

    def _one(match: re.Match[str]) -> str:
        backslashes, bracket = match.group(1), match.group(2)
        tagged = _TAG_SHAPED.match(text[match.end(2) - 1 :]) is not None
        return (backslashes * 2 if tagged else backslashes) + "\\" + bracket

    return _BRACKET_RUN.sub(_one, text)


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
        """A copy holding only what fits, carrying the styles that survived.

        The surviving span is clamped to the text that is left. A span keeping
        the end it had before the cut would reach past it, and since a truncated
        line is then EXTENDED — `_settle` appends the turn's timing to it — that
        reach would silently paint the appended characters.
        """

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
            # A span wholly past the cut is dropped rather than emitted empty:
            # `[red][/red]` is markup a reader's terminal has to parse for
            # nothing, and a pair of them is how a stray tag gets noticed.
            if start >= len(visible):
                break
            out.append(_escape(visible[cursor:start]))
            out.append(f"[{style}]{_escape(visible[start:end])}[/{style}]")
            cursor = end
        out.append(_escape(visible[cursor:]))
        return "".join(out)


class TurnStreamRenderer:
    """Folds control-event lines into terminal lines. Writes; never reads files.

    One renderer renders one session. `close()` is terminal: a `feed()` after it
    raises rather than quietly continuing the finished run into a new one.
    """

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
            SUMMARY_MAX_CHARS,
            max(_MIN_SUMMARY_WIDTH, self._width - _CHROME - _GUTTER - _OUTCOME_ROOM),
        )
        self._phase: str | None = None
        self._printed: set[int] = set()
        #: What each turn has already said, so a restatement that carries
        #: nothing new says nothing. The reducer restates a settled turn on
        #: every event that touches it — three or four times each — and a
        #: renderer that prints each restatement prints the run four times.
        self._said: set[int] = set()
        self._gated: set[int] = set()
        #: Turns that have settled on their OWN evidence, which is the only
        #: thing that makes a statement about them safe to print.
        self._settled: set[int] = set()
        self._open_turn: int | None = None
        self._open_cost = 0
        self._line_open = False
        #: A turn whose first line is held back because the ledger has not said
        #: what phase the run is in. It is placed the moment a phase is stated,
        #: or under the band already open if anything else needs the terminal
        #: first.
        self._pending: Turn | None = None
        #: Statements currently true about the ledger, and the ones already
        #: shown. A statement is shown at most once and only while it stands.
        self._held: dict[Warning, None] = {}
        self._shown: set[Warning] = set()
        self._job_notes: dict[str, float] = {}
        self._closed = False

    # -- writing -------------------------------------------------------

    def _emit(self, line: _Line) -> None:
        self._flush()
        self._close_line()
        self._write(line.render(self._width, self._tty) + "\n")

    def _open_line(self, line: _Line) -> None:
        self._flush()
        self._close_line()
        # Measured on the text, never on the markup: the escape Rich needs adds
        # characters a reader never sees, and an outcome column placed by them
        # lands in a different place on every line that held a bracket.
        self._open_cost = len(_clip(line.text, self._width))
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

    @staticmethod
    def _answered(turn: Turn) -> bool:
        """Has the ledger said how this turn came out?

        An `ObservationInfo` can exist with every field unset — another event
        put a shell there — and that is not an answer. `pending` IS one: a
        dispatched job with no exit yet is a stated outcome.
        """

        observation = turn.observation
        return observation is not None and observation.outcome is not None

    def _outcome_at(self, used: int) -> int:
        """The column this line's outcome starts in, given the head it has."""

        floor = _CHROME + min(_ALIGN_CALL, self._summary_width) + _GUTTER
        return max(floor, used + _GUTTER)

    def _outcome_line(self, turn: Turn, *, gate: bool) -> _Line:
        """What the turn's settling says, as one styled line's worth of text.

        `gate` asks for the word a gate delivered to be part of it, which it is
        only when the grading arrived before this line was written.
        """

        observation = turn.observation
        outcome = observation.outcome if observation is not None else None
        summary = observation.summary if observation is not None else None
        word = None if outcome == _UNMARKED_OUTCOME and summary else outcome
        gate_word = turn.gate.word if (gate and turn.gate is not None) else None
        line = _Line()
        if word is not None:
            line.add(word, _OUTCOME_STYLE.get(word))
        if summary:
            line.add(" · " if line.text else "").add(summary)
        if gate_word:
            line.add(" · " if line.text else "").add(f"{_GATE} {gate_word}")
        return line

    def _settle(self, turn: Turn, used: int) -> _Line | None:
        """What the turn's settling adds to a line already `used` columns long.

        The outcome starts where the call ENDED, not at a fixed column: a
        ten-character call followed by forty blanks and then a reason code cut
        in half is the worst thing this layout can do, because the cut string is
        the one a reader greps for. The timing goes flush to the right edge,
        where it stays a column to scan down and cannot be what gets clipped.
        """

        # Only an ANSWER finishes a turn's line. A gate that arrived first
        # would otherwise complete the line on its own and the outcome, landing
        # afterwards, would find the turn already spoken for and be dropped.
        if not self._answered(turn):
            return None
        line = self._outcome_line(turn, gate=turn.turn_id not in self._gated)
        room = self._width - used
        duration = _duration(turn)
        if duration is None:
            # Nothing to hold back and every caller clips what it writes, so a
            # second clip here would have nothing to do.
            return line
        body = line.truncated(max(0, room - len(duration) - _GUTTER))
        return body.add(" " * max(_GUTTER, room - len(body.text) - len(duration)) + duration)

    def render_turn(self, turn: Turn) -> None:
        """Write whatever this turn now says that it had not said before.

        Public since R6, and a public entry point is one a caller can reach
        after `close()` — so it refuses there for the same reason `feed` does.
        """

        self._refuse_when_closed()
        if self._answered(turn):
            # The ONE thing that settles a turn: the ledger stated how it came
            # out. "It has no call" is not a second way — a `turn_record` seals
            # a call-less turn and retracts both holes standing on it, and 272
            # of the 275 archived sessions carry `turn_record`. A statement
            # about a turn the ledger has not answered waits for the close,
            # where a retraction can still have reached it first.
            self._settled.add(turn.turn_id)

        # Another turn's news means the held one has waited long enough: place it
        # first, or it is overwritten and its dispatch line never printed at all.
        if self._pending is not None and self._pending.turn_id != turn.turn_id:
            self._flush()

        if turn.turn_id not in self._printed:
            self._printed.add(turn.turn_id)
            if turn.phase == UNKNOWN_PHASE and not self._answered(turn):
                # The ledger has not placed the run yet. Hold the line rather
                # than print it above the band it turns out to belong to. A
                # turn that arrives already answered is never held: there is no
                # live dispatch left to show and its band is knowable now.
                self._pending = turn
                return
            self._dispatch(turn)
            return

        if self._pending is not None and self._pending.turn_id == turn.turn_id:
            if turn.phase == UNKNOWN_PHASE and not self._answered(turn):
                return
            self._pending = None
            self._dispatch(turn)
            return

        if turn.turn_id not in self._said:
            settled = self._settle(turn, self._outcome_at(self._open_cost))
            if settled is not None:
                self._said.add(turn.turn_id)
                if turn.gate is not None:
                    self._gated.add(turn.turn_id)
                self._complete(turn, settled)
        elif turn.gate is not None and turn.turn_id not in self._gated:
            self._gated.add(turn.turn_id)
            self._emit(_Line(self._continuation(turn.turn_id)).add(f"{_GATE} {turn.gate.word}"))
        self._band(turn)

    def _dispatch(self, turn: Turn) -> None:
        """Open the turn's band if it opens one, then write its first line."""

        self._band(turn)
        head = self._head(turn)
        settled = self._settle(turn, self._outcome_at(len(head.text)))
        if settled is None:
            self._open_line(head)
            self._open_turn = turn.turn_id
            return
        self._said.add(turn.turn_id)
        if turn.gate is not None:
            # A held line defers the write past events that have already
            # happened, so a turn dispatched late can arrive with its grading
            # already in hand and on this very line. Not recording it here says
            # it a second time on the next restatement. Event order is not
            # render order, and this clause is about render order.
            self._gated.add(turn.turn_id)
        self._emit(
            head.add(" " * (self._outcome_at(len(head.text)) - len(head.text))).extend(settled)
        )

    def _flush(self) -> None:
        """Place a held first line now, under whatever band the run is in.

        Anything else reaching the terminal would otherwise be printed above a
        turn that was dispatched before it. The band it goes under is the one
        already open, which is the fallback for a phase that never resolves.
        """

        # `_pending` is cleared BEFORE the dispatch, so the `_emit` inside it
        # re-enters here and finds nothing to do. That is the whole re-entry
        # guard; a flag as well would be a second one that can never fire.
        pending, self._pending = self._pending, None
        if pending is not None:
            self._dispatch(pending)

    @staticmethod
    def _continuation(turn_id: int) -> str:
        """The marker an out-of-line outcome carries, naming its own turn.

        Anything printed between a dispatch and its answer pushes the answer
        down here, where without the id it reads as the outcome of whatever line
        happens to be above it.
        """

        return f"      ↳ #{turn_id} "

    def _complete(self, turn: Turn, settled: _Line) -> None:
        """Finish the turn's line in place, or give the outcome its own line."""

        if self._open_turn == turn.turn_id and self._line_open:
            gutter = _Line(" " * (self._outcome_at(self._open_cost) - self._open_cost))
            self._write(gutter.extend(settled).render(self._width - self._open_cost, self._tty))
            self._close_line()
            return
        marker = self._continuation(turn.turn_id)
        self._emit(_Line(marker).extend(self._settle(turn, len(marker)) or _Line()))

    def _band(self, turn: Turn) -> None:
        """Open a phase band when the run enters one.

        A band is never opened for `unknown`: that is the reducer saying the
        ledger has not placed the run yet, not the name of a phase. A turn
        dispatched before any phase is stated is held (see `render_turn`) so
        that its band can be printed above it rather than under it.
        """

        if not turn.phase or turn.phase == UNKNOWN_PHASE or turn.phase == self._phase:
            return
        self._phase = turn.phase
        self._emit(_Line().add(f"▸ {turn.phase}", _BAND_STYLE))

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
        self._emit(_Line().add(text, _REPAIR_STYLE if kind == "repair" else _CLOSE_STYLE))

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
        self._emit(_Line().add(_NOTE + " · ".join(parts), _JOB_STYLE))

    def _note_live_job(self, payload: dict) -> None:
        """A job the run walked away from. Named when the payload names it."""

        job = str(payload.get("job_id") or "")
        what = f"job {job}" if job else "a job"
        self._emit(_Line().add(f"      ! {what} still live at close", _HOLE_STYLE))

    # -- the ledger's own statements -----------------------------------

    def _refuse_when_closed(self) -> None:
        if self._closed:
            raise RuntimeError("this renderer is closed; one renderer renders one session")

    def _hold(self, added: Iterable[Warning], withdrawn: Iterable[Warning]) -> None:
        for warning in added:
            self._held[warning] = None
        for warning in withdrawn:
            self._held.pop(warning, None)

    def _release(self) -> None:
        """Show every held statement that is safe to show and worth showing.

        Safe means the thing it says can no longer stop being true: a statement
        about a turn waits for that turn to settle on its own evidence, and one
        about a line or about the run as a whole is a fact about something that
        already happened. Worth showing leaves out the two accounting codes,
        which are counted instead and stated once at the close.
        """

        for warning in sorted(self._held, key=warning_order):
            if warning.code in _ACCOUNTING_CODES:
                continue
            if warning.turn_id is None or warning.turn_id in self._settled:
                self._show(warning)

    def _show(self, warning: Warning) -> None:
        if warning in self._shown:
            return
        self._shown.add(warning)
        self._state(f"{warning.code}: {warning.detail}")

    def _state(self, text: str) -> None:
        """State one hole, whole.

        This is the one line in the stream that WRAPS instead of clipping. Every
        other line has a shape a reader can predict, so a clipped one still says
        which turn it is about and roughly what happened; a hole is nothing but
        its explanation, and half of one is not a shorter statement, it is a
        different one.
        """

        body = textwrap.wrap(
            text,
            width=self._width,
            initial_indent=_HOLE,
            subsequent_indent=_HOLE_HANG,
        ) or [_HOLE.rstrip()]
        for line in body:
            self._emit(_Line().add(line, _HOLE_STYLE))

    def _tally(self) -> None:
        """The two accounting codes, counted, with where to read them in full."""

        counts = Counter(
            warning.code for warning in self._held if warning.code in _ACCOUNTING_CODES
        )
        total = sum(counts.values())
        if not total:
            return
        named = ", ".join(f"{code} ×{count}" for code, count in sorted(counts.items()))
        self._state(
            f"{total} more note{'' if total == 1 else 's'} about the ledger itself: "
            f"{named} — read them in full with: {_TRAJECTORY_COMMAND}"
        )

    def feed(self, raw_line: str) -> None:
        """Fold one control-event line and write whatever it produced.

        A line that does not even begin as a JSON object is not an event and is
        not input: the terminal this renders to is the one the old log firehose
        used, and answering a stray `Executing command in container` line with a
        warning about it would have taken the log back in through the door this
        layer closed. A line that DOES begin as one and then fails to parse is a
        torn event — the normal result of a process killed mid-write — and goes
        to the reducer, which states it.
        """

        self._refuse_when_closed()
        if not raw_line.lstrip().startswith("{"):
            return
        delta = self._reducer.feed(raw_line)
        event = self._event(raw_line)
        if event is not None:
            kind, body = event
            if kind == "phase_transition":
                self._note_transition(body)
            elif kind == "job_barrier_wait":
                self._note_job(body)
            elif kind == "job_live_at_close":
                self._note_live_job(body)
        self.render_delta(delta)

    def render_delta(self, delta: TrajectoryDelta) -> None:
        """Write what one delta changed: its turns, then its statements.

        A live run hands this renderer raw lines and it folds them itself. A
        reader replaying or tailing a recorded session has already folded
        them — `follow_trajectory` yields whole deltas, including the one only
        the end of a follow can produce — so both arrive here and leave by the
        same two steps. Turns first, because a statement about a turn is held
        until that turn has settled; the statements after, so a hole a follow
        reports is shown on exactly the terms a replay shows it.
        """

        self._refuse_when_closed()
        for turn in delta.turns:
            self.render_turn(turn)
        self._hold(delta.warnings, delta.retracted_warnings)
        self._release()

    @staticmethod
    def _event(raw_line: str) -> tuple[str, dict] | None:
        """The line's kind and payload, or nothing when it is not readable."""

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
        # A payload that is not a mapping is no payload: the note handlers read
        # keys off it, and `None.get` is a crash in the middle of a live run.
        return kind, body if isinstance(body, dict) else {}

    def close(self) -> None:
        """Place anything held, state every hole left standing, and finish."""

        if self._closed:
            return
        self._flush()
        self._close_line()
        for warning in sorted(self._held, key=warning_order):
            if warning.code not in _ACCOUNTING_CODES:
                self._show(warning)
        self._tally()
        self._close_line()
        self._closed = True


__all__ = ["TurnStreamRenderer", "TOOL_WIDTH"]
