"""trajectory-v1 is a versioned, strict, JSON-round-trippable read model."""

import pytest

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
    with pytest.raises(Exception):
        Turn(turn_id=1, phase="build", actor="model", control_seq=[], invented_field=1)


def test_a_document_from_a_version_this_code_cannot_read_fails_loudly():
    """`schema_version` is a claim, and a wrong claim must not parse quietly.

    A v99 document written by a future reducer would carry fields this version
    has never heard of and lack fields it depends on. Accepting it and reading
    the parts that happen to overlap is how a derived view starts lying, so the
    version is pinned to the one number this module implements.
    """
    doc = Trajectory(session={"run_id": "r"}).model_dump()
    doc["schema_version"] = 99
    with pytest.raises(Exception):
        Trajectory.model_validate(doc)


def test_a_warning_is_an_immutable_statement_that_names_its_turn():
    """Warnings are compared and de-duplicated by value, so they are frozen."""
    hole = Warning(code="missing_tool_result", detail="turn 4", control_seq=124, turn_id=4)
    assert hole == Warning(code="missing_tool_result", detail="turn 4", control_seq=124, turn_id=4)
    assert len({hole, hole}) == 1  # hashable: a statement is held, or it is not
    with pytest.raises(Exception):
        hole.code = "something_else"
