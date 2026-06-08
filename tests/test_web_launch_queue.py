"""Tests for the SQLite-backed launch queue store."""

import pytest

from sag.web.launch_queue import (
    LaunchBatch,
    LaunchItem,
    LaunchQueueStore,
    WorkspaceBusyError,
)

NOW = "2026-06-07T10:00:00"
LATER = "2026-06-07T10:05:00"


def make_store(tmp_path):
    return LaunchQueueStore(tmp_path / "launch_queue.sqlite3")


def make_batch(batch_id="BATCH-20260607-abcdef", concurrency=2, total=1, accepted=1):
    return LaunchBatch(
        id=batch_id,
        created_at=NOW,
        concurrency=concurrency,
        status="running",
        total=total,
        accepted=accepted,
        rejected=total - accepted,
    )


def make_item(item_id, batch_id="BATCH-20260607-abcdef", row_index=0, **overrides):
    fields = dict(
        id=item_id,
        batch_id=batch_id,
        row_index=row_index,
        repo_url="https://github.com/apache/commons-cli.git",
        name=None,
        ref=None,
        goal=None,
        record=False,
        project_name="commons-cli",
        docker_label="commons-cli",
        workspace_id="sag-commons-cli",
        command=["python", "-m", "sag.main", "project", "https://github.com/apache/commons-cli.git"],
        process_log=f"logs/project_launches/{batch_id}/{item_id}.log",
        created_at=NOW,
    )
    fields.update(overrides)
    return LaunchItem(**fields)


def test_enqueued_items_persist_across_store_instances(tmp_path):
    db_path = tmp_path / "launch_queue.sqlite3"
    first = LaunchQueueStore(db_path)
    first.enqueue_batch(
        make_batch(),
        [make_item("LAUNCH-11111111", ref="rel/commons-cli-1.11.0")],
    )

    second = LaunchQueueStore(db_path)
    batches = second.list_batches()

    assert len(batches) == 1
    assert batches[0]["id"] == "BATCH-20260607-abcdef"
    assert batches[0]["status"] == "running"
    assert batches[0]["concurrency"] == 2
    assert batches[0]["created"] == NOW
    item = batches[0]["items"][0]
    assert item["id"] == "LAUNCH-11111111"
    assert item["row_index"] == 0
    assert item["repo_url"] == "https://github.com/apache/commons-cli.git"
    assert item["workspace_id"] == "sag-commons-cli"
    assert item["ref"] == "rel/commons-cli-1.11.0"
    assert item["status"] == "queued"
    assert item["pid"] is None
    assert item["exit_code"] is None
    assert item["error"] is None
    assert item["process_log"] == "logs/project_launches/BATCH-20260607-abcdef/LAUNCH-11111111.log"


def test_list_batches_orders_newest_first_and_items_by_row_index(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=2, accepted=2),
        [
            make_item("LAUNCH-00000002", "BATCH-20260607-aaaaaa", row_index=1),
            make_item("LAUNCH-00000001", "BATCH-20260607-aaaaaa", row_index=0),
        ],
    )
    later_batch = LaunchBatch(
        id="BATCH-20260607-bbbbbb",
        created_at=LATER,
        concurrency=1,
        status="running",
        total=1,
        accepted=1,
        rejected=0,
    )
    store.enqueue_batch(later_batch, [make_item("LAUNCH-00000003", "BATCH-20260607-bbbbbb")])

    batches = store.list_batches()

    assert [batch["id"] for batch in batches] == [
        "BATCH-20260607-bbbbbb",
        "BATCH-20260607-aaaaaa",
    ]
    assert [item["id"] for item in batches[1]["items"]] == [
        "LAUNCH-00000001",
        "LAUNCH-00000002",
    ]


def test_summary_counts_zero_for_empty_store(tmp_path):
    store = make_store(tmp_path)

    assert store.summary_counts() == {
        "queued": 0,
        "launching": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }


def enqueue_three_queued_items(store, concurrency=2):
    store.enqueue_batch(
        make_batch(concurrency=concurrency, total=3, accepted=3),
        [
            make_item("LAUNCH-00000001", row_index=0, workspace_id="sag-a", docker_label="a"),
            make_item("LAUNCH-00000002", row_index=1, workspace_id="sag-b", docker_label="b"),
            make_item("LAUNCH-00000003", row_index=2, workspace_id="sag-c", docker_label="c"),
        ],
    )


def test_claim_next_takes_oldest_row_and_marks_it_launching(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)

    claimed = store.claim_next(global_cap=8, now=LATER)

    assert claimed is not None
    assert claimed.id == "LAUNCH-00000001"
    assert claimed.status == "launching"
    assert claimed.started_at == LATER
    statuses = {
        item["id"]: item["status"] for item in store.list_batches()[0]["items"]
    }
    assert statuses["LAUNCH-00000001"] == "launching"
    assert statuses["LAUNCH-00000002"] == "queued"


def test_claim_next_orders_across_batches_by_created_at_then_row_index(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        LaunchBatch(id="BATCH-20260607-bbbbbb", created_at=LATER, concurrency=2),
        [make_item("LAUNCH-00000009", "BATCH-20260607-bbbbbb", created_at=LATER)],
    )
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=2, accepted=2),
        [
            make_item("LAUNCH-00000002", "BATCH-20260607-aaaaaa", row_index=1),
            make_item("LAUNCH-00000001", "BATCH-20260607-aaaaaa", row_index=0),
        ],
    )

    first = store.claim_next(global_cap=8, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)

    assert first.id == "LAUNCH-00000001"
    assert second.id == "LAUNCH-00000002"


def test_claim_next_respects_batch_concurrency(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=2)

    assert store.claim_next(global_cap=8, now=LATER) is not None
    assert store.claim_next(global_cap=8, now=LATER) is not None
    # Two active items in the batch == batch concurrency: nothing claimable.
    assert store.claim_next(global_cap=8, now=LATER) is None


def test_claim_next_respects_global_cap(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)

    assert store.claim_next(global_cap=1, now=LATER) is not None
    assert store.claim_next(global_cap=1, now=LATER) is None


def test_mark_running_records_pid(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)
    claimed = store.claim_next(global_cap=8, now=LATER)

    store.mark_running(claimed.id, pid=12345, now=LATER)

    item = store.list_batches()[0]["items"][0]
    assert item["status"] == "running"
    assert item["pid"] == 12345


def test_mark_completed_finishes_item_and_batch(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(make_batch(), [make_item("LAUNCH-00000001")])
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)

    store.mark_completed(claimed.id, exit_code=0, now=LATER)

    batch = store.list_batches()[0]
    assert batch["status"] == "completed"
    assert batch["items"][0]["status"] == "completed"
    assert batch["items"][0]["exit_code"] == 0


def test_mark_failed_records_error_and_fails_batch_when_done(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(make_batch(), [make_item("LAUNCH-00000001")])
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)

    store.mark_failed(claimed.id, "sag project exited with code 1", now=LATER, exit_code=1)

    batch = store.list_batches()[0]
    assert batch["status"] == "failed"
    assert batch["items"][0]["status"] == "failed"
    assert batch["items"][0]["exit_code"] == 1
    assert batch["items"][0]["error"] == "sag project exited with code 1"


def test_batch_stays_running_while_items_remain(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store)
    claimed = store.claim_next(global_cap=8, now=LATER)

    store.mark_completed(claimed.id, exit_code=0, now=LATER)

    assert store.list_batches()[0]["status"] == "running"


def test_unfinished_items_returns_launching_and_running_rows(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    first = store.claim_next(global_cap=8, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(second.id, pid=77, now=LATER)

    unfinished = store.unfinished_items()

    assert {item.id for item in unfinished} == {first.id, second.id}
    assert {item.status for item in unfinished} == {"launching", "running"}


def test_summary_counts_tracks_statuses(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    first = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(first.id, pid=1, now=LATER)
    store.mark_completed(first.id, exit_code=0, now=LATER)
    second = store.claim_next(global_cap=8, now=LATER)
    store.mark_failed(second.id, "boom", now=LATER)

    assert store.summary_counts() == {
        "queued": 1,
        "launching": 0,
        "running": 0,
        "completed": 1,
        "failed": 1,
    }


def test_mark_running_does_not_resurrect_finished_item(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(make_batch(), [make_item("LAUNCH-00000001")])
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_failed(claimed.id, "spawn failed", now=LATER)

    store.mark_running(claimed.id, pid=1, now=LATER)

    batch = store.list_batches()[0]
    assert batch["items"][0]["status"] == "failed"
    assert batch["status"] == "failed"


def test_concurrent_claims_never_double_claim_or_exceed_caps(tmp_path):
    import threading

    store = make_store(tmp_path)
    items = [
        make_item(f"LAUNCH-{index:08d}", row_index=index, workspace_id=f"sag-{index}")
        for index in range(20)
    ]
    store.enqueue_batch(make_batch(concurrency=5, total=20, accepted=20), items)

    claimed: list[str] = []
    lock = threading.Lock()

    def claim_all():
        while True:
            item = store.claim_next(global_cap=4, now=LATER)
            if item is None:
                return
            with lock:
                claimed.append(item.id)

    threads = [threading.Thread(target=claim_all) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    # Exactly the global cap of items is claimable while none finish.
    assert len(claimed) == 4
    assert len(set(claimed)) == 4
    statuses = {item["id"]: item["status"] for item in store.list_batches()[0]["items"]}
    assert sum(1 for status in statuses.values() if status == "launching") == 4


def test_active_workspace_ids_reflects_unfinished_items(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)
    store.mark_completed(claimed.id, exit_code=0, now=LATER)

    assert store.active_workspace_ids() == {"sag-b", "sag-c"}


def test_delete_workspace_items_removes_only_target_and_returns_logs(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=2, accepted=2),
        [
            make_item(
                "LAUNCH-00000001",
                "BATCH-20260607-aaaaaa",
                row_index=0,
                workspace_id="sag-a",
                docker_label="a",
            ),
            make_item(
                "LAUNCH-00000002",
                "BATCH-20260607-aaaaaa",
                row_index=1,
                workspace_id="sag-b",
                docker_label="b",
            ),
        ],
    )

    deleted, logs = store.delete_workspace_items("sag-a")

    assert deleted == 1
    assert logs == ["logs/project_launches/BATCH-20260607-aaaaaa/LAUNCH-00000001.log"]
    remaining = {
        item["id"] for batch in store.list_batches() for item in batch["items"]
    }
    assert remaining == {"LAUNCH-00000002"}
    # The batch still has another item, so it must survive.
    assert [batch["id"] for batch in store.list_batches()] == ["BATCH-20260607-aaaaaa"]


def test_delete_workspace_items_drops_emptied_batch_only(tmp_path):
    store = make_store(tmp_path)
    store.enqueue_batch(
        make_batch("BATCH-20260607-aaaaaa", total=1, accepted=1),
        [
            make_item(
                "LAUNCH-00000001",
                "BATCH-20260607-aaaaaa",
                workspace_id="sag-a",
                docker_label="a",
            )
        ],
    )
    store.enqueue_batch(
        make_batch("BATCH-20260607-bbbbbb", total=1, accepted=1),
        [
            make_item(
                "LAUNCH-00000002",
                "BATCH-20260607-bbbbbb",
                workspace_id="sag-b",
                docker_label="b",
            )
        ],
    )

    deleted, _ = store.delete_workspace_items("sag-a")

    assert deleted == 1
    # The now-empty batch is gone; the untouched workspace's batch remains.
    assert [batch["id"] for batch in store.list_batches()] == ["BATCH-20260607-bbbbbb"]


def test_delete_workspace_items_returns_zero_when_no_match(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)

    deleted, logs = store.delete_workspace_items("sag-does-not-exist")

    assert deleted == 0
    assert logs == []
    assert len(store.list_batches()[0]["items"]) == 3


def test_delete_workspace_items_raises_when_launching(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    claimed = store.claim_next(global_cap=8, now=LATER)  # marks sag-a launching

    with pytest.raises(WorkspaceBusyError):
        store.delete_workspace_items(claimed.workspace_id)

    # Deleted nothing.
    assert len(store.list_batches()[0]["items"]) == 3


def test_delete_workspace_items_raises_when_running(tmp_path):
    store = make_store(tmp_path)
    enqueue_three_queued_items(store, concurrency=3)
    claimed = store.claim_next(global_cap=8, now=LATER)
    store.mark_running(claimed.id, pid=1, now=LATER)

    with pytest.raises(WorkspaceBusyError):
        store.delete_workspace_items(claimed.workspace_id)

    assert len(store.list_batches()[0]["items"]) == 3
