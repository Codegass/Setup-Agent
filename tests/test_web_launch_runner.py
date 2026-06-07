"""Tests for the launch scheduler worker."""

import os
import threading
import time

from sag.web.launch_queue import LaunchBatch, LaunchItem, LaunchQueueStore
from sag.web.launch_runner import LaunchScheduler

NOW = "2026-06-07T10:00:00"


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self._exited = threading.Event()
        self._exit_code = 0

    def finish(self, exit_code=0):
        self._exit_code = exit_code
        self._exited.set()

    def wait(self):
        self._exited.wait(timeout=5)
        return self._exit_code


class FakeSpawner:
    def __init__(self, fail_with=None):
        self.calls = []
        self.processes = []
        self.fail_with = fail_with

    def __call__(self, argv, log_path):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((argv, log_path))
        process = FakeProcess(pid=1000 + len(self.processes))
        self.processes.append(process)
        return process


def wait_for(condition, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_store(tmp_path):
    return LaunchQueueStore(tmp_path / "launch_queue.sqlite3")


def make_item(item_id, batch_id="BATCH-20260607-abcdef", row_index=0, **overrides):
    fields = dict(
        id=item_id,
        batch_id=batch_id,
        row_index=row_index,
        repo_url="https://github.com/apache/commons-cli.git",
        project_name="commons-cli",
        docker_label="commons-cli",
        workspace_id="sag-commons-cli",
        command=["python", "-m", "sag.main", "project", "x"],
        process_log=f"logs/project_launches/{batch_id}/{item_id}.log",
        created_at=NOW,
    )
    fields.update(overrides)
    return LaunchItem(**fields)


def enqueue(store, items, concurrency=2):
    store.enqueue_batch(
        LaunchBatch(
            id="BATCH-20260607-abcdef",
            created_at=NOW,
            concurrency=concurrency,
            total=len(items),
            accepted=len(items),
        ),
        items,
    )


def item_states(store):
    return {
        item["id"]: item for item in store.list_batches()[0]["items"]
    }


def test_launch_ready_starts_no_more_than_batch_concurrency(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [
            make_item("LAUNCH-00000001", row_index=0),
            make_item("LAUNCH-00000002", row_index=1),
            make_item("LAUNCH-00000003", row_index=2),
        ],
        concurrency=2,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    assert len(spawner.calls) == 2
    states = item_states(store)
    assert states["LAUNCH-00000003"]["status"] == "queued"


def test_launch_ready_respects_global_cap_across_batches(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [make_item("LAUNCH-00000001"), make_item("LAUNCH-00000002", row_index=1)],
        concurrency=2,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=1)

    scheduler.launch_ready()

    assert len(spawner.calls) == 1


def test_started_item_records_pid_and_redirected_log_path(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "running")
    item = item_states(store)["LAUNCH-00000001"]
    assert item["pid"] == 1000
    assert item["process_log"].endswith("LAUNCH-00000001.log")
    argv, log_path = spawner.calls[0]
    assert argv == ["python", "-m", "sag.main", "project", "x"]
    assert str(log_path).endswith("LAUNCH-00000001.log")


def test_zero_exit_marks_completed(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()

    spawner.processes[0].finish(exit_code=0)

    assert wait_for(
        lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed"
    )
    assert item_states(store)["LAUNCH-00000001"]["exit_code"] == 0


def test_nonzero_exit_marks_failed_with_exit_code(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()

    spawner.processes[0].finish(exit_code=1)

    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "failed")
    item = item_states(store)["LAUNCH-00000001"]
    assert item["exit_code"] == 1
    assert "exited with code 1" in item["error"]


def test_spawn_failure_marks_failed(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner(fail_with=OSError("no such file"))
    enqueue(store, [make_item("LAUNCH-00000001")])
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)

    scheduler.launch_ready()

    item = item_states(store)["LAUNCH-00000001"]
    assert item["status"] == "failed"
    assert "Failed to start subprocess" in item["error"]


def test_capacity_freed_by_completion_lets_next_item_start(tmp_path):
    store = make_store(tmp_path)
    spawner = FakeSpawner()
    enqueue(
        store,
        [make_item("LAUNCH-00000001"), make_item("LAUNCH-00000002", row_index=1)],
        concurrency=1,
    )
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=8)
    scheduler.launch_ready()
    assert len(spawner.calls) == 1

    spawner.processes[0].finish(exit_code=0)
    assert wait_for(
        lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed"
    )
    scheduler.launch_ready()

    assert len(spawner.calls) == 2


def test_reconcile_fails_dead_process_rows_with_restart_message(tmp_path):
    store = make_store(tmp_path)
    enqueue(
        store,
        [make_item("LAUNCH-00000001", status="running", pid=2_000_000_000)],
    )
    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=lambda label: False, global_cap=8
    )

    scheduler.reconcile_stale()

    item = item_states(store)["LAUNCH-00000001"]
    assert item["status"] == "failed"
    assert "UI restart" in item["error"]


def test_reconcile_marks_completed_when_workspace_exists(tmp_path):
    store = make_store(tmp_path)
    enqueue(
        store,
        [make_item("LAUNCH-00000001", status="launching", pid=2_000_000_000)],
    )
    checked = []

    def workspace_exists(docker_label):
        checked.append(docker_label)
        return True

    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=workspace_exists, global_cap=8
    )

    scheduler.reconcile_stale()

    assert checked == ["commons-cli"]
    assert item_states(store)["LAUNCH-00000001"]["status"] == "completed"


def test_reconcile_leaves_alive_process_rows_untouched(tmp_path):
    store = make_store(tmp_path)
    enqueue(store, [make_item("LAUNCH-00000001", status="running", pid=os.getpid())])
    scheduler = LaunchScheduler(
        store, spawn=FakeSpawner(), workspace_exists=lambda label: False, global_cap=8
    )

    scheduler.reconcile_stale()

    assert item_states(store)["LAUNCH-00000001"]["status"] == "running"


def test_queued_items_from_previous_run_resume(tmp_path):
    db_path = tmp_path / "launch_queue.sqlite3"
    previous = LaunchQueueStore(db_path)
    enqueue(previous, [make_item("LAUNCH-00000001")])

    spawner = FakeSpawner()
    scheduler = LaunchScheduler(LaunchQueueStore(db_path), spawn=spawner, global_cap=8)
    scheduler.reconcile_stale()
    scheduler.launch_ready()

    assert len(spawner.calls) == 1
