import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent
from sag.agent.control_events import (
    CONTROL_EVENT_MAX_RAW_BYTES,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    ControlEvent,
    ControlEventSink,
    EvidencePublicationPayload,
    canonical_json,
)
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    EvidencePublicationConflict,
    EvidencePublicationRecoveryError,
    EvidencePublicationUnavailableError,
    MutablePublicationObservation,
    current_evidence_publication_authority,
    content_addressed_record_id,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    publish_evidence_bytes,
    publish_evidence_revision,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
    verify_evidence_bytes,
    revoke_evidence_artifact,
)

RUN_ID = "run-foundation-1"
RECORD_ID = "inv-maven-test-0001"
RAW = b'{"receipt_id":"inv-maven-test-0001","schema_version":2}'


@pytest.fixture(autouse=True)
def _isolate_current_authority():
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("isolated test context")
    )
    try:
        yield
    finally:
        reset_evidence_publication_authority(token)


def _publication_payload(raw=RAW, **overrides):
    payload = {
        "record_kind": "invocation_receipt",
        "record_id": RECORD_ID,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
        "run_id": RUN_ID,
    }
    payload.update(overrides)
    return payload


def _event_line(sequence, payload, *, kind="evidence_publication"):
    return (
        canonical_json(
            {
                "sequence": sequence,
                "kind": kind,
                "payload": payload,
                "timestamp": "2026-08-09T00:00:00Z",
                "event_id": f"control-{sequence:06d}",
            }
        )
        + "\n"
    )


def _store_binding_payload(*, run_id=RUN_ID, identity="docker:test-evidence-store"):
    return {"run_id": run_id, "store_identity": identity}


class _StableEvidenceStore:
    def __init__(self, identity: str = "docker:test-evidence-store"):
        self.identity = identity

    def evidence_store_identity(self):
        return self.identity

    def execute_command(self, _command):
        return {}


def _live_authority(*, run_id, sink, store_identity="docker:test-evidence-store"):
    authority = EvidencePublicationAuthority.for_live_run(run_id=run_id, sink=sink)
    authority.bind_store(_StableEvidenceStore(store_identity))
    return authority


def test_publication_event_has_closed_strict_schema():
    payload = EvidencePublicationPayload.model_validate(_publication_payload())
    event = ControlEvent(sequence=1, kind="evidence_publication", payload=payload.model_dump())

    assert event.typed_payload == payload
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate(
            _publication_payload(record_kind="arbitrary_file")
        )
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate(_publication_payload(byte_count="57"))
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate(_publication_payload(extra="forged"))
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate(
            _publication_payload(contract_id="ic-aaaaaaaaaaaa")
        )
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate(
            _publication_payload(contract_id=None, contract_hash=None)
        )


def test_invocation_contract_publication_binds_its_own_id_and_hash():
    digest = "a" * 64
    valid = _publication_payload(
        record_kind="invocation_contract",
        record_id="ic-aaaaaaaaaaaa",
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash=digest,
    )

    assert EvidencePublicationPayload.model_validate(valid).contract_hash == digest
    with pytest.raises(ValidationError):
        EvidencePublicationPayload.model_validate({**valid, "contract_id": "ic-bbbbbbbbbbbb"})


def test_mutable_publication_requires_one_stable_complete_revision_tuple():
    base = _publication_payload(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        revision=1,
        previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        previous_publication_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    assert EvidencePublicationPayload.model_validate(base).revision == 1
    with pytest.raises(ValidationError, match="complete revision tuple"):
        EvidencePublicationPayload.model_validate(
            {key: value for key, value in base.items() if key != "revision"}
        )
    with pytest.raises(ValidationError, match="record_id must equal"):
        EvidencePublicationPayload.model_validate({**base, "record_id": "old-snapshot-id"})


def test_host_publish_is_idempotent_and_identity_collision_fails_closed(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = _live_authority(run_id=RUN_ID, sink=sink)

    first = authority.publish_bytes(record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW)
    second = authority.publish_bytes(record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW)

    assert first == second
    assert sink.sequence == 2
    assert authority.verify_bytes(
        record_kind="invocation_receipt",
        record_id=RECORD_ID,
        raw=RAW,
    ).authorized
    with pytest.raises(EvidencePublicationConflict):
        authority.publish_bytes(
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW + b" ",
        )
    assert sink.sequence == 2


def test_exact_bytes_contract_link_and_run_are_all_verified(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = _live_authority(run_id=RUN_ID, sink=sink)
    authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id=RECORD_ID,
        raw=RAW,
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash="b" * 64,
    )

    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW + b" ",
            contract_id="ic-aaaaaaaaaaaa",
            contract_hash="b" * 64,
        ).status
        == "mismatch"
    )
    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW,
        ).status
        == "mismatch"
    )
    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW,
            run_id="run-foreign",
            contract_id="ic-aaaaaaaaaaaa",
            contract_hash="b" * 64,
        ).status
        == "foreign_run"
    )
    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW,
            contract_id="ic-aaaaaaaaaaaa",
            contract_hash="b" * 64,
        ).status
        == "verified"
    )


def test_forged_container_mirror_row_is_not_publication_authority(tmp_path):
    host_path = tmp_path / "host" / "control_events.jsonl"
    mirror_path = tmp_path / "container-mirror" / "control_events.jsonl"
    mirror_path.parent.mkdir(parents=True)
    mirror_path.write_text(_event_line(1, _publication_payload()), encoding="utf-8")

    authority = EvidencePublicationAuthority.recover_from_host_jsonl(host_path, run_id=RUN_ID)

    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW
        ).status
        == "unavailable"
    )


def test_restart_recovers_only_matching_run_from_host_stream(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    live = _live_authority(run_id=RUN_ID, sink=sink)
    live.publish_bytes(record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW)

    restarted = EvidencePublicationAuthority.recover_from_host_jsonl(sink.path, run_id=RUN_ID)
    foreign = EvidencePublicationAuthority.recover_from_host_jsonl(sink.path, run_id="run-other")

    assert restarted.writable is False
    assert (
        restarted.verify_bytes(
            record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW
        ).status
        == "verified"
    )
    assert (
        foreign.verify_bytes(record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW).status
        == "unavailable"
    )
    with pytest.raises(EvidencePublicationUnavailableError):
        restarted.publish_bytes(record_kind="invocation_receipt", record_id="inv-new", raw=RAW)


def test_restart_preserves_the_immutable_container_store_binding(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    live = _live_authority(
        run_id=RUN_ID,
        sink=sink,
        store_identity="docker:container-A",
    )
    live.publish_bytes(record_kind="policy_claim", record_id="claim-a", raw=RAW)

    restarted = EvidencePublicationAuthority.recover_from_host_jsonl(sink.path, run_id=RUN_ID)
    with pytest.raises(EvidencePublicationConflict, match="more than one container store"):
        install_evidence_publication_authority(
            restarted,
            orchestrator=_StableEvidenceStore("docker:container-B"),
        )

    install_evidence_publication_authority(
        restarted,
        orchestrator=_StableEvidenceStore("docker:container-A"),
    )
    assert restarted.verify_bytes(
        record_kind="policy_claim",
        record_id="claim-a",
        raw=RAW,
    ).authorized


def test_emit_failure_never_admits_record(tmp_path):
    class FailingSink:
        path = tmp_path / "control_events.jsonl"

        def __init__(self):
            self.calls = 0

        def emit(self, kind, *_args, **_kwargs):
            self.calls += 1
            if kind != "evidence_store_bound":
                raise OSError("disk full")

    authority = EvidencePublicationAuthority(run_id=RUN_ID, sink=FailingSink())
    authority.bind_store(_StableEvidenceStore())

    with pytest.raises(EvidencePublicationUnavailableError):
        authority.publish_bytes(record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW)
    assert authority.publication_count == 0
    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW
        ).status
        == "not_published"
    )


def test_mutable_emit_failure_never_advances_the_head(tmp_path):
    class FailingSink:
        path = tmp_path / "control_events.jsonl"

        def __init__(self):
            self.calls = 0

        def emit(self, kind, *_args, **_kwargs):
            self.calls += 1
            if kind != "evidence_store_bound":
                raise OSError("disk full")

    authority = EvidencePublicationAuthority(run_id=RUN_ID, sink=FailingSink())
    authority.bind_store(_StableEvidenceStore())

    with pytest.raises(EvidencePublicationUnavailableError):
        authority.publish_revision(
            record_kind="run_pin",
            record_id="host-run-pin",
            logical_artifact_id="host-run-pin",
            raw=RAW,
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
    assert authority.latest_head("host-run-pin") is None


@pytest.mark.parametrize("failure", ["sequence", "future", "duplicate_key", "partial"])
def test_recovery_rejects_whole_stream_on_any_bad_event(tmp_path, failure):
    path = tmp_path / "control_events.jsonl"
    first = _event_line(1, _store_binding_payload(), kind="evidence_store_bound")
    publication = _event_line(2, _publication_payload())
    if failure == "sequence":
        second = _event_line(4, _publication_payload(record_id="inv-second"))
    elif failure == "future":
        second = _event_line(3, {}, kind="evidence_publication_v2")
    elif failure == "duplicate_key":
        second = (
            '{"sequence":3,"sequence":3,"kind":"scheduler_decision",'
            '"payload":{"mode":"think","reasons":[]}}\n'
        )
    else:
        second = '{"sequence":3,"kind":"scheduler_decision"}'
    path.write_text(first + publication + second, encoding="utf-8")

    with pytest.raises(EvidencePublicationRecoveryError):
        EvidencePublicationAuthority.recover_from_host_jsonl(path, run_id=RUN_ID)


def test_recovery_rejects_oversized_event_and_publication_collision(tmp_path):
    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b"{" + b"x" * (CONTROL_EVENT_MAX_RAW_BYTES + 1) + b"\n")
    with pytest.raises(EvidencePublicationRecoveryError):
        EvidencePublicationAuthority.recover_from_host_jsonl(oversized, run_id=RUN_ID)

    collision = tmp_path / "collision.jsonl"
    collision.write_text(
        _event_line(1, _store_binding_payload(), kind="evidence_store_bound")
        + _event_line(2, _publication_payload())
        + _event_line(3, _publication_payload(raw_sha256="c" * 64)),
        encoding="utf-8",
    )
    with pytest.raises(EvidencePublicationRecoveryError):
        EvidencePublicationAuthority.recover_from_host_jsonl(collision, run_id=RUN_ID)


def test_no_installed_authority_is_explicitly_unavailable():
    orchestrator = SimpleNamespace()

    authority = current_evidence_publication_authority(orchestrator)

    assert authority.available is False
    assert (
        authority.verify_bytes(
            record_kind="invocation_receipt", record_id=RECORD_ID, raw=RAW
        ).status
        == "unavailable"
    )


def test_authority_helper_resolves_an_orchestrator_directly(tmp_path):
    class Orchestrator:
        def execute_command(self, _command):
            return {}

    orchestrator = Orchestrator()
    authority = EvidencePublicationAuthority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )
    install_evidence_publication_authority(authority, orchestrator=orchestrator)

    assert evidence_publication_authority_for(orchestrator) is authority


def test_authority_helper_resolves_a_bound_execute_callable(tmp_path):
    class Orchestrator:
        def execute_command(self, _command):
            return {}

    orchestrator = Orchestrator()
    authority = EvidencePublicationAuthority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )
    install_evidence_publication_authority(authority, orchestrator=orchestrator)

    assert evidence_publication_authority_for(orchestrator.execute_command) is authority


@pytest.mark.parametrize("source_kind", ["orchestrator", "bound_execute"])
@pytest.mark.parametrize(
    "unavailable_identity",
    ["raises", pytest.param(None, id="none")],
)
def test_contextual_authority_fails_closed_when_bound_store_identity_is_unavailable(
    tmp_path,
    source_kind,
    unavailable_identity,
):
    store_a = "docker:context-store-a"
    store_b = "docker:context-store-b"
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
        store_identity=store_a,
    )
    authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id=RECORD_ID,
        raw=RAW,
    )
    install_evidence_publication_authority(authority)

    class Orchestrator:
        identity = unavailable_identity

        def evidence_store_identity(self):
            if self.identity == "raises":
                raise RuntimeError("container identity is temporarily unavailable")
            return self.identity

        def execute_command(self, _command):
            return {}

    orchestrator = Orchestrator()
    source = orchestrator if source_kind == "orchestrator" else orchestrator.execute_command

    with pytest.raises(EvidencePublicationConflict, match="no immutable container identity"):
        evidence_publication_authority_for(source)
    with pytest.raises(EvidencePublicationConflict, match="no immutable container identity"):
        publish_evidence_bytes(
            source,
            record_kind="invocation_receipt",
            record_id="inv-contextual-bypass",
            raw=RAW,
        )
    with pytest.raises(EvidencePublicationConflict, match="no immutable container identity"):
        verify_evidence_bytes(
            source,
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW,
        )
    assert authority.publication_count == 1

    orchestrator.identity = store_b
    with pytest.raises(EvidencePublicationConflict, match="different container store"):
        evidence_publication_authority_for(source)

    orchestrator.identity = store_a
    assert evidence_publication_authority_for(source) is authority
    assert verify_evidence_bytes(
        source,
        record_kind="invocation_receipt",
        record_id=RECORD_ID,
        raw=RAW,
    ).authorized


def test_contextual_authority_defers_initial_binding_until_store_identity_exists(tmp_path):
    class Orchestrator:
        identity = None

        def evidence_store_identity(self):
            return self.identity

        def execute_command(self, _command):
            return {}

    authority = EvidencePublicationAuthority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )
    install_evidence_publication_authority(authority)
    orchestrator = Orchestrator()

    assert evidence_publication_authority_for(orchestrator) is authority
    assert (
        publish_evidence_bytes(
            orchestrator,
            record_kind="invocation_receipt",
            record_id=RECORD_ID,
            raw=RAW,
        ).status
        == "publication_unavailable"
    )

    orchestrator.identity = "docker:context-store-a"
    assert evidence_publication_authority_for(orchestrator) is authority
    assert publish_evidence_bytes(
        orchestrator,
        record_kind="invocation_receipt",
        record_id=RECORD_ID,
        raw=RAW,
    ).published


def test_one_evidence_epoch_cannot_bind_two_container_stores(tmp_path):
    class Orchestrator:
        def __init__(self, name):
            self.container_name = name

        def execute_command(self, _command):
            return {}

    authority = EvidencePublicationAuthority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )
    first = Orchestrator("sag-d0-control")
    second = Orchestrator("sag-d0-treatment")
    install_evidence_publication_authority(authority, orchestrator=first)

    with pytest.raises(EvidencePublicationConflict, match="more than one container store"):
        install_evidence_publication_authority(authority, orchestrator=second)

    # D0 arms therefore need distinct run ids and distinct control streams;
    # re-resolving the same bound store remains idempotent.
    install_evidence_publication_authority(authority, orchestrator=first)


def test_authority_waits_for_immutable_container_id_before_first_binding(tmp_path):
    class Orchestrator:
        def __init__(self):
            self.container_id = None

        def evidence_store_identity(self):
            if self.container_id is None:
                raise RuntimeError("container not created")
            return f"docker:{self.container_id}"

        def execute_command(self, _command):
            return {}

    orchestrator = Orchestrator()
    authority = EvidencePublicationAuthority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control-events.jsonl"),
    )

    install_evidence_publication_authority(authority, orchestrator=orchestrator)
    with pytest.raises(EvidencePublicationConflict, match="no immutable container identity"):
        evidence_publication_authority_for(orchestrator)

    orchestrator.container_id = "immutable-container-id"

    assert evidence_publication_authority_for(orchestrator) is authority
    assert evidence_publication_authority_for(orchestrator) is authority


def test_authority_helper_does_not_treat_a_plain_fake_as_orchestrator():
    contextual = EvidencePublicationAuthority(run_id=RUN_ID)
    install_evidence_publication_authority(contextual)

    def fake_execute(_command):
        return {}

    assert evidence_publication_authority_for(fake_execute) is contextual


def test_nonraising_publish_helper_commits_exact_bytes_and_content_identity(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = _live_authority(run_id=RUN_ID, sink=sink)
    install_evidence_publication_authority(authority)
    record_id = content_addressed_record_id("invocation-receipt", RAW)

    result = publish_evidence_bytes(
        None,
        record_kind="invocation_receipt",
        record_id=record_id,
        raw=RAW,
    )

    assert result.status == "published"
    assert record_id == f"invocation-receipt-{hashlib.sha256(RAW).hexdigest()[:24]}"
    assert authority.verify_bytes(
        record_kind="invocation_receipt",
        record_id=record_id,
        raw=RAW,
    ).authorized


def test_mutable_revision_chain_rejects_rollback_and_stale_writer(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = _live_authority(run_id=RUN_ID, sink=sink)
    first_raw = b'{"version":1}'
    second_raw = b'{"version":2}'
    first = authority.publish_revision(
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        raw=first_raw,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    second = authority.publish_revision(
        record_kind="receipt_structure",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        raw=second_raw,
        expected_previous_raw_sha256=first.raw_sha256,
    )

    assert second.revision == 2
    assert second.previous_raw_sha256 == first.raw_sha256
    assert (
        authority.verify_latest_bytes(
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=first_raw,
        ).status
        == "mismatch"
    )
    assert (
        authority.verify_latest_bytes(
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=second_raw,
        ).status
        == "verified"
    )
    with pytest.raises(EvidencePublicationConflict, match="expected host-head"):
        authority.publish_revision(
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=b'{"version":3}',
            expected_previous_raw_sha256=first.raw_sha256,
        )


def test_same_mutable_bytes_cannot_change_contract_linkage(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    contract_id = "ic-aaaaaaaaaaaa"
    first = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-contract-a",
        logical_artifact_id="job-contract-a",
        raw=RAW,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        contract_id=contract_id,
        contract_hash="a" * 64,
    )

    with pytest.raises(EvidencePublicationConflict, match="different identity or linkage"):
        authority.publish_revision(
            record_kind="job_obligation",
            record_id="job-contract-a",
            logical_artifact_id="job-contract-a",
            raw=RAW,
            expected_previous_raw_sha256=first.raw_sha256,
            contract_id=contract_id,
            contract_hash="b" * 64,
        )
    assert (
        authority.verify_latest_bytes(
            record_kind="job_obligation",
            record_id="job-contract-a",
            logical_artifact_id="job-contract-a",
            raw=RAW,
            contract_id=contract_id,
            contract_hash="a" * 64,
        ).status
        == "verified"
    )
    assert (
        authority.verify_latest_bytes(
            record_kind="job_obligation",
            record_id="job-contract-a",
            logical_artifact_id="job-contract-a",
            raw=RAW,
            contract_id=contract_id,
            contract_hash="b" * 64,
        ).status
        == "mismatch"
    )


def test_mutable_head_recovers_and_tombstone_revokes_all_old_bytes(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    live = _live_authority(run_id=RUN_ID, sink=sink)
    published = live.publish_revision(
        record_kind="report_metrics",
        record_id="report-metrics",
        logical_artifact_id="report-metrics",
        raw=RAW,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    tombstone = live.revoke_latest(
        record_kind="report_metrics",
        record_id="report-metrics",
        logical_artifact_id="report-metrics",
        expected_previous_raw_sha256=published.raw_sha256,
    )
    restarted = EvidencePublicationAuthority.recover_from_host_jsonl(sink.path, run_id=RUN_ID)

    assert tombstone.revision == 2
    assert restarted.latest_head("report-metrics") == tombstone
    assert (
        restarted.verify_latest_bytes(
            record_kind="report_metrics",
            record_id="report-metrics",
            logical_artifact_id="report-metrics",
            raw=RAW,
        ).status
        == "revoked"
    )

    assert (
        live.revoke_latest(
            record_kind="report_metrics",
            record_id="report-metrics",
            logical_artifact_id="report-metrics",
            expected_previous_raw_sha256=published.raw_sha256,
        )
        == tombstone
    )
    with pytest.raises(EvidencePublicationConflict, match="identity or predecessor"):
        live.revoke_latest(
            record_kind="run_pin",
            record_id="report-metrics",
            logical_artifact_id="report-metrics",
            expected_previous_raw_sha256=tombstone.raw_sha256,
        )


def test_recovery_rejects_a_forged_mutable_predecessor(tmp_path):
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    live = _live_authority(run_id=RUN_ID, sink=sink)
    first = live.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=b'{"pin":1}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    live.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=b'{"pin":2}',
        expected_previous_raw_sha256=first.raw_sha256,
    )
    rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
    rows[2]["payload"]["previous_raw_sha256"] = "f" * 64
    sink.path.write_text("\n".join(canonical_json(row) for row in rows) + "\n")

    with pytest.raises(EvidencePublicationRecoveryError, match="discontinuous"):
        EvidencePublicationAuthority.recover_from_host_jsonl(sink.path, run_id=RUN_ID)


def test_expected_set_exposes_deleted_and_duplicate_immutable_records(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.publish_bytes(record_kind="invocation_receipt", record_id="receipt-failed", raw=RAW)
    authority.publish_bytes(
        record_kind="invocation_receipt", record_id="receipt-green", raw=RAW + b" "
    )

    assert authority.expected_immutable_record_ids("invocation_receipt") == {
        "receipt-failed",
        "receipt-green",
    }
    assert (
        authority.verify_immutable_record_id_set("invocation_receipt", ["receipt-green"]).status
        == "mismatch"
    )
    assert (
        authority.verify_immutable_record_id_set(
            "invocation_receipt", ["receipt-failed", "receipt-green", "receipt-green"]
        ).status
        == "mismatch"
    )
    assert (
        authority.verify_immutable_record_id_set(
            "invocation_receipt", ["receipt-failed", "receipt-green"]
        ).status
        == "verified"
    )


def test_mutable_expected_set_detects_deleted_job_and_honors_tombstone(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    first = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=b'{"job_id":"job-a","state":"running"}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-b",
        logical_artifact_id="job-b",
        raw=b'{"job_id":"job-b","state":"running"}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    assert (
        authority.verify_mutable_record_id_set("job_obligation", {"job-b": "job-b"}).status
        == "mismatch"
    )
    assert (
        authority.verify_mutable_record_id_set(
            "job_obligation", {"job-a": "job-a", "job-b": "job-b"}
        ).status
        == "verified"
    )

    authority.revoke_latest(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        expected_previous_raw_sha256=first.raw_sha256,
    )
    assert authority.expected_mutable_record_ids("job_obligation") == {"job-b": "job-b"}
    assert (
        authority.verify_mutable_record_id_set("job_obligation", {"job-b": "job-b"}).status
        == "verified"
    )
    assert (
        authority.verify_mutable_record_id_set(
            "job_obligation", {"job-a": "job-a", "job-b": "job-b"}
        ).status
        == "mismatch"
    )


def test_mutable_batch_verification_is_one_complete_current_host_snapshot(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    a1 = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=b'{"job_id":"job-a","state":"running"}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash="a" * 64,
    )
    b1 = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-b",
        logical_artifact_id="job-b",
        raw=b'{"job_id":"job-b","state":"running"}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    def observed(publication):
        return MutablePublicationObservation(
            logical_artifact_id=publication.logical_artifact_id,
            raw_sha256=publication.raw_sha256,
            byte_count=publication.byte_count,
            run_id=publication.run_id,
            contract_id=publication.contract_id,
            contract_hash=publication.contract_hash,
        )

    first_snapshot = {"job-a": observed(a1), "job-b": observed(b1)}
    assert authority.verify_latest_record_set("job_obligation", first_snapshot).authorized

    a2 = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=b'{"job_id":"job-a","state":"done"}',
        expected_previous_raw_sha256=a1.raw_sha256,
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash="a" * 64,
    )
    assert authority.verify_latest_record_set("job_obligation", first_snapshot).status == "mismatch"

    b2 = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-b",
        logical_artifact_id="job-b",
        raw=b'{"job_id":"job-b","state":"done"}',
        expected_previous_raw_sha256=b1.raw_sha256,
    )
    mixed_snapshot = {"job-a": observed(a2), "job-b": observed(b1)}
    assert authority.verify_latest_record_set("job_obligation", mixed_snapshot).status == "mismatch"

    authority.revoke_latest(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        expected_previous_raw_sha256=a2.raw_sha256,
    )
    assert authority.verify_latest_record_set("job_obligation", {"job-b": observed(b2)}).authorized
    assert (
        authority.verify_latest_record_set(
            "job_obligation", {"job-a": observed(a2), "job-b": observed(b2)}
        ).status
        == "mismatch"
    )


def test_mutable_batch_verification_binds_run_and_contract_tuple(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    head = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-contract",
        logical_artifact_id="job-contract",
        raw=RAW,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash="a" * 64,
    )
    base = {
        "logical_artifact_id": "job-contract",
        "raw_sha256": head.raw_sha256,
        "byte_count": head.byte_count,
        "run_id": RUN_ID,
        "contract_id": "ic-aaaaaaaaaaaa",
        "contract_hash": "a" * 64,
    }

    assert authority.verify_latest_record_set(
        "job_obligation", {"job-contract": MutablePublicationObservation(**base)}
    ).authorized
    for field, value in (
        ("run_id", "run-foreign"),
        ("contract_id", "ic-bbbbbbbbbbbb"),
        ("contract_hash", "b" * 64),
        ("raw_sha256", "b" * 64),
        ("byte_count", head.byte_count + 1),
    ):
        changed = {**base, field: value}
        assert (
            authority.verify_latest_record_set(
                "job_obligation",
                {"job-contract": MutablePublicationObservation(**changed)},
            ).status
            == "mismatch"
        )


def test_nonraising_mutable_and_revocation_helpers_share_the_host_head(tmp_path):
    authority = _live_authority(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    install_evidence_publication_authority(authority)
    published = publish_evidence_revision(
        None,
        record_kind="report_metrics",
        record_id="report-metrics",
        logical_artifact_id="report-metrics",
        raw=RAW,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    revoked = revoke_evidence_artifact(
        None,
        record_kind="report_metrics",
        record_id="report-metrics",
        logical_artifact_id="report-metrics",
        expected_previous_raw_sha256=published.publication.raw_sha256,
    )

    assert published.status == "published"
    assert revoked.status == "revoked"
    assert revoked.committed is True


def test_nonraising_publish_helper_reports_unavailable_without_authorizing_bytes():
    unavailable = unavailable_evidence_publication_authority("host sink failed")
    install_evidence_publication_authority(unavailable)

    result = publish_evidence_bytes(
        None,
        record_kind="policy_claim",
        record_id="claim-a",
        raw=RAW,
    )

    assert result.status == "publication_unavailable"
    assert result.publication is None


def test_setup_agent_installs_host_authority_on_orchestrator(tmp_path, monkeypatch):
    class Session:
        run_pin_path = tmp_path / "run-pin.json"

        def get_control_event_sink(self, *, mirror):
            return ControlEventSink(tmp_path / "control_events.jsonl", mirror=mirror)

    class Orchestrator:
        def execute_command(self, _command):
            return {"success": True, "exit_code": 0, "output": ""}

    agent = object.__new__(SetupAgent)
    agent.run_id = RUN_ID
    agent.orchestrator = Orchestrator()
    agent.agent_logger = SimpleNamespace(error=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_module, "get_session_logger", lambda: Session())

    agent._initialize_control_recording()

    installed = current_evidence_publication_authority(agent.orchestrator)
    assert installed is agent.evidence_publication_authority
    assert installed.available is True
    assert installed.run_id == RUN_ID
    assert Path(agent.control_event_sink.path) == tmp_path / "control_events.jsonl"


def test_setup_agent_run_pin_refuses_a_host_file_that_is_not_the_current_head(tmp_path):
    class Orchestrator:
        container_name = "sag-one-run-pin-store"

        def evidence_store_identity(self):
            return "docker:sag-one-run-pin-store-id"

        def execute_command(self, _command, **_kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    orchestrator = Orchestrator()
    authority = EvidencePublicationAuthority.for_live_run(
        run_id=RUN_ID,
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    install_evidence_publication_authority(authority, orchestrator=orchestrator)
    host_path = tmp_path / "run-pin.json"
    agent = object.__new__(SetupAgent)
    agent.orchestrator = orchestrator
    agent.react_engine = None
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    agent._run_pin_host_path = host_path
    agent._run_pin_mirror = None
    agent._run_pin_template = {
        "run_id": RUN_ID,
        "container_image_digest": f"sha256:{'1' * 64}",
        "sag_git_sha": "2" * 40,
        "thinking_model": "think",
        "action_model": "act",
        "sanitized_config": {},
        "prompt_bundle_sha256": "3" * 64,
        "feature_flags": {},
        "run_order_index": None,
        "random_seed_or_null": None,
        "dependency_cache_state": "cold",
        "host_arch": "test",
    }

    assert agent._write_run_pin(target_repo_sha=None) is True
    first_head = authority.latest_head("host-run-pin")
    host_path.write_bytes(b'{"forged":true}')

    assert agent._write_run_pin(target_repo_sha="4" * 40) is False
    assert authority.latest_head("host-run-pin") == first_head
