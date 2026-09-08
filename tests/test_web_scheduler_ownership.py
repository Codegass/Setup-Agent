"""A queue has one scheduler authority, including its surviving monitors."""

import subprocess
import sys
import threading

import pytest
from test_web_launch_runner import (
    FakeSpawner,
    enqueue,
    item_states,
    make_item,
    make_store,
    wait_for,
)

from sag.web.launch_queue import LaunchQueueStore
from sag.web.launch_runner import LaunchScheduler


@pytest.mark.parametrize("entry", ["start", "reconcile_stale", "launch_ready"])
def test_second_scheduler_cannot_recover_a_claim_before_spawn(tmp_path, entry):
    store = make_store(tmp_path)
    enqueue(store, [make_item("current")])
    claimed = threading.Event()
    release = threading.Event()
    spawner = FakeSpawner()

    def blocked_spawn(*args):
        claimed.set()
        assert release.wait(timeout=3)
        return spawner(*args)

    owner = LaunchScheduler(store, spawn=blocked_spawn)
    worker = threading.Thread(target=owner.launch_ready)
    worker.start()
    assert claimed.wait(timeout=2)
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    try:
        assert item_states(store)["current"]["status"] == "launching"
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            getattr(contender, entry)()
        assert item_states(store)["current"]["status"] == "launching"
        # A stop during the in-flight claim/spawn cannot transfer ownership.
        owner.stop()
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            getattr(contender, entry)()
    finally:
        release.set()
        worker.join(timeout=3)
        for process in spawner.processes:
            process.finish()
        owner.stop()
        contender.stop()


def test_stop_holds_owner_until_monitor_persists_real_exit(tmp_path):
    store = make_store(tmp_path)
    enqueue(store, [make_item("current")])
    spawner = FakeSpawner()
    owner = LaunchScheduler(store, spawn=spawner)
    owner.launch_ready()
    owner.stop()
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    try:
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            contender.reconcile_stale()
        spawner.processes[0].finish(exit_code=7)
        assert wait_for(lambda: item_states(store)["current"]["exit_code"] == 7)
        assert wait_for(lambda: owner._owner_handle is None)
        contender.reconcile_stale()
        assert item_states(store)["current"]["exit_code"] == 7
    finally:
        spawner.processes[0].finish()
        owner.stop()
        contender.stop()


def test_process_exit_releases_owner_and_allows_truthful_recovery(tmp_path):
    store = make_store(tmp_path)
    enqueue(store, [make_item("old", status="running", pid=2_000_000_000)])
    script = (
        "import sys; from pathlib import Path; "
        "from sag.web.launch_runner import LaunchScheduler; "
        "from sag.web.launch_queue import LaunchQueueStore; "
        "s=LaunchScheduler(LaunchQueueStore(Path(sys.argv[1]))); "
        "s.reconcile_stale(); print('owned',flush=True); sys.stdin.readline()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(store.db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    try:
        assert child.stdout.readline().strip() == "owned"
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            contender.reconcile_stale()
        child.terminate()
        child.wait(timeout=3)
        contender.reconcile_stale()
        old = item_states(store)["old"]
        assert old["status"] == "failed"
        assert old["exit_code"] is None
        assert "unavailable" in old["error"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        contender.stop()


def test_alias_database_cannot_obtain_an_independent_owner(tmp_path):
    store = make_store(tmp_path)
    store.summary_counts()
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(store.db_path)
    owner = LaunchScheduler(store)
    contender = LaunchScheduler(LaunchQueueStore(alias))
    owner.reconcile_stale()
    try:
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            contender.reconcile_stale()
    finally:
        owner.stop()
        contender.stop()


def test_failed_mark_running_still_monitors_spawned_child(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    enqueue(store, [make_item("current")])
    spawner = FakeSpawner()
    owner = LaunchScheduler(store, spawn=spawner)
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    monkeypatch.setattr(
        store,
        "mark_running",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("database failed")),
    )
    try:
        with pytest.raises(OSError, match="database failed"):
            owner.launch_ready()
        owner.stop()
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            contender.reconcile_stale()
        spawner.processes[0].finish(exit_code=3)
        assert wait_for(lambda: owner._owner_handle is None)
        assert item_states(store)["current"]["exit_code"] == 3
    finally:
        for process in spawner.processes:
            process.finish()
        owner.stop()
        contender.stop()


def test_monitor_start_failure_conservatively_retains_owner(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    enqueue(store, [make_item("current")])
    spawner = FakeSpawner()
    owner = LaunchScheduler(store, spawn=spawner)
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    original_start = threading.Thread.start

    def fail_monitor(thread):
        if thread.name.startswith("sag-launch-monitor-"):
            raise RuntimeError("no monitor thread")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_monitor)
    try:
        with pytest.raises(RuntimeError, match="no monitor thread"):
            owner.launch_ready()
        owner.stop()
        with pytest.raises(RuntimeError, match="scheduler.*owner|scheduler.*active"):
            contender.reconcile_stale()
        assert item_states(store)["current"]["status"] == "running"
    finally:
        # This test supplies the missing monitor synchronously after child exit.
        spawner.processes[0].finish(exit_code=4)
        owner._monitor("current", spawner.processes[0])
        owner.stop()
        contender.stop()


def test_scheduler_thread_start_failure_releases_idle_owner(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    owner = LaunchScheduler(store)
    contender = LaunchScheduler(LaunchQueueStore(store.db_path))
    monkeypatch.setattr(
        threading.Thread, "start", lambda _: (_ for _ in ()).throw(RuntimeError("no thread"))
    )
    with pytest.raises(RuntimeError, match="no thread"):
        owner.start()
    try:
        contender.reconcile_stale()
    finally:
        owner.stop()
        contender.stop()


def test_wait_failure_preserves_live_child_workspace_occupancy(tmp_path):
    from sag.web.launch_queue import WorkspaceBusyError

    store = make_store(tmp_path)
    enqueue(store, [make_item("current")])
    failed_wait = threading.Event()

    class UnwaitableChild:
        pid = 12345

        def wait(self):
            failed_wait.set()
            raise OSError("lost wait handle while child is alive")

    process = UnwaitableChild()
    owner = LaunchScheduler(store, spawn=lambda *args: process)
    monitor_done = threading.Event()
    original_monitor = owner._monitor

    def monitored(*args):
        try:
            original_monitor(*args)
        finally:
            monitor_done.set()

    owner._monitor = monitored
    owner.launch_ready()
    assert monitor_done.wait(timeout=2)
    owner.stop()
    try:
        assert item_states(store)["current"]["status"] == "running"
        for operation in ("task", "delete"):
            with pytest.raises(WorkspaceBusyError):
                store.acquire_workspace_lease("sag-current", operation)
        assert owner._owner_handle is not None
    finally:
        # Supply a verified exit only for deterministic test cleanup.
        process.wait = lambda: 0
        owner._monitor("current", process)
        owner.stop()
