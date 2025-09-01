"""Bash tool for executing shell commands with specialized grep functionality."""

import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from loguru import logger

from .base import BaseTool, ToolResult, ToolError


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

    def _ensure_working_directory(self, requested_workdir: str) -> str:
        """Smart working directory validation and setup."""
        return self._validate_and_fix_working_directory(requested_workdir)
    
    def _validate_command(self, command: str) -> tuple[bool, str]:
        """Validate command against allowlist/blocklist.
        
        Returns:
            (is_valid, error_message)
        """
        # Split command chains (&&, ||, ;) and validate each part
        command_parts = re.split(r'\s*(?:&&|\|\||;)\s*', command)
        
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
        if '<(' in command or '>(' in command:
            return False, "Process substitution is not allowed for security reasons"
        
        return True, ""
    
    def _is_background_command(self, command: str) -> bool:
        """Check if command should run in background."""
        # Check if command ends with & (not &&)
        command = command.strip()
        return command.endswith('&') and not command.endswith('&&')
    
    def _detect_interactive_command(self, command: str) -> tuple[bool, str]:
        """Detect if command requires interactive input.
        
        Returns:
            (is_interactive, suggestion)
        """
        interactive_patterns = [
            (r'\bnpm init\b(?!.*-y)', 'Use "npm init -y" for non-interactive mode'),
            (r'\bgit rebase -i\b', 'Interactive rebase not supported in containers'),
            (r'\bgit add -i\b', 'Interactive add not supported, use specific file paths'),
            (r'\bvim?\b|\bnano\b|\bemacs\b', 'Text editors not supported, use file manipulation tools'),
            (r'\bsudo -S\b', 'Password input not supported, configure sudoers if needed'),
            (r'\bpasswd\b', 'Password changes not supported in container'),
            (r'\bssh\b.*@', 'SSH with password not supported, use key-based auth'),
            (r'\btop\b|\bhtop\b', 'Interactive monitoring not supported, use one-shot commands'),
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
            
        lines = output.split('\n')
        total_lines = len(lines)
        
        # CRITICAL: Detect verbose package management commands by COMMAND, not output
        command_lower = command.lower()
        is_verbose_package_cmd = any(pattern in command_lower for pattern in [
            'apt-get install', 'apt install', 'yum install', 'dnf install',
            'npm install', 'pip install', 'cargo install', 'go get'
        ])
        
        if is_verbose_package_cmd and total_lines > 50:
            # For verbose package commands, use AGGRESSIVE truncation
            logger.info(f"🔧 Detected verbose package command with {total_lines} lines, applying aggressive truncation")
            
            # Keep only: head (25 lines) + tail (25 lines) = 50 lines total
            key_start = lines[:25]
            key_end = lines[-25:]
            
            # Extract critical status lines from the middle if any
            critical_lines = []
            for line in lines[10:-10]:  # Skip already included start/end
                line_lower = line.lower()
                if any(critical in line_lower for critical in [
                    'error:', 'failed:', 'could not', 'unable to', 'permission denied',
                    'build success', 'build failure', 'completed successfully',
                    'warning:', 'critical:'
                ]):
                    critical_lines.append(line)
                    if len(critical_lines) >= 5:  # Limit critical lines to prevent spam
                        break
            
            result_parts = []
            result_parts.extend(key_start)
            if critical_lines:
                result_parts.append(f"\n... [Key status messages from {total_lines} lines] ...")
                result_parts.extend(critical_lines)
            result_parts.append(f"\n... [Skipped {total_lines - 50 - len(critical_lines)} lines of verbose output] ...")
            result_parts.extend(key_end)
            
            return '\n'.join(result_parts)
        
        # If this looks like grep output, preserve more context
        if any(line.strip() and ':' in line for line in lines[:10]):
            # This might be grep output with file:line:content format
            key_lines = []
            for line in lines:
                if line.strip():
                    # Preserve grep results with context
                    key_lines.append(line)
                    if len(key_lines) >= 50:  # Keep more grep results
                        break
            
            if key_lines:
                result = '\n'.join(key_lines)
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
            if any(keyword in line_lower for keyword in [
                'error:', 'exception:', 'failed:', 'warning:', 'critical:',
                'build success', 'build failure', 'success:', 'completed:'
            ]):
                if 'error' in line_lower or 'exception' in line_lower or 'failed' in line_lower:
                    error_lines.append(f"🚨 {line.strip()}")
                else:
                    key_lines.append(f"✅ {line.strip()}")
            
            # File operations (medium priority)
            elif any(keyword in line_lower for keyword in [
                'created:', 'copied:', 'moved:', 'deleted:', 'modified:'
            ]):
                key_lines.append(f"📁 {line.strip()}")
            
            # Package management (low priority - be selective)
            elif any(keyword in line_lower for keyword in [
                'installed successfully', 'removed successfully', 'updated successfully',
                'package not found', 'dependency error'
            ]):
                key_lines.append(f"📦 {line.strip()}")
            
            # Git operations (medium priority)
            elif any(keyword in line_lower for keyword in [
                'commit', 'push', 'pull', 'branch', 'merge'
            ]):
                key_lines.append(f"🔄 {line.strip()}")
        
        # Combine results with strict limits
        result_lines = error_lines[:10] + key_lines[:30]  # Limit to prevent bloat
        if result_lines:
            if total_lines > len(result_lines) + 10:
                result_lines.append(f"... [Extracted {len(result_lines)} key lines from {total_lines} total]")
            return '\n'.join(result_lines)
        
        # If no key patterns found, apply strict truncation
        if len(output) > 1500:  # Reduced from 2000
            truncated = output[:800] + "\n... [Output truncated to prevent context pollution] ..." + output[-400:]
            logger.info(f"🔧 Applied fallback truncation: {len(output)} chars → {len(truncated)} chars")
            return truncated
        
        return output

    def execute(self, command: str, working_directory: str = "/workspace", 
                environment: Optional[Dict[str, str]] = None) -> ToolResult:
        """
        Execute a bash command in the Docker container with enhanced monitoring.
        
        Args:
            command: The bash command to execute
            working_directory: Working directory (default: /workspace)
            environment: Additional environment variables to set
        """
        
        if not command or not command.strip():
            raise ToolError(
                message="Command cannot be empty",
                suggestions=["Provide a valid bash command to execute"],
                error_code="EMPTY_COMMAND"
            )
        
        if not self.docker_orchestrator:
            raise ToolError(
                message="Docker orchestrator not available",
                suggestions=["Ensure Docker is running and container is available"],
                error_code="NO_ORCHESTRATOR"
            )
        
        # Validate command against security policies
        is_valid, error_msg = self._validate_command(command)
        if not is_valid:
            raise ToolError(
                message=f"Command validation failed: {error_msg}",
                suggestions=[
                    "Check if the command is allowed by security policy",
                    "Use alternative commands that are permitted",
                    "Contact administrator to update security policy if needed"
                ],
                error_code="COMMAND_BLOCKED"
            )
        
        # Check for interactive commands
        is_interactive, suggestion = self._detect_interactive_command(command)
        if is_interactive and self.config.block_interactive_commands:
            raise ToolError(
                message="Interactive command detected",
                suggestions=[suggestion, "Use non-interactive alternatives"],
                error_code="INTERACTIVE_COMMAND"
            )
        
        # Smart working directory validation and setup
        workdir = self._ensure_working_directory(working_directory)
        
        # Check if this is a background command
        is_background = self._is_background_command(command)
        if is_background and self.config.enable_background_processes:
            # Remove the trailing & for processing
            command = command.rstrip().rstrip('&').rstrip()
            logger.info(f"🚀 Detected background command: {command}")
        
        # Prepare environment variables
        env_vars = environment or {}
        if self.config.add_sag_cli_marker:
            env_vars['SAG_CLI'] = '1'
        
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
            
            if is_long_running_command:
                # Get dynamic timeouts based on command type
                silent_timeout, absolute_timeout = self._get_command_timeout(command)
                logger.info(f"🔍 Using enhanced monitoring for long-running command (silent: {silent_timeout}s, total: {absolute_timeout}s)")
                
                result = self.docker_orchestrator.execute_command_with_monitoring(
                    command=command,
                    workdir=workdir,
                    silent_timeout=silent_timeout,
                    absolute_timeout=absolute_timeout,
                    use_timeout_wrapper=True,
                    enable_cpu_monitoring=True,
                    optimize_for_maven=('mvn' in command)
                    # Note: env vars handled in wrapped command for monitoring
                )
            else:
                # Use regular execution for normal commands
                result = self.docker_orchestrator.execute_command(
                    command, 
                    workdir=workdir,
                    capture_stderr=True,
                    environment=env_vars
                )
            
            # Handle timeout terminations
            if result.get('termination_reason'):
                # Get the timeouts that were used
                if is_long_running_command:
                    silent_timeout, absolute_timeout = self._get_command_timeout(command)
                    error_messages = {
                        'absolute_timeout': f"Command exceeded maximum execution time ({absolute_timeout/60:.0f} minutes)",
                        'silent_timeout': f"Command was silent for too long ({silent_timeout/60:.0f} minutes)",
                        'monitoring_error': f"Error occurred during command monitoring"
                    }
                else:
                    error_messages = {
                        'absolute_timeout': f"Command exceeded maximum execution time",
                        'silent_timeout': f"Command was silent for too long",
                        'monitoring_error': f"Error occurred during command monitoring"
                    }
                
                termination_reason = result['termination_reason']
                error_msg = error_messages.get(termination_reason, f"Command terminated: {termination_reason}")
                
                # Include monitoring info in suggestions
                monitoring_info = result.get('monitoring_info', {})
                suggestions = [
                    f"Command was terminated due to: {termination_reason}",
                    f"Execution time: {monitoring_info.get('execution_time', 0):.1f} seconds",
                    "Consider breaking down the command into smaller steps",
                    "Check if the command requires user interaction (not supported in containers)"
                ]
                
                if monitoring_info.get('cpu_warnings', 0) > 0:
                    suggestions.append(f"CPU warnings detected: {monitoring_info['cpu_warnings']} (possible hang)")
                
                return ToolResult(
                    success=False,
                    output=result.get('output', ''),
                    error=error_msg,
                    suggestions=suggestions,
                    error_code=f"TIMEOUT_{termination_reason.upper()}",
                    metadata={
                        'termination_reason': termination_reason,
                        'monitoring_info': monitoring_info
                    }
                )
            
            # Normal command completion
            if result["success"]:
                # Extract key information for different command types
                extracted_output = self._extract_key_info(result["output"], command)
                
                # Analyze output for completion signals
                completion_signals = self._detect_completion_signals(result["output"], command)
                
                # Separate stdout and stderr if available
                stdout = result.get('stdout', result["output"])
                stderr = result.get('stderr', '')
                
                return ToolResult(
                    success=True,
                    output=extracted_output,
                    raw_output=result["output"],
                    metadata={
                        'stdout': stdout,
                        'stderr': stderr,
                        'exit_code': result.get('exit_code', 0),
                        'signal': result.get('signal'),
                        'execution_directory': workdir,
                        'environment_vars': env_vars,
                        'monitoring_info': result.get('monitoring_info'),
                        'is_long_running': is_long_running_command,
                        'completion_signals': completion_signals,
                        'command_type': self._get_command_type(command),
                        'extracted_values': self._extract_values(result["output"], command),
                        'background_pids': []
                    }
                )
            else:
                # Analyze error for specific failure patterns
                error_analysis = self._analyze_error_output(result["output"], command)
                
                # Separate stdout and stderr if available
                stdout = result.get('stdout', result["output"])
                stderr = result.get('stderr', '')
                
                return ToolResult(
                    success=False,
                    output=result["output"],
                    error=f"Command failed with exit code {result.get('exit_code', 'unknown')}",
                    suggestions=self._generate_error_suggestions(error_analysis, command, result.get('exit_code')),
                    error_code=error_analysis.get('error_code', 'COMMAND_FAILED'),
                    metadata={
                        'stdout': stdout,
                        'stderr': stderr,
                        'exit_code': result.get('exit_code'),
                        'signal': result.get('signal'),
                        'execution_directory': workdir,
                        'environment_vars': env_vars,
                        'monitoring_info': result.get('monitoring_info'),
                        'error_analysis': error_analysis,
                        'recovery_commands': self._get_recovery_commands(error_analysis),
                        'background_pids': []
                    }
                )
        
        except Exception as e:
            raise ToolError(
                message=f"Failed to execute bash command: {str(e)}",
                suggestions=[
                    "Check if Docker container is running",
                    "Verify network connectivity",
                    "Ensure sufficient disk space in container",
                    "Check container logs for system errors"
                ],
                error_code="EXECUTION_ERROR"
            )
    
    def _execute_background_command(self, command: str, workdir: str, env_vars: Dict[str, str]) -> ToolResult:
        """Execute a command in the background and return immediately with PID."""
        try:
            # Build command that starts process in background and returns PID
            # Use nohup to prevent SIGHUP when shell exits
            pid_command = f"nohup {command} > /tmp/sag_bg_$$.out 2>&1 & echo $!"
            
            # Add environment variables to the command
            if env_vars:
                env_prefix = ' '.join(f'{k}={shlex.quote(v)}' for k, v in env_vars.items())
                pid_command = f"{env_prefix} {pid_command}"
            
            # Execute command to get PID
            result = self.docker_orchestrator.execute_command(pid_command, workdir=workdir)
            
            if result["success"] and result["output"].strip().isdigit():
                pid = int(result["output"].strip())
                
                # Track the background process
                container_name = self.docker_orchestrator.container_name
                if container_name not in self.background_processes:
                    self.background_processes[container_name] = []
                self.background_processes[container_name].append(pid)
                
                logger.info(f"🚀 Started background process with PID {pid}")
                
                return ToolResult(
                    success=True,
                    output=f"Background process started with PID: {pid}",
                    metadata={
                        'background_pids': [pid],
                        'execution_directory': workdir,
                        'environment_vars': env_vars,
                        'command_type': 'background',
                        'output_file': f'/tmp/sag_bg_{pid}.out',
                        'stdout': '',
                        'stderr': '',
                        'exit_code': None,
                        'signal': None
                    }
                )
            else:
                return ToolResult(
                    success=False,
                    output=result.get('output', ''),
                    error="Failed to start background process",
                    suggestions=[
                        "Check command syntax",
                        "Verify the command can run in the container",
                        "Check container logs for errors"
                    ],
                    error_code="BACKGROUND_START_FAILED",
                    metadata={'background_pids': []}
                )
                
        except Exception as e:
            logger.error(f"Failed to execute background command: {e}")
            raise ToolError(
                message=f"Failed to execute background command: {str(e)}",
                suggestions=[
                    "Check if background processes are enabled",
                    "Verify container supports nohup",
                    "Check command syntax"
                ],
                error_code="BACKGROUND_EXECUTION_ERROR"
            )
    
    def get_background_processes(self, container_name: Optional[str] = None) -> Dict[str, List[int]]:
        """Get list of background processes for a container or all containers."""
        if container_name:
            return {container_name: self.background_processes.get(container_name, [])}
        return self.background_processes.copy()
    
    def check_background_process(self, pid: int, container_name: Optional[str] = None) -> Dict[str, Any]:
        """Check status of a background process."""
        if not container_name and self.docker_orchestrator:
            container_name = self.docker_orchestrator.container_name
        
        if not container_name:
            return {'exists': False, 'error': 'No container specified'}
        
        try:
            # Check if process is still running
            check_cmd = f"ps -p {pid} > /dev/null 2>&1 && echo 'RUNNING' || echo 'STOPPED'"
            result = self.docker_orchestrator.execute_command(check_cmd, workdir=None)
            
            is_running = result["success"] and "RUNNING" in result["output"]
            
            # Try to get output file if it exists
            output = ""
            output_file = f"/tmp/sag_bg_{pid}.out"
            output_cmd = f"test -f {output_file} && tail -n 50 {output_file} || echo 'No output file'"
            output_result = self.docker_orchestrator.execute_command(output_cmd, workdir=None)
            if output_result["success"]:
                output = output_result["output"]
            
            return {
                'pid': pid,
                'running': is_running,
                'output': output,
                'container': container_name
            }
            
        except Exception as e:
            return {'exists': False, 'error': str(e)}

    def _is_long_running_command(self, command: str) -> bool:
        """Detect if a command is likely to be long-running and needs enhanced monitoring."""
        
        long_running_patterns = [
            'mvn clean install',
            'mvn clean compile test',
            'mvn package',
            'mvn install',
            'gradle build',
            'gradle assemble',
            'npm install',
            'npm run build',
            'docker build',
            'apt-get update',
            'apt-get install',
            'wget',
            'curl.*-o',  # downloads
            'git clone',
            'make',
            'cmake --build'
        ]
        
        command_lower = command.lower()
        
        # Check for explicit patterns
        for pattern in long_running_patterns:
            if pattern in command_lower:
                return True
        
        # Check for commands that typically take time
        time_consuming_commands = ['compile', 'build', 'test', 'install', 'download', 'clone']
        for keyword in time_consuming_commands:
            if keyword in command_lower:
                return True
        
        return False
    
    def _detect_completion_signals(self, output: str, command: str) -> dict:
        """Detect completion signals in command output."""
        signals = {
            'build_success': False,
            'tests_passed': False,
            'installation_complete': False,
            'file_created': False,
            'service_started': False,
            'package_installed': False
        }
        
        output_lower = output.lower()
        
        # Build success indicators
        if any(pattern in output_lower for pattern in ['build successful', 'build success', 'successfully built']):
            signals['build_success'] = True
        
        # Test success indicators
        if 'tests passed' in output_lower or 'all tests passed' in output_lower:
            signals['tests_passed'] = True
        elif 'tests run:' in output and 'failures: 0' in output and 'errors: 0' in output:
            signals['tests_passed'] = True
        
        # Installation indicators
        if 'successfully installed' in output_lower or 'installation complete' in output_lower:
            signals['installation_complete'] = True
        
        # Package manager indicators
        if 'packages can be updated' in output_lower or 'newly installed' in output_lower:
            signals['package_installed'] = True
        
        # Service indicators
        if 'active (running)' in output or 'started successfully' in output_lower:
            signals['service_started'] = True
        
        # File creation indicators
        if re.search(r'created|written|saved|generated', output_lower):
            signals['file_created'] = True
        
        return signals
    
    def _get_command_type(self, command: str) -> str:
        """Categorize the command type for better context."""
        command_lower = command.lower()
        
        if any(tool in command_lower for tool in ['mvn', 'gradle', 'ant']):
            return 'build_tool'
        elif any(pkg in command_lower for pkg in ['apt', 'yum', 'dnf', 'pip', 'npm', 'yarn']):
            return 'package_manager'
        elif any(git in command_lower for git in ['git ', 'git-']):
            return 'version_control'
        elif any(test in command_lower for test in ['test', 'pytest', 'jest', 'mocha']):
            return 'testing'
        elif any(file_op in command_lower for file_op in ['ls', 'find', 'grep', 'cat', 'head', 'tail']):
            return 'file_operation'
        elif any(sys in command_lower for sys in ['ps', 'top', 'df', 'du', 'free']):
            return 'system_info'
        elif 'cd ' in command_lower or 'pwd' in command_lower:
            return 'navigation'
        elif any(net in command_lower for net in ['curl', 'wget', 'ping', 'netstat']):
            return 'network'
        else:
            return 'general'
    
    def _extract_values(self, output: str, command: str) -> dict:
        """Extract specific values from output based on command type."""
        values = {}
        
        # Extract file paths
        file_paths = re.findall(r'/[\w/.-]+', output)
        if file_paths:
            values['file_paths'] = list(set(file_paths))[:10]  # Limit to 10 unique paths
        
        # Extract URLs
        urls = re.findall(r'https?://[^\s]+', output)
        if urls:
            values['urls'] = list(set(urls))[:5]
        
        # Extract version numbers
        versions = re.findall(r'\b\d+\.\d+(?:\.\d+)?\b', output)
        if versions:
            values['versions'] = list(set(versions))[:5]
        
        # Extract IP addresses
        ips = re.findall(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', output)
        if ips:
            values['ip_addresses'] = list(set(ips))[:5]
        
        # Extract numbers (counts, sizes, etc.)
        if 'ls' in command or 'find' in command or 'grep -c' in command:
            lines = output.strip().split('\n')
            if lines and lines[0]:
                values['count'] = len(lines)
        
        # Extract process IDs
        pids = re.findall(r'\bPID[:\s]+(\d+)', output)
        if pids:
            values['pids'] = pids[:5]
        
        return values
    
    def _analyze_error_output(self, output: str, command: str) -> dict:
        """Analyze error output to determine specific failure type."""
        analysis = {
            'error_type': 'general',
            'error_code': 'COMMAND_FAILED',
            'key_errors': [],
            'compilation_errors': [],
            'test_failures': [],
            'error_count': 0,
            'test_stats': {}
        }
        
        output_lower = output.lower()
        lines = output.split('\n')
        
        # Java Compilation errors (HIGH PRIORITY - check first)
        if 'unmappable character for encoding' in output_lower or 'compilation failed' in output_lower:
            analysis['error_type'] = 'compilation_error'
            analysis['error_code'] = 'COMPILATION_FAILED'
            # Count compilation errors
            for line in lines:
                if 'unmappable character' in line.lower() or 'error:' in line:
                    analysis['compilation_errors'].append(line.strip())
                    if 'error:' in line:
                        analysis['error_count'] += 1
            # Limit stored errors to prevent bloat
            analysis['compilation_errors'] = analysis['compilation_errors'][:20]
        
        # Build failures (Maven/Gradle)
        elif 'build failed' in output_lower or 'build failure' in output_lower:
            analysis['error_type'] = 'build_failed'
            analysis['error_code'] = 'BUILD_FAILED'
            # Look for specific build error patterns
            for line in lines:
                if 'BUILD FAILED' in line or 'BUILD FAILURE' in line:
                    analysis['key_errors'].append(line.strip())
                elif 'compilation error' in line.lower():
                    analysis['compilation_errors'].append(line.strip())
                    analysis['error_count'] += 1
        
        # Test failures
        elif 'tests failed' in output_lower or 'test failures' in output_lower or 'failures: ' in output_lower:
            analysis['error_type'] = 'test_failed'
            analysis['error_code'] = 'TEST_FAILED'
            # Extract test statistics
            for line in lines:
                # JUnit pattern: "Tests run: X, Failures: Y, Errors: Z, Skipped: W"
                if 'tests run:' in line.lower():
                    import re
                    stats_match = re.search(r'tests?\s+run:\s*(\d+).*?failures?:\s*(\d+).*?errors?:\s*(\d+)', line, re.IGNORECASE)
                    if stats_match:
                        analysis['test_stats'] = {
                            'total': int(stats_match.group(1)),
                            'failures': int(stats_match.group(2)),
                            'errors': int(stats_match.group(3))
                        }
                # Gradle pattern: "X tests completed, Y failed"
                elif 'tests completed' in line.lower():
                    stats_match = re.search(r'(\d+)\s+tests?\s+completed.*?(\d+)\s+failed', line, re.IGNORECASE)
                    if stats_match:
                        analysis['test_stats'] = {
                            'total': int(stats_match.group(1)),
                            'failures': int(stats_match.group(2)),
                            'errors': 0
                        }
                # Capture failed test names
                if 'FAILED' in line and ('Test' in line or 'test' in line):
                    analysis['test_failures'].append(line.strip())
            # Limit test failures stored
            analysis['test_failures'] = analysis['test_failures'][:15]
        
        # Permission errors
        elif 'permission denied' in output_lower or 'access denied' in output_lower:
            analysis['error_type'] = 'permission'
            analysis['error_code'] = 'PERMISSION_DENIED'
        
        # File/directory not found
        elif 'no such file or directory' in output_lower or 'cannot find' in output_lower:
            analysis['error_type'] = 'not_found'
            analysis['error_code'] = 'FILE_NOT_FOUND'
        
        # Command not found
        elif 'command not found' in output_lower or 'not found' in output_lower:
            analysis['error_type'] = 'command_not_found'
            analysis['error_code'] = 'COMMAND_NOT_FOUND'
        
        # Network errors
        elif any(net_err in output_lower for net_err in ['connection refused', 'connection timeout', 'unreachable']):
            analysis['error_type'] = 'network'
            analysis['error_code'] = 'NETWORK_ERROR'
        
        # Disk space errors
        elif 'no space left' in output_lower or 'disk full' in output_lower:
            analysis['error_type'] = 'disk_space'
            analysis['error_code'] = 'DISK_FULL'
        
        # Package manager errors
        elif 'unable to locate package' in output_lower or 'no matching packages' in output_lower:
            analysis['error_type'] = 'package_not_found'
            analysis['error_code'] = 'PACKAGE_NOT_FOUND'
        
        # Extract general error lines if not already captured
        if not analysis['key_errors'] and not analysis['compilation_errors']:
            error_lines = [line for line in lines if 'error' in line.lower() or 'fail' in line.lower()]
            analysis['key_errors'] = error_lines[:10]  # Increased limit for better diagnostics
        
        return analysis
    
    def _generate_error_suggestions(self, error_analysis: dict, command: str, exit_code: int) -> list:
        """Generate specific suggestions based on error analysis."""
        suggestions = []
        error_type = error_analysis.get('error_type', 'general')
        
        if error_type == 'compilation_error':
            compilation_errors = error_analysis.get('compilation_errors', [])
            error_count = error_analysis.get('error_count', 0)
            suggestions.extend([
                f"Compilation failed with {error_count} errors",
                "Check source code for syntax errors",
                "Verify character encoding (use UTF-8 for source files)",
                "Check Java/compiler version compatibility",
                "Review the first few errors as they often cascade"
            ])
            # Add specific encoding fix if detected
            if any('unmappable character' in err.lower() for err in compilation_errors):
                suggestions.insert(0, "Fix encoding: Add -Dfile.encoding=UTF-8 to MAVEN_OPTS or gradle.properties")
        
        elif error_type == 'build_failed':
            suggestions.extend([
                "Build failed - check compilation errors above",
                "Verify all dependencies are available",
                "Check if build tool (Maven/Gradle) is properly configured",
                "Try running with -X (Maven) or --debug (Gradle) for detailed output",
                "Clean and rebuild: 'mvn clean' or 'gradle clean'"
            ])
        
        elif error_type == 'test_failed':
            test_stats = error_analysis.get('test_stats', {})
            if test_stats:
                total = test_stats.get('total', 0)
                failures = test_stats.get('failures', 0)
                errors = test_stats.get('errors', 0)
                suggestions.append(f"Tests failed: {failures} failures, {errors} errors out of {total} tests")
            suggestions.extend([
                "Review test failure details in the output above",
                "Check test logs for detailed error messages",
                "Run tests individually to isolate failures",
                "Verify test environment and dependencies",
                "Use -DskipTests to skip tests temporarily (not recommended for production)"
            ])
        
        elif error_type == 'permission':
            suggestions.extend([
                "Try running with sudo if appropriate",
                "Check file/directory permissions with 'ls -la'",
                "Ensure the user has necessary permissions",
                "Verify ownership with 'ls -l' command"
            ])
        elif error_type == 'not_found':
            suggestions.extend([
                "Verify the file/directory path is correct",
                "Use 'ls' or 'find' to locate the resource",
                "Check if you're in the correct working directory",
                "Use absolute paths instead of relative paths"
            ])
        elif error_type == 'command_not_found':
            cmd_name = command.split()[0] if command else 'command'
            suggestions.extend([
                f"Install {cmd_name} using the package manager",
                f"Check if {cmd_name} is in PATH with 'which {cmd_name}'",
                "Verify the command name is spelled correctly",
                "Use the full path to the executable"
            ])
        elif error_type == 'network':
            suggestions.extend([
                "Check network connectivity",
                "Verify the target host is reachable",
                "Check firewall/proxy settings",
                "Try again after a brief delay"
            ])
        elif error_type == 'disk_space':
            suggestions.extend([
                "Check disk space with 'df -h'",
                "Clean up unnecessary files",
                "Remove old logs or temporary files",
                "Increase container disk allocation if needed"
            ])
        elif error_type == 'package_not_found':
            suggestions.extend([
                "Update package lists with 'apt-get update' or equivalent",
                "Check the package name spelling",
                "Search for the package with 'apt-cache search' or equivalent",
                "Add required repositories if package is from external source"
            ])
        else:
            # Generic suggestions
            suggestions.extend([
                "Check the command syntax and parameters",
                "Verify that required files/directories exist",
                f"Command exited with code {exit_code}",
                "Review the error output for specific issues"
            ])
        
        return suggestions
    
    def _get_recovery_commands(self, error_analysis: dict) -> list:
        """Get recovery commands based on error type."""
        recovery_commands = []
        error_type = error_analysis.get('error_type', 'general')
        
        if error_type == 'permission':
            recovery_commands.extend([
                "ls -la",  # Check permissions
                "whoami",  # Check current user
                "id"       # Check user groups
            ])
        elif error_type == 'not_found':
            recovery_commands.extend([
                "pwd",     # Check current directory
                "ls -la",  # List files
                "find . -name '*' -type f | head -20"  # Search for files
            ])
        elif error_type == 'command_not_found':
            recovery_commands.extend([
                "echo $PATH",  # Check PATH
                "which <command>",  # Check if command exists
                "apt-get update && apt-cache search <command>"  # Search for package
            ])
        elif error_type == 'network':
            recovery_commands.extend([
                "ping -c 3 google.com",  # Check internet
                "cat /etc/resolv.conf",  # Check DNS
                "ip addr show"           # Check network interfaces
            ])
        elif error_type == 'disk_space':
            recovery_commands.extend([
                "df -h",     # Check disk space
                "du -sh /*", # Check directory sizes
                "find /tmp -type f -mtime +7 -delete"  # Clean old tmp files
            ])
        
        return recovery_commands
    
    def _get_command_timeout(self, command: str) -> tuple[int, int]:
        """
        Get appropriate timeouts based on command type.
        Returns (silent_timeout, absolute_timeout) in seconds.
        """
        command_lower = command.lower()
        
        # Maven/Gradle builds - need very long timeouts, especially for first runs
        if any(tool in command_lower for tool in ['mvn', 'gradle', './gradlew']):
            # Check for specific Maven/Gradle operations
            if any(op in command_lower for op in ['clean install', 'clean test', 'build']):
                # Full builds with tests can take very long, especially with dependency downloads
                return (1800, 7200)  # 30 min silent, 120 min total
            elif 'compile' in command_lower:
                # Compilation with dependency downloads
                return (1200, 3600)  # 20 min silent, 60 min total
            elif 'package' in command_lower:
                # Packaging can involve downloads
                return (1200, 3600)  # 20 min silent, 60 min total
            elif 'test' in command_lower:
                # Tests can be long running
                return (1200, 3600)  # 20 min silent, 60 min total
            else:
                # Default for Maven/Gradle (includes dependency resolution)
                return (900, 2400)  # 15 min silent, 40 min total
        
        # NPM/Node operations
        elif 'npm' in command_lower or 'yarn' in command_lower:
            if 'install' in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            elif 'build' in command_lower or 'test' in command_lower:
                return (300, 1200)  # 5 min silent, 20 min total
            else:
                return (180, 600)  # 3 min silent, 10 min total
        
        # Python operations
        elif 'pip' in command_lower or 'pytest' in command_lower:
            if 'install' in command_lower:
                return (180, 600)  # 3 min silent, 10 min total
            elif 'test' in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            else:
                return (120, 300)  # 2 min silent, 5 min total
        
        # Git operations
        elif 'git' in command_lower:
            if 'clone' in command_lower:
                # Large repos can take time
                return (300, 1200)  # 5 min silent, 20 min total
            else:
                return (60, 180)  # 1 min silent, 3 min total
        
        # Docker operations
        elif 'docker' in command_lower:
            if 'build' in command_lower:
                return (600, 1800)  # 10 min silent, 30 min total
            elif 'pull' in command_lower:
                return (300, 900)  # 5 min silent, 15 min total
            else:
                return (60, 300)  # 1 min silent, 5 min total
        
        # Package installation
        elif any(pkg in command_lower for pkg in ['apt-get install', 'apt install', 'yum install']):
            return (300, 900)  # 5 min silent, 15 min total
        
        # Make/CMake operations
        elif 'make' in command_lower or 'cmake' in command_lower:
            return (300, 1200)  # 5 min silent, 20 min total
        
        # Default for unknown long-running commands
        elif self._is_long_running_command(command):
            return (300, 900)  # 5 min silent, 15 min total
        
        # Default for regular commands
        else:
            return (60, 300)  # 1 min silent, 5 min total

    def _validate_and_fix_working_directory(self, requested_workdir: str) -> str:
        """
        Validate working directory exists and fix if needed.
        
        PRIORITY LOGIC:
        1. FIRST: Try to ensure /workspace works (this is the standard)
        2. SECOND: Try to repair /workspace if it's missing
        3. LAST RESORT: Fall back to alternative directories only if /workspace is completely broken
        
        This ensures clone operations happen in the correct workspace location.
        """
        logger.debug(f"🔍 Validating working directory: {requested_workdir}")
        
        # PRIORITY 1: If requesting /workspace (or subdirs), try to ensure it works
        if requested_workdir.startswith("/workspace"):
            logger.info(f"🎯 PRIORITY: Ensuring /workspace is available for proper project setup")
            
            # First check if /workspace exists
            workspace_check = self.docker_orchestrator.execute_command(
                "test -d /workspace && echo 'EXISTS' || echo 'MISSING'", 
                workdir=None
            )
            
            if workspace_check["success"] and "EXISTS" in workspace_check["output"]:
                logger.info(f"✅ /workspace exists and is accessible")
                # Verify the specific subdirectory if needed
                if requested_workdir != "/workspace":
                    subdir_check = self.docker_orchestrator.execute_command(
                        f"test -d {requested_workdir} && echo 'EXISTS' || echo 'MISSING'",
                        workdir=None
                    )
                    if subdir_check["success"] and "EXISTS" in subdir_check["output"]:
                        logger.debug(f"✅ Subdirectory {requested_workdir} exists")
                        return requested_workdir
                    else:
                        # Try to create the subdirectory
                        logger.info(f"🔧 Creating subdirectory: {requested_workdir}")
                        create_subdir = self.docker_orchestrator.execute_command(
                            f"mkdir -p {requested_workdir} && echo 'CREATED'",
                            workdir="/workspace"  # Use /workspace as base since it exists
                        )
                        if create_subdir["success"] and "CREATED" in create_subdir["output"]:
                            logger.info(f"✅ Created subdirectory: {requested_workdir}")
                            return requested_workdir
                        else:
                            logger.warning(f"⚠️ Could not create {requested_workdir}, using /workspace")
                            return "/workspace"
                else:
                    return "/workspace"
            
            # PRIORITY 2: /workspace is missing, try to repair it
            logger.warning(f"⚠️ /workspace is missing - attempting repair")
            repair_steps = [
                ("mkdir -p /workspace", "Create /workspace directory"),
                ("chmod 755 /workspace", "Set /workspace permissions"),
                ("touch /workspace/.sag_workspace_marker", "Create workspace marker"),
                ("chown root:root /workspace", "Set workspace ownership")
            ]
            
            workspace_repaired = True
            for repair_cmd, description in repair_steps:
                logger.info(f"🔧 REPAIR: {description}")
                repair_result = self.docker_orchestrator.execute_command(repair_cmd, workdir=None)
                
                if not repair_result["success"]:
                    logger.error(f"❌ REPAIR FAILED: {description}")
                    workspace_repaired = False
                    break
                else:
                    logger.info(f"✅ REPAIR SUCCESS: {description}")
            
            if workspace_repaired:
                # Verify repair worked
                verify_result = self.docker_orchestrator.execute_command(
                    "test -d /workspace && test -w /workspace && echo 'REPAIRED' || echo 'FAILED'",
                    workdir=None
                )
                if verify_result["success"] and "REPAIRED" in verify_result["output"]:
                    logger.info(f"✅ WORKSPACE REPAIRED: /workspace is now available")
                    
                    # Now try to create the requested subdirectory if needed
                    if requested_workdir != "/workspace":
                        create_result = self.docker_orchestrator.execute_command(
                            f"mkdir -p {requested_workdir} && echo 'CREATED'",
                            workdir="/workspace"
                        )
                        if create_result["success"]:
                            logger.info(f"✅ Created requested directory after repair: {requested_workdir}")
                            return requested_workdir
                        else:
                            logger.warning(f"⚠️ Could not create {requested_workdir} after repair, using /workspace")
                            return "/workspace"
                    else:
                        return "/workspace"
                else:
                    logger.error(f"❌ WORKSPACE REPAIR VERIFICATION FAILED")
                    workspace_repaired = False
            
            # PRIORITY 3: LAST RESORT - /workspace cannot be repaired
            if not workspace_repaired:
                logger.error(f"❌ CRITICAL: Cannot establish /workspace - falling back to alternative directories")
                logger.error(f"❌ This may cause issues with project cloning and file operations")
        
        # For non-workspace directories or as last resort fallback
        logger.info(f"🔍 Checking alternative directory: {requested_workdir}")
        check_result = self.docker_orchestrator.execute_command(
            f"test -d {requested_workdir} && echo 'EXISTS' || echo 'MISSING'", 
            workdir=None
        )
        
        if check_result["success"] and "EXISTS" in check_result["output"]:
            logger.debug(f"✅ Alternative directory {requested_workdir} exists")
            return requested_workdir
        
        # Try to create the alternative directory
        logger.info(f"🔧 Attempting to create alternative directory: {requested_workdir}")
        create_result = self.docker_orchestrator.execute_command(
            f"mkdir -p {requested_workdir} && echo 'CREATED' || echo 'FAILED'",
            workdir=None
        )
        
        if create_result["success"] and "CREATED" in create_result["output"]:
            logger.warning(f"⚠️ FALLBACK: Using alternative directory: {requested_workdir}")
            return requested_workdir
        
        # Ultimate fallback to known good directories
        fallback_dirs = ["/root", "/tmp", "/"]
        
        for fallback_dir in fallback_dirs:
            logger.error(f"🆘 ULTIMATE FALLBACK: Trying {fallback_dir}")
            fallback_check = self.docker_orchestrator.execute_command(
                f"test -d {fallback_dir} && echo 'EXISTS' || echo 'MISSING'",
                workdir=None
            )
            
            if fallback_check["success"] and "EXISTS" in fallback_check["output"]:
                logger.error(f"🆘 USING EMERGENCY FALLBACK: {fallback_dir} (MAJOR ISSUE - workspace unavailable)")
                return fallback_dir
        
        # Last resort: no workdir (let Docker decide)
        logger.error(f"❌ COMPLETE FAILURE: No working directory available - using container default")
        return None

    def _enhance_grep_command(self, command: str) -> str:
        """Enhance grep commands with helpful default flags."""
        if not command.strip().startswith('grep'):
            return command
        
        # Parse the command to avoid double-adding flags
        parts = shlex.split(command)
        if len(parts) < 2:
            return command
        
        # Check if useful flags are already present
        has_recursive = '-r' in parts or '--recursive' in parts
        has_line_numbers = '-n' in parts or '--line-number' in parts
        has_color = '--color' in parts
        
        enhanced_parts = [parts[0]]  # Start with 'grep'
        
        # Add helpful flags if not present
        if not has_line_numbers:
            enhanced_parts.append('-n')
        if not has_color:
            enhanced_parts.append('--color=always')
        
        # Add the rest of the command
        enhanced_parts.extend(parts[1:])
        
        enhanced_command = ' '.join(shlex.quote(part) if ' ' in part else part for part in enhanced_parts)
        
        # If the command seems to be searching in current directory without -r, suggest it
        if not has_recursive and (enhanced_command.endswith(' .') or enhanced_command.endswith(' ./')):
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
                    "'grep -rni \"error\\|exception\" . --include=\"*.py\"' for specific file types, "
                    "'grep -rn -C 3 \"function_name\" .' for context around matches. "
                    "See get_grep_examples() for comprehensive patterns."
                },
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

    def get_usage_example(self) -> str:
        """Get usage examples focused on grep investigations."""
        return f"""
{self.name}(command="grep -rn 'def process_data' . --include='*.py'")  # Find function definitions
{self.name}(command="grep -rni 'error|exception' . --include='*.py' -C 2")  # Find error handling with context
{self.name}(command="grep -rn 'import pandas' .")  # Find specific imports
{self.name}(command="ls -la", working_directory="/workspace")  # Standard file operations
{self.name}(command="git status", working_directory="/workspace/project")  # Git operations

💡 For comprehensive grep patterns, use: get_grep_examples()
        """
