# tests/test_java_version_repair.py
"""Plan 7 round two — a build that states its own java mismatch gets a typed
code and a repair proposal.

Live evidence. p7-polaris (`logs/session_20260727_182218_41763`): Gradle
printed "requires Java 21." and "Detected Java version: 17", and no typed code
named it, so the repair loop had nothing to react to. p7-camel
(`logs/session_20260727_182221_41809`): the project's own wrapper ran under
Java 17 against a build needing 17+. In both runs the sentence was right there
and nothing read it.

This is the one repair whose support is an assessment rather than a document
claim: the requirement was stated by the runner, in its own output, which is
receipt evidence — the strongest provenance the loop has.
"""

import json

from test_repair_contracts import ScriptedOrchestrator

from sag.agent.evidence_assessments import java_version_mismatch
from sag.agent.repair_contracts import REPAIR_DIR, accepted_repair_for, build_repair

GRADLE_OUTPUT = (
    "FAILURE: Build failed with an exception.\n"
    "* What went wrong:\n"
    "A problem occurred configuring root project 'polaris'.\n"
    "> Dependency requires at least JVM runtime version 21. "
    "This build uses a Java 17 JVM.\n"
    "  Build requires Java 21.\n"
    "        Detected Java version: 17\n"
)

MAVEN_ENFORCER_OUTPUT = (
    "[WARNING] Rule 0: RequireJavaVersion failed with message:\n"
    "Detected JDK Version: 11.0.22 is not in the allowed range [17,).\n"
)

RECEIPT = {"receipt_id": "inv-gradle-1-0001", "outcome": "failed"}


def test_the_gradle_shape_yields_both_majors():
    (assessment,) = java_version_mismatch(RECEIPT, GRADLE_OUTPUT)

    assert assessment.typed_code == "java_version_mismatch"
    assert assessment.receipt_id == "inv-gradle-1-0001"
    assert "requires java 21" in assessment.detail
    assert "ran under java 17" in assessment.detail


def test_the_maven_enforcer_shape_yields_both_majors():
    (assessment,) = java_version_mismatch(RECEIPT, MAVEN_ENFORCER_OUTPUT)

    assert assessment.typed_code == "java_version_mismatch"
    assert "requires java 17" in assessment.detail
    assert "ran under java 11" in assessment.detail


def test_one_major_alone_states_nothing():
    """Inferring the missing half would invent a requirement."""
    assert java_version_mismatch(RECEIPT, "Build requires Java 21.\n") == []
    assert java_version_mismatch(RECEIPT, "Detected Java version: 17\n") == []


def test_two_majors_that_agree_are_not_a_mismatch():
    output = "Build requires Java 17.\n        Detected Java version: 17\n"

    assert java_version_mismatch(RECEIPT, output) == []


def test_no_output_states_nothing():
    assert java_version_mismatch(RECEIPT, None) == []
    assert java_version_mismatch(RECEIPT, "") == []


def test_the_proposal_names_the_required_major_and_cites_the_assessment():
    (assessment,) = java_version_mismatch(RECEIPT, GRADLE_OUTPUT)
    trigger = {
        "assessment_id": assessment.assessment_id,
        "receipt_id": assessment.receipt_id,
        "typed_code": assessment.typed_code,
        "detail": assessment.detail,
    }

    repair = build_repair(trigger, [], domain_root="/workspace/polaris")

    call = repair["proposed_public_call"]
    assert call["tool"] == "project"
    assert call["params"] == {"action": "provision", "java_version": "21"}
    assert repair["supporting_claim_ids"] == [assessment.assessment_id]


def test_the_proposal_needs_no_document_claim():
    """The build's own statement is the support; claims are not required."""
    trigger = {
        "assessment_id": "asm-x-java_version_mismatch-abc123",
        "receipt_id": "inv-gradle-1-0001",
        "typed_code": "java_version_mismatch",
        "detail": "build requires java 21, ran under java 17",
    }

    repair = build_repair(trigger, [], domain_root="/workspace/demo")

    assert repair is not None
    assert repair["proposed_public_call"]["params"]["java_version"] == "21"


def test_a_detail_without_a_major_proposes_nothing():
    trigger = {
        "assessment_id": "asm-x-java_version_mismatch-abc123",
        "receipt_id": "inv-gradle-1-0001",
        "typed_code": "java_version_mismatch",
        "detail": "the versions disagree",
    }

    assert build_repair(trigger, [], domain_root="/workspace/demo") is None


def test_another_code_with_no_claims_still_proposes_nothing():
    """The no-claims rule stands for every code that is not this one."""
    trigger = {
        "assessment_id": "asm-x-timeout-abc123",
        "receipt_id": "inv-gradle-1-0001",
        "typed_code": "timeout",
        "detail": "the dispatch timed out",
    }

    assert build_repair(trigger, [], domain_root="/workspace/demo") is None


# ---------------------------------------------------------------------------
# accepting it — §C6: "acceptance is the model calling that exact call"
# ---------------------------------------------------------------------------

PROPOSAL = {
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


def _orchestrator(*repairs):
    return ScriptedOrchestrator(
        files={
            f"{REPAIR_DIR}/{repair['repair_id']}.json": json.dumps(repair, sort_keys=True)
            for repair in repairs
        }
    )


def test_a_provision_proposal_is_acceptable_by_making_the_call():
    """Live p7b-polaris: the proposal named `project`, and acceptance matched
    only `build`, so the one call the harness asked for could never be
    recognised as accepting it. The proposal states its own tool; that is the
    tool acceptance compares against."""
    orchestrator = _orchestrator(PROPOSAL)

    accepted = accepted_repair_for(
        orchestrator, "project", {"action": "provision", "java_version": "21"}
    )

    assert accepted == "rep-790e1a3c9e0c"


def test_a_different_tool_never_borrows_the_proposal():
    orchestrator = _orchestrator(PROPOSAL)

    assert (
        accepted_repair_for(
            orchestrator, "build", {"action": "provision", "java_version": "21"}
        )
        is None
    )


def test_a_different_major_is_a_different_intent():
    orchestrator = _orchestrator(PROPOSAL)

    assert (
        accepted_repair_for(
            orchestrator, "project", {"action": "provision", "java_version": "17"}
        )
        is None
    )


def test_every_proposable_facade_can_be_accepted():
    """The two halves that disagreed in p7b are pinned together here.

    `build_repair` may name only these facades, and `accepted_repair_for`
    probes only these facades. A new generator that proposes a third one must
    extend `PROPOSABLE_TOOLS` in the same change, or it ships a proposal
    nothing can accept.
    """
    from sag.agent.repair_contracts import PROPOSABLE_TOOLS

    (assessment,) = java_version_mismatch(RECEIPT, GRADLE_OUTPUT)
    trigger = {
        "assessment_id": assessment.assessment_id,
        "receipt_id": assessment.receipt_id,
        "typed_code": assessment.typed_code,
        "detail": assessment.detail,
    }

    repair = build_repair(trigger, [], domain_root="/workspace/polaris")

    assert repair["proposed_public_call"]["tool"] in PROPOSABLE_TOOLS
    assert PROPOSABLE_TOOLS == {"build", "project"}
