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
        key_results: Optional[str] = None,
        force: bool = False,
        **kwargs
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
            force: Force completion even if validation fails (use with caution)
        """
        
        # Check for unexpected parameters
        if kwargs:
            invalid_params = list(kwargs.keys())
            return ToolResult(
                success=False,
                output=(
                    f"❌ Invalid parameters for context tool: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - action (required): 'get_info', 'start_task', 'add_context', 'get_full_context', etc.\n"
                    f"  - task_id (optional): Task ID for task-specific actions\n"
                    f"  - entry (optional): Context entry for 'add_context'\n"
                    f"  - summary (optional): Summary for completion actions\n"
                    f"  - new_context (optional): New context for 'compact_context'\n"
                    f"  - key_results (optional): Key results for 'complete_with_results'\n"
                    f"  - force (optional): Override validation failures (use with caution)\n\n"
                    f"Example: context(action='get_info')\n"
                    f"Example: context(action='start_task', task_id='task_1')"
                ),
                error=f"Invalid parameters: {invalid_params}"
            )
        
        # Check for required parameters
        if not action:
            return ToolResult(
                success=False,
                output=(
                    "❌ Missing required parameter: 'action'\n\n"
                    "The context tool requires an 'action' parameter.\n"
                    "Valid actions: 'get_info', 'start_task', 'add_context', 'get_full_context', etc.\n"
                    "Example: context(action='get_info')"
                ),
                error="Missing required parameter: action"
            )
        
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
                return self._complete_task_with_results(summary, key_results, force)
                
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
            
            # 🆕 ENHANCED: Show updated TODO list status after task start
            output += self._get_compact_todo_status_update(f"Started task {task_id}")
            
            output += f"\n💡 Next steps:\n"
            output += f"• Use add_context() to record thoughts and tool results\n"
            output += f"• Use get_full_context() to review complete history\n"
            output += f"• Use complete_with_results() to summarize and switch to next task\n"
            
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
        """Complete current task with strict validation AND state recovery to prevent task ID confusion"""
        if not summary:
            raise ToolError(
                message="summary is required for completing a task",
                suggestions=[
                    "Provide a summary of what was accomplished",
                    "Include key results and important findings"
                ],
                error_code="MISSING_SUMMARY"
            )
        
        # ENHANCED: State recovery mechanism for robustness
        if not self.context_manager.current_task_id:
            # Try to recover state before failing
            recovery_result = self._attempt_state_recovery()
            if recovery_result["recovered"]:
                logger.warning(f"🔧 Recovered state: {recovery_result['message']}")
                # Continue with the recovered task ID
            else:
                # Enhanced error with diagnostic information
                diagnostic_info = self._diagnose_context_state()
                
                raise ToolError(
                    message="No active task to complete",
                    suggestions=[
                        "Use start_task action to begin working on a task",
                        f"Current context state: {diagnostic_info['state']}",
                        f"Available tasks: {', '.join(diagnostic_info['available_tasks'])}",
                        "Try get_info action to see current context status",
                        "If context is corrupted, restart with start_task"
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
                    "Ensure trunk context is properly initialized",
                    "Check if context files exist in .setup_agent/contexts/",
                    "Try restarting the agent or recreating context"
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
            # Enhanced error with recovery suggestions
            all_task_ids = [task.id for task in trunk_context.todo_list]
            raise ToolError(
                message=f"Current task {current_task_id} not found in project plan",
                suggestions=[
                    "Use get_info to check current context state",
                    f"Available tasks in plan: {', '.join(all_task_ids)}",
                    f"Try start_task with a valid task ID from: {all_task_ids}",
                    "Context may be corrupted - verify task plan integrity"
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

    def _complete_task_with_results(self, summary: Optional[str], key_results: Optional[str], force: bool = False) -> ToolResult:
        """
        Complete current task with key results recording - ATOMIC operation to prevent state/action separation.
        This is the RECOMMENDED way to complete tasks as it enforces proper state management.
        ENHANCED: Add validation to ensure critical tasks are properly completed.
        
        Args:
            summary: Summary of what was accomplished
            key_results: Key results to record for future tasks
            force: Override validation failures (use with caution)
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
        
        # CRITICAL: Check if this is a report generation task that needs report tool first
        current_task_id = self.context_manager.current_task_id
        if current_task_id:
            # Get current task description from trunk context
            trunk_context = self.context_manager.load_trunk_context()
            if trunk_context and 'todo_list' in trunk_context:
                for task in trunk_context['todo_list']:
                    if task.get('id') == current_task_id:
                        task_desc = task.get('description', '').lower()
                        # If this is a report generation task
                        if 'report' in task_desc and ('generate' in task_desc or 'completion' in task_desc or 'final' in task_desc):
                            # Check if they mention using report tool in summary/key_results
                            summary_lower = summary.lower() if summary else ''
                            key_results_lower = key_results.lower() if key_results else ''
                            
                            # If neither mentions report generation, they probably forgot to use report tool
                            if not any(word in summary_lower + key_results_lower for word in ['report generated', 'report created', 'setup-report', 'report tool', 'markdown report']):
                                raise ToolError(
                                    message="⚠️ WAIT! Report generation task requires using the 'report' tool first!",
                                    suggestions=[
                                        "📋 This is a report generation task - you must generate the report before completing it.",
                                        "",
                                        "STEP 1: Generate the report:",
                                        "report(",
                                        "    summary='Successfully set up and tested project',",
                                        "    status='success',",
                                        "    details='All build and test tasks completed successfully'",
                                        ")",
                                        "",
                                        "STEP 2: After report is generated, then complete the task:",
                                        "manage_context(",
                                        "    action='complete_with_results',",
                                        "    summary='Generated comprehensive setup report',",
                                        "    key_results='Report created at /workspace/setup-report-*.md'",
                                        ")",
                                        "",
                                        "The report tool creates a markdown file with all project details."
                                    ],
                                    error_code="REPORT_GENERATION_REQUIRED"
                                )
                        break
        
        # ENHANCED: State recovery mechanism for robustness (same as _complete_task)
        if not self.context_manager.current_task_id:
            # Try to recover state before failing
            recovery_result = self._attempt_state_recovery()
            if recovery_result["recovered"]:
                logger.warning(f"🔧 Recovered state: {recovery_result['message']}")
                # Continue with the recovered task ID
            else:
                # Enhanced error with diagnostic information
                diagnostic_info = self._diagnose_context_state()
                
                raise ToolError(
                    message="No active task to complete",
                    suggestions=[
                        "Use start_task action to begin working on a task",
                        f"Current context state: {diagnostic_info['state']}",
                        f"Available tasks: {', '.join(diagnostic_info['available_tasks'])}",
                        "Try get_info action to see current context status",
                        "If context is corrupted, restart with start_task"
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
        
        # ENHANCED: Task-specific completion validation
        validation_result = self._validate_task_completion(current_task, summary, key_results)
        if not validation_result["valid"]:
            if force:
                logger.warning(f"⚠️ Force completing task despite validation failure: {validation_result['reason']}")
                logger.info("🔧 Using force parameter - validation bypassed")
            else:
                raise ToolError(
                    message=f"Task completion validation failed: {validation_result['reason']}",
                    suggestions=validation_result["suggestions"] + [
                        "⚠️ If you're certain the task was completed correctly, use force=True to override validation",
                        "Example: manage_context(action='complete_with_results', summary='...', key_results='...', force=True)"
                    ],
                    error_code="TASK_COMPLETION_VALIDATION_FAILED"
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
            
            # 🆕 ENHANCED: Show updated TODO list status after task completion
            output += self._get_compact_todo_status_update(f"Completed task {current_task_id}")
            
            if result.get('next_task'):
                next_task = result['next_task']
                output += f"\n➡️ Next task available:\n"
                output += f"• ID: {next_task['id']}\n"
                output += f"• Description: {next_task['description']}\n"
                output += f"• Status: {next_task['status']}\n\n"
                output += f"🚀 Start next task with:\n"
                output += f"manage_context(action='start_task', task_id='{next_task['id']}')\n\n"
                output += f"💡 IMPORTANT: The key results above will be available to guide your next task!\n"
            elif result.get('all_tasks_completed'):
                output += f"\n🎉 All tasks completed! Project setup finished.\n"
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
        """Format context information with enhanced human-readable todo list display."""
        output = f"📋 Current Context Information\n\n"
        
        if info["context_type"] == "trunk":
            output += f"🎯 Trunk Context (Project Overview)\n"
            output += f"• Context ID: {info['context_id']}\n"
            output += f"• Project Goal: {info['goal']}\n"
            output += f"• Project Progress: {info['progress']}\n"
            
            # 🆕 ENHANCED: Human-friendly TODO list display with visual borders
            if 'todo_list' in info and info['todo_list']:
                output += self._format_human_friendly_todo_list(info['todo_list'])
            
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
            output += f"• complete_with_results() - Complete task with key results\n"
        
        output += f"\n⏰ Last updated: {info['last_updated']}\n"
        return output

    def _format_human_friendly_todo_list(self, todo_list: List[Dict[str, Any]]) -> str:
        """
        Format TODO list in clean checkbox style.
        Makes it easy to track project setup progress at a glance.
        """
        if not todo_list:
            return "\n📋 No tasks in TODO list\n"
        
        output = f"\n📋 PROJECT SETUP TODO LIST ({len(todo_list)} tasks):\n\n"
        
        for task in todo_list:
            status = task.get('status', 'pending')
            description = task.get('description', 'No description')
            
            # Status checkbox - using simple Unicode checkboxes
            if status == 'completed':
                checkbox = '☒'  # Checked box
            elif status == 'in_progress':
                checkbox = '⬚'  # Square with progress
            elif status == 'failed':
                checkbox = '☒'  # Checked (but failed)
            else:  # pending
                checkbox = '☐'  # Empty box
            
            # Clean task line
            output += f"   {checkbox} {description}\n"
            
            # Add key results for completed tasks on next line
            if status == 'completed' and task.get('key_results'):
                key_results = task.get('key_results', '')
                # Show abbreviated results
                if len(key_results) > 80:
                    results_preview = key_results[:80] + "..."
                else:
                    results_preview = key_results
                output += f"      └─ {results_preview}\n"
        
        # Summary statistics
        completed = sum(1 for task in todo_list if task.get('status') == 'completed')
        in_progress = sum(1 for task in todo_list if task.get('status') == 'in_progress')
        failed = sum(1 for task in todo_list if task.get('status') == 'failed')
        pending = len(todo_list) - completed - in_progress - failed
        
        output += f"\n📊 Progress: {completed}/{len(todo_list)} completed"
        if in_progress > 0:
            output += f", {in_progress} active"
        if failed > 0:
            output += f", {failed} failed"
        if pending > 0:
            output += f", {pending} pending"
        output += f"\n"
        
        # Highlight CORE SETUP progress
        core_tasks = [task for task in todo_list if '🚨 CORE SETUP' in task.get('description', '')]
        if core_tasks:
            core_completed = sum(1 for task in core_tasks if task.get('status') == 'completed')
            output += f"🚨 CORE SETUP: {core_completed}/{len(core_tasks)} build/test tasks done\n"
        
        return output

    def get_usage_example(self) -> str:
        """Get usage examples with enhanced human-friendly TODO list features"""
        return """
Context Management Tool Usage Examples (Enhanced with Human-Friendly TODO List):

1. Check current status with visual TODO list:
   manage_context(action="get_info")
   
   This will show a clean TODO list like:
   
   📋 PROJECT SETUP TODO LIST (5 tasks):
   
      ☒ Clone repository and setup basic environment
         └─ Repository cloned successfully to /workspace/project-name
      ⬚ Analyze project structure and generate intelligent plan
      ☐ Execute Maven build and compile project
      ☐ Run project test suite
      ☐ Generate final completion report
   
   📊 Progress: 1/5 completed, 1 active, 3 pending
   🚨 CORE SETUP: 0/2 build/test tasks done

2. Start new task (with TODO status update):
   manage_context(action="start_task", task_id="task_2")

3. Add thought process:
   manage_context(action="add_context", entry={"thought": "Analyzing project structure", "observation": "Found config files"})

4. View complete history:
   manage_context(action="get_full_context")

5. Complete task with validation (RECOMMENDED - atomic):
   manage_context(action="complete_with_results", 
                  summary="Successfully built all modules with Maven", 
                  key_results="BUILD SUCCESS: All 15 modules compiled without errors. Tests: 247 passed, 0 failed.")

   🚨 CRITICAL for CORE SETUP tasks: Summary and key_results MUST include explicit success confirmation!

CORRECT WORKFLOW:
Trunk → start_task(task_id) → Branch → [work on task] → complete_with_results(summary, key_results) → Trunk (automatic)

🚨 CORE SETUP TASK GUIDELINES:
• For build tasks: Include "BUILD SUCCESS" or "compilation successful" in summary
• For test tasks: Include test counts and "Tests run: X, Failures: 0" format
• CORE SETUP tasks have STRICT validation - must provide clear evidence of success
• Failure indicators will prevent task completion until issues are resolved

ENHANCED FEATURES:
• Human-readable TODO list with [x], [~], [ ] checkboxes
• Visual borders and progress tracking
• CORE SETUP task highlighting with 🚨 markers
• Compact status updates after each task start/completion
• Strict validation for critical build and test tasks

IMPORTANT:
• USE complete_with_results instead of complete_task - it's ATOMIC and prevents state/action separation!
• key_results should be specific and useful for next tasks (file locations, build results, test outcomes)
• CORE SETUP tasks are CRITICAL - provide explicit success confirmation
• Context switching is AUTOMATIC when you use start_task and complete_with_results
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

    def _attempt_state_recovery(self) -> Dict[str, Any]:
        """
        Attempt to recover from inconsistent context state.
        This is critical for robustness when context manager state is lost.
        """
        try:
            logger.info("🔧 Attempting state recovery...")
            
            # Load trunk context to check for in-progress tasks
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                return {"recovered": False, "message": "No trunk context found"}
            
            # Look for tasks marked as in_progress
            in_progress_tasks = [task for task in trunk_context.todo_list if task.status.value == "in_progress"]
            
            if len(in_progress_tasks) == 1:
                # Found exactly one in-progress task - this is likely our current task
                recovered_task = in_progress_tasks[0]
                self.context_manager.current_task_id = recovered_task.id
                
                logger.info(f"✅ Recovered active task: {recovered_task.id}")
                return {
                    "recovered": True,
                    "message": f"Recovered active task: {recovered_task.id} ({recovered_task.description})",
                    "task_id": recovered_task.id
                }
            elif len(in_progress_tasks) > 1:
                # Multiple in-progress tasks - this is inconsistent state
                task_ids = [task.id for task in in_progress_tasks]
                logger.warning(f"⚠️ Multiple in-progress tasks found: {task_ids}")
                
                # Use the most recent one (assuming task IDs are sequential)
                recovered_task = max(in_progress_tasks, key=lambda t: t.id)
                self.context_manager.current_task_id = recovered_task.id
                
                return {
                    "recovered": True,
                    "message": f"Recovered from inconsistent state: chose most recent task {recovered_task.id}",
                    "task_id": recovered_task.id,
                    "warning": f"Multiple in-progress tasks detected: {task_ids}"
                }
            else:
                # No in-progress tasks - check if we can start the next pending task
                next_task = trunk_context.get_next_pending_task()
                if next_task:
                    return {
                        "recovered": False,
                        "message": f"No active tasks, but {next_task.id} is ready to start",
                        "suggestion": f"Use start_task with task_id='{next_task.id}'"
                    }
                else:
                    return {
                        "recovered": False,
                        "message": "No in-progress tasks and no pending tasks available"
                    }
                    
        except Exception as e:
            logger.error(f"State recovery failed: {e}")
            return {"recovered": False, "message": f"Recovery failed: {str(e)}"}

    def _diagnose_context_state(self) -> Dict[str, Any]:
        """
        Diagnose current context state to provide useful diagnostic information.
        Helps agents and users understand what went wrong and how to fix it.
        """
        try:
            diagnosis = {
                "state": "unknown",
                "available_tasks": [],
                "issues": [],
                "recommendations": []
            }
            
            # Check trunk context
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                diagnosis["state"] = "no_trunk_context"
                diagnosis["issues"].append("Trunk context file not found or corrupted")
                diagnosis["recommendations"].append("Recreate context with proper initialization")
                return diagnosis
            
            # Analyze task states
            total_tasks = len(trunk_context.todo_list)
            completed_tasks = len([t for t in trunk_context.todo_list if t.status.value == "completed"])
            in_progress_tasks = [t for t in trunk_context.todo_list if t.status.value == "in_progress"]
            pending_tasks = [t for t in trunk_context.todo_list if t.status.value == "pending"]
            
            diagnosis["available_tasks"] = [t.id for t in trunk_context.todo_list]
            
            if len(in_progress_tasks) > 1:
                diagnosis["state"] = "multiple_active"
                diagnosis["issues"].append(f"Multiple tasks marked as in-progress: {[t.id for t in in_progress_tasks]}")
                diagnosis["recommendations"].append("Complete or reset one of the in-progress tasks")
            elif len(in_progress_tasks) == 1:
                diagnosis["state"] = "task_active_but_not_tracked"
                active_task = in_progress_tasks[0]
                diagnosis["issues"].append(f"Task {active_task.id} is marked in-progress but not tracked in memory")
                diagnosis["recommendations"].append(f"Continue with task {active_task.id} or use get_info to check status")
            elif pending_tasks:
                diagnosis["state"] = "ready_for_next_task"
                next_task = pending_tasks[0]
                diagnosis["recommendations"].append(f"Start next task: {next_task.id}")
            elif completed_tasks == total_tasks:
                diagnosis["state"] = "all_tasks_completed"
                diagnosis["recommendations"].append("All tasks completed - generate final report")
            else:
                diagnosis["state"] = "unclear"
                diagnosis["issues"].append("Context state is unclear or corrupted")
                
            # Check for context file inconsistencies
            if self.context_manager.current_task_id:
                current_task_file = self.context_manager.contexts_dir / f"{self.context_manager.current_task_id}.json"
                if not current_task_file.exists():
                    diagnosis["issues"].append(f"Task file missing for current task: {self.context_manager.current_task_id}")
                    
            return diagnosis
            
        except Exception as e:
            logger.error(f"Context diagnosis failed: {e}")
            return {
                "state": "diagnosis_failed",
                "available_tasks": [],
                "issues": [f"Diagnosis failed: {str(e)}"],
                "recommendations": ["Try restarting context management or recreating context"]
            }

    def _validate_task_completion(self, task: Any, summary: str, key_results: str) -> Dict[str, Any]:
        """
        Validate that a task has been properly completed based on its description and expected outcomes.
        CRITICAL for ensuring project_analyzer and other key tasks are not prematurely completed.
        """
        try:
            task_id = task.id
            task_description = task.description.lower()
            summary_lower = summary.lower()
            key_results_lower = key_results.lower()
            
            logger.info(f"🔍 Validating completion of {task_id}: {task.description}")
            
            validation_result = {
                "valid": True,
                "reason": "",
                "suggestions": []
            }
            
            # Task-specific validation rules
            if "project_analyzer" in task_description or "analyze project" in task_description:
                # CRITICAL: Ensure project_analyzer tool was actually used
                if not self._check_project_analyzer_execution():
                    validation_result.update({
                        "valid": False,
                        "reason": "Task requires project_analyzer tool execution but no evidence found",
                        "suggestions": [
                            "Use project_analyzer tool with action='analyze' before completing this task",
                            "The project_analyzer tool must be called to generate intelligent execution plan",
                            "Do not complete this task until project_analyzer has created additional tasks"
                        ]
                    })
                    return validation_result
                
                # Check if additional tasks were generated (evidence of proper analysis)
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context and len(trunk_context.todo_list) <= 4:  # Original 4 tasks
                    validation_result.update({
                        "valid": False,
                        "reason": "Project analysis should generate additional tasks but todo list unchanged",
                        "suggestions": [
                            "Ensure project_analyzer tool generated additional tasks",
                            "The analysis should expand the todo list with specific build/test tasks",
                            "Check if project_analyzer completed successfully with update_context=True"
                        ]
                    })
                    return validation_result
            
            elif "clone repository" in task_description:
                # Verify repository was actually cloned
                required_indicators = ["cloned", "repository", "workspace"]
                missing_indicators = [ind for ind in required_indicators 
                                   if ind not in summary_lower and ind not in key_results_lower]
                
                if missing_indicators:
                    validation_result.update({
                        "valid": False,
                        "reason": f"Repository cloning task missing evidence: {missing_indicators}",
                        "suggestions": [
                            "Ensure repository was successfully cloned to workspace",
                            "Include repository path and project type in key results",
                            "Use project_setup tool with action='clone' before completing"
                        ]
                    })
                    return validation_result
            
            elif "build and test" in task_description:
                # Verify build/test tasks were executed
                build_indicators = ["maven", "gradle", "build", "compile", "test"]
                has_build_evidence = any(ind in summary_lower or ind in key_results_lower 
                                       for ind in build_indicators)
                
                if not has_build_evidence:
                    validation_result.update({
                        "valid": False,
                        "reason": "Build and test task missing evidence of build tool execution",
                        "suggestions": [
                            "Execute Maven or Gradle commands before completing this task",
                            "Include build results and test outcomes in summary",
                            "Use maven or gradle tools to perform actual build and test"
                        ]
                    })
                    return validation_result
            
            # 🚨 ENHANCED: Specific validation for CORE SETUP tasks (build/test)
            elif "🚨 CORE SETUP" in task_description:
                if "build tasks" in task_description or "compilation success" in task_description:
                    # STRICT validation for build tasks
                    success_indicators = ["success", "successful", "build success", "compilation successful"]
                    failure_indicators = ["failed", "error", "failure", "unsuccessful", "compilation failed"]
                    
                    has_success = any(ind in summary_lower or ind in key_results_lower for ind in success_indicators)
                    has_failure = any(ind in summary_lower or ind in key_results_lower for ind in failure_indicators)
                    
                    if has_failure:
                        validation_result.update({
                            "valid": False,
                            "reason": "🚨 CORE SETUP build task indicates failure - cannot mark as completed",
                            "suggestions": [
                                "Fix build errors before completing this CORE SETUP task",
                                "Ensure compilation succeeds completely for all modules",
                                "Check build logs and resolve all compilation issues",
                                "This is a CRITICAL task for project setup success"
                            ]
                        })
                        return validation_result
                        
                    if not has_success:
                        validation_result.update({
                            "valid": False,
                            "reason": "🚨 CORE SETUP build task missing explicit success confirmation",
                            "suggestions": [
                                "Include explicit BUILD SUCCESS confirmation in summary",
                                "Provide clear evidence of successful compilation (e.g., 'BUILD SUCCESS')",
                                "Confirm all modules compiled without errors",
                                "This is a CRITICAL task - success must be clearly documented"
                            ]
                        })
                        return validation_result
                        
                elif "test suite" in task_description or "all tests pass" in task_description:
                    # STRICT validation for test tasks  
                    test_indicators = ["test", "passed", "success", "all tests", "tests run"]
                    failure_indicators = ["failed", "error", "failure", "unsuccessful", "test failed"]
                    
                    has_test_evidence = any(ind in summary_lower or ind in key_results_lower for ind in test_indicators)
                    has_failure = any(ind in summary_lower or ind in key_results_lower for ind in failure_indicators)
                    
                    if has_failure:
                        validation_result.update({
                            "valid": False,
                            "reason": "🚨 CORE SETUP test task indicates test failures - cannot mark as completed",
                            "suggestions": [
                                "Fix failing tests before completing this CORE SETUP task",
                                "Ensure all tests pass completely",
                                "Check test logs and resolve all test failures",
                                "This is a CRITICAL task for project setup success"
                            ]
                        })
                        return validation_result
                        
                    if not has_test_evidence:
                        validation_result.update({
                            "valid": False,
                            "reason": "🚨 CORE SETUP test task missing evidence of test execution and success",
                            "suggestions": [
                                "Execute complete test suite and include results in summary",
                                "Provide clear evidence that all tests passed",
                                "Include test count and success confirmation (e.g., 'Tests run: X, Failures: 0')",
                                "This is a CRITICAL task - test success must be clearly documented"
                            ]
                        })
                        return validation_result
            
            elif "report" in task_description:
                # Verify report generation
                if "report" not in summary_lower and "completion" not in summary_lower:
                    validation_result.update({
                        "valid": False,
                        "reason": "Report generation task missing evidence of report creation",
                        "suggestions": [
                            "Use report tool to generate completion report",
                            "Include report status in task summary",
                            "Ensure final project status is documented"
                        ]
                    })
                    return validation_result
            
            logger.info(f"✅ Task {task_id} validation passed")
            return validation_result
            
        except Exception as e:
            logger.error(f"Task validation failed: {e}")
            return {
                "valid": False,
                "reason": f"Validation error: {str(e)}",
                "suggestions": ["Check task completion manually", "Verify all required steps were completed"]
            }

    def _check_project_analyzer_execution(self) -> bool:
        """
        Check if project_analyzer tool was actually executed by examining multiple sources.
        Enhanced to check trunk context updates and output storage in addition to branch history.
        """
        try:
            if not self.context_manager.current_task_id:
                return False
            
            # Method 1: Check branch history (original check)
            branch_history = self.context_manager.load_branch_history(self.context_manager.current_task_id)
            if branch_history:
                # Check if any history entry indicates project_analyzer usage
                for entry in branch_history.history:
                    if isinstance(entry, dict):
                        # Check for action entries that mention project_analyzer
                        if (entry.get("type") == "action" and 
                            entry.get("tool_name") == "project_analyzer"):
                            logger.info("✅ Found evidence of project_analyzer execution in branch history")
                            return True
                        
                        # Check for content that mentions project_analyzer
                        content = str(entry.get("content", "")).lower()
                        if "project_analyzer" in content:
                            logger.info("✅ Found reference to project_analyzer in branch history")
                            return True
            
            # Method 2: Check if trunk context was updated with new tasks (evidence of analyzer execution)
            trunk_evidence = self._check_trunk_context_for_analyzer_updates()
            if trunk_evidence:
                logger.warning("⚠️ Project analyzer execution not in branch history but trunk context shows updates")
                logger.info("✅ Accepting based on trunk context evidence (new tasks or project metadata)")
                return True
            
            # Method 3: Check output storage for project_analyzer outputs
            if self._check_output_storage_for_analyzer():
                logger.warning("⚠️ Project analyzer execution not in branch history but found in output storage")
                logger.info("✅ Accepting based on output storage evidence")
                return True
            
            logger.warning("❌ No evidence of project_analyzer execution found in any source")
            logger.info("💡 Hint: Use project_analyzer(action='analyze') before completing this task")
            return False
            
        except Exception as e:
            logger.error(f"Failed to check project_analyzer execution: {e}")
            # Be lenient on errors - don't block completion due to validation errors
            return True
    
    def _check_trunk_context_for_analyzer_updates(self) -> bool:
        """
        Check if trunk context shows evidence of project_analyzer execution.
        Looks for new tasks added or project metadata updates.
        """
        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                return False
            
            # Check if we have tasks beyond the initial task_2 (analyzer task)
            # Initial setup usually has task_1 (clone) and task_2 (analyze)
            # If we have task_3+ it means analyzer added new tasks
            task_count = len(trunk_context.todo_list)
            if task_count > 2:
                # Check if any task after task_2 was created recently
                for task in trunk_context.todo_list:
                    task_id = task.id
                    # Tasks created by analyzer typically start from task_3
                    if task_id not in ["task_1", "task_2"]:
                        logger.debug(f"Found task {task_id} which suggests analyzer was executed")
                        return True
            
            # Also check if any task has java_version or build_system in key_results
            # These are typically set by project_analyzer
            for task in trunk_context.todo_list:
                if task.key_results:
                    key_results_lower = task.key_results.lower()
                    if any(indicator in key_results_lower for indicator in 
                           ["java_version", "build_system", "maven", "gradle", "project_path", "dependencies"]):
                        logger.debug(f"Found project metadata in task {task.id} key_results")
                        return True
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to check trunk context: {e}")
            return False
    
    def _check_output_storage_for_analyzer(self) -> bool:
        """
        Check output storage for evidence of project_analyzer execution.
        """
        try:
            # Check if we have output storage manager
            if not hasattr(self.context_manager, 'output_storage') or not self.context_manager.output_storage:
                return False
            
            # Search for project_analyzer outputs
            results = self.context_manager.output_storage.search_outputs(
                tool_name="project_analyzer",
                task_id=self.context_manager.current_task_id,
                limit=1
            )
            
            if results:
                logger.debug(f"Found project_analyzer output in storage: {results[0].get('ref_id')}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to check output storage: {e}")
            return False

    def _get_compact_todo_status_update(self, action_message: str) -> str:
        """
        Helper to get a simple TODO list status update showing all tasks.
        This is useful for showing the state after a task start/completion.
        """
        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                return "\n📋 Unable to load TODO list status\n"

            output = f"\n📋 TODO LIST UPDATE: {action_message}\n"
            
            # Show complete TODO list with simple checkboxes
            for task in trunk_context.todo_list:
                status = task.status.value
                description = task.description
                
                # Simple checkbox - only ☒ for completed, ☐ for everything else
                if status == 'completed':
                    checkbox = '☒'
                else:
                    checkbox = '☐'
                
                output += f"     {checkbox} {description}\n"
            
            # Summary statistics
            completed = len([t for t in trunk_context.todo_list if t.status.value == 'completed'])
            total = len(trunk_context.todo_list)
            
            output += f"\nProgress: {completed}/{total} completed\n"
            
            return output
            
        except Exception as e:
            logger.error(f"Failed to get compact TODO status: {e}")
            return f"\n📋 TODO status update failed: {str(e)}\n"
