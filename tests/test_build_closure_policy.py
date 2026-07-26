# tests/test_build_closure_policy.py
"""Build phase closure policy (spec §3.4-7): no blocked/failed closure
without one real build attempt receipt, and a missing OS package/venv module
is a local repairable prerequisite, never an external blocker."""

import json
from types import SimpleNamespace

from sag.agent.attempt_policy import (
    build_attempt_requirement,
    has_build_attempt_receipt,
    local_prerequisite_signature,
)
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.tools.base import ToolResult
from sag.tools.phase_tool import PhaseTool


class BuildManifestOrch:
    def __init__(self, manifest=None):
        self.manifest = manifest if manifest is not None else {
            "build_system": "python",
            "test_root": "/workspace/tvm",
        }

    def execute_command(self, command, workdir=None, timeout=None):
        return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}


def _state_with_build_receipt(attempt_id="build-1"):
    state = RunEvidenceState(run_id="build-policy")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_failure(
            output="pip failed",
            error="deps failed",
            error_code="DEPS_FAILED",
            metadata={"runner_dispatched": True, "command": "pip install -e ."},
        ),
        params={"action": "deps", "working_directory": "/workspace/tvm"},
        source_phase="build",
        source_attempt_id=attempt_id,
    )
    return state


def test_no_build_attempt_blocks_closure():
    state = RunEvidenceState(run_id="build-policy")
    message = build_attempt_requirement(
        state, BuildManifestOrch(), phase="build", attempt_id="build-1"
    )
    assert message is not None
    assert "build attempt" in message


def test_a_real_attempt_allows_closure():
    state = _state_with_build_receipt()
    assert has_build_attempt_receipt(state, attempt_id="build-1") is True
    assert (
        build_attempt_requirement(
            state, BuildManifestOrch(), phase="build", attempt_id="build-1"
        )
        is None
    )


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
    assert gate_calls == []  # rejected before the gate
