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


class ObservationInfo(_TrajectoryModel):
    """[C] of the quad: what came back, and how it failed if it failed."""

    ref: str | None = None
    error_code: str | None = None
    failure_signature: str | None = None


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


class Annotation(_TrajectoryModel):
    """Something worth marking on a turn that is not part of its skeleton."""

    kind: AnnotationKind
    turn_id: int
    data: dict[str, Any] = Field(default_factory=dict)


class Warning(_TrajectoryModel):  # noqa: A001 - the schema's own word for a hole
    """A hole in the ledger, or an event this version does not understand.

    Warnings are how the derivation stays honest about pre-closure sessions:
    the reducer states the gap and keeps folding, and never raises.
    """

    code: str
    detail: str
    control_seq: int | None = None


class Trajectory(_TrajectoryModel):
    """The whole derived view of one session at one moment."""

    schema_version: int = SCHEMA_VERSION
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

    `turns` carries the full current state of every turn the event touched;
    consumers upsert by `turn_id`. Accumulating deltas that way reproduces the
    snapshot exactly — which is what makes live follow and batch replay the
    same fold.
    """

    turns: list[Turn] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    session_patch: dict[str, Any] = Field(default_factory=dict)
    #: The full tier's bytes for the refs THIS delta's turns name, on the same
    #: terms as `Trajectory.outputs`. A follower that upserts turns by id can
    #: merge these the same way, and a summary follower never carries any.
    outputs: dict[str, str] | None = None


__all__ = [
    "SCHEMA_VERSION",
    "DETAIL_TIERS",
    "Actor",
    "Annotation",
    "AnnotationKind",
    "CallInfo",
    "DetailTier",
    "GateInfo",
    "ObservationInfo",
    "PhaseInfo",
    "SessionInfo",
    "TokenUsage",
    "Trajectory",
    "TrajectoryDelta",
    "Turn",
    "Warning",
]
