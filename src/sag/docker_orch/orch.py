"""Docker Orchestrator for managing containers and volumes."""

import os
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound
from loguru import logger

from sag.config import get_config
from sag.runtime.exec_env import DEFAULT_UTF8_ENVIRONMENT, default_utf8_environment


ENV_OVERLAY_SCRIPT_PATH = "/workspace/.setup_agent/env_overlay.sh"
UNKNOWN_EXIT_FAILURE_MARKERS = (
    "BUILD FAILURE",
    "BUILD FAILED",
    "Compilation failure",
    "[ERROR] Could not resolve",
)
MAVEN_ENFORCER_VERSION_RANGE_MARKERS = (
    "Detected Maven Version:",
    "is not in the allowed range",
)


def _has_unknown_exit_failure_marker(output: str) -> bool:
    """Return True when unknown-exit output contains an explicit terminal failure marker."""
    normalized_output = output.casefold()
    if any(marker.casefold() in normalized_output for marker in UNKNOWN_EXIT_FAILURE_MARKERS):
        return True

    return all(
        marker.casefold() in normalized_output
        for marker in MAVEN_ENFORCER_VERSION_RANGE_MARKERS
    )


class DockerOrchestrator:
    """Orchestrates Docker containers for project setup."""

    def __init__(self, base_image: str = None, project_name: str = None):
        self.config = get_config()
        self.base_image = base_image or self.config.docker_base_image
        self.project_name = project_name

        # Docker client
        try:
            self.client = docker.from_env()
            self.client.ping()  # Test connection
            logger.info("Docker client initialized successfully")
        except DockerException as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            raise

        # Container names (SAG naming convention)
        if self.project_name:
            self.container_name = f"sag-{self.project_name}"
            self.volume_name = f"sag-{self.project_name}-vol"
        else:
            self.container_name = "sag-default"
            self.volume_name = "sag-default-vol"

        logger.info(f"Docker Orchestrator initialized for project: {project_name}")

    def create_and_start_container(self) -> bool:
        """Create and start a new container for the project."""

        if not self.project_name:
            raise ValueError("Project name is required to create container")

        try:
            # Check if container already exists
            if self.container_exists():
                logger.info(f"Container {self.container_name} already exists")
                if not self.is_container_running():
                    logger.info("Starting existing container")
                    return self.start_container()
                return True

            # Create volume if it doesn't exist
            # Skip volume creation - we're not using volumes anymore
            # if not self._volume_exists():
            #     self._create_volume()

            # Ensure the base image is available locally
            if not self._ensure_image_available():
                logger.error(f"Failed to ensure image {self.base_image} is available")
                return False

            # Prepare container configuration
            container_config = self._get_container_config()

            logger.info(f"Creating container {self.container_name} with image {self.base_image}")

            # Create container
            container = self.client.containers.create(
                image=self.base_image, name=self.container_name, **container_config
            )

            # Start container
            container.start()

            # Wait for container to be ready
            if self._wait_for_container_ready():
                logger.info(f"Container {self.container_name} created and started successfully")

                # Setup the container environment
                self._setup_container_environment()

                return True
            else:
                logger.error("Container failed to become ready")
                return False

        except Exception as e:
            logger.error(f"Failed to create and start container: {e}")
            return False

    def start_container(self) -> bool:
        """Start an existing container."""

        try:
            container = self.client.containers.get(self.container_name)

            if container.status == "running":
                logger.info(f"Container {self.container_name} is already running")
                return True

            logger.info(f"Starting container {self.container_name}")
            container.start()

            if self._wait_for_container_ready():
                logger.info(f"Container {self.container_name} started successfully")
                return True
            else:
                logger.error("Container failed to become ready after start")
                return False

        except NotFound:
            logger.error(f"Container {self.container_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to start container: {e}")
            return False

    def stop_container(self) -> bool:
        """Stop the container."""

        try:
            container = self.client.containers.get(self.container_name)

            if container.status != "running":
                logger.info(f"Container {self.container_name} is not running")
                return True

            logger.info(f"Stopping container {self.container_name}")
            container.stop(timeout=30)

            logger.info(f"Container {self.container_name} stopped successfully")
            return True

        except NotFound:
            logger.error(f"Container {self.container_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    def remove_project(self) -> bool:
        """Remove the project container and volume."""

        try:
            success = True

            # Stop and remove container
            if self.container_exists():
                container = self.client.containers.get(self.container_name)

                if container.status == "running":
                    logger.info(f"Stopping container {self.container_name}")
                    container.stop(timeout=30)

                logger.info(f"Removing container {self.container_name}")
                container.remove()

            # Remove volume
            # Skip volume removal - we're not using volumes anymore
            # if self._volume_exists():
            #     logger.info(f"Removing volume {self.volume_name}")
            #     volume = self.client.volumes.get(self.volume_name)
            #     volume.remove()

            logger.info(f"Project {self.project_name} removed successfully")
            return success

        except Exception as e:
            logger.error(f"Failed to remove project: {e}")
            return False

    def container_exists(self) -> bool:
        """Check if the container exists."""

        try:
            self.client.containers.get(self.container_name)
            return True
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking container existence: {e}")
            return False

    def is_container_running(self) -> bool:
        """Check if the container is running."""

        try:
            container = self.client.containers.get(self.container_name)
            return container.status == "running"
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking container status: {e}")
            return False

    def connect_to_container(self, shell: str = "/bin/bash") -> None:
        """Connect to the container interactively."""
        import subprocess
        import sys

        if not self.is_container_running():
            raise RuntimeError(f"Container {self.container_name} is not running")

        # Check if we're in an interactive terminal
        is_tty = sys.stdin.isatty()
        env_args = [
            arg
            for key, value in DEFAULT_UTF8_ENVIRONMENT.items()
            for arg in ("-e", f"{key}={value}")
        ]

        if is_tty:
            # Use docker exec with -it for interactive terminal
            cmd = ["docker", "exec", *env_args, "-it", self.container_name, shell]
        else:
            # Use docker exec without -it for non-interactive (piped input)
            cmd = ["docker", "exec", *env_args, "-i", self.container_name, shell]

        logger.info(f"Connecting to container with command: {' '.join(cmd)}")
        logger.info(f"TTY mode: {is_tty}")

        try:
            # Use subprocess.call for better compatibility
            # This preserves the current process and handles TTY correctly
            result = subprocess.call(cmd)
            if result != 0:
                logger.error(f"Docker exec returned non-zero exit code: {result}")
                raise RuntimeError(f"Failed to connect to container (exit code: {result})")
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            logger.info("Container connection interrupted by user")
            return
        except Exception as e:
            logger.error(f"Failed to connect to container: {e}")
            raise

    def _runtime_profile_prefix(self) -> str:
        """Return shell sources needed before running commands in the container."""
        return (
            "export LANG=${LANG:-C.UTF-8}; "
            "export LC_ALL=${LC_ALL:-C.UTF-8}; "
            f"source {ENV_OVERLAY_SCRIPT_PATH} 2>/dev/null || true; "
            "source /etc/profile 2>/dev/null || true; "
            "source ~/.bashrc 2>/dev/null || true"
        )

    def _default_exec_environment(
        self, environment: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Merge caller-provided env with SAG's safe UTF-8 execution defaults."""
        return default_utf8_environment(environment)

    def _is_json_content(self, output: str, command: str) -> bool:
        """
        检测是否为JSON内容，避免对JSON文件进行破坏性截断
        """
        # 如果command包含.json文件路径
        if ".json" in command and ("cat" in command or "head" in command or "tail" in command):
            return True

        # 如果输出内容看起来像JSON结构
        stripped = output.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                import json

                json.loads(stripped)  # 验证是否为有效JSON
                return True
            except json.JSONDecodeError:
                pass

        return False

    def _is_xml_content(self, output: str, command: str) -> bool:
        """
        Detect if content is XML/POM file to avoid destructive truncation
        """
        # Check if command is reading a POM or XML file
        if (".xml" in command or "pom.xml" in command) and (
            "cat" in command or "head" in command or "tail" in command
        ):
            return True

        # Check if output looks like XML
        stripped = output.strip()
        if stripped.startswith("<?xml") or stripped.startswith("<project"):
            return True

        return False

    def _smart_xml_truncate(self, xml_content: str, max_lines: int = 150) -> str:
        """
        Smart truncation for XML/POM files that preserves error-prone sections
        """
        lines = xml_content.split("\n")

        if len(lines) <= max_lines:
            return xml_content

        # For POM files, try to preserve important sections
        important_sections = []
        error_prone_sections = []

        # Look for potential problem areas in the XML
        # First, find complete properties sections
        properties_sections = []
        i = 0
        while i < len(lines):
            if "<properties>" in lines[i]:
                start_idx = i
                # Find the matching closing tag
                for j in range(i + 1, min(i + 50, len(lines))):  # Look up to 50 lines ahead
                    if "</properties>" in lines[j]:
                        # Include a few lines before and after for context
                        properties_sections.append((max(0, start_idx - 2), min(len(lines), j + 3)))
                        i = j
                        break
            i += 1

        # Now look for other important tags
        for i, line in enumerate(lines):
            # Check for orphaned tags or malformed XML patterns
            stripped = line.strip()
            # Common problematic patterns
            if any(
                tag in stripped
                for tag in [
                    "<groupId>",
                    "<artifactId>",
                    "<version>",
                    "<dependency>",
                    "</dependency>",
                ]
            ):
                # Get context around these tags (5 lines before and after)
                start = max(0, i - 5)
                end = min(len(lines), i + 6)
                error_prone_sections.append((start, end))

        # Combine properties sections with other error-prone sections
        # Keep properties sections separate for now to prevent merging

        # Merge overlapping sections (but not properties sections)
        if error_prone_sections or properties_sections:
            merged = []

            # First merge non-properties sections
            if error_prone_sections:
                error_prone_sections.sort()
                current_start, current_end = error_prone_sections[0]

                for start, end in error_prone_sections[1:]:
                    if start <= current_end:
                        current_end = max(current_end, end)
                    else:
                        merged.append((current_start, current_end))
                        current_start, current_end = start, end
                merged.append((current_start, current_end))

            # Add all properties sections without merging (they're critical for Java version detection)
            merged.extend(properties_sections)

            # Sort the final list
            merged.sort()

            # Build truncated output preserving error-prone sections
            result = []
            result.extend(lines[:30])  # First 30 lines (header, organization info)
            result.append(
                f"\n... [SMART XML TRUNCATION: Preserving error-prone sections including properties] ...\n"
            )

            for start, end in merged:
                # Skip sections that are already included in the first 30 or last 20 lines
                if end <= 30 or start >= len(lines) - 20:
                    continue
                # Include the section, adjusting for overlap with already included lines
                actual_start = max(start, 30)  # Don't duplicate lines already in first 30
                actual_end = min(end, len(lines) - 20)  # Don't duplicate lines in last 20
                if actual_start < actual_end:
                    result.append(f"... [Lines {actual_start+1}-{actual_end}] ...")
                    result.extend(lines[actual_start:actual_end])

            result.append(f"\n... [End of error-prone sections] ...\n")
            result.extend(lines[-20:])  # Last 20 lines

            logger.info(
                f"🔧 Applied XML-aware truncation: {len(lines)} lines → {len(result)} lines (preserved error-prone sections)"
            )
            return "\n".join(result)
        else:
            # Fallback to standard truncation if no error-prone sections found
            truncated = (
                "\n".join(lines[:50])
                + f"\n... [XML TRUNCATED: {len(lines)} total lines] ...\n"
                + "\n".join(lines[-50:])
            )
            logger.info(f"🔧 Applied XML truncation: {len(lines)} lines → 100 lines")
            return truncated

    def _smart_json_truncate(self, json_content: str, max_entries: int = 10) -> str:
        """
        智能截断JSON内容，保持JSON有效性
        主要针对context history文件进行安全压缩
        """
        try:
            import json

            data = json.loads(json_content)

            # 如果是branch context history，可以安全截断history数组
            if isinstance(data, dict) and "history" in data and isinstance(data["history"], list):
                history = data["history"]
                if len(history) > max_entries:
                    # 保留前5个和后5个history条目，中间标记截断
                    truncated_count = len(history) - max_entries
                    data["history"] = (
                        history[:5]
                        + [
                            {
                                "type": "truncated",
                                "message": f"[SMART TRUNCATION: {truncated_count} entries omitted to prevent context pollution]",
                                "timestamp": "system",
                            }
                        ]
                        + history[-5:]
                    )
                    # 更新元数据
                    data["entry_count"] = len(data["history"])
                    # 重新计算token count
                    if "token_count" in data:
                        data["token_count"] = len(json.dumps(data)) // 4

                    logger.info(
                        f"📊 Applied smart JSON truncation: {len(history)} → {len(data['history'])} entries"
                    )
                    return json.dumps(data, indent=2)

            # Not the {history:[...]} shape we can trim — return unchanged. Debug,
            # not warning: no data is lost and it fires on every large trunk*.json
            # dashboard read (log spam).
            logger.debug(
                "Large JSON file left untruncated (unrecognized shape) - preserving integrity"
            )
            return json_content

        except json.JSONDecodeError:
            # 如果不是有效JSON，返回原内容
            return json_content

    def execute_command(
        self,
        command: str,
        workdir: Optional[str] = None,
        capture_stderr: bool = True,
        environment: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        truncate_output: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a command in the container.

        Args:
            command: The command to execute.
            workdir: The working directory to execute the command in.
            capture_stderr: Whether to capture stderr separately.
            environment: Additional environment variables.
            timeout: Optional maximum total execution time in seconds.
            truncate_output: When True (default) large output is truncated to
                protect the agent's context window. Set False only when the
                caller needs the complete output for durable storage (e.g.
                persisting a finished detached build log to the output store);
                the full log is never fed straight into the model context.

        Returns:
            A dictionary with the result of the command execution.
        """
        # Ensure container is running and get container object
        if not self.is_container_running():
            if not self.container_exists():
                raise RuntimeError(
                    f"Container {self.container_name} does not exist. Create it first."
                )
            if not self.start_container():
                raise RuntimeError(f"Failed to start container {self.container_name}")

        # Get the container object
        container = self.client.containers.get(self.container_name)

        # Build the command to be executed in the container with proper environment loading
        # Source profile to ensure all environment variables (JAVA_HOME, M2_HOME, PATH) are loaded
        # CRITICAL FIX: Source environment files BEFORE changing directory
        # This prevents the source commands from resetting the working directory
        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            # Source environment first, THEN change to the specified working directory
            quoted_workdir = shlex.quote(workdir)
            wrapped_command = f"{runtime_profile_prefix}; cd {quoted_workdir} && {command}"
        else:
            # No working directory specified, use default behavior
            wrapped_command = f"{runtime_profile_prefix}; {command}"
        timeout_seconds = int(timeout) if timeout is not None else None
        if timeout_seconds is not None and timeout_seconds > 0:
            escaped_command = shlex.quote(wrapped_command)
            wrapped_command = (
                f"timeout --preserve-status {timeout_seconds} bash -c {escaped_command}"
            )
        exec_command = ["/bin/bash", "-c", wrapped_command]

        logger.info(f"Executing command in container: {command}")
        if workdir:
            logger.info(f"Working directory: {workdir}")

        try:
            # Prepare environment
            exec_env = self._default_exec_environment(environment)

            # Execute the command with stderr capture
            # Use demux to separate stdout and stderr when requested
            # NOTE: We don't use Docker's workdir parameter here because we handle it
            # explicitly with cd in the bash command for better reliability
            result = container.exec_run(
                exec_command,
                workdir=None,  # Handled by cd command in bash
                stderr=True,  # Explicitly capture stderr
                stdout=True,  # Explicitly capture stdout
                demux=capture_stderr,  # Separate stdout/stderr when True
                environment=exec_env,
            )

            # Handle output based on whether demux was used
            if capture_stderr and isinstance(result.output, tuple):
                # demux=True returns (stdout, stderr)
                stdout, stderr = result.output
                stdout_str = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
                stderr_str = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
                # Combine for backward compatibility
                output = (stdout_str + "\n" + stderr_str).strip() if stderr_str else stdout_str
                exit_code = result.exit_code
            else:
                # demux=False returns combined output
                output = (
                    result.output.decode("utf-8", errors="replace").strip() if result.output else ""
                )
                stdout_str = output
                stderr_str = ""
                exit_code = result.exit_code

            logger.debug(f"Command finished with exit code: {exit_code}")

            # IMPROVED: Content-aware truncation logic
            original_length = len(output)
            if truncate_output and original_length > 10000:  # ~100 lines threshold
                lines = output.split("\n")
                if len(lines) > 100:
                    # Check if this is JSON content that needs protection
                    if self._is_json_content(output, command):
                        # For JSON files, apply smart truncation that preserves validity
                        output = self._smart_json_truncate(output, max_entries=10)
                        logger.info(f"🔧 Applied JSON-aware truncation to preserve file integrity")
                    # Check if this is XML/POM content that needs special handling
                    elif self._is_xml_content(output, command):
                        # For XML/POM files, apply smart truncation that preserves error-prone sections
                        output = self._smart_xml_truncate(output, max_lines=150)
                        logger.info(
                            f"🔧 Applied XML-aware truncation to preserve error-prone sections"
                        )
                    else:
                        # Apply normal truncation for non-JSON/XML content
                        truncated = (
                            "\n".join(lines[:25])
                            + f"\n... [ORCHESTRATOR TRUNCATED: {len(lines)} lines, {original_length} chars] ...\n"
                            + "\n".join(lines[-25:])
                        )
                        logger.warning(
                            f"🚨 Orchestrator applied emergency truncation: {len(lines)} lines → 50 lines to prevent context pollution"
                        )
                        output = truncated

            # Smart debug logging: show structure of truncated output
            if (
                original_length > 10000 and len(output.split("\n")) <= 60
            ):  # If we applied truncation
                # For truncated output, show the structure more clearly
                output_lines = output.split("\n")
                if len(output_lines) > 10:
                    debug_display = (
                        "\n".join(output_lines[:5])
                        + f"\n... [Truncated output: showing first 5 + last 5 lines of {len(output_lines)} total] ...\n"
                        + "\n".join(output_lines[-5:])
                    )
                else:
                    debug_display = output
                logger.debug(f"Command output (showing truncation structure):\n{debug_display}")
            else:
                # For normal output, use character limit
                debug_output = output[:500] + "..." if len(output) > 500 else output
                logger.debug(f"Command output (truncated for logs):\n{debug_output}")

            # Enhanced success detection for build tools
            # Check for explicit failure markers in addition to exit code
            build_failed = False
            if "mvn" in command or "maven" in command.lower():
                # Maven-specific failure detection
                if "BUILD FAILURE" in output or "[ERROR] BUILD FAILURE" in output:
                    build_failed = True
                    logger.warning("Maven BUILD FAILURE detected despite exit code")
            elif "gradle" in command:
                # Gradle-specific failure detection
                if "BUILD FAILED" in output or "FAILURE: Build failed" in output:
                    build_failed = True
                    logger.warning("Gradle BUILD FAILED detected despite exit code")
            elif "npm" in command:
                # NPM-specific failure detection
                if "npm ERR!" in output or "ERR!" in stderr_str:
                    build_failed = True
                    logger.warning("NPM error detected despite exit code")

            timeout_exit_codes = {124, 137, 143}
            timeout_terminated = (
                timeout_seconds is not None
                and timeout_seconds > 0
                and exit_code in timeout_exit_codes
            )
            termination_reason = "absolute_timeout" if timeout_terminated else None
            monitoring_info = {"execution_time": timeout_seconds} if timeout_terminated else None

            # Determine final success status
            success = (exit_code == 0) and not build_failed and not timeout_terminated

            return {
                "success": success,
                "exit_code": exit_code,
                "output": output,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "signal": None,  # Docker doesn't directly provide signal info
                "build_failed": build_failed,  # Additional flag for build failures
                "termination_reason": termination_reason,
                "monitoring_info": monitoring_info,
                "timeout": timeout_seconds if timeout_seconds and timeout_seconds > 0 else None,
            }
        except Exception as e:
            logger.error(f"Failed to execute command '{command}': {e}")
            return {"success": False, "exit_code": -1, "output": str(e)}

    def execute_command_with_monitoring(
        self,
        command: str,
        workdir: str = None,
        silent_timeout: int = 600,  # 10 minutes no output
        absolute_timeout: int = 2400,  # 40 minutes total
        use_timeout_wrapper: bool = True,
        enable_cpu_monitoring: bool = True,
        optimize_for_maven: bool = True,
    ) -> Dict[str, Any]:
        """
        Enhanced execute_command with comprehensive timeout and monitoring capabilities.

        Args:
            command: Command to execute
            workdir: Working directory
            silent_timeout: Seconds without output before timeout (default: 10 min)
            absolute_timeout: Maximum execution time (default: 30 min)
            use_timeout_wrapper: Whether to wrap command with GNU timeout
            enable_cpu_monitoring: Whether to monitor CPU usage for hang detection
            optimize_for_maven: Whether to apply Maven-specific optimizations
        """

        # Get the container object
        container = self.client.containers.get(self.container_name)

        # Apply Maven optimizations if requested
        # Only optimize if the command actually starts with mvn, not if it's part of a compound command
        if optimize_for_maven and (command.startswith("mvn ") or command == "mvn"):
            command = self._optimize_maven_command(command, absolute_timeout)
            logger.info(f"🔧 Applied Maven optimizations to command")
        elif optimize_for_maven and "&&" in command and "mvn" in command:
            # For compound commands, only optimize the mvn part
            parts = command.split("&&")
            optimized_parts = []
            for part in parts:
                part = part.strip()
                if part.startswith("mvn ") or part == "mvn":
                    optimized_parts.append(self._optimize_maven_command(part, absolute_timeout))
                else:
                    optimized_parts.append(part)
            command = " && ".join(optimized_parts)
            logger.info(f"🔧 Applied Maven optimizations to mvn parts of compound command")

        # Build the command with proper working directory handling
        # CRITICAL FIX: Source environment files BEFORE changing directory
        # This prevents the source commands from resetting the working directory
        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            # Source environment first, THEN change to the specified working directory
            quoted_workdir = shlex.quote(workdir)
            base_cmd = f"{runtime_profile_prefix}; cd {quoted_workdir} && {command}"
        else:
            # No working directory specified, use default
            base_cmd = f"{runtime_profile_prefix}; {command}"

        # Wrap with GNU timeout if requested
        if use_timeout_wrapper:
            # Use timeout with preserve-status to get the actual exit code
            # The entire command (including cd) needs to be wrapped
            # Use double quotes and escape them properly for nested shell execution
            escaped_cmd = base_cmd.replace("'", "'\\''")
            final_command = f"timeout --preserve-status {absolute_timeout} bash -c '{escaped_cmd}'"
            logger.info(f"🕐 Wrapped command with {absolute_timeout}s absolute timeout")
        else:
            final_command = base_cmd

        exec_command = ["/bin/bash", "-c", final_command]

        logger.info(f"Executing command with monitoring: {command}")
        if workdir:
            logger.info(f"Working directory: {workdir}")
        logger.info(f"⏱️ Timeouts: Silent={silent_timeout}s, Absolute={absolute_timeout}s")

        # Monitoring state
        monitoring_state = {
            "last_output_time": time.time(),
            "start_time": time.time(),
            "total_output": "",
            "process_terminated": False,
            "termination_reason": None,
            "cpu_warnings": 0,
            # Liveness-probe fragment for blind enforcement if the stream dies.
            "command_fragment": command[:60],
        }

        try:
            # Start the command execution
            # NOTE: We don't use Docker's workdir parameter here because we handle it
            # explicitly with cd in the bash command for better compatibility with timeout wrapper
            exec_result = container.exec_run(
                exec_command,
                workdir=None,  # Handled by cd command in bash
                stream=True,  # Enable streaming to monitor output
                demux=True,  # Separate stdout/stderr
                environment=self._default_exec_environment(),
            )

            # Start CPU monitoring thread if enabled
            cpu_monitor_thread = None
            if enable_cpu_monitoring:
                cpu_monitor_thread = threading.Thread(
                    target=self._monitor_cpu_usage,
                    args=(
                        monitoring_state,
                        silent_timeout // 2,
                    ),  # Check every half of silent timeout
                    daemon=True,
                )
                cpu_monitor_thread.start()

            # Monitor the execution with timeouts
            result = self._monitor_execution_with_timeouts(
                exec_result, monitoring_state, silent_timeout, absolute_timeout
            )

            # Clean up monitoring thread
            monitoring_state["process_terminated"] = True
            if cpu_monitor_thread:
                cpu_monitor_thread.join(timeout=1)  # Give it 1 second to finish

            return result

        except Exception as e:
            monitoring_state["process_terminated"] = True
            logger.error(f"Failed to execute command '{command}': {e}")
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Execution failed: {str(e)}",
                "termination_reason": "exception",
                "monitoring_info": monitoring_state,
            }

    def _optimize_maven_command(self, command: str, timeout_seconds: int) -> str:
        """Apply Maven-specific optimizations to reduce timeout risks."""
        import re

        optimizations = []

        # Add batch mode and quiet flags if not present
        if "-B" not in command:
            optimizations.append("-B")  # Batch mode (non-interactive)

        # CRITICAL: Don't add -q for commands that run tests as it suppresses test output and reports
        # This was causing test reports to not be generated (issue found 2025-09-13)

        # Check if tests are explicitly skipped
        skip_patterns = [
            r"-DskipTests(?:=true)?(?:\s|$)",
            r"-Dmaven\.test\.skip(?:=true)?(?:\s|$)",
            r"-DskipITs?(?:=true)?(?:\s|$)",
            r"-DskipUTs?(?:=true)?(?:\s|$)",
        ]
        tests_explicitly_skipped = any(re.search(pattern, command) for pattern in skip_patterns)

        # Check if tests are explicitly enabled (overrides skip)
        tests_explicitly_enabled = re.search(r"-DskipTests=false", command) is not None

        # Lifecycle phases that run tests (unless explicitly skipped)
        # Note: test-compile and test-jar don't actually run tests
        test_lifecycle_phases = [
            "test",
            "verify",
            "integration-test",
            "package",
            "install",
            "deploy",
        ]

        # Plugin goals that run tests
        test_plugin_goals = ["surefire:test", "failsafe:integration-test", "failsafe:verify"]

        # Build regex pattern for precise matching
        # Maven goals are typically separated by spaces or are at the start/end of the command
        # We need to ensure we don't match "test" in "test-compile" or "contest"
        lifecycle_pattern = (
            r"(?:^|\s)("
            + "|".join(re.escape(phase) for phase in test_lifecycle_phases)
            + r")(?:\s|$)"
        )
        plugin_pattern = (
            r"(?:^|\s)(" + "|".join(re.escape(goal) for goal in test_plugin_goals) + r")(?:\s|$)"
        )

        # Determine if this command will run tests
        contains_test_phase = re.search(lifecycle_pattern, command) is not None
        contains_test_plugin = re.search(plugin_pattern, command) is not None

        # A command runs tests if:
        # 1. It contains a test-running phase/plugin AND
        # 2. Tests are not explicitly skipped OR tests are explicitly enabled
        will_run_tests = (contains_test_phase or contains_test_plugin) and (
            not tests_explicitly_skipped or tests_explicitly_enabled
        )

        # Don't add -q if tests will run or if debug mode is on
        if "-q" not in command and "-X" not in command and not will_run_tests:
            optimizations.append("-q")  # Quiet mode (reduce output - but NOT for tests!)

        # Add Maven-specific timeout settings
        maven_timeout_props = [
            f"-Dmaven.execution.timeout={timeout_seconds}000",  # Maven timeout in milliseconds
            "-Dmaven.artifact.threads=4",  # Parallel downloads
            "-Dmaven.resolver.transport=wagon",  # Use wagon transport for better reliability
        ]

        # Insert optimizations after 'mvn' but before other arguments
        parts = command.split(" ", 1)
        if len(parts) == 2:
            maven_cmd, remaining_args = parts
            optimized_command = f"{maven_cmd} {' '.join(optimizations)} {' '.join(maven_timeout_props)} {remaining_args}"
        else:
            optimized_command = (
                f"{command} {' '.join(optimizations)} {' '.join(maven_timeout_props)}"
            )

        # Log optimization details, especially for test commands
        if will_run_tests:
            details = []
            if contains_test_phase:
                match = re.search(lifecycle_pattern, command)
                details.append(f"lifecycle phase: {match.group(1)}")
            if contains_test_plugin:
                match = re.search(plugin_pattern, command)
                details.append(f"plugin goal: {match.group(1)}")
            if tests_explicitly_enabled:
                details.append("tests explicitly enabled with -DskipTests=false")
            logger.info(
                f"🧪 Maven TEST command detected ({', '.join(details)}) - preserving output for test reports"
            )
        elif tests_explicitly_skipped:
            logger.info("⏭️ Tests explicitly skipped - applying quiet mode for faster execution")
        logger.info(
            f"🔧 Maven optimizations applied: {', '.join(optimizations + maven_timeout_props)}"
        )
        return optimized_command

    DISPATCH_DIR = "/tmp/sag_jobs"

    def execute_command_detached(
        self,
        command: str,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Start a command detached from the exec stream.

        Output goes to a container log file and the exit code to <log>.exit
        when the command finishes; the process is never killed by a stream or
        socket failure. Returns a handle for poll_detached_command.
        """
        job_id = uuid.uuid4().hex[:12]
        log_path = f"{self.DISPATCH_DIR}/{job_id}.log"
        exit_code_path = f"{log_path}.exit"
        pid_path = f"{self.DISPATCH_DIR}/{job_id}.pid"

        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            inner = f"{runtime_profile_prefix}; cd {shlex.quote(workdir)} && {command}"
        else:
            inner = f"{runtime_profile_prefix}; {command}"
        # Write the exit code atomically (tmp + mv) so a poll can never read a
        # created-but-empty exit file.
        quoted_exit = shlex.quote(exit_code_path)
        quoted_exit_tmp = shlex.quote(exit_code_path + ".tmp")
        wrapped = f"{inner}; echo $? > {quoted_exit_tmp} && mv {quoted_exit_tmp} {quoted_exit}"
        launcher = (
            f"mkdir -p {self.DISPATCH_DIR} && "
            f"(nohup bash -c {shlex.quote(wrapped)} > {shlex.quote(log_path)} 2>&1 & "
            f'pid=$!; printf \'%s\\n\' "$pid" > {shlex.quote(pid_path)}; echo "$pid")'
        )

        result = self.execute_command(
            launcher, workdir=None, environment=environment, timeout=60
        )

        pid: Optional[int] = None
        for token in reversed((result.get("output") or "").split()):
            if token.isdigit():
                pid = int(token)
                break
        started = result.get("exit_code") == 0 and pid is not None

        if started:
            logger.info(f"🚀 Dispatched detached command (pid {pid}, log {log_path}): {command}")
        else:
            logger.error(
                f"Failed to dispatch detached command: {command} "
                f"(exit={result.get('exit_code')}, output={result.get('output', '')[:200]})"
            )

        return {
            "started": started,
            "job_id": job_id,
            "pid": pid,
            "pid_path": pid_path,
            "log_path": log_path,
            "exit_code_path": exit_code_path,
            "command": command,
            "launch_output": result.get("output", ""),
        }

    def detached_handle(self, job_id: str) -> Dict[str, Any]:
        """Reconstruct a stable detached-job handle from its public poll id."""
        if (
            not job_id
            or len(job_id) > 64
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in job_id
            )
        ):
            raise ValueError("invalid detached job id")
        log_path = f"{self.DISPATCH_DIR}/{job_id}.log"
        return {
            "job_id": job_id,
            "pid": None,
            "pid_path": f"{self.DISPATCH_DIR}/{job_id}.pid",
            "log_path": log_path,
            "exit_code_path": f"{log_path}.exit",
        }

    def poll_detached_command(self, handle: Dict[str, Any], tail_lines: int = 40) -> Dict[str, Any]:
        """Poll a detached command: completion state, exit code, and log tail."""
        log_path = shlex.quote(handle["log_path"])
        exit_code_path = shlex.quote(handle["exit_code_path"])
        pid = handle.get("pid")
        if pid:
            pid_assignment = f"pid={int(pid)}; "
        else:
            pid_path = shlex.quote(handle.get("pid_path") or f"{handle['log_path']}.pid")
            pid_assignment = f'pid="$(cat {pid_path} 2>/dev/null)"; '
        probe = pid_assignment + (
            f'if [ -f {exit_code_path} ]; then echo "STATE:EXIT:$(cat {exit_code_path})"; '
            f'elif [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; '
            f'then echo "STATE:RUNNING"; '
            f'else echo "STATE:VANISHED"; fi; '
            f'echo "SIZE:$(wc -c < {log_path} 2>/dev/null || echo 0)"; '
            f'echo "---TAIL---"; tail -n {int(tail_lines)} {log_path} 2>/dev/null'
        )
        result = self.execute_command(probe, workdir=None, timeout=60)
        output = result.get("output") or ""

        # Only the head (before ---TAIL---) carries trusted markers; build
        # output in the tail could itself contain STATE:/SIZE: lines.
        head, _, tail_section = output.partition("---TAIL---")
        tail = tail_section.strip()

        finished = False
        running = False
        state = "unknown"
        exit_code: Optional[int] = None
        log_size = 0
        for line in head.splitlines():
            stripped = line.strip()
            if stripped.startswith("STATE:EXIT:"):
                finished = True
                state = "finished"
                code_text = stripped.rsplit(":", 1)[-1].strip()
                try:
                    exit_code = int(code_text)
                except ValueError:
                    exit_code = None
            elif stripped == "STATE:RUNNING":
                running = True
                state = "running"
            elif stripped == "STATE:VANISHED":
                state = "vanished"
            elif stripped.startswith("SIZE:"):
                try:
                    log_size = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    log_size = 0

        return {
            "finished": finished,
            "running": running,
            "exit_code": exit_code,
            "tail": tail,
            "log_size": log_size,
            "probe_success": result.get("exit_code") == 0,
            "state": state,
        }

    @staticmethod
    def _detached_poll_state(poll: Dict[str, Any]) -> str:
        state = poll.get("state")
        if state in {"finished", "running", "vanished"}:
            return str(state)
        if poll.get("finished"):
            return "finished"
        if poll.get("running"):
            return "running"
        if poll.get("probe_success"):
            return "vanished"
        return "unknown"

    def execute_command_with_soft_timeout(
        self,
        command: str,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        soft_timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
        tail_lines: int = 40,
    ) -> Dict[str, Any]:
        """Dispatch-and-poll execution with a soft window (no hard kill).

        The command runs detached with output in a container log file. If it
        finishes within soft_timeout, the result looks like a normal
        execute_command result. If it is still running or its liveness cannot
        be established when the window closes, the result is a handoff carrying
        the log tail and poll instructions. Only terminal observations are
        collected.
        """
        config = getattr(self, "config", None)
        if soft_timeout is None:
            soft_timeout = getattr(config, "dispatch_soft_timeout_seconds", 900) or 900
        if poll_interval is None:
            poll_interval = getattr(config, "dispatch_poll_interval_seconds", 15) or 15

        handle = self.execute_command_detached(command, workdir=workdir, environment=environment)
        if not handle.get("started"):
            return {
                "success": False,
                "exit_code": 1,
                "output": f"Failed to dispatch command: {handle.get('launch_output', '')}",
                "termination_reason": None,
                "dispatch_status": "dispatch_failed",
                "dispatch": handle,
            }

        deadline = time.time() + max(1, int(soft_timeout))
        # Short early polls catch quick commands without paying a full interval.
        delays = [2, 5, 10]
        poll_count = 0
        while True:
            now = time.time()
            if now >= deadline:
                break
            delay = delays[poll_count] if poll_count < len(delays) else poll_interval
            time.sleep(max(0.05, min(delay, deadline - now)))
            poll_count += 1
            poll = self.poll_detached_command(handle, tail_lines=tail_lines)
            if self._detached_poll_state(poll) in {"finished", "vanished"}:
                return self.collect_detached_result(handle, poll)

        final_poll = self.poll_detached_command(handle, tail_lines=tail_lines)
        final_state = self._detached_poll_state(final_poll)
        if final_state in {"finished", "vanished"}:
            return self.collect_detached_result(handle, final_poll)

        liveness_unknown = final_state == "unknown"
        dispatch_status = "liveness_unknown_detached" if liveness_unknown else "running_detached"
        if liveness_unknown:
            logger.warning(
                f"Soft window of {soft_timeout}s expired without a conclusive liveness "
                f"probe; preserving detached command handle (pid {handle['pid']}, "
                f"log {handle['log_path']})"
            )
            handoff_summary = (
                "Command liveness could not be established after the soft window. "
                "Its detached handle was preserved and the operation remains pending."
            )
        else:
            logger.info(
                f"⏳ Soft window of {soft_timeout}s expired; handing off still-running command "
                f"(pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command still running after the {soft_timeout}s soft window — it was left "
                "running in the background (NOT killed)."
            )
        handoff_output = (
            f"{handoff_summary}\n"
            f"Background job: pid {handle['pid']}, log file {handle['log_path']}\n"
            f"Last output:\n{final_poll.get('tail') or '(no output yet)'}\n\n"
            f"NEXT STEPS — poll the log instead of re-running the command:\n"
            f"  1. Progress: bash(command=\"tail -n 50 {handle['log_path']}\")\n"
            f"  2. Completion: bash(command=\"cat {handle['exit_code_path']} 2>/dev/null || echo STILL_RUNNING\") "
            f"— prints the exit code once the command finishes\n"
            f"  3. Do other useful work between polls; do NOT start the same build again."
        )
        return {
            "success": True,
            "exit_code": None,
            "output": handoff_output,
            "termination_reason": None,
            "dispatch_status": dispatch_status,
            "lifecycle_state": "pending",
            "liveness_state": final_state,
            "dispatch": {
                **handle,
                "last_tail": final_poll.get("tail", ""),
                "log_size": final_poll.get("log_size", 0),
                "soft_timeout": soft_timeout,
            },
        }

    def collect_detached_result(
        self, handle: Dict[str, Any], poll: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build an execute_command-shaped result for a finished detached command."""
        # Read the complete log (truncate_output=False): a finished build log is
        # exactly what the agent needs to diagnose a failure, and the orchestrator's
        # emergency truncation would otherwise gut the middle of it (only first/last
        # 25 lines survive), hiding the real compiler/reactor error. The full text
        # goes into `full_output` for the build tools to persist to the output store;
        # the inline `output` stays bounded so it never floods the model context.
        log_result = self.execute_command(
            f"cat {shlex.quote(handle['log_path'])}",
            workdir=None,
            timeout=120,
            truncate_output=False,
        )
        full_output = log_result.get("output") or poll.get("tail") or ""

        state = self._detached_poll_state(poll)
        exit_code = poll.get("exit_code")
        if exit_code is None and state == "vanished":
            # A vanished process with no exit file is explicit crash evidence.
            exit_code = 1
            full_output += "\n[detached command ended without recording an exit code]"

        inline_output = full_output
        if len(inline_output) > 10000:
            inline_output = self._truncate_output_smartly(full_output)

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "output": inline_output,
            "full_output": full_output,
            "termination_reason": None,
            "dispatch_status": "completed_detached",
            "dispatch": handle,
            "lifecycle_state": state,
        }

    def _collect_detached_result(
        self, handle: Dict[str, Any], poll: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Backward-compatible private entry point for internal callers/tests."""
        return self.collect_detached_result(handle, poll)

    def _monitor_execution_with_timeouts(
        self, exec_result, monitoring_state: dict, silent_timeout: int, absolute_timeout: int
    ) -> Dict[str, Any]:
        """Monitor command execution with dual timeout mechanism."""

        output_buffer = []
        last_chunk_time = time.time()
        saw_stream_read_timeout = False
        stream_broken = False

        try:
            # Read output stream with timeout monitoring. Timeout checks run
            # BEFORE each read so they fire even when no chunk ever arrives —
            # a socket read timeout on the stream must not disable enforcement
            # (that suppression let a 1200s-capped gradle build run for 20000s).
            stream = iter(exec_result.output)
            while True:
                current_time = time.time()

                # Check absolute timeout
                if current_time - monitoring_state["start_time"] > absolute_timeout:
                    logger.error(f"⏰ ABSOLUTE TIMEOUT: Command exceeded {absolute_timeout}s limit")
                    monitoring_state["termination_reason"] = "absolute_timeout"
                    self._terminate_container_processes()
                    break

                # Check silent timeout
                if current_time - last_chunk_time > silent_timeout:
                    logger.warning(f"🔇 SILENT TIMEOUT: No output for {silent_timeout}s")
                    monitoring_state["termination_reason"] = "silent_timeout"
                    self._terminate_container_processes()
                    break

                try:
                    chunk = next(stream)
                except StopIteration:
                    # A generator that previously raised is closed for good, so
                    # a StopIteration right after a read timeout means the
                    # stream died — NOT that the process finished.
                    if saw_stream_read_timeout:
                        stream_broken = True
                    break
                except Exception as stream_exc:
                    if self._is_stream_read_timeout(stream_exc):
                        saw_stream_read_timeout = True
                        logger.warning(
                            f"📡 Output stream read timeout ({stream_exc}); "
                            "continuing wall-clock timeout enforcement"
                        )
                        continue
                    raise

                # A chunk arrived after a read timeout: the stream is resumable,
                # so a later StopIteration is a genuine end-of-stream.
                saw_stream_read_timeout = False

                current_time = time.time()

                # Process the chunk
                if chunk[0]:  # stdout
                    decoded_chunk = chunk[0].decode("utf-8")
                    output_buffer.append(decoded_chunk)
                    monitoring_state["total_output"] += decoded_chunk
                    last_chunk_time = current_time
                    monitoring_state["last_output_time"] = current_time

                    # Log progress periodically
                    if len(output_buffer) % 50 == 0:  # Every 50 chunks
                        elapsed = current_time - monitoring_state["start_time"]
                        logger.info(
                            f"📊 Progress: {len(output_buffer)} chunks, {elapsed:.1f}s elapsed"
                        )

                if chunk[1]:  # stderr
                    decoded_chunk = chunk[1].decode("utf-8")
                    output_buffer.append(f"STDERR: {decoded_chunk}")
                    last_chunk_time = current_time
                    monitoring_state["last_output_time"] = current_time

            # The stream died after a read timeout but the container process
            # may still be running unsupervised — keep enforcing the same
            # wall-clock timeouts without output visibility.
            if stream_broken and monitoring_state["termination_reason"] is None:
                self._enforce_timeouts_without_stream(
                    monitoring_state, silent_timeout, absolute_timeout, last_chunk_time
                )

            # Combine all output
            full_output = "".join(output_buffer)

            # Get final execution result
            exit_code = exec_result.exit_code

            # For streaming execution, Docker can leave exit_code unknown after the stream ends.
            if exit_code is None:
                if monitoring_state.get("stream_lost_exit_unknown"):
                    # The output is partial (stream died mid-run) so a missing
                    # failure marker proves nothing — fail safe and tell the
                    # agent to verify rather than report a false success.
                    logger.warning(
                        "Stream lost before completion; reporting failure with unknown exit code"
                    )
                    exit_code = 1
                    full_output += (
                        "\n[output stream was lost mid-run; the command finished with an "
                        "unknown exit code — verify the build state before relying on this result]"
                    )
                elif _has_unknown_exit_failure_marker(full_output):
                    logger.warning("Command failure inferred from unknown-exit output")
                    exit_code = 1
                else:
                    exit_code = 0

            # Apply truncation if needed
            if len(full_output) > 10000:
                full_output = self._truncate_output_smartly(full_output)

            success = exit_code == 0 and monitoring_state["termination_reason"] is None

            # Generate monitoring summary
            monitoring_info = {
                "execution_time": time.time() - monitoring_state["start_time"],
                "termination_reason": monitoring_state["termination_reason"],
                "cpu_warnings": monitoring_state["cpu_warnings"],
                "output_chunks": len(output_buffer),
            }

            if not success and monitoring_state["termination_reason"]:
                logger.error(
                    f"❌ Command terminated due to: {monitoring_state['termination_reason']}"
                )

            return {
                "success": success,
                "exit_code": exit_code or 0,
                "output": full_output,
                "termination_reason": monitoring_state["termination_reason"],
                "monitoring_info": monitoring_info,
            }

        except Exception as e:
            logger.error(f"Error during execution monitoring: {e}")
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Monitoring error: {str(e)}",
                "termination_reason": "monitoring_error",
                "monitoring_info": monitoring_state,
            }

    @staticmethod
    def _is_stream_read_timeout(exc: Exception) -> bool:
        """Whether a streaming-exec exception is a socket read timeout.

        Covers socket.timeout/TimeoutError plus the requests/urllib3 wrappers
        docker-py surfaces ("Read timed out. (read timeout=60)").
        """
        if isinstance(exc, TimeoutError):
            return True
        return "read timed out" in str(exc).lower()

    def _command_still_running(self, command_fragment: str) -> Optional[bool]:
        """Best-effort liveness probe for a command after its stream died.

        Returns True/False when the probe works, None when it cannot tell
        (probe failure must not be mistaken for process exit).
        """
        fragment = (command_fragment or "").strip()
        if not fragment:
            return None
        try:
            probe = (
                f"ps -eo args | grep -F {shlex.quote(fragment)} "
                f"| grep -v -e grep -e 'ps -eo' | head -1"
            )
            result = self.execute_command(probe, workdir=None, timeout=30)
            output = result.get("output") or ""
            if result.get("exit_code") != 0 or "command not found" in output.lower():
                return None
            return bool(output.strip())
        except Exception as exc:
            logger.debug(f"Liveness probe failed: {exc}")
            return None

    def _enforce_timeouts_without_stream(
        self,
        monitoring_state: dict,
        silent_timeout: int,
        absolute_timeout: int,
        last_chunk_time: float,
    ) -> None:
        """Enforce absolute/silent timeouts after the output stream died.

        The docker exec output generator is closed once it raises (e.g. a
        socket read timeout), but the container process keeps running. Poll
        for liveness and apply the same timeout rules the streaming loop
        would; terminate on breach. Without this, a broken stream silently
        disabled all enforcement and runaway builds consumed the whole run.
        """
        poll_interval = monitoring_state.get("blind_poll_interval", 10.0)
        fragment = monitoring_state.get("command_fragment") or ""
        logger.warning(
            "📡 Output stream lost; enforcing timeouts blind "
            f"(absolute={absolute_timeout}s, silent={silent_timeout}s)"
        )
        while True:
            now = time.time()
            if now - monitoring_state["start_time"] > absolute_timeout:
                logger.error(
                    f"⏰ ABSOLUTE TIMEOUT (stream lost): command exceeded {absolute_timeout}s limit"
                )
                monitoring_state["termination_reason"] = "absolute_timeout"
                self._terminate_container_processes()
                return

            # Liveness first: a probe-confirmed running process counts as
            # progress (output is invisible after stream loss, so the silent
            # timer alone would kill a healthy build).
            alive = self._command_still_running(fragment)
            if alive is False:
                logger.info("📡 Process finished after stream loss; exit code is unknown")
                monitoring_state["stream_lost_exit_unknown"] = True
                return
            if alive is True:
                last_chunk_time = now
            elif now - last_chunk_time > silent_timeout:
                logger.warning(
                    f"🔇 SILENT TIMEOUT (stream lost): no liveness signal for {silent_timeout}s"
                )
                monitoring_state["termination_reason"] = "silent_timeout"
                self._terminate_container_processes()
                return

            remaining_absolute = absolute_timeout - (now - monitoring_state["start_time"])
            time.sleep(max(0.05, min(poll_interval, remaining_absolute)))

    def _monitor_cpu_usage(self, monitoring_state: dict, check_interval: int):
        """Monitor CPU usage to detect hung processes."""

        consecutive_low_cpu = 0
        cpu_threshold = 1.0  # Consider CPU usage below 1% as potentially hung

        while not monitoring_state["process_terminated"]:
            try:
                time.sleep(check_interval)

                if monitoring_state["process_terminated"]:
                    break

                # Get CPU stats
                container = self.client.containers.get(self.container_name)
                stats = container.stats(stream=False)

                # Calculate CPU percentage
                cpu_percent = self._calculate_cpu_percentage(stats)

                current_time = time.time()
                silent_duration = current_time - monitoring_state["last_output_time"]

                # Check for potential hang: low CPU + no output for a while
                if cpu_percent < cpu_threshold and silent_duration > check_interval:
                    consecutive_low_cpu += 1
                    monitoring_state["cpu_warnings"] += 1

                    logger.warning(
                        f"⚠️ CPU MONITOR: {cpu_percent:.2f}% CPU, "
                        f"{silent_duration:.1f}s since last output "
                        f"(warning #{consecutive_low_cpu})"
                    )

                    # Alert after 3 consecutive low CPU readings
                    if consecutive_low_cpu >= 3:
                        logger.error(
                            f"🚨 HANG DETECTED: Consistently low CPU ({cpu_percent:.2f}%) "
                            f"with {silent_duration:.1f}s silence"
                        )
                        # Don't auto-terminate here, let the silent timeout handle it

                else:
                    consecutive_low_cpu = 0  # Reset counter if CPU is normal

            except Exception as e:
                logger.warning(f"CPU monitoring error: {e}")
                time.sleep(check_interval)

    def _calculate_cpu_percentage(self, stats: dict) -> float:
        """Calculate CPU percentage from Docker stats."""
        try:
            cpu_stats = stats["cpu_stats"]
            precpu_stats = stats["precpu_stats"]

            cpu_delta = (
                cpu_stats["cpu_usage"]["total_usage"] - precpu_stats["cpu_usage"]["total_usage"]
            )
            system_delta = cpu_stats["system_cpu_usage"] - precpu_stats["system_cpu_usage"]

            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (
                    (cpu_delta / system_delta) * len(cpu_stats["cpu_usage"]["percpu_usage"]) * 100.0
                )
                return cpu_percent

        except (KeyError, ZeroDivisionError, TypeError):
            pass

        return 0.0

    def _terminate_container_processes(self):
        """Gracefully terminate processes in the container."""
        try:
            container = self.client.containers.get(self.container_name)

            logger.info("🛑 Attempting graceful termination (SIGTERM)...")
            # Send SIGTERM to all java/mvn processes
            container.exec_run(["pkill", "-TERM", "java"], detach=True)
            container.exec_run(["pkill", "-TERM", "mvn"], detach=True)

            # Wait 30 seconds for graceful shutdown
            time.sleep(30)

            # Force kill if still running
            logger.info("🔪 Force terminating remaining processes (SIGKILL)...")
            container.exec_run(["pkill", "-KILL", "java"], detach=True)
            container.exec_run(["pkill", "-KILL", "mvn"], detach=True)

        except Exception as e:
            logger.error(f"Failed to terminate container processes: {e}")

    def _truncate_output_smartly(self, output: str) -> str:
        """Smart output truncation that preserves important information."""
        lines = output.split("\n")

        if len(lines) <= 100:
            return output

        # Keep more lines from the end (recent output) than the beginning
        head_lines = 30
        tail_lines = 50

        truncated = (
            "\n".join(lines[:head_lines])
            + f"\n... [TRUNCATED: {len(lines) - head_lines - tail_lines} lines omitted] ...\n"
            + "\n".join(lines[-tail_lines:])
        )

        return truncated

    def get_container_info(self) -> Optional[Dict[str, Any]]:
        """Get container information."""

        try:
            if not self.container_exists():
                return None

            container = self.client.containers.get(self.container_name)

            return {
                "name": container.name,
                "status": container.status,
                "image": container.image.tags[0] if container.image.tags else "unknown",
                "created": container.attrs["Created"],
                "ports": container.ports,
                "mounts": [
                    mount["Source"] + ":" + mount["Destination"]
                    for mount in container.attrs.get("Mounts", [])
                ],
                "workspace_path": self.config.workspace_path,
            }

        except Exception as e:
            logger.error(f"Failed to get container info: {e}")
            return None

    def get_detailed_status(self) -> Dict[str, Any]:
        """Get detailed status information."""

        status = {
            "project_name": self.project_name,
            "container_name": self.container_name,
            "volume_name": self.volume_name,
            "container_exists": self.container_exists(),
            "container_running": self.is_container_running(),
            "volume_exists": self._volume_exists(),
        }

        if status["container_exists"]:
            container_info = self.get_container_info()
            if container_info:
                status.update(container_info)

        return status

    def list_sag_projects(self) -> List[Dict[str, Any]]:
        """List all SAG projects with their status and last comment."""

        projects = []

        try:
            # Get all containers with sag- prefix
            containers = self.client.containers.list(all=True)

            for container in containers:
                if container.name.startswith("sag-"):
                    project_name = container.name.replace("sag-", "")

                    # Get container info
                    created = container.attrs.get("Created", "")
                    if created:
                        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        created_str = created_dt.strftime("%Y-%m-%d %H:%M")
                    else:
                        created_str = "Unknown"

                    # Get last comment from volume file
                    temp_orch = DockerOrchestrator(project_name=project_name)
                    last_comment = temp_orch.get_last_comment_from_volume()

                    projects.append(
                        {
                            "project_name": project_name,
                            "docker_name": container.name,
                            "status": container.status,
                            "created": created_str,
                            "image": container.image.tags[0] if container.image.tags else "Unknown",
                            "last_comment": last_comment,
                        }
                    )

            # Sort by creation time (newest first)
            projects.sort(key=lambda x: x["created"], reverse=True)

            return projects

        except Exception as e:
            logger.error(f"Failed to list SAG projects: {e}")
            return []

    def update_last_comment(self, comment: str) -> bool:
        """Update the last comment for the project container."""

        try:
            if not self.container_exists():
                logger.warning(f"Container {self.container_name} does not exist")
                return False

            container = self.client.containers.get(self.container_name)

            # Get current labels
            current_labels = container.attrs.get("Config", {}).get("Labels", {}) or {}

            # Update the last comment label
            current_labels["sag.last_comment"] = comment
            current_labels["sag.last_update"] = datetime.now().isoformat()

            # Commit the container with updated labels
            # Note: This creates a new image, but we'll use a simple approach
            # by stopping and recreating the container with new labels

            logger.info(f"Updated last comment for {self.container_name}: {comment}")

            # For now, we'll store the comment in a volume file
            # This is simpler than recreating the container
            self._store_comment_in_volume(comment)

            return True

        except Exception as e:
            logger.error(f"Failed to update last comment: {e}")
            return False

    def _store_comment_in_volume(self, comment: str) -> bool:
        """Store the comment in a file within the container volume."""

        try:
            if not self.is_container_running():
                self.start_container()

            # Create a comment file in the workspace
            comment_data = {
                "comment": comment,
                "timestamp": datetime.now().isoformat(),
                "project": self.project_name,
            }

            import json

            comment_json = json.dumps(comment_data, indent=2)

            # Write to container
            result = self.execute_command(
                f"echo '{comment_json}' > {self.config.workspace_path}/.sag_last_comment.json"
            )

            if result.get("success", False):
                logger.debug(f"Comment stored successfully for {self.project_name}")
                return True
            else:
                logger.warning(f"Failed to store comment: {result.get('error', 'Unknown error')}")
                return False

        except Exception as e:
            logger.error(f"Failed to store comment in volume: {e}")
            return False

    def get_last_comment_from_volume(self) -> str:
        """Get the last comment from the volume file."""

        try:
            if not self.is_container_running():
                return "Container not running"

            # Read comment file from container
            result = self.execute_command(
                f"cat {self.config.workspace_path}/.sag_last_comment.json 2>/dev/null || echo '{{}}'"
            )

            if result.get("success", False):
                import json

                try:
                    comment_data = json.loads(result.get("output", "{}"))
                    return comment_data.get("comment", "No comment available")
                except json.JSONDecodeError:
                    return "No comment available"
            else:
                return "No comment available"

        except Exception as e:
            logger.error(f"Failed to get comment from volume: {e}")
            return "Error reading comment"

    ## TODO：Need to add default java and python related config
    def _get_container_config(self) -> Dict[str, Any]:
        """Get container configuration."""

        config = {
            "detach": True,
            "stdin_open": True,
            "tty": True,
            "working_dir": self.config.workspace_path,
            # Remove volume mount - files will be stored inside container
            # "volumes": {self.volume_name: {"bind": self.config.workspace_path, "mode": "rw"}},
            "environment": {
                **DEFAULT_UTF8_ENVIRONMENT,
                "DEBIAN_FRONTEND": "noninteractive",
                "TERM": "xterm-256color",
            },
            "labels": {
                "setup-agent.project": self.project_name,
                "setup-agent.created": datetime.now().isoformat(),
            },
            # Add a command to keep container running
            "command": [
                "/bin/bash",
                "-c",
                f"mkdir -p {self.config.workspace_path} && while true; do sleep 30; done",
            ],
        }

        return config

    def _create_volume(self) -> bool:
        """Create a Docker volume for the project."""

        try:
            logger.info(f"Creating volume {self.volume_name}")

            self.client.volumes.create(
                name=self.volume_name,
                labels={
                    "setup-agent.project": self.project_name,
                    "setup-agent.created": datetime.now().isoformat(),
                },
            )

            logger.info(f"Volume {self.volume_name} created successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to create volume: {e}")
            return False

    def _volume_exists(self) -> bool:
        """Check if the volume exists."""

        try:
            self.client.volumes.get(self.volume_name)
            return True
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking volume existence: {e}")
            return False

    def _ensure_image_available(self) -> bool:
        """Ensure the Docker image is available locally, pull if needed."""

        try:
            # Check if image exists locally
            try:
                self.client.images.get(self.base_image)
                logger.info(f"Image {self.base_image} already exists locally")
                return True
            except NotFound:
                logger.info(f"Image {self.base_image} not found locally, pulling...")

            # Pull the image
            logger.info(f"Pulling Docker image: {self.base_image}")
            logger.info("This may take a few minutes on first run...")

            # Pull with progress logging
            for line in self.client.api.pull(self.base_image, stream=True, decode=True):
                if "status" in line:
                    status = line["status"]
                    if "id" in line:
                        logger.debug(f"{line['id']}: {status}")
                    else:
                        logger.info(status)

            logger.info(f"✅ Successfully pulled image: {self.base_image}")
            return True

        except Exception as e:
            logger.error(f"Failed to pull image {self.base_image}: {e}")
            return False

    def _wait_for_container_ready(self, timeout: int = 30) -> bool:
        """Wait for container to be ready."""

        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                container = self.client.containers.get(self.container_name)

                if container.status == "running":
                    # Test if we can execute a simple command
                    result = container.exec_run("echo 'ready'", stdout=True, stderr=True)
                    if result.exit_code == 0:
                        return True

                time.sleep(1)

            except Exception as e:
                logger.debug(f"Container not ready yet: {e}")
                time.sleep(1)

        logger.error(
            f"Container {self.container_name} did not become ready within {timeout} seconds"
        )
        return False

    def _setup_container_environment(self) -> bool:
        """
        Setup the basic environment in the container.

        ★★★ CRITICAL FIX: Ensure /workspace directory always exists to prevent OCI runtime exec failed.
        ★★ PRIORITY FIX: Install Git during environment initialization to prevent chain failures.
        """

        try:
            logger.info("Setting up container environment with robust workspace creation")

            # ★★★ STEP 1: CRITICAL - Ensure workspace directory exists and is permanent
            workspace_commands = [
                f"mkdir -p {self.config.workspace_path}",
                f"chown -R root:root {self.config.workspace_path}",
                f"chmod 755 {self.config.workspace_path}",
                f"touch {self.config.workspace_path}/.sag_workspace_marker",  # Marker to verify persistence
                f"ls -la {self.config.workspace_path}",  # Verify creation
            ]

            logger.info("🔧 CRITICAL: Creating persistent workspace directory")
            for i, command in enumerate(workspace_commands):
                logger.info(f"Workspace setup {i+1}/{len(workspace_commands)}: {command}")
                result = self.execute_command(
                    command, workdir=None
                )  # Use no workdir for workspace creation

                if not result["success"]:
                    logger.error(f"❌ CRITICAL: Workspace setup failed at step {i+1}: {command}")
                    logger.error(f"Exit code: {result.get('exit_code', 'unknown')}")
                    logger.error(f"Output: {result.get('output', 'no output')}")
                    return False  # Fail fast on workspace creation failure
                else:
                    logger.info(f"✅ Workspace step {i+1} completed successfully")

            # ★★ STEP 2: PRIORITY - Install Git and essential tools during initialization
            logger.info("🔧 PRIORITY: Installing Git and essential tools")

            # Update package lists first
            logger.info("📦 Updating package lists...")
            update_result = self.execute_command("apt-get update -qq", workdir=None)
            if not update_result["success"]:
                logger.warning("⚠️ Package list update failed, continuing with cached lists")

            # Install essential packages including Git - this prevents chain failure B
            essential_packages = [
                "curl",
                "wget",
                "git",
                "nano",
                "vim",
                "python3",
                "python3-pip",
                "nodejs",
                "npm",
                "build-essential",
                "grep",
                "findutils",
                "less",
            ]

            install_command = f"apt-get install -y -qq {' '.join(essential_packages)}"
            logger.info(f"📦 Installing essential packages: {' '.join(essential_packages)}")

            install_result = self.execute_command(install_command, workdir=None)

            if not install_result["success"]:
                logger.error("❌ Essential package installation failed")
                logger.error(f"Exit code: {install_result.get('exit_code', 'unknown')}")
                logger.error(f"Output: {install_result.get('output', 'no output')}")

                # Try to install Git separately as it's critical for the workflow
                logger.info("🔧 Attempting to install Git separately...")
                git_result = self.execute_command("apt-get install -y git", workdir=None)
                if not git_result["success"]:
                    logger.error(
                        "❌ CRITICAL: Git installation failed - this will cause chain failure B"
                    )
                    return False
                else:
                    logger.info("✅ Git installed successfully as fallback")
            else:
                logger.info("✅ All essential packages installed successfully")

            # STEP 3: Verify critical tools are available and log versions
            verification_commands = [
                ("git --version", "Git"),
                ("grep --version | head -1", "grep"),
                ("curl --version | head -1", "curl"),
                ("python3 --version", "Python3"),
                (
                    f"test -d {self.config.workspace_path} && echo 'Workspace exists' || echo 'Workspace missing'",
                    "Workspace",
                ),
                (
                    f"test -f {self.config.workspace_path}/.sag_workspace_marker && echo 'Marker exists' || echo 'Marker missing'",
                    "Workspace marker",
                ),
            ]

            logger.info("🔍 Verifying critical tools and workspace...")
            verification_failed = False

            for cmd, tool_name in verification_commands:
                result = self.execute_command(cmd, workdir=None)
                if result["success"]:
                    output_summary = (
                        result["output"][:100] + "..."
                        if len(result["output"]) > 100
                        else result["output"]
                    )
                    logger.info(f"✅ {tool_name}: {output_summary}")
                else:
                    logger.error(f"❌ {tool_name} verification failed")
                    verification_failed = True

                    # Special handling for critical failures
                    if tool_name == "Git":
                        logger.error(
                            "❌ CRITICAL: Git verification failed - this will cause project clone failures"
                        )
                    elif tool_name == "Workspace":
                        logger.error(
                            "❌ CRITICAL: Workspace verification failed - this will cause OCI runtime exec failures"
                        )

            # STEP 4: Create environment script for persistent environment variables
            env_script = f"""#!/bin/bash
# SAG Environment Setup Script
export WORKSPACE_PATH="{self.config.workspace_path}"
export SAG_CONTAINER_INITIALIZED="true"
export LANG="${{LANG:-C.UTF-8}}"
export LC_ALL="${{LC_ALL:-C.UTF-8}}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Ensure workspace always exists
if [ ! -d "$WORKSPACE_PATH" ]; then
    echo "WARNING: Workspace missing, recreating..."
    mkdir -p "$WORKSPACE_PATH"
    chmod 755 "$WORKSPACE_PATH"
    touch "$WORKSPACE_PATH/.sag_workspace_marker"
fi

cd "$WORKSPACE_PATH" 2>/dev/null || cd /root
"""

            # Write environment script
            script_result = self.execute_command(
                f"echo '{env_script}' > /etc/profile.d/sag_env.sh && chmod +x /etc/profile.d/sag_env.sh",
                workdir=None,
            )

            if script_result["success"]:
                logger.info("✅ Environment script created successfully")
            else:
                logger.warning("⚠️ Failed to create environment script")

            if verification_failed:
                logger.error("❌ Environment setup completed with failures")
                return False
            else:
                logger.info(
                    "✅ Container environment setup completed successfully with all verifications passing"
                )
                return True

        except Exception as e:
            logger.error(f"❌ Failed to setup container environment: {e}")
            return False
