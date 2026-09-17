"""One incremental reducer — the same events fold live or replayed.

`TrajectoryReducer.feed()` takes a raw control-event LINE, not a path. That is
the whole trick behind spec §1's "one derivation pipeline": live tail-follow
hands it the lines as they are appended, batch replay hands it the lines of an
archived file, and neither feed knows about the other. There is no I/O here, no
file handling, and no console-log reading — the reducer only ever folds bytes
somebody else read out of the authoritative layer.

The fold is deliberately forgiving. Pre-closure sessions (spec §0) have real
holes: calls with no result, phase calls that emit no `loop_decision`, results
whose envelope never appeared. None of those raise. Each becomes a `Warning`
row that names the hole and the control sequence where it opened, so the
derived view stays honest about what the ledger did not say. After Pillar 1
closes those holes, the warnings simply stop appearing.

**Turn assembly.** A turn is one call and everything the ledger says about it:

- `action_envelope` (or `forced_action`) ALWAYS opens a turn and supplies [B],
  the call; it never joins a turn already open. The turn stays open until the
  next one opens.
- `tool_result` joins its turn by `envelope_id` and supplies [C], the
  observation.
- `loop_decision` attaches the control layer's reading of that call — the
  iteration, the phase, the error code and failure signature. It joins the
  open turn when that turn is still undecided and called the tool the decision
  names; otherwise it opens a turn of its own, which is how a decision that no
  envelope accounts for still gets a row (and a `missing_envelope` warning)
  instead of being silently stapled onto somebody else's call.
- `refusal_record` opens a turn and answers it in the same breath: a call that
  never reached a tool has no envelope and no result, and the record stands in
  both places. Such a turn is not a hole; it is a complete account of a call
  that was refused, and the refusal is stated as an annotation.
- `turn_record` SEALS the turn it belongs to. From then on the turn is stated
  rather than inferred — its phase, iteration, span, bill and window come off
  the record, and its holes are what the record leaves rather than what the
  pairing could not find. The engine's own turn numbering is checked for holes
  (`conservation_violation`) instead of being adopted as this view's row ids:
  the two count the same things only when the ledger is closed, and saying so
  when they differ is the job.
- `gate_decision` / `gate_outcome_revised` attach the word the gate delivered
  to the turn that carried it, and to the phase band it graded. A
  `gate_outcome_revised` attaches to the turn that carried the word it
  REVISES, which by then is rarely the open one — the model has already acted
  on the delivered outcome and the run has moved on.
- `phase_transition` closes the band of the phase the run is in and opens a new
  segment for the phase it names.

**Phase bands are contiguous segments.** Only a `phase_transition` opens one.
Every other event that names a phase — a late `loop_decision` for a call made
before the transition, a gate grading a phase already closed — attaches to that
phase's most recent segment instead of appending a duplicate band, so the
timeline never draws a phantom segment and the run is never in two places at
once. A phase genuinely re-entered gets its own segment, because a transition
said so.

**Where the run IS and where a turn BELONGS are two different questions.** Only
a `phase_transition` moves the run pointer. A `tool_result` or `loop_decision`
naming a phase bands THAT turn and nothing else: the phase it names is the phase
its CALL was made in, which a slow call answers from after the run has already
left. Letting that word move the pointer put the run back in a phase it had
finished, and the next transition then closed a band that was already closed
while the band actually being left stayed open for the rest of the run. The
pointer is only ever SET by a band coming into existence while the run has not
been placed at all — the opening phase no transition announces, and the phase
after a transition that closes one without naming a successor.

**A turn holds one gate, and a second word does not arrive quietly.** The turn
keeps the word delivered LAST, because that is the word in force. Whether the
replacement was designed or not is a separate fact, and it is stated: a gate
whose `supersedes` names the word it displaces is the chain spec §3.1 defines
and passes in silence; anything else replaced a grading the record does not
connect it to, and `gate_replaced_without_supersedes` names both decision ids
so the reader can see whether either was identified at all. Holding only the
last word made a doubly-graded turn indistinguishable from a singly-graded one
(rocketmq-externals seq 87 then 90: accepted false, then true, neither carrying
a `decision_id`).

**The ledger is counted as well as paired.** Spec §2.2 rule 5 equates three
sides — the calls opened, the calls answered, the calls decided — and this
module states any shortfall as a `conservation_violation` naming which side
came up short and by how much. The count is a second, independent reading of
the same holes the per-turn pairing finds: kafka's ten silent phase calls are
ten `missing_loop_decision` rows AND a decided side nine short. It is taken
over the calls the ledger has FINISHED writing, because between an envelope
and its answer every side legitimately disagrees, and the turn still in flight
already states its own holes.

**Where the run BLED is marked, and only where the ledger says so.** Two facts
in the authoritative layer are not holes and not ordinary outcomes, and a reader
scanning a timeline needs them found rather than expanded into:

- a process that did not finish and fail but was KILLED — an exit in the 128+N
  range, of which 137 is the OOM killer's SIGKILL (ignite d2r2 seq 160). It is
  read off `metadata.exit_code` (and `metadata.execution.exit_code`), never out
  of the result's prose, and never out of a log;
- a detached job the run never heard the end of — `job_live_at_close`, and its
  historical spelling `job_unsettled`. The mark lands on the turn that STARTED
  the job, joined by the job id the ledger itself writes in three places
  (`metadata.job_id`, `poll_ref`, `loop_decision.event.job_id`), because that
  dispatch otherwise reads as a call that merely had not answered yet.

Both are `Annotation(kind="conflict")` — a fact ABOUT a turn, never a `Warning`,
which is this layer's word for a hole in the ledger. The event that states one
joins the marked turn's `control_seq`, so a badge can always be descended to the
bytes behind it.

The third anomaly spec §4 names — a provider 5xx retried into success — is NOT
derived here, because nothing in the authoritative layer records it: the retry
is `react_engine._native_turn_with_retry`'s business and it survives only as a
console-log warning, which is never an input (§1). Deriving it would mean
reading a render, and the trajectory would be claiming a fact the ledger does
not hold. It becomes derivable when the engine seals it, not before.

**Warnings are statements, and a statement can stop being true.** Turn-level
warnings are recomputed from turn state, never stored at seal time: a hole is a
claim about the ledger AS IT STANDS. Each is claimed the moment it is true —
including on the turn still open, which is the one a live watcher most needs
stated — and withdrawn (`TrajectoryDelta.retracted_warnings`) the moment the
missing piece arrives. That is what makes accumulating every delta reproduce
`snapshot()` exactly rather than approximately; `DeltaAccumulator` at the foot
of this module is the reference implementation of the folding rules, and the
fences compare the two documents whole.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sag.agent.control_events import CANCELLED_CALL_REFUSAL_CODE, CONTROL_EVENT_KINDS
from sag.trajectory.schema import (
    KEY_RESULTS_MAX_CHARS,
    Annotation,
    CallInfo,
    GateInfo,
    ObservationInfo,
    PhaseInfo,
    SessionInfo,
    TokenUsage,
    Trajectory,
    TrajectoryDelta,
    Turn,
    Warning,
    warning_order,
)
from sag.trajectory.summaries import (
    call_summary,
    observation_outcome,
    observation_summary,
    refusal_summary,
)

#: The engine's own event vocabulary is the definition of "known". Anything
#: outside it is a kind this schema version has never heard of.
KNOWN_EVENT_KINDS = frozenset(CONTROL_EVENT_KINDS)

#: What a turn's phase is called before any event has said which phase it is in.
UNKNOWN_PHASE = "unknown"

#: The three sides of spec §2.2 rule 5, each named by the counters that add up
#: to it. Every side counts CALLS, and the same number of calls, which is the
#: whole content of the fence:
#:
#:     #action_envelope + #forced_action + #refusal_record
#:       == #tool_result + #refusal_record
#:       == #loop_decision + #cancelled
#:
#: A refusal record stands in both of the places its call never reached, so it
#: appears on the first two sides. `cancelled` counts the one exception: a call
#: a batch break cancelled dispatched nothing, so it has no envelope and no
#: result, and it emits no `loop_decision` either, because the recurrence
#: ladder reads outcomes and this call produced none.
_CONSERVATION_SIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("opened", ("action_envelope", "forced_action", "refusal_record")),
    ("answered", ("tool_result", "refusal_record")),
    ("decided", ("loop_decision", "cancelled")),
)

#: A process that exits 128+N was killed by signal N — the convention every
#: shell writes terminal signals with, and the one Docker reports a killed
#: container's exit with. 137 is 128+9, SIGKILL, which is what an OOM kill
#: leaves behind. This is a READING of the number rather than a second fact the
#: ledger states, which is why the exit code itself is carried beside it: a
#: reader who thinks a program simply chose to exit 137 can say so.
_SIGNAL_EXIT_FLOOR = 128
#: Linux defines 64 signals; past 128+64 the convention has nothing to say, and
#: a status that high is a program's own number.
_SIGNAL_EXIT_CEILING = 192


@dataclass
class _TurnState:
    """A turn under construction. Rendered into a `Turn` on demand."""

    turn_id: int
    actor: str
    phase: str = UNKNOWN_PHASE
    iteration: int | None = None
    envelope_id: str | None = None
    call: CallInfo | None = None
    observation: ObservationInfo | None = None
    gate: GateInfo | None = None
    window_ref: str | None = None
    window_components: list[str] | None = None
    tokens: TokenUsage | None = None
    t0: str | None = None
    t1: str | None = None
    control_seq: list[int] = field(default_factory=list)
    #: Whether the ENGINE stated this turn (`turn_record`). A sealed turn is no
    #: longer inferred: its identity, span, bill and window are read off the
    #: record, and what counts as a hole in it is what the record leaves open
    #: rather than what the inference could not pair.
    sealed: bool = False
    #: The envelope the record NAMED, which is not the same as the envelope the
    #: reducer saw. A record naming one nobody opened is a hole; a record
    #: naming none — a controller answering from policy — is not.
    stated_envelope: str | None = None
    has_decision: bool = False
    #: Only a `tool_result` (or, after Pillar 1, a typed refusal) answers a
    #: call. A `loop_decision` may describe the answer, but it is not one — so
    #: a turn whose result never came still says so, even when the decision
    #: told us its error code.
    has_result: bool = False
    #: Whether a `refusal_record` opened this turn. A refused call reached no
    #: tool, so there was no execution for the recurrence ladder to read: it is
    #: not missing a `loop_decision`, it was never owed one. Refusals that WERE
    #: read — the ones refused inside the executor — still carry their decision
    #: and record it, which is why this exempts rather than replaces.
    refused: bool = False
    #: What this turn contributes to each side of the conservation formula
    #: (§2.2 rule 5), keyed by the counter names of `_CONSERVATION_SIDES`. Held
    #: per turn so the count fence can exclude the call still in flight.
    counts: dict[str, int] = field(default_factory=dict)

    def touch(self, sequence: int | None) -> None:
        if sequence is not None and sequence not in self.control_seq:
            self.control_seq.append(sequence)

    @property
    def opened_at(self) -> int | None:
        return self.control_seq[0] if self.control_seq else None

    def render(self) -> Turn:
        return Turn(
            turn_id=self.turn_id,
            phase=self.phase,
            iteration=self.iteration,
            actor=self.actor,
            window_ref=self.window_ref,
            window_components=(
                list(self.window_components) if self.window_components is not None else None
            ),
            call=self.call,
            observation=self.observation,
            gate=self.gate,
            tokens=self.tokens,
            t0=self.t0,
            t1=self.t1,
            control_seq=list(self.control_seq),
        )


@dataclass
class _PhaseState:
    name: str
    termination: str | None = None
    gates: list[GateInfo] = field(default_factory=list)
    #: What the gate that last STATED each of them read, in the gate's own
    #: words. The three merge independently (`_band_reading`): a later gate
    #: replaces a field it states and leaves alone a field it does not, so a
    #: band graded ten times keeps every reading any gate actually gave it.
    validator_state: str | None = None
    reason: str | None = None
    key_results: str | None = None

    def render(self) -> PhaseInfo:
        return PhaseInfo(
            name=self.name,
            termination=self.termination,
            gates=list(self.gates),
            validator_state=self.validator_state,
            reason=self.reason,
            key_results=self.key_results,
        )


class TrajectoryReducer:
    """Folds control-event lines into a trajectory-v1 view, incrementally."""

    def __init__(self) -> None:
        self._turns: list[_TurnState] = []
        self._by_envelope: dict[str, _TurnState] = {}
        #: Which turn carried each grading, so a revision of a word delivered
        #: long ago finds its owner instead of landing on whatever is open.
        self._by_decision: dict[str, _TurnState] = {}
        #: Which turn STARTED each detached job, so a job event that lands after
        #: the run has moved on marks the dispatch rather than the open turn.
        self._by_job: dict[str, _TurnState] = {}
        #: Every anomaly already drawn, as (turn, anomaly, what it is about). A
        #: killed job is stated twice by design — observed, then settled — and
        #: two badges on one row would read as two kills.
        self._marked: set[tuple[Any, ...]] = set()
        self._phases: list[_PhaseState] = []
        #: Where the RUN is: the band a `phase_transition` will terminate. `None`
        #: means the run has not been placed — before its first phase is banded,
        #: and after a transition that closed one without naming a successor.
        self._phase: str | None = None
        self._annotations: list[Annotation] = []
        self._event_warnings: list[Warning] = []
        #: What each turn's holes were the last time a delta said so. The diff
        #: against the turn's current holes is what a delta ships.
        self._claimed: dict[int, tuple[Warning, ...]] = {}
        #: The last turn id the ENGINE sealed. The sequence it states is its
        #: own, and it is checked for holes rather than adopted as this view's
        #: row ids: a run seals a record for every turn it takes, and this view
        #: derives a row for everything the ledger shows — which is the same
        #: set only when the ledger is closed, and stating the difference is
        #: the whole job.
        self._last_sealed_turn_id = 0
        #: Every counted event of the run, by counter name. The conservation
        #: fence subtracts the OPEN turn's own counts from these, so a call
        #: still in flight is not read as a ledger that does not balance.
        self._counted: dict[str, int] = {}
        #: The imbalance last stated, so a statement that stops being true is
        #: withdrawn rather than left standing.
        self._conservation: Warning | None = None
        self._run_id: str | None = None
        self._first_timestamp: str | None = None
        self._last_timestamp: str | None = None
        self._wall_clock: float | None = None
        #: What the last delta said about the two derived session fields, so a
        #: patch names only what actually moved.
        self._told_wall_clock: float | None = None
        self._banding: tuple[PhaseInfo, ...] = ()

    # ---- public API ---------------------------------------------------

    def feed(self, raw_line: str) -> TrajectoryDelta:
        """Fold one raw control-event line; never raises on bad input."""
        collector = _Delta(self)
        line = raw_line.strip()
        if not line:
            return collector.render()

        try:
            event = json.loads(line)
        except ValueError as exc:
            collector.warn("malformed_event_line", f"line is not JSON: {exc}", None)
            return collector.render()
        if not isinstance(event, dict):
            collector.warn("malformed_event_line", "event line is not a JSON object", None)
            return collector.render()

        sequence = event.get("sequence")
        sequence = sequence if isinstance(sequence, int) else None
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            collector.warn("malformed_event_line", "event line carries no kind", sequence)
            return collector.render()
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        timestamp = event.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) else None
        self._note_timestamp(timestamp)

        if kind not in KNOWN_EVENT_KINDS:
            collector.warn(
                "unknown_event_kind",
                f"event kind {kind!r} is not in this schema version's vocabulary",
                sequence,
            )
            return collector.render()

        handler = self._HANDLERS.get(kind)
        if handler is not None:
            handler(self, collector, sequence, timestamp, payload)
        return collector.render()

    def snapshot(self) -> Trajectory:
        """Return the whole derived view as it stands right now."""
        return Trajectory(
            session=self._session_info(),
            phases=[phase.render() for phase in self._phases],
            turns=[turn.render() for turn in self._turns],
            annotations=list(self._annotations),
            warnings=self._all_warnings(),
        )

    # ---- session ------------------------------------------------------

    def _session_info(self) -> SessionInfo:
        return SessionInfo(run_id=self._run_id or "", wall_clock_seconds=self._wall_clock)

    def _note_timestamp(self, timestamp: str | None) -> None:
        if timestamp is None:
            return
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._last_timestamp = timestamp
        self._wall_clock = _elapsed(self._first_timestamp, self._last_timestamp)

    # ---- warnings -----------------------------------------------------

    def _all_warnings(self) -> list[Warning]:
        standing = self._conservation_statement()
        return order_warnings(
            list(self._event_warnings)
            + [w for turn in self._turns for w in _turn_warnings(turn)]
            + ([standing] if standing is not None else [])
        )

    # ---- the conservation fence (spec §2.2 rule 5) --------------------

    def _count(self, turn: _TurnState, *counters: str) -> None:
        """Attribute one event to its call, on every side of the fence it feeds.

        Counting is per TURN, not per line, so an event that belongs to no call
        — an `orphan_tool_result`, an answer to a call this ledger never
        recorded making — is stated by its own warning and never silently
        pushed onto a side of the formula.
        """
        for counter in counters:
            turn.counts[counter] = turn.counts.get(counter, 0) + 1
            self._counted[counter] = self._counted.get(counter, 0) + 1

    def _conservation_statement(self) -> Warning | None:
        """Do the three sides count the same calls? Name the ones that fall short.

        The fence is over the calls the ledger has FINISHED writing: the open
        turn's own events are subtracted, because between an envelope and its
        answer every side disagrees and the turn's own holes already say so.
        Once the turn closes, whatever it never got is arithmetic.

        A statement about the run as a whole names no turn and no sequence —
        there is no single line to point at, and a detail that moved with the
        counts would read as a fresh claim on every event.
        """
        open_counts = self._open_turn.counts if self._open_turn is not None else {}
        totals = []
        for name, counters in _CONSERVATION_SIDES:
            value = sum(
                self._counted.get(counter, 0) - open_counts.get(counter, 0) for counter in counters
            )
            totals.append((name, counters, value))
        accounted = max(value for _name, _counters, value in totals)
        short = [
            f"{name} ({' + '.join(counters)}) by {accounted - value}"
            for name, counters, value in totals
            if value < accounted
        ]
        if not short:
            return None
        return Warning(
            code="conservation_violation",
            detail=(
                f"the ledger accounts for {accounted} closed call(s) on its fullest side, "
                f"and is short: {'; '.join(short)}"
            ),
            control_seq=None,
        )

    def _restate_conservation(self, collector: "_Delta") -> None:
        current = self._conservation_statement()
        held, self._conservation = self._conservation, current
        if current == held:
            return
        if current is not None:
            collector.state([current])
        if held is not None:
            collector.withdraw([held])

    def _restate(self, collector: "_Delta", turn: _TurnState) -> None:
        """Ship the difference between this turn's holes and its last claim."""
        current = tuple(_turn_warnings(turn))
        claimed = self._claimed.get(turn.turn_id, ())
        if current == claimed:
            return
        self._claimed[turn.turn_id] = current
        collector.state([w for w in current if w not in claimed])
        collector.withdraw([w for w in claimed if w not in current])

    # ---- turn bookkeeping ---------------------------------------------

    @property
    def _open_turn(self) -> _TurnState | None:
        return self._turns[-1] if self._turns else None

    def _open(self, collector: "_Delta", *, actor: str, phase: str | None) -> _TurnState:
        """Seal whatever turn is open and start a new one."""
        previous = self._open_turn
        if previous is not None:
            collector.seal(previous)
        turn = _TurnState(
            turn_id=len(self._turns) + 1,
            actor=actor,
            phase=phase or self._current_phase(),
        )
        self._turns.append(turn)
        return turn

    def _current_phase(self) -> str:
        return self._phase or UNKNOWN_PHASE

    def _phase_band(self, name: str, *, open_segment: bool = False) -> _PhaseState:
        """The band a phase's events belong to, opening a segment only on demand.

        A phase is re-entered only when a `phase_transition` says so, and that
        is the only caller that passes `open_segment`. Every other mention of a
        phase — including one that arrives after the phase closed — lands on
        that phase's most recent segment, because bands are contiguous stretches
        of the run and a stray late event does not start a new stretch.

        A band coming into existence while the run has not been placed SETS the
        pointer, which is not the same thing as moving it. No transition names
        the phase a run opens in (the first one in every archived ledger already
        names the phase being entered next), and a transition that closes a phase
        without naming a successor leaves the run between phases; in both cases
        the next phase to appear is where the run is, and terminating it later
        depends on having said so.
        """
        if not open_segment:
            for phase in reversed(self._phases):
                if phase.name == name:
                    return phase
        band = _PhaseState(name=name)
        self._phases.append(band)
        if self._phase is None:
            self._phase = name
        return band

    def _enter(self, name: str, *, open_segment: bool = False) -> _PhaseState:
        """Say where the run is now, and hand back the band it is in."""
        band = self._phase_band(name, open_segment=open_segment)
        self._phase = name
        return band

    def _adopt_phase(self, turn: _TurnState, phase: Any) -> None:
        """Band THIS turn, and leave the run where the last transition put it.

        The phase a `tool_result` or a `loop_decision` names is the phase its
        CALL was made in, not where the run is now — the two differ for every
        call whose answer outlives the transition that followed it.
        """
        if isinstance(phase, str) and phase:
            turn.phase = phase
            self._phase_band(phase)

    # ---- anomalies (spec §4: where the run bled) ----------------------

    def _index_job(self, turn: _TurnState, *candidates: Any) -> None:
        """Remember which turn STARTED a job, under every name the ledger uses.

        The first turn to name a job is the one that dispatched it; a later poll
        of the same job names it again, and marking that turn would point a
        reader at the call that merely asked after the job rather than the call
        that started it.
        """
        for candidate in candidates:
            key = _job_key(candidate)
            if key and key not in self._by_job:
                self._by_job[key] = turn

    def _mark(
        self,
        collector: "_Delta",
        turn: _TurnState,
        about: Any,
        data: dict[str, Any],
        sequence: int | None,
    ) -> None:
        """Draw one anomaly on one turn, once, and let the row reach its bytes."""
        key = (turn.turn_id, data["anomaly"], about)
        if key in self._marked:
            return
        self._marked.add(key)
        collector.annotate("conflict", turn.turn_id, data)
        turn.touch(sequence)
        collector.touched(turn)

    def _mark_job(
        self,
        collector: "_Delta",
        sequence: int | None,
        kind: str,
        job_id: str | None,
        about: Any,
        data: dict[str, Any],
    ) -> None:
        """Mark the turn that started this job, or say that none did."""
        turn = self._by_job.get(job_id) if job_id else None
        if turn is None:
            collector.warn(
                "orphan_job_anomaly",
                f"{kind} names job {job_id!r}, which no turn in this ledger dispatched",
                sequence,
            )
            return
        self._mark(collector, turn, about, data, sequence)

    def _note_kill(
        self, collector: "_Delta", turn: _TurnState, result: Any, sequence: int | None
    ) -> None:
        """A call whose process was killed says so where the row can be read."""
        for code in _exit_codes(result):
            signal = _signal_of(code)
            if signal is None:
                continue
            self._mark(
                collector,
                turn,
                code,
                {
                    "anomaly": "killed_by_signal",
                    "exit_code": code,
                    "signal": signal,
                    "stated_by": "tool_result",
                },
                sequence,
            )

    def _job_exit(
        self, collector: "_Delta", sequence: int | None, payload: dict, kind: str
    ) -> None:
        """The terminal exit code of a job, stated after its call had returned.

        A dispatch that hands its work to a detached job returns `pending` and
        carries no exit code at all; these two kinds are the only place one is
        ever written. A kill stated here is the same kill — it just lands on a
        turn that closed long ago.
        """
        code = payload.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            return
        signal = _signal_of(code)
        if signal is None:
            return
        job_id = _job_key(payload.get("job_id"))
        self._mark_job(
            collector,
            sequence,
            kind,
            job_id,
            code,
            {
                "anomaly": "killed_by_signal",
                "exit_code": code,
                "signal": signal,
                "job_id": job_id,
                "stated_by": kind,
            },
        )

    def _job_unfinished(
        self, collector: "_Delta", sequence: int | None, payload: dict, kind: str
    ) -> None:
        """A job the run never heard the end of, marked on the turn that started it."""
        job_id = _job_key(payload.get("job_id"))
        self._mark_job(
            collector,
            sequence,
            kind,
            job_id,
            job_id,
            {
                "anomaly": "job_never_settled",
                "job_id": job_id,
                "close_reason": _text(payload.get("close_reason")),
                "log_ref": _text(payload.get("log_ref")),
                "obligation_ref": _text(payload.get("obligation_ref")),
                "evidence_ref": _text(payload.get("evidence_ref")),
                "stated_by": kind,
            },
        )

    # ---- handlers -----------------------------------------------------

    def _on_job_settled(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        self._job_exit(collector, sequence, payload, "job_settled")

    def _on_job_terminal_observed(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        self._job_exit(collector, sequence, payload, "job_terminal_observed")

    def _on_job_live_at_close(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        self._job_unfinished(collector, sequence, payload, "job_live_at_close")

    def _on_job_unsettled(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        self._job_unfinished(collector, sequence, payload, "job_unsettled")

    def _on_evidence_store_bound(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id and run_id != self._run_id:
            self._run_id = run_id
            collector.patch_session(run_id=run_id)

    def _on_action_envelope(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        tool = payload.get("tool")
        tool = tool if isinstance(tool, str) and tool else "unknown"
        actor = "controller" if payload.get("intent_source") == "controller" else "model"
        envelope_id = payload.get("envelope_id")
        envelope_id = envelope_id if isinstance(envelope_id, str) and envelope_id else None

        # An envelope ALWAYS opens a turn. It never joins one already open, not
        # even a call-less one naming the same tool: this engine writes
        # envelope→result→decision (measured across all 27 archived ledgers, 763
        # envelopes, zero results preceding their envelope), so a still-open
        # call-less turn is an orphan `loop_decision` — a refusal of an EARLIER
        # call — and folding this envelope into it would assert that THIS
        # envelope returned that refusal's error code and evidence ref. In a
        # repair retry the two tool names match BY CONSTRUCTION, so tool-name
        # adoption corrupts exactly the shape it looks safest on (camel-quarkus
        # seq 124→125 and 138→139; the slice corpus pins the holes at the
        # refusals, seq 124/138/216, not at the retries).
        turn = self._open(collector, actor=actor, phase=None)
        turn.call = CallInfo(
            tool=tool,
            params_ref=envelope_id,
            summary=call_summary(tool, payload.get("exact_params")),
        )
        turn.envelope_id = envelope_id
        turn.t0 = timestamp
        turn.touch(sequence)
        self._count(turn, "action_envelope")
        if envelope_id:
            self._by_envelope[envelope_id] = turn
        collector.touched(turn)

    def _on_forced_action(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        envelope_id = payload.get("envelope_id")
        envelope_id = envelope_id if isinstance(envelope_id, str) and envelope_id else None
        tool = payload.get("tool")
        tool = tool if isinstance(tool, str) and tool else "unknown"
        phase = payload.get("phase")
        turn = self._open(
            collector, actor="controller", phase=phase if isinstance(phase, str) else None
        )
        turn.call = CallInfo(
            tool=tool,
            params_ref=envelope_id,
            summary=call_summary(tool, payload.get("exact_params")),
        )
        turn.envelope_id = envelope_id
        turn.t0 = timestamp
        turn.touch(sequence)
        self._count(turn, "forced_action")
        if envelope_id:
            self._by_envelope[envelope_id] = turn
        self._adopt_phase(turn, phase)
        collector.annotate(
            "forced",
            turn.turn_id,
            {
                "policy": payload.get("policy"),
                "trigger": payload.get("trigger"),
                "reason_code": payload.get("reason_code"),
                "source_attempt_id": payload.get("source_attempt_id"),
            },
        )
        collector.touched(turn)

    def _on_turn_record(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        """The engine states a turn it took; the inference steps aside.

        A record arrives when its turn is over, so it seals the turn already
        under construction rather than opening one — unless nothing under
        construction is its turn, which is exactly the controller's case: an
        engine-generated gate close takes a turn of its own while the model's
        last turn, already sealed, is still the one the events point at. The
        owner is found by the envelope it names, then by an unsealed open turn
        of the same actor, and only then by opening a row for a turn the ledger
        showed no other trace of.

        What the record says wins over what was inferred, because the engine
        was there: the phase and iteration it was in, the span it ran for, the
        bill for it, and the window it answered from. The turn ids it states
        are ITS sequence, and this view checks them for holes instead of
        adopting them — the two count different things, and a run whose ledger
        is closed is the run where they agree.
        """
        actor = payload.get("actor")
        actor = actor if actor in ("model", "controller") else "model"
        stated_envelope = _text(payload.get("envelope_ref"))

        turn = None
        if stated_envelope:
            candidate = self._by_envelope.get(stated_envelope)
            turn = candidate if candidate is not None and not candidate.sealed else None
        if turn is None:
            open_turn = self._open_turn
            if open_turn is not None and not open_turn.sealed and open_turn.actor == actor:
                turn = open_turn
        if turn is None:
            turn = self._open(collector, actor=actor, phase=None)

        turn.sealed = True
        turn.actor = actor
        turn.stated_envelope = stated_envelope
        self._adopt_phase(turn, payload.get("phase"))
        iteration = payload.get("iteration")
        if isinstance(iteration, int):
            turn.iteration = iteration
        t0 = _text(payload.get("t0"))
        t1 = _text(payload.get("t1"))
        if t0:
            turn.t0 = t0
        if t1:
            turn.t1 = t1
        tokens_in = payload.get("tokens_in")
        tokens_out = payload.get("tokens_out")
        if isinstance(tokens_in, int) and isinstance(tokens_out, int):
            turn.tokens = TokenUsage(input=tokens_in, output=tokens_out)
        delivered = _text(payload.get("observation_ref"))
        if delivered:
            turn.observation, displaced = _delivered_observation(turn.observation, delivered)
            if displaced is not None:
                collector.warn(
                    "observation_ref_dropped",
                    (
                        f"turn {turn.turn_id} names three observation refs and a row carries "
                        f"two: {displaced} is not carried"
                    ),
                    sequence,
                    turn_id=turn.turn_id,
                )
        components = _window_components(payload.get("window_digest"))
        if components is not None:
            turn.window_components = components
        turn.window_ref = _window_ref(payload.get("window_digest")) or turn.window_ref
        turn.touch(sequence)
        self._check_sealed_sequence(collector, payload.get("turn_id"), sequence)
        collector.touched(turn)

    def _check_sealed_sequence(
        self, collector: "_Delta", stated: Any, sequence: int | None
    ) -> None:
        """The engine's turn ids are monotone by one, or the ledger lost a turn."""
        if not isinstance(stated, int):
            return
        expected = self._last_sealed_turn_id + 1
        if stated != expected:
            collector.warn(
                "conservation_violation",
                (
                    f"the engine sealed turn {stated} after turn "
                    f"{self._last_sealed_turn_id}: the sealed turn sequence has a hole"
                ),
                sequence,
            )
        self._last_sealed_turn_id = max(stated, self._last_sealed_turn_id)

    def _on_refusal_record(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        """A call that never reached a tool: its own turn, and no hole in it.

        The record stands where an envelope and a result would have stood, so
        it opens a turn exactly as an envelope does and answers it in the same
        breath. The turn is not a hole — it is a complete account of a call
        that was refused, which is what spec §2.2 rule 4 asked the ledger for —
        and the refusal itself is stated as an annotation, where a reader looks
        for what happened to a turn rather than for what is missing from it.
        """
        tool = _text(payload.get("tool")) or "unknown"
        refusal_code = _text(payload.get("refusal_code"))
        turn = self._open(collector, actor="model", phase=None)
        turn.call = CallInfo(tool=tool, params_ref=None)
        outcome, summary = refusal_summary(payload)
        turn.observation = (turn.observation or ObservationInfo()).model_copy(
            update={"outcome": outcome, "summary": summary}
        )
        turn.t0 = timestamp
        turn.t1 = timestamp
        turn.has_result = True
        turn.refused = True
        turn.touch(sequence)
        self._count(turn, "refusal_record")
        if refusal_code == CANCELLED_CALL_REFUSAL_CODE:
            # The one call that is owed no `loop_decision`: it dispatched
            # nothing, so it produced no outcome for the ladder to read.
            self._count(turn, "cancelled")
        collector.annotate(
            "refusal",
            turn.turn_id,
            {
                "tool": tool,
                "refusal_code": payload.get("refusal_code"),
                "exact_params_sha256": payload.get("exact_params_sha256"),
                "tool_call_id": payload.get("tool_call_id"),
            },
        )
        collector.touched(turn)

    def _on_tool_result(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        envelope_id = payload.get("envelope_id")
        turn = self._by_envelope.get(envelope_id) if isinstance(envelope_id, str) else None
        if turn is None:
            collector.warn(
                "orphan_tool_result",
                f"tool_result names envelope {envelope_id!r}, which no turn opened",
                sequence,
            )
            return
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        turn.observation = _merge_observation(
            turn.observation,
            ref=_text(result.get("output_ref")),
            error_code=_text(result.get("error_code")),
            failure_signature=_text(result.get("failure_signature")),
        )
        # The result is the only event that carries the projected payload the
        # summarisers read, so it is the only one that derives [C]'s line. A
        # result that stated no outcome is not a failure and a line nobody could
        # derive is not "" — so `None` is never invented into a value, and (via
        # `_stated`) never written over one an earlier result did state.
        tool_name = turn.call.tool if turn.call else (_text(payload.get("tool")) or "")
        turn.observation = turn.observation.model_copy(
            update=_stated(
                outcome=observation_outcome(result),
                summary=observation_summary(tool_name, result),
            )
        )
        turn.has_result = True
        turn.t1 = timestamp
        turn.touch(sequence)
        self._count(turn, "tool_result")
        self._adopt_phase(turn, payload.get("source_phase"))
        metadata = result.get("metadata")
        self._index_job(
            turn,
            metadata.get("job_id") if isinstance(metadata, dict) else None,
            result.get("poll_ref"),
        )
        self._note_kill(collector, turn, result, sequence)
        collector.touched(turn)

    def _on_loop_decision(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        event = payload.get("event")
        event = event if isinstance(event, dict) else {}
        tool_name = _text(event.get("tool_name"))

        # A decision joins the open turn only when it can plausibly be about it:
        # the turn is still undecided AND it called the tool this decision names.
        # A decision that fits nothing open opens its own turn, which then says
        # out loud that no envelope and no result account for it. Guessing which
        # earlier call it "really" meant is the archaeology this layer exists to
        # end (spec §0, camel-quarkus seq 124/138).
        turn = self._open_turn
        joinable = (
            turn is not None
            and not turn.has_decision
            and (turn.call is None or not tool_name or turn.call.tool == tool_name)
        )
        if not joinable or turn is None:
            turn = self._open(collector, actor="model", phase=None)

        turn.has_decision = True
        self._count(turn, "loop_decision")
        iteration = event.get("iteration")
        if isinstance(iteration, int):
            turn.iteration = iteration
        self._adopt_phase(turn, event.get("phase"))
        error_code = _text(event.get("error_code"))
        failure_signature = _text(event.get("failure_signature"))
        turn.observation = _merge_observation(
            turn.observation,
            ref=_text(event.get("evidence_ref")),
            error_code=error_code,
            failure_signature=failure_signature,
        )
        if turn.t1 is None:
            turn.t1 = timestamp
        turn.touch(sequence)
        self._index_job(turn, event.get("job_id"))

        recurrence = event.get("recurrence_count")
        if isinstance(recurrence, int) and recurrence > 1:
            collector.annotate(
                "recurrence",
                turn.turn_id,
                {
                    "recurrence_count": recurrence,
                    "failure_signature": failure_signature,
                    "error_code": error_code,
                },
            )
        collector.touched(turn)

    def _on_gate_decision(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        word = _text(payload.get("expected_outcome"))
        if word is None:
            collector.warn(
                "gate_without_a_word",
                "gate_decision carries no expected_outcome",
                sequence,
            )
            return
        gate = GateInfo(
            word=word,
            decision_id=_text(payload.get("decision_id")),
            supersedes=_text(payload.get("supersedes")),
        )
        self._attach_gate(collector, gate, sequence, payload.get("phase"), payload)

    def _on_gate_outcome_revised(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        """Replace a word already delivered, on the turn that was given it.

        A revision exists precisely because the model has already acted on the
        first word, so the turn that carried it is behind the one now open.
        Attaching to the open turn would hand a gate to a call nobody graded and
        leave the graded turn showing an outcome the record has withdrawn.

        The owner is found by `delivered_decision_id`. When no gate in this run
        delivered that id, the revision has no owner and none is guessed: it is
        an orphan, which `sag.agent.replay` treats as an integrity failure and
        this read-only layer states as a warning (spec §3).
        """
        word = _text(payload.get("revised_outcome"))
        if word is None:
            collector.warn(
                "gate_without_a_word",
                "gate_outcome_revised carries no revised_outcome",
                sequence,
            )
            return
        delivered = _text(payload.get("delivered_decision_id"))
        gate = GateInfo(
            word=word,
            decision_id=_text(payload.get("revised_decision_id")),
            supersedes=delivered,
        )
        owner = self._by_decision.get(delivered) if delivered else None
        if owner is None:
            self._band_gate(gate, payload.get("phase"), payload)
            collector.warn(
                "orphan_gate_revision",
                f"a revision replaces {delivered!r}, which no gate in this run delivered",
                sequence,
            )
            return
        self._attach_gate(collector, gate, sequence, payload.get("phase"), payload, owner=owner)

    def _band_gate(self, gate: GateInfo, phase: Any, payload: dict) -> None:
        """Record in the phase's band the word the gate delivered, and what it read."""
        if isinstance(phase, str) and phase:
            band = self._phase_band(phase)
            band.gates.append(gate)
            _band_reading(band, payload)

    def _attach_gate(
        self,
        collector: "_Delta",
        gate: GateInfo,
        sequence: int | None,
        phase: Any,
        payload: dict,
        *,
        owner: _TurnState | None = None,
    ) -> None:
        self._band_gate(gate, phase, payload)
        turn = owner or self._open_turn
        if turn is None:
            collector.warn(
                "orphan_gate_decision",
                f"a gate delivered {gate.word!r} before any turn opened",
                sequence,
            )
            return
        held = turn.gate
        # A chain is a word naming the word it replaces. `supersedes` unset names
        # nothing, so it never counts as one — which is the whole rocketmq-externals
        # case, where neither grading carried a `decision_id` either.
        if held is not None and not (gate.supersedes and gate.supersedes == held.decision_id):
            collector.warn(
                "gate_replaced_without_supersedes",
                (
                    f"turn {turn.turn_id} held gate {held.decision_id!r} and a second gate "
                    f"{gate.decision_id!r} replaced it without naming it in supersedes"
                ),
                sequence,
            )
        turn.gate = gate
        if gate.decision_id:
            self._by_decision[gate.decision_id] = turn
        turn.touch(sequence)
        collector.touched(turn)

    def _on_phase_transition(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        """Close the band of the phase the run is IN, then open the one it names.

        The band closed is looked up by phase name, not taken as "whichever band
        was appended last": a gate can band a phase no turn has entered, and
        terminating that stranger left the phase actually being left open for
        the rest of the run.

        A transition with no target — `evidence_close`, `flow_close` — closes a
        phase without naming a successor, so it leaves the run unplaced rather
        than parked on the band it just closed. Whatever phase appears next is
        where the run went, and it is a band that can still be terminated.
        """
        kind = _text(payload.get("expected_kind"))
        leaving = next((band for band in reversed(self._phases) if band.name == self._phase), None)
        if leaving is not None and leaving.termination is None:
            leaving.termination = kind
        target = _text(payload.get("expected_target"))
        if target:
            self._enter(target, open_segment=True)
        else:
            self._phase = None

    def _on_repair_context_opened(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        context = payload.get("context")
        context = context if isinstance(context, dict) else {}
        turn = self._open_turn
        if turn is None:
            collector.warn(
                "orphan_repair_context",
                "a repair context opened before any turn opened",
                sequence,
            )
            return
        collector.annotate(
            "repair_context",
            turn.turn_id,
            {
                "repair_context_id": context.get("repair_context_id"),
                "domain_id": context.get("domain_id"),
                "blocker_owner": context.get("blocker_owner"),
                "trigger_assessment_id": context.get("trigger_assessment_id"),
                "source_phase_attempt_id": payload.get("source_phase_attempt_id"),
                "source_gate_sequence": payload.get("source_gate_sequence"),
            },
        )

    #: Known kinds absent from this table are folded as "nothing to derive yet"
    #: — they are part of the ledger's vocabulary, so they are never warnings.
    _HANDLERS = {
        "evidence_store_bound": _on_evidence_store_bound,
        "action_envelope": _on_action_envelope,
        "forced_action": _on_forced_action,
        "turn_record": _on_turn_record,
        "refusal_record": _on_refusal_record,
        "tool_result": _on_tool_result,
        "loop_decision": _on_loop_decision,
        "gate_decision": _on_gate_decision,
        "gate_outcome_revised": _on_gate_outcome_revised,
        "phase_transition": _on_phase_transition,
        "repair_context_opened": _on_repair_context_opened,
        "job_settled": _on_job_settled,
        "job_terminal_observed": _on_job_terminal_observed,
        "job_live_at_close": _on_job_live_at_close,
        "job_unsettled": _on_job_unsettled,
    }


class _Delta:
    """Accumulates what one fed line changed."""

    def __init__(self, reducer: TrajectoryReducer) -> None:
        self._reducer = reducer
        self._touched: list[_TurnState] = []
        self._stated: list[Warning] = []
        self._withdrawn: list[Warning] = []
        self._annotations: list[Annotation] = []
        self._session_patch: dict[str, Any] = {}

    def warn(
        self,
        code: str,
        detail: str,
        control_seq: int | None,
        *,
        turn_id: int | None = None,
    ) -> None:
        """State something about THIS line. A line does not change its mind."""
        warning = Warning(code=code, detail=detail, control_seq=control_seq, turn_id=turn_id)
        self._reducer._event_warnings.append(warning)
        self._stated.append(warning)

    def state(self, warnings: list[Warning]) -> None:
        self._stated.extend(warnings)

    def withdraw(self, warnings: list[Warning]) -> None:
        self._withdrawn.extend(warnings)

    def seal(self, turn: _TurnState) -> None:
        """A turn just went out of reach; it is still touched, not still open."""
        self.touched(turn)

    def touched(self, turn: _TurnState) -> None:
        if turn not in self._touched:
            self._touched.append(turn)

    def annotate(self, kind: str, turn_id: int, data: dict[str, Any]) -> None:
        annotation = Annotation(
            kind=kind, turn_id=turn_id, data={k: v for k, v in data.items() if v is not None}
        )
        self._reducer._annotations.append(annotation)
        self._annotations.append(annotation)

    def patch_session(self, **fields: Any) -> None:
        self._session_patch.update(fields)

    def render(self) -> TrajectoryDelta:
        reducer = self._reducer
        for turn in self._touched:
            reducer._restate(self, turn)
        reducer._restate_conservation(self)
        patch = dict(self._session_patch)
        if reducer._wall_clock != reducer._told_wall_clock:
            reducer._told_wall_clock = reducer._wall_clock
            patch["wall_clock_seconds"] = reducer._wall_clock
        banding = tuple(phase.render() for phase in reducer._phases)
        changed_banding = banding != reducer._banding
        reducer._banding = banding
        return TrajectoryDelta(
            turns=[turn.render() for turn in self._touched],
            phases=list(banding) if changed_banding else None,
            annotations=list(self._annotations),
            warnings=list(self._stated),
            retracted_warnings=list(self._withdrawn),
            session_patch=patch,
        )


def _turn_warnings(turn: _TurnState) -> list[Warning]:
    """State every hole this turn has, as it stands.

    The wording names the turn and nothing that can change underneath it: a
    statement whose text drifted (with the turn's phase, say) would read as a
    retraction and a fresh claim of the same hole every time the phase settled.
    """
    if turn.sealed:
        return _sealed_turn_warnings(turn)
    holes: list[Warning] = []
    if turn.call is None:
        holes.append(_hole(turn, "missing_envelope", "has a loop_decision but no action_envelope"))
    if not turn.has_result:
        holes.append(_hole(turn, "missing_tool_result", "has no tool_result and no typed refusal"))
    if turn.call is not None and not turn.has_decision and not turn.refused:
        holes.append(
            _hole(
                turn,
                "missing_loop_decision",
                f"called {turn.call.tool!r} and emitted no loop_decision",
            )
        )
    return holes


def _sealed_turn_warnings(turn: _TurnState) -> list[Warning]:
    """A stated turn's holes are what the RECORD leaves, not what pairing missed.

    The inference has to treat a call-less turn as two holes at once, because
    all it can see is a decision nothing accounts for. A record removes the
    guess: a controller answering from policy made no call, so it is missing
    neither an envelope nor an answer — while a record that NAMES an envelope
    the ledger never opened is a hole exactly where the record says one is.
    """
    holes: list[Warning] = []
    if turn.stated_envelope and turn.call is None:
        holes.append(
            _hole(
                turn,
                "missing_envelope",
                f"names envelope {turn.stated_envelope!r}, which no action_envelope opened",
            )
        )
    if turn.call is None:
        return holes
    if not turn.has_result:
        holes.append(_hole(turn, "missing_tool_result", "has no tool_result and no typed refusal"))
    if not turn.has_decision and not turn.refused:
        holes.append(
            _hole(
                turn,
                "missing_loop_decision",
                f"called {turn.call.tool!r} and emitted no loop_decision",
            )
        )
    return holes


def _window_components(digest: Any) -> list[str] | None:
    """[A] whole: every component the record named, in the order it rendered them.

    The record states [A] as an ORDERED LIST of component refs, because bytes
    are stored once and a window is many messages. Resolving the list in order
    reproduces the array, which is what the quad view expands — so the row
    carries the list, not just a handle into it.

    A digest with no components states no window (a controller was shown none,
    or a component would not store): that is an absent list, never an empty
    one, because "shown nothing" and "shown a window of no messages" are not
    two facts a reader should have to tell apart.
    """
    if not isinstance(digest, dict):
        return None
    refs = digest.get("component_refs")
    if not isinstance(refs, (list, tuple)):
        return None
    named = [ref for ref in (_text(item) for item in refs) if ref is not None]
    return named or None


def _window_ref(digest: Any) -> str | None:
    """One handle into that window, for a row that has one slot.

    The handle is the list's LAST component — the newest message, the one thing
    in the window that this turn did not share with the turn before it. The
    head would be the system message, which every turn of a run resolves to the
    same bytes, and which the record already names by hash; a row keyed on it
    tells no two turns apart.
    """
    components = _window_components(digest)
    return components[-1] if components else None


def _hole(turn: _TurnState, code: str, what: str) -> Warning:
    return Warning(
        code=code,
        detail=f"turn {turn.turn_id} {what}",
        control_seq=turn.opened_at,
        turn_id=turn.turn_id,
    )


def order_warnings(warnings: list[Warning]) -> list[Warning]:
    """The canonical rendering of a set of statements: deduplicated, ordered."""
    return sorted(dict.fromkeys(warnings), key=warning_order)


class DeltaAccumulator:
    """The consumer side of the fold: deltas in, the same `Trajectory` out.

    This is the reference implementation of `TrajectoryDelta`'s accumulation
    rules, and the reason those rules are testable rather than aspirational —
    the golden fences replay an archived session through `follow_trajectory`,
    fold every delta here, and compare the result to `build_trajectory` whole.
    A live consumer (the timeline) folds the same way.
    """

    def __init__(self) -> None:
        self._turns: dict[int, Turn] = {}
        self._phases: list[PhaseInfo] = []
        self._annotations: list[Annotation] = []
        self._held: dict[Warning, None] = {}
        self._session: dict[str, Any] = {}
        self._outputs: dict[str, str] | None = None

    def feed(self, delta: TrajectoryDelta) -> None:
        for turn in delta.turns:
            self._turns[turn.turn_id] = turn
        if delta.phases is not None:
            self._phases = list(delta.phases)
        self._annotations.extend(delta.annotations)
        for warning in delta.retracted_warnings:
            self._held.pop(warning, None)
        for warning in delta.warnings:
            self._held.setdefault(warning, None)
        self._session.update(delta.session_patch)
        if delta.outputs is not None:
            self._outputs = {**(self._outputs or {}), **delta.outputs}

    def snapshot(self) -> Trajectory:
        return Trajectory(
            session=SessionInfo(**{"run_id": "", **self._session}),
            phases=list(self._phases),
            turns=[self._turns[key] for key in sorted(self._turns)],
            annotations=list(self._annotations),
            warnings=order_warnings(list(self._held)),
            outputs=self._outputs,
        )


def _merge_observation(
    existing: ObservationInfo | None,
    *,
    ref: str | None,
    error_code: str | None,
    failure_signature: str | None,
) -> ObservationInfo:
    """Fold what this event says about [C] into what is already known.

    Whichever event speaks first wins: the result and the decision describe the
    same observation, and neither is allowed to erase a field the other filled.
    A ref that arrives while a DIFFERENT one is already held is kept as the
    evidence ref rather than dropped — two events naming two byte strings for
    one observation is information, not noise.
    """
    if existing is None:
        return ObservationInfo(ref=ref, error_code=error_code, failure_signature=failure_signature)
    displaced = existing.evidence_ref
    if displaced is None and ref is not None and existing.ref not in (None, ref):
        displaced = ref
    return ObservationInfo(
        ref=existing.ref or ref,
        evidence_ref=displaced,
        error_code=existing.error_code or error_code,
        failure_signature=existing.failure_signature or failure_signature,
        # Derived by the result, which is the only event holding the payload
        # they were read off. The decision that follows it describes the same
        # observation from four fields and could not re-derive them, so it
        # carries them forward rather than rebuilding [C] without them.
        outcome=existing.outcome,
        summary=existing.summary,
    )


def _delivered_observation(
    existing: ObservationInfo | None, delivered: str
) -> tuple[ObservationInfo, str | None]:
    """The record's [C] takes the row; whatever it displaces stays named — or is.

    The engine states the observation it DELIVERED — the text the model read —
    and that is what the row shows. The ref it displaces is the tool's own
    output, which does not disappear: it moves to `evidence_ref`, where a
    reader after the tool's bytes finds them.

    Three refs do not fit two slots. When a `loop_decision` has already
    contributed a second ref and the record then delivers a third, one of them
    leaves — and it leaves NAMED, because a ref somebody wrote into the ledger
    is the one thing this layer may not drop in silence. The second return
    value is that ref, for the caller to state.
    """
    if existing is None:
        return ObservationInfo(ref=delivered), None
    evidence = existing.evidence_ref
    displaced = None
    if existing.ref not in (None, delivered):
        displaced = evidence if evidence not in (None, existing.ref) else None
        evidence = existing.ref
    return (
        ObservationInfo(
            ref=delivered,
            evidence_ref=evidence,
            error_code=existing.error_code,
            failure_signature=existing.failure_signature,
            outcome=existing.outcome,
            summary=existing.summary,
        ),
        displaced,
    )


def _text(value: Any) -> str | None:
    """Empty strings in the ledger mean "not stated"; say so as None."""
    if isinstance(value, str) and value:
        return value
    return None


def _stated(**values: Any) -> dict[str, Any]:
    """The fields the event actually stated, for a `model_copy` update.

    One rule, applied wherever a later event describes something an earlier one
    already answered: **a blank never erases a stated value.** Writing `None`
    over a real answer asserts an absence nobody declared, which is the mirror
    image of inventing a value — and this layer is not allowed to do either.
    The first event to state a field still writes it, because there is nothing
    to erase.
    """
    return {key: value for key, value in values.items() if value is not None}


def _band_reading(band: _PhaseState, payload: dict) -> None:
    """Record on the band what the gate read, field by field and stated only.

    Three independent fields, not one block: a gate may restate the validator's
    word without restating the paragraph of results, and it routinely does. Each
    is replaced only by a gate that states it — a gate saying nothing about
    `key_results` does not un-say what an earlier gate wrote.

    camel-quarkus-d2r3's `test` band is why this is a rule and not a nicety: ten
    gates grade that band, nine state `key_results` (the last of them 769
    characters) and the tenth, seq 250, states `""`. Under last-writer-wins the
    band rendered `key_results: null` — a phase that reported nine paragraphs of
    what it achieved, claiming nothing was ever stated.

    Every string goes through `_text` because the engine defaults `reason` and
    `key_results` to `""` and this schema rejects a blank cell: `""` in the
    ledger means "this gate did not state it", which is exactly the case this
    function leaves the standing value alone for.
    """
    validator_state = _text(payload.get("validator_state"))
    if validator_state is not None:
        band.validator_state = validator_state
    reason = _text(payload.get("reason"))
    if reason is not None:
        band.reason = reason
    key_results = _text(payload.get("key_results"))
    if key_results is not None:
        if len(key_results) > KEY_RESULTS_MAX_CHARS:
            key_results = key_results[: KEY_RESULTS_MAX_CHARS - 1] + "…"
        band.key_results = key_results


def _job_key(value: Any) -> str | None:
    """One job, one key: the ledger writes both `2c4d56b2fdca` and `job:2c4d56b2fdca`.

    A `tool_result` names a job bare in `metadata.job_id` and prefixed in
    `poll_ref`; a `loop_decision` names it prefixed; the job events name it
    bare. Two spellings of one id would leave a close unable to find the
    dispatch it is about.
    """
    text = _text(value)
    if text is None:
        return None
    return _text(text[4:]) if text.startswith("job:") else text


def _exit_codes(result: Any) -> list[int]:
    """Every exit code this result STATES, in the two places the engine writes one.

    `metadata.exit_code` is the tool's own reading; `metadata.execution` carries
    the runner's, and a result may hold either or both. The prose in `error` is
    not read: this layer takes numbers from fields, never from sentences.
    """
    if not isinstance(result, dict):
        return []
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    execution = metadata.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    codes: list[int] = []
    for value in (metadata.get("exit_code"), execution.get("exit_code")):
        if isinstance(value, int) and not isinstance(value, bool) and value not in codes:
            codes.append(value)
    return codes


def _signal_of(code: int) -> int | None:
    """The signal a 128+N exit names, or None for a status a program chose."""
    if _SIGNAL_EXIT_FLOOR < code <= _SIGNAL_EXIT_CEILING:
        return code - _SIGNAL_EXIT_FLOOR
    return None


def _elapsed(first: str, last: str) -> float | None:
    """Seconds between two ledger stamps, whatever mix of shapes they arrive in.

    Ledgers mix aware and naive stamps (control events carry `Z`, other
    artifacts do not). Subtracting one from the other raises, and a wall clock
    is never worth an exception — a naive stamp is read as UTC, which is the
    clock every SAG artifact is written against.
    """
    try:
        start, end = _as_utc(first), _as_utc(last)
    except ValueError:
        return None
    return (end - start).total_seconds()


def _as_utc(timestamp: str) -> datetime:
    moment = datetime.fromisoformat(timestamp)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


__all__ = [
    "KNOWN_EVENT_KINDS",
    "UNKNOWN_PHASE",
    "DeltaAccumulator",
    "TrajectoryReducer",
    "order_warnings",
]
