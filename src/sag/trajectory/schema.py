"""trajectory-v1 — the versioned read model of one run.

This module is pure shape: no I/O, no engine imports, no derivation. It states
what a trajectory IS so that the reducer (`reducer.py`) and every consumer
(CLI, web API, timeline) agree on one vocabulary and one version number.

Three rules carry over from the design (spec §1/§3):

- **Read-only.** Nothing here writes, reorders, or mutates a source file. The
  models are built fresh by the reducer on every snapshot; no consumer is
  expected to mutate one in place.
- **Strict.** Every model forbids extra fields, following the `_StrictPayload`
  style of `sag.agent.control_events`: an unexpected field is a bug surfaced
  now, not a value silently swallowed.
- **Warnings, not exceptions.** A hole in the ledger or an event kind this
  version does not know is a `Warning` row, never a raised error. That is why
  `Warning` is a first-class model and why every join field is optional.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

#: The two detail tiers of spec §3, exactly. `summary` is names, codes, timing
#: and tokens (the timeline's main view); `full` additionally resolves refs to
#: verbatim bytes (the [A]/[B]/[C]/[D] quad on expand).
DETAIL_TIERS: tuple[str, ...] = ("summary", "full")
DetailTier = Literal["summary", "full"]

#: How long a one-line summary may be. Long enough for a Maven command, short
#: enough that a turn stays one terminal row at 80 columns.
SUMMARY_MAX_CHARS = 80
#: A phase's key results are a paragraph the gate wrote, not a line.
KEY_RESULTS_MAX_CHARS = 400

ObservationOutcome = Literal["ok", "failed", "refused", "pending", "cancelled"]
ValidatorState = Literal["green", "partial", "red", "unavailable"]

#: Who moved. A model turn is one the model asked for; a controller turn is one
#: the harness took on its own (forced actions, engine-generated gates).
Actor = Literal["model", "controller"]

AnnotationKind = Literal["refusal", "forced", "repair_context", "recurrence", "conflict"]


class _TrajectoryModel(BaseModel):
    """Strict base for every trajectory-v1 model (mirrors `_StrictPayload`)."""

    model_config = ConfigDict(extra="forbid")


class SessionInfo(_TrajectoryModel):
    """What the run was, and how it ended if it has ended."""

    run_id: str
    project: str | None = None
    verdict: str | None = None
    rates: dict[str, Any] | None = None
    wall_clock_seconds: float | None = None


class CallInfo(_TrajectoryModel):
    """[B] of the quad: what was asked of the tool.

    `params_ref` names where the exact params live rather than copying them,
    so the summary tier stays small and the full tier can resolve bytes.
    """

    tool: str
    params_ref: str | None = None
    #: What this call asked for, in one line. Derived from the envelope's exact
    #: params; `None` when the tool's shape is unknown to the summariser, never
    #: an empty string, so "no summary" and "an empty summary" stay distinct.
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)


class ObservationInfo(_TrajectoryModel):
    """[C] of the quad: what came back, and how it failed if it failed.

    `ref` is what the model READ — the observation the engine delivered, which
    is the tool's text plus whatever the engine appended to it. `evidence_ref`
    is what the TOOL wrote, when the two are not the same bytes. Both are
    named, because a reader investigating an observation wants the copy the
    model saw and the copy the tool produced, and neither stands in for the
    other.
    """

    ref: str | None = None
    evidence_ref: str | None = None
    error_code: str | None = None
    failure_signature: str | None = None
    #: How the call came out, as one of five words. `pending` is a dispatched
    #: job with no terminal exit yet; `cancelled` is a call a batch break
    #: stopped before it dispatched anything.
    outcome: ObservationOutcome | None = None
    #: What came back, in one line.
    summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)


class GateInfo(_TrajectoryModel):
    """The word a gate delivered for this turn.

    `word` is the outcome the gate actually delivered to the model — not the
    outcome the model claimed. `supersedes` names the earlier grading this one
    replaced, so a reader can walk the chain instead of guessing at it.
    """

    word: str
    decision_id: str | None = None
    supersedes: str | None = None


class TokenUsage(_TrajectoryModel):
    """Prompt and completion tokens attributed to the turn."""

    input: int
    output: int


class Turn(_TrajectoryModel):
    """One turn: who moved, what they asked, what they heard, what it cost.

    `control_seq` carries every control-event sequence number folded into this
    turn, so any row can be descended to the authoritative bytes it came from.
    """

    turn_id: int = Field(ge=1)
    phase: str
    iteration: int | None = None
    actor: Actor
    window_ref: str | None = None
    #: [A] whole: every component the turn's record named, in render order, so
    #: resolving them in list order reproduces the messages array. `window_ref`
    #: stays the row's primary handle — one cell holds one ref — and this is
    #: what the full tier expands. `None` means no record stated a window; the
    #: list is never empty, because a digest naming no component states no
    #: window at all. A `window_truncated:<n>` entry names a CUT rather than
    #: bytes, and is declared out of store rather than resolved.
    window_components: list[str] | None = None
    call: CallInfo | None = None
    observation: ObservationInfo | None = None
    gate: GateInfo | None = None
    tokens: TokenUsage | None = None
    t0: str | None = None
    t1: str | None = None
    control_seq: list[int] = Field(default_factory=list)


class PhaseInfo(_TrajectoryModel):
    """A phase band: its name, how it ended, and the gates decided inside it."""

    name: str
    termination: str | None = None
    gates: list[GateInfo] = Field(default_factory=list)
    #: What the validator observed when the phase closed.
    validator_state: ValidatorState | None = None
    #: Why the gate decided what it decided.
    reason: str | None = None
    #: What the phase reported it achieved, as the gate recorded it.
    key_results: str | None = Field(default=None, max_length=KEY_RESULTS_MAX_CHARS)


class Annotation(_TrajectoryModel):
    """Something worth marking on a turn that is not part of its skeleton."""

    kind: AnnotationKind
    turn_id: int
    data: dict[str, Any] = Field(default_factory=dict)


class Warning(_TrajectoryModel):  # noqa: A001 - the schema's own word for a hole
    """A hole in the ledger, or an event this version does not understand.

    Warnings are how the derivation stays honest about pre-closure sessions:
    the reducer states the gap and keeps folding, and never raises.

    A warning is a STATEMENT about the ledger as it stands, so it is frozen and
    compared by value: the same hole stated twice is one hole, and a hole that
    fills is withdrawn (`TrajectoryDelta.retracted_warnings`) rather than
    edited. `turn_id` names the turn the statement is about, when it is about a
    turn; a statement about one line carries `control_seq` instead, and one
    about the run as a whole carries neither.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    detail: str
    control_seq: int | None = None
    turn_id: int | None = None


def warning_order(warning: Warning) -> tuple[Any, ...]:
    """The one order warnings are listed in, wherever they are assembled.

    Batch replay sorts a list it built in one pass; a live consumer sorts a set
    it accumulated over a session of additions and retractions. The two can only
    be compared if the order is TOTAL — a key that leaves ties would let the same
    set of statements render as two different documents.
    """
    return (
        warning.control_seq is None,
        warning.control_seq or 0,
        warning.turn_id is None,
        warning.turn_id or 0,
        warning.code,
        warning.detail,
    )


class Trajectory(_TrajectoryModel):
    """The whole derived view of one session at one moment."""

    #: Pinned, not merely defaulted: a document claiming a version this code does
    #: not implement must fail loudly instead of being read for the fields that
    #: happen to overlap.
    schema_version: Literal[1] = SCHEMA_VERSION
    session: SessionInfo
    phases: list[PhaseInfo] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    #: The full tier's bytes: every resolvable ref the turns name, mapped to
    #: the verbatim output it stands for. `None` at the summary tier means "not
    #: asked for"; an empty dict means "asked for, and nothing resolved" — a
    #: distinction a reader needs, so the two are never collapsed.
    outputs: dict[str, str] | None = None


class TrajectoryDelta(_TrajectoryModel):
    """What one fed event changed.

    Accumulating every delta of a session reproduces `Trajectory` EXACTLY —
    that is the contract, and `sag.trajectory.reducer.DeltaAccumulator` is its
    reference implementation. Four accumulation rules, one per field:

    - `turns` carries the full current state of every turn the event touched;
      consumers upsert by `turn_id`.
    - `annotations` are appended; an annotation is a fact about an event that
      already happened, and facts do not change their minds.
    - `warnings` are ADDED to the set of statements currently held, and
      `retracted_warnings` are removed from it. A hole is claimed the moment it
      is true — including on the turn still open, which is the one a live
      watcher most needs stated — and withdrawn when the missing piece arrives.
      Statements are compared by value, so restating one changes nothing.
    - `phases` is the complete banding whenever any band changed, and `None`
      when it did not; the list is small and wholly restated rather than
      diffed.
    - `session_patch` carries only the session fields whose value changed.
    """

    turns: list[Turn] = Field(default_factory=list)
    phases: list[PhaseInfo] | None = None
    annotations: list[Annotation] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    retracted_warnings: list[Warning] = Field(default_factory=list)
    session_patch: dict[str, Any] = Field(default_factory=dict)
    #: The full tier's bytes for the refs THIS delta's turns name, on the same
    #: terms as `Trajectory.outputs`. A follower that upserts turns by id can
    #: merge these the same way, and a summary follower never carries any.
    outputs: dict[str, str] | None = None


__all__ = [
    "SCHEMA_VERSION",
    "DETAIL_TIERS",
    "SUMMARY_MAX_CHARS",
    "KEY_RESULTS_MAX_CHARS",
    "Actor",
    "Annotation",
    "AnnotationKind",
    "CallInfo",
    "DetailTier",
    "GateInfo",
    "ObservationInfo",
    "ObservationOutcome",
    "PhaseInfo",
    "SessionInfo",
    "TokenUsage",
    "Trajectory",
    "TrajectoryDelta",
    "Turn",
    "ValidatorState",
    "Warning",
    "warning_order",
]
