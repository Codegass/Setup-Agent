"""Docker Orchestrator for managing containers and volumes."""

import base64
import binascii
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound
from loguru import logger

from sag.config import get_config
from sag.runtime.exec_env import DEFAULT_UTF8_ENVIRONMENT, default_utf8_environment

ENV_OVERLAY_SCRIPT_PATH = "/workspace/.setup_agent/env_overlay.sh"
CONTROL_EXEC_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DETACHED_TERMINAL_AUTHORITY = "docker_exec_inspect_v1"
DETACHED_POLL_TAIL_MAX_BYTES = 6144
DETACHED_INLINE_OUTPUT_MAX_CHARS = 10000


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

    def evidence_store_identity(self) -> str:
        """Return the immutable Docker identity of the current evidence store.

        Container names are reusable. Binding host authority to a name would
        authorize a replacement container after teardown, so callers receive
        only Docker's immutable container id.
        """

        container = self.client.containers.get(self.container_name)
        container_id = str(getattr(container, "id", "") or "").strip()
        if not container_id:
            raise RuntimeError("container has no immutable Docker id")
        return f"docker:{container_id}"

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
        """Return locale setup that cannot execute project-writable shell code."""
        return "export LANG=${LANG:-C.UTF-8}; " "export LC_ALL=${LC_ALL:-C.UTF-8}"

    def _control_exec_environment(self) -> Dict[str, str]:
        """Environment for host-control transport, isolated from runtime overlays."""

        return {
            **default_utf8_environment(),
            "PATH": CONTROL_EXEC_PATH,
            "BASH_ENV": "",
            "ENV": "",
            "CDPATH": "",
        }

    @staticmethod
    def _isolated_exec_argv(
        command: str,
        *,
        runtime_environment: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> list[str]:
        """Build an argv whose controller utilities never use project PATH.

        Docker starts only absolute host-control executables. For a normal
        project command, ``/usr/bin/env`` installs the already validated
        runtime environment for the inner ``/bin/bash``; timeout and the outer
        shell remain under the fixed control environment.
        """

        if runtime_environment is None:
            argv = ["/bin/bash", "-c", command]
        else:
            assignments = [f"{key}={value}" for key, value in runtime_environment.items()]
            argv = ["/usr/bin/env", *assignments, "/bin/bash", "-c", command]
        if timeout_seconds is not None and timeout_seconds > 0:
            argv = [
                "/usr/bin/timeout",
                "--preserve-status",
                str(timeout_seconds),
                *argv,
            ]
        return argv

    def _default_exec_environment(
        self, environment: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Merge controller env with the exact host-authorized runtime overlay."""

        base = {
            **default_utf8_environment(environment),
            "PATH": CONTROL_EXEC_PATH,
            "BASH_ENV": "",
            "ENV": "",
            "CDPATH": "",
        }
        # Normal project commands require a run-scoped host authority. Container
        # bootstrap and control-plane I/O use ``execute_control_command``
        # explicitly; treating an unavailable authority as an empty overlay
        # would otherwise turn a fresh orchestrator into an unrecorded runner.
        from sag.agent.evidence_publications import (
            EvidencePublicationAuthority,
            current_evidence_publication_authority,
        )

        authority = current_evidence_publication_authority(self)
        if not isinstance(authority, EvidencePublicationAuthority):
            from sag.runtime.env_overlay import EnvOverlayUnavailableError

            raise EnvOverlayUnavailableError(
                "runtime environment has no host publication authority"
            )
        from sag.runtime.env_overlay import EnvOverlayStore

        return EnvOverlayStore(self).authorized_environment(base)

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
        _clean_control_path: bool = False,
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

        # Shell startup files are never sourced. Runtime variables are injected
        # through Docker's environment argument after strict host publication
        # verification; this prefix carries locale defaults only.
        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            quoted_workdir = shlex.quote(workdir)
            wrapped_command = f"{runtime_profile_prefix}; cd {quoted_workdir} && {command}"
        else:
            # No working directory specified, use default behavior
            wrapped_command = f"{runtime_profile_prefix}; {command}"
        timeout_seconds = int(timeout) if timeout is not None else None

        logger.info(f"Executing command in container: {command}")
        if workdir:
            logger.info(f"Working directory: {workdir}")

        runner_dispatched = False
        try:
            # Prepare environment
            runtime_environment = (
                None if _clean_control_path else self._default_exec_environment(environment)
            )
            exec_command = self._isolated_exec_argv(
                wrapped_command,
                runtime_environment=runtime_environment,
                timeout_seconds=timeout_seconds,
            )
            exec_env = self._control_exec_environment()

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
            runner_dispatched = True

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
                        # Head and tail only. Bookkeeping, not an alarm: every
                        # caller left on this path reads the exit code, and the
                        # model-facing tools opt out of the cut (`bash` passes
                        # truncate_output=False; a detached build log is read
                        # whole). The marker stays in the output because it is
                        # true; the log states what was kept, at DEBUG.
                        truncated = (
                            "\n".join(lines[:25])
                            + f"\n... [ORCHESTRATOR TRUNCATED: {len(lines)} lines, {original_length} chars] ...\n"
                            + "\n".join(lines[-25:])
                        )
                        logger.debug(
                            f"{command[:60]}: {len(lines)} lines ({original_length} chars); "
                            "kept the first 25 and last 25"
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
                # A returned command is not proof that Docker accepted it.
                # This bit is set only after ``container.exec_run`` returned a
                # real execution object and is propagated into runner receipts.
                "runner_dispatched": True,
            }
        except Exception as e:
            logger.error(f"Failed to execute command '{command}': {e}")
            dispatch_status = (
                "environment_overlay_unavailable"
                if getattr(e, "code", None) == "environment_overlay_unavailable"
                and not runner_dispatched
                else ("execution_observation_failed" if runner_dispatched else "dispatch_failed")
            )
            return {
                "success": False,
                "exit_code": -1,
                "output": str(e),
                "dispatch_status": dispatch_status,
                "runner_dispatched": runner_dispatched,
            }

    def execute_control_command(
        self,
        command: str,
        workdir: Optional[str] = None,
        capture_stderr: bool = True,
        environment: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        truncate_output: bool = True,
    ) -> Dict[str, Any]:
        """Execute host-control I/O without profiles or runtime overlay authority.

        ``environment`` is accepted for call-shape compatibility but cannot
        alter the fixed control environment.  Evidence transports should not
        have an ambient project-controlled environment channel.
        """

        del environment
        try:
            return self.execute_command(
                command,
                workdir=workdir,
                capture_stderr=capture_stderr,
                timeout=timeout,
                truncate_output=truncate_output,
                _clean_control_path=True,
            )
        except TypeError as exc:
            # A monkeypatched legacy method cannot prove that it honored the
            # clean-control flag. Never retry it through the normal runtime
            # environment: that would make a compatibility seam an authority
            # bypass.
            if not any(
                name in str(exc)
                for name in (
                    "_clean_control_path",
                    "capture_stderr",
                    "truncate_output",
                    "workdir",
                    "timeout",
                )
            ):
                raise
            return {
                "success": False,
                "exit_code": -1,
                "output": "clean control transport unavailable",
                "dispatch_status": "control_transport_unavailable",
                "runner_dispatched": False,
            }

    def execute_command_with_monitoring(
        self,
        command: str,
        workdir: Optional[str] = None,
        silent_timeout: int = 600,  # 10 minutes no output
        absolute_timeout: int = 2400,  # 40 minutes total
        use_timeout_wrapper: bool = True,
        enable_cpu_monitoring: bool = True,
        optimize_for_maven: bool = True,
        _clean_control_path: bool = False,
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

        # Runtime environment is injected below; shell startup files are never
        # sourced. The prefix carries locale defaults only.
        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            quoted_workdir = shlex.quote(workdir)
            base_cmd = f"{runtime_profile_prefix}; cd {quoted_workdir} && {command}"
        else:
            # No working directory specified, use default
            base_cmd = f"{runtime_profile_prefix}; {command}"

        # Timeout is applied as an absolute outer argv below, never through
        # the project runtime PATH.
        if use_timeout_wrapper:
            logger.info(f"🕐 Wrapped command with {absolute_timeout}s absolute timeout")

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

        runner_dispatched = False
        try:
            runtime_environment = None if _clean_control_path else self._default_exec_environment()
            exec_command = self._isolated_exec_argv(
                base_cmd,
                runtime_environment=runtime_environment,
                timeout_seconds=absolute_timeout if use_timeout_wrapper else None,
            )
            # Streaming exec_run cannot expose the final exit code. Retain
            # the daemon exec id and inspect this exact process after its
            # stream ends instead of guessing an exit from log text.
            created = self.client.api.exec_create(
                container.id,
                exec_command,
                workdir=None,  # Handled by cd command in bash
                environment=self._control_exec_environment(),
            )
            exec_id = created.get("Id") if isinstance(created, dict) else None
            if not isinstance(exec_id, str) or not exec_id:
                raise RuntimeError("Docker did not return the monitored exec identity")
            runner_dispatched = None  # start acceptance is unknown until the response arrives
            stream = self.client.api.exec_start(exec_id, stream=True, demux=True)
            runner_dispatched = True
            exec_result = SimpleNamespace(
                output=stream,
                exit_code=None,
                docker_exec_id=exec_id,
                docker_container_id=container.id,
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
            # Exec start returned before monitoring began, so even a later
            # stream failure or timeout is a physical dispatch receipt.
            result["runner_dispatched"] = runner_dispatched

            # Clean up monitoring thread
            monitoring_state["process_terminated"] = True
            if cpu_monitor_thread:
                cpu_monitor_thread.join(timeout=1)  # Give it 1 second to finish

            return result

        except Exception as e:
            monitoring_state["process_terminated"] = True
            logger.error(f"Failed to execute command '{command}': {e}")
            dispatch_status = (
                "environment_overlay_unavailable"
                if getattr(e, "code", None) == "environment_overlay_unavailable"
                and not runner_dispatched
                else (
                    "execution_observation_failed"
                    if runner_dispatched is True
                    else "dispatch_unknown" if runner_dispatched is None else "dispatch_failed"
                )
            )
            return {
                "success": False,
                "exit_code": None if runner_dispatched is not False else -1,
                "observed_exit_code": None,
                "exit_code_inferred": False,
                "execution_observation_complete": False,
                "output": f"Execution failed: {str(e)}",
                "termination_reason": "exception",
                "dispatch_status": dispatch_status,
                "runner_dispatched": runner_dispatched,
                "monitoring_info": monitoring_state,
            }

    def execute_control_command_with_monitoring(
        self,
        command: str,
        workdir: Optional[str] = None,
        silent_timeout: int = 600,
        absolute_timeout: int = 2400,
        use_timeout_wrapper: bool = True,
        enable_cpu_monitoring: bool = True,
        optimize_for_maven: bool = False,
    ) -> Dict[str, Any]:
        """Stream a host-control command under the isolated control environment."""

        return self.execute_command_with_monitoring(
            command,
            workdir=workdir,
            silent_timeout=silent_timeout,
            absolute_timeout=absolute_timeout,
            use_timeout_wrapper=use_timeout_wrapper,
            enable_cpu_monitoring=enable_cpu_monitoring,
            optimize_for_maven=optimize_for_maven,
            _clean_control_path=True,
        )

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
        _clean_control_path: bool = False,
    ) -> Dict[str, Any]:
        """Start a command whose terminal status is owned by the Docker daemon.

        Output goes to a container log file, while the physical exit status is
        retained by Docker's exec record.  A container file cannot be terminal
        authority: the project command and its descendants may share the
        supervisor's uid and can therefore replace any predictable marker
        after the original leader exits.
        """
        job_id = uuid.uuid4().hex[:12]
        log_path = f"{self.DISPATCH_DIR}/{job_id}.log"
        exit_code_path = f"{log_path}.exit"
        pid_path = f"{self.DISPATCH_DIR}/{job_id}.pid"
        pgid_path = f"{self.DISPATCH_DIR}/{job_id}.pgid"
        identity_path = f"{self.DISPATCH_DIR}/{job_id}.identity"

        try:
            runtime_environment = (
                self._control_exec_environment()
                if _clean_control_path
                else self._default_exec_environment(environment)
            )
        except Exception as exc:
            dispatch_status = (
                "environment_overlay_unavailable"
                if getattr(exc, "code", None) == "environment_overlay_unavailable"
                else "dispatch_failed"
            )
            return {
                "started": False,
                "job_id": job_id,
                "pid": None,
                "pgid": None,
                "process_identity_token": "",
                "docker_exec_id": "",
                "container_id": "",
                "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
                "start_accepted": False,
                "startup_identity_verified": False,
                "pid_path": pid_path,
                "pgid_path": pgid_path,
                "identity_path": identity_path,
                "log_path": log_path,
                "exit_code_path": exit_code_path,
                "command": command,
                "launch_output": str(exc),
                "dispatch_status": dispatch_status,
                "runner_dispatched": False,
                "runner_dispatch_state": "not_accepted",
            }

        runtime_profile_prefix = self._runtime_profile_prefix()
        if workdir:
            inner = f"{runtime_profile_prefix}; cd {shlex.quote(workdir)} && {command}"
        else:
            inner = f"{runtime_profile_prefix}; {command}"
        # The command itself runs in a fresh session/process group.  The Docker
        # exec process remains in the foreground as its supervisor, so Docker
        # records the status returned by ``setsid --wait`` outside the
        # container filesystem. Cleanup can still signal exactly ``-PGID``.
        quoted_pid = shlex.quote(pid_path)
        quoted_pid_tmp = shlex.quote(pid_path + ".tmp")
        quoted_pgid = shlex.quote(pgid_path)
        quoted_pgid_tmp = shlex.quote(pgid_path + ".tmp")
        quoted_identity = shlex.quote(identity_path)
        quoted_identity_tmp = shlex.quote(identity_path + ".tmp")
        runtime_exec = " ".join(
            [
                "/usr/bin/env",
                *(shlex.quote(f"{key}={value}") for key, value in runtime_environment.items()),
                "/bin/bash",
                "-c",
                shlex.quote(inner),
            ]
        )
        job_script = (
            "set +e; job_pid=$$; "
            'job_pgid="$(ps -o pgid= -p "$job_pid" 2>/dev/null | tr -d \' \')"; '
            'job_sid="$(ps -o sid= -p "$job_pid" 2>/dev/null | tr -d \' \')"; '
            'stat_line="$(cat "/proc/$job_pid/stat" 2>/dev/null)" || exit 124; '
            "stat_rest=${stat_line##*) }; set -- $stat_rest; job_start_ticks=${20:-}; "
            'job_boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || exit 124; '
            'case "$job_pid:$job_pgid:$job_sid" in '
            "([0-9]*:[0-9]*:[0-9]*) ;; (*) exit 125 ;; esac; "
            'case "$job_start_ticks:$job_boot_id" in '
            "([0-9]*:[0-9a-fA-F-]*) ;; (*) exit 125 ;; esac; "
            '[ "$job_pid" = "$job_pgid" ] && [ "$job_pid" = "$job_sid" ] || exit 125; '
            'job_identity="$(printf \'%s:%s:%s:%s\' "$job_boot_id" "$job_pid" '
            '"$job_pgid" "$job_start_ticks" | sha256sum | awk \'{print $1}\')"; '
            f"printf '%s\\n' \"$job_pid\" > {quoted_pid_tmp} && "
            f"mv {quoted_pid_tmp} {quoted_pid} && "
            f"printf '%s\\n' \"$job_pgid\" > {quoted_pgid_tmp} && "
            f"mv {quoted_pgid_tmp} {quoted_pgid} && "
            f"printf '%s\n' \"$job_identity\" > {quoted_identity_tmp} && "
            f"mv {quoted_identity_tmp} {quoted_identity} || exit 126; "
            f"exec {runtime_exec}"
        )
        supervisor = (
            f"set +e; umask 077; mkdir -p {self.DISPATCH_DIR} || exit 126; "
            f": > {shlex.quote(log_path)} || exit 126; "
            f"/usr/bin/setsid --fork --wait /bin/bash -c {shlex.quote(job_script)} "
            f'> {shlex.quote(log_path)} 2>&1; rc=$?; exit "$rc"'
        )

        docker_exec_id = ""
        container_id = ""
        runner_dispatched = False
        start_accepted = False
        try:
            container = self.client.containers.get(self.container_name)
            container_id = str(getattr(container, "id", "") or "").strip()
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise RuntimeError("container has no immutable Docker id")
            created = self.client.api.exec_create(
                container_id,
                ["/bin/bash", "-c", supervisor],
                stdout=False,
                stderr=False,
                environment=self._control_exec_environment(),
                workdir=None,
            )
            docker_exec_id = str((created or {}).get("Id") or "").strip()
            if re.fullmatch(r"[0-9a-f]{64}", docker_exec_id) is None:
                raise RuntimeError("Docker did not return an immutable exec id")
        except Exception as exc:
            logger.error(f"Failed to dispatch detached command: {command}: {exc}")
            return {
                "started": False,
                "job_id": job_id,
                "pid": None,
                "pgid": None,
                "process_identity_token": "",
                "docker_exec_id": docker_exec_id,
                "container_id": container_id,
                "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
                "start_accepted": False,
                "startup_identity_verified": False,
                "pid_path": pid_path,
                "pgid_path": pgid_path,
                "identity_path": identity_path,
                "log_path": log_path,
                "exit_code_path": exit_code_path,
                "command": command,
                "launch_output": str(exc),
                "dispatch_status": "dispatch_failed",
                "runner_dispatched": runner_dispatched,
                "runner_dispatch_state": "not_accepted",
            }

        try:
            self.client.api.exec_start(docker_exec_id, detach=True)
            runner_dispatched = True
            start_accepted = True
        except Exception as exc:
            # A detached start response can be lost after Docker accepted the
            # exec. Inspect the daemon record before classifying the dispatch;
            # otherwise a retry can duplicate a runner that is already live.
            terminal = self.inspect_detached_terminal(
                {
                    "docker_exec_id": docker_exec_id,
                    "container_id": container_id,
                    "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
                    "start_accepted": False,
                    "startup_identity_verified": False,
                }
            )
            if terminal.get("state") == "running" and terminal.get("probe_success") is True:
                runner_dispatched = True
                start_accepted = True
            else:
                logger.error(f"Detached start outcome is unknown for {command}: {exc}")
                return {
                    "started": False,
                    "job_id": job_id,
                    "pid": None,
                    "pgid": None,
                    "process_identity_token": "",
                    "docker_exec_id": docker_exec_id,
                    "container_id": container_id,
                    "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
                    "start_accepted": False,
                    "startup_identity_verified": False,
                    "pid_path": pid_path,
                    "pgid_path": pgid_path,
                    "identity_path": identity_path,
                    "log_path": log_path,
                    "exit_code_path": exit_code_path,
                    "command": command,
                    "launch_output": str(exc),
                    "dispatch_status": "dispatch_unknown",
                    "runner_dispatched": None,
                    "runner_dispatch_state": "unknown",
                }

        identity_probe = (
            'i=0; while [ "$i" -lt 100 ] && '
            f"[ ! -s {quoted_pid} -o ! -s {quoted_pgid} -o ! -s {quoted_identity} ]; do "
            "sleep 0.05; i=$((i + 1)); done; "
            f'pid="$(cat {quoted_pid} 2>/dev/null)"; '
            f'pgid="$(cat {quoted_pgid} 2>/dev/null)"; '
            f'identity="$(cat {quoted_identity} 2>/dev/null)"; '
            'case "$pid:$pgid" in ([0-9]*:[0-9]*) ;; (*) exit 70 ;; esac; '
            '[ "$pid" = "$pgid" ] || exit 70; '
            'case "$identity" in ([0-9a-f][0-9a-f]*) ;; (*) exit 70 ;; esac; '
            'printf \'PID:%s\\nPGID:%s\\nIDENTITY:%s\\n\' "$pid" "$pgid" "$identity"'
        )
        result = self.execute_control_command(identity_probe, workdir=None, timeout=60)
        if not isinstance(result, dict):
            result = {}

        pid: Optional[int] = None
        pgid: Optional[int] = None
        process_identity_token = ""
        pid_markers = 0
        pgid_markers = 0
        identity_markers = 0
        for line in str(result.get("output") or "").splitlines():
            marker, separator, value = line.strip().partition(":")
            if not separator:
                continue
            if marker == "PID" and value.isdigit():
                pid_markers += 1
                pid = int(value)
            elif marker == "PGID" and value.isdigit():
                pgid_markers += 1
                pgid = int(value)
            elif marker == "IDENTITY" and re.fullmatch(r"[0-9a-f]{64}", value):
                identity_markers += 1
                process_identity_token = value
        started = (
            isinstance(result, dict)
            and result.get("success") is not False
            and result.get("exit_code") == 0
            and not result.get("dispatch_status")
            and pid_markers == 1
            and pgid_markers == 1
            and identity_markers == 1
            and pid is not None
            and pgid is not None
            and pid > 1
            and pid == pgid
            and bool(process_identity_token)
        )

        if started:
            logger.info(
                f"🚀 Dispatched detached command (pid {pid}, pgid {pgid}, "
                f"log {log_path}): {command}"
            )
        else:
            logger.error(
                f"Failed to dispatch detached command: {command} "
                f"(exit={result.get('exit_code')}, output={result.get('output', '')[:200]})"
            )
            # Container-written identity files are useful only after the
            # clean probe returned one unique, self-consistent tuple.  Partial
            # or duplicate markers must not survive as signal authority.
            pid = None
            pgid = None
            process_identity_token = ""

        handle = {
            "started": started,
            "job_id": job_id,
            "pid": pid,
            "pgid": pgid,
            "process_identity_token": process_identity_token,
            "docker_exec_id": docker_exec_id,
            "container_id": container_id,
            "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
            "start_accepted": start_accepted,
            "startup_identity_verified": started,
            "pid_path": pid_path,
            "pgid_path": pgid_path,
            "identity_path": identity_path,
            "log_path": log_path,
            "exit_code_path": exit_code_path,
            "command": command,
            "launch_output": result.get("output", ""),
            "dispatch_status": (
                result.get("dispatch_status")
                if started
                else str(result.get("dispatch_status") or "execution_observation_failed")
            ),
            "runner_dispatched": runner_dispatched,
            "runner_dispatch_state": "accepted",
        }
        if started:
            remembered = self.__dict__.setdefault("_detached_handles", {})
            remembered[job_id] = dict(handle)
        return handle

    def execute_control_command_detached(
        self,
        command: str,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Dispatch detached host-control work without runtime overlay influence."""

        del environment
        return self.execute_command_detached(
            command,
            workdir=workdir,
            _clean_control_path=True,
        )

    def detached_handle(self, job_id: str) -> Dict[str, Any]:
        """Return an in-memory host handle, or an explicitly incomplete one.

        A Docker exec id cannot be reconstructed from a container-controlled
        file or from a job id. Durable restart recovery must obtain it from the
        host-sealed obligation rather than silently downgrading authority.
        """
        if (
            not job_id
            or len(job_id) > 64
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in job_id
            )
        ):
            raise ValueError("invalid detached job id")
        remembered = self.__dict__.get("_detached_handles", {}).get(job_id)
        if isinstance(remembered, dict):
            return dict(remembered)
        log_path = f"{self.DISPATCH_DIR}/{job_id}.log"
        return {
            "job_id": job_id,
            "pid": None,
            "pgid": None,
            "process_identity_token": "",
            "docker_exec_id": "",
            "container_id": "",
            "terminal_authority": DETACHED_TERMINAL_AUTHORITY,
            "start_accepted": False,
            "startup_identity_verified": False,
            "runner_dispatch_state": "unknown",
            "pid_path": f"{self.DISPATCH_DIR}/{job_id}.pid",
            "pgid_path": f"{self.DISPATCH_DIR}/{job_id}.pgid",
            "identity_path": f"{self.DISPATCH_DIR}/{job_id}.identity",
            "log_path": log_path,
            "exit_code_path": f"{log_path}.exit",
        }

    def inspect_detached_terminal(self, handle: Dict[str, Any]) -> Dict[str, Any]:
        """Read detached process truth exclusively from Docker's exec record.

        The immutable exec id and container id are host-side dispatch facts.
        Neither an exit marker nor any other container-controlled byte is a
        fallback when those facts are absent, conflict, or cannot be inspected.
        """

        docker_exec_id = str(handle.get("docker_exec_id") or "").strip()
        expected_container_id = str(handle.get("container_id") or "").strip()
        authority = str(handle.get("terminal_authority") or "").strip()
        start_accepted = handle.get("start_accepted")
        if (
            authority != DETACHED_TERMINAL_AUTHORITY
            or re.fullmatch(r"[0-9a-f]{64}", docker_exec_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", expected_container_id) is None
            or type(start_accepted) is not bool
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "running": False,
                "finished": False,
                "exit_code": None,
                "probe_error": "detached_daemon_identity_missing",
                "start_accepted": False,
            }

        try:
            container = self.client.containers.get(self.container_name)
            current_container_id = str(getattr(container, "id", "") or "").strip()
            if current_container_id != expected_container_id:
                raise ValueError("detached container identity conflict")
            inspected = self.client.api.exec_inspect(docker_exec_id)
        except Exception as exc:
            return {
                "probe_success": False,
                "state": "unknown",
                "running": False,
                "finished": False,
                "exit_code": None,
                "probe_error": f"detached_daemon_inspect_failed:{type(exc).__name__}",
                "start_accepted": bool(start_accepted),
            }

        if not isinstance(inspected, dict):
            inspected = {}
        inspected_exec_id = str(inspected.get("ID") or "").strip()
        inspected_container_id = str(inspected.get("ContainerID") or "").strip()
        running = inspected.get("Running")
        if (
            inspected_exec_id != docker_exec_id
            or inspected_container_id != expected_container_id
            or not isinstance(running, bool)
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "running": False,
                "finished": False,
                "exit_code": None,
                "probe_error": "detached_daemon_identity_conflict",
                "start_accepted": bool(start_accepted),
            }
        if running:
            return {
                "probe_success": True,
                "state": "running",
                "running": True,
                "finished": False,
                "exit_code": None,
                "probe_error": None,
                # A daemon-owned Running=true observation is itself a durable
                # acceptance fact even when exec_start's HTTP response was
                # lost.  Finished=false/exit-zero is not: Docker may retain a
                # created-but-never-started exec in exactly that shape.
                "start_accepted": True,
            }

        if start_accepted is not True:
            return {
                "probe_success": False,
                "state": "unknown",
                "running": False,
                "finished": False,
                "exit_code": None,
                "probe_error": "detached_start_acceptance_unproven",
                "start_accepted": False,
            }

        exit_code = inspected.get("ExitCode")
        if (
            not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not 0 <= exit_code <= 255
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "running": False,
                "finished": False,
                "exit_code": None,
                "probe_error": "detached_daemon_exit_code_invalid",
                "start_accepted": True,
            }
        return {
            "probe_success": True,
            "state": "finished",
            "running": False,
            "finished": True,
            "exit_code": exit_code,
            "probe_error": None,
            "start_accepted": True,
        }

    def poll_detached_command(
        self,
        handle: Dict[str, Any],
        tail_lines: int = 40,
        progress_workdir: Optional[str] = None,
        progress_since: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Poll a detached command: completion state, exit code, and log tail.

        With ``progress_workdir`` + ``progress_since`` the probe also answers
        (spec 2026-08-06 §2, S2): has ANY file under the workdir's build-output
        subtrees been written since ``progress_since`` (container clock, 1s
        slack)? ``NOW:`` always carries the container clock so the caller's
        next ``progress_since`` never mixes host and container time.
        """
        terminal = self.inspect_detached_terminal(handle)
        if terminal.get("probe_success") is not True:
            return {
                "finished": False,
                "running": False,
                "exit_code": None,
                "tail": "",
                "log_size": 0,
                "probe_success": False,
                "terminal_probe_success": False,
                "progress_probe_success": False,
                "state": "unknown",
                "now_epoch": None,
                "progress_fresh": None,
                "probe_error": terminal.get("probe_error") or "detached_daemon_inspect_failed",
            }

        log_path = shlex.quote(handle["log_path"])
        tail_count = max(1, min(1000, int(tail_lines)))
        progress_probe = 'echo "NOW:$(/usr/bin/date +%s)"; '
        if progress_workdir and progress_since is not None:
            quoted_dir = shlex.quote(progress_workdir)
            progress_probe += (
                f"if [ -d {quoted_dir} ]; then "
                f"fresh=$(/usr/bin/find {quoted_dir} "
                f"\\( -path '*/target/*' -o -path '*/build/*' "
                f"-o -path '*/.setup_agent/pytest-reports/*' \\) "
                f"-type f -newermt @{int(progress_since) - 1} -print -quit 2>/dev/null); "
                f'if [ -n "$fresh" ]; then echo "PROGRESS:FRESH"; '
                f'else echo "PROGRESS:NONE"; fi; fi; '
            )
        probe = (
            "set -o pipefail; "
            f'echo "SIZE:$(/usr/bin/wc -c < {log_path} 2>/dev/null || echo 0)"; '
            f"{progress_probe}"
            'echo "---TAIL---"; '
            f"/usr/bin/tail -c {DETACHED_POLL_TAIL_MAX_BYTES} {log_path} 2>/dev/null "
            f"| /usr/bin/tail -n {tail_count} "
            "| /usr/bin/base64 -w 0"
        )
        result = self.execute_control_command(probe, workdir=None, timeout=60)
        transport_succeeded = bool(
            isinstance(result, dict)
            and result.get("success") is not False
            and result.get("exit_code") == 0
            and not result.get("dispatch_status")
        )
        # This probe carries diagnostics only. Terminal truth was already read
        # from Docker; marker-looking bytes in either section have no authority
        # over the physical exit code.
        output = (result.get("output") or "") if transport_succeeded else ""

        # Only the head (before ---TAIL---) carries trusted markers; build
        # output in the tail could itself contain STATE:/SIZE: lines.
        separator_count = output.count("---TAIL---")
        head, separator, tail_section = output.partition("---TAIL---")
        tail = ""

        finished = bool(terminal.get("finished"))
        running = bool(terminal.get("running"))
        state = str(terminal.get("state") or "unknown")
        exit_code = terminal.get("exit_code")
        log_size = 0
        now_epoch: Optional[int] = None
        progress_fresh: Optional[bool] = None
        size_markers = 0
        now_markers = 0
        progress_markers = 0
        malformed = not transport_succeeded or not separator or separator_count != 1
        for line in head.splitlines():
            stripped = line.strip()
            if stripped.startswith("SIZE:"):
                size_markers += 1
                try:
                    log_size = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    malformed = True
                if not 0 <= log_size <= (2**63 - 1):
                    malformed = True
            elif stripped.startswith("NOW:"):
                now_markers += 1
                try:
                    now_epoch = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    malformed = True
                if now_epoch is not None and now_epoch <= 0:
                    malformed = True
            elif stripped == "PROGRESS:FRESH":
                progress_markers += 1
                progress_fresh = True
            elif stripped == "PROGRESS:NONE":
                progress_markers += 1
                progress_fresh = False
            elif stripped:
                malformed = True

        if size_markers != 1 or now_markers != 1 or progress_markers > 1:
            malformed = True
        encoded_tail = tail_section.strip()
        try:
            tail_bytes = base64.b64decode(encoded_tail, validate=True)
        except (binascii.Error, ValueError, TypeError):
            tail_bytes = b""
            malformed = True
        if len(tail_bytes) > DETACHED_POLL_TAIL_MAX_BYTES:
            tail_bytes = b""
            malformed = True
        if not malformed:
            tail = tail_bytes.decode("utf-8", errors="replace").strip()
        progress_probe_success = transport_succeeded and not malformed
        if not progress_probe_success:
            return {
                "finished": False,
                "running": False,
                "exit_code": None,
                "tail": "",
                "log_size": 0,
                "probe_success": False,
                "terminal_probe_success": True,
                "progress_probe_success": False,
                "state": "unknown",
                "now_epoch": None,
                "progress_fresh": None,
                "probe_error": "malformed_detached_log_probe",
            }

        return {
            "finished": finished,
            "running": running,
            "exit_code": exit_code,
            "tail": tail,
            "log_size": log_size,
            "probe_success": True,
            "terminal_probe_success": True,
            "progress_probe_success": progress_probe_success,
            "state": state,
            "now_epoch": now_epoch,
            "progress_fresh": progress_fresh,
            "probe_error": None,
        }

    @staticmethod
    def _detached_poll_state(poll: Dict[str, Any]) -> str:
        if poll.get("probe_success") is not True:
            return "unknown"
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
        *,
        hold: str = "windowed",
        stall_seconds: Optional[int] = None,
        now=None,
        sleep=None,
    ) -> Dict[str, Any]:
        """Dispatch-and-poll execution with a soft window (no hard kill).

        The command runs detached with output in a container log file. If it
        finishes within soft_timeout, the result looks like a normal
        execute_command result. If it is still running or its liveness cannot
        be established when the window closes, the result is a handoff carrying
        the log tail and poll instructions. Only terminal observations are
        collected.

        Stall window (spec 2026-08-06): progress on either signal — stdout
        growth or a build-tree write — resets a stall clock; when it reaches
        ``stall_seconds`` the command is handed off, never killed.
        ``hold="progress"`` (prerequisite dispatches) drops the total window
        and holds while progress continues, bounded ONLY by the engine's
        installed ``hold_deadline_provider``; without that provider the total
        window applies — a missing budget basis degrades to the bounded old
        behavior, never to an unbounded hold. ``hold="windowed"`` (default:
        test-running and unclassifiable dispatches) keeps the total window
        and hands off at min(stall, window).
        """
        import time as _time

        now = now or _time.time
        sleep = sleep or _time.sleep
        config = getattr(self, "config", None)
        if soft_timeout is None:
            soft_timeout = getattr(config, "dispatch_soft_timeout_seconds", 900) or 900
        if poll_interval is None:
            poll_interval = getattr(config, "dispatch_poll_interval_seconds", 15) or 15
        if stall_seconds is None:
            stall_seconds = getattr(config, "dispatch_stall_seconds", 600)
        stall_seconds = max(0, int(stall_seconds or 0))

        handle = self.execute_command_detached(command, workdir=workdir, environment=environment)
        if not handle.get("started"):
            if handle.get("runner_dispatched") is True:
                # Docker accepted and started the exec, but the clean startup
                # identity handshake did not complete. Losing that fact would
                # permit a duplicate dispatch and leave an untracked process.
                # Preserve the daemon-bound handle as pending; if the daemon
                # already has a framed terminal observation, collect it now.
                poll = self.poll_detached_command(handle, tail_lines=tail_lines)
                if self._detached_poll_state(poll) == "finished":
                    return self.collect_detached_result(handle, poll)
                last_tail = str(poll.get("tail") or "")
                return {
                    "success": True,
                    "exit_code": None,
                    "output": (
                        (
                            "Docker accepted the detached runner, but its startup "
                            "PID/PGID identity could not be observed"
                            if handle.get("runner_dispatched") is True
                            else "Docker returned an ambiguous detached start result, "
                            "and daemon inspection could not determine whether it ran"
                        )
                        + " through clean control transport. The daemon-bound handle "
                        "was preserved as pending; no duplicate dispatch is permitted.\n"
                        "Controller-owned job barrier: no model action is requested."
                    ),
                    "termination_reason": None,
                    "dispatch_status": "liveness_unknown_detached",
                    "runner_dispatched": handle.get("runner_dispatched"),
                    "runner_dispatch_state": handle.get("runner_dispatch_state"),
                    "lifecycle_state": "pending",
                    "liveness_state": "unknown",
                    "handoff_reason": "startup_identity_unavailable",
                    "dispatch": {
                        **handle,
                        "last_tail": last_tail,
                        "log_size": int(poll.get("log_size") or 0),
                        "handoff_reason": "startup_identity_unavailable",
                    },
                }
            if handle.get("runner_dispatch_state") == "unknown":
                # A created Docker exec can look terminal/exit-zero even when
                # exec_start never sent its request. Until startup acceptance
                # is proven, daemon terminal state and container-controlled log
                # bytes cannot be promoted into a successful tool result.
                return {
                    "success": False,
                    "exit_code": None,
                    "output": (
                        "Docker returned an ambiguous detached start result. "
                        "No terminal success is claimable until startup acceptance "
                        "has a host-observed identity handshake."
                    ),
                    "termination_reason": None,
                    "dispatch_status": "dispatch_unknown",
                    "runner_dispatched": None,
                    "runner_dispatch_state": "unknown",
                    "lifecycle_state": "pending",
                    "liveness_state": "unknown",
                    "handoff_reason": "startup_acceptance_unproven",
                    "dispatch": {
                        **handle,
                        "handoff_reason": "startup_acceptance_unproven",
                    },
                }
            dispatch_status = str(handle.get("dispatch_status") or "dispatch_failed")
            raw_exit = handle.get("exit_code")
            exit_code = (
                raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else -1
            )
            return {
                "success": False,
                "exit_code": exit_code,
                "output": f"Failed to dispatch command: {handle.get('launch_output', '')}",
                "termination_reason": None,
                "dispatch_status": dispatch_status,
                "runner_dispatched": bool(handle.get("runner_dispatched")),
                "dispatch": handle,
            }

        provider = getattr(self, "hold_deadline_provider", None)
        wall_deadline: Optional[float] = None
        if callable(provider):
            try:
                raw = provider()
                wall_deadline = float(raw) if raw is not None else None
            except Exception:
                wall_deadline = None

        start = now()
        # Short early polls catch quick commands without paying a full interval.
        delays = [2, 5, 10]
        if wall_deadline is not None:
            # The wall guard bounds how long a hold may EXTEND, not whether the
            # dispatch gets to look at all. Inside the report reserve the
            # deadline is already past, and without this floor the loop would
            # break before its first poll: a command that finishes in 2s would
            # come back "pending" with zero observations and open an obligation
            # nothing can settle. The floor costs the early-poll budget only.
            wall_deadline = max(wall_deadline, start + sum(delays))
        # Spec §3/§4.2: the total window drops ONLY when a stall clock AND a
        # wall-clock budget both exist (P2: a missing basis is not permission).
        unbounded = hold == "progress" and stall_seconds > 0 and wall_deadline is not None
        window_deadline = None if unbounded else start + max(1, int(soft_timeout))

        last_progress = start
        last_stdout_growth = start
        last_tree_write: Optional[float] = None
        max_log_size = 0
        since_epoch: Optional[int] = None
        unanswered_probes = 0

        def _next_deadline() -> float:
            candidates = []
            if window_deadline is not None:
                candidates.append(window_deadline)
            if wall_deadline is not None:
                candidates.append(wall_deadline)
            if stall_seconds > 0:
                candidates.append(last_progress + stall_seconds)
            return min(candidates)

        def _poll(with_progress: bool) -> Dict[str, Any]:
            probe_workdir = workdir if (with_progress and stall_seconds > 0) else None
            try:
                return self.poll_detached_command(
                    handle,
                    tail_lines=tail_lines,
                    progress_workdir=probe_workdir,
                    progress_since=since_epoch if with_progress else None,
                )
            except TypeError as exc:
                # Small test orchestrators predate the progress params.
                if "progress_workdir" not in str(exc):
                    raise
                return self.poll_detached_command(handle, tail_lines=tail_lines)

        poll_count = 0
        while True:
            ts = now()
            if ts >= _next_deadline():
                break
            delay = delays[poll_count] if poll_count < len(delays) else poll_interval
            sleep(max(0.05, min(delay, _next_deadline() - ts)))
            poll_count += 1
            poll = _poll(with_progress=True)
            if self._detached_poll_state(poll) in {"finished", "vanished"}:
                return self.collect_detached_result(handle, poll)
            ts = now()
            if poll.get("progress_probe_success", poll.get("probe_success")) is False:
                # Docker may have answered liveness while the clean diagnostic
                # probe did not answer. That is not an observation of quiet —
                # it is no progress observation at all, so it may not be
                # reported as one.
                unanswered_probes += 1
                continue
            unanswered_probes = 0
            size = int(poll.get("log_size") or 0)
            if size > max_log_size:
                max_log_size = size
                last_stdout_growth = ts
                last_progress = ts
            if poll.get("progress_fresh"):
                last_tree_write = ts
                last_progress = ts
            if poll.get("now_epoch"):
                since_epoch = int(poll["now_epoch"])

        final_poll = _poll(with_progress=False)
        final_state = self._detached_poll_state(final_poll)
        if final_state in {"finished", "vanished"}:
            return self.collect_detached_result(handle, final_poll)

        ts = now()
        if stall_seconds > 0 and ts >= last_progress + stall_seconds:
            handoff_reason = "stalled"
        elif window_deadline is not None and ts >= window_deadline:
            handoff_reason = "window"
        else:
            handoff_reason = "wall_clock"

        held = int(ts - start)
        quiet = int(ts - last_progress)
        if unanswered_probes:
            # Spec §5: state observations. "stdout last grew Ns ago" would be a
            # claim about a log the last probes never read.
            stall_observations = (
                f"the last {unanswered_probes} liveness probe(s) did not answer, "
                f"so progress could not be observed for {quiet}s"
            )
        else:
            tree_line = (
                f"last build-tree write observed {int(ts - last_tree_write)}s ago"
                if last_tree_write is not None
                else "no build-tree writes observed since dispatch"
            )
            stall_observations = (
                f"stdout last grew {int(ts - last_stdout_growth)}s ago "
                f"(log size {max_log_size} bytes); {tree_line}"
            )
        liveness_unknown = final_state == "unknown"
        dispatch_status = "liveness_unknown_detached" if liveness_unknown else "running_detached"
        if liveness_unknown:
            logger.warning(
                f"Hold ended ({handoff_reason}) without a conclusive liveness "
                f"probe; preserving detached command handle (pid {handle['pid']}, "
                f"log {handle['log_path']})"
            )
            handoff_summary = (
                "Command liveness could not be established when the hold ended. "
                "Its detached handle was preserved and the operation remains pending."
            )
            if handoff_reason == "stalled":
                # A stall handoff carries its per-signal observations even when
                # the last probe came back inconclusive: the reason travels in
                # the result and the metadata, so the text must match it.
                handoff_summary += (
                    f"\nNo observable progress for {quiet}s before that.\n"
                    f"Observations: {stall_observations}."
                )
        elif handoff_reason == "stalled":
            logger.info(
                f"⏳ Stall window ({stall_seconds}s) reached after {held}s; handing off "
                f"still-running command (pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command handed off after {held}s: no observable progress for "
                f"{quiet}s — it was left running in the background (NOT killed).\n"
                f"Observations: {stall_observations}."
            )
        elif handoff_reason == "wall_clock":
            logger.info(
                f"⏳ Report reserve reached after {held}s of holding; handing off "
                f"still-running command (pid {handle['pid']}, log {handle['log_path']})"
            )
            handoff_summary = (
                f"⏳ Command still running after {held}s — the run's report reserve "
                "was reached, so the harness stopped holding. It was left running "
                "in the background (NOT killed)."
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
            "Controller-owned job barrier: the harness will poll this registered job, "
            "reconcile its terminal marker, and settle its evidence. No model action is "
            "requested; do not poll or dispatch this job again."
        )
        return {
            "success": True,
            "exit_code": None,
            "output": handoff_output,
            "termination_reason": None,
            "dispatch_status": dispatch_status,
            "runner_dispatched": True,
            "lifecycle_state": "pending",
            "liveness_state": final_state,
            "handoff_reason": handoff_reason,
            "dispatch": {
                **handle,
                "last_tail": final_poll.get("tail", ""),
                "log_size": final_poll.get("log_size", 0),
                "soft_timeout": soft_timeout,
                "handoff_reason": handoff_reason,
            },
        }

    def collect_detached_result(
        self, handle: Dict[str, Any], poll: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build an execute_command-shaped result for a finished detached command."""
        state = self._detached_poll_state(poll)
        exit_code = poll.get("exit_code")
        accepted = bool(
            handle.get("start_accepted") is True
            and handle.get("runner_dispatch_state") == "accepted"
            and handle.get("runner_dispatched") is True
        )
        terminal_observed = bool(
            poll.get("probe_success") is True
            and poll.get("terminal_probe_success") is True
            and state == "finished"
            and type(exit_code) is int
            and 0 <= exit_code <= 255
        )
        if not accepted or not terminal_observed:
            return {
                "success": False,
                "exit_code": None,
                "output": (
                    "Detached terminal result is unavailable because daemon start "
                    "acceptance or the complete terminal observation was not proven."
                ),
                "full_output": "",
                "termination_reason": None,
                "dispatch_status": (
                    "dispatch_unknown" if not accepted else "execution_observation_failed"
                ),
                "runner_dispatched": handle.get("runner_dispatched"),
                "runner_dispatch_state": handle.get("runner_dispatch_state"),
                "dispatch": handle,
                "lifecycle_state": "pending",
                "execution_observation_complete": False,
            }
        # Read the complete log (truncate_output=False): a finished build log is
        # exactly what the agent needs to diagnose a failure, and the orchestrator's
        # emergency truncation would otherwise gut the middle of it (only first/last
        # 25 lines survive), hiding the real compiler/reactor error. The full text
        # goes into `full_output` for the build tools to persist to the output store;
        # the inline `output` stays bounded so it never floods the model context.
        log_result = self.execute_control_command(
            f"cat {shlex.quote(handle['log_path'])}",
            workdir=None,
            timeout=120,
            truncate_output=False,
        )
        log_observed = bool(
            isinstance(log_result, dict)
            and log_result.get("success") is not False
            and log_result.get("exit_code") == 0
            and not log_result.get("dispatch_status")
        )
        if not log_observed:
            tail = str(poll.get("tail") or "")
            diagnostic = "[detached terminal log could not be read through clean control transport]"
            incomplete_output = f"{tail}\n{diagnostic}" if tail else diagnostic
            return {
                "success": False,
                "exit_code": exit_code,
                "output": incomplete_output,
                "full_output": incomplete_output,
                "termination_reason": None,
                "dispatch_status": "execution_observation_failed",
                "runner_dispatched": True,
                "dispatch": handle,
                "lifecycle_state": state,
                "execution_observation_complete": False,
            }

        full_output = log_result.get("output") or ""
        inline_output = full_output
        if len(inline_output) > DETACHED_INLINE_OUTPUT_MAX_CHARS:
            inline_output = self._truncate_output_smartly(full_output)
        if len(inline_output) > DETACHED_INLINE_OUTPUT_MAX_CHARS:
            marker = "\n...[detached inline output clipped]...\n"
            content_budget = DETACHED_INLINE_OUTPUT_MAX_CHARS - len(marker)
            head_budget = content_budget // 2
            tail_budget = content_budget - head_budget
            inline_output = full_output[:head_budget] + marker + full_output[-tail_budget:]

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "output": inline_output,
            "full_output": full_output,
            "termination_reason": None,
            "dispatch_status": "completed_detached",
            "runner_dispatched": True,
            "dispatch": handle,
            "lifecycle_state": state,
            "execution_observation_complete": True,
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
        raw_output_buffer = []
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
                    raw_output_buffer.append(decoded_chunk)
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
                    raw_output_buffer.append(decoded_chunk)
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

            # The process exit and output completeness are separate facts.
            # Docker's stream result commonly has exit_code=None even after
            # EOF; only a same-exec, same-container terminal inspect can fill it.
            observed_exit_code = getattr(exec_result, "exit_code", None)
            exec_id = getattr(exec_result, "docker_exec_id", None)
            if exec_id is not None:
                observed_exit_code = None
                try:
                    inspected = self.client.api.exec_inspect(exec_id)
                    candidate = inspected.get("ExitCode")
                    if (
                        inspected.get("ID") == exec_id
                        and inspected.get("ContainerID")
                        == getattr(exec_result, "docker_container_id", None)
                        and inspected.get("Running") is False
                        and type(candidate) is int
                        and 0 <= candidate <= 255
                    ):
                        observed_exit_code = candidate
                except Exception as exc:
                    logger.warning(f"Monitored exec terminal inspection unavailable: {exc}")
            if type(observed_exit_code) is not int or not 0 <= observed_exit_code <= 255:
                observed_exit_code = None
            exit_code = observed_exit_code
            observation_complete = exit_code is not None and not stream_broken
            complete_output = "".join(raw_output_buffer)
            if not observation_complete:
                detail = (
                    "output stream was lost; terminal exit/output observation is incomplete"
                    if stream_broken
                    else "terminal exit code was not observed for this Docker exec"
                )
                full_output += f"\n[{detail}; result unavailable]"

            # Apply truncation if needed
            if len(full_output) > 10000:
                full_output = self._truncate_output_smartly(full_output)

            success = (
                observation_complete
                and exit_code == 0
                and monitoring_state["termination_reason"] is None
            )

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
                "exit_code": exit_code,
                "observed_exit_code": observed_exit_code,
                "exit_code_inferred": False,
                "output": full_output,
                "full_output": complete_output,
                "execution_observation_complete": observation_complete,
                **(
                    {"dispatch_status": "execution_observation_failed"}
                    if not observation_complete
                    else {}
                ),
                "termination_reason": monitoring_state["termination_reason"],
                "monitoring_info": monitoring_info,
            }

        except Exception as e:
            logger.error(f"Error during execution monitoring: {e}")
            return {
                "success": False,
                "exit_code": None,
                "observed_exit_code": None,
                "exit_code_inferred": False,
                "execution_observation_complete": False,
                "dispatch_status": "execution_observation_failed",
                "full_output": "".join(raw_output_buffer),
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
            result = self.execute_control_command(probe, workdir=None, timeout=30)
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
            result = self.execute_control_command(
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
            result = self.execute_control_command(
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
                result = self.execute_control_command(
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
            update_result = self.execute_control_command("apt-get update -qq", workdir=None)
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
                "ripgrep",
                "sed",
                "findutils",
                "less",
                "unzip",
                "procps",
                "util-linux",
                "coreutils",
                "iproute2",
            ]

            install_command = f"apt-get install -y -qq {' '.join(essential_packages)}"
            logger.info(f"📦 Installing essential packages: {' '.join(essential_packages)}")

            install_result = self.execute_control_command(install_command, workdir=None)

            if not install_result["success"]:
                logger.error("❌ Essential package installation failed")
                logger.error(f"Exit code: {install_result.get('exit_code', 'unknown')}")
                logger.error(f"Output: {install_result.get('output', 'no output')}")

                # Try to install Git separately as it's critical for the workflow
                logger.info("🔧 Attempting to install Git separately...")
                git_result = self.execute_control_command("apt-get install -y git", workdir=None)
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
                    "command -v setsid ps sha256sum timeout base64 >/dev/null",
                    "Detached job controller prerequisites",
                ),
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
                result = self.execute_control_command(cmd, workdir=None)
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
            script_result = self.execute_control_command(
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
