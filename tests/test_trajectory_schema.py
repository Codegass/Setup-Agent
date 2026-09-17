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


def test_call_carries_an_optional_human_summary():
    from sag.trajectory.schema import CallInfo

    assert CallInfo(tool="build").summary is None
    assert CallInfo(tool="build", summary="verify mvn clean verify").summary == (
        "verify mvn clean verify"
    )


def test_a_summary_longer_than_the_cap_is_rejected():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import SUMMARY_MAX_CHARS, CallInfo

    with pytest.raises(ValidationError):
        CallInfo(tool="bash", summary="x" * (SUMMARY_MAX_CHARS + 1))


def test_observation_states_how_the_call_came_out():
    from sag.trajectory.schema import ObservationInfo

    observation = ObservationInfo(outcome="failed", summary="exit 1 · enforcer")
    assert observation.outcome == "failed"
    assert observation.summary == "exit 1 · enforcer"
    assert ObservationInfo().outcome is None


def test_observation_outcome_is_a_closed_vocabulary():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import ObservationInfo

    with pytest.raises(ValidationError):
        ObservationInfo(outcome="probably fine")


def test_phase_carries_what_its_gate_decided():
    from sag.trajectory.schema import PhaseInfo

    phase = PhaseInfo(
        name="build",
        termination="advance",
        validator_state="green",
        reason="JVM build execution validated",
        key_results="Executed mvn clean verify",
    )
    assert phase.validator_state == "green"
    assert PhaseInfo(name="build").reason is None


def test_a_document_without_the_new_fields_still_validates():
    from sag.trajectory.schema import Trajectory

    document = Trajectory.model_validate(
        {
            "schema_version": 1,
            "session": {"run_id": "r"},
            "phases": [{"name": "build"}],
            "turns": [
                {
                    "turn_id": 1,
                    "phase": "build",
                    "actor": "model",
                    "call": {"tool": "build"},
                    "observation": {"ref": "output_a"},
                    "control_seq": [1],
                }
            ],
        }
    )
    assert document.turns[0].call.summary is None
    assert document.turns[0].observation.outcome is None


def test_an_empty_string_is_never_a_summary():
    """Absence is stated with `None`; `""` is the forbidden value, not a synonym.

    `ValidatorObservationPayload.reason` defaults to `""`, so a one-line wiring
    of the reducer would write the forbidden value straight in unless the
    boundary rejects it here.
    """
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import CallInfo, ObservationInfo, PhaseInfo

    with pytest.raises(ValidationError):
        CallInfo(tool="build", summary="")
    with pytest.raises(ValidationError):
        ObservationInfo(summary="")
    with pytest.raises(ValidationError):
        PhaseInfo(name="build", reason="")
    with pytest.raises(ValidationError):
        PhaseInfo(name="build", key_results="")


def test_an_observation_summary_is_capped_like_a_call_summary():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import SUMMARY_MAX_CHARS, ObservationInfo

    assert len(ObservationInfo(summary="x" * SUMMARY_MAX_CHARS).summary) == SUMMARY_MAX_CHARS
    with pytest.raises(ValidationError):
        ObservationInfo(summary="x" * (SUMMARY_MAX_CHARS + 1))


def test_key_results_are_capped_at_a_paragraph():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import KEY_RESULTS_MAX_CHARS, PhaseInfo

    assert KEY_RESULTS_MAX_CHARS == 400
    assert len(PhaseInfo(name="b", key_results="x" * KEY_RESULTS_MAX_CHARS).key_results) == 400
    with pytest.raises(ValidationError):
        PhaseInfo(name="b", key_results="x" * (KEY_RESULTS_MAX_CHARS + 1))


def test_validator_state_is_a_closed_vocabulary():
    import pytest
    from pydantic import ValidationError

    from sag.trajectory.schema import PhaseInfo

    assert PhaseInfo(name="b", validator_state="unavailable").validator_state == "unavailable"
    with pytest.raises(ValidationError):
        PhaseInfo(name="b", validator_state="mostly green")


def test_the_validator_state_words_are_the_engine_s_own():
    """This schema mirrors a vocabulary it must not import; pin the two together.

    `sag.trajectory` is a read-only derivation and does not import the agent
    package, so `ValidatorState` is a copy. A copy that drifts is a word the
    reader sees that the engine never wrote.
    """
    from typing import get_args

    from sag.agent.control_events import ValidatorObservationPayload
    from sag.trajectory.schema import ValidatorState

    engine = ValidatorObservationPayload.model_fields["validator_state"].annotation
    assert set(get_args(ValidatorState)) == set(get_args(engine))
