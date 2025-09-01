"""
Physical Validator for Fact-Based Build and Test Validation

This module validates build and test status based on physical evidence
rather than log inference, ensuring accurate status determination.
"""

import os
import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


class PhysicalValidator:
    """
    Validates build/test status based on physical evidence.
    
    This class checks actual files and re-executes commands to determine
    true status, eliminating inference-based errors.
    """
    
    def __init__(self, docker_orchestrator=None, project_path: str = "/workspace"):
        """
        Initialize physical validator.
        
        Args:
            docker_orchestrator: Docker orchestrator for command execution
            project_path: Base path of the project in container
        """
        self.docker_orchestrator = docker_orchestrator
        self.project_path = project_path
        
        # Cache for validation results
        self.validation_cache = {}
        self.last_validation = None
        
        logger.info(f"PhysicalValidator initialized for project at: {project_path}")
    
    def validate_build_artifacts(self, project_name: str = None) -> Dict[str, any]:
        """
        Check three levels of build evidence:
        1. .class files exist and are recent
        2. JAR files in target/build directories  
        3. Compilation timestamps vs source timestamps
        
        Args:
            project_name: Name of the project (for path construction)
            
        Returns:
            Dictionary with validation results
        """
        if not self.docker_orchestrator:
            return {"valid": False, "error": "No docker orchestrator"}
        
        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path
        
        validation_result = {
            "valid": False,
            "class_files": 0,
            "jar_files": 0,
            "recent_compilation": False,
            "missing_classes": [],
            "evidence": []
        }
        
        # 1. Check for .class files
        class_check = self._check_class_files(project_dir)
        validation_result["class_files"] = class_check["count"]
        validation_result["class_file_paths"] = class_check["paths"][:10]  # Sample paths
        
        # 2. Check for JAR files
        jar_check = self._check_jar_files(project_dir)
        validation_result["jar_files"] = jar_check["count"]
        validation_result["jar_file_paths"] = jar_check["paths"]
        
        # 3. Check compilation recency
        recency_check = self._check_compilation_recency(project_dir)
        validation_result["recent_compilation"] = recency_check["recent"]
        validation_result["newest_class_time"] = recency_check.get("newest_class_time")
        
        # 4. Find missing classes (Java files without corresponding class files)
        missing = self.validate_missing_classes(project_dir)
        validation_result["missing_classes"] = missing
        
        # Determine overall validity
        validation_result["valid"] = (
            validation_result["class_files"] > 0 and
            len(validation_result["missing_classes"]) == 0 and
            validation_result["recent_compilation"]
        )
        
        # Build evidence summary
        if validation_result["valid"]:
            validation_result["evidence"].append(f"✅ Found {validation_result['class_files']} .class files")
            validation_result["evidence"].append(f"✅ Found {validation_result['jar_files']} JAR files")
            validation_result["evidence"].append("✅ Recent compilation detected")
        else:
            if validation_result["class_files"] == 0:
                validation_result["evidence"].append("❌ No .class files found")
            if len(validation_result["missing_classes"]) > 0:
                validation_result["evidence"].append(f"❌ {len(validation_result['missing_classes'])} Java files not compiled")
            if not validation_result["recent_compilation"]:
                validation_result["evidence"].append("❌ No recent compilation detected")
        
        self.last_validation = validation_result
        return validation_result
    
    def _check_class_files(self, project_dir: str) -> Dict[str, any]:
        """Check for .class files in the project."""
        try:
            # Count .class files
            count_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
            count_result = self.docker_orchestrator.execute_command(count_cmd)
            count = int(count_result.get("output", "0").strip())
            
            # Get sample paths
            paths_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | head -10"
            paths_result = self.docker_orchestrator.execute_command(paths_cmd)
            paths = [p.strip() for p in paths_result.get("output", "").split("\n") if p.strip()]
            
            return {"count": count, "paths": paths}
        except Exception as e:
            logger.error(f"Failed to check class files: {e}")
            return {"count": 0, "paths": []}
    
    def _check_jar_files(self, project_dir: str) -> Dict[str, any]:
        """Check for JAR files in target/build directories."""
        try:
            # Check Maven target and Gradle build directories
            cmd = f"find {project_dir} \\( -path '*/target/*.jar' -o -path '*/build/*.jar' \\) -type f 2>/dev/null"
            result = self.docker_orchestrator.execute_command(cmd)
            paths = [p.strip() for p in result.get("output", "").split("\n") if p.strip()]
            
            return {"count": len(paths), "paths": paths}
        except Exception as e:
            logger.error(f"Failed to check JAR files: {e}")
            return {"count": 0, "paths": []}
    
    def _check_compilation_recency(self, project_dir: str) -> Dict[str, any]:
        """Check if compilation is recent (within last hour)."""
        try:
            # Find newest .class file modification time
            cmd = f"find {project_dir} -name '*.class' -type f -exec stat -c '%Y' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result = self.docker_orchestrator.execute_command(cmd)
            
            if result.get("output", "").strip():
                newest_timestamp = int(result["output"].strip())
                current_timestamp = int(datetime.now().timestamp())
                
                # Check if compiled within last hour
                age_seconds = current_timestamp - newest_timestamp
                recent = age_seconds < 3600  # 1 hour
                
                return {
                    "recent": recent,
                    "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                    "age_seconds": age_seconds
                }
            
            return {"recent": False, "error": "No class files found"}
        except Exception as e:
            logger.error(f"Failed to check compilation recency: {e}")
            return {"recent": False, "error": str(e)}
    
    def validate_missing_classes(self, project_dir: str) -> List[str]:
        """
        Find .java files without corresponding .class files.
        This indicates compilation failure.
        
        Args:
            project_dir: Project directory path
            
        Returns:
            List of Java files missing corresponding class files
        """
        try:
            # Get all Java source files
            java_cmd = f"find {project_dir}/src -name '*.java' -type f 2>/dev/null"
            java_result = self.docker_orchestrator.execute_command(java_cmd)
            java_files = [f.strip() for f in java_result.get("output", "").split("\n") if f.strip()]
            
            missing_classes = []
            
            for java_file in java_files[:100]:  # Check first 100 to avoid timeout
                # Extract expected class name from Java file path
                # e.g., /workspace/project/src/main/java/com/example/MyClass.java
                # -> com/example/MyClass.class
                
                # Get the relative path from src/main/java or src/test/java
                relative_path = None
                for src_pattern in ["/src/main/java/", "/src/test/java/"]:
                    if src_pattern in java_file:
                        relative_path = java_file.split(src_pattern)[1]
                        break
                
                if relative_path:
                    # Convert to class path
                    class_name = relative_path.replace(".java", ".class")
                    
                    # Check if corresponding class file exists
                    class_check_cmd = f"find {project_dir} -path '*/{class_name}' -type f 2>/dev/null | head -1"
                    class_result = self.docker_orchestrator.execute_command(class_check_cmd)
                    
                    if not class_result.get("output", "").strip():
                        missing_classes.append(java_file)
                        if len(missing_classes) >= 10:  # Limit to first 10 missing
                            break
            
            return missing_classes
        except Exception as e:
            logger.error(f"Failed to validate missing classes: {e}")
            return []
    
    def replay_last_build_command(self, command: str, working_dir: str = None) -> bool:
        """
        Re-run a build command and check exit code.
        Don't rely on logs - check actual result.
        
        Args:
            command: The build command to execute
            working_dir: Working directory (defaults to project path)
            
        Returns:
            True if build succeeds, False otherwise
        """
        if not self.docker_orchestrator:
            return False
        
        working_dir = working_dir or self.project_path
        
        logger.info(f"Replaying build command for validation: {command[:100]}...")
        
        try:
            # Execute the command
            full_command = f"cd {working_dir} && {command}"
            result = self.docker_orchestrator.execute_command(full_command, timeout=300000)  # 5 min timeout
            
            exit_code = result.get("exit_code", 1)
            output = result.get("output", "")
            
            # Check for explicit build success markers
            if "mvn" in command or "maven" in command.lower():
                build_success = "BUILD SUCCESS" in output and "BUILD FAILURE" not in output
            elif "gradle" in command:
                build_success = "BUILD SUCCESSFUL" in output and "BUILD FAILED" not in output
            else:
                # For other build systems, rely on exit code
                build_success = exit_code == 0
            
            logger.info(f"Build replay result: exit_code={exit_code}, success={build_success}")
            
            return build_success
            
        except Exception as e:
            logger.error(f"Failed to replay build command: {e}")
            return False
    
    def replay_all_test_commands(self, commands: List[str], working_dir: str = None) -> Dict[str, any]:
        """
        Re-run all test commands to get accurate counts.
        
        Args:
            commands: List of test commands to execute
            working_dir: Working directory (defaults to project path)
            
        Returns:
            Dictionary with aggregated test results
        """
        if not self.docker_orchestrator:
            return {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": "No orchestrator"}
        
        working_dir = working_dir or self.project_path
        
        total_stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        command_results = []
        
        for command in commands:
            logger.info(f"Replaying test command: {command[:100]}...")
            
            try:
                full_command = f"cd {working_dir} && {command}"
                result = self.docker_orchestrator.execute_command(full_command, timeout=600000)  # 10 min timeout
                
                output = result.get("output", "")
                exit_code = result.get("exit_code", 1)
                
                # Extract test statistics from output
                stats = self._extract_test_statistics(command, output)
                
                # Aggregate statistics
                total_stats["total"] += stats["total"]
                total_stats["passed"] += stats["passed"]
                total_stats["failed"] += stats["failed"]
                total_stats["skipped"] += stats["skipped"]
                
                command_results.append({
                    "command": command,
                    "exit_code": exit_code,
                    "stats": stats
                })
                
            except Exception as e:
                logger.error(f"Failed to replay test command: {e}")
                command_results.append({
                    "command": command,
                    "error": str(e)
                })
        
        return {
            "total": total_stats["total"],
            "passed": total_stats["passed"],
            "failed": total_stats["failed"],
            "skipped": total_stats["skipped"],
            "command_count": len(commands),
            "command_results": command_results
        }
    
    def _extract_test_statistics(self, command: str, output: str) -> Dict[str, int]:
        """
        Extract test statistics from command output.
        
        Args:
            command: The test command executed
            output: Command output
            
        Returns:
            Dictionary with test statistics
        """
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        
        if "mvn" in command or "maven" in command.lower():
            # Maven test output pattern
            pattern = r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)'
            matches = re.findall(pattern, output)
            
            for match in matches:
                total = int(match[0])
                failures = int(match[1])
                errors = int(match[2])
                skipped = int(match[3])
                
                stats["total"] += total
                stats["failed"] += failures + errors
                stats["skipped"] += skipped
                stats["passed"] += total - failures - errors - skipped
                
        elif "gradle" in command:
            # Gradle test output pattern
            pattern = r'(\d+)\s+tests?\s+completed,\s*(\d+)\s+failed'
            match = re.search(pattern, output)
            
            if match:
                total = int(match.group(1))
                failed = int(match.group(2))
                
                stats["total"] = total
                stats["failed"] = failed
                stats["passed"] = total - failed
        
        return stats
    
    def validate_project_completely(self, project_name: str, command_tracker=None) -> Dict[str, any]:
        """
        Complete fact-based validation of a project.
        
        Args:
            project_name: Name of the project
            command_tracker: CommandTracker instance with recorded commands
            
        Returns:
            Complete validation report
        """
        logger.info(f"Starting complete validation for project: {project_name}")
        
        validation_report = {
            "project": project_name,
            "timestamp": datetime.now().isoformat(),
            "build_validation": {},
            "test_validation": {},
            "artifact_validation": {},
            "overall_status": "unknown"
        }
        
        # 1. Validate build artifacts
        validation_report["artifact_validation"] = self.validate_build_artifacts(project_name)
        
        # 2. Replay build command if tracker available
        if command_tracker:
            last_build = command_tracker.get_last_build_command()
            if last_build:
                build_success = self.replay_last_build_command(
                    last_build["command"],
                    last_build.get("working_dir")
                )
                validation_report["build_validation"] = {
                    "command": last_build["command"],
                    "replay_success": build_success,
                    "original_result": last_build.get("build_success")
                }
        
        # 3. Replay test commands if build succeeded
        if validation_report["build_validation"].get("replay_success", False):
            if command_tracker:
                test_commands = [cmd["command"] for cmd in command_tracker.get_all_test_commands()]
                if test_commands:
                    validation_report["test_validation"] = self.replay_all_test_commands(test_commands)
        else:
            validation_report["test_validation"] = {
                "skipped": True,
                "reason": "Build failed, cannot run tests"
            }
        
        # 4. Determine overall status based on facts
        if validation_report["artifact_validation"]["valid"] and \
           validation_report["build_validation"].get("replay_success", False):
            if validation_report["test_validation"].get("failed", 0) == 0:
                validation_report["overall_status"] = "SUCCESS"
            else:
                validation_report["overall_status"] = "PARTIAL"
        else:
            validation_report["overall_status"] = "FAILED"
        
        logger.info(f"Validation complete for {project_name}: {validation_report['overall_status']}")
        
        return validation_report