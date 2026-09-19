"""Render one finished run as the document its reader opens.

Nothing here derives a result. The card is built by `sag.result_card` from the
run's verdict and its ledger, exactly as the terminal block and the web band
build theirs; this module arranges what those derivations already state, adds
the run's own identity, and writes it down.

Two rules bind every section below:

- **The record or nothing.** Every line traces to a named field of a file the
  run wrote. A section whose source is empty prints nothing at all rather than
  a heading over a blank.
- **Read at one instant.** The whole document is rendered after the run's last
  event, so the counts it states are the counts every other surface states.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from sag import __version__
from sag.result_card import build_result_card
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import RunResultCard
from sag.result_card.run_evidence import read_run_counts, recorded_report
from sag.trajectory.builder import control_events_path, resolve_session_dir

#: A run's own artifacts, by the names it writes them under, and the two roots
#: it writes them at — the same pair `sag result` reads, so a document and the
#: block printed beside it are never read from different copies of one run.
_VERDICT_NAME = "verdict.json"
_RUN_PIN_NAME = "run-pin.json"
_PROJECT_META_NAME = "project_meta.json"
_MODULE_METRICS_NAME = "module_metrics.json"
_REPORT_METRICS_NAME = "report_metrics.json"
_ENV_OVERLAY_NAME = "env_overlay.json"
_RUN_ARTIFACT_ROOTS = (".", ".setup_agent")

#: How many characters of a commit a reader is shown — the same seven the
#: terminal block prints (`console/result_block.py`), so one run's commit is
#: spelled one way wherever it appears.
_COMMIT_CHARS = 7

#: How the run's end is written down. The run's own host clock, because the
#: timestamps a run's other artifacts carry are that clock too, and a reader
#: comparing them should not have to apply an offset in their head.
_WRITTEN_FORMAT = "%Y-%m-%d %H:%M:%S"

#: How the document is named, and the format the name's timestamp takes. The
#: same name the report phase's own deliverable carries, so one run leaves one
#: report rather than two files a reader has to choose between.
_REPORT_NAME_FORMAT = "setup-report-%Y%m%d-%H%M%S.md"


def read_run_document(base: Path, name: str) -> dict[str, Any] | None:
    """Read one of a run's JSON artifacts, or nothing when it is not readable."""

    for root in _RUN_ARTIFACT_ROOTS:
        path = base / root / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def report_name(report_path: str | None, session_dir: str | None) -> str | None:
    """Name the report the way a reader of that session directory reaches it.

    A document sitting beside the ledger is named by its file: the surfaces
    that print it have already named the directory, and a second copy of the
    path is both wider than the column and no more use for opening it.
    """

    if not report_path or not session_dir:
        return report_path
    try:
        return str(Path(report_path).relative_to(session_dir))
    except ValueError:
        return report_path


def run_ended_at(session_dir: Path | str) -> datetime | None:
    """The instant of the run's last recorded event, or nothing.

    The document is written at that instant, and says so. The last event is
    read from the ledger rather than from the last turn: a run's closing
    events — its final grading, its phase transition, the last artifact it
    published — land after the turn that caused them, and the report tool's
    old habit of stating a time before the run's own end is the whole reason
    this package exists.
    """

    ledger = control_events_path(session_dir)
    if ledger is None:
        return None
    stamp: str | None = None
    try:
        with ledger.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.lstrip().startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, Mapping):
                    recorded = event.get("timestamp")
                    if isinstance(recorded, str) and recorded:
                        stamp = recorded
    except OSError as exc:
        logger.debug(f"the run's end could not be read from {ledger}: {exc}")
        return None
    if stamp is None:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    # A naive stamp is read as UTC, which is the clock every SAG artifact is
    # written against (`trajectory/reducer.py` reads them the same way).
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def setup_report_name(session_dir: Path | str) -> str:
    """What to call this run's report, when the run has not already named one.

    A run that mirrored its artifacts out already has a file to replace, and
    replacing it is what keeps one run to one report. This names the rest, off
    the run's own end so that re-rendering an archived run reproduces the name
    it was written under rather than inventing today's.
    """

    ended = run_ended_at(session_dir) or datetime.now()
    return ended.strftime(_REPORT_NAME_FORMAT)


def _mapping(value: Any) -> dict[str, Any]:
    """One shape for an artifact that arrives as a file, a dict or a model."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _card_for(directory: Path, base: Path) -> RunResultCard | None:
    """Build the run's card from its own directory, the way `sag result` does."""

    verdict = read_run_document(base, _VERDICT_NAME)
    if verdict is None:
        return None
    meta = read_run_document(base, _PROJECT_META_NAME) or {}
    termination, found_report = recorded_report(directory, base)
    return build_result_card(
        verdict,
        module_metrics=read_run_document(base, _MODULE_METRICS_NAME),
        report_metrics=read_run_document(base, _REPORT_METRICS_NAME),
        run_pin=read_run_document(base, _RUN_PIN_NAME),
        **read_run_counts(base),
        termination=termination,
        project=meta.get("project_name") or None,
        goal=meta.get("goal") or None,
        session_dir=str(directory),
        report_path=report_name(found_report, str(directory)),
    )


def _header(
    card: RunResultCard,
    *,
    project_meta: Mapping[str, Any],
    run_pin: Mapping[str, Any],
    ended_at: datetime | None,
) -> list[str]:
    """One line of identity, one of provenance. Section C.1 of the spec."""

    project = card.project or project_meta.get("project_name")
    lines = [f"# {project} — setup report" if project else "# Setup report", ""]

    # The commit the card carries, or the one the run pinned before it started.
    # Both are the record's word for what was checked out; neither is derived.
    commit = card.commit or run_pin.get("target_repo_sha")
    short = str(commit)[:_COMMIT_CHARS] if commit else None
    url = project_meta.get("project_url")
    repository = " at ".join(part for part in (url, f"`{short}`" if short else None) if part)
    if repository:
        lines.append(f"**Repository** {repository}")

    provenance = [
        f"`{card.run_id}`" if card.run_id else None,
        f"Setup-Agent v{__version__}" if __version__ else None,
        f"written {ended_at.strftime(_WRITTEN_FORMAT)}" if ended_at else None,
    ]
    stated = " · ".join(part for part in provenance if part)
    if stated:
        lines.append(f"**Run** {stated}")
    lines.append("")
    return lines


def render_setup_report(
    session_dir: Path | str,
    *,
    card: RunResultCard | None = None,
    project_meta: Mapping[str, Any] | None = None,
    run_pin: Mapping[str, Any] | None = None,
) -> str:
    """The document for one finished run, as markdown.

    `session_dir` is the run's own directory and is the only input that is not
    optional: every other argument names an artifact this function would
    otherwise read from that directory, and exists for the one caller that has
    already read it — the end of a live run, which holds the container's copies
    and may not have mirrored them to the host yet.
    """

    directory = Path(session_dir)
    base = resolve_session_dir(directory)
    resolved_card = card if card is not None else _card_for(directory, base)
    if resolved_card is None:
        # No verdict, no result: a document that led with a heading and no
        # card would be a page about a run nobody can read the outcome of.
        return ""

    # What the caller states wins over what the directory holds, field by
    # field: a run that has not mirrored its artifacts out yet has no
    # `project_meta.json` on the host, and a run that has one still knows its
    # own repository URL better than a file copied out of a container.
    meta = {
        **(read_run_document(base, _PROJECT_META_NAME) or {}),
        **{key: value for key, value in _mapping(project_meta).items() if value is not None},
    }
    pin = _mapping(run_pin) or read_run_document(base, _RUN_PIN_NAME) or {}

    lines = _header(
        resolved_card,
        project_meta=meta,
        run_pin=pin,
        ended_at=run_ended_at(base),
    )
    lines.extend(render_result_card_markdown(resolved_card))
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "read_run_document",
    "render_setup_report",
    "report_name",
    "run_ended_at",
    "setup_report_name",
]
