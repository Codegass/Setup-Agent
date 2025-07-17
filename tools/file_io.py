"""File I/O tool for reading and writing files."""

import os
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolResult
from .enhanced_base import EnhancedBaseTool


class FileIOTool(EnhancedBaseTool):
    """Tool for file input/output operations."""

    def __init__(self):
        super().__init__(
            name="file_io",
            description="Read, write, or append to files. Use for editing configuration files, "
            "reading documentation, creating scripts, etc.",
        )

    def execute(self, action: str, path: str, content: str = None) -> ToolResult:
        """Execute file I/O operation."""
        if action not in ["read", "write", "append"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid action '{action}'. Must be 'read', 'write', or 'append'",
            )

        # Ensure path is within workspace
        if not path.startswith("/workspace"):
            path = f"/workspace/{path.lstrip('/')}"

        file_path = Path(path)

        try:
            if action == "read":
                return self._read_file(file_path)
            elif action == "write":
                return self._write_file(file_path, content or "")
            elif action == "append":
                return self._append_file(file_path, content or "")

        except Exception as e:
            error_msg = f"File operation failed: {str(e)}"
            logger.error(f"File I/O error for {action} on {path}: {error_msg}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                metadata={"action": action, "path": str(file_path)},
            )

    def _read_file(self, file_path: Path) -> ToolResult:
        """Read a file."""
        if not file_path.exists():
            return ToolResult(success=False, output="", error=f"File does not exist: {file_path}")

        if not file_path.is_file():
            return ToolResult(success=False, output="", error=f"Path is not a file: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
            logger.debug(f"Successfully read file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=content,
                metadata={"action": "read", "path": str(file_path), "size": len(content)},
            )
        except UnicodeDecodeError:
            # Try reading as binary and show first few bytes
            try:
                raw_content = file_path.read_bytes()
                preview = raw_content[:100].hex()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File appears to be binary. First 100 bytes (hex): {preview}",
                    metadata={"action": "read", "path": str(file_path), "binary": True},
                )
            except Exception as e:
                return ToolResult(success=False, output="", error=f"Failed to read file: {str(e)}")

    def _write_file(self, file_path: Path, content: str) -> ToolResult:
        """Write content to a file."""
        try:
            # Create parent directories if they don't exist
            file_path.parent.mkdir(parents=True, exist_ok=True)

            file_path.write_text(content, encoding="utf-8")
            logger.debug(f"Successfully wrote file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=f"Successfully wrote {len(content)} characters to {file_path}",
                metadata={"action": "write", "path": str(file_path), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to write file: {str(e)}")

    def _append_file(self, file_path: Path, content: str) -> ToolResult:
        """Append content to a file."""
        try:
            # Create parent directories if they don't exist
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Create file if it doesn't exist
            if not file_path.exists():
                file_path.touch()

            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)

            logger.debug(f"Successfully appended to file: {file_path} ({len(content)} chars)")

            return ToolResult(
                success=True,
                output=f"Successfully appended {len(content)} characters to {file_path}",
                metadata={"action": "append", "path": str(file_path), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to append to file: {str(e)}")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "append"],
                    "description": "The file operation to perform",
                },
                "path": {
                    "type": "string",
                    "description": "The file path (relative to /workspace or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write or append (not needed for read)",
                    "default": "",
                },
            },
            "required": ["action", "path"],
        }
