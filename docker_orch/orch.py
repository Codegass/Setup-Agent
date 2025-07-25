"""Docker Orchestrator for managing containers and volumes."""

import os
import subprocess
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound
from loguru import logger

from config import get_config


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

        if not self.is_container_running():
            raise RuntimeError(f"Container {self.container_name} is not running")

        # Use docker exec to connect interactively
        cmd = ["docker", "exec", "-it", self.container_name, shell]

        logger.info(f"Connecting to container with command: {' '.join(cmd)}")

        try:
            # Replace current process with docker exec
            os.execvp("docker", cmd)
        except Exception as e:
            logger.error(f"Failed to connect to container: {e}")
            raise

    def _is_json_content(self, output: str, command: str) -> bool:
        """
        检测是否为JSON内容，避免对JSON文件进行破坏性截断
        """
        # 如果command包含.json文件路径
        if '.json' in command and ('cat' in command or 'head' in command or 'tail' in command):
            return True
        
        # 如果输出内容看起来像JSON结构
        stripped = output.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                import json
                json.loads(stripped)  # 验证是否为有效JSON
                return True
            except json.JSONDecodeError:
                pass
        
        return False

    def _smart_json_truncate(self, json_content: str, max_entries: int = 10) -> str:
        """
        智能截断JSON内容，保持JSON有效性
        主要针对context history文件进行安全压缩
        """
        try:
            import json
            data = json.loads(json_content)
            
            # 如果是branch context history，可以安全截断history数组
            if isinstance(data, dict) and 'history' in data and isinstance(data['history'], list):
                history = data['history']
                if len(history) > max_entries:
                    # 保留前5个和后5个history条目，中间标记截断
                    truncated_count = len(history) - max_entries
                    data['history'] = (
                        history[:5] + 
                        [{"type": "truncated", "message": f"[SMART TRUNCATION: {truncated_count} entries omitted to prevent context pollution]", "timestamp": "system"}] +
                        history[-5:]
                    )
                    # 更新元数据
                    data['entry_count'] = len(data['history'])
                    # 重新计算token count
                    if 'token_count' in data:
                        data['token_count'] = len(json.dumps(data)) // 4
                    
                    logger.info(f"📊 Applied smart JSON truncation: {len(history)} → {len(data['history'])} entries")
                    return json.dumps(data, indent=2)
            
            # 如果无法安全截断，返回原内容（但会有警告）
            logger.warning("🚨 Large JSON file detected but cannot be safely truncated - preserving integrity")
            return json_content
            
        except json.JSONDecodeError:
            # 如果不是有效JSON，返回原内容
            return json_content

    def execute_command(self, command: str, workdir: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute a command in the container.

        Args:
            command: The command to execute.
            workdir: The working directory to execute the command in.

        Returns:
            A dictionary with the result of the command execution.
        """
        # Ensure container is running and get container object
        if not self.is_container_running():
            if not self.container_exists():
                raise RuntimeError(f"Container {self.container_name} does not exist. Create it first.")
            if not self.start_container():
                raise RuntimeError(f"Failed to start container {self.container_name}")
        
        # Get the container object
        container = self.client.containers.get(self.container_name)

        # Build the command to be executed in the container with proper environment loading
        # Source profile to ensure all environment variables (JAVA_HOME, M2_HOME, PATH) are loaded
        wrapped_command = f"source /etc/profile 2>/dev/null || true; source ~/.bashrc 2>/dev/null || true; {command}"
        exec_command = ["/bin/bash", "-c", wrapped_command]
        
        logger.info(f"Executing command in container: {command}")
        if workdir:
            logger.info(f"Working directory: {workdir}")

        try:
            # Execute the command
            result = container.exec_run(exec_command, workdir=workdir)

            # Decode output
            output = result.output.decode("utf-8").strip()
            exit_code = result.exit_code

            logger.debug(f"Command finished with exit code: {exit_code}")
            
            # IMPROVED: JSON-aware truncation logic
            original_length = len(output)
            if original_length > 10000:  # ~100 lines threshold
                lines = output.split('\n')
                if len(lines) > 100:
                    # Check if this is JSON content that needs protection
                    if self._is_json_content(output, command):
                        # For JSON files, apply smart truncation that preserves validity
                        output = self._smart_json_truncate(output, max_entries=10)
                        logger.info(f"🔧 Applied JSON-aware truncation to preserve file integrity")
                    else:
                        # Apply normal truncation for non-JSON content
                        truncated = '\n'.join(lines[:25]) + f"\n... [ORCHESTRATOR TRUNCATED: {len(lines)} lines, {original_length} chars] ...\n" + '\n'.join(lines[-25:])
                        logger.warning(f"🚨 Orchestrator applied emergency truncation: {len(lines)} lines → 50 lines to prevent context pollution")
                        output = truncated
            
            # Smart debug logging: show structure of truncated output
            if original_length > 10000 and len(output.split('\n')) <= 60:  # If we applied truncation
                # For truncated output, show the structure more clearly
                output_lines = output.split('\n')
                if len(output_lines) > 10:
                    debug_display = '\n'.join(output_lines[:5]) + f"\n... [Truncated output: showing first 5 + last 5 lines of {len(output_lines)} total] ...\n" + '\n'.join(output_lines[-5:])
                else:
                    debug_display = output
                logger.debug(f"Command output (showing truncation structure):\n{debug_display}")
            else:
                # For normal output, use character limit
                debug_output = output[:500] + "..." if len(output) > 500 else output
                logger.debug(f"Command output (truncated for logs):\n{debug_output}")

            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "output": output
            }
        except Exception as e:
            logger.error(f"Failed to execute command '{command}': {e}")
            return {
                "success": False,
                "exit_code": -1,
                "output": str(e)
            }

    def execute_command_with_monitoring(
        self,
        command: str,
        workdir: str = None,
        silent_timeout: int = 600,  # 10 minutes no output
        absolute_timeout: int = 2400,  # 40 minutes total
        use_timeout_wrapper: bool = True,
        enable_cpu_monitoring: bool = True,
        optimize_for_maven: bool = True
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
        if optimize_for_maven and ('mvn ' in command or command.startswith('mvn')):
            command = self._optimize_maven_command(command, absolute_timeout)
            logger.info(f"🔧 Applied Maven optimizations to command")
        
        # Wrap with GNU timeout if requested
        if use_timeout_wrapper:
            # Use timeout with preserve-status to get the actual exit code
            wrapped_cmd = f"timeout --preserve-status {absolute_timeout} bash -c '{command}'"
            logger.info(f"🕐 Wrapped command with {absolute_timeout}s absolute timeout")
        else:
            wrapped_cmd = command
        
        # Build the final command with environment loading
        final_command = f"source /etc/profile 2>/dev/null || true; source ~/.bashrc 2>/dev/null || true; {wrapped_cmd}"
        exec_command = ["/bin/bash", "-c", final_command]
        
        logger.info(f"Executing command with monitoring: {command}")
        if workdir:
            logger.info(f"Working directory: {workdir}")
        logger.info(f"⏱️ Timeouts: Silent={silent_timeout}s, Absolute={absolute_timeout}s")
        
        # Monitoring state
        monitoring_state = {
            'last_output_time': time.time(),
            'start_time': time.time(),
            'total_output': '',
            'process_terminated': False,
            'termination_reason': None,
            'cpu_warnings': 0
        }
        
        try:
            # Start the command execution
            exec_result = container.exec_run(
                exec_command, 
                workdir=workdir,
                stream=True,  # Enable streaming to monitor output
                demux=True    # Separate stdout/stderr
            )
            
            # Start CPU monitoring thread if enabled
            cpu_monitor_thread = None
            if enable_cpu_monitoring:
                cpu_monitor_thread = threading.Thread(
                    target=self._monitor_cpu_usage,
                    args=(monitoring_state, silent_timeout // 2),  # Check every half of silent timeout
                    daemon=True
                )
                cpu_monitor_thread.start()
            
            # Monitor the execution with timeouts
            result = self._monitor_execution_with_timeouts(
                exec_result, 
                monitoring_state, 
                silent_timeout, 
                absolute_timeout
            )
            
            # Clean up monitoring thread
            monitoring_state['process_terminated'] = True
            if cpu_monitor_thread:
                cpu_monitor_thread.join(timeout=1)  # Give it 1 second to finish
            
            return result
            
        except Exception as e:
            monitoring_state['process_terminated'] = True
            logger.error(f"Failed to execute command '{command}': {e}")
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Execution failed: {str(e)}",
                "termination_reason": "exception",
                "monitoring_info": monitoring_state
            }

    def _optimize_maven_command(self, command: str, timeout_seconds: int) -> str:
        """Apply Maven-specific optimizations to reduce timeout risks."""
        
        optimizations = []
        
        # Add batch mode and quiet flags if not present
        if '-B' not in command:
            optimizations.append('-B')  # Batch mode (non-interactive)
        
        if '-q' not in command and '-X' not in command:  # Don't add quiet if debug mode
            optimizations.append('-q')  # Quiet mode (reduce output)
        
        # Add Maven-specific timeout settings
        maven_timeout_props = [
            f'-Dmaven.execution.timeout={timeout_seconds}000',  # Maven timeout in milliseconds
            '-Dmaven.artifact.threads=4',  # Parallel downloads
            '-Dmaven.resolver.transport=wagon',  # Use wagon transport for better reliability
        ]
        
        # Insert optimizations after 'mvn' but before other arguments
        parts = command.split(' ', 1)
        if len(parts) == 2:
            maven_cmd, remaining_args = parts
            optimized_command = f"{maven_cmd} {' '.join(optimizations)} {' '.join(maven_timeout_props)} {remaining_args}"
        else:
            optimized_command = f"{command} {' '.join(optimizations)} {' '.join(maven_timeout_props)}"
        
        logger.info(f"🔧 Maven optimizations applied: {', '.join(optimizations + maven_timeout_props)}")
        return optimized_command

    def _monitor_execution_with_timeouts(
        self, 
        exec_result, 
        monitoring_state: dict, 
        silent_timeout: int, 
        absolute_timeout: int
    ) -> Dict[str, Any]:
        """Monitor command execution with dual timeout mechanism."""
        
        output_buffer = []
        last_chunk_time = time.time()
        
        try:
            # Read output stream with timeout monitoring
            for chunk in exec_result.output:
                current_time = time.time()
                
                # Check absolute timeout
                if current_time - monitoring_state['start_time'] > absolute_timeout:
                    logger.error(f"⏰ ABSOLUTE TIMEOUT: Command exceeded {absolute_timeout}s limit")
                    monitoring_state['termination_reason'] = 'absolute_timeout'
                    self._terminate_container_processes()
                    break
                
                # Check silent timeout
                if current_time - last_chunk_time > silent_timeout:
                    logger.warning(f"🔇 SILENT TIMEOUT: No output for {silent_timeout}s")
                    monitoring_state['termination_reason'] = 'silent_timeout'
                    self._terminate_container_processes()
                    break
                
                # Process the chunk
                if chunk[0]:  # stdout
                    decoded_chunk = chunk[0].decode('utf-8')
                    output_buffer.append(decoded_chunk)
                    monitoring_state['total_output'] += decoded_chunk
                    last_chunk_time = current_time
                    monitoring_state['last_output_time'] = current_time
                    
                    # Log progress periodically
                    if len(output_buffer) % 50 == 0:  # Every 50 chunks
                        elapsed = current_time - monitoring_state['start_time']
                        logger.info(f"📊 Progress: {len(output_buffer)} chunks, {elapsed:.1f}s elapsed")
                
                if chunk[1]:  # stderr
                    decoded_chunk = chunk[1].decode('utf-8')
                    output_buffer.append(f"STDERR: {decoded_chunk}")
                    last_chunk_time = current_time
                    monitoring_state['last_output_time'] = current_time
            
            # Get final execution result
            exit_code = exec_result.exit_code
            
            # For streaming execution, exit_code might be None until stream is fully consumed
            if exit_code is None:
                # If we got output without errors, assume success
                exit_code = 0
            
            # Combine all output
            full_output = ''.join(output_buffer)
            
            # Apply truncation if needed
            if len(full_output) > 10000:
                full_output = self._truncate_output_smartly(full_output)
            
            success = exit_code == 0 and monitoring_state['termination_reason'] is None
            
            # Generate monitoring summary
            monitoring_info = {
                'execution_time': time.time() - monitoring_state['start_time'],
                'termination_reason': monitoring_state['termination_reason'],
                'cpu_warnings': monitoring_state['cpu_warnings'],
                'output_chunks': len(output_buffer)
            }
            
            if not success and monitoring_state['termination_reason']:
                logger.error(f"❌ Command terminated due to: {monitoring_state['termination_reason']}")
            
            return {
                "success": success,
                "exit_code": exit_code or 0,
                "output": full_output,
                "termination_reason": monitoring_state['termination_reason'],
                "monitoring_info": monitoring_info
            }
            
        except Exception as e:
            logger.error(f"Error during execution monitoring: {e}")
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Monitoring error: {str(e)}",
                "termination_reason": "monitoring_error",
                "monitoring_info": monitoring_state
            }

    def _monitor_cpu_usage(self, monitoring_state: dict, check_interval: int):
        """Monitor CPU usage to detect hung processes."""
        
        consecutive_low_cpu = 0
        cpu_threshold = 1.0  # Consider CPU usage below 1% as potentially hung
        
        while not monitoring_state['process_terminated']:
            try:
                time.sleep(check_interval)
                
                if monitoring_state['process_terminated']:
                    break
                
                # Get CPU stats
                container = self.client.containers.get(self.container_name)
                stats = container.stats(stream=False)
                
                # Calculate CPU percentage
                cpu_percent = self._calculate_cpu_percentage(stats)
                
                current_time = time.time()
                silent_duration = current_time - monitoring_state['last_output_time']
                
                # Check for potential hang: low CPU + no output for a while
                if cpu_percent < cpu_threshold and silent_duration > check_interval:
                    consecutive_low_cpu += 1
                    monitoring_state['cpu_warnings'] += 1
                    
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
            cpu_stats = stats['cpu_stats']
            precpu_stats = stats['precpu_stats']
            
            cpu_delta = cpu_stats['cpu_usage']['total_usage'] - precpu_stats['cpu_usage']['total_usage']
            system_delta = cpu_stats['system_cpu_usage'] - precpu_stats['system_cpu_usage']
            
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * len(cpu_stats['cpu_usage']['percpu_usage']) * 100.0
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
        lines = output.split('\n')
        
        if len(lines) <= 100:
            return output
        
        # Keep more lines from the end (recent output) than the beginning
        head_lines = 30
        tail_lines = 50
        
        truncated = (
            '\n'.join(lines[:head_lines]) +
            f"\n... [TRUNCATED: {len(lines) - head_lines - tail_lines} lines omitted] ...\n" +
            '\n'.join(lines[-tail_lines:])
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
            "environment": {"DEBIAN_FRONTEND": "noninteractive", "TERM": "xterm-256color"},
            "labels": {
                "setup-agent.project": self.project_name,
                "setup-agent.created": datetime.now().isoformat(),
            },
            # Add a command to keep container running
            "command": ["/bin/bash", "-c", f"mkdir -p {self.config.workspace_path} && while true; do sleep 30; done"],
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
                result = self.execute_command(command, workdir=None)  # Use no workdir for workspace creation
                
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
                "curl", "wget", "git", "nano", "vim", 
                "python3", "python3-pip", "nodejs", "npm", 
                "build-essential", "grep", "findutils", "less"
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
                    logger.error("❌ CRITICAL: Git installation failed - this will cause chain failure B")
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
                (f"test -d {self.config.workspace_path} && echo 'Workspace exists' || echo 'Workspace missing'", "Workspace"),
                (f"test -f {self.config.workspace_path}/.sag_workspace_marker && echo 'Marker exists' || echo 'Marker missing'", "Workspace marker"),
            ]
            
            logger.info("🔍 Verifying critical tools and workspace...")
            verification_failed = False
            
            for cmd, tool_name in verification_commands:
                result = self.execute_command(cmd, workdir=None)
                if result["success"]:
                    output_summary = result["output"][:100] + "..." if len(result["output"]) > 100 else result["output"]
                    logger.info(f"✅ {tool_name}: {output_summary}")
                else:
                    logger.error(f"❌ {tool_name} verification failed")
                    verification_failed = True
                    
                    # Special handling for critical failures
                    if tool_name == "Git":
                        logger.error("❌ CRITICAL: Git verification failed - this will cause project clone failures")
                    elif tool_name == "Workspace":
                        logger.error("❌ CRITICAL: Workspace verification failed - this will cause OCI runtime exec failures")

            # STEP 4: Create environment script for persistent environment variables
            env_script = f"""#!/bin/bash
# SAG Environment Setup Script
export WORKSPACE_PATH="{self.config.workspace_path}"
export SAG_CONTAINER_INITIALIZED="true"
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
                f'echo \'{env_script}\' > /etc/profile.d/sag_env.sh && chmod +x /etc/profile.d/sag_env.sh',
                workdir=None
            )
            
            if script_result["success"]:
                logger.info("✅ Environment script created successfully")
            else:
                logger.warning("⚠️ Failed to create environment script")

            if verification_failed:
                logger.error("❌ Environment setup completed with failures")
                return False
            else:
                logger.info("✅ Container environment setup completed successfully with all verifications passing")
                return True

        except Exception as e:
            logger.error(f"❌ Failed to setup container environment: {e}")
            return False
