"""Maven tool with comprehensive error handling and raw output access."""

import re
from typing import Dict, Any, Optional
from pathlib import Path

from loguru import logger

from .base import BaseTool, ToolResult, ToolError
from .command_tracker import CommandTracker
from agent.output_storage import OutputStorageManager


class MavenTool(BaseTool):
    """Maven build tool with enhanced error handling and raw output access."""
    
    def __init__(self, orchestrator, command_tracker: CommandTracker = None):
        super().__init__(
            name="maven",
            description="Execute Maven commands for building and testing Java projects. "
                       "IMPORTANT: Set working_directory to the folder containing pom.xml. "
                       "For multi-module projects with test failures, use fail_at_end=True to test all modules. "
                       "Common commands: compile, test, package, install. "
                       "Automatically installs Maven if not present."
        )
        self.orchestrator = orchestrator
        self.command_tracker = command_tracker
        self.output_storage = None  # Will be initialized when needed
    
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
        fail_at_end: bool = False,
        ignore_test_failures: bool = False,
        **kwargs  # Accept any additional parameters
    ) -> ToolResult:
        """
        Execute Maven commands with comprehensive error handling.

        Args:
            command: Main Maven phase/goal to execute. Common values:
                    - 'compile' - Compile source code
                    - 'test' - Run unit tests (stops at first module failure by default)
                    - 'package' - Create JAR/WAR files
                    - 'install' - Install to local repository
                    - 'clean' - Clean build artifacts
                    Can also be compound like 'clean compile' or 'clean test'

            working_directory: REQUIRED - Path to directory containing pom.xml
                             Example: '/workspace/struts' for the Struts project
                             Default: '/workspace' (will auto-search for pom.xml)

            fail_at_end: IMPORTANT for multi-module projects!
                        Set to True when running tests to test ALL modules even if some fail.
                        Without this, Maven stops at the first module with test failures.
                        Example: maven(command='test', fail_at_end=True)

            properties: Maven properties as comma-separated key=value pairs.
                       Examples:
                       - 'skipTests=true' - Skip test execution
                       - 'maven.test.skip=true' - Skip test compilation and execution
                       - 'maven.compiler.source=17,maven.compiler.target=17' - Set Java version

            goals: Additional goals to append after the main command (rarely needed)
                  Example: goals='dependency:tree' to add dependency analysis

            profiles: Activate Maven profiles defined in pom.xml
                     Example: 'production' or 'dev,test' for multiple profiles

            raw_output: Set to True to get complete Maven output for debugging
                       Useful when build fails and you need to see all details

            pom_file: Path to specific pom.xml if not in working_directory (rarely needed)
                     Example: '/workspace/project/custom-pom.xml'

            ignore_test_failures: Continue build even if tests fail (alternative to fail_at_end)
                                 Sets maven.test.failure.ignore=true property

            timeout: Maximum seconds to wait for command completion (default: 300)
        """
        
        # Deterministic working directory fallback (do not override user intent unless certain)
        try:
            if working_directory in (None, "/workspace") and self.orchestrator:
                project_name = getattr(self.orchestrator, 'project_name', None)
                # 1) Prefer /workspace/<project_name> if it has pom.xml
                if project_name:
                    probe_dir = f"/workspace/{project_name}"
                    probe_cmd = f"test -f {probe_dir}/pom.xml && echo EXISTS || echo MISSING"
                    probe_res = self.orchestrator.execute_command(probe_cmd)
                    if probe_res.get("exit_code") == 0 and 'EXISTS' in (probe_res.get('output') or ''):
                        if working_directory != probe_dir:
                            logger.info(f"🔧 Auto-selected project directory for Maven: {probe_dir}")
                            working_directory = probe_dir
                # 2) If still /workspace, detect single candidate pom up to depth 2
                if working_directory == "/workspace":
                    find_cmd = "find /workspace -maxdepth 2 -type f -name pom.xml 2>/dev/null | head -3"
                    find_res = self.orchestrator.execute_command(find_cmd)
                    if find_res.get("exit_code") == 0:
                        candidates = [p.strip() for p in (find_res.get('output') or '').split('\n') if p.strip()]
                        if len(candidates) == 1:
                            import os
                            cand_dir = os.path.dirname(candidates[0])
                            logger.info(f"🔧 Auto-selected Maven directory by single pom.xml candidate: {cand_dir}")
                            working_directory = cand_dir
                        elif len(candidates) > 1 and project_name:
                            preferred = f"/workspace/{project_name}/pom.xml"
                            if preferred in candidates:
                                import os
                                cand_dir = os.path.dirname(preferred)
                                logger.info(f"🔧 Auto-selected Maven directory by preferred project: {cand_dir}")
                                working_directory = cand_dir
        except Exception as _e:
            logger.debug(f"Working directory fallback skipped: {_e}")

        # Check if Maven is installed, install if not
        if not self._is_maven_installed():
            install_result = self._install_maven()
            if not install_result.success:
                return install_result
        
        # Handle ignore_test_failures by adding to properties
        if ignore_test_failures:
            if properties:
                properties += ",maven.test.failure.ignore=true"
            else:
                properties = "maven.test.failure.ignore=true"

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
        maven_cmd = self._build_maven_command(command, goals, profiles, properties, pom_file, fail_at_end)

        # Check if this is a multi-module project running tests without fail handling
        if command == "test" and self._is_multi_module_project(working_directory):
            if not fail_at_end and not ignore_test_failures:
                logger.warning("⚠️ Multi-module project detected! Maven will STOP at first module with test failures.")
                logger.info("💡 To test ALL modules: maven(command='test', fail_at_end=True, working_directory='{}')".
                          format(working_directory))

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
            
            # Store full output if large
            full_output = result["output"]
            ref_id = None
            if len(full_output) > 800:
                if not self.output_storage:
                    contexts_dir = Path("/workspace/.setup_agent/contexts")
                    self.output_storage = OutputStorageManager(contexts_dir, self.orchestrator)
                
                ref_id = self.output_storage.store_output(
                    task_id=f"maven_{working_directory.replace('/', '_')}",
                    tool_name="maven",
                    output=full_output,
                    metadata={"command": maven_cmd, "exit_code": result["exit_code"]}
                )
                logger.debug(f"Stored Maven output with ref_id: {ref_id}")
            
            # Track command for fact-based validation
            if self.command_tracker:
                # Determine if this is a build or test command
                is_test_command = any(test_word in command.lower() for test_word in ["test", "verify"])
                is_build_command = any(build_word in command.lower() for build_word in ["compile", "package", "install"])
                
                if is_test_command:
                    self.command_tracker.track_test_command(
                        command=maven_cmd,
                        tool="maven",
                        working_dir=working_directory,
                        exit_code=result["exit_code"],
                        output=result["output"]
                    )
                elif is_build_command:
                    self.command_tracker.track_build_command(
                        command=maven_cmd,
                        tool="maven",
                        working_dir=working_directory,
                        exit_code=result["exit_code"],
                        output=result["output"]
                    )
                logger.debug(f"Tracked Maven command: {maven_cmd[:100]}...")
            
            if raw_output:
                return ToolResult(
                    success=analysis["build_success"],  # Use analyzed build success, not exit code
                    output=result["output"],
                    raw_output=result["output"],
                    metadata={
                        "command": maven_cmd,
                        "exit_code": result["exit_code"],
                        "analysis": analysis
                    }
                )
            
            # Use analysis result to determine success, not just exit code
            if analysis["build_success"]:
                # Validate build artifacts for compile/package/install commands using container-based validation
                validation_result = None
                if any(goal in command for goal in ["compile", "package", "install"]) and working_directory:
                    try:
                        # Use container-based artifact validation instead of host Path.exists()
                        validation_result = self._validate_build_artifacts_in_container(working_directory, command)
                        logger.debug(f"Build validation: {validation_result}")
                        
                        # Add validation info to analysis
                        analysis["artifacts_validated"] = validation_result.get("artifacts_exist", False)
                        analysis["found_artifacts"] = validation_result.get("found_artifacts", [])
                        
                        # If artifacts don't exist despite "success", it's actually a failure
                        if not validation_result.get("artifacts_exist") and "compile" in command:
                            logger.warning("Build claimed success but no artifacts found!")
                            analysis["build_success"] = False
                            analysis["validation_error"] = "Build artifacts not found despite BUILD SUCCESS marker"
                            analysis["missing_artifacts"] = validation_result.get("missing_artifacts", [])
                            return self._handle_maven_error(
                                result["output"], 
                                result["exit_code"], 
                                maven_cmd, 
                                analysis
                            )
                    except Exception as e:
                        logger.warning(f"Could not validate build artifacts: {e}")
                
                return ToolResult(
                    success=True,
                    output=self._format_success_output_enhanced(analysis, ref_id),
                    raw_output=result["output"],
                    metadata={
                        "command": maven_cmd,
                        "exit_code": result["exit_code"],
                        "analysis": analysis,
                        "validation": validation_result,
                        "output_ref_id": ref_id
                    }
                )
            else:
                # Build failed - use error handler even if exit code was 0
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
    
    def _build_maven_command(self, command: str, goals: str, profiles: str, properties: str, pom_file: str = None, fail_at_end: bool = False) -> str:
        """Build the complete Maven command."""
        cmd_parts = ["mvn"]

        # Add fail-at-end flag for multi-module projects
        if fail_at_end:
            cmd_parts.append("--fail-at-end")
        
        # Add profiles
        if profiles:
            cmd_parts.append(f"-P{profiles}")
        
        # Add properties
        if properties:
            # Handle both string and list types for properties
            if isinstance(properties, list):
                for prop in properties:
                    # If it starts with -, add it directly (like -B)
                    if prop.startswith("-"):
                        cmd_parts.append(prop)
                    else:
                        cmd_parts.append(f"-D{prop.strip()}")
            else:
                for prop in properties.split(","):
                    cmd_parts.append(f"-D{prop.strip()}")
        
        # Add pom file if specified
        if pom_file:
            cmd_parts.extend(["-f", pom_file])
        
        # Add command and goals
        # Handle both string and list types for command
        if isinstance(command, list):
            command = " ".join(command)
        
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

    def _is_multi_module_project(self, working_directory: str) -> bool:
        """Check if this is a multi-module Maven project."""
        if not self.orchestrator:
            return False

        try:
            # Check for <modules> tag in the root pom.xml
            check_cmd = f"grep -q '<modules>' {working_directory}/pom.xml 2>/dev/null && echo 'HAS_MODULES' || echo 'NO_MODULES'"
            result = self.orchestrator.execute_command(check_cmd)
            return result.get("success", False) and "HAS_MODULES" in result.get("output", "")
        except Exception:
            return False

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
                # Try to find pom.xml in subdirectories and parent directories
                find_cmd = f"find {working_directory} -name pom.xml -type f 2>/dev/null | head -10"
                find_result = self.orchestrator.execute_command(find_cmd)
                found_poms = [p.strip() for p in find_result.get("output", "").split("\n") if p.strip()]
                
                # For multi-module projects like Struts, prioritize root pom.xml
                root_pom = None
                for pom in found_poms:
                    # Check if this is likely a root pom.xml (contains <modules> or is shortest path)
                    if pom.count('/') < 4:  # Likely root level
                        check_modules_cmd = f"grep -q '<modules>' {pom} 2>/dev/null && echo 'HAS_MODULES' || echo 'NO_MODULES'"
                        modules_result = self.orchestrator.execute_command(check_modules_cmd)
                        if "HAS_MODULES" in modules_result.get("output", ""):
                            root_pom = pom
                            break
                
                # If we found a root pom, suggest it first
                if root_pom:
                    found_poms = [root_pom] + [p for p in found_poms if p != root_pom]
                
                return {
                    "exists": False,
                    "searched_path": pom_path,
                    "found_alternatives": found_poms,
                    "working_directory": working_directory,
                    "suggested_root": root_pom
                }
            
            return {"exists": True, "path": pom_path}
            
        except Exception as e:
            return {"exists": False, "error": str(e)}
    
    def _handle_missing_pom(self, validation_result: dict, working_directory: str) -> ToolResult:
        """Handle the case when pom.xml is not found."""
        suggestions = [
            f"📍 Current directory: {working_directory}",
            "⚠️ This directory doesn't contain a pom.xml file"
        ]

        # If we found alternative pom.xml files, suggest them
        if validation_result.get("found_alternatives"):
            suggestions.append("\n🔍 Found pom.xml in these locations:")

            # Highlight root pom.xml if found
            suggested_root = validation_result.get("suggested_root")
            if suggested_root:
                root_dir = suggested_root.replace("/pom.xml", "")
                suggestions.append(f"\n🎯 RECOMMENDED: maven(command='...', working_directory='{root_dir}')")
                if "<modules>" in suggested_root:
                    suggestions.append("   ^ This is the root of a multi-module project")

            # Show other alternatives
            for pom_path in validation_result["found_alternatives"][:3]:
                if pom_path != suggested_root:  # Don't duplicate the root pom
                    pom_dir = pom_path.replace("/pom.xml", "")
                    suggestions.append(f"\n• Alternative: maven(command='...', working_directory='{pom_dir}')")
        else:
            suggestions.extend([
                "\n🔍 To find pom.xml files:",
                "bash(command='find /workspace -name pom.xml')",
                "\n📂 To list current directory:",
                "bash(command='ls -la', working_directory='/workspace')"
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
        # CRITICAL: Check for BUILD SUCCESS/FAILURE in output, not just exit code
        # Maven can return 0 even when build fails in some scenarios
        has_build_success = "BUILD SUCCESS" in output or "[INFO] BUILD SUCCESS" in output
        has_build_failure = "BUILD FAILURE" in output or "[ERROR] BUILD FAILURE" in output
        
        # Determine actual build success
        if has_build_failure:
            build_success = False
        elif has_build_success:
            build_success = True
        else:
            # Fallback to exit code if no explicit markers found
            build_success = exit_code == 0
            
        analysis = {
            "build_success": build_success,
            "has_build_success_marker": has_build_success,
            "has_build_failure_marker": has_build_failure,
            "exit_code": exit_code,
            "phases_executed": [],
            "tests_run": None,
            "compilation_errors": [],
            "dependency_issues": [],
            "warnings": [],
            "build_time": None,
            "artifacts_created": [],
            "java_version_error": None,  # Added for Maven Enforcer detection
            "enforcer_error": None,  # Store the full enforcer error message
            "pom_parse_error": None,  # Added for POM parsing error detection
            "error_type": None  # Track specific error type
        }
        
        lines = output.split('\n')

        # Check for POM parsing errors
        if "Non-parseable POM" in output and "Unrecognised tag" in output:
            import re
            pom_error_match = re.search(r'Non-parseable POM ([^:]+): Unrecognised tag: \'([^\']+)\'.+@(\d+):(\d+)', output)
            if pom_error_match:
                analysis["pom_parse_error"] = {
                    "file": pom_error_match.group(1),
                    "tag": pom_error_match.group(2),
                    "line": int(pom_error_match.group(3)),
                    "column": int(pom_error_match.group(4))
                }
                analysis["error_type"] = "POM_PARSE_ERROR"
                analysis["build_success"] = False  # Override build success for POM errors

        # Check for Maven Enforcer Java version errors
        import re
        enforcer_pattern = r"Detected JDK Version: ([\d\.]+).*is not in the allowed range \[([\d\.]+),\)"
        for i, line in enumerate(lines):
            match = re.search(enforcer_pattern, line)
            if match:
                current_version = match.group(1)
                required_version = match.group(2)
                
                # Normalize versions (1.8 -> 8)
                if current_version.startswith("1."):
                    current_version = current_version[2:]
                if required_version.startswith("1."):
                    required_version = required_version[2:]
                
                analysis["java_version_error"] = {
                    "current": current_version,
                    "required": required_version,
                    "error_type": "maven_enforcer"
                }
                analysis["enforcer_error"] = line
                logger.info(f"Detected Maven Enforcer Java version error: Current {current_version}, Required {required_version}")
                break
        
        # Also check for simpler Java version error messages
        if not analysis["java_version_error"]:
            for line in lines:
                if "Java" in line and "required" in line.lower():
                    # Try to extract version numbers
                    version_match = re.search(r"Java (\d+).*required", line, re.IGNORECASE)
                    if version_match:
                        analysis["java_version_error"] = {
                            "current": "unknown",
                            "required": version_match.group(1),
                            "error_type": "generic"
                        }
                        logger.info(f"Detected generic Java version requirement: {version_match.group(1)}")
                        break
        
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
    
    def _format_success_output_enhanced(self, analysis: Dict[str, Any], ref_id: Optional[str] = None) -> str:
        """Format with essential validation data always visible."""
        output = "✅ Maven build completed\n\n"
        
        # ALWAYS show what phases executed (critical for validation)
        output += "📍 Phases executed: "
        if analysis["phases_executed"]:
            phases = list(set(analysis["phases_executed"]))  # Remove duplicates
            output += ", ".join(phases[:5])
            if len(phases) > 5:
                output += f" (+{len(phases)-5} more)"
        else:
            output += "⚠️ NONE DETECTED (possible parsing issue)"
        
        # ALWAYS show test execution status if test/verify phase ran
        test_phases = ["test", "verify", "surefire", "failsafe"]
        phases_lower = [p.lower() for p in analysis.get("phases_executed", [])]
        if any(phase in phases_lower for phase in test_phases):
            output += "\n📊 Test Execution: "
            if analysis["tests_run"]:
                tests = analysis["tests_run"]
                output += f"{tests['total']} tests run, {tests['failures']} failures, {tests['errors']} errors"
                if tests['failures'] > 0 or tests['errors'] > 0:
                    output += " ❌"
                else:
                    output += " ✅"
            else:
                output += "⚠️ Test phase ran but no results captured (check target/surefire-reports/)"
        
        # Show artifacts if created
        if analysis["artifacts_created"]:
            output += f"\n📦 Artifacts: {len(analysis['artifacts_created'])} created"
        
        # Show compilation status for compile phase
        if "compile" in phases_lower:
            if analysis.get("compilation_errors"):
                output += f"\n❌ Compilation: {len(analysis['compilation_errors'])} errors"
            else:
                output += "\n✅ Compilation: successful"
        
        # Reference to full output
        if ref_id:
            output += f"\n\n📄 Full output reference: {ref_id}"
            output += f"\n💡 Use: output_search(action='retrieve', ref_id='{ref_id}') for complete log"
        
        # Warnings summary
        if analysis["warnings"]:
            output += f"\n⚠️ {len(analysis['warnings'])} warnings (see full output for details)"
        
        return output
    
    def _handle_maven_error(self, output: str, exit_code: int, command: str, analysis: Dict[str, Any]) -> ToolResult:
        """Enhanced error handling with specific suggestions based on error type."""
        """Handle Maven build errors with detailed analysis."""
        
        error_suggestions = []
        documentation_links = []
        error_code = "MAVEN_BUILD_ERROR"
        
        # Analyze specific error types
        if analysis.get("java_version_error"):
            # Java version mismatch detected
            java_error = analysis["java_version_error"]
            error_code = "JAVA_VERSION_MISMATCH"
            current = java_error.get("current", "unknown")
            required = java_error.get("required", "unknown")
            
            error_suggestions.extend([
                f"Java version mismatch: Current version is {current}, but {required} is required",
                f"Install Java {required} using: system(action='install_java', java_version='{required}')",
                f"Or manually: bash(command='apt-get update && apt-get install -y openjdk-{required}-jdk')",
                f"After installation, retry the Maven command"
            ])
            
            if java_error.get("error_type") == "maven_enforcer":
                error_suggestions.insert(1, "This requirement is enforced by Maven Enforcer plugin")
                documentation_links.append("https://maven.apache.org/enforcer/maven-enforcer-plugin/")
        
        if analysis.get("validation_error"):
            error_code = "BUILD_VALIDATION_ERROR"
            error_suggestions.extend([
                "Build claims success but artifacts are missing",
                "Check if the build actually completed",
                "Look for hidden errors in build output with raw_output=true",
                "Try running 'maven clean compile' to force a full rebuild",
                f"Expected artifacts: {', '.join(analysis.get('missing_artifacts', []))}"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/compile.html")
        
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
                "List current directory: bash(command='ls -la', working_directory='/workspace')"
            ])
            documentation_links.append("https://maven.apache.org/guides/getting-started/maven-in-five-minutes.html")

        # Check for POM parsing errors
        if "Non-parseable POM" in output:
            error_code = "POM_PARSE_ERROR"
            error_suggestions.extend([
                "POM file has XML syntax errors - check the error message for the specific line and tag",
                "Use bash to examine the problematic line in the POM file",
                "Common issues: orphaned tags, missing closing tags, tags outside proper parent elements",
                "Try: bash(command='xmllint --noout /path/to/pom.xml') to validate XML structure",
                "If unfixable, exclude the module: maven(command='test', properties='pl=!module-name')"
            ])
            documentation_links.append("https://maven.apache.org/pom.html#Quick_Overview")

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
            if analysis and analysis.get("java_version_error"):
                java_error = analysis["java_version_error"]
                if java_error.get("required"):
                    required_version = str(java_error["required"])
            
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
        elif error_code == "POM_PARSE_ERROR":
            # Get POM parsing error details
            pom_error = analysis.get("pom_parse_error", {})
            pom_file = pom_error.get("file", "pom.xml")
            tag = pom_error.get("tag", "unknown")
            line = pom_error.get("line", 0)

            recovery_actions.extend([
                {"action": "examine_pom", "command": f"bash(command='cat {pom_file} | head -n {line + 5} | tail -n 10')"},
                {"action": "validate_xml", "command": f"bash(command='xmllint --noout {pom_file} 2>&1 || echo \"XML validation failed\"')"},
                {"action": "find_orphaned_tags", "command": f"bash(command='grep -n \"<{tag}>\" {pom_file}')"},
                {"action": "fix_orphaned_tag", "description": f"Remove orphaned <{tag}> tag at line {line} that's outside proper XML structure"},
                {"action": "backup_and_fix", "command": f"bash(command='cp {pom_file} {pom_file}.backup && sed -i \"{line}d\" {pom_file}')"},
                {"action": "check_parent_pom", "command": "bash(command='if [ -f ../pom.xml ]; then grep -A 5 -B 5 \"<modules>\" ../pom.xml; fi')"},
                {"action": "skip_module", "description": f"If POM cannot be fixed, consider excluding this module from parent POM (last resort)"}
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
        elif error_code == "POM_PARSE_ERROR":
            diagnostics.extend([
                "bash(command='find /workspace -name pom.xml -type f | xargs -I {} xmllint --noout {} 2>&1')",
                "maven(command='validate', raw_output=true)",
                "bash(command='grep -r \"<groupId>\" --include=\"pom.xml\" /workspace | head -20')"
            ])

        return diagnostics

    def _validate_build_artifacts_in_container(self, working_directory: str, command: str) -> Dict[str, Any]:
        """
        Validate build artifacts exist in container using find commands.
        This replaces BuildAnalyzer.validate_build_artifacts to avoid host Path.exists() issues.
        
        Args:
            working_directory: Maven working directory
            command: Maven command executed
            
        Returns:
            Dictionary with validation results
        """
        validation = {
            "artifacts_exist": False,
            "missing_artifacts": [],
            "found_artifacts": [],
            "validation_performed": True
        }
        
        if not self.orchestrator:
            validation["validation_performed"] = False
            return validation
        
        try:
            # Check for .class files in target/classes (primary indicator for compile)
            class_check_cmd = f"find {working_directory} -path '*/target/classes/*.class' -type f -print -quit"
            class_result = self.orchestrator.execute_command(class_check_cmd)
            
            if class_result.get("exit_code") == 0 and class_result.get("output", "").strip():
                validation["found_artifacts"].append("target/classes/*.class")
                validation["artifacts_exist"] = True
                logger.debug(f"✅ Found .class files in {working_directory}/target/classes")
            else:
                validation["missing_artifacts"].append("target/classes/*.class")
                logger.debug(f"❌ No .class files found in {working_directory}/target/classes")
            
            # Check for JAR files in target/ (for package/install commands)
            if any(goal in command for goal in ["package", "install"]):
                jar_check_cmd = f"find {working_directory} -path '*/target/*.jar' -type f -print -quit"
                jar_result = self.orchestrator.execute_command(jar_check_cmd)
                
                if jar_result.get("exit_code") == 0 and jar_result.get("output", "").strip():
                    validation["found_artifacts"].append("target/*.jar")
                    validation["artifacts_exist"] = True
                    logger.debug(f"✅ Found .jar files in {working_directory}/target")
                else:
                    validation["missing_artifacts"].append("target/*.jar")
                    logger.debug(f"❌ No .jar files found in {working_directory}/target")
            
            # For multi-module projects, check if any module has artifacts
            if not validation["artifacts_exist"]:
                # Check for any target/classes directories in subdirectories
                multi_check_cmd = f"find {working_directory} -path '*/target/classes' -type d -print -quit"
                multi_result = self.orchestrator.execute_command(multi_check_cmd)
                
                if multi_result.get("exit_code") == 0 and multi_result.get("output", "").strip():
                    # Found target/classes dirs, now check for actual .class files
                    multi_class_cmd = f"find {working_directory} -path '*/target/classes/*.class' -type f -print -quit"
                    multi_class_result = self.orchestrator.execute_command(multi_class_cmd)
                    
                    if multi_class_result.get("exit_code") == 0 and multi_class_result.get("output", "").strip():
                        validation["found_artifacts"].append("multi-module target/classes/*.class")
                        validation["artifacts_exist"] = True
                        logger.debug(f"✅ Found .class files in multi-module project")
            
            logger.debug(f"Container artifact validation: {validation['artifacts_exist']} "
                        f"(found: {len(validation['found_artifacts'])}, missing: {len(validation['missing_artifacts'])})")
            
        except Exception as e:
            logger.error(f"Container artifact validation failed: {e}")
            validation["validation_performed"] = False
            validation["error"] = str(e)
        
        return validation

    def get_usage_example(self) -> str:
        """Get usage examples for Maven tool."""
        return """
Maven Tool Usage Examples:

🎯 MOST COMMON USAGE:
• maven(command="compile", working_directory="/workspace/myproject")
• maven(command="test", working_directory="/workspace/myproject")
• maven(command="clean install", working_directory="/workspace/myproject")

⚠️ MULTI-MODULE PROJECTS (like Struts, Tika):
• maven(command="test", working_directory="/workspace/struts", fail_at_end=True)
  # Without fail_at_end=True, Maven stops at first module with test failures!
  # With it, all 2,711 tests run instead of just 326

🔧 COMMON SCENARIOS:
• Skip tests: maven(command="install", properties="skipTests=true")
• Debug failures: maven(command="test", raw_output=True)
• Clean build: maven(command="clean compile")
• With profiles: maven(command="package", profiles="production")

💡 PARAMETER TIPS:
• working_directory: ALWAYS set to folder containing pom.xml
• fail_at_end: ALWAYS use True for multi-module project tests
• command vs goals: Use 'command' for main phases, 'goals' only for extras
• raw_output: Use True when debugging build failures
"""
    
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Maven phase to execute: 'compile', 'test', 'package', 'install', 'clean', or compound like 'clean test'",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Path to directory containing pom.xml (e.g., '/workspace/struts'). Will auto-search if not specified.",
                    "default": "/workspace",
                },
                "fail_at_end": {
                    "type": "boolean",
                    "description": "IMPORTANT for multi-module projects: Set to True to test ALL modules even if some fail. Without this, Maven stops at first failure.",
                    "default": False,
                },
                "properties": {
                    "type": "string",
                    "description": "Maven properties as 'key=value,key2=value2'. Common: 'skipTests=true' to skip tests.",
                    "default": None,
                },
                "raw_output": {
                    "type": "boolean",
                    "description": "Return complete Maven output for debugging failed builds",
                    "default": False,
                },
                "goals": {
                    "type": "string",
                    "description": "Additional goals to append (rarely needed, use 'command' for main goals)",
                    "default": None,
                },
                "profiles": {
                    "type": "string",
                    "description": "Activate Maven profiles from pom.xml (e.g., 'production' or 'dev,test')",
                    "default": None,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (increase for slow builds)",
                    "default": 300,
                },
                "pom_file": {
                    "type": "string",
                    "description": "Path to specific pom.xml if not in working_directory (rarely needed)",
                    "default": None,
                },
                "ignore_test_failures": {
                    "type": "boolean",
                    "description": "Continue build despite test failures (alternative to fail_at_end)",
                    "default": False,
                },
            },
            "required": ["command"],
        }