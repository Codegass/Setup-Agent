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
from typing import Any, Mapping, Sequence

from loguru import logger

from sag import __version__
from sag.console.turn_stream import turn_duration_text
from sag.result_card import build_result_card
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import RunResultCard
from sag.result_card.rows import duration_text
from sag.result_card.run_evidence import read_run_counts, recorded_report
from sag.tools.report_metrics import format_evidence_accounting_lines
from sag.trajectory.builder import build_trajectory, control_events_path, resolve_session_dir
from sag.trajectory.schema import SessionInfo, Trajectory, Turn

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

#: What a cell says when the run did not record the thing it is for. Absence
#: is stated; it is never rendered as a zero a reader would take for a count.
_ABSENT = "—"

#: What marks a turn the harness took rather than one the model asked for.
_HARNESS_MARK = "(the harness asked)"

#: How a tool came to be the one the run used, said the way a reader would say
#: it. A source not named here is printed as the overlay recorded it: an
#: unfamiliar word from the record is honest, and an invented sentence is not.
_HOW_PROVISIONED: dict[str, str] = {
    "system_install": "installed by the run",
    "agent_registered": "registered by the run",
}

#: How much of the toolchain the second table shows before it starts counting,
#: and how much of one reason a row carries. The same bounds the in-loop
#: renderer has always used, so a pathological reason cannot flood the page.
_MAX_REFUSED_ROWS = 5
_MAX_REASON_CHARS = 160

#: What each kind of citation is called, singular and plural. The kinds are
#: `classify_evidence_ref`'s answers; a kind it does not know is counted under
#: `other` and named as a citation, because dropping it would make the total
#: a number that does not add up.
_EVIDENCE_NOUNS: dict[str, tuple[str, str]] = {
    "surefire": ("surefire report file", "surefire report files"),
    "output": ("stored tool output", "stored tool outputs"),
    "class": ("compiled class", "compiled classes"),
    "jar": ("jar", "jars"),
    "validator": ("validator observation", "validator observations"),
    "directory": ("workspace directory", "workspace directories"),
    "other": ("other citation", "other citations"),
}

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

    try:
        ledger = control_events_path(session_dir)
    except Exception as exc:
        # A directory that is not there raises rather than answering, and this
        # is a reader: every caller of it is doing something best-effort, and
        # one of them was naming a file at the end of a successful run.
        logger.debug(f"the run's end could not be read from {session_dir}: {exc}")
        return None
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


def _trajectory_of(base: Path) -> Trajectory:
    """The run's turns, or an empty document when the ledger cannot be read."""

    try:
        return build_trajectory(base)
    except Exception as exc:
        # A reader is never worth a traceback: a run whose ledger is
        # unreadable prints no account of its turns and says everything else.
        logger.debug(f"the run's turns are unavailable for the report: {exc}")
        return Trajectory(session=SessionInfo(run_id=""))


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


def _counts(value: Any) -> str:
    """One number, grouped — or the dash that says nobody recorded it."""

    return f"{value:,}" if isinstance(value, int) else _ABSENT


def _clipped(value: Any) -> str | None:
    """One reason, cut to a row's worth of it and marked where it was cut."""

    if value is None:
        return None
    reason = str(value).replace("\n", " ").strip()
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[: _MAX_REASON_CHARS - 1].rstrip() + "…"


def _cell(value: Any) -> str:
    """One table cell, escaped the way the card escapes its own.

    A pipe inside a version string or a path would split the column it sits
    in, and a newline would end the row early.
    """

    if value is None:
        return ""
    return str(value).replace("|", r"\|").replace("\n", " ")


def _what_was_set_up(env_overlay: Mapping[str, Any]) -> list[str]:
    """The toolchain the run provisioned. Section C.3 of the spec.

    Read from the overlay the run wrote when it activated each tool, which is
    the record of what the commands afterwards actually ran against, and from
    the same file's record of the executables it found and refused. A run that
    could not provision a JDK has nothing active and everything to explain, so
    the second table stands on its own when the first has no rows.
    """

    tools = env_overlay.get("tools")
    active_rows: list[str] = []
    refused_rows: list[str] = []
    for name, entry in sorted((tools or {}).items()):
        if not isinstance(entry, Mapping):
            continue
        active = entry.get("active")
        if active:
            candidate = (entry.get("candidates") or {}).get(active) or {}
            source = str(candidate.get("source") or "")
            how = _HOW_PROVISIONED.get(source, source.replace("_", " "))
            active_rows.append(
                f"| {_cell(name)} | {_cell(candidate.get('version'))} "
                f"| `{_cell(active)}` | {_cell(how)} |"
            )
        for refused in entry.get("blocked") or ():
            if not isinstance(refused, Mapping):
                continue
            refused_rows.append(
                f"| {_cell(name)} | {_cell(refused.get('version'))} "
                f"| `{_cell(refused.get('executable'))}` | {_cell(refused.get('requirement'))} "
                f"| {_cell(_clipped(refused.get('reason')))} |"
            )
    if not active_rows and not refused_rows:
        return []

    lines = ["## What was set up", ""]
    if active_rows:
        lines.extend(["| Tool | Version | Path | How |", "|---|---|---|---|", *active_rows, ""])
    if refused_rows:
        shown = refused_rows[:_MAX_REFUSED_ROWS]
        lines.extend(
            [
                "### Tools the run did not use",
                "",
                "| Tool | Version | Path | Required | Why |",
                "|---|---|---|---|---|",
                *shown,
            ]
        )
        if len(refused_rows) > len(shown):
            lines.append(f"- and {len(refused_rows) - len(shown):,} more")
        lines.append("")
    return lines


def _turn_rows(turns: Sequence[Turn]) -> list[str]:
    """One row per turn: what was asked, what came back, how long it took."""

    rows = []
    for turn in turns:
        asked = turn.call.summary if turn.call else None
        # A turn the harness took is not a turn the model asked for, and a
        # reader comparing the run to the model's reasoning needs to see which
        # is which rather than be told the model asked for it.
        if asked and turn.actor == "controller":
            asked = f"{asked} {_HARNESS_MARK}"
        answered = None
        if turn.observation is not None:
            answered = turn.observation.summary or turn.observation.outcome
        rows.append(
            f"| {turn.turn_id} | {_cell(turn.call.tool if turn.call else None)} "
            f"| {_cell(asked)} | {_cell(answered)} "
            f"| {_cell(turn_duration_text(turn))} |"
        )
    return rows


def _banded(document: Trajectory) -> list[tuple[str, list[Turn]]]:
    """The run's turns, in the bands the reducer banded them into.

    The bands are the record's, not a grouping of consecutive phase names: a
    phase the run was sent back to opens a second band with the same name and
    no other phase between, and by name alone the two visits read as one. What
    separates them is the grading each band closed on, and the turn that
    carried that grading is in the ledger — so a band takes the turns up to
    and including its own last gate, and the next band starts after it.

    A run whose ledger bands nothing still has its turns, under the phase each
    one names.
    """

    turns = list(document.turns)
    bands = list(document.phases)
    if not bands:
        grouped: list[tuple[str, list[Turn]]] = []
        for turn in turns:
            if grouped and grouped[-1][0] == turn.phase:
                grouped[-1][1].append(turn)
            else:
                grouped.append((turn.phase, [turn]))
        return grouped

    decided_at = {
        turn.gate.decision_id: index
        for index, turn in enumerate(turns)
        if turn.gate is not None and turn.gate.decision_id
    }
    # Where each band ends: the last turn it graded. A band the ledger graded
    # without a turn ends nowhere, and the band after it takes those turns.
    ends = [
        max(
            (decided_at[gate.decision_id] for gate in band.gates if gate.decision_id in decided_at),
            default=None,
        )
        for band in bands
    ]

    held: list[list[Turn]] = [[] for _ in bands]
    index = 0
    for position, turn in enumerate(turns):
        while index < len(bands) - 1 and (ends[index] is None or position > ends[index]):
            index += 1
        held[index].append(turn)
    return [(band.name, held[index]) for index, band in enumerate(bands)]


def _the_run(document: Trajectory) -> list[str]:
    """The run as its turns, banded by phase. Section C.4 of the spec.

    Every cell is a field the derivation already holds — the same summaries
    `sag trajectory` prints, for the same turns, in the same order. Nothing is
    computed here, so the table and the stream cannot come to disagree about
    what a turn did.
    """

    turns = list(document.turns)
    if not turns:
        return []

    counted = [
        f"{len(turns):,} turn{'' if len(turns) == 1 else 's'}",
        (
            f"across {len(document.phases):,} phase{'' if len(document.phases) == 1 else 's'}"
            if document.phases
            else None
        ),
    ]
    said = " ".join(part for part in counted if part)
    elapsed = duration_text(document.session.wall_clock_seconds)
    lines = ["## The run", "", f"{said}, {elapsed}." if elapsed else f"{said}.", ""]

    for band, held in _banded(document):
        if not held:
            continue
        lines.extend(
            [
                f"**{_cell(band)}**",
                "",
                "| # | Tool | Asked | Result | Took |",
                "|---|---|---|---|---|",
                *_turn_rows(held),
                "",
            ]
        )
    return lines


def _tokens(card: RunResultCard, document: Trajectory) -> list[str]:
    """What the run was billed, one row per model. Section C.5 of the spec.

    Never one number. The executor and the advisor are two models, and a sum
    would say the first spent it all — hiding which model the bill went to and
    what turning the advisor off would save. A row is printed only for a model
    the run actually paid.
    """

    stats = card.stats
    turns = tuple(document.turns)
    rows = []
    for label, model, tokens_in, tokens_out, calls in (
        (
            "Model",
            stats.model,
            stats.tokens_in,
            stats.tokens_out,
            sum(1 for turn in turns if turn.tokens is not None),
        ),
        (
            "Advisor",
            stats.advisor_model,
            stats.advisor_tokens_in,
            stats.advisor_tokens_out,
            sum(1 for turn in turns if turn.advisor_tokens is not None),
        ),
    ):
        if tokens_in is None and tokens_out is None:
            continue
        # Each side on its own: a run whose completion tokens were never
        # recorded still has an input total, and a dash is what the record
        # says about the other side.
        rows.append(
            f"| **{label}** | {_cell(model) or _ABSENT} | {calls:,} "
            f"| {_counts(tokens_in)} | {_counts(tokens_out)} |"
        )
    if not rows:
        return []
    return [
        "## Tokens",
        "",
        "| | Model | Calls | In | Out |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ]


def classify_evidence_ref(ref: str, roots: Sequence[str] = ()) -> str:
    """Which kind of thing one citation names, by the shape of the citation.

    Six kinds cover everything the archived run cited; anything else is
    counted under `other`, never dropped — a reader told the run cited 72
    artifacts and shown 71 has been told a falsehood about the seventy-second.

    `roots` are the directories the run worked in, and they are the only
    citations called directories: an extensionless path is a `Makefile` or an
    `mvnw` as often as it is a folder, and nothing in the citation itself says
    which.
    """

    if ref.startswith("output_"):
        return "output"
    if ref.startswith("validator:"):
        return "validator"
    if ref.rstrip("/") in {root.rstrip("/") for root in roots}:
        return "directory"
    tail = ref.rsplit("/", 1)[-1]
    if tail.endswith(".xml") and "surefire" in ref:
        return "surefire"
    if tail.endswith(".class"):
        return "class"
    if tail.endswith(".jar"):
        return "jar"
    return "other"


def artifacts_by_kind(refs: Sequence[str], *, roots: Sequence[str] = ()) -> dict[str, int]:
    """How many distinct artifacts the run cited, by kind.

    The same file is cited both absolutely and relative to the project — nine
    such pairs on the archived run — and counting the spellings would tell a
    reader the run touched nine files it never touched. A relative citation
    folds into an absolute one that ends with it, and only when exactly one
    does: with two candidates the fold would be a guess, so the citation is
    counted on its own, as something the report cannot name — never folded
    into one of them, which would undercount the other.
    """

    absolute = [ref for ref in refs if ref.startswith("/")]
    counted: dict[str, int] = {}
    seen: set[str] = set()
    for ref in refs:
        key, kind = ref, classify_evidence_ref(ref, roots)
        if not ref.startswith("/"):
            matches = [other for other in absolute if other.endswith(f"/{ref}")]
            if len(matches) == 1:
                key = matches[0]
            elif matches:
                kind = "other"
        if key in seen:
            continue
        seen.add(key)
        counted[kind] = counted.get(kind, 0) + 1
    return counted


def _counted(kind: str, count: int) -> str:
    """`47 surefire report files`, `1 jar`, said the way a reader would say it."""

    singular, plural = _EVIDENCE_NOUNS.get(kind, (kind, f"{kind}s"))
    return f"1 {singular}" if count == 1 else f"{count:,} {plural}"


def _build_turn(document: Trajectory) -> Turn | None:
    """The turn that ran the build, which is the one worth opening first."""

    for turn in document.turns:
        if turn.call is not None and turn.call.tool == "build":
            return turn
    return None


def _evidence(card: RunResultCard, document: Trajectory, verdict: Mapping[str, Any]) -> list[str]:
    """What the run cited, counted, and how to reach it. Section C.6.

    The list itself stays in the record. Forty-seven surefire filenames in a
    paragraph is not something a reader can act on; the count is, and the
    commands below open the thing the count is about.
    """

    build_evidence = verdict.get("build_evidence")
    refs = [
        str(ref)
        for ref in (
            *(verdict.get("input_refs") or ()),
            *((build_evidence or {}).get("refs") or ()),
        )
        if str(ref).strip()
    ]
    # Where the run worked, which is what makes a citation a directory rather
    # than an extensionless file.
    roots = ["/workspace", *([f"/workspace/{card.project}"] if card.project else [])]
    counts = artifacts_by_kind(list(dict.fromkeys(refs)), roots=roots)
    total = sum(counts.values())

    lines: list[str] = []
    if total:
        # Largest class first, and ties by the name a reader sees, so one
        # run's evidence reads in the same order every time it is rendered.
        ordered = sorted(
            counts.items(),
            key=lambda item: (-item[1], _EVIDENCE_NOUNS.get(item[0], (item[0],))[0]),
        )
        said = ", ".join(_counted(kind, count) for kind, count in ordered)
        lines.extend(
            [
                f"The run cited {total:,} distinct "
                f"artifact{'' if total == 1 else 's'}: {said}.",
                "",
            ]
        )

    # A command printed without its argument is not a command a reader can
    # run, which is the rule the terminal block's Next line already follows.
    session = card.session_dir
    if session:
        commands = [f"uv run sag trajectory {session}"]
        turn = _build_turn(document)
        if card.project and turn is not None:
            commands.append(
                f"uv run sag inspect {card.project} --session {session} --turn {turn.turn_id}"
            )
        commands.append(f"uv run sag result {session}")
        commands.append("uv run sag ui")
        lines.extend(["To open it:", "", *[f"    {command}" for command in commands], ""])

    return ["## Evidence", "", *lines] if lines else []


def _evidence_accounting(
    report_metrics: Mapping[str, Any], verdict: Mapping[str, Any]
) -> list[str]:
    """How every test observation was accounted for. Section C.7 of the spec.

    The seven lines are the ones the in-loop report already printed, from the
    same formatter. The eighth is the static declaration count, moved here out
    of the diagnostics table this document drops: it was the one fact that
    table carried which the Tests row does not, and it belongs beside the
    denominators it is explicitly not one of.
    """

    if not report_metrics:
        return []
    lines = [f"- {line}" for line in format_evidence_accounting_lines(report_metrics)]
    discovered = (verdict.get("test_stats") or {}).get("discovered")
    if isinstance(discovered, int):
        lines.append(
            f"- Static test declarations found by analysis: {discovered:,} "
            "(diagnostic; not the denominator above)"
        )
    return ["## Evidence accounting", "", *lines, ""]


def render_setup_report(
    session_dir: Path | str,
    *,
    card: RunResultCard | None = None,
    verdict: Any = None,
    report_metrics: Mapping[str, Any] | None = None,
    project_meta: Mapping[str, Any] | None = None,
    run_pin: Mapping[str, Any] | None = None,
    env_overlay: Mapping[str, Any] | None = None,
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
    lines.extend(
        _what_was_set_up(_mapping(env_overlay) or read_run_document(base, _ENV_OVERLAY_NAME) or {})
    )
    turns = _trajectory_of(base)
    lines.extend(_the_run(turns))
    lines.extend(_tokens(resolved_card, turns))
    sealed = _mapping(verdict) or read_run_document(base, _VERDICT_NAME) or {}
    lines.extend(_evidence(resolved_card, turns, sealed))
    lines.extend(
        _evidence_accounting(
            _mapping(report_metrics) or read_run_document(base, _REPORT_METRICS_NAME) or {},
            sealed,
        )
    )
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "artifacts_by_kind",
    "classify_evidence_ref",
    "read_run_document",
    "render_setup_report",
    "report_name",
    "run_ended_at",
    "setup_report_name",
]
