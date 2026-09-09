"""Strict host-publication authority for the live document-map consumer."""

import hashlib
import json

import pytest
from container_evidence_fakes import ContainerFS

from sag.agent.document_map import (
    DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
    DOCUMENT_MAP_PATH,
    document_map_payload,
    entry_id,
    read_live_document_map,
    validate_document_map_v1,
    write_document_map,
)
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    publish_evidence_revision,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.physical_survey import build_domain_facts, read_document_map

ROOT = "/workspace/proj"
README_PATH = f"{ROOT}/README.md"
TARGET_SHA = "a" * 40


class DocumentMapOrchestrator:
    """Expose the shared exact-file transport through an orchestrator surface."""

    def __init__(self) -> None:
        self.filesystem = ContainerFS()
        self.files = self.filesystem.files

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)

    # Strict evidence transport refuses a bound project-runtime executor and
    # requires the clean host-control channel; delegate so subclasses that
    # reshape the transport keep doing so on both channels.
    def execute_control_command(self, command, **kwargs):
        return self.execute_command(command, **kwargs)


class WrongFilenameOrchestrator(DocumentMapOrchestrator):
    """Return the right bytes under a non-canonical framed basename."""

    def execute_command(self, command, **kwargs):
        if command.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in command:
            records = (
                [("renamed-map.json", self.files[DOCUMENT_MAP_PATH])]
                if DOCUMENT_MAP_PATH in self.files
                else []
            )
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(records),
            }
        return super().execute_command(command, **kwargs)


def _entry(*, path: str = README_PATH, source_hash: str | None = None, **changes):
    payload = {
        "entry_id": entry_id(path),
        "target_sha": TARGET_SHA,
        "path": path,
        "realpath": path,
        "source_hash": source_hash or hashlib.sha256(b"# setup\n").hexdigest(),
        "kind": "markdown",
        "section_index": [],
        "parser_version": "1",
        "discovery_status": "indexed",
    }
    payload.update(changes)
    return payload


def _map(*, entries=None, partial_map=None, **changes):
    payload = document_map_payload(
        {
            "entries": [_entry()] if entries is None else entries,
            "partial_map": [] if partial_map is None else partial_map,
        }
    )
    payload.update(changes)
    return payload


def _publish_raw(orch, raw: str, *, previous=EVIDENCE_PUBLICATION_GENESIS_SHA256):
    orch.files[DOCUMENT_MAP_PATH] = raw
    result = publish_evidence_revision(
        orch,
        record_kind="document_map",
        record_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=previous,
    )
    assert result.published is True
    return result.publication


def test_live_document_map_returns_the_exact_current_host_head():
    orch = DocumentMapOrchestrator()
    payload = _map()
    assert write_document_map(orch.execute_command, payload) is True

    live = read_live_document_map(orch)

    assert live.complete is True
    assert live.conflict is None
    assert live.payload == payload
    assert live.raw == orch.files[DOCUMENT_MAP_PATH].encode("utf-8")


def test_forensic_document_map_reader_remains_explicitly_mirror_only():
    orch = DocumentMapOrchestrator()
    payload = _map()
    orch.files[DOCUMENT_MAP_PATH] = json.dumps(payload)

    assert read_document_map(orch) == payload
    assert read_live_document_map(orch).conflict == "publication_set_mismatch"


def test_live_document_map_rejects_tamper_delete_and_rollback():
    orch = DocumentMapOrchestrator()
    first = _map()
    second = _map(partial_map=[{"path": f"{ROOT}/vendor", "reason": "generated_tree"}])
    assert write_document_map(orch.execute_command, first) is True
    first_raw = orch.files[DOCUMENT_MAP_PATH]
    assert write_document_map(orch.execute_command, second) is True

    orch.files[DOCUMENT_MAP_PATH] = first_raw
    rollback = read_live_document_map(orch)
    orch.files[DOCUMENT_MAP_PATH] = json.dumps(_map(entries=[]), sort_keys=True)
    tamper = read_live_document_map(orch)
    del orch.files[DOCUMENT_MAP_PATH]
    deleted = read_live_document_map(orch)

    assert rollback.conflict == "publication_set_mismatch"
    assert tamper.conflict == "publication_set_mismatch"
    assert deleted.conflict == "publication_set_mismatch"


def test_live_document_map_honours_tombstone_and_verified_absence(
    bind_host_evidence_publication_authority,
):
    orch = DocumentMapOrchestrator()
    assert write_document_map(orch.execute_command, _map()) is True
    head = bind_host_evidence_publication_authority.latest_head(DOCUMENT_MAP_LOGICAL_ARTIFACT_ID)
    bind_host_evidence_publication_authority.revoke_latest(
        record_kind="document_map",
        record_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
        expected_previous_raw_sha256=head.raw_sha256,
    )

    stale = read_live_document_map(orch)
    del orch.files[DOCUMENT_MAP_PATH]
    absent = read_live_document_map(orch)

    assert stale.conflict == "publication_set_mismatch"
    assert absent.complete is True
    assert absent.conflict is None
    assert absent.payload is None


def test_live_document_map_requires_the_complete_current_mutable_set(
    bind_host_evidence_publication_authority,
):
    orch = DocumentMapOrchestrator()
    assert write_document_map(orch.execute_command, _map()) is True
    bind_host_evidence_publication_authority.publish_revision(
        record_kind="document_map",
        record_id="other-document-map",
        logical_artifact_id="other-document-map",
        raw=b'{"other":true}',
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    assert read_live_document_map(orch).conflict == "publication_set_mismatch"


def test_live_document_map_requires_the_stable_record_and_logical_identity():
    orch = DocumentMapOrchestrator()
    raw = json.dumps(_map(), sort_keys=True)
    orch.files[DOCUMENT_MAP_PATH] = raw
    publication = publish_evidence_revision(
        orch,
        record_kind="document_map",
        record_id="renamed-document-map",
        logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    assert publication.status == "publication_invalid"
    assert read_live_document_map(orch).conflict == "publication_set_mismatch"


def test_live_document_map_rejects_an_unavailable_host_authority():
    orch = DocumentMapOrchestrator()
    orch.files[DOCUMENT_MAP_PATH] = json.dumps(_map())
    unavailable = unavailable_evidence_publication_authority("host stream unavailable")
    token = install_evidence_publication_authority(unavailable, orchestrator=orch)
    try:
        live = read_live_document_map(orch)
    finally:
        reset_evidence_publication_authority(token)

    assert live.complete is False
    assert live.conflict == "publication_set_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_map(schema_version=2), id="future-schema"),
        pytest.param(_map(parser_version="2"), id="future-parser"),
        pytest.param(_map(entries=[_entry(source_hash="not-a-digest")]), id="source-hash"),
        pytest.param(_map(entries=[_entry(target_sha="not-a-sha")]), id="target-sha"),
        pytest.param(_map(entries=[_entry(entry_id="doc-000000000000")]), id="entry-id"),
        pytest.param(_map(document_map_fingerprint="f" * 64), id="fingerprint"),
        pytest.param(
            _map(partial_map=[{"path": f"{ROOT}/vendor", "reason": "invented"}]),
            id="partial-reason",
        ),
    ],
)
def test_strict_document_map_schema_rejects_future_or_noncanonical_payloads(payload):
    orch = DocumentMapOrchestrator()
    raw = json.dumps(payload, sort_keys=True)
    _publish_raw(orch, raw)

    live = read_live_document_map(orch)

    assert live.complete is False
    assert live.conflict == "record_schema_invalid"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not-json", id="malformed-json"),
        pytest.param('{"schema_version":1,"schema_version":1}', id="duplicate-key"),
        pytest.param("[]", id="not-object"),
    ],
)
def test_live_document_map_rejects_malformed_exactly_published_bytes(raw):
    orch = DocumentMapOrchestrator()
    _publish_raw(orch, raw)

    live = read_live_document_map(orch)

    assert live.complete is False
    assert live.conflict == "record_malformed"


def test_live_document_map_rejects_a_noncanonical_framed_filename():
    orch = WrongFilenameOrchestrator()
    raw = json.dumps(_map(), sort_keys=True)
    _publish_raw(orch, raw)

    live = read_live_document_map(orch)

    assert live.complete is False
    assert live.conflict == "record_filename_invalid"


def test_document_map_validator_returns_the_exact_schema_object():
    payload = _map()

    assert validate_document_map_v1(payload, DOCUMENT_MAP_LOGICAL_ARTIFACT_ID) == payload
    with pytest.raises(ValueError, match="publication identity"):
        validate_document_map_v1(payload, "renamed-document-map")


@pytest.mark.parametrize(
    "extent",
    [
        {"indexed_bytes": 5},
        {"content_truncated": False},
        {"indexed_bytes": True, "content_truncated": False},
        {"indexed_bytes": -1, "content_truncated": False},
        {"indexed_bytes": 512001, "content_truncated": True},
        {"indexed_bytes": 5, "content_truncated": "false"},
        {"indexed_bytes": 5, "content_truncated": True},
    ],
)
def test_document_extent_cannot_claim_an_invalid_or_undisclosed_prefix(extent):
    payload = _map(entries=[_entry(**extent)])
    with pytest.raises(ValueError, match="indexed byte extent"):
        validate_document_map_v1(payload)


@pytest.mark.parametrize("truncated", [False, True])
def test_published_document_extent_round_trips_without_inventing_legacy_extent(truncated):
    orch = DocumentMapOrchestrator()
    payload = _map(
        entries=[
            _entry(
                indexed_bytes=5,
                content_truncated=truncated,
                discovery_status="truncated" if truncated else "indexed",
            )
        ]
    )
    assert write_document_map(orch.execute_command, payload)
    assert read_live_document_map(orch).payload == payload
    legacy = _map()
    assert validate_document_map_v1(legacy) == legacy
    assert "indexed_bytes" not in legacy["entries"][0]


def test_published_document_extents_cannot_exceed_the_total_budget(monkeypatch):
    from sag.agent import document_map

    monkeypatch.setattr(document_map, "MAX_TOTAL_BYTES", 6)
    payload = _map(
        entries=[
            _entry(path=f"{ROOT}/a.md", indexed_bytes=5, content_truncated=False),
            _entry(path=f"{ROOT}/b.md", indexed_bytes=5, content_truncated=False),
        ]
    )
    with pytest.raises(ValueError, match="total budget"):
        validate_document_map_v1(payload)


def test_domain_facts_derive_partial_conflicts_only_from_the_live_document_map():
    orch = DocumentMapOrchestrator()
    payload = _map(partial_map=[{"path": f"{ROOT}/vendor/blob.bin", "reason": "generated_tree"}])
    assert write_document_map(orch.execute_command, payload) is True

    (fact,) = build_domain_facts(
        orch,
        [{"root": ROOT, "system": "maven"}],
        claims=[],
    )

    assert fact["open_conflicts"] == [
        {
            "kind": "partial_map",
            "path": f"{ROOT}/vendor/blob.bin",
            "reason": "generated_tree",
        }
    ]


def test_domain_facts_fail_closed_when_document_map_host_authority_is_missing():
    orch = DocumentMapOrchestrator()
    forged = _map(partial_map=[{"path": f"{ROOT}/forged", "reason": "generated_tree"}])
    orch.files[DOCUMENT_MAP_PATH] = json.dumps(forged)

    (fact,) = build_domain_facts(
        orch,
        [{"root": ROOT, "system": "maven"}],
        claims=[],
    )

    assert fact["open_conflicts"] == [
        {
            "kind": "document_map_integrity_unavailable",
            "reason": "publication_set_mismatch",
        }
    ]
    assert "forged" not in json.dumps(fact)


def test_domain_facts_treat_verified_document_map_absence_as_unavailable():
    orch = DocumentMapOrchestrator()

    (fact,) = build_domain_facts(
        orch,
        [{"root": ROOT, "system": "maven"}],
        claims=[],
    )

    assert fact["open_conflicts"] == [
        {"kind": "document_map_integrity_unavailable", "reason": "verified_absence"}
    ]
