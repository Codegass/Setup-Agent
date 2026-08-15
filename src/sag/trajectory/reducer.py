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
- `gate_decision` / `gate_outcome_revised` attach the word the gate delivered
  to the turn that carried it, and to the phase band it graded.
- `phase_transition` closes the band of the phase the run is in and opens a new
  segment for the phase it names.

**Phase bands are contiguous segments.** Only a `phase_transition` opens one.
Every other event that names a phase — a late `loop_decision` for a call made
before the transition, a gate grading a phase already closed — attaches to that
phase's most recent segment instead of appending a duplicate band, so the
timeline never draws a phantom segment and the run is never in two places at
once. A phase genuinely re-entered gets its own segment, because a transition
said so.

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

from sag.agent.control_events import CONTROL_EVENT_KINDS
from sag.trajectory.schema import (
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

#: The engine's own event vocabulary is the definition of "known". Anything
#: outside it is a kind this schema version has never heard of.
KNOWN_EVENT_KINDS = frozenset(CONTROL_EVENT_KINDS)

#: What a turn's phase is called before any event has said which phase it is in.
UNKNOWN_PHASE = "unknown"


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
    tokens: TokenUsage | None = None
    t0: str | None = None
    t1: str | None = None
    control_seq: list[int] = field(default_factory=list)
    has_decision: bool = False
    #: Only a `tool_result` (or, after Pillar 1, a typed refusal) answers a
    #: call. A `loop_decision` may describe the answer, but it is not one — so
    #: a turn whose result never came still says so, even when the decision
    #: told us its error code.
    has_result: bool = False

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

    def render(self) -> PhaseInfo:
        return PhaseInfo(name=self.name, termination=self.termination, gates=list(self.gates))


class TrajectoryReducer:
    """Folds control-event lines into a trajectory-v1 view, incrementally."""

    def __init__(self) -> None:
        self._turns: list[_TurnState] = []
        self._by_envelope: dict[str, _TurnState] = {}
        self._phases: list[_PhaseState] = []
        self._phase: str = UNKNOWN_PHASE
        self._annotations: list[Annotation] = []
        self._event_warnings: list[Warning] = []
        #: What each turn's holes were the last time a delta said so. The diff
        #: against the turn's current holes is what a delta ships.
        self._claimed: dict[int, tuple[Warning, ...]] = {}
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
        return order_warnings(
            list(self._event_warnings) + [w for turn in self._turns for w in _turn_warnings(turn)]
        )

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
        return self._phase

    def _phase_band(self, name: str, *, open_segment: bool = False) -> _PhaseState:
        """The band a phase's events belong to, opening a segment only on demand.

        A phase is re-entered only when a `phase_transition` says so, and that
        is the only caller that passes `open_segment`. Every other mention of a
        phase — including one that arrives after the phase closed — lands on
        that phase's most recent segment, because bands are contiguous stretches
        of the run and a stray late event does not start a new stretch.
        """
        if not open_segment:
            for phase in reversed(self._phases):
                if phase.name == name:
                    return phase
        band = _PhaseState(name=name)
        self._phases.append(band)
        return band

    def _enter(self, name: str, *, open_segment: bool = False) -> _PhaseState:
        """Say where the run is now, and hand back the band it is in."""
        self._phase = name
        return self._phase_band(name, open_segment=open_segment)

    def _adopt_phase(self, turn: _TurnState, phase: Any) -> None:
        if isinstance(phase, str) and phase:
            turn.phase = phase
            self._enter(phase)

    # ---- handlers -----------------------------------------------------

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
        turn.call = CallInfo(tool=tool, params_ref=envelope_id)
        turn.envelope_id = envelope_id
        turn.t0 = timestamp
        turn.touch(sequence)
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
        turn.call = CallInfo(tool=tool, params_ref=envelope_id)
        turn.envelope_id = envelope_id
        turn.t0 = timestamp
        turn.touch(sequence)
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
        turn.has_result = True
        turn.t1 = timestamp
        turn.touch(sequence)
        self._adopt_phase(turn, payload.get("source_phase"))
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
        self._attach_gate(collector, gate, sequence, payload.get("phase"))

    def _on_gate_outcome_revised(
        self, collector: "_Delta", sequence: int | None, timestamp: str | None, payload: dict
    ) -> None:
        word = _text(payload.get("revised_outcome"))
        if word is None:
            collector.warn(
                "gate_without_a_word",
                "gate_outcome_revised carries no revised_outcome",
                sequence,
            )
            return
        gate = GateInfo(
            word=word,
            decision_id=_text(payload.get("revised_decision_id")),
            supersedes=_text(payload.get("delivered_decision_id")),
        )
        self._attach_gate(collector, gate, sequence, payload.get("phase"))

    def _attach_gate(
        self, collector: "_Delta", gate: GateInfo, sequence: int | None, phase: Any
    ) -> None:
        band = self._phase_band(phase) if isinstance(phase, str) and phase else None
        if band is not None:
            band.gates.append(gate)
        turn = self._open_turn
        if turn is None:
            collector.warn(
                "orphan_gate_decision",
                f"a gate delivered {gate.word!r} before any turn opened",
                sequence,
            )
            return
        turn.gate = gate
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
        """
        kind = _text(payload.get("expected_kind"))
        leaving = next((band for band in reversed(self._phases) if band.name == self._phase), None)
        if leaving is not None and leaving.termination is None:
            leaving.termination = kind
        target = _text(payload.get("expected_target"))
        if target:
            self._enter(target, open_segment=True)

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
        "tool_result": _on_tool_result,
        "loop_decision": _on_loop_decision,
        "gate_decision": _on_gate_decision,
        "gate_outcome_revised": _on_gate_outcome_revised,
        "phase_transition": _on_phase_transition,
        "repair_context_opened": _on_repair_context_opened,
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

    def warn(self, code: str, detail: str, control_seq: int | None) -> None:
        """State something about THIS line. A line does not change its mind."""
        warning = Warning(code=code, detail=detail, control_seq=control_seq)
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
    holes: list[Warning] = []
    if turn.call is None:
        holes.append(_hole(turn, "missing_envelope", "has a loop_decision but no action_envelope"))
    if not turn.has_result:
        holes.append(_hole(turn, "missing_tool_result", "has no tool_result and no typed refusal"))
    if turn.call is not None and not turn.has_decision:
        holes.append(
            _hole(
                turn,
                "missing_loop_decision",
                f"called {turn.call.tool!r} and emitted no loop_decision",
            )
        )
    return holes


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
    """
    if existing is None:
        return ObservationInfo(ref=ref, error_code=error_code, failure_signature=failure_signature)
    return ObservationInfo(
        ref=existing.ref or ref,
        error_code=existing.error_code or error_code,
        failure_signature=existing.failure_signature or failure_signature,
    )


def _text(value: Any) -> str | None:
    """Empty strings in the ledger mean "not stated"; say so as None."""
    if isinstance(value, str) and value:
        return value
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
