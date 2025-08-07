"""Base classes for agent tools."""

import inspect
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

from loguru import logger
from pydantic import BaseModel


class ToolError(Exception):
    """Enhanced tool error with actionable guidance."""

    def __init__(
        self,
        message: str,
        suggestions: Optional[List[str]] = None,
        documentation_links: Optional[List[str]] = None,
        error_code: Optional[str] = None,
        raw_output: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.suggestions = suggestions or []
        self.documentation_links = documentation_links or []
        self.error_code = error_code
        self.raw_output = raw_output


class ToolResult(BaseModel):
    """Result of a tool execution."""

    success: bool
    output: str
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = []
    documentation_links: List[str] = []
    raw_output: Optional[str] = None
    metadata: Dict[str, Any] = {}

    def __str__(self) -> str:
        if self.success:
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
        self.head_length = 4000       # Length of beginning portion (increased from 1200)
        self.tail_length = 3000        # Length of ending portion (increased from 800)
        
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
                if param.annotation == int:
                    param_info["type"] = "integer"
                elif param.annotation == float:
                    param_info["type"] = "number"
                elif param.annotation == bool:
                    param_info["type"] = "boolean"
                elif param.annotation == list:
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
            logger.info(f"Applied {tool_name or self.name}-specific extraction, reduced from {len(output)} to {len(extracted)} chars")
            # If extraction is still too long, apply general truncation
            if len(extracted) <= self.max_output_length:
                return extracted
            output = extracted
        
        # General truncation: head + tail with guidance
        head = output[:self.head_length]
        tail = output[-self.tail_length:]
        
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
        lines = output.split('\n')
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
            elif any(error_pattern in line_lower for error_pattern in [
                "error:", "[error]", "exception:", "failed to", "cannot find", "package does not exist",
                "compilation failure", "cannot resolve", "symbol not found", "method does not exist"
            ]):
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
                first_lines = '\n'.join(lines[:10])
                last_lines = '\n'.join(lines[-10:])
                
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
        
        lines = output.split('\n')
        
        # For error cases, prioritize error messages
        error_lines = []
        warning_lines = []
        info_lines = []
        
        for line in lines:
            line_lower = line.lower()
            if any(error_word in line_lower for error_word in [
                'error:', 'failed:', 'cannot', 'no such', 'permission denied', 'not found'
            ]):
                error_lines.append(line)
            elif any(warning_word in line_lower for warning_word in ['warning:', 'warn:']):
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
        
        summary_parts.append(f"\n💡 Full output has {len(lines)} lines. Use 'grep' to search for specific patterns.")
        
        return '\n'.join(summary_parts)

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def _validate_parameters(self, kwargs: Dict[str, Any]) -> Optional[ToolResult]:
        """Validate parameters and return error result if invalid."""
        required_params = self._parameter_schema.get("required", [])
        provided_params = set(kwargs.keys())

        # Check for missing required parameters
        missing_params = [p for p in required_params if p not in provided_params]
        if missing_params:
            return ToolResult(
                success=False,
                output="",
                error=f"Missing required parameters: {', '.join(missing_params)}",
                error_code="MISSING_PARAMETERS",
                suggestions=[
                    f"Provide the missing parameters: {', '.join(missing_params)}",
                    f"Use the parameter schema to understand required parameters",
                    f"Example usage: {self.name}({', '.join(f'{p}=<value>' for p in required_params)})",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
            )

        # Check for unexpected parameters
        expected_params = set(self._parameter_schema.get("properties", {}).keys())
        unexpected_params = provided_params - expected_params
        if unexpected_params:
            return ToolResult(
                success=False,
                output="",
                error=f"Unexpected parameters: {', '.join(unexpected_params)}",
                error_code="UNEXPECTED_PARAMETERS",
                suggestions=[
                    f"Remove unexpected parameters: {', '.join(unexpected_params)}",
                    f"Valid parameters are: {', '.join(expected_params)}",
                    f"Check the parameter schema for correct parameter names",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
            )

        return None

    def safe_execute(self, **kwargs) -> ToolResult:
        """Execute the tool with enhanced error handling and validation."""
        try:
            logger.info(f"Executing tool: {self.name}")

            # Validate parameters first
            validation_error = self._validate_parameters(kwargs)
            if validation_error:
                self._log_execution(kwargs, validation_error)
                return validation_error

            result = self.execute(**kwargs)
            
            # Apply output truncation if needed
            if result.success and result.output:
                original_length = len(result.output)
                result.output = self._truncate_output(result.output, self.name)
                
                # Update metadata with truncation info
                if len(result.output) < original_length:
                    result.metadata["output_truncated"] = True
                    result.metadata["original_length"] = original_length
                    result.metadata["truncated_length"] = len(result.output)
            
            self._log_execution(kwargs, result)
            return result

        except ToolError as e:
            # Handle custom tool errors with enhanced information
            result = ToolResult(
                success=False,
                output="",
                error=e.message,
                error_code=e.error_code,
                suggestions=e.suggestions,
                documentation_links=e.documentation_links,
                raw_output=e.raw_output,
            )
            self._log_execution(kwargs, result)
            return result

        except Exception as e:
            # Handle unexpected errors
            error_msg = f"Tool {self.name} crashed: {str(e)}"
            logger.error(error_msg, exc_info=True)

            result = ToolResult(
                success=False,
                output="",
                error=error_msg,
                error_code="UNEXPECTED_ERROR",
                suggestions=[
                    "Check the tool parameters for correctness",
                    "Review the tool documentation",
                    "Try a simpler version of the command first",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
            )
            self._log_execution(kwargs, result)
            return result

    def _log_execution(self, params: Dict[str, Any], result: ToolResult) -> None:
        """Log tool execution for debugging."""
        logger.debug(f"Tool {self.name} executed with params: {params}")
        if result.success:
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
