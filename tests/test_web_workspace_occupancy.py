"""Task, project launch and deletion share one workspace admission boundary."""

import errno
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from test_web_launch_queue import make_batch, make_item
from test_web_task_runner import FakeSessionStore

import sag.web.task_runner as task_module
from sag.web.launch_queue import LaunchQueueStore, WorkspaceBusyError
from sag.web.task_runner import AgentTaskLauncher, TaskRequest, TaskRunner
from sag.web.workspace_service import WorkspaceService


@pytest.fixture
def store(tmp_path):
    return LaunchQueueStore(tmp_path / "queue.sqlite3")


@pytest.mark.parametrize("status", ["queued", "launching", "running"])
def test_task_admission_rejects_active_project_launch(store, status):
    store.enqueue_batch(make_batch(), [make_item("launch", workspace_id="sag-demo", status=status)])
    sessions = FakeSessionStore()

    with pytest.raises(WorkspaceBusyError):
        AgentTaskLauncher(sessions, store=store).run("sag-demo", "task", None)

    assert sessions.started == []


def test_task_lease_blocks_launch_and_delete_but_not_other_workspace(store):
    touched = []
    service = WorkspaceService(store, orchestrator_factory=lambda _: touched.append(True))
    with store.acquire_workspace_lease("sag-demo", "task"):
        item = make_item("launch", workspace_id="sag-demo")
        assert store.enqueue_batch(make_batch(), [item]) == [item]
        with pytest.raises(WorkspaceBusyError):
            service.delete_workspace("sag-demo")
        with store.acquire_workspace_lease("sag-other", "task"):
            assert store.is_workspace_busy("sag-other")
    assert touched == []
    assert not store.is_workspace_busy("sag-demo")


@pytest.mark.parametrize("owner, contender", [("demo", "sag-demo"), ("sag-demo", "demo")])
def test_workspace_aliases_share_task_launch_and_delete_exclusion(store, owner, contender):
    touched = []
    service = WorkspaceService(store, orchestrator_factory=lambda _: touched.append(True))
    with store.acquire_workspace_lease(owner, "task"):
        assert store.is_workspace_busy(contender)
        with pytest.raises(WorkspaceBusyError):
            service.delete_workspace(contender)
        with pytest.raises(WorkspaceBusyError):
            AgentTaskLauncher(FakeSessionStore(), store=store).run(contender, "task", None)
        item = make_item("launch", workspace_id=contender)
        assert len(store.enqueue_batch(make_batch(), [item])) == 1
    assert touched == []


def test_bare_workspace_task_uses_one_canonical_session_identity(store, monkeypatch):
    captured = []

    class PendingThread:
        def __init__(self, **kwargs):
            captured.append(kwargs["args"])

        def start(self):
            pass

    monkeypatch.setattr(task_module, "Thread", PendingThread)
    sessions = FakeSessionStore()
    runner = TaskRunner(AgentTaskLauncher(sessions, store=store))
    result = runner.submit("demo", TaskRequest(task="task", source_session="previous"))
    try:
        assert (
            result["workspace_id"]
            == sessions.started[0]["workspace_id"]
            == captured[0][1]
            == "sag-demo"
        )
        assert result["source_session"] == "previous"
    finally:
        captured[0][-1].release()


def test_bare_launch_workspace_is_stored_and_deleted_canonically(store):
    store.enqueue_batch(make_batch(), [make_item("launch", workspace_id="demo")])
    assert store.list_batches()[0]["items"][0]["workspace_id"] == "sag-demo"
    assert store.active_workspace_ids() == {"sag-demo"}
    assert store.delete_workspace_items("demo")[0] == 1


def test_bare_delete_routes_to_canonical_container_id(store):
    calls = []

    def factory(workspace_id):
        calls.append(workspace_id)
        return SimpleNamespace(remove_project=lambda: True)

    result = WorkspaceService(store, orchestrator_factory=factory).delete_workspace("demo")
    assert calls == ["sag-demo"]
    assert result["workspace_id"] == "sag-demo"


def test_delete_holds_workspace_until_docker_removal_finishes(store):
    class Docker:
        def remove_project(self):
            with pytest.raises(WorkspaceBusyError):
                store.acquire_workspace_lease("sag-demo", "task")
            item = make_item("launch", workspace_id="sag-demo")
            assert store.enqueue_batch(make_batch(), [item]) == [item]
            return True

    service = WorkspaceService(store, orchestrator_factory=lambda _: Docker())
    assert service.delete_workspace("sag-demo")["status"] == "deleted"
    with store.acquire_workspace_lease("sag-demo", "task"):
        pass


def test_delete_reservation_prevents_claiming_a_queued_launch(store):
    store.enqueue_batch(make_batch(), [make_item("launch", workspace_id="sag-demo")])

    class Docker:
        def remove_project(self):
            return True

    def factory(_):
        assert store.claim_next(4, "2026-09-07T00:00:00") is None
        return Docker()

    assert (
        WorkspaceService(store, orchestrator_factory=factory).delete_workspace("sag-demo")[
            "queue_items_removed"
        ]
        == 1
    )


def test_task_and_launch_admission_are_atomic_across_store_instances(store):
    store.summary_counts()  # Initialize schema before racing independent connections.
    second = LaunchQueueStore(store.db_path)
    barrier = Barrier(2)
    item = make_item("launch", workspace_id="sag-demo")

    def task():
        barrier.wait(timeout=2)
        try:
            return second.acquire_workspace_lease("sag-demo", "task")
        except WorkspaceBusyError:
            return None

    def launch():
        barrier.wait(timeout=2)
        return store.enqueue_batch(make_batch(), [item])

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_future = pool.submit(task)
        launch_future = pool.submit(launch)
        lease, rejected = task_future.result(), launch_future.result()
    try:
        assert (lease is not None) == bool(rejected)
    finally:
        if lease is not None:
            lease.release()


def test_duplicate_tasks_have_one_admission_and_one_session(store, monkeypatch):
    threads = []

    class PendingThread:
        def __init__(self, **kwargs):
            self.args = kwargs["args"]
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(task_module, "Thread", PendingThread)
    sessions = FakeSessionStore()
    launcher = AgentTaskLauncher(sessions, store=store)
    launcher.run("sag-demo", "task", None)
    try:
        with pytest.raises(WorkspaceBusyError):
            launcher.run("sag-demo", "another task", None)
        assert len(sessions.started) == len(threads) == 1
    finally:
        threads[0].args[-1].release()


@pytest.mark.parametrize("failure", ["session", "thread", "thread_and_finish"])
def test_task_start_failure_releases_workspace(store, monkeypatch, failure):
    class FailingSessionStore(FakeSessionStore):
        def mark_started(self, **kwargs):
            if failure == "session":
                raise RuntimeError("session write failed")
            super().mark_started(**kwargs)

        def mark_finished(self, **kwargs):
            if failure == "thread_and_finish":
                raise RuntimeError("finish write failed")
            super().mark_finished(**kwargs)

    class FailingThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(task_module, "Thread", FailingThread)
    sessions = FailingSessionStore()
    expected_error = "session write failed" if failure == "session" else "thread start failed"
    with pytest.raises(RuntimeError, match=expected_error):
        AgentTaskLauncher(sessions, store=store).run("sag-demo", "task", None)
    assert not store.is_workspace_busy("sag-demo")
    if failure == "thread":
        assert sessions.finished[0]["success"] is False
        assert sessions.finished[0]["outcome"] == "Task did not start: task"


def test_agent_and_finish_record_failures_still_release_workspace(store, monkeypatch):
    class FailingSessionStore(FakeSessionStore):
        def mark_finished(self, **kwargs):
            raise RuntimeError("session finish unavailable")

    def fail_orchestrator(**kwargs):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr("sag.docker_orch.orch.DockerOrchestrator", fail_orchestrator)
    launcher = AgentTaskLauncher(FailingSessionStore(), store=store)
    lease = store.acquire_workspace_lease("sag-demo", "task")
    launcher._run_agent("UI-test", "sag-demo", "task", None, lease)
    assert not store.is_workspace_busy("sag-demo")


@pytest.mark.parametrize("success", [True, False])
def test_task_completion_holds_lease_through_session_update(store, monkeypatch, success):
    class SessionStore(FakeSessionStore):
        def mark_finished(self, **kwargs):
            assert store.is_workspace_busy("sag-demo")
            super().mark_finished(**kwargs)

    class Docker:
        def __init__(self, **kwargs):
            pass

        def container_exists(self):
            return True

        def is_container_running(self):
            return True

    class Agent:
        def __init__(self, **kwargs):
            pass

        def run_task(self, **kwargs):
            return success

    monkeypatch.setattr("sag.docker_orch.orch.DockerOrchestrator", Docker)
    monkeypatch.setattr("sag.agent.agent.SetupAgent", Agent)
    monkeypatch.setattr("sag.config.get_config", lambda: None)
    monkeypatch.setattr("sag.config.ensure_session_logging", lambda *args, **kwargs: None)
    sessions = SessionStore()
    launcher = AgentTaskLauncher(sessions, store=store)
    monkeypatch.setattr(launcher, "_read_project_name", lambda *args, **kwargs: "demo")
    lease = store.acquire_workspace_lease("sag-demo", "task")
    launcher._run_agent("UI-test", "sag-demo", "task", None, lease)
    assert sessions.finished[0]["success"] is success
    assert not store.is_workspace_busy("sag-demo")


def test_abandoned_process_lease_is_reclaimed_by_next_store_transaction(store):
    script = """
import os, sys
from pathlib import Path
from sag.web.launch_queue import LaunchQueueStore
lease = LaunchQueueStore(Path(sys.argv[1])).acquire_workspace_lease('sag-demo', 'task')
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(store.db_path)], check=True, timeout=10)
    with store.acquire_workspace_lease("sag-demo", "task"):
        assert store.is_workspace_busy("sag-demo")


def test_live_lease_is_visible_to_another_process(store):
    script = """
import sys
from pathlib import Path
from sag.web.launch_queue import LaunchQueueStore
assert LaunchQueueStore(Path(sys.argv[1])).is_workspace_busy('sag-demo')
"""
    with store.acquire_workspace_lease("sag-demo", "task"):
        subprocess.run([sys.executable, "-c", script, str(store.db_path)], check=True, timeout=10)


def test_database_symlink_uses_the_same_owner_lock_directory(store, tmp_path):
    store.summary_counts()
    alias = tmp_path / "alias.sqlite3"
    try:
        alias.symlink_to(store.db_path)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    other = LaunchQueueStore(alias)
    assert other.db_path == store.db_path
    with store.acquire_workspace_lease("sag-demo", "task"):
        with pytest.raises(WorkspaceBusyError):
            other.acquire_workspace_lease("sag-demo", "task")
        assert other.is_workspace_busy("sag-demo")


def test_release_failure_does_not_mask_original_error_or_leave_live_lock(store, monkeypatch):
    lease = store.acquire_workspace_lease("sag-demo", "task")
    monkeypatch.setattr(
        store,
        "_release_workspace_lease",
        lambda *args: (_ for _ in ()).throw(RuntimeError("DB unavailable")),
    )
    with pytest.raises(ValueError, match="original"):
        with lease:
            raise ValueError("original")
    with LaunchQueueStore(store.db_path).acquire_workspace_lease("sag-demo", "task"):
        pass


def test_default_app_task_admission_shares_launch_and_delete_occupancy(store):
    from fastapi.testclient import TestClient
    from test_web_launch_service import FakeScheduler

    from sag.web.app import create_app
    from sag.web.launch_service import LaunchService
    from sag.web.read_model import ReadModelBuilder

    launches = LaunchService(store, scheduler=FakeScheduler(), workspace_exists=lambda _: False)
    client = TestClient(create_app(ReadModelBuilder(demo_mode=True), launch_service=launches))
    with store.acquire_workspace_lease("sag-demo", "task"):
        task = client.post("/api/workspaces/sag-demo/tasks", json={"task": "task"})
        delete = client.delete("/api/workspaces/sag-demo")
        batch = client.post(
            "/api/project-launches/batch",
            json={"projects": [{"repo_url": "https://example.invalid/demo.git"}]},
        )
    assert task.status_code == delete.status_code == batch.status_code == 409
    assert "in use" in task.json()["detail"]


@pytest.mark.parametrize("method, suffix", [("POST", "/tasks"), ("DELETE", "")])
def test_empty_workspace_label_is_a_client_validation_error(method, suffix):
    from fastapi.testclient import TestClient

    from sag.web.app import create_app
    from sag.web.read_model import ReadModelBuilder

    client = TestClient(create_app(ReadModelBuilder(demo_mode=True)))
    response = client.request(method, f"/api/workspaces/sag-{suffix}", json={"task": "task"})
    assert response.status_code == 422
    assert "nonempty project label" in response.json()["detail"]


def test_windows_lease_branch_uses_nonblocking_byte_lock(tmp_path, monkeypatch):
    import sag.web.workspace_leases as leases

    calls = []
    windows = SimpleNamespace(LK_NBLCK=123, locking=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "msvcrt", windows)
    monkeypatch.setattr(leases, "os", SimpleNamespace(name="nt"))
    path = tmp_path / "owner.lock"
    handle = leases.open_lease_lock(path, create=True)
    assert handle is not None
    assert calls == [(handle.fileno(), 123, 1)]
    handle.close()

    def busy(*args):
        raise OSError(errno.EACCES, "lock owned")

    windows.locking = busy
    assert leases.open_lease_lock(path) is None
