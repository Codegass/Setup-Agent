"""
Output Search Tool

This tool allows agents to search and retrieve full outputs that were truncated
in the context files.
"""

from typing import Dict, Any, Optional, List
from loguru import logger
from pathlib import Path

from tools.base import BaseTool, ToolResult, ToolError
from agent.output_storage import OutputStorageManager


class OutputSearchTool(BaseTool):
    """Tool for searching and retrieving full outputs from storage with grep-like capabilities."""

    def __init__(self, orchestrator=None, contexts_dir: Optional[Path] = None):
        """
        Initialize the output search tool.

        Args:
            orchestrator: Docker orchestrator for container file operations
            contexts_dir: Directory containing the output storage files
        """
        super().__init__(
            name="output_search",
            description="Intelligently search and retrieve outputs with grep-like pattern matching"
        )
        self.orchestrator = orchestrator
        self.contexts_dir = contexts_dir or Path("/workspace/.setup_agent/contexts")
        self.storage_manager = OutputStorageManager(self.contexts_dir, orchestrator=self.orchestrator)
    
    def execute(
        self,
        action: str = "search",
        ref_id: Optional[str] = None,
        pattern: Optional[str] = None,
        grep_pattern: Optional[str] = None,
        context_lines: int = 2,
        head_lines: int = 50,
        tail_lines: int = 50,
        task_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 10,
        show_line_numbers: bool = False,
        extreme: bool = False
    ) -> ToolResult:
        """
        Execute intelligent output search operations.

        Args:
            action: The action to perform:
                - 'search': Search for outputs and show preview (first/last 100 chars)
                - 'preview': Show first N and last M lines of an output
                - 'grep': Search within output using pattern with context lines
                - 'retrieve': Get output (auto-truncates to 8000 chars, or 12000 with extreme=True)
                - 'list': List available outputs with metadata
            ref_id: Reference ID for specific output operations
            pattern: Regex pattern to search across all outputs (for 'search' action)
            grep_pattern: Regex pattern to search within a specific output (for 'grep' action)
            context_lines: Number of lines before and after matches to show (like grep -C)
            head_lines: Number of lines from beginning to show (for 'preview' action)
            tail_lines: Number of lines from end to show (for 'preview' action)
            task_id: Filter by task ID
            tool_name: Filter by tool name
            limit: Maximum number of search results or grep matches
            show_line_numbers: Whether to show line numbers in output
            extreme: For 'retrieve' action, allow up to 12000 chars instead of 8000 (use cautiously)

        Returns:
            ToolResult with search results or retrieved output
        """
        # The base class now handles parameter validation automatically
        
        try:
            if action == "retrieve":
                return self._retrieve_output(ref_id, extreme=extreme)
            elif action == "search":
                return self._search_outputs(pattern, task_id, tool_name, limit)
            elif action == "preview":
                return self._preview_output(ref_id, head_lines, tail_lines, show_line_numbers)
            elif action == "grep":
                return self._grep_output(ref_id, grep_pattern, context_lines, limit, show_line_numbers)
            elif action == "list":
                return self._list_outputs(task_id, tool_name, limit)
            else:
                raise ToolError(
                    message=f"Unknown action: {action}. Use 'search', 'retrieve', 'preview', 'grep', or 'list'",
                    category="validation",
                    error_code="INVALID_ACTION",
                    suggestions=[
                        "Use action='search' to search for patterns across all outputs",
                        "Use action='retrieve' to get a specific output by reference ID",
                        "Use action='preview' to show head/tail of an output",
                        "Use action='grep' to search within a specific output",
                        "Use action='list' to list available outputs"
                    ],
                    details={"provided_action": action, "valid_actions": ["search", "retrieve", "preview", "grep", "list"]},
                    retryable=True
                )
        except ToolError:
            # Re-raise ToolErrors without wrapping them
            raise
        except Exception as e:
            logger.error(f"Output search tool error: {e}")
            raise ToolError(
                message=f"Error during output search: {str(e)}",
                category="system",
                error_code="SEARCH_ERROR",
                details={"exception_type": type(e).__name__},
                retryable=False
            )
    
    def _retrieve_output(self, ref_id: Optional[str], extreme: bool = False) -> ToolResult:
        """Retrieve a specific output by reference ID with smart truncation."""
        if not ref_id:
            raise ToolError(
                message="Reference ID is required for retrieve action",
                category="validation",
                error_code="MISSING_REF_ID",
                suggestions=["Provide the 'ref_id' parameter to retrieve a specific output"],
                retryable=True
            )

        output = self.storage_manager.retrieve_output(ref_id)
        if output:
            original_length = len(output)
            # Apply smart truncation
            max_chars = 12000 if extreme else 8000
            if len(output) > max_chars:
                # Truncate intelligently at line boundaries
                lines = output.split('\n')
                truncated = []
                current_length = 0

                # Try to include complete lines
                for line in lines:
                    if current_length + len(line) + 1 > max_chars:
                        break
                    truncated.append(line)
                    current_length += len(line) + 1

                output = '\n'.join(truncated)
                truncation_note = f"\n\n[Output truncated to {max_chars} chars. Original: {original_length} chars]"
                output += truncation_note

            return ToolResult(
                success=True,
                output=f"📄 Full output for {ref_id}:\n\n{output}",
                metadata={"original_length": original_length, "truncated": original_length > max_chars}
            )
        else:
            return ToolResult(
                success=False,
                output=f"No output found with reference ID: {ref_id}"
            )
    
    def _search_outputs(
        self,
        pattern: Optional[str],
        task_id: Optional[str],
        tool_name: Optional[str],
        limit: int
    ) -> ToolResult:
        """Search outputs matching criteria."""
        results = self.storage_manager.search_outputs(
            pattern=pattern,
            task_id=task_id,
            tool_name=tool_name,
            limit=limit
        )
        
        if not results:
            return ToolResult(
                success=True,
                output="No matching outputs found"
            )
        
        output_lines = [f"🔍 Found {len(results)} matching outputs:\n"]
        
        for i, result in enumerate(results, 1):
            output_lines.append(f"\n{i}. Reference: {result['ref_id']}")
            output_lines.append(f"   Task: {result.get('task_id', 'N/A')}")
            output_lines.append(f"   Tool: {result.get('tool_name', 'N/A')}")
            output_lines.append(f"   Timestamp: {result.get('timestamp', 'N/A')}")
            output_lines.append(f"   Length: {result.get('output_length', 0)} chars")
            
            if pattern and 'match_count' in result:
                output_lines.append(f"   Matches: {result['match_count']}")
                if 'snippet' in result:
                    snippet = result['snippet'][:200] if len(result['snippet']) > 200 else result['snippet']
                    output_lines.append(f"   Snippet: ...{snippet}...")
            else:
                if 'first_100' in result:
                    output_lines.append(f"   Beginning: {result['first_100'][:100]}...")
                if 'last_100' in result:
                    output_lines.append(f"   Ending: ...{result['last_100'][-100:]}")
        
        output_lines.append(f"\n💡 Use action='retrieve' with ref_id to get full output")
        
        return ToolResult(
            success=True,
            output="\n".join(output_lines)
        )
    
    def _list_outputs(
        self,
        task_id: Optional[str],
        tool_name: Optional[str],
        limit: int
    ) -> ToolResult:
        """List stored outputs with metadata."""
        return self._search_outputs(
            pattern=None,
            task_id=task_id,
            tool_name=tool_name,
            limit=limit
        )

    def _preview_output(
        self,
        ref_id: Optional[str],
        head_lines: int = 50,
        tail_lines: int = 50,
        show_line_numbers: bool = False
    ) -> ToolResult:
        """Preview the beginning and end of an output."""
        if not ref_id:
            raise ToolError(
                message="Reference ID is required for preview action",
                category="validation",
                error_code="MISSING_REF_ID",
                suggestions=["Provide the 'ref_id' parameter to preview a specific output"],
                retryable=True
            )

        output = self.storage_manager.retrieve_output(ref_id)
        if not output:
            return ToolResult(
                success=False,
                output=f"No output found with reference ID: {ref_id}"
            )

        lines = output.split('\n')
        total_lines = len(lines)

        result_lines = [f"📄 Preview of {ref_id} ({total_lines} total lines):\n"]

        # Head section
        if head_lines > 0:
            result_lines.append(f"=== First {min(head_lines, total_lines)} lines ===")
            for i, line in enumerate(lines[:head_lines], 1):
                if show_line_numbers:
                    result_lines.append(f"{i:6}: {line}")
                else:
                    result_lines.append(line)

        # Add separator if showing both head and tail
        if head_lines > 0 and tail_lines > 0 and total_lines > head_lines + tail_lines:
            result_lines.append(f"\n... ({total_lines - head_lines - tail_lines} lines omitted) ...\n")

        # Tail section
        if tail_lines > 0 and total_lines > head_lines:
            result_lines.append(f"=== Last {min(tail_lines, total_lines)} lines ===")
            start_line = max(total_lines - tail_lines, head_lines) if head_lines > 0 else total_lines - tail_lines
            for i, line in enumerate(lines[start_line:], start_line + 1):
                if show_line_numbers:
                    result_lines.append(f"{i:6}: {line}")
                else:
                    result_lines.append(line)

        return ToolResult(
            success=True,
            output="\n".join(result_lines),
            metadata={"total_lines": total_lines, "head_shown": min(head_lines, total_lines),
                     "tail_shown": min(tail_lines, total_lines)}
        )

    def _grep_output(
        self,
        ref_id: Optional[str],
        grep_pattern: Optional[str],
        context_lines: int = 2,
        limit: int = 10,
        show_line_numbers: bool = True
    ) -> ToolResult:
        """Search within a specific output with grep-like functionality."""
        if not ref_id:
            raise ToolError(
                message="Reference ID is required for grep action",
                category="validation",
                error_code="MISSING_REF_ID",
                suggestions=["Provide the 'ref_id' parameter to grep within a specific output"],
                retryable=True
            )

        if not grep_pattern:
            raise ToolError(
                message="Pattern is required for grep action",
                category="validation",
                error_code="MISSING_PATTERN",
                suggestions=["Provide the 'grep_pattern' parameter to search within the output"],
                retryable=True
            )

        output = self.storage_manager.retrieve_output(ref_id)
        if not output:
            return ToolResult(
                success=False,
                output=f"No output found with reference ID: {ref_id}"
            )

        try:
            import re
            regex = re.compile(grep_pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise ToolError(
                message=f"Invalid regex pattern: {e}",
                category="validation",
                error_code="INVALID_REGEX",
                suggestions=["Check your regex pattern syntax"],
                retryable=True
            )

        lines = output.split('\n')
        matches = []

        # Find all matching lines with their line numbers
        for i, line in enumerate(lines):
            if regex.search(line):
                matches.append(i)
                if len(matches) >= limit:
                    break

        if not matches:
            return ToolResult(
                success=True,
                output=f"No matches found for pattern '{grep_pattern}' in {ref_id}"
            )

        # Build result with context lines
        result_lines = [f"🔍 Grep results for '{grep_pattern}' in {ref_id}:"]
        result_lines.append(f"Found {len(matches)} matches (showing up to {limit}):\n")

        shown_lines = set()
        for match_idx in matches:
            # Calculate context range
            start = max(0, match_idx - context_lines)
            end = min(len(lines), match_idx + context_lines + 1)

            # Add separator between match groups
            if shown_lines and min(range(start, end)) > max(shown_lines) + 1:
                result_lines.append("---")

            # Add lines with context
            for i in range(start, end):
                if i not in shown_lines:
                    shown_lines.add(i)
                    line_marker = ">>>" if i == match_idx else "   "
                    if show_line_numbers:
                        result_lines.append(f"{line_marker} {i+1:6}: {lines[i]}")
                    else:
                        result_lines.append(f"{line_marker} {lines[i]}")

        # Add summary
        if len(matches) > limit:
            result_lines.append(f"\n... ({len(matches) - limit} more matches not shown)")

        return ToolResult(
            success=True,
            output="\n".join(result_lines),
            metadata={
                "total_matches": len(matches),
                "matches_shown": min(len(matches), limit),
                "context_lines": context_lines
            }
        )
    
    def get_parameter_schema(self) -> Dict[str, Any]:
        """Return the parameter schema for function calling."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "retrieve", "preview", "grep", "list"],
                    "description": "The action to perform: search (find outputs), retrieve (get full), preview (head/tail), grep (search within), list (show all)",
                    "default": "search"
                },
                "ref_id": {
                    "type": "string",
                    "description": "Reference ID for specific output operations (required for retrieve/preview/grep)"
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search across all outputs (for 'search' action)"
                },
                "grep_pattern": {
                    "type": "string",
                    "description": "Regex pattern to search within a specific output (for 'grep' action)"
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of lines before and after matches to show (like grep -C)",
                    "default": 2
                },
                "head_lines": {
                    "type": "integer",
                    "description": "Number of lines from beginning to show (for 'preview' action)",
                    "default": 50
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Number of lines from end to show (for 'preview' action)",
                    "default": 50
                },
                "task_id": {
                    "type": "string",
                    "description": "Filter by task ID"
                },
                "tool_name": {
                    "type": "string",
                    "description": "Filter by tool name (e.g., 'maven', 'gradle', 'bash')"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of search results or grep matches",
                    "default": 10
                },
                "show_line_numbers": {
                    "type": "boolean",
                    "description": "Whether to show line numbers in output",
                    "default": False
                },
                "extreme": {
                    "type": "boolean",
                    "description": "For 'retrieve' action, allow up to 12000 chars instead of 8000 (use cautiously)",
                    "default": False
                }
            },
            "required": []
        }