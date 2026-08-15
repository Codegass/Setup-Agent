"""trajectory-v1 is a versioned, strict, JSON-round-trippable read model."""

from sag.trajectory.schema import SCHEMA_VERSION, Trajectory, Turn, Warning


def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1


def test_a_minimal_trajectory_round_trips_through_json():
    turn = Turn(turn_id=1, phase="build", iteration=7, actor="model", control_seq=[41, 43])
    doc = Trajectory(
        session={"run_id": "r", "project": "kafka", "wall_clock_seconds": None},
        phases=[],
        turns=[turn],
        annotations=[],
        warnings=[Warning(code="missing_tool_result", detail="seq 124", control_seq=124)],
    )
    again = Trajectory.model_validate_json(doc.model_dump_json())
    assert again.schema_version == 1 and again.turns[0].control_seq == [41, 43]


def test_unknown_fields_are_rejected_not_swallowed():
    import pytest

    with pytest.raises(Exception):
        Turn(turn_id=1, phase="build", actor="model", control_seq=[], invented_field=1)
