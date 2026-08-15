"""Batch and follow are the same fold over a session directory.

`build_trajectory` replays an archived session from its first event;
`follow_trajectory` tails a running one. They are not two derivations — both
push raw lines through the same `TrajectoryReducer`, and the only difference is
where the lines stop coming from. That is spec §1's "one code path, two feeds",
and it is what lets a batch replay stand as the idempotence fence for live
accumulation: replaying an archived session must produce the trajectory that
live accumulation would have produced.

Everything here opens files read-only. Nothing is written, moved, locked, or
truncated, and console logs are never read — the inputs are the control ledger,
the token ledger, the verdict, and the project record, all authoritative.

Four joins happen above the reducer, because they come from artifacts the
control stream does not carry:

- **tokens** — `token_usage.csv` bills one row per model RESPONSE, keyed by
  `iteration`. One response can open several turns, so the row bills exactly
  one of them (the first) and the siblings ride along unbilled: the run paid
  once. Controller turns are never billed — a forced action is the harness
  moving, not the model. A row that bills no turn at all is stated as
  `tokens_unattributed` rather than dropped, because unattributable spend is a
  fact about the ledger's holes, not a rounding error. Advisor rows are that
  advisor's own spend and never a turn's.
- **verdict and rates** — `verdict.json` exists only once a run has finished,
  so its absence is the normal state of a live session, not a hole.
- **project** — `project_meta.json` names the repository under test.
- **bytes, at the full tier only** — `contexts/full_outputs.jsonl` holds what
  the refs stand for. It is read through `OutputStorageManager`, the component
  that already owns that file's layout; this module never parses it itself.
  The summary tier never opens it, which is what keeps the timeline's main view
  cheap.

Both feeds apply those joins by the same rules, in `_Joiner` for the follower
and `_SessionSources.finish` for the replay, so that accumulating a session's
deltas yields the document a replay of the finished file produces — not a
document that agrees about turns and diverges about everything else.

A session that has not written its ledger yet is a running session, not an
error: the trajectory comes back empty with a `missing_control_events` warning.
A session directory that does not exist at all IS an error, and says so.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sag.agent.output_storage import OutputStorageManager
from sag.tools.base import is_output_storage_ref
from sag.trajectory.reducer import TrajectoryReducer, order_warnings
from sag.trajectory.schema import (
    DETAIL_TIERS,
    TokenUsage,
    Trajectory,
    TrajectoryDelta,
    Turn,
    Warning,
)

#: How long the follower waits before asking the ledger for more lines.
DEFAULT_POLL_SECONDS = 1.0

CONTROL_EVENTS_NAME = "control_events.jsonl"
TOKEN_USAGE_NAME = "token_usage.csv"
VERDICT_NAME = "verdict.json"
PROJECT_META_NAME = "project_meta.json"
FULL_OUTPUTS_NAME = "full_outputs.jsonl"
CONTEXTS_DIR = "contexts"

#: A session writes some artifacts at its root and some under `.setup_agent/`.
#: Both are looked at, root first, and neither is required to exist.
_ARTIFACT_ROOTS = (".", ".setup_agent")


def build_trajectory(session_dir: Path | str, *, detail: str = "summary") -> Trajectory:
    """Replay a whole session directory into one trajectory-v1 snapshot."""
    sources = _SessionSources(session_dir, detail=detail)
    reducer = TrajectoryReducer()
    ledger = sources.control_events()
    if ledger is None:
        return sources.finish(reducer.snapshot(), extra=[_missing_ledger(sources.path)])
    for line in _read_lines(ledger):
        reducer.feed(line)
    return sources.finish(reducer.snapshot())


def follow_trajectory(
    session_dir: Path | str,
    *,
    detail: str = "summary",
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[TrajectoryDelta]:
    """Tail a session's ledger, yielding deltas as its events land.

    The generator never ends on its own — a live run has no last line. Callers
    stop by breaking out of the loop; tests stop by having `sleep` raise.

    Folding every delta yielded here reproduces `build_trajectory` over the same
    bytes EXACTLY (`DeltaAccumulator` is the fold). One delta per event carries
    what that event changed; one further delta per poll carries what changed
    around the ledger — a verdict written, an output store that finally exists,
    a token row that bills nobody yet.
    """
    sources = _SessionSources(session_dir, detail=detail)
    reducer = TrajectoryReducer()
    joiner = _Joiner(sources)
    tail: _LedgerTail | None = None
    while True:
        if tail is None:
            ledger = sources.control_events()
            tail = _LedgerTail(ledger) if ledger is not None else None
        joiner.poll(ledger_missing=tail is None)
        if tail is not None:
            for line in tail.drain():
                decorated = joiner.wrap(reducer.feed(line))
                if decorated is not None:
                    yield decorated
        around = joiner.wrap(TrajectoryDelta())
        if around is not None:
            yield around
        sleep(poll_seconds)


class _SessionSources:
    """Read-only access to the artifacts of one session directory."""

    def __init__(self, session_dir: Path | str, *, detail: str) -> None:
        if detail not in DETAIL_TIERS:
            raise ValueError(f"detail tier must be one of {DETAIL_TIERS}, not {detail!r}")
        self.path = Path(session_dir)
        if not self.path.is_dir():
            raise FileNotFoundError(f"session directory does not exist: {self.path}")
        self.detail = detail
        self.outputs = _OutputResolver(self)

    def _locate(self, name: str) -> Path | None:
        for root in _ARTIFACT_ROOTS:
            candidate = self.path / root / name
            if candidate.is_file():
                return candidate
        return None

    def control_events(self) -> Path | None:
        return self._locate(CONTROL_EVENTS_NAME)

    def output_store(self) -> Path | None:
        return self._locate(f"{CONTEXTS_DIR}/{FULL_OUTPUTS_NAME}")

    def token_ledger(self) -> tuple[dict[int, TokenUsage], list[Warning]]:
        path = self._locate(TOKEN_USAGE_NAME)
        if path is None:
            return {}, []
        return _read_token_usage(path)

    def document(self, name: str) -> dict[str, Any] | None:
        path = self._locate(name)
        if path is None:
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def finish(self, snapshot: Trajectory, *, extra: list[Warning] | None = None) -> Trajectory:
        """Join the artifacts the control stream does not carry."""
        tokens, token_warnings = self.token_ledger()
        biller = _TokenBiller()
        turns = [biller.bill(turn, tokens) for turn in snapshot.turns]
        unattributed = biller.unattributed(tokens)
        refs = _output_refs(turns)
        resolved, output_warnings = self.outputs.resolve(refs) if self.full else (None, [])
        return snapshot.model_copy(
            update={
                "session": snapshot.session.model_copy(
                    update=_session_join(snapshot.session.run_id, self)
                ),
                "turns": turns,
                "warnings": order_warnings(
                    snapshot.warnings
                    + token_warnings
                    + ([unattributed] if unattributed else [])
                    + output_warnings
                    + list(extra or [])
                ),
                "outputs": resolved,
            }
        )

    @property
    def full(self) -> bool:
        return self.detail == "full"


class _OutputResolver:
    """The full tier's [C]: what the refs stand for, in verbatim bytes.

    The lookup goes through `OutputStorageManager`, which already knows how
    `contexts/full_outputs.jsonl` is laid out and how to recover a ref whose
    index entry is stale. Two rules keep this read-only and honest:

    - the manager is constructed only over a store that already exists, because
      constructing one over a missing directory would CREATE it, and this layer
      never writes into a session it is reading;
    - a store that is not there, and a ref the store does not carry, are
      warnings. Bytes that have not been written yet are the normal state of a
      live run, so nothing here raises and NOTHING NEGATIVE IS CACHED: the look
      is repeated every time bytes are asked for while no store has been found.
      Attaching to a session before it writes its first output is the ordinary
      way to watch a run start, and remembering that one absence would have
      meant the full tier resolved nothing for the rest of the session.
    """

    def __init__(self, sources: "_SessionSources") -> None:
        self._sources = sources
        self._manager: OutputStorageManager | None = None
        self._resolved: dict[str, str] = {}

    def _store(self) -> OutputStorageManager | None:
        if self._manager is None:
            store = self._sources.output_store()
            if store is not None:
                self._manager = OutputStorageManager(store.parent)
        return self._manager

    def resolve(self, refs: list[str]) -> tuple[dict[str, str], list[Warning]]:
        """Resolve every output ref named; state whichever ones would not."""
        if not refs:
            return {}, []
        manager = self._store()
        if manager is None:
            return {}, [
                Warning(
                    code="missing_output_store",
                    detail=(
                        f"{CONTEXTS_DIR}/{FULL_OUTPUTS_NAME} is not in {self._sources.path}; "
                        f"{len(refs)} ref(s) cannot be resolved to bytes"
                    ),
                    control_seq=None,
                )
            ]

        resolved: dict[str, str] = {}
        warnings: list[Warning] = []
        for ref in refs:
            if ref not in self._resolved:
                text = manager.retrieve_output(ref)
                if text is None:
                    warnings.append(
                        Warning(
                            code="unresolved_output_ref",
                            detail=ref,
                            control_seq=None,
                        )
                    )
                    continue
                self._resolved[ref] = text
            resolved[ref] = self._resolved[ref]
        return resolved, warnings


class _LedgerTail:
    """Yields whole lines appended to a growing file, and never a torn one."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._partial = b""

    def drain(self) -> list[str]:
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []
        if not chunk:
            return []
        complete, self._partial = _complete_lines(self._partial + chunk)
        return complete


def _complete_lines(buffer: bytes) -> tuple[list[str], bytes]:
    """Split off the lines that are finished, and keep the one that is not.

    A line exists once its newline does. A trailing fragment is a line still
    being written — the engine is mid-`write`, not the author of a malformed
    event — so it is withheld until its newline arrives.

    Batch replay and tail-follow both split here. They used to disagree: the
    replay ran `read_text().splitlines()`, which promotes a half-written tail
    into an event and then calls it malformed. That is the ONE case that only
    happens live, which makes it exactly the case the two feeds must not
    disagree on.

    The split is over BYTES so a multi-byte character straddling a read
    boundary is never decoded in halves.
    """
    *complete, partial = buffer.split(b"\n")
    return [raw.decode("utf-8", errors="replace") for raw in complete], partial


def _read_lines(path: Path) -> list[str]:
    try:
        return _complete_lines(path.read_bytes())[0]
    except OSError:
        return []


def _read_token_usage(path: Path) -> tuple[dict[int, TokenUsage], list[Warning]]:
    """Bill each iteration from its executor row; the first row wins."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, [_token_warning(f"{path.name} could not be read: {exc}")]

    billed: dict[int, TokenUsage] = {}
    try:
        for row in csv.DictReader(io.StringIO(text)):
            if (row.get("type") or "").strip() != "executor":
                continue
            iteration = int(str(row.get("iteration", "")).strip())
            if iteration in billed:
                continue
            billed[iteration] = TokenUsage(
                input=int(str(row.get("prompt_tokens", "")).strip()),
                output=int(str(row.get("completion_tokens", "")).strip()),
            )
    except (ValueError, csv.Error) as exc:
        return {}, [_token_warning(f"{path.name} is not the expected token ledger: {exc}")]
    return billed, []


def _token_warning(detail: str) -> Warning:
    return Warning(code="token_usage_unreadable", detail=detail, control_seq=None)


def _output_refs(turns: list[Turn]) -> list[str]:
    """Every output-store ref these turns name, in first-seen order.

    Envelope ids (`call.params_ref`) are deliberately not in scope: they are
    the ledger's own handles, resolvable from `control_events.jsonl`, and the
    output store has never heard of them.
    """
    refs: list[str] = []
    for turn in turns:
        candidates = [turn.window_ref, turn.observation.ref if turn.observation else None]
        for ref in candidates:
            if ref and is_output_storage_ref(ref) and ref not in refs:
                refs.append(ref)
    return refs


class _TokenBiller:
    """One executor row, one turn — because the run was charged once.

    `iteration` counts model RESPONSES, not turns: a response that asks for two
    tools opens two turns carrying the same iteration (kafka bills iterations 1,
    3 and 6 that way). Copying the row onto both invented spend, so the row
    bills the first model turn of its iteration and the siblings ride along.

    Controller turns are never billed. A forced action carries the iteration of
    the loop it interrupted, which is precisely the shape that would hand the
    harness's move the model's bill.

    Both feeds bill in the same order — a turn's `iteration` is set by its
    `loop_decision`, and a turn's decision always precedes the next turn's
    opening, so ledger order and turn order are the same order.
    """

    def __init__(self) -> None:
        self._claimed: dict[int, int] = {}

    def bill(self, turn: Turn, billed: dict[int, TokenUsage]) -> Turn:
        iteration = turn.iteration
        if iteration is None or turn.actor != "model" or iteration not in billed:
            return turn
        if self._claimed.setdefault(iteration, turn.turn_id) != turn.turn_id:
            return turn
        return turn.model_copy(update={"tokens": billed[iteration]})

    def unattributed(self, billed: dict[int, TokenUsage]) -> Warning | None:
        """Name the rows that billed nobody. Unattributable spend is a finding.

        Every orphan is a call the ledger left silent — kafka's ten phase and
        advisor calls emit no `loop_decision`, so 8 of its 19 rows join nothing.
        A token total that quietly omits 42% of a run is worse than no total.
        """
        orphans = sorted(set(billed) - set(self._claimed))
        if not orphans:
            return None
        return Warning(
            code="tokens_unattributed",
            detail=(
                f"{len(orphans)} executor row(s) bill no turn: "
                f"iteration(s) {', '.join(str(i) for i in orphans)}"
            ),
            control_seq=None,
        )


class _Joiner:
    """The follower's side of the joins `_SessionSources.finish` makes at once.

    A replay computes the joins over a finished session; a follower recomputes
    them as the session grows and says only what moved — added statements, the
    statements no longer true, the session fields that changed, and the bytes
    not sent yet. The rules are the ones `finish` uses, which is what makes the
    accumulated stream and the replay the same document.
    """

    def __init__(self, sources: "_SessionSources") -> None:
        self._sources = sources
        self._biller = _TokenBiller()
        self._tokens: dict[int, TokenUsage] = {}
        self._token_warnings: list[Warning] = []
        self._ledger_missing = True
        self._refs: list[str] = []
        self._sent: set[str] = set()
        self._held: tuple[Warning, ...] = ()
        self._around: dict[str, Any] = _session_join("", sources)
        self._reduced: dict[str, Any] = {}
        self._told: dict[str, Any] = {}

    def poll(self, *, ledger_missing: bool) -> None:
        """Re-read the artifacts that live beside the ledger, once per cycle.

        Once per cycle, not once per event: a verdict is written when a run
        ends and a token row when a response completes, so re-reading either on
        every line would buy nothing and cost a file read per event.
        """
        self._ledger_missing = ledger_missing
        self._tokens, self._token_warnings = self._sources.token_ledger()
        self._around = _session_join("", self._sources)

    def wrap(self, delta: TrajectoryDelta) -> TrajectoryDelta | None:
        turns = [self._biller.bill(turn, self._tokens) for turn in delta.turns]
        for ref in _output_refs(turns):
            if ref not in self._refs:
                self._refs.append(ref)
        outputs, unresolved = self._resolve()
        stated = self._restate(unresolved)
        self._reduced.update(delta.session_patch)
        joined = delta.model_copy(
            update={
                "turns": turns,
                "warnings": delta.warnings + stated[0],
                "retracted_warnings": delta.retracted_warnings + stated[1],
                "session_patch": self._session_patch(),
                "outputs": outputs,
            }
        )
        return None if _is_empty(joined) else joined

    def _resolve(self) -> tuple[dict[str, str] | None, list[Warning]]:
        """Bytes for the refs not sent yet — which is also the retry list.

        A ref that has been sent is done. A ref that has not is either new or
        one the store could not answer for last time, and a live store answers
        later than it is asked; so the unsent set is exactly what to try again.
        """
        if not self._sources.full:
            return None, []
        pending = [ref for ref in self._refs if ref not in self._sent]
        fresh, warnings = self._sources.outputs.resolve(pending)
        self._sent.update(fresh)
        return fresh, warnings

    def _restate(self, unresolved: list[Warning]) -> tuple[list[Warning], list[Warning]]:
        current = list(self._token_warnings)
        orphans = self._biller.unattributed(self._tokens)
        if orphans is not None:
            current.append(orphans)
        if self._ledger_missing:
            current.append(_missing_ledger(self._sources.path))
        current.extend(unresolved)
        held, self._held = self._held, tuple(current)
        return [w for w in current if w not in held], [w for w in held if w not in current]

    def _session_patch(self) -> dict[str, Any]:
        desired = dict(self._around)
        desired["run_id"] = self._reduced.get("run_id") or desired["run_id"]
        desired["wall_clock_seconds"] = self._reduced.get("wall_clock_seconds")
        patch = {k: v for k, v in desired.items() if k not in self._told or self._told[k] != v}
        self._told.update(patch)
        return patch


def _session_join(run_id: str, sources: "_SessionSources") -> dict[str, Any]:
    """What the artifacts beside the ledger say about the session as a whole."""
    verdict = sources.document(VERDICT_NAME) or {}
    meta = sources.document(PROJECT_META_NAME) or {}
    rates = verdict.get("rates")
    return {
        "run_id": run_id or _text(verdict.get("run_id")) or "",
        "project": _text(meta.get("project_name")),
        "verdict": _text(verdict.get("verdict")),
        "rates": rates if isinstance(rates, dict) else None,
    }


def _missing_ledger(path: Path) -> Warning:
    return Warning(
        code="missing_control_events",
        detail=f"{CONTROL_EVENTS_NAME} has not been written in {path}",
        control_seq=None,
    )


def _is_empty(delta: TrajectoryDelta) -> bool:
    return not (
        delta.turns
        or delta.phases
        or delta.annotations
        or delta.warnings
        or delta.retracted_warnings
        or delta.session_patch
        or delta.outputs
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = ["DEFAULT_POLL_SECONDS", "build_trajectory", "follow_trajectory"]
