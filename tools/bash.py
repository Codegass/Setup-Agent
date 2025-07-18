"""Bash tool for executing shell commands."""

import shlex
import subprocess
from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolResult


class BashTool(BaseTool):
    """Tool for executing bash commands."""

    def __init__(self, docker_orchestrator=None):
        super().__init__(
            name="bash",
            description="Execute shell commands in the container. Use for file operations, "
            "package installation, git operations, and other system tasks.",
        )
        self.docker_orchestrator = docker_orchestrator

    def execute(self, command: str, timeout: int = 60, working_directory: str = None) -> ToolResult:
        """Execute a bash command."""
        if not command.strip():
            return ToolResult(success=False, output="", error="Empty command provided")

        logger.debug(f"Executing bash command: {command}")
        if working_directory:
            logger.debug(f"Working directory: {working_directory}")

        try:
            # Use docker orchestrator if available
            if self.docker_orchestrator:
                result = self.docker_orchestrator.execute_command(command, workdir=working_directory)
                
                return ToolResult(
                    success=result["exit_code"] == 0,
                    output=result["output"],
                    error=None if result["exit_code"] == 0 else f"Command failed with exit code {result['exit_code']}",
                    metadata={"exit_code": result["exit_code"], "command": command, "timeout": timeout},
                )
            else:
                # Fallback to local execution
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=working_directory or "/workspace",  # Use provided directory or default
                )

                # Combine stdout and stderr for complete output
                output = ""
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    if output:
                        output += "\n--- STDERR ---\n"
                    output += result.stderr

                success = result.returncode == 0

                return ToolResult(
                    success=success,
                    output=output,
                    error=None if success else f"Command failed with exit code {result.returncode}",
                    metadata={"exit_code": result.returncode, "command": command, "timeout": timeout},
                )

        except subprocess.TimeoutExpired:
            error_msg = f"Command timed out after {timeout} seconds"
            logger.warning(f"Bash command timeout: {command}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                metadata={"timeout": timeout, "command": command},
            )

        except Exception as e:
            error_msg = f"Failed to execute command: {str(e)}"
            logger.error(f"Bash command execution error: {error_msg}")
            return ToolResult(
                success=False, output="", error=error_msg, metadata={"command": command}
            )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 60)",
                    "default": 60,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Working directory for command execution (default: /workspace)",
                    "default": None,
                },
            },
            "required": ["command"],
        }
