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
        
        # Validate that pom.xml exists in the working directory
        pom_validation = self._validate_pom_exists(working_directory, pom_file)
        if not pom_validation["exists"]:
            return self._handle_missing_pom(pom_validation, working_directory)
        
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
            # Use extended timeout for Maven commands which often download dependencies
            # Check if this is a potentially long-running command
            is_long_running = any(cmd in maven_cmd for cmd in [
                'clean', 'compile', 'test', 'package', 'install', 'deploy', 'verify', 'site'
            ])
            
            if is_long_running and hasattr(self.orchestrator, 'execute_command_with_monitoring'):
                # Use monitoring version with extended timeouts for build commands
                logger.info(f"Executing Maven command with extended timeout: {maven_cmd}")
                result = self.orchestrator.execute_command_with_monitoring(
                    maven_cmd,
                    workdir=working_directory,
                    silent_timeout=1200,  # 20 minutes for no output (dependency downloads)
                    absolute_timeout=3600,  # 60 minutes total
                    optimize_for_maven=True
                )
            else:
                # Use regular version for quick commands like help, version, etc.
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

    def _validate_pom_exists(self, working_directory: str, pom_file: str = None) -> dict:
        """Validate that pom.xml exists and is accessible."""
        if not self.orchestrator:
            return {"exists": False, "error": "No orchestrator available"}
        
        # Check for pom.xml in working directory
        pom_path = pom_file if pom_file else f"{working_directory}/pom.xml"
        check_cmd = f"test -f {pom_path} && echo 'EXISTS' || echo 'NOT_FOUND'"
        
        try:
            result = self.orchestrator.execute_command(check_cmd)
            exists = "EXISTS" in result.get("output", "")
            
            if not exists:
                # Try to find pom.xml in subdirectories
                find_cmd = f"find {working_directory} -name pom.xml -type f 2>/dev/null | head -5"
                find_result = self.orchestrator.execute_command(find_cmd)
                found_poms = [p.strip() for p in find_result.get("output", "").split("\n") if p.strip()]
                
                return {
                    "exists": False,
                    "searched_path": pom_path,
                    "found_alternatives": found_poms,
                    "working_directory": working_directory
                }
            
            return {"exists": True, "path": pom_path}
            
        except Exception as e:
            return {"exists": False, "error": str(e)}
    
    def _handle_missing_pom(self, validation_result: dict, working_directory: str) -> ToolResult:
        """Handle the case when pom.xml is not found."""
        suggestions = [
            f"Ensure you're in the correct project directory",
            f"Current directory: {working_directory}",
            "Change to the project root directory containing pom.xml"
        ]
        
        # If we found alternative pom.xml files, suggest them
        if validation_result.get("found_alternatives"):
            suggestions.append("Found pom.xml in these locations:")
            for pom_path in validation_result["found_alternatives"][:3]:
                pom_dir = pom_path.replace("/pom.xml", "")
                suggestions.append(f"  - Try: maven(working_directory='{pom_dir}')")
        else:
            suggestions.extend([
                "Use bash tool to navigate: bash(command='ls -la', workdir='/workspace')",
                "Find pom.xml: bash(command='find /workspace -name pom.xml')"
            ])
        
        return ToolResult(
            success=False,
            output="",
            error=f"No pom.xml found at {validation_result.get('searched_path', working_directory)}",
            error_code="NO_POM_XML",
            suggestions=suggestions,
            metadata={
                "validation_result": validation_result,
                "working_directory": working_directory
            }
        )
    
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
        """Enhanced error handling with specific suggestions based on error type."""
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
                "Use bash to find pom.xml: bash(command='find /workspace -name pom.xml')",
                "List current directory: bash(command='ls -la', workdir='/workspace')"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/maven-in-five-minutes.html")
        
        # Check for Java version issues (including Maven Enforcer plugin)
        if ("Unsupported major.minor version" in output or 
            "java.lang.UnsupportedClassVersionError" in output or
            "RequireJavaVersion" in output or
            "Detected JDK" in output and "not in the allowed range" in output):
            error_code = "JAVA_VERSION_ERROR"
            
            # Try to extract required Java version from different error patterns
            java_version = None
            
            # Pattern 1: Maven Enforcer plugin - "not in the allowed range [17,)"
            enforcer_match = re.search(r'allowed range \[(\d+),', output)
            if enforcer_match:
                java_version = enforcer_match.group(1)
            
            # Pattern 2: Traditional version error - "version 55.0" maps to Java versions
            version_match = re.search(r'version (\d+\.\d+)', output)
            if not java_version and version_match:
                class_version = float(version_match.group(1))
                # Map class file version to Java version
                version_map = {52.0: "8", 53.0: "9", 54.0: "10", 55.0: "11", 
                              56.0: "12", 57.0: "13", 58.0: "14", 59.0: "15",
                              60.0: "16", 61.0: "17", 62.0: "18", 63.0: "19", 
                              64.0: "20", 65.0: "21"}
                java_version = version_map.get(class_version, str(int(class_version - 44)))
            
            # Pattern 3: Extract current vs required from enforcer message
            current_match = re.search(r'version (\d+(?:\.\d+)*) which is not', output)
            if current_match and not java_version:
                # If we see current version is too low, suggest next LTS
                current = int(current_match.group(1).split('.')[0])
                if current < 17:
                    java_version = "17"  # Suggest Java 17 LTS
                elif current < 21:
                    java_version = "21"  # Suggest Java 21 LTS
            
            if not java_version:
                java_version = "17"  # Default to Java 17 LTS if can't determine
            
            error_suggestions.extend([
                f"Java version mismatch detected - Java {java_version} or higher is required",
                f"Install Java {java_version}: bash(command='apt-get update && apt-get install -y openjdk-{java_version}-jdk')",
                f"Set JAVA_HOME: bash(command='export JAVA_HOME=/usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture)')",
                f"Update alternatives: bash(command='update-alternatives --set java /usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture)/bin/java')",
                "Verify installation: bash(command='java -version && javac -version')"
            ])
        
        # Check for missing compiler
        if "No compiler is provided" in output or "Unable to locate the Javac Compiler" in output:
            error_code = "NO_JAVA_COMPILER"
            error_suggestions.extend([
                "Java Development Kit (JDK) not found, only JRE is installed",
                "Install JDK: bash(command='apt-get update && apt-get install -y default-jdk')",
                "Or install specific version: bash(command='apt-get install -y openjdk-11-jdk')",
                "Verify javac installation: bash(command='javac -version')"
            ])
        
        # Check for memory issues
        if "java.lang.OutOfMemoryError" in output or "GC overhead limit exceeded" in output:
            error_code = "OUT_OF_MEMORY"
            error_suggestions.extend([
                "Increase JVM memory allocation",
                "Try: maven(command='compile', properties='maven.compiler.fork=true,maven.compiler.meminitial=256m,maven.compiler.maxmem=1024m')",
                "Or set MAVEN_OPTS: bash(command='export MAVEN_OPTS=\"-Xmx2048m -XX:MaxPermSize=512m\"')",
                "Consider building modules separately if project is large"
            ])
        
        # Check for network/proxy issues
        if "Connection timed out" in output or "Could not transfer artifact" in output or "Connection refused" in output:
            error_code = "NETWORK_ERROR"
            error_suggestions.extend([
                "Network connectivity issue detected",
                "Check Maven repository accessibility: bash(command='curl -I https://repo.maven.apache.org/maven2/')",
                "Try with offline mode if dependencies are cached: maven(command='compile', properties='offline=true')",
                "Configure proxy if behind firewall (update ~/.m2/settings.xml)"
            ])
        
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
                "key_errors_extracted": True,
                "error_type": error_code,
                "recovery_actions": self._generate_recovery_actions(error_code, analysis),
                "diagnostic_commands": self._get_diagnostic_commands(error_code)
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
    
    def _generate_recovery_actions(self, error_code: str, analysis: Dict[str, Any]) -> list:
        """Generate specific recovery actions based on error type."""
        recovery_actions = []
        
        if error_code == "COMPILATION_ERROR":
            recovery_actions.extend([
                {"action": "view_errors", "command": "maven(command='compile', raw_output=true)"},
                {"action": "check_syntax", "description": "Review Java syntax in source files"},
                {"action": "verify_imports", "description": "Check all import statements are correct"}
            ])
        elif error_code == "DEPENDENCY_ERROR":
            recovery_actions.extend([
                {"action": "resolve_deps", "command": "maven(command='dependency:resolve')"},
                {"action": "show_tree", "command": "maven(command='dependency:tree')"},
                {"action": "force_update", "command": "maven(command='clean', properties='U=true')"}
            ])
        elif error_code == "TEST_FAILURE":
            test_info = analysis.get("tests_run", {})
            recovery_actions.extend([
                {"action": "skip_tests", "command": "maven(command='package', properties='skipTests=true')"},
                {"action": "run_specific_test", "description": "Run individual test class to isolate issue"},
                {"action": "view_test_reports", "command": "bash(command='find /workspace/target/surefire-reports -name *.txt | head -5 | xargs cat')"}
            ])
        elif error_code == "NO_POM_XML":
            recovery_actions.extend([
                {"action": "find_pom", "command": "bash(command='find /workspace -name pom.xml -type f')"},
                {"action": "list_dirs", "command": "bash(command='ls -la /workspace')"},
                {"action": "check_structure", "command": "bash(command='tree -L 2 /workspace 2>/dev/null || find /workspace -maxdepth 2 -type d')"}
            ])
        elif error_code == "JAVA_VERSION_ERROR":
            # Extract Java version from error analysis if available
            required_version = "17"  # Default to Java 17 LTS
            if "error_suggestions" in locals():
                for suggestion in error_suggestions:
                    if "Java" in suggestion and "required" in suggestion:
                        import re
                        ver_match = re.search(r'Java (\d+)', suggestion)
                        if ver_match:
                            required_version = ver_match.group(1)
                            break
            
            recovery_actions.extend([
                {"action": "check_current", "command": "bash(command='java -version 2>&1 && echo \"---\" && javac -version 2>&1')"},
                {"action": "list_available", "command": "bash(command='apt-cache search openjdk | grep -E \"openjdk-[0-9]+-jdk\" | sort -V')"},
                {"action": "install_required", "command": f"bash(command='apt-get update && apt-get install -y openjdk-{required_version}-jdk')"},
                {"action": "set_java_home", "command": f"bash(command='export JAVA_HOME=/usr/lib/jvm/java-{required_version}-openjdk-$(dpkg --print-architecture) && echo $JAVA_HOME')"},
                {"action": "update_alternatives", "command": f"bash(command='update-alternatives --set java /usr/lib/jvm/java-{required_version}-openjdk-$(dpkg --print-architecture)/bin/java')"},
                {"action": "verify_install", "command": "bash(command='java -version && mvn -version')"}
            ])
        elif error_code == "OUT_OF_MEMORY":
            recovery_actions.extend([
                {"action": "increase_memory", "command": "bash(command='export MAVEN_OPTS=\"-Xmx2048m\"')"},
                {"action": "build_modules", "description": "Build project modules separately"},
                {"action": "clean_target", "command": "maven(command='clean')"}
            ])
        
        return recovery_actions
    
    def _get_diagnostic_commands(self, error_code: str) -> list:
        """Get diagnostic commands to help debug the specific error."""
        diagnostics = []
        
        if error_code == "COMPILATION_ERROR":
            diagnostics.extend([
                "maven(command='compile', properties='maven.compiler.verbose=true')",
                "bash(command='find /workspace/src -name *.java | head -5 | xargs head -20')"
            ])
        elif error_code == "DEPENDENCY_ERROR":
            diagnostics.extend([
                "maven(command='dependency:analyze')",
                "bash(command='cat /workspace/pom.xml | grep -A 5 -B 5 dependency')"
            ])
        elif error_code == "TEST_FAILURE":
            diagnostics.extend([
                "maven(command='test', properties='maven.surefire.debug=true')",
                "bash(command='ls -la /workspace/target/surefire-reports/')"
            ])
        elif error_code == "JAVA_VERSION_ERROR":
            diagnostics.extend([
                "bash(command='echo $JAVA_HOME')",
                "bash(command='which java && which javac')"
            ])
        elif error_code == "NETWORK_ERROR":
            diagnostics.extend([
                "bash(command='ping -c 3 repo.maven.apache.org 2>/dev/null || echo Network unreachable')",
                "bash(command='cat ~/.m2/settings.xml 2>/dev/null || echo No settings.xml')"
            ])
        
        return diagnostics

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