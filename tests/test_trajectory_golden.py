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
  calls spec §0 measured — not swallowed.

Both fixtures are pre-closure sessions. Their warnings are the point: when
Pillar 1 lands, new sessions stop producing them, and these archived ones keep
theirs, because the bytes never change.
"""

from pathlib import Path

from sag.trajectory.builder import build_trajectory
from sag.trajectory.reducer import TrajectoryReducer
from sag.trajectory.schema import Trajectory

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"
KAFKA = FIXTURES / "kafka-d2r3"
CAMEL_QUARKUS = FIXTURES / "camel-quarkus-d2r3"

#: The warning codes that name a call the ledger never fully accounted for.
SILENT_CALL_CODES = ("missing_loop_decision", "missing_tool_result")


def _tokenless(trajectory: Trajectory) -> dict:
    """Strip the builder-only token join, leaving the reducer's own output."""
    document = trajectory.model_dump()
    for turn in document["turns"]:
        turn["tokens"] = None
    return document


def _replayed(session_dir: Path) -> TrajectoryReducer:
    reducer = TrajectoryReducer()
    for line in (session_dir / "control_events.jsonl").read_text(encoding="utf-8").splitlines():
        reducer.feed(line)
    return reducer


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


def test_a_hole_is_a_warning_and_never_a_lost_turn():
    """Every camel-quarkus call still has a row — the holes annotate, not delete."""
    snap = build_trajectory(CAMEL_QUARKUS)
    assert sum(1 for t in snap.turns if t.call is not None) == 55  # action_envelope count
    assert all(t.turn_id == i + 1 for i, t in enumerate(snap.turns))


def test_live_accumulation_equals_batch_replay():
    """The idempotence fence: one fold, whether the lines arrive live or archived."""
    for session_dir in (KAFKA, CAMEL_QUARKUS):
        batch = build_trajectory(session_dir)
        assert _tokenless(_replayed(session_dir).snapshot()) == _tokenless(batch)


def test_the_token_join_is_the_only_thing_batch_replay_adds_here():
    """`_tokenless` must not be papering over a difference that matters."""
    batch = build_trajectory(KAFKA)
    live = _replayed(KAFKA).snapshot()
    assert any(t.tokens is not None for t in batch.turns)
    assert all(t.tokens is None for t in live.turns)
    assert live.warnings == batch.warnings and live.session == batch.session
