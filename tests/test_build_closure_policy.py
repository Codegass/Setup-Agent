# tests/test_build_closure_policy.py
"""Build phase closure policy (spec §3.4-7): no blocked/failed closure
without one real build attempt receipt, and a missing OS package/venv module
is a local repairable prerequisite, never an external blocker."""

from types import SimpleNamespace

from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import (
    add_published_mutable_json,
    canonical_json,
    strict_published_evidence,
)
from sag.agent.attempt_policy import (
    build_attempt_requirement,
    has_build_attempt_receipt,
    local_prerequisite_signature,
)
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.agent.invocation_receipts import RECEIPT_DIR
from sag.tools.base import ToolResult
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.phase_tool import PhaseTool

TARGET_SHA = "b" * 40


class BuildManifestOrch:
    def __init__(self, manifest=None, receipts=()):
        # Live authority requires a complete v1 manifest. v1 has no top-level
        # `build_system`; the surveyed build_islands carry the coordinates.
        self.manifest = (
            manifest
            if manifest is not None
            else complete_build_requirements_v1(
                project_root="/workspace/tvm",
                build_system="pytest",
                build_islands=[{"root": "/workspace/tvm", "system": None}],
            )
        )
        self.receipts = {receipt["receipt_id"]: receipt for receipt in receipts}
        self.evidence = strict_published_evidence(
            self,
            run_id="build-policy",
            target_sha=TARGET_SHA,
            receipts=tuple(receipts),
        )
        add_published_mutable_json(
            self,
            self.evidence,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    def execute_command(self, command, workdir=None, timeout=None):
        if "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.evidence(command)
        if "run-pin.json" in command or RECEIPT_DIR in command:
            return self.evidence(command)
        return {"success": True, "exit_code": 0, "output": canonical_json(self.manifest)}

    def execute_control_command(self, command, **kwargs):
        # Strict evidence reads only accept the clean host-control channel.
        return self.execute_command(command)


def _state_with_build_receipt(attempt_id="build-1"):
    receipt_id = "receipt-build-1"
    state = RunEvidenceState(run_id="build-policy")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_failure(
            output="pip failed",
            error="deps failed",
            error_code="DEPS_FAILED",
            metadata={"receipt_id": receipt_id},
        ),
        params={"action": "compile", "working_directory": "/workspace/tvm"},
        source_phase="build",
        source_attempt_id=attempt_id,
    )
    receipt = {
        "schema_version": 3,
        "receipt_id": receipt_id,
        "run_id": "build-policy",
        "tool": "python",
        "requested_action": "compile",
        "effective_action": "compile",
        "working_directory": "/workspace/tvm",
        "actual_cwd": "/workspace/tvm",
        "target_sha": TARGET_SHA,
        "domain_id": "/workspace/tvm",
        "outcome": "failed",
        "exit_code": 1,
    }
    return state, receipt


def test_no_build_attempt_blocks_closure():
    state = RunEvidenceState(run_id="build-policy")
    requirement = build_attempt_requirement(
        state, BuildManifestOrch(), phase="build", attempt_id="build-1"
    )
    assert requirement is not None
    facts = requirement.to_metadata()
    assert facts["terminal_build_receipts"] == 0
    # Premise: v1 forbids a top-level build_system, so the requirement's
    # coordinates are the surveyed islands rather than one system name.
    assert "build_system" not in facts
    assert facts["build_islands"] == [{"root": "/workspace/tvm", "system": None}]
    assert "required_action" not in facts


def test_a_real_attempt_allows_closure():
    state, receipt = _state_with_build_receipt()
    orch = BuildManifestOrch(receipts=[receipt])
    assert (
        has_build_attempt_receipt(
            state,
            attempt_id="build-1",
            orchestrator=orch,
            manifest=orch.manifest,
        )
        is True
    )
    assert build_attempt_requirement(state, orch, phase="build", attempt_id="build-1") is None


def test_tampered_manifest_cannot_authorize_build_closure():
    state, _receipt = _state_with_build_receipt()
    orch = BuildManifestOrch()
    orch.evidence.files[REQUIREMENTS_PATH] = canonical_json(
        {**orch.manifest, "build_system": "forged"}
    )

    requirement = build_attempt_requirement(state, orch, phase="build", attempt_id="build-1")

    assert requirement is not None
    assert requirement.manifest_status == "unavailable"
    assert requirement.build_system is None


def test_local_prerequisites_are_classified():
    assert local_prerequisite_signature("ensurepip is not available") is not None
    assert local_prerequisite_signature("gradlew: line 180: unzip: command not found") is not None
    assert local_prerequisite_signature("network unreachable: proxy denied") is None


def _phase_tool(orch, state, gate):
    machine = SimpleNamespace(
        current_phase="build", is_complete=False, current_attempt_id="build-1"
    )
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=orch,
        project_name="tvm",
        gate_fn=gate,
    )
    tool.run_evidence_state = state
    return tool


def test_blocked_without_build_attempt_is_rejected_with_repair():
    state = RunEvidenceState(run_id="build-policy")
    gate_calls = []
    tool = _phase_tool(BuildManifestOrch(), state, lambda *a: gate_calls.append(a))
    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="ensurepip is not available in the environment",
        evidence=["output_x"],
    )
    assert result.succeeded is False
    assert result.error_code in ("BUILD_ATTEMPT_REQUIRED", "LOCAL_PREREQUISITE_NOT_BLOCKER")
    assert result.metadata["control_disposition"] == "repair_required"
    assert result.suggestions == []
    assert "build(action" not in result.output
    assert gate_calls == []  # rejected before the gate
