"""Tests for the SQLite-backed launch queue store."""

from sag.web.launch_queue import LaunchBatch, LaunchItem, LaunchQueueStore

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
