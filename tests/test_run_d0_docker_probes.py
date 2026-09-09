import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_d0_docker_probes.py"
SPEC = importlib.util.spec_from_file_location("run_d0_docker_probes", SCRIPT)
assert SPEC and SPEC.loader
d0 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = d0
SPEC.loader.exec_module(d0)


def _facts() -> d0.HostFacts:
    return d0.HostFacts(
        sag_git_sha="a" * 40,
        source_tree_sha256="b" * 64,
        source_dirty=True,
        base_image="ubuntu:24.04",
        image_id="sha256:" + "c" * 64,
        image_repo_digests=("ubuntu@sha256:" + "d" * 64,),
        image_labels=(
            (d0.PREPARED_IMAGE_LABEL, d0.PREPARED_IMAGE_VERSION),
            (d0.PREPARED_SOURCE_LABEL, "b" * 64),
        ),
        docker_server_version="27.1.1",
        host_arch="arm64",
        python_version="3.12.4",
    )


class _EpochOrchestrator:
    def __init__(self, container_name):
        self.container_name = container_name
        self.filesystem = None

    def execute_command(self, command, **kwargs):
        if self.filesystem is not None:
            return self.filesystem(command, **kwargs)
        return {"exit_code": 0, "success": True, "output": ""}

    def evidence_store_identity(self):
        digest = hashlib.sha256(self.container_name.encode("utf-8")).hexdigest()
        return f"test-d0-store:{digest}"

    def execute_command_detached(self, command, **_kwargs):
        return {"started": True, "job_id": command, "output": ""}

    def poll_detached_command(self, handle, **_kwargs):
        return {"state": "terminal", "job_id": handle["job_id"]}

    def collect_detached_result(self, handle, _poll, **_kwargs):
        return {"exit_code": 0, "success": True, "output": handle["job_id"]}


def _epoch_runtime(tmp_path, *, probe="http-wrapper-unzip-ablation"):
    return d0.DockerProbeRuntime(
        repo=Path(__file__).parents[1],
        campaign_dir=tmp_path,
        campaign_id="D0 campaign:unsafe/spaces",
        base_image=d0.DEFAULT_PREPARED_IMAGE,
        spec=d0.PROBE_SPECS[probe],
        run_pin={},
        keep_containers=True,
    )


def _registered_epoch(runtime, name):
    audit = d0.CommandAudit(_EpochOrchestrator(name))
    runtime.audits.append(audit)
    return audit, runtime._register_epoch(audit, suffix=name)


def test_registry_is_exactly_the_seven_preregistered_no_model_probes():
    assert tuple(d0.PROBE_SPECS) == (
        "sync-large-evidence",
        "detached-large-settlement",
        "terminal-receipt-failure",
        "http-wrapper-unzip-ablation",
        "dynamic-jdk-authority",
        "gradle-runner-classification",
        "multi-job-progress-barrier",
    )
    assert [spec.order for spec in d0.PROBE_SPECS.values()] == list(range(7))
    assert all(spec.no_model for spec in d0.PROBE_SPECS.values())
    assert d0.PROBE_SPECS["http-wrapper-unzip-ablation"].container_count == 2
    assert set(d0.PROBE_IMPLEMENTATIONS) == set(d0.PROBE_SPECS)


def test_probe_selection_preserves_preregistered_order_and_rejects_unknowns():
    selected = d0.select_probes(["multi-job-progress-barrier", "sync-large-evidence"])
    assert [spec.name for spec in selected] == [
        "sync-large-evidence",
        "multi-job-progress-barrier",
    ]

    with pytest.raises(d0.D0Error, match="unknown D0 probe"):
        d0.select_probes(["not-registered"])


def test_registered_probe_starts_without_authority_and_restores_context(monkeypatch, tmp_path):
    from sag.agent.evidence_publications import current_evidence_publication_authority
    from sag.agent.invocation_receipts import active_receipt_run_id

    spec = d0.PROBE_SPECS["sync-large-evidence"]
    runtime = d0.DockerProbeRuntime(
        repo=Path(__file__).parents[1],
        campaign_dir=tmp_path,
        campaign_id="d0-epoch",
        base_image=d0.DEFAULT_PREPARED_IMAGE,
        spec=spec,
        run_pin={},
        keep_containers=True,
    )
    observed = []

    def implementation(_runtime):
        observed.append(active_receipt_run_id())
        return d0.ProbeObservation(passed=True)

    monkeypatch.setitem(d0.PROBE_IMPLEMENTATIONS, spec.name, implementation)

    previous_run = active_receipt_run_id()
    previous_authority = current_evidence_publication_authority()
    d0.execute_registered_probe(runtime, spec, {})

    assert observed == [previous_run]
    assert active_receipt_run_id() == previous_run
    assert current_evidence_publication_authority() is previous_authority


def test_each_fresh_container_has_distinct_sanitized_epoch_path_sink_and_authority(tmp_path):
    runtime = _epoch_runtime(tmp_path)
    control, control_epoch = _registered_epoch(runtime, "control")
    treatment, treatment_epoch = _registered_epoch(runtime, "treatment")

    assert control_epoch.run_id != treatment_epoch.run_id
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", control_epoch.run_id)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", treatment_epoch.run_id)
    assert control_epoch.control_event_path != treatment_epoch.control_event_path
    assert control_epoch.sink is not treatment_epoch.sink
    assert control_epoch.authority is not treatment_epoch.authority
    assert runtime.epoch_for(control) is control_epoch
    assert runtime.epoch_for(treatment) is treatment_epoch


def test_one_host_control_path_cannot_register_multiple_live_sinks(tmp_path):
    from sag.agent.control_events import ControlEventSink

    runtime = _epoch_runtime(tmp_path)
    path = tmp_path / "one-control-stream.jsonl"
    runtime._register_control_sink(path, ControlEventSink(path))
    with pytest.raises(d0.D0Stop, match="cannot own multiple live sinks"):
        runtime._register_control_sink(path, ControlEventSink(path))


def test_fresh_epoch_refuses_a_preexisting_host_stream(tmp_path):
    runtime = _epoch_runtime(tmp_path)
    path = runtime.scratch_dir / "epochs" / "container-1" / "control-events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("stale\n", encoding="utf-8")
    audit = d0.CommandAudit(_EpochOrchestrator("control"))
    runtime.audits.append(audit)

    with pytest.raises(d0.D0Stop, match="already exists"):
        runtime._register_epoch(audit, suffix="control")


def test_epoch_authority_rejects_a_second_container_store(tmp_path):
    from sag.agent.evidence_publications import EvidencePublicationConflict

    runtime = _epoch_runtime(tmp_path)
    _control, epoch = _registered_epoch(runtime, "control")
    other = d0.CommandAudit(_EpochOrchestrator("treatment"))

    with pytest.raises(EvidencePublicationConflict, match="more than one container store"):
        epoch.authority.bind_store(other)


def test_controller_scope_uses_matching_epoch_and_restores_both_contextvars(tmp_path):
    from sag.agent.evidence_publications import current_evidence_publication_authority
    from sag.agent.invocation_receipts import active_receipt_run_id

    runtime = _epoch_runtime(tmp_path, probe="dynamic-jdk-authority")
    audit, epoch = _registered_epoch(runtime, "main")
    previous_authority = current_evidence_publication_authority()
    previous_run = active_receipt_run_id()

    with d0._controller_action_scope(
        runtime,
        audit=audit,
        label="scope-proof",
        domain_id="d0-scope",
        tool="build",
        params={"action": "compile", "working_directory": "/workspace"},
        next_action_kind="compile",
    ):
        assert current_evidence_publication_authority() is epoch.authority
        assert active_receipt_run_id() == epoch.run_id

    assert current_evidence_publication_authority() is previous_authority
    assert active_receipt_run_id() == previous_run


def test_no_phase_engine_reuses_the_container_epoch_sink_and_run_id(tmp_path):
    runtime = _epoch_runtime(tmp_path, probe="multi-job-progress-barrier")
    audit, epoch = _registered_epoch(runtime, "main")

    class NoModelClient:
        def setup(self):
            raise AssertionError("D0 no-model engine must not initialize a provider")

    engine = d0._configure_no_phase_engine(
        runtime=runtime,
        audit=audit,
        orchestrator=audit,
        client=NoModelClient(),
    )

    assert engine.control_event_sink is epoch.sink
    assert engine.run_evidence_state.run_id == epoch.run_id


def _archived_publication_epoch(tmp_path, *, mutable=False):
    from sag.agent.control_events import EVIDENCE_PUBLICATION_GENESIS_SHA256, ControlEventSink
    from sag.agent.evidence_publications import EvidencePublicationAuthority

    container = tmp_path / "container-1"
    setup_agent = container / ".setup_agent"
    setup_agent.mkdir(parents=True)
    stream = container / "control-events.jsonl"
    run_id = "d0-archive-epoch-1"
    authority = EvidencePublicationAuthority.for_live_run(
        run_id=run_id,
        sink=ControlEventSink(stream),
    )
    archive_store_token = hashlib.sha256(str(container.resolve()).encode("utf-8")).hexdigest()
    authority.bind_store(_EpochOrchestrator(f"archive-{archive_store_token}"))
    if mutable:
        path = setup_agent / "document_map.json"
        first = json.dumps({"generation": 1}, sort_keys=True).encode()
        second = json.dumps({"generation": 2}, sort_keys=True).encode()
        authority.publish_revision(
            record_kind="document_map",
            record_id="workspace-document-map",
            logical_artifact_id="workspace-document-map",
            raw=first,
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        authority.publish_revision(
            record_kind="document_map",
            record_id="workspace-document-map",
            logical_artifact_id="workspace-document-map",
            raw=second,
            expected_previous_raw_sha256=hashlib.sha256(first).hexdigest(),
        )
        path.write_bytes(second)
        bodies = (first, second)
    else:
        path = setup_agent / "claims" / "claim-one.json"
        path.parent.mkdir()
        raw = json.dumps({"claim_id": "claim-one"}, sort_keys=True).encode()
        authority.publish_bytes(
            record_kind="policy_claim",
            record_id="claim-one",
            raw=raw,
        )
        path.write_bytes(raw)
        bodies = (raw,)
    (container / "evidence-epoch.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "container_name": "sag-d0-archive",
                "host_control_event_path": str(stream),
                "control_event_path": "container-1/control-events.jsonl",
            }
        ),
        encoding="utf-8",
    )
    return container, authority, path, bodies


def test_archive_epoch_recovery_accepts_exact_host_published_bytes(tmp_path):
    _archived_publication_epoch(tmp_path)

    events = d0._verify_archived_evidence_epochs(tmp_path)

    assert [event["kind"] for event in events] == [
        "evidence_store_bound",
        "evidence_publication",
    ]


@pytest.mark.parametrize("attack", [None, "foreign-run", "invalid-schema"])
def test_archive_epoch_validates_production_published_verdict(tmp_path, attack):
    from container_evidence_fakes import ContainerFS

    from sag.agent.evidence_publications import (
        install_evidence_publication_authority,
        reset_evidence_publication_authority,
    )
    from sag.agent.evidence_state import RunEvidenceState
    from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer

    container, authority, _path, _bodies = _archived_publication_epoch(tmp_path)
    store_token = hashlib.sha256(str(container.resolve()).encode("utf-8")).hexdigest()
    orchestrator = _EpochOrchestrator(f"archive-{store_token}")
    orchestrator.filesystem = ContainerFS()
    orchestrator.files = orchestrator.filesystem.files
    token = install_evidence_publication_authority(authority, orchestrator=orchestrator)
    try:
        snapshot = VerdictFinalizer(orchestrator).finalize(
            RunEvidenceState(run_id=authority.run_id), EvidenceCloseReason.ABORTED
        )
    finally:
        reset_evidence_publication_authority(token)
    raw = orchestrator.files["/workspace/.setup_agent/verdict.json"].encode("utf-8")
    assert json.loads(raw) == snapshot.model_dump(mode="json")
    if attack is not None:
        payload = json.loads(raw)
        payload["run_id" if attack == "foreign-run" else "schema_version"] = (
            "foreign-run" if attack == "foreign-run" else 999
        )
        revised = json.dumps(payload, sort_keys=True).encode("utf-8")
        authority.publish_revision(
            record_kind="verdict",
            record_id="run-verdict",
            logical_artifact_id="run-verdict",
            raw=revised,
            expected_previous_raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
        raw = revised
    (container / ".setup_agent" / "verdict.json").write_bytes(raw)

    if attack is not None:
        with pytest.raises(d0.D0Stop, match="verdict.*(invalid|another epoch)"):
            d0._verify_archived_evidence_epochs(tmp_path)
    else:
        events = d0._verify_archived_evidence_epochs(tmp_path)
        assert len(events) == 3


@pytest.mark.parametrize("attack", ["extra", "delete", "tamper"])
def test_archive_epoch_rejects_extra_deleted_or_tampered_immutable_evidence(tmp_path, attack):
    container, _authority, path, _bodies = _archived_publication_epoch(tmp_path)
    if attack == "extra":
        (path.parent / "claim-extra.json").write_text(
            json.dumps({"claim_id": "claim-extra"}), encoding="utf-8"
        )
    elif attack == "delete":
        path.unlink()
    else:
        path.write_text(json.dumps({"claim_id": "claim-one", "tampered": True}), encoding="utf-8")

    with pytest.raises(d0.D0Stop, match="expected set|absent|bytes differ"):
        d0._verify_archived_evidence_epochs(tmp_path)


def test_archive_epoch_rejects_mutable_rollback(tmp_path):
    _container, _authority, path, bodies = _archived_publication_epoch(tmp_path, mutable=True)
    path.write_bytes(bodies[0])

    with pytest.raises(d0.D0Stop, match="latest mutable evidence bytes differ"):
        d0._verify_archived_evidence_epochs(tmp_path)


def test_archive_epoch_rejects_tombstone_resurrection(tmp_path):
    container, authority, path, bodies = _archived_publication_epoch(tmp_path, mutable=True)
    authority.revoke_latest(
        record_kind="document_map",
        record_id="workspace-document-map",
        logical_artifact_id="workspace-document-map",
        expected_previous_raw_sha256=hashlib.sha256(bodies[1]).hexdigest(),
    )
    path.write_bytes(bodies[1])

    with pytest.raises(d0.D0Stop, match="tombstoned evidence was resurrected"):
        d0._verify_archived_evidence_epochs(tmp_path)


def test_epoch_authority_is_installed_where_the_project_lane_resolves_it(tmp_path):
    """DockerOrchestrator._default_exec_environment resolves authority from the
    orchestrator itself, so binding only the audit proxy leaves every project
    command refused with "runtime environment has no host publication authority".
    """

    from sag.agent.evidence_publications import current_evidence_publication_authority

    runtime = _epoch_runtime(tmp_path)
    audit, epoch = _registered_epoch(runtime, "main")

    assert current_evidence_publication_authority(audit.orchestrator) is epoch.authority
    assert current_evidence_publication_authority(audit) is epoch.authority
    # One container store, one identity: the proxy must never report its own.
    assert audit.evidence_store_identity() == audit.orchestrator.evidence_store_identity()


def test_control_plane_io_uses_the_clean_channel_not_the_project_lane():
    """Bootstrap/verification/archive I/O must not require the runtime overlay."""

    class Orchestrator:
        def __init__(self):
            self.clean_calls = []
            self.project_calls = []

        def execute_command(self, command, *_args, **kwargs):
            if kwargs.get("_clean_control_path"):
                self.clean_calls.append(command)
            else:
                self.project_calls.append(command)
            return {"exit_code": 0, "success": True, "output": ""}

        def execute_control_command(self, command, **kwargs):
            return self.execute_command(command, _clean_control_path=True, **kwargs)

        def execute_command_detached(self, command, *_args, **_kwargs):
            return {"started": True, "job_id": command, "output": ""}

        def poll_detached_command(self, handle, *_args, **_kwargs):
            return {"state": "terminal", "job_id": handle["job_id"]}

        def collect_detached_result(self, handle, _poll, *_args, **_kwargs):
            return {"exit_code": 0, "success": True, "output": ""}

    orchestrator = Orchestrator()
    audit = d0.CommandAudit(orchestrator)

    audit.execute_control_command("find /workspace/.setup_agent -name '*.tmp'")

    assert orchestrator.clean_calls == ["find /workspace/.setup_agent -name '*.tmp'"]
    assert orchestrator.project_calls == []
    # Clean-channel commands still reach the sealed content-addressed journal.
    assert [row["kind"] for row in audit.records] == ["execute_command"]


def test_detached_identity_gate_refuses_anything_short_of_a_docker_exec_envelope():
    """A schema-v3 obligation may only state an identity Docker really issued."""

    complete = {
        "started": True,
        "start_accepted": True,
        "startup_identity_verified": True,
        "runner_dispatch_state": "accepted",
        "terminal_authority": "docker_exec_inspect_v1",
        "docker_exec_id": "a" * 64,
        "container_id": "b" * 64,
        "process_identity_token": "c" * 64,
        "pid": 42,
        "pgid": 42,
        "job_id": "job",
        "log_path": "/tmp/sag_jobs/job.log",
        "exit_code_path": "/tmp/sag_jobs/job.log.exit",
        "pid_path": "/tmp/sag_jobs/job.pid",
        "pgid_path": "/tmp/sag_jobs/job.pgid",
        "identity_path": "/tmp/sag_jobs/job.identity",
    }

    assert d0._detached_identity_errors(complete) == []
    assert d0._detached_identity_errors({**complete, "docker_exec_id": "deadbeef"})
    assert d0._detached_identity_errors({**complete, "start_accepted": False})
    assert d0._detached_identity_errors({**complete, "pgid": 43})
    assert d0._detached_identity_errors({**complete, "terminal_authority": "exit_marker"})


def test_archived_obligation_identity_is_validated_against_the_live_v3_schema():
    from sag.agent.job_obligations import OBLIGATION_SCHEMA_VERSION, build_obligation

    assert OBLIGATION_SCHEMA_VERSION == 3
    obligation = build_obligation(
        job_id="d0-schema-probe",
        run_id="d0-schema-run",
        tool="bash",
        attempt=1,
        requested_action="run",
        effective_action="run",
        argv="sh -c 'exit 0'",
        working_directory="/workspace",
        before={},
        log_path="/tmp/sag_jobs/d0-schema-probe.log",
        exit_code_path="/tmp/sag_jobs/d0-schema-probe.log.exit",
        terminal_authority="docker_exec_inspect_v1",
        docker_exec_id="a" * 64,
        container_id="b" * 64,
        start_accepted=True,
        startup_identity_verified=True,
        runner_dispatch_state="accepted",
        pid=42,
        pgid=42,
        pid_path="/tmp/sag_jobs/d0-schema-probe.pid",
        pgid_path="/tmp/sag_jobs/d0-schema-probe.pgid",
        identity_path="/tmp/sag_jobs/d0-schema-probe.identity",
        process_identity_token="c" * 64,
    )
    raw = json.dumps(obligation, sort_keys=True).encode("utf-8")

    assert (
        d0._validate_archived_semantic_identity(
            record_kind="job_obligation",
            record_id="d0-schema-probe",
            raw=raw,
            run_id="d0-schema-run",
        )
        == obligation
    )
    tampered = json.dumps({**obligation, "start_accepted": False}, sort_keys=True).encode("utf-8")
    with pytest.raises(d0.D0Stop, match="job obligation is invalid"):
        d0._validate_archived_semantic_identity(
            record_kind="job_obligation",
            record_id="d0-schema-probe",
            raw=tampered,
            run_id="d0-schema-run",
        )


def test_receipt_writer_fails_when_no_host_publication_authority_is_installed():
    from container_evidence_fakes import ContainerFS

    from sag.agent.evidence_publications import (
        install_evidence_publication_authority,
        reset_evidence_publication_authority,
        unavailable_evidence_publication_authority,
    )
    from sag.agent.invocation_receipts import (
        HOST_PUBLICATION_FAILED,
        active_receipt_run_id,
        build_receipt,
        set_active_receipt_run_id,
        write_receipt_result,
    )

    run_id = "d0-no-authority"
    previous_run_id = active_receipt_run_id()
    set_active_receipt_run_id(run_id)
    receipt = build_receipt(
        receipt_id="inv-bash-run-no-authority-0001",
        tool="bash",
        requested_action="run",
        effective_action="run",
        argv="sh -c true",
        working_directory="/workspace",
        exit_code=0,
        before={},
        after={},
    )
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("unit test has no host sink", run_id=run_id)
    )
    try:
        result = write_receipt_result(ContainerFS(), receipt)
    finally:
        reset_evidence_publication_authority(token)
        set_active_receipt_run_id(previous_run_id)

    assert result.persisted is False
    assert result.code == HOST_PUBLICATION_FAILED


def test_strict_container_receipts_requires_named_bytes_and_host_publication(tmp_path):
    from container_evidence_fakes import ContainerFS

    from sag.agent.invocation_receipts import RECEIPT_DIR, build_receipt

    runtime = _epoch_runtime(tmp_path, probe="gradle-runner-classification")
    audit, epoch = _registered_epoch(runtime, "main")
    filesystem = ContainerFS()
    audit.orchestrator.filesystem = filesystem
    with runtime.evidence_epoch(audit):
        receipt = build_receipt(
            receipt_id="inv-bash-run-published-0001",
            tool="bash",
            requested_action="run",
            effective_action="run",
            argv="sh -c true",
            working_directory="/workspace",
            exit_code=0,
            before={},
            after={},
        )
    raw = json.dumps(receipt, sort_keys=True)
    path = f"{RECEIPT_DIR}/{receipt['receipt_id']}.json"
    filesystem.files[path] = raw
    epoch.authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id=receipt["receipt_id"],
        raw=raw.encode(),
    )

    assert d0._strict_container_receipts(audit) == [receipt]
    filesystem.files[path] = json.dumps({**receipt, "exit_code": 7}, sort_keys=True)
    with pytest.raises(d0.D0Error, match="not host-authorized"):
        d0._strict_container_receipts(audit)


def test_campaign_lock_pins_no_model_registry_image_source_and_stop_rules():
    lock = d0.build_campaign_lock(
        campaign_id="d0-20260808",
        facts=_facts(),
        selected=d0.select_probes(["sync-large-evidence"]),
    )

    assert lock["schema_version"] == d0.LOCK_SCHEMA_VERSION
    assert lock["model_pin"] == "none:no-model"
    assert lock["prompt_bundle_sha256"] == d0.NOT_APPLICABLE_SHA256
    assert len(lock["control_bundle_sha256"]) == 64
    assert lock["image"]["id"] == "sha256:" + "c" * 64
    assert lock["source"]["tree_sha256"] == "b" * 64
    assert lock["probes"][0]["run_order_index"] == 0
    assert lock["probe_registry_sha256"] == d0.canonical_sha256(d0.probe_registry_material())
    assert lock["stop_conditions"] == list(d0.STOP_CONDITIONS)


def test_existing_lock_must_match_every_comparability_pin(tmp_path):
    selected = d0.select_probes(["sync-large-evidence"])
    expected = d0.build_campaign_lock(campaign_id="same", facts=_facts(), selected=selected)
    path = tmp_path / "campaign-lock.json"
    path.write_text(d0.canonical_json(expected) + "\n", encoding="utf-8")

    assert d0.ensure_campaign_lock(path, expected) == expected

    drift = json.loads(json.dumps(expected))
    drift["image"]["id"] = "sha256:" + "e" * 64
    with pytest.raises(d0.D0Stop, match="campaign pin drift"):
        d0.ensure_campaign_lock(path, drift)


def test_new_lock_is_written_atomically_and_has_no_surviving_temp(tmp_path):
    selected = d0.select_probes(["sync-large-evidence"])
    expected = d0.build_campaign_lock(campaign_id="new", facts=_facts(), selected=selected)
    path = tmp_path / "campaign-lock.json"

    d0.ensure_campaign_lock(path, expected)

    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert list(tmp_path.glob("*.tmp")) == []


def test_source_pin_covers_python_and_prompt_yaml_content(tmp_path):
    source = tmp_path / "src" / "sag" / "config" / "prompts"
    source.mkdir(parents=True)
    (source / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
    prompt = source / "react_engine.yaml"
    prompt.write_text("system: one\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runner.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    before = d0._source_tree_digest(tmp_path)
    prompt.write_text("system: two\n", encoding="utf-8")
    after = d0._source_tree_digest(tmp_path)

    assert before != after


def test_source_pin_covers_d0_docker_fixture_content(tmp_path):
    fixture = tmp_path / "tests" / "fixtures" / "d0"
    fixture.mkdir(parents=True)
    dockerfile = fixture / "Dockerfile"
    dockerfile.write_text("FROM ubuntu:24.04\n", encoding="utf-8")

    before = d0._source_tree_digest(tmp_path)
    dockerfile.write_text("FROM ubuntu:24.04\nRUN true\n", encoding="utf-8")

    assert d0._source_tree_digest(tmp_path) != before


def test_prepare_image_uses_only_bounded_fixture_context_and_binds_source(tmp_path, monkeypatch):
    fixture = tmp_path / "tests" / "fixtures" / "d0"
    fixture.mkdir(parents=True)
    (fixture / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return d0.subprocess.CompletedProcess(command, 0, stdout="built", stderr="")

    source_digest = d0._source_tree_digest(tmp_path)
    inspect = json.dumps(
        [
            {
                "Id": "sha256:" + "f" * 64,
                "Config": {
                    "Labels": {
                        d0.PREPARED_IMAGE_LABEL: d0.PREPARED_IMAGE_VERSION,
                        d0.PREPARED_SOURCE_LABEL: source_digest,
                    }
                },
            }
        ]
    )
    monkeypatch.setattr(d0.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(d0, "_run", run)
    monkeypatch.setattr(d0, "_required_command", lambda *_args, **_kwargs: inspect)

    summary = d0.prepare_d0_image(
        tmp_path,
        parent_image="ubuntu@sha256:" + "a" * 64,
        target_image="d0:test",
    )

    assert summary["status"] == "prepared"
    build = commands[0]
    assert build[-1] == str(fixture)
    assert f"{d0.PREPARED_SOURCE_LABEL}={source_digest}" in build
    assert f"GRADLE_VERSION={d0.GRADLE_VERSION}" in build
    assert f"GRADLE_ZIP_SHA256={d0.GRADLE_ZIP_SHA256}" in build
    assert f"HTTP_REPOSITORY_URL={d0.HTTP_REPOSITORY_URL}" in build
    assert f"HTTP_REPOSITORY_REF={d0.HTTP_REPOSITORY_REF}" in build
    assert f"HTTP_TARGET_SHA={d0.HTTP_TARGET_SHA}" in build


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (
            d0.ProbeObservation(
                passed=True,
                exit_marker_job_ids=("job-1",),
                job_unsettled_ids=("job-1",),
            ),
            "exit marker exists",
        ),
        (
            d0.ProbeObservation(passed=True, max_command_chars=60_201),
            "60,200",
        ),
        (
            d0.ProbeObservation(
                passed=True, output_excerpt="exec /bin/bash: argument list too long"
            ),
            "argument list too long",
        ),
        (
            d0.ProbeObservation(passed=True, byte_hash_json_mismatch=True),
            "byte/hash/JSON",
        ),
        (
            d0.ProbeObservation(passed=True, temporary_files=("x.tmp",)),
            "temporary file",
        ),
        (
            d0.ProbeObservation(passed=True, wrapper_checksum_mutated=True),
            "checksum mutation",
        ),
        (
            d0.ProbeObservation(passed=True, jdk_authority_regressed=True),
            "JDK authority",
        ),
        (
            d0.ProbeObservation(passed=True, no_op_claim_count=4, no_op_claim_cap=3),
            "no-op cap",
        ),
        (
            d0.ProbeObservation(passed=True, cross_runner_false_failure=True),
            "another ecosystem",
        ),
        (
            d0.ProbeObservation(passed=True, duplicate_dispatches=("root/job",)),
            "dispatched twice",
        ),
        (
            d0.ProbeObservation(passed=True, evidence_grains_combined=True),
            "evidence grains",
        ),
        (
            d0.ProbeObservation(passed=True, unregistered_control_red=True),
            "unregistered red",
        ),
    ],
)
def test_stop_conditions_are_fail_closed(observation, message):
    with pytest.raises(d0.D0Stop, match=message):
        d0.enforce_stop_conditions(observation)


def test_probe_failure_is_itself_a_stop_condition():
    with pytest.raises(d0.D0Stop, match="probe assertion failed"):
        d0.enforce_stop_conditions(d0.ProbeObservation(passed=False, failures=("receipt missing",)))


def test_archive_tree_preserves_bytes_and_writes_a_complete_checksum_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.json").write_text('{"ok":true}\n', encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"\x00\xffpayload")
    destination = tmp_path / "archive"

    checksums = d0.archive_tree(source, destination)

    assert (destination / "a.json").read_bytes() == (source / "a.json").read_bytes()
    assert (destination / "nested" / "b.bin").read_bytes() == b"\x00\xffpayload"
    assert checksums == {
        "a.json": hashlib.sha256((source / "a.json").read_bytes()).hexdigest(),
        "nested/b.bin": hashlib.sha256(b"\x00\xffpayload").hexdigest(),
    }


def test_command_audit_retains_full_output_with_hash_and_stable_index():
    class Orchestrator:
        def execute_command(self, command, *_args, **_kwargs):
            assert command == "probe"
            return {
                "exit_code": 0,
                "success": True,
                "output": "bounded preview\n",
                "full_output": "full\noutput\n",
            }

        def execute_command_detached(self, command, *_args, **_kwargs):
            return {"started": True, "job_id": command, "output": "detached"}

        def poll_detached_command(self, handle, *_args, **_kwargs):
            return {"state": "terminal", "output": handle["job_id"]}

        def collect_detached_result(self, handle, poll, *_args, **_kwargs):
            return {"exit_code": 0, "output": handle["job_id"] + poll["state"]}

    audit = d0.CommandAudit(Orchestrator())

    result = audit.execute_command("probe")

    assert result["output"] == "bounded preview\n"
    assert audit.records[0]["command_index"] == 0
    output_ref = audit.records[0]["output_ref"].removeprefix("sha256:")
    full_output_ref = audit.records[0]["full_output_ref"].removeprefix("sha256:")
    assert audit.blobs[output_ref] == b"bounded preview\n"
    assert audit.blobs[full_output_ref] == b"full\noutput\n"
    assert audit.output_excerpt == "full\noutput\n"


def test_command_audit_intercepts_all_detached_apis_and_nested_orchestrator_calls():
    class Orchestrator:
        def execute_command(self, command, *_args, **_kwargs):
            return {"exit_code": 0, "success": True, "output": command}

        def execute_command_detached(self, command, *_args, **_kwargs):
            nested = self.execute_command("launcher:" + command)
            return {"started": True, "job_id": "j1", "output": nested["output"]}

        def poll_detached_command(self, handle, *_args, **_kwargs):
            return {"state": "terminal", "output": handle["job_id"]}

        def collect_detached_result(self, handle, poll, *_args, **_kwargs):
            return {"exit_code": 0, "output": handle["job_id"] + poll["state"]}

    orchestrator = Orchestrator()
    audit = d0.CommandAudit(orchestrator)

    handle = orchestrator.execute_command_detached("work")
    poll = orchestrator.poll_detached_command(handle)
    result = orchestrator.collect_detached_result(handle, poll)

    assert result["output"] == "j1terminal"
    assert {row["kind"] for row in audit.records} == {
        "execute_command",
        "execute_command_detached",
        "poll_detached_command",
        "collect_detached_result",
    }
    assert all(row["command_ref"].startswith("sha256:") for row in audit.records)
    assert all(row["output_ref"].startswith("sha256:") for row in audit.records)


def _controller_runtime(tmp_path, probe_name="http-wrapper-unzip-ablation"):
    runtime = _epoch_runtime(tmp_path, probe=probe_name)
    _registered_epoch(runtime, "main")
    return runtime


def test_controller_action_scope_emits_typed_envelope_and_restores_context(tmp_path):
    from sag.agent.invocation_contracts import current_action_context

    runtime = _controller_runtime(tmp_path)
    assert current_action_context().envelope_id is None

    with d0._controller_action_scope(
        runtime,
        label="control-validate",
        domain_id="d0-http-control",
        tool="maven",
        params={
            "command": "validate",
            "working_directory": "/workspace/httpcomponents-client",
            "timeout": 180,
        },
        next_action_kind="maven",
        predecessor_contract_id="ic-000000000001",
    ) as binding:
        current = current_action_context()
        assert binding.intent_source == "controller"
        assert current.envelope_id == binding.envelope_id
        assert current.intent_id == binding.intent_id
        assert current.action_fingerprint == binding.action_fingerprint
        assert current.predecessor_contract_id == "ic-000000000001"

    assert current_action_context().envelope_id is None
    events = d0._control_events(runtime.control_event_path)
    # A control stream is a grammar: production's strict recovery refuses a
    # stream that ends on an unanswered envelope, so the scope answers its own.
    assert [event["kind"] for event in events] == [
        "evidence_store_bound",
        "action_envelope",
        "tool_result",
    ]
    payload = events[1]["payload"]
    assert payload["envelope_id"] == binding.envelope_id
    assert payload["intent_id"] == binding.intent_id
    assert payload["intent_source"] == "controller"
    assert payload["tool"] == "maven"
    assert payload["exact_params"]["command"] == "validate"
    answer = events[2]["payload"]
    assert answer["envelope_id"] == binding.envelope_id
    assert answer["tool"] == payload["tool"]
    assert answer["params"] == payload["exact_params"]
    assert answer["result"]["invocation_status"] == "completed"


def test_controller_action_scope_answers_its_envelope_even_when_the_body_raises(tmp_path):
    runtime = _controller_runtime(tmp_path)

    with pytest.raises(RuntimeError, match="probe body exploded"):
        with d0._controller_action_scope(
            runtime,
            label="raising-body",
            domain_id="d0-http-control",
            tool="maven",
            params={"command": "validate", "working_directory": "/workspace/x"},
            next_action_kind="maven",
        ):
            raise RuntimeError("probe body exploded")

    events = d0._control_events(runtime.control_event_path)
    assert [event["kind"] for event in events][-2:] == ["action_envelope", "tool_result"]
    assert events[-1]["payload"]["result"]["invocation_status"] == "crashed"
    assert events[-1]["payload"]["envelope_id"] == events[-2]["payload"]["envelope_id"]


def test_live_control_stream_survives_production_strict_recovery(tmp_path):
    """The engine reads the live stream through this exact recovery path."""

    from sag.agent.replay import recover_active_repair_context_from_path

    runtime = _controller_runtime(tmp_path)
    with d0._controller_action_scope(
        runtime,
        label="recovered",
        domain_id="d0-http-control",
        tool="maven",
        params={"command": "validate", "working_directory": "/workspace/x"},
        next_action_kind="maven",
    ):
        pass

    state = recover_active_repair_context_from_path(runtime.control_event_path)
    assert state.context is None


def test_controller_dispatch_scope_freezes_before_body_and_fails_closed(tmp_path, monkeypatch):
    from sag.agent import invocation_contracts

    runtime = _controller_runtime(tmp_path, "detached-large-settlement")
    order = []

    def fake_freeze(_execute, **kwargs):
        assert d0._control_events(runtime.control_event_path)[-1]["kind"] == "action_envelope"
        order.append(("freeze", kwargs["envelope_id"], kwargs["expected_argv"]))
        return invocation_contracts.build_contract(
            run_id=kwargs["run_id"],
            envelope_id=kwargs["envelope_id"],
            tool=kwargs["tool"],
            params=kwargs["params"],
            effective_tool=kwargs["effective_tool"],
            effective_action=kwargs["effective_action"],
            expected_cwd=kwargs["expected_cwd"],
            expected_argv=kwargs["expected_argv"],
            execution_binding=kwargs["execution_binding"],
            intent_source=kwargs["intent_source"],
            intent_id=kwargs["intent_id"],
            intent_domain_id=kwargs["intent_domain_id"],
            intent_exact_params=kwargs["intent_exact_params"],
            action_fingerprint=kwargs["action_fingerprint"],
            trigger_assessment_id=kwargs.get("trigger_assessment_id"),
            repair_context_id=kwargs.get("repair_context_id"),
            repair_context_sha256=kwargs.get("repair_context_sha256"),
            predecessor_contract_id=kwargs.get("predecessor_contract_id"),
        )

    monkeypatch.setattr(invocation_contracts, "freeze_contract", fake_freeze)
    execute = runtime.audits[0].execute_command
    with d0._controller_dispatch_scope(
        runtime,
        execute,
        label="large-job",
        domain_id="d0-large",
        tool="bash",
        params={"command": "python3 -c 'raise SystemExit(7)'"},
        next_action_kind="bash",
        effective_action="run",
        expected_cwd="/workspace/d0",
        expected_argv="-c 'raise SystemExit(7)'",
    ) as binding:
        order.append(("body", binding.contract["contract_id"]))
        assert invocation_contracts.current_contract() == binding.contract

    assert order[0][0] == "freeze"
    assert order[1] == ("body", binding.contract["contract_id"])
    assert re.fullmatch(r"ic-[0-9a-f]{12}", binding.contract["contract_id"])
    assert invocation_contracts.current_contract() is None

    monkeypatch.setattr(invocation_contracts, "freeze_contract", lambda *_args, **_kwargs: None)
    with pytest.raises(d0.D0Stop, match="contract.*persist"):
        with d0._controller_dispatch_scope(
            runtime,
            execute,
            label="large-job-refused",
            domain_id="d0-large",
            tool="bash",
            params={"command": "exit 7"},
            next_action_kind="bash",
            effective_action="run",
            expected_cwd="/workspace/d0",
            expected_argv="7",
        ):
            pytest.fail("a dispatch body ran without a frozen contract")


def test_dispatch_count_uses_exact_durable_receipts_not_wrapper_command_mentions():
    exact = {
        "schema_version": 3,
        "receipt_id": "inv-maven-1",
        "tool": "maven",
        "working_directory": "/workspace/httpcomponents-client",
        "actual_cwd": "/workspace/httpcomponents-client",
        "effective_action": "validate",
        "exit_code": 0,
        "argv": "/workspace/httpcomponents-client/mvnw validate",
    }
    records = [
        exact,
        {**exact, "receipt_id": "", "argv": "mvnw --version"},
        {**exact, "receipt_id": "wrong-action", "effective_action": "compile"},
        {**exact, "receipt_id": "wrong-root", "actual_cwd": "/workspace/other"},
        {**exact, "receipt_id": "wrong-tool", "tool": "gradle"},
        {**exact, "receipt_id": "legacy", "schema_version": 1},
        {**exact, "receipt_id": "bool-exit", "exit_code": True},
    ]

    selected = d0.dispatch_receipts(
        records,
        tool="maven",
        working_directory="/workspace/httpcomponents-client/",
        effective_action="validate",
    )

    assert selected == [exact]


def test_direct_runner_contract_vector_excludes_only_the_physical_executable():
    command = "python3 -c 'raise SystemExit(7)'"

    assert d0._runner_argument_vector(command) == "-c 'raise SystemExit(7)'"
    assert d0._runner_argument_vector("true") is None


def test_progress_count_uses_changed_complete_physical_probe_responses():
    command = "set +e; tmp=$(mktemp -d /tmp/sag-job-progress.XXXXXX)"

    def response(job_id, *, log_size, artifact, complete="1"):
        return "\n".join(
            (
                f"JOB_ID:{job_id}",
                "PGID:42",
                "PROCESS_STATE:running",
                f"LOG_SIZE:{log_size}",
                "CPU_TICKS_DELTA:0",
                "PROCESS_COUNT:2",
                "CHILD_COUNT:1",
                f"IDENTITY_COMPLETE:{complete}",
                f"ARTIFACT_COMPLETE:{complete}",
                f"REPORT_COMPLETE:{complete}",
                "PROCESS_IDENTITY_SHA256:" + "a" * 64,
                f"ARTIFACT_SHA256:{artifact}",
                "REPORT_SHA256:" + "c" * 64,
            )
        )

    pairs = [
        (command, response("j1", log_size=1, artifact="b" * 64)),
        (command, response("j1", log_size=1, artifact="b" * 64)),
        (command, response("j1", log_size=2, artifact="d" * 64)),
        (command, response("j1", log_size=3, artifact="e" * 64)),
        (command, response("j1", log_size=4, artifact="f" * 64, complete="0")),
        ("echo JOB_ID:j1", response("j1", log_size=5, artifact="0" * 64)),
    ]

    assert d0.progress_response_counts(pairs, job_ids=("j1", "j2")) == {
        "j1": 2,
        "j2": 0,
    }


def test_terminal_gate_rejects_any_competing_lifecycle_or_integrity_state():
    base_facts = {
        "job_id": "job-1",
        "replay_event_kinds": ["job_terminal_observed", "job_terminal_unpersisted"],
        "model_turns": 0,
        "replay_external_calls": 0,
        "replay_unconsumed_events": [],
        "replay_event_digest": "a" * 64,
        "replay_expected_event_digest": "a" * 64,
        "replay_conflicts": ["job_terminal_unpersisted:job-1"],
    }
    for forbidden in sorted(d0.TERMINAL_REPLAY_FORBIDDEN_KINDS):
        events = [
            {"kind": "job_terminal_observed", "payload": {"job_id": "job-1"}},
            {"kind": "job_terminal_unpersisted", "payload": {"job_id": "job-1"}},
            {"kind": forbidden, "payload": {"job_id": "job-1"}},
        ]

        errors = d0.semantic_probe_evidence_errors(
            d0.PROBE_SPECS["terminal-receipt-failure"], base_facts, events
        )

        assert any("contradictory lifecycle" in error for error in errors), forbidden


def test_terminal_gate_rejects_reversed_observed_unpersisted_order():
    facts = {
        "job_id": "job-1",
        "replay_event_kinds": ["job_terminal_observed", "job_terminal_unpersisted"],
        "model_turns": 0,
        "replay_external_calls": 0,
        "replay_unconsumed_events": [],
        "replay_event_digest": "a" * 64,
        "replay_expected_event_digest": "a" * 64,
        "replay_conflicts": ["job_terminal_unpersisted:job-1"],
    }
    events = [
        {"kind": "job_terminal_unpersisted", "payload": {"job_id": "job-1"}},
        {"kind": "job_terminal_observed", "payload": {"job_id": "job-1"}},
    ]

    errors = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["terminal-receipt-failure"], facts, events
    )

    assert any("lifecycle was not observed then" in error for error in errors)


@pytest.mark.parametrize("probe_name", tuple(d0.PROBE_SPECS))
def test_each_probe_fails_required_evidence_gate_without_its_public_entrypoint(probe_name):
    spec = d0.PROBE_SPECS[probe_name]
    facts = {key: {} for key in d0.PROBE_REQUIRED_FACTS[probe_name]}
    facts["production_entrypoints"] = []

    errors = d0.required_probe_evidence_errors(spec, facts)

    assert any("production entrypoint evidence absent" in error for error in errors)


def test_probe_implementations_name_public_paths_and_not_private_wrapper_chooser():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "MavenTool(control).execute(" in source
    assert "BuildTool(audit, maven_tool=MavenTool(audit))" in source
    assert "BuildTool(" in source and "gradle_tool=GradleTool(audit)" in source
    assert source.count("ControlReplayRunner.offline(") == 2
    assert "ReportTool(audit).finalize_metrics_v2(snapshot)" in source
    assert "evaluate_v2_campaign([persisted_metrics])" in source
    assert source.count("engine.run_react_loop(") == 2
    assert "._choose_maven_runner(" not in source
    assert "ReActEngine.__new__" not in source
    assert "._drain_job_barrier(" not in source
    assert "classify_detached_completion(" not in source
    assert "audit.orchestrator.execute_command_detached(" not in source
    assert "HTTP_WRAPPER_SCRIPT" not in source
    assert "gradlew" not in source


def test_http_fixture_uses_real_pinned_checkout_and_hides_compatible_maven():
    dockerfile = (SCRIPT.parents[1] / "tests" / "fixtures" / "d0" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'git clone --depth 1 --branch "${HTTP_REPOSITORY_REF}"' in dockerfile
    assert 'rev-parse HEAD)" = "${HTTP_TARGET_SHA}"' in dockerfile
    assert "/opt/d0/httpcomponents-client/mvnw --version" in dockerfile
    assert 'mv "/opt/d0/apache-maven-${MAVEN_VERSION}" /opt/d0/jdk-maven' in dockerfile
    assert "/opt/d0/jdk-maven/bin/mvn" in dockerfile
    assert "gradle-${GRADLE_VERSION}-bin.zip" in dockerfile
    assert "${GRADLE_ZIP_SHA256}" in dockerfile
    assert "/opt/d0/gradle-${GRADLE_VERSION}/bin/gradle" in dockerfile
    assert "cp -a /opt/d0/httpcomponents-client" in source
    assert d0.HTTP_TARGET_SHA == "4f86ca6a5eb528613edb892a4f7161e23dce15d7"
    assert d0.GRADLE_ZIP_SHA256 == (
        "5b9c5eb3f9fc2c94abaea57d90bd78747ca117ddbbf96c859d3741181a12bf2a"
    )


def test_small_real_row_flows_reader_projection_and_public_evaluator():
    from container_evidence_fakes import strict_published_evidence

    from sag.agent.invocation_receipts import build_receipt
    from sag.agent.receipt_test_rows import testcase_execution_id
    from sag.tools.report_tool import ReportTool
    from scripts.evaluate_golden_battery import evaluate_v2_campaign

    run_id = "run-pytest"
    report = "/workspace/proj/target/surefire-reports/TEST-d0.xml"
    row = {
        "run_id": run_id,
        "receipt_id": "inv-maven-test-0001",
        "execution_index": 1,
        "execution_ordinal": 1,
        "target_sha": "a" * 40,
        "domain_id": "/workspace/proj",
        "module_coordinate": ".",
        "framework": "junit-xml",
        "owner": "d0.ExactTest",
        "test_name": "works",
        "parameter_id": None,
        "outcome": "passed",
        "report_path": report,
        "report_sha256": "e" * 64,
        "disposition": "claimed",
        "qualifying_invocation": True,
    }
    row["execution_id"] = testcase_execution_id(row)
    receipt = build_receipt(
        receipt_id="inv-maven-test-0001",
        run_id=run_id,
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={report: "e" * 64},
        target_sha="a" * 40,
        domain_id="/workspace/proj",
        testcase_execution_rows={
            "schema_version": 2,
            "status": "complete",
            "report_count": 1,
            "rows": [row],
        },
    )

    class MetricsOrchestrator:
        def __init__(self):
            self.filesystem = None
            self.receipt_reads = 0

        def evidence_store_identity(self):
            return "test-d0-store:report-metrics-run-pytest"

        def _execute(self, command):
            if "SAG_NAMED_JSON_RECORD_V1" in command and "invocation_receipts" in command:
                self.receipt_reads += 1
            assert self.filesystem is not None
            return self.filesystem(command)

        def execute_command(self, command, **_kwargs):
            return self._execute(command)

        def execute_control_command(self, command, **_kwargs):
            return self._execute(command)

    orchestrator = MetricsOrchestrator()
    orchestrator.filesystem = strict_published_evidence(
        orchestrator,
        run_id=run_id,
        target_sha="a" * 40,
        receipts=[receipt],
    )
    orchestrator.files = orchestrator.filesystem.files
    metrics = ReportTool(orchestrator).finalize_metrics_v2(
        {
            "verdict": "partial",
            "finalized_at": "2026-08-08T12:00:00Z",
            "build_evidence": {
                "observed": True,
                "judgment": "success",
                "domain_states": {"/workspace/proj": {"state": "success"}},
            },
            "test_stats": {"judgment": "success"},
            "conflicts": [],
            "input_refs": ["inv-maven-test-0001"],
        }
    )
    evaluated = evaluate_v2_campaign([metrics])

    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] == 1
    assert evaluated["diagnostic_totals"]["claimed"]["receipt_executions"]["executed"] == 1
    assert orchestrator.receipt_reads == 1


def test_public_controller_factory_constructs_the_normal_engine_without_provider_setup(tmp_path):
    from sag.agent.control_events import ControlEventSink
    from sag.agent.loop_memory import LoopMemory
    from sag.agent.react_engine import ReActEngine

    class Orchestrator:
        def execute_command(self, _command, **_kwargs):
            return {"exit_code": 0, "success": True, "output": ""}

    class TripwireClient:
        def setup(self):
            raise AssertionError("injected no-model client must not run provider setup")

    orchestrator = Orchestrator()
    client = TripwireClient()
    memory = LoopMemory()
    engine = ReActEngine.for_controller_loop(
        orchestrator=orchestrator,
        llm_client=client,
        control_event_sink=ControlEventSink(tmp_path / "control.jsonl"),
        run_id="d0-controller-factory",
        loop_memory=memory,
        max_wall_clock_seconds=90,
        dispatch_stall_seconds=3,
        obligation_poll_seconds=0.25,
    )

    assert engine.context_manager.orchestrator is orchestrator
    assert engine.orchestrator is orchestrator
    assert engine.llm_client is client
    assert engine.loop_memory is memory
    assert engine.transition_policy.repair_guard is memory
    assert engine.run_evidence_state.run_id == "d0-controller-factory"
    assert engine.config.max_wall_clock_seconds == 90
    assert engine.config.dispatch_stall_seconds == 3
    assert engine._OBLIGATION_POLL_SECONDS == 0.25


def test_terminal_semantic_gate_rejects_a_missing_exactly_once_event():
    facts = {
        "replay_event_kinds": ["job_terminal_unpersisted"],
        "model_turns": 0,
    }

    errors = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["terminal-receipt-failure"], facts, []
    )

    assert any("exactly one terminal" in error for error in errors)


def test_http_semantic_gate_rejects_fewer_than_three_maven_proper_repetitions():
    facts = {
        "control": {
            "error_code": "prerequisite_executable_missing:unzip",
            "runner_dispatched": False,
        },
        "treatment_repetitions": [
            {"maven_proper": True, "checksum_changed": False},
            {"maven_proper": True, "checksum_changed": False},
        ],
    }

    errors = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["http-wrapper-unzip-ablation"], facts, []
    )

    assert any("three HTTP" in error for error in errors)


def test_http_semantic_gate_keeps_control_contract_out_of_three_treatment_receipts():
    facts = {
        "control": {
            "error_code": "prerequisite_executable_missing:unzip",
            "runner_dispatched": False,
            "contract_id": "control-contract",
        },
        "treatment_repetitions": [
            {
                "contract_id": f"treatment-{index}",
                "maven_proper": True,
                "checksum_changed": False,
            }
            for index in range(3)
        ],
        "dispatch_receipt_ids": [f"receipt-{index}" for index in range(3)],
        "registered_treatment_mutation": "ln -s /opt/d0/unzip /usr/bin/unzip",
        "controller_lineage": {
            "intent_ids": [f"intent-{index}" for index in range(4)],
            "envelope_ids": [f"envelope-{index}" for index in range(4)],
            "contract_ids": ["control-contract"] + [f"treatment-{index}" for index in range(3)],
            "receipt_contract_ids": [f"treatment-{index}" for index in range(3)],
        },
    }

    accepted = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["http-wrapper-unzip-ablation"], facts, []
    )
    facts["controller_lineage"]["receipt_contract_ids"][0] = "control-contract"
    rejected = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["http-wrapper-unzip-ablation"], facts, []
    )

    assert not any("pre-dispatch refusal" in error for error in accepted)
    assert any("pre-dispatch refusal" in error for error in rejected)


def test_jdk_semantic_gate_rejects_an_unbounded_or_nonpersistent_repair():
    facts = {
        "first_call": {
            "succeeded": True,
            "jdk_retry": {"from": "11", "to": "17"},
            "physical_dispatches": 3,
        },
        "later_call": {
            "succeeded": True,
            "physical_dispatches": 1,
            "effective_major": "11",
            "requirement_authority": "static_survey",
        },
        "runtime_requirement": {"required_major": "17"},
    }

    errors = d0.semantic_probe_evidence_errors(d0.PROBE_SPECS["dynamic-jdk-authority"], facts, [])

    assert any("bounded to one retry" in error for error in errors)
    assert any("retain dynamic Java 17" in error for error in errors)
    assert any("controller contract chain" in error for error in errors)


def test_gradle_semantic_gate_rejects_foreign_runner_classification():
    errors = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["gradle-runner-classification"],
        {"physical_exit_code": 0, "operation_outcome": "failed", "runner": "maven"},
        [],
    )

    assert any("foreign runner" in error for error in errors)


def test_barrier_semantic_gate_rejects_missing_progress_before_model_turn():
    facts = {
        "job_ids": ["j1", "j2"],
        "sentinel_turns": 1,
        "intervening_model_turns": 0,
        "model_ledger_states": [{"j1": "terminal/settled", "j2": "terminal/settled"}],
        "progress_by_job": {"j1": 2, "j2": 1},
        "signal_commands": 0,
    }
    events = [
        {"kind": "job_settled", "payload": {"job_id": "j1"}},
        {"kind": "job_settled", "payload": {"job_id": "j2"}},
    ]

    errors = d0.semantic_probe_evidence_errors(
        d0.PROBE_SPECS["multi-job-progress-barrier"], facts, events
    )

    assert any("multiple barrier samples" in error for error in errors)


def test_sync_large_semantic_gate_requires_explicit_synthetic_non_claimable_disposition():
    spec = d0.PROBE_SPECS["sync-large-evidence"]
    observation = d0.ProbeObservation(passed=True)

    missing = d0.semantic_probe_evidence_errors(spec, {}, [])
    accepted = d0.semantic_probe_evidence_errors(
        spec,
        {
            "evidence_disposition": "synthetic_non_claimable",
            "physical_dispatches": 0,
        },
        [],
    )

    assert any("synthetic non-claimable" in error for error in missing)
    assert not any("synthetic non-claimable" in error for error in accepted)
    assert d0.probe_evaluator_disposition(spec, observation) == "synthetic_non_claimable"
    assert d0.probe_evaluator_grain(spec) == "synthetic-transport-fixture"


@pytest.mark.parametrize(
    ("probe_name", "expected"),
    sorted(d0.PROBE_CONTROLLER_LINEAGE_COUNTS.items()),
)
def test_physical_probe_semantic_gate_requires_exact_controller_lineage_counts(
    probe_name, expected
):
    actions, contracts, receipts = expected
    facts = {
        "controller_lineage": {
            "intent_ids": [f"intent-{index}" for index in range(actions)],
            "envelope_ids": [f"envelope-{index}" for index in range(actions)],
            "contract_ids": [f"contract-{index}" for index in range(contracts)],
            "receipt_contract_ids": [f"contract-{index}" for index in range(receipts)],
        }
    }

    accepted = d0.semantic_probe_evidence_errors(d0.PROBE_SPECS[probe_name], facts, [])
    facts["controller_lineage"]["contract_ids"] = []
    rejected = d0.semantic_probe_evidence_errors(d0.PROBE_SPECS[probe_name], facts, [])

    assert not any("lineage counts" in error for error in accepted)
    assert any("lineage counts" in error for error in rejected)


def _archived_controller_chain(tmp_path):
    from sag.agent.action_intents import EngineActionIntentFactory
    from sag.agent.control_events import (
        ActionEnvelopePayload,
        ControlEvent,
        action_envelope_sha256,
    )
    from sag.agent.invocation_contracts import build_contract

    artifact = tmp_path / "gradle-archive"
    contract_dir = artifact / "container-1" / ".setup_agent" / "invocation_contracts"
    receipt_dir = artifact / "container-1" / ".setup_agent" / "invocation_receipts"
    contract_dir.mkdir(parents=True)
    receipt_dir.mkdir(parents=True)
    tool_call_id = "d0-gradle-controller-1"
    params = {
        "action": "test",
        "working_directory": "/workspace/d0-gradle",
        "timeout": 180,
    }
    intent = EngineActionIntentFactory.for_controller().from_submission(
        {
            "domain_id": "d0-gradle",
            "tool": "build",
            "params": params,
            "next_action_kind": "test",
        },
        tool_call_id=tool_call_id,
    )
    envelope_id = "d0-envelope-gradle-1"
    payload = ActionEnvelopePayload(
        envelope_id=envelope_id,
        tool_call_id=tool_call_id,
        tool="build",
        exact_params=params,
        intent_id=intent.intent_id,
        intent_source=intent.source,
        action_fingerprint=intent.action_fingerprint,
        envelope_sha256=action_envelope_sha256(
            tool_call_id=tool_call_id,
            tool="build",
            exact_params=params,
            intent_id=intent.intent_id,
            intent_source=intent.source,
            action_fingerprint=intent.action_fingerprint,
        ),
    )
    event = ControlEvent(
        sequence=1,
        kind="action_envelope",
        payload=payload.model_dump(mode="json"),
    ).model_dump(mode="json")
    contract = build_contract(
        run_id="run-d0-gradle-archive",
        envelope_id=envelope_id,
        tool="build",
        params=params,
        effective_tool="gradle",
        effective_action="test",
        expected_cwd="/workspace/d0-gradle",
        expected_argv="test",
        execution_binding="argv_v1",
        intent_source=intent.source,
        intent_id=intent.intent_id,
        intent_domain_id="d0-gradle",
        intent_exact_params=params,
        action_fingerprint=intent.action_fingerprint,
    )
    contract_path = contract_dir / f"{contract['contract_id']}.json"
    contract_path.write_text(d0.canonical_json(contract) + "\n", encoding="utf-8")
    receipt_path = receipt_dir / "inv-gradle-1.json"
    receipt_path.write_text(
        d0.canonical_json(
            {
                "schema_version": 3,
                "receipt_id": "inv-gradle-1",
                "contract_id": contract["contract_id"],
                "contract_hash": contract["contract_hash"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact, event, contract, contract_path


def test_archive_contract_lineage_audit_rejects_missing_contract_intent_or_envelope(tmp_path):
    from sag.agent.invocation_contracts import contract_hash

    artifact, event, contract, contract_path = _archived_controller_chain(tmp_path)
    spec = d0.PROBE_SPECS["gradle-runner-classification"]

    assert d0._archived_contract_lineage_errors(artifact, spec, [event]) == []

    missing_intent_contract = dict(contract)
    missing_intent_contract.pop("intent_id")
    missing_intent_contract["contract_hash"] = contract_hash(missing_intent_contract)
    contract_path.write_text(d0.canonical_json(missing_intent_contract) + "\n", encoding="utf-8")
    missing_intent = d0._archived_contract_lineage_errors(artifact, spec, [event])
    assert any("complete controller intent" in error for error in missing_intent)
    contract_path.write_text(d0.canonical_json(contract) + "\n", encoding="utf-8")

    missing_envelope = d0._archived_contract_lineage_errors(artifact, spec, [])
    assert any("action envelope" in error for error in missing_envelope)

    contract_path.unlink()
    missing_contract = d0._archived_contract_lineage_errors(artifact, spec, [event])
    assert any("frozen invocation contract" in error for error in missing_contract)
    assert any(contract["contract_id"] in error for error in missing_contract)


def _patch_campaign_archive(monkeypatch):
    def archive(runtime, observation):
        destination = runtime.artifact_dir
        destination.mkdir(parents=True)
        d0._atomic_json(destination / "result.json", observation.to_json())
        d0._atomic_json(destination / "stop-audit.json", {"status": "passed"})
        d0._atomic_json(destination / "checksums.json", {})
        d0._atomic_json(destination / "seal.json", {"status": "sealed"})
        return {}

    monkeypatch.setattr(d0.DockerProbeRuntime, "archive", archive)
    monkeypatch.setattr(d0.DockerProbeRuntime, "verify_archive_seal", lambda _runtime: None)
    monkeypatch.setattr(d0.DockerProbeRuntime, "cleanup", lambda _runtime: None)


def _sealed_gradle_runtime(tmp_path, monkeypatch, *, fail_first_cp=False):
    image_id = "sha256:" + "c" * 64

    class Orchestrator:
        container_name = "sag-d0-archive"

        def execute_command(self, _command, *_args, **_kwargs):
            return {
                "exit_code": 0,
                "success": True,
                "output": "> Task :checkKotlinGradlePluginConfigurationErrors\nBUILD SUCCESSFUL\n",
            }

        def execute_command_detached(self, command, *_args, **_kwargs):
            return {"started": True, "job_id": command, "output": ""}

        def poll_detached_command(self, handle, *_args, **_kwargs):
            return {"state": "terminal", "output": handle["job_id"]}

        def collect_detached_result(self, handle, poll, *_args, **_kwargs):
            return {"exit_code": 0, "output": handle["job_id"] + poll["state"]}

        def remove_project(self):
            return True

    audit = d0.CommandAudit(Orchestrator())
    audit.execute_command("gradle-shaped")
    run_pin = {
        "image": {"id": image_id},
        "model_pin": "none:no-model",
        "prompt_bundle_sha256": d0.NOT_APPLICABLE_SHA256,
    }
    runtime = d0.DockerProbeRuntime(
        repo=Path(__file__).parents[1],
        campaign_dir=tmp_path / "campaign",
        campaign_id="archive",
        base_image=d0.DEFAULT_PREPARED_IMAGE,
        spec=d0.PROBE_SPECS["gradle-runner-classification"],
        run_pin=run_pin,
        keep_containers=False,
        audits=[audit],
    )
    cp_calls = 0

    def fake_run(command, **_kwargs):
        nonlocal cp_calls
        if command[:2] == ["docker", "inspect"]:
            return d0.subprocess.CompletedProcess(
                command, 0, stdout=json.dumps([{"Image": image_id}]), stderr=""
            )
        if command[:2] == ["docker", "logs"]:
            return d0.subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["docker", "cp"]:
            cp_calls += 1
            if fail_first_cp and cp_calls == 1:
                return d0.subprocess.CompletedProcess(command, 1, stdout="", stderr="cp failed")
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            return d0.subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(d0, "_run", fake_run)
    observation = d0.ProbeObservation(
        passed=True,
        facts={
            "physical_exit_code": 0,
            "operation_outcome": "success",
            "runner": "gradle",
            "dispatch_receipt_ids": ["inv-gradle-test-0001"],
            "production_entrypoints": ["BuildTool.execute", "GradleTool.execute"],
        },
    )
    return runtime, observation


def test_archive_is_content_addressed_sealed_and_verifiable(tmp_path, monkeypatch):
    runtime, observation = _sealed_gradle_runtime(tmp_path, monkeypatch)

    runtime.archive(observation)

    assert (runtime.artifact_dir / "seal.json").is_file()
    assert (runtime.artifact_dir / "evaluator-row.json").is_file()
    assert (runtime.artifact_dir / "control-events.jsonl").is_file()
    assert list(runtime.artifact_dir.glob("container-*/command-blobs/*"))
    runtime.verify_archive_seal()


def test_archive_stop_audit_fails_closed_when_physical_probe_has_no_lineage(tmp_path, monkeypatch):
    runtime, observation = _sealed_gradle_runtime(tmp_path, monkeypatch)

    runtime.archive(observation)

    stop_audit = json.loads((runtime.artifact_dir / "stop-audit.json").read_text(encoding="utf-8"))
    assert stop_audit["status"] == "failed"
    assert any("frozen invocation contract" in reason for reason in stop_audit["reasons"])
    with pytest.raises(d0.D0Stop, match="frozen invocation contract"):
        d0.enforce_stop_audit(stop_audit)


def test_archive_half_failure_has_no_final_seal_and_can_retry_from_retained_container(
    tmp_path, monkeypatch
):
    runtime, observation = _sealed_gradle_runtime(tmp_path, monkeypatch, fail_first_cp=True)

    with pytest.raises(d0.D0Error, match="cannot archive"):
        runtime.archive(observation)

    assert not runtime.artifact_dir.exists()
    assert list((runtime.campaign_dir / ".archive-failures").glob("*.json"))

    runtime.archive(observation)
    assert (runtime.artifact_dir / "seal.json").is_file()


def test_dry_validation_never_creates_or_executes_a_probe(tmp_path):
    calls = []

    summary = d0.run_campaign(
        repo=Path(__file__).parents[1],
        campaign_dir=tmp_path / "campaign",
        campaign_id="dry",
        base_image="ubuntu:24.04",
        selected=d0.select_probes(["sync-large-evidence"]),
        dry_run=True,
        keep_containers=False,
        fact_loader=lambda *_args, **_kwargs: _facts(),
        probe_executor=lambda *_args, **_kwargs: calls.append("executed"),
    )

    assert calls == []
    assert summary["status"] == "dry-valid"
    assert summary["probes"] == ["sync-large-evidence"]
    assert (tmp_path / "campaign" / "campaign-lock.json").is_file()


def test_campaign_stops_after_first_failure_and_preserves_cause_report(tmp_path, monkeypatch):
    _patch_campaign_archive(monkeypatch)
    calls = []

    def execute(_runtime, spec, _run_pin):
        calls.append(spec.name)
        if spec.name == "detached-large-settlement":
            return d0.ProbeObservation(passed=False, failures=("settlement missing",))
        return d0.ProbeObservation(passed=True)

    with pytest.raises(d0.D0Stop, match="probe assertion failed"):
        d0.run_campaign(
            repo=Path(__file__).parents[1],
            campaign_dir=tmp_path / "campaign",
            campaign_id="fail-fast",
            base_image="ubuntu:24.04",
            selected=d0.select_probes(
                [
                    "sync-large-evidence",
                    "detached-large-settlement",
                    "dynamic-jdk-authority",
                ]
            ),
            dry_run=False,
            keep_containers=False,
            fact_loader=lambda *_args, **_kwargs: _facts(),
            probe_executor=execute,
        )

    assert calls == ["sync-large-evidence", "detached-large-settlement"]
    cause = json.loads((tmp_path / "campaign" / "cause-report.json").read_text(encoding="utf-8"))
    assert cause["failed_probe"] == "detached-large-settlement"
    assert cause["completed_probes"] == ["sync-large-evidence"]


def test_campaign_revalidates_pins_before_every_probe(tmp_path, monkeypatch):
    _patch_campaign_archive(monkeypatch)
    baseline = _facts()
    drifted = d0.HostFacts(
        **{
            **baseline.__dict__,
            "source_tree_sha256": "f" * 64,
        }
    )
    observations = iter((baseline, baseline, drifted))
    calls = []

    with pytest.raises(d0.D0Stop, match="campaign pin drift before probe dispatch"):
        d0.run_campaign(
            repo=Path(__file__).parents[1],
            campaign_dir=tmp_path / "campaign",
            campaign_id="pin-drift",
            base_image="ubuntu:24.04",
            selected=d0.select_probes(["sync-large-evidence", "dynamic-jdk-authority"]),
            dry_run=False,
            keep_containers=False,
            fact_loader=lambda *_args, **_kwargs: next(observations),
            probe_executor=lambda *_args, **_kwargs: calls.append("ran")
            or d0.ProbeObservation(passed=True),
        )

    assert calls == ["ran"]
    cause = json.loads((tmp_path / "campaign" / "cause-report.json").read_text(encoding="utf-8"))
    assert cause["failed_probe"] == "dynamic-jdk-authority"


def test_campaign_never_cleans_a_container_when_archive_has_no_seal(tmp_path, monkeypatch):
    cleanup_calls = []

    def failed_archive(_runtime, _observation):
        raise d0.D0Error("archive transport failed")

    monkeypatch.setattr(d0.DockerProbeRuntime, "archive", failed_archive)
    monkeypatch.setattr(
        d0.DockerProbeRuntime,
        "cleanup",
        lambda _runtime: cleanup_calls.append("cleaned"),
    )

    with pytest.raises(d0.D0Stop, match="archive transport failed"):
        d0.run_campaign(
            repo=Path(__file__).parents[1],
            campaign_dir=tmp_path / "campaign",
            campaign_id="archive-failure",
            base_image=d0.DEFAULT_PREPARED_IMAGE,
            selected=d0.select_probes(["gradle-runner-classification"]),
            dry_run=False,
            keep_containers=False,
            fact_loader=lambda *_args, **_kwargs: _facts(),
            probe_executor=lambda *_args, **_kwargs: d0.ProbeObservation(passed=True),
        )

    assert cleanup_calls == []
    cause = json.loads((tmp_path / "campaign" / "cause-report.json").read_text(encoding="utf-8"))
    assert cause["archive_integrity"] == "failed"


def test_cause_report_does_not_call_an_existing_but_invalid_seal_verified(tmp_path):
    campaign = tmp_path / "campaign"
    runtime = d0.DockerProbeRuntime(
        repo=Path(__file__).parents[1],
        campaign_dir=campaign,
        campaign_id="invalid-seal",
        base_image=d0.DEFAULT_PREPARED_IMAGE,
        spec=d0.PROBE_SPECS["gradle-runner-classification"],
        run_pin={},
        keep_containers=True,
    )
    runtime.artifact_dir.mkdir(parents=True)
    d0._atomic_json(runtime.artifact_dir / "seal.json", {"status": "sealed"})

    d0._cause_report(
        campaign_dir=campaign,
        spec=runtime.spec,
        completed=(),
        error=d0.D0Stop("stop"),
        runtime=runtime,
    )

    cause = json.loads((campaign / "cause-report.json").read_text(encoding="utf-8"))
    assert cause["archive_integrity"] == "failed"


def test_prearchive_aggregation_failure_still_writes_a_fail_closed_cause_report(
    tmp_path, monkeypatch
):
    def broken_aggregate(*_args, **_kwargs):
        raise RuntimeError("aggregation transport failed")

    monkeypatch.setattr(d0.DockerProbeRuntime, "aggregate_observation", broken_aggregate)

    with pytest.raises(d0.D0Stop, match="evidence archive also failed"):
        d0.run_campaign(
            repo=Path(__file__).parents[1],
            campaign_dir=tmp_path / "campaign",
            campaign_id="aggregate-failure",
            base_image=d0.DEFAULT_PREPARED_IMAGE,
            selected=d0.select_probes(["gradle-runner-classification"]),
            dry_run=False,
            keep_containers=True,
            fact_loader=lambda *_args, **_kwargs: _facts(),
            probe_executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("probe failed before observation")
            ),
        )

    cause = json.loads((tmp_path / "campaign" / "cause-report.json").read_text(encoding="utf-8"))
    assert cause["archive_integrity"] == "failed"
    assert cause["error_type"] == "D0Stop"
    assert "aggregation transport failed" in cause["error"]


def test_cli_lists_probes_without_touching_docker(capsys):
    assert d0.main(["--list-probes"]) == 0
    output = capsys.readouterr().out
    assert "sync-large-evidence" in output
    assert "multi-job-progress-barrier" in output
