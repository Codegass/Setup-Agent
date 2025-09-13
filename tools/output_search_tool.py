"""
Output Search Tool

This tool allows agents to search and retrieve full outputs that were truncated
in the context files.
"""

from typing import Dict, Any, Optional, List
from loguru import logger
from pathlib import Path

from tools.base import BaseTool, ToolResult
from agent.output_storage import OutputStorageManager


class OutputSearchTool(BaseTool):
    """Tool for searching and retrieving full outputs from storage."""
    
    def __init__(self, contexts_dir: Optional[Path] = None):
        """
        Initialize the output search tool.
        
        Args:
            contexts_dir: Directory containing the output storage files
        """
        super().__init__(
            name="output_search",
            description="Search and retrieve full outputs that were truncated in context files"
        )
        self.contexts_dir = contexts_dir or Path("/workspace/.setup_agent/contexts")
        self.storage_manager = OutputStorageManager(self.contexts_dir)
    
    def execute(
        self,
        action: str = "search",
        ref_id: Optional[str] = None,
        pattern: Optional[str] = None,
        task_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 10,
        **kwargs
    ) -> ToolResult:
        """
        Execute output search operations.
        
        Args:
            action: The action to perform - "search", "retrieve", or "list"
            ref_id: Reference ID for retrieving specific output
            pattern: Regex pattern to search in outputs
            task_id: Filter by task ID
            tool_name: Filter by tool name
            limit: Maximum number of search results
            
        Returns:
            ToolResult with search results or retrieved output
        """
        
        # Check for unexpected parameters
        if kwargs:
            invalid_params = list(kwargs.keys())
            return ToolResult(
                success=False,
                output=(
                    f"❌ Invalid parameters for output_search tool: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - action (optional): 'search', 'retrieve', or 'list' (default: 'search')\n"
                    f"  - ref_id (optional): Reference ID for 'retrieve' action\n"
                    f"  - pattern (optional): Search pattern for 'search' action\n"
                    f"  - task_id (optional): Filter by task ID\n"
                    f"  - tool_name (optional): Filter by tool name\n"
                    f"  - limit (optional): Maximum results (default: 10)\n\n"
                    f"Example: output_search(action='search', pattern='error')\n"
                    f"Example: output_search(action='retrieve', ref_id='abc123')"
                ),
                error=f"Invalid parameters: {invalid_params}"
            )
        
        try:
            if action == "retrieve":
                return self._retrieve_output(ref_id)
            elif action == "search":
                return self._search_outputs(pattern, task_id, tool_name, limit)
            elif action == "list":
                return self._list_outputs(task_id, tool_name, limit)
            else:
                return ToolResult(
                    success=False,
                    output=f"Unknown action: {action}. Use 'search', 'retrieve', or 'list'"
                )
        except Exception as e:
            logger.error(f"Output search tool error: {e}")
            return ToolResult(
                success=False,
                output=f"Error during output search: {str(e)}"
            )
    
    def _retrieve_output(self, ref_id: Optional[str]) -> ToolResult:
        """Retrieve a specific output by reference ID."""
        if not ref_id:
            return ToolResult(
                success=False,
                output="Reference ID is required for retrieve action"
            )
        
        output = self.storage_manager.retrieve_output(ref_id)
        if output:
            return ToolResult(
                success=True,
                output=f"📄 Full output for {ref_id}:\n\n{output}"
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
    
    def get_parameter_schema(self) -> Dict[str, Any]:
        """Return the parameter schema for function calling."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "retrieve", "list"],
                    "description": "The action to perform",
                    "default": "search"
                },
                "ref_id": {
                    "type": "string",
                    "description": "Reference ID for retrieving specific output (required for 'retrieve' action)"
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search in outputs (for 'search' action)"
                },
                "task_id": {
                    "type": "string",
                    "description": "Filter by task ID"
                },
                "tool_name": {
                    "type": "string",
                    "description": "Filter by tool name"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": 10
                }
            },
            "required": []
        }