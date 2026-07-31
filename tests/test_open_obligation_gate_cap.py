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

from types import SimpleNamespace

from test_react_engine_phase_wiring import _engine_with_machine
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


# ---------------------------------------------------------------------------
# the floor safety net still closes a starved attempt (round-3 blocker)
# ---------------------------------------------------------------------------
#
# `_enforce_phase_floors` is the safety net that stops a budget-exhausted phase
# from hanging, and it closes UNCONDITIONALLY: it derives its own claim from the
# validator state, so claim == validated always held and the gate always
# confirmed it. Round two's cap rewrote `validated` and left the derived claim
# at SUCCESS, so the gate CONTRADICTED the harness's own claim,
# `PhaseMachine.close_attempt` raised ValueError('a rejected phase claim cannot
# close an attempt'), the run loop's `except Exception` turned it into
# `abort(reason='engine exception: ValueError')` — and a starved phase with one
# job still out terminated the whole run ABORTED with the report phase skipped.
# Every precondition co-occurs by construction: `_inspect_phase` returns the
# GREEN state and the open-obligation fact from the SAME probe dict.
#
# The invariant the floor relies on is that the claim it derives is the outcome
# the gate will validate. The cap is a pure function of (validator state, open
# obligations), so the derived claim is capped the same way.


def _starved_engine(phase, facts, *, iteration):
    """A phase whose floor has been reached, with `facts` on the probe."""
    engine = _engine_with_machine(start_phase=phase)
    engine.current_iteration = iteration
    engine.gates = []
    engine._phase_gate_check = lambda _phase: {
        "ok": True,
        "reason": POLARIS_REASON,
        "suggestions": [],
        "validator_state": "green",
        "evidence_refs": ["file:///workspace/polaris/build.log"],
        "validated_facts": dict(facts),
    }
    # A spy on the gate the floor actually built — not a re-implementation of
    # it. `_emit_control_gate` is the one seam both claim and gate pass through.
    engine._emit_control_gate = lambda claim, gate: engine.gates.append((claim, gate))
    return engine


def test_the_starved_build_floor_closes_an_honest_partial_while_a_job_is_open():
    """p7d polaris, at floor exhaustion: green evidence, one compile job with no
    terminal receipt. The attempt closes `partial` and the phase routes."""
    engine = _starved_engine(
        "build",
        {"build.test_entry_ready": True, **OPEN},
        iteration=131,
    )

    forced = engine._enforce_phase_floors()

    assert forced is True
    record = engine.phase_machine.records[0]
    assert record.termination.value == "completed"
    assert record.outcome is PhaseOutcome.PARTIAL
    assert record.transition is not None
    assert engine.phase_machine.current_phase == "test"
    assert engine.finalized_reasons == []  # nothing aborted the run


def test_the_floor_derives_the_claim_the_gate_confirms():
    """The floor cannot decline: it has no second move. So the claim it derives
    must be the one the gate accepts, cap included."""
    engine = _starved_engine(
        "build",
        {"build.test_entry_ready": True, **OPEN},
        iteration=131,
    )

    engine._enforce_phase_floors()

    (claim, gate) = engine.gates[-1]
    assert claim.claimed_outcome is PhaseOutcome.PARTIAL
    assert gate.validated_outcome is claim.claimed_outcome
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.accepted is True
    assert f"job {JOB} has no terminal receipt" in gate.reason


def test_a_settled_ledger_leaves_the_starved_floor_exactly_as_it_was():
    """The provision template this reproduction was copied from: with nothing
    open, the floor closes `success` on green evidence, as it always has."""
    engine = _starved_engine(
        "provision",
        {"provision.workspace_ready": True},
        iteration=121,
    )

    forced = engine._enforce_phase_floors()

    assert forced is True
    record = engine.phase_machine.records[0]
    assert record.outcome is PhaseOutcome.SUCCESS
    assert engine.phase_machine.current_phase == "analyze"


def test_the_starved_floor_still_reports_red_evidence_as_failed():
    """The cap only ever removes strength from GREEN. A red probe is untouched,
    open obligation or not."""
    engine = _starved_engine(
        "provision",
        {"provision.workspace_ready": False, **OPEN},
        iteration=121,
    )
    engine._phase_gate_check = lambda _phase: {
        "ok": False,
        "reason": "no workspace",
        "suggestions": [],
        "validator_state": "red",
        "evidence_refs": ["workspace:///missing"],
        "validated_facts": {"provision.workspace_ready": False, **OPEN},
    }

    forced = engine._enforce_phase_floors()

    assert forced is True
    assert engine.phase_machine.records[0].outcome is PhaseOutcome.FAILED


def _floor_engine_on_the_real_gate(monkeypatch, orchestrator):
    """A starved build phase whose probe is the PRODUCTION `_phase_gate_check`.

    Only the physical inspection is stubbed green. The validator state and the
    open-obligation fact then reach the floor the way they do in a live run —
    out of the same probe dict, through `check_phase_done` -> `_inspect_phase`
    -> the container's own ledger — instead of out of a hand-written dict.
    """
    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda phase, validator, orchestrator, project_name: _ValidatorObservation(
            ValidatorState.GREEN,
            reason=POLARIS_REASON,
            code="build_verified",
            validated_facts={"build.test_entry_ready": True},
        ),
    )
    engine = _engine_with_machine(start_phase="build")
    engine.current_iteration = 131
    engine.orchestrator = orchestrator
    engine.physical_validator = SimpleNamespace(docker_orchestrator=orchestrator)
    return engine


def test_the_floor_closes_partial_on_the_obligation_the_real_gate_found(monkeypatch):
    """End to end at the seam that aborted the run: a job with no exit file in
    the container's ledger, a green inspection, a starved build floor."""
    engine = _floor_engine_on_the_real_gate(monkeypatch, Orchestrator(terminated=False))

    forced = engine._enforce_phase_floors()

    assert forced is True
    record = engine.phase_machine.records[0]
    assert record.termination.value == "completed"
    assert record.outcome is PhaseOutcome.PARTIAL
    assert engine.phase_machine.current_phase == "test"
    assert engine.finalized_reasons == []
    # Premise corrected (#28, both round-four reviewers): the open-jobs list
    # travels on the gate result and the gate_decision control event — never
    # into run state, where the `run.` prefix would land it in the
    # PROJECT_ANALYSIS epoch vector and a diagnostic write would count as
    # material progress for retry authority.
    assert engine.run_evidence_state.fact_value(OPEN_OBLIGATIONS_FACT) is None


def test_the_floor_closes_success_once_the_gate_settles_the_books(monkeypatch):
    """The other half of the same seam: trigger 2 settles the terminated job
    BEFORE grading, so nothing is open and the floor closes success. The cap
    costs an honest run nothing."""
    engine = _floor_engine_on_the_real_gate(monkeypatch, Orchestrator(terminated=True))

    forced = engine._enforce_phase_floors()

    assert forced is True
    assert engine.phase_machine.records[0].outcome is PhaseOutcome.SUCCESS
    assert engine.run_evidence_state.fact_value(OPEN_OBLIGATIONS_FACT) is None


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
