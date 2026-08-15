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

Three joins happen above the reducer, because they come from artifacts the
control stream does not carry:

- **tokens** — `token_usage.csv` bills one row per model call; its `executor`
  rows join turns by `iteration`. Advisor rows are that advisor's own spend and
  never a turn's.
- **verdict and rates** — `verdict.json` exists only once a run has finished,
  so its absence is the normal state of a live session, not a hole.
- **project** — `project_meta.json` names the repository under test.

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

from sag.trajectory.reducer import TrajectoryReducer
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

#: A session writes some artifacts at its root and some under `.setup_agent/`.
#: Both are looked at, root first, and neither is required to exist.
_ARTIFACT_ROOTS = (".", ".setup_agent")


def build_trajectory(session_dir: Path | str, *, detail: str = "summary") -> Trajectory:
    """Replay a whole session directory into one trajectory-v1 snapshot."""
    sources = _SessionSources(session_dir, detail=detail)
    reducer = TrajectoryReducer(detail=detail)
    ledger = sources.control_events()
    if ledger is None:
        return sources.finish(
            reducer.snapshot(),
            extra=[
                Warning(
                    code="missing_control_events",
                    detail=f"{CONTROL_EVENTS_NAME} has not been written in {sources.path}",
                    control_seq=None,
                )
            ],
        )
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
    """Tail a session's ledger, yielding one delta per event as it lands.

    The generator never ends on its own — a live run has no last line. Callers
    stop by breaking out of the loop; tests stop by having `sleep` raise. Each
    yielded delta carries the full current state of every turn the event
    touched, so upserting by `turn_id` reproduces `build_trajectory` exactly.
    """
    sources = _SessionSources(session_dir, detail=detail)
    reducer = TrajectoryReducer(detail=detail)
    tail: _LedgerTail | None = None
    while True:
        if tail is None:
            ledger = sources.control_events()
            tail = _LedgerTail(ledger) if ledger is not None else None
        if tail is not None:
            tokens = sources.token_ledger()[0]
            for line in tail.drain():
                delta = reducer.feed(line)
                if _is_empty(delta):
                    continue
                yield delta.model_copy(
                    update={"turns": [_with_tokens(turn, tokens) for turn in delta.turns]}
                )
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

    def _locate(self, name: str) -> Path | None:
        for root in _ARTIFACT_ROOTS:
            candidate = self.path / root / name
            if candidate.is_file():
                return candidate
        return None

    def control_events(self) -> Path | None:
        return self._locate(CONTROL_EVENTS_NAME)

    def token_ledger(self) -> tuple[dict[int, TokenUsage], list[Warning]]:
        path = self._locate(TOKEN_USAGE_NAME)
        if path is None:
            return {}, []
        return _read_token_usage(path)

    def _document(self, name: str) -> dict[str, Any] | None:
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
        session = snapshot.session
        verdict = self._document(VERDICT_NAME) or {}
        meta = self._document(PROJECT_META_NAME) or {}
        return snapshot.model_copy(
            update={
                "session": session.model_copy(
                    update={
                        "run_id": session.run_id or _text(verdict.get("run_id")) or "",
                        "project": session.project or _text(meta.get("project_name")),
                        "verdict": _text(verdict.get("verdict")),
                        "rates": (
                            verdict.get("rates") if isinstance(verdict.get("rates"), dict) else None
                        ),
                    }
                ),
                "turns": [_with_tokens(turn, tokens) for turn in snapshot.turns],
                "warnings": snapshot.warnings + token_warnings + list(extra or []),
            }
        )


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
        # Split on bytes so a multi-byte character straddling a read boundary
        # is never decoded in halves; the trailing fragment waits for its
        # newline, because a line without one is a line still being written.
        *complete, self._partial = (self._partial + chunk).split(b"\n")
        return [raw.decode("utf-8", errors="replace") for raw in complete]


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
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


def _with_tokens(turn: Turn, billed: dict[int, TokenUsage]) -> Turn:
    if turn.iteration is None or turn.iteration not in billed:
        return turn
    return turn.model_copy(update={"tokens": billed[turn.iteration]})


def _is_empty(delta: TrajectoryDelta) -> bool:
    return not (delta.turns or delta.annotations or delta.warnings or delta.session_patch)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = ["DEFAULT_POLL_SECONDS", "build_trajectory", "follow_trajectory"]
