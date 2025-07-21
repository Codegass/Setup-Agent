"""Context management tool for the agent."""

from typing import Optional, Dict, Any, List

from loguru import logger

from agent.context_manager import ContextManager, BranchContextHistory

from .base import BaseTool, ToolResult, ToolError


class ContextTool(BaseTool):
    """Refactored context management tool supporting branch history files and strict sequential execution."""

    def __init__(self, context_manager: ContextManager):
        super().__init__(
            name="manage_context",
            description="Manage context for task execution with branch history tracking. "
                       "Supports starting tasks, adding context entries, compression, and completion.",
        )
        self.context_manager = context_manager

    def execute(
        self,
        action: str,
        task_id: Optional[str] = None,
        entry: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        new_context: Optional[List[Dict[str, Any]]] = None
    ) -> ToolResult:
        """
        Execute context management actions.
        
        Args:
            action: The action to perform ('get_info', 'start_task', 'add_context', 'get_full_context', 'compact_context', 'complete_task')
            task_id: Task ID (required for 'start_task', 'complete_task')
            entry: Context entry to add (required for 'add_context')
            summary: Summary of work done (required for 'complete_task')
            new_context: Compacted context history (required for 'compact_context')
        """
        
        valid_actions = ["get_info", "start_task", "create_branch", "add_context", "get_full_context", "compact_context", "complete_task", "switch_to_trunk"]
        
        if action not in valid_actions:
            raise ToolError(
                message=f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
                suggestions=[
                    f"Use one of the valid actions: {', '.join(valid_actions)}",
                    "• get_info: Get current context information",
                    "• start_task: Start a new branch task",
                    "• add_context: Add entry to current task history",
                    "• get_full_context: Get complete task history",
                    "• compact_context: Replace history with compressed version",
                    "• complete_task: Complete current task and return to trunk"
                ],
                documentation_links=[
                    "Context Management Guide: Use get_info first to understand current state"
                ],
                error_code="INVALID_ACTION"
            )
        
        try:
            if action == "get_info":
                return self._get_context_info()
            elif action in ["start_task", "create_branch"]:
                return self._start_task(task_id)
            elif action == "add_context":
                return self._add_context(entry)
            elif action == "get_full_context":
                return self._get_full_context()
            elif action == "compact_context":
                return self._compact_context(new_context)
            elif action in ["complete_task", "switch_to_trunk"]:
                return self._complete_task(summary)
                
        except Exception as e:
            raise ToolError(
                message=f"Context management failed: {str(e)}",
                suggestions=[
                    "Check that the context manager is properly initialized",
                    "Verify the task_id exists in the TODO list (for start_task)",
                    "Ensure you're in the correct context type for the action"
                ],
                documentation_links=[
                    "Context Management Troubleshooting Guide"
                ],
                error_code="CONTEXT_MANAGEMENT_ERROR"
            )

    def _get_context_info(self) -> ToolResult:
        """Get current context information"""
        try:
            info = self.context_manager.get_current_context_info()
            
            if "error" in info:
                raise ToolError(
                    message=info["error"],
                    suggestions=[
                        "Check if context manager is properly initialized",
                        "Create a trunk context first if none exists"
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
                    "Check if context manager is properly initialized"
                ],
                error_code="CONTEXT_INFO_ERROR"
            )

    def _start_task(self, task_id: Optional[str]) -> ToolResult:
        """Start a new branch task"""
        if not task_id:
            raise ToolError(
                message="task_id is required for starting a task",
                suggestions=[
                    "Provide a task_id parameter: manage_context(action='start_task', task_id='task_1')",
                    "Use get_info action first to see available tasks in the TODO list"
                ],
                error_code="MISSING_TASK_ID"
            )
        
        try:
            result = self.context_manager.start_new_branch(task_id)
            
            output = f"✅ Started task: {result['task_description']}\n\n"
            output += f"📋 Task Details:\n"
            output += f"• Task ID: {task_id}\n"
            output += f"• Task Description: {result['task_description']}\n"
            
            if result['previous_summary']:
                output += f"• Previous Task Results: {result['previous_summary']}\n"
            
            output += f"\n📝 Branch history file created: {result['branch_file']}\n\n"
            output += f"💡 Next steps:\n"
            output += f"• Use add_context() to record thoughts and tool results\n"
            output += f"• Use get_full_context() to review complete history\n"
            output += f"• Use complete_task() to summarize and switch to next task\n"
            
            return ToolResult(
                success=True,
                output=output,
                metadata=result
            )
            
        except ValueError as e:
            raise ToolError(
                message=str(e),
                suggestions=[
                    "Check if task ID is correct",
                    "Ensure tasks are executed in order (cannot skip)",
                    "Use get_info to see currently executable task"
                ],
                error_code="TASK_START_ERROR"
            )

    def _add_context(self, entry: Optional[Dict[str, Any]]) -> ToolResult:
        """Add context entry to current task history"""
        if not entry:
            raise ToolError(
                message="entry is required for adding context",
                suggestions=[
                    "Provide an entry dict: manage_context(action='add_context', entry={'thought': '...', 'action': '...'})",
                    "Entry should contain relevant information like thoughts, tool outputs, observations"
                ],
                error_code="MISSING_ENTRY"
            )
        
        if not self.context_manager.current_task_id:
            raise ToolError(
                message="No active task. Start a task first.",
                suggestions=[
                    "Use start_task action to begin working on a task",
                    "Check current context with get_info"
                ],
                error_code="NO_ACTIVE_TASK"
            )
        
        try:
            result = self.context_manager.add_to_branch_history(
                self.context_manager.current_task_id, entry
            )
            
            output = f"✅ Context entry added\n\n"
            output += f"📊 History status:\n"
            output += f"• Entry count: {result['entry_count']}\n"
            output += f"• Estimated tokens: {result['token_count']}\n"
            
            if result['needs_compression']:
                output += f"\n⚠️ Context size warning:\n"
                output += f"• {result.get('compression_warning', '')}\n"
                output += f"• Consider using compact_context() for compression\n"
            
            return ToolResult(
                success=True,
                output=output,
                metadata=result
            )
            
        except Exception as e:
            raise ToolError(
                message=f"Failed to add context entry: {str(e)}",
                suggestions=[
                    "Check if the current task is properly initialized",
                    "Verify the entry format is correct"
                ],
                error_code="ADD_CONTEXT_ERROR"
            )

    def _get_full_context(self) -> ToolResult:
        """Get complete history of current task"""
        if not self.context_manager.current_task_id:
            raise ToolError(
                message="No active task",
                suggestions=[
                    "Use start_task action to begin working on a task"
                ],
                error_code="NO_ACTIVE_TASK"
            )
        
        try:
            branch_history = self.context_manager.load_branch_history(
                self.context_manager.current_task_id
            )
            
            if not branch_history:
                raise ToolError(
                    message="Failed to load branch history",
                    error_code="LOAD_HISTORY_ERROR"
                )
            
            output = f"📚 Complete task history: {branch_history.task_description}\n\n"
            
            if branch_history.previous_task_summary:
                output += f"📋 Previous task results:\n{branch_history.previous_task_summary}\n\n"
            
            output += f"📝 Execution history ({branch_history.entry_count} entries):\n"
            
            for i, entry in enumerate(branch_history.history, 1):
                timestamp = entry.get('timestamp', 'Unknown time')
                output += f"\n--- Entry {i} ({timestamp}) ---\n"
                for key, value in entry.items():
                    if key != 'timestamp':
                        output += f"{key}: {value}\n"
            
            output += f"\n📊 Statistics:\n"
            output += f"• Total entries: {branch_history.entry_count}\n"
            output += f"• Estimated tokens: {branch_history.token_count}\n"
            output += f"• Needs compression: {'Yes' if branch_history.token_count > branch_history.context_window_threshold else 'No'}\n"
            
            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "task_id": branch_history.task_id,
                    "entry_count": branch_history.entry_count,
                    "token_count": branch_history.token_count,
                    "history": branch_history.history
                }
            )
            
        except Exception as e:
            raise ToolError(
                message=f"Failed to get full context: {str(e)}",
                error_code="GET_CONTEXT_ERROR"
            )

    def _compact_context(self, new_context: Optional[List[Dict[str, Any]]]) -> ToolResult:
        """Replace current history with compressed context"""
        if not new_context:
            raise ToolError(
                message="new_context is required for compacting",
                suggestions=[
                    "Provide a compacted history list",
                    "Use get_full_context first to review current history"
                ],
                error_code="MISSING_NEW_CONTEXT"
            )
        
        if not self.context_manager.current_task_id:
            raise ToolError(
                message="No active task",
                suggestions=[
                    "Use start_task action to begin working on a task"
                ],
                error_code="NO_ACTIVE_TASK"
            )
        
        try:
            success = self.context_manager.compact_branch_history(
                self.context_manager.current_task_id, new_context
            )
            
            if success:
                # Get compression statistics
                branch_history = self.context_manager.load_branch_history(
                    self.context_manager.current_task_id
                )
                
                output = f"✅ Context compressed\n\n"
                output += f"📊 Compression results:\n"
                output += f"• New entry count: {len(new_context)}\n"
                
                if branch_history:
                    output += f"• New token count: {branch_history.token_count}\n"
                    output += f"• Compression ratio: {len(new_context) / max(1, branch_history.entry_count) * 100:.1f}%\n"
                
                output += f"\n💡 Context optimized, can continue task execution\n"
                
                return ToolResult(
                    success=True,
                    output=output,
                    metadata={
                        "compacted_entries": len(new_context),
                        "new_token_count": branch_history.token_count if branch_history else 0
                    }
                )
            else:
                raise ToolError(
                    message="Failed to compact context",
                    error_code="COMPACT_ERROR"
                )
                
        except Exception as e:
            raise ToolError(
                message=f"Failed to compact context: {str(e)}",
                error_code="COMPACT_ERROR"
            )

    def _complete_task(self, summary: Optional[str]) -> ToolResult:
        """Complete current task"""
        if not summary:
            raise ToolError(
                message="summary is required for completing a task",
                suggestions=[
                    "Provide a summary of what was accomplished",
                    "Include key results and important findings"
                ],
                error_code="MISSING_SUMMARY"
            )
        
        if not self.context_manager.current_task_id:
            raise ToolError(
                message="No active task to complete",
                suggestions=[
                    "Use start_task action to begin working on a task"
                ],
                error_code="NO_ACTIVE_TASK"
            )
        
        try:
            current_task_id = self.context_manager.current_task_id
            result = self.context_manager.complete_branch(current_task_id, summary)
            
            output = f"✅ Task completed: {current_task_id}\n\n"
            output += f"📋 Completion summary:\n{summary}\n\n"
            output += f"📊 Project progress:\n{result['progress']}\n\n"
            
            if result.get('next_task'):
                next_task = result['next_task']
                output += f"➡️ Next task:\n"
                output += f"• ID: {next_task['id']}\n"
                output += f"• Description: {next_task['description']}\n"
                output += f"• Status: {next_task['status']}\n\n"
                output += f"🚀 Start next task:\n"
                output += f"manage_context(action='start_task', task_id='{next_task['id']}')\n"
            elif result.get('all_tasks_completed'):
                output += f"🎉 All tasks completed! Project setup finished.\n"
            
            return ToolResult(
                success=True,
                output=output,
                metadata=result
            )
            
        except Exception as e:
            raise ToolError(
                message=f"Failed to complete task: {str(e)}",
                error_code="COMPLETE_TASK_ERROR"
            )

    def _format_context_info(self, info: dict) -> str:
        """Format context information"""
        output = f"📋 Current Context Information\n\n"
        
        if info["context_type"] == "trunk":
            output += f"🎯 Trunk Context (Project Overview)\n"
            output += f"• Context ID: {info['context_id']}\n"
            output += f"• Project Goal: {info['goal']}\n"
            output += f"• Project Progress: {info['progress']}\n"
            output += f"• Next Task: {info['next_task']}\n"
            
            if info.get('next_task_id'):
                output += f"\n💡 Start next task:\n"
                output += f"manage_context(action='start_task', task_id='{info['next_task_id']}')\n"
            
        elif info["context_type"] == "branch":
            output += f"🎯 Branch Task Context\n"
            output += f"• Task ID: {info['task_id']}\n"
            output += f"• Task Description: {info['task_description']}\n"
            output += f"• History Entries: {info['entry_count']}\n"
            output += f"• Token Count: {info['token_count']}\n"
            
            if info['needs_compression']:
                output += f"\n⚠️ Context compression recommended (exceeds threshold)\n"
            
            output += f"\n💡 Available operations:\n"
            output += f"• add_context() - Add thoughts or results\n"
            output += f"• get_full_context() - View complete history\n"
            output += f"• compact_context() - Compress history\n"
            output += f"• complete_task() - Complete task\n"
        
        output += f"\n⏰ Last updated: {info['last_updated']}\n"
        return output

    def get_usage_example(self) -> str:
        """Get usage examples"""
        return """
Context Management Tool Usage Examples:

1. Check current status:
   manage_context(action="get_info")

2. Start new task:
   manage_context(action="start_task", task_id="task_1")

3. Add thought process:
   manage_context(action="add_context", entry={"thought": "Analyzing project structure", "observation": "Found config files"})

4. View complete history:
   manage_context(action="get_full_context")

5. Compress context:
   manage_context(action="compact_context", new_context=[{"summary": "Completed project analysis"}])

6. Complete task:
   manage_context(action="complete_task", summary="Successfully configured development environment")

Workflow:
Trunk → start_task → Branch → add_context (multiple) → [optional: compact_context] → complete_task → Trunk

Tips:
• Execute tasks in strict order, cannot skip
• Record important thought processes and results timely
• Use compression when context becomes too long
• Provide detailed summary when completing tasks
"""

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get parameters schema"""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_info", "start_task", "create_branch", "add_context", "get_full_context", "compact_context", "complete_task", "switch_to_trunk"],
                    "description": "Action to execute",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (required for start_task and complete_task)",
                    "default": None,
                },
                "entry": {
                    "type": "object",
                    "description": "Context entry to add (required for add_context)",
                    "default": None,
                },
                "summary": {
                    "type": "string",
                    "description": "Task completion summary (required for complete_task)",
                    "default": None,
                },
                "new_context": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Compressed context history (required for compact_context)",
                    "default": None,
                },
            },
            "required": ["action"],
        }
