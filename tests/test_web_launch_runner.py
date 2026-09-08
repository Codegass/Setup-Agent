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
        workspace_id=f"sag-{item_id}",
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
    return {item["id"]: item for item in store.list_batches()[0]["items"]}


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

    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed")
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
    assert wait_for(lambda: item_states(store)["LAUNCH-00000001"]["status"] == "completed")
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


def test_reconcile_never_infers_completion_from_container_existence(tmp_path):
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

    assert checked == []
    item = item_states(store)["LAUNCH-00000001"]
    assert item["status"] == "failed"
    assert item["exit_code"] is None


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


def test_start_survives_reconcile_failure(tmp_path):
    store = make_store(tmp_path)
    scheduler = LaunchScheduler(store, spawn=FakeSpawner(), global_cap=8)

    def boom():
        raise RuntimeError("reconcile boom")

    scheduler.reconcile_stale = boom
    scheduler.start()
    try:
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
    finally:
        scheduler.stop()


def test_restarted_scheduler_keeps_watching_recovered_process_and_releases_capacity(
    tmp_path, monkeypatch
):
    store = make_store(tmp_path)
    enqueue(
        store,
        [make_item("old", status="running", pid=987654), make_item("next", row_index=1)],
        concurrency=1,
    )
    alive = threading.Event()
    alive.set()
    monkeypatch.setattr("sag.web.launch_runner._pid_alive", lambda pid: alive.is_set())
    spawner = FakeSpawner()
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=1, poll_interval=0.01)
    scheduler.start()
    try:
        assert item_states(store)["old"]["status"] == "running"
        alive.clear()
        scheduler.wake()
        assert wait_for(lambda: len(spawner.calls) == 1)
        old = item_states(store)["old"]
        assert old["status"] == "failed"
        assert old["exit_code"] is None
        assert "unavailable" in old["error"]
    finally:
        for process in spawner.processes:
            process.finish()
        scheduler.stop()


def test_start_retries_transient_recovery_failure_before_launching_new_work(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    enqueue(
        store,
        [
            make_item("old", status="running", pid=2_000_000_000),
            make_item("next", row_index=1),
        ],
        concurrency=2,
    )
    original_unfinished = store.unfinished_items
    recovery_reads = []
    allow_recovery = threading.Event()

    def temporarily_unavailable():
        recovery_reads.append(None)
        if not allow_recovery.is_set():
            raise OSError("temporary queue read failure")
        return original_unfinished()

    monkeypatch.setattr(store, "unfinished_items", temporarily_unavailable)
    spawner = FakeSpawner()
    scheduler = LaunchScheduler(store, spawn=spawner, global_cap=2, poll_interval=0.01)
    scheduler.start()
    try:
        # Spare capacity must not allow fresh children while recovery is failing.
        assert wait_for(lambda: len(recovery_reads) >= 2)
        assert spawner.calls == []
        allow_recovery.set()
        scheduler.wake()
        assert wait_for(lambda: len(spawner.calls) == 1)
        states = item_states(store)
        assert states["old"]["status"] == "failed"
        assert states["old"]["exit_code"] is None
        assert "unavailable" in states["old"]["error"]
        assert scheduler._recovered_ids == set()
        # This scheduler owns the new child and must use its real wait result.
        spawner.processes[0].finish(exit_code=7)
        assert wait_for(lambda: item_states(store)["next"]["status"] == "failed")
        assert item_states(store)["next"]["exit_code"] == 7
        assert len(recovery_reads) >= 2
    finally:
        for process in spawner.processes:
            process.finish()
        scheduler.stop()
