"""A session stream must not transfer repair authority between commands."""

import pytest
from test_repair_lineage_foundation import _strict_events, _v3_repair_rows

from sag.agent.control_events import ControlEvent
from sag.agent.repair_contexts import repair_context_sha256
from sag.agent.replay import (
    ReplayValidationError,
    recover_active_repair_context,
    recover_active_repair_context_from_path,
)


def _open_repair_events(run_id, *, offset=0):
    rows = _v3_repair_rows()
    opened = next(i for i, row in enumerate(rows) if row.get("kind") == "repair_context_opened")
    context = rows[opened]["payload"]["context"]
    context["domain_id"] = f"build:/workspace/{run_id}"
    rows[opened]["payload"]["context_sha256"] = repair_context_sha256(context)
    result = []
    for event in _strict_events(rows[: opened + 1]):
        payload = dict(event.payload)
        if event.kind == "repair_context_opened":
            payload["source_gate_sequence"] += offset
        result.append(
            event.model_copy(
                update={
                    "sequence": event.sequence + offset,
                    "run_id": run_id,
                    "payload": payload,
                }
            )
        )
    return result


def test_repair_recovery_keeps_original_gate_sequences_across_runs(tmp_path):
    old_events = _open_repair_events("old-run")
    events = old_events + _open_repair_events("new-run", offset=len(old_events))
    path = tmp_path / "control_events.jsonl"
    path.write_text("".join(event.model_dump_json() + "\n" for event in events))

    recovered = recover_active_repair_context_from_path(path, run_id="new-run")

    assert recovered.context.domain_id == "build:/workspace/new-run"
    assert recovered.source_gate_sequence > len(old_events)
    assert (
        recover_active_repair_context(events, run_id="old-run").context.domain_id
        == "build:/workspace/old-run"
    )
    assert recover_active_repair_context(events, run_id="absent-run").context is None


def test_current_run_incomplete_lineage_still_rejects_recovery():
    events = _open_repair_events("current-run")[:-1]

    with pytest.raises(ReplayValidationError, match="repair_context_opened"):
        recover_active_repair_context(events, run_id="current-run")


def test_foreign_run_sequence_corruption_is_not_ignored():
    events = _open_repair_events("old-run")
    events[0] = events[0].model_copy(update={"sequence": 2})

    with pytest.raises(ReplayValidationError, match="sequence"):
        recover_active_repair_context(events, run_id="new-run")


def test_unscoped_history_is_not_claimed_by_a_new_run():
    events = [event.model_copy(update={"run_id": None}) for event in _open_repair_events("old-run")]

    assert recover_active_repair_context(events, run_id="new-run").context is None
    assert recover_active_repair_context(events).context is not None
    assert "run_id" not in events[0].model_dump()


def test_evidence_event_cannot_claim_a_different_outer_run():
    with pytest.raises(ValueError, match="run identity disagrees"):
        ControlEvent(
            sequence=1,
            kind="evidence_store_bound",
            run_id="current-run",
            payload={"run_id": "different-run", "store_identity": "docker:test-store"},
        )
