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
  turn's observation is checked against the call it claims to describe. Its
  token ledger is here as well, because refusals are where the billing rule is
  hardest to state and this is the archive that has them.
- **ignite** (`session_20260814_074153_238028_5398df380672_24385`) — the one
  archived session with a real `forced_action` (seq 235, the forced test
  dispatch its slice reconstructs in Part 2). The controller's turns are the
  half of the vocabulary the other two fixtures never exercise: derived like any
  other turn, annotated as forced, and never billed for a model response the
  model did not make.

All three fixtures are pre-closure sessions. Their warnings are the point: when
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
IGNITE = FIXTURES / "ignite-d2r3"

#: The warning codes that name a call the ledger never fully accounted for.
SILENT_CALL_CODES = ("missing_loop_decision", "missing_tool_result")


class _EndOfLedger(Exception):
    """Raised by the injected clock once the archived file is fully replayed."""


def _accumulated(
    session_dir: Path,
    tmp_path: Path,
    *,
    chunk: int = 40,
    withheld: tuple[str, ...] = (),
    detail: str = "summary",
) -> Trajectory:
    """Replay an archived ledger THROUGH `follow_trajectory`, folding its deltas.

    The archived bytes are copied into a growing file so the live path — the
    tail reader, the incremental joins, the warning retractions — is the code
    actually under test. Hand-feeding the reducer would fence the reducer
    against itself and leave everything the follower adds unmeasured.

    `withheld` names the artifacts a live run does not write until it ends, so
    the follow sees them land AFTER its last event rather than before its first
    poll. Copying everything up front would hand the follower a finished session
    wearing a growing ledger — the one arrangement in which a join that only
    ever looks forward still passes.
    """
    live = tmp_path / session_dir.name
    live.mkdir(parents=True, exist_ok=True)
    for artifact in session_dir.iterdir():
        if artifact.name != "control_events.jsonl" and artifact.name not in withheld:
            (live / artifact.name).write_bytes(artifact.read_bytes())
    events = live / "control_events.jsonl"
    events.write_bytes(b"")

    lines = (session_dir / "control_events.jsonl").read_bytes().split(b"\n")
    pending = [b"\n".join(lines[i : i + chunk]) + b"\n" for i in range(0, len(lines) - 1, chunk)]
    late = list(withheld)
    accumulator = DeltaAccumulator()

    def fake_sleep(seconds: float) -> None:
        if pending:
            with events.open("ab") as handle:
                handle.write(pending.pop(0))
        elif late:
            name = late.pop(0)
            (live / name).write_bytes((session_dir / name).read_bytes())
        else:
            raise _EndOfLedger

    with pytest.raises(_EndOfLedger):
        for delta in follow_trajectory(live, detail=detail, poll_seconds=0.01, sleep=fake_sleep):
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


def test_kafkas_silent_calls_come_up_short_on_the_decided_side_of_the_fence():
    """The count reaches the same verdict as the pairing, from the other end.

    The per-turn holes name ten calls that emitted no `loop_decision`. The
    conservation formula (§2.2 rule 5) counts calls instead of pairing them,
    and finds the decided side short by nine — the tenth silent call is turn
    24, the run's last, which is still in flight and therefore not yet counted.
    Two independent readings of one measured hole.
    """
    snap = build_trajectory(KAFKA)
    [violation] = [w for w in snap.warnings if w.code == "conservation_violation"]

    assert "decided (loop_decision + cancelled) by 9" in violation.detail
    silent = [w.turn_id for w in snap.warnings if w.code == "missing_loop_decision"]
    assert len(silent) == 10 and silent[-1] == snap.turns[-1].turn_id == 24


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
    for session_dir in (KAFKA, CAMEL_QUARKUS, IGNITE):
        assert _accumulated(session_dir, tmp_path) == build_trajectory(session_dir)


def test_the_follow_of_kafka_says_everything_the_replay_says(tmp_path):
    """The equality above is not two empty documents agreeing."""
    accumulated = _accumulated(KAFKA, tmp_path)
    assert len(accumulated.turns) == 24
    assert [w.code for w in accumulated.warnings].count("missing_loop_decision") == 10
    assert any(t.tokens is not None for t in accumulated.turns)
    assert accumulated.session.run_id.endswith("b9b1b06dfff3")


def test_kafkas_token_ledger_lands_after_its_last_event_and_still_bills(tmp_path):
    """The real write order: the engine exports `token_usage.csv` when it exits.

    Every `_export_token_usage_csv` call site in the ReAct engine is a
    termination path, so a `sag trajectory --follow` attached to a running
    kafka sees no token ledger at all while its twenty-four turns are emitted —
    the file appears after the last event. Replayed that way, the follow used to
    bill zero of the eleven turns the replay bills and to claim nineteen
    unattributed rows where the replay claims eight: two documents, one name.
    """
    accumulated = _accumulated(KAFKA, tmp_path, withheld=("token_usage.csv",))
    assert len([t for t in accumulated.turns if t.tokens is not None]) == 11
    assert accumulated == build_trajectory(KAFKA)


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


def test_camel_quarkus_bills_a_refusal_to_the_response_that_was_refused():
    """A refused response cost tokens, and the run paid for it exactly once.

    The three refusals (seq 124, 138, 216) are the hard case for the billing
    rule, because a refusal is a model turn with no call, and its retry is a
    different response with a bill of its own:

    - seq 124 is iteration 17 and the retry at seq 125 is iteration 18. Two
      responses, two rows, two bills — the refusal is not the retry's cost and
      the retry is not free.
    - seq 216 is iteration 43, the SAME response that opened the `search` call
      at seq 213. One response, two turns: the row bills the first of them and
      the refusal rides along, exactly as any pair of sibling calls does.

    Nothing about a refusal is special to the biller, and that is the claim: the
    bill follows the RESPONSE, so the rule needs no case for refusals at all.
    """
    snap = build_trajectory(CAMEL_QUARKUS)
    opened = {turn.control_seq[0]: turn for turn in snap.turns if turn.control_seq}

    refusal, retry = opened[124], opened[125]
    assert (refusal.iteration, retry.iteration) == (17, 18)
    assert refusal.call is None and refusal.tokens.input == 16220  # row: iteration 17
    assert retry.tokens.input == 16393  # row: iteration 18, the retry's own response

    assert opened[138].iteration == 21 and opened[138].tokens.input == 15820
    assert opened[139].iteration == 22 and opened[139].tokens.input == 14613

    sibling, shared_refusal = opened[213], opened[216]
    assert sibling.iteration == shared_refusal.iteration == 43
    assert sibling.tokens is not None and shared_refusal.tokens is None


def test_camel_quarkus_bills_every_response_once_and_no_response_twice():
    """Sum conservation over the real ledger: what was billed is what was spent.

    A per-turn join is only honest if it adds up. Every billed turn's tokens
    must come from an executor row of its own iteration, no iteration may be
    billed twice, and the total over the turns must equal the total over exactly
    the rows that joined them — the rest being the `tokens_unattributed` spend
    the ledger's silent calls left ownerless.
    """
    snap = build_trajectory(CAMEL_QUARKUS)
    billed = [t for t in snap.turns if t.tokens is not None]
    iterations = [t.iteration for t in billed]
    rows = _executor_rows(CAMEL_QUARKUS)
    joined = [r for r in rows if int(r["iteration"]) in set(iterations)]

    assert iterations == sorted(set(iterations))  # billed once, in ledger order
    assert all(t.actor == "model" for t in billed)
    assert sum(t.tokens.input for t in billed) == sum(int(r["prompt_tokens"]) for r in joined)
    assert sum(t.tokens.output for t in billed) == sum(int(r["completion_tokens"]) for r in joined)
    assert len(billed) == 37 and len(rows) == 55
    assert [w.detail for w in snap.warnings if w.code == "tokens_unattributed"] == [
        "18 executor row(s) bill no turn: iteration(s) "
        "2, 4, 6, 16, 20, 29, 34, 42, 44, 46, 48, 49, 50, 51, 52, 53, 54, 55"
    ]


def test_ignites_forced_dispatch_is_a_controller_turn_on_the_record():
    """The forced test dispatch of the ignite slice, derived rather than dug up.

    The slice's Part 2 reconstructs seq 235 by hand: "The dispatch was not a
    model call: `intent_source` is `controller` and the envelope id is
    `forced-000235`", phase `test`, attempt `test-1`, model iteration 28. The
    trajectory must say all of that without an archaeologist, and say WHY the
    turn exists — the policy and trigger that produced it — through the `forced`
    annotation rather than by leaving the row indistinguishable from a model's.
    """
    snap = build_trajectory(IGNITE)
    controller = [t for t in snap.turns if t.actor == "controller"]
    assert len(controller) == 1
    turn = controller[0]

    assert turn.call.tool == "build" and turn.call.params_ref == "forced-000235"
    assert turn.phase == "test" and turn.iteration == 28
    # The dispatch, its result, its decision — and seq 240, the close that found
    # the job it started still running, which is folded into this turn because
    # it is a fact ABOUT this turn (see the anomaly fence below).
    assert turn.control_seq == [235, 238, 239, 240]
    assert turn.observation.ref == "job:2c4d56b2fdca"  # the job the dispatch created

    forced = [a for a in snap.annotations if a.kind == "forced" and a.turn_id == turn.turn_id]
    assert len(forced) == 1
    assert forced[0].data == {
        "policy": "test_attempt_required",
        "trigger": "termination_refusal",
        "reason_code": "test_receipt_missing",
        "source_attempt_id": "test-1",
    }
    assert len(snap.turns) == 35  # slice census: 34 action_envelope + 1 forced_action


def test_ignites_run_ended_with_its_test_job_still_running_and_says_so():
    """The ignite slice's last fact, derived: the run closed on a live job.

    Seq 240 is the archive's only `job_live_at_close` — the deadline boundary
    finding `2c4d56b2fdca`, the job the forced dispatch at seq 235 started, with
    no terminal marker. Read as a row alone, that dispatch looks like a call
    that merely had not answered yet; the mark is what tells a reader the answer
    never came, and it lands on the turn that started the job because the ledger
    itself joins them by job id.

    This is the same fence `tests/test_trajectory_reducer.py` runs over the four
    events pasted into it, replayed here over the UNTRIMMED file, so no trim can
    be what makes either pass.
    """
    snap = build_trajectory(IGNITE)
    turn = next(t for t in snap.turns if t.actor == "controller")

    anomalies = [a for a in snap.annotations if a.kind == "conflict"]
    assert len(anomalies) == 1  # one bleed in the whole run, and it is this one
    assert anomalies[0].turn_id == turn.turn_id
    assert anomalies[0].data == {
        "anomaly": "job_never_settled",
        "job_id": "2c4d56b2fdca",
        "close_reason": "deadline",
        "log_ref": "/tmp/sag_jobs/2c4d56b2fdca.log",
        "obligation_ref": "/workspace/.setup_agent/job_obligations/2c4d56b2fdca.json",
        "stated_by": "job_live_at_close",
    }
    # A mark is a fact the ledger stated; it never becomes a hole in the ledger.
    assert [w for w in snap.warnings if w.code == "orphan_job_anomaly"] == []


def test_kafka_and_camel_quarkus_bled_nowhere_the_ledger_can_name():
    """No mark is drawn on a run that stated no exit code out of range.

    Both fixtures fail plenty — camel-quarkus exits 127 twice and 1 five times —
    and neither run was killed or left a job unsettled. A badge on those rows
    would be this layer inventing a severity the ledger never wrote.
    """
    for fixture in (KAFKA, CAMEL_QUARKUS):
        snap = build_trajectory(fixture)
        assert [a for a in snap.annotations if a.kind == "conflict"] == []


def test_ignites_job_ref_is_declared_out_of_store_instead_of_silently_absent():
    """`job:2c4d56b2fdca` is a real ref that `full_outputs.jsonl` has never held.

    The forced dispatch at seq 235 returned a detached job, and its observation
    ref is the job handle — `job:`-prefixed, not `output_`-prefixed. The full
    tier resolves output-store refs, so this one resolves to nothing, and the
    ref filter used to drop it before the resolver ever saw it. A reader
    expanding that row then got a turn whose observation names a ref and an
    `outputs` map that does not mention it, with nothing anywhere saying why.

    The rule is stated instead: a ref the output store was never asked to hold
    is declared out-of-store, by name, and the resolver is left alone.
    """
    snap = build_trajectory(IGNITE, detail="full")
    turn = next(t for t in snap.turns if t.actor == "controller")
    assert turn.observation.ref == "job:2c4d56b2fdca"

    declared = [w for w in snap.warnings if w.code == "ref_out_of_store"]
    assert [w.detail.split(" ", 1)[0] for w in declared] == ["job:2c4d56b2fdca"]
    assert "job:2c4d56b2fdca" not in (snap.outputs or {})
    # not an unresolved lookup: the store was never asked about a non-store ref
    assert [w for w in snap.warnings if w.code == "unresolved_output_ref"] == []


def test_the_summary_tier_declares_nothing_out_of_store_because_it_resolves_nothing():
    """The declaration is about bytes, and the summary tier never asks for bytes."""
    snap = build_trajectory(IGNITE)
    assert [w for w in snap.warnings if w.code == "ref_out_of_store"] == []


def test_a_live_watcher_hears_the_out_of_store_declaration_too(tmp_path):
    """One derivation: the full-tier follow says what the full-tier replay says.

    The one statement the two feeds legitimately word differently is
    `missing_output_store`, which names the directory it looked in — and the
    follow looks in a copy of the fixture, because that is what growing a
    ledger under a live reader requires. It is dropped from both sides rather
    than papered over, and everything else must match whole.
    """

    def without_the_path(document):
        kept = [w for w in document.warnings if w.code != "missing_output_store"]
        assert len(kept) == len(document.warnings) - 1  # it was there, on both sides
        return document.model_copy(update={"warnings": kept})

    accumulated = _accumulated(IGNITE, tmp_path, detail="full")
    assert [w.code for w in accumulated.warnings if w.code == "ref_out_of_store"] == [
        "ref_out_of_store"
    ]
    assert without_the_path(accumulated) == without_the_path(
        build_trajectory(IGNITE, detail="full")
    )


def test_ignites_forced_dispatch_is_never_billed_for_the_model_response_it_rode():
    """The harness moved; the model was not charged for it.

    A forced action carries the iteration of the loop it interrupted — 28, the
    response whose refusal triggered it — so an iteration-keyed join hands the
    controller the model's bill unless the actor is checked. The row for that
    response then bills nobody, and says so out loud rather than being quietly
    dropped into the controller's row.
    """
    snap = build_trajectory(IGNITE)
    turn = next(t for t in snap.turns if t.actor == "controller")
    assert turn.iteration == 28 and turn.tokens is None
    assert any(int(r["iteration"]) == 28 for r in _executor_rows(IGNITE))  # the row exists
    assert [w.detail for w in snap.warnings if w.code == "tokens_unattributed"] == [
        "6 executor row(s) bill no turn: iteration(s) 5, 9, 10, 14, 16, 28"
    ]


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
