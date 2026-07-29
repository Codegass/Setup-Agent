# tests/test_repair_channel.py
"""Plan 7 round four — two channels answered the same fact, and the wrong one
ended the run.

Live evidence, twice, identically. p7-polaris
(`logs/session_20260727_182218_41763`) and p7b-polaris
(`logs/session_20260728_020933_55691`): Gradle said "The Apache Polaris build
requires Java 21. Detected Java version: 17". The model read it, agreed with
it, and restated it correctly — "provisioning JDK 21 should satisfy the build's
runtime check and allow compilation to proceed". Then it submitted that
diagnosis through `phase(action='repair', target_phase='build')`.

`_REPAIR_EDGES` is `{("test","build"), ("build","analyze")}`. `build→build` is
`illegal_edge`, and so is `build→provision` — no repair edge from build reaches
the phase that installs a JDK, so the call the harness itself proposed could
not be routed by the channel the model used. Worse, the engine closes the
attempt BEFORE it checks legality, so the rejection arrived with the build
phase already terminal; test was skipped and the run went to report. Both times
the model was right and the machinery refused it.

The proposal never needed that channel. §C6: "acceptance is the model calling
that exact call" — `project(action='provision', java_version='21')` was
callable right there, in the build phase, with no rollback and no permission.
The phase repair channel exists to roll BACK to an earlier phase. Routing a
performable call through it converts a proposal into a terminal `failed` claim.

So when a proposal for this typed code is already on disk, the phase channel
refuses and names the call. Nothing is closed, nothing is skipped, and the
wrong guess costs one turn instead of the run.
"""

import json
from types import SimpleNamespace

from test_repair_contracts import ScriptedOrchestrator

from sag.agent.repair_contracts import REPAIR_DIR
from sag.tools.phase_tool import PhaseTool

JAVA_PROPOSAL = {
    "schema_version": 1,
    "repair_id": "rep-790e1a3c9e0c",
    "typed_failure_or_capability": "java_version_mismatch",
    "trigger_receipt_id": "inv-gradle-1-0001",
    "trigger_assessment_id": "asm-inv_gradle_1_0001-java_version_mismatch-947fb39f",
    "supporting_claim_ids": ["asm-inv_gradle_1_0001-java_version_mismatch-947fb39f"],
    "proposed_public_call": {
        "tool": "project",
        "params": {"action": "provision", "java_version": "21"},
    },
    "permitted_semantic_envelope": {"tool": "project", "actions": ["provision"]},
}

# The exact parameters p7b's model sent (control event `envelope-000028`).
POLARIS_REPAIR = {
    "action": "repair",
    "target_phase": "build",
    "reason_code": "java_version_mismatch",
    "failure_signature": "DETACHED_OPERATION_FAILED:e66b98679f36bcb0",
    "hypothesis": (
        "Gradle settings require Java 21; provisioning JDK 21 should satisfy "
        "the build's runtime check and allow compilation to proceed."
    ),
    "evidence": ["output_7c4da0b66f89"],
}

# The one edge a repair from build actually has, for the cases that must still
# reach the engine untouched.
LEGAL_REPAIR = {**POLARIS_REPAIR, "target_phase": "analyze"}


def _tool(*repairs, phase="build"):
    orchestrator = ScriptedOrchestrator(
        files={
            f"{REPAIR_DIR}/{repair['repair_id']}.json": json.dumps(repair, sort_keys=True)
            for repair in repairs
        }
    )
    machine = SimpleNamespace(
        current_phase=phase,
        current_attempt_id=f"{phase}-1",
        is_complete=False,
    )
    return PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=orchestrator,
        project_name="polaris",
    )


def test_a_performable_proposal_is_refused_by_the_phase_channel():
    result = _tool(JAVA_PROPOSAL).execute(**POLARIS_REPAIR)

    assert result.succeeded is False
    assert result.error_code == "PHASE_REPAIR_ALREADY_PROPOSED"


def test_the_refusal_leaves_the_phase_untouched():
    """The whole point: p7b lost the build phase to this call."""
    result = _tool(JAVA_PROPOSAL).execute(**POLARIS_REPAIR)

    assert "phase_signal" not in result.metadata


def test_the_refusal_names_the_exact_call_to_make():
    """One spelling only: `render_public_call` renders both the surfaced
    `[repair]` block and this refusal, so the model is never shown the same
    call two ways and left to guess which one accepts it."""
    result = _tool(JAVA_PROPOSAL).execute(**POLARIS_REPAIR)

    call = 'project({"action":"provision","java_version":"21"})'
    assert call in result.suggestions
    assert call in result.output


def test_the_refusal_carries_the_repair_it_points_at():
    result = _tool(JAVA_PROPOSAL).execute(**POLARIS_REPAIR)

    assert result.metadata["repair_id"] == "rep-790e1a3c9e0c"
    assert result.metadata["proposed_call"] == {
        "tool": "project",
        "params": {"action": "provision", "java_version": "21"},
    }


def test_another_typed_code_still_reaches_the_rollback_channel():
    """The channel is not removed. It is declined for the one case that never
    needed it — a proposal the model can simply perform."""
    result = _tool(JAVA_PROPOSAL).execute(
        **{**LEGAL_REPAIR, "reason_code": "compile_no_source_mismatch"}
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "repair"


def test_no_proposal_on_disk_changes_nothing():
    result = _tool().execute(**LEGAL_REPAIR)

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "repair"


def test_a_tool_with_no_orchestrator_changes_nothing():
    """Direct constructions (and every existing test) pass no orchestrator."""
    machine = SimpleNamespace(
        current_phase="build", current_attempt_id="build-1", is_complete=False
    )
    tool = PhaseTool(machine=machine, validator=None, orchestrator=None, project_name="x")

    result = tool.execute(**LEGAL_REPAIR)

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "repair"


def test_the_proposal_refusal_outranks_the_edge_refusal():
    """polaris named an illegal edge AND had a live proposal. "Make this call"
    is the more useful of the two answers, so it is the one it gets."""
    result = _tool(JAVA_PROPOSAL).execute(**POLARIS_REPAIR)

    assert result.error_code == "PHASE_REPAIR_ALREADY_PROPOSED"


# ---------------------------------------------------------------------------
# the edge the policy does not have is answerable without any evidence
# ---------------------------------------------------------------------------
#
# `_repair_rejection` runs inside `request_repair`, which the engine calls
# AFTER `machine.close_attempt(gate)`. So a proposal naming an edge the policy
# has never had still costs the phase it was proposed from — the p7/p7b polaris
# shape, minus the proposal. Whether an edge exists needs no gate, no validator
# and no physical evidence: it is a property of the request. Answering it at
# the surface means nothing has moved yet when the answer is no.


def test_an_edge_the_policy_does_not_have_is_refused_at_the_surface():
    result = _tool().execute(**{**POLARIS_REPAIR, "reason_code": "semantic_failure"})

    assert result.succeeded is False
    assert result.error_code == "PHASE_REPAIR_ILLEGAL_TARGET"
    assert "phase_signal" not in result.metadata


def test_the_refusal_states_the_targets_this_phase_does_have():
    result = _tool().execute(**{**POLARIS_REPAIR, "reason_code": "semantic_failure"})

    assert "analyze" in result.output
    assert result.metadata["legal_targets"] == ["analyze"]


def test_a_legal_edge_still_reaches_the_engine():
    result = _tool().execute(
        **{**POLARIS_REPAIR, "reason_code": "semantic_failure", "target_phase": "analyze"}
    )

    assert result.succeeded is True
    assert result.metadata["phase_signal"] == "repair"


def test_a_phase_with_no_repair_target_says_so():
    """provision and report have no repair edge at all; listing none would
    read as "you named the wrong one" rather than "there is no such move"."""
    result = _tool(phase="provision").execute(
        **{**POLARIS_REPAIR, "reason_code": "semantic_failure"}
    )

    assert result.succeeded is False
    assert result.error_code == "PHASE_REPAIR_ILLEGAL_TARGET"
    assert result.metadata["legal_targets"] == []
    assert "no repair target" in result.output


def test_the_legal_targets_are_derived_from_the_policy_table():
    """One source of truth: the tool must not restate the edge set."""
    from sag.agent.phase_transitions import _REPAIR_EDGES, repair_targets_for

    for source, target in _REPAIR_EDGES:
        assert target in repair_targets_for(source)
    assert repair_targets_for("build") == ("analyze",)
    assert repair_targets_for("test") == ("build",)
    assert repair_targets_for("report") == ()


# ---------------------------------------------------------------------------
# what deliberately still closes the phase
# ---------------------------------------------------------------------------
#
# `_repair_rejection` has three more refusals, and they stay behind the gate on
# purpose:
#
# * `repair_budget_exhausted` — the budget exists to bound how many times a run
#   may roll back. Asking again once it is spent is where the phase is supposed
#   to close, so closing is the designed outcome and not an accident.
# * the recurrence guard — same shape: it exists to end a loop.
# * `repair_source_green` — the gate validated the attempt as SUCCESS, so
#   closing and advancing is the correct route, not a penalty.
#
# `stale_repair_evidence` is the one open case. It is malformed-request shaped
# like the edge check, but the attempt's evidence refs reach
# `RunEvidenceState` on the gate path, so a surface check could refuse a
# legitimate repair whose refs simply have not landed yet. Moving it needs a
# live run to confirm the ordering; it is not the defect p7/p7b showed.
