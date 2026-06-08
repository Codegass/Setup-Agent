from sag.agent.tool_orchestration import format_tool_result
from sag.evidence import EvidenceStatus, TestStats
from sag.agent.tool_orchestration import (
    ParameterFix,
    ToolCall,
    ToolExecution,
    ToolLifecycleEvent,
)
from sag.tools.base import ToolResult


def test_tool_call_keeps_raw_and_validated_params_separate():
    call = ToolCall(
        name="bash",
        raw_params={"cmd": "pwd"},
        validated_params={"command": "pwd", "working_directory": "/workspace"},
        parameter_fixes=[
            ParameterFix(
                field="cmd",
                before="pwd",
                after={"command": "pwd"},
                reason="renamed to schema field",
                source="schema_alias",
            )
        ],
        execution_signature="bash:[('command', 'pwd'), ('working_directory', '/workspace')]",
        raw_action_text="ACTION: bash",
        source_step_index=3,
        model_used="action-model",
    )

    assert call.raw_params == {"cmd": "pwd"}
    assert call.validated_params["command"] == "pwd"
    assert call.parameter_fixes[0].source == "schema_alias"


def test_tool_execution_status_is_separate_from_tool_result_success():
    result = ToolResult(
        success=False,
        output="timeout guidance",
        error="timed out",
        error_code="TIMEOUT_HANDLED",
    )
    execution = ToolExecution(
        call=ToolCall(name="bash", raw_params={"command": "mvn test"}),
        result=result,
        status="recovery_attempted",
        raw_params={"command": "mvn test"},
        validated_params={"command": "mvn test", "working_directory": "/workspace"},
        executed_params={"command": "mvn test", "working_directory": "/workspace"},
        duration_ms=12.5,
        observation_text="handled timeout",
        recovery_applied=True,
        recovery_strategy="bash_timeout_guidance",
        attempted_execution=True,
    )

    assert execution.status == "recovery_attempted"
    assert execution.result.success is False
    assert execution.executed_params["working_directory"] == "/workspace"


def test_lifecycle_event_is_ui_agnostic_metadata_carrier():
    call = ToolCall(name="file_io", raw_params={"path": "README.md"})
    event = ToolLifecycleEvent(
        event_type="tool_start",
        call=call,
        message="Starting file_io",
        level="info",
        metadata={"raw_params": call.raw_params},
    )

    assert event.event_type == "tool_start"
    assert event.metadata["raw_params"] == {"path": "README.md"}


def test_tool_observation_includes_evidence_status_refs_and_conflicts():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Maven exited zero but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
    )

    observation = format_tool_result("maven", result)

    assert "Evidence status: partial" in observation
    assert "Evidence refs: output_abc" in observation
    assert "Conflicts: maven_success_vs_surefire_failures" in observation


def test_tool_observation_includes_test_stats_summary_when_present():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Maven exited zero but tests failed.",
        test_stats=TestStats(executed=214, passed=206, failed=3, skipped=5),
    )

    observation = format_tool_result("maven", result)

    assert "Test stats: 206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped" in observation


def test_tool_observation_omits_success_status_for_raw_success_string():
    result = ToolResult.model_construct(success=True, status="success", output="ok")

    observation = format_tool_result("bash", result)

    assert "Evidence status:" not in observation


def test_tool_observation_omits_success_status_for_default_success():
    result = ToolResult(success=True, output="ok")

    observation = format_tool_result("bash", result)

    assert "Evidence status:" not in observation


def test_tool_observation_omits_success_status_for_legacy_constructed_success():
    result = ToolResult.model_construct(success=True, output="ok")

    observation = format_tool_result("bash", result)

    assert "Evidence status:" not in observation


def test_tool_observation_normalizes_raw_success_string_before_visibility_check():
    result = ToolResult.model_construct(success=True, status="SUCCESS", output="ok")

    observation = format_tool_result("bash", result)

    assert "Evidence status:" not in observation
