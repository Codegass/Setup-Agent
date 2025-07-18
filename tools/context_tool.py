"""Context management tool for the agent."""

from typing import Optional, Dict, Any

from loguru import logger

from agent.context_manager import BranchContext, ContextManager, TrunkContext

from .base import BaseTool, ToolResult, ToolError


class ContextTool(BaseTool):
    """Enhanced context management tool with clear interface and comprehensive error handling."""

    def __init__(self, context_manager: ContextManager):
        super().__init__(
            name="manage_context",
            description="Manage context switching between trunk (main) and branch (task-specific) contexts. "
                       "Use this tool to organize your work and maintain focus on specific tasks.",
        )
        self.context_manager = context_manager

    def execute(
        self,
        action: str,
        task_id: Optional[str] = None,
        summary: Optional[str] = None
    ) -> ToolResult:
        """
        Execute context management actions.
        
        Args:
            action: The action to perform ('get_info', 'create_branch', 'switch_to_trunk')
            task_id: Task ID for creating branch context (required for 'create_branch')
            summary: Summary of work done (optional for 'switch_to_trunk')
        """
        
        valid_actions = ["get_info", "create_branch", "switch_to_trunk"]
        
        if action not in valid_actions:
            raise ToolError(
                message=f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
                suggestions=[
                    f"Use one of the valid actions: {', '.join(valid_actions)}",
                    "• get_info: Get current context information",
                    "• create_branch: Create a new branch context for a specific task",
                    "• switch_to_trunk: Return to trunk context with optional summary"
                ],
                documentation_links=[
                    "Context Management Guide: Use get_info first to understand current state"
                ],
                error_code="INVALID_ACTION"
            )
        
        try:
            if action == "get_info":
                return self._get_context_info()
            elif action == "create_branch":
                return self._create_branch_context(task_id)
            elif action == "switch_to_trunk":
                return self._switch_to_trunk(summary)
                
        except Exception as e:
            raise ToolError(
                message=f"Context management failed: {str(e)}",
                suggestions=[
                    "Check that the context manager is properly initialized",
                    "Verify the task_id exists in the TODO list (for create_branch)",
                    "Ensure you're in the correct context type for the action"
                ],
                documentation_links=[
                    "Context Management Troubleshooting Guide"
                ],
                error_code="CONTEXT_MANAGEMENT_ERROR"
            )

    def _create_branch_context(self, task_id: Optional[str]) -> ToolResult:
        """Create a new branch context for a specific task."""
        
        if not task_id:
            raise ToolError(
                message="task_id is required for creating branch context",
                suggestions=[
                    "Provide a task_id parameter: manage_context(action='create_branch', task_id='task_1')",
                    "Use get_info action first to see available tasks in the TODO list",
                    "Task IDs should match those in the trunk context TODO list"
                ],
                documentation_links=[
                    "Branch Context Creation Guide: How to work with specific tasks"
                ],
                error_code="MISSING_TASK_ID"
            )
        
        if not self.context_manager.trunk_context:
            raise ToolError(
                message="No trunk context exists. Cannot create branch context.",
                suggestions=[
                    "Create a trunk context first",
                    "Initialize the project properly",
                    "Check if the agent setup completed successfully"
                ],
                error_code="NO_TRUNK_CONTEXT"
            )
        
        # Find the task in the trunk context
        task = None
        for t in self.context_manager.trunk_context.todo_list:
            if t.id == task_id:
                task = t
                break
        
        if not task:
            available_tasks = [t.id for t in self.context_manager.trunk_context.todo_list]
            raise ToolError(
                message=f"Task '{task_id}' not found in TODO list",
                suggestions=[
                    f"Use one of the available task IDs: {', '.join(available_tasks)}",
                    "Use get_info action to see the complete TODO list",
                    "Check if the task ID is spelled correctly"
                ],
                documentation_links=[
                    "TODO List Management: How to work with tasks"
                ],
                error_code="TASK_NOT_FOUND"
            )
        
        # Check if we're already in a branch context
        if isinstance(self.context_manager.current_context, BranchContext):
            current_task = self.context_manager.current_context.task_description
            raise ToolError(
                message="Already in a branch context. Return to trunk first.",
                suggestions=[
                    "Use switch_to_trunk action to return to trunk context",
                    f"Complete current task: {current_task}",
                    "Provide a summary of work done when switching to trunk"
                ],
                documentation_links=[
                    "Context Switching Guide: How to manage context transitions"
                ],
                error_code="ALREADY_IN_BRANCH"
            )
        
        # Create the branch context
        branch_context = self.context_manager.create_branch_context(task_id, task.description)
        
        output = f"✅ Created branch context for task: {task.description}\n\n"
        output += f"📋 Context Details:\n"
        output += f"• Context ID: {branch_context.context_id}\n"
        output += f"• Task ID: {task_id}\n"
        output += f"• Task Status: {task.status}\n\n"
        output += f"🎯 Focus Area: {task.description}\n\n"
        output += f"📝 Instructions:\n"
        output += f"• Use tools to work on this specific task in detail\n"
        output += f"• Log your progress and findings\n"
        output += f"• When complete, use: manage_context(action='switch_to_trunk', summary='your_summary')\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "action": "create_branch",
                "context_id": branch_context.context_id,
                "task_id": task_id,
                "task_description": task.description,
                "task_status": task.status
            }
        )

    def _switch_to_trunk(self, summary: Optional[str]) -> ToolResult:
        """Switch back to trunk context with optional summary."""
        
        if not isinstance(self.context_manager.current_context, BranchContext):
            raise ToolError(
                message="Not currently in a branch context",
                suggestions=[
                    "You're already in trunk context - use get_info to confirm",
                    "Use create_branch to create a new branch context if needed",
                    "Check current context status with get_info action"
                ],
                documentation_links=[
                    "Context Navigation Guide: Understanding context states"
                ],
                error_code="NOT_IN_BRANCH"
            )
        
        # Get the current branch context for logging
        current_branch = self.context_manager.current_context
        
        # Switch to trunk
        trunk_context = self.context_manager.switch_to_trunk(summary or "")
        
        output = f"✅ Switched back to trunk context\n\n"
        output += f"📋 Completed Work:\n"
        output += f"• Task: {current_branch.task_description}\n"
        output += f"• Branch Context: {current_branch.context_id}\n"
        
        if summary:
            output += f"• Summary: {summary}\n"
        
        output += f"\n📊 Current Progress:\n"
        output += f"{trunk_context.get_progress_summary()}\n\n"
        
        # Show next task if available
        next_task = trunk_context.get_next_pending_task()
        if next_task:
            output += f"➡️ Next Task Available:\n"
            output += f"• ID: {next_task.id}\n"
            output += f"• Description: {next_task.description}\n"
            output += f"• Status: {next_task.status}\n\n"
            output += f"🚀 To start working on it:\n"
            output += f"manage_context(action='create_branch', task_id='{next_task.id}')\n"
        else:
            output += f"🎉 All tasks completed! No more pending tasks.\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "action": "switch_to_trunk",
                "previous_branch": current_branch.context_id,
                "trunk_context": trunk_context.context_id,
                "summary_provided": bool(summary),
                "next_task_available": next_task is not None
            }
        )

    def _get_context_info(self) -> ToolResult:
        """Get comprehensive information about the current context."""
        try:
            info = self.context_manager.get_current_context_info()
            
            if "error" in info:
                raise ToolError(
                    message=info["error"],
                    suggestions=[
                        "Check if context manager is properly initialized",
                        "Verify that a trunk context exists"
                    ],
                    error_code="CONTEXT_INFO_ERROR"
                )
            
            output = self._format_context_info(info)
            
            return ToolResult(
                success=True,
                output=output,
                metadata=info
            )
            
        except Exception as e:
            raise ToolError(
                message=f"Failed to get context info: {str(e)}",
                suggestions=[
                    "Check if context manager is properly initialized",
                    "Try creating a trunk context first if none exists"
                ],
                error_code="CONTEXT_INFO_ERROR"
            )

    def _format_context_info(self, info: dict) -> str:
        """Format context information in a user-friendly way."""
        output = f"📋 Current Context Information\n\n"
        output += f"• Context ID: {info['context_id']}\n"
        output += f"• Context Type: {info['context_type'].upper()}\n"
        output += f"• Step Count: {info['step_count']}\n"
        output += f"• Last Updated: {info['last_updated']}\n\n"
        
        if info["context_type"] == "trunk":
            output += f"🎯 Project Overview:\n"
            output += f"• Goal: {info['goal']}\n"
            output += f"• Progress: {info['progress']}\n"
            output += f"• Next Task: {info['next_task']}\n\n"
            
            # Show TODO list with better formatting
            if self.context_manager.trunk_context:
                output += f"📝 TODO List:\n"
                for task in self.context_manager.trunk_context.todo_list:
                    status_icon = {
                        "pending": "⏳",
                        "in_progress": "🔄",
                        "completed": "✅",
                        "failed": "❌"
                    }.get(task.status, "❓")
                    
                    output += f"  {status_icon} {task.id}: {task.description}\n"
                
                output += f"\n💡 To work on a specific task:\n"
                pending_tasks = [t for t in self.context_manager.trunk_context.todo_list if t.status == "pending"]
                if pending_tasks:
                    output += f"manage_context(action='create_branch', task_id='{pending_tasks[0].id}')\n"
        
        elif info["context_type"] == "branch":
            output += f"🎯 Current Task Focus:\n"
            output += f"• Task: {info['task']}\n"
            output += f"• Current Focus: {info['focus']}\n"
            output += f"• Log Entries: {info['log_entries']}\n\n"
            
            output += f"💡 When task is complete:\n"
            output += f"manage_context(action='switch_to_trunk', summary='describe what you accomplished')\n"
        
        return output

    def get_usage_example(self) -> str:
        """Get comprehensive usage examples for context management."""
        return """
Context Management Tool Usage Examples:

1. Check current context:
   manage_context(action="get_info")

2. Create branch context for specific task:
   manage_context(action="create_branch", task_id="task_1")

3. Return to trunk context with summary:
   manage_context(action="switch_to_trunk", summary="Completed repository cloning and setup")

4. Return to trunk without summary:
   manage_context(action="switch_to_trunk")

Context Flow:
TRUNK → create_branch → BRANCH → switch_to_trunk → TRUNK

Tips:
• Always use get_info first to understand current context
• Use descriptive summaries when switching to trunk
• Task IDs must match those in the TODO list
• You can only create branch contexts from trunk context
"""

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_info", "create_branch", "switch_to_trunk"],
                    "description": "The action to perform",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID for creating branch context (required for 'create_branch')",
                    "default": None,
                },
                "summary": {
                    "type": "string", 
                    "description": "Summary of work done (optional for 'switch_to_trunk')",
                    "default": None,
                },
            },
            "required": ["action"],
        }
