import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent, _active_setup_run_id
from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    CONTROL_EVENT_SCHEMA_VERSION,
    ControlEvent,
    ControlEventSink,
    EvidencePublicationPayload,
    RunPin,
    canonical_sha256,
    forced_action_sha256,
    job_stall_transition,
    sanitize_config,
)
from sag.agent.react_engine import ReActEngine
from sag.agent.replay import (
    ControlReplayRunner,
    ReplayMismatchError,
    ReplayValidationError,
    _validate_analysis_recovery_audit,
)
from sag.config.logger import SessionLogger
from sag.config.prompt_loader import PromptConfig
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult

FIXTURES = Path(__file__).parent / "fixtures" / "control_layer"


def test_live_run_id_is_a_unique_command_epoch_under_the_log_session(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "get_session_logger",
        lambda: SimpleNamespace(session_id="20260717_190128_88744"),
    )

    first = _active_setup_run_id(7)
    second = _active_setup_run_id(7)

    assert first.startswith("20260717_190128_88744-7-")
    assert second.startswith("20260717_190128_88744-7-")
    assert first != second


@pytest.mark.parametrize(
    "fixture_name",
    ["tvm.jsonl", "bigtop.jsonl", "paramiko.jsonl", "cassandra-java-driver.jsonl"],
)
def test_fixture_replays_to_declared_snapshot_without_external_calls(fixture_name):
    """Frozen v3 bytes verify while replay returns today's v5 projection.

    Premise updated 2026-08-10: archived expectations remain immutable; the
    comparison-only v3 view is checked inside the runner.
    """
    runner = ControlReplayRunner(
        llm_factory=lambda: pytest.fail("replay must not construct an LLM"),
        orchestrator_factory=lambda: pytest.fail("replay must not construct a container"),
    )

    result = runner.run(FIXTURES / fixture_name)

    assert result.header.fixture_kind == "recorded_tool_transcript"
    assert result.header.source_manifest
    assert result.expected_snapshot["schema_version"] == 3
    assert result.snapshot.schema_version == 5
    assert result.snapshot.rates
    assert result.snapshot.ci_comparison.status == "no_target"
    assert result.snapshot.ci_comparison.certificate is None
    assert result.unconsumed_events == ()
    assert result.produced_event_digest == result.expected_event_digest


def test_tvm_replay_never_enters_test_after_failed_build():
    result = ControlReplayRunner.offline().run(FIXTURES / "tvm.jsonl")

    assert result.phase("build").outcome.value == "failed"
    assert result.phase("test").termination.value == "skipped"
    assert result.loop_decisions[1].decision == "guide"


def test_bigtop_repair_is_dependency_valid_and_append_only():
    result = ControlReplayRunner.offline().run(FIXTURES / "bigtop.jsonl")

    assert [record.attempt_id for record in result.phase_attempts("build")] == [
        "build-1",
        "build-2",
    ]
    assert result.repair_routes[0].edge == ("test", "build")
    assert result.repair_routes[0].accepted is True


def test_paramiko_replay_pairs_six_envelopes_and_counts_historical_rows():
    """Plan 2: the six envelopes and their six results are the contract; the
    two `planner_response` rows are historical bookkeeping, not verification."""
    result = ControlReplayRunner.offline().run(FIXTURES / "paramiko.jsonl")

    assert result.executed_envelope_count == 6
    assert result.paired_envelope_count == 6
    assert result.planner_response_count == 2
    assert result.skipped_event_kinds["planner_response"] == 2
    assert result.compatibility_action_model_calls == 0


@pytest.mark.parametrize("mutation", ["duplicate", "out_of_order", "unknown_field"])
def test_transcript_rejects_noncanonical_event_stream(tmp_path, mutation):
    source = (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    if mutation == "duplicate":
        source.insert(2, source[1])
    elif mutation == "out_of_order":
        source[1], source[2] = source[2], source[1]
    else:
        source[1] = source[1][:-1] + ',"invented":true}'
    transcript = tmp_path / "invalid.jsonl"
    transcript.write_text("\n".join(source) + "\n", encoding="utf-8")

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline().run(transcript)


def test_envelope_hash_mismatch_is_rejected(tmp_path):
    text = (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8")
    transcript = tmp_path / "invalid-envelope.jsonl"
    transcript.write_text(
        text.replace('"envelope_sha256":"', '"envelope_sha256":"bad'), encoding="utf-8"
    )

    with pytest.raises(ReplayValidationError, match="envelope hash"):
        ControlReplayRunner.offline().run(transcript)


def test_replay_checks_recorded_loop_recurrence_count(tmp_path):
    rows = [
        json.loads(line)
        for line in (FIXTURES / "tvm.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    loop = next(row for row in rows if row.get("kind") == "loop_decision")
    loop["payload"]["event"]["recurrence_count"] = 99
    transcript = tmp_path / "invalid-recurrence.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReplayValidationError, match="recurrence count"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _completion_claim_decision(count):
    at_cap = count == 3
    return {
        "kind": "completion_claim_decision",
        "payload": {
            "phase_attempt_id": "test-1",
            "claim_kind": "done" if count % 2 else "blocked",
            "judge_disposition": "repair_required",
            "blocker_id": "test_failed",
            "mechanical_evidence_digest": "a" * 64,
            "assessment_fingerprints": ["b" * 64],
            "open_job_fingerprints": [],
            "evidence_refs": ["output_paramiko_tests"],
            "target_fingerprint": "target-1",
            "config_fingerprint": "config-1",
            "fact_fingerprint": "c" * 64,
            "expected_decision": "agent_no_progress" if at_cap else "continue",
            "expected_recurrence_count": count,
            "expected_reason_code": (
                "agent_no_progress" if at_cap else "completion_claim_without_action"
            ),
            "expected_close_phase": at_cap,
        },
    }


def test_replay_reconstructs_done_blocked_no_op_convergence(tmp_path):
    rows = _paramiko_rows_with_job_events(
        [_completion_claim_decision(count) for count in (1, 2, 3)]
    )
    transcript = tmp_path / "completion-convergence.jsonl"
    _write_replay_rows(transcript, rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot is not None


def test_replay_rejects_a_forged_completion_recurrence_count(tmp_path):
    events = [_completion_claim_decision(count) for count in (1, 2, 3)]
    events[1]["payload"]["expected_recurrence_count"] = 3
    rows = _paramiko_rows_with_job_events(events)
    transcript = tmp_path / "forged-completion-count.jsonl"
    _write_replay_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match="completion-claim decision"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _sealed_turn_event():
    return {
        "kind": "turn_record",
        "payload": {
            "turn_id": 1,
            "phase": "test",
            "iteration": 4,
            "actor": "model",
            "window_digest": {"system_prompt_sha256": "a" * 64, "component_refs": []},
            "t0": "2026-08-15T10:00:00Z",
            "t1": "2026-08-15T10:00:04Z",
        },
    }


def _refusal_event():
    return {
        "kind": "refusal_record",
        "payload": {
            "tool": "search",
            "tool_call_id": "call_9",
            "refusal_code": "REPAIR_CONTEXT_NOT_ACTIVE",
            "exact_params_sha256": canonical_sha256({"pattern": "spotless"}),
        },
    }


@pytest.mark.parametrize("record", [_sealed_turn_event, _refusal_event])
def test_a_record_about_a_run_never_makes_that_run_unreplayable(tmp_path, record):
    """Stage B's two observability kinds are CARRIED by the walk, not refused.

    Both are statements ABOUT a transcript the walk already verifies — a sealed
    turn, and a call that never reached a tool (spec §2.2 rule 4). Neither moves
    replay state. A kind with no branch falls through to `unsupported event
    kind`, which is an integrity-family abort raised on a legitimate ledger:
    every post-closure session carrying one becomes unreplayable, and the
    house's control-integrity oracle declares an honest run impossible.
    """
    rows = _paramiko_rows_with_job_events([record()])
    transcript = tmp_path / "sealed-record.jsonl"
    _write_replay_rows(transcript, rows)

    kind = record()["kind"]
    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot is not None
    assert result.unconsumed_events == ()
    assert kind in {json.loads(line).get("kind") for line in transcript.read_text().splitlines()}
    # Carried by a branch of its own, not filed away as machinery this walk
    # stopped modelling: these kinds are current, and the notice is for the old.
    assert kind not in result.skipped_event_kinds


def _write_replay_rows(path, rows):
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _paramiko_rows_with_job_events(events):
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [source[0]]
    for row in source[1:]:
        if row.get("kind") == "evidence_close":
            rows.extend({**event, "source": row["source"]} for event in events)
        rows.append(row)
    return rows


def _publication_event(
    *,
    run_id="replay-paramiko-20260712",
    record_kind="policy_claim",
    record_id="claim-replay-a",
    raw_sha256="a" * 64,
):
    payload = {
        "record_kind": record_kind,
        "record_id": record_id,
        "raw_sha256": raw_sha256,
        "byte_count": 1,
        "run_id": run_id,
    }
    if record_kind in {
        "job_obligation",
        "build_requirements",
        "run_pin",
        "document_map",
        "receipt_structure",
        "stall_cleanup",
        "report_metrics",
    }:
        logical_artifact_id = f"logical-{record_kind}"
        payload.update(
            {
                "record_id": logical_artifact_id,
                "logical_artifact_id": logical_artifact_id,
                "revision": 1,
                "previous_raw_sha256": "0" * 64,
                "previous_publication_sha256": "0" * 64,
            }
        )
    return {
        "kind": "evidence_publication",
        "payload": payload,
    }


def _store_binding_event(*, run_id="replay-paramiko-20260712"):
    return {
        "kind": "evidence_store_bound",
        "payload": {
            "run_id": run_id,
            "store_identity": "docker:replay-paramiko-store",
        },
    }


def _paramiko_rows_with_publications(publications, *, placement="inside_envelope"):
    rows = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    source = rows[1]["source"]
    # The host stream binds one immutable container store before it can
    # authorize any mirrored evidence bytes.  Keep the binding before the
    # action stream so both in-envelope and post-close publications share the
    # same durable epoch.
    rows.insert(1, {**_store_binding_event(), "source": source})
    additions = [{**publication, "source": source} for publication in publications]
    if placement == "inside_envelope":
        envelope_index = next(
            index for index, row in enumerate(rows) if row.get("kind") == "action_envelope"
        )
        rows[envelope_index + 1 : envelope_index + 1] = additions
    elif placement == "after_close":
        rows.extend(additions)
    else:
        raise AssertionError(f"unknown publication placement: {placement}")
    return rows


def test_evidence_publication_is_strict_but_inert_inside_pending_action_pair(tmp_path):
    transcript = tmp_path / "publication-inside-envelope.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_publications([_publication_event()]),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot.schema_version == 5
    assert result.snapshot.verdict == "partial"
    assert result.executed_envelope_count == result.paired_envelope_count == 6


def test_evidence_publication_without_a_prior_store_binding_is_rejected(tmp_path):
    rows = _paramiko_rows_with_publications([_publication_event()])
    rows = [row for row in rows if row.get("kind") != "evidence_store_bound"]
    transcript = tmp_path / "publication-without-store-binding.jsonl"
    _write_replay_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match="precedes the immutable store binding"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_evidence_store_binding_is_unique_and_matches_the_replay_run(tmp_path):
    duplicate_rows = _paramiko_rows_with_publications([_publication_event()])
    duplicate_rows.insert(2, duplicate_rows[1].copy())
    duplicate = tmp_path / "duplicate-store-binding.jsonl"
    _write_replay_rows(duplicate, duplicate_rows)
    with pytest.raises(ReplayValidationError, match="bind exactly once"):
        ControlReplayRunner.offline(verify_expected=False).run(duplicate)

    foreign_rows = _paramiko_rows_with_publications([_publication_event()])
    foreign_rows[1]["payload"]["run_id"] = "replay-foreign-run"
    foreign = tmp_path / "foreign-store-binding.jsonl"
    _write_replay_rows(foreign, foreign_rows)
    with pytest.raises(ReplayValidationError, match="binding belongs to a foreign run"):
        ControlReplayRunner.offline(verify_expected=False).run(foreign)


def test_evidence_publication_duplicate_is_idempotent_but_collision_is_rejected(tmp_path):
    publication = _publication_event()
    idempotent = tmp_path / "publication-idempotent.jsonl"
    _write_replay_rows(
        idempotent,
        _paramiko_rows_with_publications([publication, publication]),
    )
    assert ControlReplayRunner.offline(verify_expected=False).run(idempotent).snapshot

    conflict = tmp_path / "publication-conflict.jsonl"
    _write_replay_rows(
        conflict,
        _paramiko_rows_with_publications([publication, _publication_event(raw_sha256="b" * 64)]),
    )
    with pytest.raises(ReplayValidationError, match="conflicting record bytes"):
        ControlReplayRunner.offline(verify_expected=False).run(conflict)


def _next_mutable_publication(first, *, raw_sha256="b" * 64, record_kind=None):
    prior = EvidencePublicationPayload.model_validate(first["payload"])
    payload = dict(first["payload"])
    payload.update(
        {
            "record_kind": record_kind or prior.record_kind,
            "raw_sha256": raw_sha256,
            "revision": int(prior.revision) + 1,
            "previous_raw_sha256": prior.raw_sha256,
            "previous_publication_sha256": canonical_sha256(
                prior.model_dump(mode="json", exclude_unset=True)
            ),
        }
    )
    return {"kind": "evidence_publication", "payload": payload}


def test_mutable_publication_revision_chain_is_inert_and_strict(tmp_path):
    first = _publication_event(record_kind="run_pin")
    second = _next_mutable_publication(first)
    valid = tmp_path / "publication-revision-chain.jsonl"
    _write_replay_rows(
        valid,
        _paramiko_rows_with_publications([first, second]),
    )
    result = ControlReplayRunner.offline(verify_expected=False).run(valid)
    assert result.snapshot.schema_version == 5
    assert result.snapshot.verdict == "partial"

    forged = tmp_path / "publication-revision-forged.jsonl"
    second["payload"]["previous_publication_sha256"] = "f" * 64
    _write_replay_rows(
        forged,
        _paramiko_rows_with_publications([first, second]),
    )
    with pytest.raises(ReplayValidationError, match="revision chain is discontinuous"):
        ControlReplayRunner.offline(verify_expected=False).run(forged)


def test_mutable_publication_cannot_switch_kind_under_one_logical_head(tmp_path):
    first = _publication_event(record_kind="run_pin")
    second = _next_mutable_publication(first, record_kind="report_metrics")
    transcript = tmp_path / "publication-cross-kind.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_publications([first, second]),
    )

    with pytest.raises(ReplayValidationError, match="revision chain is discontinuous"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_mutable_tombstone_replays_but_cannot_be_resurrected(tmp_path):
    first = _publication_event(record_kind="report_metrics")
    tombstone = _next_mutable_publication(first)
    tombstone["payload"].update(
        {
            "raw_sha256": "0" * 64,
            "byte_count": 0,
            "publication_state": "revoked",
        }
    )
    valid = tmp_path / "publication-tombstone.jsonl"
    _write_replay_rows(
        valid,
        _paramiko_rows_with_publications([first, tombstone], placement="after_close"),
    )
    assert ControlReplayRunner.offline(verify_expected=False).run(valid).snapshot

    revoked = EvidencePublicationPayload.model_validate(tombstone["payload"])
    resurrection = {
        "kind": "evidence_publication",
        "payload": {
            **first["payload"],
            "raw_sha256": "c" * 64,
            "revision": 3,
            "previous_raw_sha256": revoked.raw_sha256,
            "previous_publication_sha256": canonical_sha256(
                revoked.model_dump(mode="json", exclude_unset=True)
            ),
        },
    }
    forged = tmp_path / "publication-resurrection.jsonl"
    _write_replay_rows(
        forged,
        _paramiko_rows_with_publications([first, tombstone, resurrection], placement="after_close"),
    )
    with pytest.raises(ReplayValidationError, match="revision chain is discontinuous"):
        ControlReplayRunner.offline(verify_expected=False).run(forged)


def test_evidence_publication_rejects_foreign_run(tmp_path):
    transcript = tmp_path / "publication-foreign-run.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_publications([_publication_event(run_id="replay-foreign-run")]),
    )

    with pytest.raises(ReplayValidationError, match="foreign run"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize(
    ("record_kind", "accepted"),
    [("run_pin", True), ("report_metrics", True), ("policy_claim", False)],
)
def test_only_final_host_artifacts_may_publish_after_evidence_close(
    tmp_path, record_kind, accepted
):
    transcript = tmp_path / f"publication-after-close-{record_kind}.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_publications(
            [_publication_event(record_kind=record_kind)],
            placement="after_close",
        ),
    )

    if accepted:
        assert ControlReplayRunner.offline(verify_expected=False).run(transcript).snapshot
    else:
        with pytest.raises(ReplayValidationError, match="may follow evidence_close"):
            ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _terminal_observed(job_id="job-replay-1", exit_code=137):
    return {
        "kind": "job_terminal_observed",
        "payload": {
            "job_id": job_id,
            "exit_code": exit_code,
            "marker_ref": f"/tmp/sag_jobs/{job_id}.exit",
            "observed_at": "2026-08-08T12:00:00Z",
            "obligation_ref": f"/workspace/.setup_agent/job_obligations/{job_id}.json",
        },
    }


def test_ws2_job_lifecycle_control_events_are_strict_and_append_only():
    assert CONTROL_EVENT_SCHEMA_VERSION == 5
    assert CONTROL_EVENT_KINDS[13:22] == (
        "job_terminal_observed",
        "job_terminal_unpersisted",
        "job_live_at_close",
        "job_barrier_integrity_failure",
        "completion_claim_decision",
        "job_stall_observed",
        "repair_context_opened",
        "evidence_publication",
        "evidence_store_bound",
    )
    event = ControlEvent(
        sequence=1,
        **_terminal_observed(),
    )

    assert event.typed_payload.exit_code == 137
    with pytest.raises(ValueError):
        ControlEvent(
            sequence=1,
            kind="job_live_at_close",
            payload={
                "job_id": "job-replay-1",
                "obligation_ref": "unpersisted",
                "log_ref": "/tmp/job.log",
                "close_reason": "deadline",
                "unsupported_conclusion": "hung",
            },
        )

    for failures in ([], [""]):
        with pytest.raises(ValueError):
            ControlEvent(
                sequence=1,
                kind="job_barrier_integrity_failure",
                payload={"failures": failures},
            )

    with pytest.raises(ValueError, match="sealed evidence"):
        ControlEvent(
            sequence=1,
            kind="job_stall_observed",
            payload={
                "job_id": "job-replay-1",
                "obligation_ref": "/workspace/.setup_agent/job_obligations/job-replay-1.json",
                "diagnostic_ref": "/workspace/.setup_agent/job_diagnostics/job-replay-1/bundle.json",
                "diagnostic_fingerprint": "a" * 64,
                "observation": "unknown",
                "progress_signals": [],
                "controller_code": "term_failed",
                "evidence_sealed": False,
                "term_sent": True,
                "kill_sent": False,
                "group_live": True,
            },
        )


def test_job_stall_observation_replays_as_physical_fact_without_project_cause(tmp_path):
    job_id = "job-stalled-1"
    event = {
        "kind": "job_stall_observed",
        "payload": {
            "job_id": job_id,
            "obligation_ref": f"/workspace/.setup_agent/job_obligations/{job_id}.json",
            "diagnostic_ref": f"/workspace/.setup_agent/job_diagnostics/{job_id}/bundle.json",
            "diagnostic_fingerprint": "b" * 64,
            "observation": "thread_join_wait",
            "progress_signals": [],
            "controller_code": "killed",
            "evidence_sealed": True,
            "term_sent": True,
            "kill_sent": True,
            "group_live": False,
        },
    }
    transcript = tmp_path / "job-stall-observed.jsonl"
    _write_replay_rows(transcript, _paramiko_rows_with_job_events([event]))

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert event["payload"]["diagnostic_ref"] in result.snapshot.input_refs
    assert not any("missing_broker" in conflict.lower() for conflict in result.snapshot.conflicts)


def test_stall_loop_projection_does_not_turn_unobservable_into_no_progress():
    base = {
        "job_id": "job-stalled-1",
        "obligation_ref": "/workspace/.setup_agent/job_obligations/job-stalled-1.json",
        "diagnostic_ref": "/workspace/.setup_agent/job_diagnostics/job-stalled-1/bundle.json",
        "diagnostic_fingerprint": "c" * 64,
        "observation": "unknown",
        "progress_signals": [],
        "controller_code": "progress_unobservable",
        "evidence_sealed": False,
        "term_sent": False,
        "kill_sent": False,
        "group_live": True,
    }

    assert job_stall_transition(base) is None
    assert job_stall_transition({**base, "evidence_sealed": True}) == "stalled"
    assert (
        job_stall_transition(
            {**base, "controller_code": "progress_observed", "progress_signals": ["cpu_active"]}
        )
        == "progress"
    )


def test_barrier_integrity_failure_replays_as_the_live_harness_conflict(tmp_path):
    transcript = tmp_path / "job-barrier-integrity-failure.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events(
            [
                {
                    "kind": "job_barrier_integrity_failure",
                    "payload": {
                        "failures": [
                            "job-1:exit_marker_malformed",
                            "job-2:obligation_transport_read_failed",
                        ]
                    },
                }
            ]
        ),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert "job_barrier_integrity_failure" in result.snapshot.conflicts
    assert "control:job_barrier_integrity_failure" in result.snapshot.input_refs


def test_duplicate_barrier_integrity_failure_is_rejected(tmp_path):
    event = {
        "kind": "job_barrier_integrity_failure",
        "payload": {"failures": ["job-1:exit_marker_unreadable"]},
    }
    transcript = tmp_path / "duplicate-job-barrier-integrity-failure.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events([event, event]),
    )

    with pytest.raises(ReplayValidationError, match="duplicate job barrier"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_barrier_integrity_failure_after_evidence_close_is_rejected(tmp_path):
    rows = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows.append(
        {
            "kind": "job_barrier_integrity_failure",
            "payload": {"failures": ["job-1:exit_marker_unreadable"]},
            "source": rows[-1]["source"],
        }
    )
    transcript = tmp_path / "late-job-barrier-integrity-failure.jsonl"
    _write_replay_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match="cannot follow evidence_close"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_terminal_observation_cannot_close_before_settlement_resolves(tmp_path):
    transcript = tmp_path / "terminal-observed.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events([_terminal_observed()]),
    )

    with pytest.raises(ReplayValidationError, match="unresolved terminal settlement"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_terminal_observation_then_settlement_preserves_the_verdict(tmp_path):
    job_id = "job-replay-settled"
    transcript = tmp_path / "terminal-settled.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events(
            [
                _terminal_observed(job_id=job_id, exit_code=0),
                {
                    "kind": "job_settled",
                    "payload": {
                        "job_id": job_id,
                        "receipt_id": "receipt-job-replay-settled",
                        "exit_code": 0,
                    },
                },
            ]
        ),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot.verdict == "partial"
    assert not any(conflict.startswith("job_terminal_") for conflict in result.snapshot.conflicts)


@pytest.mark.parametrize(
    "second",
    [
        {"receipt_id": "receipt-job-replay-settled", "exit_code": 0},
        {"receipt_id": "receipt-conflicting", "exit_code": 137},
    ],
)
def test_replay_rejects_any_second_job_settled_event(tmp_path, second):
    job_id = "job-replay-settled"
    events = [
        _terminal_observed(job_id=job_id, exit_code=0),
        {
            "kind": "job_settled",
            "payload": {
                "job_id": job_id,
                "receipt_id": "receipt-job-replay-settled",
                "exit_code": 0,
            },
        },
        {
            "kind": "job_settled",
            "payload": {"job_id": job_id, **second},
        },
    ]
    transcript = tmp_path / "duplicate-job-settled.jsonl"
    _write_replay_rows(transcript, _paramiko_rows_with_job_events(events))

    with pytest.raises(ReplayValidationError, match="duplicate job_settled"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _analysis_recovery_audit(**overrides):
    return {
        "kind": "framework_survey",
        "attempt": 1,
        "before_code": "analysis_facts_missing",
        "survey_status": "created",
        "after_code": "analysis_green",
        "resolved": True,
        **overrides,
    }


def test_analysis_recovery_audit_has_one_strict_replay_shape():
    audit = _analysis_recovery_audit()

    assert (
        _validate_analysis_recovery_audit(
            audit,
            phase="analyze",
            control_disposition="terminal_claimable",
        )
        == audit
    )

    with pytest.raises(ReplayValidationError, match="resolved disagrees"):
        _validate_analysis_recovery_audit(
            _analysis_recovery_audit(resolved=False),
            phase="analyze",
            control_disposition="terminal_claimable",
        )

    with pytest.raises(ReplayValidationError, match="before_code must be a string"):
        _validate_analysis_recovery_audit(
            _analysis_recovery_audit(before_code=["analysis_facts_missing"]),
            phase="analyze",
            control_disposition="terminal_claimable",
        )


@pytest.mark.parametrize("placement", ["non_analyze", "observation_only"])
def test_replay_rejects_misplaced_analysis_recovery_audit(tmp_path, placement):
    rows = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    observation = next(
        row
        for row in rows
        if row.get("kind") == "validator_observation" and row["payload"]["phase"] == "build"
    )
    gate = next(
        row
        for row in rows
        if row.get("kind") == "gate_decision" and row["payload"]["phase"] == "build"
    )
    observation["payload"]["validated_facts"]["run.analysis_recovery"] = _analysis_recovery_audit()
    if placement == "non_analyze":
        gate["payload"]["validated_facts"]["run.analysis_recovery"] = _analysis_recovery_audit()
        expected = "requires the analyze phase"
    else:
        # Prove final-only semantics independently of the phase restriction.
        observation["payload"]["phase"] = "analyze"
        expected = "differs from final gate"
    transcript = tmp_path / f"analysis-recovery-{placement}.jsonl"
    _write_replay_rows(transcript, rows)

    with pytest.raises(ReplayValidationError, match=expected):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_terminal_unpersisted_replay_rebuilds_the_live_conflict(tmp_path):
    job_id = "job-replay-unpersisted"
    observed = _terminal_observed(job_id=job_id, exit_code=0)
    unpersisted = {
        "kind": "job_terminal_unpersisted",
        "payload": {
            "job_id": job_id,
            "exit_code": 0,
            "attempted_receipt_id": "receipt-job-replay-unpersisted",
            "persistence_code": "transport_publish_failed",
            "attempt_count": 2,
            "obligation_ref": (f"/workspace/.setup_agent/job_obligations/{job_id}.json"),
            "log_ref": f"/tmp/sag_jobs/{job_id}.log",
        },
    }
    transcript = tmp_path / "terminal-unpersisted.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events([observed, unpersisted]),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert f"job_terminal_unpersisted:{job_id}" in result.snapshot.conflicts
    assert f"job_unsettled:{job_id}" not in result.snapshot.conflicts


def test_live_at_close_replay_rebuilds_the_live_conflict(tmp_path):
    job_id = "job-replay-live"
    transcript = tmp_path / "live-at-close.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events(
            [
                {
                    "kind": "job_live_at_close",
                    "payload": {
                        "job_id": job_id,
                        "obligation_ref": (
                            f"/workspace/.setup_agent/job_obligations/{job_id}.json"
                        ),
                        "log_ref": f"/tmp/sag_jobs/{job_id}.log",
                        "close_reason": "dependents_skipped",
                    },
                }
            ]
        ),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert f"job_live_at_close:{job_id}" in result.snapshot.conflicts
    assert f"job_unsettled:{job_id}" not in result.snapshot.conflicts


def test_legacy_job_unsettled_still_replays(tmp_path):
    job_id = "job-replay-legacy"
    transcript = tmp_path / "legacy-job-unsettled.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events(
            [
                {
                    "kind": "job_unsettled",
                    "payload": {
                        "job_id": job_id,
                        "evidence_ref": (f"/workspace/.setup_agent/job_obligations/{job_id}.json"),
                        "obligation": {"tool": "maven"},
                    },
                }
            ]
        ),
    )

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert f"job_unsettled:{job_id}" in result.snapshot.conflicts


@pytest.mark.parametrize(
    "events,match",
    [
        (
            [
                {
                    "kind": "job_terminal_unpersisted",
                    "payload": {
                        "job_id": "job-no-observation",
                        "exit_code": 1,
                        "attempted_receipt_id": "receipt-1",
                        "persistence_code": "transport_write_failed",
                        "attempt_count": 2,
                        "obligation_ref": "unpersisted",
                        "log_ref": "/tmp/job.log",
                    },
                }
            ],
            "prior terminal observation",
        ),
        (
            [
                _terminal_observed(job_id="job-legacy-terminal", exit_code=1),
                {
                    "kind": "job_unsettled",
                    "payload": {
                        "job_id": "job-legacy-terminal",
                        "evidence_ref": "obligation.json",
                        "obligation": {},
                    },
                },
            ],
            "cannot describe a terminal job",
        ),
        (
            [
                {
                    "kind": "job_unsettled",
                    "payload": {
                        "job_id": "job-legacy-first",
                        "evidence_ref": "obligation.json",
                        "obligation": {},
                    },
                },
                _terminal_observed(job_id="job-legacy-first", exit_code=1),
            ],
            "cannot precede terminal observation",
        ),
        (
            [
                {
                    "kind": "job_live_at_close",
                    "payload": {
                        "job_id": "job-contradiction",
                        "obligation_ref": "obligation.json",
                        "log_ref": "/tmp/job.log",
                        "close_reason": "deadline",
                    },
                },
                _terminal_observed(job_id="job-contradiction", exit_code=0),
            ],
            "both terminal and live at close",
        ),
        (
            [
                _terminal_observed(job_id="job-exit-mismatch", exit_code=0),
                {
                    "kind": "job_terminal_unpersisted",
                    "payload": {
                        "job_id": "job-exit-mismatch",
                        "exit_code": 137,
                        "attempted_receipt_id": "receipt-2",
                        "persistence_code": "transport_write_failed",
                        "attempt_count": 2,
                        "obligation_ref": "obligation.json",
                        "log_ref": "/tmp/job.log",
                    },
                },
            ],
            "exit code differs",
        ),
    ],
)
def test_replay_rejects_impossible_job_lifecycle(events, match, tmp_path):
    transcript = tmp_path / "impossible-job-lifecycle.jsonl"
    _write_replay_rows(
        transcript,
        _paramiko_rows_with_job_events(events),
    )

    with pytest.raises(ReplayValidationError, match=match):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_extra_historical_scheduler_rows_stay_inert(tmp_path):
    """Live transcripts interleaved a `scheduler_decision` before every
    envelope. Those rows are skipped now, so a transcript full of them must
    reach the same verdict as one without any."""
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [source[0]]
    for row in source[1:]:
        if row.get("kind") == "action_envelope":
            rows.append(
                {
                    "kind": "scheduler_decision",
                    "payload": {
                        "mode": "action",
                        "reasons": [],
                        "plan_index": row["payload"]["plan_index"],
                    },
                    "source": row["source"],
                }
            )
        rows.append(row)
    transcript = tmp_path / "live-shaped-paramiko.jsonl"
    _write_replay_rows(transcript, rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 6
    assert result.paired_envelope_count == 6
    assert result.skipped_event_kinds["scheduler_decision"] == 8
    assert result.snapshot.verdict == "partial"


def _forced_bigtop_rows():
    fixture = [
        json.loads(line)
        for line in (FIXTURES / "bigtop.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    header = fixture[0]
    header["schema_version"] = 2
    header["initial_state"] = {
        "start_phase": "test",
        "heartbeat_actions": 5,
        "available_tools": ["build", "search", "project"],
        "facts": [
            {
                "key": "build.test_entry_ready",
                "value": True,
                "evidence_ref": "artifact://test-ready",
                "scope": "artifacts",
            }
        ],
        "conflicts": [],
        "repair_global_remaining": 2,
        "repair_phase_remaining": {"test": 1, "build": 1},
    }
    source_excerpt = fixture[1]["source"]
    coordinate = "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-transaction-queue"
    exact_params = {
        "action": "test",
        "working_directory": coordinate,
    }
    resolution = {
        "status": "available",
        "workspace_root": "/workspace",
        "project_root": "/workspace/bigtop",
        "candidates": [{"root": coordinate, "system": "maven"}],
    }
    forced_payload = {
        "envelope_id": "forced-bigtop-test-1",
        "policy": "test_attempt_required",
        "trigger": "phase_floor",
        "phase": "test",
        "source_attempt_id": "test-1",
        "reason_code": "test_receipt_missing",
        "tool": "build",
        "exact_params": exact_params,
        "candidate_root": coordinate,
        "candidate_system": "maven",
        "parent_execution_id": None,
        "candidate_resolution": resolution,
    }
    forced_payload["action_sha256"] = forced_action_sha256(
        policy=forced_payload["policy"],
        trigger=forced_payload["trigger"],
        phase=forced_payload["phase"],
        source_attempt_id=forced_payload["source_attempt_id"],
        reason_code=forced_payload["reason_code"],
        tool=forced_payload["tool"],
        exact_params=forced_payload["exact_params"],
        candidate_root=forced_payload["candidate_root"],
        candidate_system=forced_payload["candidate_system"],
        parent_execution_id=None,
        candidate_resolution=resolution,
    )
    rows = [
        header,
        {
            "kind": "forced_action",
            "payload": forced_payload,
            "source": source_excerpt,
        },
        {
            "kind": "tool_result",
            "payload": {
                "envelope_id": forced_payload["envelope_id"],
                "execution_id": "forced-bigtop-execution-1",
                "tool": "build",
                "params": exact_params,
                "scope": "test_runtime",
                "roles": ["test"],
                "result": {
                    "invocation_status": "completed",
                    "operation_outcome": "failed",
                    "evidence_status": "verified",
                    "output": "Maven reached the test runner and failed.",
                    "output_ref": "output_forced_bigtop",
                    "evidence_assessment": "blocked",
                    "metadata": {
                        "command": "mvn test",
                        "runner_dispatched": True,
                        "exit_code": 1,
                    },
                    "evidence_refs": ["output_forced_bigtop"],
                    "conflicts": [],
                    "validator_findings": [],
                    "facts": {"system": "maven"},
                    "refs": ["output_forced_bigtop"],
                    "error": "test failure",
                    "error_code": "MAVEN_BUILD_FAILED",
                    "failure_signature": "maven:test:failed",
                    "error_tail_preview": "test failure",
                },
                "source_phase": "test",
                "source_attempt_id": "test-1",
            },
            "source": source_excerpt,
        },
        {
            "kind": "gate_decision",
            "payload": {
                "phase": "test",
                "signal": "done",
                "claimed_outcome": "failed",
                "validator_state": "red",
                "expected_accepted": True,
                "expected_outcome": "failed",
                "reason": "the runner reached a terminal test failure",
                "key_results": "test runner reached",
                "evidence_refs": ["output_forced_bigtop"],
                "validated_facts": {},
                "source_attempt_id": "test-1",
                "test_candidate_resolution": resolution,
            },
            "source": source_excerpt,
        },
        {
            "kind": "phase_transition",
            "payload": {
                "expected_kind": "evidence_close",
                "expected_target": None,
                "expected_reason_code": "test_terminal",
                "repair_request": None,
            },
            "source": source_excerpt,
        },
        {
            "kind": "evidence_close",
            "payload": {"reason": "test_terminated"},
            "source": source_excerpt,
        },
    ]
    outer_result = rows[2]["payload"]
    outer_result["actual_executions"] = [
        {
            "execution_id": "forced-bigtop-backend-execution-1",
            "tool": "maven",
            "params": {
                "command": "test",
                "working_directory": coordinate,
            },
            "scope": "test_runtime",
            "roles": ["test"],
            "result": json.loads(json.dumps(outer_result["result"])),
        }
    ]
    return rows


def test_engine_owned_forced_action_replays_without_scheduler_plan(tmp_path):
    source = _forced_bigtop_rows()
    transcript = tmp_path / "forced-action-bigtop.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 1
    assert result.unconsumed_events == ()


def test_forced_result_pairs_and_leaves_trailing_historical_rows_inert(tmp_path):
    source = _forced_bigtop_rows()
    result_index = next(
        index for index, row in enumerate(source) if row.get("kind") == "tool_result"
    )
    plan = {
        "steps": [
            {
                "tool": "project",
                "exact_params": {"action": "analyze"},
                "preconditions": [],
                "expected_evidence": ["fresh survey"],
                "success_criteria": ["survey persisted"],
            }
        ],
        "invalidate_on": ["failure", "conflict", "unknown", "phase_change"],
    }
    source[result_index + 1 : result_index + 1] = [
        {
            "kind": "scheduler_decision",
            "payload": {
                "mode": "think",
                "reasons": ["initial", "plan_exhausted"],
                "plan_index": None,
            },
            "source": source[1]["source"],
        },
        {
            "kind": "planner_response",
            "payload": {
                "plan_id": "after-forced-action",
                "plan": plan,
                "response_sha256": canonical_sha256(plan),
            },
            "source": source[1]["source"],
        },
    ]
    transcript = tmp_path / "forced-action-plan-invalidation.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    # The forced envelope is answered exactly once; the plan rows recorded after
    # it (a live scheduler invalidating its plan) are skipped, not re-executed.
    assert result.paired_envelope_count == result.executed_envelope_count == 1
    assert result.skipped_event_kinds == {
        "scheduler_decision": 1,
        "planner_response": 1,
    }
    assert result.unconsumed_events == ()


def test_forced_action_is_versioned_as_control_schema_v2(tmp_path):
    assert CONTROL_EVENT_SCHEMA_VERSION == 5
    source = _forced_bigtop_rows()
    source[0]["schema_version"] = 1
    transcript = tmp_path / "forced-action-v1.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="requires control-event schema version 2"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_rejects_unknown_control_schema_version(tmp_path):
    source = _forced_bigtop_rows()
    source[0]["schema_version"] = 4
    transcript = tmp_path / "unknown-control-schema.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize("mutation", ["missing_leaf", "wrong_root", "wrong_system"])
def test_forced_build_receipt_is_bound_to_the_backend_leaf(tmp_path, mutation):
    source = _forced_bigtop_rows()
    result = next(row for row in source if row.get("kind") == "tool_result")
    leaves = result["payload"]["actual_executions"]
    if mutation == "missing_leaf":
        result["payload"]["actual_executions"] = []
    elif mutation == "wrong_root":
        leaves[0]["params"]["working_directory"] = "/workspace/bigtop"
    else:
        leaves[0]["tool"] = "gradle"
        leaves[0]["params"] = {
            "tasks": "test",
            "working_directory": leaves[0]["params"]["working_directory"],
        }
    transcript = tmp_path / f"forced-backend-{mutation}.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_forced_preexecution_refusal_replays_as_control_not_test_evidence(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    marker = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "no_runner_dispatch",
        "reason_code": "MAVEN_PREFLIGHT_REJECTED",
    }
    conflict = (
        "forced_test_attempt_nonreceipt:test-1:"
        f"{forced['candidate_root']}:maven:no_runner_dispatch:"
        "MAVEN_PREFLIGHT_REJECTED"
    )
    for projection in [
        result["result"],
        result["actual_executions"][0]["result"],
    ]:
        projection["metadata"] = {
            "harness_forced_test_attempt": marker,
        }
        projection["conflicts"] = [conflict]
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "unknown",
            "validator_state": "unavailable",
            "expected_outcome": "unknown",
            "reason": "runner dispatch was deterministically refused",
        }
    )
    transcript = tmp_path / "forced-refusal.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 1
    assert conflict in result.snapshot.conflicts
    assert result.phase("test").outcome.value == "unknown"


def test_facade_only_forced_no_runner_receipt_replays_and_stays_non_green(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    result["actual_executions"] = []
    result["result"]["metadata"] = {
        "harness_forced_test_attempt": {
            "phase": "test",
            "source_attempt_id": "test-1",
            "root": forced["candidate_root"],
            "system": forced["candidate_system"],
            "actual_root": forced["candidate_root"],
            "actual_system": "maven",
            "disposition": "no_runner_dispatch",
            "reason_code": "MAVEN_PREFLIGHT_REJECTED",
        }
    }
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "unknown",
            "validator_state": "unavailable",
            "expected_outcome": "unknown",
            "reason": "runner dispatch was refused at the facade",
        }
    )
    transcript = tmp_path / "forced-facade-refusal.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.phase("test").outcome.value == "unknown"


def test_facade_only_forced_command_is_not_a_replayable_runner_receipt(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    result["actual_executions"] = []
    result["result"]["metadata"]["harness_forced_test_attempt"] = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "candidate_mismatch",
        "reason_code": "FORCED_TEST_CANDIDATE_MISMATCH",
    }
    transcript = tmp_path / "forced-facade-command.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="facade-only forced build"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_forced_preexecution_refusal_cannot_replay_as_a_green_gate(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    marker = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "no_runner_dispatch",
        "reason_code": "MAVEN_PREFLIGHT_REJECTED",
    }
    for projection in [
        result["result"],
        result["actual_executions"][0]["result"],
    ]:
        projection["metadata"] = {"harness_forced_test_attempt": marker}
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "success",
            "validator_state": "green",
            "expected_outcome": "success",
            "reason": "stale artifacts looked green",
        }
    )
    transcript = tmp_path / "forced-refusal-green.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="non-green gate"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize(
    "mutation",
    ["empty_result_attempt", "missing_gate_attempt", "missing_gate_resolution"],
)
def test_forced_action_replay_fails_closed_on_missing_lineage(tmp_path, mutation):
    source = _forced_bigtop_rows()
    forced = next(row for row in source if row.get("kind") == "forced_action")
    result = next(
        row
        for row in source
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == forced["payload"]["envelope_id"]
    )
    gate = next(
        row
        for row in source
        if row.get("kind") == "gate_decision"
        and row["payload"].get("phase") == "test"
        and row["payload"].get("source_attempt_id") == "test-1"
    )
    if mutation == "empty_result_attempt":
        result["payload"]["source_attempt_id"] = ""
    elif mutation == "missing_gate_attempt":
        gate["payload"].pop("source_attempt_id")
    else:
        gate["payload"].pop("test_candidate_resolution")
    transcript = tmp_path / f"forced-{mutation}.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _pending_forced_bigtop_rows():
    source = _forced_bigtop_rows()
    dispatch = source[2]
    dispatch["payload"]["result"] = {
        "invocation_status": "pending",
        "operation_outcome": "unknown",
        "evidence_status": "unknown",
        "output": "Maven test is running.",
        "evidence_assessment": "unknown",
        "poll_ref": "job:test-123",
        "metadata": {
            "command": "mvn test",
            "runner_dispatched": True,
            "dispatch_status": "running_detached",
            "job_id": "test-123",
        },
        "evidence_refs": [],
        "conflicts": [],
        "validator_findings": [],
        "facts": {"system": "maven"},
        "refs": ["job:test-123"],
    }
    dispatch["payload"]["actual_executions"][0]["result"] = json.loads(
        json.dumps(dispatch["payload"]["result"])
    )
    first = source[1]["payload"]
    poll_payload = {
        "envelope_id": "forced-bigtop-poll-1",
        "policy": "test_attempt_required",
        "trigger": "phase_floor",
        "phase": "test",
        "source_attempt_id": "test-1",
        "reason_code": "pending_test_poll_required",
        "tool": "search",
        "exact_params": {"target": "job:test-123"},
        "candidate_root": first["candidate_root"],
        "candidate_system": first["candidate_system"],
        "parent_execution_id": dispatch["payload"]["actual_executions"][0]["execution_id"],
        "candidate_resolution": first["candidate_resolution"],
    }
    poll_payload["action_sha256"] = forced_action_sha256(
        policy=poll_payload["policy"],
        trigger=poll_payload["trigger"],
        phase=poll_payload["phase"],
        source_attempt_id=poll_payload["source_attempt_id"],
        reason_code=poll_payload["reason_code"],
        tool=poll_payload["tool"],
        exact_params=poll_payload["exact_params"],
        candidate_root=poll_payload["candidate_root"],
        candidate_system=poll_payload["candidate_system"],
        parent_execution_id=poll_payload["parent_execution_id"],
        candidate_resolution=poll_payload["candidate_resolution"],
    )
    poll_events = [
        {
            "kind": "forced_action",
            "payload": poll_payload,
            "source": source[1]["source"],
        },
        {
            "kind": "tool_result",
            "payload": {
                "envelope_id": poll_payload["envelope_id"],
                "execution_id": "forced-bigtop-poll-execution-1",
                "tool": "search",
                "params": {"target": "job:test-123"},
                "scope": "test_runtime",
                "roles": [],
                "result": {
                    "invocation_status": "completed",
                    "operation_outcome": "success",
                    "evidence_status": "unknown",
                    "output": "Detached Maven test completed.",
                    "evidence_assessment": "unknown",
                    "poll_ref": "job:test-123",
                    "metadata": {
                        "dispatch_status": "completed_detached",
                        "job_id": "test-123",
                    },
                    "evidence_refs": [],
                    "conflicts": [],
                    "validator_findings": [],
                    "facts": {},
                    "refs": ["job:test-123"],
                },
                "source_phase": "test",
                "source_attempt_id": "test-1",
            },
            "source": source[1]["source"],
        },
    ]
    source[3:3] = poll_events
    return source


def test_forced_poll_replays_with_parent_dispatch_lineage(tmp_path):
    source = _pending_forced_bigtop_rows()
    transcript = tmp_path / "forced-poll.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 2


def test_forced_action_rejects_stale_pending_parent_lineage(tmp_path):
    source = _pending_forced_bigtop_rows()
    forced = [row for row in source if row.get("kind") == "forced_action"][1]
    forced["payload"]["parent_execution_id"] = "not-the-dispatch"
    forced["payload"]["action_sha256"] = forced_action_sha256(
        policy=forced["payload"]["policy"],
        trigger=forced["payload"]["trigger"],
        phase=forced["payload"]["phase"],
        source_attempt_id=forced["payload"]["source_attempt_id"],
        reason_code=forced["payload"]["reason_code"],
        tool=forced["payload"]["tool"],
        exact_params=forced["payload"]["exact_params"],
        candidate_root=forced["payload"]["candidate_root"],
        candidate_system=forced["payload"]["candidate_system"],
        parent_execution_id=forced["payload"]["parent_execution_id"],
        candidate_resolution=forced["payload"]["candidate_resolution"],
    )
    transcript = tmp_path / "forced-wrong-parent.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_rejects_candidate_snapshot_outside_recorded_project_root(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    forced["candidate_resolution"]["candidates"][0]["root"] = "/workspace/other/tests"
    forced["action_sha256"] = forced_action_sha256(
        policy=forced["policy"],
        trigger=forced["trigger"],
        phase=forced["phase"],
        source_attempt_id=forced["source_attempt_id"],
        reason_code=forced["reason_code"],
        tool=forced["tool"],
        exact_params=forced["exact_params"],
        candidate_root=forced["candidate_root"],
        candidate_system=forced["candidate_system"],
        parent_execution_id=forced["parent_execution_id"],
        candidate_resolution=forced["candidate_resolution"],
    )
    transcript = tmp_path / "forced-candidate-snapshot-escape.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_session_logger_control_sink_appends_host_and_mirror(tmp_path):
    mirrored = []
    session_logger = object.__new__(SessionLogger)
    session_logger.session_log_dir = tmp_path
    session_logger._control_event_sink = None

    sink = session_logger.get_control_event_sink(
        mirror=mirrored.append,
        clock=lambda: "2026-07-17T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )
    sink.emit("scheduler_decision", {"mode": "think", "reasons": ["initial"]})

    event = ControlEvent.model_validate_json(
        (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    )
    assert event.sequence == 1
    assert event.event_id == "live-1"
    assert mirrored == [(tmp_path / "control_events.jsonl").read_text(encoding="utf-8")]


# Plan 2 Task 8: old protocol removed — the live envelope is keyed by the
# native tool_call id, not by a scheduler plan index.
def test_live_engine_emits_native_envelope_and_redacted_result(tmp_path):
    from sag.agent.invocation_contracts import clear_action_context

    clear_action_context()
    sink = ControlEventSink(
        tmp_path / "control_events.jsonl",
        clock=lambda: "2026-07-17T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )
    params = {"action": "build", "working_directory": "/workspace/demo"}
    engine = object.__new__(ReActEngine)
    engine.control_event_sink = sink
    engine.phase_machine = None
    engine.steps = []
    engine._active_native_tool_call_id = "call_build_1"

    envelope_id = engine._emit_control_action_envelope("build", params)
    result = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.SUCCESS,
        evidence_status=EvidenceStatus.VERIFIED,
        output_ref="output_live_build",
        output="secret build output " * 100,
        raw_output="never serialize this full body",
        facts={"compiled_classes": 41},
        refs=["output_live_build"],
        evidence_refs=["output_live_build"],
    )
    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-live-1",
        tool="build",
        params=params,
        result=result,
    )

    text = (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    events = [ControlEvent.model_validate_json(line) for line in text.splitlines()]
    assert [event.kind for event in events] == ["action_envelope", "tool_result"]
    assert events[0].payload["tool_call_id"] == "call_build_1"
    assert "plan_index" not in events[0].payload
    assert events[-1].payload["envelope_id"] == envelope_id
    assert events[-1].payload["result"]["output"] == "stored as output_live_build"
    assert events[-1].payload["result"]["facts"] == {"compiled_classes": 41}
    assert "secret build output" not in text
    assert "never serialize" not in text
    clear_action_context()


def test_sanitized_config_excludes_secrets_and_api_endpoints():
    sanitized = sanitize_config(
        {
            "thinking_model": "gpt-5",
            "openai_api_key": "secret",
            "openai_base_url": "https://secret.example/v1",
            "nested": {"token": "secret", "safe": 3},
        }
    )

    assert sanitized == {"nested": {"safe": 3}, "thinking_model": "gpt-5"}


class _PinEvidenceStore:
    def evidence_store_identity(self):
        return "docker:run-pin-test-store"

    def execute_command(self, _command, **_kwargs):
        return {"success": True, "exit_code": 0, "output": ""}


def test_setup_agent_updates_complete_run_pin_after_clone(tmp_path):
    mirrored = []
    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent._run_pin_mirror = mirrored.append
    agent.orchestrator = _PinEvidenceStore()
    agent.run_id = "run-pytest"
    agent._run_pin_template = {
        "run_id": "run-pytest",
        "container_image_digest": "sha256:" + "b" * 64,
        "sag_git_sha": "c" * 40,
        "thinking_model": "thinking-model",
        "action_model": "action-model",
        "sanitized_config": {"max_iterations": 50},
        "prompt_bundle_sha256": "d" * 64,
        "feature_flags": {"control_events": True},
        "random_seed_or_null": None,
        # The runner assigns a real run-order index; it must reach the pin.
        "run_order_index": 5,
        "dependency_cache_state": "warm",
        "host_arch": "arm64",
    }
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    agent._record_target_repo_sha("a" * 40)

    pin = RunPin.model_validate_json((tmp_path / "run-pin.json").read_text(encoding="utf-8"))
    assert pin.target_repo_sha == "a" * 40
    assert pin.run_order_index == 5
    assert mirrored == [(tmp_path / "run-pin.json").read_text(encoding="utf-8")]


def _pin_template_agent(tmp_path):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent._run_pin_mirror = None
    agent.orchestrator = _PinEvidenceStore()
    agent.run_id = "run-pytest"
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "sys"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_a, **_k: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64
    return agent


def test_run_pin_is_written_at_startup_even_without_a_target_sha(tmp_path):
    """Item-3 regression: the pin write previously fired ONLY inside
    _record_target_repo_sha, so a run that never observed a target SHA left NO
    pin file at all (real 2026-07-19 S2-00000-r3). The template init now writes
    the pin UNCONDITIONALLY with a null target SHA."""
    agent = _pin_template_agent(tmp_path)

    agent._initialize_run_pin_template()

    pin_path = tmp_path / "run-pin.json"
    assert pin_path.is_file(), "run pin must exist after startup even with no target SHA"
    pin = RunPin.model_validate_json(pin_path.read_text(encoding="utf-8"))
    assert pin.target_repo_sha is None
    # The rest of the provenance is already complete at startup.
    assert pin.sag_git_sha == "a" * 40
    assert pin.container_image_digest == "sha256:" + "b" * 64


def test_observed_target_sha_rewrites_the_startup_pin(tmp_path):
    """The startup pin's null SHA is replaced the moment a real one is
    observed — the collector's current-run validation requires the real SHA."""
    agent = _pin_template_agent(tmp_path)
    agent._initialize_run_pin_template()
    pin_path = tmp_path / "run-pin.json"
    assert RunPin.model_validate_json(pin_path.read_text(encoding="utf-8")).target_repo_sha is None

    agent._record_target_repo_sha("f" * 40)

    assert (
        RunPin.model_validate_json(pin_path.read_text(encoding="utf-8")).target_repo_sha == "f" * 40
    )


def test_run_pin_hashes_the_complete_prompt_bundle(tmp_path):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "x" * 600 + "a"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64

    agent._initialize_run_pin_template()
    first = agent._run_pin_template["prompt_bundle_sha256"]
    agent.react_engine.prompts = PromptConfig({"system": "x" * 600 + "b"})
    agent._initialize_run_pin_template()

    assert agent._run_pin_template["prompt_bundle_sha256"] != first


def test_run_pin_template_reads_run_order_index_from_env(tmp_path, monkeypatch):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "sys"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_a, **_k: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64

    # The runner injects SAG_RUN_ORDER_INDEX; the template must carry it.
    monkeypatch.setenv("SAG_RUN_ORDER_INDEX", "11")
    agent._initialize_run_pin_template()
    assert agent._run_pin_template["run_order_index"] == 11

    # Absent (ad-hoc run) -> None, never a crash.
    monkeypatch.delenv("SAG_RUN_ORDER_INDEX", raising=False)
    agent._initialize_run_pin_template()
    assert agent._run_pin_template["run_order_index"] is None


def test_the_pin_mirror_writes_the_exact_published_bytes():
    """Live lp-commons-dbcp (2026-08-09): the run-pin mirror rode
    write_container_text, whose heredoc always appends a trailing newline —
    one byte the host publication never hashed. The strict byte-exact reader
    then reported run_pin_unreadable for the entire run and every receipt
    binding collapsed. The mirror must produce bytes whose sha256 equals the
    published raw hash."""

    import hashlib
    import json

    from test_container_io import FakeContainer

    from sag.agent.agent import SetupAgent

    class Orch:
        def __init__(self):
            self._fs = FakeContainer()

        def execute_control_command(self, command, **kwargs):
            return self._fs.execute_command(command)

        def execute_command(self, command, **kwargs):
            return self._fs.execute_command(command)

    orch = Orch()
    payload = json.dumps({"run_id": "run-1", "target_repo_sha": None}, sort_keys=True)

    agent = SetupAgent.__new__(SetupAgent)
    agent.orchestrator = orch
    mirror = SetupAgent._make_run_pin_mirror(agent)
    mirror(payload)

    stored = orch._fs.files["/workspace/.setup_agent/run-pin.json"]
    assert (
        hashlib.sha256(stored.encode("utf-8")).hexdigest()
        == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    ), "mirror bytes must hash exactly like the published payload"


def test_v4_expectation_projects_only_additive_ci_field_and_still_detects_drift(tmp_path):
    frozen = FIXTURES / "paramiko.jsonl"
    original_bytes = frozen.read_bytes()
    rows = [json.loads(line) for line in original_bytes.decode().splitlines()]
    current = ControlReplayRunner.offline(verify_expected=False).run(frozen)
    historical = current.snapshot.model_dump(mode="json")
    historical["schema_version"] = 4
    historical.pop("ci_comparison")
    rows[0]["expected_snapshot"] = historical
    transcript = tmp_path / "v4-expectation.jsonl"
    _write_replay_rows(transcript, rows)
    result = ControlReplayRunner.offline().run(transcript)
    assert result.snapshot.schema_version == 5
    assert result.expected_snapshot == historical
    assert result.snapshot.rates == historical["rates"]
    assert result.snapshot.verdict == historical["verdict"]
    assert result.snapshot.ci_comparison.certificate is None
    rows[0]["expected_snapshot"]["verdict"] = "success"
    _write_replay_rows(transcript, rows)
    with pytest.raises(ReplayMismatchError, match="snapshot"):
        ControlReplayRunner.offline().run(transcript)
    assert frozen.read_bytes() == original_bytes
