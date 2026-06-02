"""Internal tool orchestration contracts and execution boundary."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, Callable, Dict, Literal, MutableSequence, Optional

from loguru import logger as default_logger

from sag.tools.base import BaseTool, ToolResult

ParameterFixSource = Literal["schema_alias", "default", "state_injection", "safety_fix"]
ToolExecutionStatus = Literal[
    "success",
    "failure",
    "missing_tool",
    "validation_failed",
    "repetition_blocked",
    "recovery_attempted",
    "recovered",
    "recovery_failed",
    "exception",
]
ToolLifecycleEventType = Literal[
    "tool_start",
    "tool_parameters_fixed",
    "tool_result",
    "tool_recovery",
    "tool_error",
]
ToolLifecycleLevel = Literal["debug", "info", "warning", "error", "success"]
GuidancePriority = int | str


@dataclass(slots=True)
class ParameterFix:
    field: str
    before: Any
    after: Any
    reason: str
    source: ParameterFixSource


@dataclass(slots=True)
class ToolCall:
    name: str
    raw_params: Dict[str, Any]
    validated_params: Optional[Dict[str, Any]] = None
    parameter_fixes: list[ParameterFix] = field(default_factory=list)
    execution_signature: Optional[str] = None
    raw_action_text: Optional[str] = None
    source_step_index: Optional[int] = None
    model_used: Optional[str] = None


@dataclass(slots=True)
class ToolExecutionRecord:
    signature: str
    success: bool
    timestamp: str


@dataclass(slots=True)
class RecoveryDecision:
    should_recover: bool
    strategy: Optional[str] = None
    guidance: Optional[str] = None
    replacement_result: Optional[ToolResult] = None
    replacement_params: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolExecution:
    call: ToolCall
    result: ToolResult
    status: ToolExecutionStatus
    raw_params: Dict[str, Any]
    validated_params: Optional[Dict[str, Any]] = None
    executed_params: Optional[Dict[str, Any]] = None
    duration_ms: Optional[float] = None
    observation_text: str = ""
    recovery_applied: bool = False
    recovery_strategy: Optional[str] = None
    attempted_execution: bool = False
    parameter_fixes: list[ParameterFix] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolLifecycleEvent:
    event_type: ToolLifecycleEventType
    call: ToolCall
    message: str
    level: ToolLifecycleLevel = "info"
    metadata: Dict[str, Any] = field(default_factory=dict)


def format_tool_result(tool_name: str, result: ToolResult) -> str:
    """Format tool result for observation. Output truncation is now handled in BaseTool."""
    if result.success:
        # For successful results, preserve key status information
        formatted = f"✅ {tool_name} executed successfully"

        # Add command information for bash tool
        if tool_name == "bash" and result.metadata and "command" in result.metadata:
            formatted += f"\nCommand: {result.metadata['command']}"

        # Add output (already processed by BaseTool truncation)
        if result.output:
            formatted += f"\n\nOutput: {result.output}"

        # Add metadata if available
        if result.metadata:
            if "exit_code" in result.metadata:
                formatted += f"\nExit code: {result.metadata['exit_code']}"
            if "auto_installed" in result.metadata:
                formatted += f"\nAuto-installed: {result.metadata['auto_installed']}"
            # Show truncation info if applicable
            if result.metadata.get("output_truncated"):
                original_len = result.metadata.get("original_length", 0)
                truncated_len = result.metadata.get("truncated_length", 0)
                formatted += f"\n📏 Output truncated: {original_len} → {truncated_len} chars"

    else:
        # For failed results, show error and suggestions
        error_msg = result.error if result.error else "Unknown error occurred"
        formatted = f"❌ {tool_name} failed: {error_msg}"

        # Add command information for failed bash tool
        if tool_name == "bash" and result.metadata and "command" in result.metadata:
            formatted += f"\nCommand: {result.metadata['command']}"

        # Show extracted error details from output (especially important for maven tool)
        if result.output and result.output.strip():
            formatted += f"\n\n{result.output}"

        if result.suggestions:
            formatted += f"\n\nSuggestions:\n" + "\n".join(
                f"• {s}" for s in result.suggestions[:3]
            )

        if result.error_code:
            formatted += f"\nError code: {result.error_code}"

        # Add full raw output if available and error message is unclear (and no specific output was provided)
        if (
            result.raw_output
            and (not result.error or len(result.error.strip()) < 10)
            and (not result.output or len(result.output.strip()) < 20)
        ):
            formatted += f"\n\nRaw output: {result.raw_output}"

    return formatted


class ToolOrchestrator:
    def __init__(
        self,
        *,
        tools: Dict[str, BaseTool],
        context_manager: Any,
        recent_tool_executions: MutableSequence[dict[str, Any] | ToolExecutionRecord],
        successful_states: Dict[str, Any],
        repository_url: Optional[str],
        track_tool_execution: Callable[[str, bool], None],
        update_successful_states: Callable[[str, Dict[str, Any], ToolResult], None],
        add_system_guidance: Callable[[str, GuidancePriority], None],
        get_timestamp: Callable[[], str],
        event_sink: Optional[Callable[[ToolLifecycleEvent], None]] = None,
        logger: Any = None,
    ):
        from sag.agent.tool_recovery import ToolRecoveryHandler

        self.tools = tools
        self.context_manager = context_manager
        self.recent_tool_executions = recent_tool_executions
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.track_tool_execution = track_tool_execution
        self.update_successful_states = update_successful_states
        self.add_system_guidance = add_system_guidance
        self.get_timestamp = get_timestamp
        self.event_sink = event_sink
        self.logger = logger or default_logger
        self.recovery_handler = ToolRecoveryHandler(
            tools=self.tools,
            context_manager=self.context_manager,
            successful_states=self.successful_states,
            repository_url=self.repository_url,
            add_system_guidance=self.add_system_guidance,
            logger=self.logger,
        )

    def execute(self, call: ToolCall) -> ToolExecution:
        self._emit(
            "tool_start",
            call,
            message=f"Starting {call.name}",
            level="info",
            metadata={"raw_params": call.raw_params},
        )

        if call.name not in self.tools:
            feedback = self._generate_unknown_tool_feedback(call.name)
            self._log_unknown_tool_attempt(call, feedback)
            result = ToolResult(
                success=False,
                output=feedback,
                error=f"Unknown tool requested: {call.name}",
                error_code="UNKNOWN_TOOL",
            )
            execution = ToolExecution(
                call=call,
                result=result,
                status="missing_tool",
                raw_params=call.raw_params,
                validated_params=call.validated_params,
                observation_text=feedback,
                attempted_execution=False,
                parameter_fixes=call.parameter_fixes,
            )
            self._emit(
                "tool_error",
                call,
                message=feedback,
                level="error",
                metadata={"status": execution.status, "raw_params": call.raw_params},
            )
            return execution

        parameter_fixes = call.parameter_fixes
        if call.validated_params is None:
            try:
                validated_params = self._validate_and_fix_parameters(
                    call.name, call.raw_params, parameter_fixes
                )
            except Exception as exc:
                result = ToolResult(
                    success=False,
                    output="",
                    error=f"Tool {call.name} parameter validation failed: {exc}",
                    error_code="PARAMETER_VALIDATION_FAILED",
                    metadata={"exception_type": type(exc).__name__},
                )
                observation_text = format_tool_result(call.name, result)
                execution = ToolExecution(
                    call=call,
                    result=result,
                    status="validation_failed",
                    raw_params=call.raw_params,
                    validated_params=None,
                    executed_params=None,
                    observation_text=observation_text,
                    attempted_execution=False,
                    parameter_fixes=parameter_fixes,
                    metadata={"validation_exception": type(exc).__name__},
                )
                self._emit(
                    "tool_error",
                    call,
                    message=observation_text,
                    level="error",
                    metadata={
                        "status": execution.status,
                        "raw_params": call.raw_params,
                        "error_code": result.error_code,
                    },
                )
                return execution
        else:
            validated_params = dict(call.validated_params)

        call.validated_params = validated_params
        call.parameter_fixes = parameter_fixes
        signature = f"{call.name}:{str(sorted(validated_params.items()))}"
        call.execution_signature = signature

        if parameter_fixes:
            self._emit(
                "tool_parameters_fixed",
                call,
                message=f"Fixed parameters for {call.name}",
                level="info",
                metadata={
                    "raw_params": call.raw_params,
                    "validated_params": validated_params,
                    "parameter_fixes": parameter_fixes,
                    "params_changed": True,
                },
            )

        repetition_level = self._get_repetition_level(signature)
        repetition_metadata: Dict[str, Any] = {}
        if repetition_level > 0:
            recent_executions = self._recent_executions_for_tool(call.name)
            failure_count = sum(
                1 for execution in recent_executions if not self._execution_success(execution)
            )
            repetition_metadata = {
                "repetition_level": repetition_level,
                "recent_execution_count": len(recent_executions),
                "failure_count": failure_count,
            }

            if repetition_level >= 3:
                if self._is_java_configuration_loop(
                    call.name, validated_params, recent_executions
                ):
                    return self._attempt_java_configuration_auto_fix(
                        call,
                        signature,
                        validated_params,
                        repetition_metadata,
                    )

                result = ToolResult(
                    success=False,
                    output=(
                        f"INFINITE LOOP BROKEN: {call.name} was called "
                        f"{len(recent_executions)} times without progress.\n"
                        f"Failures: {failure_count}/{len(recent_executions)}\n"
                        "Moving to next task to prevent resource waste."
                    ),
                    error="Infinite loop detected and broken",
                    error_code="INFINITE_LOOP_BROKEN",
                    suggestions=[
                        "Task has been marked as incomplete",
                        "Proceeding with next task",
                        "Review logs for root cause analysis",
                    ],
                )
                self._track_tool_execution(signature, False)
                observation_text = format_tool_result(call.name, result)
                metadata = {
                    "execution_signature": signature,
                    "force_next_task": True,
                    **repetition_metadata,
                }
                execution = ToolExecution(
                    call=call,
                    result=result,
                    status="repetition_blocked",
                    raw_params=call.raw_params,
                    validated_params=validated_params,
                    executed_params=None,
                    observation_text=observation_text,
                    attempted_execution=False,
                    parameter_fixes=call.parameter_fixes,
                    metadata=metadata,
                )
                self._emit(
                    "tool_error",
                    call,
                    message=observation_text,
                    level="error",
                    metadata={
                        "status": execution.status,
                        "error_code": result.error_code,
                        **metadata,
                    },
                )
                return execution

        try:
            result = self.tools[call.name].safe_execute(**validated_params)
        except Exception as exc:
            self._track_tool_execution(signature, False)
            result = ToolResult(
                success=False,
                output="",
                error=f"Tool {call.name} execution failed unexpectedly: {exc}",
                error_code="TOOL_EXECUTION_EXCEPTION",
                metadata={"exception_type": type(exc).__name__},
            )
            observation_text = format_tool_result(call.name, result)
            execution = ToolExecution(
                call=call,
                result=result,
                status="exception",
                raw_params=call.raw_params,
                validated_params=validated_params,
                executed_params=validated_params,
                observation_text=observation_text,
                attempted_execution=True,
                parameter_fixes=call.parameter_fixes,
                metadata={"execution_signature": signature, **repetition_metadata},
            )
            self._emit(
                "tool_error",
                call,
                message=observation_text,
                level="error",
                metadata={"status": execution.status, "execution_signature": signature},
            )
            return execution

        executed_params = validated_params
        recovery_applied = False
        recovery_strategy: Optional[str] = None
        recovery_metadata: Optional[Dict[str, Any]] = None
        status: ToolExecutionStatus = "success" if result.success else "failure"

        if not result.success:
            decision = self.recovery_handler.recover(call.name, validated_params, result)
            recovery_metadata = dict(decision.metadata)
            recovery_metadata.setdefault("attempted", decision.should_recover)
            recovery_metadata.setdefault("success", False)
            recovery_metadata.setdefault("message", decision.guidance)
            recovery_metadata.setdefault("strategy", decision.strategy)

            if decision.should_recover:
                replacement_success = (
                    decision.replacement_result.success
                    if decision.replacement_result is not None
                    else False
                )
                self._emit(
                    "tool_recovery",
                    call,
                    message=decision.guidance or "Tool recovery attempted",
                    level="success" if replacement_success else "error",
                    metadata={
                        "recovery_strategy": decision.strategy,
                        "attempted": True,
                        "success": replacement_success,
                        "guidance": decision.guidance,
                        "replacement_result_success": (
                            decision.replacement_result.success
                            if decision.replacement_result is not None
                            else None
                        ),
                        "recovery_params": decision.replacement_params or validated_params,
                    },
                )

                recovery_strategy = decision.strategy
                if decision.replacement_result is not None:
                    result = decision.replacement_result
                    executed_params = decision.replacement_params or validated_params
                    if recovery_metadata.get("guidance_only"):
                        status = "recovery_attempted"
                    else:
                        status = "recovered" if result.success else "recovery_failed"
                    recovery_applied = True
                    recovery_metadata["success"] = result.success
                else:
                    status = "recovery_attempted"

        self._track_tool_execution(signature, result.success)
        if result.success:
            self._update_successful_states(call.name, executed_params, result)

        if repetition_level in {1, 2}:
            warning = self._build_repetition_warning(
                call.name,
                validated_params,
                repetition_level,
                repetition_metadata["recent_execution_count"],
                repetition_metadata["failure_count"],
            )
            result.output = (
                f"{warning}\n\n{result.output}" if result.output else warning
            )

        observation_text = format_tool_result(call.name, result)
        execution_metadata = {"execution_signature": signature, **repetition_metadata}
        if repetition_level == 2:
            execution_metadata["force_thinking_next"] = True
        if recovery_metadata is not None:
            execution_metadata["recovery"] = recovery_metadata
        if recovery_strategy:
            execution_metadata["recovery_strategy"] = recovery_strategy

        execution = ToolExecution(
            call=call,
            result=result,
            status=status,
            raw_params=call.raw_params,
            validated_params=validated_params,
            executed_params=executed_params,
            observation_text=observation_text,
            recovery_applied=recovery_applied,
            recovery_strategy=recovery_strategy,
            attempted_execution=True,
            parameter_fixes=call.parameter_fixes,
            metadata=execution_metadata,
        )
        if result.success and call.name == "manage_context":
            action = executed_params.get("action", "")
            if action in {
                "start_task",
                "complete_task",
                "complete_with_results",
                "add_context",
                "compact_context",
                "create_branch",
                "switch_to_trunk",
                "switch_to_branch",
            }:
                execution.metadata["invalidate_trunk_cache"] = True

        event_metadata = {
            "status": execution.status,
            "result_success": result.success,
            "error_code": result.error_code,
            "executed_params": executed_params,
            "recovery_applied": recovery_applied,
            "execution_signature": signature,
        }
        if recovery_strategy:
            event_metadata["recovery_strategy"] = recovery_strategy
        if recovery_metadata is not None:
            event_metadata["recovery"] = recovery_metadata
        event_metadata.update(repetition_metadata)
        if execution.metadata.get("force_thinking_next"):
            event_metadata["force_thinking_next"] = True
        if execution.metadata.get("invalidate_trunk_cache"):
            event_metadata["invalidate_trunk_cache"] = True

        self._emit(
            "tool_result",
            call,
            message=observation_text,
            level="success" if result.success else "error",
            metadata=event_metadata,
        )
        return execution

    def _recent_signature(self, execution: dict[str, Any] | ToolExecutionRecord) -> str:
        if isinstance(execution, ToolExecutionRecord):
            return execution.signature
        return str(execution.get("signature", ""))

    def _execution_success(self, execution: dict[str, Any] | ToolExecutionRecord) -> bool:
        if isinstance(execution, ToolExecutionRecord):
            return execution.success
        return bool(execution.get("success", False))

    def _recent_executions_for_tool(
        self, tool_name: str
    ) -> list[dict[str, Any] | ToolExecutionRecord]:
        return [
            execution
            for execution in self.recent_tool_executions
            if self._recent_signature(execution).startswith(tool_name + ":")
        ]

    def _get_repetition_level(self, tool_signature: str) -> int:
        """
        Get the level of repetition for graduated response.
        Returns:
            0: No repetition concern
            1: Warning level (3 repetitions)
            2: Guidance level (4 repetitions)
            3: Force break level (5+ repetitions)
        """
        exact_match_count = sum(
            1
            for execution in self.recent_tool_executions
            if self._recent_signature(execution) == tool_signature
        )

        tool_name = tool_signature.split(":")[0]

        if tool_name == "manage_context":
            if "start_task" in tool_signature or "get_info" in tool_signature:
                return 0
            if "complete_with_results" in tool_signature:
                if exact_match_count >= 6:
                    return 3
                if exact_match_count >= 5:
                    return 2
                if exact_match_count >= 4:
                    return 1
                return 0

        tool_count = len(self._recent_executions_for_tool(tool_name))

        if exact_match_count >= 5 or tool_count >= 8:
            return 3
        if exact_match_count >= 4 or tool_count >= 6:
            return 2
        if exact_match_count >= 3 or tool_count >= 5:
            return 1

        return 0

    def _build_repetition_warning(
        self,
        tool_name: str,
        params: Dict[str, Any],
        repetition_level: int,
        recent_execution_count: int,
        failure_count: int,
    ) -> str:
        lines = [
            (
                f"REPETITIVE EXECUTION WARNING: {tool_name} has been called "
                f"{recent_execution_count} times."
            ),
            f"Failures: {failure_count}/{recent_execution_count}.",
        ]
        if repetition_level >= 2:
            suggestions = self._generate_alternative_suggestions(
                tool_name, params, self._recent_executions_for_tool(tool_name)
            )
            lines.extend(
                [
                    "Consider alternative approaches:",
                    f"• {suggestions}",
                ]
            )
        return "\n".join(lines)

    def _generate_alternative_suggestions(
        self,
        tool_name: str,
        params: Dict[str, Any],
        recent_executions: list[dict[str, Any] | ToolExecutionRecord],
    ) -> str:
        """Generate context-aware alternative suggestions."""
        suggestions = []

        if tool_name == "bash":
            if any("update-alternatives" in str(execution) for execution in recent_executions):
                suggestions.append(
                    "Use system tool's install_java action instead of manual update-alternatives"
                )
            if any("java" in str(execution) for execution in recent_executions):
                suggestions.append(
                    "Try: system(action='verify_java') to check current Java version"
                )
            suggestions.append("Use file_io tool to examine files before executing commands")

        elif tool_name == "maven":
            suggestions.append("Try: bash(command='mvn --version') to verify Maven installation")
            suggestions.append("Check pom.xml exists: file_io(action='read', file_path='pom.xml')")
            suggestions.append("Use bash tool for manual investigation: bash(command='ls -la')")

        elif tool_name == "system":
            if params.get("action") == "install_java":
                suggestions.append(
                    "Java might already be installed - verify with: system(action='verify_java')"
                )
                suggestions.append(
                    "Check available Java versions: bash(command='ls /usr/lib/jvm/')"
                )

        return "\n• ".join(suggestions) if suggestions else "Try a different tool or approach"

    def _is_java_configuration_loop(
        self,
        tool_name: str,
        validated_params: Dict[str, Any],
        recent_executions: list[dict[str, Any] | ToolExecutionRecord],
    ) -> bool:
        action = str(validated_params.get("action", "")).lower()
        if tool_name == "system" and action in {"install_java", "verify_java"}:
            return True

        command_contexts = [str(validated_params.get("command", ""))]
        for execution in recent_executions:
            signature = self._recent_signature(execution)
            recent_tool_name, _, _ = signature.partition(":")
            recent_params = self._params_from_signature(signature)
            if recent_tool_name == "system":
                recent_action = str(recent_params.get("action", "")).lower()
                if recent_action in {"install_java", "verify_java"}:
                    return True
            command_contexts.append(str(recent_params.get("command", "")))

        return any(
            self._has_java_alternatives_marker(context) for context in command_contexts
        )

    def _params_from_signature(self, signature: str) -> Dict[str, Any]:
        _, _, params_repr = signature.partition(":")
        try:
            return dict(ast.literal_eval(params_repr))
        except (TypeError, ValueError, SyntaxError):
            return {}

    def _has_java_alternatives_marker(self, value: str) -> bool:
        context = value.lower()
        if "update-java-alternatives" in context:
            return True
        if re.search(r"\bupdate-alternatives\b[^\n;]*\bjava(?:c)?\b", context):
            return True
        return bool(re.search(r"\balternatives\s+--config\s+java(?:c)?\b", context))

    def _attempt_java_configuration_auto_fix(
        self,
        call: ToolCall,
        signature: str,
        validated_params: Dict[str, Any],
        repetition_metadata: Dict[str, Any],
    ) -> ToolExecution:
        recovery_strategy = "java_configuration_auto_fix"
        try:
            result = self._auto_fix_java_configuration()
        except Exception as exc:
            result = ToolResult(
                success=False,
                output="Could not auto-fix Java configuration. Skipping to next task.",
                error=f"Auto-fix failed: {exc}",
                error_code="JAVA_AUTO_FIX_EXCEPTION",
                metadata={"exception_type": type(exc).__name__},
            )

        self._track_tool_execution(signature, result.success)
        status: ToolExecutionStatus = "recovered" if result.success else "recovery_failed"
        observation_text = format_tool_result(call.name, result)
        metadata = {
            "execution_signature": signature,
            "recovery_strategy": recovery_strategy,
            **repetition_metadata,
        }
        execution = ToolExecution(
            call=call,
            result=result,
            status=status,
            raw_params=call.raw_params,
            validated_params=validated_params,
            executed_params=None,
            observation_text=observation_text,
            recovery_applied=True,
            recovery_strategy=recovery_strategy,
            attempted_execution=False,
            parameter_fixes=call.parameter_fixes,
            metadata=metadata,
        )
        self._emit(
            "tool_recovery",
            call,
            message=observation_text,
            level="success" if result.success else "error",
            metadata={
                "status": execution.status,
                "result_success": result.success,
                "error_code": result.error_code,
                **metadata,
            },
        )
        return execution

    def _auto_fix_java_configuration(self) -> ToolResult:
        """Automatically fix Java configuration issues."""
        self.logger.info("Attempting automatic Java configuration fix")

        if "system" in self.tools:
            self.tools["system"].safe_execute(action="verify_java")

            current_context = ""
            if self.context_manager and hasattr(self.context_manager, "get_current_context"):
                try:
                    current_context = str(self.context_manager.get_current_context())
                except Exception as exc:
                    self.logger.warning(f"Failed to inspect current context for Java fix: {exc}")

            if "17" in current_context:
                self.logger.info(
                    "Detected Java 17 requirement, using system tool for proper installation"
                )
                install_result = self.tools["system"].safe_execute(
                    action="install_java", java_version="17"
                )

                if install_result.success:
                    return ToolResult(
                        success=True,
                        output=(
                            "Auto-fixed Java configuration using enhanced system tool\n"
                            + install_result.output
                        ),
                        metadata={"auto_fixed": True, "java_version": "17"},
                    )

        return ToolResult(
            success=False,
            output="Could not auto-fix Java configuration. Skipping to next task.",
            error="Auto-fix failed",
            error_code="AUTO_FIX_FAILED",
            suggestions=["Manual intervention may be required", "Check Java installation logs"],
        )

    def _add_parameter_fix(
        self,
        fixes: list[ParameterFix],
        *,
        field: str,
        before: Any,
        after: Any,
        reason: str,
        source: ParameterFixSource,
    ) -> None:
        if before != after:
            fixes.append(
                ParameterFix(
                    field=field,
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            )

    def _validate_and_fix_parameters(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Validate and fix tool parameters with self-healing capability."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        if tool_name not in self.tools:
            self.logger.error(f"Unknown tool: {tool_name}")
            return params

        tool = self.tools[tool_name]

        # Handle completely empty parameters
        if not params:
            params = {}

        # Get the tool's parameter schema
        if hasattr(tool, "get_parameter_schema"):
            schema = tool.get_parameter_schema()
        elif hasattr(tool, "_get_parameters_schema"):
            schema = tool._get_parameters_schema()
        else:
            # No schema available, apply basic fixes
            return self._apply_basic_parameter_fixes(tool_name, params, fixes)

        # Validate and fix parameters
        validated_params = self._fix_parameters_against_schema(
            params, schema, tool_name, fixes
        )

        # Apply additional tool-specific fixes
        validated_params = self._apply_tool_specific_fixes(
            tool_name, validated_params, fixes
        )

        # Check for unexpected parameters and provide warnings
        expected_params = set(schema.get("properties", {}).keys())
        actual_params = set(validated_params.keys())
        unexpected_params = actual_params - expected_params

        if unexpected_params:
            self.logger.warning(
                f"🚨 Unexpected parameters for {tool_name}: {unexpected_params}"
            )
            self.logger.warning(f"Expected parameters: {expected_params}")

            # Only remove parameters that are clearly invalid, keep potentially useful ones
            params_to_remove = []
            for param in unexpected_params:
                param_value = validated_params[param]

                # Keep parameters that might be useful extensions
                if tool_name == "maven" and param in ["pom_file", "maven_home", "java_home"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful Maven parameter: {param}={param_value}"
                    )
                    continue
                elif tool_name == "bash" and param in ["env", "environment"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful bash parameter: {param}={param_value}"
                    )
                    continue
                elif tool_name == "system" and param in ["sudo", "force"]:
                    self.logger.info(
                        f"🔧 Keeping potentially useful system parameter: {param}={param_value}"
                    )
                    continue
                else:
                    # Remove clearly invalid parameters
                    params_to_remove.append(param)

            # DISABLED: Auto-removal of invalid parameters to enable proper error feedback
            # Let tools handle their own parameter validation and provide clear error messages
            # for param in params_to_remove:
            #     self.logger.warning(f"🔧 Removing invalid parameter: {param}={validated_params[param]}")
            #     del validated_params[param]

        # Log parameter fixes if any were made
        if validated_params != params:
            self.logger.info(f"🔧 Parameter self-healing applied for {tool_name}")
            self.logger.debug(f"Original params: {params}")
            self.logger.debug(f"Fixed params: {validated_params}")

        return validated_params

    def _fix_parameters_against_schema(
        self,
        params: Dict[str, Any],
        schema: Dict[str, Any],
        tool_name: str,
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Fix parameters against a schema with intelligent defaults."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Get schema properties
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Fix missing required parameters
        for param_name in required:
            if param_name not in fixed_params or fixed_params[param_name] is None:
                before = fixed_params.get(param_name)
                default_value = self._get_smart_default(
                    param_name, properties.get(param_name, {}), tool_name
                )
                if default_value is not None:
                    fixed_params[param_name] = default_value
                    self._add_parameter_fix(
                        fixes,
                        field=param_name,
                        before=before,
                        after=default_value,
                        reason=f"Added missing required parameter '{param_name}'",
                        source="default",
                    )
                    self.logger.info(
                        f"🔧 Added missing required parameter '{param_name}' with default: {default_value}"
                    )

        # Fix parameter types
        for param_name, param_value in fixed_params.items():
            if param_name in properties:
                prop_schema = properties[param_name]
                expected_type = prop_schema.get("type")

                # Try to convert to expected type
                if expected_type and param_value is not None:
                    converted_value = self._convert_parameter_type(
                        param_value, expected_type, param_name
                    )
                    if converted_value != param_value:
                        fixed_params[param_name] = converted_value
                        self._add_parameter_fix(
                            fixes,
                            field=param_name,
                            before=param_value,
                            after=converted_value,
                            reason=f"Converted parameter '{param_name}' to {expected_type}",
                            source="safety_fix",
                        )
                        self.logger.info(
                            f"🔧 Converted parameter '{param_name}' from {type(param_value).__name__} to {expected_type}"
                        )

        # Handle common parameter naming issues
        fixed_params = self._fix_parameter_names(
            fixed_params, properties, tool_name, fixes
        )

        return fixed_params

    def _get_smart_default(
        self, param_name: str, param_schema: Dict[str, Any], tool_name: str
    ) -> Any:
        """Get smart default values for common parameters."""
        param_type = param_schema.get("type", "string")

        # Check if there's a default in the schema
        if "default" in param_schema:
            return param_schema["default"]

        # Smart defaults based on parameter names and tool types
        smart_defaults = {
            # Command-related parameters
            "command": "help" if tool_name == "bash" else None,
            "cmd": "help",
            "timeout": 60,
            # File-related parameters
            "action": self._get_tool_specific_action_default(tool_name),
            "path": "/workspace",
            "file_path": "/workspace",
            "directory": "/workspace",
            "working_directory": "/workspace",
            # Web search parameters
            "query": "help" if tool_name == "web_search" else None,
            "max_results": 5,
            # System parameters
            "packages": [] if param_type == "array" else None,
            # Maven parameters
            "goals": None,
            "profiles": None,
            "properties": None,
            "raw_output": False,
            # Context management
            "context_type": "branch",
            "summary": "Task in progress",
            # Project setup parameters - DO NOT provide defaults for URLs
            # These should come from the user's actual repository URL
            "repository_url": None,
            "url": None,
            "repo_url": None,
            # Generic defaults by type
            "boolean": False,
            "integer": 0,
            "array": [],
            "object": {},
        }

        # Try parameter name first
        if param_name in smart_defaults:
            return smart_defaults[param_name]

        # Try parameter type
        if param_type in smart_defaults:
            return smart_defaults[param_type]

        return None

    def _get_tool_specific_action_default(self, tool_name: str) -> str:
        """Get tool-specific default action."""
        tool_action_defaults = {
            "file_io": "read",
            "project_setup": "clone",
            "system": "install_missing",
            "manage_context": "get_info",
            "maven": "compile",
            "bash": None,
            "web_search": None,
        }
        return tool_action_defaults.get(tool_name, "list")

    def _convert_parameter_type(self, value: Any, expected_type: str, param_name: str) -> Any:
        """Convert parameter to expected type."""
        try:
            if expected_type == "string":
                # Handle list to string conversion properly
                if isinstance(value, list):
                    # If list has one element, return just that element
                    if len(value) == 1:
                        return str(value[0])
                    # If multiple elements, join with spaces (common for command-line args)
                    else:
                        return " ".join(str(v) for v in value)
                return str(value)
            elif expected_type == "integer":
                if isinstance(value, str):
                    # Try to extract number from string
                    import re

                    match = re.search(r"\d+", value)
                    if match:
                        return int(match.group())
                return int(value)
            elif expected_type == "boolean":
                if isinstance(value, str):
                    return value.lower() in ["true", "1", "yes", "on"]
                elif isinstance(value, list):
                    # Handle list to boolean conversion properly
                    if len(value) == 0:
                        return False  # Empty list = False
                    elif len(value) == 1:
                        # Single element - convert that element recursively
                        return self._convert_parameter_type(value[0], "boolean", param_name)
                    else:
                        # Multiple elements - true if any are true
                        return any(
                            self._convert_parameter_type(v, "boolean", param_name) for v in value
                        )
                return bool(value)
            elif expected_type == "array":
                if isinstance(value, str):
                    # Try to parse as JSON array or split by common delimiters
                    try:
                        import json

                        return json.loads(value)
                    except:
                        # Split by common delimiters
                        return [item.strip() for item in value.split(",")]
                elif not isinstance(value, list):
                    return [value]
                return value
            elif expected_type == "object":
                if isinstance(value, str):
                    try:
                        import json

                        return json.loads(value)
                    except:
                        # CRITICAL FIX: Don't lose the original string value!
                        # For manage_context entry parameter, wrap string in meaningful object
                        if param_name == "entry":
                            return {"content": value}  # Preserve the original string as content
                        elif "description" in param_name.lower() or "content" in param_name.lower():
                            return {"description": value}
                        else:
                            return {"value": value}  # Fallback: preserve in generic wrapper
                # Don't wrap lists of dicts unnecessarily
                if isinstance(value, list) and all(
                    isinstance(item, dict) for item in value if value
                ):
                    return value  # Return list of dicts as-is
                return value if isinstance(value, dict) else {"value": value}
        except Exception as e:
            self.logger.warning(f"Failed to convert parameter '{param_name}' to {expected_type}: {e}")
            return value

        return value

    def _fix_parameter_names(
        self,
        params: Dict[str, Any],
        properties: Dict[str, Any],
        tool_name: str,
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Fix common parameter naming issues."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Common parameter name mappings (removed conflicting mappings)
        name_mappings = {
            # Action variations (file_io, context tools)
            "op": "action",
            "operation": "action",
            "method": "action",
            "type": "action",
            # Query variations (web_search tool)
            "search": "query",
            "q": "query",
            "term": "query",
            "search_term": "query",
            "keywords": "query",
            # URL variations (project_setup tool)
            "url": "repository_url",
            "repo_url": "repository_url",
            "git_url": "repository_url",
            "repository": "repository_url",
            "repo": "repository_url",
            "git_repo": "repository_url",
            # Target directory variations (project_setup tool)
            "destination": "target_directory",
            "dest": "target_directory",
            "target_dir": "target_directory",
            "output_dir": "target_directory",
            "clone_dir": "target_directory",
            # Maven/build specific (non-conflicting)
            "options": "properties",
            "opts": "properties",
            "maven_options": "properties",
            "build_options": "properties",
            # Context specific
            "context_type": "action",
            "name": "task_id",
            "parameters": "summary",
            "task_name": "task_id",
            "id": "task_id",
            # Content variations (file_io tool)
            "data": "content",
            "text": "content",
            "body": "content",
            "file_content": "content",
        }

        # Tool-specific mappings for better accuracy
        tool_specific_mappings = {
            "bash": {
                "cmd": "command",
                "script": "command",
                "exec": "command",
                "shell": "command",
                "run": "command",
                "execute": "command",
                "bash_command": "command",
                "shell_command": "command",
                "dir": "working_directory",
                "cwd": "working_directory",
                "working_dir": "working_directory",
                "workdir": "working_directory",  # Map old workdir to working_directory
                "work_dir": "working_directory",
                "directory": "working_directory",
                "path": "working_directory",  # Path should also map to working_directory for bash
            },
            "file_io": {
                "file": "path",
                "filename": "path",
                "filepath": "path",
                "file_path": "path",
                "operation": "action",
                "op": "action",
                "data": "content",
                "text": "content",
            },
            "project_setup": {
                "url": "repository_url",
                "repo": "repository_url",
                "destination": "target_directory",
                "dest": "target_directory",
                "output": "target_directory",
            },
            "maven": {
                # Don't map 'goals' - it's a separate parameter from 'command'
                "options": "properties",
                "dir": "working_directory",
                "project_dir": "working_directory",
                "cmd": "command",  # Common mistake
                "maven_command": "command",
            },
            "manage_context": {
                "type": "action",
                "operation": "action",
                "context_type": "action",
                "name": "task_id",
                "id": "task_id",
                "target": "action",  # Map target to action for switch-like operations
                "switch": "action",  # Map switch to action
                "task_name": "task_id",
                "branch_name": "task_id",
                # CRITICAL FIX: Map content-related parameters to 'entry' for add_context action
                "description": "entry",  # Fixed: was incorrectly mapped to 'summary'
                "content": "entry",
                "data": "entry",
                "info": "entry",
                "details": "entry",
                "context": "entry",
                "observation": "entry",
                "result": "entry",
                # For complete_task action, these should map to summary
                "completion_summary": "summary",
                "task_summary": "summary",
                "results": "summary",
            },
        }

        # Apply tool-specific mappings first (higher priority)
        if tool_name in tool_specific_mappings:
            tool_mappings = tool_specific_mappings[tool_name]
            for old_name, new_name in tool_mappings.items():
                if old_name in fixed_params and new_name in properties:
                    old_value = fixed_params[old_name]
                    # If target parameter exists but old parameter has a non-default value, use the old value
                    if new_name in fixed_params:
                        # Check if the existing value is a default/placeholder value
                        existing_value = fixed_params[new_name]
                        if (
                            existing_value in ["help", "", None]
                            or str(existing_value).strip() == ""
                            or (
                                isinstance(existing_value, str)
                                and len(old_value) > len(existing_value)
                            )
                        ):
                            fixed_params[new_name] = old_value
                            self._add_parameter_fix(
                                fixes,
                                field=new_name,
                                before=existing_value,
                                after=old_value,
                                reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                                source="schema_alias",
                            )
                            self.logger.info(
                                f"🔧 Tool-specific rename (override): '{old_name}' → '{new_name}' for {tool_name}"
                            )
                        else:
                            self._add_parameter_fix(
                                fixes,
                                field=old_name,
                                before=old_value,
                                after=None,
                                reason=f"Removed alias '{old_name}' because '{new_name}' already had a value",
                                source="schema_alias",
                            )
                            self.logger.debug(
                                f"🔧 Skipping rename '{old_name}' → '{new_name}' (target has value: {existing_value})"
                            )
                    else:
                        # Target doesn't exist, normal mapping
                        fixed_params[new_name] = old_value
                        self._add_parameter_fix(
                            fixes,
                            field=new_name,
                            before=None,
                            after=old_value,
                            reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                            source="schema_alias",
                        )
                        self.logger.info(
                            f"🔧 Tool-specific rename: '{old_name}' → '{new_name}' for {tool_name}"
                        )

                    # Always delete the old parameter
                    del fixed_params[old_name]

        # Apply general mappings if target parameter exists in schema
        mappings_applied = []
        for old_name, new_name in name_mappings.items():
            if old_name in fixed_params and new_name in properties and new_name not in fixed_params:
                # Extract value from nested structure if needed (fix for parameters->summary mapping issue)
                old_value = fixed_params[old_name]
                if isinstance(old_value, dict) and len(old_value) == 1 and new_name in old_value:
                    # Handle case where we have {'summary': {'summary': '...'}} -> extract the inner value
                    new_value = old_value[new_name]
                    fixed_params[new_name] = new_value
                    self.logger.info(
                        f"🔧 Extracted nested value from '{old_name}' to '{new_name}' for {tool_name}"
                    )
                else:
                    new_value = old_value
                    fixed_params[new_name] = new_value
                    self.logger.info(
                        f"🔧 Renamed parameter '{old_name}' to '{new_name}' for {tool_name}"
                    )

                self._add_parameter_fix(
                    fixes,
                    field=new_name,
                    before=None,
                    after=new_value,
                    reason=f"Renamed parameter '{old_name}' to '{new_name}'",
                    source="schema_alias",
                )
                del fixed_params[old_name]
                mappings_applied.append(f"{old_name} → {new_name}")

        # Log all mappings applied for debugging
        if mappings_applied:
            self.logger.debug(
                f"Parameter mappings applied for {tool_name}: {', '.join(mappings_applied)}"
            )

        return fixed_params

    def _apply_basic_parameter_fixes(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Apply basic parameter fixes when schema is not available."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        # Tool-specific basic fixes
        if tool_name == "maven":
            if not fixed_params.get("command"):
                before = fixed_params.get("command")
                fixed_params["command"] = "compile"
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="compile",
                    reason="Added default Maven command",
                    source="default",
                )
        elif tool_name == "bash":
            if not fixed_params.get("command"):
                before = fixed_params.get("command")
                fixed_params["command"] = "pwd"  # Safe default
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="pwd",
                    reason="Added default bash command",
                    source="default",
                )
        elif tool_name == "file_io":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "read"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="read",
                    reason="Added default file_io action",
                    source="default",
                )
            if not fixed_params.get("file_path") and fixed_params.get("action") == "read":
                before = fixed_params.get("file_path")
                fixed_params["file_path"] = "/workspace"
                self._add_parameter_fix(
                    fixes,
                    field="file_path",
                    before=before,
                    after="/workspace",
                    reason="Added default file path for read action",
                    source="default",
                )
        elif tool_name == "manage_context":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "get_info"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="get_info",
                    reason="Added default manage_context action",
                    source="default",
                )
        elif tool_name == "project_setup":
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                # If we have a repository URL, default to clone
                if self.repository_url:
                    fixed_params["action"] = "clone"
                    self._add_parameter_fix(
                        fixes,
                        field="action",
                        before=before,
                        after="clone",
                        reason="Added default project_setup action",
                        source="default",
                    )
                    repo_before = fixed_params.get("repository_url")
                    fixed_params["repository_url"] = self.repository_url
                    self._add_parameter_fix(
                        fixes,
                        field="repository_url",
                        before=repo_before,
                        after=self.repository_url,
                        reason="Injected repository URL from orchestrator state",
                        source="state_injection",
                    )
                else:
                    fixed_params["action"] = "detect_project_type"
                    self._add_parameter_fix(
                        fixes,
                        field="action",
                        before=before,
                        after="detect_project_type",
                        reason="Added default project_setup action",
                        source="default",
                    )
        elif tool_name == "web_search":
            if not fixed_params.get("query"):
                before = fixed_params.get("query")
                fixed_params["query"] = "help"
                self._add_parameter_fix(
                    fixes,
                    field="query",
                    before=before,
                    after="help",
                    reason="Added default web_search query",
                    source="default",
                )

        return fixed_params

    def _apply_tool_specific_fixes(
        self,
        tool_name: str,
        params: Dict[str, Any],
        parameter_fixes: Optional[list[ParameterFix]] = None,
    ) -> Dict[str, Any]:
        """Apply tool-specific parameter fixes using state memory."""
        fixes = parameter_fixes if parameter_fixes is not None else []
        fixed_params = params.copy()

        if tool_name == "project_setup":
            # Auto-inject repository URL if available and action is clone
            if fixed_params.get("action") == "clone" and not fixed_params.get("repository_url"):
                if self.repository_url:
                    before = fixed_params.get("repository_url")
                    fixed_params["repository_url"] = self.repository_url
                    self._add_parameter_fix(
                        fixes,
                        field="repository_url",
                        before=before,
                        after=self.repository_url,
                        reason="Injected repository URL from orchestrator state",
                        source="state_injection",
                    )
                    self.logger.info(f"🔧 Auto-injected repository URL: {self.repository_url}")

            # CRITICAL FIX: Handle target_directory correctly for workspace vs fallback modes
            if fixed_params.get("action") == "clone":
                # Check current workspace status
                is_fallback_mode = self.successful_states.get("workspace_fallback", False)
                current_workdir = self.successful_states.get("working_directory", "/workspace")

                if is_fallback_mode:
                    # We're in abnormal fallback mode - need to specify full path
                    fallback_reason = self.successful_states.get(
                        "fallback_reason", "Unknown reason"
                    )

                    self.logger.error(f"🚨 CLONE IN FALLBACK MODE: Using {current_workdir}")
                    self.logger.error(f"🚨 Reason: {fallback_reason}")
                    self.logger.error("🚨 This is SUBOPTIMAL - clone should happen in /workspace")

                    # For fallback mode, we need to specify the full path
                    if not fixed_params.get("target_directory"):
                        # Extract project name from URL
                        repo_name = (
                            fixed_params.get("repository_url", "")
                            .split("/")[-1]
                            .replace(".git", "")
                        )
                        before = fixed_params.get("target_directory")
                        if repo_name:
                            fallback_target = f"{current_workdir}/{repo_name}"
                            fixed_params["target_directory"] = fallback_target
                            self._add_parameter_fix(
                                fixes,
                                field="target_directory",
                                before=before,
                                after=fallback_target,
                                reason="Injected fallback clone target from current working directory",
                                source="state_injection",
                            )
                            self.logger.error(f"🚨 Setting fallback clone target: {fallback_target}")
                        else:
                            # Use fallback directory as-is
                            fixed_params["target_directory"] = current_workdir
                            self._add_parameter_fix(
                                fixes,
                                field="target_directory",
                                before=before,
                                after=current_workdir,
                                reason="Injected fallback clone target from current working directory",
                                source="state_injection",
                            )
                            self.logger.error(f"🚨 Using fallback directory directly: {current_workdir}")
                else:
                    # Normal case - workspace is available
                    self.logger.info("✅ CLONE IN WORKSPACE: Standard workspace cloning")

                    # CRITICAL FIX: Don't set target_directory to /workspace!
                    # Let project_setup tool auto-generate the project subdirectory name
                    if fixed_params.get("target_directory") == "/workspace":
                        # Remove the incorrect target_directory - let tool auto-generate
                        before = fixed_params["target_directory"]
                        del fixed_params["target_directory"]
                        self._add_parameter_fix(
                            fixes,
                            field="target_directory",
                            before=before,
                            after=None,
                            reason="Removed workspace root clone target so project_setup can create a subdirectory",
                            source="safety_fix",
                        )
                        self.logger.info(
                            "🔧 Removed incorrect target_directory, will auto-generate project subdirectory"
                        )
                    elif not fixed_params.get("target_directory"):
                        # No target_directory specified - this is correct, tool will auto-generate
                        self.logger.info(
                            "✅ No target_directory specified - project_setup will create subdirectory"
                        )
                    else:
                        # Explicit target_directory specified
                        target_dir = fixed_params["target_directory"]
                        if not target_dir.startswith("/workspace/"):
                            self.logger.warning(f"⚠️ EXPLICIT NON-WORKSPACE CLONE: {target_dir}")
                            self.logger.warning("⚠️ This may cause project layout issues")
                        else:
                            self.logger.info(f"✅ Workspace subdirectory clone: {target_dir}")

            # Prevent duplicate cloning
            cloned_repos = self.successful_states.get("cloned_repos", set())
            if (
                fixed_params.get("action") == "clone"
                and fixed_params.get("repository_url") in cloned_repos
            ):
                before = fixed_params.get("action")
                self.logger.warning(
                    "🔧 Repository already cloned, changing action to detect_project_type"
                )
                fixed_params["action"] = "detect_project_type"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="detect_project_type",
                    reason="Avoided duplicate clone for already cloned repository",
                    source="safety_fix",
                )

        elif tool_name == "maven":
            # Ensure maven has a valid command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                before = fixed_params.get("command")
                # Use intelligent default based on current state
                if self.successful_states.get("maven_success"):
                    fixed_params["command"] = "test"  # If compile succeeded before, try test
                else:
                    fixed_params["command"] = "compile"  # Start with compile
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after=fixed_params["command"],
                    reason="Added default Maven command based on successful state",
                    source="default",
                )

            # Auto-inject successful working directory for Maven operations
            if "working_directory" not in fixed_params:
                if self.successful_states.get("working_directory"):
                    before = fixed_params.get("working_directory")
                    fixed_params["working_directory"] = self.successful_states[
                        "working_directory"
                    ]
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after=self.successful_states["working_directory"],
                        reason="Injected working directory from successful state",
                        source="state_injection",
                    )
                    self.logger.info(
                        f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}"
                    )
                else:
                    # Try to infer from repository URL
                    if self.repository_url:
                        repo_name = self.repository_url.split("/")[-1].replace(".git", "")
                        inferred_workdir = f"/workspace/{repo_name}"
                        before = fixed_params.get("working_directory")
                        fixed_params["working_directory"] = inferred_workdir
                        self._add_parameter_fix(
                            fixes,
                            field="working_directory",
                            before=before,
                            after=inferred_workdir,
                            reason="Inferred working directory from repository URL",
                            source="state_injection",
                        )
                        self.logger.info(
                            f"🔧 Inferred working directory from repo: /workspace/{repo_name}"
                        )

            # Convert common typos
            command = fixed_params.get("command", "")
            if command in ["test", "tests"]:
                fixed_params["command"] = "test"
            elif command in ["build", "compile"]:
                fixed_params["command"] = "compile"
            elif command in ["install", "package"]:
                fixed_params["command"] = "package"
            self._add_parameter_fix(
                fixes,
                field="command",
                before=command,
                after=fixed_params.get("command"),
                reason="Normalized Maven command alias",
                source="safety_fix",
            )

        elif tool_name == "bash":
            # Ensure bash has a command
            if not fixed_params.get("command") or fixed_params.get("command").strip() == "":
                before = fixed_params.get("command")
                fixed_params["command"] = "pwd"
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after="pwd",
                    reason="Added default bash command",
                    source="default",
                )

            # Auto-inject successful working directory for bash operations
            if "working_directory" not in fixed_params:
                before = fixed_params.get("working_directory")
                if self.successful_states.get("working_directory"):
                    fixed_params["working_directory"] = self.successful_states[
                        "working_directory"
                    ]
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after=self.successful_states["working_directory"],
                        reason="Injected working directory from successful state",
                        source="state_injection",
                    )
                    self.logger.info(
                        f"🔧 Auto-injected successful working directory: {self.successful_states['working_directory']}"
                    )
                else:
                    fixed_params["working_directory"] = "/workspace"
                    self._add_parameter_fix(
                        fixes,
                        field="working_directory",
                        before=before,
                        after="/workspace",
                        reason="Injected default workspace working directory",
                        source="state_injection",
                    )

            command_str = fixed_params.get("command", "")
            if command_str and "mvn" in command_str and "--fail-at-end" not in command_str:
                before = command_str
                fixed_params["command"] = f"{command_str} --fail-at-end"
                self._add_parameter_fix(
                    fixes,
                    field="command",
                    before=before,
                    after=fixed_params["command"],
                    reason="Appended Maven fail-at-end flag to bash command",
                    source="safety_fix",
                )
                self.logger.info("🔧 Appended --fail-at-end to bash Maven command")

        elif tool_name == "file_io":
            # Ensure file_io has an action
            if not fixed_params.get("action"):
                before = fixed_params.get("action")
                fixed_params["action"] = "read"
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=before,
                    after="read",
                    reason="Added default file_io action",
                    source="default",
                )

            # CRITICAL PRIORITY: Use workspace paths when possible, warn about fallbacks
            current_workdir = self.successful_states.get("working_directory", "/workspace")
            is_fallback_mode = self.successful_states.get("workspace_fallback", False)

            # If reading but no file path, default to current directory listing
            if fixed_params.get("action") == "read" and not fixed_params.get("path"):
                action_before = fixed_params.get("action")
                path_before = fixed_params.get("path")
                fixed_params["action"] = "list"
                fixed_params["path"] = current_workdir
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=action_before,
                    after="list",
                    reason="Switched read without path to directory listing",
                    source="safety_fix",
                )
                self._add_parameter_fix(
                    fixes,
                    field="path",
                    before=path_before,
                    after=current_workdir,
                    reason="Injected current working directory for file listing",
                    source="state_injection",
                )

                if is_fallback_mode:
                    self.logger.error(
                        f"🚨 FILE_IO FALLBACK: Listing {current_workdir} (not in workspace)"
                    )
                else:
                    self.logger.info(f"✅ FILE_IO WORKSPACE: Listing {current_workdir}")

            # If path is relative and we have a known working directory, make it absolute
            elif fixed_params.get("path") and not fixed_params["path"].startswith("/"):
                relative_path = fixed_params["path"]
                absolute_path = f"{current_workdir}/{relative_path}"
                fixed_params["path"] = absolute_path
                self._add_parameter_fix(
                    fixes,
                    field="path",
                    before=relative_path,
                    after=absolute_path,
                    reason="Resolved relative file path against current working directory",
                    source="safety_fix",
                )

                if is_fallback_mode:
                    self.logger.error(
                        f"🚨 FILE_IO FALLBACK PATH: {relative_path} → {absolute_path} (not in workspace)"
                    )
                else:
                    self.logger.info(f"✅ FILE_IO WORKSPACE PATH: {relative_path} → {absolute_path}")

            # PRIORITY CHECK: If path points to /workspace but we're in fallback mode, this is concerning
            elif fixed_params.get("path") and fixed_params["path"].startswith("/workspace"):
                if is_fallback_mode and not current_workdir.startswith("/workspace"):
                    original_path = fixed_params["path"]
                    self.logger.error(
                        f"🚨 FILE_IO MISMATCH: Requesting {original_path} but workspace unavailable"
                    )
                    self.logger.error(f"🚨 Current fallback directory: {current_workdir}")

                    # Try to map /workspace/... to current_workdir/...
                    relative_part = original_path.replace("/workspace", "").lstrip("/")
                    if relative_part:
                        adjusted_path = f"{current_workdir}/{relative_part}"
                        self.logger.error(
                            f"🚨 ATTEMPTING PATH MAPPING: {original_path} → {adjusted_path}"
                        )
                        self.logger.error("🚨 This may fail if files are actually in /workspace")
                    else:
                        adjusted_path = current_workdir
                        self.logger.error(f"🚨 MAPPING WORKSPACE ROOT to fallback: {adjusted_path}")

                    fixed_params["path"] = adjusted_path
                    self._add_parameter_fix(
                        fixes,
                        field="path",
                        before=original_path,
                        after=adjusted_path,
                        reason="Mapped workspace path to fallback working directory",
                        source="safety_fix",
                    )
                else:
                    # Normal case - workspace path and we're in workspace
                    if not is_fallback_mode:
                        self.logger.debug(f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']}")
                    else:
                        self.logger.info(
                            f"✅ FILE_IO WORKSPACE: Accessing {fixed_params['path']} (workspace available)"
                        )

            # If we're in fallback mode, warn about any non-fallback paths
            elif is_fallback_mode and fixed_params.get("path"):
                path = fixed_params["path"]
                if not path.startswith(current_workdir):
                    self.logger.warning(
                        f"⚠️ FILE_IO OUTSIDE FALLBACK: Accessing {path} while in fallback mode ({current_workdir})"
                    )
                    self.logger.warning("⚠️ This may fail if the path doesn't exist")

        elif tool_name == "manage_context":
            # Fix common action name errors with comprehensive alias mapping
            action = fixed_params.get("action", "")

            # Map common variations to correct actions
            action_aliases = {
                # Start task aliases
                "start": "start_task",
                "begin": "start_task",
                "create": "start_task",
                "create_branch": "start_task",
                "new": "start_task",
                "new_task": "start_task",
                # Get info aliases
                "info": "get_info",
                "status": "get_info",
                "current": "get_info",
                "check": "get_info",
                # Complete task aliases
                "complete": "complete_task",
                "finish": "complete_task",
                "end": "complete_task",
                "done": "complete_task",
                "complete_branch": "complete_task",
                "switch_to_trunk": "complete_task",
                "failure": "complete_task",
                "failed": "complete_task",
                # Add context aliases
                "add": "add_context",
                "record": "add_context",
                "log": "add_context",
                # Get context aliases
                "get": "get_full_context",
                "show": "get_full_context",
                "view": "get_full_context",
                "history": "get_full_context",
                # Compact context aliases
                "compress": "compact_context",
                "compact": "compact_context",
                "reduce": "compact_context",
            }

            if action in action_aliases:
                original_action = action
                fixed_params["action"] = action_aliases[action]
                self._add_parameter_fix(
                    fixes,
                    field="action",
                    before=original_action,
                    after=action_aliases[action],
                    reason="Normalized manage_context action alias",
                    source="safety_fix",
                )
                self.logger.info(
                    f"🔧 Converted action '{original_action}' to '{action_aliases[action]}' for manage_context"
                )

                # Add default summary for completion actions
                if action_aliases[action] == "complete_task" and not fixed_params.get("summary"):
                    summary_before = fixed_params.get("summary")
                    if action in ["failure", "failed"]:
                        fixed_params["summary"] = (
                            "Task failed to complete successfully due to encountered issues"
                        )
                    else:
                        fixed_params["summary"] = "Task completed with mixed results"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=summary_before,
                        after=fixed_params["summary"],
                        reason="Added default summary for complete_task action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for complete_task action")
            elif action == "switch_to_trunk":
                # This is correct, but ensure we have a summary if needed
                if not fixed_params.get("summary"):
                    before = fixed_params.get("summary")
                    fixed_params["summary"] = "Switching back to trunk context"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=before,
                        after="Switching back to trunk context",
                        reason="Added default summary for switch_to_trunk action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for switch_to_trunk action")

            # Ensure required parameters for create_branch
            if fixed_params.get("action") == "create_branch":
                if not fixed_params.get("task_id"):
                    # Generate a default task_id if missing
                    before = fixed_params.get("task_id")
                    summary = fixed_params.get("summary", "default_task")
                    task_id = summary.replace(" ", "_").lower()[:20]
                    fixed_params["task_id"] = task_id
                    self._add_parameter_fix(
                        fixes,
                        field="task_id",
                        before=before,
                        after=task_id,
                        reason="Generated missing task_id from summary",
                        source="default",
                    )
                    self.logger.info(f"🔧 Generated missing task_id: {task_id}")

            # For start_task, ensure we have task_id
            elif fixed_params.get("action") == "start_task":
                if not fixed_params.get("task_id"):
                    # Auto-inject the correct next task ID based on context
                    before = fixed_params.get("task_id")
                    fixed_params["task_id"] = "task_1"  # Default to first task
                    self._add_parameter_fix(
                        fixes,
                        field="task_id",
                        before=before,
                        after="task_1",
                        reason="Added default task_id for start_task",
                        source="default",
                    )
                    self.logger.info("🔧 Auto-injected default task_id: task_1 for start_task")

            # For complete_task, ensure we have summary
            elif fixed_params.get("action") == "complete_task":
                if not fixed_params.get("summary"):
                    before = fixed_params.get("summary")
                    fixed_params["summary"] = "Task completed with mixed results"
                    self._add_parameter_fix(
                        fixes,
                        field="summary",
                        before=before,
                        after="Task completed with mixed results",
                        reason="Added default summary for complete_task action",
                        source="default",
                    )
                    self.logger.info("🔧 Added default summary for complete_task action")

        return fixed_params

    def _generate_unknown_tool_feedback(self, requested_tool: str) -> str:
        """Generate comprehensive feedback for unknown tool requests."""
        # Common tool name mappings
        tool_mappings = {
            "git": "project_setup",
            "git_clone": "project_setup",
            "clone": "project_setup",
            "setup": "project_setup",
            "mvn": "maven",
            "gradle_build": "gradle",
            "npm": "bash",
            "pip": "bash",
            "python": "bash",
            "ls": "bash",
            "cd": "bash",
            "cat": "file_io",
            "echo": "bash",
            "mkdir": "bash",
            "rm": "bash",
            "cp": "bash",
            "mv": "bash",
            "find": "file_search",
            "grep": "file_search",
            "test": "bash",
            "build": "maven or gradle or bash",
            "compile": "maven or gradle",
            "install": "system or bash",
            "package": "maven or gradle",
            "analyze": "project_analyzer",
            "review": "code_review",
            "report": "report",
            "context": "manage_context",
            "search": "file_search or web_search",
            "read": "file_io",
            "write": "file_io",
            "edit": "file_io",
        }

        # Build feedback message
        feedback_parts = [f"❌ Tool '{requested_tool}' does not exist."]

        # Check for direct mapping
        if requested_tool.lower() in tool_mappings:
            suggested = tool_mappings[requested_tool.lower()]
            feedback_parts.append(f"\n✅ Did you mean: {suggested}?")

            # Add usage example for the suggested tool
            if suggested == "project_setup":
                feedback_parts.append(
                    "\n📝 Usage: Use 'project_setup' with action='clone' and repo_url to clone repositories"
                )
            elif suggested == "maven":
                feedback_parts.append(
                    "\n📝 Usage: Use 'maven' with command='compile' or 'test' or 'package'"
                )
            elif suggested == "bash":
                feedback_parts.append(
                    f"\n📝 Usage: Use 'bash' with command='{requested_tool} <args>' to run shell commands"
                )
            elif suggested == "manage_context":
                feedback_parts.append(
                    "\n📝 Usage: Use 'manage_context' with action='get_info' or 'create_branch'"
                )
        else:
            # Find similar tool names
            available_tools = list(self.tools.keys())
            close_matches = get_close_matches(requested_tool, available_tools, n=3, cutoff=0.6)

            if close_matches:
                feedback_parts.append(f"\n✅ Did you mean one of these? {', '.join(close_matches)}")

            # Always list available tools for reference
            feedback_parts.append("\n\n📋 Available tools:")
            tool_descriptions = {
                "bash": "Execute shell commands",
                "file_io": "Read, write, and manipulate files",
                "file_search": "Search for files and content",
                "web_search": "Search the web for information",
                "project_setup": "Clone repositories and install dependencies",
                "project_analyzer": "Analyze project structure and requirements",
                "maven": "Run Maven build commands",
                "gradle": "Run Gradle build commands",
                "system": "Install system packages and configure Java",
                "manage_context": "Manage task context and branching",
                "report": "Generate project reports",
                "code_review": "Review code quality and security",
                "output_search": "Search truncated outputs",
            }

            for tool in available_tools[:10]:  # Show first 10 tools
                desc = tool_descriptions.get(tool, "Tool for specialized operations")
                feedback_parts.append(f"  • {tool}: {desc}")

            if len(available_tools) > 10:
                feedback_parts.append(f"  ... and {len(available_tools) - 10} more tools")

        # Add general guidance
        feedback_parts.append("\n\n💡 Tips:")
        feedback_parts.append("• For shell commands, use 'bash' tool with command parameter")
        feedback_parts.append("• For file operations, use 'file_io' tool with action parameter")
        feedback_parts.append("• For Java projects, use 'maven' or 'gradle' tools")
        feedback_parts.append(
            "• Check tool parameter requirements with action='help' where supported"
        )

        return "\n".join(feedback_parts)

    def _log_unknown_tool_attempt(self, call: ToolCall, feedback: str) -> None:
        try:
            from sag.agent.error_logger import ErrorLogger

            error_logger = ErrorLogger.get_instance()

            # Extract suggested tool from feedback
            suggested_tool = None
            if "Did you mean:" in feedback:
                # Parse suggested tool from feedback
                lines = feedback.split("\n")
                for line in lines:
                    if "Did you mean:" in line:
                        suggested_tool = line.split("Did you mean:")[1].strip().rstrip("?")
                        break

            error_logger.log_unknown_tool(
                requested_tool=call.name,
                suggested_tool=suggested_tool,
                feedback_provided=feedback,
                context={
                    "step_content": call.raw_action_text,
                    "tool_params": call.raw_params,
                    "available_tools": list(self.tools.keys()),
                },
            )
        except Exception as exc:
            self.logger.warning(f"Failed to log unknown tool to error logger: {exc}")

    def _track_tool_execution(self, signature: str, success: bool) -> None:
        try:
            self.track_tool_execution(signature, success)
        except Exception as exc:
            self.logger.warning(f"Failed to track tool execution: {exc}")

    def _update_successful_states(
        self, tool_name: str, params: Dict[str, Any], result: ToolResult
    ) -> None:
        try:
            self.update_successful_states(tool_name, params, result)
        except Exception as exc:
            self.logger.warning(f"Failed to update successful tool state: {exc}")

    def _emit(
        self,
        event_type: ToolLifecycleEventType,
        call: ToolCall,
        *,
        message: str,
        level: ToolLifecycleLevel = "info",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.event_sink:
            return

        try:
            self.event_sink(
                ToolLifecycleEvent(
                    event_type=event_type,
                    call=call,
                    message=message,
                    level=level,
                    metadata=metadata or {},
                )
            )
        except Exception as exc:
            self.logger.warning(f"Failed to emit tool lifecycle event: {exc}")
