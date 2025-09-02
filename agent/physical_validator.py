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
        
        # Cache for validation results with TTL
        self.validation_cache = {}
        self.cache_timestamps = {}
        self.cache_ttl = 60  # 60 seconds TTL
        self.last_validation = None
        
        logger.info(f"PhysicalValidator initialized for project at: {project_path}")
    
    def _get_cache_key(self, operation: str, *args) -> str:
        """Generate cache key for operation and arguments."""
        return f"{operation}:{':'.join(str(arg) for arg in args)}"
    
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
            
            # Get sample paths
            paths_cmd = f"find {project_dir} -name '*.class' -type f 2>/dev/null | head -10"
            paths_result = self._execute_command_with_logging(paths_cmd, "class file paths")
            
            paths = []
            if paths_result["success"] and paths_result["output"]:
                paths = [p.strip() for p in paths_result["output"].split("\n") if p.strip()]
            
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
            # Try GNU stat first (Linux/container environments)
            cmd = f"find {project_dir} -name '*.class' -type f -exec stat -c '%Y' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result = self._execute_command_with_logging(cmd, "GNU stat compilation check")
            
            if result["success"] and result["output"].strip():
                try:
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
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to parse GNU stat output: {e}")
            
            # Fallback to BSD stat (macOS)
            logger.debug("GNU stat failed, trying BSD stat")
            cmd_bsd = f"find {project_dir} -name '*.class' -type f -exec stat -f '%m' {{}} \\; 2>/dev/null | sort -rn | head -1"
            result_bsd = self._execute_command_with_logging(cmd_bsd, "BSD stat compilation check")
            
            if result_bsd["success"] and result_bsd["output"].strip():
                try:
                    newest_timestamp = int(result_bsd["output"].strip())
                    current_timestamp = int(datetime.now().timestamp())
                    
                    age_seconds = current_timestamp - newest_timestamp
                    recent = age_seconds < 3600  # 1 hour
                    
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
            # Get list of .class files with detailed info
            cmd = f"find {project_dir} -name '*.class' -type f -exec ls -la {{}} \\; 2>/dev/null | head -100"
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
                recent = age_seconds < 3600  # 1 hour
                
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
            # Find all test report XML files
            find_cmd = f"find {project_dir} -type f \\( -path '*/surefire-reports/*.xml' -o -path '*/test-results/*.xml' \\) 2>/dev/null"
            result = self.docker_orchestrator.execute_command(find_cmd)
            
            if result.get("exit_code") != 0:
                test_result["error"] = "Failed to find test reports"
                return test_result
            
            report_files = [f.strip() for f in result.get("output", "").split("\n") if f.strip()]
            test_result["report_files"] = report_files
            
            if not report_files:
                test_result["error"] = "No test report files found"
                return test_result
            
            # Parse each XML file
            for report_file in report_files[:20]:  # Limit to first 20 files to avoid timeout
                try:
                    # Read the XML content
                    cat_cmd = f"cat '{report_file}'"
                    xml_result = self.docker_orchestrator.execute_command(cat_cmd)
                    
                    if xml_result.get("exit_code") != 0:
                        test_result["parsing_errors"].append(f"Failed to read {report_file}")
                        continue
                    
                    xml_content = xml_result.get("output", "")
                    if not xml_content.strip():
                        continue
                    
                    # Parse XML based on format
                    stats = self._parse_single_test_xml(xml_content, report_file)
                    if stats:
                        test_result["total_tests"] += stats.get("total", 0)
                        test_result["passed_tests"] += stats.get("passed", 0)
                        test_result["failed_tests"] += stats.get("failed", 0)
                        test_result["error_tests"] += stats.get("errors", 0)
                        test_result["skipped_tests"] += stats.get("skipped", 0)
                    else:
                        # XML parsing failed - record the error
                        test_result["parsing_errors"].append(f"Failed to parse XML structure in {report_file}")
                    
                except Exception as e:
                    test_result["parsing_errors"].append(f"Error parsing {report_file}: {str(e)}")
                    logger.warning(f"Failed to parse test report {report_file}: {e}")
            
            # Determine test success: only if no failures and no errors
            test_result["test_success"] = (
                test_result["failed_tests"] == 0 and 
                test_result["error_tests"] == 0 and
                test_result["total_tests"] > 0
            )
            test_result["valid"] = True
            
            logger.info(f"📊 Test report analysis: {test_result['total_tests']} total, "
                       f"{test_result['passed_tests']} passed, {test_result['failed_tests']} failed, "
                       f"{test_result['error_tests']} errors, {test_result['skipped_tests']} skipped")
            
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
            return None
        except (ValueError, AttributeError) as e:
            logger.warning(f"Data extraction error in {file_path}: {e}")
            return None
    
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