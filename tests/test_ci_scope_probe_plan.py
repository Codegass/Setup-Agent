"""A deterministic controller plan cites durable, path-bound source bytes."""

import hashlib
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_ci_comparison import ComparisonFS
from test_receipt_assessor import _publish_requirements

from sag.agent.ci_comparison import _accepted_plan
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.output_storage import OutputStorageManager
from sag.agent.project_execution_plan import read_sealed_project_execution_plan
from sag.tools.base import OutputPersistenceError
from scripts.ci_scope_small_project_probe import probe_steps, seal_probe_plan


def fixture(tmp_path):
    fs = ComparisonFS()
    body = (
        "<project>\n"
        + "<!-- pinned source -->\n" * 1500
        + "<artifactId>tail</artifactId>\n</project>\n"
    )
    fs.files["/workspace/commons-cli/pom.xml"] = body
    _publish_requirements(fs)
    storage = OutputStorageManager(tmp_path / "outputs")
    epoch = SimpleNamespace(run_id="run-pytest")
    runtime = SimpleNamespace(
        output_storage_for=lambda _audit: storage, evidence_epoch=lambda _audit: nullcontext(epoch)
    )
    return fs, body, storage, epoch, runtime


def test_real_source_read_persists_full_bytes_and_binds_accepted_controller_plan(tmp_path):
    fs, body, storage, epoch, runtime = fixture(tmp_path)
    artifact, record, result, params = seal_probe_plan(
        runtime, fs, epoch, "commons-cli", probe_steps("commons-cli")
    )
    assert result.succeeded and result.output_ref
    assert storage.retrieve_output(result.output_ref) == body
    assert result.metadata["source_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert read_sealed_project_execution_plan(fs) == artifact
    assert artifact.plan.documents_reviewed[0].evidence_refs == (result.output_ref,)
    state = RunEvidenceState(run_id=epoch.run_id)
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "controller_source_read",
        result,
        params=params,
        source_phase="analyze",
        source_attempt_id=record.attempt_id,
    )
    state.record_phase_record(record)
    assert _accepted_plan(fs, state) == artifact


def test_output_that_cannot_round_trip_never_seals_a_plan(tmp_path, monkeypatch):
    fs, _, storage, epoch, runtime = fixture(tmp_path)
    monkeypatch.setattr(storage, "retrieve_output", lambda _ref: None)
    with pytest.raises(OutputPersistenceError):
        seal_probe_plan(runtime, fs, epoch, "commons-cli", probe_steps("commons-cli"))
    assert read_sealed_project_execution_plan(fs) is None
