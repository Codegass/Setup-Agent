"""The archived campaigns are the trajectory's golden anchors.

Two whole sessions from the d2r3 serial campaign are committed under
`tests/fixtures/trajectory/`, copied byte-for-byte out of their `logs/` session
directories. They are here because spec §5 asks the reducer to *mechanically
reproduce* facts the slice corpus already pinned by hand — so that the next
campaign reads a derivation instead of re-doing the archaeology.

The arbiter of every count below is `logs/d2r3-serial-20260814/slices/*.md`. If
a number here and a number there ever disagree, the slice is right and this
layer is wrong:

- **kafka** (`session_20260814_072758_651456_4d81644227c3_24117`) — the slice's
  §0 census is "derived from `control_events.jsonl` by filtering
  `kind == "action_envelope"` (24 events)". Twenty-four calls, therefore, and
  the reducer must find exactly twenty-four.
- **camel-quarkus** (`session_20260814_093336_763203_12dba3497295_26013`) — the
  slice states two holes in plain words: "`phase`-tool calls and the `advisor`
  call emit no `loop_decision`" and "Three `loop_decision` events (seq 124, 138,
  216) carry a refusal and have no `tool_result`". Pre-Pillar-1 those are
  warnings, and the fence is that they are *stated* — at least the ten silent
  calls spec §0 measured — not swallowed. Stated at the right sequence, too: a
  count alone would pass while every hole pointed one event past the call it
  belongs to, so the refusals are pinned to 124/138/216 by number and each
  turn's observation is checked against the call it claims to describe.

Both fixtures are pre-closure sessions. Their warnings are the point: when
Pillar 1 lands, new sessions stop producing them, and these archived ones keep
theirs, because the bytes never change.
"""

import csv
import io
from pathlib import Path

import pytest

from sag.trajectory.builder import build_trajectory, follow_trajectory
from sag.trajectory.reducer import DeltaAccumulator
from sag.trajectory.schema import Trajectory

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"
KAFKA = FIXTURES / "kafka-d2r3"
CAMEL_QUARKUS = FIXTURES / "camel-quarkus-d2r3"

#: The warning codes that name a call the ledger never fully accounted for.
SILENT_CALL_CODES = ("missing_loop_decision", "missing_tool_result")


class _EndOfLedger(Exception):
    """Raised by the injected clock once the archived file is fully replayed."""


def _accumulated(session_dir: Path, tmp_path: Path, *, chunk: int = 40) -> Trajectory:
    """Replay an archived ledger THROUGH `follow_trajectory`, folding its deltas.

    The archived bytes are copied into a growing file so the live path — the
    tail reader, the incremental joins, the warning retractions — is the code
    actually under test. Hand-feeding the reducer would fence the reducer
    against itself and leave everything the follower adds unmeasured.
    """
    live = tmp_path / session_dir.name
    live.mkdir(parents=True, exist_ok=True)
    for artifact in session_dir.iterdir():
        if artifact.name != "control_events.jsonl":
            (live / artifact.name).write_bytes(artifact.read_bytes())
    events = live / "control_events.jsonl"
    events.write_bytes(b"")

    lines = (session_dir / "control_events.jsonl").read_bytes().split(b"\n")
    pending = [b"\n".join(lines[i : i + chunk]) + b"\n" for i in range(0, len(lines) - 1, chunk)]
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if not pending:
            raise _EndOfLedger
        with events.open("ab") as handle:
            handle.write(pending.pop(0))

    with pytest.raises(_EndOfLedger):
        for delta in follow_trajectory(live, poll_seconds=0.01, sleep=fake_sleep):
            accumulator.feed(delta)
    return accumulator.snapshot()


def _executor_rows(session_dir: Path) -> list[dict]:
    text = (session_dir / "token_usage.csv").read_text(encoding="utf-8")
    return [r for r in csv.DictReader(io.StringIO(text)) if r["type"] == "executor"]


def test_kafka_d2r3_replays_to_exactly_24_calls():
    snap = build_trajectory(KAFKA)
    assert sum(1 for t in snap.turns if t.call is not None) == 24  # pinned by slice corpus


def test_kafka_d2r3_is_the_run_the_slice_corpus_read():
    """The fixture is that session, not a lookalike: the run id says so."""
    snap = build_trajectory(KAFKA)
    assert snap.session.run_id == "20260814_072758_651456_4d81644227c3_24117-7-b9b1b06dfff3"
    assert [p.name for p in snap.phases] == ["provision", "analyze", "build", "test", "report"]


def test_kafka_d2r3_joins_the_tokens_its_run_was_billed():
    snap = build_trajectory(KAFKA)
    billed = [t for t in snap.turns if t.tokens is not None]
    assert billed and billed[0].tokens.input == 4134  # token_usage.csv, iteration 1


def test_camel_quarkus_ten_silent_phase_calls_surface_as_warnings():
    snap = build_trajectory(CAMEL_QUARKUS)
    silent = [w for w in snap.warnings if w.code in SILENT_CALL_CODES]
    assert len(silent) >= 10  # the ledger holes the slice campaign measured


def test_camel_quarkus_silent_calls_are_the_phase_and_advisor_calls_the_slice_named():
    """The slice says which calls went silent; the derivation must agree."""
    snap = build_trajectory(CAMEL_QUARKUS)
    silent_turns = {
        turn.turn_id
        for turn in snap.turns
        for warning in snap.warnings
        if warning.code == "missing_loop_decision" and warning.control_seq == turn.control_seq[0]
    }
    tools = {snap.turns[turn_id - 1].call.tool for turn_id in silent_turns}
    assert tools <= {"phase", "advisor", "report"}
    assert "phase" in tools


def test_camel_quarkus_refusal_holes_sit_where_the_slice_pinned_them():
    """seq 124, 138, 216 — the three refusals, and no others.

    The slice states it in words (`slices/camel-quarkus.md:1249`): "Three
    `loop_decision` events (seq 124, 138, 216) carry a refusal and have no
    `tool_result`". A hole reported one event later would name the model's
    retry instead of the refused call — the same archaeology, re-derived.
    """
    snap = build_trajectory(CAMEL_QUARKUS)
    assert [w.control_seq for w in snap.warnings if w.code == "missing_envelope"] == [124, 138, 216]
    for sequence in (124, 138, 216):
        codes = {w.code for w in snap.warnings if w.control_seq == sequence}
        assert codes == {"missing_envelope", "missing_tool_result"}


def test_camel_quarkus_refusals_never_borrow_the_next_call_envelope():
    """The refused call has no envelope; the retry that followed keeps its own."""
    snap = build_trajectory(CAMEL_QUARKUS)
    opened = {turn.control_seq[0]: turn for turn in snap.turns if turn.control_seq}

    refusal = opened[124]
    assert refusal.call is None  # nothing to borrow — seq 125 is the retry, not this call
    assert refusal.iteration == 17
    assert refusal.observation.error_code == "REPAIR_INTENT_REQUIRED"
    assert refusal.observation.ref == "output_8a2db03465f6"

    retry = opened[125]
    assert retry.call.params_ref == "envelope-000125" and retry.iteration == 18
    assert retry.observation.ref == "output_8351840c096f"  # seq 127: operation_outcome success
    assert retry.observation.error_code is None
    assert retry.control_seq == [125, 127, 128]

    # seq 138 shifted five turns' worth of decisions when it was adopted; the
    # first of them is the fence for the whole cascade.
    assert opened[138].call is None and opened[138].iteration == 21
    assert opened[139].call.params_ref == "envelope-000139" and opened[139].iteration == 22
    assert opened[139].observation.error_code is None


def test_a_hole_is_a_warning_and_never_a_lost_turn():
    """Every camel-quarkus call still has a row — the holes annotate, not delete."""
    snap = build_trajectory(CAMEL_QUARKUS)
    assert sum(1 for t in snap.turns if t.call is not None) == 55  # action_envelope count
    assert all(t.turn_id == i + 1 for i, t in enumerate(snap.turns))


def test_live_accumulation_equals_batch_replay(tmp_path):
    """The idempotence fence, over the live path: one fold, one document.

    Spec §5 asks that live-accumulated == batch-replayed per session. The
    comparison is the WHOLE document — turns, phases, annotations, session,
    warnings — because a fence that stripped the parts the two feeds compute
    differently would be measuring the parts that never differed.
    """
    for session_dir in (KAFKA, CAMEL_QUARKUS):
        assert _accumulated(session_dir, tmp_path) == build_trajectory(session_dir)


def test_the_follow_of_kafka_says_everything_the_replay_says(tmp_path):
    """The equality above is not two empty documents agreeing."""
    accumulated = _accumulated(KAFKA, tmp_path)
    assert len(accumulated.turns) == 24
    assert [w.code for w in accumulated.warnings].count("missing_loop_decision") == 10
    assert any(t.tokens is not None for t in accumulated.turns)
    assert accumulated.session.run_id.endswith("b9b1b06dfff3")


def test_an_executor_row_bills_exactly_one_turn():
    """Tokens are a bill, and a bill is paid once.

    `iteration` is not a turn key: one model response can open several turns
    (kafka bills iterations 1, 3 and 6 for two calls each), and copying the
    row into every turn that carries the iteration invented spend the run never
    paid. The row bills the FIRST model turn of its iteration — the turn that
    response opened — and the siblings ride along unbilled.
    """
    snap = build_trajectory(KAFKA)
    billed = [t for t in snap.turns if t.tokens is not None]
    iterations = [t.iteration for t in billed]
    joined = [r for r in _executor_rows(KAFKA) if int(r["iteration"]) in set(iterations)]

    assert iterations == sorted(set(iterations))  # no iteration billed twice
    assert sum(t.tokens.input for t in billed) == sum(int(r["prompt_tokens"]) for r in joined)
    assert sum(t.tokens.output for t in billed) == sum(int(r["completion_tokens"]) for r in joined)
    assert all(t.actor == "model" for t in billed)
    assert len(billed) == 11  # 24 calls, 19 executor rows, 11 that join a model turn


def test_the_executor_rows_that_bill_nobody_are_counted_out_loud():
    """42% of kafka's rows join no turn; silence about that is a lie by omission.

    Every unattributed row is a call the ledger left silent — the ten phase and
    advisor calls that emit no `loop_decision`. Until Pillar 1 closes that hole
    the spend is real and unattributable, and the trajectory says so rather than
    presenting a token total that quietly omits a third of the run.
    """
    snap = build_trajectory(KAFKA)
    unattributed = [w for w in snap.warnings if w.code == "tokens_unattributed"]
    billed_iterations = {t.iteration for t in snap.turns if t.tokens is not None}
    orphans = [r for r in _executor_rows(KAFKA) if int(r["iteration"]) not in billed_iterations]

    assert len(unattributed) == 1
    assert str(len(orphans)) in unattributed[0].detail
    assert len(orphans) == 8 and len(_executor_rows(KAFKA)) == 19
