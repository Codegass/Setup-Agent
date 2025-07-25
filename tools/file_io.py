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
        if action == "read":
            return self._read(path, start_line, end_line)
        elif action == "write":
            return self._write(path, content)
        elif action == "list":
            return self._list(path)
        else:
            raise ToolError(
                f"Invalid action '{action}'. Must be 'read', 'write', or 'list'.",
                error_code="INVALID_ACTION"
            )

    def _read(self, path: str, start_line: int, end_line: Optional[int]) -> ToolResult:
        """Read a file."""
        if not path:
            raise ToolError("Path is required for reading.", error_code="MISSING_PATH")
            
        # Command to read the file content
        command = f"cat '{path}'"
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(f"Failed to read file: {result['output']}", error_code="READ_FAILED")

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
            raise ToolError("Content is required for writing.", error_code="MISSING_CONTENT")

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
            raise ToolError(f"Failed to write to file: {result['output']}", error_code="WRITE_FAILED")

        return ToolResult(success=True, output=f"Successfully wrote {len(content)} characters to {path}")

    def _list(self, path: str) -> ToolResult:
        """List files in a directory."""
        if not path:
            raise ToolError("Path is required for listing.", error_code="MISSING_PATH")
            
        command = f"ls -la '{path}'"
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(f"Failed to list directory: {result['output']}", error_code="LIST_FAILED")

        return ToolResult(success=True, output=result["output"])
