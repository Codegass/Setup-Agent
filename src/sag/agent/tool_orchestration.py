"""Internal tool orchestration contracts and execution boundary."""

from __future__ import annotations

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

        validated_params = dict(
            call.validated_params if call.validated_params is not None else call.raw_params
        )
        call.validated_params = validated_params
        signature = f"{call.name}:{str(sorted(validated_params.items()))}"
        call.execution_signature = signature

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
                metadata={"execution_signature": signature},
            )
            self._emit(
                "tool_error",
                call,
                message=observation_text,
                level="error",
                metadata={"status": execution.status, "execution_signature": signature},
            )
            return execution

        self._track_tool_execution(signature, result.success)
        if result.success:
            self._update_successful_states(call.name, validated_params, result)

        observation_text = format_tool_result(call.name, result)
        status: ToolExecutionStatus = "success" if result.success else "failure"
        execution = ToolExecution(
            call=call,
            result=result,
            status=status,
            raw_params=call.raw_params,
            validated_params=validated_params,
            executed_params=validated_params,
            observation_text=observation_text,
            attempted_execution=True,
            parameter_fixes=call.parameter_fixes,
            metadata={"execution_signature": signature},
        )
        self._emit(
            "tool_result",
            call,
            message=observation_text,
            level="success" if result.success else "error",
            metadata={
                "status": execution.status,
                "result_success": result.success,
                "error_code": result.error_code,
                "executed_params": validated_params,
                "recovery_applied": False,
                "execution_signature": signature,
            },
        )
        return execution

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
