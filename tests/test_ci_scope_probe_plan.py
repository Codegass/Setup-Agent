"""A deterministic controller plan cites durable, path-bound source bytes."""

import hashlib
import json
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


def test_production_counterfactuals_remove_scores_without_changing_execution(tmp_path):
    from test_ci_comparison import ROOT, setup_run

    from scripts.ci_scope_small_project_probe import production_target_ablations

    run = setup_run()
    run.state.seal(finalized_at="2026-09-09T00:00:00Z", close_reason="test_terminated")
    execution_files = dict(run.fs.files)
    result = production_target_ablations(
        run.fs,
        run.state,
        validator=run.validator,
        project_path=ROOT,
        repo="apache/example",
        target=run.target,
        destination=tmp_path,
    )
    assert result["full_target"]["attainment"]["verdict"] == "met"
    assert result["scope_removal_control"]["status"] == "passed"
    assert result["without_target_scope"]["attainment"]["alpha_build"] is None
    assert result["without_test_scope"]["attainment"]["alpha_test"] is None
    for label in ("without_target_scope", "without_test_scope"):
        assert result[label]["attainment"]["alpha"] is None
        assert result[label]["attainment"]["verdict"] != "met"
        assert (
            result[label]["certificate_input_sha256"]
            == result["full_target"]["certificate_input_sha256"]
        )
    assert result["without_target"]["status"] == "no_target"
    assert run.fs.files == execution_files


@pytest.mark.parametrize("fails", [False, True])
def test_probe_archives_latest_publications_even_when_body_raises(tmp_path, monkeypatch, fails):
    from scripts import ci_scope_small_project_probe as probe

    archive_calls = []
    audit = SimpleNamespace(latest_publication="before-run")
    epoch = object()

    def body(project, out, *, evidence_context, **kwargs):
        destination = out / project
        destination.mkdir()
        evidence_context.update(destination=destination, audit=audit, epoch=epoch)
        audit.latest_publication = "latest-after-body"
        if fails:
            raise RuntimeError("original physical failure")
        return {"project": project}

    def archive(destination, observed_audit, observed_epoch, name, project):
        assert project == "commons-cli"
        archive_calls.append((observed_audit.latest_publication, observed_epoch, name))
        return {"status": "complete", "file_count": 1}

    monkeypatch.setattr(probe, "_run_project_body", body)
    monkeypatch.setattr(probe, "archive_probe_evidence", archive)
    call = lambda: probe.run_project(
        "commons-cli",
        tmp_path,
        container_name="sag-fixture",
        experiment_id="fixture",
        expected_source_sha="a" * 40,
    )
    if fails:
        with pytest.raises(RuntimeError, match="original physical failure"):
            call()
        assert (
            json.loads((tmp_path / "commons-cli/failure.json").read_text())["error_type"]
            == "RuntimeError"
        )
    else:
        assert call()["archive_integrity"] == "complete"
    assert archive_calls == [("latest-after-body", epoch, "sag-fixture")]
    assert (
        json.loads((tmp_path / "commons-cli/archive-status.json").read_text())["status"]
        == "complete"
    )
