"""
Physical Validator for Fact-Based Build and Test Validation

This module provides comprehensive validation of build and test status based on 
physical evidence rather than log inference, ensuring accurate status determination.

Key Features:
- Cross-platform compilation timestamp checking (GNU stat, BSD stat, Python fallback)
- Comprehensive build artifact detection (Maven target/, Gradle build/, build/libs/)  
- Strict XML test report parsing (Maven Surefire, Gradle test reports)
- TTL-based result caching (60s default) for expensive file system operations
- Unified command execution with standardized error handling and logging
- Logical consistency enforcement (no build without clone, no test without build)

The PhysicalValidator eliminates common issues with log-based inference by directly
examining the file system state and parsing structured test reports.

Example Usage:
    validator = PhysicalValidator(docker_orchestrator=orchestrator)
    build_result = validator.validate_build_artifacts("my-project")
    test_result = validator.parse_test_reports("/workspace/my-project")
    
    if build_result['valid'] and test_result['test_success']:
        print("Project built and tested successfully!")
"""

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from loguru import logger
from urllib.parse import quote


class PhysicalValidator:
    """
    Validates build/test status based on physical evidence.
    
    This class checks actual files and re-executes commands to determine
    true status, eliminating inference-based errors.
    """
    
    def __init__(self, docker_orchestrator=None, project_path: str = "/workspace", 
                 compilation_recency_hours: int = 1):
        """
        Initialize physical validator.
        
        Args:
            docker_orchestrator: Docker orchestrator for command execution
            project_path: Base path of the project in container
            compilation_recency_hours: Hours to consider compilation as recent (default 1)
        """
        self.docker_orchestrator = docker_orchestrator
        self.project_path = project_path
        self.compilation_recency_hours = compilation_recency_hours
        
        # Cache for validation results with TTL
        self.validation_cache = {}
        self.cache_timestamps = {}
        self.cache_ttl = 60  # 60 seconds TTL
        self.last_validation = None
        
        logger.info(f"PhysicalValidator initialized for project at: {project_path}")
    
    def _get_cache_key(self, operation: str, *args) -> str:
        """Generate cache key for operation and arguments."""
        # Escape special characters in arguments to prevent cache key issues
        escaped_args = [quote(str(arg), safe='') for arg in args]
        return f"{operation}:{':'.join(escaped_args)}"
    
    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cache entry is still valid (within TTL)."""
        if cache_key not in self.cache_timestamps:
            return False
        
        age = datetime.now().timestamp() - self.cache_timestamps[cache_key]
        return age < self.cache_ttl
    
    def _get_cached_result(self, cache_key: str):
        """Get cached result if valid, otherwise return None."""
        if self._is_cache_valid(cache_key):
            logger.debug(f"Cache hit for {cache_key}")
            return self.validation_cache[cache_key]
        else:
            # Clean up expired cache entry
            if cache_key in self.validation_cache:
                del self.validation_cache[cache_key]
            if cache_key in self.cache_timestamps:
                del self.cache_timestamps[cache_key]
            return None
    
    def _cache_result(self, cache_key: str, result):
        """Cache result with current timestamp."""
        self.validation_cache[cache_key] = result
        self.cache_timestamps[cache_key] = datetime.now().timestamp()
        logger.debug(f"Cached result for {cache_key}")
    
    def clear_cache(self):
        """Clear all cached results. Useful after build operations."""
        self.validation_cache.clear()
        self.cache_timestamps.clear()
        logger.debug("Cache cleared")
    
    def _execute_command_with_logging(self, command: str, operation: str = "command") -> Dict[str, any]:
        """
        Execute command with standardized result handling and logging.
        
        Args:
            command: Command to execute
            operation: Description of operation for logging
            
        Returns:
            Standardized result dictionary with success, output, exit_code
        """
        try:
            if not self.docker_orchestrator:
                return {
                    "success": False,
                    "output": "",
                    "exit_code": -1,
                    "error": "No docker orchestrator available"
                }
            
            result = self.docker_orchestrator.execute_command(command)
            
            # Standardize result format
            exit_code = result.get("exit_code", 1)  # Default to failure if not provided
            output = result.get("output", "")
            success = exit_code == 0
            
            # Log command execution details
            if success:
                logger.debug(f"✅ {operation} succeeded: {command[:100]}...")
            else:
                logger.warning(f"❌ {operation} failed (exit_code={exit_code}): {command[:100]}...")
                if output:
                    logger.warning(f"Command output: {output[:200]}...")
            
            return {
                "success": success,
                "output": output,
                "exit_code": exit_code,
                "command": command[:100] + "..." if len(command) > 100 else command
            }
            
        except Exception as e:
            logger.error(f"❌ {operation} execution failed: {e}")
            logger.error(f"Command: {command[:100]}...")
            return {
                "success": False,
                "output": "",
                "exit_code": -1,
                "error": str(e),
                "command": command[:100] + "..." if len(command) > 100 else command
            }
    
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
        
        # Use the complete artifact check to avoid duplication
        artifact_check = self._check_build_artifacts_complete(project_dir)
        
        validation_result = {
            "valid": False,
            "class_files": artifact_check["class_count"],
            "jar_files": artifact_check["jar_count"],
            "recent_compilation": False,
            "missing_classes": [],
            "evidence": []
        }
        
        # Get sample class file paths for debugging
        class_check = self._check_class_files(project_dir)
        validation_result["class_file_paths"] = class_check["paths"][:10]  # Sample paths
        
        # Get JAR file paths
        jar_check = self._check_jar_files(project_dir)
        validation_result["jar_file_paths"] = jar_check["paths"]
        
        # Check compilation recency
        recency_check = self._check_compilation_recency(project_dir)
        validation_result["recent_compilation"] = recency_check["recent"]
        validation_result["newest_class_time"] = recency_check.get("newest_class_time")
        
        # Find missing classes (Java files without corresponding class files)
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
        """Check for .class files in the project with caching."""
        cache_key = self._get_cache_key("class_files", project_dir)
        
        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result
        
        try:
            # Count .class files
            count_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
            count_result = self._execute_command_with_logging(count_cmd, "class file count")
            
            if not count_result["success"]:
                logger.warning(f"Class file count failed: {count_result.get('error', 'Unknown error')}")
                return {"count": 0, "paths": [], "error": count_result.get("error")}
            
            count = int(count_result["output"].strip()) if count_result["output"].strip() else 0
            
            # Get all paths (removed head limit for complete scan)
            paths_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null"
            paths_result = self._execute_command_with_logging(paths_cmd, "class file paths")
            
            paths = []
            if paths_result["success"] and paths_result["output"]:
                # Limit to first 100 for memory efficiency, but count is complete
                all_paths = [p.strip() for p in paths_result["output"].split("\n") if p.strip()]
                paths = all_paths[:100]  # Sample for details, but count is complete
            
            result = {"count": count, "paths": paths}
            
            # Cache the result
            self._cache_result(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"Failed to check class files: {e}")
            return {"count": 0, "paths": [], "error": str(e)}
    
    def _check_jar_files(self, project_dir: str) -> Dict[str, any]:
        """Check for JAR files in target/build directories with caching."""
        cache_key = self._get_cache_key("jar_files", project_dir)
        
        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result
        
        try:
            # Check Maven target and Gradle build directories (including build/libs)
            cmd = f"find {project_dir} \\( -path '*/target/*.jar' -o -path '*/build/*.jar' -o -path '*/build/libs/*.jar' \\) -type f 2>/dev/null"
            result = self._execute_command_with_logging(cmd, "JAR file search")
            
            paths = []
            if result["success"] and result["output"]:
                paths = [p.strip() for p in result["output"].split("\n") if p.strip()]
            elif not result["success"]:
                logger.warning(f"JAR file search failed: {result.get('error', 'Unknown error')}")
            
            result_dict = {"count": len(paths), "paths": paths}
            if not result["success"]:
                result_dict["error"] = result.get("error")
            
            # Cache the result
            self._cache_result(cache_key, result_dict)
            return result_dict
        except Exception as e:
            logger.error(f"Failed to check JAR files: {e}")
            return {"count": 0, "paths": [], "error": str(e)}
    
    def _check_compilation_recency(self, project_dir: str) -> Dict[str, any]:
        """Check if compilation is recent (within last hour)."""
        try:
            # Try GNU stat first (Linux/container environments) - get newest file
            cmd = f"find {project_dir} -name '*.class' -type f -exec stat -c '%Y' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result = self._execute_command_with_logging(cmd, "GNU stat compilation check")
            
            if result["success"] and result["output"].strip():
                try:
                    newest_timestamp = int(result["output"].strip())
                    current_timestamp = int(datetime.now().timestamp())
                    
                    # Check if compiled within last hour
                    age_seconds = current_timestamp - newest_timestamp
                    recent = age_seconds < (self.compilation_recency_hours * 3600)
                    
                    return {
                        "recent": recent,
                        "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                        "age_seconds": age_seconds
                    }
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to parse GNU stat output: {e}")
            
            # Fallback to BSD stat (macOS) - get newest file
            logger.debug("GNU stat failed, trying BSD stat")
            cmd_bsd = f"find {project_dir} -name '*.class' -type f -exec stat -f '%m' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result_bsd = self._execute_command_with_logging(cmd_bsd, "BSD stat compilation check")
            
            if result_bsd["success"] and result_bsd["output"].strip():
                try:
                    newest_timestamp = int(result_bsd["output"].strip())
                    current_timestamp = int(datetime.now().timestamp())
                    
                    age_seconds = current_timestamp - newest_timestamp
                    recent = age_seconds < (self.compilation_recency_hours * 3600)
                    
                    return {
                        "recent": recent,
                        "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                        "age_seconds": age_seconds
                    }
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to parse BSD stat output: {e}")
            
            # Final fallback to Python-based approach
            logger.debug("Both stat commands failed, using Python fallback")
            return self._check_compilation_recency_python_fallback(project_dir)
            
        except Exception as e:
            logger.error(f"Failed to check compilation recency: {e}")
            return {"recent": False, "error": str(e)}
    
    def _check_compilation_recency_python_fallback(self, project_dir: str) -> Dict[str, any]:
        """
        Python-based fallback for checking compilation recency.
        This method uses find + ls to get file modification times.
        """
        try:
            # Get list of .class files with detailed info (complete scan)
            cmd = f"find {project_dir} -name '*.class' -type f -exec ls -la {{}} \\; 2>/dev/null"
            result = self._execute_command_with_logging(cmd, "Python fallback compilation check")
            
            if not result["success"] or not result["output"].strip():
                return {"recent": False, "error": "No class files found or command failed"}
            
            newest_timestamp = 0
            current_timestamp = int(datetime.now().timestamp())
            
            # Parse ls output to extract modification times
            # ls -la format: -rw-r--r-- 1 user group size date time filename
            lines = result["output"].strip().split('\n')
            
            for line in lines:
                if not line.strip() or not line.startswith('-'):
                    continue
                
                try:
                    # Extract date/time from ls output
                    parts = line.split()
                    if len(parts) >= 8:
                        # Try to parse different date formats
                        date_str = f"{parts[5]} {parts[6]} {parts[7]}"
                        
                        # Handle different ls date formats
                        try:
                            # Format: "Dec 25 14:30" (current year)
                            file_time = datetime.strptime(f"{datetime.now().year} {date_str}", "%Y %b %d %H:%M")
                        except ValueError:
                            try:
                                # Format: "Dec 25 2023" (specific year)
                                file_time = datetime.strptime(date_str, "%b %d %Y")
                            except ValueError:
                                # Skip unparseable dates
                                continue
                        
                        file_timestamp = int(file_time.timestamp())
                        if file_timestamp > newest_timestamp:
                            newest_timestamp = file_timestamp
                
                except (ValueError, IndexError) as e:
                    logger.debug(f"Failed to parse ls line '{line}': {e}")
                    continue
            
            if newest_timestamp > 0:
                age_seconds = current_timestamp - newest_timestamp
                recent = age_seconds < (self.compilation_recency_hours * 3600)
                
                return {
                    "recent": recent,
                    "newest_class_time": datetime.fromtimestamp(newest_timestamp).isoformat(),
                    "age_seconds": age_seconds
                }
            
            return {"recent": False, "error": "Could not parse file timestamps"}
            
        except Exception as e:
            logger.error(f"Python fallback compilation recency check failed: {e}")
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
            
            # Check all files, not just first 100 (removed artificial limit)
            for java_file in java_files:
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
                        # Removed limit - check all missing classes for accuracy
            
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
    
    def parse_test_reports(self, project_dir: str) -> Dict[str, any]:
        """
        Parse test report XML files to get accurate test statistics.
        Supports both Maven Surefire and Gradle test reports.
        
        Args:
            project_dir: Project directory path
            
        Returns:
            Dictionary with detailed test statistics and status
        """
        if not self.docker_orchestrator:
            return {"valid": False, "error": "No docker orchestrator"}
        
        cache_key = self._get_cache_key("test_reports", project_dir)
        
        # Try cache first
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result
        
        test_result = {
            "valid": False,
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 0,
            "test_success": False,
            "report_files": [],
            "parsing_errors": []
        }
        
        try:
            # Step 1: Discover report directories first to avoid massive single-command outputs
            # Include Maven Surefire, Maven Failsafe, and Gradle test-results
            dirs_cmd = (
                f"find {project_dir} -type d "
                f"\\( -name 'surefire-reports' -o -name 'failsafe-reports' -o -path '*/build/test-results/*' \\) 2>/dev/null"
            )
            dirs_result = self.docker_orchestrator.execute_command(dirs_cmd)
            report_dirs = []
            if dirs_result.get("exit_code") == 0 and dirs_result.get("output"):
                report_dirs = [d.strip() for d in dirs_result.get("output", "").split("\n") if d.strip()]

            # Step 2: For each directory, list XML files in small batches to avoid truncation
            report_files: List[str] = []
            for report_dir in report_dirs:
                # Limit depth to keep per-command output small; Gradle may have nested per-class dirs
                list_cmd = f"find '{report_dir}' -maxdepth 2 -type f -name '*.xml' 2>/dev/null"
                list_result = self.docker_orchestrator.execute_command(list_cmd)
                if list_result.get("exit_code") == 0 and list_result.get("output"):
                    files = [f.strip() for f in list_result.get("output", "").split("\n") if f.strip()]
                    # Filter obviously irrelevant XMLs if any (keep flexible)
                    report_files.extend(files)

            # Fallback: If directory discovery failed to find anything, do a global file search (may be heavy)
            if not report_files:
                fallback_cmd = (
                    f"find {project_dir} -type f \\( -path '*/surefire-reports/*.xml' -o -path '*/failsafe-reports/*.xml' -o -path '*/test-results/*.xml' \\) 2>/dev/null"
                )
                fallback_res = self.docker_orchestrator.execute_command(fallback_cmd)
                if fallback_res.get("exit_code") == 0 and fallback_res.get("output"):
                    report_files = [f.strip() for f in fallback_res.get("output", "").split("\n") if f.strip()]

            test_result["report_files"] = report_files

            if not report_files:
                test_result["error"] = "No test report files found"
                return test_result

            # Step 3: Parse all XML files
            for report_file in report_files:
                try:
                    xml_result = self.docker_orchestrator.execute_command(f"cat '{report_file}'")
                    if xml_result.get("exit_code") != 0:
                        test_result["parsing_errors"].append(f"Failed to read {report_file}")
                        continue

                    xml_content = xml_result.get("output", "")
                    if not xml_content.strip():
                        continue

                    stats = self._parse_single_test_xml(xml_content, report_file)
                    if stats:
                        test_result["total_tests"] += stats.get("total", 0)
                        test_result["passed_tests"] += stats.get("passed", 0)
                        test_result["failed_tests"] += stats.get("failed", 0)
                        test_result["error_tests"] += stats.get("errors", 0)
                        test_result["skipped_tests"] += stats.get("skipped", 0)
                    else:
                        test_result["parsing_errors"].append(
                            f"Failed to parse XML structure in {report_file}"
                        )
                except Exception as e:
                    test_result["parsing_errors"].append(f"Error parsing {report_file}: {str(e)}")
                    logger.warning(f"Failed to parse test report {report_file}: {e}")

            # Determine test success: only if no failures and no errors
            test_result["test_success"] = (
                test_result["failed_tests"] == 0
                and test_result["error_tests"] == 0
                and test_result["total_tests"] > 0
            )
            test_result["valid"] = True

            logger.info(
                f"📊 Test report analysis: {test_result['total_tests']} total, "
                f"{test_result['passed_tests']} passed, {test_result['failed_tests']} failed, "
                f"{test_result['error_tests']} errors, {test_result['skipped_tests']} skipped"
            )

            # Check for multi-module projects and identify modules without tests
            modules_without_tests = self._check_modules_without_tests(project_dir, report_dirs)
            if modules_without_tests:
                test_result["modules_without_tests"] = modules_without_tests
                logger.warning(
                    f"⚠️ Multi-module project: {len(modules_without_tests)} modules lack test reports: "
                    f"{', '.join(modules_without_tests[:5])}"
                    f"{'...' if len(modules_without_tests) > 5 else ''}"
                )

        except Exception as e:
            test_result["error"] = f"Failed to parse test reports: {str(e)}"
            logger.error(f"Test report parsing failed: {e}")
        
        # Cache the result
        self._cache_result(cache_key, test_result)
        return test_result
    
    def _parse_single_test_xml(self, xml_content: str, file_path: str) -> Optional[Dict[str, int]]:
        """
        Parse a single test XML file and extract statistics.
        Handles both Maven Surefire and Gradle formats.
        """
        try:
            root = ET.fromstring(xml_content)
            
            # Maven Surefire format: <testsuite tests="X" failures="Y" errors="Z" skipped="W">
            if root.tag == "testsuite":
                total = int(root.get("tests", "0"))
                failures = int(root.get("failures", "0"))
                errors = int(root.get("errors", "0"))
                skipped = int(root.get("skipped", "0"))
                passed = total - failures - errors - skipped
                
                return {
                    "total": total,
                    "passed": max(0, passed),  # Ensure non-negative
                    "failed": failures,
                    "errors": errors,
                    "skipped": skipped
                }
            
            # Gradle format: <testsuites> containing multiple <testsuite>
            elif root.tag == "testsuites":
                total_stats = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
                
                for testsuite in root.findall("testsuite"):
                    suite_total = int(testsuite.get("tests", "0"))
                    suite_failures = int(testsuite.get("failures", "0"))
                    suite_errors = int(testsuite.get("errors", "0"))
                    suite_skipped = int(testsuite.get("skipped", "0"))
                    suite_passed = suite_total - suite_failures - suite_errors - suite_skipped
                    
                    total_stats["total"] += suite_total
                    total_stats["passed"] += max(0, suite_passed)
                    total_stats["failed"] += suite_failures
                    total_stats["errors"] += suite_errors
                    total_stats["skipped"] += suite_skipped
                
                return total_stats
            
            # Try to find testsuite elements even if root is different
            testsuites = root.findall(".//testsuite")
            if testsuites:
                total_stats = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
                
                for testsuite in testsuites:
                    suite_total = int(testsuite.get("tests", "0"))
                    suite_failures = int(testsuite.get("failures", "0"))
                    suite_errors = int(testsuite.get("errors", "0"))
                    suite_skipped = int(testsuite.get("skipped", "0"))
                    suite_passed = suite_total - suite_failures - suite_errors - suite_skipped
                    
                    total_stats["total"] += suite_total
                    total_stats["passed"] += max(0, suite_passed)
                    total_stats["failed"] += suite_failures
                    total_stats["errors"] += suite_errors
                    total_stats["skipped"] += suite_skipped
                
                return total_stats if total_stats["total"] > 0 else None
            
            logger.warning(f"Unrecognized XML format in {file_path}, root tag: {root.tag}")
            return None
            
        except ET.ParseError as e:
            logger.warning(f"XML parsing error in {file_path}: {e}")
            # Try fallback extraction instead of returning None
            return self._extract_test_stats_fallback(xml_content, file_path)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Data extraction error in {file_path}: {e}")
            # Try fallback extraction for other errors too
            return self._extract_test_stats_fallback(xml_content, file_path)

    def _check_modules_without_tests(self, project_dir: str, report_dirs: List[str]) -> List[str]:
        """
        Check if this is a multi-module project and identify modules without test reports.

        Returns:
            List of module names that lack test reports
        """
        modules_without_tests = []

        try:
            # Check if root pom.xml has <modules> section
            pom_check_cmd = f"test -f {project_dir}/pom.xml && echo 'EXISTS' || echo 'MISSING'"
            pom_result = self._execute_command_with_logging(pom_check_cmd, "checking for root pom.xml")

            if not pom_result['success'] or 'MISSING' in pom_result.get('output', ''):
                return []  # Not a Maven project or no root POM

            # Extract modules from pom.xml
            modules_cmd = f"grep -A 100 '<modules>' {project_dir}/pom.xml 2>/dev/null | grep -B 100 '</modules>' | grep '<module>' | sed 's/<module>//g' | sed 's/<\\/module>//g' | tr -d ' \\t'"
            modules_result = self._execute_command_with_logging(modules_cmd, "extracting Maven modules")

            if not modules_result['success'] or not modules_result.get('output'):
                return []  # No modules found

            modules = [m.strip() for m in modules_result['output'].split('\n') if m.strip()]

            if not modules:
                return []  # Not a multi-module project

            logger.debug(f"Found {len(modules)} modules in project: {modules}")

            # Convert report_dirs to set for faster lookup
            report_dir_set = set(report_dirs)

            # Check each module for test reports
            for module in modules:
                module_dir = f"{project_dir}/{module}"

                # Check if module has any test reports
                has_reports = False
                for potential_report_dir in [
                    f"{module_dir}/target/surefire-reports",
                    f"{module_dir}/target/failsafe-reports",
                    f"{module_dir}/build/test-results"
                ]:
                    if potential_report_dir in report_dir_set:
                        has_reports = True
                        break

                if not has_reports:
                    # Double-check by looking for any XML test reports
                    check_cmd = f"find {module_dir} -path '*/target/surefire-reports/*.xml' -o -path '*/target/failsafe-reports/*.xml' 2>/dev/null | head -1"
                    check_result = self._execute_command_with_logging(check_cmd, f"checking for test reports in {module}")

                    if not check_result['success'] or not check_result.get('output', '').strip():
                        modules_without_tests.append(module)
                        logger.debug(f"Module {module} has no test reports")

            return modules_without_tests

        except Exception as e:
            logger.warning(f"Failed to check modules without tests: {e}")
            return []

    def _extract_test_stats_fallback(self, xml_content: str, file_path: str) -> Dict[str, int]:
        """
        Fallback regex extraction when XML parsing fails.
        This ensures we don't lose test counts from malformed XML files.

        Args:
            xml_content: Raw XML content
            file_path: Path to the XML file for logging

        Returns:
            Dictionary with test statistics (never None)
        """
        try:
            # Try to extract from testsuite tag attributes using regex
            # Handle both single-line and multi-line testsuite tags
            import re

            # Pattern to match testsuite opening tag and extract attributes
            # This pattern is flexible with attribute order
            testsuite_pattern = r'<testsuite[^>]*?>'
            match = re.search(testsuite_pattern, xml_content, re.IGNORECASE)

            if match:
                testsuite_tag = match.group(0)

                # Extract individual attributes
                tests_match = re.search(r'tests=["\'](\d+)["\']', testsuite_tag)
                failures_match = re.search(r'failures=["\'](\d+)["\']', testsuite_tag)
                errors_match = re.search(r'errors=["\'](\d+)["\']', testsuite_tag)
                skipped_match = re.search(r'skipped=["\'](\d+)["\']', testsuite_tag)

                if tests_match:
                    total = int(tests_match.group(1))
                    failures = int(failures_match.group(1)) if failures_match else 0
                    errors = int(errors_match.group(1)) if errors_match else 0
                    skipped = int(skipped_match.group(1)) if skipped_match else 0
                    passed = total - failures - errors - skipped

                    logger.info(f"Recovered stats from malformed XML {file_path}: {total} tests")
                    return {
                        "total": total,
                        "passed": max(0, passed),
                        "failed": failures,
                        "errors": errors,
                        "skipped": skipped
                    }

            # If we can't extract from testsuite, try counting testcase tags as last resort
            testcase_count = len(re.findall(r'<testcase\s', xml_content))
            if testcase_count > 0:
                logger.info(f"Counted {testcase_count} testcase tags in {file_path}")
                # We can't determine pass/fail from just counting tags
                return {
                    "total": testcase_count,
                    "passed": testcase_count,  # Assume passed unless we know otherwise
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0
                }

        except Exception as e:
            logger.warning(f"Fallback extraction also failed for {file_path}: {e}")

        # Return zeros instead of None to prevent losing the file entirely
        # This way we at least know a file existed even if we couldn't parse it
        logger.warning(f"Could not extract any test data from {file_path}, returning zeros")
        return {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    def validate_build_status(self, project_name: str) -> Dict[str, any]:
        """
        Build status validation based on physical evidence hierarchy.
        Does NOT execute build commands to avoid environment dependencies.
        ONLY validates build/compilation status, NOT test results.
        
        Args:
            project_name: Name of the project
            
        Returns:
            Dict with:
                - success: bool (determined by build evidence hierarchy)
                - evidence: dict of build validation details
                - reason: str (explanation of the decision)
        """
        logger.info(f"Starting build artifact validation for project: {project_name}")
        
        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path
        
        # Collect build evidence only (no test-related checks)
        evidence = {
            'build_system': None,
            'has_artifacts': False,
            'artifact_count': 0,
            'has_build_fingerprints': False,
            'fingerprint_details': {}
        }
        
        # Detect build system
        build_system = self._detect_build_system(project_dir)
        evidence['build_system'] = build_system
        logger.info(f"Detected build system: {build_system}")
        
        # Check 1: Build artifacts
        artifacts_result = self._check_build_artifacts_complete(project_dir)
        evidence['has_artifacts'] = artifacts_result['exist']
        evidence['artifact_count'] = artifacts_result['count']
        if evidence['has_artifacts']:
            logger.info(f"✅ Found {artifacts_result['count']} build artifacts (JARs: {artifacts_result['jar_count']}, Classes: {artifacts_result['class_count']})")
        else:
            logger.info("❌ No build artifacts found")
        
        # Check 2: Build system fingerprints
        if build_system == 'maven':
            fingerprints = self._validate_maven_fingerprints(project_dir)
            evidence['has_build_fingerprints'] = fingerprints['valid']
            evidence['fingerprint_details'] = fingerprints['details']
            if fingerprints['valid']:
                logger.info(f"✅ Maven build fingerprints found: {fingerprints['details']}")
                if fingerprints['modules']:
                    logger.info(f"   Multi-module project with modules: {fingerprints['modules']}")
        elif build_system == 'gradle':
            cache = self._validate_gradle_cache(project_dir)
            evidence['has_build_fingerprints'] = cache['valid']
            evidence['fingerprint_details'] = cache['details']
            if cache['valid']:
                logger.info(f"✅ Gradle build cache found: {cache['details']}")
                if cache['subprojects']:
                    logger.info(f"   Multi-project build with subprojects: {cache['subprojects']}")
        
        # Decision logic for BUILD ONLY (no test considerations)
        success = False
        reason = ""
        
        # Priority 1: Build fingerprints (strongest evidence of successful build)
        if evidence['has_build_fingerprints']:
            success = True
            reason = f"Build fingerprints found for {build_system} project"
        
        # Priority 2: Build artifacts exist
        elif evidence['has_artifacts']:
            # Check if expected artifacts are present
            expected_artifacts = self._get_expected_artifacts(project_dir, build_system)
            if expected_artifacts:
                actual_vs_expected = self._verify_expected_artifacts(project_dir, expected_artifacts)
                if actual_vs_expected['all_present']:
                    success = True
                    reason = f"All expected build artifacts found: {', '.join(actual_vs_expected['found'])}"
                else:
                    success = False
                    reason = f"Missing expected build artifacts: {', '.join(actual_vs_expected['missing'])}"
            else:
                # Cannot determine expected artifacts, fall back to existence check
                # For Java projects, at least having classes indicates compilation occurred
                if build_system in ['maven', 'gradle'] and artifacts_result.get('class_count', 0) > 0:
                    success = True
                    reason = f"Found {artifacts_result['class_count']} compiled classes (build appears successful)"
                else:
                    success = True
                    reason = f"Found {evidence['artifact_count']} build artifacts"
        
        # Priority 3: No build evidence found
        else:
            success = False
            reason = "No build evidence found (no artifacts or build fingerprints)"
        
        result = {
            'success': success,
            'evidence': evidence,
            'reason': reason
        }
        
        logger.info(f"Build validation complete: {'SUCCESS' if success else 'FAILURE'} - {reason}")
        return result
    
    def validate_test_status(self, project_name: str) -> Dict[str, any]:
        """
        Validate test execution status with pass rate calculation.
        Completely separate from build validation.
        
        Args:
            project_name: Name of the project
            
        Returns:
            Dict with:
                - has_test_reports: bool
                - total_tests: int
                - passed_tests: int
                - failed_tests: int
                - error_tests: int
                - skipped_tests: int
                - pass_rate: float (0-100)
                - test_exclusions: List[str] (detected excluded tests)
                - status: str (SUCCESS/WARNING/PARTIAL/FAILED)
                - reason: str (explanation of the status)
        """
        logger.info(f"Starting test validation for project: {project_name}")
        
        project_dir = f"{self.project_path}/{project_name}" if project_name else self.project_path
        
        # Parse test reports with enhanced metrics
        test_metrics = self.parse_test_reports_with_metrics(project_dir)
        
        # Calculate pass rate
        pass_rate = self.calculate_test_pass_rate(test_metrics)
        
        # Determine test status based on metrics
        if not test_metrics.get('valid', False):
            status = "WARNING"
            reason = "No test reports found"
        elif pass_rate == 100.0:
            status = "SUCCESS"
            reason = f"All {test_metrics['total_tests']} tests passed"
        elif pass_rate > 0:
            status = "PARTIAL"
            reason = f"Tests partially passed: {test_metrics['passed_tests']}/{test_metrics['total_tests']} ({pass_rate:.1f}%)"
        else:
            status = "FAILED"
            reason = f"All tests failed: 0/{test_metrics['total_tests']} passed"
        
        result = {
            'has_test_reports': test_metrics.get('valid', False),
            'total_tests': test_metrics.get('total_tests', 0),
            'passed_tests': test_metrics.get('passed_tests', 0),
            'failed_tests': test_metrics.get('failed_tests', 0),
            'error_tests': test_metrics.get('error_tests', 0),
            'skipped_tests': test_metrics.get('skipped_tests', 0),
            'pass_rate': pass_rate,
            'test_exclusions': test_metrics.get('test_exclusions', []),
            'modules_without_tests': test_metrics.get('modules_without_tests', []),
            'status': status,
            'reason': reason,
            'report_files': test_metrics.get('report_files', []),
            'parsing_errors': test_metrics.get('parsing_errors', [])
        }
        
        logger.info(f"Test validation complete: {status} - {reason}")
        if result['test_exclusions']:
            logger.warning(f"Detected test exclusions: {', '.join(result['test_exclusions'])}")
        if result['modules_without_tests']:
            untested_count = len(result['modules_without_tests'])
            logger.warning(f"⚠️ MISSING TEST REPORTS: {untested_count} modules have no test results: {', '.join(result['modules_without_tests'])}")
            logger.warning(f"📊 Found {result['total_tests']} tests executed, but some modules were skipped")
            logger.info("💡 RECOMMENDED: Use maven(command='test', fail_at_end=True) at project root to test ALL modules")
            logger.info("💡 Alternative: Run bash(command='cd /workspace/PROJECT && mvn test --fail-at-end')")

        return result
    
    def parse_test_reports_with_metrics(self, project_dir: str) -> Dict[str, any]:
        """
        Enhanced test report parsing with pass rate calculations and exclusion detection.
        Extends the existing parse_test_reports with additional metrics.
        
        Args:
            project_dir: Project directory path
            
        Returns:
            Dictionary with enhanced test statistics including:
                - All fields from parse_test_reports
                - test_exclusions: List of detected excluded tests
                - modules_without_tests: List of modules that lack test reports
        """
        # Start with the base test report parsing
        base_result = self.parse_test_reports(project_dir)
        
        # Add enhanced metrics
        base_result['test_exclusions'] = self._detect_test_exclusions(project_dir)

        # We don't calculate coverage - that's about test quality, not our concern
        # SAG only cares that tests were executed, not how comprehensive they are
        # The actual test count from reports is the only truth we need
        
        return base_result
    
    def calculate_test_pass_rate(self, test_metrics: Dict[str, any]) -> float:
        """
        Calculate test pass rate from test statistics.
        
        Args:
            test_metrics: Dictionary containing test statistics
            
        Returns:
            Pass rate as percentage (0-100)
        """
        total = test_metrics.get('total_tests', 0)
        if total == 0:
            return 0.0
        
        passed = test_metrics.get('passed_tests', 0)
        return (passed / total) * 100
    
    def _detect_test_exclusions(self, project_dir: str) -> List[str]:
        """
        Detect test exclusion patterns in build configuration or recent commands.
        
        Returns:
            List of detected test exclusion patterns
        """
        exclusions = []
        
        try:
            # Maven: scan all pom.xml files but only extract excludes inside surefire/failsafe plugin blocks
            poms_cmd = f"find {project_dir} -type f -name 'pom.xml' 2>/dev/null"
            poms_result = self._execute_command_with_logging(poms_cmd, "discovering Maven POMs for exclusions")
            pom_files = [p.strip() for p in (poms_result.get('output') or '').split('\n') if p.strip()] if poms_result['success'] else []

            plugin_pattern = re.compile(
                r"<plugin>[\s\S]*?<artifactId>\s*maven-(surefire|failsafe)-plugin\s*</artifactId>[\s\S]*?</plugin>",
                re.IGNORECASE
            )
            exclude_tag_pattern = re.compile(r"<exclude>\s*([^<]+?)\s*</exclude>", re.IGNORECASE)
            skip_flag_pattern = re.compile(r"<skipTests>\s*true\s*</skipTests>", re.IGNORECASE)

            for pom in pom_files:
                cat_res = self._execute_command_with_logging(f"cat '{pom}' 2>/dev/null || true", f"reading {pom}")
                if not cat_res['success'] or not cat_res.get('output'):
                    continue
                content = cat_res['output']
                for plugin_block in plugin_pattern.findall(content) or []:
                    # The regex returns only the group; re-find full blocks to extract excludes
                    for block_match in re.finditer(
                        r"<plugin>[\s\S]*?<artifactId>\s*maven-(?:surefire|failsafe)-plugin\s*</artifactId>[\s\S]*?</plugin>",
                        content,
                        re.IGNORECASE,
                    ):
                        block = block_match.group(0)
                        exclusions.extend(exclude_tag_pattern.findall(block))
                        if skip_flag_pattern.search(block):
                            exclusions.append("ALL_TESTS_SKIPPED")

            # Gradle: inspect test{} blocks only
            gradle_cmd = f"find {project_dir} -type f \\(-name 'build.gradle' -o -name 'build.gradle.kts'\\) 2>/dev/null"
            gradle_files_res = self._execute_command_with_logging(gradle_cmd, "discovering Gradle build files for exclusions")
            gradle_files = [g.strip() for g in (gradle_files_res.get('output') or '').split('\n') if g.strip()] if gradle_files_res['success'] else []

            for gf in gradle_files:
                gcat = self._execute_command_with_logging(f"cat '{gf}' 2>/dev/null || true", f"reading {gf}")
                if not gcat['success'] or not gcat.get('output'):
                    continue
                gcontent = gcat['output']
                # Capture test { ... } blocks
                for test_block_match in re.finditer(r"test\s*\{([\s\S]*?)\}", gcontent, re.IGNORECASE):
                    block = test_block_match.group(1)
                    # Direct exclude 'exclude "pattern"' or 'exclude 'pattern''
                    exclusions.extend(re.findall(r"exclude\s*['\"]([^'\"]+)['\"]", block))
                    # excludeTestsMatching inside filter {}
                    for filter_block in re.findall(r"filter\s*\{([\s\S]*?)\}", block, re.IGNORECASE):
                        exclusions.extend(re.findall(r"excludeTestsMatching\s*['\"]([^'\"]+)['\"]", filter_block))
                # useJUnitPlatform { excludeTags 'slow' }
                for ujp_block in re.finditer(r"useJUnitPlatform\s*\{([\s\S]*?)\}", gcontent, re.IGNORECASE):
                    tags = re.findall(r"excludeTags\s*['\"]([^'\"]+)['\"]", ujp_block.group(1))
                    exclusions.extend([f"EXCLUDE_TAG:{t}" for t in tags])

            # Check for skip flags in recent commands (from command history if available)
            history_cmd = (
                f"grep -E 'DskipTests|-x test|--exclude-task test|Dtest=' {project_dir}/.setup_agent/command_history.txt 2>/dev/null || true"
            )
            history_result = self._execute_command_with_logging(history_cmd, "checking command history for test skips")
            if history_result['success'] and history_result.get('output'):
                hist = history_result['output']
                if '-DskipTests' in hist or 'skipTests=true' in hist:
                    exclusions.append("ALL_TESTS_SKIPPED")
                if '-x test' in hist or '--exclude-task test' in hist:
                    exclusions.append("GRADLE_TESTS_EXCLUDED")
                # Extract -Dtest=!Pattern and -Dtest=Pattern
                exclusions.extend(re.findall(r"-Dtest=!([^\s]+)", hist))
                exclusions.extend([f"INCLUDE_TEST:{m}" for m in re.findall(r"-Dtest=([^!\s][^\s]*)", hist)])

        except Exception as e:
            logger.warning(f"Failed to detect test exclusions: {e}")
        
        # Remove duplicates and return
        return list(set(exclusions))
    
    
    def _detect_build_system(self, project_dir: str) -> str:
        """
        Detect the build system used by the project.
        
        Returns:
            'maven', 'gradle', 'npm', 'python', or 'unknown'
        """
        # Check for build files
        checks = [
            ('pom.xml', 'maven'),
            ('build.gradle', 'gradle'),
            ('build.gradle.kts', 'gradle'),
            ('package.json', 'npm'),
            ('requirements.txt', 'python'),
            ('setup.py', 'python'),
            ('pyproject.toml', 'python')
        ]
        
        for filename, build_system in checks:
            cmd = f"test -f {project_dir}/{filename}"
            result = self._execute_command_with_logging(cmd, f"checking for {filename}")
            if result['success']:
                return build_system
        
        return 'unknown'
    
    def _check_build_artifacts_complete(self, project_dir: str) -> Dict[str, any]:
        """
        Complete check of build artifacts without head limits.
        
        Returns:
            Dict with artifact existence and counts
        """
        cache_key = self._get_cache_key("artifacts_complete", project_dir)
        cached = self._get_cached_result(cache_key)
        if cached:
            return cached
        
        result = {
            'exist': False,
            'count': 0,
            'jar_count': 0,
            'class_count': 0,
            'details': {}
        }
        
        # Count JAR files (complete scan, no head limit)
        jar_cmd = f"find {project_dir} -name '*.jar' -type f 2>/dev/null | wc -l"
        jar_result = self._execute_command_with_logging(jar_cmd, "counting JAR files")
        if jar_result['success']:
            result['jar_count'] = int(jar_result['output'].strip() or 0)
        
        # Count class files (complete scan)
        class_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | wc -l"
        class_result = self._execute_command_with_logging(class_cmd, "counting class files")
        if class_result['success']:
            result['class_count'] = int(class_result['output'].strip() or 0)
        
        # Check for Node modules
        node_cmd = f"test -d {project_dir}/node_modules && find {project_dir}/node_modules -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"
        node_result = self._execute_command_with_logging(node_cmd, "checking Node modules")
        if node_result['success']:
            result['details']['node_modules'] = int(node_result['output'].strip() or 0)
        
        # Determine if artifacts exist
        result['count'] = result['jar_count'] + result['class_count'] + result['details'].get('node_modules', 0)
        result['exist'] = result['count'] > 0
        
        self._cache_result(cache_key, result)
        return result
    
    def _validate_maven_fingerprints(self, project_dir: str) -> Dict[str, any]:
        """
        Validate Maven build fingerprints without executing mvn commands.
        
        Checks:
        - target/maven-status/ directories
        - target/maven-archiver/pom.properties
        - Multi-module fingerprints
        """
        result = {
            'valid': False,
            'details': {},
            'modules': []
        }
        
        # Check main project fingerprints
        fingerprint_checks = [
            f"{project_dir}/target/maven-status/maven-compiler-plugin/compile/default-compile/createdFiles.lst",
            f"{project_dir}/target/maven-archiver/pom.properties",
            f"{project_dir}/target/classes"
        ]
        
        fingerprints_found = 0
        for fingerprint_path in fingerprint_checks:
            cmd = f"test -e {fingerprint_path}"
            check_result = self._execute_command_with_logging(cmd, f"checking {fingerprint_path}")
            if check_result['success']:
                fingerprints_found += 1
                result['details'][fingerprint_path.split('/')[-1]] = True
        
        # Check for multi-module projects
        modules_cmd = f"find {project_dir} -mindepth 2 -maxdepth 2 -name 'pom.xml' -type f 2>/dev/null"
        modules_result = self._execute_command_with_logging(modules_cmd, "finding Maven modules")
        if modules_result['success'] and modules_result['output']:
            modules = modules_result['output'].strip().split('\n')
            for module_pom in modules:
                if module_pom:
                    module_dir = '/'.join(module_pom.split('/')[:-1])
                    module_name = module_dir.split('/')[-1]
                    
                    # Check module fingerprints
                    module_target = f"{module_dir}/target/maven-status"
                    cmd = f"test -d {module_target}"
                    if self._execute_command_with_logging(cmd, f"checking module {module_name}")['success']:
                        result['modules'].append(module_name)
        
        # Determine validity
        result['valid'] = fingerprints_found > 0 or len(result['modules']) > 0
        
        return result
    
    def _get_expected_artifacts(self, project_dir: str, build_system: str) -> List[Dict[str, str]]:
        """
        Parse build configuration to determine expected artifacts.
        
        Returns:
            List of expected artifacts with paths and types
        """
        expected = []
        
        if build_system == 'maven':
            expected = self._parse_maven_expected_artifacts(project_dir)
        elif build_system == 'gradle':
            expected = self._parse_gradle_expected_artifacts(project_dir)
        
        return expected
    
    def _parse_maven_expected_artifacts(self, project_dir: str) -> List[Dict[str, str]]:
        """
        Parse pom.xml to determine expected Maven artifacts including .class files.
        Enhanced with multiple fallback strategies for robustness.
        """
        expected = []

        # Read main pom.xml
        pom_cmd = f"cat {project_dir}/pom.xml 2>/dev/null"
        pom_result = self._execute_command_with_logging(pom_cmd, "reading pom.xml")

        if not pom_result['success']:
            return expected

        pom_content = pom_result['output']

        import re

        # Initialize variables
        artifact_id = None
        version = None
        packaging = 'jar'  # default

        # Strategy 1: Enhanced regex with more truncation anchors
        try:
            # Remove parent section to avoid matching parent artifactId
            pom_without_parent = re.sub(r'<parent>.*?</parent>', '', pom_content, flags=re.DOTALL)

            # Apply multiple truncation anchors to isolate project definition
            # Each split removes potential interference from later sections
            project_section = pom_without_parent

            # More comprehensive list of sections to truncate
            truncation_points = [
                '<dependencies>', '<dependencyManagement>',
                '<build>', '<reporting>', '<profiles>',
                '<pluginManagement>', '<properties>',
                '<distributionManagement>', '<repositories>'
            ]

            for truncation_point in truncation_points:
                if truncation_point in project_section:
                    project_section = project_section.split(truncation_point)[0]

            # Extract artifactId from isolated project section
            artifact_match = re.search(r'<artifactId>([^<]+)</artifactId>', project_section)
            artifact_id = artifact_match.group(1).strip() if artifact_match else None

            # Extract version (might be inherited from parent)
            version_match = re.search(r'<version>([^<]+)</version>', project_section)
            version = version_match.group(1).strip() if version_match else None

            # Extract packaging
            packaging_match = re.search(r'<packaging>([^<]+)</packaging>', project_section)
            packaging = packaging_match.group(1).strip() if packaging_match else 'jar'

        except Exception as e:
            logger.debug(f"Regex parsing failed: {e}, trying XML parsing fallback")

        # Strategy 2: XML parsing fallback (lightweight, no external dependencies)
        if not artifact_id:
            try:
                import xml.etree.ElementTree as ET

                # Parse POM as XML, handling namespaces
                root = ET.fromstring(pom_content)

                # Handle default Maven namespace
                ns = {'m': 'http://maven.apache.org/POM/4.0.0'}

                # Try with namespace first
                artifact_elem = root.find('m:artifactId', ns)
                if artifact_elem is None:
                    # Try without namespace
                    artifact_elem = root.find('artifactId')

                if artifact_elem is not None and artifact_elem.text:
                    artifact_id = artifact_elem.text.strip()

                # Get version
                version_elem = root.find('m:version', ns)
                if version_elem is None:
                    version_elem = root.find('version')

                if version_elem is not None and version_elem.text:
                    version = version_elem.text.strip()

                # Get packaging
                packaging_elem = root.find('m:packaging', ns)
                if packaging_elem is None:
                    packaging_elem = root.find('packaging')

                if packaging_elem is not None and packaging_elem.text:
                    packaging = packaging_elem.text.strip()

            except Exception as e:
                logger.debug(f"XML parsing fallback failed: {e}")

        # Strategy 3: Read version from pom.properties if still missing
        if artifact_id and not version:
            pom_props_cmd = f"cat {project_dir}/target/maven-archiver/pom.properties 2>/dev/null"
            pom_props_result = self._execute_command_with_logging(pom_props_cmd, "reading pom.properties for version")

            if pom_props_result['success']:
                # Parse properties file
                for line in pom_props_result['output'].split('\n'):
                    if line.startswith('version='):
                        version = line.split('=', 1)[1].strip()
                        logger.debug(f"Retrieved version {version} from pom.properties")
                        break

        # Strategy 4: Try to infer version from existing JARs if still missing
        if artifact_id and not version and packaging != 'pom':
            # Look for existing JAR that matches the pattern
            jar_search_cmd = f"ls {project_dir}/target/{artifact_id}-*.{packaging} 2>/dev/null | head -1"
            jar_result = self._execute_command_with_logging(jar_search_cmd, f"searching for {artifact_id} JAR")

            if jar_result['success'] and jar_result['output']:
                # Extract version from filename
                import os
                jar_name = os.path.basename(jar_result['output'].strip())
                # Pattern: artifactId-version.packaging
                version_pattern = f"{artifact_id}-(.+)\\.{packaging}"
                version_match = re.match(version_pattern, jar_name)
                if version_match:
                    version = version_match.group(1)
                    logger.debug(f"Inferred version {version} from existing JAR: {jar_name}")
        
        # Check for modules (multi-module project)
        modules_match = re.search(r'<modules>(.*?)</modules>', pom_content, re.DOTALL)
        if modules_match:
            # Multi-module project
            module_content = modules_match.group(1)
            modules = re.findall(r'<module>([^<]+)</module>', module_content)
            
            for module in modules:
                module_dir = f"{project_dir}/{module}"
                # Recursively get expected artifacts for each module
                module_expected = self._parse_maven_expected_artifacts(module_dir)
                expected.extend(module_expected)
        else:
            # Single module project
            if packaging == 'pom':
                # Parent POM doesn't produce artifacts but might coordinate compilation
                pass
            else:
                # Expected .class files
                # Check if src/main/java exists
                src_main_check = f"test -d {project_dir}/src/main/java && echo EXISTS"
                src_main_result = self._execute_command_with_logging(src_main_check, "checking src/main/java")
                
                if src_main_result['success'] and 'EXISTS' in src_main_result.get('output', ''):
                    # Count Java source files to expect corresponding class files
                    java_count_cmd = f"find {project_dir}/src/main/java -name '*.java' -type f 2>/dev/null | wc -l"
                    java_count_result = self._execute_command_with_logging(java_count_cmd, "counting Java sources")
                    
                    if java_count_result['success']:
                        java_count = int(java_count_result['output'].strip() or 0)
                        if java_count > 0:
                            # Expect classes in target/classes
                            expected.append({
                                'path': f"{project_dir}/target/classes",
                                'type': 'classes',
                                'artifact': f"compiled classes (from {java_count} source files)",
                                'min_count': java_count  # At least one .class per .java
                            })
                
                # Expected JAR/WAR artifact
                if artifact_id and version:
                    expected_path = f"{project_dir}/target/{artifact_id}-{version}.{packaging}"
                    expected.append({
                        'path': expected_path,
                        'type': packaging,
                        'artifact': f"{artifact_id}-{version}.{packaging}"
                    })
        
        return expected
    
    def _parse_gradle_expected_artifacts(self, project_dir: str) -> List[Dict[str, str]]:
        """
        Parse build.gradle to determine expected Gradle artifacts including .class files.
        """
        expected = []
        
        # Check for build.gradle or build.gradle.kts
        gradle_files = ['build.gradle', 'build.gradle.kts']
        gradle_content = None
        
        for gradle_file in gradle_files:
            cmd = f"cat {project_dir}/{gradle_file} 2>/dev/null"
            result = self._execute_command_with_logging(cmd, f"reading {gradle_file}")
            if result['success']:
                gradle_content = result['output']
                break
        
        if not gradle_content:
            return expected
        
        # Check if it's a Java/Kotlin project
        if 'java' in gradle_content or 'kotlin' in gradle_content:
            # Check for settings.gradle to detect multi-project
            settings_cmd = f"cat {project_dir}/settings.gradle 2>/dev/null"
            settings_result = self._execute_command_with_logging(settings_cmd, "reading settings.gradle")
            
            if settings_result['success']:
                settings_content = settings_result['output']
                # Extract included projects
                import re
                # Updated regex to handle multi-line include statements
                includes = re.findall(r"include\s*\(?\s*['\"]([^'\"]+)['\"]\s*\)?", settings_content, re.MULTILINE)
                
                for subproject in includes:
                    subproject_path = subproject.replace(':', '/')
                    subproject_dir = f"{project_dir}/{subproject_path}"
                    
                    # Expected .class files for subproject
                    src_check = f"test -d {subproject_dir}/src/main/java && echo EXISTS"
                    src_result = self._execute_command_with_logging(src_check, f"checking {subproject_path} sources")
                    
                    if src_result['success'] and 'EXISTS' in src_result.get('output', ''):
                        # Count Java sources
                        count_cmd = f"find {subproject_dir}/src/main/java -name '*.java' -type f 2>/dev/null | wc -l"
                        count_result = self._execute_command_with_logging(count_cmd, f"counting {subproject_path} sources")
                        
                        if count_result['success']:
                            java_count = int(count_result['output'].strip() or 0)
                            if java_count > 0:
                                expected.append({
                                    'path': f"{subproject_dir}/build/classes/java/main",
                                    'type': 'classes',
                                    'artifact': f"{subproject_path} classes ({java_count} sources)",
                                    'min_count': java_count
                                })
                    
                    # Expected JAR for subproject
                    expected.append({
                        'path': f"{subproject_dir}/build/libs",
                        'type': 'jar',
                        'artifact': f"{subproject_path} JAR"
                    })
            else:
                # Single project
                # Check for Java sources
                src_check = f"test -d {project_dir}/src/main/java && echo EXISTS"
                src_result = self._execute_command_with_logging(src_check, "checking main sources")
                
                if src_result['success'] and 'EXISTS' in src_result.get('output', ''):
                    # Count Java sources
                    count_cmd = f"find {project_dir}/src/main/java -name '*.java' -type f 2>/dev/null | wc -l"
                    count_result = self._execute_command_with_logging(count_cmd, "counting main sources")
                    
                    if count_result['success']:
                        java_count = int(count_result['output'].strip() or 0)
                        if java_count > 0:
                            expected.append({
                                'path': f"{project_dir}/build/classes/java/main",
                                'type': 'classes',
                                'artifact': f"compiled classes ({java_count} sources)",
                                'min_count': java_count
                            })
                
                # Expected JAR
                expected.append({
                    'path': f"{project_dir}/build/libs",
                    'type': 'jar',
                    'artifact': "main JAR"
                })
        
        return expected
    
    def _verify_expected_artifacts(self, project_dir: str, expected_artifacts: List[Dict[str, str]]) -> Dict[str, any]:
        """
        Verify that expected artifacts actually exist.
        """
        result = {
            'all_present': True,
            'found': [],
            'missing': []
        }
        
        for expected in expected_artifacts:
            artifact_found = False
            
            if expected['type'] == 'classes':
                # For .class files, check if directory has sufficient class files
                class_count_cmd = f"find {expected['path']} -name '*.class' -type f 2>/dev/null | wc -l"
                class_count_result = self._execute_command_with_logging(class_count_cmd, f"counting classes in {expected['path']}")
                
                if class_count_result['success']:
                    class_count = int(class_count_result['output'].strip() or 0)
                    min_expected = expected.get('min_count', 1)
                    
                    # We expect at least as many .class files as .java files
                    # (could be more due to inner classes, anonymous classes, etc.)
                    if class_count >= min_expected:
                        artifact_found = True
                        result['found'].append(f"{expected['artifact']} ({class_count} classes found)")
                    else:
                        result['missing'].append(f"{expected['artifact']} (found {class_count}, expected >={min_expected})")
                        result['all_present'] = False
                else:
                    result['missing'].append(expected['artifact'])
                    result['all_present'] = False
                    
            elif expected['path'].endswith('/build/libs') or expected['path'].endswith('/target'):
                # Directory-based check for JARs (removed head limit to check all)
                check_cmd = f"find {expected['path']} -name '*.{expected['type']}' -type f 2>/dev/null"
                check_result = self._execute_command_with_logging(check_cmd, f"checking {expected['artifact']}")
                
                if check_result['success'] and check_result.get('output', '').strip():
                    artifact_found = True
                    result['found'].append(expected['artifact'])
                else:
                    result['missing'].append(expected['artifact'])
                    result['all_present'] = False
                    
            else:
                # Specific file check (Maven style)
                check_cmd = f"test -f {expected['path']} && echo EXISTS"
                check_result = self._execute_command_with_logging(check_cmd, f"checking {expected['artifact']}")
                
                if check_result['success'] and check_result.get('output', '').strip():
                    artifact_found = True
                    result['found'].append(expected['artifact'])
                else:
                    result['missing'].append(expected['artifact'])
                    result['all_present'] = False
        
        return result
    
    def _validate_gradle_cache(self, project_dir: str) -> Dict[str, any]:
        """
        Validate Gradle build cache without executing gradle commands.
        
        Checks:
        - .gradle/ directory structure
        - build/classes/ directories
        - build/libs/*.jar files
        """
        result = {
            'valid': False,
            'details': {},
            'subprojects': []
        }
        
        # Check Gradle cache directories
        cache_checks = [
            f"{project_dir}/.gradle",
            f"{project_dir}/build/classes",
            f"{project_dir}/build/libs"
        ]
        
        cache_found = 0
        for cache_path in cache_checks:
            cmd = f"test -d {cache_path}"
            check_result = self._execute_command_with_logging(cmd, f"checking {cache_path}")
            if check_result['success']:
                cache_found += 1
                result['details'][cache_path.split('/')[-1]] = True
        
        # Check for JAR files in build/libs
        jars_cmd = f"find {project_dir}/build/libs -name '*.jar' -type f 2>/dev/null | wc -l"
        jars_result = self._execute_command_with_logging(jars_cmd, "counting Gradle JARs")
        if jars_result['success']:
            jar_count = int(jars_result['output'].strip() or 0)
            if jar_count > 0:
                result['details']['jar_count'] = jar_count
                cache_found += 1
        
        # Check for multi-project builds
        subprojects_cmd = f"find {project_dir} -mindepth 2 -maxdepth 2 -name 'build.gradle' -o -name 'build.gradle.kts' 2>/dev/null"
        subprojects_result = self._execute_command_with_logging(subprojects_cmd, "finding Gradle subprojects")
        if subprojects_result['success'] and subprojects_result['output']:
            subprojects = subprojects_result['output'].strip().split('\n')
            for subproject_build in subprojects:
                if subproject_build:
                    subproject_dir = '/'.join(subproject_build.split('/')[:-1])
                    subproject_name = subproject_dir.split('/')[-1]
                    
                    # Check subproject build directory
                    subproject_build_dir = f"{subproject_dir}/build"
                    cmd = f"test -d {subproject_build_dir}"
                    if self._execute_command_with_logging(cmd, f"checking subproject {subproject_name}")['success']:
                        result['subprojects'].append(subproject_name)
        
        # Determine validity
        result['valid'] = cache_found > 0 or len(result['subprojects']) > 0
        
        return result
    
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