# tests/test_repair_refusal_visibility.py
"""Plan 7 round four — the refusal reason was computed and then thrown away.

Live p7b-polaris (`logs/session_20260728_020933_55691`). The policy decided
`illegal_edge`, wrote it into `RepairRecord.decision_reason`, and the handoff
projection dropped the field. Everything the model ever saw about it was:

    REPAIR ROUTES:
    - build-1: build->build rejected signature=detached_operation_failed:e66b…

No reason, and nothing to do differently. `reason_code` on that same line is
the MODEL's typed code for the failure (`java_version_mismatch`) — it says why
the model asked, never why the policy said no, and reading it as the latter is
the natural mistake the line invites.

The edge case now never reaches the engine (the surface answers it), so what is
left to surface here are the refusals that legitimately end a phase — an
exhausted budget, a loop guard, a green source. A record that states which one
is a record the next phase can act on.

The schema half of the same defect: `target_phase` was documented as "repair:
direct dependency target" with no enum, so the legal set was stated nowhere the
model could read it. It is derived from `_REPAIR_EDGES`, so it cannot drift.
"""

from types import SimpleNamespace

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_transitions import RepairRequest, repair_moves
from sag.tools.phase_tool import PhaseTool

REQUEST = RepairRequest(
    from_phase="build",
    target_phase="analyze",
    source_attempt_id="build-1",
    reason_code="java_version_mismatch",
    failure_signature="detached_operation_failed:e66b98679f36bcb0",
    hypothesis="provisioning JDK 21 should satisfy the build's runtime check",
    evidence_refs=("output_7c4da0b66f89",),
)


def _rendered(*, accepted, decision_reason):
    state = RunEvidenceState(run_id="r1")
    state.record_repair(
        REQUEST,
        state_vector={"environment": 1},
        accepted=accepted,
        decision_reason=decision_reason,
    )
    return PhaseHandoff(state).project_for("report", char_budget=8000).to_prompt_text()


# ---------------------------------------------------------------------------
# the reason reaches the model
# ---------------------------------------------------------------------------


def test_a_refusal_states_why_the_policy_said_no():
    rendered = _rendered(accepted=False, decision_reason="repair_budget_exhausted")

    assert "rejected (repair_budget_exhausted)" in rendered


def test_the_model_s_own_reason_code_is_still_its_own():
    """Both belong on the line, and neither may be read as the other."""
    rendered = _rendered(accepted=False, decision_reason="repair_budget_exhausted")

    assert "asked=java_version_mismatch" in rendered
    assert "rejected (repair_budget_exhausted)" in rendered


def test_an_acceptance_needs_no_reason():
    """`repair_accepted` restates the word `accepted`; the line stays short."""
    rendered = _rendered(accepted=True, decision_reason="repair_accepted")

    assert "build-1: build->analyze accepted" in rendered
    assert "repair_accepted)" not in rendered


def test_a_record_with_no_stated_reason_renders_as_before():
    """Replayed transcripts predate the field; absence must not print `()`."""
    rendered = _rendered(accepted=False, decision_reason="")

    assert "build-1: build->analyze rejected asked=" in rendered
    assert "rejected (" not in rendered


def test_the_reason_survives_the_projection():
    state = RunEvidenceState(run_id="r1")
    state.record_repair(
        REQUEST,
        state_vector={"environment": 1},
        accepted=False,
        decision_reason="loop_without_progress",
    )

    (route,) = PhaseHandoff(state).project_for("report", char_budget=8000).repair_routes

    assert route.decision_reason == "loop_without_progress"
    assert route.reason_code == "java_version_mismatch"


# ---------------------------------------------------------------------------
# the legal set is stated where the model reads the parameter
# ---------------------------------------------------------------------------


def test_the_target_phase_parameter_names_the_moves_that_exist():
    machine = SimpleNamespace(
        current_phase="build", current_attempt_id="build-1", is_complete=False
    )
    tool = PhaseTool(machine=machine, validator=None, orchestrator=None, project_name="x")

    parameter = tool._get_parameters_schema()["properties"]["target_phase"]

    assert parameter["enum"] == ["analyze", "build"]
    assert "build->analyze" in parameter["description"]
    assert "test->build" in parameter["description"]


def test_the_documented_moves_are_the_policy_table():
    assert repair_moves() == (("build", ("analyze",)), ("test", ("build",)))
