"""Gradle tool with comprehensive error handling and Gradle-specific features."""

import json
import re
from typing import Dict, Any, Optional, List

from loguru import logger

from .base import BaseTool, ToolResult, ToolError


class GradleTool(BaseTool):
    """Gradle build tool with enhanced error handling and Gradle-specific features."""
    
    def __init__(self, orchestrator):
        super().__init__(
            name="gradle",
            description="Execute Gradle commands with comprehensive error analysis and raw output access. "
                       "Supports all Gradle tasks, multi-project builds, dependency management, and build analysis. "
                       "Automatically uses gradlew wrapper if present, installs Gradle if needed."
        )
        self.orchestrator = orchestrator
    
    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use Gradle-specific extraction."""
        if tool_name == "gradle" or tool_name == self.name:
            return self._extract_gradle_key_info(output)
        return output
    
    def execute(
        self,
        tasks: str = None,
        command: str = None,  # Alias for tasks for compatibility
        properties: str = None,
        gradle_args: str = None,
        build_file: str = None,
        raw_output: bool = False,
        working_directory: str = "/workspace",
        timeout: int = 300,
        use_wrapper: bool = True,  # Gradle-specific: prefer wrapper
        parallel: bool = False,  # Gradle-specific: parallel execution
        configure_on_demand: bool = False,  # Gradle-specific optimization
        build_cache: bool = True,  # Gradle-specific: use build cache
        **kwargs  # Accept any additional parameters
    ) -> ToolResult:
        """
        Execute Gradle commands with comprehensive error handling.
        
        Args:
            tasks: Gradle tasks to run (e.g., 'clean build', 'test', 'assemble')
            command: Alias for tasks (for compatibility)
            properties: Gradle properties (e.g., '-Pversion=1.0', '-PskipTests')
            gradle_args: Additional Gradle arguments (e.g., '--info', '--stacktrace', '--scan')
            build_file: Specific build file to use (e.g., 'custom.gradle', 'build.gradle.kts')
            raw_output: Whether to return raw Gradle output for detailed analysis
            working_directory: Directory to execute Gradle in
            timeout: Command timeout in seconds
            use_wrapper: Whether to prefer gradlew wrapper over system gradle
            parallel: Enable parallel execution
            configure_on_demand: Only configure relevant projects
            build_cache: Use Gradle build cache for faster builds
        """
        
        # Handle command as alias for tasks
        if command and not tasks:
            tasks = command
        
        # Deterministic working directory fallback (only when safe)
        try:
            if working_directory in (None, "/workspace") and self.orchestrator:
                project_name = getattr(self.orchestrator, 'project_name', None)
                if project_name:
                    probe_dir = f"/workspace/{project_name}"
                    # Recognize Gradle project by standard files
                    probe_cmd = (
                        f"test -f {probe_dir}/build.gradle -o -f {probe_dir}/build.gradle.kts -o -f {probe_dir}/settings.gradle -o -f {probe_dir}/settings.gradle.kts && echo EXISTS || echo MISSING"
                    )
                    probe_res = self.orchestrator.execute_command(probe_cmd)
                    if probe_res.get('exit_code') == 0 and 'EXISTS' in (probe_res.get('output') or ''):
                        if working_directory != probe_dir:
                            logger.info(f"🔧 Auto-selected project directory for Gradle: {probe_dir}")
                            working_directory = probe_dir
        except Exception as _e:
            logger.debug(f"Gradle working directory fallback skipped: {_e}")

        # Check for Gradle wrapper or system Gradle
        gradle_executable = self._determine_gradle_executable(working_directory, use_wrapper)
        if not gradle_executable:
            install_result = self._install_gradle(working_directory)
            if not install_result.success:
                return install_result
            gradle_executable = self._determine_gradle_executable(working_directory, use_wrapper)
        
        # Validate that build.gradle or build.gradle.kts exists
        build_validation = self._validate_build_file_exists(working_directory, build_file)
        if not build_validation["exists"]:
            return self._handle_missing_build_file(build_validation, working_directory)
        
        # Build Gradle command
        gradle_cmd = self._build_gradle_command(
            gradle_executable, 
            tasks, 
            properties, 
            gradle_args,
            build_file,
            parallel,
            configure_on_demand,
            build_cache
        )
        
        # Execute the command
        try:
            # Use extended timeout for Gradle commands which often download dependencies
            is_long_running = any(task in gradle_cmd for task in [
                'build', 'test', 'assemble', 'compileJava', 'compileKotlin', 
                'publishToMavenLocal', 'publish', 'check', 'integrationTest'
            ])
            
            if is_long_running and hasattr(self.orchestrator, 'execute_command_with_monitoring'):
                # Use monitoring version with extended timeouts for build commands
                logger.info(f"Executing Gradle command with extended timeout: {gradle_cmd}")
                result = self.orchestrator.execute_command_with_monitoring(
                    gradle_cmd,
                    workdir=working_directory,
                    silent_timeout=1200,  # 20 minutes for no output (dependency downloads)
                    absolute_timeout=3600,  # 60 minutes total
                    optimize_for_gradle=True
                )
            else:
                # Use regular version for quick commands like help, tasks, etc.
                result = self.orchestrator.execute_command(
                    gradle_cmd,
                    workdir=working_directory,
                    timeout=timeout
                )
            
            # Analyze the output
            analysis = self._analyze_gradle_output(result["output"], result["exit_code"])
            
            if raw_output:
                return ToolResult(
                    success=result["exit_code"] == 0,
                    output=result["output"],
                    raw_output=result["output"],
                    metadata={
                        "command": gradle_cmd,
                        "exit_code": result["exit_code"],
                        "analysis": analysis
                    }
                )
            
            if result["exit_code"] == 0:
                return ToolResult(
                    success=True,
                    output=self._format_success_output(analysis),
                    raw_output=result["output"],
                    metadata={
                        "command": gradle_cmd,
                        "exit_code": result["exit_code"],
                        "analysis": analysis
                    }
                )
            else:
                return self._handle_gradle_error(result["output"], result["exit_code"], gradle_cmd, analysis)
                
        except Exception as e:
            raise ToolError(
                message=f"Failed to execute Gradle command: {str(e)}",
                suggestions=[
                    "Check if Gradle wrapper (gradlew) exists in the project",
                    "Verify the working directory contains a build.gradle or build.gradle.kts file",
                    "Check Docker container connectivity",
                    "Try running with --stacktrace for more details"
                ],
                documentation_links=[
                    "https://docs.gradle.org/current/userguide/gradle_wrapper.html",
                    "https://docs.gradle.org/current/userguide/command_line_interface.html"
                ],
                error_code="GRADLE_EXECUTION_ERROR"
            )
    
    def _determine_gradle_executable(self, working_directory: str, use_wrapper: bool) -> Optional[str]:
        """Determine which Gradle executable to use."""
        if use_wrapper:
            # Check for gradlew wrapper
            wrapper_check = self.orchestrator.execute_command(
                f"test -f {working_directory}/gradlew && echo 'exists'",
                workdir=working_directory
            )
            if wrapper_check.get("exit_code") == 0 and "exists" in wrapper_check.get("output", ""):
                logger.info("Found Gradle wrapper (gradlew)")
                # Make sure it's executable
                self.orchestrator.execute_command(
                    f"chmod +x {working_directory}/gradlew",
                    workdir=working_directory
                )
                return "./gradlew"
        
        # Check for system Gradle
        gradle_check = self.orchestrator.execute_command("which gradle")
        if gradle_check.get("exit_code") == 0:
            logger.info("Found system Gradle")
            return "gradle"
        
        return None
    
    def _install_gradle(self, working_directory: str) -> ToolResult:
        """Install Gradle or create wrapper."""
        logger.info("Installing Gradle...")
        
        # First try to create a Gradle wrapper if we have a build file
        build_file_exists = self._validate_build_file_exists(working_directory, None)
        if build_file_exists["exists"]:
            # Try to generate wrapper using system gradle
            wrapper_cmd = (
                "apt-get update && apt-get install -y gradle && "
                "gradle wrapper --gradle-version=7.6"
            )
            result = self.orchestrator.execute_command(
                wrapper_cmd,
                workdir=working_directory,
                timeout=300
            )
            if result.get("exit_code") == 0:
                return ToolResult(
                    success=True,
                    output="✅ Gradle wrapper created successfully"
                )
        
        # Install system Gradle
        install_cmd = (
            "apt-get update && "
            "apt-get install -y gradle"
        )
        
        result = self.orchestrator.execute_command(install_cmd, timeout=300)
        
        if result.get("exit_code") == 0:
            return ToolResult(
                success=True,
                output="✅ Gradle installed successfully"
            )
        else:
            raise ToolError(
                message="Failed to install Gradle",
                suggestions=[
                    "Check if the container has internet access",
                    "Try installing Gradle manually",
                    "Ensure the Gradle wrapper is committed to the repository"
                ],
                error_code="GRADLE_INSTALLATION_FAILED"
            )
    
    def _validate_build_file_exists(self, working_directory: str, build_file: str = None) -> Dict[str, Any]:
        """Validate that a Gradle build file exists."""
        if build_file:
            # Check specific build file
            check_cmd = f"test -f {working_directory}/{build_file}"
        else:
            # Check for standard build files
            check_cmd = (
                f"test -f {working_directory}/build.gradle || "
                f"test -f {working_directory}/build.gradle.kts || "
                f"test -f {working_directory}/settings.gradle || "
                f"test -f {working_directory}/settings.gradle.kts"
            )
        
        result = self.orchestrator.execute_command(check_cmd, workdir=working_directory)
        
        if result.get("exit_code") == 0:
            return {"exists": True}
        
        # Try to find build files in subdirectories
        find_result = self.orchestrator.execute_command(
            "find . -maxdepth 3 -name 'build.gradle' -o -name 'build.gradle.kts' -o -name 'settings.gradle' -o -name 'settings.gradle.kts' | head -10",
            workdir=working_directory
        )
        
        found_files = []
        if find_result.get("exit_code") == 0 and find_result.get("output"):
            found_files = [f.strip() for f in find_result["output"].strip().split('\n') if f.strip()]
        
        return {
            "exists": False,
            "searched_in": working_directory,
            "found_files": found_files
        }
    
    def _handle_missing_build_file(self, validation: Dict, working_directory: str) -> ToolResult:
        """Handle missing build.gradle file."""
        suggestions = [
            f"Change to the correct directory containing build.gradle",
            "Ensure the project has been properly cloned or initialized"
        ]
        
        if validation.get("found_files"):
            suggestions.insert(0, f"Found build files in: {', '.join(validation['found_files'][:3])}")
            suggestions.insert(1, f"Try changing working_directory to the correct path")
        
        raise ToolError(
            message=f"No build.gradle or build.gradle.kts found in {working_directory}",
            suggestions=suggestions,
            documentation_links=[
                "https://docs.gradle.org/current/userguide/tutorial_using_tasks.html"
            ],
            error_code="BUILD_FILE_NOT_FOUND"
        )
    
    def _build_gradle_command(
        self, 
        executable: str,
        tasks: str,
        properties: str,
        gradle_args: str,
        build_file: str,
        parallel: bool,
        configure_on_demand: bool,
        build_cache: bool
    ) -> str:
        """Build the complete Gradle command."""
        cmd_parts = [executable]
        
        # Add build file if specified
        if build_file:
            cmd_parts.extend(["-b", build_file])
        
        # Add performance flags
        if parallel:
            cmd_parts.append("--parallel")
        if configure_on_demand:
            cmd_parts.append("--configure-on-demand")
        if build_cache:
            cmd_parts.append("--build-cache")
        
        # Add properties
        if properties:
            # Handle both space and comma-separated properties
            props = properties.replace(',', ' ').split()
            for prop in props:
                if not prop.startswith('-P') and not prop.startswith('-D'):
                    prop = f"-P{prop}"
                cmd_parts.append(prop)
        
        # Add gradle arguments
        if gradle_args:
            cmd_parts.append(gradle_args)
        
        # Add tasks (default to 'build' if none specified)
        if tasks:
            cmd_parts.extend(tasks.split())
        else:
            cmd_parts.append("build")
        
        return " ".join(cmd_parts)
    
    def _analyze_gradle_output(self, output: str, exit_code: int) -> Dict[str, Any]:
        """Analyze Gradle output for important information."""
        analysis = {
            "exit_code": exit_code,
            "build_successful": False,
            "test_results": None,
            "compilation_errors": [],
            "test_failures": [],
            "dependency_errors": [],
            "warnings": [],
            "deprecated_features": [],
            "build_time": None,
            "tasks_executed": [],
            "cache_hits": 0
        }
        
        lines = output.split('\n')
        
        for i, line in enumerate(lines):
            # Check for build success
            if "BUILD SUCCESSFUL" in line:
                analysis["build_successful"] = True
            elif "BUILD FAILED" in line:
                analysis["build_successful"] = False
            
            # Extract test results
            if "tests completed" in line.lower() or "test run:" in line.lower():
                test_match = re.search(r'(\d+)\s+tests?\s+completed', line, re.IGNORECASE)
                if test_match:
                    if not analysis["test_results"]:
                        analysis["test_results"] = {}
                    analysis["test_results"]["total"] = int(test_match.group(1))
                
                # Look for failures and errors
                fail_match = re.search(r'(\d+)\s+failed', line, re.IGNORECASE)
                if fail_match:
                    analysis["test_results"]["failed"] = int(fail_match.group(1))
            
            # Check for compilation errors
            if "compilation failed" in line.lower() or "compiler error" in line.lower():
                # Extract error details from surrounding lines
                error_context = lines[max(0, i-2):min(len(lines), i+3)]
                analysis["compilation_errors"].append('\n'.join(error_context))
            
            # Check for dependency resolution errors
            if "could not resolve" in line.lower() or "dependency" in line.lower() and "not found" in line.lower():
                analysis["dependency_errors"].append(line.strip())
            
            # Extract build time
            if "Total time:" in line:
                time_match = re.search(r'Total time:\s+(.+)', line)
                if time_match:
                    analysis["build_time"] = time_match.group(1)
            
            # Track executed tasks
            if "> Task :" in line:
                task_match = re.search(r'> Task :(\S+)', line)
                if task_match:
                    analysis["tasks_executed"].append(task_match.group(1))
            
            # Check for cache hits (Gradle-specific)
            if "FROM-CACHE" in line or "UP-TO-DATE" in line:
                analysis["cache_hits"] += 1
            
            # Deprecated features warning
            if "deprecated" in line.lower():
                analysis["deprecated_features"].append(line.strip())
        
        return analysis
    
    def _format_success_output(self, analysis: Dict[str, Any]) -> str:
        """Format successful Gradle execution output."""
        output = "✅ Gradle build completed successfully!\n\n"
        
        if analysis.get("tasks_executed"):
            output += f"📋 Tasks executed: {', '.join(analysis['tasks_executed'][:5])}\n"
            if len(analysis['tasks_executed']) > 5:
                output += f"   ... and {len(analysis['tasks_executed']) - 5} more\n"
        
        if analysis.get("test_results"):
            results = analysis["test_results"]
            output += f"🧪 Test Results:\n"
            output += f"   Total: {results.get('total', 0)}\n"
            if results.get('failed', 0) > 0:
                output += f"   ❌ Failed: {results['failed']}\n"
            else:
                output += f"   ✅ All tests passed\n"
        
        if analysis.get("build_time"):
            output += f"⏱️ Build time: {analysis['build_time']}\n"
        
        if analysis.get("cache_hits", 0) > 0:
            output += f"🚀 Cache optimization: {analysis['cache_hits']} tasks cached\n"
        
        if analysis.get("deprecated_features"):
            output += f"⚠️ Deprecated features detected: {len(analysis['deprecated_features'])} warnings\n"
        
        return output
    
    def _handle_gradle_error(self, output: str, exit_code: int, command: str, analysis: Dict[str, Any]) -> ToolResult:
        """Handle Gradle execution errors with detailed analysis."""
        error_message = f"Gradle command failed with exit code {exit_code}"
        suggestions = []
        
        # Analyze specific error patterns
        if analysis.get("compilation_errors"):
            error_message = "Compilation failed"
            suggestions.extend([
                "Check the Java source code for syntax errors",
                "Verify all imports are correct",
                "Ensure all dependencies are properly declared",
                "Run with --stacktrace for detailed error information"
            ])
        
        elif analysis.get("test_failures"):
            error_message = f"Tests failed: {len(analysis['test_failures'])} test(s) failed"
            suggestions.extend([
                "Review the test failure details in the output",
                "Run specific failing tests with --tests <TestClass>",
                "Check test logs in build/reports/tests/",
                "Run with --info for more detailed test output"
            ])
        
        elif analysis.get("dependency_errors"):
            error_message = "Dependency resolution failed"
            suggestions.extend([
                "Check your internet connection",
                "Verify repository URLs in build.gradle",
                "Try running with --refresh-dependencies",
                "Check if required repositories are configured",
                "Run 'gradle dependencies' to analyze dependency tree"
            ])
        
        elif "permission denied" in output.lower():
            error_message = "Permission denied error"
            suggestions.extend([
                "Ensure gradlew is executable: chmod +x gradlew",
                "Check file permissions in the project directory",
                "Verify Docker container has proper permissions"
            ])
        
        elif "out of memory" in output.lower() or "heap space" in output.lower():
            error_message = "Out of memory error"
            suggestions.extend([
                "Increase JVM heap size with -Xmx flag",
                "Add 'org.gradle.jvmargs=-Xmx2g' to gradle.properties",
                "Close other applications to free memory",
                "Use --no-daemon to avoid daemon memory issues"
            ])
        
        elif "could not find or load main class" in output.lower():
            error_message = "Gradle wrapper or Java configuration issue"
            suggestions.extend([
                "Regenerate Gradle wrapper: gradle wrapper",
                "Check JAVA_HOME environment variable",
                "Verify Java installation with 'java -version'",
                "Ensure gradle-wrapper.jar exists in gradle/wrapper/"
            ])
        
        else:
            # Generic error handling
            suggestions.extend([
                "Run with --stacktrace option for more details",
                "Run with --info or --debug for verbose output",
                "Check build.gradle for configuration errors",
                "Try running 'gradle clean' before building",
                "Verify all required plugins are properly configured"
            ])
        
        # Extract the most relevant error snippet
        error_snippet = self._extract_gradle_key_info(output)
        
        raise ToolError(
            message=error_message,
            suggestions=suggestions,
            documentation_links=[
                "https://docs.gradle.org/current/userguide/troubleshooting.html",
                "https://docs.gradle.org/current/userguide/command_line_interface.html#sec:command_line_debugging"
            ],
            error_code="GRADLE_BUILD_FAILED",
            raw_output=output,
            metadata={
                "exit_code": exit_code,
                "command": command,
                "analysis": analysis,
                "error_snippet": error_snippet
            }
        )
    
    def _extract_gradle_key_info(self, output: str) -> str:
        """Extract key information from Gradle output."""
        if not output:
            return "No output"
        
        lines = output.split('\n')
        key_patterns = [
            r'BUILD FAILED',
            r'BUILD SUCCESSFUL',
            r'FAILURE:',
            r'> Task .* FAILED',
            r'error:',
            r'Error:',
            r'caused by:',
            r'\* What went wrong:',
            r'\* Try:',
            r'tests? failed',
            r'compilation failed',
            r'could not resolve'
        ]
        
        key_lines = []
        for i, line in enumerate(lines):
            for pattern in key_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    # Include some context
                    start = max(0, i - 2)
                    end = min(len(lines), i + 5)
                    key_lines.extend(lines[start:end])
                    break
        
        if key_lines:
            # Remove duplicates while preserving order
            seen = set()
            unique_lines = []
            for line in key_lines:
                if line not in seen:
                    seen.add(line)
                    unique_lines.append(line)
            return '\n'.join(unique_lines[:50])  # Limit to 50 lines
        
        # If no key patterns found, return the last 30 lines
        return '\n'.join(lines[-30:])
    
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get parameters schema for function calling."""
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "string",
                    "description": "Gradle tasks to execute (e.g., 'clean build', 'test', 'assemble')"
                },
                "properties": {
                    "type": "string", 
                    "description": "Gradle properties (e.g., '-Pversion=1.0', '-PskipTests')"
                },
                "gradle_args": {
                    "type": "string",
                    "description": "Additional Gradle arguments (e.g., '--info', '--stacktrace', '--scan')"
                },
                "build_file": {
                    "type": "string",
                    "description": "Specific build file to use (e.g., 'custom.gradle')"
                },
                "raw_output": {
                    "type": "boolean",
                    "description": "Return raw Gradle output for detailed analysis",
                    "default": False
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory to execute Gradle in",
                    "default": "/workspace"
                },
                "use_wrapper": {
                    "type": "boolean",
                    "description": "Prefer gradlew wrapper over system gradle",
                    "default": True
                },
                "parallel": {
                    "type": "boolean",
                    "description": "Enable parallel execution",
                    "default": False
                },
                "build_cache": {
                    "type": "boolean",
                    "description": "Use Gradle build cache",
                    "default": True
                }
            },
            "required": []
        }