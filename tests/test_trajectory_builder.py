"""A session directory folds the same way whether it is finished or still running.

The event bytes are the ones pinned in `test_trajectory_reducer` — the archived
kafka d2r3 ledger — and the token rows are that run's real `token_usage.csv`
header and first rows. Batch replay and tail-follow are asserted to produce the
same turns, which is the idempotence fence of spec §5 in seed form.
"""

import json
import os
from pathlib import Path

import pytest
from test_trajectory_reducer import (
    REAL_FAILED_CALL_JSONL,
    REAL_FORCED_ACTION_JSONL,
    REAL_TRIPLE_JSONL,
    REAL_TWO_DECISIONS_JSONL,
)

from sag.agent.output_storage import OBSERVABILITY_TASK_ID, OutputStorageManager
from sag.trajectory.builder import build_trajectory, follow_trajectory
from sag.trajectory.reducer import DeltaAccumulator

#: root reads a mode-000 file regardless of its mode, so the permission fences
#: below have nothing to measure when the suite runs as root.
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="a mode-000 file is readable by root, so there is no unreadable ledger to fence",
)

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


#: kafka's own advisor row for iteration 1, the bytes the tracker wrote.
ADVISOR_ROW = "1,2026-08-14T07:28:30.000000,advisor,Unknown,gpt-5.4-mini,2713,2575,138,0,138\n"
#: What the same tracker writes when a consult's context had to be packed first
#: (`react_llm.summarize_advisor_context`). Still the advisor spending.
COMPRESSION_ROW = (
    "1,2026-08-14T07:28:31.000000,advisor_compression,Unknown,gpt-5.4-mini,600,560,40,0,40\n"
)


def test_an_advisor_row_that_reaches_no_advisor_turn_is_stated_not_dropped(tmp_path):
    """Neither turn below asked for advice, so the row pays nobody — out loud.

    This is the hole the join was written to close. For two rounds the reader
    skipped every advisor row and said nothing, so 38% of a real run's spend
    appeared on no turn, in no total and in no warning. A row that bills nobody
    is stated exactly as an executor row that bills nobody is.
    """
    tokens = REAL_TOKEN_CSV + ADVISOR_ROW
    snap = build_trajectory(_session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=tokens))
    assert all(t.advisor_tokens is None for t in snap.turns)
    assert [(w.code, w.detail) for w in snap.warnings] == [
        ("advisor_tokens_unattributed", "1 advisor row(s) bill no turn: iteration(s) 1")
    ]


#: SYNTHETIC in its numbering only: an `advisor` call in the shape every other
#: call in this file has — envelope, result, decision — so the ledger states a
#: turn that consulted someone on iteration 3. The archived fixtures' advisor
#: calls emit no `loop_decision` at all, so none of them carries an iteration,
#: and a fence about which row pays a consult needs one that does.
ADVISOR_CALL_LINES = [
    json.dumps(
        {
            "event_id": "control-000020",
            "kind": "action_envelope",
            "payload": {
                "tool": "advisor",
                "envelope_id": "envelope-000020",
                "exact_params": {},
                "intent_source": "model",
            },
            "sequence": 20,
            "source": None,
            "timestamp": "2026-08-14T11:29:20Z",
        }
    ),
    json.dumps(
        {
            "event_id": "control-000021",
            "kind": "tool_result",
            "payload": {
                "envelope_id": "envelope-000020",
                "tool": "advisor",
                "params": {},
                "result": {"operation_outcome": "success", "invocation_status": "completed"},
            },
            "sequence": 21,
            "source": None,
            "timestamp": "2026-08-14T11:29:21Z",
        }
    ),
    json.dumps(
        {
            "event_id": "control-000022",
            "kind": "loop_decision",
            "payload": {
                "event": {
                    "tool_name": "advisor",
                    "iteration": 3,
                    "phase": "build",
                    "args": {},
                    "attempt_id": "build-1",
                    "error_code": "",
                    "failure_signature": "",
                    "invocation_status": "completed",
                    "operation_outcome": "success",
                    "recurrence_count": 1,
                },
                "expected_decision": "continue",
                "expected_reason_code": "outcome_not_loop_candidate",
            },
            "sequence": 22,
            "source": None,
            "timestamp": "2026-08-14T11:29:22Z",
        }
    ),
]

#: The two rows one consult on iteration 3 writes, IN THE ORDER THE ENGINE
#: WRITES THEM. `_advisor_messages` packs the context before `get_advisor_response`
#: sends it, so `summarize_advisor_context` files its `advisor_compression` row
#: ahead of the consult's own `advisor` row. A rule that keeps the first row
#: therefore keeps the packing and throws the advice away.
PACKED_CONSULT_ROWS = (
    "3,2026-08-14T07:28:31.000000,advisor_compression,Unknown,gpt-5.4-mini,600,560,40,0,40\n"
    "3,2026-08-14T07:28:32.000000,advisor,Unknown,gpt-5.4-mini,2713,2575,138,0,138\n"
)


def _consulting_session(tmp_path: Path, tokens: str) -> Path:
    return _session(
        tmp_path, events="\n".join(EVENT_LINES + ADVISOR_CALL_LINES) + "\n", tokens=tokens
    )


def test_packing_a_consult_is_part_of_what_the_consult_cost(tmp_path):
    """Two calls, one consult: the bill is both of them added together.

    `advisor_compression` is not a second bill for one answer — it is the call
    that packed the context the answer was given on, and the run paid for both
    before it had any advice. Keeping only the first row kept the packing (600
    tokens of the EXECUTOR model) and threw away the advisor's own 2,713-token
    answer while calling it a duplicate.
    """
    snap = build_trajectory(_consulting_session(tmp_path, REAL_TOKEN_CSV + PACKED_CONSULT_ROWS))

    consult = snap.turns[2]
    assert consult.call.tool == "advisor" and consult.iteration == 3
    assert (consult.advisor_tokens.input, consult.advisor_tokens.output) == (3135, 178)
    # Nothing was dropped, so nothing is stated.
    assert [w.code for w in snap.warnings if w.code.startswith("advisor_tokens")] == []


def test_the_same_consult_billed_twice_is_still_one_bill(tmp_path):
    """A repeated `advisor` row is the one thing that may never be added.

    Packing is a second CALL; a second row for the same answer is the ledger
    disagreeing with itself, and adding it would invent spend. The first keeps
    it, the packing is added to it, and the repeat is stated.
    """
    repeated = "3,2026-08-14T07:28:33.000000,advisor,Unknown,gpt-5.4-mini,2713,2575,138,0,138\n"
    snap = build_trajectory(
        _consulting_session(tmp_path, REAL_TOKEN_CSV + PACKED_CONSULT_ROWS + repeated)
    )

    consult = snap.turns[2]
    assert (consult.advisor_tokens.input, consult.advisor_tokens.output) == (3135, 178)
    assert [(w.code, w.detail) for w in snap.warnings] == [
        (
            "advisor_tokens_duplicate_row",
            "1 duplicate advisor row(s) for iteration 3 bill nothing; "
            "the first row keeps the bill",
        )
    ]


def test_a_packing_call_with_no_advice_after_it_is_still_the_advisor_spending(tmp_path):
    """A consult that packed and then failed still spent what the packing cost."""
    packing_only = (
        "3,2026-08-14T07:28:31.000000,advisor_compression,Unknown,gpt-5.4-mini,600,560,40,0,40\n"
    )
    snap = build_trajectory(_consulting_session(tmp_path, REAL_TOKEN_CSV + packing_only))

    consult = snap.turns[2]
    assert (consult.advisor_tokens.input, consult.advisor_tokens.output) == (560, 40)
    assert [w.code for w in snap.warnings if w.code.startswith("advisor_tokens")] == []


def test_a_packing_call_is_the_advisor_spending_too(tmp_path):
    """`advisor_compression` is the advisor's own call under a longer name.

    The row is written by the same tracker on the same consult, and a reader
    matching the word `advisor` exactly would drop it. Neither turn below asked
    for advice, so both rows reach nobody and both are stated — the packing is
    not a duplicate of the answer, so only the repeat of one kind is.
    """
    tokens = REAL_TOKEN_CSV + COMPRESSION_ROW + ADVISOR_ROW
    snap = build_trajectory(_session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=tokens))
    assert [(w.code, w.detail) for w in snap.warnings] == [
        ("advisor_tokens_unattributed", "1 advisor row(s) bill no turn: iteration(s) 1"),
    ]


def test_two_calls_from_one_model_response_are_billed_once_between_them(tmp_path):
    """`iteration` counts model responses, not turns; the bill follows the response.

    Both decisions below carry iteration 1 — one response, two calls. Copying
    the row onto both turns doubles spend that was charged once.
    """
    events = "\n".join(_lines(REAL_TWO_DECISIONS_JSONL)) + "\n"
    snap = build_trajectory(_session(tmp_path, events=events, tokens=REAL_TOKEN_CSV))

    assert [t.iteration for t in snap.turns] == [1, 1]
    assert snap.turns[0].tokens.input == 4134
    assert snap.turns[1].tokens is None
    assert [w.detail for w in snap.warnings if w.code == "tokens_unattributed"] == [
        "1 executor row(s) bill no turn: iteration(s) 2"
    ]


def test_a_controller_turn_is_never_billed_for_a_response_the_model_did_not_make(tmp_path):
    """A forced action is the harness moving; the model was not charged for it.

    The forced turn below carries iteration 13 — the controller's decision names
    the loop it interrupted — so an iteration-keyed join would hand it the
    model's bill for that response.
    """
    tokens = (
        "iteration,timestamp,type,tool_name,model,total_tokens,prompt_tokens,"
        "completion_tokens,reasoning_tokens,actual_output_tokens\n"
        "13,2026-08-14T00:31:44.000000,executor,project,gpt-5.4-mini,7985,7868,117,0,117\n"
    )
    events = "\n".join(_lines(REAL_FORCED_ACTION_JSONL)) + "\n"
    snap = build_trajectory(_session(tmp_path, events=events, tokens=tokens))

    assert [t.actor for t in snap.turns] == ["controller"]
    assert snap.turns[0].iteration == 13 and snap.turns[0].tokens is None
    assert [w.code for w in snap.warnings] == ["tokens_unattributed"]


def test_the_full_tier_reads_every_store_the_session_has(tmp_path):
    """One ref namespace, two files — and a reader that asks both.

    A sealed turn's bytes are the engine's own: the window it rendered and the
    observation it delivered, written HOST-side beside the ledger while the run
    is live. A tool's output is the container's, and reaches the session
    directory only when `--record` copies `.setup_agent` in. Both are `output_`
    handles from one namespace, and whoever holds one cannot tell which file
    answers it — so the resolver asks every store the session has. Asking only
    the first one found meant that the moment the engine began writing its own
    store, every tool output in the session stopped resolving.
    """
    from test_trajectory_cli import REAL_FULL_OUTPUT_RECORD

    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=REAL_TOKEN_CSV)
    recorded = session_dir / ".setup_agent" / "contexts"
    recorded.mkdir(parents=True)
    (recorded / "full_outputs.jsonl").write_text(REAL_FULL_OUTPUT_RECORD + "\n", encoding="utf-8")
    sealed = session_dir / "contexts"
    sealed.mkdir()
    (sealed / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_bbab28ecefd9",
                "task_id": "turn_records",
                "tool_name": "delivered_observation",
                "timestamp": "2026-08-14T11:29:04.600000",
                "output_length": 61,
                "output": "Env overlay executable is not executable: /usr/bin/gradle",
                "metadata": {"kind": "delivered_observation"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snap = build_trajectory(session_dir, detail="full")

    assert snap.outputs["output_6163859b019d"].startswith("✅ Repository cloned")
    assert snap.outputs["output_bbab28ecefd9"].startswith("Env overlay executable")
    assert [
        w.code for w in snap.warnings if w.code.startswith(("missing_output", "unresolved"))
    ] == []


def test_a_second_executor_row_for_one_iteration_is_stated_not_dropped(tmp_path):
    """Two rows for one response is a fact about the ledger, not a rounding step.

    The join has to pick one — billing both would invent spend — and it picks
    the first, which is deterministic and the same in both feeds. What it may
    not do is pick silently: `tokens_unattributed` already says out loud when a
    row bills nobody, and a row that bills nobody because another row got there
    first is the same kind of statement. Whoever reads the total is entitled to
    know the ledger disagreed with itself.
    """
    duplicate = "1,2026-08-14T07:28:41.000000,executor,project,gpt-5.4-mini,9999,9000,999,0,999\n"
    tokens = REAL_TOKEN_CSV + duplicate
    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=tokens)

    snap = build_trajectory(session_dir)
    assert snap.turns[0].tokens.input == 4134  # the first row keeps the bill
    assert [(w.code, w.detail) for w in snap.warnings] == [
        (
            "tokens_duplicate_row",
            "1 duplicate executor row(s) for iteration 1 bill nothing; "
            "the first row keeps the bill",
        )
    ]


def test_a_duplicate_row_is_stated_to_a_live_watcher_too(tmp_path):
    """The two feeds disagree about nothing, including what the ledger got wrong."""
    duplicate = "1,2026-08-14T07:28:41.000000,executor,project,gpt-5.4-mini,9999,9000,999,0,999\n"
    session_dir = _session(tmp_path, events="")
    accumulated = _accumulate_then_bill(
        session_dir,
        [line + "\n" for line in EVENT_LINES],
        tokens=REAL_TOKEN_CSV + duplicate,
    )
    assert [w.code for w in accumulated.warnings] == ["tokens_duplicate_row"]
    assert accumulated == build_trajectory(session_dir)


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


@not_root
def test_a_ledger_that_cannot_be_read_is_a_warning_not_an_empty_document(tmp_path):
    """A ledger the reader may not open is not a run that did nothing.

    Swallowing the `OSError` and returning no lines produced a trajectory
    indistinguishable from a session that never called a tool — zero turns, no
    warnings, and a document a consumer would have every right to believe. The
    file is right there and full of events; what is missing is permission, so
    the derivation says which errno stopped it and keeps its "0 turns" honest.
    """
    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=REAL_TOKEN_CSV)
    (session_dir / "control_events.jsonl").chmod(0o000)

    snap = build_trajectory(session_dir)

    assert snap.turns == []
    unreadable = [w for w in snap.warnings if w.code == "ledger_unreadable"]
    assert len(unreadable) == 1
    assert "errno 13" in unreadable[0].detail  # EACCES, named rather than implied
    # the ledger is present, so this is not the "not written yet" state
    assert [w.code for w in snap.warnings if w.code == "missing_control_events"] == []


@not_root
def test_a_follower_that_cannot_read_the_ledger_says_the_same_thing(tmp_path):
    """Both feeds state the same hole, or the live view is a second derivation."""
    session_dir = _session(tmp_path, events="\n".join(EVENT_LINES) + "\n", tokens=REAL_TOKEN_CSV)
    (session_dir / "control_events.jsonl").chmod(0o000)
    accumulated = _accumulate(session_dir, [])

    assert accumulated.turns == []
    assert [w.code for w in accumulated.warnings if w.code == "ledger_unreadable"] == [
        "ledger_unreadable"
    ]
    assert accumulated == build_trajectory(session_dir)


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


def _accumulate(session_dir: Path, pending: list[str], **kwargs):
    """Follow a session that grows one line per poll, and fold what it yields."""
    events = session_dir / "control_events.jsonl"
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        with events.open("a", encoding="utf-8") as handle:
            handle.write(pending.pop(0))

    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep, **kwargs):
            accumulator.feed(delta)
    return accumulator.snapshot()


def _accumulate_then_bill(session_dir: Path, pending: list[str], tokens: str = REAL_TOKEN_CSV):
    """Follow a session that writes its token ledger only once the run has ended.

    This is the engine's real order, not a pessimistic one: every
    `_export_token_usage_csv` call site in `react_engine.py` sits on a
    termination path, so no live follow ever sees a token row while a turn is
    still being emitted. Writing the ledger up front — as the golden helper did
    — hands the follower a finished artifact and measures nothing about lateness.
    """
    events = session_dir / "control_events.jsonl"
    ledger = session_dir / "token_usage.csv"
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if pending:
            with events.open("a", encoding="utf-8") as handle:
                handle.write(pending.pop(0))
        elif not ledger.exists():
            ledger.write_text(tokens, encoding="utf-8")  # the loop exits, the bill lands
        else:
            raise _NoMoreLines

    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep):
            accumulator.feed(delta)
    return accumulator.snapshot()


def test_a_token_ledger_written_after_the_last_event_still_bills_its_turns(tmp_path):
    """The bill arrives after the turns it pays for, and still finds them.

    A join that only ever looks at the turns in the delta in front of it bills
    nothing at all live, because the ledger it consults is empty every time a
    turn passes. The accumulated document then disagrees with the replay about
    every token joined AND about which rows billed nobody — in exactly the mode
    spec §0 names the primary consumer.
    """
    session_dir = _session(tmp_path, events="")
    accumulated = _accumulate_then_bill(session_dir, [line + "\n" for line in EVENT_LINES])

    assert [t.tokens.input for t in accumulated.turns if t.tokens is not None] == [4134, 4463]
    assert [w.code for w in accumulated.warnings] == []
    assert accumulated == build_trajectory(session_dir)


def test_a_late_bill_pays_the_turn_the_replay_pays_not_the_one_in_flight(tmp_path):
    """One response, two turns: the row pays the first, whenever it lands.

    The claim on an iteration is made when the TURN appears, not when its row
    does. Deferring the claim until a row exists would hand the bill to
    whichever sibling turn the follower happened to be holding when the ledger
    finally landed, and the run would be billed on a different turn than the
    replay of the same bytes bills.
    """
    events = "\n".join(_lines(REAL_TWO_DECISIONS_JSONL)) + "\n"
    session_dir = _session(tmp_path, events="")
    accumulated = _accumulate_then_bill(session_dir, [line + "\n" for line in events.splitlines()])

    assert [t.iteration for t in accumulated.turns] == [1, 1]
    assert [t.turn_id for t in accumulated.turns if t.tokens is not None] == [1]
    assert accumulated == build_trajectory(session_dir)


def test_the_accumulated_follow_is_the_batch_replay_document(tmp_path):
    """Not "the same turns": the same document, warnings and all.

    Live and post-hoc are one fold (spec §1), so a watcher who folds every
    delta must end holding exactly what a replay of the finished file produces.
    Anything the follow cannot say — a hole in the turn still open, a token row
    that billed nobody — is a claim the batch view makes and the live view does
    not, which is two derivations wearing one name.
    """
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    accumulated = _accumulate(session_dir, [line + "\n" for line in EVENT_LINES])
    assert accumulated == build_trajectory(session_dir)


def test_the_accumulated_follow_states_the_holes_of_the_turn_still_open(tmp_path):
    """The half-told turn at the tail is the live view's whole point."""
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    accumulated = _accumulate(session_dir, [EVENT_LINES[0] + "\n", EVENT_LINES[3] + "\n"])
    assert {(w.code, w.turn_id, w.control_seq) for w in accumulated.warnings} == {
        ("missing_loop_decision", 1, 3),
        ("missing_tool_result", 1, 3),
        ("missing_loop_decision", 2, 11),
        ("missing_tool_result", 2, 11),
        # neither call reported back, so neither response's spend has an owner
        ("tokens_unattributed", None, None),
        # turn 1 is no longer in flight and never got an answer or a decision,
        # so the count fence says so too — turn 2 still can, and is not counted
        ("conservation_violation", None, None),
    }
    assert accumulated == build_trajectory(session_dir)


def test_batch_withholds_a_torn_tail_exactly_as_follow_does(tmp_path):
    """A line without its newline is a line still being written, in both feeds.

    This is the one case that only happens live, so it is the one case the two
    feeds must not disagree on: batch read the fragment as an event and called
    it malformed, while follow — correctly — waited for the newline.
    """
    torn = "\n".join(EVENT_LINES) + "\n" + EVENT_LINES[0][:200]
    session_dir = _session(tmp_path, events=torn, tokens=REAL_TOKEN_CSV)

    snap = build_trajectory(session_dir)
    assert [w.code for w in snap.warnings if w.code == "malformed_event_line"] == []
    assert len(snap.turns) == 2

    live_root = tmp_path / "live"
    live_root.mkdir()
    live_dir = _session(live_root, events="", tokens=REAL_TOKEN_CSV)
    assert _accumulate(live_dir, [torn]).turns == snap.turns


def test_an_archived_ledger_that_ends_mid_line_says_so(tmp_path):
    """Withholding a fragment is right; withholding that there IS one is not.

    A replay is reading a file nobody is appending to any more, so the missing
    newline is never coming: those bytes are a line the run died in the middle
    of writing. Dropping them silently is the trajectory quietly disagreeing
    with the ledger's own byte count, which is the one thing an evidence layer
    may not do. The offset says exactly where to look.
    """
    whole = "\n".join(EVENT_LINES) + "\n"
    fragment = EVENT_LINES[0][:200]
    session_dir = _session(tmp_path, events=whole + fragment, tokens=REAL_TOKEN_CSV)

    snap = build_trajectory(session_dir)
    assert len(snap.turns) == 2  # the fragment is still not an event
    assert [w.code for w in snap.warnings] == ["ledger_tail_torn"]
    assert snap.warnings[0].detail == (
        f"control_events.jsonl ends mid-line: 200 byte(s) from offset "
        f"{len(whole.encode())} carry no newline yet"
    )


def test_a_follow_withholds_a_torn_tail_while_live_and_states_it_when_finalized(tmp_path):
    """Live, the newline is coming. Once nobody is watching, it never was.

    These are the same bytes reported two ways on purpose: mid-run a fragment is
    a line being written and warning about it would cry wolf on every poll that
    caught the engine mid-`write`. The follow's END is the moment that stops
    being true, so `close()` — the caller saying it has stopped watching — is
    where the statement belongs, and it is the same statement a replay of those
    bytes makes.
    """
    whole = "\n".join(EVENT_LINES) + "\n"
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    pending = [whole, EVENT_LINES[0][:200]]
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        with events.open("a", encoding="utf-8") as handle:
            handle.write(pending.pop(0))

    follow = follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep)
    with pytest.raises(_NoMoreLines):
        for delta in follow:
            accumulator.feed(delta)
    assert all(w.code != "ledger_tail_torn" for w in accumulator.snapshot().warnings)

    final = follow.close()
    assert [w.code for w in final.warnings] == ["ledger_tail_torn"]
    accumulator.feed(final)
    assert accumulator.snapshot() == build_trajectory(session_dir)


def test_closing_a_follow_twice_states_the_torn_tail_once(tmp_path):
    """`close()` is idempotent, because a statement made twice is two statements.

    A caller that closes in a `finally` and again on the way out — or a CLI that
    closes the stream it also broke out of — would otherwise be handed the torn
    tail a second time and print it twice. Warnings are compared by value, so a
    folding consumer survives that; a consumer printing one delta per line does
    not, and the ledger did not end mid-line twice.
    """
    whole = "\n".join(EVENT_LINES) + "\n"
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    pending = [whole + EVENT_LINES[0][:200]]

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        events.write_text(pending.pop(0), encoding="utf-8")

    follow = follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep)
    with pytest.raises(_NoMoreLines):
        for _ in follow:
            pass

    first = follow.close()
    assert [w.code for w in first.warnings] == ["ledger_tail_torn"]
    assert follow.close() is None


def test_a_follow_that_ends_on_a_whole_ledger_has_nothing_left_to_say(tmp_path):
    """`close()` is a question, not an announcement: an intact tail answers None."""
    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    events = session_dir / "control_events.jsonl"
    pending = ["\n".join(EVENT_LINES) + "\n"]

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        events.write_text(pending.pop(0), encoding="utf-8")

    follow = follow_trajectory(session_dir, poll_seconds=0.01, sleep=fake_sleep)
    with pytest.raises(_NoMoreLines):
        for _ in follow:
            pass
    assert follow.close() is None


def test_an_output_store_written_after_the_follow_started_still_resolves(tmp_path):
    """A live session writes its bytes as it goes; one absent look is not forever.

    Attaching to a session before `contexts/full_outputs.jsonl` exists is the
    normal way to watch a run start. Remembering "no store" from that first look
    meant the full tier resolved nothing for the rest of the session.
    """
    from test_trajectory_cli import REAL_FULL_OUTPUT_RECORD

    session_dir = _session(tmp_path, events="", tokens=REAL_TOKEN_CSV)
    contexts = session_dir / ".setup_agent" / "contexts"
    events = session_dir / "control_events.jsonl"
    pending = [line + "\n" for line in EVENT_LINES[:3]]
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _NoMoreLines
        if not contexts.exists() and len(pending) == 1:  # the store lands mid-run
            contexts.mkdir(parents=True)
            (contexts / "full_outputs.jsonl").write_text(
                REAL_FULL_OUTPUT_RECORD + "\n", encoding="utf-8"
            )
        with events.open("a", encoding="utf-8") as handle:
            handle.write(pending.pop(0))

    with pytest.raises(_NoMoreLines):
        for delta in follow_trajectory(
            session_dir, detail="full", poll_seconds=0.01, sleep=fake_sleep
        ):
            accumulator.feed(delta)

    snapshot = accumulator.snapshot()
    assert snapshot.outputs["output_6163859b019d"].startswith("✅ Repository cloned")
    assert [w.code for w in snapshot.warnings if w.code == "missing_output_store"] == []
    assert snapshot == build_trajectory(session_dir, detail="full")


def test_the_full_tier_resolves_the_whole_window_and_declares_the_cut(tmp_path):
    """[A] is a list, and the quad view resolves all of it — marker included.

    `window_ref` is one handle: the newest message. A reader expanding the row
    wants the ARRAY, so the full tier resolves every component the record
    names. The one entry that is not bytes is the truncation marker, which
    names a cut rather than a body — declared out of store by name, exactly
    like ignite's `job:` handle, and never handed to the resolver.
    """
    session_dir = _session(tmp_path)
    store = OutputStorageManager(session_dir / "contexts")
    bodies = [json.dumps({"role": "user", "content": f"message {index}"}) for index in range(3)]
    refs = [
        store.store_output(
            task_id=OBSERVABILITY_TASK_ID,
            tool_name="window_component",
            output=body,
            timestamp=f"2026-08-15T00:00:0{index}Z",
        )
        for index, body in enumerate(bodies)
    ]
    record = {
        "event_id": "control-000001",
        "kind": "turn_record",
        "payload": {
            "turn_id": 1,
            "phase": "build",
            "iteration": 4,
            "actor": "model",
            "window_digest": {
                "system_prompt_sha256": "a" * 64,
                "component_refs": ["window_truncated:953", *refs],
            },
            "t0": "2026-08-15T00:00:00Z",
            "t1": "2026-08-15T00:00:01Z",
        },
        "sequence": 1,
        "source": None,
        "timestamp": "2026-08-15T00:00:01Z",
    }
    (session_dir / "control_events.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    snapshot = build_trajectory(session_dir, detail="full")

    turn = snapshot.turns[0]
    assert turn.window_components == ["window_truncated:953", *refs]
    assert turn.window_ref == refs[-1]
    assert [snapshot.outputs[ref] for ref in refs] == bodies
    assert "window_truncated:953" not in snapshot.outputs
    assert [(w.code, w.detail.split(" ", 1)[0]) for w in snapshot.warnings] == [
        ("ref_out_of_store", "window_truncated:953")
    ]
