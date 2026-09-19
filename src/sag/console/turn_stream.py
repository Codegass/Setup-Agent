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

**When the outcome appears.** Not when the tool answered: when the engine wrote
the turn down. The engine states its own start and end for every turn it takes,
and the trajectory, the written report and the browser all state that span — so
a stream timing the gap between two events instead would put a different number
beside the same turn. On the archived commons-cli run the record lands 60 ms to
3.4 s after the answer (3.4 s of it on a two-minute build), and that wait is the
whole price of the four surfaces agreeing on one number per turn. A ledger that
records nothing — the three sessions that predate the engine's per-turn
record — keeps its outcome only until the next thing needs the terminal: a
later turn's first line, a band, a job note, a log sink calling `give_way()`,
or the close. Nothing is ever held past `close()`.

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

from sag.trajectory.phases import blocked_close
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

#: What each kind of transition means to a reader. Spec §4.2 prescribes three
#: forms — advanced, blocked, repair — and the band used to print the ledger's
#: own `expected_kind` for the other three, so two of the five closes on every
#: normal run read `evidence_close` and `flow_close`.
_TRANSITION_WORD = {
    "advance": "advanced",
    "evidence_close": "finished",
    "report": "reported",
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


def turn_duration_text(turn: Turn) -> str | None:
    """How long the turn took, or nothing when the ledger has not said yet.

    Public because the written report prints the same column beside the same
    turns, and a turn that took `2m02s` on one surface took it on both. That
    holds because both read `t0` and `t1` off the same turn, and because the
    terminal waits for the engine's own record of the turn before it writes the
    number down — the record is where those two timestamps come from.
    """

    seconds = elapsed(turn.t0, turn.t1) if turn.t0 and turn.t1 else None
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def _stated(value: object) -> str | None:
    """A non-empty string off an event payload, or nothing at all.

    A payload key can hold anything a writer put there, and a turn's envelope
    and span are compared against it, so an empty string or a number must read
    as "the record did not say" rather than as a value nothing can match.
    """

    return value if isinstance(value, str) and value else None


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
        #: The last word a gate delivered inside the open band, so the band's
        #: close can say whether the phase finished.
        self._phase_gate: str | None = None
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
        #: A turn whose outcome is decided and not yet written. It waits for the
        #: engine's own record of the turn, because that record carries the span
        #: every other surface states.
        self._outcome: Turn | None = None
        #: Whether that outcome line carries the gate's word. Decided when the
        #: outcome was, not when it is written: a gate that lands in between
        #: takes a continuation line of its own, exactly as it does today.
        self._outcome_gate = False
        #: A gate's word for the turn whose outcome is still waiting. It cannot
        #: be written yet — the line it belongs under has not been written —
        #: and it must not place that line either, because the record has not
        #: landed and the span would be the gap between two events.
        self._queued_gate: str | None = None
        #: What the `turn_record` line just read names: the envelope it was
        #: taken for and the span it states. Read by the delta that follows it,
        #: which is where the reducer restates that turn with those very
        #: values. The record's own turn ids are ITS sequence and are not this
        #: view's, so the turn is recognised by what the record says about it.
        self._record: tuple[str | None, str | None, str | None] | None = None
        #: Whether this ledger records its turns at all. Once it has shown one
        #: record it will show the rest, so an outcome here has something to
        #: wait for and waits for it. A ledger that has never shown one has
        #: nothing coming, and its outcome goes out with the next thing that
        #: needs the terminal.
        self._records = False
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

    @staticmethod
    def _gate_adds(turn: Turn) -> bool:
        """Does the gate's word say anything the turn's own line has not?

        R49. Where the model claimed `done success` and the gate delivered
        `success`, the word costs a line and adds nothing: five of the nineteen
        turns of the first real run. Where it claimed `done success` and the
        gate delivered `partial`, the disagreement is the single most valuable
        thing in the stream. Show disagreement, suppress agreement — which is
        also what makes the disagreements visible. A gate on a turn that
        claimed nothing always adds.
        """

        if turn.gate is None:
            return False
        claimed = turn.call.claimed_outcome if turn.call is not None else None
        return turn.gate.word != claimed

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
        gate_word = turn.gate.word if (gate and self._gate_adds(turn)) else None
        line = _Line()
        if word is not None:
            line.add(word, _OUTCOME_STYLE.get(word))
        if summary:
            line.add(" · " if line.text else "").add(summary)
        if gate_word:
            line.add(" · " if line.text else "").add(f"{_GATE} {gate_word}")
        return line

    def _settle(self, turn: Turn, used: int, *, gate: bool | None = None) -> _Line | None:
        """What the turn's settling adds to a line already `used` columns long.

        The outcome starts where the call ENDED, not at a fixed column: a
        ten-character call followed by forty blanks and then a reason code cut
        in half is the worst thing this layout can do, because the cut string is
        the one a reader greps for. The timing goes flush to the right edge,
        where it stays a column to scan down and cannot be what gets clipped.

        `gate` overrides whether the gate's word is part of the line. An
        outcome that waited was decided before it was written, and asking
        `_gated` again at writing time would answer for the wrong moment.
        """

        # Only an ANSWER finishes a turn's line. A gate that arrived first
        # would otherwise complete the line on its own and the outcome, landing
        # afterwards, would find the turn already spoken for and be dropped.
        if not self._answered(turn):
            return None
        if gate is None:
            gate = turn.turn_id not in self._gated
        line = self._outcome_line(turn, gate=gate)
        room = self._width - used
        duration = turn_duration_text(turn)
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

        # An outcome still waiting always takes the newest statement of its own
        # turn. The record's start and end arrive on a restatement, and a line
        # written from the statement before it states a span nothing else does.
        waiting = self._outcome is not None and self._outcome.turn_id == turn.turn_id
        if waiting:
            self._outcome = turn
        recorded = self._is_recorded(turn)

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
            if self._answered(turn):
                self._hold_outcome(turn)
        else:
            if waiting and recorded:
                self._place()
            if turn.gate is not None and turn.turn_id not in self._gated:
                self._gated.add(turn.turn_id)
                if self._gate_adds(turn):
                    # The outcome was decided before this word arrived, so the
                    # word goes on the `↳ #N` line below it rather than joining
                    # it — which is where a late grading has always been read.
                    # An outcome still WAITING has no line above yet: writing
                    # this now would place that outcome before the record, with
                    # the gap between two events for its span. So it waits with
                    # it and goes out directly under it.
                    if waiting and self._outcome is not None:
                        self._queued_gate = turn.gate.word
                    else:
                        self._emit(
                            _Line(self._continuation(turn.turn_id)).add(
                                f"{_GATE} {turn.gate.word}"
                            )
                        )
        self._band(turn)

    def _dispatch(self, turn: Turn) -> None:
        """Open the turn's band if it opens one, then write its first line.

        A turn that arrives already answered AND already recorded has nothing
        left to wait for and goes out whole. One that arrives answered before
        its record — the two refusals of `sling-commons-osgi-v4` do — still
        gets its first line now and its outcome when the record lands, so the
        span on screen is the span the engine stated.
        """

        self._band(turn)
        head = self._head(turn)
        if self._answered(turn) and self._is_recorded(turn):
            settled = self._settle(turn, self._outcome_at(len(head.text)))
            self._said.add(turn.turn_id)
            if turn.gate is not None:
                # A held line defers the write past events that have already
                # happened, so a turn dispatched late can arrive with its
                # grading already in hand and on this very line. Not recording
                # it here says it a second time on the next restatement. Event
                # order is not render order, and this clause is about render
                # order.
                self._gated.add(turn.turn_id)
            self._emit(
                head.add(" " * (self._outcome_at(len(head.text)) - len(head.text))).extend(
                    settled or _Line()
                )
            )
            return
        self._open_line(head)
        self._open_turn = turn.turn_id
        if self._answered(turn):
            self._hold_outcome(turn)

    def _hold_outcome(self, turn: Turn) -> None:
        """Decide what the turn's outcome says; write it when the record lands.

        Deciding and writing are two moments now, and everything the outcome
        depends on other than the span is fixed here, at the first moment the
        ledger answered the turn: the turn is spoken for, and the gate's word
        is part of the line only if the grading was already in hand.
        """

        if self._outcome is not None and self._outcome.turn_id != turn.turn_id:
            # Two outcomes cannot wait at once: the one already waiting belongs
            # to the line that is open, and this one does not. Its own queued
            # grading goes out under it, where it belongs.
            self._place()
        self._outcome = turn
        # The grading is part of this line only if it was already in hand. A
        # restatement can hand the waiting outcome a gate that landed after it
        # was decided, and that gate's place is the `↳ #N` line below.
        self._outcome_gate = turn.gate is not None and turn.turn_id not in self._gated
        self._said.add(turn.turn_id)
        if turn.gate is not None:
            self._gated.add(turn.turn_id)
        if self._is_recorded(turn):
            self._place()

    def _is_recorded(self, turn: Turn) -> bool:
        """Is this the turn the record just read was taken for?

        The record names the envelope it answered and the span it ran for, and
        the reducer puts both on the turn it closed. Either identifies it. The
        record's own turn id does not: those are the engine's sequence, which
        this view counts differently on purpose.

        A record naming neither — no envelope, no span — says only that some
        turn was finished with, and a waiting outcome is left to wait.
        """

        if self._record is None:
            return False
        envelope, t0, t1 = self._record
        if envelope is not None:
            return turn.call is not None and turn.call.params_ref == envelope
        if t0 is not None and t1 is not None:
            return turn.t0 == t0 and turn.t1 == t1
        return False

    def _place(self) -> None:
        """Write the outcome that was waiting, then any grading queued under it.

        Both are cleared BEFORE the write, so an `_emit` inside either
        re-enters `_flush` and finds nothing left to place.
        """

        turn, self._outcome = self._outcome, None
        queued, self._queued_gate = self._queued_gate, None
        if turn is None:
            return
        gate = self._outcome_gate
        settled = self._settle(turn, self._outcome_at(self._open_cost), gate=gate)
        if settled is not None:
            self._complete(turn, settled, gate=gate)
        if queued is not None:
            self._emit(_Line(self._continuation(turn.turn_id)).add(f"{_GATE} {queued}"))

    def _flush(self) -> None:
        """Place what is waiting: an outcome first, then a held first line.

        Anything else reaching the terminal would otherwise be printed above a
        turn that was dispatched before it. The outcome goes first because it
        belongs to the line that is still open; the band a held first line goes
        under is the one already open, which is the fallback for a phase that
        never resolves.

        An outcome is placed here only while the ledger has shown no record at
        all. Once it has shown one, the rest are coming, and a turn whose own
        record is still on its way keeps waiting: the open line closes without
        it and it takes a `↳ #N` line when the record lands. The engine does
        seal turns out of order — a phase closing between a turn's answer and
        its record is how five archived runs are written — and placing the
        outcome on that close would state the gap between two events for a turn
        the engine measured at 23 seconds.
        """

        # `_pending` is cleared BEFORE anything is written, so the `_emit`
        # inside it re-enters here and finds nothing to do. That is the whole
        # re-entry guard; a flag as well would be a second one that can never
        # fire. `_place` clears its own.
        pending, self._pending = self._pending, None
        if not self._records:
            self._place()
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

    def _complete(self, turn: Turn, settled: _Line, *, gate: bool | None = None) -> None:
        """Finish the turn's line in place, or give the outcome its own line."""

        if self._open_turn == turn.turn_id and self._line_open:
            gutter = _Line(" " * (self._outcome_at(self._open_cost) - self._open_cost))
            self._write(gutter.extend(settled).render(self._width - self._open_cost, self._tty))
            self._close_line()
            return
        marker = self._continuation(turn.turn_id)
        self._emit(_Line(marker).extend(self._settle(turn, len(marker), gate=gate) or _Line()))

    def _band(self, turn: Turn) -> None:
        """Open a phase band when the run enters one, and remember its grading.

        A band is never opened for `unknown`: that is the reducer saying the
        ledger has not placed the run yet, not the name of a phase. A turn
        dispatched before any phase is stated is held (see `render_turn`) so
        that its band can be printed above it rather than under it.

        The gate word is recorded after any band change, so a turn that both
        enters a phase and carries its grading records the grading against the
        phase it is in rather than the one it left.
        """

        if turn.phase and turn.phase != UNKNOWN_PHASE and turn.phase != self._phase:
            self._phase = turn.phase
            self._phase_gate = None
            self._emit(_Line().add(f"▸ {turn.phase}", _BAND_STYLE))
        if turn.gate is not None:
            self._phase_gate = turn.gate.word

    # -- events --------------------------------------------------------

    def _note_transition(self, payload: dict) -> None:
        """One line for a transition, in the three forms §4.2 prescribes.

        The ledger's `expected_kind` and `expected_reason_code` are machine
        vocabulary and stay out of the line. `advance` carries no reason by
        spec; a phase whose gate said `failed` closes blocked rather than with
        a tick over it, which is what a reader scanning the left edge reads.
        """

        kind = str(payload.get("expected_kind") or "")
        target = payload.get("expected_target")
        reason = payload.get("expected_reason_code")
        name = self._phase or target or "phase"

        if kind == "repair":
            text = f"{_TRANSITION_GLYPH['repair']} {target or name} repair"
            if reason:
                text = f"{text} · {reason}"
            self._emit(_Line().add(text, _REPAIR_STYLE))
            return

        # One rule, two surfaces: the same call decides the fraction of phases
        # the result card says finished, so a band this line ticks is a band
        # that card counts. It is consulted before the run-level close as well
        # as after — the last band is still a band, and a tick over one the
        # card refuses is the two surfaces disagreeing about one run.
        blocked = blocked_close(kind, self._phase_gate)

        # `flow_close` ends the RUN. Naming it after whichever phase happened to
        # be open attributed a run-level event to a phase that did not cause it.
        # A grading that said the phase had not passed is the exception: there
        # the glyph matters more than the wording, so the run-level line gives
        # way to the one every other blocked close prints.
        if kind == "flow_close" and not blocked:
            self._emit(_Line().add("✓ run ended", _CLOSE_STYLE))
            return
        if blocked:
            # No reason here: the measured close codes (`test_terminal`,
            # `build_evidence_closed`, …) say which close fired, not why the
            # phase failed, and the turn line above already carries that.
            text = f"✗ {name} blocked"
        else:
            word = _TRANSITION_WORD.get(kind)
            if word is None:
                # A kind this layer does not know yet is shown whole rather
                # than guessed at, with a neutral glyph rather than a KeyError.
                text = f"· {name} {kind}"
                if reason:
                    text = f"{text} · {reason}"
            else:
                text = f"{_TRANSITION_GLYPH.get(kind, '·')} {name} {word}"
        self._emit(_Line().add(text, _CLOSE_STYLE))

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

    def give_way(self) -> None:
        """Finish the open line: something not from the ledger needs the screen.

        The console log sink calls this before every line it writes. The
        renderer holds a dispatch line open until the answer arrives and cannot
        see another writer — so the other writer says so first: the held first
        line is placed, the open line is closed, and the answer, when it comes,
        takes a `↳ #N` line of its own. Never raises: a log sink that raises
        prints a traceback over the very screen it was making room on, so after
        `close()` this does nothing at all.
        """

        if self._closed:
            return
        self._flush()
        self._close_line()

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
        self._note_event(raw_line)
        self._apply(delta)

    def note_event(self, raw_line: str) -> None:
        """Write the notes an event carries that no turn and no delta does.

        Three kinds say something a reader needs and nothing downstream keeps:
        a phase closing, a job the run is still waiting on, and a job left
        running at the close. They live in the raw event, so a caller that
        folds the lines elsewhere — `follow_trajectory` does — has to hand them
        here as well, or the same ledger reads one way replayed and another way
        tailed.
        """

        self._refuse_when_closed()
        self._note_event(raw_line)

    def _note_event(self, raw_line: str) -> None:
        event = self._event(raw_line)
        if event is None:
            return
        kind, body = event
        if kind == "phase_transition":
            self._note_transition(body)
        elif kind == "job_barrier_wait":
            self._note_job(body)
        elif kind == "job_live_at_close":
            self._note_live_job(body)
        elif kind == "turn_record":
            # Nothing to print. What it says is that the engine has finished
            # with a turn, and the delta this line produces restates that turn
            # with the engine's own start and end — which is the moment a
            # waiting outcome can be written with the span every other surface
            # states. Which turn is read off the record itself: the engine
            # seals turns out of order, and a delta carrying one turn's record
            # can restate another turn for its own reasons.
            self._records = True
            self._record = (
                _stated(body.get("envelope_ref")),
                _stated(body.get("t0")),
                _stated(body.get("t1")),
            )

    def render_delta(self, delta: TrajectoryDelta) -> None:
        """Write what one delta changed: its turns, then its statements.

        A live run hands this renderer raw lines and it folds them itself. A
        reader replaying or tailing a recorded session has already folded
        them — `follow_trajectory` yields whole deltas, including the one only
        the end of a follow can produce — so both arrive here and leave by the
        same two steps.
        """

        self._refuse_when_closed()
        self._apply(delta)

    def _apply(self, delta: TrajectoryDelta) -> None:
        """Turns first, then statements.

        Turns first because a statement about a turn is held until that turn
        has settled on its own evidence; the statements after, so a hole a
        follow reports is shown on exactly the terms a replay shows it.
        """

        for turn in delta.turns:
            self.render_turn(turn)
        # The record's reach is one delta: the one its own line produced.
        self._record = None
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
        # Whatever is still waiting goes out here, recorded or not: a run that
        # ended owes the reader every outcome it has, and nothing may be held
        # past this point.
        self._place()
        self._flush()
        self._close_line()
        for warning in sorted(self._held, key=warning_order):
            if warning.code not in _ACCOUNTING_CODES:
                self._show(warning)
        self._tally()
        self._close_line()
        self._closed = True


__all__ = ["TurnStreamRenderer", "TOOL_WIDTH", "turn_duration_text"]
