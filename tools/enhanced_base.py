"""Enhanced base classes for agent tools with better error handling."""

import inspect
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
    """Enhanced result of a tool execution."""

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


class EnhancedBaseTool(ABC):
    """Enhanced base class for all agent tools with better error handling."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self._parameter_schema: Dict[str, Any] = {}
        self._generate_parameter_schema()

    def _generate_parameter_schema(self):
        """Auto-generate parameter schema from execute method signature."""
        sig = inspect.signature(self.execute)
        schema = {"type": "object", "properties": {}, "required": []}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
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
            logger.debug(f"Tool {self.name} succeeded: {result.output[:200]}...")
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

    def get_parameter_schema(self) -> Dict[str, Any]:
        """Get the OpenAI function calling parameter schema."""
        return self._parameter_schema

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return self._parameter_schema
