"""
Shared utilities for build tools following DRY principle.

This module centralizes build detection logic to avoid code duplication
across maven_tool.py, gradle_tool.py, and docker_orch/orch.py.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from loguru import logger


class BuildAnalyzer:
    """Centralized build output analysis following DRY principle."""
    
    @staticmethod
    def detect_build_status(output: str, command: str = "") -> Dict[str, Any]:
        """
        Unified build status detection for all build tools.
        
        Args:
            output: The build tool output to analyze
            command: The command that was executed
            
        Returns:
            Dictionary with:
            - success: True/False/None (None if no markers found)
            - tool: Detected build tool name
            - markers_found: List of detected status markers
        """
        result = {
            "success": None,
            "tool": "unknown",
            "markers_found": []
        }
        
        # Normalize command and output for case-insensitive matching
        command_lower = command.lower()
        
        # Maven detection
        if "mvn" in command or "maven" in command_lower:
            result["tool"] = "maven"
            
            # Check for failure first (higher priority)
            if "BUILD FAILURE" in output or "[ERROR] BUILD FAILURE" in output:
                result["success"] = False
                result["markers_found"].append("BUILD FAILURE")
                logger.debug("Maven BUILD FAILURE detected in output")
            elif "BUILD SUCCESS" in output or "[INFO] BUILD SUCCESS" in output:
                result["success"] = True
                result["markers_found"].append("BUILD SUCCESS")
                logger.debug("Maven BUILD SUCCESS detected in output")
            
            # Additional Maven-specific failure patterns
            if "[ERROR] Failed to execute goal" in output:
                result["success"] = False
                result["markers_found"].append("Failed to execute goal")
        
        # Gradle detection
        elif "gradle" in command_lower or "./gradlew" in command:
            result["tool"] = "gradle"
            
            # Check for failure first
            if "BUILD FAILED" in output or "FAILURE: Build failed" in output:
                result["success"] = False
                result["markers_found"].append("BUILD FAILED")
                logger.debug("Gradle BUILD FAILED detected in output")
            elif "BUILD SUCCESSFUL" in output:
                result["success"] = True
                result["markers_found"].append("BUILD SUCCESSFUL")
                logger.debug("Gradle BUILD SUCCESSFUL detected in output")
            
            # Additional Gradle-specific patterns
            if "Execution failed for task" in output:
                result["success"] = False
                result["markers_found"].append("Execution failed for task")
        
        # NPM/Node.js detection
        elif "npm" in command_lower or "yarn" in command_lower or "pnpm" in command_lower:
            result["tool"] = "npm"
            
            # NPM error detection
            if "npm ERR!" in output or "ERR!" in output:
                result["success"] = False
                result["markers_found"].append("npm ERR!")
                logger.debug("NPM error detected in output")
            elif "npm notice" in output and "npm ERR!" not in output:
                # If we see notices but no errors, likely successful
                result["success"] = True
                result["markers_found"].append("npm completed without errors")
        
        # Python/pip detection
        elif "pip" in command_lower or "python" in command_lower or "pytest" in command_lower:
            result["tool"] = "python"
            
            if "ERROR:" in output or "FAILED" in output:
                result["success"] = False
                result["markers_found"].append("Python ERROR/FAILED")
            elif "Successfully installed" in output or "passed" in output.lower():
                result["success"] = True
                result["markers_found"].append("Python success")
        
        # Make detection
        elif "make" in command_lower:
            result["tool"] = "make"
            
            if "Error" in output or "*** [" in output:
                result["success"] = False
                result["markers_found"].append("Make error")
            elif "make: Nothing to be done" in output or not re.search(r'make.*Error', output):
                result["success"] = True
                result["markers_found"].append("Make completed")
        
        return result
    
    @staticmethod
    def extract_common_patterns(output: str) -> Dict[str, Any]:
        """
        Extract common patterns from build output that apply to all build tools.
        
        Args:
            output: The build output to analyze
            
        Returns:
            Dictionary with common build patterns
        """
        output_lower = output.lower()
        
        patterns = {
            # Compilation issues
            "has_compilation_error": any(pattern in output_lower for pattern in [
                "compilation error", "compile error", "cannot find symbol",
                "syntax error", "cannot resolve"
            ]),
            
            # Test issues
            "has_test_failure": any(pattern in output_lower for pattern in [
                "test failure", "tests failed", "test error",
                "failures: [1-9]", "errors: [1-9]"
            ]),
            
            # Dependency issues
            "has_dependency_error": any(pattern in output_lower for pattern in [
                "could not resolve dependencies",
                "dependency not found",
                "unable to resolve",
                "could not find artifact",
                "package not found"
            ]),
            
            # Warning detection
            "has_warnings": "warning" in output_lower or "warn" in output_lower,
            
            # Deprecation detection
            "has_deprecation": "deprecated" in output_lower,
            
            # Out of memory
            "has_memory_error": any(pattern in output_lower for pattern in [
                "out of memory", "heap space", "gc overhead"
            ]),
            
            # Network issues
            "has_network_error": any(pattern in output_lower for pattern in [
                "connection refused", "connection timeout",
                "unable to connect", "network unreachable"
            ]),
            
            # Permission issues
            "has_permission_error": any(pattern in output_lower for pattern in [
                "permission denied", "access denied", "unauthorized"
            ]),
            
            # Statistics
            "total_lines": len(output.split('\n')),
            "error_count": output_lower.count("error"),
            "warning_count": output_lower.count("warning")
        }
        
        return patterns
    
    @staticmethod
    def validate_build_artifacts(project_path: str, build_tool: str, orchestrator=None) -> Dict[str, Any]:
        """
        Validate that expected build artifacts exist after a build.
        
        Args:
            project_path: Root path of the project
            build_tool: The build tool used (maven, gradle, npm, etc.)
            orchestrator: Optional Docker orchestrator for container-based validation
            
        Returns:
            Dictionary with validation results
        """
        validation = {
            "artifacts_exist": False,
            "missing_artifacts": [],
            "found_artifacts": [],
            "validation_performed": True
        }
        
        # Use container-based validation if orchestrator is provided
        if orchestrator:
            return BuildAnalyzer._validate_artifacts_in_container(project_path, build_tool, orchestrator)
        
        # Fallback to host-based validation (may not work in container environments)
        project_path = Path(project_path)
        
        # Define expected artifacts for each build tool
        expected_artifacts = []
        
        if build_tool == "maven":
            expected_artifacts = [
                project_path / "target",
                project_path / "target" / "classes"
            ]
            # For multi-module projects, also check module targets
            pom_files = list(project_path.glob("*/pom.xml"))
            for pom in pom_files:
                module_target = pom.parent / "target"
                expected_artifacts.append(module_target)
                
        elif build_tool == "gradle":
            expected_artifacts = [
                project_path / "build",
                project_path / "build" / "classes"
            ]
            # Check for Gradle wrapper
            if (project_path / "gradlew").exists():
                validation["found_artifacts"].append("gradlew")
                
        elif build_tool == "npm":
            expected_artifacts = [
                project_path / "node_modules",
                project_path / "dist"  # Common output directory
            ]
            # Also check for build/out directories
            for dir_name in ["build", "out", "lib"]:
                if (project_path / dir_name).exists():
                    validation["found_artifacts"].append(dir_name)
                    
        elif build_tool == "python":
            expected_artifacts = [
                project_path / "__pycache__",
                project_path / ".pytest_cache",
                project_path / "dist",
                project_path / "build"
            ]
        else:
            validation["validation_performed"] = False
            return validation
        
        # Check for existence of expected artifacts
        for artifact_path in expected_artifacts:
            if artifact_path.exists():
                validation["found_artifacts"].append(str(artifact_path))
            else:
                validation["missing_artifacts"].append(str(artifact_path))
        
        # Determine overall success
        validation["artifacts_exist"] = len(validation["found_artifacts"]) > 0
        
        # Log validation results
        if validation["artifacts_exist"]:
            logger.debug(f"Build artifacts found for {build_tool}: {validation['found_artifacts']}")
        else:
            logger.warning(f"No build artifacts found for {build_tool}. Expected: {validation['missing_artifacts']}")
        
        return validation
    
    @staticmethod
    def _validate_artifacts_in_container(project_path: str, build_tool: str, orchestrator) -> Dict[str, Any]:
        """
        Container-based artifact validation using find commands.
        
        Args:
            project_path: Root path of the project in container
            build_tool: The build tool used
            orchestrator: Docker orchestrator for command execution
            
        Returns:
            Dictionary with validation results
        """
        validation = {
            "artifacts_exist": False,
            "missing_artifacts": [],
            "found_artifacts": [],
            "validation_performed": True
        }
        
        try:
            if build_tool == "maven":
                # Check for Maven artifacts
                checks = [
                    (f"find {project_path} -path '*/target/classes/*.class' -type f -print -quit", "target/classes/*.class"),
                    (f"find {project_path} -path '*/target/*.jar' -type f -print -quit", "target/*.jar")
                ]
            elif build_tool == "gradle":
                # Check for Gradle artifacts
                checks = [
                    (f"find {project_path} -path '*/build/classes/*/*.class' -type f -print -quit", "build/classes/*.class"),
                    (f"find {project_path} -path '*/build/libs/*.jar' -type f -print -quit", "build/libs/*.jar")
                ]
            elif build_tool == "npm":
                # Check for Node.js artifacts
                checks = [
                    (f"test -d {project_path}/node_modules && echo 'EXISTS'", "node_modules/"),
                    (f"test -d {project_path}/dist && echo 'EXISTS'", "dist/")
                ]
            else:
                # Generic checks
                checks = [
                    (f"find {project_path} -name '*.class' -type f -print -quit", "*.class"),
                    (f"find {project_path} -name '*.jar' -type f -print -quit", "*.jar")
                ]
            
            for check_cmd, artifact_name in checks:
                result = orchestrator.execute_command(check_cmd)
                if result.get("exit_code") == 0 and result.get("output", "").strip():
                    validation["found_artifacts"].append(artifact_name)
                    validation["artifacts_exist"] = True
                else:
                    validation["missing_artifacts"].append(artifact_name)
            
        except Exception as e:
            validation["validation_performed"] = False
            validation["error"] = str(e)
        
        return validation
    
    @staticmethod
    def extract_test_results(output: str, build_tool: str) -> Dict[str, Any]:
        """
        Extract test execution results from build output.
        
        Args:
            output: The build output containing test results
            build_tool: The build tool used
            
        Returns:
            Dictionary with test results
        """
        test_results = {
            "tests_run": False,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "success_rate": 0.0
        }
        
        if build_tool == "maven":
            # Maven Surefire pattern: Tests run: X, Failures: Y, Errors: Z, Skipped: W
            pattern = r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)'
            match = re.search(pattern, output)
            if match:
                test_results["tests_run"] = True
                test_results["total_tests"] = int(match.group(1))
                test_results["failed"] = int(match.group(2))
                test_results["errors"] = int(match.group(3))
                test_results["skipped"] = int(match.group(4))
                test_results["passed"] = test_results["total_tests"] - test_results["failed"] - test_results["errors"] - test_results["skipped"]
                
        elif build_tool == "gradle":
            # Gradle pattern: X tests completed, Y failed
            pattern = r'(\d+)\s+tests?\s+completed(?:,\s*(\d+)\s+failed)?'
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                test_results["tests_run"] = True
                test_results["total_tests"] = int(match.group(1))
                test_results["failed"] = int(match.group(2)) if match.group(2) else 0
                test_results["passed"] = test_results["total_tests"] - test_results["failed"]
                
        elif build_tool == "npm":
            # Jest pattern: Tests: X passed, Y total
            pattern = r'Tests?:\s*(\d+)\s+passed,\s*(\d+)\s+total'
            match = re.search(pattern, output)
            if match:
                test_results["tests_run"] = True
                test_results["passed"] = int(match.group(1))
                test_results["total_tests"] = int(match.group(2))
                test_results["failed"] = test_results["total_tests"] - test_results["passed"]
                
        elif build_tool == "python":
            # Pytest pattern: X passed, Y failed
            pattern = r'(\d+)\s+passed(?:,\s*(\d+)\s+failed)?'
            match = re.search(pattern, output)
            if match:
                test_results["tests_run"] = True
                test_results["passed"] = int(match.group(1))
                test_results["failed"] = int(match.group(2)) if match.group(2) else 0
                test_results["total_tests"] = test_results["passed"] + test_results["failed"]
        
        # Calculate success rate
        if test_results["total_tests"] > 0:
            test_results["success_rate"] = (test_results["passed"] / test_results["total_tests"]) * 100
        
        return test_results
    
    @staticmethod
    def suggest_fixes(patterns: Dict[str, Any], build_tool: str) -> List[str]:
        """
        Suggest fixes based on detected patterns.
        
        Args:
            patterns: Dictionary of detected patterns from extract_common_patterns
            build_tool: The build tool being used
            
        Returns:
            List of suggested fixes
        """
        suggestions = []
        
        if patterns.get("has_dependency_error"):
            if build_tool == "maven":
                suggestions.append("Run 'mvn dependency:resolve' to download missing dependencies")
                suggestions.append("Check your network connection and Maven repository settings")
            elif build_tool == "gradle":
                suggestions.append("Run 'gradle dependencies' to resolve missing dependencies")
                suggestions.append("Check your repositories configuration in build.gradle")
            elif build_tool == "npm":
                suggestions.append("Run 'npm install' to install missing packages")
                suggestions.append("Try 'npm cache clean --force' if dependencies are corrupted")
        
        if patterns.get("has_compilation_error"):
            suggestions.append("Check for syntax errors in your source files")
            suggestions.append("Ensure all required imports/dependencies are present")
            if build_tool == "maven":
                suggestions.append("Verify Java version compatibility with 'mvn -version'")
        
        if patterns.get("has_memory_error"):
            suggestions.append("Increase heap size with -Xmx flag (e.g., -Xmx2g)")
            if build_tool == "maven":
                suggestions.append("Set MAVEN_OPTS='-Xmx2g' environment variable")
            elif build_tool == "gradle":
                suggestions.append("Add 'org.gradle.jvmargs=-Xmx2g' to gradle.properties")
        
        if patterns.get("has_permission_error"):
            suggestions.append("Check file permissions in the project directory")
            suggestions.append("Ensure you have write access to the output directories")
        
        if patterns.get("has_network_error"):
            suggestions.append("Check your internet connection")
            suggestions.append("Verify proxy settings if behind a corporate firewall")
            suggestions.append("Try using a different repository mirror")
        
        return suggestions