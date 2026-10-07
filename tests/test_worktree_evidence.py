"""Worktree evidence observes actual bytes without contaminating the checkout."""

import base64
import hashlib
import json
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from sag.agent.worktree_evidence import WorktreeEvidenceRecorder, probe_worktree


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", root], check=True)
    (root / "tracked.txt").write_text("original\n")
    subprocess.run(["git", "-C", root, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root


def test_preserves_nul_paths_raw_hashes_and_boundary_changes(repository, tmp_path):
    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository)
    )
    start = recorder.capture("task_start")
    assert start["tracked_clean"] is True
    assert start["untracked_paths"] == []
    (repository / "tracked.txt").write_text("modified\n")
    (repository / "agent note\nwith newline.txt").write_text("diagnosis\n")
    close = recorder.capture("evidence_close")
    assert close["head"] == start["head"]
    assert close["tracked_clean"] is False
    assert close["tracked_paths"] == ["tracked.txt"]
    assert close["untracked_paths"] == ["agent note\nwith newline.txt"]
    assert start["tracked_clean"] is True  # not rewritten by later observation
    for snapshot in (start, close):
        for probe in snapshot["probes"].values():
            for stream in ("stdout", "stderr"):
                ref = probe[stream]
                raw = (tmp_path / "session" / ref["path"]).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
                assert len(raw) == ref["bytes"]
    assert not (repository / "worktree-evidence").exists()
    assert recorder.capture("task_start") is start


def test_nonzero_untracked_probe_is_unknown_not_empty(repository, tmp_path, monkeypatch):
    probes = probe_worktree(str(repository))
    probes["untracked"].update(
        exit_code=128, stdout="", stderr=base64.b64encode(b"fatal\n").decode()
    )
    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository)
    )
    monkeypatch.setattr(recorder, "_probe", lambda: probes)
    row = recorder.capture("task_start")
    assert row["status"] == "unavailable"
    assert row["tracked_clean"] is True
    assert row["untracked_paths"] is None
    assert row["probes"]["untracked"]["exit_code"] == 128


def test_transport_failure_records_no_fabricated_git_results(repository, tmp_path):
    def failed(*args, **kwargs):
        return {"exit_code": -1, "dispatch_status": "timeout", "output": ""}

    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository), execute=failed
    )
    row = recorder.capture("evidence_close")
    assert row["status"] == "unavailable"
    assert row["tracked_clean"] is None and row["untracked_paths"] is None
    assert all(p["exit_code"] is None for p in row["probes"].values())


def test_container_framing_matches_local_probes_with_non_ascii_newline_path(repository, tmp_path):
    (repository / "诊断\n.txt").write_bytes(b"note")

    def execute(command, **kwargs):
        assert kwargs["truncate_output"] is False
        completed = subprocess.run(shlex.split(command), capture_output=True, check=False)
        return {"exit_code": completed.returncode, "output": completed.stdout.decode()}

    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository), execute=execute
    )
    row = recorder.capture("task_start")
    assert row["status"] == "observed"
    assert row["untracked_paths"] == ["诊断\n.txt"]


def test_recorder_refuses_checkout_destination(repository):
    with pytest.raises(ValueError, match="outside"):
        WorktreeEvidenceRecorder(
            repository / "results", run_id="run-1", project_root=str(repository)
        )


def test_task_matching_retries_have_separate_before_after_snapshots(repository, tmp_path):
    task = {
        "steps": [{"id": "test", "runner": "maven", "argv": ["mvn", "clean", "verify"], "cwd": "."}]
    }
    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository), task_definition=task
    )
    contract = {
        "run_id": "run-1",
        "expected_cwd": str(repository),
        "effective_tool": "maven",
        "expected_argv": "clean verify",
        "contract_id": "first",
    }
    recorder.before_contract({**contract, "run_id": "other-run"})
    recorder.before_contract({**contract, "expected_argv": "clean verify -DskipTests"})
    assert not recorder.directory.exists()
    recorder.before_contract(contract)
    (repository / "note.txt").write_text("new")
    recorder.after_receipt({"run_id": "run-1", "contract_id": "first", "receipt_id": "receipt-1"})
    recorder.before_contract({**contract, "contract_id": "retry"})
    recorder.after_receipt({"run_id": "run-1", "contract_id": "retry", "receipt_id": "receipt-2"})
    rows = [json.loads(path.read_text()) for path in recorder.directory.glob("*.json")]
    assert len(rows) == 4
    first_before = next(
        r for r in rows if r["invocation_id"] == "first" and r["boundary"] == "acceptance_before"
    )
    first_after = next(
        r for r in rows if r["invocation_id"] == "first" and r["boundary"] == "acceptance_after"
    )
    assert first_before["untracked_paths"] == []
    assert first_after["untracked_paths"] == ["note.txt"]
    assert first_after["observation_source"] == "receipt_publication"
    assert first_after["receipt_id"] == "receipt-1"


def test_existing_facade_contract_and_receipt_call_live_hooks(tmp_path, monkeypatch):
    from test_acceptance_task import task_run
    from test_fixed_command_verification import dispatch
    from test_ci_comparison import ROOT

    run = task_run(tmp_path, "mvn clean install")
    recorder = WorktreeEvidenceRecorder(
        tmp_path / "worktree-session",
        run_id=run.state.run_id,
        project_root=ROOT,
        task_definition=run.task.model_dump(mode="json"),
    )
    calls = []
    original = recorder.capture
    monkeypatch.setattr(recorder, "_probe", lambda: {})

    def capture(boundary, **kwargs):
        calls.append((boundary, kwargs))
        return original(boundary, **kwargs)

    monkeypatch.setattr(recorder, "capture", capture)
    run.fs.worktree_evidence_recorder = recorder
    result = dispatch(run, "mvn clean install")
    assert [c[0] for c in calls] == ["acceptance_before", "acceptance_after"]
    assert calls[0][1]["invocation_id"] == calls[1][1]["invocation_id"]
    assert calls[1][1]["receipt_id"] == result.metadata["receipt_id"]


def test_clone_callback_and_evidence_close_are_actual_lifecycle_boundaries(repository, tmp_path):
    from sag.agent.agent import SetupAgent
    from sag.agent.react_engine import ReActEngine
    from sag.agent.verdict_finalizer import EvidenceCloseReason

    recorder = WorktreeEvidenceRecorder(
        tmp_path / "session", run_id="run-1", project_root=str(repository)
    )
    orchestrator = SimpleNamespace(worktree_evidence_recorder=recorder)
    agent = object.__new__(SetupAgent)
    agent.orchestrator = orchestrator
    agent._write_run_pin = lambda **kwargs: None
    agent._record_target_repo_sha("a" * 40)
    (repository / "after-clone.txt").write_text("diagnostic")
    engine = object.__new__(ReActEngine)
    engine.orchestrator = orchestrator
    engine.run_evidence_state = SimpleNamespace(sealed=False)
    engine._await_open_obligations = lambda reason: None
    engine._sweep_job_obligations = lambda: None
    engine._record_unsettled_job_conflicts = lambda reason: None
    engine._emit_control_event = lambda *args: None
    observed = []

    def finalize(*args):
        rows = [json.loads(p.read_text()) for p in recorder.directory.glob("*.json")]
        observed.extend(rows)
        return "snapshot"

    engine.verdict_finalizer = SimpleNamespace(finalize=finalize)
    assert engine._finalize_evidence(EvidenceCloseReason.ABORTED) == "snapshot"
    assert {r["boundary"] for r in observed} == {"task_start", "evidence_close"}
    assert next(r for r in observed if r["boundary"] == "task_start")["untracked_paths"] == []
    assert next(r for r in observed if r["boundary"] == "evidence_close")["untracked_paths"] == [
        "after-clone.txt"
    ]
