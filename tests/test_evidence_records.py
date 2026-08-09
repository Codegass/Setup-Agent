import base64
import hashlib
import json
import subprocess

import pytest

import sag.agent.evidence_records as evidence_records_module
from sag.agent.attempt_policy import resolve_current_build_receipt_scope
from sag.agent.control_events import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    ControlEventSink,
    RunPin,
    canonical_json,
)
from sag.agent.evidence_publications import (
    RUN_PIN_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.evidence_records import (
    NAMED_JSON_RECORD_END_PREFIX,
    EvidencePublicationBinding,
    NamedJsonRecord,
    decode_json_record_stream,
    decode_named_json_record_stream,
    execute_named_json_file_stream,
    execute_named_json_record_stream,
    frame_json_record_stream,
    frame_named_json_record_stream,
    json_record_stream_command,
    named_json_record_stream_command,
    read_json_records,
    read_live_published_json_records,
    read_live_published_mutable_json_object,
)

_DIRECTORY_STREAM_COMMANDS = (
    (json_record_stream_command, decode_json_record_stream),
    (named_json_record_stream_command, decode_named_json_record_stream),
)


def _run_directory_stream(command_factory, directory):
    return subprocess.run(
        ["/bin/bash", "-c", command_factory(str(directory))],
        check=False,
        capture_output=True,
        text=True,
    )


class _AtomicRecordContainer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.command = ""
        self.kwargs = {}

    def execute_command(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        if self.fail:
            return {"exit_code": 70, "output": frame_json_record_stream(['{"record":1}'])}
        return {
            "exit_code": 0,
            "output": frame_json_record_stream(
                json.dumps({"record": number}, separators=(",", ":")) for number in (1, 2)
            ),
        }


def test_atomic_json_files_are_read_with_explicit_transport_boundaries():
    container = _AtomicRecordContainer()

    assert read_json_records(container, "/workspace/.setup_agent/claims") == [
        {"record": 1},
        {"record": 2},
    ]
    assert "for file in" in container.command
    assert "base64" in container.command
    assert "sha256sum" in container.command
    assert "SAG_JSON_RECORD_END_V1" in container.command
    assert "cat /workspace/.setup_agent/claims/*.json" not in container.command
    assert container.kwargs["truncate_output"] is False
    assert container.kwargs["timeout"] == 120


def test_partial_multi_file_read_is_not_returned_as_complete_evidence():
    assert read_json_records(_AtomicRecordContainer(fail=True), "/evidence") == []


def _result(output, *, exit_code=0):
    return {"success": exit_code == 0, "exit_code": exit_code, "output": output}


@pytest.mark.parametrize(
    "command_factory",
    [json_record_stream_command, named_json_record_stream_command],
)
def test_directory_stream_commands_prebound_every_body_before_base64(command_factory):
    command = command_factory("/workspace/.setup_agent/evidence")

    guards = (
        f'[ "$bytes" -le {evidence_records_module.NAMED_JSON_RECORD_MAX_BYTES} ]',
        f'[ "$next_count" -le {evidence_records_module.NAMED_JSON_RECORD_MAX_COUNT} ]',
        f'[ "$next_raw_bytes" -le {evidence_records_module.NAMED_JSON_RECORD_MAX_STREAM_BYTES} ]',
        f'[ "$next_output_bytes" -le {evidence_records_module.NAMED_JSON_RECORD_MAX_STREAM_BYTES} ]',
    )
    first_base64 = command.index("base64")
    for guard in guards:
        assert guard in command
        assert command.index(guard) < first_base64


@pytest.mark.parametrize(("command_factory", "decoder"), _DIRECTORY_STREAM_COMMANDS)
def test_directory_stream_rejects_oversized_file_before_emitting_its_body(
    tmp_path,
    monkeypatch,
    command_factory,
    decoder,
):
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_BYTES", 8)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_COUNT", 10)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_STREAM_BYTES", 4096)
    raw = b"oversized"
    (tmp_path / "record.json").write_bytes(raw)

    completed = _run_directory_stream(command_factory, tmp_path)

    assert completed.returncode == 70
    assert completed.stdout == ""
    assert base64.b64encode(raw).decode("ascii") not in completed.stdout
    decoded = decoder(_result(completed.stdout, exit_code=completed.returncode))
    assert decoded.complete is False
    assert decoded.conflict == "stream_unreadable"


@pytest.mark.parametrize(("command_factory", "decoder"), _DIRECTORY_STREAM_COMMANDS)
def test_directory_stream_rejects_record_count_before_emitting_excess_body(
    tmp_path,
    monkeypatch,
    command_factory,
    decoder,
):
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_BYTES", 64)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_COUNT", 1)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_STREAM_BYTES", 4096)
    first = b'{"record":"first"}'
    excess = b'{"record":"excess-secret"}'
    (tmp_path / "a.json").write_bytes(first)
    (tmp_path / "b.json").write_bytes(excess)

    completed = _run_directory_stream(command_factory, tmp_path)

    assert completed.returncode == 70
    assert base64.b64encode(first).decode("ascii") in completed.stdout
    assert base64.b64encode(excess).decode("ascii") not in completed.stdout
    decoded = decoder(_result(completed.stdout, exit_code=completed.returncode))
    assert decoded.complete is False
    assert decoded.conflict == "stream_unreadable"


@pytest.mark.parametrize(("command_factory", "decoder"), _DIRECTORY_STREAM_COMMANDS)
def test_directory_stream_accounts_for_base64_expansion_before_output(
    tmp_path,
    monkeypatch,
    command_factory,
    decoder,
):
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_BYTES", 64)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_COUNT", 10)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_STREAM_BYTES", 160)
    raw = b"x" * 60
    (tmp_path / "record.json").write_bytes(raw)

    completed = _run_directory_stream(command_factory, tmp_path)

    assert len(raw) < evidence_records_module.NAMED_JSON_RECORD_MAX_STREAM_BYTES
    assert completed.returncode == 70
    assert completed.stdout == ""
    assert base64.b64encode(raw).decode("ascii") not in completed.stdout
    decoded = decoder(_result(completed.stdout, exit_code=completed.returncode))
    assert decoded.complete is False
    assert decoded.conflict == "stream_unreadable"


@pytest.mark.parametrize(("command_factory", "decoder"), _DIRECTORY_STREAM_COMMANDS)
def test_directory_stream_output_budget_is_an_inclusive_exact_bound(
    tmp_path,
    monkeypatch,
    command_factory,
    decoder,
):
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_BYTES", 64)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_COUNT", 10)
    monkeypatch.setattr(evidence_records_module, "NAMED_JSON_RECORD_MAX_STREAM_BYTES", 4096)
    (tmp_path / "record.json").write_bytes(b'{"record":1}')
    baseline = _run_directory_stream(command_factory, tmp_path)
    exact_output_bytes = len(baseline.stdout.encode("utf-8"))

    assert baseline.returncode == 0
    assert decoder(_result(baseline.stdout)).complete is True

    monkeypatch.setattr(
        evidence_records_module,
        "NAMED_JSON_RECORD_MAX_STREAM_BYTES",
        exact_output_bytes,
    )
    exact = _run_directory_stream(command_factory, tmp_path)
    assert exact.returncode == 0
    assert exact.stdout == baseline.stdout

    monkeypatch.setattr(
        evidence_records_module,
        "NAMED_JSON_RECORD_MAX_STREAM_BYTES",
        exact_output_bytes - 1,
    )
    over = _run_directory_stream(command_factory, tmp_path)
    assert over.returncode == 70
    assert over.stdout == ""
    assert decoder(_result(over.stdout, exit_code=over.returncode)).complete is False


def test_pretty_json_is_one_framed_file_and_decodes_as_one_object():
    pretty = json.dumps({"nested": {"record": 1}}, indent=2)

    decoded = decode_json_record_stream(_result(frame_json_record_stream([pretty])))

    assert decoded.complete is True
    assert decoded.conflict is None
    assert decoded.records == ({"nested": {"record": 1}},)


def test_duplicate_keys_are_rejected_at_every_object_depth():
    stream = frame_json_record_stream(['{"good":1}', '{"outer":{"same":1,"same":2}}'])

    decoded = decode_json_record_stream(_result(stream))

    assert decoded.complete is True
    assert decoded.conflict == "record_malformed"
    assert decoded.records == ({"good": 1},)


def test_forgiving_reader_never_promotes_a_bad_record():
    class Container:
        def execute_command(self, command, **kwargs):
            return _result(frame_json_record_stream(['{"good":1}', '{"same":1,"same":2}']))

    assert read_json_records(Container(), "/evidence") == [{"good": 1}]


def test_multiple_objects_inside_one_file_are_one_malformed_record():
    stream = frame_json_record_stream(['{"first":1}\n{"second":2}'])

    decoded = decode_json_record_stream(_result(stream))

    assert decoded.complete is True
    assert decoded.conflict == "record_malformed"
    assert decoded.records == ()


def test_malformed_json_and_top_level_non_object_are_rejected():
    decoded = decode_json_record_stream(
        _result(frame_json_record_stream(['{"broken":', '[{"not":"object"}]']))
    )

    assert decoded.complete is True
    assert decoded.conflict == "record_malformed"
    assert decoded.records == ()


def test_partial_transport_without_terminal_frame_is_fail_closed():
    complete = frame_json_record_stream(['{"record":1}', '{"record":2}'])
    truncated = complete.rsplit("SAG_JSON_RECORD_END_V1", 1)[0]

    decoded = decode_json_record_stream(_result(truncated))

    assert decoded.complete is False
    assert decoded.conflict == "stream_incomplete"
    assert decoded.records == ()


def test_partial_base64_frame_is_fail_closed_even_when_command_claims_success():
    complete = frame_json_record_stream(['{"record":1}'])
    header, footer = complete.splitlines()
    truncated = f"{header[:-4]}\n{footer}\n"

    decoded = decode_json_record_stream(_result(truncated))

    assert decoded.complete is False
    assert decoded.conflict == "stream_malformed"
    assert decoded.records == ()


def test_length_and_digest_are_verified_before_json_is_trusted():
    raw = b'{"record":1}'
    encoded = base64.b64encode(raw).decode("ascii")
    digest = hashlib.sha256(raw).hexdigest()

    bad_length = f"SAG_JSON_RECORD_V1\t{len(raw) + 1}\t{digest}\t{encoded}\n"
    bad_digest = f"SAG_JSON_RECORD_V1\t{len(raw)}\t{'0' * 64}\t{encoded}\n"
    footer = "SAG_JSON_RECORD_END_V1\t1\n"

    assert decode_json_record_stream(_result(bad_length + footer)).complete is False
    assert decode_json_record_stream(_result(bad_digest + footer)).complete is False


def test_empty_stream_requires_a_complete_zero_record_footer():
    empty = decode_json_record_stream(_result("SAG_JSON_RECORD_END_V1\t0"))
    missing_footer = decode_json_record_stream(_result(""))

    assert empty.complete is True
    assert empty.records == ()
    assert missing_footer.complete is False


def test_complete_footer_without_terminal_newline_matches_real_executor_transport():
    stripped_by_executor = frame_json_record_stream(['{"record":1}']).rstrip("\n")

    decoded = decode_json_record_stream(_result(stripped_by_executor))

    assert decoded.complete is True
    assert decoded.conflict is None
    assert decoded.records == ({"record": 1},)


def test_clipped_footer_without_terminal_newline_is_still_incomplete():
    complete = frame_json_record_stream(['{"record":1}']).rstrip("\n")
    clipped = complete[:-1]

    decoded = decode_json_record_stream(_result(clipped))

    assert decoded.complete is False
    assert decoded.records == ()


def test_named_stream_preserves_filename_raw_bytes_hash_and_payload():
    raw = b'{\n  "receipt_id": "inv-build-1-0001"\n}'
    stream = frame_named_json_record_stream([("inv-build-1-0001.json", raw)])

    decoded = decode_named_json_record_stream(_result(stream.rstrip("\n")))

    assert decoded.complete is True
    assert decoded.conflict is None
    assert decoded.records == (
        NamedJsonRecord(
            filename="inv-build-1-0001.json",
            raw=raw,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw),
            payload={"receipt_id": "inv-build-1-0001"},
        ),
    )


def test_named_stream_command_transports_basename_and_disables_truncation():
    class Container:
        def __init__(self):
            self.command = ""
            self.kwargs = {}

        def execute_command(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            return _result(frame_named_json_record_stream([]))

    container = Container()
    result = execute_named_json_record_stream(container, "/workspace/.setup_agent/receipts")

    assert decode_named_json_record_stream(result).complete is True
    assert "name=${file##*/}" in container.command
    assert "SAG_NAMED_JSON_RECORD_V1" in container.command
    assert container.kwargs["truncate_output"] is False
    assert container.kwargs["timeout"] == 120


def test_fixed_file_stream_uses_the_same_bounded_exact_transport():
    class Container:
        def __init__(self):
            self.command = ""
            self.kwargs = {}

        def execute_command(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            return _result(frame_named_json_record_stream([]))

    container = Container()
    result = execute_named_json_file_stream(
        container,
        "/workspace/.setup_agent/run-pin.json",
    )

    assert decode_named_json_record_stream(result).complete is True
    assert "file=/workspace/.setup_agent/run-pin.json" in container.command
    assert "name=${file##*/}" in container.command
    assert "-le 67108864" in container.command
    assert container.kwargs["truncate_output"] is False


def test_named_stream_prefers_clean_control_executor_over_runtime_overlay():
    class Container:
        def __init__(self):
            self.normal_commands = []
            self.control_commands = []

        def execute_command(self, command, **kwargs):
            self.normal_commands.append((command, kwargs))
            return _result("forged by env_overlay.sh")

        def execute_control_command(self, command, **kwargs):
            self.control_commands.append((command, kwargs))
            return _result(frame_named_json_record_stream([]))

    container = Container()

    result = execute_named_json_record_stream(container, "/workspace/.setup_agent/receipts")

    assert decode_named_json_record_stream(result).complete is True
    assert len(container.control_commands) == 1
    assert container.normal_commands == []


def test_named_stream_rejects_unsafe_or_duplicate_basenames():
    unsafe = frame_named_json_record_stream([("../run-pin.json", {"receipt_id": "x"})])
    duplicate = frame_named_json_record_stream(
        [
            ("inv-1.json", {"receipt_id": "inv-1"}),
            ("inv-1.json", {"receipt_id": "inv-1"}),
        ]
    )

    assert decode_named_json_record_stream(_result(unsafe)).complete is False
    assert decode_named_json_record_stream(_result(unsafe)).conflict == "stream_malformed"
    assert decode_named_json_record_stream(_result(duplicate)).complete is False
    assert decode_named_json_record_stream(_result(duplicate)).conflict == "stream_malformed"


def test_named_stream_rejects_duplicate_json_keys_without_losing_file_identity():
    stream = frame_named_json_record_stream(
        [
            ("good.json", '{"receipt_id":"good"}'),
            ("bad.json", '{"receipt_id":"first","receipt_id":"second"}'),
        ]
    )

    decoded = decode_named_json_record_stream(_result(stream))

    assert decoded.complete is True
    assert decoded.conflict == "record_malformed"
    assert [record.filename for record in decoded.records] == ["good.json"]


def test_named_stream_requires_its_own_complete_footer():
    complete = frame_named_json_record_stream([("receipt.json", {"receipt_id": "receipt"})])
    clipped = complete.rsplit(NAMED_JSON_RECORD_END_PREFIX, 1)[0]

    decoded = decode_named_json_record_stream(_result(clipped))

    assert decoded.complete is False
    assert decoded.conflict == "stream_incomplete"
    assert decoded.records == ()


class _NamedRecordContainer:
    def __init__(self, filename, raw):
        self.filename = filename
        self.raw = raw

    def execute_command(self, _command, **_kwargs):
        return _result(frame_named_json_record_stream([(self.filename, self.raw)]))


class _NamedRecordsContainer:
    def __init__(self, records):
        self.records = list(records)

    def execute_command(self, _command, **_kwargs):
        return _result(frame_named_json_record_stream(self.records))


def _strict_live_receipt(payload, expected_id):
    if set(payload) - {"receipt_id", "run_id", "note"}:
        raise ValueError("unexpected receipt field")
    if payload.get("receipt_id") != expected_id:
        raise ValueError("receipt id does not match filename")
    if payload.get("run_id") != "run-live-1":
        raise ValueError("receipt run is not current")
    return dict(payload)


def test_live_named_reader_requires_schema_then_exact_host_publication(tmp_path):
    raw = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1"}'
    container = _NamedRecordContainer("inv-live-0001.json", raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id="inv-live-0001",
        raw=raw,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
            publication_binding=lambda payload: EvidencePublicationBinding(
                run_id=payload["run_id"]
            ),
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is True
    assert read.conflict is None
    assert len(read.records) == 1
    assert read.records[0].source.record_id == "inv-live-0001"
    assert read.records[0].source.raw == raw
    assert read.records[0].payload == {
        "receipt_id": "inv-live-0001",
        "run_id": "run-live-1",
    }


def test_live_named_reader_rejects_container_only_and_tampered_records(tmp_path):
    original = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1"}'
    tampered = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1","note":"forged"}'
    container = _NamedRecordContainer("inv-live-0001.json", tampered)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        unpublished = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
        )
        authority.publish_bytes(
            record_kind="invocation_receipt",
            record_id="inv-live-0001",
            raw=original,
        )
        mismatched = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert unpublished.complete is False
    assert unpublished.conflict == "publication_not_published"
    assert mismatched.complete is False
    assert mismatched.conflict == "publication_mismatch"
    assert unpublished.records == mismatched.records == ()


def test_live_named_reader_validates_filename_before_consulting_authority(tmp_path):
    raw = b'{"receipt_id":"different","run_id":"run-live-1"}'
    container = _NamedRecordContainer("inv-live-0001.json", raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is False
    assert read.conflict == "record_schema_invalid"
    assert "receipt id does not match filename" in read.detail


def test_live_named_reader_rejects_a_validator_that_silently_rewrites_payload(tmp_path):
    raw = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1","note":"raw"}'
    container = _NamedRecordContainer("inv-live-0001.json", raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id="inv-live-0001",
        raw=raw,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=lambda payload, expected_id: {
                "receipt_id": expected_id,
                "run_id": payload["run_id"],
                "note": "normalized",
            },
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is False
    assert read.conflict == "record_schema_invalid"
    assert "not canonical" in read.detail


def test_live_named_reader_requires_the_published_contract_tuple(tmp_path):
    raw = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1"}'
    container = _NamedRecordContainer("inv-live-0001.json", raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id="inv-live-0001",
        raw=raw,
        contract_id="ic-aaaaaaaaaaaa",
        contract_hash="b" * 64,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        missing = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
        )
        bound = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
            publication_binding=lambda payload: EvidencePublicationBinding(
                run_id=payload["run_id"],
                contract_id="ic-aaaaaaaaaaaa",
                contract_hash="b" * 64,
            ),
        )
    finally:
        reset_evidence_publication_authority(token)

    assert missing.conflict == "publication_mismatch"
    assert bound.complete is True
    assert bound.conflict is None


def test_live_named_reader_rejects_a_deleted_host_expected_record(tmp_path):
    first = b'{"receipt_id":"inv-live-0001","run_id":"run-live-1"}'
    second = b'{"receipt_id":"inv-live-0002","run_id":"run-live-1"}'
    container = _NamedRecordContainer("inv-live-0001.json", first)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    for identifier, raw in (("inv-live-0001", first), ("inv-live-0002", second)):
        authority.publish_bytes(
            record_kind="invocation_receipt",
            record_id=identifier,
            raw=raw,
        )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is False
    assert read.conflict == "publication_set_mismatch"
    assert "expected set" in read.detail


def test_live_named_reader_ignores_strictly_classified_foreign_and_forensic_rows(tmp_path):
    records = [
        (
            "inv-foreign.json",
            b'{"receipt_id":"inv-foreign","run_id":"run-other"}',
        ),
        ("inv-forensic.json", b'{"receipt_id":"inv-forensic","schema_version":1}'),
    ]

    class Container:
        def execute_command(self, _command, **_kwargs):
            return _result(frame_named_json_record_stream(records))

    container = Container()
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )

    def scope(payload, current_run):
        if payload.get("schema_version") == 1:
            return "forensic"
        if payload.get("schema_version", 2) != 2 or not isinstance(payload.get("run_id"), str):
            return "current"
        return "current" if payload.get("run_id") == current_run else "foreign"

    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
            record_scope=scope,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is True
    assert read.records == ()


def test_live_named_reader_never_classifies_an_unknown_future_schema_as_foreign(tmp_path):
    raw = b'{"schema_version":999,"receipt_id":"inv-future","run_id":"run-other"}'
    container = _NamedRecordContainer("inv-future.json", raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )

    def scope(payload, current_run):
        if payload.get("schema_version") == 1:
            return "forensic"
        if payload.get("schema_version", 2) != 2:
            return "current"
        return "current" if payload.get("run_id") == current_run else "foreign"

    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/invocation_receipts",
            record_kind="invocation_receipt",
            validator=_strict_live_receipt,
            record_scope=scope,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert read.complete is False
    assert read.conflict == "record_schema_invalid"


def _strict_live_job(payload, expected_id):
    if set(payload) != {"job_id", "run_id", "state"}:
        raise ValueError("unexpected job field")
    if payload.get("job_id") != expected_id:
        raise ValueError("job id does not match filename")
    if payload.get("run_id") != "run-live-1":
        raise ValueError("job run is not current")
    if payload.get("state") not in {"running", "settled"}:
        raise ValueError("job state is invalid")
    return dict(payload)


def _job_publication_binding(payload):
    return EvidencePublicationBinding(
        run_id=payload["run_id"],
        logical_artifact_id=payload["job_id"],
    )


def test_live_named_reader_authorizes_only_the_latest_mutable_revision(tmp_path):
    running = b'{"job_id":"job-a","run_id":"run-live-1","state":"running"}'
    settled = b'{"job_id":"job-a","run_id":"run-live-1","state":"settled"}'
    container = _NamedRecordContainer("job-a.json", running)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    first = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=running,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=settled,
        expected_previous_raw_sha256=first.raw_sha256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        rolled_back = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
        container.raw = settled
        latest = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert rolled_back.complete is False
    assert rolled_back.conflict == "publication_set_mismatch"
    assert latest.complete is True
    assert latest.conflict is None
    assert latest.records[0].payload["state"] == "settled"


def test_live_named_reader_requires_all_mutable_heads_and_honors_tombstones(tmp_path):
    job_a = b'{"job_id":"job-a","run_id":"run-live-1","state":"running"}'
    job_b = b'{"job_id":"job-b","run_id":"run-live-1","state":"running"}'
    container = _NamedRecordsContainer([("job-a.json", job_a)])
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=job_a,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    second = authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-b",
        logical_artifact_id="job-b",
        raw=job_b,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        deleted = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
        authority.revoke_latest(
            record_kind="job_obligation",
            record_id="job-b",
            logical_artifact_id="job-b",
            expected_previous_raw_sha256=second.raw_sha256,
        )
        after_tombstone = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
        container.records.append(("job-b.json", job_b))
        stale_tombstoned_file = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert deleted.complete is False
    assert deleted.conflict == "publication_set_mismatch"
    assert after_tombstone.complete is True
    assert after_tombstone.conflict is None
    assert stale_tombstoned_file.complete is False
    assert stale_tombstoned_file.conflict == "publication_set_mismatch"


def test_live_mutable_reader_cannot_return_a_revision_advanced_at_batch_check(tmp_path):
    running = b'{"job_id":"job-a","run_id":"run-live-1","state":"running"}'
    settled = b'{"job_id":"job-a","run_id":"run-live-1","state":"settled"}'

    class AdvancingAuthority(EvidencePublicationAuthority):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.batch_calls = 0

        def verify_latest_record_set(self, record_kind, observed_records):
            self.batch_calls += 1
            if self.batch_calls == 1:
                prior = self.latest_head("job-a")
                assert prior is not None
                self.publish_revision(
                    record_kind="job_obligation",
                    record_id="job-a",
                    logical_artifact_id="job-a",
                    raw=settled,
                    expected_previous_raw_sha256=prior.raw_sha256,
                )
            return super().verify_latest_record_set(record_kind, observed_records)

    container = _NamedRecordContainer("job-a.json", running)
    authority = AdvancingAuthority(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_revision(
        record_kind="job_obligation",
        record_id="job-a",
        logical_artifact_id="job-a",
        raw=running,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        read = read_live_published_json_records(
            container,
            "/workspace/.setup_agent/job_obligations",
            record_kind="job_obligation",
            validator=_strict_live_job,
            publication_binding=_job_publication_binding,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert authority.batch_calls == 1
    assert read.complete is False
    assert read.conflict == "publication_set_mismatch"
    assert "current host revision" in read.detail


class _FixedFileContainer:
    def __init__(self, raw):
        self.raw = raw

    def execute_command(self, _command, **_kwargs):
        records = [] if self.raw is None else [("run-pin.json", self.raw)]
        return _result(frame_named_json_record_stream(records))


def _strict_run_pin(payload, _expected_id):
    if set(payload) != {"run_id", "target"}:
        raise ValueError("unexpected run pin field")
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise ValueError("run id is invalid")
    if not isinstance(payload.get("target"), str) or not payload["target"]:
        raise ValueError("target is invalid")
    return dict(payload)


def _run_pin_binding(payload):
    return EvidencePublicationBinding(
        run_id=payload["run_id"],
        logical_artifact_id="host-run-pin",
    )


def test_fixed_mutable_reader_rejects_rollback_and_accepts_latest_head(tmp_path):
    first_raw = b'{"run_id":"run-live-1","target":"first"}'
    second_raw = b'{"run_id":"run-live-1","target":"second"}'
    container = _FixedFileContainer(first_raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    first = authority.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=first_raw,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    authority.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=second_raw,
        expected_previous_raw_sha256=first.raw_sha256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        rolled_back = read_live_published_mutable_json_object(
            container,
            "/workspace/.setup_agent/run-pin.json",
            record_kind="run_pin",
            record_id="host-run-pin",
            validator=_strict_run_pin,
            publication_binding=_run_pin_binding,
        )
        container.raw = second_raw
        current = read_live_published_mutable_json_object(
            container,
            "/workspace/.setup_agent/run-pin.json",
            record_kind="run_pin",
            record_id="host-run-pin",
            validator=_strict_run_pin,
            publication_binding=_run_pin_binding,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert rolled_back.complete is False
    assert rolled_back.conflict == "publication_set_mismatch"
    assert current.complete is True
    assert current.payload == {"run_id": "run-live-1", "target": "second"}
    assert current.raw == second_raw


def test_fixed_mutable_reader_tombstone_requires_physical_absence(tmp_path):
    raw = b'{"run_id":"run-live-1","target":"first"}'
    container = _FixedFileContainer(raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    published = authority.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=raw,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    authority.revoke_latest(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        expected_previous_raw_sha256=published.raw_sha256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        stale = read_live_published_mutable_json_object(
            container,
            "/workspace/.setup_agent/run-pin.json",
            record_kind="run_pin",
            record_id="host-run-pin",
            validator=_strict_run_pin,
            publication_binding=_run_pin_binding,
        )
        container.raw = None
        absent = read_live_published_mutable_json_object(
            container,
            "/workspace/.setup_agent/run-pin.json",
            record_kind="run_pin",
            record_id="host-run-pin",
            validator=_strict_run_pin,
            publication_binding=_run_pin_binding,
        )
    finally:
        reset_evidence_publication_authority(token)

    assert stale.complete is False
    assert stale.conflict == "publication_set_mismatch"
    assert absent.complete is True
    assert absent.payload is None


def _current_run_pin(*, target_sha="a" * 40):
    return RunPin(
        run_id="run-live-1",
        target_repo_sha=target_sha,
        container_image_digest=f"sha256:{'d' * 64}",
        sag_git_sha="b" * 40,
        thinking_model="thinking",
        action_model="action",
        sanitized_config={},
        prompt_bundle_sha256="c" * 64,
        feature_flags={"control_events": True},
        run_order_index=0,
        random_seed_or_null=None,
        dependency_cache_state="cold",
        host_arch="x86_64",
    )


def test_current_build_receipt_scope_requires_the_host_published_run_pin(tmp_path):
    raw = canonical_json(_current_run_pin()).encode("utf-8")
    container = _FixedFileContainer(raw)
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-live-1",
        sink=ControlEventSink(tmp_path / "control_events.jsonl"),
    )
    authority.bind_store(container)
    authority.publish_revision(
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        raw=raw,
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    token = install_evidence_publication_authority(authority, orchestrator=container)
    try:
        available = resolve_current_build_receipt_scope(
            container,
            run_id="run-live-1",
            manifest={"survey": {"project_path": "/workspace/project"}},
        )
        container.raw = canonical_json(_current_run_pin(target_sha="d" * 40)).encode("utf-8")
        tampered = resolve_current_build_receipt_scope(
            container,
            run_id="run-live-1",
            manifest={"survey": {"project_path": "/workspace/project"}},
        )
    finally:
        reset_evidence_publication_authority(token)

    assert available.available is True
    assert available.target_sha == "a" * 40
    assert available.project_root == "/workspace/project"
    assert tampered.available is False
    assert tampered.status == "run_pin_unreadable"
