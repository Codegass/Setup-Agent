import json
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from container_evidence_fakes import ContainerFS, canonical_json, complete_run_pin
from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import (
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    EvidencePublicationAuthority,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.verdict_finalizer import (
    VERDICT_LOGICAL_ARTIFACT_ID,
    VERDICT_SNAPSHOT_PATH,
    EvidenceCloseReason,
    RunVerdictSnapshot,
    VerdictFinalizer,
    read_live_verdict_snapshot,
    read_verdict_snapshot,
    validate_verdict_snapshot_v3,
)
from sag.web.session_registry import _read_setup_verdict_snapshot


@dataclass
class MemorySink:
    path: str = "/host/control_events.jsonl"
    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)
    fail: bool = False

    def emit(self, kind, payload, *, source=None):
        del source
        if self.fail and kind == "evidence_publication":
            raise OSError("host sink unavailable")
        self.events.append((str(kind), dict(payload)))


class VerdictOrchestrator:
    def __init__(self, files=None):
        self.filesystem = ContainerFS(files=files)
        self.files = self.filesystem.files

    def execute_command(self, command, **kwargs):
        if command.startswith("cat ") and command.endswith(" 2>/dev/null"):
            path = command.removeprefix("cat ").removesuffix(" 2>/dev/null")
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {"success": False, "exit_code": 1, "output": ""}
        return self.filesystem(command, **kwargs)


class StableStoreVerdictOrchestrator(VerdictOrchestrator):
    def __init__(self, store_identity: str, files=None):
        super().__init__(files=files)
        self.store_identity = store_identity

    def evidence_store_identity(self):
        return self.store_identity


class CleanOnlyVerdictOrchestrator(VerdictOrchestrator):
    def __init__(self, files=None):
        super().__init__(files=files)
        self.normal_calls = 0

    def execute_command(self, command, **kwargs):
        self.normal_calls += 1
        raise AssertionError("verdict evidence must not use the runtime executor")

    def execute_control_command(self, command, **kwargs):
        return VerdictOrchestrator.execute_command(self, command, **kwargs)


def _bind(orchestrator, run_id: str, *, sink: MemorySink | None = None):
    sink = sink or MemorySink()
    authority = EvidencePublicationAuthority(run_id=run_id, sink=sink)
    token = install_evidence_publication_authority(authority, orchestrator=orchestrator)
    reset_evidence_publication_authority(token)
    return authority, sink


def _snapshot(run_id: str, *, verdict: str = "failed") -> RunVerdictSnapshot:
    if verdict == "success":
        build_modules = {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1}
        test_cases = {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1}
    elif verdict == "failed":
        build_modules = {"rate": 0.0, "band": "none", "numerator": 0, "denominator": 1}
        test_cases = {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1}
    else:
        build_modules = {"band": "unavailable", "reason": "fixture module scan unavailable"}
        test_cases = {"rate": 100.0, "band": "fully", "numerator": 1, "denominator": 1}
    return RunVerdictSnapshot(
        run_id=run_id,
        finalized_at="2026-08-09T05:00:00Z",
        verdict=verdict,
        rates={
            "build": {
                "modules": build_modules,
                "classes": {"band": "unavailable", "reason": "fixture class census unavailable"},
            },
            "test": {
                "cases": test_cases,
                "modules": {"band": "unavailable", "reason": "fixture test survey unavailable"},
            },
            "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
        },
    )


def _publish_raw(authority, raw: str) -> None:
    head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    authority.publish_revision(
        record_kind="verdict",
        record_id=VERDICT_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=(
            head.raw_sha256 if head is not None else EVIDENCE_PUBLICATION_GENESIS_SHA256
        ),
    )


def test_public_v3_validator_is_pure_and_accepts_a_canonical_payload():
    payload = json.loads(_snapshot("schema-run").model_dump_json())
    payload["schema_version"] = 3
    payload.pop("rates")

    assert validate_verdict_snapshot_v3(payload).run_id == "schema-run"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", True, "schema version"),
        ("unique.executed", True, "unique test executed"),
        ("raw.errors", -1, "raw test errors"),
        ("collection_errors", True, "collection_errors"),
        ("compiled_classes", True, "compiled class"),
    ],
)
def test_public_v3_validator_rejects_non_strict_or_negative_counts(field, value, message):
    payload = json.loads(_snapshot("schema-run").model_dump_json())
    if field.startswith("unique.") or field.startswith("raw."):
        basis, count = field.split(".")
        payload["test_stats"][basis][count] = value
    elif field == "compiled_classes":
        payload["build_evidence"][field] = value
    elif field == "schema_version":
        payload[field] = value
    else:
        payload["test_stats"][field] = value

    with pytest.raises(ValueError, match=message):
        validate_verdict_snapshot_v3(payload)


def test_finalize_publishes_one_stable_latest_head_and_replays_without_new_revision():
    orchestrator = VerdictOrchestrator()
    authority, sink = _bind(orchestrator, "verdict-run")
    state = RunEvidenceState(run_id="verdict-run")
    finalizer = VerdictFinalizer(orchestrator)

    first = finalizer.finalize(state, EvidenceCloseReason.ABORTED)
    events_after_first = list(sink.events)
    second = finalizer.finalize(state, EvidenceCloseReason.ABORTED)
    restarted = VerdictFinalizer(orchestrator).finalize(state, EvidenceCloseReason.ABORTED)

    head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    assert head is not None
    assert head.record_kind == "verdict"
    assert head.record_id == VERDICT_LOGICAL_ARTIFACT_ID
    assert head.logical_artifact_id == VERDICT_LOGICAL_ARTIFACT_ID
    assert head.revision == 1
    assert sink.events == events_after_first
    assert first == second == restarted
    assert read_live_verdict_snapshot(orchestrator) == first


def test_finalize_and_read_use_only_the_clean_control_transport():
    orchestrator = CleanOnlyVerdictOrchestrator()
    _bind(orchestrator, "clean-verdict-run")
    state = RunEvidenceState(run_id="clean-verdict-run")

    snapshot = VerdictFinalizer(orchestrator).finalize(state, EvidenceCloseReason.ABORTED)

    assert read_verdict_snapshot(orchestrator) == snapshot
    assert orchestrator.normal_calls == 0


def test_forensic_read_never_falls_back_to_the_runtime_executor():
    class FailedCleanRead:
        def __init__(self):
            self.normal_calls = 0

        def execute_control_command(self, _command, **_kwargs):
            return {
                "success": False,
                "exit_code": -1,
                "dispatch_status": "control_transport_unavailable",
                "output": "",
            }

        def execute_command(self, _command, **_kwargs):
            self.normal_calls += 1
            return {
                "success": True,
                "exit_code": 0,
                "output": _snapshot("forged").model_dump_json(),
            }

    orchestrator = FailedCleanRead()

    snapshot = read_verdict_snapshot(orchestrator)

    assert snapshot.verdict == "unknown"
    assert snapshot.conflicts == ("snapshot_missing",)
    assert orchestrator.normal_calls == 0


def test_publication_failure_leaves_container_snapshot_forensic_only():
    orchestrator = VerdictOrchestrator()
    _authority, _sink = _bind(orchestrator, "publish-failed", sink=MemorySink(fail=True))
    state = RunEvidenceState(run_id="publish-failed")

    with pytest.raises(OSError, match="host publication"):
        VerdictFinalizer(orchestrator).finalize(state, EvidenceCloseReason.ABORTED)

    assert VERDICT_SNAPSHOT_PATH in orchestrator.files
    assert read_verdict_snapshot(orchestrator).run_id == "publish-failed"
    live = read_live_verdict_snapshot(orchestrator)
    assert live.verdict == "unknown"
    assert "publication" in " ".join(live.conflicts)


@pytest.mark.parametrize("mutation", ["overwrite", "delete"])
def test_live_reader_rejects_forged_overwrite_and_delete(mutation):
    orchestrator = VerdictOrchestrator()
    _authority, _sink = _bind(orchestrator, "tamper-run")
    state = RunEvidenceState(run_id="tamper-run")
    VerdictFinalizer(orchestrator).finalize(state, EvidenceCloseReason.ABORTED)

    if mutation == "delete":
        del orchestrator.files[VERDICT_SNAPSHOT_PATH]
    else:
        orchestrator.files[VERDICT_SNAPSHOT_PATH] = _snapshot(
            "tamper-run", verdict="success"
        ).model_dump_json()

    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_rollback_to_an_older_published_body():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "rollback-run")
    old_raw = _snapshot("rollback-run", verdict="failed").model_dump_json()
    new_raw = _snapshot("rollback-run", verdict="partial").model_dump_json()
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = old_raw
    _publish_raw(authority, old_raw)
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = new_raw
    _publish_raw(authority, new_raw)

    orchestrator.files[VERDICT_SNAPSHOT_PATH] = old_raw

    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_stale_file_after_host_tombstone():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "tombstone-run")
    raw = _snapshot("tombstone-run").model_dump_json()
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = raw
    _publish_raw(authority, raw)
    head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    assert head is not None
    authority.revoke_latest(
        record_kind="verdict",
        record_id=VERDICT_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
        expected_previous_raw_sha256=head.raw_sha256,
    )

    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_container_mirror_without_host_publication():
    raw = _snapshot("mirror-only").model_dump_json()
    orchestrator = VerdictOrchestrator({VERDICT_SNAPSHOT_PATH: raw})
    _bind(orchestrator, "mirror-only")

    assert read_verdict_snapshot(orchestrator).verdict == "failed"
    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_explicit_recovered_authority_accepts_its_original_identical_store(tmp_path):
    raw = _snapshot("store-bound-run").model_dump_json()
    original = StableStoreVerdictOrchestrator(
        "docker:store-a",
        {VERDICT_SNAPSHOT_PATH: raw},
    )
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = EvidencePublicationAuthority(run_id="store-bound-run", sink=sink)
    authority.assert_store(original)
    _publish_raw(authority, raw)
    recovered = EvidencePublicationAuthority.recover_from_host_jsonl(
        sink.path,
        run_id="store-bound-run",
    )

    assert read_live_verdict_snapshot(original, authority=recovered) == _snapshot("store-bound-run")


def test_explicit_recovered_authority_rejects_identical_bytes_from_another_store(tmp_path):
    raw = _snapshot("store-bound-run").model_dump_json()
    original = StableStoreVerdictOrchestrator("docker:store-a")
    sink = ControlEventSink(tmp_path / "control_events.jsonl")
    authority = EvidencePublicationAuthority(run_id="store-bound-run", sink=sink)
    authority.assert_store(original)
    _publish_raw(authority, raw)
    recovered = EvidencePublicationAuthority.recover_from_host_jsonl(
        sink.path,
        run_id="store-bound-run",
    )
    replacement = StableStoreVerdictOrchestrator(
        "docker:store-b",
        {VERDICT_SNAPSHOT_PATH: raw},
    )

    live = read_live_verdict_snapshot(replacement, authority=recovered)

    assert live.verdict == "unknown"
    assert live.conflicts == ("snapshot_live_publication_unavailable",)


def test_live_reader_rejects_an_unexpected_second_verdict_artifact():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "expected-set-run")
    raw = _snapshot("expected-set-run").model_dump_json()
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = raw
    _publish_raw(authority, raw)
    authority.publish_revision(
        record_kind="verdict",
        record_id="other-verdict",
        logical_artifact_id="other-verdict",
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )

    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_foreign_run():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "current-run")
    foreign_raw = _snapshot("foreign-run").model_dump_json()
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = foreign_raw
    _publish_raw(authority, foreign_raw)
    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_host_published_future_schema():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "current-run")
    future = json.loads(_snapshot("current-run").model_dump_json())
    future["schema_version"] = 999
    future_raw = json.dumps(future, sort_keys=True, separators=(",", ":"))
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = future_raw
    _publish_raw(authority, future_raw)
    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_host_published_duplicate_key_json():
    orchestrator = VerdictOrchestrator()
    authority, _sink = _bind(orchestrator, "duplicate-run")
    canonical = _snapshot("duplicate-run").model_dump_json()
    duplicate = canonical[:-1] + ',"verdict":"success"}'
    orchestrator.files[VERDICT_SNAPSHOT_PATH] = duplicate
    _publish_raw(authority, duplicate)

    assert read_live_verdict_snapshot(orchestrator).verdict == "unknown"


def test_live_reader_rejects_wrong_physical_filename():
    raw = _snapshot("filename-run").model_dump_json()

    class WrongFilenameOrchestrator(VerdictOrchestrator):
        def execute_command(self, command, **kwargs):
            if "SAG_NAMED_JSON_RECORD_V1" in command:
                return {
                    "success": True,
                    "exit_code": 0,
                    "output": frame_named_json_record_stream([("other.json", raw)]),
                }
            return super().execute_command(command, **kwargs)

    orchestrator = WrongFilenameOrchestrator({VERDICT_SNAPSHOT_PATH: raw})
    authority, _sink = _bind(orchestrator, "filename-run")
    _publish_raw(authority, raw)

    live = read_live_verdict_snapshot(orchestrator)
    assert live.verdict == "unknown"
    assert live.conflicts == ("snapshot_live_filename_invalid",)


def test_finalizer_refuses_to_replace_unexpected_existing_verdict_bytes():
    forged = _snapshot("cas-run", verdict="success").model_dump_json()
    orchestrator = VerdictOrchestrator({VERDICT_SNAPSHOT_PATH: forged})
    _authority, sink = _bind(orchestrator, "cas-run")
    state = RunEvidenceState(run_id="cas-run")

    with pytest.raises(OSError, match="compare-and-publish"):
        VerdictFinalizer(orchestrator).finalize(state, EvidenceCloseReason.ABORTED)

    assert orchestrator.files[VERDICT_SNAPSHOT_PATH] == forged
    assert [kind for kind, _payload in sink.events] == ["evidence_store_bound"]


def _publish_host_epoch(sink, orchestrator, *, run_id: str, verdict_raw: str):
    authority = EvidencePublicationAuthority(run_id=run_id, sink=sink)
    authority.assert_store(orchestrator)
    pin_raw = canonical_json(complete_run_pin(run_id, "a" * 40))
    authority.publish_revision(
        record_kind="run_pin",
        record_id="host-run-pin",
        logical_artifact_id="host-run-pin",
        raw=pin_raw.encode("utf-8"),
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    authority.publish_revision(
        record_kind="verdict",
        record_id=VERDICT_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
        raw=verdict_raw.encode("utf-8"),
        expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
    )
    return pin_raw


def test_web_offline_reader_recovers_verdict_from_host_run_pin_authority(tmp_path):
    session = tmp_path / "session_current"
    session.mkdir()
    (session / "command_project_demo.log").write_text("started", encoding="utf-8")
    sink = ControlEventSink(session / "control_events.jsonl")
    raw = _snapshot("offline-run", verdict="partial").model_dump_json()
    orchestrator = StableStoreVerdictOrchestrator(
        "docker:offline-store",
        {VERDICT_SNAPSHOT_PATH: raw},
    )
    pin_raw = _publish_host_epoch(
        sink,
        orchestrator,
        run_id="offline-run",
        verdict_raw=raw,
    )
    (session / "run-pin.json").write_text(pin_raw, encoding="utf-8")

    snapshot, status = _read_setup_verdict_snapshot(
        orchestrator,
        logs_root=tmp_path,
        project_name="demo",
    )

    assert status == "valid"
    assert snapshot is not None
    assert snapshot.run_id == "offline-run"
    assert snapshot.verdict == "partial"
    assert evidence_publication_authority_for(orchestrator).run_id == "offline-run"


def test_web_offline_reader_rejects_old_published_run_selected_by_container(tmp_path):
    session = tmp_path / "session_reused"
    session.mkdir()
    (session / "command_project_demo.log").write_text("started", encoding="utf-8")
    sink = ControlEventSink(session / "control_events.jsonl")
    old_raw = _snapshot("old-run", verdict="success").model_dump_json()
    old_orchestrator = StableStoreVerdictOrchestrator("docker:old-store")
    _publish_host_epoch(
        sink,
        old_orchestrator,
        run_id="old-run",
        verdict_raw=old_raw,
    )
    current_raw = _snapshot("current-run", verdict="failed").model_dump_json()
    orchestrator = StableStoreVerdictOrchestrator(
        "docker:current-store",
        {VERDICT_SNAPSHOT_PATH: old_raw},
    )
    current_pin_raw = _publish_host_epoch(
        sink,
        orchestrator,
        run_id="current-run",
        verdict_raw=current_raw,
    )
    (session / "run-pin.json").write_text(current_pin_raw, encoding="utf-8")

    snapshot, status = _read_setup_verdict_snapshot(
        orchestrator,
        logs_root=tmp_path,
        project_name="demo",
    )

    assert snapshot is None
    assert status == "untrusted"
