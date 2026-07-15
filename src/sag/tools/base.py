"""Base classes for agent tools."""

import hashlib
import inspect
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union, get_args, get_origin

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from sag.evidence import (
    EvidenceAssessment,
    EvidenceFinding,
    EvidenceStatus,
    InvocationStatus,
    OperationOutcome,
    TestStats,
)


class ToolError(Exception):
    """Enhanced tool error with actionable guidance and categorization."""

    def __init__(
        self,
        message: str,
        category: str = "execution",  # "validation" | "execution" | "system"
        suggestions: Optional[List[str]] = None,
        documentation_links: Optional[List[str]] = None,
        error_code: Optional[str] = None,
        raw_output: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.suggestions = suggestions or []
        self.documentation_links = documentation_links or []
        self.error_code = error_code
        self.raw_output = raw_output
        self.details = details or {}
        self.retryable = retryable

    def to_result(self, duration: Optional[float] = None) -> "ToolResult":
        """Convert ToolError to ToolResult with preserved metadata."""
        metadata = {
            "failure_category": self.category,
            "retryable": self.retryable,
        }

        # Add optional metadata
        if duration is not None:
            metadata["duration_ms"] = duration * 1000  # Convert to milliseconds

        if self.details:
            metadata["error_details"] = self.details

        return ToolResult.completed_failure(
            output="",
            error=self.message,
            error_code=self.error_code,
            suggestions=self.suggestions,
            documentation_links=self.documentation_links,
            raw_output=self.raw_output,
            metadata=metadata,
        )


LEGAL_RESULT_STATES = {
    InvocationStatus.PENDING: {OperationOutcome.UNKNOWN},
    InvocationStatus.COMPLETED: {
        OperationOutcome.UNKNOWN,
        OperationOutcome.SUCCESS,
        OperationOutcome.PARTIAL,
        OperationOutcome.FAILED,
        OperationOutcome.SKIPPED,
    },
    InvocationStatus.TIMEOUT: {
        OperationOutcome.UNKNOWN,
        OperationOutcome.PARTIAL,
        OperationOutcome.FAILED,
    },
    InvocationStatus.CRASHED: {OperationOutcome.UNKNOWN, OperationOutcome.FAILED},
    InvocationStatus.CANCELLED: {OperationOutcome.UNKNOWN, OperationOutcome.SKIPPED},
}

READ_ONLY_RESULT_FIELDS = {
    "invocation_status",
    "operation_outcome",
    "evidence_status",
    "poll_ref",
    "failure_signature",
    "error_tail_preview",
    "output_ref",
    "evidence_assessment",
}


class ToolResult(BaseModel):
    """Canonical, orthogonal tool execution result."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    invocation_status: InvocationStatus
    operation_outcome: OperationOutcome
    evidence_status: EvidenceStatus
    poll_ref: Optional[str] = None
    failure_signature: Optional[str] = None
    error_tail_preview: Optional[str] = None
    output_ref: Optional[str] = None
    output: str
    evidence_assessment: EvidenceAssessment = EvidenceAssessment.UNKNOWN
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)
    documentation_links: List[str] = Field(default_factory=list)
    raw_output: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    validator_findings: List[EvidenceFinding] = Field(default_factory=list)
    test_stats: Optional[TestStats] = None

    facts: Dict[str, Any] = Field(default_factory=dict)
    refs: List[str] = Field(default_factory=list)

    @classmethod
    def completed(
        cls,
        *,
        output: str,
        operation_outcome: OperationOutcome | str,
        evidence_status: EvidenceStatus | str = EvidenceStatus.VERIFIED,
        **payload: Any,
    ) -> "ToolResult":
        """Build a terminal result and fill stable provenance for failures."""
        outcome = OperationOutcome(operation_outcome)
        payload.setdefault(
            "evidence_assessment",
            {
                OperationOutcome.SUCCESS: EvidenceAssessment.SUCCESS,
                OperationOutcome.PARTIAL: EvidenceAssessment.PARTIAL,
                OperationOutcome.FAILED: EvidenceAssessment.BLOCKED,
            }.get(outcome, EvidenceAssessment.UNKNOWN),
        )
        if outcome is OperationOutcome.FAILED:
            source = str(payload.get("raw_output") or output or payload.get("error") or "")
            digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:16]
            error_code = str(payload.get("error_code") or "TOOL_OPERATION_FAILED")
            payload.setdefault("error_code", error_code)
            payload.setdefault("failure_signature", f"{error_code}:{digest}")
            payload.setdefault("error_tail_preview", source[-400:] or error_code)
            refs = payload.get("refs") or []
            payload.setdefault("output_ref", refs[0] if refs else f"tool-result:{digest}")
        return cls(
            invocation_status=InvocationStatus.COMPLETED,
            operation_outcome=outcome,
            evidence_status=evidence_status,
            output=output,
            **payload,
        )

    @classmethod
    def completed_success(cls, *, output: str, **payload: Any) -> "ToolResult":
        return cls.completed(
            output=output,
            operation_outcome=OperationOutcome.SUCCESS,
            **payload,
        )

    @classmethod
    def completed_failure(cls, *, output: str, **payload: Any) -> "ToolResult":
        return cls.completed(
            output=output,
            operation_outcome=OperationOutcome.FAILED,
            **payload,
        )

    @model_validator(mode="after")
    def _validate_result_state(self) -> "ToolResult":
        allowed_outcomes = LEGAL_RESULT_STATES[self.invocation_status]
        if self.operation_outcome not in allowed_outcomes:
            raise ValueError(
                f"{self.invocation_status.value} results require operation_outcome to be one of "
                f"{sorted(outcome.value for outcome in allowed_outcomes)}"
            )
        if self.invocation_status is InvocationStatus.PENDING:
            if not self.poll_ref or not self.poll_ref.strip():
                raise ValueError("pending results require a stable poll_ref")
            if self.evidence_status is not EvidenceStatus.UNKNOWN:
                raise ValueError("pending results require evidence_status='unknown'")

        if self.operation_outcome is OperationOutcome.FAILED:
            for field_name in ("failure_signature", "error_tail_preview", "output_ref"):
                value = getattr(self, field_name)
                if not value or not value.strip():
                    raise ValueError(f"canonical failed results require nonblank {field_name}")
            if len(self.error_tail_preview) > 400:
                raise ValueError("error_tail_preview must be at most 400 characters")
            if not self.error_code or not self.error_code.strip():
                raise ValueError("canonical failed results require nonblank error_code")

        return self

    @property
    def is_terminal(self) -> bool:
        return self.invocation_status is not InvocationStatus.PENDING

    @property
    def succeeded(self) -> bool:
        return (
            self.invocation_status is InvocationStatus.COMPLETED
            and self.operation_outcome is OperationOutcome.SUCCESS
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in READ_ONLY_RESULT_FIELDS:
            raise TypeError(f"ToolResult.{name} is read-only after construction")
        super().__setattr__(name, value)

    def __str__(self) -> str:
        if self.succeeded:
            return self.output
        else:
            result = f"Error: {self.error}"
            if self.error_code:
                result += f" (Code: {self.error_code})"
            if self.suggestions:
                result += f"\n\nSuggestions:\n" + "\n".join(f"• {s}" for s in self.suggestions)
            if self.documentation_links:
                result += f"\n\nDocumentation:\n" + "\n".join(
                    f"• {link}" for link in self.documentation_links
                )
            return result


class BaseTool(ABC):
    """Base class for all agent tools with enhanced error handling."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._parameter_schema: Dict[str, Any] = {}

        # Output truncation settings - increased for build tools
        self.max_output_length = 10000  # Maximum total output length (increased from 3000)
        self.head_length = 4000  # Length of beginning portion (increased from 1200)
        self.tail_length = 3000  # Length of ending portion (increased from 800)

        self._generate_parameter_schema()

    def _generate_parameter_schema(self):
        """Auto-generate parameter schema from execute method signature."""
        sig = inspect.signature(self.execute)
        schema = {"type": "object", "properties": {}, "required": []}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            # Skip **kwargs parameters as they are handled specially
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                continue

            # Skip *args parameters as they are not supported in JSON schema
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                continue

            param_info = {
                "type": "string",  # Default to string
                "description": f"Parameter {param_name}",
            }

            # Check if parameter has a default value
            if param.default == inspect.Parameter.empty:
                schema["required"].append(param_name)
            else:
                param_info["default"] = param.default

            # Try to infer type from annotation
            if param.annotation != inspect.Parameter.empty:
                annotation = param.annotation
                origin = get_origin(annotation)
                if origin is Union:
                    non_none_args = [arg for arg in get_args(annotation) if arg is not type(None)]
                    if len(non_none_args) == 1:
                        annotation = non_none_args[0]
                        origin = get_origin(annotation)

                if annotation == int:
                    param_info["type"] = "integer"
                elif annotation == float:
                    param_info["type"] = "number"
                elif annotation == bool:
                    param_info["type"] = "boolean"
                elif annotation == list or origin is list:
                    param_info["type"] = "array"

            schema["properties"][param_name] = param_info

        self._parameter_schema = schema

    def _truncate_output(self, output: str, tool_name: str = None) -> str:
        """
        Intelligently truncate long output to preserve context window.

        Args:
            output: The raw output to truncate
            tool_name: Name of the tool (used for custom extraction)

        Returns:
            Truncated output with head, tail, and guidance
        """
        if not output or len(output) <= self.max_output_length:
            return output

        # Try tool-specific extraction first
        extracted = self._extract_key_info(output, tool_name or self.name)
        if extracted and extracted != output:
            logger.info(
                f"Applied {tool_name or self.name}-specific extraction, reduced from {len(output)} to {len(extracted)} chars"
            )
            # If extraction is still too long, apply general truncation
            if len(extracted) <= self.max_output_length:
                return extracted
            output = extracted

        # General truncation: head + tail with guidance
        head = output[: self.head_length]
        tail = output[-self.tail_length :]

        truncation_info = (
            f"\n\n... [OUTPUT TRUNCATED: {len(output)} chars total, showing first {self.head_length} "
            f"and last {self.tail_length} chars] ...\n"
            f"💡 TIP: If you need specific information from the full output, use 'bash' tool with 'grep' "
            f"to search for keywords, or 'file_io' to save and search through the complete output.\n\n"
        )

        return head + truncation_info + tail

    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """
        Extract key information from tool output.
        Override in subclasses for tool-specific extraction.

        Args:
            output: Raw tool output
            tool_name: Name of the tool

        Returns:
            Extracted key information or original output
        """
        # Default implementation - can be overridden by specific tools
        return output

    def _extract_maven_key_info(self, output: str) -> str:
        """Extract key information from Maven output."""
        lines = output.split("\n")
        key_lines = []

        # Capture key indicators
        build_status = ""
        test_summary = ""
        compilation_info = ""
        error_summary = []

        for line in lines:
            line_lower = line.lower()

            # Build status
            if "build success" in line_lower:
                build_status = "✅ BUILD SUCCESS"
            elif "build failure" in line_lower:
                build_status = "❌ BUILD FAILURE"

            # Test results
            elif "tests run:" in line_lower:
                test_summary = f"📊 {line.strip()}"

            # Compilation info
            elif "compilation failure" in line_lower:
                compilation_info = "⚠️ Compilation failures detected"
            elif "nothing to compile" in line_lower:
                compilation_info = "✅ All classes up to date"
            elif "building jar:" in line_lower:
                compilation_info = "📦 JAR artifact created"

            # Error patterns - collect specific errors (increased limit)
            elif any(
                error_pattern in line_lower
                for error_pattern in [
                    "error:",
                    "[error]",
                    "exception:",
                    "failed to",
                    "cannot find",
                    "package does not exist",
                    "compilation failure",
                    "cannot resolve",
                    "symbol not found",
                    "method does not exist",
                ]
            ):
                if len(error_summary) < 15:  # Increased limit to capture more errors
                    error_summary.append(f"🚨 {line.strip()}")

        # Build the summary
        summary_parts = []

        if build_status:
            summary_parts.append(build_status)

        if test_summary:
            summary_parts.append(test_summary)

        if compilation_info:
            summary_parts.append(compilation_info)

        if error_summary:
            summary_parts.append("Key Errors:")
            summary_parts.extend(error_summary[:10])  # Show more errors for better debugging
            if len(error_summary) > 10:
                summary_parts.append(f"... and {len(error_summary) - 10} more errors")

        # If we found key info, return it; otherwise return truncated original
        if summary_parts:
            key_info = "\n".join(summary_parts)

            # Add a sample of the raw output for context
            if len(output) > 1000:
                # Add first and last few lines for context
                first_lines = "\n".join(lines[:10])
                last_lines = "\n".join(lines[-10:])

                full_summary = (
                    f"Maven Build Summary:\n{key_info}\n\n"
                    f"Build Output (first 10 lines):\n{first_lines}\n\n"
                    f"... [full output truncated, {len(lines)} total lines] ...\n\n"
                    f"Build Output (last 10 lines):\n{last_lines}\n\n"
                    f"💡 Use 'bash' with 'grep' to search for specific errors or patterns in the full output."
                )
                return full_summary
            else:
                return f"Maven Build Summary:\n{key_info}\n\nFull Output:\n{output}"

        return output

    def _extract_bash_key_info(self, output: str) -> str:
        """Extract key information from bash command output."""
        if not output or len(output) <= self.max_output_length:
            return output

        lines = output.split("\n")

        # For error cases, prioritize error messages
        error_lines = []
        warning_lines = []
        info_lines = []

        for line in lines:
            line_lower = line.lower()
            if any(
                error_word in line_lower
                for error_word in [
                    "error:",
                    "failed:",
                    "cannot",
                    "no such",
                    "permission denied",
                    "not found",
                ]
            ):
                error_lines.append(line)
            elif any(warning_word in line_lower for warning_word in ["warning:", "warn:"]):
                warning_lines.append(line)
            elif line.strip():  # Non-empty lines
                info_lines.append(line)

        # Build summary
        summary_parts = []

        # Add first few lines for context
        summary_parts.append("Command output (first 15 lines):")
        summary_parts.extend(lines[:15])

        if error_lines:
            summary_parts.append(f"\n🚨 Errors found ({len(error_lines)} total):")
            summary_parts.extend(error_lines[:5])  # Show first 5 errors
            if len(error_lines) > 5:
                summary_parts.append(f"... and {len(error_lines) - 5} more errors")

        if warning_lines:
            summary_parts.append(f"\n⚠️ Warnings found ({len(warning_lines)} total):")
            summary_parts.extend(warning_lines[:3])  # Show first 3 warnings

        # Add last few lines
        if len(lines) > 20:
            summary_parts.append(f"\n... [middle content truncated, {len(lines)} total lines] ...")
            summary_parts.append("\nCommand output (last 10 lines):")
            summary_parts.extend(lines[-10:])

        summary_parts.append(
            f"\n💡 Full output has {len(lines)} lines. Use 'grep' to search for specific patterns."
        )

        return "\n".join(summary_parts)

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def _validate_parameters(self, kwargs: Dict[str, Any]) -> None:
        """Validate parameters and raise ToolError if invalid."""
        required_params = self._parameter_schema.get("required", [])
        provided_params = set(kwargs.keys())

        # Check for missing required parameters
        missing_params = [p for p in required_params if p not in provided_params]
        if missing_params:
            raise ToolError(
                message=f"Missing required parameters: {', '.join(missing_params)}",
                category="validation",
                error_code="MISSING_PARAMETERS",
                suggestions=[
                    f"Provide the missing parameters: {', '.join(missing_params)}",
                    f"Use the parameter schema to understand required parameters",
                    f"Example usage: {self.name}({', '.join(f'{p}=<value>' for p in required_params)})",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"missing_parameters": missing_params},
                retryable=True,
            )

        # Check for unexpected parameters — unless the schema explicitly allows
        # pass-through parameters (facades forward **kwargs to delegates whose
        # full vocabularies are wider than the documented surface).
        if self._parameter_schema.get("additionalProperties"):
            return

        expected_params = set(self._parameter_schema.get("properties", {}).keys())
        unexpected_params = provided_params - expected_params
        if unexpected_params:
            raise ToolError(
                message=f"Unexpected parameters: {', '.join(unexpected_params)}",
                category="validation",
                error_code="UNEXPECTED_PARAMETERS",
                suggestions=[
                    f"Remove unexpected parameters: {', '.join(unexpected_params)}",
                    f"Valid parameters are: {', '.join(expected_params)}",
                    f"Check the parameter schema for correct parameter names",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"unexpected_parameters": list(unexpected_params)},
                retryable=True,
            )

    def safe_execute(self, **kwargs) -> ToolResult:
        """Execute the tool with enhanced error handling and validation."""
        import time

        start_time = time.time()

        try:
            logger.info(f"Executing tool: {self.name}")

            # Validate parameters - will raise ToolError if invalid
            self._validate_parameters(kwargs)

            result = self.execute(**kwargs)

            # Apply output truncation if needed
            if result.succeeded and result.output:
                original_length = len(result.output)
                result.output = self._truncate_output(result.output, self.name)

                # Update metadata with truncation info
                if len(result.output) < original_length:
                    result.metadata["output_truncated"] = True
                    result.metadata["original_length"] = original_length
                    result.metadata["truncated_length"] = len(result.output)

            # Add execution duration to successful results
            duration = time.time() - start_time
            result.metadata["duration_ms"] = duration * 1000

            self._log_execution(kwargs, result)
            return result

        except ToolError as e:
            # Handle tool errors using to_result() method
            duration = time.time() - start_time
            result = e.to_result(duration=duration)

            # Log to centralized error logger
            try:
                from sag.agent.error_logger import ErrorLogger

                error_logger = ErrorLogger.get_instance()
                error_logger.log_tool_error(
                    tool_name=self.name,
                    error_message=e.message,
                    category=e.category,
                    error_code=e.error_code,
                    suggestions=e.suggestions,
                    retryable=e.retryable,
                    details=e.details,
                    context={"parameters": kwargs},
                )
            except Exception as log_error:
                logger.warning(f"Failed to log error to centralized logger: {log_error}")

            self._log_execution(kwargs, result)
            return result

        except Exception as e:
            # Handle unexpected errors with proper categorization
            duration = time.time() - start_time
            error_msg = f"Tool {self.name} crashed: {str(e)}"
            logger.error(error_msg, exc_info=True)

            # Create a system-level ToolError and convert it
            system_error = ToolError(
                message=error_msg,
                category="system",
                error_code="UNEXPECTED_ERROR",
                suggestions=[
                    "Check the tool parameters for correctness",
                    "Review the tool documentation",
                    "Try a simpler version of the command first",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"exception_type": type(e).__name__, "exception_str": str(e)},
                retryable=False,
            )

            result = system_error.to_result(duration=duration)

            # Log system error to centralized logger
            try:
                from sag.agent.error_logger import ErrorLogger

                error_logger = ErrorLogger.get_instance()
                error_logger.log_tool_error(
                    tool_name=self.name,
                    error_message=system_error.message,
                    category="system",
                    error_code=system_error.error_code,
                    suggestions=system_error.suggestions,
                    retryable=system_error.retryable,
                    details=system_error.details,
                    context={"parameters": kwargs, "exception_type": type(e).__name__},
                )
            except Exception as log_error:
                logger.warning(f"Failed to log system error to centralized logger: {log_error}")

            self._log_execution(kwargs, result)
            return result

    def _log_execution(self, params: Dict[str, Any], result: ToolResult) -> None:
        """Log tool execution for debugging."""
        logger.debug(f"Tool {self.name} executed with params: {params}")
        if result.succeeded:
            output_info = f"{len(result.output)} chars"
            if result.metadata.get("output_truncated"):
                output_info += f" (truncated from {result.metadata.get('original_length', 0)})"
            logger.debug(f"Tool {self.name} succeeded: {output_info}")
        else:
            logger.warning(f"Tool {self.name} failed: {result.error}")
            if result.suggestions:
                logger.info(f"Suggestions for {self.name}: {result.suggestions}")

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool schema for the LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._parameter_schema,
            "usage_example": self.get_usage_example(),
        }

    def get_parameter_schema(self) -> Dict[str, Any]:
        """Return this tool's JSON parameter schema for function calling."""
        schema_method = self._get_parameters_schema
        if getattr(schema_method, "__func__", None) is not BaseTool._get_parameters_schema:
            return schema_method()
        return self._parameter_schema

    def get_usage_example(self) -> str:
        """Get a usage example for this tool."""
        required_params = self._parameter_schema.get("required", [])
        optional_params = [
            p
            for p in self._parameter_schema.get("properties", {}).keys()
            if p not in required_params
        ]

        example = f"{self.name}("
        param_examples = []

        for param in required_params:
            param_examples.append(f'{param}="<required_value>"')

        for param in optional_params[:2]:  # Show max 2 optional params
            param_examples.append(f'{param}="<optional_value>"')

        example += ", ".join(param_examples)
        example += ")"

        return example

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return self._parameter_schema
