#!/usr/bin/env python3
"""
Centralized Error Logger for Setup-Agent

This module provides a unified error logging system that captures:
- ToolErrors with full metadata (category, suggestions, retryable status)
- Unknown tool attempts with feedback provided
- Execution failures with context
- Error patterns for analysis and reporting
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from enum import Enum

from loguru import logger


class ErrorType(str, Enum):
    """Types of errors we track."""
    TOOL_ERROR = "tool_error"
    UNKNOWN_TOOL = "unknown_tool"
    VALIDATION_ERROR = "validation_error"
    EXECUTION_ERROR = "execution_error"
    SYSTEM_ERROR = "system_error"
    TIMEOUT_ERROR = "timeout_error"
    RECOVERY_FAILED = "recovery_failed"


class ErrorLogger:
    """Centralized error logger for all agent operations."""
    
    _instance = None
    _initialized = False
    
    def __new__(cls, *args, **kwargs):
        """Singleton pattern to ensure one error logger."""
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, workspace_path: str = "/workspace", session_id: Optional[str] = None):
        """Initialize the error logger.
        
        Args:
            workspace_path: Path to the workspace directory
            session_id: Optional session ID for grouping errors
        """
        # Prevent re-initialization
        if ErrorLogger._initialized:
            return
            
        self.workspace_path = Path(workspace_path)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create error log directory
        self.error_dir = self.workspace_path / ".setup_agent" / "errors"
        self.error_dir.mkdir(parents=True, exist_ok=True)
        
        # Error log file in JSONL format for easy parsing
        self.error_log_file = self.error_dir / f"errors_{self.session_id}.jsonl"
        
        # Summary statistics
        self.error_counts: Dict[str, int] = {}
        self.tool_error_categories: Dict[str, int] = {}
        self.unknown_tools_attempted: List[str] = []
        
        ErrorLogger._initialized = True
        logger.info(f"ErrorLogger initialized: {self.error_log_file}")
    
    def log_tool_error(
        self,
        tool_name: str,
        error_message: str,
        category: str = "execution",
        error_code: Optional[str] = None,
        suggestions: Optional[List[str]] = None,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a ToolError with full metadata.
        
        Args:
            tool_name: Name of the tool that failed
            error_message: Error message
            category: Error category (validation, execution, system)
            error_code: Optional error code
            suggestions: List of suggestions for recovery
            retryable: Whether the error is retryable
            details: Additional error details
            context: Execution context when error occurred
        """
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": ErrorType.TOOL_ERROR,
            "tool_name": tool_name,
            "error_message": error_message,
            "category": category,
            "error_code": error_code,
            "suggestions": suggestions or [],
            "retryable": retryable,
            "details": details or {},
            "context": context or {},
            "session_id": self.session_id
        }
        
        self._write_error(error_entry)
        
        # Update statistics
        self.error_counts[ErrorType.TOOL_ERROR] = self.error_counts.get(ErrorType.TOOL_ERROR, 0) + 1
        self.tool_error_categories[category] = self.tool_error_categories.get(category, 0) + 1
        
        logger.debug(f"Logged tool error: {tool_name} - {category} - {error_message[:100]}")
    
    def log_unknown_tool(
        self,
        requested_tool: str,
        suggested_tool: Optional[str] = None,
        feedback_provided: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log an unknown tool attempt.
        
        Args:
            requested_tool: The tool name that was requested but doesn't exist
            suggested_tool: The tool that was suggested as alternative
            feedback_provided: The full feedback message provided to agent
            context: Execution context when attempt was made
        """
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": ErrorType.UNKNOWN_TOOL,
            "requested_tool": requested_tool,
            "suggested_tool": suggested_tool,
            "feedback_provided": feedback_provided,
            "context": context or {},
            "session_id": self.session_id
        }
        
        self._write_error(error_entry)
        
        # Update statistics
        self.error_counts[ErrorType.UNKNOWN_TOOL] = self.error_counts.get(ErrorType.UNKNOWN_TOOL, 0) + 1
        self.unknown_tools_attempted.append(requested_tool)
        
        logger.debug(f"Logged unknown tool attempt: {requested_tool} -> {suggested_tool}")
    
    def log_validation_error(
        self,
        tool_name: str,
        parameter: str,
        error_message: str,
        provided_value: Any = None,
        expected_type: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a parameter validation error.
        
        Args:
            tool_name: Name of the tool
            parameter: Parameter that failed validation
            error_message: Validation error message
            provided_value: The value that was provided
            expected_type: The expected type/format
            context: Execution context
        """
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": ErrorType.VALIDATION_ERROR,
            "tool_name": tool_name,
            "parameter": parameter,
            "error_message": error_message,
            "provided_value": str(provided_value) if provided_value is not None else None,
            "expected_type": expected_type,
            "context": context or {},
            "session_id": self.session_id
        }
        
        self._write_error(error_entry)
        
        # Update statistics
        self.error_counts[ErrorType.VALIDATION_ERROR] = self.error_counts.get(ErrorType.VALIDATION_ERROR, 0) + 1
        
        logger.debug(f"Logged validation error: {tool_name}.{parameter}")
    
    def log_execution_error(
        self,
        operation: str,
        error_message: str,
        error_type: str = ErrorType.EXECUTION_ERROR,
        stack_trace: Optional[str] = None,
        recovery_attempted: bool = False,
        recovery_successful: bool = False,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log a general execution error.
        
        Args:
            operation: The operation that failed
            error_message: Error message
            error_type: Type of error from ErrorType enum
            stack_trace: Optional stack trace
            recovery_attempted: Whether recovery was attempted
            recovery_successful: Whether recovery succeeded
            context: Execution context
        """
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": error_type,
            "operation": operation,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "recovery_attempted": recovery_attempted,
            "recovery_successful": recovery_successful,
            "context": context or {},
            "session_id": self.session_id
        }
        
        self._write_error(error_entry)
        
        # Update statistics
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1
        
        if recovery_attempted and not recovery_successful:
            self.error_counts[ErrorType.RECOVERY_FAILED] = self.error_counts.get(ErrorType.RECOVERY_FAILED, 0) + 1
        
        logger.debug(f"Logged execution error: {operation} - {error_type}")
    
    def _write_error(self, error_entry: Dict[str, Any]) -> None:
        """Write error entry to the log file.
        
        Args:
            error_entry: Error entry dictionary
        """
        try:
            with open(self.error_log_file, "a") as f:
                f.write(json.dumps(error_entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write to error log: {e}")
    
    def get_error_summary(self) -> Dict[str, Any]:
        """Get summary of all errors logged.
        
        Returns:
            Dictionary with error statistics and patterns
        """
        return {
            "session_id": self.session_id,
            "total_errors": sum(self.error_counts.values()),
            "error_counts_by_type": self.error_counts.copy(),
            "tool_error_categories": self.tool_error_categories.copy(),
            "unknown_tools_attempted": list(set(self.unknown_tools_attempted)),
            "unknown_tools_count": len(self.unknown_tools_attempted),
            "recovery_failure_rate": self._calculate_recovery_failure_rate()
        }
    
    def _calculate_recovery_failure_rate(self) -> float:
        """Calculate the rate of failed recovery attempts.
        
        Returns:
            Recovery failure rate as a percentage
        """
        recovery_failed = self.error_counts.get(ErrorType.RECOVERY_FAILED, 0)
        total_errors = sum(self.error_counts.values())
        
        if total_errors == 0:
            return 0.0
        
        return (recovery_failed / total_errors) * 100
    
    def get_errors_for_analysis(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read all errors from the log file for analysis.
        
        Args:
            limit: Optional limit on number of errors to return
        
        Returns:
            List of error entries
        """
        errors = []
        
        if not self.error_log_file.exists():
            return errors
        
        try:
            with open(self.error_log_file, "r") as f:
                for line in f:
                    if line.strip():
                        errors.append(json.loads(line))
                        if limit and len(errors) >= limit:
                            break
        except Exception as e:
            logger.error(f"Failed to read error log: {e}")
        
        return errors
    
    def generate_error_report(self) -> str:
        """Generate a markdown report of errors.
        
        Returns:
            Markdown-formatted error report
        """
        summary = self.get_error_summary()
        errors = self.get_errors_for_analysis(limit=50)  # Last 50 errors for detail
        
        report = ["## Error Analysis Report\n"]
        report.append(f"**Session ID:** {summary['session_id']}\n")
        report.append(f"**Total Errors:** {summary['total_errors']}\n")
        
        # Error breakdown
        report.append("### Error Type Breakdown\n")
        if summary['error_counts_by_type']:
            for error_type, count in sorted(summary['error_counts_by_type'].items(), key=lambda x: x[1], reverse=True):
                percentage = (count / summary['total_errors']) * 100 if summary['total_errors'] > 0 else 0
                report.append(f"- **{error_type}**: {count} ({percentage:.1f}%)")
        else:
            report.append("No errors recorded.")
        report.append("")
        
        # Tool error categories
        if summary['tool_error_categories']:
            report.append("### Tool Error Categories\n")
            for category, count in sorted(summary['tool_error_categories'].items(), key=lambda x: x[1], reverse=True):
                report.append(f"- **{category}**: {count}")
            report.append("")
        
        # Unknown tools
        if summary['unknown_tools_attempted']:
            report.append("### Unknown Tools Attempted\n")
            report.append(f"Total unique unknown tools: {len(summary['unknown_tools_attempted'])}\n")
            for tool in sorted(summary['unknown_tools_attempted']):
                report.append(f"- `{tool}`")
            report.append("")
        
        # Recovery failure rate
        if summary['recovery_failure_rate'] > 0:
            report.append(f"### Recovery Failure Rate: {summary['recovery_failure_rate']:.1f}%\n")
        
        # Recent errors detail
        if errors:
            report.append("### Recent Errors (Last 50)\n")
            
            # Group by type
            errors_by_type: Dict[str, List] = {}
            for error in errors[-50:]:
                error_type = error.get('type', 'unknown')
                if error_type not in errors_by_type:
                    errors_by_type[error_type] = []
                errors_by_type[error_type].append(error)
            
            for error_type, error_list in errors_by_type.items():
                report.append(f"\n#### {error_type} ({len(error_list)} occurrences)\n")
                
                for i, error in enumerate(error_list[:5], 1):  # Show first 5 of each type
                    if error_type == ErrorType.TOOL_ERROR:
                        report.append(f"{i}. **{error.get('tool_name', 'unknown')}** - {error.get('category', 'unknown')}")
                        report.append(f"   - Error: {error.get('error_message', 'No message')[:100]}")
                        if error.get('suggestions'):
                            report.append(f"   - Suggestions: {', '.join(error['suggestions'][:2])}")
                    elif error_type == ErrorType.UNKNOWN_TOOL:
                        report.append(f"{i}. Requested: `{error.get('requested_tool', 'unknown')}` → Suggested: `{error.get('suggested_tool', 'none')}`")
                    else:
                        report.append(f"{i}. {error.get('operation', error.get('tool_name', 'unknown'))}: {error.get('error_message', 'No message')[:100]}")
                
                if len(error_list) > 5:
                    report.append(f"   ... and {len(error_list) - 5} more\n")
        
        return "\n".join(report)
    
    @classmethod
    def get_instance(cls, workspace_path: str = "/workspace", session_id: Optional[str] = None) -> "ErrorLogger":
        """Get or create the singleton instance.
        
        Args:
            workspace_path: Path to workspace
            session_id: Optional session ID
        
        Returns:
            ErrorLogger instance
        """
        if not cls._instance:
            cls._instance = cls(workspace_path, session_id)
        return cls._instance
    
    def reset(self) -> None:
        """Reset error statistics (useful for testing)."""
        self.error_counts.clear()
        self.tool_error_categories.clear()
        self.unknown_tools_attempted.clear()
        logger.debug("Error logger statistics reset")