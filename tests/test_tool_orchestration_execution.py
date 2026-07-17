import pytest

from sag.agent.output_storage import OutputStorageManager
from sag.agent.tool_orchestration import (
    RecoveryDecision,
    ToolCall,
    ToolExecutionRecord,
    ToolOrchestrator,
    format_tool_result,
)
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolError, ToolResult, bind_tool_result_output_storage


class EchoTool(BaseTool):
    def __init__(self, name="echo"):
        super().__init__(name, "Echo test tool")

    def execute(self, command: str, working_directory: str = "/workspace") -> ToolResult:
        return ToolResult.completed_success(output=f"ran {command}", metadata={"command": command})


class ManageContextTool(BaseTool):
    def __init__(self, *, success=True):
        super().__init__("manage_context", "Manage context test tool")
        self.success = success

    def execute(self, action: str, summary: str = "") -> ToolResult:
        return ToolResult.completed(
            operation_outcome="success" if self.success else "failed",
            output=f"{action} result",
        )


class PendingTool(BaseTool):
    def __init__(self):
        super().__init__("pending", "Pending test tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(
            invocation_status=InvocationStatus.PENDING,
            operation_outcome=OperationOutcome.UNKNOWN,
            evidence_status=EvidenceStatus.UNKNOWN,
            poll_ref="job:pending-1",
            output="still running",
        )


class FailureTool(BaseTool):
    def __init__(self):
        super().__init__("failure", "Failure test tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult.completed_failure(
            output="durable failure details",
            error="failed",
            error_code="FAILURE_TOOL_FAILED",
        )


class WheelFailureTool(BaseTool):
    def __init__(self):
        super().__init__("wheel", "Wheel build test tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult.completed_failure(
            output="CMake configuration failed",
            error="wheel build failed",
            error_code="WHEEL_BUILD_FAILED",
            metadata={"operation": "build", "evidence_only": True},
        )


class PartialTool(BaseTool):
    def __init__(self):
        super().__init__("partial", "Partial tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult.completed(
            output="partial execution details",
            operation_outcome=OperationOutcome.PARTIAL,
        )


class ExplodingSafeExecuteTool(BaseTool):
    def __init__(self):
        super().__init__("explode", "Exploding tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult.completed_success(output="unused")

    def safe_execute(self, **kwargs) -> ToolResult:
        raise RuntimeError("boom")


class FakeDurableOutputStorage:
    def __init__(self):
        self.outputs = {}
        self.records = {}

    def store_output(self, **kwargs):
        ref = f"output_failure_{len(self.outputs) + 1}"
        self.outputs[ref] = kwargs["output"]
        self.records[ref] = kwargs
        return ref

    def retrieve_output(self, ref):
        return self.outputs.get(ref)


def test_format_tool_result_surfaces_maven_version_contract():
    result = ToolResult.completed_failure(
        output="[ERROR] Detected Maven Version: 3.6.3 is not in the allowed range [3.9,).",
        error="Maven build failed",
        error_code="MAVEN_BUILD_FAILED",
        metadata={
            "maven_version_requirement": {
                "raw": "[3.9,)",
                "source": "build_error",
                "kind": "range",
            },
            "maven_runtime": {
                "executable": "/usr/bin/mvn",
                "version": "3.6.3",
                "source": "system",
            },
            "compatible_maven_candidate": None,
        },
    )

    formatted = format_tool_result("maven", result)
    formatted_for_build = format_tool_result("build", result)

    assert "Maven version requirement: [3.9,) (source: build_error)" in formatted
    assert "Current Maven executable: /usr/bin/mvn" in formatted
    assert "Current Maven version: 3.6.3" in formatted
    assert "Compatible Maven candidate: none" in formatted
    assert "via project(action='env'), then retry the build" in formatted
    # The consolidated build facade surfaces the same contract.
    assert "Maven version requirement: [3.9,) (source: build_error)" in formatted_for_build


def test_failed_observation_and_lifecycle_event_preserve_failure_provenance():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"failure": FailureTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="failure", raw_params={"command": "run"}))

    assert "Failure signature:" in execution.observation_text
    assert "Error tail:" in execution.observation_text
    metadata = events[-1].metadata
    assert metadata["failure_signature"] == execution.result.failure_signature
    assert metadata["error_tail_preview"] == execution.result.error_tail_preview
    assert metadata["output_ref"] == execution.result.output_ref


def test_orchestrator_executes_successful_tool_and_emits_events():
    events = []
    tracking_calls = []
    state_updates = []

    def update_successful_states(tool_name, params, result):
        state_updates.append((tool_name, params, result))

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=update_successful_states,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"
    assert execution.attempted_execution is True
    assert execution.executed_params == {"command": "pwd"}
    assert "echo executed successfully" in execution.observation_text
    assert [event.event_type for event in events] == ["tool_start", "tool_result"]
    assert events[-1].metadata["status"] == "success"
    assert events[-1].metadata["invocation_status"] == "completed"
    assert events[-1].metadata["operation_outcome"] == "success"
    assert events[-1].metadata["evidence_status"] == "verified"
    assert events[-1].metadata["error_code"] is None
    assert events[-1].metadata["executed_params"] == {"command": "pwd"}
    assert events[-1].metadata["recovery_applied"] is False
    assert execution.call.execution_signature == "echo:[('command', 'pwd')]"
    assert tracking_calls == [(execution.call.execution_signature, execution.result)]
    assert len(state_updates) == 1
    assert state_updates[0][0] == "echo"
    assert state_updates[0][1] == {"command": "pwd"}
    assert state_updates[0][2] is execution.result


def test_orchestrator_persists_failure_before_emitting_canonical_result():
    storage = FakeDurableOutputStorage()
    orchestrator = ToolOrchestrator(
        tools={"failure": FailureTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=storage,
    )

    execution = orchestrator.execute(ToolCall(name="failure", raw_params={"command": "run"}))

    assert execution.result.output_ref.startswith("output_")
    assert storage.retrieve_output(execution.result.output_ref) == "durable failure details"


def test_stored_wheel_failure_metadata_matches_typed_outcome():
    storage = FakeDurableOutputStorage()
    events = []
    orchestrator = ToolOrchestrator(
        tools={"wheel": WheelFailureTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=storage,
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="wheel", raw_params={"command": "build"}))

    stored = storage.records[execution.result.output_ref]
    typed_outcome = execution.result.operation_outcome.value
    assert typed_outcome == "failed"
    assert stored["metadata"]["operation_outcome"] == typed_outcome
    assert events[-1].metadata["operation_outcome"] == typed_outcome
    assert execution.result.succeeded is False


def test_orchestrator_uses_emergency_during_failed_result_construction(tmp_path, monkeypatch):
    storage = OutputStorageManager(tmp_path)
    primary_calls = []
    monkeypatch.setattr(
        storage,
        "store_output",
        lambda **kwargs: primary_calls.append(kwargs) or "",
    )
    orchestrator = ToolOrchestrator(
        tools={"failure": FailureTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=storage,
    )

    execution = orchestrator.execute(ToolCall(name="failure", raw_params={"command": "run"}))

    assert len(primary_calls) == 1
    assert execution.result.output_ref.startswith("output_emergency_")
    assert storage.retrieve_output(execution.result.output_ref) == "durable failure details"


def test_orchestrator_uses_emergency_during_partial_result_construction(tmp_path, monkeypatch):
    storage = OutputStorageManager(tmp_path)
    monkeypatch.setattr(storage, "store_output", lambda **kwargs: "")
    orchestrator = ToolOrchestrator(
        tools={"partial": PartialTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        output_storage=storage,
    )

    execution = orchestrator.execute(ToolCall(name="partial", raw_params={"command": "run"}))

    assert execution.status == "partial"
    assert execution.result.output_ref.startswith("output_emergency_")
    assert storage.retrieve_output(execution.result.output_ref) == "partial execution details"


def test_orchestrator_refuses_failure_without_output_storage_boundary():
    orchestrator = ToolOrchestrator(
        tools={"failure": FailureTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
    )

    with bind_tool_result_output_storage(None):
        with pytest.raises(ValueError, match="durable output storage"):
            orchestrator.execute(ToolCall(name="failure", raw_params={"command": "run"}))


def test_pending_execution_is_tracked_and_emitted_without_failure_shape():
    events = []
    tracking_calls = []
    orchestrator = ToolOrchestrator(
        tools={"pending": PendingTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: tracking_calls.append(args),
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="pending", raw_params={"command": "run"}))

    assert execution.status == "pending"
    assert len(tracking_calls) == 1
    assert tracking_calls[0][0] == execution.call.execution_signature
    assert tracking_calls[0][1] is execution.result
    assert events[-1].event_type == "tool_result"
    assert events[-1].level == "info"
    assert events[-1].metadata["invocation_status"] == "pending"
    assert events[-1].metadata["operation_outcome"] == "unknown"
    assert "result_succeeded" not in events[-1].metadata


def test_execution_history_classifies_pending_without_boolean_truth():
    pending = ToolExecutionRecord(
        signature="build:[('action', 'test')]",
        invocation_status=InvocationStatus.PENDING,
        operation_outcome=OperationOutcome.UNKNOWN,
        timestamp="ts-1",
    )
    failed = ToolExecutionRecord(
        signature="build:[('action', 'test')]",
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.FAILED,
        timestamp="ts-2",
    )
    orchestrator = ToolOrchestrator(
        tools={},
        context_manager=None,
        recent_tool_executions=[pending, failed],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda *args: None,
        update_successful_states=lambda *args: None,
        add_system_guidance=lambda *args, **kwargs: None,
        get_timestamp=lambda: "ts",
    )

    assert not hasattr(pending, "success")
    assert orchestrator._execution_failed(pending) is False
    assert orchestrator._execution_failed(failed) is True


def test_lifecycle_events_include_required_metadata():
    events = []
    orchestrator = ToolOrchestrator(
        tools={"bash": EchoTool("bash")},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(
        ToolCall(name="bash", raw_params={"cmd": "pwd"}, source_step_index=4)
    )

    start = events[0]
    parameters_fixed = next(
        event for event in events if event.event_type == "tool_parameters_fixed"
    )
    result = events[-1]

    assert start.metadata["tool_name"] == "bash"
    assert start.metadata["source_step_index"] == 4
    assert start.metadata["raw_params"] == {"cmd": "pwd"}
    assert "execution_signature" in start.metadata
    assert parameters_fixed.metadata["raw_params"] == {"cmd": "pwd"}
    assert parameters_fixed.metadata["validated_params"] == {
        "command": "pwd",
        "working_directory": "/workspace",
    }
    assert parameters_fixed.metadata["parameter_fixes"]
    assert parameters_fixed.metadata["params_changed"] is True
    assert result.metadata["status"] == execution.status
    assert result.metadata["duration_ms"] is not None
    assert result.metadata["invocation_status"] == "completed"
    assert result.metadata["operation_outcome"] == "success"
    assert result.metadata["evidence_status"] == "verified"
    assert result.metadata["error_code"] is None
    assert result.metadata["executed_params"] == {
        "command": "pwd",
        "working_directory": "/workspace",
    }
    assert result.metadata["recovery_applied"] is False


def test_orchestrator_returns_missing_tool_execution_with_existing_feedback():
    events = []
    tracking_calls = []
    state_updates = []
    orchestrator = ToolOrchestrator(
        tools={"bash": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    execution = orchestrator.execute(ToolCall(name="ls", raw_params={"path": "/workspace"}))

    assert execution.status == "missing_tool"
    assert execution.result.succeeded is False
    assert execution.attempted_execution is False
    assert execution.executed_params is None
    assert "Tool 'ls' does not exist" in execution.result.output
    assert "Did you mean: bash" in execution.result.output
    assert events[-1].event_type == "tool_error"
    assert tracking_calls == []
    assert state_updates == []


def test_empty_validated_params_are_used_instead_of_raw_params():
    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(
        ToolCall(name="echo", raw_params={"command": "raw"}, validated_params={})
    )

    assert execution.status == "failure"
    assert execution.executed_params == {}
    assert execution.result.succeeded is False
    assert execution.result.error_code == "MISSING_PARAMETERS"


def test_tool_error_metadata_and_suggestions_are_preserved():
    class ErrorTool(BaseTool):
        def __init__(self):
            super().__init__("error_tool", "Error tool")

        def execute(self, command: str) -> ToolResult:
            raise ToolError(
                "bad input",
                category="validation",
                error_code="BAD_INPUT",
                suggestions=["try a better command"],
                details={"command": command},
                retryable=True,
            )

    tracking_calls = []
    state_updates = []
    orchestrator = ToolOrchestrator(
        tools={"error_tool": ErrorTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: state_updates.append(
            (tool_name, params, result)
        ),
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="error_tool", raw_params={"command": "bad"}))

    assert execution.status == "failure"
    assert execution.result.error_code == "BAD_INPUT"
    assert execution.result.suggestions == ["try a better command"]
    assert execution.result.metadata["failure_category"] == "validation"
    assert execution.result.metadata["retryable"] is True
    assert execution.call.execution_signature == "error_tool:[('command', 'bad')]"
    assert tracking_calls == [(execution.call.execution_signature, execution.result)]
    assert state_updates == []


def test_manage_context_invalidation_metadata_only_for_successful_context_changes():
    def execute_manage_context(action, *, success=True):
        orchestrator = ToolOrchestrator(
            tools={"manage_context": ManageContextTool(success=success)},
            context_manager=None,
            recent_tool_executions=[],
            successful_states={},
            repository_url=None,
            track_tool_execution=lambda signature, result: None,
            update_successful_states=lambda tool_name, params, result: None,
            add_system_guidance=lambda message, priority=5: None,
            get_timestamp=lambda: "ts",
        )
        return orchestrator.execute(ToolCall(name="manage_context", raw_params={"action": action}))

    changing_execution = execute_manage_context("complete_task")
    info_execution = execute_manage_context("get_info")
    failed_changing_execution = execute_manage_context("complete_task", success=False)

    assert changing_execution.status == "success"
    assert changing_execution.metadata.get("invalidate_trunk_cache") is True
    assert "invalidate_trunk_cache" not in info_execution.metadata
    assert "invalidate_trunk_cache" not in failed_changing_execution.metadata


def test_unexpected_safe_execute_exception_reaches_recovery_and_preserves_crash(monkeypatch):
    tracking_calls = []
    recovery_calls = []
    events = []
    orchestrator = ToolOrchestrator(
        tools={"explode": ExplodingSafeExecuteTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )
    monkeypatch.setattr(
        orchestrator.recovery_handler,
        "recover",
        lambda tool_name, params, result: (
            recovery_calls.append((tool_name, params, result))
            or RecoveryDecision(
                should_recover=False,
                strategy="crash_declined",
                guidance="Crash recovery is unsupported",
            )
        ),
    )

    execution = orchestrator.execute(ToolCall(name="explode", raw_params={"command": "pwd"}))

    assert len(recovery_calls) == 1
    tool_name, params, crashed = recovery_calls[0]
    assert tool_name == "explode"
    assert params == {"command": "pwd"}
    assert crashed is execution.result
    assert execution.status == "exception"
    assert execution.result.succeeded is False
    assert execution.result.invocation_status is InvocationStatus.CRASHED
    assert execution.result.operation_outcome is OperationOutcome.FAILED
    assert execution.result.error_code == "TOOL_EXECUTION_EXCEPTION"
    assert execution.attempted_execution is True
    assert execution.metadata["recovery"]["strategy"] == "crash_declined"
    assert execution.metadata["recovery"]["attempted"] is False
    assert tracking_calls == [("explode:[('command', 'pwd')]", execution.result)]
    error_event = events[-1]
    assert error_event.event_type == "tool_error"
    assert all(event.event_type != "tool_result" for event in events)
    error_metadata = error_event.metadata
    assert error_metadata["invocation_status"] == "crashed"
    assert error_metadata["operation_outcome"] == "failed"
    assert error_metadata["evidence_status"] == "verified"
    assert error_metadata["failure_signature"] == execution.result.failure_signature
    assert error_metadata["error_tail_preview"] == execution.result.error_tail_preview
    assert error_metadata["output_ref"] == execution.result.output_ref
    assert error_metadata["recovery_attempted"] is False


def test_unexpected_safe_execute_exception_honors_supported_replacement(monkeypatch):
    recovery_calls = []
    tracking_calls = []
    events = []
    replacement = ToolResult.completed_success(output="recovered from crash")
    orchestrator = ToolOrchestrator(
        tools={"explode": ExplodingSafeExecuteTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: tracking_calls.append((signature, result)),
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=events.append,
    )

    def recover(tool_name, params, failed_result):
        recovery_calls.append((tool_name, params, failed_result))
        return RecoveryDecision(
            should_recover=True,
            strategy="crash_replacement",
            guidance="Use the supported replacement",
            replacement_result=replacement,
            replacement_params={"command": "fallback"},
        )

    monkeypatch.setattr(orchestrator.recovery_handler, "recover", recover)

    execution = orchestrator.execute(ToolCall(name="explode", raw_params={"command": "pwd"}))

    assert len(recovery_calls) == 1
    tool_name, params, crashed = recovery_calls[0]
    assert tool_name == "explode"
    assert params == {"command": "pwd"}
    assert crashed.invocation_status is InvocationStatus.CRASHED
    assert crashed.operation_outcome is OperationOutcome.FAILED
    assert execution.status == "recovered"
    assert execution.result is replacement
    assert execution.recovery_applied is True
    assert execution.recovery_strategy == "crash_replacement"
    assert execution.executed_params == {"command": "fallback"}
    assert execution.metadata["recovery"]["success"] is True
    assert tracking_calls == [("explode:[('command', 'pwd')]", replacement)]
    recovery_event = next(event for event in events if event.event_type == "tool_recovery")
    assert recovery_event.metadata["recovery_strategy"] == "crash_replacement"
    error_event = events[-1]
    assert error_event.event_type == "tool_error"
    assert all(event.event_type != "tool_result" for event in events)
    assert error_event.metadata["invocation_status"] == "crashed"
    assert error_event.metadata["operation_outcome"] == "failed"
    assert error_event.metadata["failure_signature"] == crashed.failure_signature
    assert error_event.metadata["error_tail_preview"] == crashed.error_tail_preview
    assert error_event.metadata["output_ref"] == crashed.output_ref
    assert error_event.metadata["recovery_attempted"] is True


def test_event_sink_exception_does_not_abort_successful_execution():
    def event_sink(event):
        raise RuntimeError("event sink failed")

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=lambda tool_name, params, result: None,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
        event_sink=event_sink,
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"


def test_successful_state_callback_exception_does_not_abort_successful_execution():
    def update_successful_states(tool_name, params, result):
        raise RuntimeError("state update failed")

    orchestrator = ToolOrchestrator(
        tools={"echo": EchoTool()},
        context_manager=None,
        recent_tool_executions=[],
        successful_states={},
        repository_url=None,
        track_tool_execution=lambda signature, result: None,
        update_successful_states=update_successful_states,
        add_system_guidance=lambda message, priority=5: None,
        get_timestamp=lambda: "ts",
    )

    execution = orchestrator.execute(ToolCall(name="echo", raw_params={"command": "pwd"}))

    assert execution.status == "success"
    assert execution.result.output == "ran pwd"
