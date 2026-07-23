from sag.tools.internal.command_tracker import CommandTracker


def test_track_build_command_records_duration():
    tracker = CommandTracker()
    tracker.track_build_command(
        command="mvn -q clean package",
        tool="maven",
        working_dir="/workspace",
        exit_code=0,
        output="BUILD SUCCESS",
        duration=47.2,
    )
    last = tracker.get_last_build_command()
    assert last is not None
    assert last["duration"] == 47.2
    assert last["command"] == "mvn -q clean package"


def test_track_build_command_duration_optional():
    tracker = CommandTracker()
    tracker.track_build_command(command="mvn install", tool="maven", output="BUILD SUCCESS")
    last = tracker.get_last_build_command()
    assert last is not None
    assert last.get("duration") is None


def test_pending_execution_receipt_is_provenance_not_replayable_build():
    tracker = CommandTracker()

    receipt = tracker.track_execution_receipt(
        command="mvn -Pfoo package",
        tool="maven",
        working_dir="/workspace/project",
        command_kind="build",
        dispatch_status="running_detached",
        poll_ref="job:maven-1",
        invocation_status="pending",
    )

    assert tracker.get_last_build_command() is None
    assert tracker.get_all_execution_receipts() == [receipt]
    assert receipt["poll_ref"] == "job:maven-1"
    assert receipt["invocation_status"] == "pending"

    assert tracker.update_execution_receipt(
        "job:maven-1",
        invocation_status="completed",
        dispatch_status="completed_detached",
        exit_code=0,
        operation_outcome="success",
        lifecycle_state="finished",
    )
    assert receipt["invocation_status"] == "completed"
    assert receipt["operation_outcome"] == "success"
