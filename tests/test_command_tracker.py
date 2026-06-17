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
