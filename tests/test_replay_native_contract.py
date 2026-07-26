# tests/test_replay_native_contract.py
"""Replay's Plan 2 verification contract.

Replay used to prove determinism by re-executing the production
`ReasoningScheduler` and asserting mode/reason/plan-index equality. That
verifier was deleted with the protocol, so the contract is now a walk over the
recorded event stream:

1. every `tool_result` resolves to an `action_envelope` whose sha256 recomputes
   byte-identically from `{tool, params, tool_call_id|plan_index}` (dual key);
2. tool_result ordering is monotone per phase attempt;
3. the pairing invariant holds — exactly one `tool_result` per envelope;
4. gate decisions carry a claim for the open phase attempt;
5. transcripts recorded before Plan 2 verify under the SAME walk: rows of a
   kind the walk no longer models are skipped with a counted notice, never an
   error.
"""

import json
from pathlib import Path

import pytest

from sag.agent.control_events import action_envelope_sha256
from sag.agent.replay import ControlReplayRunner, ReplayValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "control_layer"


def _rows(fixture_name):
    return [
        json.loads(line)
        for line in (FIXTURES / fixture_name).read_text(encoding="utf-8").splitlines()
    ]


def _write(path, rows):
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _to_native(rows):
    """Re-key every envelope from plan_index to a native tool_call id."""
    converted = []
    for row in rows:
        if row.get("kind") == "action_envelope":
            payload = dict(row["payload"])
            call_id = f"call_{payload.pop('plan_index')}_{payload['envelope_id']}"
            payload["tool_call_id"] = call_id
            payload["envelope_sha256"] = action_envelope_sha256(
                tool_call_id=call_id,
                tool=payload["tool"],
                exact_params=payload["exact_params"],
            )
            row = {**row, "payload": payload}
        converted.append(row)
    return converted


def _drop_historical(rows):
    return [
        row
        for row in rows
        if row.get("kind") not in {"scheduler_decision", "planner_response"}
    ]


# --- 1. dual-key envelope hashing ------------------------------------------


def test_native_tool_call_envelopes_verify_under_the_same_walk(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    transcript = _write(tmp_path / "native-paramiko.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 6
    assert result.snapshot.verdict == "success"
    assert result.unconsumed_events == ()


def test_native_envelope_hash_mismatch_is_rejected(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    envelope = next(row for row in rows if row.get("kind") == "action_envelope")
    envelope["payload"]["tool_call_id"] = "call_tampered"
    transcript = _write(tmp_path / "native-bad-hash.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="envelope hash"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_plan_index_envelopes_still_hash_byte_identically():
    """Old transcripts keep verifying: their hashes were never recomputed."""
    result = ControlReplayRunner.offline().run(FIXTURES / "paramiko.jsonl")

    assert result.executed_envelope_count == 6
    assert result.produced_event_digest == result.expected_event_digest


# --- 2. pairing invariant ---------------------------------------------------


def test_an_envelope_without_a_tool_result_is_rejected(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    first_result = next(
        index for index, row in enumerate(rows) if row.get("kind") == "tool_result"
    )
    del rows[first_result]
    transcript = _write(tmp_path / "unanswered-envelope.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="exactly one tool_result"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_a_second_tool_result_for_one_envelope_is_rejected(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    index = next(i for i, row in enumerate(rows) if row.get("kind") == "tool_result")
    rows.insert(index + 1, json.loads(json.dumps(rows[index])))
    transcript = _write(tmp_path / "double-answered-envelope.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="tool_result"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_pairing_is_reported_on_the_result(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    transcript = _write(tmp_path / "paired.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.paired_envelope_count == result.executed_envelope_count


# --- 3. per-attempt tool_result ordering ------------------------------------


def test_a_tool_result_for_a_reopened_earlier_attempt_is_rejected(tmp_path):
    rows = _rows("bigtop.jsonl")
    results = [row for row in rows if row.get("kind") == "tool_result"]
    attempts = [str(row["payload"].get("source_attempt_id") or "") for row in results]
    assert len(set(attempts)) > 1, "fixture must span more than one phase attempt"

    stale_attempt = attempts[0]
    last_result = results[-1]
    assert last_result["payload"].get("source_attempt_id") != stale_attempt
    last_result["payload"]["source_attempt_id"] = stale_attempt
    transcript = _write(tmp_path / "out-of-order-attempt.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="monotone|stale phase attempt"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


# --- 4. gate decisions reference a live claim -------------------------------


def test_gate_decision_without_a_claim_phase_is_rejected(tmp_path):
    rows = _rows("paramiko.jsonl")
    gate = next(row for row in rows if row.get("kind") == "gate_decision")
    gate["payload"]["phase"] = "report"
    transcript = _write(tmp_path / "stale-gate.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="phase"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_gate_decisions_are_counted(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    expected = sum(1 for row in rows if row.get("kind") == "gate_decision")
    transcript = _write(tmp_path / "gates.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.gate_decision_count == expected


# --- 5. historical rows: skipped and counted, never fatal -------------------


def test_pre_plan2_rows_are_skipped_with_a_counted_notice(tmp_path):
    rows = _rows("paramiko.jsonl")
    scheduler_rows = sum(1 for row in rows if row.get("kind") == "scheduler_decision")
    planner_rows = sum(1 for row in rows if row.get("kind") == "planner_response")
    assert scheduler_rows and planner_rows, "fixture must carry pre-Plan-2 rows"

    result = ControlReplayRunner.offline().run(FIXTURES / "paramiko.jsonl")

    assert result.skipped_event_kinds == {
        "scheduler_decision": scheduler_rows,
        "planner_response": planner_rows,
    }
    assert result.snapshot.verdict == "success"


def test_a_native_transcript_skips_nothing(tmp_path):
    rows = _to_native(_drop_historical(_rows("paramiko.jsonl")))
    transcript = _write(tmp_path / "native-clean.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.skipped_event_kinds == {}


def test_scheduler_mode_and_reasons_are_no_longer_re_executed(tmp_path):
    """The deleted contract: replay used to reject a transcript whose recorded
    scheduler mode/reasons differed from a freshly driven scheduler. There is
    no scheduler to drive, so an impossible-looking decision row is inert."""
    rows = _rows("paramiko.jsonl")
    decision = next(row for row in rows if row.get("kind") == "scheduler_decision")
    decision["payload"]["mode"] = "action"
    decision["payload"]["reasons"] = ["a_reason_no_scheduler_would_produce"]
    transcript = _write(tmp_path / "inert-scheduler-row.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot.verdict == "success"


def test_the_module_documents_the_contract_change():
    import sag.agent.replay as replay_module

    doc = replay_module.__doc__ or ""
    assert "scheduler" in doc.lower()
    assert "tool_call" in doc
