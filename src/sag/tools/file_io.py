"""File I/O tool for the agent."""

import inspect
import json
import shlex
from typing import Any, Dict, Optional

from loguru import logger

from sag.docker_orch.orch import DockerOrchestrator
from sag.runtime.container_io import _execute_untruncated

from .base import BaseTool, ToolError, ToolResult
from .output_paging import read_text_page, render_text_page


class FileIOTool(BaseTool):
    """Tool for reading, writing, and listing files."""

    def __init__(self, orchestrator: DockerOrchestrator):
        super().__init__(
            "file_io",
            "Read, write, or list files in the container. For reads, start_line is zero-based "
            "and end_line is exclusive. max_chars defaults to 20000 (capped at 100000). "
            "Follow the returned Next read arguments, including column_offset for long lines, "
            "to continue without losing text.",
        )
        self.orchestrator = orchestrator

    def execute(
        self,
        action: str,
        path: str,
        content: Optional[str] = None,
        start_line: int = 0,
        end_line: Optional[int] = None,
        column_offset: int = 0,
        max_chars: int = 20_000,
    ) -> ToolResult:
        """
        Execute a file operation.

        Args:
            action: The action to perform ('read', 'write', 'list').
            path: The path to the file or directory.
            content: The content to write to the file (for 'write' action).
            start_line: Zero-based starting line for reading.
            end_line: Exclusive ending line for reading.
            column_offset: Character offset within the starting line; use Next read to continue.
            max_chars: Page size, capped at 100000 characters; follow Next read for more.
        """
        # The base class now handles parameter validation automatically
        # via _validate_parameters() which checks the schema

        if action == "read":
            return self._read(path, start_line, end_line, column_offset, max_chars)
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
                    "Use action='list' to list directory contents",
                ],
                details={"provided_action": action, "valid_actions": ["read", "write", "list"]},
                retryable=True,
            )

    def _read(
        self,
        path: str,
        start_line: int,
        end_line: Optional[int],
        column_offset: int = 0,
        max_chars: int = 20_000,
    ) -> ToolResult:
        """Read a file."""
        if not path:
            raise ToolError(
                message="Path is required for reading.",
                category="validation",
                error_code="MISSING_PATH",
                retryable=True,
            )

        # Page next to the file, before crossing Docker's output transport.
        script = "from __future__ import annotations\nimport json\n" + inspect.getsource(
            read_text_page
        )
        script += (
            f"\nwith open({path!r}, encoding='utf-8', errors='replace', newline='\\n') as source:\n"
            f"    page = read_text_page(source, {start_line!r}, {end_line!r}, "
            f"{column_offset!r}, {max_chars!r})\n"
            "print(json.dumps(page, ensure_ascii=True))\n"
        )
        command = "python3 -c " + shlex.quote(script)
        result = _execute_untruncated(self.orchestrator, command)

        if not result["success"]:
            raise ToolError(
                message=f"Failed to read file: {result['output']}",
                category="execution",
                error_code="READ_FAILED",
                raw_output=result.get("output"),
                suggestions=[
                    "Check if the file exists",
                    "Verify you have read permissions",
                    "Ensure the path is correct",
                ],
                retryable=True,
            )

        page = json.loads(result["output"])

        return ToolResult.completed_success(
            output=render_text_page(page, {"action": "read", "path": path}),
            raw_output=page["text"],
            metadata={
                "path": path,
                "output_page": True,
                **{key: value for key, value in page.items() if key != "text"},
            },
        )

    def _write(self, path: str, content: Optional[str]) -> ToolResult:
        """Write content to a file."""
        if content is None:
            raise ToolError(
                message="Content is required for writing.",
                category="validation",
                error_code="MISSING_CONTENT",
                suggestions=["Provide the 'content' parameter with the text to write"],
                retryable=True,
            )

        import base64

        encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")

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
                raw_output=result.get("output"),
                suggestions=[
                    "Check if you have write permissions",
                    "Ensure the directory exists",
                    "Verify disk space is available",
                ],
                retryable=True,
            )

        return ToolResult.completed_success(
            output=f"Successfully wrote {len(content)} characters to {path}"
        )

    def _list(self, path: str) -> ToolResult:
        """List files in a directory."""
        if not path:
            raise ToolError(
                message="Path is required for listing.",
                category="validation",
                error_code="MISSING_PATH",
                retryable=True,
            )

        command = f"ls -la '{path}'"
        result = self.orchestrator.execute_command(command)

        if not result["success"]:
            raise ToolError(
                message=f"Failed to list directory: {result['output']}",
                category="execution",
                error_code="LIST_FAILED",
                raw_output=result.get("output"),
                suggestions=[
                    "Check if the directory exists",
                    "Verify you have read permissions",
                    "Ensure the path is a directory, not a file",
                ],
                retryable=True,
            )

        return ToolResult.completed_success(output=result["output"])
