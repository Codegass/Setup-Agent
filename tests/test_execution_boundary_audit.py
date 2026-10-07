"""A historical audit must not invent missing sources or edit frozen inputs."""

import hashlib
import json

import pytest

from scripts.audit_execution_boundaries import audit_campaign, audit_run


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def archive(root, *, started="2026-09-26T12:01:00Z"):
    write(root, "result.json", {"run_key": "trial", "run_id": "trial", "inference_finished_at": "2026-09-26T12:00:00Z"})
    write(root, "score.json", {"agent": "claude", "status": "complete"})
    write(root, "inputs/requirements.json", {"project_id": "example"})
    inv = write(root, "records/invocation.json", {"recorder_version": "portable-requirements-v2", "started_at": started})
    write(root, "records/campaign-run.json", {"invocations": [{"path": "invocation.json", "sha256": hashlib.sha256(inv.read_bytes()).hexdigest()}]})
    write(root, "checksums.json", {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()})


def test_post_agent_build_keeps_original_score_but_has_no_native_success(tmp_path):
    archive(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = audit_run(tmp_path)
    assert result["original_status"] == "complete"
    assert result["original_execution_origin"] == "post_agent_replay"
    assert result["native_agent_task_status"] == "unavailable"
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_timestamps_inside_agent_window_do_not_certify_agent_provenance(tmp_path):
    archive(tmp_path, started="2026-09-26T11:59:00Z")
    result = audit_run(tmp_path)
    assert result["original_execution_origin"] == "unavailable"
    assert result["native_agent_task_status"] == "unavailable"


def test_changed_archive_bytes_are_not_reinterpreted(tmp_path):
    archive(tmp_path)
    write(tmp_path, "score.json", {"agent": "claude", "status": "incomplete"})
    with pytest.raises(ValueError, match="Sealed source"):
        audit_run(tmp_path)


def test_missing_inventory_is_not_a_successful_zero_run_audit(tmp_path):
    write(tmp_path, "state.json", {"planned_attempts": 60})
    with pytest.raises(ValueError, match="inventory"):
        audit_campaign(tmp_path, tmp_path.parent / "missing-inventory-audit")


def test_audit_cannot_write_inside_frozen_campaign(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        audit_campaign(tmp_path, tmp_path / "analysis")
