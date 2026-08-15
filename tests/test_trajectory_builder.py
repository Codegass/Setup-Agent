"""A session directory folds the same way whether it is finished or still running.

The event bytes are the ones pinned in `test_trajectory_reducer` — the archived
kafka d2r3 ledger — and the token rows are that run's real `token_usage.csv`
header and first rows. Batch replay and tail-follow are asserted to produce the
same turns, which is the idempotence fence of spec §5 in seed form.
"""

from pathlib import Path

import pytest

from sag.trajectory.builder import build_trajectory, follow_trajectory
from test_trajectory_reducer import REAL_FAILED_CALL_JSONL, REAL_TRIPLE_JSONL

# The real header of `token_usage.csv`, with this run's first two executor rows.
REAL_TOKEN_CSV = (
    "iteration,timestamp,type,tool_name,model,total_tokens,prompt_tokens,"
    "completion_tokens,reasoning_tokens,actual_output_tokens\n"
    "1,2026-08-14T07:28:36.912919,executor,project,gpt-5.4-mini,4205,4134,71,0,71\n"
    "2,2026-08-14T07:29:03.777748,executor,project,gpt-5.4-mini,4505,4463,42,0,42\n"
)

REAL_VERDICT = (
    '{"run_id": "kafka-run", "verdict": "partial", "rates": {"build": {"modules": 82.8}}}'
)


def _lines(block: str) -> list[str]:
    return block.strip().splitlines()


EVENT_LINES = _lines(REAL_TRIPLE_JSONL) + _lines(REAL_FAILED_CALL_JSONL)


def _session(tmp_path: Path, *, events: str | None = None, tokens: str | None = None) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    if events is not None:
        (session_dir / "control_events.jsonl").write_text(events, encoding="utf-8")
    if tokens is not None:
        (session_dir / "token_usage.csv").write_text(tokens, encoding="utf-8")
    return session_dir


class _NoMoreLines(Exception):
    """Raised by the injected clock once the test has nothing left to append."""


def test_build_trajectory_joins_executor_tokens_by_iteration(tmp_path):
    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=REAL_TOKEN_CSV)
    snap = build_trajectory(session_dir)

    assert [t.iteration for t in snap.turns] == [1, 2]
    assert snap.turns[0].tokens.input == 4134 and snap.turns[0].tokens.output == 71
    assert snap.turns[1].tokens.input == 4463 and snap.turns[1].tokens.output == 42
    assert snap.warnings == []


def test_only_executor_rows_may_claim_a_turns_tokens(tmp_path):
    """An advisor call spends tokens of its own; they are not the turn's."""
    tokens = REAL_TOKEN_CSV.replace(
        "1,2026-08-14T07:28:36.912919,executor",
        "1,2026-08-14T07:28:30.000000,advisor,Unknown,gpt-5.4-mini,2713,2575,138,0,138\n"
        "1,2026-08-14T07:28:36.912919,executor",
    )
    snap = build_trajectory(_session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=tokens))
    assert snap.turns[0].tokens.input == 4134


def test_a_turn_whose_iteration_never_billed_carries_no_tokens(tmp_path):
    one_row = "\n".join(REAL_TOKEN_CSV.splitlines()[:2]) + "\n"
    snap = build_trajectory(
        _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=one_row)
    )
    assert snap.turns[0].tokens is not None and snap.turns[1].tokens is None


def test_the_verdict_fills_the_session_once_the_run_has_finished(tmp_path):
    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=REAL_TOKEN_CSV)
    (session_dir / ".setup_agent").mkdir()
    (session_dir / ".setup_agent" / "verdict.json").write_text(REAL_VERDICT, encoding="utf-8")

    snap = build_trajectory(session_dir)
    assert snap.session.verdict == "partial"
    assert snap.session.rates == {"build": {"modules": 82.8}}
    assert snap.session.run_id == "kafka-run"


def test_a_running_session_with_no_verdict_yet_still_builds(tmp_path):
    snap = build_trajectory(_session(tmp_path, events="\n".join(EVENT_LINES) + "\n"))
    assert snap.session.verdict is None and snap.session.rates is None
    assert len(snap.turns) == 2


def test_a_session_dir_with_no_ledger_yet_is_a_warning_not_a_crash(tmp_path):
    snap = build_trajectory(_session(tmp_path))
    assert snap.turns == []
    assert [w.code for w in snap.warnings] == ["missing_control_events"]


def test_an_unreadable_token_ledger_is_a_warning_not_a_crash(tmp_path):
    session_dir = _session(
        tmp_path,
        events="\n".join(EVENT_LINES) + "\n",
        tokens="iteration,type,prompt_tokens\nnot-a-number,executor,nope\n",
    )
    snap = build_trajectory(session_dir)
    assert [w.code for w in snap.warnings] == ["token_usage_unreadable"]
    assert len(snap.turns) == 2 and snap.turns[0].tokens is None


def test_a_missing_session_directory_is_an_error_not_an_empty_trajectory(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_trajectory(tmp_path / "never-existed")


def test_follow_accumulates_exactly_what_batch_replay_produces(tmp_path):
    """Live and post-hoc are one fold: the same lines, the same turns."""
    session_dir = _session(tmp_path, events=EVENT_LINES[0] + "\n", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    pending = [line + "\n" for line in EVENT_LINES[1:]]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if not pending:
            raise _NoMoreLines
        events.write_text(events.read_text(encoding="utf-8") + pending.pop(0), encoding="utf-8")

    accumulated: dict[int, object] = {}
    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.25, sleep=fake_sleep):
            for turn in delta.turns:
                accumulated[turn.turn_id] = turn

    batch = build_trajectory(session_dir)
    assert [accumulated[t.turn_id] for t in batch.turns] == batch.turns
    assert slept and set(slept) == {0.25}


def test_follow_never_feeds_a_half_written_line(tmp_path):
    """A line only exists once its newline does; a torn tail is not an event."""
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    whole = EVENT_LINES[0] + "\n"
    chunks = [whole[:200], whole[200:], EVENT_LINES[1] + "\n", EVENT_LINES[2] + "\n"]

    def fake_sleep(seconds: float) -> None:
        if not chunks:
            raise _NoMoreLines
        with events.open("a", encoding="utf-8") as handle:
            handle.write(chunks.pop(0))

    deltas = []
    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep):
            deltas.append(delta)

    assert all(w.code != "malformed_event_line" for d in deltas for w in d.warnings)
    assert build_trajectory(session_dir).turns == [t for d in deltas for t in d.turns][-1:]


def test_follow_waits_for_a_ledger_that_does_not_exist_yet(tmp_path):
    """A session that has not written its first event is not an error."""
    session_dir = _session(tmp_path)
    events = session_dir / "control_events.jsonl"
    pending = ["\n".join(EVENT_LINES) + "\n"]

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        events.write_text(pending.pop(0), encoding="utf-8")

    seen = []
    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep):
            seen.extend(delta.turns)
    assert {turn.turn_id for turn in seen} == {1, 2}


def test_follow_carries_the_tokens_batch_replay_would_have_joined(tmp_path):
    session_dir = _session(tmp_path, events=EVENT_LINES[0] + "\n", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    pending = [line + "\n" for line in EVENT_LINES[1:3]]

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        events.write_text(events.read_text(encoding="utf-8") + pending.pop(0), encoding="utf-8")

    last = {}
    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep):
            for turn in delta.turns:
                last[turn.turn_id] = turn
    assert last[1].tokens.input == 4134


def test_the_detail_tier_is_checked_before_any_file_is_opened(tmp_path):
    with pytest.raises(ValueError):
        build_trajectory(_session(tmp_path, events=""), detail="verbose")
