"""Tests for the batch launch service facade."""

import re
import sys

import pytest

from sag.web.launch_queue import LaunchQueueStore
from sag.web.launch_service import (
    LaunchBatchRequest,
    LaunchService,
    LaunchValidationError,
    default_concurrency,
)

REPO = "https://github.com/apache/commons-cli.git"


class FakeScheduler:
    def __init__(self):
        self.woken = 0
        self.started = False
        self.stopped = False

    def wake(self):
        self.woken += 1

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_service(tmp_path, existing=(), cpu_count=8, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: cpu_count)
    store = LaunchQueueStore(tmp_path / "launch_queue.sqlite3")
    scheduler = FakeScheduler()
    service = LaunchService(
        store=store,
        scheduler=scheduler,
        workspace_exists=lambda label: label in existing,
    )
    return service, store, scheduler


def request_for(*rows, concurrency=None):
    return LaunchBatchRequest(concurrency=concurrency, projects=list(rows))


def test_accepted_rows_are_enqueued_with_cli_equivalent_command(tmp_path):
    service, store, scheduler = make_service(tmp_path)

    outcome = service.submit_batch(
        request_for(
            {
                "repo_url": REPO,
                "name": "commons-cli-111",
                "ref": "rel/commons-cli-1.11.0",
                "goal": "Setup and verify Apache Commons CLI",
                "record": True,
            }
        )
    )

    assert len(outcome["accepted"]) == 1
    accepted = outcome["accepted"][0]
    assert accepted["row_index"] == 0
    assert accepted["workspace_id"] == "sag-commons-cli-111"
    assert accepted["status"] == "queued"
    assert re.fullmatch(r"LAUNCH-[0-9a-f]{8}", accepted["launch_id"])
    assert re.fullmatch(r"BATCH-\d{8}-[0-9a-f]{6}", outcome["batch_id"])
    assert scheduler.woken == 1

    queued = store.list_batches()[0]["items"][0]
    assert queued["status"] == "queued"
    assert queued["process_log"] == (
        f"logs/project_launches/{outcome['batch_id']}/{accepted['launch_id']}.log"
    )


def test_stored_command_matches_manual_sag_project_invocation(tmp_path):
    service, store, _ = make_service(tmp_path)

    service.submit_batch(
        request_for({"repo_url": REPO, "ref": "v1.0", "record": True})
    )

    claimed = store.claim_next(global_cap=8, now="2026-06-07T10:00:00")
    assert claimed.command == [
        sys.executable,
        "-m",
        "sag.main",
        "project",
        REPO,
        "--ref",
        "v1.0",
        "--record",
    ]


def test_optional_fields_are_trimmed_and_blank_becomes_none(tmp_path):
    service, store, _ = make_service(tmp_path)

    service.submit_batch(
        request_for(
            {"repo_url": f"  {REPO}  ", "name": "  ", "ref": " v1.0 ", "goal": ""}
        )
    )

    claimed = store.claim_next(global_cap=8, now="2026-06-07T10:00:00")
    assert claimed.repo_url == REPO
    assert claimed.name is None
    assert claimed.ref == "v1.0"
    assert claimed.goal is None
    assert claimed.docker_label == "commons-cli"
    assert claimed.workspace_id == "sag-commons-cli"


def test_existing_workspace_conflicts_are_rejected_and_not_enqueued(tmp_path):
    service, store, scheduler = make_service(tmp_path, existing={"existing"})

    outcome = service.submit_batch(
        request_for(
            {"repo_url": REPO},
            {"repo_url": "https://github.com/x/existing.git"},
        )
    )

    assert len(outcome["accepted"]) == 1
    assert outcome["rejected"] == [
        {
            "row_index": 1,
            "workspace_id": "sag-existing",
            "status": "conflict",
            "message": "Workspace already exists: sag-existing",
        }
    ]
    assert len(store.list_batches()[0]["items"]) == 1


def test_all_conflicts_creates_no_batch(tmp_path):
    service, store, scheduler = make_service(tmp_path, existing={"commons-cli"})

    outcome = service.submit_batch(request_for({"repo_url": REPO}))

    assert outcome["accepted"] == []
    assert outcome["batch_id"] is None
    assert outcome["rejected"][0]["status"] == "conflict"
    assert store.list_batches() == []
    assert scheduler.woken == 0


def test_duplicate_workspace_within_batch_is_a_conflict(tmp_path):
    service, _, _ = make_service(tmp_path)

    outcome = service.submit_batch(
        request_for({"repo_url": REPO}, {"repo_url": REPO})
    )

    assert len(outcome["accepted"]) == 1
    assert outcome["rejected"][0]["status"] == "conflict"
    assert "Duplicate workspace in batch" in outcome["rejected"][0]["message"]


def test_underivable_repo_url_is_rejected_as_invalid(tmp_path):
    service, store, _ = make_service(tmp_path)

    outcome = service.submit_batch(request_for({"repo_url": "/"}))

    assert outcome["accepted"] == []
    assert outcome["rejected"][0]["status"] == "invalid"
    assert store.list_batches() == []


def test_omitted_concurrency_uses_cpu_aware_default(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=6, monkeypatch=monkeypatch)

    outcome = service.submit_batch(request_for({"repo_url": REPO}))

    assert outcome["concurrency"] == 3  # max(1, min(6 // 2, 4))


def test_default_concurrency_formula(monkeypatch):
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: 16)
    assert default_concurrency() == 4
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: 1)
    assert default_concurrency() == 1
    monkeypatch.setattr("sag.web.launch_service.os.cpu_count", lambda: None)
    assert default_concurrency() == 1


def test_concurrency_above_cpu_count_is_rejected(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=4, monkeypatch=monkeypatch)

    with pytest.raises(LaunchValidationError):
        service.submit_batch(request_for({"repo_url": REPO}, concurrency=5))


def test_concurrency_below_one_is_rejected(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=4, monkeypatch=monkeypatch)

    with pytest.raises(LaunchValidationError):
        service.submit_batch(request_for({"repo_url": REPO}, concurrency=0))


def test_blank_repo_url_fails_request_validation():
    with pytest.raises(ValueError):
        LaunchBatchRequest(concurrency=None, projects=[{"repo_url": "   "}])


def test_empty_projects_list_fails_request_validation():
    with pytest.raises(ValueError):
        LaunchBatchRequest(concurrency=None, projects=[])


def test_queue_state_reports_defaults_summary_and_batches(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=8, monkeypatch=monkeypatch)
    outcome = service.submit_batch(
        request_for({"repo_url": REPO, "ref": "v1.0"}, concurrency=2)
    )

    state = service.queue_state()

    assert state["default_concurrency"] == 4  # max(1, min(8 // 2, 4))
    assert state["summary"] == {
        "queued": 1,
        "launching": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
    assert len(state["batches"]) == 1
    batch = state["batches"][0]
    assert batch["id"] == outcome["batch_id"]
    assert batch["status"] == "running"
    assert batch["concurrency"] == 2
    item = batch["items"][0]
    assert item["id"] == outcome["accepted"][0]["launch_id"]
    assert item["repo_url"] == REPO
    assert item["ref"] == "v1.0"
    assert item["status"] == "queued"
    assert item["pid"] is None
    assert item["exit_code"] is None
    assert item["error"] is None
    assert item["process_log"].startswith("logs/project_launches/")


def test_queue_state_on_empty_store(tmp_path):
    service, _, _ = make_service(tmp_path)

    state = service.queue_state()

    assert state["batches"] == []
    assert sum(state["summary"].values()) == 0


def test_concurrency_at_cpu_count_is_accepted(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, cpu_count=4, monkeypatch=monkeypatch)

    outcome = service.submit_batch(request_for({"repo_url": REPO}, concurrency=4))

    assert outcome["concurrency"] == 4


def test_start_and_stop_delegate_to_scheduler(tmp_path):
    service, _, scheduler = make_service(tmp_path)

    service.start()
    service.stop()

    assert scheduler.started
    assert scheduler.stopped
