"""Context management system for the agent."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum

from loguru import logger
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Status of a task in the TODO list."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(BaseModel):
    """A task in the TODO list."""
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notes: str = ""


class ContextType(str, Enum):
    """Type of context."""
    TRUNK = "trunk"
    BRANCH = "branch"


class BaseContext(BaseModel):
    """Base context class."""
    context_id: str
    context_type: ContextType
    created_at: datetime = Field(default_factory=datetime.now)
    last_updated: datetime = Field(default_factory=datetime.now)
    step_count: int = 0
    
    def update_timestamp(self):
        """Update the last_updated timestamp."""
        self.last_updated = datetime.now()
    
    def increment_step(self):
        """Increment the step counter."""
        self.step_count += 1
        self.update_timestamp()


class TrunkContext(BaseContext):
    """Main context that tracks the overall project setup."""
    
    goal: str
    project_url: str
    project_name: str
    todo_list: List[Task] = Field(default_factory=list)
    environment_summary: Dict[str, Any] = Field(default_factory=dict)
    progress_summary: str = ""
    
    def __init__(self, **data):
        super().__init__(context_type=ContextType.TRUNK, **data)
    
    def add_task(self, description: str) -> str:
        """Add a new task to the TODO list."""
        task_id = f"task_{len(self.todo_list) + 1}"
        task = Task(id=task_id, description=description)
        self.todo_list.append(task)
        self.update_timestamp()
        logger.info(f"Added task to TODO: {description}")
        return task_id
    
    def update_task_status(self, task_id: str, status: TaskStatus, notes: str = "") -> bool:
        """Update the status of a task."""
        for task in self.todo_list:
            if task.id == task_id:
                old_status = task.status
                task.status = status
                task.notes = notes
                
                if status == TaskStatus.IN_PROGRESS and not task.started_at:
                    task.started_at = datetime.now()
                elif status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                    task.completed_at = datetime.now()
                
                self.update_timestamp()
                logger.info(f"Updated task {task_id}: {old_status} -> {status}")
                return True
        return False
    
    def get_next_pending_task(self) -> Optional[Task]:
        """Get the next pending task from the TODO list."""
        for task in self.todo_list:
            if task.status == TaskStatus.PENDING:
                return task
        return None
    
    def get_progress_summary(self) -> str:
        """Get a summary of current progress."""
        total = len(self.todo_list)
        completed = sum(1 for task in self.todo_list if task.status == TaskStatus.COMPLETED)
        failed = sum(1 for task in self.todo_list if task.status == TaskStatus.FAILED)
        in_progress = sum(1 for task in self.todo_list if task.status == TaskStatus.IN_PROGRESS)
        
        return (
            f"Progress: {completed}/{total} completed, "
            f"{failed} failed, {in_progress} in progress"
        )


class BranchContext(BaseContext):
    """Sub-context for working on specific tasks."""
    
    parent_context_id: str
    task_id: str
    task_description: str
    detailed_log: List[str] = Field(default_factory=list)
    current_focus: str = ""
    
    def __init__(self, **data):
        super().__init__(context_type=ContextType.BRANCH, **data)
    
    def add_log_entry(self, entry: str):
        """Add an entry to the detailed log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {entry}"
        self.detailed_log.append(log_entry)
        self.update_timestamp()
        logger.debug(f"Branch context log: {entry}")
    
    def set_focus(self, focus: str):
        """Set the current focus area."""
        self.current_focus = focus
        self.add_log_entry(f"Focus changed to: {focus}")
    
    def get_summary(self) -> str:
        """Get a summary of work done in this branch context."""
        if not self.detailed_log:
            return f"Started working on: {self.task_description}"
        
        summary = f"Worked on: {self.task_description}\n"
        summary += f"Steps taken: {len(self.detailed_log)}\n"
        if self.current_focus:
            summary += f"Final focus: {self.current_focus}\n"
        
        # Add last few log entries as examples
        if len(self.detailed_log) > 3:
            summary += "Recent activities:\n"
            for entry in self.detailed_log[-3:]:
                summary += f"  - {entry}\n"
        else:
            summary += "All activities:\n"
            for entry in self.detailed_log:
                summary += f"  - {entry}\n"
        
        return summary


class ContextManager:
    """Manages the context switching and persistence."""
    
    def __init__(self, workspace_path: str = "/workspace"):
        self.workspace_path = Path(workspace_path)
        self.contexts_dir = self.workspace_path / ".setup_agent" / "contexts"
        self.contexts_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_context: Optional[BaseContext] = None
        self.trunk_context: Optional[TrunkContext] = None
        
    def create_trunk_context(self, goal: str, project_url: str, project_name: str) -> TrunkContext:
        """Create the main trunk context."""
        context_id = f"trunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        self.trunk_context = TrunkContext(
            context_id=context_id,
            goal=goal,
            project_url=project_url,
            project_name=project_name
        )
        
        self.current_context = self.trunk_context
        self._save_context(self.trunk_context)
        
        logger.info(f"Created trunk context: {context_id}")
        return self.trunk_context
    
    def create_branch_context(self, task_id: str, task_description: str) -> BranchContext:
        """Create a new branch context for a specific task."""
        if not self.trunk_context:
            raise ValueError("No trunk context exists")
        
        context_id = f"branch_{task_id}_{datetime.now().strftime('%H%M%S')}"
        
        branch_context = BranchContext(
            context_id=context_id,
            parent_context_id=self.trunk_context.context_id,
            task_id=task_id,
            task_description=task_description
        )
        
        # Update task status to in-progress
        self.trunk_context.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        self._save_context(self.trunk_context)
        
        self.current_context = branch_context
        self._save_context(branch_context)
        
        logger.info(f"Created branch context: {context_id} for task: {task_description}")
        return branch_context
    
    def switch_to_trunk(self, summary: str = "") -> TrunkContext:
        """Switch back to the trunk context, optionally with a summary."""
        if not self.trunk_context:
            raise ValueError("No trunk context exists")
        
        # If we're coming from a branch context, process the summary
        if (self.current_context and 
            isinstance(self.current_context, BranchContext) and 
            summary):
            
            # Update the trunk with the summary
            self.trunk_context.progress_summary += f"\n\n{summary}"
            
            # Update task status based on summary
            if "completed" in summary.lower() or "success" in summary.lower():
                self.trunk_context.update_task_status(
                    self.current_context.task_id, 
                    TaskStatus.COMPLETED, 
                    summary
                )
            elif "failed" in summary.lower() or "error" in summary.lower():
                self.trunk_context.update_task_status(
                    self.current_context.task_id, 
                    TaskStatus.FAILED, 
                    summary
                )
        
        self.current_context = self.trunk_context
        self.trunk_context.increment_step()
        self._save_context(self.trunk_context)
        
        logger.info("Switched to trunk context")
        return self.trunk_context
    
    def load_or_create_trunk_context(self, goal: str, project_url: str, project_name: str) -> TrunkContext:
        """Load existing trunk context or create a new one."""
        # Try to find existing trunk context for this project
        existing_context = self._find_existing_trunk_context(project_name)
        
        if existing_context:
            self.trunk_context = existing_context
            self.current_context = existing_context
            logger.info(f"Loaded existing trunk context: {existing_context.context_id}")
            return existing_context
        else:
            return self.create_trunk_context(goal, project_url, project_name)
    
    def _find_existing_trunk_context(self, project_name: str) -> Optional[TrunkContext]:
        """Find existing trunk context for a project."""
        for context_file in self.contexts_dir.glob("trunk_*.json"):
            try:
                with open(context_file, 'r') as f:
                    data = json.load(f)
                    if data.get('project_name') == project_name:
                        return TrunkContext(**data)
            except Exception as e:
                logger.warning(f"Failed to load context file {context_file}: {e}")
        return None
    
    def _save_context(self, context: BaseContext):
        """Save a context to disk."""
        filename = f"{context.context_id}.json"
        filepath = self.contexts_dir / filename
        
        try:
            with open(filepath, 'w') as f:
                json.dump(context.model_dump(), f, default=str, indent=2)
            logger.debug(f"Saved context: {context.context_id}")
        except Exception as e:
            logger.error(f"Failed to save context {context.context_id}: {e}")
    
    def get_current_context_info(self) -> Dict[str, Any]:
        """Get information about the current context."""
        if not self.current_context:
            return {"error": "No active context"}
        
        info = {
            "context_id": self.current_context.context_id,
            "context_type": self.current_context.context_type,
            "step_count": self.current_context.step_count,
            "last_updated": self.current_context.last_updated.isoformat()
        }
        
        if isinstance(self.current_context, TrunkContext):
            info.update({
                "goal": self.current_context.goal,
                "progress": self.current_context.get_progress_summary(),
                "next_task": (
                    self.current_context.get_next_pending_task().description
                    if self.current_context.get_next_pending_task()
                    else "No pending tasks"
                )
            })
        elif isinstance(self.current_context, BranchContext):
            info.update({
                "task": self.current_context.task_description,
                "focus": self.current_context.current_focus,
                "log_entries": len(self.current_context.detailed_log)
            })
        
        return info
