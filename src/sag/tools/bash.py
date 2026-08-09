"""Bash tool for executing shell commands with specialized grep functionality."""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import BaseTool, ToolError, ToolResult
from .internal.build_utils import (
    DETACHED_HANDOFF_STATUSES,
    classify_detached_completion,
    detached_handoff_tool_result,
    detached_poll_ref,
    detached_runner_from_command,
)


@dataclass
class BashToolConfig:
    """Configuration for the BashTool."""

    allowed_commands: List[str] = field(default_factory=list)
    blocked_commands: List[str] = field(default_factory=list)
    enable_background_processes: bool = True
    block_interactive_commands: bool = True
    audit_command_execution: bool = True
    add_sag_cli_marker: bool = True


class BashTool(BaseTool):
    """Tool for executing bash commands with advanced grep investigation capabilities.

    GREP - THE PRIMARY INVESTIGATION TOOL:
    grep is the most powerful tool for code investigation. Use it to:
    • Find function definitions: grep -r "def function_name" .
    • Search for class declarations: grep -r "class ClassName" .
    • Find imports: grep -r "import module_name" .
    • Search for specific patterns: grep -r "error\\|exception\\|fail" .
    • Find configuration: grep -r "config\\|setting" .

    ESSENTIAL GREP PATTERNS:
    • Basic search: grep "pattern" file.txt
    • Recursive search: grep -r "pattern" directory/
    • Case insensitive: grep -i "pattern" file.txt
    • Show line numbers: grep -n "pattern" file.txt
    • Show context: grep -C 3 "pattern" file.txt (3 lines before/after)
    • Multiple patterns: grep -E "pattern1|pattern2" file.txt
    • Exclude files: grep -r "pattern" . --exclude="*.log"
    • Include only specific files: grep -r "pattern" . --include="*.py"
    • Count matches: grep -c "pattern" file.txt
    • Show only matching files: grep -l "pattern" *.txt
    • Invert match: grep -v "pattern" file.txt

    INVESTIGATION WORKFLOWS:
    1. Project Overview: grep -r "def\\|class\\|import" . --include="*.py" | head -20
    2. Error Investigation: grep -r -i "error\\|exception\\|fail" . --include="*.py" -C 2
    3. Configuration Discovery: grep -r "config\\|setting\\|env" . --exclude-dir=".git"
    4. API Endpoints: grep -r "route\\|endpoint\\|@app" . --include="*.py"
    5. Database Queries: grep -r "SELECT\\|INSERT\\|UPDATE\\|DELETE" . -i

    ADVANCED GREP TECHNIQUES:
    • Regex patterns: grep -E "^[A-Z]+_[A-Z]+" config.txt
    • Fixed strings (no regex): grep -F "literal.string" file.txt
    • Whole words only: grep -w "word" file.txt
    • Binary files: grep -a "pattern" binary_file
    • Follow symlinks: grep -r -L "pattern" directory/

    Use bash for: file operations, package installation, git operations, system tasks, and ESPECIALLY grep-based code investigation.
    """

    def __init__(self, docker_orchestrator=None, config: Optional[BashToolConfig] = None):
        super().__init__(
            name="bash",
            description="Execute shell commands in the container. SPECIALIZES in grep-based code investigation. "
            "grep is your PRIMARY tool for understanding codebases, finding patterns, and investigating issues. "
            "Use for file operations, package installation, git operations, and comprehensive code analysis. "
            "Supports background processes with & and command validation.",
        )
        self.docker_orchestrator = docker_orchestrator
        self.config = config or BashToolConfig()
        self.background_processes: Dict[str, List[int]] = {}  # Track background PIDs per container

    @staticmethod
    def _execution_metadata(
        command: str,
        working_directory: Optional[str],
        *,
        exit_code: Optional[int],
        timed_out: bool,
        duration: Optional[float],
        executed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return {
            "command": command,
            "cwd": working_directory,
            "executed": bool(exit_code is not None) if executed is None else executed,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration": 0 if duration is None else duration,
        }

    def _with_execution_metadata(
        self,
        metadata: Optional[Dict[str, Any]],
        *,
        command: str,
        working_directory: Optional[str],
        exit_code: Optional[int],
        timed_out: bool,
        duration: Optional[float],
        executed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        result_metadata = dict(metadata or {})
        result_metadata["execution"] = self._execution_metadata(
            command,
            working_directory,
            exit_code=exit_code,
            timed_out=timed_out,
            duration=duration,
            executed=executed,
        )
        return result_metadata

    def _pre_execution_failure(
        self,
        *,
        command: Optional[str],
        working_directory: Optional[str],
        output: str,
        error: str,
        error_code: str,
        failure_category: str,
        retryable: bool,
        suggestions: Optional[List[str]] = None,
        raw_output: str = "",
    ) -> ToolResult:
        return ToolResult.completed_failure(
            output=output,
            error=error,
            error_code=error_code,
            suggestions=suggestions or [],
            raw_output=raw_output,
            metadata=self._with_execution_metadata(
                {
                    "failure_category": failure_category,
                    "retryable": retryable,
                },
                command=command,
                working_directory=working_directory,
                exit_code=None,
                timed_out=False,
                duration=0,
                executed=False,
            ),
        )

    def _maybe_backfill_pre_execution_metadata(
        self,
        result: ToolResult,
        *,
        command: Optional[str],
        working_directory: Optional[str],
    ) -> ToolResult:
        if "execution" in result.metadata:
            return result

        if result.error_code not in {
            "MISSING_PARAMETERS",
            "UNEXPECTED_PARAMETERS",
            "MISSING_COMMAND",
            "INVALID_PARAMETERS",
            "INVALID_TIMEOUT",
            "NO_ORCHESTRATOR",
            "COMMAND_BLOCKED",
            "INTERACTIVE_COMMAND",
        }:
            return result

        metadata = dict(result.metadata)
        metadata.setdefault(
            "failure_category",
            "execution" if result.error_code == "NO_ORCHESTRATOR" else "validation",
        )
        metadata.setdefault("retryable", result.error_code != "NO_ORCHESTRATOR")
        result.metadata = self._with_execution_metadata(
            metadata,
            command=command,
            working_directory=working_directory,
            exit_code=None,
            timed_out=False,
            duration=0,
            executed=False,
        )
        return result

    def _working_directory_available(self, requested_workdir: str) -> tuple[bool, str]:
        """Observe whether the submitted cwd exists without changing it.

        This probe is deliberately read-only.  A missing or unprobeable cwd is
        a typed pre-execution refusal; the shell tool must never create the
        directory or silently dispatch the command from another location.
        """

        if not isinstance(requested_workdir, str) or not requested_workdir.strip():
            return False, "working directory must be a non-empty string"
        try:
            # POSIX test has no `--` end-of-options: with three args the middle
            # one must be a binary operator, so `test -d -- /path` is exit 2
            # for EVERY path (live 2026-08-09: all bash-with-cwd calls were
            # refused). The two-argument form is unambiguous — $2 is always an
            # operand, even when the quoted path starts with a dash.
            probe = self.docker_orchestrator.execute_command(
                f"test -d {shlex.quote(requested_workdir)}",
                workdir=None,
            )
        except Exception as exc:
            return False, f"working-directory probe failed: {type(exc).__name__}"
        if not isinstance(probe, dict):
            return False, "working-directory probe returned no structured result"
        if probe.get("dispatch_status") or probe.get("exit_code") == -1:
            return False, "working-directory probe did not reach a terminal result"
        succeeded = probe.get("success")
        if succeeded is None:
            succeeded = probe.get("exit_code") == 0
        if succeeded:
            return True, ""
        return False, f"working directory does not exist or is unavailable: {requested_workdir}"

    def _validate_parameters(self, kwargs: Dict[str, Any]) -> None:
        super()._validate_parameters(kwargs)

        command = kwargs.get("command")
        if command is None:
            raise ToolError(
                message="Missing required parameters: command",
                category="validation",
                error_code="MISSING_PARAMETERS",
                suggestions=[
                    "Provide the missing parameters: command",
                    "Use the parameter schema to understand required parameters",
                    "Example usage: bash(command=<value>)",
                ],
                documentation_links=[f"Tool documentation: {self.get_usage_example()}"],
                details={"missing_parameters": ["command"]},
                retryable=True,
            )

    def _validate_command(self, command: str) -> tuple[bool, str]:
        """Validate command against allowlist/blocklist.

        Returns:
            (is_valid, error_message)
        """
        # Split command chains (&&, ||, ;) and validate each part
        command_parts = re.split(r"\s*(?:&&|\|\||;)\s*", command)

        for part in command_parts:
            part = part.strip()
            if not part:
                continue

            # Extract the base command (first word)
            base_cmd = shlex.split(part)[0] if part else ""

            # Check blocklist first (takes precedence)
            for blocked in self.config.blocked_commands:
                if base_cmd.startswith(blocked) or part.startswith(blocked):
                    return False, f"Command '{base_cmd}' is blocked by security policy"

            # If allowlist is configured, check if command is allowed
            if self.config.allowed_commands:
                allowed = False
                for allowed_cmd in self.config.allowed_commands:
                    if base_cmd.startswith(allowed_cmd) or allowed_cmd == "*":
                        allowed = True
                        break
                if not allowed:
                    return False, f"Command '{base_cmd}' is not in the allowlist"

        # Check for dangerous patterns (process substitution)
        if "<(" in command or ">(" in command:
            return False, "Process substitution is not allowed for security reasons"

        return True, ""

    def _is_background_command(self, command: str) -> bool:
        """Check if command should run in background."""
        # Check if command ends with & (not &&)
        command = command.strip()
        return command.endswith("&") and not command.endswith("&&")

    def _detect_interactive_command(self, command: str) -> tuple[bool, str]:
        """Detect if command requires interactive input.

        Returns:
            (is_interactive, suggestion)
        """
        interactive_patterns = [
            (r"\bnpm init\b(?!.*-y)", 'Use "npm init -y" for non-interactive mode'),
            (r"\bgit rebase -i\b", "Interactive rebase not supported in containers"),
            (r"\bgit add -i\b", "Interactive add not supported, use specific file paths"),
            (
                r"\bvim?\b|\bnano\b|\bemacs\b",
                "Text editors not supported, use file manipulation tools",
            ),
            (r"\bsudo -S\b", "Password input not supported, configure sudoers if needed"),
            (r"\bpasswd\b", "Password changes not supported in container"),
            (r"\bssh\b.*@", "SSH with password not supported, use key-based auth"),
            (r"\btop\b|\bhtop\b", "Interactive monitoring not supported, use one-shot commands"),
        ]

        for pattern, suggestion in interactive_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                if self.config.block_interactive_commands:
                    return True, suggestion
                else:
                    logger.warning(f"Potentially interactive command detected: {suggestion}")

        return False, ""

    def _extract_key_info(self, output: str, command: str = "") -> str:
        """Extract key information from command output."""
        return self._extract_bash_key_info(output, command)

    def _extract_bash_key_info(self, output: str, command: str = "") -> str:
        """Extract key information from bash output with aggressive truncation for verbose commands."""
        if not output:
            return output

        lines = output.split("\n")
        total_lines = len(lines)

        # CRITICAL: Detect verbose package management commands by COMMAND, not output
        command_lower = command.lower()
        is_verbose_package_cmd = any(
            pattern in command_lower
            for pattern in [
                "apt-get install",
                "apt install",
                "yum install",
                "dnf install",
                "npm install",
                "pip install",
                "cargo install",
                "go get",
            ]
        )

        if is_verbose_package_cmd and total_lines > 50:
            # For verbose package commands, use AGGRESSIVE truncation
            logger.info(
                f"🔧 Detected verbose package command with {total_lines} lines, applying aggressive truncation"
            )

            # Keep only: head (25 lines) + tail (25 lines) = 50 lines total
            key_start = lines[:25]
            key_end = lines[-25:]

            # Extract critical status lines from the middle if any
            critical_lines = []
            for line in lines[10:-10]:  # Skip already included start/end
                line_lower = line.lower()
                if any(
                    critical in line_lower
                    for critical in [
                        "error:",
                        "failed:",
                        "could not",
                        "unable to",
                        "permission denied",
                        "build success",
                        "build failure",
                        "completed successfully",
                        "warning:",
                        "critical:",
                    ]
                ):
                    critical_lines.append(line)
                    if len(critical_lines) >= 5:  # Limit critical lines to prevent spam
                        break

            result_parts = []
            result_parts.extend(key_start)
            if critical_lines:
                result_parts.append(f"\n... [Key status messages from {total_lines} lines] ...")
                result_parts.extend(critical_lines)
            result_parts.append(
                f"\n... [Skipped {total_lines - 50 - len(critical_lines)} lines of verbose output] ..."
            )
            result_parts.extend(key_end)

            return "\n".join(result_parts)

        # If this looks like grep output, preserve more context
        if any(line.strip() and ":" in line for line in lines[:10]):
            # This might be grep output with file:line:content format
            key_lines = []
            for line in lines:
                if line.strip():
                    # Preserve grep results with context
                    key_lines.append(line)
                    if len(key_lines) >= 50:  # Keep more grep results
                        break

            if key_lines:
                result = "\n".join(key_lines)
                if len(lines) > len(key_lines):
                    result += f"\n... [Showing first {len(key_lines)} matches out of {len(lines)} total lines]"
                return result

        # For regular commands, extract key information
        key_lines = []
        error_lines = []
        line_count = 0

        for line in lines:
            line_lower = line.lower()
            line_count += 1

            # Stop processing if we've seen too many lines (prevent context pollution)
            if line_count > 100:
                key_lines.append(f"... [Stopped processing after 100 lines, total: {total_lines}]")
                break

            # Capture errors and important status (high priority)
            if any(
                keyword in line_lower
                for keyword in [
                    "error:",
                    "exception:",
                    "failed:",
                    "warning:",
                    "critical:",
                    "build success",
                    "build failure",
                    "success:",
                    "completed:",
                ]
            ):
                if "error" in line_lower or "exception" in line_lower or "failed" in line_lower:
                    error_lines.append(f"🚨 {line.strip()}")
                else:
                    key_lines.append(f"✅ {line.strip()}")

            # File operations (medium priority)
            elif any(
                keyword in line_lower
                for keyword in ["created:", "copied:", "moved:", "deleted:", "modified:"]
            ):
                key_lines.append(f"📁 {line.strip()}")

            # Package management (low priority - be selective)
            elif any(
                keyword in line_lower
                for keyword in [
                    "installed successfully",
                    "removed successfully",
                    "updated successfully",
                    "package not found",
                    "dependency error",
                ]
            ):
                key_lines.append(f"📦 {line.strip()}")

            # Git operations (medium priority)
            elif any(
                keyword in line_lower for keyword in ["commit", "push", "pull", "branch", "merge"]
            ):
                key_lines.append(f"🔄 {line.strip()}")

        # Combine results with strict limits
        result_lines = error_lines[:10] + key_lines[:30]  # Limit to prevent bloat
        if result_lines:
            if total_lines > len(result_lines) + 10:
                result_lines.append(
                    f"... [Extracted {len(result_lines)} key lines from {total_lines} total]"
                )
            return "\n".join(result_lines)

        # If no key patterns found, apply strict truncation
        if len(output) > 1500:  # Reduced from 2000
            truncated = (
                output[:800]
                + "\n... [Output truncated to prevent context pollution] ..."
                + output[-400:]
            )
            logger.info(
                f"🔧 Applied fallback truncation: {len(output)} chars → {len(truncated)} chars"
            )
            return truncated

        return output

    def execute(
        self,
        command: str = None,
        timeout: int = 60,
        working_directory: str = "/workspace",
        environment: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Execute a bash command in the Docker container with enhanced monitoring.

        Args:
            command: The bash command to execute
            timeout: Maximum total execution time in seconds
            working_directory: Working directory (default: /workspace)
            environment: Additional environment variables to set
        """

        # Check for unexpected parameters and provide clear error feedback
        if kwargs:
            invalid_params = list(kwargs.keys())
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output=(
                    f"❌ Invalid parameters for bash tool: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - command (required): The bash command to execute\n"
                    f"  - timeout (optional): Maximum total execution time in seconds (default: 60)\n"
                    f"  - working_directory (optional): Working directory (default: /workspace)\n"
                    f"  - environment (optional): Additional environment variables\n\n"
                    "Example syntax: bash(command='ls -la', timeout=30, "
                    "working_directory='/workspace/project')"
                ),
                error=f"Invalid parameters: {invalid_params}",
                error_code="INVALID_PARAMETERS",
                failure_category="validation",
                retryable=True,
            )

        # Check for required parameter
        if not command or not command.strip():
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output=(
                    "❌ Missing required parameter: 'command'\n\n"
                    "The bash tool requires a 'command' parameter.\n"
                    "Example syntax: bash(command='ls -la', timeout=30)"
                ),
                error="Missing required parameter: command",
                error_code="MISSING_COMMAND",
                failure_category="validation",
                retryable=True,
            )

        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output="❌ Invalid timeout for bash tool: timeout must be a positive integer.",
                error="Invalid timeout: must be a positive integer",
                error_code="INVALID_TIMEOUT",
                failure_category="validation",
                retryable=True,
            )
        if timeout <= 0:
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output="❌ Invalid timeout for bash tool: timeout must be greater than 0.",
                error="Invalid timeout: must be greater than 0",
                error_code="INVALID_TIMEOUT",
                failure_category="validation",
                retryable=True,
            )

        if not self.docker_orchestrator:
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output="Docker orchestrator not available",
                error="Docker orchestrator not available",
                error_code="NO_ORCHESTRATOR",
                failure_category="execution",
                retryable=False,
                suggestions=["Ensure Docker is running and container is available"],
            )

        # Validate command against security policies
        is_valid, error_msg = self._validate_command(command)
        if not is_valid:
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output=f"Command validation failed: {error_msg}",
                error=f"Command validation failed: {error_msg}",
                error_code="COMMAND_BLOCKED",
                failure_category="validation",
                retryable=True,
                suggestions=[
                    "Check if the command is allowed by security policy",
                    "Use alternative commands that are permitted",
                    "Contact administrator to update security policy if needed",
                ],
            )

        # Check for interactive commands
        is_interactive, suggestion = self._detect_interactive_command(command)
        if is_interactive and self.config.block_interactive_commands:
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output="Interactive command detected",
                error="Interactive command detected",
                error_code="INTERACTIVE_COMMAND",
                failure_category="validation",
                retryable=True,
                suggestions=[suggestion, "Use non-interactive alternatives"],
            )

        # Validate the submitted cwd without creating or substituting one.
        # ActionIntent/envelope/runner metadata must describe the same command
        # and cwd byte-for-byte.
        cwd_available, cwd_error = self._working_directory_available(working_directory)
        if not cwd_available:
            return self._pre_execution_failure(
                command=command,
                working_directory=working_directory,
                output=f"Working directory unavailable: {working_directory}",
                error=cwd_error,
                error_code="WORKING_DIRECTORY_UNAVAILABLE",
                failure_category="validation",
                retryable=True,
                suggestions=[
                    "Observed fact: the submitted working directory is unavailable",
                    "Constraint: shell execution does not create or substitute a working directory",
                ],
            )
        workdir = working_directory

        # Check if this is a background command
        is_background = self._is_background_command(command)
        if is_background and self.config.enable_background_processes:
            # Remove the trailing & for processing
            command = command.rstrip().rstrip("&").rstrip()
            logger.info(f"🚀 Detected background command: {command}")

        # Prepare environment variables
        env_vars = environment or {}
        if self.config.add_sag_cli_marker:
            env_vars["SAG_CLI"] = "1"

        # Detect if this is a long-running command that needs enhanced monitoring
        is_long_running_command = self._is_long_running_command(command) and not is_background

        try:
            logger.info(f"Executing bash command: {command}")
            logger.info(f"Working directory: {workdir}")
            if env_vars:
                logger.info(f"Environment variables: {list(env_vars.keys())}")

            # Handle background execution
            if is_background and self.config.enable_background_processes:
                return self._execute_background_command(command, workdir, env_vars)

            if is_long_running_command and hasattr(
                self.docker_orchestrator, "execute_command_with_soft_timeout"
            ):
                # Dispatch-and-poll: run detached with a soft window bounded by
                # the caller's timeout; if still running when it closes, hand
                # the log tail back instead of killing the process.
                soft_timeout = min(timeout, self._dispatch_soft_timeout_default())
                logger.info(
                    f"🔍 Using dispatch-and-poll for long-running command (soft window: {soft_timeout}s)"
                )
                result = self.docker_orchestrator.execute_command_with_soft_timeout(
                    command=command,
                    workdir=workdir,
                    environment=env_vars,
                    soft_timeout=soft_timeout,
                )
            elif is_long_running_command:
                # Get dynamic timeouts based on command type
                silent_timeout, absolute_timeout = self._get_command_timeout(command)
                absolute_timeout = timeout
                silent_timeout = min(silent_timeout, max(1, timeout // 2))
                logger.info(
                    f"🔍 Using enhanced monitoring for long-running command (silent: {silent_timeout}s, total: {absolute_timeout}s)"
                )

                result = self.docker_orchestrator.execute_command_with_monitoring(
                    command=command,
                    workdir=workdir,
                    silent_timeout=silent_timeout,
                    absolute_timeout=absolute_timeout,
                    use_timeout_wrapper=True,
                    enable_cpu_monitoring=True,
                    optimize_for_maven=("mvn" in command),
                    # Note: env vars handled in wrapped command for monitoring
                )
            else:
                # Use regular execution for normal commands
                result = self.docker_orchestrator.execute_command(
                    command,
                    workdir=workdir,
                    capture_stderr=True,
                    environment=env_vars,
                    timeout=timeout,
                )

            # Handle a nonterminal dispatch-and-poll handoff; tell the agent how
            # to poll the same job even when the latest liveness probe was inconclusive.
            if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
                return detached_handoff_tool_result("bash", command, result)

            if result.get("dispatch_status") == "completed_detached":
                detached_result = classify_detached_completion(
                    result.get("exit_code"),
                    str(result.get("output") or ""),
                    runner=detached_runner_from_command(command),
                    full_output=str(result.get("full_output") or result.get("output") or ""),
                    poll_ref=detached_poll_ref(result),
                    invocation_status=(
                        "crashed" if result.get("lifecycle_state") == "vanished" else "completed"
                    ),
                )
                if not detached_result.succeeded:
                    detached_result.metadata.update(
                        self._with_execution_metadata(
                            {
                                "dispatch_status": "completed_detached",
                                "exit_code": result.get("exit_code"),
                                "execution_directory": workdir,
                                "environment_vars": env_vars,
                                "timeout": timeout,
                            },
                            command=command,
                            working_directory=workdir,
                            exit_code=result.get("exit_code"),
                            timed_out=False,
                            duration=result.get("duration"),
                        )
                    )
                    return detached_result

            # Handle timeout terminations
            if result.get("termination_reason"):
                # Get the timeouts that were used
                if is_long_running_command:
                    silent_timeout, absolute_timeout = self._get_command_timeout(command)
                    absolute_timeout = timeout
                    silent_timeout = min(silent_timeout, max(1, timeout // 2))
                    error_messages = {
                        "absolute_timeout": f"Command exceeded maximum execution time ({absolute_timeout/60:.0f} minutes)",
                        "silent_timeout": f"Command was silent for too long ({silent_timeout/60:.0f} minutes)",
                        "monitoring_error": f"Error occurred during command monitoring",
                    }
                else:
                    error_messages = {
                        "absolute_timeout": f"Command exceeded maximum execution time",
                        "silent_timeout": f"Command was silent for too long",
                        "monitoring_error": f"Error occurred during command monitoring",
                    }

                termination_reason = result["termination_reason"]
                error_msg = error_messages.get(
                    termination_reason, f"Command terminated: {termination_reason}"
                )
                is_timeout = termination_reason in {"absolute_timeout", "silent_timeout"}
                error_code = (
                    f"TIMEOUT_{termination_reason.upper()}"
                    if is_timeout
                    else (
                        "MONITORING_ERROR"
                        if termination_reason == "monitoring_error"
                        else f"COMMAND_TERMINATED_{termination_reason.upper()}"
                    )
                )

                # Include monitoring info in suggestions
                monitoring_info = result.get("monitoring_info", {})
                suggestions = [
                    f"Command was terminated due to: {termination_reason}",
                    f"Execution time: {monitoring_info.get('execution_time', 0):.1f} seconds",
                    "Consider breaking down the command into smaller steps",
                    "Check if the command requires user interaction (not supported in containers)",
                ]

                if monitoring_info.get("cpu_warnings", 0) > 0:
                    suggestions.append(
                        f"CPU warnings detected: {monitoring_info['cpu_warnings']} (possible hang)"
                    )

                return ToolResult.terminal_failure(
                    invocation_status=("timeout" if is_timeout else "crashed"),
                    output=result.get("output", ""),
                    error=error_msg,
                    suggestions=suggestions,
                    error_code=error_code,
                    metadata=self._with_execution_metadata(
                        {
                            "termination_reason": termination_reason,
                            "timeout": timeout,
                            "monitoring_info": monitoring_info,
                        },
                        command=command,
                        working_directory=workdir,
                        exit_code=result.get("exit_code"),
                        timed_out=is_timeout,
                        duration=result.get("duration", monitoring_info.get("execution_time")),
                    ),
                )

            # Normal command completion
            if result["success"]:
                # Extract key information for different command types
                extracted_output = self._extract_key_info(result["output"], command)

                # Analyze output for completion signals
                completion_signals = self._detect_completion_signals(result["output"], command)

                # Detect and record test telemetry if applicable
                test_data = self._detect_test_output(command, result["output"])
                if test_data:
                    self._write_test_telemetry(
                        command, workdir, result.get("exit_code", 0), test_data
                    )
                    logger.info(
                        f"📊 Detected {test_data['tool']} test results: {test_data['tests']}"
                    )

                # Separate stdout and stderr if available
                stdout = result.get("stdout", result["output"])
                stderr = result.get("stderr", "")

                return ToolResult.completed_success(
                    output=extracted_output,
                    raw_output=result["output"],
                    metadata=self._with_execution_metadata(
                        {
                            "stdout": stdout,
                            "stderr": stderr,
                            "exit_code": result.get("exit_code", 0),
                            "signal": result.get("signal"),
                            "execution_directory": workdir,
                            "environment_vars": env_vars,
                            "timeout": timeout,
                            "monitoring_info": result.get("monitoring_info"),
                            "is_long_running": is_long_running_command,
                            "completion_signals": completion_signals,
                            "command_type": self._get_command_type(command),
                            "extracted_values": self._extract_values(result["output"], command),
                            "background_pids": [],
                        },
                        command=command,
                        working_directory=workdir,
                        exit_code=result.get("exit_code", 0),
                        timed_out=False,
                        duration=result.get(
                            "duration", (result.get("monitoring_info") or {}).get("execution_time")
                        ),
                    ),
                )
            else:
                # Analyze error for specific failure patterns
                error_analysis = self._analyze_error_output(result["output"], command)

                # Separate stdout and stderr if available
                stdout = result.get("stdout", result["output"])
                stderr = result.get("stderr", "")

                return ToolResult.completed_failure(
                    output=result["output"],
                    error=f"Command failed with exit code {result.get('exit_code', 'unknown')}",
                    suggestions=self._generate_error_suggestions(
                        error_analysis, command, result.get("exit_code")
                    ),
                    error_code=error_analysis.get("error_code", "COMMAND_FAILED"),
                    metadata=self._with_execution_metadata(
                        {
                            "stdout": stdout,
                            "stderr": stderr,
                            "exit_code": result.get("exit_code"),
                            "signal": result.get("signal"),
                            "execution_directory": workdir,
                            "environment_vars": env_vars,
                            "timeout": timeout,
                            "monitoring_info": result.get("monitoring_info"),
                            "error_analysis": error_analysis,
                            "recovery_commands": self._get_recovery_commands(error_analysis),
                            "background_pids": [],
                        },
                        command=command,
                        working_directory=workdir,
                        exit_code=result.get("exit_code"),
                        timed_out=False,
                        duration=result.get(
                            "duration", (result.get("monitoring_info") or {}).get("execution_time")
                        ),
                    ),
                )

        except Exception as e:
            raise ToolError(
                message=f"Failed to execute bash command: {str(e)}",
                suggestions=[
                    "Check if Docker container is running",
                    "Verify network connectivity",
                    "Ensure sufficient disk space in container",
                    "Check container logs for system errors",
                ],
                error_code="EXECUTION_ERROR",
            )

    def safe_execute(self, **kwargs) -> ToolResult:
        result = super().safe_execute(**kwargs)
        return self._maybe_backfill_pre_execution_metadata(
            result,
            command=kwargs.get("command"),
            working_directory=kwargs.get("working_directory", "/workspace"),
        )

    def _execute_background_command(
        self, command: str, workdir: str, env_vars: Dict[str, str]
    ) -> ToolResult:
        """Dispatch a background command and return its stable poll handle."""
        try:
            handle = self.docker_orchestrator.execute_command_detached(
                command, workdir=workdir, environment=env_vars
            )
            source_dispatch_status = str(handle.get("dispatch_status") or "").strip()
            dispatch_status = source_dispatch_status
            start_definitively_not_accepted = bool(
                handle.get("started") is False
                and handle.get("start_accepted") is False
                and handle.get("runner_dispatched") is False
                and handle.get("runner_dispatch_state") == "not_accepted"
            )
            if start_definitively_not_accepted:
                failure_metadata = {
                    "dispatch_status": dispatch_status or "dispatch_failed",
                    "runner_dispatched": False,
                    "runner_dispatch_state": "not_accepted",
                    "start_accepted": False,
                    "startup_identity_verified": handle.get("startup_identity_verified"),
                    "job_id": handle.get("job_id"),
                    "terminal_authority": handle.get("terminal_authority"),
                    "docker_exec_id": handle.get("docker_exec_id"),
                    "container_id": handle.get("container_id"),
                    "log_path": handle.get("log_path"),
                    "dispatch": dict(handle),
                }
                return ToolResult.completed_failure(
                    output=handle.get("launch_output", ""),
                    error="Failed to dispatch background process",
                    suggestions=[
                        "Check command syntax",
                        "Verify the command can run in the container",
                        "Check container logs for errors",
                    ],
                    error_code="BACKGROUND_START_FAILED",
                    metadata=self._with_execution_metadata(
                        failure_metadata,
                        command=command,
                        working_directory=workdir,
                        exit_code=None,
                        timed_out=False,
                        duration=None,
                        executed=False,
                    ),
                )

            # Anything other than a proven non-acceptance remains a controller
            # barrier. In particular, a lost exec_start response may describe
            # a runner Docker already accepted; returning a completed failure
            # would invite a duplicate retry.
            if not dispatch_status:
                dispatch_status = (
                    "running_detached" if handle.get("started") is True else "dispatch_unknown"
                )
            elif dispatch_status not in DETACHED_HANDOFF_STATUSES:
                dispatch_status = (
                    "liveness_unknown_detached"
                    if (
                        handle.get("start_accepted") is True
                        and handle.get("runner_dispatched") is True
                        and handle.get("runner_dispatch_state") == "accepted"
                    )
                    else "dispatch_unknown"
                )
            handoff_result: Dict[str, Any] = {
                "output": (
                    f"Background command dispatched as job:{handle['job_id']}"
                    if handle.get("started") is True
                    else str(handle.get("launch_output") or "Background dispatch is unresolved")
                ),
                "dispatch_status": dispatch_status,
                "runner_dispatched": handle.get("runner_dispatched"),
                "handoff_reason": "explicit_background_command",
                "dispatch": dict(handle),
                # Generic Bash background execution has no frozen invocation
                # contract or trustworthy pre-dispatch report snapshot. It
                # therefore enters the controller's ephemeral barrier but may
                # not mint an obligation whose later settlement could claim
                # pre-existing report XML as evidence from this command.
                "job_obligation_persisted": False,
                "job_obligation_persistence_code": (
                    "bash_background_evidence_boundary_unavailable"
                    if (
                        handle.get("started") is True
                        and handle.get("start_accepted") is True
                        and handle.get("runner_dispatched") is True
                        and handle.get("runner_dispatch_state") == "accepted"
                        and handle.get("startup_identity_verified") is True
                    )
                    else "invalid_dispatch_handle"
                ),
            }
            pending = detached_handoff_tool_result("bash", command, handoff_result)
            pending.metadata.update(
                {
                    # Keep the host-returned handle byte-for-byte equivalent
                    # while projecting the fields the ReAct barrier validates.
                    "dispatch": dict(handle),
                    "source_dispatch_status": source_dispatch_status,
                    "working_directory": workdir,
                    "startup_identity_verified": handle.get("startup_identity_verified"),
                    "started": handle.get("started"),
                }
            )
            return pending

        except Exception as e:
            logger.error(f"Failed to execute background command: {e}")
            raise ToolError(
                message=f"Failed to execute background command: {str(e)}",
                suggestions=[
                    "Check if background processes are enabled",
                    "Verify container supports nohup",
                    "Check command syntax",
                ],
                error_code="BACKGROUND_EXECUTION_ERROR",
            )

    def get_background_processes(
        self, container_name: Optional[str] = None
    ) -> Dict[str, List[int]]:
        """Get list of background processes for a container or all containers."""
        if container_name:
            return {container_name: self.background_processes.get(container_name, [])}
        return self.background_processes.copy()

    def check_background_process(
        self, pid: int, container_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Check status of a background process."""
        if not container_name and self.docker_orchestrator:
            container_name = self.docker_orchestrator.container_name

        if not container_name:
            return {"exists": False, "error": "No container specified"}

        try:
            # Check if process is still running
            check_cmd = f"ps -p {pid} > /dev/null 2>&1 && echo 'RUNNING' || echo 'STOPPED'"
            result = self.docker_orchestrator.execute_command(check_cmd, workdir=None)

            is_running = result["success"] and "RUNNING" in result["output"]

            # Try to get output file if it exists
            output = ""
            output_file = f"/tmp/sag_bg_{pid}.out"
            output_cmd = (
                f"test -f {output_file} && tail -n 50 {output_file} || echo 'No output file'"
            )
            output_result = self.docker_orchestrator.execute_command(output_cmd, workdir=None)
            if output_result["success"]:
                output = output_result["output"]

            return {
                "pid": pid,
                "running": is_running,
                "output": output,
                "container": container_name,
            }

        except Exception as e:
            return {"exists": False, "error": str(e)}

    # Read-only inspection commands are never long-running, even when their
    # arguments mention build/test files (`cat build.gradle`, `ls src/test`,
    # `test -d /workspace/x`) — routing them through dispatch-and-poll would
    # add seconds of latency to sub-second commands.
    QUICK_INSPECTION_COMMANDS = frozenset(
        {
            "cat",
            "ls",
            "head",
            "tail",
            "grep",
            "echo",
            "find",
            "stat",
            "wc",
            "pwd",
            "which",
            "file",
            "du",
            "df",
            "env",
            "printenv",
            "ps",
            "test",
            "[",
            "tree",
            "realpath",
            "readlink",
        }
    )

    def _is_long_running_command(self, command: str) -> bool:
        """Detect if a command is likely to be long-running and needs enhanced monitoring."""

        # Scan only the command line itself — keywords inside a heredoc body
        # are data, not commands (round 3: python heredocs got dispatched).
        scan_text = command.split("<<", 1)[0].lower()
        stripped = scan_text.strip()

        # Version/usage probes finish in milliseconds regardless of the binary —
        # but only when the WHOLE command is a single short invocation. A probe
        # flag inside a compound or piped command belongs to one segment only;
        # a short version probe before a later command must not exempt the
        # remaining work from dispatch-and-poll.
        first_line_tokens = stripped.split()
        if (
            len(first_line_tokens) <= 3
            and not any(sep in stripped for sep in ("&&", ";", "|"))
            and any(
                tok in ("-v", "--version", "-version", "-h", "--help")
                for tok in first_line_tokens[1:]
            )
        ):
            return False

        tokens = first_line_tokens
        first_token = tokens[0].rsplit("/", 1)[-1] if tokens else ""
        if first_token in self.QUICK_INSPECTION_COMMANDS and not any(
            sep in stripped for sep in ("&&", ";")
        ):
            return False

        long_running_patterns = [
            # Any maven/gradle invocation can be long (compile, verify, jar...)
            "mvn ",
            "mvnw",
            "gradlew",
            "gradle ",
            "npm install",
            "npm run build",
            "docker build",
            "apt-get update",
            "apt-get install",
            "wget",
            "curl.*-o",  # downloads
            "git clone",
            "make",
            "cmake --build",
        ]

        # Check for explicit patterns
        for pattern in long_running_patterns:
            if pattern in scan_text:
                return True

        # Check for commands that typically take time
        time_consuming_commands = ["compile", "build", "test", "install", "download", "clone"]
        for keyword in time_consuming_commands:
            if keyword in scan_text:
                return True

        return False

    def _dispatch_soft_timeout_default(self) -> int:
        """Configured soft window for dispatch-and-poll (settings fallback 900s)."""
        config = getattr(self.docker_orchestrator, "config", None)
        return getattr(config, "dispatch_soft_timeout_seconds", 900) or 900

    def _detect_completion_signals(self, output: str, command: str) -> dict:
        """Detect completion signals in command output."""
        signals = {
            "build_success": False,
            "tests_passed": False,
            "installation_complete": False,
            "file_created": False,
            "service_started": False,
            "package_installed": False,
        }

        output_lower = output.lower()

        # Build success indicators
        if any(
            pattern in output_lower
            for pattern in ["build successful", "build success", "successfully built"]
        ):
            signals["build_success"] = True

        # Test success indicators
        if "tests passed" in output_lower or "all tests passed" in output_lower:
            signals["tests_passed"] = True
        elif "tests run:" in output and "failures: 0" in output and "errors: 0" in output:
            signals["tests_passed"] = True

        # Installation indicators
        if "successfully installed" in output_lower or "installation complete" in output_lower:
            signals["installation_complete"] = True

        # Package manager indicators
        if "packages can be updated" in output_lower or "newly installed" in output_lower:
            signals["package_installed"] = True

        # Service indicators
        if "active (running)" in output or "started successfully" in output_lower:
            signals["service_started"] = True

        # File creation indicators
        if re.search(r"created|written|saved|generated", output_lower):
            signals["file_created"] = True

        return signals

    def _get_command_type(self, command: str) -> str:
        """Categorize the command type for better context."""
        command_lower = command.lower()

        if any(tool in command_lower for tool in ["mvn", "gradle", "ant"]):
            return "build_tool"
        elif any(pkg in command_lower for pkg in ["apt", "yum", "dnf", "pip", "npm", "yarn"]):
            return "package_manager"
        elif any(git in command_lower for git in ["git ", "git-"]):
            return "version_control"
        elif any(test in command_lower for test in ["test", "pytest", "jest", "mocha"]):
            return "testing"
        elif any(
            file_op in command_lower for file_op in ["ls", "find", "grep", "cat", "head", "tail"]
        ):
            return "file_operation"
        elif any(sys in command_lower for sys in ["ps", "top", "df", "du", "free"]):
            return "system_info"
        elif "cd " in command_lower or "pwd" in command_lower:
            return "navigation"
        elif any(net in command_lower for net in ["curl", "wget", "ping", "netstat"]):
            return "network"
        else:
            return "general"

    def _extract_values(self, output: str, command: str) -> dict:
        """Extract specific values from output based on command type."""
        values = {}

        # Extract file paths
        file_paths = re.findall(r"/[\w/.-]+", output)
        if file_paths:
            values["file_paths"] = list(set(file_paths))[:10]  # Limit to 10 unique paths

        # Extract URLs
        urls = re.findall(r"https?://[^\s]+", output)
        if urls:
            values["urls"] = list(set(urls))[:5]

        # Extract version numbers
        versions = re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", output)
        if versions:
            values["versions"] = list(set(versions))[:5]

        # Extract IP addresses
        ips = re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", output)
        if ips:
            values["ip_addresses"] = list(set(ips))[:5]

        # Extract numbers (counts, sizes, etc.)
        if "ls" in command or "find" in command or "grep -c" in command:
            lines = output.strip().split("\n")
            if lines and lines[0]:
                values["count"] = len(lines)

        # Extract process IDs
        pids = re.findall(r"\bPID[:\s]+(\d+)", output)
        if pids:
            values["pids"] = pids[:5]

        return values

    def _analyze_error_output(self, output: str, command: str) -> dict:
        """Analyze error output to determine specific failure type."""
        analysis = {
            "error_type": "general",
            "error_code": "COMMAND_FAILED",
            "key_errors": [],
            "compilation_errors": [],
            "test_failures": [],
            "error_count": 0,
            "test_stats": {},
        }

        output_lower = output.lower()
        lines = output.split("\n")

        # Java Compilation errors (HIGH PRIORITY - check first)
        if (
            "unmappable character for encoding" in output_lower
            or "compilation failed" in output_lower
        ):
            analysis["error_type"] = "compilation_error"
            analysis["error_code"] = "COMPILATION_FAILED"
            # Count compilation errors
            for line in lines:
                if "unmappable character" in line.lower() or "error:" in line:
                    analysis["compilation_errors"].append(line.strip())
                    if "error:" in line:
                        analysis["error_count"] += 1
            # Limit stored errors to prevent bloat
            analysis["compilation_errors"] = analysis["compilation_errors"][:20]

        # Build failures (Maven/Gradle)
        elif "build failed" in output_lower or "build failure" in output_lower:
            analysis["error_type"] = "build_failed"
            analysis["error_code"] = "BUILD_FAILED"
            # Look for specific build error patterns
            for line in lines:
                if "BUILD FAILED" in line or "BUILD FAILURE" in line:
                    analysis["key_errors"].append(line.strip())
                elif "compilation error" in line.lower():
                    analysis["compilation_errors"].append(line.strip())
                    analysis["error_count"] += 1

        # Test failures
        elif (
            "tests failed" in output_lower
            or "test failures" in output_lower
            or "failures: " in output_lower
        ):
            analysis["error_type"] = "test_failed"
            analysis["error_code"] = "TEST_FAILED"
            # Extract test statistics
            for line in lines:
                # JUnit pattern: "Tests run: X, Failures: Y, Errors: Z, Skipped: W"
                if "tests run:" in line.lower():
                    import re

                    stats_match = re.search(
                        r"tests?\s+run:\s*(\d+).*?failures?:\s*(\d+).*?errors?:\s*(\d+)",
                        line,
                        re.IGNORECASE,
                    )
                    if stats_match:
                        analysis["test_stats"] = {
                            "total": int(stats_match.group(1)),
                            "failures": int(stats_match.group(2)),
                            "errors": int(stats_match.group(3)),
                        }
                # Gradle pattern: "X tests completed, Y failed"
                elif "tests completed" in line.lower():
                    stats_match = re.search(
                        r"(\d+)\s+tests?\s+completed.*?(\d+)\s+failed", line, re.IGNORECASE
                    )
                    if stats_match:
                        analysis["test_stats"] = {
                            "total": int(stats_match.group(1)),
                            "failures": int(stats_match.group(2)),
                            "errors": 0,
                        }
                # Capture failed test names
                if "FAILED" in line and ("Test" in line or "test" in line):
                    analysis["test_failures"].append(line.strip())
            # Limit test failures stored
            analysis["test_failures"] = analysis["test_failures"][:15]

        # Permission errors
        elif "permission denied" in output_lower or "access denied" in output_lower:
            analysis["error_type"] = "permission"
            analysis["error_code"] = "PERMISSION_DENIED"

        # File/directory not found
        elif "no such file or directory" in output_lower or "cannot find" in output_lower:
            analysis["error_type"] = "not_found"
            analysis["error_code"] = "FILE_NOT_FOUND"

        # Command not found
        elif "command not found" in output_lower or "not found" in output_lower:
            analysis["error_type"] = "command_not_found"
            analysis["error_code"] = "COMMAND_NOT_FOUND"

        # Network errors
        elif any(
            net_err in output_lower
            for net_err in ["connection refused", "connection timeout", "unreachable"]
        ):
            analysis["error_type"] = "network"
            analysis["error_code"] = "NETWORK_ERROR"

        # Disk space errors
        elif "no space left" in output_lower or "disk full" in output_lower:
            analysis["error_type"] = "disk_space"
            analysis["error_code"] = "DISK_FULL"

        # Package manager errors
        elif "unable to locate package" in output_lower or "no matching packages" in output_lower:
            analysis["error_type"] = "package_not_found"
            analysis["error_code"] = "PACKAGE_NOT_FOUND"

        # Extract general error lines if not already captured
        if not analysis["key_errors"] and not analysis["compilation_errors"]:
            error_lines = [
                line for line in lines if "error" in line.lower() or "fail" in line.lower()
            ]
            analysis["key_errors"] = error_lines[:10]  # Increased limit for better diagnostics

        return analysis

    def _generate_error_suggestions(
        self, error_analysis: dict, command: str, exit_code: int
    ) -> list:
        """Project bounded observations from a failed shell command.

        The shell runner reports what failed and relevant constraints.  It
        does not author the next build, package, diagnostic, or repair call.
        """
        observations = []
        error_type = error_analysis.get("error_type", "general")

        # A failing hand-rolled mvn/gradle invocation is almost always the
        # wrong path or the wrong (stale PATH) version — the build tool
        # resolves the registered toolchain automatically. Round-4 eval: the
        # model retried './bin/mvn' 50x instead of using build(action='test').
        command_head = command.split("&&")[-1].strip().split()
        if command_head and any(
            tok in ("mvn", "gradle")
            or tok.endswith("/mvn")
            or tok.endswith("/gradle")
            or tok.endswith("mvnw")
            or tok.endswith("gradlew")
            for tok in command_head[:1]
        ):
            observations.append(
                "Observed boundary: the failed command invoked a build runner through bash; "
                "the public build affordance owns registered toolchain resolution"
            )

        if error_type == "compilation_error":
            error_count = error_analysis.get("error_count", 0)
            observations.append(f"Observed category: compilation failed with {error_count} errors")

        elif error_type == "build_failed":
            observations.append("Observed category: build command failed")

        elif error_type == "test_failed":
            test_stats = error_analysis.get("test_stats", {})
            if test_stats:
                total = test_stats.get("total", 0)
                failures = test_stats.get("failures", 0)
                errors = test_stats.get("errors", 0)
                observations.append(
                    f"Tests failed: {failures} failures, {errors} errors out of {total} tests"
                )
            else:
                observations.append("Observed category: test command failed")

        elif error_type == "permission":
            observations.extend(
                [
                    "Observed category: permission denied",
                    "Constraint: the requested operation requires access to its target",
                ]
            )
        elif error_type == "not_found":
            observations.extend(
                [
                    "Observed category: requested path was not found",
                    "Relevant facts: submitted path and working directory",
                ]
            )
        elif error_type == "command_not_found":
            cmd_name = command.split()[0] if command else "command"
            observations.extend(
                [
                    f"Observed capability: executable {cmd_name!r} was not found",
                    "Relevant constraints: executable identity and active PATH",
                ]
            )
        elif error_type == "network":
            observations.extend(
                [
                    "Observed category: network operation failed",
                    "Relevant constraints: target reachability, DNS, proxy, and firewall policy",
                ]
            )
        elif error_type == "disk_space":
            observations.extend(
                [
                    "Observed category: insufficient disk space",
                    "Constraint: the operation requires additional writable capacity",
                ]
            )
        elif error_type == "package_not_found":
            observations.extend(
                [
                    "Observed category: requested package was not found",
                    "Relevant constraints: package identity and configured repository inventory",
                ]
            )
        else:
            observations.append(f"Observed exit code: {exit_code}")

        return observations

    def _get_recovery_commands(self, error_analysis: dict) -> list:
        """Compatibility field: the evidence layer no longer composes commands."""
        del error_analysis
        return []

    def _get_command_timeout(self, command: str) -> tuple[int, int]:
        """
        Get appropriate timeouts based on command type.
        Returns (silent_timeout, absolute_timeout) in seconds.
        """
        command_lower = command.lower()

        # Maven/Gradle builds - need very long timeouts, especially for first runs
        if any(tool in command_lower for tool in ["mvn", "gradle", "./gradlew"]):
            # Check for specific Maven/Gradle operations
            if any(op in command_lower for op in ["clean install", "clean test", "build"]):
                # Full builds with tests can take very long, especially with dependency downloads
                return (1800, 7200)  # 30 min silent, 120 min total
            elif "compile" in command_lower:
                # Compilation with dependency downloads
                return (1200, 3600)  # 20 min silent, 60 min total
            elif "package" in command_lower:
                # Packaging can involve downloads
                return (1200, 3600)  # 20 min silent, 60 min total
            elif "test" in command_lower:
                # Tests can be long running
                return (1200, 3600)  # 20 min silent, 60 min total
            else:
                # Default for Maven/Gradle (includes dependency resolution)
                return (900, 2400)  # 15 min silent, 40 min total

        # NPM/Node operations
        elif "npm" in command_lower or "yarn" in command_lower:
            if "install" in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            elif "build" in command_lower or "test" in command_lower:
                return (300, 1200)  # 5 min silent, 20 min total
            else:
                return (180, 600)  # 3 min silent, 10 min total

        # Python operations
        elif "pip" in command_lower or "pytest" in command_lower:
            if "install" in command_lower:
                return (180, 600)  # 3 min silent, 10 min total
            elif "test" in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            else:
                return (120, 300)  # 2 min silent, 5 min total

        # Git operations
        elif "git" in command_lower:
            if "clone" in command_lower:
                # Large repos can take time
                return (300, 1200)  # 5 min silent, 20 min total
            else:
                return (60, 180)  # 1 min silent, 3 min total

        # Docker operations
        elif "docker" in command_lower:
            if "build" in command_lower:
                return (600, 1800)  # 10 min silent, 30 min total
            elif "pull" in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            else:
                return (60, 300)  # 1 min silent, 5 min total

        # Package installation
        elif any(pkg in command_lower for pkg in ["apt-get install", "apt install", "yum install"]):
            return (300, 900)  # 5 min silent, 15 min total

        # Make/CMake operations
        elif "make" in command_lower or "cmake" in command_lower:
            return (300, 1200)  # 5 min silent, 20 min total

        # Default for unknown long-running commands
        elif self._is_long_running_command(command):
            return (300, 900)  # 5 min silent, 15 min total

        # Default for regular commands
        else:
            return (60, 300)  # 1 min silent, 5 min total

    def _enhance_grep_command(self, command: str) -> str:
        """Enhance grep commands with helpful default flags."""
        if not command.strip().startswith("grep"):
            return command

        # Parse the command to avoid double-adding flags
        parts = shlex.split(command)
        if len(parts) < 2:
            return command

        # Check if useful flags are already present
        has_recursive = "-r" in parts or "--recursive" in parts
        has_line_numbers = "-n" in parts or "--line-number" in parts
        has_color = "--color" in parts

        enhanced_parts = [parts[0]]  # Start with 'grep'

        # Add helpful flags if not present
        if not has_line_numbers:
            enhanced_parts.append("-n")
        if not has_color:
            enhanced_parts.append("--color=always")

        # Add the rest of the command
        enhanced_parts.extend(parts[1:])

        enhanced_command = " ".join(
            shlex.quote(part) if " " in part else part for part in enhanced_parts
        )

        # If the command seems to be searching in current directory without -r, suggest it
        if not has_recursive and (
            enhanced_command.endswith(" .") or enhanced_command.endswith(" ./")
        ):
            logger.info("💡 TIP: Consider using 'grep -r' for recursive search in directories")

        return enhanced_command

    def get_grep_examples(self) -> str:
        """Get comprehensive grep usage examples."""
        return """
GREP INVESTIGATION EXAMPLES:

🔍 BASIC SEARCHES:
• Find function: grep -rn "def my_function" .
• Find class: grep -rn "class MyClass" .
• Find imports: grep -rn "import requests" .
• Case insensitive: grep -rni "error" .

🎯 PATTERN MATCHING:
• Multiple patterns: grep -rn "error\\|exception\\|fail" .
• Regex pattern: grep -rn "def [a-z_]*test" .
• Whole words: grep -rnw "test" .
• Start of line: grep -rn "^class " .

📁 FILE FILTERING:
• Python files only: grep -rn "pattern" . --include="*.py"
• Exclude logs: grep -rn "pattern" . --exclude="*.log"
• Exclude directories: grep -rn "pattern" . --exclude-dir=".git"

🔬 CONTEXT & DETAILS:
• Show context: grep -rn -C 3 "error" .  (3 lines before/after)
• Count matches: grep -rc "pattern" .
• List files only: grep -rl "pattern" .
• Invert match: grep -rnv "pattern" .

🚀 ADVANCED INVESTIGATIONS:
• Find all APIs: grep -rn "@app\\|@route\\|def.*api" . --include="*.py"
• Database queries: grep -rni "select\\|insert\\|update\\|delete" .
• Configuration: grep -rn "config\\|setting\\|env" . --exclude-dir=".git"
• Error handling: grep -rn "try:\\|except\\|raise" . --include="*.py" -A 2
• Find TODOs: grep -rn "TODO\\|FIXME\\|HACK" .

💡 PRO TIPS:
• Use -C 2 to see context around matches
• Combine with head/tail: grep -rn "pattern" . | head -20
• Use --color=always for better readability
• Save results: grep -rn "pattern" . > search_results.txt
        """

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute. For grep investigations, use patterns like: "
                    "'grep -rn \"pattern\" .' for recursive search, "
                    '\'grep -rni "error\\|exception" . --include="*.py"\' for specific file types, '
                    "'grep -rn -C 3 \"function_name\" .' for context around matches. "
                    "See get_grep_examples() for comprehensive patterns.",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Maximum total execution time in seconds (default: 60). "
                        "Use a larger value for long-running commands such as installs, builds, or tests."
                    ),
                    "default": 60,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Working directory for command execution (default: /workspace)",
                    "default": "/workspace",
                },
            },
            "required": ["command"],
        }

    def _detect_test_output(self, command: str, output: str) -> Optional[Dict[str, Any]]:
        """Detect and parse test output from various test runners."""
        if not output:
            return None

        # Check if this is a test command
        test_indicators = [
            "pytest",
            "jest",
            "npm test",
            "yarn test",
            "go test",
            "cargo test",
            "phpunit",
            "rspec",
        ]
        is_test_command = any(indicator in command.lower() for indicator in test_indicators)
        if not is_test_command:
            return None

        test_data = {}

        # Pytest detection
        if "pytest" in command or "= test session starts =" in output:
            # Pattern: "5 passed, 2 failed, 1 error in 10.5s"
            match = re.search(
                r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) error)?(?:, (\d+) skipped)?", output
            )
            if match:
                passed = int(match.group(1) or 0)
                failed = int(match.group(2) or 0)
                errors = int(match.group(3) or 0)
                skipped = int(match.group(4) or 0)
                test_data = {
                    "tool": "pytest",
                    "tests": {
                        "total": passed + failed + errors + skipped,
                        "passed": passed,
                        "failed": failed,
                        "error": errors,
                        "skipped": skipped,
                    },
                }

        # Jest/npm test detection
        elif "jest" in command or "npm test" in command or "Tests:" in output:
            # Pattern: "Tests:       5 passed, 2 failed, 7 total"
            match = re.search(
                r"Tests:\s+(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?, (\d+) total", output
            )
            if match:
                passed = int(match.group(1) or 0)
                failed = int(match.group(2) or 0)
                skipped = int(match.group(3) or 0)
                total = int(match.group(4) or 0)
                test_data = {
                    "tool": "jest",
                    "tests": {
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "error": 0,
                        "skipped": skipped,
                    },
                }

        # Go test detection
        elif "go test" in command:
            # Pattern: "PASS" or "FAIL" at the end, "ok  \tpackage\t0.123s"
            passed = len(re.findall(r"^PASS", output, re.MULTILINE))
            failed = len(re.findall(r"^FAIL", output, re.MULTILINE))
            if passed > 0 or failed > 0:
                test_data = {
                    "tool": "go",
                    "tests": {
                        "total": passed + failed,
                        "passed": passed,
                        "failed": failed,
                        "error": 0,
                        "skipped": 0,
                    },
                }

        # PHPUnit detection
        elif "phpunit" in command.lower():
            # Pattern: "OK (5 tests, 10 assertions)" or "FAILURES! Tests: 5, Assertions: 10, Failures: 2."
            match = re.search(
                r"(?:OK \((\d+) tests?|Tests: (\d+)).*?(?:Failures: (\d+))?(?:.*?Errors: (\d+))?",
                output,
            )
            if match:
                total = int(match.group(1) or match.group(2) or 0)
                failures = int(match.group(3) or 0)
                errors = int(match.group(4) or 0)
                test_data = {
                    "tool": "phpunit",
                    "tests": {
                        "total": total,
                        "passed": total - failures - errors,
                        "failed": failures,
                        "error": errors,
                        "skipped": 0,
                    },
                }

        return test_data if test_data else None

    def _write_test_telemetry(
        self, command: str, working_directory: str, exit_code: int, test_data: Dict[str, Any]
    ) -> None:
        """Write test execution telemetry to JSONL file."""
        if not self.docker_orchestrator or not test_data:
            return

        try:
            # Create telemetry entry following the schema
            entry = {
                "event": "test_session_end",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tool": test_data.get("tool", "unknown"),
                "command": command,
                "working_directory": working_directory,
                "exit_code": exit_code,
                "tests": test_data.get("tests", {}),
            }

            # Write to telemetry file
            metrics_dir = "/workspace/.setup_agent/metrics"
            summary_path = f"{metrics_dir}/test_summary.jsonl"

            self.docker_orchestrator.execute_command(f"mkdir -p {metrics_dir}")
            payload = json.dumps(entry, sort_keys=True)
            append_cmd = f"cat >> {summary_path} <<'EOF'\n{payload}\nEOF"
            self.docker_orchestrator.execute_command(append_cmd)

            logger.debug(f"Recorded test telemetry for {test_data.get('tool')} to {summary_path}")

        except Exception as exc:
            logger.warning(f"Failed to write test telemetry: {exc}")

    def get_usage_example(self) -> str:
        """Get usage examples focused on grep investigations."""
        return f"""
{self.name}(command="grep -rn 'def process_data' . --include='*.py'")  # Find function definitions
{self.name}(command="grep -rni 'error|exception' . --include='*.py' -C 2")  # Find error handling with context
{self.name}(command="grep -rn 'import pandas' .")  # Find specific imports
{self.name}(command="ls -la", timeout=30, working_directory="/workspace")  # Standard file operations
{self.name}(command="npm install", timeout=900, working_directory="/workspace/project")  # Long-running command
{self.name}(command="git status", timeout=30, working_directory="/workspace/project")  # Git operations

💡 For comprehensive grep patterns, use: get_grep_examples()
        """
