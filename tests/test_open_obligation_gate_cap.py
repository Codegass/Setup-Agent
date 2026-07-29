# tests/test_open_obligation_gate_cap.py
"""Plan 8 Stage 2 — the gate stopped grading a moving target (spec §3.3).

The worst single fact in the p7d runs, stated precisely
(`logs/session_20260729_111737_22356`, graded from control events):

    polaris build: claimed = partial   ->  validated = SUCCESS (green)

The MODEL was honest. It claimed `partial` while its compile job was still
running. The GATE upgraded the claim to success, on this evidence:

    Built 100% of expected classes (>= 100% threshold) ·
    Module coverage: 1/26 built [build-logic]

One hundred percent and one-of-twenty-six in the same sentence.

The P0-F truth table exists to stop exactly this — "the gate may confirm or
downgrade, never upgrade" — and did not fire, because its cap is keyed on
unclosed SURVEYED DOMAINS and polaris's survey cannot read Kotlin settings, so
`build_islands: []`. No domains, no cap. An empty domain graph is not evidence
that nothing is unfinished.

So the trigger set grows and the direction does not. While any obligation in
the ledger is unsettled, OR any surveyed domain is unclosed, the gate may
confirm or downgrade and never refine upward; and a GREEN validator state is
capped to PARTIAL while an obligation is open, because success requires
settled books. An honest `partial`/`unknown` claim passes exactly as it does
today. Honesty is never punished.
"""

from test_settlement_triggers import Orchestrator

from sag.agent import phase_gates
from sag.agent.phase_gates import (
    OPEN_OBLIGATIONS_FACT,
    ClaimDisposition,
    ValidatorState,
    _ValidatorObservation,
    check_phase_claim,
    validate_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome

JOB = "373f63e5a0a4"

# The polaris build gate's own sentence, verbatim from the run.
POLARIS_REASON = (
    "Built 100% of expected classes (>= 100% threshold) · "
    "Module coverage: 1/26 built [build-logic]"
)

OPEN = {OPEN_OBLIGATIONS_FACT: [JOB]}
# The pre-Plan-8 shape of the same phase: polaris surveyed no domains at all.
NO_DOMAINS = {"build.domain_states": {}}


def _claim(outcome, phase="build", signal="done"):
    return PhaseClaim(phase=phase, signal=signal, claimed_outcome=PhaseOutcome(outcome))


# ---------------------------------------------------------------------------
# the anchor: the assertion this lane exists for
# ---------------------------------------------------------------------------


def test_an_honest_partial_is_not_upgraded_while_a_job_is_still_running():
    """polaris p7d, replayed under the new rule: claimed partial, validator
    green, one compile job with no terminal receipt -> validated PARTIAL."""
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason=POLARIS_REASON,
        validated_facts={**NO_DOMAINS, **OPEN},
    )

    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.validator_state is ValidatorState.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.accepted is True


def test_the_same_claim_was_upgraded_before_the_ledger_existed():
    """The defect, pinned: with no obligation fact the old path is unchanged,
    which is exactly what every recorded transcript replays through."""
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason=POLARIS_REASON,
        validated_facts=NO_DOMAINS,
    )

    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.disposition is ClaimDisposition.PESSIMISTIC


def test_the_reason_names_the_evidence():
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason=POLARIS_REASON,
        validated_facts=OPEN,
    )

    assert gate.reason.startswith(POLARIS_REASON)
    assert f"job {JOB} has no terminal receipt — the claim is confirmable at most" in gate.reason


# ---------------------------------------------------------------------------
# success requires settled books
# ---------------------------------------------------------------------------


def test_green_is_capped_to_partial_while_an_obligation_is_open():
    """A claim of success cannot be validated green on books that are still
    open — the run does not yet know what its own job did."""
    gate = validate_phase_claim(
        _claim("success"),
        ValidatorState.GREEN,
        reason="Built 100% of expected classes",
        validated_facts=OPEN,
    )

    assert gate.validator_state is ValidatorState.PARTIAL
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert gate.accepted is False
    assert f"job {JOB} has no terminal receipt — success requires settled books" in gate.reason


def test_a_settled_ledger_leaves_a_green_success_alone():
    gate = validate_phase_claim(
        _claim("success"),
        ValidatorState.GREEN,
        reason="Built 100% of expected classes",
        validated_facts={},
    )

    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.disposition is ClaimDisposition.CONFIRMED


# ---------------------------------------------------------------------------
# honesty is never punished
# ---------------------------------------------------------------------------


def test_an_honest_partial_against_a_partial_validator_passes_exactly_as_today():
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.PARTIAL,
        reason="two of four modules built",
        validated_facts=OPEN,
    )

    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.accepted is True


def test_an_honest_unknown_claim_is_still_unverifiable_not_refused():
    """p7d polaris made two honest `blocked(outcome='unknown')` claims while
    it polled. Neither may cost the run anything."""
    gate = validate_phase_claim(
        _claim("unknown", signal="blocked"),
        ValidatorState.UNAVAILABLE,
        reason="the compile job has not reported",
        validated_facts=OPEN,
    )

    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.accepted is True


def test_an_honest_failed_claim_is_confirmed_unchanged():
    gate = validate_phase_claim(
        _claim("failed"),
        ValidatorState.RED,
        reason="compile failed",
        validated_facts=OPEN,
    )

    assert gate.validated_outcome is PhaseOutcome.FAILED
    assert gate.disposition is ClaimDisposition.CONFIRMED


# ---------------------------------------------------------------------------
# the direction is untouched
# ---------------------------------------------------------------------------


def test_a_claim_above_the_evidence_is_still_contradicted():
    """The cap only ever removes refinement above the claim. It never rescues
    a claim the physical oracle contradicts."""
    gate = validate_phase_claim(
        _claim("success"),
        ValidatorState.RED,
        reason="no classes were produced",
        validated_facts=OPEN,
    )

    assert gate.validated_outcome is PhaseOutcome.FAILED
    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert gate.accepted is False


def test_the_cap_never_manufactures_a_state_below_the_claim():
    """It stops AT the claim: a failed claim on green evidence is confirmed
    failed, not refused."""
    gate = validate_phase_claim(
        _claim("failed"),
        ValidatorState.GREEN,
        reason=POLARIS_REASON,
        validated_facts=OPEN,
    )

    assert gate.validated_outcome is PhaseOutcome.FAILED
    assert gate.disposition is ClaimDisposition.CONFIRMED


# ---------------------------------------------------------------------------
# the domain cap is unchanged, and the two compose
# ---------------------------------------------------------------------------


def test_the_domain_sentence_is_untouched():
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason="bigtop",
        validated_facts={"build.domain_states": {"/workspace/bigtop/hadoop": {"state": "failed"}}},
    )

    assert (
        "no refinement above the claim while surveyed build domains are "
        "unclosed: /workspace/bigtop/hadoop=failed" in gate.reason
    )
    assert "terminal receipt" not in gate.reason


def test_both_blockers_are_named_when_both_apply():
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason="bigtop",
        validated_facts={
            "build.domain_states": {"/workspace/bigtop/hadoop": {"state": "untried"}},
            **OPEN,
        },
    )

    assert "surveyed build domains are unclosed" in gate.reason
    assert f"job {JOB} has no terminal receipt" in gate.reason


def test_several_open_jobs_are_named_as_jobs():
    gate = validate_phase_claim(
        _claim("partial"),
        ValidatorState.GREEN,
        reason="polaris",
        validated_facts={OPEN_OBLIGATIONS_FACT: [JOB, "9f21c0be55aa"]},
    )

    assert f"jobs {JOB}, 9f21c0be55aa have no terminal receipt" in gate.reason


def test_the_whole_path_caps_the_polaris_claim_from_the_ledger_on_disk(monkeypatch):
    """End to end at the seam the model actually reaches: an open obligation in
    the container's ledger, a green physical inspection, an honest `partial`
    claim — and the fact travels into `validated_facts`, so replay reproduces
    the cap offline from the transcript alone."""
    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda phase, validator, orchestrator, project_name: _ValidatorObservation(
            ValidatorState.GREEN,
            reason=POLARIS_REASON,
            code="build_verified",
        ),
    )

    gate = check_phase_claim(
        "build",
        _claim("partial"),
        None,
        Orchestrator(terminated=False),
        "polaris",
    )

    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.validated_facts[OPEN_OBLIGATIONS_FACT] == [JOB]
    assert f"job {JOB} has no terminal receipt" in gate.reason


def test_a_malformed_obligation_fact_caps_nothing():
    """The fact is read the way every other validated fact is read: a shape
    the gate does not understand states nothing, and a gate must not invent a
    blocker out of a corrupt key."""
    for value in ("373f63e5a0a4", 3, None, [], {}):
        gate = validate_phase_claim(
            _claim("partial"),
            ValidatorState.GREEN,
            reason=POLARIS_REASON,
            validated_facts={OPEN_OBLIGATIONS_FACT: value},
        )

        assert gate.validated_outcome is PhaseOutcome.SUCCESS
