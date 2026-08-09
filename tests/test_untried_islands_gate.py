# tests/test_untried_islands_gate.py
"""Untried-islands closure gate (spec §3.4 island guarantee, §3.3 message
standard): the build phase may not close by giving up while surveyed
independent build islands have no attempt receipt bound to them.

Live evidence (bigtop 2026-07-18/r1): the agent hammered one broken island
and closed the phase while three healthy islands were never touched. The
spec moved that guarantee into the gate; Plan 4 Task 4 implements it."""

from types import SimpleNamespace

from container_evidence_fakes import add_published_mutable_json, strict_published_evidence
from sag.agent.attempt_policy import untried_islands_requirement
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.tools.base import ToolResult
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.phase_tool import PhaseTool

BIGTOP = "/workspace/bigtop"
ISLANDS = (
    (f"{BIGTOP}/bigtop-test-framework", "maven"),
    (f"{BIGTOP}/bigtop-data-generators", "gradle"),
    (f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-spark", "gradle"),
    (f"{BIGTOP}/bigtop-bigpetstore/bigpetstore-transaction-queue", "gradle"),
)
TARGET_SHA = "a" * 40


def _manifest(islands=ISLANDS):
    return {
        "survey": {"project_path": BIGTOP},
        "root_shape": "pathological_aggregator",
        "build_system": "maven",
        "build_root": ISLANDS[0][0],
        "build_islands": [{"root": root, "system": system} for root, system in islands],
    }


class ManifestOrch:
    """Plain-`cat` manifest transport, matching the build-closure test double."""

    def __init__(self, manifest=None, readable=True, receipts=()):
        self.manifest = manifest
        self.readable = readable
        self.receipts = {receipt["receipt_id"]: receipt for receipt in receipts}
        self.evidence = strict_published_evidence(
            self,
            run_id="island-gate",
            target_sha=TARGET_SHA,
            receipts=tuple(receipts),
        )
        if isinstance(manifest, dict):
            add_published_mutable_json(
                self,
                self.evidence,
                path=REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=manifest,
            )

    def execute_command(self, command, workdir=None, timeout=None):
        if not self.readable:
            return {"success": False, "exit_code": 1, "output": "No such file"}
        return self.evidence(command)


def _state_with_island_attempts(*roots, succeeded=False):
    """One dispatched build receipt per island root (outcome irrelevant)."""
    state = RunEvidenceState(run_id="island-gate")
    receipts = []
    for index, root in enumerate(roots):
        receipt_id = f"receipt-island-{index}"
        metadata = {"receipt_id": receipt_id}
        if succeeded:
            result = ToolResult.completed_success(
                output="BUILD SUCCESS",
                facts={"system": "maven"},
                metadata=metadata,
            )
        else:
            result = ToolResult.completed_failure(
                output="BUILD FAILURE",
                error="compilation failure",
                metadata=metadata,
            )
        state.ingest_tool_result(
            StateScope.ARTIFACTS,
            "build",
            result,
            params={"action": "compile", "working_directory": root},
            source_phase="build",
            source_attempt_id="build-1",
            execution_id=f"exec-{index}",
        )
        receipts.append(
            {
                "schema_version": 2,
                "receipt_id": receipt_id,
                "run_id": "island-gate",
                "tool": "maven",
                "requested_action": "compile",
                "effective_action": "compile",
                "working_directory": root,
                "actual_cwd": root,
                "target_sha": TARGET_SHA,
                "domain_id": next(
                    (island_root for island_root, _ in ISLANDS if root.startswith(island_root)),
                    BIGTOP,
                ),
                "outcome": "completed" if succeeded else "failed",
                "exit_code": 0 if succeeded else 1,
            }
        )
    return state, receipts


# --------------------------------------------------------------------------- #
# Policy layer
# --------------------------------------------------------------------------- #
def test_closure_with_one_island_attempted_names_the_other_three():
    state, receipts = _state_with_island_attempts(ISLANDS[0][0])
    requirement = untried_islands_requirement(
        state,
        ManifestOrch(_manifest(), receipts=receipts),
        phase="build",
        signal="blocked",
        outcome="failed",
    )
    assert requirement is not None
    assert list(requirement.roots) == [root for root, _ in ISLANDS[1:]]
    message = requirement.message()
    for root, _ in ISLANDS[1:]:
        assert root in message
    # The attempted island is not re-demanded.
    assert message.count(ISLANDS[0][0]) == 0
    # The judge names facts and blockers but never selects the next command.
    assert "NEXT REQUIRED ACTION" not in message
    assert "build(action" not in message
    assert "phase(" not in message
    assert "required_action" not in requirement.to_metadata()


def test_all_islands_attempted_with_mixed_outcomes_passes_the_policy():
    state, receipts = _state_with_island_attempts(ISLANDS[0][0], ISLANDS[1][0], ISLANDS[3][0])
    # The remaining island was attempted successfully — attempted is the bar.
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_success(
            output="BUILD SUCCESSFUL",
            facts={"system": "gradle"},
            metadata={"receipt_id": "receipt-island-green"},
        ),
        params={"action": "compile", "working_directory": ISLANDS[2][0]},
        source_phase="build",
        source_attempt_id="build-1",
        execution_id="exec-green",
    )
    receipts.append(
        {
            "schema_version": 2,
            "receipt_id": "receipt-island-green",
            "run_id": "island-gate",
            "tool": "gradle",
            "requested_action": "compile",
            "effective_action": "compile",
            "working_directory": ISLANDS[2][0],
            "actual_cwd": ISLANDS[2][0],
            "target_sha": TARGET_SHA,
            "domain_id": ISLANDS[2][0],
            "outcome": "completed",
            "exit_code": 0,
        }
    )
    assert (
        untried_islands_requirement(
            state,
            ManifestOrch(_manifest(), receipts=receipts),
            phase="build",
            signal="done",
            outcome="partial",
        )
        is None
    )


def test_receipt_binds_when_the_attempt_ran_below_the_island_root():
    state, receipts = _state_with_island_attempts(
        f"{ISLANDS[0][0]}/submodule",
        ISLANDS[1][0],
        ISLANDS[2][0],
        ISLANDS[3][0],
    )
    assert (
        untried_islands_requirement(
            state,
            ManifestOrch(_manifest(), receipts=receipts),
            phase="build",
            signal="blocked",
            outcome="failed",
        )
        is None
    )


def test_resolved_working_directory_from_metadata_does_not_bind_without_receipt():
    """Raw metadata is a claim; only a durable receipt can mark an island tried."""
    state = RunEvidenceState(run_id="island-gate")
    for index, (root, _system) in enumerate(ISLANDS):
        state.ingest_tool_result(
            StateScope.ARTIFACTS,
            "build",
            ToolResult.completed_failure(
                output="BUILD FAILURE",
                error="compilation failure",
                metadata={
                    "runner_dispatched": True,
                    "command": "mvn -B install",
                    "working_directory": root,
                },
            ),
            params={"action": "compile"},
            source_phase="build",
            source_attempt_id="build-1",
            execution_id=f"meta-{index}",
        )
    requirement = untried_islands_requirement(
        state,
        ManifestOrch(_manifest()),
        phase="build",
        signal="blocked",
        outcome="failed",
    )
    assert requirement is not None
    assert requirement.roots == tuple(root for root, _ in ISLANDS)


def test_undispatched_call_at_an_island_is_not_an_attempt_receipt():
    state = RunEvidenceState(run_id="island-gate")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_failure(
            output="No known build system marker found",
            error="unknown build system",
            metadata={"runner_dispatched": False},
        ),
        params={"action": "compile", "working_directory": ISLANDS[1][0]},
        source_phase="build",
        source_attempt_id="build-1",
    )
    requirement = untried_islands_requirement(
        state,
        ManifestOrch(_manifest()),
        phase="build",
        signal="blocked",
        outcome="failed",
    )
    assert requirement is not None
    assert ISLANDS[1][0] in requirement.roots


def test_success_claim_is_exempt():
    state, _ = _state_with_island_attempts(ISLANDS[0][0], succeeded=True)
    assert (
        untried_islands_requirement(
            state,
            ManifestOrch(_manifest()),
            phase="build",
            signal="done",
            outcome="success",
        )
        is None
    )


def test_absent_or_empty_islands_are_exempt():
    state = RunEvidenceState(run_id="island-gate")
    empty = _manifest(islands=())
    no_key = _manifest()
    no_key.pop("build_islands")
    for manifest in (empty, no_key):
        assert (
            untried_islands_requirement(
                state,
                ManifestOrch(manifest),
                phase="build",
                signal="blocked",
                outcome="failed",
            )
            is None
        )


def test_a_single_untried_island_is_still_named():
    """The exemption list is empty/absent islands — not 'only one island'."""
    state = RunEvidenceState(run_id="island-gate")
    requirement = untried_islands_requirement(
        state,
        ManifestOrch(_manifest(islands=ISLANDS[:1])),
        phase="build",
        signal="done",
        outcome="partial",
    )
    assert requirement is not None
    assert list(requirement.roots) == [ISLANDS[0][0]]


def test_unreadable_manifest_raises_no_island_requirement():
    state = RunEvidenceState(run_id="island-gate")
    assert (
        untried_islands_requirement(
            state,
            ManifestOrch(readable=False),
            phase="build",
            signal="blocked",
            outcome="failed",
        )
        is None
    )
    assert (
        untried_islands_requirement(
            state,
            None,
            phase="build",
            signal="blocked",
            outcome="failed",
        )
        is None
    )


def test_non_build_phase_is_exempt():
    state = RunEvidenceState(run_id="island-gate")
    assert (
        untried_islands_requirement(
            state,
            ManifestOrch(_manifest()),
            phase="test",
            signal="blocked",
            outcome="failed",
        )
        is None
    )


# --------------------------------------------------------------------------- #
# phase_tool surface
# --------------------------------------------------------------------------- #
class GateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, phase, claim, validator, orchestrator, project_name, *, sealed=False):
        self.calls.append(phase)
        raise AssertionError("the gate must not be reached")


def _phase_tool(orch, state, gate):
    machine = SimpleNamespace(
        current_phase="build", is_complete=False, current_attempt_id="build-1"
    )
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=orch,
        project_name="bigtop",
        gate_fn=gate,
    )
    tool.run_evidence_state = state
    return tool


def test_phase_blocked_with_untried_islands_is_rejected_before_the_gate():
    gate = GateRecorder()
    state, receipts = _state_with_island_attempts(ISLANDS[0][0])
    tool = _phase_tool(ManifestOrch(_manifest(), receipts=receipts), state, gate)

    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="the framework island fails to compile",
        evidence=["output_x"],
    )

    assert result.succeeded is False
    assert result.error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert gate.calls == []
    for root, _ in ISLANDS[1:]:
        assert root in result.output
    gate_facts = result.metadata["gate_result"]["validated_facts"]
    assert gate_facts["untried_islands"]["untried_island_roots"] == [
        root for root, _ in ISLANDS[1:]
    ]
    assert result.suggestions == []
    assert "required_action" not in gate_facts["untried_islands"]
    assert "build(action" not in result.output


def test_phase_done_partial_with_untried_islands_is_rejected():
    gate = GateRecorder()
    state, receipts = _state_with_island_attempts(ISLANDS[0][0])
    tool = _phase_tool(ManifestOrch(_manifest(), receipts=receipts), state, gate)

    result = tool.execute(
        action="done",
        outcome="partial",
        key_results="one island built",
        evidence=["output_x"],
    )

    assert result.succeeded is False
    assert result.error_code == "ISLAND_ATTEMPT_REQUIRED"
    assert gate.calls == []


def test_phase_done_success_reaches_the_gate_with_untried_islands():
    calls = []

    def gate(phase, claim, validator, orchestrator, project_name, *, sealed=False):
        calls.append(phase)
        raise RuntimeError("gate reached")

    state, receipts = _state_with_island_attempts(ISLANDS[0][0], succeeded=True)
    tool = _phase_tool(ManifestOrch(_manifest(), receipts=receipts), state, gate)

    try:
        tool.execute(
            action="done",
            outcome="success",
            key_results="green",
            evidence=["output_x"],
        )
    except RuntimeError:
        pass
    assert calls == ["build"]
