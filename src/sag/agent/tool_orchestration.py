"""Internal tool orchestration contracts and execution boundary."""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, Callable, Dict, Literal, MutableSequence, Optional

from loguru import logger as default_logger

from sag.evidence import EvidenceAssessment, InvocationStatus, OperationOutcome
from sag.tools.base import BaseTool, ToolResult, bind_tool_result_output_storage

ParameterFixSource = Literal["schema_alias", "default", "state_injection", "safety_fix"]
ToolExecutionStatus = Literal[
    "success",
    "pending",
    "partial",
    "unknown",
    "skipped",
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


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    signature: str
    invocation_status: InvocationStatus
    operation_outcome: OperationOutcome
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


def _format_maven_version_contract(result: ToolResult) -> str:
    metadata = result.metadata or {}
    requirement = metadata.get("maven_version_requirement")
    if not requirement:
        return ""

    lines = [
        "",
        "Maven version contract:",
        f"Maven version requirement: {requirement.get('raw')} (source: {requirement.get('source', 'unknown')})",
    ]

    runtime = metadata.get("maven_runtime") or {}
    if runtime.get("executable"):
        lines.append(f"Current Maven executable: {runtime['executable']}")
    if runtime.get("version"):
        lines.append(f"Current Maven version: {runtime['version']}")

    if metadata.get("compatible_maven_candidate") is None:
        lines.append("Compatible Maven candidate: none")

    raw_requirement = requirement.get("raw")
    if raw_requirement:
        lines.append(
            "Next action: provide or register a Maven executable that satisfies "
            f"{raw_requirement} via project(action='env'), then retry the build"
        )

    return "\n".join(lines)


def _format_evidence_observation(result: ToolResult) -> list[str]:
    normalized_status = result.evidence_assessment
    include_status = bool(
        normalized_status != EvidenceAssessment.SUCCESS
        or result.evidence_refs
        or result.conflicts
        or result.test_stats
    )

    lines: list[str] = []
    if include_status:
        lines.append(f"Evidence status: {normalized_status.value}")
    if result.evidence_refs:
        lines.append(f"Evidence refs: {', '.join(result.evidence_refs)}")
    if result.conflicts:
        lines.append(f"Conflicts: {', '.join(result.conflicts)}")
    if result.test_stats:
        lines.append(f"Test stats: {result.test_stats.as_summary()}")
    return lines


def format_tool_result(tool_name: str, result: ToolResult) -> str:
    """Format tool result for observation. Output truncation is now handled in BaseTool."""
    evidence_lines = _format_evidence_observation(result)

    if result.operation_outcome is not OperationOutcome.FAILED:
        if result.invocation_status is InvocationStatus.PENDING:
            if result.metadata.get("dispatch_status") == "liveness_unknown_detached":
                formatted = (
                    f"⏳ {tool_name} dispatched — command liveness is unknown; "
                    f"poll existing job {result.poll_ref}"
                )
            else:
                formatted = f"⏳ {tool_name} dispatched — command still running in background"
        elif result.succeeded:
            formatted = f"✅ {tool_name} executed successfully"
        else:
            outcome = result.operation_outcome.value
            icon = {"partial": "⚠️", "unknown": "❔", "skipped": "⏭"}.get(outcome, "✅")
            formatted = f"{icon} {tool_name} result: {outcome.upper()}"

        if evidence_lines:
            formatted += "\n" + "\n".join(evidence_lines)

        # Add command information for bash tool
        if tool_name == "bash" and result.metadata and "command" in result.metadata:
            formatted += f"\nCommand: {result.metadata['command']}"

        # Add output (already processed by BaseTool truncation)
        if result.output:
            formatted += f"\n\nOutput: {result.output}"

        # Envelope extras (spec §5): machine-readable facts and retrieval refs.
        facts = getattr(result, "facts", None)
        if facts:
            fact_text = ", ".join(f"{k}={v}" for k, v in list(facts.items())[:10])
            formatted += f"\nFacts: {fact_text}"
        refs = getattr(result, "refs", None)
        if refs:
            formatted += f"\nFull output refs (use search tool): {', '.join(refs[:5])}"

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

        if evidence_lines:
            formatted += "\n" + "\n".join(evidence_lines)

        # Add command information for failed bash tool
        if tool_name == "bash" and result.metadata and "command" in result.metadata:
            formatted += f"\nCommand: {result.metadata['command']}"

        # Show extracted error details from output (especially important for maven tool)
        if result.output and result.output.strip():
            formatted += f"\n\n{result.output}"

        if tool_name in ("maven", "build"):
            formatted += _format_maven_version_contract(result)

        if result.suggestions:
            formatted += f"\n\nSuggestions:\n" + "\n".join(f"• {s}" for s in result.suggestions[:3])

        if result.error_code:
            formatted += f"\nError code: {result.error_code}"
        if result.failure_signature:
            formatted += f"\nFailure signature: {result.failure_signature}"
        if result.error_tail_preview:
            formatted += f"\nError tail: {result.error_tail_preview}"
        if result.output_ref:
            formatted += f"\nFull output ref: {result.output_ref}"

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
        repository_ref: Optional[str] = None,
        track_tool_execution: Callable[[str, ToolResult], None],
        update_successful_states: Callable[[str, Dict[str, Any], ToolResult], None],
        add_system_guidance: Callable[[str, GuidancePriority], None],
        get_timestamp: Callable[[], str],
        event_sink: Optional[Callable[[ToolLifecycleEvent], None]] = None,
        output_storage: Any = None,
        logger: Any = None,
    ):
        from sag.agent.tool_parameters import ToolParameterNormalizer
        from sag.agent.tool_recovery import ToolRecoveryHandler

        self.tools = tools
        self.context_manager = context_manager
        self.recent_tool_executions = recent_tool_executions
        self.successful_states = successful_states
        self.repository_url = repository_url
        self.repository_ref = repository_ref
        self.track_tool_execution = track_tool_execution
        self.update_successful_states = update_successful_states
        self.add_system_guidance = add_system_guidance
        self.get_timestamp = get_timestamp
        self.event_sink = event_sink
        self.output_storage = output_storage
        self.logger = logger or default_logger
        self.parameter_normalizer = ToolParameterNormalizer(
            tools=self.tools,
            successful_states=self.successful_states,
            repository_url=self.repository_url,
            repository_ref=self.repository_ref,
            logger=self.logger,
        )
        self.recovery_handler = ToolRecoveryHandler(
            tools=self.tools,
            context_manager=self.context_manager,
            successful_states=self.successful_states,
            repository_url=self.repository_url,
            repository_ref=self.repository_ref,
            add_system_guidance=self.add_system_guidance,
            logger=self.logger,
        )

    def _recommended_workdir(self, action: str) -> Optional[str]:
        """Analyzer's recommended reactor root for a build/test call, or None.

        Turns the build_recommendation (env summary) from advisory prose into the
        enforced working_directory default when the model omits one — test_root for
        the test phase, else build_root. Best-effort: any failure yields None so the
        caller keeps the existing /workspace default.
        """
        try:
            trunk = self.context_manager.load_trunk_context()
            rec = (getattr(trunk, "environment_summary", None) or {}).get("build_recommendation")
        except Exception:
            return None
        if not rec:
            return None
        root = rec.get("test_root") if action == "test" else rec.get("build_root")
        return root or None

    def execute(self, call: ToolCall) -> ToolExecution:
        if self.output_storage is None:
            return self._execute(call)
        task_id = str(getattr(self.context_manager, "current_task_id", None) or call.name)
        with bind_tool_result_output_storage(
            self.output_storage,
            task_id=task_id,
            tool_name=call.name,
        ):
            return self._execute(call)

    def _execute(self, call: ToolCall) -> ToolExecution:
        started_at = time.perf_counter()
        model_omitted_workdir = not str(
            (call.raw_params or {}).get("working_directory") or ""
        ).strip()
        if call.name not in self.tools:
            # Legacy tool names (model drift) map onto their stage-1 successors
            # before any lookup, so old names execute instead of failing.
            resolved_name, resolved_params = self.parameter_normalizer.resolve_legacy_alias(
                call.name, call.raw_params
            )
            if resolved_name != call.name:
                self.logger.info(f"Legacy tool alias resolved: {call.name} -> {resolved_name}")
                call.name = resolved_name
                call.raw_params = resolved_params
        start_signature = call.execution_signature or self._execution_signature(
            call.name, call.validated_params or call.raw_params
        )
        self._emit(
            "tool_start",
            call,
            message=f"Starting {call.name}",
            level="info",
            metadata={
                "tool_name": call.name,
                "source_step_index": call.source_step_index,
                "raw_params": call.raw_params,
                "execution_signature": start_signature,
            },
        )

        if call.name not in self.tools:
            feedback = self._generate_unknown_tool_feedback(call.name)
            self._log_unknown_tool_attempt(call, feedback)
            result = ToolResult.completed_failure(
                output=feedback,
                error=f"Unknown tool requested: {call.name}",
                error_code="UNKNOWN_TOOL",
                suggestions=self._unknown_tool_suggestions(call.name),
            )
            duration_ms = self._duration_since(started_at)
            execution = ToolExecution(
                call=call,
                result=result,
                status="missing_tool",
                raw_params=call.raw_params,
                validated_params=call.validated_params,
                duration_ms=duration_ms,
                observation_text=feedback,
                attempted_execution=False,
                parameter_fixes=call.parameter_fixes,
                metadata={"execution_signature": start_signature},
            )
            self._emit(
                "tool_error",
                call,
                message=feedback,
                level="error",
                metadata={
                    "status": execution.status,
                    "raw_params": call.raw_params,
                    "execution_signature": start_signature,
                    **self._tool_error_metadata(
                        result, recovery_attempted=False, category="validation"
                    ),
                },
            )
            return execution

        if call.name == "build" and model_omitted_workdir:
            action = str((call.raw_params or {}).get("action") or "").strip().lower()
            recommended_workdir = self._recommended_workdir(action)
            if recommended_workdir:
                before = (call.raw_params or {}).get("working_directory")
                call.raw_params = {
                    **(call.raw_params or {}),
                    "working_directory": recommended_workdir,
                }
                call.parameter_fixes = [
                    *call.parameter_fixes,
                    ParameterFix(
                        field="working_directory",
                        before=before,
                        after=recommended_workdir,
                        reason="analyzer-recommended reactor root (model omitted working_directory)",
                        source="state_injection",
                    ),
                ]

        parameter_fixes = call.parameter_fixes
        if call.validated_params is None:
            try:
                validated_params = self.parameter_normalizer.validate_and_fix(
                    call.name, call.raw_params, parameter_fixes
                )
            except Exception as exc:
                result = ToolResult.completed_failure(
                    output="",
                    error=f"Tool {call.name} parameter validation failed: {exc}",
                    error_code="PARAMETER_VALIDATION_FAILED",
                    metadata={
                        "exception_type": type(exc).__name__,
                        "failure_category": "validation",
                    },
                )
                duration_ms = self._duration_since(started_at)
                observation_text = format_tool_result(call.name, result)
                execution = ToolExecution(
                    call=call,
                    result=result,
                    status="validation_failed",
                    raw_params=call.raw_params,
                    validated_params=None,
                    executed_params=None,
                    duration_ms=duration_ms,
                    observation_text=observation_text,
                    attempted_execution=False,
                    parameter_fixes=parameter_fixes,
                    metadata={
                        "execution_signature": start_signature,
                        "validation_exception": type(exc).__name__,
                    },
                )
                self._emit(
                    "tool_error",
                    call,
                    message=observation_text,
                    level="error",
                    metadata={
                        "status": execution.status,
                        "raw_params": call.raw_params,
                        "execution_signature": start_signature,
                        **self._tool_error_metadata(result, recovery_attempted=False),
                    },
                )
                return execution
        else:
            validated_params = dict(call.validated_params)

        call.validated_params = validated_params
        call.parameter_fixes = parameter_fixes
        signature = self._execution_signature(call.name, validated_params)
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
                1 for execution in recent_executions if self._execution_failed(execution)
            )
            repetition_metadata = {
                "repetition_level": repetition_level,
                "recent_execution_count": len(recent_executions),
                "failure_count": failure_count,
            }

            if repetition_level >= 3:
                if self._is_java_configuration_loop(call.name, validated_params, recent_executions):
                    execution = self._attempt_java_configuration_auto_fix(
                        call,
                        signature,
                        validated_params,
                        repetition_metadata,
                    )
                    execution.duration_ms = self._duration_since(started_at)
                    self._emit(
                        "tool_result",
                        call,
                        message=execution.observation_text,
                        level=self._result_event_level(execution.result),
                        metadata={
                            "status": execution.status,
                            "duration_ms": execution.duration_ms,
                            "error_code": execution.result.error_code,
                            "executed_params": execution.executed_params,
                            "recovery_applied": execution.recovery_applied,
                            "execution_signature": signature,
                            "recovery_strategy": execution.recovery_strategy,
                            "recovery": execution.metadata.get("recovery"),
                            **self._result_lifecycle_metadata(execution.result),
                            **repetition_metadata,
                        },
                    )
                    return execution

                result = ToolResult.completed_failure(
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
                    metadata={"failure_category": "execution"},
                )
                self._track_tool_execution(signature, result)
                duration_ms = self._duration_since(started_at)
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
                    duration_ms=duration_ms,
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
                        **metadata,
                        **self._tool_error_metadata(result, recovery_attempted=False),
                    },
                )
                return execution

        escaped_exception_result: Optional[ToolResult] = None
        try:
            result = self.tools[call.name].safe_execute(**validated_params)
        except Exception as exc:
            result = ToolResult.terminal_failure(
                invocation_status=InvocationStatus.CRASHED,
                output="",
                error=f"Tool {call.name} execution failed unexpectedly: {exc}",
                error_code="TOOL_EXECUTION_EXCEPTION",
                metadata={
                    "exception_type": type(exc).__name__,
                    "failure_category": "system",
                },
            )
            escaped_exception_result = result

        executed_params = validated_params
        recovery_applied = False
        recovery_strategy: Optional[str] = None
        recovery_metadata: Optional[Dict[str, Any]] = None
        status = (
            "exception"
            if escaped_exception_result is not None
            else self._result_execution_status(result)
        )

        if result.is_terminal and result.operation_outcome is OperationOutcome.FAILED:
            decision = self.recovery_handler.recover(call.name, validated_params, result)
            recovery_metadata = dict(decision.metadata)
            recovery_metadata.setdefault("attempted", decision.should_recover)
            recovery_metadata.setdefault("success", False)
            recovery_metadata.setdefault("message", decision.guidance)
            recovery_metadata.setdefault("strategy", decision.strategy)

            if decision.should_recover:
                replacement_success = (
                    decision.replacement_result.succeeded
                    if decision.replacement_result is not None
                    else False
                )
                recovery_params = decision.replacement_params or validated_params
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
                        "replacement_result_succeeded": (
                            decision.replacement_result.succeeded
                            if decision.replacement_result is not None
                            else None
                        ),
                        "recovery_params": recovery_params,
                        "parameter_diff": self._parameter_diff(validated_params, recovery_params),
                    },
                )

                recovery_strategy = decision.strategy
                if decision.replacement_result is not None:
                    result = decision.replacement_result
                    executed_params = recovery_params
                    if recovery_metadata.get("guidance_only"):
                        status = "recovery_attempted"
                    else:
                        status = (
                            "recovered"
                            if result.operation_outcome is OperationOutcome.SUCCESS
                            else "recovery_failed"
                        )
                    recovery_applied = True
                    recovery_metadata["success"] = result.succeeded
                else:
                    status = "recovery_attempted"

        self._track_tool_execution(signature, result)
        if result.succeeded:
            self._update_successful_states(call.name, executed_params, result)

        if escaped_exception_result is None and repetition_level in {1, 2}:
            warning = self._build_repetition_warning(
                call.name,
                validated_params,
                repetition_level,
                repetition_metadata["recent_execution_count"],
                repetition_metadata["failure_count"],
            )
            result.output = f"{warning}\n\n{result.output}" if result.output else warning

        duration_ms = self._duration_since(started_at)
        observation_text = format_tool_result(call.name, result)
        execution_metadata = {"execution_signature": signature, **repetition_metadata}
        if escaped_exception_result is None and repetition_level == 2:
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
            duration_ms=duration_ms,
            observation_text=observation_text,
            recovery_applied=recovery_applied,
            recovery_strategy=recovery_strategy,
            attempted_execution=True,
            parameter_fixes=call.parameter_fixes,
            metadata=execution_metadata,
        )
        if result.succeeded and call.name == "manage_context":
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
            "duration_ms": duration_ms,
            "error_code": result.error_code,
            "executed_params": executed_params,
            "recovery_applied": recovery_applied,
            "execution_signature": signature,
            **self._result_lifecycle_metadata(result),
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

        if escaped_exception_result is not None:
            self._emit(
                "tool_error",
                call,
                message=format_tool_result(call.name, escaped_exception_result),
                level="error",
                metadata={
                    "status": execution.status,
                    "execution_signature": signature,
                    **self._tool_error_metadata(
                        escaped_exception_result,
                        recovery_attempted=bool(
                            recovery_metadata and recovery_metadata.get("attempted")
                        ),
                    ),
                },
            )
        else:
            self._emit(
                "tool_result",
                call,
                message=observation_text,
                level=self._result_event_level(result),
                metadata=event_metadata,
            )
        return execution

    def _execution_signature(self, tool_name: str, params: Dict[str, Any]) -> str:
        return f"{tool_name}:{str(sorted(params.items()))}"

    def _duration_since(self, started_at: float) -> float:
        return (time.perf_counter() - started_at) * 1000

    def _tool_error_metadata(
        self,
        result: ToolResult,
        *,
        recovery_attempted: bool,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            **self._result_lifecycle_metadata(result),
            "error_code": result.error_code,
            "category": category or result.metadata.get("failure_category") or "execution",
            "suggestions": list(result.suggestions),
            "original_error": result.error,
            "recovery_attempted": recovery_attempted,
        }

    def _unknown_tool_suggestions(self, requested_tool: str) -> list[str]:
        suggestions = [
            "Use one of the available tool names",
            "For shell commands, use the bash tool with the command parameter",
            "For file operations, use the file_io tool",
        ]
        close_matches = get_close_matches(requested_tool, list(self.tools), n=3, cutoff=0.6)
        if close_matches:
            suggestions.insert(0, f"Did you mean: {', '.join(close_matches)}")
        return suggestions

    def _parameter_diff(
        self, before: Dict[str, Any], after: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        diff = {}
        for key in sorted(set(before) | set(after)):
            before_value = before.get(key)
            after_value = after.get(key)
            if before_value != after_value:
                diff[key] = {"before": before_value, "after": after_value}
        return diff

    @staticmethod
    def _is_dispatch_poll_signature(signature: str) -> bool:
        """Whether a tool signature polls a detached dispatch job's log/exit file.

        Covers both prescribed polling forms: bash tail/cat on the
        /tmp/sag_jobs/<id> files and search(target='job:<id>').
        """
        return "/tmp/sag_jobs/" in signature or "'target', 'job:" in signature

    def _recent_signature(self, execution: dict[str, Any] | ToolExecutionRecord) -> str:
        if isinstance(execution, ToolExecutionRecord):
            return execution.signature
        return str(execution.get("signature", ""))

    def _execution_failed(self, execution: dict[str, Any] | ToolExecutionRecord) -> bool:
        if isinstance(execution, ToolExecutionRecord):
            return execution.operation_outcome is OperationOutcome.FAILED
        return execution.get("operation_outcome") in {
            OperationOutcome.FAILED,
            OperationOutcome.FAILED.value,
        }

    def _execution_succeeded(self, execution: dict[str, Any] | ToolExecutionRecord) -> bool:
        if isinstance(execution, ToolExecutionRecord):
            return (
                execution.invocation_status is InvocationStatus.COMPLETED
                and execution.operation_outcome is OperationOutcome.SUCCESS
            )
        return execution.get("invocation_status") in {
            InvocationStatus.COMPLETED,
            InvocationStatus.COMPLETED.value,
        } and execution.get("operation_outcome") in {
            OperationOutcome.SUCCESS,
            OperationOutcome.SUCCESS.value,
        }

    @staticmethod
    def _result_execution_status(result: ToolResult) -> ToolExecutionStatus:
        if result.invocation_status is InvocationStatus.PENDING:
            return "pending"
        return {
            OperationOutcome.SUCCESS: "success",
            OperationOutcome.PARTIAL: "partial",
            OperationOutcome.UNKNOWN: "unknown",
            OperationOutcome.SKIPPED: "skipped",
            OperationOutcome.FAILED: "failure",
        }[result.operation_outcome]

    @staticmethod
    def _result_event_level(result: ToolResult) -> ToolLifecycleLevel:
        if result.invocation_status is InvocationStatus.PENDING:
            return "info"
        return {
            OperationOutcome.SUCCESS: "success",
            OperationOutcome.PARTIAL: "warning",
            OperationOutcome.UNKNOWN: "info",
            OperationOutcome.SKIPPED: "info",
            OperationOutcome.FAILED: "error",
        }[result.operation_outcome]

    @staticmethod
    def _result_lifecycle_metadata(result: ToolResult) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "invocation_status": result.invocation_status.value,
            "operation_outcome": result.operation_outcome.value,
            "evidence_status": result.evidence_status.value,
        }
        for field_name in ("failure_signature", "error_tail_preview", "output_ref"):
            value = getattr(result, field_name)
            if value:
                metadata[field_name] = value
        return metadata

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

        # Polling a detached build's log/exit file is PRESCRIBED behavior (the
        # dispatch-and-poll handoff tells the agent to repeat these commands),
        # not a loop — never warn/block it, and don't let it inflate the
        # per-tool flood count for other commands.
        if self._is_dispatch_poll_signature(tool_signature):
            return 0

        tool_count = len(
            [
                execution
                for execution in self._recent_executions_for_tool(tool_name)
                if not self._is_dispatch_poll_signature(self._recent_signature(execution))
            ]
        )

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
                    "Use project(action='provision', java_version=...) instead of manual update-alternatives"
                )
            if any("java" in str(execution) for execution in recent_executions):
                suggestions.append(
                    "Try: bash(command='java -version') to check the current Java version"
                )
            suggestions.append("Use file_io tool to examine files before executing commands")

        elif tool_name in ("maven", "build"):
            suggestions.append("Try: bash(command='mvn --version') to verify Maven installation")
            suggestions.append("Check pom.xml exists: file_io(action='read', file_path='pom.xml')")
            suggestions.append("Use bash tool for manual investigation: bash(command='ls -la')")

        elif tool_name == "project" and params.get("action") == "provision":
            suggestions.append(
                "Java might already be installed - verify with: bash(command='java -version')"
            )
            suggestions.append("Check available Java versions: bash(command='ls /usr/lib/jvm/')")

        return "\n• ".join(suggestions) if suggestions else "Try a different tool or approach"

    def _is_java_configuration_loop(
        self,
        tool_name: str,
        validated_params: Dict[str, Any],
        recent_executions: list[dict[str, Any] | ToolExecutionRecord],
    ) -> bool:
        def _is_java_install_call(name: str, call_params: Dict[str, Any]) -> bool:
            call_action = str(call_params.get("action", "")).lower()
            if name == "system" and call_action in {"install_java", "verify_java"}:
                return True
            # Stage-1 surface: JDK installs go through project(action='provision').
            return (
                name == "project"
                and call_action == "provision"
                and bool(call_params.get("java_version"))
            )

        if _is_java_install_call(tool_name, validated_params):
            return True

        command_contexts = [str(validated_params.get("command", ""))]
        for execution in recent_executions:
            signature = self._recent_signature(execution)
            recent_tool_name, _, _ = signature.partition(":")
            recent_params = self._params_from_signature(signature)
            if _is_java_install_call(recent_tool_name, recent_params):
                return True
            command_contexts.append(str(recent_params.get("command", "")))

        return any(self._has_java_alternatives_marker(context) for context in command_contexts)

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
            result = ToolResult.completed_failure(
                output="Could not auto-fix Java configuration. Skipping to next task.",
                error=f"Auto-fix failed: {exc}",
                error_code="JAVA_AUTO_FIX_EXCEPTION",
                metadata={"exception_type": type(exc).__name__},
            )

        self._track_tool_execution(signature, result)
        status: ToolExecutionStatus = "recovered" if result.succeeded else "recovery_failed"
        observation_text = format_tool_result(call.name, result)
        recovery_params = self._java_auto_fix_recovery_params(result)
        recovery_metadata = {
            "attempted": True,
            "success": result.succeeded,
            "message": observation_text,
            "strategy": recovery_strategy,
            "replacement_result_succeeded": result.succeeded,
            "recovery_params": recovery_params,
            "parameter_diff": self._parameter_diff(validated_params, recovery_params),
        }
        metadata = {
            "execution_signature": signature,
            "recovery_strategy": recovery_strategy,
            "recovery": recovery_metadata,
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
            level="success" if result.succeeded else "error",
            metadata={
                "recovery_strategy": recovery_strategy,
                "attempted": True,
                "success": result.succeeded,
                "guidance": observation_text,
                "replacement_result_succeeded": result.succeeded,
                "recovery_params": recovery_params,
                "parameter_diff": recovery_metadata["parameter_diff"],
                **metadata,
            },
        )
        return execution

    def _java_auto_fix_recovery_params(self, result: ToolResult) -> Dict[str, Any]:
        java_version = (result.metadata or {}).get("java_version")
        if java_version:
            return {"action": "install_java", "java_version": java_version}
        return {"action": "verify_java"}

    def _system_delegate(self):
        """SystemTool by retired direct name or via the project facade."""
        tool = self.tools.get("system")
        if tool is None:
            project = self.tools.get("project")
            tool = getattr(project, "system_tool", None) if project is not None else None
        return tool

    def _auto_fix_java_configuration(self) -> ToolResult:
        """Automatically fix Java configuration issues."""
        self.logger.info("Attempting automatic Java configuration fix")

        system_tool = self._system_delegate()
        if system_tool is not None:
            system_tool.safe_execute(action="verify_java")

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
                install_result = system_tool.safe_execute(action="install_java", java_version="17")

                if install_result.succeeded:
                    return ToolResult.completed_success(
                        output=(
                            "Auto-fixed Java configuration using enhanced system tool\n"
                            + install_result.output
                        ),
                        metadata={"auto_fixed": True, "java_version": "17"},
                    )

        return ToolResult.completed_failure(
            output="Could not auto-fix Java configuration. Skipping to next task.",
            error="Auto-fix failed",
            error_code="AUTO_FIX_FAILED",
            suggestions=["Manual intervention may be required", "Check Java installation logs"],
        )

    def _generate_unknown_tool_feedback(self, requested_tool: str) -> str:
        """Generate comprehensive feedback for unknown tool requests."""
        # Common tool name mappings
        tool_mappings = {
            "git": "project",
            "git_clone": "project",
            "clone": "project",
            "setup": "project",
            "mvn": "build",
            "gradle_build": "build",
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
            "find": "search",
            "grep": "search",
            "test": "build",
            "compile": "build",
            "install": "project or bash",
            "package": "build",
            "analyze": "project",
            "report": "report",
            "context": "manage_context",
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
            if suggested == "project":
                feedback_parts.append(
                    "\n📝 Usage: Use 'project' with action='clone' and repo_url to clone repositories"
                )
            elif suggested == "build":
                feedback_parts.append(
                    "\n📝 Usage: Use 'build' with action='deps' | 'compile' | 'test' | 'package'"
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
                "build": "Run builds and tests (action= deps|compile|test|package; auto-selects maven/gradle)",
                "project": "Clone, provision, analyze, and register env (action= clone|provision|analyze|env)",
                "search": "Search stored outputs, container files, job logs, or the web",
                "manage_context": "Manage task context and branching",
                "report": "Generate project reports",
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
        feedback_parts.append(
            "• For builds and tests, use the 'build' tool (auto-selects maven/gradle)"
        )
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

    def _track_tool_execution(self, signature: str, result: ToolResult) -> None:
        try:
            self.track_tool_execution(signature, result)
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
