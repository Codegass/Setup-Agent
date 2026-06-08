"""Tests for the workspace deletion service facade."""

import pytest

from sag.web.launch_queue import (
    LaunchBatch,
    LaunchItem,
    LaunchQueueStore,
    WorkspaceBusyError,
)
from sag.web.workspace_service import WorkspaceService

NOW = "2026-06-07T10:00:00"


class FakeOrchestrator:
    """Records remove_project() calls; never touches real Docker."""

    def __init__(self, workspace_id, removed=True):
        self.workspace_id = workspace_id
        self._removed = removed
        self.remove_calls = 0

    def remove_project(self):
        self.remove_calls += 1
        return self._removed


def make_store(tmp_path):
    return LaunchQueueStore(tmp_path / "launch_queue.sqlite3")


def enqueue(
    store,
    workspace_id="sag-a",
    item_id="LAUNCH-00000001",
    batch_id="BATCH-20260607-aaaaaa",
    status="queued",
    process_log=None,
):
    process_log = process_log or f"logs/project_launches/{batch_id}/{item_id}.log"
    batch = LaunchBatch(
        id=batch_id,
        created_at=NOW,
        concurrency=1,
        status="running",
        total=1,
        accepted=1,
        rejected=0,
    )
    item = LaunchItem(
        id=item_id,
        batch_id=batch_id,
        row_index=0,
        repo_url="https://github.com/apache/commons-cli.git",
        project_name="commons-cli",
        docker_label=workspace_id.removeprefix("sag-"),
        workspace_id=workspace_id,
        command=["python", "-m", "sag.main", "project"],
        process_log=process_log,
        created_at=NOW,
        status=status,
    )
    store.enqueue_batch(batch, [item])
    return item


def make_service(tmp_path, orchestrators=None, removed=True, launches_root=None):
    store = make_store(tmp_path)
    created: dict = {} if orchestrators is None else orchestrators

    def factory(workspace_id):
        orch = FakeOrchestrator(workspace_id, removed=removed)
        created[workspace_id] = orch
        return orch

    service = WorkspaceService(
        store=store,
        orchestrator_factory=factory,
        launches_root=launches_root if launches_root is not None else tmp_path / "launches",
    )
    return service, store, created


def test_delete_workspace_removes_container_and_queue_rows(tmp_path):
    service, store, orchestrators = make_service(tmp_path)
    enqueue(store, workspace_id="sag-a")

    result = service.delete_workspace("sag-a")

    assert orchestrators["sag-a"].remove_calls == 1
    assert result == {
        "workspace_id": "sag-a",
        "container_removed": True,
        "queue_items_removed": 1,
        "status": "deleted",
    }
    assert store.list_batches() == []
    assert store.active_workspace_ids() == set()


def test_delete_workspace_idempotent_with_no_queue_items(tmp_path):
    service, store, orchestrators = make_service(tmp_path)

    result = service.delete_workspace("sag-ghost")

    # Already-gone container still reports removed; zero rows is a clean success.
    assert orchestrators["sag-ghost"].remove_calls == 1
    assert result["container_removed"] is True
    assert result["queue_items_removed"] == 0
    assert result["status"] == "deleted"


def test_delete_workspace_busy_does_not_touch_container(tmp_path):
    service, store, orchestrators = make_service(tmp_path)
    enqueue(store, workspace_id="sag-a", status="running")

    with pytest.raises(WorkspaceBusyError):
        service.delete_workspace("sag-a")

    # remove_project never ran, and the queue row is intact.
    assert orchestrators == {}
    assert len(store.list_batches()[0]["items"]) == 1


def test_delete_workspace_swallows_missing_log_files(tmp_path):
    missing = tmp_path / "launches" / "BATCH-20260607-aaaaaa" / "missing.log"
    service, store, orchestrators = make_service(tmp_path)
    enqueue(store, workspace_id="sag-a", process_log=str(missing))

    result = service.delete_workspace("sag-a")

    assert result["queue_items_removed"] == 1
    assert orchestrators["sag-a"].remove_calls == 1


def test_delete_workspace_deletes_log_and_prunes_empty_batch_dir(tmp_path):
    launches_root = tmp_path / "launches"
    batch_dir = launches_root / "BATCH-20260607-aaaaaa"
    batch_dir.mkdir(parents=True)
    log_file = batch_dir / "LAUNCH-00000001.log"
    log_file.write_text("setup output", encoding="utf-8")

    service, store, _ = make_service(tmp_path, launches_root=launches_root)
    enqueue(store, workspace_id="sag-a", process_log=str(log_file))

    service.delete_workspace("sag-a")

    assert not log_file.exists()
    assert not batch_dir.exists()


def test_delete_workspace_reports_container_not_removed(tmp_path):
    service, store, _ = make_service(tmp_path, removed=False)
    enqueue(store, workspace_id="sag-a")

    result = service.delete_workspace("sag-a")

    assert result["container_removed"] is False
    assert result["queue_items_removed"] == 1
