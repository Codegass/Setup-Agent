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
import shlex
from types import SimpleNamespace

from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import add_published_mutable_json
from test_job_settlement import (
    AFTER,
    CONTAINER_ID,
    DOCKER_EXEC_ID,
    JOB,
    LOG_PATH,
    POLARIS_LOG,
    TERMINAL_AUTHORITY,
    TERMINAL_IDENTITY,
    JobContainer,
    _obligation,
)

from sag.agent import phase_gates
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.job_obligations import write_obligation
from sag.agent.phase_gates import (
    JOB_BARRIER_FACT,
    OPEN_OBLIGATIONS_FACT,
    ClaimDisposition,
    GateControlDisposition,
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

    def __init__(self, *, manifest=None):
        files = {LOG_PATH: POLARIS_LOG}
        if manifest is not None:
            files[BUILD_REQUIREMENTS_PATH] = json.dumps(manifest)
        super().__init__(files=files, reports=AFTER)

    def __call__(self, command, **kwargs):
        tokens = shlex.split(command) if "\n" not in command else []
        if len(tokens) == 3 and tokens[:2] == ["cat", "--"]:
            self.commands.append(command)
            path = tokens[2]
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {
                "success": False,
                "exit_code": 1,
                "output": f"cat: {path}: No such file or directory",
            }
        return super().__call__(command, **kwargs)


class Orchestrator:
    def __init__(self, *, terminated=False, manifest=None):
        self.filesystem = Container(manifest=manifest)
        terminal_state = "finished" if terminated else "running"
        terminal_exit_code = 0 if terminated else None
        self.detached_terminal_states = {
            DOCKER_EXEC_ID: (terminal_state, terminal_exit_code),
        }
        self.terminal_inspections = []
        self.detached_handle = {
            **TERMINAL_IDENTITY,
            "terminal_authority": TERMINAL_AUTHORITY,
            "docker_exec_id": DOCKER_EXEC_ID,
            "container_id": CONTAINER_ID,
        }
        if manifest is not None:
            add_published_mutable_json(
                self,
                self.filesystem,
                path=BUILD_REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=manifest,
            )
        assert write_obligation(self.execute_control_command, _obligation())

    def execute_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)

    def execute_control_command(self, command, **kwargs):
        return self.filesystem(command, **kwargs)

    def set_detached_terminal_state(self, exec_id, state, exit_code=None):
        self.detached_terminal_states[exec_id] = (state, exit_code)

    def inspect_detached_terminal(self, handle):
        self.terminal_inspections.append(dict(handle))
        if (
            handle.get("terminal_authority") != TERMINAL_AUTHORITY
            or handle.get("container_id") != CONTAINER_ID
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "probe_error": "terminal_identity_mismatch",
            }
        state, exit_code = self.detached_terminal_states.get(
            handle.get("docker_exec_id"),
            ("unknown", None),
        )
        if state == "running":
            return {
                "probe_success": True,
                "state": "running",
                "running": True,
                "finished": False,
                "exit_code": None,
            }
        if state == "finished":
            return {
                "probe_success": True,
                "state": "finished",
                "running": False,
                "finished": True,
                "exit_code": exit_code,
            }
        return {
            "probe_success": False,
            "state": "unknown",
            "probe_error": "terminal_state_unknown",
        }


def _manifest():
    islands = [{"root": root, "system": system} for root, system in ISLANDS]
    return complete_build_requirements_v1(
        project_root=POLARIS,
        build_system="gradle",
        root_shape="pathological_aggregator",
        build_root=ISLANDS[0][0],
        build_islands=islands,
    )


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
    """Both read the same controller-owned wait disposition."""
    _inspect(monkeypatch)
    orchestrator = Orchestrator(terminated=False)

    probe = check_phase_done("build", None, orchestrator, "polaris")
    gate = check_phase_claim("build", _claim("success"), None, orchestrator, "polaris")

    assert probe["validated_facts"][OPEN_OBLIGATIONS_FACT] == [JOB]
    assert probe["validator_state"] == gate.validator_state.value == "unavailable"
    assert probe["ok"] is False
    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert probe["control_disposition"] == "wait_required"
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED


def test_the_capped_probe_names_the_job_that_capped_it(monkeypatch):
    """The wait projection names its controller state without project prose."""
    _inspect(monkeypatch)

    probe = check_phase_done("build", None, Orchestrator(terminated=False), "polaris")

    assert probe["reason"] == "controller job barrier is active; project evidence was not inspected"
    assert probe["validated_facts"][JOB_BARRIER_FACT] == [{"job_id": JOB, "state": "running"}]


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


def test_an_untried_island_and_an_open_job_leave_no_model_terminal_claim(monkeypatch):
    """While the controller barrier is active the model may not close a phase."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == []
    assert refused["done/success"].error_code == "job_controller_barrier"
    assert refused["done/failed"].error_code == "job_controller_barrier"
    assert refused["blocked/failed"].error_code == "job_controller_barrier"


def test_partial_cannot_bypass_the_island_rule_during_controller_wait(monkeypatch):
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    result = tool.execute(
        action="done",
        outcome="partial",
        key_results="one island built, one compile job still out",
        evidence=["file:///workspace/polaris/build.log"],
    )

    assert result.succeeded is False
    assert result.error_code == "job_controller_barrier"
    assert "phase_signal" not in result.metadata


def test_settled_books_remove_only_the_controller_barrier(monkeypatch):
    """Settlement removes only the controller barrier.

    The in-memory ``runner_dispatched`` observation is not a durable invocation
    receipt: a partial closure still owes the untried island, while a failed
    closure first owes one mechanically proven build attempt.
    """
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=True, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == ["done/success"]
    assert refused["done/partial"].error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert refused["done/failed"].error_code == "BUILD_ATTEMPT_REQUIRED"
    assert "build_attempt_requirement" in refused["done/failed"].facts


def test_a_failed_build_never_rides_an_open_job_out_of_the_island_rule(monkeypatch):
    """The cap only ever fires on GREEN, so only the claim it substitutes for
    `success` is exempt. A red build with an untried island is the bigtop case
    the rule exists for, open job or not."""
    _inspect(monkeypatch, state=ValidatorState.RED)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    accepted, refused = _available_claims(tool)

    assert accepted == []
    assert refused["done/failed"].error_code == "job_controller_barrier"
    assert refused["done/partial"].error_code == "job_controller_barrier"


def test_a_partial_build_still_owes_the_island_an_attempt(monkeypatch):
    """And an open job must never become the way to unlock a closure the
    evidence does not support — dispatching work cannot buy an exemption."""
    _inspect(monkeypatch, state=ValidatorState.PARTIAL)
    tool = _phase_tool(Orchestrator(terminated=False, manifest=_manifest()))

    _, refused = _available_claims(tool)

    assert refused["done/partial"].error_code == "job_controller_barrier"


def test_a_blocked_claim_on_green_evidence_is_refused_while_a_job_is_open(monkeypatch):
    """Controller wait wins before any project-level blocked classification."""
    _inspect(monkeypatch)
    tool = _phase_tool(Orchestrator(terminated=False))  # no islands surveyed

    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="the gradle daemon cannot reach the internal mirror",
        evidence=["file:///workspace/polaris/build.log"],
    )

    assert result.succeeded is False
    assert result.error_code == "job_controller_barrier"
    assert result.metadata["control_disposition"] == "wait_required"


# ---------------------------------------------------------------------------
# 3. a cap that cannot be discharged is a dead end
# ---------------------------------------------------------------------------


def test_a_sealed_run_cannot_grade_past_a_live_job(monkeypatch):
    _inspect(monkeypatch, facts={"report.delivered": True})

    gate = check_phase_claim(
        "report",
        _claim("success", phase="report"),
        None,
        Orchestrator(terminated=False),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.accepted is False
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED
    assert gate.validated_facts[OPEN_OBLIGATIONS_FACT] == [JOB]


def test_a_sealed_gate_does_not_mutate_a_stale_running_record(monkeypatch):
    _inspect(monkeypatch, facts={"report.delivered": True})

    gate = check_phase_claim(
        "report",
        _claim("success", phase="report"),
        None,
        Orchestrator(terminated=True),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED
    assert gate.validated_facts[OPEN_OBLIGATIONS_FACT] == [JOB]


def test_an_unsealed_live_job_returns_controller_wait(monkeypatch):
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
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED
    assert gate.code == "job_controller_barrier"
    assert any("controller" in suggestion.lower() for suggestion in gate.suggestions)


def test_the_polaris_upgrade_stays_dead_behind_a_sealed_barrier(monkeypatch):
    _inspect(monkeypatch)

    gate = check_phase_claim(
        "build",
        _claim("partial"),
        None,
        Orchestrator(terminated=False),
        "polaris",
        sealed=True,
    )

    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED
    assert JOB in gate.reason
