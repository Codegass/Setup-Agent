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
    
    def execute(
        self,
        command: str,
        goals: str = None,
        profiles: str = None,
        properties: str = None,
        raw_output: bool = False,
        working_directory: str = "/workspace",
        timeout: int = 300
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
        
        # Build Maven command
        maven_cmd = self._build_maven_command(command, goals, profiles, properties)
        
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
    
    def _build_maven_command(self, command: str, goals: str, profiles: str, properties: str) -> str:
        """Build the complete Maven command."""
        cmd_parts = ["mvn"]
        
        # Add profiles
        if profiles:
            cmd_parts.append(f"-P{profiles}")
        
        # Add properties
        if properties:
            for prop in properties.split(","):
                cmd_parts.append(f"-D{prop.strip()}")
        
        # Add command and goals
        if goals:
            cmd_parts.append(f"{command} {goals}")
        else:
            cmd_parts.append(command)
        
        return " ".join(cmd_parts)
    
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
                logger.info("Maven installed successfully")
                return ToolResult(
                    success=True,
                    output="Maven installed successfully",
                    metadata={"auto_installed": True}
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
        
        return ToolResult(
            success=False,
            output="",
            error=error_message,
            error_code=error_code,
            suggestions=error_suggestions,
            documentation_links=documentation_links,
            raw_output=output,
            metadata={
                "command": command,
                "exit_code": exit_code,
                "analysis": analysis
            }
        )
    
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