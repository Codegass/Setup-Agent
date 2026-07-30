# tests/test_settlement_cap_blast_radius.py
"""Plan 8 §3.3 — what else assumed `success` was always reachable.

Round two added the cap: "a GREEN validator state is capped to PARTIAL while an
obligation is open: success requires settled books". That single sentence makes
`success` the ONE outcome a gate cannot accept while a job is still out, and
three places were written on the assumption that it was always available.

1. The mid-phase evidence nudge computed "would the gate pass" from the UNCAPPED
   validator state (`check_phase_done`'s `ok`), while the completion gate applied
   the cap. Two computations answering one question (P3) — and the harness told
   the model the gate passes on exactly the evidence the gate then contradicted.

2. The untried-islands rule exempts `done/success` on the stated ground that
   "the physical gate below checks it". With the cap, success is the one claim
   the gate cannot accept while a job is open — so a build phase with untried
   surveyed islands and an open obligation had NO accepted terminal claim:
   success exempted by the island rule and contradicted by the cap, partial
   refused by the island rule. The two refusals pointed at each other.

3. The cap was unscoped by the run's evidence seal. A sealed run settles
   nothing (§3.2), so after evidence-close the obligation it still names can
   never be discharged: a report-phase success claim on a delivered report was
   CONTRADICTED on books the run is no longer allowed to settle, and a job that
   DID terminate before the report claim recorded `partial` where the evidence
   was complete. A cap that cannot be discharged is not a cap, it is a dead end.

The direction is untouched everywhere: the no-refinement-above-the-claim half of
§3.3 still fires on a sealed run, so the p7d polaris upgrade (claimed partial ->
validated SUCCESS) stays dead in every state below.
"""

import json
from types import SimpleNamespace

from test_job_settlement import JobContainer, _obligation
from test_job_settlement import AFTER, EXIT_PATH, JOB, LOG_PATH, POLARIS_LOG

from sag.agent import phase_gates
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.job_obligations import write_obligation
from sag.agent.phase_gates import (
    OPEN_OBLIGATIONS_FACT,
    ClaimDisposition,
    ValidatorState,
    _ValidatorObservation,
    check_phase_claim,
    check_phase_done,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH
from sag.tools.base import ToolResult
from sag.tools.phase_tool import PhaseTool

POLARIS = "/workspace/polaris"
# Two surveyed islands; the run attempts the first and never touches the second.
ISLANDS = ((f"{POLARIS}/build-logic", "gradle"), (f"{POLARIS}/polaris-core", "gradle"))
REASON = "Built 100% of expected classes (>= 100% threshold)"


class Container(JobContainer):
    """The ledger AND the survey manifest, on one container read surface."""

    def __init__(self, *, terminated, manifest=None):
        files = {LOG_PATH: POLARIS_LOG}
        if terminated:
            files[EXIT_PATH] = "0\n"
        if manifest is not None:
            files[BUILD_REQUIREMENTS_PATH] = json.dumps(manifest)
        super().__init__(files=files, reports=AFTER)


class Orchestrator:
    def __init__(self, *, terminated=False, manifest=None):
        self.filesystem = Container(terminated=terminated, manifest=manifest)
        write_obligation(self.execute_command, _obligation())

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)


def _manifest():
    return {
        "survey": {"project_path": POLARIS},
        "build_system": "gradle",
        "build_islands": [{"root": root, "system": system} for root, system in ISLANDS],
    }


def _inspect(monkeypatch, state=ValidatorState.GREEN, facts=None):
    """Stub ONLY the physical inspection; settlement and the cap stay live."""
    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda phase, validator, orchestrator, project_name: _ValidatorObservation(
            state,
            reason=REASON,
            code="build_verified",
            evidence_refs=("file:///workspace/polaris/build.log",),
            validated_facts=dict(facts or {"build.test_entry_ready": True}),
        ),
    )


def _claim(outcome, phase="build", signal="done"):
    return PhaseClaim(phase=phase, signal=signal, claimed_outcome=PhaseOutcome(outcome))


# ---------------------------------------------------------------------------
# 1. the nudge and the gate read one determination
# ---------------------------------------------------------------------------


def test_the_probe_the_nudge_reads_is_the_state_the_gate_grades(monkeypatch):
    """P3. `check_phase_done` answers "would the gate pass"; the gate answers
    "does it pass". One question, so one computation — the cap included."""
    _inspect(monkeypatch)
    orchestrator = Orchestrator(terminated=False)

    probe = check_phase_done("build", None, orchestrator, "polaris")
    gate = check_phase_claim("build", _claim("success"), None, orchestrator, "polaris")

    assert probe["validated_facts"][OPEN_OBLIGATIONS_FACT] == [JOB]
    assert probe["validator_state"] == gate.validator_state.value == "partial"
    assert probe["ok"] is False
    assert gate.disposition is ClaimDisposition.CONTRADICTED


def test_the_capped_probe_names_the_job_that_capped_it(monkeypatch):
    """The reason the floor and the nudge read has to carry the evidence too —
    a state without its sentence is the same two computations again."""
    _inspect(monkeypatch)

    probe = check_phase_done("build", None, Orchestrator(terminated=False), "polaris")

    assert probe["reason"].startswith(REASON)
    assert f"job {JOB} has no terminal receipt — success requires settled books" in probe["reason"]


def test_settled_books_leave_the_probe_green(monkeypatch):
    """The cap costs an honest run nothing: trigger 2 settles the terminated job
    before grading, so the probe is green and `ok` again."""
    _inspect(monkeypatch)

    probe = check_phase_done("build", None, Orchestrator(terminated=True), "polaris")

    assert probe["ok"] is True
    assert probe["validator_state"] == "green"
    assert OPEN_OBLIGATIONS_FACT not in probe["validated_facts"]


def _nudging_engine(orchestrator, *, phase="build"):
    from test_react_engine_phase_wiring import _engine_with_machine

    engine = _engine_with_machine(start_phase=phase)
    engine.orchestrator = orchestrator
    engine.physical_validator = SimpleNamespace(docker_orchestrator=orchestrator)
    engine.steps = []
    engine._phase_iterations = engine.NUDGE_EVERY
    return engine


def test_the_harness_never_says_the_gate_passes_on_evidence_it_contradicts(monkeypatch):
    """The live shape: a build phase deep in a rabbit hole with one compile job
    still out. The nudge used to announce 'the completion gate passes' and the
    gate then refused the success claim it had just invited."""
    _inspect(monkeypatch)
    engine = _nudging_engine(Orchestrator(terminated=False))

    nudged = engine._maybe_nudge_phase_done()

    assert nudged is False
    assert engine.steps == []


def test_the_nudge_still_fires_once_the_books_are_settled(monkeypatch):
    """The guard is the open obligation, not the nudge."""
    _inspect(monkeypatch)
    engine = _nudging_engine(Orchestrator(terminated=True))

    nudged = engine._maybe_nudge_phase_done()

    assert nudged is True
    assert "the completion gate passes" in engine.steps[0].content


# ---------------------------------------------------------------------------
# 2. the model always has one honest terminal claim
# ---------------------------------------------------------------------------

TERMINAL_CLAIMS = (
    ("done", "success"),
    ("done", "partial"),
    ("done", "failed"),
    ("done", "unknown"),
    ("blocked", "partial"),
    ("blocked", "failed"),
    ("blocked", "unknown"),
)


def _state_with_one_island_attempted():
    state = RunEvidenceState(run_id="cap-blast")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="BUILD SUCCESSFUL",
            facts={"system": "gradle"},
            metadata={"runner_dispatched": True, "command": "./gradlew build"},
        ),
        params={"action": "compile", "working_directory": ISLANDS[0][0]},
        source_phase="build",
        source_attempt_id="build-1",
        execution_id="exec-island-0",
    )
    return state


def _phase_tool(orchestrator, *, sealed=False):
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="build", is_complete=False, current_attempt_id="build-1"
        ),
        validator=None,
        orchestrator=orchestrator,
        project_name="polaris",
        gate_fn=check_phase_claim,
    )
    state = _state_with_one_island_attempted()
    if sealed:
        state.seal(finalized_at="2026-07-29T11:17:37Z", close_reason="test_terminated")
    tool.run_evidence_state = state
    return tool


def _available_claims(tool):
    """Every terminal claim the tool accepts in this state, and why it refused
    the rest. One `execute` per claim: nothing here re-implements the gate."""
    accepted, refused = [], {}
    for verb, outcome in TERMINAL_CLAIMS:
        result = tool.execute(
            action=verb,
            outcome=outcome,
            key_results="one island built, one compile job still out",
            reason="the surveyed core island was never attempted",
            evidence=["file:///workspace/polaris/build.log"],
        )
        if result.succeeded:
            accepted.append(f"{verb}/{outcome}")
        else:
            refused[f"{verb}/{outcome}"] = result
    return accepted, refused


def test_an_untried_island_and_an_open_job_still_leave_one_honest_claim(monkeypatch):
    """The state with no exit: success is exempted by the island rule and then
    contradicted by the cap; every other claim is refused by the island rule.
    The claim the gate itself validates on this evidence is `partial`, and that
    is the one the island rule may not refuse — its own stated ground is that
    the physical gate below checks it."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == ["done/partial"]
    # success is refused by the cap, and the two refusals no longer disagree:
    # the claim the cap leaves is the one the island rule lets through.
    assert "success requires settled books" in refused["done/success"].output
    assert refused["done/failed"].error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert refused["blocked/failed"].error_code == "ISLAND_ATTEMPT_REQUIRED"


def test_the_accepted_claim_is_the_outcome_the_gate_validates(monkeypatch):
    """Not a concession: `partial` IS the gate's determination on this evidence,
    so the claim is CONFIRMED and the phase records what the evidence says."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    result = tool.execute(
        action="done",
        outcome="partial",
        key_results="one island built, one compile job still out",
        evidence=["file:///workspace/polaris/build.log"],
    )

    gate = result.metadata["gate_result"]
    assert result.succeeded is True
    assert gate["validated_outcome"] == "partial"
    assert gate["claim_disposition"] == "confirmed"
    assert gate["validated_facts"][OPEN_OBLIGATIONS_FACT] == [JOB]


def test_settled_books_leave_the_island_rule_exactly_as_it_was(monkeypatch):
    """With nothing open the only exempt claim is `done/success`, as it has
    always been: a giving-up closure may not abandon an untried island."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=True, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == ["done/success"]
    assert refused["done/partial"].error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert refused["done/failed"].error_code == "ISLAND_ATTEMPT_REQUIRED"


def test_a_failed_build_never_rides_an_open_job_out_of_the_island_rule(monkeypatch):
    """The cap only ever fires on GREEN, so only the claim it substitutes for
    `success` is exempt. A red build with an untried island is the bigtop case
    the rule exists for, open job or not."""
    _inspect(monkeypatch, state=ValidatorState.RED)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == []
    assert refused["done/failed"].error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert refused["done/partial"].error_code == "ISLAND_ATTEMPT_REQUIRED"


def test_a_partial_build_still_owes_the_island_an_attempt(monkeypatch):
    """And an open job must never become the way to unlock a closure the
    evidence does not support — dispatching work cannot buy an exemption."""
    _inspect(monkeypatch, state=ValidatorState.PARTIAL)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    _, refused = _available_claims(tool)

    assert refused["done/partial"].error_code == "ISLAND_ATTEMPT_REQUIRED"


def test_a_blocked_claim_on_green_evidence_is_refused_while_a_job_is_open(monkeypatch):
    """The other consumer that keyed on `success`: `blocked` is reserved for
    external impediments and green evidence contradicts it. That guard read the
    CAPPED outcome, so an open obligation disarmed it."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False))  # no islands surveyed

    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="the gradle daemon cannot reach the internal mirror",
        evidence=["file:///workspace/polaris/build.log"],
    )

    assert result.succeeded is False
    assert result.error_code == "blocked_contradicted_by_green_evidence"


# ---------------------------------------------------------------------------
# 3. a cap that cannot be discharged is a dead end
# ---------------------------------------------------------------------------


def test_a_sealed_run_does_not_cap_a_report_claim_on_books_it_cannot_settle(monkeypatch):
    """After evidence-close the run settles nothing (§3.2), so the obligation the
    ledger still names can never be discharged. The report was delivered; the
    claim is confirmed, and the unsettled job stands on the verdict as the
    `job_unsettled` conflict recorded before the close."""
    _inspect(monkeypatch, facts={"report.delivered": True})

    gate = check_phase_claim(
        "report",
        _claim("success", phase="report"),
        None,
        Orchestrator(terminated=False),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.validated_facts[OPEN_OBLIGATIONS_FACT] == [JOB]


def test_a_job_that_terminated_before_the_report_claim_is_no_longer_a_partial(monkeypatch):
    """The second half: the exit file is on disk, so the books COULD close — but
    a sealed run may not settle them, and naming them open then recorded the
    report phase `partial` on evidence that was complete."""
    _inspect(monkeypatch, facts={"report.delivered": True})

    gate = check_phase_claim(
        "report",
        _claim("success", phase="report"),
        None,
        Orchestrator(terminated=True),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.validated_facts[OPEN_OBLIGATIONS_FACT] == [JOB]


def test_an_unsealed_claim_is_still_capped_and_says_what_can_be_claimed(monkeypatch):
    """Before the close the cap is dischargeable — the job can still terminate
    and settle — so it stays, and the refusal now carries a move."""
    _inspect(monkeypatch, facts={"report.delivered": True})

    gate = check_phase_claim(
        "report",
        _claim("success", phase="report"),
        None,
        Orchestrator(terminated=False),
        "polaris",
        sealed=False,
    )

    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert gate.suggestions
    assert any("partial" in suggestion for suggestion in gate.suggestions)
    assert any(JOB in suggestion for suggestion in gate.suggestions)


def test_the_polaris_upgrade_stays_dead_on_a_sealed_run(monkeypatch):
    """The anchor, re-checked in the state where the cap no longer fires: the
    OTHER half of §3.3 — no refinement above the claim while an obligation is
    open — is not scoped by the seal, and it is what stops the upgrade."""
    _inspect(monkeypatch)

    gate = check_phase_claim(
        "build",
        _claim("partial"),
        None,
        Orchestrator(terminated=False),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert f"job {JOB} has no terminal receipt — the claim is confirmable at most" in gate.reason
