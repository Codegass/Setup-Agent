"""
Command Tracker for Build and Test Validation

This module tracks all build and test commands executed by the agent,
enabling fact-based validation through command replay.
"""

import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from loguru import logger


class CommandTracker:
    """
    Tracks all build and test commands for replay validation.
    
    This eliminates inference-based status determination by storing
    exact commands that can be re-executed to verify actual results.
    """
    
    def __init__(self, docker_orchestrator=None, project_name: str = None):
        """
        Initialize command tracker.
        
        Args:
            docker_orchestrator: Docker orchestrator for command execution
            project_name: Name of the project being tracked
        """
        self.docker_orchestrator = docker_orchestrator
        self.project_name = project_name
        
        # Command storage
        self.build_commands: List[Dict[str, Any]] = []
        self.test_commands: List[Dict[str, Any]] = []
        self.command_results: Dict[str, Any] = {}
        
        # Track last successful commands
        self.last_successful_build: Optional[str] = None
        self.last_successful_test: Optional[str] = None
        
        logger.info(f"CommandTracker initialized for project: {project_name}")
    
    def track_build_command(self, command: str, tool: str, working_dir: str = "/workspace", 
                          exit_code: int = None, output: str = None) -> None:
        """
        Store build command for later validation.
        
        Args:
            command: The exact command executed
            tool: Tool that executed it (maven, gradle, bash)
            working_dir: Working directory for the command
            exit_code: Exit code of the command
            output: Command output
        """
        entry = {
            "command": command,
            "tool": tool,
            "working_dir": working_dir,
            "timestamp": datetime.now().isoformat(),
            "exit_code": exit_code,
            "output_snippet": output[:500] if output else None,  # Store snippet
            "build_success": self._detect_build_success(tool, output) if output else None
        }
        
        self.build_commands.append(entry)
        
        # Track last successful build command
        if entry.get("build_success"):
            self.last_successful_build = command
            logger.debug(f"Tracked successful build command: {command[:100]}...")
        else:
            logger.debug(f"Tracked failed build command: {command[:100]}...")
    
    def track_test_command(self, command: str, tool: str, working_dir: str = "/workspace",
                         exit_code: int = None, output: str = None) -> None:
        """
        Store test command for later validation.
        
        Args:
            command: The exact command executed
            tool: Tool that executed it (maven, gradle, bash)
            working_dir: Working directory for the command
            exit_code: Exit code of the command
            output: Command output
        """
        entry = {
            "command": command,
            "tool": tool,
            "working_dir": working_dir,
            "timestamp": datetime.now().isoformat(),
            "exit_code": exit_code,
            "output_snippet": output[:500] if output else None,
            "test_stats": self._extract_test_stats(tool, output) if output else None
        }
        
        self.test_commands.append(entry)
        
        # Track last successful test command
        if exit_code == 0 and self._detect_test_success(tool, output):
            self.last_successful_test = command
            logger.debug(f"Tracked successful test command: {command[:100]}...")
    
    def _detect_build_success(self, tool: str, output: str) -> bool:
        """
        Detect if build actually succeeded based on output.
        
        Args:
            tool: Build tool used
            output: Command output
            
        Returns:
            True if build succeeded, False otherwise
        """
        if not output:
            return False
            
        output_lower = output.lower()
        
        if tool == "maven":
            # Maven success markers
            return "BUILD SUCCESS" in output and "BUILD FAILURE" not in output
        elif tool == "gradle":
            # Gradle success markers
            return "BUILD SUCCESSFUL" in output and "BUILD FAILED" not in output
        elif tool == "bash":
            # For bash, check for common build success patterns
            return ("build successful" in output_lower or 
                   "compilation successful" in output_lower) and \
                   "error" not in output_lower
        
        return False
    
    def _detect_test_success(self, tool: str, output: str) -> bool:
        """
        Detect if tests actually succeeded based on output.
        
        Args:
            tool: Test tool used
            output: Command output
            
        Returns:
            True if tests passed, False otherwise
        """
        if not output:
            return False
            
        if tool == "maven":
            # Check for test failures
            return "BUILD SUCCESS" in output and \
                   "Tests run:" in output and \
                   "Failures: 0" in output
        elif tool == "gradle":
            # Check for test failures
            return "BUILD SUCCESSFUL" in output and \
                   ("0 failed" in output or "tests passed" in output.lower())
        
        return False
    
    def _extract_test_stats(self, tool: str, output: str) -> Dict[str, int]:
        """
        Extract test statistics from output.
        
        Args:
            tool: Test tool used
            output: Command output
            
        Returns:
            Dictionary with test statistics
        """
        import re
        
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        
        if not output:
            return stats
        
        if tool == "maven":
            # Maven pattern: Tests run: X, Failures: Y, Errors: Z, Skipped: W
            match = re.search(
                r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)',
                output
            )
            if match:
                total = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                
                stats = {
                    "total": total,
                    "passed": total - failures - errors - skipped,
                    "failed": failures + errors,
                    "skipped": skipped
                }
        elif tool == "gradle":
            # Gradle pattern: X tests completed, Y failed
            match = re.search(r'(\d+)\s+tests?\s+completed.*?(\d+)\s+failed', output)
            if match:
                total = int(match.group(1))
                failed = int(match.group(2))
                stats = {
                    "total": total,
                    "passed": total - failed,
                    "failed": failed,
                    "skipped": 0
                }
        
        return stats
    
    def get_last_build_command(self) -> Optional[Dict[str, Any]]:
        """Get the last build command executed."""
        return self.build_commands[-1] if self.build_commands else None
    
    def get_last_test_command(self) -> Optional[Dict[str, Any]]:
        """Get the last test command executed."""
        return self.test_commands[-1] if self.test_commands else None
    
    def get_all_build_commands(self) -> List[Dict[str, Any]]:
        """Get all build commands executed."""
        return self.build_commands
    
    def get_all_test_commands(self) -> List[Dict[str, Any]]:
        """Get all test commands executed."""
        return self.test_commands
    
    def replay_last_build(self) -> Dict[str, Any]:
        """
        Replay the last build command and return actual result.
        
        Returns:
            Dictionary with replay results
        """
        if not self.build_commands or not self.docker_orchestrator:
            return {"success": False, "error": "No build commands to replay"}
        
        last_build = self.build_commands[-1]
        command = last_build["command"]
        working_dir = last_build.get("working_dir", "/workspace")
        
        logger.info(f"Replaying build command: {command[:100]}...")
        
        try:
            # Execute the command
            result = self.docker_orchestrator.execute_command(
                f"cd {working_dir} && {command}"
            )
            
            output = result.get("output", "")
            exit_code = result.get("exit_code", 1)
            
            # Determine actual success
            build_success = self._detect_build_success(last_build["tool"], output)
            
            return {
                "success": build_success,
                "exit_code": exit_code,
                "command": command,
                "output_snippet": output[:500] if output else None,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Failed to replay build command: {e}")
            return {"success": False, "error": str(e)}
    
    def replay_all_tests(self) -> Dict[str, Any]:
        """
        Replay all test commands and return aggregated results.
        
        Returns:
            Dictionary with test replay results
        """
        if not self.test_commands or not self.docker_orchestrator:
            return {"success": False, "error": "No test commands to replay"}
        
        total_stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        successful_replays = 0
        failed_replays = 0
        
        for test_cmd in self.test_commands:
            command = test_cmd["command"]
            working_dir = test_cmd.get("working_dir", "/workspace")
            
            logger.info(f"Replaying test command: {command[:100]}...")
            
            try:
                result = self.docker_orchestrator.execute_command(
                    f"cd {working_dir} && {command}"
                )
                
                output = result.get("output", "")
                exit_code = result.get("exit_code", 1)
                
                # Extract test statistics
                stats = self._extract_test_stats(test_cmd["tool"], output)
                
                # Aggregate statistics
                total_stats["total"] += stats["total"]
                total_stats["passed"] += stats["passed"]
                total_stats["failed"] += stats["failed"]
                total_stats["skipped"] += stats["skipped"]
                
                if self._detect_test_success(test_cmd["tool"], output):
                    successful_replays += 1
                else:
                    failed_replays += 1
                    
            except Exception as e:
                logger.error(f"Failed to replay test command: {e}")
                failed_replays += 1
        
        return {
            "success": failed_replays == 0 and total_stats["failed"] == 0,
            "total_commands": len(self.test_commands),
            "successful_replays": successful_replays,
            "failed_replays": failed_replays,
            "test_stats": total_stats,
            "timestamp": datetime.now().isoformat()
        }
    
    def save_to_file(self, filepath: str) -> None:
        """
        Save tracked commands to a JSON file for analysis.
        
        Args:
            filepath: Path to save the JSON file
        """
        data = {
            "project": self.project_name,
            "timestamp": datetime.now().isoformat(),
            "build_commands": self.build_commands,
            "test_commands": self.test_commands,
            "last_successful_build": self.last_successful_build,
            "last_successful_test": self.last_successful_test
        }
        
        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved command tracking data to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save command tracking data: {e}")
    
    def load_from_file(self, filepath: str) -> None:
        """
        Load tracked commands from a JSON file.
        
        Args:
            filepath: Path to load the JSON file from
        """
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            self.project_name = data.get("project")
            self.build_commands = data.get("build_commands", [])
            self.test_commands = data.get("test_commands", [])
            self.last_successful_build = data.get("last_successful_build")
            self.last_successful_test = data.get("last_successful_test")
            
            logger.info(f"Loaded command tracking data from {filepath}")
        except Exception as e:
            logger.error(f"Failed to load command tracking data: {e}")