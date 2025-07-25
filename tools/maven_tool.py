"""Maven tool with comprehensive error handling and raw output access."""

import json
import re
from typing import Dict, Any, Optional

from loguru import logger

from .base import BaseTool, ToolResult, ToolError


class MavenTool(BaseTool):
    """Maven build tool with enhanced error handling and raw output access."""
    
    def __init__(self, orchestrator):
        super().__init__(
            name="maven",
            description="Execute Maven commands with comprehensive error analysis and raw output access. "
                       "Supports all Maven lifecycle phases, dependency management, and build analysis. "
                       "Automatically installs Maven if not present."
        )
        self.orchestrator = orchestrator
    
    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use Maven-specific extraction."""
        if tool_name == "maven" or tool_name == self.name:
            return self._extract_maven_key_info(output)
        return output
    
    def execute(
        self,
        command: str,
        goals: str = None,
        profiles: str = None,
        properties: str = None,
        raw_output: bool = False,
        working_directory: str = "/workspace",
        timeout: int = 300,
        pom_file: str = None,
        **kwargs  # Accept any additional parameters
    ) -> ToolResult:
        """
        Execute Maven commands with comprehensive error handling.
        
        Args:
            command: Maven command (e.g., 'clean', 'compile', 'test', 'package', 'install')
            goals: Additional goals to run (e.g., 'clean compile', 'test-compile')
            profiles: Maven profiles to activate (e.g., 'dev,test')
            properties: Maven properties (e.g., 'skipTests=true,maven.test.skip=true')
            raw_output: Whether to return raw Maven output for detailed analysis
            working_directory: Directory to execute Maven in
            timeout: Command timeout in seconds
        """
        
        # Check if Maven is installed, install if not
        if not self._is_maven_installed():
            install_result = self._install_maven()
            if not install_result.success:
                return install_result
        
        # If pom_file is specified, extract directory and use it as working_directory
        if pom_file and pom_file.endswith("pom.xml"):
            import os
            pom_dir = os.path.dirname(pom_file)
            if pom_dir and pom_dir != working_directory:
                working_directory = pom_dir
                logger.info(f"🔧 Using directory from pom_file: {working_directory}")
        
        # Build Maven command
        maven_cmd = self._build_maven_command(command, goals, profiles, properties, pom_file)
        
        # Execute the command
        try:
            result = self.orchestrator.execute_command(
                maven_cmd,
                workdir=working_directory
            )
            
            # Analyze the output
            analysis = self._analyze_maven_output(result["output"], result["exit_code"])
            
            if raw_output:
                return ToolResult(
                    success=result["exit_code"] == 0,
                    output=result["output"],
                    raw_output=result["output"],
                    metadata={
                        "command": maven_cmd,
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
                        "command": maven_cmd,
                        "exit_code": result["exit_code"],
                        "analysis": analysis
                    }
                )
            else:
                return self._handle_maven_error(result["output"], result["exit_code"], maven_cmd, analysis)
                
        except Exception as e:
            raise ToolError(
                message=f"Failed to execute Maven command: {str(e)}",
                suggestions=[
                    "Check if Maven is installed in the container",
                    "Verify the working directory contains a pom.xml file",
                    "Check Docker container connectivity"
                ],
                documentation_links=[
                    "https://maven.apache.org/guides/getting-started/maven-in-five-minutes.html"
                ],
                error_code="MAVEN_EXECUTION_ERROR"
            )
    
    def _build_maven_command(self, command: str, goals: str, profiles: str, properties: str, pom_file: str = None) -> str:
        """Build the complete Maven command."""
        cmd_parts = ["mvn"]
        
        # Add profiles
        if profiles:
            cmd_parts.append(f"-P{profiles}")
        
        # Add properties
        if properties:
            for prop in properties.split(","):
                cmd_parts.append(f"-D{prop.strip()}")
        
        # Add pom file if specified
        if pom_file:
            cmd_parts.extend(["-f", pom_file])
        
        # Add command and goals
        if goals:
            cmd_parts.append(f"{command} {goals}")
        else:
            cmd_parts.append(command)
        
        return " ".join(cmd_parts)
    
    def _ensure_maven_in_path(self):
        """Ensure Maven is accessible in PATH for bash tool compatibility."""
        try:
            # Check if mvn is already in PATH
            which_result = self.orchestrator.execute_command("which mvn")
            if which_result["exit_code"] == 0:
                mvn_path = which_result["output"].strip()
                logger.debug(f"Maven found at: {mvn_path}")
                
                # Create symlink in /usr/local/bin if not already there
                if not mvn_path.startswith("/usr/local/bin"):
                    self.orchestrator.execute_command(f"ln -sf {mvn_path} /usr/local/bin/mvn")
                    logger.info("Created symlink for mvn in /usr/local/bin")
                
                # Update system-wide PATH by modifying profile
                profile_commands = [
                    "echo 'export PATH=/usr/local/bin:$PATH' >> /etc/profile",
                    "echo 'export PATH=/usr/local/bin:$PATH' >> /root/.bashrc",
                    "chmod +x /usr/local/bin/mvn"
                ]
                
                for cmd in profile_commands:
                    result = self.orchestrator.execute_command(cmd)
                    if not result["success"]:
                        logger.warning(f"Failed to execute: {cmd}")
                
                logger.info("Updated system PATH to include /usr/local/bin")
                
        except Exception as e:
            logger.warning(f"Failed to ensure Maven in PATH: {e}")

    def _setup_java_environment(self):
        """Setup JAVA_HOME environment variable for newly installed JDK."""
        try:
            # Find JAVA_HOME for the latest JDK
            find_java_result = self.orchestrator.execute_command("find /usr/lib/jvm -name 'java-*-openjdk*' -type d | sort -V | tail -1")
            
            if find_java_result["exit_code"] == 0 and find_java_result["output"].strip():
                java_home = find_java_result["output"].strip()
                logger.info(f"Found Java installation at: {java_home}")
                
                # Set JAVA_HOME system-wide
                java_env_commands = [
                    f"echo 'export JAVA_HOME={java_home}' >> /etc/profile",
                    f"echo 'export JAVA_HOME={java_home}' >> /root/.bashrc",
                    f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /etc/profile",
                    f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /root/.bashrc"
                ]
                
                for cmd in java_env_commands:
                    result = self.orchestrator.execute_command(cmd)
                    if not result["success"]:
                        logger.warning(f"Failed to execute: {cmd}")
                
                # Also update current session environment
                self.orchestrator.execute_command(f"export JAVA_HOME={java_home}")
                self.orchestrator.execute_command(f"export PATH=$JAVA_HOME/bin:$PATH")
                
                logger.info(f"Set JAVA_HOME to {java_home}")
                return java_home
            else:
                logger.warning("Could not find Java installation directory")
                return None
                
        except Exception as e:
            logger.warning(f"Failed to setup Java environment: {e}")
            return None

    def _is_maven_installed(self) -> bool:
        """Check if Maven is installed."""
        try:
            result = self.orchestrator.execute_command("which mvn")
            return result["exit_code"] == 0
        except Exception:
            return False
    
    def _install_maven(self) -> ToolResult:
        """Install Maven automatically."""
        logger.info("Maven not found, installing automatically...")
        
        try:
            # Update package lists
            update_result = self.orchestrator.execute_command("apt-get update")
            if update_result["exit_code"] != 0:
                logger.warning("Failed to update package lists, continuing anyway...")
            
            # Install Maven
            install_result = self.orchestrator.execute_command("apt-get install -y maven")
            
            if install_result["exit_code"] == 0:
                # Ensure mvn is in PATH for bash tool compatibility
                self._ensure_maven_in_path()
                logger.info("Maven installed successfully and added to PATH")
                return ToolResult(
                    success=True,
                    output="Maven installed successfully and added to PATH",
                    metadata={"auto_installed": True, "path_updated": True}
                )
            else:
                return ToolResult(
                    success=False,
                    output=install_result["output"],
                    error="Failed to install Maven automatically",
                    error_code="MAVEN_INSTALL_FAILED",
                    suggestions=[
                        "Check network connectivity",
                        "Try running: apt-get update && apt-get install -y maven",
                        "Verify package repositories are accessible"
                    ],
                    documentation_links=["https://maven.apache.org/install.html"]
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to install Maven: {str(e)}",
                error_code="MAVEN_INSTALL_ERROR",
                suggestions=[
                    "Check Docker container permissions",
                    "Verify apt-get is available",
                    "Try manual installation"
                ]
            )
    
    def _analyze_maven_output(self, output: str, exit_code: int) -> Dict[str, Any]:
        """Analyze Maven output for key information."""
        analysis = {
            "build_success": exit_code == 0,
            "phases_executed": [],
            "tests_run": None,
            "compilation_errors": [],
            "dependency_issues": [],
            "warnings": [],
            "build_time": None,
            "artifacts_created": []
        }
        
        lines = output.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Extract executed phases
            if "--- maven-" in line and "plugin:" in line:
                phase_match = re.search(r'--- maven-(\w+)-plugin:', line)
                if phase_match:
                    analysis["phases_executed"].append(phase_match.group(1))
            
            # Extract test results
            if "Tests run:" in line:
                test_match = re.search(r'Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)', line)
                if test_match:
                    analysis["tests_run"] = {
                        "total": int(test_match.group(1)),
                        "failures": int(test_match.group(2)),
                        "errors": int(test_match.group(3)),
                        "skipped": int(test_match.group(4))
                    }
            
            # Extract compilation errors
            if "[ERROR]" in line and (".java:" in line or "compilation error" in line.lower()):
                analysis["compilation_errors"].append(line)
            
            # Extract dependency issues
            if "Could not resolve dependencies" in line or "Dependency resolution failed" in line:
                analysis["dependency_issues"].append(line)
            
            # Extract warnings
            if "[WARNING]" in line:
                analysis["warnings"].append(line)
            
            # Extract build time
            if "Total time:" in line:
                time_match = re.search(r'Total time: (.+)', line)
                if time_match:
                    analysis["build_time"] = time_match.group(1).strip()
            
            # Extract created artifacts
            if "Building jar:" in line:
                artifact_match = re.search(r'Building jar: (.+)', line)
                if artifact_match:
                    analysis["artifacts_created"].append(artifact_match.group(1).strip())
        
        return analysis
    
    def _format_success_output(self, analysis: Dict[str, Any]) -> str:
        """Format successful build output."""
        output = "✅ Maven build completed successfully!\n\n"
        
        if analysis["phases_executed"]:
            output += f"Phases executed: {', '.join(set(analysis['phases_executed']))}\n"
        
        if analysis["tests_run"]:
            tests = analysis["tests_run"]
            output += f"Tests: {tests['total']} run, {tests['failures']} failures, {tests['errors']} errors, {tests['skipped']} skipped\n"
        
        if analysis["artifacts_created"]:
            output += f"Artifacts created:\n"
            for artifact in analysis["artifacts_created"]:
                output += f"  • {artifact}\n"
        
        if analysis["build_time"]:
            output += f"Build time: {analysis['build_time']}\n"
        
        if analysis["warnings"]:
            output += f"\n⚠️ Warnings ({len(analysis['warnings'])}): Use raw_output=true to see details\n"
        
        return output
    
    def _handle_maven_error(self, output: str, exit_code: int, command: str, analysis: Dict[str, Any]) -> ToolResult:
        """Handle Maven build errors with detailed analysis."""
        
        error_suggestions = []
        documentation_links = []
        error_code = "MAVEN_BUILD_ERROR"
        
        # Analyze specific error types
        if analysis["compilation_errors"]:
            error_code = "COMPILATION_ERROR"
            error_suggestions.extend([
                "Fix compilation errors in your Java source files",
                "Check for missing imports and typos",
                "Ensure all dependencies are available",
                "Use 'maven compile' with raw_output=true to see detailed compilation errors"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/compile.html")
        
        if analysis["dependency_issues"]:
            error_code = "DEPENDENCY_ERROR"
            error_suggestions.extend([
                "Check your pom.xml dependencies for correctness",
                "Verify dependency versions are compatible",
                "Try running 'maven dependency:resolve' to debug dependency issues",
                "Check if Maven repositories are accessible"
            ])
            documentation_links.append("https://maven.apache.org/guides/introduction/introduction-to-dependency-mechanism.html")
        
        if analysis["tests_run"] and (analysis["tests_run"]["failures"] > 0 or analysis["tests_run"]["errors"] > 0):
            error_code = "TEST_FAILURE"
            error_suggestions.extend([
                "Fix failing tests or use -DskipTests=true to skip tests temporarily",
                "Run 'maven test' with raw_output=true to see detailed test failure information",
                "Check test logs for specific failure reasons"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/test.html")
        
        if "mvn: command not found" in output:
            error_code = "MAVEN_NOT_FOUND"
            error_suggestions.extend([
                "Install Maven in the container",
                "Use bash tool to run: 'apt update && apt install -y maven'",
                "Verify Maven installation with 'mvn --version'"
            ])
            documentation_links.append("https://maven.apache.org/install.html")
        
        if "No pom.xml found" in output or "The goal you specified requires a project to execute" in output:
            error_code = "NO_POM_XML"
            error_suggestions.extend([
                "Ensure you're in a directory containing a pom.xml file",
                "Change to the correct project directory",
                "Create a new Maven project with 'mvn archetype:generate'"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/maven-in-five-minutes.html")
        
        # Default suggestions if no specific error type identified
        if not error_suggestions:
            error_suggestions = [
                "Check the full Maven output for detailed error information",
                "Try running with -X flag for debug output: maven command='clean compile -X'",
                "Verify your pom.xml file is valid",
                "Check if all required dependencies are available"
            ]
        
        error_message = f"Maven build failed with exit code {exit_code}"
        
        if analysis["compilation_errors"]:
            error_message += f"\nCompilation errors found: {len(analysis['compilation_errors'])}"
        
        if analysis["tests_run"]:
            tests = analysis["tests_run"]
            if tests["failures"] > 0 or tests["errors"] > 0:
                error_message += f"\nTest failures: {tests['failures']}, Test errors: {tests['errors']}"
        
        # Extract key error lines for immediate visibility
        error_output = self._extract_key_error_lines(output)
        
        return ToolResult(
            success=False,
            output=error_output,  # Show key error lines immediately
            error=error_message,
            error_code=error_code,
            suggestions=error_suggestions,
            documentation_links=documentation_links,
            raw_output=output,
            metadata={
                "command": command,
                "exit_code": exit_code,
                "analysis": analysis,
                "key_errors_extracted": True
            }
        )
    
    def _extract_key_error_lines(self, output: str) -> str:
        """Extract key error lines from Maven output for immediate visibility."""
        if not output:
            return "No output available"
        
        lines = output.split('\n')
        key_lines = []
        
        # Look for critical error patterns (expanded list)
        error_patterns = [
            r'\[ERROR\].*compilation.*failed',
            r'\[ERROR\].*Failed to execute goal',
            r'\[ERROR\].*Cannot resolve dependencies',
            r'\[ERROR\].*No compiler is provided',
            r'\[ERROR\].*Java compiler.*error',
            r'\[ERROR\].*Tests in error',
            r'\[ERROR\].*BUILD FAILURE',
            r'\[ERROR\].*could not find or load main class',
            r'\[ERROR\].*package .* does not exist',
            r'\[ERROR\].*cannot find symbol',
            r'\[ERROR\].*class .* is public, should be declared in a file named',
            r'\[ERROR\].*dependency resolution failed',
            r'\[ERROR\].*artifact .* not found',
            r'mvn: command not found',
            r'No pom\.xml found',
            r'COMPILATION ERROR',
            r'Test.*FAILED',
            r'.*\.java:\d+: error:',  # Java compilation errors with line numbers
            r'Exception in thread',
            r'Caused by:',
            r'BUILD FAILURE',
            r'BUILD ERROR',
        ]
        
        # Extract lines matching error patterns with some context
        for i, line in enumerate(lines):
            for pattern in error_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    key_lines.append(line.strip())
                    # Add next 2 lines for context if they contain useful information
                    for j in range(1, 3):
                        if i + j < len(lines) and lines[i + j].strip():
                            next_line = lines[i + j].strip()
                            # Only add if it looks like error context (starts with space, contains specific keywords, etc.)
                            if (next_line.startswith(' ') or 
                                any(word in next_line.lower() for word in ['at ', 'symbol:', 'location:', 'required:', 'found:']) or
                                re.search(r'^\s*\^', next_line)):  # Compilation error pointer
                                key_lines.append(next_line)
                    break
        
        # If no specific patterns found, get lines containing ERROR, FAIL, EXCEPTION with context
        if not key_lines:
            for i, line in enumerate(lines):
                if any(word in line.upper() for word in ['ERROR', 'FAIL', 'EXCEPTION']):
                    # Add previous line for context if available
                    if i > 0 and lines[i-1].strip():
                        key_lines.append(lines[i-1].strip())
                    key_lines.append(line.strip())
                    # Add next line for context if available
                    if i + 1 < len(lines) and lines[i+1].strip():
                        key_lines.append(lines[i+1].strip())
                    if len(key_lines) >= 15:  # Limit to avoid too much output
                        break
        
        # Enhanced fallback: get last meaningful lines if still nothing
        if not key_lines:
            # Look for the last non-empty, non-trivial lines
            meaningful_lines = []
            for line in reversed(lines):
                stripped = line.strip()
                if (stripped and 
                    not stripped.startswith('[INFO]') and
                    not stripped.startswith('--------') and
                    len(stripped) > 5):  # Skip very short lines
                    meaningful_lines.insert(0, stripped)
                    if len(meaningful_lines) >= 15:
                        break
            key_lines = meaningful_lines
        
        result = "🚨 Maven Build Error Details:\n\n"
        if key_lines:
            # Remove duplicates while preserving order
            seen = set()
            unique_lines = []
            for line in key_lines:
                if line not in seen:
                    seen.add(line)
                    unique_lines.append(line)
            
            result += "\n".join(unique_lines[:20])  # Increased limit for better context
            if len(unique_lines) > 20:
                result += f"\n... and {len(unique_lines) - 20} more lines (use raw_output=true for full details)"
        else:
            result += "No error information could be extracted. Use raw_output=true for full Maven output."
        
        return result

    def get_usage_example(self) -> str:
        """Get usage examples for Maven tool."""
        return """
Maven Tool Usage Examples:

Basic commands:
• maven(command="clean")
• maven(command="compile")
• maven(command="test")
• maven(command="package")

Advanced usage:
• maven(command="clean", goals="compile test")
• maven(command="test", properties="skipTests=false")
• maven(command="package", profiles="production")
• maven(command="install", raw_output=true)  # Get full Maven output

For debugging:
• maven(command="compile", raw_output=true)  # See full compilation details
• maven(command="dependency:tree")  # Analyze dependencies
• maven(command="help:effective-pom")  # See effective POM
"""
    
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Maven command (e.g., 'clean', 'compile', 'test', 'package', 'install')",
                },
                "goals": {
                    "type": "string",
                    "description": "Additional goals to run (e.g., 'clean compile', 'test-compile')",
                    "default": None,
                },
                "profiles": {
                    "type": "string",
                    "description": "Maven profiles to activate (e.g., 'dev,test')",
                    "default": None,
                },
                "properties": {
                    "type": "string",
                    "description": "Maven properties (e.g., 'skipTests=true,maven.test.skip=true')",
                    "default": None,
                },
                "raw_output": {
                    "type": "boolean",
                    "description": "Whether to return raw Maven output for detailed analysis",
                    "default": False,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory to execute Maven in",
                    "default": "/workspace",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Command timeout in seconds",
                    "default": 300,
                },
                "pom_file": {
                    "type": "string",
                    "description": "Path to specific pom.xml file to use",
                    "default": None,
                },
            },
            "required": ["command"],
        }