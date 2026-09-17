"""What a finished run's own files say about it, read once for every surface.

Four places build a result card — the end of a CLI run, `sag result`, the web
API and the written report — and each of them used to fold the run's ledger
itself. Two of those readers drifted: one learned to bill tokens and the other
did not, so a single run reported 117068 tokens on the terminal and nothing at
all through the API. A reader comparing the two saw a contradiction where the
honest answer was one number.

So there is one reader, and it answers with a mapping whose keys are the
keyword names `build_result_card` takes. Every site splats it whole
(`build_result_card(payload, **read_run_counts(directory))`), which is what
makes the next divergence impossible rather than merely unlikely: a surface
cannot fill four of the five counts and forget the fifth, because it never
names them one at a time.

The counts travel as a group for the same reason. They are five readings of one
replay; asking the ledger five separate times could answer five different
things about one run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from sag.agent.verdict_finalizer import ReportDeliveryStatus
from sag.trajectory.builder import CONTROL_EVENTS_NAME, build_trajectory, resolve_session_dir

#: The keyword names `build_result_card` takes for the group. Named here so a
#: test can check the mapping against the function's own signature.
RUN_COUNT_KEYS = (
    "trajectory_session",
    "turn_count",
    "tool_calls",
    "tool_failures",
    "token_usage",
)

#: What a mirrored report is called beside the ledger. The report tool writes
#: `setup-report-<timestamp>.md`, and a `--record` run copies it out under that
#: same name.
_REPORT_GLOB = "setup-report*.md"

#: Where to look for it, relative to a directory that holds a run. A session
#: directory keeps it at its own root; a campaign archive keeps the host
#: session, and the report with it, under `logs/`. Both are bounded patterns
#: rather than a walk: a search that recurses would find another run's report
#: in a directory holding several.
_REPORT_PATTERNS = (_REPORT_GLOB, f"logs/*/{_REPORT_GLOB}")


def read_run_counts(session_dir: Path | str | None) -> dict[str, Any]:
    """Fold a run's own ledger into the counts every result card states.

    Every count is absent unless the ledger supplied it: a run whose turns were
    never recorded reports no turns, not zero turns, and a directory that
    cannot be read leaves the whole group absent rather than raising.

    The failure signal is the error code or failure signature the observation
    carries, which is the one definition of a failed call shared by all four
    surfaces.
    """

    counts: dict[str, Any] = {key: None for key in RUN_COUNT_KEYS}
    if session_dir is None or (isinstance(session_dir, str) and not session_dir):
        return counts
    try:
        document = build_trajectory(resolve_session_dir(session_dir))
    except Exception as exc:
        # Broad on purpose: this is a reader, and no card is worth a traceback
        # on a surface that was only ever going to print a dash.
        logger.debug(f"run counts for the result card are unavailable: {exc}")
        return counts

    counts["trajectory_session"] = document.session.model_dump(mode="json")
    turns = tuple(document.turns)
    if not turns:
        return counts
    counts["turn_count"] = len(turns)
    counts["tool_calls"] = sum(1 for turn in turns if turn.call is not None)
    counts["tool_failures"] = sum(
        1
        for turn in turns
        if turn.observation is not None
        and (turn.observation.error_code or turn.observation.failure_signature)
    )
    # The bill each turn carries, not the token file's raw rows: the derivation
    # has already decided which row pays for which turn, and a second reader
    # adding the rows up itself would double-count the duplicates it dropped.
    billed = tuple(
        {"prompt_tokens": turn.tokens.input, "output_tokens": turn.tokens.output}
        for turn in turns
        if turn.tokens is not None
    )
    counts["token_usage"] = billed or None
    return counts


@dataclass(frozen=True)
class ReportDeliveryOnly:
    """What a reader outside the run can state about how it ended: the report.

    The result card's Report row reads ``report_delivery_status`` and its Setup
    row reads ``termination``. A surface that reads a finished run's artifacts
    from outside the run can say whether the report document exists — it is
    holding one — but nothing it reads says whether the run completed or was
    cut short. Carrying no ``termination`` is how the Setup row is told that,
    and it reads the same as the run ending normally.
    """

    report_delivery_status: ReportDeliveryStatus


def recorded_report(*directories: Path | None) -> tuple[ReportDeliveryOnly | None, str | None]:
    """The report a recorded run left behind, and the word for having found it.

    Nothing is constructed out of the knowledge that a report ought to exist.
    Without a document on disk this answers with nothing at all, and the Report
    row then says the run did not record whether a report was written — which
    is true of a directory that holds none. Asserting the absence instead, as
    an unfilled ``termination`` does, tells a reader no report was written
    while the file sits in the directory being read.
    """

    for directory in directories:
        if directory is None:
            continue
        try:
            found = sorted(
                path
                for pattern in _REPORT_PATTERNS
                for path in directory.glob(pattern)
                if path.is_file()
            )
        except OSError:
            continue
        if found:
            return (
                ReportDeliveryOnly(report_delivery_status=ReportDeliveryStatus("delivered")),
                str(found[-1]),
            )
    return None, None


def find_recorded_session(
    run_id: str | None, *, logs_root: Path | str = Path("logs")
) -> Path | None:
    """The host session directory that holds this run, or nothing.

    Named by the run it belongs to, never by "the newest directory of this
    project": pointing a reader at another run's evidence is worse than
    pointing at none. The ledger is asked because it is what a reader of that
    directory is served — the run_id in its first event is the run these very
    events state.
    """

    if not run_id:
        return None
    root = Path(logs_root)
    if not root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for session_dir in root.glob("session_*"):
        ledger = session_dir / CONTROL_EVENTS_NAME
        if not ledger.is_file():
            continue
        try:
            candidates.append((ledger.stat().st_mtime, session_dir))
        except OSError:
            continue
    for _, session_dir in sorted(candidates, reverse=True):
        if _ledger_run_id(session_dir / CONTROL_EVENTS_NAME) == run_id:
            return session_dir
    return None


def _ledger_run_id(ledger: Path) -> str | None:
    """The run the ledger's first readable event names, without folding it."""

    try:
        with ledger.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.lstrip().startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                # Both shapes are written: later events carry the run at the
                # top level, and the first event of a session names it only
                # inside its payload. A reader that knows one of them answers
                # "no run here" for the sessions written the other way.
                for stated in (event.get("run_id"), (payload or {}).get("run_id")):
                    if isinstance(stated, str) and stated:
                        return stated
    except OSError:
        return None
    return None


__all__ = [
    "RUN_COUNT_KEYS",
    "ReportDeliveryOnly",
    "find_recorded_session",
    "read_run_counts",
    "recorded_report",
]
