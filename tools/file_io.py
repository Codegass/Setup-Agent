"""File I/O tool for the agent."""
from typing import Dict, Any, Optional

from loguru import logger

from docker_orch.orch import DockerOrchestrator
from .base import BaseTool, ToolResult, ToolError


class FileIOTool(BaseTool):
    """Tool for reading, writing, and listing files."""

    def __init__(self, orchestrator: DockerOrchestrator):
        super().__init__(
            "file_io",
            "A tool for reading, writing, and listing files in the workspace."
        )
        self.orchestrator = orchestrator

    def execute(
        self,
        action: str,
        path: str,
        content: Optional[str] = None,
        start_line: int = 0,
        end_line: Optional[int] = None
    ) -> ToolResult:
        """
        Execute a file operation.

        Args:
            action: The action to perform ('read', 'write', 'list').
            path: The path to the file or directory.
            content: The content to write to the file (for 'write' action).
            start_line: The starting line number for reading (for 'read' action).
            end_line: The ending line number for reading (for 'read' action).
        """
        # The base class now handles parameter validation automatically
        # via _validate_parameters() which checks the schema

        if action == "read":
            return self._read(path, start_line, end_line)
        elif action == "write":
            return self._write(path, content)
        elif action == "list":
            return self._list(path)
        else:
            raise ToolError(
                message=f"Invalid action '{action}'. Must be 'read', 'write', or 'list'.",
                category="validation",
                error_code="INVALID_ACTION",
                suggestions=[
                    "Use action='read' to read a file",
                    "Use action='write' to write content to a file",
                    "Use action='list' to list directory contents"
                ],
                details={"provided_action": action, "valid_actions": ["read", "write", "list"]},
                retryable=True
            )

    def _read(self, path: str, start_line: int, end_line: Optional[int]) -> ToolResult:
        """Read a file."""
        if not path:
            raise ToolError(
                message="Path is required for reading.",
                category="validation",
                error_code="MISSING_PATH",
                retryable=True
            )
            
        # Command to read the file content
        command = f"cat '{path}'"
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(
                message=f"Failed to read file: {result['output']}",
                category="execution",
                error_code="READ_FAILED",
                raw_output=result.get('output'),
                suggestions=[
                    "Check if the file exists",
                    "Verify you have read permissions",
                    "Ensure the path is correct"
                ],
                retryable=True
            )

        file_content = result["output"]
        lines = file_content.splitlines()
        
        # Handle line slicing
        if end_line is None:
            end_line = len(lines)
        
        selected_lines = lines[start_line:end_line]
        output = "\n".join(selected_lines)

        return ToolResult(
            success=True,
            output=output,
            metadata={
                "path": path,
                "total_lines": len(lines),
                "read_lines": len(selected_lines),
                "start_line": start_line,
                "end_line": end_line
            }
        )

    def _write(self, path: str, content: Optional[str]) -> ToolResult:
        """Write content to a file."""
        if content is None:
            raise ToolError(
                message="Content is required for writing.",
                category="validation",
                error_code="MISSING_CONTENT",
                suggestions=["Provide the 'content' parameter with the text to write"],
                retryable=True
            )

        import base64
        encoded_content = base64.b64encode(content.encode('utf-8')).decode('ascii')
        
        command = f"""python3 -c "
import base64
import os
encoded_data = '{encoded_content}'
decoded_data = base64.b64decode(encoded_data).decode('utf-8')
os.makedirs(os.path.dirname('{path}'), exist_ok=True)
with open('{path}', 'w') as f:
    f.write(decoded_data)
"
"""
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(
                message=f"Failed to write to file: {result['output']}",
                category="execution",
                error_code="WRITE_FAILED",
                raw_output=result.get('output'),
                suggestions=[
                    "Check if you have write permissions",
                    "Ensure the directory exists",
                    "Verify disk space is available"
                ],
                retryable=True
            )

        return ToolResult(success=True, output=f"Successfully wrote {len(content)} characters to {path}")

    def _list(self, path: str) -> ToolResult:
        """List files in a directory."""
        if not path:
            raise ToolError(
                message="Path is required for listing.",
                category="validation",
                error_code="MISSING_PATH",
                retryable=True
            )
            
        command = f"ls -la '{path}'"
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(
                message=f"Failed to list directory: {result['output']}",
                category="execution",
                error_code="LIST_FAILED",
                raw_output=result.get('output'),
                suggestions=[
                    "Check if the directory exists",
                    "Verify you have read permissions",
                    "Ensure the path is a directory, not a file"
                ],
                retryable=True
            )

        return ToolResult(success=True, output=result["output"])
