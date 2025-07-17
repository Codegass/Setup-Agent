"""Base classes for agent tools."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from loguru import logger
from pydantic import BaseModel


class ToolResult(BaseModel):
    """Result of a tool execution."""

    success: bool
    output: str
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: list = []
    documentation_links: list = []
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
                result += f"\n\nDocumentation:\n" + "\n".join(f"• {link}" for link in self.documentation_links)
            return result


class BaseTool(ABC):
    """Base class for all agent tools."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def _log_execution(self, params: Dict[str, Any], result: ToolResult) -> None:
        """Log tool execution for debugging."""
        logger.debug(f"Tool {self.name} executed with params: {params}")
        if result.success:
            logger.debug(f"Tool {self.name} succeeded: {result.output[:200]}...")
        else:
            logger.warning(f"Tool {self.name} failed: {result.error}")

    def safe_execute(self, **kwargs) -> ToolResult:
        """Execute the tool with error handling and logging."""
        try:
            logger.info(f"Executing tool: {self.name}")
            result = self.execute(**kwargs)
            self._log_execution(kwargs, result)
            return result
        except Exception as e:
            error_msg = f"Tool {self.name} crashed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return ToolResult(success=False, output="", error=error_msg)

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool schema for the LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._get_parameters_schema(),
        }

    @abstractmethod
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        pass
