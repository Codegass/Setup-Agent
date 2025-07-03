"""Context management tool for the agent."""

from typing import Any, Dict

from loguru import logger

from .base import BaseTool, ToolResult
from agent.context_manager import ContextManager, BranchContext, TrunkContext


class ContextTool(BaseTool):
    """Tool for managing context switching."""
    
    def __init__(self, context_manager: ContextManager):
        super().__init__(
            name="manage_context",
            description="Switch between contexts: create branch context for focused work "
                       "on specific tasks, or return to trunk context with summary."
        )
        self.context_manager = context_manager
    
    def execute(self, action: str, task_id: str = None, summary: str = None) -> ToolResult:
        """Execute context management action."""
        if action not in ["create_branch", "switch_to_trunk", "get_info"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid action '{action}'. Must be 'create_branch', 'switch_to_trunk', or 'get_info'"
            )
        
        try:
            if action == "create_branch":
                return self._create_branch_context(task_id)
            elif action == "switch_to_trunk":
                return self._switch_to_trunk(summary)
            elif action == "get_info":
                return self._get_context_info()
                
        except Exception as e:
            error_msg = f"Context management failed: {str(e)}"
            logger.error(f"Context tool error for action '{action}': {error_msg}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                metadata={"action": action}
            )
    
    def _create_branch_context(self, task_id: str) -> ToolResult:
        """Create a new branch context for a specific task."""
        if not task_id:
            return ToolResult(
                success=False,
                output="",
                error="task_id is required for creating branch context"
            )
        
        if not self.context_manager.trunk_context:
            return ToolResult(
                success=False,
                output="",
                error="No trunk context exists. Cannot create branch context."
            )
        
        # Find the task in the trunk context
        task = None
        for t in self.context_manager.trunk_context.todo_list:
            if t.id == task_id:
                task = t
                break
        
        if not task:
            return ToolResult(
                success=False,
                output="",
                error=f"Task '{task_id}' not found in TODO list"
            )
        
        # Check if we're already in a branch context
        if isinstance(self.context_manager.current_context, BranchContext):
            return ToolResult(
                success=False,
                output="",
                error="Already in a branch context. Return to trunk first."
            )
        
        # Create the branch context
        branch_context = self.context_manager.create_branch_context(task_id, task.description)
        
        output = f"Created branch context for task: {task.description}\n"
        output += f"Context ID: {branch_context.context_id}\n"
        output += f"Focus on this specific task. Use tools to work on it in detail.\n"
        output += f"When done, use manage_context with action='switch_to_trunk' and provide a summary."
        
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "action": "create_branch",
                "context_id": branch_context.context_id,
                "task_id": task_id,
                "task_description": task.description
            }
        )
    
    def _switch_to_trunk(self, summary: str) -> ToolResult:
        """Switch back to trunk context with optional summary."""
        if not isinstance(self.context_manager.current_context, BranchContext):
            return ToolResult(
                success=False,
                output="",
                error="Not currently in a branch context"
            )
        
        # Get the current branch context for logging
        current_branch = self.context_manager.current_context
        
        # Switch to trunk
        trunk_context = self.context_manager.switch_to_trunk(summary or "")
        
        output = f"Switched back to trunk context\n"
        output += f"Completed work on: {current_branch.task_description}\n"
        
        if summary:
            output += f"Summary: {summary}\n"
        
        output += f"\nCurrent progress: {trunk_context.get_progress_summary()}\n"
        
        # Show next task if available
        next_task = trunk_context.get_next_pending_task()
        if next_task:
            output += f"Next pending task: {next_task.description}\n"
            output += f"Use manage_context with action='create_branch' and task_id='{next_task.id}' to start working on it."
        else:
            output += "No more pending tasks. All tasks completed!"
        
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "action": "switch_to_trunk",
                "previous_branch": current_branch.context_id,
                "trunk_context": trunk_context.context_id,
                "summary_provided": bool(summary)
            }
        )
    
    def _get_context_info(self) -> ToolResult:
        """Get information about the current context."""
        info = self.context_manager.get_current_context_info()
        
        if "error" in info:
            return ToolResult(
                success=False,
                output="",
                error=info["error"]
            )
        
        output = f"Current Context Information:\n"
        output += f"Context ID: {info['context_id']}\n"
        output += f"Context Type: {info['context_type']}\n"
        output += f"Step Count: {info['step_count']}\n"
        output += f"Last Updated: {info['last_updated']}\n"
        
        if info['context_type'] == 'trunk':
            output += f"\nGoal: {info['goal']}\n"
            output += f"Progress: {info['progress']}\n"
            output += f"Next Task: {info['next_task']}\n"
            
            # Show TODO list
            if self.context_manager.trunk_context:
                output += f"\nTODO List:\n"
                for task in self.context_manager.trunk_context.todo_list:
                    status_icon = {
                        "pending": "⏳",
                        "in_progress": "🔄", 
                        "completed": "✅",
                        "failed": "❌"
                    }.get(task.status, "❓")
                    
                    output += f"  {status_icon} {task.id}: {task.description}\n"
        
        elif info['context_type'] == 'branch':
            output += f"\nTask: {info['task']}\n"
            output += f"Current Focus: {info['focus']}\n"
            output += f"Log Entries: {info['log_entries']}\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata=info
        )
    
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_branch", "switch_to_trunk", "get_info"],
                    "description": "The context management action to perform"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (required for create_branch action)"
                },
                "summary": {
                    "type": "string",
                    "description": "Summary of work done (optional for switch_to_trunk)"
                }
            },
            "required": ["action"]
        }
