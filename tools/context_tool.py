"""Context management tool for the agent."""

from typing import Optional, Dict, Any, List

from loguru import logger

from agent.context_manager import ContextManager, BranchContextHistory, TaskStatus

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
        new_context: Optional[List[Dict[str, Any]]] = None,
        key_results: Optional[str] = None
    ) -> ToolResult:
        """
        Execute context management actions.
        
        Args:
            action: The action to perform ('get_info', 'start_task', 'add_context', 'get_full_context', 'compact_context', 'complete_task', 'complete_with_results')
            task_id: Task ID (required for 'start_task', 'complete_task')
            entry: Context entry to add (required for 'add_context')
            summary: Summary of work done (required for 'complete_task', 'complete_with_results')
            new_context: Compacted context history (required for 'compact_context')
            key_results: Key results to record (required for 'complete_with_results')
        """
        
        valid_actions = ["get_info", "start_task", "create_branch", "add_context", "get_full_context", "compact_context", "complete_task", "complete_with_results", "switch_to_trunk"]
        
        if action not in valid_actions:
            # CRITICAL: Provide intelligent error guidance to fix common mental model mistakes
            suggested_action = None
            helpful_message = f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
            
            # Smart suggestions based on common mental model errors
            if action in ["branch_start", "create_branch", "start_branch"]:
                suggested_action = "start_task"
                helpful_message = f"Invalid action '{action}'. Did you mean 'start_task'? Use 'start_task' to begin working on a task."
            elif action in ["branch_end", "end_branch", "finish_branch", "close_branch"]:
                suggested_action = "complete_task"
                helpful_message = f"Invalid action '{action}'. Did you mean 'complete_task'? Use 'complete_task' to finish your current task."
            elif action in ["switch_trunk", "return_trunk", "back_to_trunk"]:
                suggested_action = "complete_task"
                helpful_message = f"Invalid action '{action}'. Use 'complete_task' to finish your task and automatically return to trunk context."
            
            suggestions = [
                f"Use one of the valid actions: {', '.join(valid_actions)}",
                "🔄 CORRECT WORKFLOW: start_task(task_id) → [work on task] → complete_task(summary)",
                "💡 No need for 'branch_start' or 'branch_end' - the system handles context switching automatically"
            ]
            
            if suggested_action:
                suggestions.insert(0, f"✅ Try '{suggested_action}' instead of '{action}'")
            
            suggestions.extend([
                                    "• get_info: Get current context information",
                    "• start_task: Start a new branch task",
                    "• add_context: Add entry to current task history",
                    "• get_full_context: Get complete task history",
                    "• compact_context: Replace history with compressed version",
                    "• complete_task: Complete current task and return to trunk",
                    "• complete_with_results: Complete task and record key results (RECOMMENDED)"
            ])
            
            raise ToolError(
                message=helpful_message,
                suggestions=suggestions,
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
            elif action == "complete_with_results":
                return self._complete_task_with_results(summary, key_results)
                
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
            # Add debug information to help diagnose issues
            logger.debug("Getting current context info...")
            logger.debug(f"Context manager workspace: {self.context_manager.workspace_path}")
            logger.debug(f"Contexts directory: {self.context_manager.contexts_dir}")
            logger.debug(f"Current task ID: {self.context_manager.current_task_id}")
            logger.debug(f"Trunk context file: {self.context_manager.trunk_context_file}")
            
            info = self.context_manager.get_current_context_info()
            
            if "error" in info:
                logger.warning(f"Context manager returned error: {info['error']}")
                
                # Try to provide more helpful error information
                error_details = info["error"]
                if "No active context" in error_details:
                    # Check if we can find any context files
                    if self.context_manager.orchestrator:
                        find_cmd = f"find {self.context_manager.contexts_dir} -name '*.json' -type f 2>/dev/null | head -5"
                        result = self.context_manager.orchestrator.execute_command(find_cmd)
                        if result.get("success") and result.get("output", "").strip():
                            found_files = result["output"].strip().split("\n")
                            logger.info(f"Found context files: {found_files}")
                            error_details += f". Found {len(found_files)} context files in directory."
                        else:
                            logger.warning("No context files found in contexts directory")
                            error_details += ". No context files found in contexts directory."
                
                raise ToolError(
                    message=error_details,
                    suggestions=[
                        "Check if context manager is properly initialized",
                        "Verify that trunk context was created successfully during setup",
                        "Try running the setup process to create initial context",
                        "Check file permissions in the contexts directory"
                    ],
                    error_code="CONTEXT_INFO_ERROR"
                )
            
            output = self._format_context_info(info)
            logger.debug(f"Context info retrieved successfully: {info.get('context_type', 'unknown')} context")
            
            return ToolResult(
                success=True,
                output=output,
                metadata=info
            )
            
        except Exception as e:
            logger.error(f"Failed to get context info: {e}")
            raise ToolError(
                message=f"Failed to get context info: {str(e)}",
                suggestions=[
                    "Check if context manager is properly initialized",
                    "Verify that the contexts directory exists and is accessible",
                    "Try restarting the agent if this persists"
                ],
                error_code="CONTEXT_INFO_ERROR"
            )

    def _start_task(self, task_id: Optional[str]) -> ToolResult:
        """Start a new branch task with enhanced validation to prevent hallucinated task IDs"""
        if not task_id:
            raise ToolError(
                message="task_id is required for starting a task",
                suggestions=[
                    "Provide a task_id parameter: manage_context(action='start_task', task_id='task_1')",
                    "Use get_info action first to see available tasks in the TODO list"
                ],
                error_code="MISSING_TASK_ID"
            )
        
        # CRITICAL: Validate task_id exists in trunk context to prevent hallucinations
        trunk_context = self.context_manager.load_trunk_context()
        if not trunk_context:
            raise ToolError(
                message="No trunk context exists. Cannot validate task ID.",
                suggestions=[
                    "Ensure trunk context is properly initialized",
                    "Use get_info to check current context state"
                ],
                error_code="NO_TRUNK_CONTEXT"
            )
        
        # Check if task_id exists in the TODO list
        valid_task_ids = [task.id for task in trunk_context.todo_list]
        if task_id not in valid_task_ids:
            raise ToolError(
                message=f"Invalid task ID '{task_id}'. This task does not exist in the project plan.",
                suggestions=[
                    f"Use one of the valid task IDs: {', '.join(valid_task_ids)}",
                    "Use get_info to see the current task plan and available IDs",
                    "Do not invent or hallucinate task IDs - only use predefined ones from the plan"
                ],
                error_code="INVALID_TASK_ID"
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
        """Add context entry to current task history with enhanced type handling"""
        if not entry:
            raise ToolError(
                message="entry is required for adding context",
                suggestions=[
                    "Provide an entry dict: manage_context(action='add_context', entry={'thought': '...', 'action': '...'})",
                    "Entry should contain relevant information like thoughts, tool outputs, observations"
                ],
                error_code="MISSING_ENTRY"
            )
        
        # CRITICAL FIX: Enhanced type safety for entry parameter
        if not isinstance(entry, dict):
            # Handle cases where entry got converted to unexpected types
            if isinstance(entry, str):
                entry = {"content": entry}
                logger.info(f"🔧 ContextTool auto-wrapped string entry to dict")
            elif hasattr(entry, '__dict__'):
                # Handle object types
                entry = entry.__dict__
                logger.info(f"🔧 ContextTool converted object to dict")
            else:
                # Fallback for any other type
                entry = {"data": str(entry)}
                logger.info(f"🔧 ContextTool fallback conversion: {type(entry).__name__} → dict")
        
        # Ensure we have a clean dict
        if not isinstance(entry, dict):
            entry = {"value": str(entry)}
        
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
        """Complete current task with strict validation to prevent task ID confusion"""
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
            
        # CRITICAL: Validate that we're completing the correct task
        # This prevents the agent from completing wrong tasks due to cognitive confusion
        current_task_id = self.context_manager.current_task_id
        
        # Load trunk context to verify task status
        trunk_context = self.context_manager.load_trunk_context()
        if not trunk_context:
            raise ToolError(
                message="Cannot complete task: trunk context not found",
                suggestions=[
                    "Ensure trunk context is properly initialized"
                ],
                error_code="NO_TRUNK_CONTEXT"
            )
        
        # Find the current task and verify it's in progress
        current_task = None
        for task in trunk_context.todo_list:
            if task.id == current_task_id:
                current_task = task
                break
        
        if not current_task:
            raise ToolError(
                message=f"Current task {current_task_id} not found in project plan",
                suggestions=[
                    "Use get_info to check current context state"
                ],
                error_code="TASK_NOT_FOUND"
            )
        
        if current_task.status.value != "in_progress":
            raise ToolError(
                message=f"Cannot complete task {current_task_id}: status is {current_task.status.value}, not in_progress",
                suggestions=[
                    f"Task {current_task_id} must be in progress to be completed",
                    "Use get_info to check task status",
                    "If task is already completed, use switch_to_trunk action instead"
                ],
                error_code="TASK_NOT_IN_PROGRESS"
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

    def _complete_task_with_results(self, summary: Optional[str], key_results: Optional[str]) -> ToolResult:
        """
        Complete current task with key results recording - ATOMIC operation to prevent state/action separation.
        This is the RECOMMENDED way to complete tasks as it enforces proper state management.
        """
        if not summary:
            raise ToolError(
                message="summary is required for completing a task",
                suggestions=[
                    "Provide a summary of what was accomplished",
                    "Include key results and important findings"
                ],
                error_code="MISSING_SUMMARY"
            )
            
        if not key_results:
            raise ToolError(
                message="key_results is required for complete_with_results",
                suggestions=[
                    "Provide specific key results that will be useful for next tasks",
                    "Examples: 'Cloned repo to /workspace/project', 'Found pom.xml, Maven project confirmed'",
                    "Be specific about file locations, project types, or important discoveries"
                ],
                error_code="MISSING_KEY_RESULTS"
            )
        
        if not self.context_manager.current_task_id:
            raise ToolError(
                message="No active task to complete",
                suggestions=[
                    "Use start_task action to begin working on a task"
                ],
                error_code="NO_ACTIVE_TASK"
            )
            
        # CRITICAL: Validate that we're completing the correct task (same validation as regular complete_task)
        current_task_id = self.context_manager.current_task_id
        
        trunk_context = self.context_manager.load_trunk_context()
        if not trunk_context:
            raise ToolError(
                message="Cannot complete task: trunk context not found",
                suggestions=[
                    "Ensure trunk context is properly initialized"
                ],
                error_code="NO_TRUNK_CONTEXT"
            )
        
        # Find the current task and verify it's in progress
        current_task = None
        for task in trunk_context.todo_list:
            if task.id == current_task_id:
                current_task = task
                break
        
        if not current_task:
            raise ToolError(
                message=f"Current task {current_task_id} not found in project plan",
                suggestions=[
                    "Use get_info to check current context state"
                ],
                error_code="TASK_NOT_FOUND"
            )
        
        if current_task.status.value != "in_progress":
            raise ToolError(
                message=f"Cannot complete task {current_task_id}: status is {current_task.status.value}, not in_progress",
                suggestions=[
                    f"Task {current_task_id} must be in progress to be completed",
                    "Use get_info to check task status"
                ],
                error_code="TASK_NOT_IN_PROGRESS"
            )
        
        try:
            # CRITICAL FIX: True atomic operation - do all updates in one transaction
            # Load the most recent trunk context to avoid lost updates
            fresh_trunk_context = self.context_manager.load_trunk_context()
            if not fresh_trunk_context:
                raise Exception("Failed to load fresh trunk context for atomic update")
            
            # Perform all updates on the fresh context
            fresh_trunk_context.update_task_status(current_task_id, TaskStatus.COMPLETED, summary)
            fresh_trunk_context.update_task_key_results(current_task_id, key_results)
            
            # Save once with all updates
            self.context_manager._save_trunk_context(fresh_trunk_context)
            
            # Clear current task in context manager
            self.context_manager.current_task_id = None
            
            # Generate result info using the updated context
            next_task = fresh_trunk_context.get_next_pending_task()
            result = {
                "completed_task": current_task_id,
                "summary": summary,
                "progress": fresh_trunk_context.get_progress_summary()
            }
            
            if next_task:
                result["next_task"] = {
                    "id": next_task.id,
                    "description": next_task.description,
                    "status": next_task.status
                }
            else:
                result["all_tasks_completed"] = True
            
            output = f"✅ Task completed atomically: {current_task_id}\n\n"
            output += f"📋 Completion summary:\n{summary}\n\n"
            output += f"🔑 Key results recorded:\n{key_results}\n\n"
            output += f"📊 Project progress:\n{result['progress']}\n\n"
            
            if result.get('next_task'):
                next_task = result['next_task']
                output += f"➡️ Next task available:\n"
                output += f"• ID: {next_task['id']}\n"
                output += f"• Description: {next_task['description']}\n"
                output += f"• Status: {next_task['status']}\n\n"
                output += f"🚀 Start next task with:\n"
                output += f"manage_context(action='start_task', task_id='{next_task['id']}')\n\n"
                output += f"💡 IMPORTANT: The key results above will be available to guide your next task!\n"
            elif result.get('all_tasks_completed'):
                output += f"🎉 All tasks completed! Project setup finished.\n"
                output += f"📋 Generate final report with:\n"
                output += f"report(summary='All tasks completed successfully', status='success')\n"
            
            # Enhanced metadata for better tracking
            enhanced_result = dict(result)
            enhanced_result.update({
                "key_results": key_results,
                "atomic_completion": True,
                "completion_method": "complete_with_results"
            })
            
            return ToolResult(
                success=True,
                output=output,
                metadata=enhanced_result
            )
            
        except Exception as e:
            raise ToolError(
                message=f"Failed to complete task with results: {str(e)}",
                error_code="COMPLETE_WITH_RESULTS_ERROR"
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

6. Complete task (RECOMMENDED - atomic):
   manage_context(action="complete_with_results", summary="Successfully configured development environment", key_results="Maven project detected at /workspace/commons-cli, pom.xml confirmed, Java 11 environment ready")

6b. Complete task (basic):
   manage_context(action="complete_task", summary="Successfully configured development environment")

CORRECT WORKFLOW:
Trunk → start_task(task_id) → Branch → [work on task] → complete_with_results(summary, key_results) → Trunk (automatic)

IMPORTANT:
• USE complete_with_results instead of complete_task - it's ATOMIC and prevents state/action separation!
• key_results should be specific and useful for next tasks (file locations, project types, important discoveries)
• NO 'branch_start', 'branch_end', 'create_branch', or 'switch_to_trunk' actions exist!
• Context switching is AUTOMATIC when you use start_task and complete_with_results
• Execute tasks in strict order from TODO list, cannot skip
• Record important thought processes and results using add_context
• Use compression when context becomes too long
• The key_results from previous tasks will be available to guide your next task
• ALWAYS use complete_with_results after finishing technical work to avoid "ghost" states
"""

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get parameters schema"""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_info", "start_task", "create_branch", "add_context", "get_full_context", "compact_context", "complete_task", "complete_with_results", "switch_to_trunk"],
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
                    "description": "Task completion summary (required for complete_task and complete_with_results)",
                    "default": None,
                },
                "key_results": {
                    "type": "string",
                    "description": "Key results to record for future tasks (required for complete_with_results)",
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
