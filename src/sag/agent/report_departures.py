"""Whether the report phase needs the model, and what the record says when it does.

The report tool renders the record itself; the model's summary and details
never reach the markdown in setup mode. So a model turn in the report phase is
worth its tokens only when the run left the clean path and a person needs the
notes the record cannot hold: where the run got stuck, why, and what to do
next.

Both functions here read the record alone and re-run nothing:

- `report_turn_needed` decides whether the run is green — success verdict, the
  task complete when one was given, each of provision/analyze/build/test closed
  exactly once as completed/success (and, when the agent closed it, the agent
  itself claimed success), no blocker (active or resolved), no conflict, no
  repair, and the evidence closed when the tests ended — and says why in plain
  English for the ledger.
- `render_departure_block` lists every departure, one `Departure: ` line per
  item, so the report brief can keep whole lines under a bounded contract.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

CLEAN_PHASES = ("provision", "analyze", "build", "test")
CLEAN_CLOSE_REASON = "test_terminated"

DEPARTURE_HEADER = "Departures from a clean run (from the record):"
DEPARTURE_PREFIX = "Departure: "
DEPARTURE_LINE_CAP = 1500
DEPARTURE_BLOCK_CAP = 3600
DEPARTURE_CUT = "… (cut; full text in the phase handoff)"
# Below this many characters a cut line would carry its prefix and the cut
# notice and almost nothing of the item; the item is counted as not shown.
_MIN_CUT_BODY = 40


def _value(field: Any) -> str:
    """An enum's value or the string itself."""
    return str(getattr(field, "value", field) or "")


def _one_line(text: Any) -> str:
    """Item text as one line: a newline would split an item the brief keeps whole."""
    return " ".join(str(text or "").split())


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _task_words(task_status: Optional[str]) -> str:
    return f"task {task_status}" if task_status else "task none given"


def _claimed_below_success(record: Any) -> str:
    """What the agent claimed when it did not call its close a success; else empty.

    A record with no claim was minted by the harness and is judged on its close
    alone. The gate can grade a phase success over the agent's own "partial";
    that is exactly when the agent's notes are wanted.
    """
    claim = getattr(record, "claim", None)
    if claim is None:
        return ""
    claimed = _value(claim.claimed_outcome)
    if claimed == "success":
        return ""
    return claimed or "an unrecorded outcome"


def report_turn_needed(
    *,
    verdict: str,
    task_status: Optional[str],
    records: Iterable[Any],
    blockers: Sequence[Any],
    conflicts: Sequence[str],
    repairs: Sequence[Any],
    close_reason: Optional[str],
) -> tuple[bool, str]:
    """(needed, reason): True when the run departed from a clean path.

    `task_status` is None when no task was given; that is not a departure.
    """
    records = list(records)
    reasons: list[str] = []
    if verdict != "success":
        reasons.append(f"verdict {verdict or 'unknown'}")
    if task_status is not None and task_status != "complete":
        reasons.append(_task_words(task_status))
    for phase in CLEAN_PHASES:
        closes = [record for record in records if getattr(record, "phase", None) == phase]
        if len(closes) != 1:
            reasons.append(f"{phase} closed {len(closes)} times")
            continue
        termination = _value(closes[0].termination)
        outcome = _value(closes[0].outcome)
        if termination != "completed" or outcome != "success":
            reasons.append(f"{phase} closed {termination} [{outcome}]")
            continue
        claimed = _claimed_below_success(closes[0])
        if claimed:
            reasons.append(f"{phase} closed as success but the agent claimed {claimed}")
    if blockers:
        reasons.append(f"{_count(len(blockers), 'blocker')} on record")
    if conflicts:
        reasons.append(f"{_count(len(conflicts), 'conflict')} on record")
    if repairs:
        reasons.append(f"{_count(len(repairs), 'repair')} on record")
    if close_reason != CLEAN_CLOSE_REASON:
        reasons.append(f"evidence closed for {close_reason or 'an unrecorded reason'}")
    if reasons:
        return True, "; ".join(reasons)
    return False, (
        f"verdict success; {_task_words(task_status)}; provision, analyze, build and test "
        "each closed once as success; no blockers, conflicts or repairs on record; the "
        "evidence closed when the tests ended"
    )


def _cut(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    return line[: max(0, limit - len(DEPARTURE_CUT) - 1)].rstrip() + " " + DEPARTURE_CUT


def _omitted_line(count: int) -> str:
    return f"{DEPARTURE_PREFIX}{count} more not shown (full text in the phase handoff)"


def departure_items(
    *,
    records: Iterable[Any],
    blockers: Sequence[Any],
    conflicts: Sequence[str],
    repairs: Sequence[Any],
) -> list[str]:
    """Every departure, in record order, before any cap."""
    items: list[str] = []
    for record in records:
        termination = _value(record.termination)
        outcome = _value(record.outcome)
        close = f"{termination} [{outcome}]"
        if termination == "completed" and outcome == "success":
            claimed = _claimed_below_success(record)
            if not claimed:
                continue
            close += f", the agent claimed {claimed}"
        text = _one_line(record.key_results or record.reason) or "no notes recorded"
        items.append(
            f"{DEPARTURE_PREFIX}{record.phase} attempt {record.attempt_id}: {close} — {text}"
        )
    for blocker in blockers:
        items.append(
            f"{DEPARTURE_PREFIX}blocker {blocker.blocker_id} in "
            f"{blocker.source_phase or 'an unrecorded phase'}: {_one_line(blocker.error_code)} "
            f"{_one_line(blocker.failure_signature)} [{blocker.status}]"
        )
    for conflict in conflicts:
        items.append(f"{DEPARTURE_PREFIX}conflict: {_one_line(conflict)}")
    for repair in repairs:
        reason = _one_line(repair.decision_reason or repair.hypothesis) or "no reason recorded"
        items.append(
            f"{DEPARTURE_PREFIX}repair in {repair.from_phase}: "
            f"{_one_line(repair.reason_code)} — {reason}"
        )
    return items


def render_departure_block(
    *,
    records: Iterable[Any],
    blockers: Sequence[Any],
    conflicts: Sequence[str],
    repairs: Sequence[Any],
    verdict: str,
    task_status: Optional[str],
) -> list[str]:
    """The header, one capped line per departure, and the verdict line last.

    Each line is at most DEPARTURE_LINE_CAP characters and the joined block at
    most DEPARTURE_BLOCK_CAP. A cut line says so at its end; items that do not
    fit at all are counted on one line, so nothing is dropped silently.
    """
    items = departure_items(
        records=records, blockers=blockers, conflicts=conflicts, repairs=repairs
    )
    final = _cut(
        f"{DEPARTURE_PREFIX}verdict {verdict or 'unknown'}; {_task_words(task_status)}",
        DEPARTURE_LINE_CAP,
    )
    # Every kept line costs its length plus the newline that joins it.
    budget = DEPARTURE_BLOCK_CAP - len(DEPARTURE_HEADER) - (len(final) + 1)
    kept: list[str] = []
    for index, item in enumerate(items):
        line = _cut(item, DEPARTURE_LINE_CAP)
        rest = len(items) - index - 1
        reserve = len(_omitted_line(rest)) + 1 if rest else 0
        if len(line) + 1 + reserve <= budget:
            kept.append(line)
            budget -= len(line) + 1
            continue
        room = budget - 1 - reserve
        if room >= len(DEPARTURE_PREFIX) + len(DEPARTURE_CUT) + _MIN_CUT_BODY:
            kept.append(_cut(line, room))
            not_shown = rest
        else:
            not_shown = rest + 1
        if not_shown:
            kept.append(_omitted_line(not_shown))
        break
    return [DEPARTURE_HEADER, *kept, final]
