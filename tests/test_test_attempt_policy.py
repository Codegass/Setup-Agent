import json
import shlex
from types import SimpleNamespace

import pytest
from test_evidence_ingestion import _action_step, _engine, _prepare_action_execution

from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.react_engine import ReActEngine
from sag.agent.reasoning_scheduler import ReasoningScheduler, SchedulerMode
from sag.agent.attempt_policy import (
    TestCandidateResolution,
    forced_test_refusal_receipts,
    has_test_candidate_refresh_receipt,
    required_test_attempt,
    resolve_survey_test_candidates,
    survey_test_candidates,
    terminal_test_receipts,
)
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult
from sag.tools.phase_tool import PhaseTool
from sag.agent.tool_orchestration import ActualToolExecution, ToolCall, ToolExecution


class ManifestOrchestrator:
    def __init__(self):
        self.manifest = {
            "survey": {"project_path": "/workspace/bigtop"},
            "test_root": "/workspace/bigtop/bigtop-data-generators",
            "test_system": "gradle",
            "test_islands": [
                {
                    "root": "/workspace/bigtop/bigtop-data-generators",
                    "system": "gradle",
                },
                {
                    "root": "/workspace/bigtop/bigtop-test-framework",
                    "system": "gradle",
                },
            ],
        }
        self.realpaths = {}
        self.files = {
            "/workspace/bigtop/bigtop-data-generators/build.gradle": "",
            "/workspace/bigtop/bigtop-test-framework/build.gradle": "",
        }

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            path = shlex.split(command)[-1]
            resolved = self.realpaths.get(path, path)
            if resolved is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": f"{resolved}\n"}
        if command.startswith("if test -f "):
            tokens = shlex.split(command)
            path = tokens[tokens.index("-f") + 1]
            output = "__SAG_GRAPH_FILE__" if path in self.files else "__SAG_GRAPH_ABSENT__"
            return {"success": True, "exit_code": 0, "output": output}
        if command.startswith("if test -d "):
            tokens = shlex.split(command)
            path = tokens[tokens.index("-d") + 1]
            prefix = path.rstrip("/") + "/"
            output = (
                "__SAG_GRAPH_DIRECTORY__"
                if any(file_path.startswith(prefix) for file_path in self.files)
                else "__SAG_GRAPH_ABSENT__"
            )
            return {"success": True, "exit_code": 0, "output": output}
        if command.startswith("cat -- "):
            path = shlex.split(command)[-1]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {
                "success": True,
                "exit_code": 0,
                "output": self.files[path],
            }
        return {
            "success": True,
            "exit_code": 0,
            "output": json.dumps(self.manifest),
        }


class UnreadableManifestOrchestrator:
    def execute_command(self, command, workdir=None, timeout=None):
        return {"success": False, "exit_code": 1, "output": ""}


class AcceptingGate:
    def __init__(self):
        self.calls = []

    def __call__(self, phase, claim, validator, orchestrator, project_name):
        self.calls.append(phase)
        return GateResult(
            accepted=True,
            validated_outcome=PhaseOutcome.SUCCESS,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=ValidatorState.GREEN,
            reason="physical test evidence accepted",
            claim=claim,
        )


def _ready_state() -> RunEvidenceState:
    state = RunEvidenceState(run_id="test-attempt-policy")
    state.register_fact(
        StateScope.ARTIFACTS,
        "build.test_entry_ready",
        True,
        "artifact://compiled-classes",
    )
    return state


def _record_gradle_test(
    state: RunEvidenceState,
    result: ToolResult,
    *,
    attempt_id: str = "test-1",
    root: str = "/workspace/bigtop/bigtop-data-generators",
    execution_id: str | None = None,
) -> None:
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "gradle",
        result,
        params={
            "tasks": "test",
            "working_directory": root,
        },
        source_phase="test",
        source_attempt_id=attempt_id,
        execution_id=execution_id,
    )


def test_candidates_keep_the_manifest_primary_coordinate_first():
    candidates = survey_test_candidates(ManifestOrchestrator())

    assert [candidate.root for candidate in candidates] == [
        "/workspace/bigtop/bigtop-data-generators",
        "/workspace/bigtop/bigtop-test-framework",
    ]
    assert candidates[0].required_action == {
        "tool": "build",
        "params": {
            "action": "test",
            "working_directory": "/workspace/bigtop/bigtop-data-generators",
        },
    }


def test_candidates_reject_coordinates_outside_the_survey_project():
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["test_islands"].insert(
        0,
        {"root": "/workspace/other-project/tests", "system": "pytest"},
    )

    candidates = survey_test_candidates(orchestrator)

    assert all(candidate.root.startswith("/workspace/bigtop/") for candidate in candidates)


def test_candidates_require_a_current_survey_project_boundary():
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["survey"] = {}

    resolution = resolve_survey_test_candidates(orchestrator)

    assert resolution.status == "coordinates_missing"
    assert resolution.candidates == ()


def test_project_root_symlink_cannot_escape_the_real_workspace():
    orchestrator = ManifestOrchestrator()
    orchestrator.realpaths["/workspace/bigtop"] = "/outside/bigtop"

    resolution = resolve_survey_test_candidates(orchestrator)

    assert resolution.status == "unsafe_coordinates"
    assert resolution.candidates == ()
    requirement = required_test_attempt(
        _ready_state(),
        orchestrator,
        phase="test",
        attempt_id="test-1",
    )
    assert requirement is not None
    assert requirement.required_action == {
        "tool": "project",
        "params": {"action": "analyze"},
    }


def test_nested_candidate_symlink_cannot_escape_the_real_project_root():
    orchestrator = ManifestOrchestrator()
    candidate = "/workspace/bigtop/bigtop-data-generators"
    orchestrator.realpaths[candidate] = "/workspace/other-checkout/tests"

    resolution = resolve_survey_test_candidates(orchestrator)

    assert resolution.status == "unsafe_coordinates"
    assert resolution.candidates == ()


def test_unreadable_manifest_fails_closed_with_one_analyze_refresh():
    state = _ready_state()
    orchestrator = UnreadableManifestOrchestrator()

    first = required_test_attempt(
        state,
        orchestrator,
        phase="test",
        attempt_id="test-1",
    )
    assert first is not None
    assert first.reason_code == "manifest_unreadable"
    assert first.required_action == {
        "tool": "project",
        "params": {"action": "analyze"},
    }

    state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "project",
        ToolResult.completed_failure(
            output="survey refresh failed",
            error="manifest unavailable",
        ),
        params={"action": "analyze"},
        source_phase="test",
        source_attempt_id="test-1",
    )

    assert has_test_candidate_refresh_receipt(state, attempt_id="test-1")
    assert (
        required_test_attempt(
            state,
            orchestrator,
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_pre_execution_rejection_does_not_unlock_test_termination():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_failure(
            output="selector rejected before execution",
            error="invalid selector",
            error_code="PYTEST_ARGS_REJECTED",
        ),
    )

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=survey_test_candidates(ManifestOrchestrator()),
        )
        == ()
    )
    requirement = required_test_attempt(
        state,
        ManifestOrchestrator(),
        phase="test",
        attempt_id="test-1",
    )
    assert requirement is not None
    assert requirement.required_action["tool"] == "build"


def test_harness_forced_preexecution_refusal_is_bounded_but_not_a_test_receipt():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_failure(
            output="collection policy refused 51 targets before pytest",
            error="too many collection targets",
            error_code="PYTEST_COLLECTION_LIMIT",
            facts={"system": "gradle"},
            metadata={
                "harness_forced_test_attempt": {
                    "phase": "test",
                    "source_attempt_id": "test-1",
                    "root": "/workspace/bigtop/bigtop-data-generators",
                    "system": "gradle",
                    "actual_root": "/workspace/bigtop/bigtop-data-generators",
                    "actual_system": "gradle",
                    "disposition": "no_runner_dispatch",
                    "reason_code": "PYTEST_COLLECTION_LIMIT",
                }
            },
        ),
    )
    candidates = survey_test_candidates(ManifestOrchestrator())

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=candidates,
        )
        == ()
    )
    assert (
        len(
            forced_test_refusal_receipts(
                state,
                attempt_id="test-1",
                candidates=candidates,
            )
        )
        == 1
    )
    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_forced_success_without_runner_command_is_bounded_as_a_nonreceipt():
    state = _ready_state()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    requirement = survey_test_candidates(ManifestOrchestrator())[0]
    result = ToolResult.completed_success(
        output="facade claimed success before runner dispatch",
        facts={"system": "gradle"},
    )
    call = ToolCall(
        name="build",
        raw_params=dict(requirement.required_action["params"]),
        raw_action_text="forced test",
        source_step_index=1,
        model_used="harness",
    )
    execution = ToolExecution(
        call=call,
        result=result,
        status="success",
        raw_params=call.raw_params,
        validated_params=call.raw_params,
        attempted_execution=True,
    )

    engine._mark_forced_test_refusals(execution, requirement)
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        execution.result,
        params=call.raw_params,
        source_phase="test",
        source_attempt_id="test-1",
    )
    candidates = survey_test_candidates(ManifestOrchestrator())

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=candidates,
        )
        == ()
    )
    assert (
        len(
            forced_test_refusal_receipts(
                state,
                attempt_id="test-1",
                candidates=candidates,
            )
        )
        == 1
    )
    assert (
        execution.result.metadata["harness_forced_test_attempt"]["disposition"]
        == "no_runner_dispatch"
    )
    assert execution.result.conflicts


def test_forced_wrong_backend_command_is_bounded_as_a_candidate_mismatch():
    state = _ready_state()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    requirement = survey_test_candidates(ManifestOrchestrator())[0]
    wrong_backend = ToolResult.completed_failure(
        output="Maven ran at a Gradle survey coordinate",
        error="wrong backend",
        error_code="MAVEN_BUILD_FAILED",
        metadata={"command": "mvn test", "runner_dispatched": True},
    )
    call = ToolCall(
        name="build",
        raw_params=dict(requirement.required_action["params"]),
        raw_action_text="forced test",
        source_step_index=1,
        model_used="harness",
    )
    actual = ActualToolExecution(
        tool_name="maven",
        params={
            "command": "test",
            "working_directory": requirement.root,
        },
        result=wrong_backend,
        execution_id="wrong-backend-1",
    )
    execution = ToolExecution(
        call=call,
        result=wrong_backend,
        status="failure",
        raw_params=call.raw_params,
        validated_params=call.raw_params,
        attempted_execution=True,
        actual_executions=[actual],
    )

    engine._mark_forced_test_refusals(execution, requirement)
    marked = execution.actual_executions[0].result
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "maven",
        marked,
        params=actual.params,
        source_phase="test",
        source_attempt_id="test-1",
        execution_id=actual.execution_id,
    )
    candidates = survey_test_candidates(ManifestOrchestrator())

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=candidates,
        )
        == ()
    )
    assert (
        len(
            forced_test_refusal_receipts(
                state,
                attempt_id="test-1",
                candidates=candidates,
            )
        )
        == 1
    )
    assert marked.metadata["harness_forced_test_attempt"]["disposition"] == ("candidate_mismatch")
    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_terminal_runner_receipt_unlocks_test_termination():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_success(
            output="50 tests passed",
            metadata={
                "command": "./gradlew test",
                "runner_dispatched": True,
                "exit_code": 0,
            },
        ),
    )

    assert (
        len(
            terminal_test_receipts(
                state,
                attempt_id="test-1",
                candidates=survey_test_candidates(ManifestOrchestrator()),
            )
        )
        == 1
    )
    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_command_without_physical_dispatch_does_not_unlock_test_termination():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_failure(
            output="docker rejected the exec request",
            error="dispatch failed",
            metadata={
                "command": "./gradlew test",
                "dispatch_status": "dispatch_failed",
                "runner_dispatched": False,
            },
        ),
    )

    candidates = survey_test_candidates(ManifestOrchestrator())
    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=candidates,
        )
        == ()
    )
    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is not None
    )


def test_forced_dispatch_failure_is_bounded_as_a_nonreceipt():
    state = _ready_state()
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    requirement = survey_test_candidates(ManifestOrchestrator())[0]
    result = ToolResult.completed_failure(
        output="docker rejected the exec request",
        error="dispatch failed",
        facts={"system": "gradle"},
        metadata={
            "command": "./gradlew test",
            "dispatch_status": "dispatch_failed",
            "runner_dispatched": False,
        },
    )
    call = ToolCall(
        name="build",
        raw_params=dict(requirement.required_action["params"]),
        raw_action_text="forced test",
        source_step_index=1,
        model_used="harness",
    )
    execution = ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        validated_params=call.raw_params,
        attempted_execution=True,
    )

    engine._mark_forced_test_refusals(execution, requirement)
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "build",
        execution.result,
        params=call.raw_params,
        source_phase="test",
        source_attempt_id="test-1",
    )

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=survey_test_candidates(ManifestOrchestrator()),
        )
        == ()
    )
    refusals = forced_test_refusal_receipts(
        state,
        attempt_id="test-1",
        candidates=survey_test_candidates(ManifestOrchestrator()),
    )
    assert len(refusals) == 1
    assert (
        execution.result.metadata["harness_forced_test_attempt"]["disposition"]
        == "no_runner_dispatch"
    )


def test_wrong_candidate_root_does_not_unlock_test_termination():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_success(
            output="root aggregate happened to run tests",
            metadata={
                "command": "./gradlew test",
                "runner_dispatched": True,
                "exit_code": 0,
            },
        ),
        root="/workspace/bigtop",
    )

    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is not None
    )


def test_build_facade_wrong_or_missing_system_does_not_unlock_candidate():
    for facts in ({}, {"system": "maven"}):
        state = _ready_state()
        state.ingest_tool_result(
            StateScope.TEST_RUNTIME,
            "build",
            ToolResult.completed_success(
                output="test-shaped facade result",
                metadata={
                    "command": "./gradlew test",
                    "runner_dispatched": True,
                    "exit_code": 0,
                },
                facts=facts,
            ),
            params={
                "action": "test",
                "working_directory": "/workspace/bigtop/bigtop-data-generators",
            },
            source_phase="test",
            source_attempt_id="test-1",
        )

        assert (
            required_test_attempt(
                state,
                ManifestOrchestrator(),
                phase="test",
                attempt_id="test-1",
            )
            is not None
        )


def test_pending_dispatch_requires_poll_and_only_terminal_poll_unlocks():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult(
            invocation_status=InvocationStatus.PENDING,
            operation_outcome=OperationOutcome.UNKNOWN,
            evidence_status=EvidenceStatus.UNKNOWN,
            poll_ref="job:test-123",
            output="running",
            metadata={"command": "./gradlew test", "runner_dispatched": True},
        ),
    )

    requirement = required_test_attempt(
        state,
        ManifestOrchestrator(),
        phase="test",
        attempt_id="test-1",
    )
    assert requirement is not None
    assert requirement.required_action == {
        "tool": "search",
        "params": {"target": "job:test-123"},
    }
    parent = next(
        observation
        for observation in state.tool_observations
        if observation.result.poll_ref == "job:test-123"
    )
    assert requirement.parent_execution_id == parent.execution_id

    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "search",
        ToolResult(
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.SUCCESS,
            evidence_status=EvidenceStatus.UNKNOWN,
            poll_ref="job:test-123",
            output="build failed",
            metadata={"dispatch_status": "completed_detached"},
        ),
        params={"target": "job:test-123"},
        source_phase="test",
        source_attempt_id="test-1",
    )

    assert (
        len(
            terminal_test_receipts(
                state,
                attempt_id="test-1",
                candidates=survey_test_candidates(ManifestOrchestrator()),
            )
        )
        == 1
    )
    assert (
        required_test_attempt(
            state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_fake_pending_without_runner_command_cannot_be_unlocked_by_a_poll():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult(
            invocation_status=InvocationStatus.PENDING,
            operation_outcome=OperationOutcome.UNKNOWN,
            evidence_status=EvidenceStatus.UNKNOWN,
            poll_ref="job:not-dispatched",
            output="claimed pending without runner metadata",
        ),
    )
    state.ingest_tool_result(
        StateScope.TEST_RUNTIME,
        "search",
        ToolResult(
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=OperationOutcome.SUCCESS,
            evidence_status=EvidenceStatus.UNKNOWN,
            poll_ref="job:not-dispatched",
            output="not a real detached completion",
            metadata={"dispatch_status": "completed_detached"},
        ),
        params={"target": "job:not-dispatched"},
        source_phase="test",
        source_attempt_id="test-1",
    )

    assert (
        terminal_test_receipts(
            state,
            attempt_id="test-1",
            candidates=survey_test_candidates(ManifestOrchestrator()),
        )
        == ()
    )
    requirement = required_test_attempt(
        state,
        ManifestOrchestrator(),
        phase="test",
        attempt_id="test-1",
    )
    assert requirement is not None
    assert requirement.required_action["tool"] == "build"


@pytest.mark.parametrize("action", ["done", "blocked"])
def test_phase_tool_rejects_zero_attempt_terminal_claims(action):
    state = _ready_state()
    machine = SimpleNamespace(
        current_phase="test",
        current_attempt_id="test-1",
        is_complete=False,
    )
    gate = AcceptingGate()
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=ManifestOrchestrator(),
        project_name="bigtop",
        gate_fn=gate,
        run_evidence_state=state,
    )

    result = tool.execute(
        action=action,
        outcome="failed",
        reason="external blocker" if action == "blocked" else "",
    )

    assert result.error_code == "TEST_ATTEMPT_REQUIRED"
    assert result.metadata["test_execution_receipts"] == 0
    assert result.metadata["required_action"]["params"]["working_directory"].endswith(
        "bigtop-data-generators"
    )
    assert gate.calls == []


def test_phase_tool_allows_claim_after_terminal_test_receipt():
    state = _ready_state()
    _record_gradle_test(
        state,
        ToolResult.completed_failure(
            output="tests reached Gradle and failed",
            error="test failure",
            error_code="GRADLE_BUILD_FAILED",
            metadata={
                "command": "./gradlew test",
                "runner_dispatched": True,
                "exit_code": 1,
            },
        ),
    )
    gate = AcceptingGate()
    tool = PhaseTool(
        machine=SimpleNamespace(
            current_phase="test",
            current_attempt_id="test-1",
            is_complete=False,
        ),
        validator=None,
        orchestrator=ManifestOrchestrator(),
        project_name="bigtop",
        gate_fn=gate,
        run_evidence_state=state,
    )

    result = tool.execute(action="done", outcome="failed")

    assert result.succeeded is True
    assert gate.calls == ["test"]


def test_phase_floor_executes_exact_test_action_without_a_model_plan():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    engine.run_evidence_state = _ready_state()
    engine.orchestrator = ManifestOrchestrator()
    engine.config = SimpleNamespace(
        phase_min_floors={"report": 8},
        max_iterations=10,
    )
    engine.current_iteration = 8
    engine.steps = []
    engine.tools = {}
    engine.reasoning_scheduler = ReasoningScheduler(available_tools={"build", "search", "phase"})
    engine._scheduler_active = True
    engine._get_timestamp = lambda: "2026-07-23T00:00:00Z"
    engine.control_event_sink = None
    calls: list[ToolCall] = []
    forced_result = ToolResult.completed_success(
        output="50 tests passed",
        metadata={
            "command": "./gradlew test",
            "runner_dispatched": True,
            "exit_code": 0,
        },
        facts={"system": "gradle"},
    )

    def execute(call):
        calls.append(call)
        return ToolExecution(
            call=call,
            result=forced_result,
            status="success",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text="50 tests passed",
            attempted_execution=True,
        )

    engine._execute_tool_call = execute
    engine._record_execution_bundle = lambda execution, call: (
        execution.result,
        "forced-execution-1",
        [],
    )
    engine._emit_control_tool_result = lambda **kwargs: None
    engine._apply_tool_execution_loop_effects = lambda execution: None
    engine._add_observation_step = lambda text: None
    engine._request_scheduler_reasoning = lambda trigger: True

    assert engine._enforce_phase_floors() is False
    assert engine.phase_machine.current_phase == "test"
    assert len(calls) == 1
    assert calls[0].name == "build"
    assert calls[0].raw_params == {
        "action": "test",
        "working_directory": "/workspace/bigtop/bigtop-data-generators",
    }
    turn = engine.reasoning_scheduler.next_turn()
    assert turn.mode is SchedulerMode.THINK


def test_phase_floor_forced_refusal_runs_once_then_closes_honestly(tmp_path):
    engine, _ = _engine(tmp_path, phase="test")
    engine.run_evidence_state.register_fact(
        StateScope.ARTIFACTS,
        "build.test_entry_ready",
        True,
        "artifact://compiled-classes",
    )
    engine.orchestrator = ManifestOrchestrator()
    engine.config = SimpleNamespace(
        verbose=False,
        phase_min_floors={"report": 8},
        max_iterations=10,
    )
    engine.current_iteration = 8
    engine.tools = {}
    engine._scheduler_active = False
    engine.control_event_sink = None
    engine._get_timestamp = lambda: "2026-07-23T00:00:00Z"
    calls: list[ToolCall] = []
    refused = ToolResult.completed_failure(
        output="test selector rejected before runner dispatch",
        error="selector expands beyond the deterministic limit",
        error_code="TEST_SELECTOR_LIMIT",
        facts={"system": "gradle"},
    )

    def execute(call):
        calls.append(call)
        return ToolExecution(
            call=call,
            result=refused,
            status="failure",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text=refused.output,
            attempted_execution=True,
        )

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=execute)
    engine._apply_tool_execution_loop_effects = lambda execution: None
    engine._add_observation_step = lambda text: None
    engine._request_scheduler_reasoning = lambda trigger: False
    engine._phase_gate_check = lambda phase: {
        "ok": True,
        "validator_state": "green",
        "reason": "stale artifacts looked green",
        "evidence_refs": [],
        "suggestions": [],
        "validated_facts": {},
        "code": "green",
    }
    emitted: list[GateResult] = []
    engine._emit_control_gate = lambda claim, gate: emitted.append(gate)
    engine._record_gate_facts = lambda phase, gate: None
    engine._apply_phase_decision = lambda record, decision: None

    assert engine._enforce_phase_floors() is False
    assert len(calls) == 1
    assert engine._missing_required_test_attempt() is None
    candidates = survey_test_candidates(engine.orchestrator)
    assert (
        terminal_test_receipts(
            engine.run_evidence_state,
            attempt_id="test-1",
            candidates=candidates,
        )
        == ()
    )

    assert engine._enforce_phase_floors() is True
    assert len(calls) == 1
    assert emitted[-1].validator_state is ValidatorState.UNAVAILABLE
    assert emitted[-1].validated_outcome is PhaseOutcome.UNKNOWN
    assert emitted[-1].code == "forced_test_attempt_nonreceipt"


def test_terminal_refusal_immediately_executes_the_harness_owned_test(tmp_path):
    engine, _ = _engine(tmp_path, phase="test")
    engine.run_evidence_state.register_fact(
        StateScope.ARTIFACTS,
        "build.test_entry_ready",
        True,
        "artifact://compiled-classes",
    )
    engine.orchestrator = ManifestOrchestrator()
    engine.tools = {}
    engine._scheduler_active = False
    engine.control_event_sink = None
    engine._get_timestamp = lambda: "2026-07-23T00:00:00Z"
    calls: list[ToolCall] = []
    refused = ToolResult.completed_failure(
        output="test attempt required",
        error="missing runner receipt",
        error_code="TEST_ATTEMPT_REQUIRED",
    )
    terminal = ToolResult.completed_success(
        output="50 tests passed",
        metadata={
            "command": "./gradlew test",
            "runner_dispatched": True,
            "exit_code": 0,
        },
        facts={"system": "gradle"},
    )

    def execute(call):
        calls.append(call)
        result = refused if call.name == "phase" else terminal
        return ToolExecution(
            call=call,
            result=result,
            status="failure" if call.name == "phase" else "success",
            raw_params=call.raw_params,
            validated_params=call.raw_params,
            observation_text=result.output,
            attempted_execution=True,
        )

    engine._get_tool_orchestrator = lambda: SimpleNamespace(execute=execute)
    _prepare_action_execution(engine)

    engine._execute_steps([_action_step("phase", {"action": "done", "outcome": "failed"})])

    assert [call.name for call in calls] == ["phase", "build"]
    assert calls[1].raw_params == {
        "action": "test",
        "working_directory": "/workspace/bigtop/bigtop-data-generators",
    }
    assert (
        required_test_attempt(
            engine.run_evidence_state,
            ManifestOrchestrator(),
            phase="test",
            attempt_id="test-1",
        )
        is None
    )


def test_intro_does_not_preselect_a_must_attempt_action():
    source = ReActEngine._phase_intro_step.__code__

    assert "NEXT REQUIRED ACTION" not in source.co_consts


def test_failed_refresh_caps_a_green_test_gate_without_requesting_another_refresh():
    state = _ready_state()
    state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "project",
        ToolResult.completed_failure(
            output="survey refresh failed",
            error="manifest unavailable",
            conflicts=["test_candidate_resolution_unresolved:test-1:manifest_unreadable"],
        ),
        params={"action": "analyze"},
        source_phase="test",
        source_attempt_id="test-1",
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="test")
    engine.run_evidence_state = state
    engine.orchestrator = UnreadableManifestOrchestrator()
    claim = PhaseClaim(
        phase="test",
        claimed_outcome=PhaseOutcome.SUCCESS,
    )
    green = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        claim=claim,
    )

    capped = engine._cap_unresolved_test_gate(claim, green)

    assert capped.accepted is False
    assert capped.validated_outcome is PhaseOutcome.UNKNOWN
    assert capped.code == "test_candidate_resolution_unavailable"
    assert engine._missing_required_test_attempt() is None


def _terminal_gradle_result():
    return ToolResult.completed_success(
        output="BUILD SUCCESSFUL\n2 actionable tasks: 2 executed",
        metadata={"runner_dispatched": True, "command": "./gradlew test"},
    )


def test_auxiliary_island_receipt_does_not_discharge_the_primary():
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["test_islands"] = [
        {"root": "/workspace/bigtop/bigtop-test-framework", "system": "gradle"},
        {"root": "/workspace/bigtop/bigtop-data-generators", "system": "gradle"},
    ]
    state = _ready_state()
    _record_gradle_test(
        state, _terminal_gradle_result(), root="/workspace/bigtop/bigtop-test-framework"
    )
    requirement = required_test_attempt(
        state, orchestrator, phase="test", attempt_id="test-1"
    )
    assert requirement is not None
    assert requirement.root == "/workspace/bigtop/bigtop-data-generators"


def test_primary_receipt_discharges_the_requirement():
    orchestrator = ManifestOrchestrator()
    state = _ready_state()
    _record_gradle_test(
        state, _terminal_gradle_result(), root="/workspace/bigtop/bigtop-data-generators"
    )
    assert (
        required_test_attempt(state, orchestrator, phase="test", attempt_id="test-1")
        is None
    )


def test_resolution_exposes_the_primary_candidate():
    resolution = resolve_survey_test_candidates(ManifestOrchestrator())
    assert resolution.primary is not None
    assert resolution.primary.root == "/workspace/bigtop/bigtop-data-generators"


def test_snapshot_round_trip_preserves_the_primary_coordinate():
    """Replay verification must enforce the same primary-coordinate policy as
    the live path (review finding on spec §3.4-6): without `primary` in the
    snapshot, a rehydrated resolution silently fell back to the legacy
    any-island discharge set."""
    resolution = resolve_survey_test_candidates(ManifestOrchestrator())
    assert resolution.primary is not None

    restored = TestCandidateResolution.from_snapshot(resolution.to_snapshot())

    assert restored.primary is not None
    assert restored.primary.root == resolution.primary.root
    assert restored.primary.system == resolution.primary.system
