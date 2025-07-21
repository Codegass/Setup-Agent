"""Context management system for the agent."""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    key_results: str = ""  # Stores key results after task completion


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
        # Remove context_type from data if it exists to avoid conflicts
        data.pop('context_type', None)
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
        """Get the next pending task from the TODO list (strict order)."""
        # Strict order execution: only return the first pending task
        # If there are incomplete tasks ahead, cannot execute later tasks
        for task in self.todo_list:
            if task.status == TaskStatus.PENDING:
                return task
            elif task.status == TaskStatus.IN_PROGRESS:
                # If there's a task in progress, cannot start new tasks
                return None
            elif task.status in [TaskStatus.FAILED]:
                # If there's a failed task, cannot continue with subsequent tasks
                return None
        return None
    
    def can_start_task(self, task_id: str) -> bool:
        """Check if the specified task can be started (strict order validation)"""
        next_task = self.get_next_pending_task()
        return next_task is not None and next_task.id == task_id
    
    def update_task_key_results(self, task_id: str, key_results: str) -> bool:
        """Update the key results of a task"""
        for task in self.todo_list:
            if task.id == task_id:
                task.key_results = key_results
                self.update_timestamp()
                logger.info(f"Updated key results for task {task_id}")
                return True
        return False

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


# DEPRECATED: Legacy BranchContext class - replaced by BranchContextHistory
# Kept for backward compatibility but should not be used in new code
class BranchContext(BaseContext):
    """DEPRECATED: Sub-context for working on specific tasks. Use BranchContextHistory instead."""

    parent_context_id: str
    task_id: str
    task_description: str
    detailed_log: List[str] = Field(default_factory=list)
    current_focus: str = ""

    def __init__(self, **data):
        # Remove context_type from data if it exists to avoid conflicts
        data.pop('context_type', None)
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


class BranchContextHistory(BaseModel):
    """Branch task history file for storing all events during single task execution."""
    
    task_id: str
    task_description: str
    created_at: datetime = Field(default_factory=datetime.now)
    last_updated: datetime = Field(default_factory=datetime.now)
    
    # Key information inherited from previous task
    previous_task_summary: str = ""
    
    # Core: records all events during task execution
    history: List[Dict[str, Any]] = Field(default_factory=list)
    
    # Metadata for compression
    token_count: int = 0
    entry_count: int = 0
    context_window_threshold: int = 15000  # Token threshold for compression reminder
    
    def add_entry(self, entry: Dict[str, Any]) -> bool:
        """Add new history entry, returns whether compression is needed"""
        self.history.append(entry)
        self.entry_count += 1
        self.last_updated = datetime.now()
        
        # Simple token count estimation (can use more precise methods in actual implementation)
        entry_text = json.dumps(entry, ensure_ascii=False)
        self.token_count += len(entry_text) // 4  # Rough estimation
        
        # Check if compression is needed
        return self.token_count > self.context_window_threshold
    
    def replace_history(self, new_history: List[Dict[str, Any]]):
        """Replace current history with compressed history"""
        self.history = new_history
        self.entry_count = len(new_history)
        self.last_updated = datetime.now()
        
        # Recalculate token count
        total_text = json.dumps(new_history, ensure_ascii=False)
        self.token_count = len(total_text) // 4
    
    def get_summary_info(self) -> Dict[str, Any]:
        """Get summary information of history records"""
        return {
            "task_id": self.task_id,
            "task_description": self.task_description,
            "entry_count": self.entry_count,
            "token_count": self.token_count,
            "needs_compression": self.token_count > self.context_window_threshold,
            "last_updated": self.last_updated.isoformat()
        }


class ContextManager:
    """Manages the context switching and persistence."""

    def __init__(self, workspace_path: str = "/workspace", orchestrator=None):
        self.workspace_path = Path(workspace_path)
        self.contexts_dir = self.workspace_path / ".setup_agent" / "contexts"
        self.orchestrator = orchestrator

        # Initialize contexts directory using orchestrator if available
        if self.orchestrator:
            self._ensure_contexts_dir_in_container()
        else:
            # Fallback to local directory creation (for testing or non-container usage)
            self.contexts_dir.mkdir(parents=True, exist_ok=True)

        # New design: no longer maintain current_context, but operate files directly
        self.current_task_id: Optional[str] = None  # Currently executing task ID
        self.trunk_context_file: Optional[str] = None  # Trunk context file path

    def _ensure_contexts_dir_in_container(self):
        """Ensure the contexts directory exists in the container."""
        if not self.orchestrator:
            return

        # Create the contexts directory in the container
        contexts_path = str(self.contexts_dir)
        
        # Create directory with proper permissions
        create_cmd = f"mkdir -p {contexts_path} && chmod 755 {contexts_path}"
        result = self.orchestrator.execute_command(create_cmd)

        if result.get("success") or result.get("exit_code") == 0:
            logger.info(f"Created contexts directory in container: {contexts_path}")
            
            # Verify the directory exists and is writable
            test_cmd = f"test -d {contexts_path} && test -w {contexts_path}"
            test_result = self.orchestrator.execute_command(test_cmd)
            
            if not (test_result.get("success") or test_result.get("exit_code") == 0):
                # Try to fix permissions
                chmod_cmd = f"chmod 755 {contexts_path}"
                self.orchestrator.execute_command(chmod_cmd)
                logger.warning(f"Fixed permissions for contexts directory: {contexts_path}")
        else:
            logger.error(f"Failed to create contexts directory: {result.get('output', '')}")
            raise RuntimeError(f"Cannot create contexts directory in container: {contexts_path}")

    def create_trunk_context(self, goal: str, project_url: str, project_name: str, tasks: List[str] = None) -> TrunkContext:
        """Create the main trunk context with optional task list."""
        context_id = f"trunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        trunk_context = TrunkContext(
            context_id=context_id, goal=goal, project_url=project_url, project_name=project_name
        )

        # If task list is provided, add to TODO list
        if tasks:
            for i, task_desc in enumerate(tasks, 1):
                task_id = f"task_{i}"
                task = Task(id=task_id, description=task_desc)
                trunk_context.todo_list.append(task)

        # Save to file
        filename = f"{context_id}.json"
        self.trunk_context_file = str(self.contexts_dir / filename)
        self._save_trunk_context(trunk_context)

        logger.info(f"Created trunk context: {context_id} with {len(trunk_context.todo_list)} tasks")
        return trunk_context

    def load_trunk_context(self) -> Optional[TrunkContext]:
        """Load the trunk context from file."""
        if not self.trunk_context_file:
            return None
        
        try:
            if self.orchestrator:
                # Check file existence in container
                check_cmd = f"test -f {self.trunk_context_file}"
                check_result = self.orchestrator.execute_command(check_cmd)
                if not (check_result.get("success") or check_result.get("exit_code") == 0):
                    logger.warning(f"Trunk context file not found in container: {self.trunk_context_file}")
                    return None

                # Load from container
                cat_result = self.orchestrator.execute_command(f"cat {self.trunk_context_file}")
                if cat_result.get("success") or cat_result.get("exit_code") == 0:
                    data = json.loads(cat_result["output"])
                    return TrunkContext(**data)
                else:
                    logger.error(f"Failed to read trunk context from container: {cat_result.get('output')}")
                    return None
            else:
                # Load from local file system
                if not Path(self.trunk_context_file).exists():
                    return None
                with open(self.trunk_context_file, "r") as f:
                    data = json.load(f)
                    return TrunkContext(**data)
        except Exception as e:
            logger.error(f"Failed to load trunk context: {e}")
        return None

    def _save_trunk_context(self, trunk_context: TrunkContext):
        """Save trunk context to file."""
        if not self.trunk_context_file:
            raise ValueError("No trunk context file path set")
        
        try:
            context_data = json.dumps(trunk_context.model_dump(), default=str, indent=2)
            
            if self.orchestrator:
                # SIMPLIFIED: Save trunk context using cat with heredoc (consistent with branch history)
                # Create directory first
                dir_path = str(Path(self.trunk_context_file).parent)
                mkdir_result = self.orchestrator.execute_command(f"mkdir -p {dir_path}")
                if not (mkdir_result.get("success") or mkdir_result.get("exit_code") == 0):
                    logger.warning(f"Failed to create directory {dir_path}: {mkdir_result.get('output', '')}")
                
                # Use cat with heredoc - much simpler and more transparent
                save_command = f"""cat > {self.trunk_context_file} << 'CONTEXT_EOF'
{context_data}
CONTEXT_EOF"""
                
                result = self.orchestrator.execute_command(save_command)
                if not (result.get("success") or result.get("exit_code") == 0):
                    raise Exception(f"Failed to save trunk context: {result.get('output', '')}")
                    
                logger.debug(f"Saved trunk context using heredoc to: {self.trunk_context_file}")
            else:
                # Save to local file system
                Path(self.trunk_context_file).parent.mkdir(parents=True, exist_ok=True)
                with open(self.trunk_context_file, "w") as f:
                    f.write(context_data)
            
            logger.debug(f"Saved trunk context to: {self.trunk_context_file}")
        except Exception as e:
            logger.error(f"Failed to save trunk context: {e}")
            raise

    def start_new_branch(self, task_id: str) -> Dict[str, Any]:
        """Start a new branch task."""
        # Load trunk context
        trunk_context = self.load_trunk_context()
        if not trunk_context:
            raise ValueError("No trunk context exists")

        # Validate if task can be started (strict order)
        if not trunk_context.can_start_task(task_id):
            next_task = trunk_context.get_next_pending_task()
            if next_task:
                raise ValueError(f"Cannot start task {task_id}. Must complete {next_task.id} first.")
            else:
                raise ValueError(f"Task {task_id} cannot be started. Check task status.")
        
        # Find task information
        task = None
        for t in trunk_context.todo_list:
            if t.id == task_id:
                task = t
                break
        
        if not task:
            raise ValueError(f"Task {task_id} not found in TODO list")
        
        # Get key results from previous completed task
        previous_summary = ""
        for i, t in enumerate(trunk_context.todo_list):
            if t.id == task_id and i > 0:
                prev_task = trunk_context.todo_list[i-1]
                if prev_task.status == TaskStatus.COMPLETED and prev_task.key_results:
                    previous_summary = f"Previous task ({prev_task.id}): {prev_task.key_results}"
                break
        
        # Create branch history file
        branch_history = BranchContextHistory(
            task_id=task_id,
            task_description=task.description,
            previous_task_summary=previous_summary
        )
        
        # Save branch history file
        branch_file = str(self.contexts_dir / f"{task_id}.json")
        self._save_branch_history(branch_history, branch_file)
        
        # Update task status in trunk context
        trunk_context.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        self._save_trunk_context(trunk_context)
        
        # Set current task
        self.current_task_id = task_id
        
        logger.info(f"Started branch task: {task_id}")
        
        return {
            "task_id": task_id,
            "task_description": task.description,
            "previous_summary": previous_summary,
            "branch_file": branch_file
        }

    def load_branch_history(self, task_id: str) -> Optional[BranchContextHistory]:
        """Load branch history from file."""
        branch_file = str(self.contexts_dir / f"{task_id}.json")
        
        try:
            if self.orchestrator:
                # Check file existence in container
                check_cmd = f"test -f {branch_file}"
                check_result = self.orchestrator.execute_command(check_cmd)
                if not (check_result.get("success") or check_result.get("exit_code") == 0):
                    logger.warning(f"Branch history file not found in container: {branch_file}")
                    return None

                # Load from container
                cat_result = self.orchestrator.execute_command(f"cat {branch_file}")
                if cat_result.get("success") or cat_result.get("exit_code") == 0:
                    data = json.loads(cat_result["output"])
                    return BranchContextHistory(**data)
                else:
                    logger.error(f"Failed to read branch history from container: {cat_result.get('output')}")
                    return None
            else:
                # Load from local file system
                if Path(branch_file).exists():
                    with open(branch_file, "r") as f:
                        data = json.load(f)
                        return BranchContextHistory(**data)
        except Exception as e:
            logger.error(f"Failed to load branch history for {task_id}: {e}")
        return None

    def _save_branch_history(self, branch_history: BranchContextHistory, branch_file: str):
        """Save branch history to file."""
        try:
            context_data = json.dumps(branch_history.model_dump(), default=str, indent=2)
            
            if self.orchestrator:
                # SIMPLIFIED: Save in container using cat with heredoc (much cleaner!)
                # Create directory first
                dir_path = str(Path(branch_file).parent)
                mkdir_result = self.orchestrator.execute_command(f"mkdir -p {dir_path}")
                if not (mkdir_result.get("success") or mkdir_result.get("exit_code") == 0):
                    logger.warning(f"Failed to create directory {dir_path}: {mkdir_result.get('output', '')}")
                
                # Use cat with heredoc to write file - much simpler and more transparent
                save_command = f"""cat > {branch_file} << 'CONTEXT_EOF'
{context_data}
CONTEXT_EOF"""
                
                result = self.orchestrator.execute_command(save_command)
                if not (result.get("success") or result.get("exit_code") == 0):
                    raise Exception(f"Failed to save branch history: {result.get('output', '')}")
                    
                logger.debug(f"Saved branch history using heredoc to: {branch_file}")
            else:
                # Save to local file system
                Path(branch_file).parent.mkdir(parents=True, exist_ok=True)
                with open(branch_file, "w") as f:
                    f.write(context_data)
            
            logger.debug(f"Saved branch history to: {branch_file}")
        except Exception as e:
            logger.error(f"Failed to save branch history: {e}")
            raise

    def add_to_branch_history(self, task_id: str, new_entry: Dict[str, Any]) -> Dict[str, Any]:
        """Add entry to branch history with type safety, returns status info (including compression needs)"""
        # Load branch history
        branch_history = self.load_branch_history(task_id)
        if not branch_history:
            raise ValueError(f"No branch history found for task {task_id}")
        
        # CRITICAL FIX: Ensure new_entry is always a dictionary
        if not isinstance(new_entry, dict):
            # Handle non-dict entries by wrapping them safely
            if isinstance(new_entry, str):
                new_entry = {"content": new_entry}
            else:
                new_entry = {"data": str(new_entry)}
            logger.info(f"🔧 Context entry auto-wrapped: {type(new_entry).__name__} → dict")
        
        # Ensure we have a mutable copy
        new_entry = dict(new_entry)
        
        # Add timestamp safely
        if "timestamp" not in new_entry:
            new_entry["timestamp"] = datetime.now().isoformat()
        
        # Add entry and check if compression is needed
        needs_compression = branch_history.add_entry(new_entry)
        
        # Save updated history
        branch_file = str(self.contexts_dir / f"{task_id}.json")
        self._save_branch_history(branch_history, branch_file)
        
        result = {
            "success": True,
            "entry_count": branch_history.entry_count,
            "token_count": branch_history.token_count,
            "needs_compression": needs_compression
        }
        
        if needs_compression:
            result["compression_warning"] = f"Context size ({branch_history.token_count} tokens) exceeds threshold ({branch_history.context_window_threshold}). Consider compression."
        
        logger.debug(f"Added entry to branch {task_id}, total entries: {branch_history.entry_count}")
        return result

    def compact_branch_history(self, task_id: str, compacted_history: List[Dict[str, Any]]) -> bool:
        """Replace current history with compressed history"""
        # Load branch history
        branch_history = self.load_branch_history(task_id)
        if not branch_history:
            raise ValueError(f"No branch history found for task {task_id}")
        
        # Replace history
        branch_history.replace_history(compacted_history)
        
        # Save updated history
        branch_file = str(self.contexts_dir / f"{task_id}.json")
        self._save_branch_history(branch_history, branch_file)
        
        logger.info(f"Compacted branch history for {task_id}: {len(compacted_history)} entries, {branch_history.token_count} tokens")
        return True

    def complete_branch(self, task_id: str, summary: str) -> Dict[str, Any]:
        """Complete branch task, update trunk context, and return next task info"""
        # Load trunk context
        trunk_context = self.load_trunk_context()
        if not trunk_context:
            raise ValueError("No trunk context exists")

        # Update task status and key results
        trunk_context.update_task_status(task_id, TaskStatus.COMPLETED, summary)
        trunk_context.update_task_key_results(task_id, summary)
        
        # Save trunk context
        self._save_trunk_context(trunk_context)
        
        # Clear current task
        self.current_task_id = None
        
        # Get next task
        next_task = trunk_context.get_next_pending_task()
        
        result = {
            "completed_task": task_id,
            "summary": summary,
            "progress": trunk_context.get_progress_summary()
        }
        
        if next_task:
            result["next_task"] = {
                "id": next_task.id,
                "description": next_task.description,
                "status": next_task.status
            }
        else:
            result["all_tasks_completed"] = True
        
        logger.info(f"Completed task {task_id}")
        return result

    def get_current_context_info(self) -> Dict[str, Any]:
        """Get current context information - improved implementation that auto-discovers trunk context"""
        if self.current_task_id:
            # Currently in branch task
            branch_history = self.load_branch_history(self.current_task_id)
            if branch_history:
                return {
                    "context_type": "branch",
                    "task_id": self.current_task_id,
                    "task_description": branch_history.task_description,
                    "entry_count": branch_history.entry_count,
                    "token_count": branch_history.token_count,
                    "needs_compression": branch_history.token_count > branch_history.context_window_threshold,
                    "last_updated": branch_history.last_updated.isoformat()
                }
        
        # Currently in trunk context - auto-discover trunk context file
        trunk_context = self.load_trunk_context()
        
        # If trunk_context_file is not set, try to find it automatically
        if not trunk_context and not self.trunk_context_file:
            self._auto_discover_trunk_context()
            trunk_context = self.load_trunk_context()
        
        if trunk_context:
            next_task = trunk_context.get_next_pending_task()
            return {
                "context_type": "trunk",
                "context_id": trunk_context.context_id,
                "goal": trunk_context.goal,
                "progress": trunk_context.get_progress_summary(),
                "next_task": next_task.description if next_task else "No pending tasks",
                "next_task_id": next_task.id if next_task else None,
                "last_updated": trunk_context.last_updated.isoformat()
            }
        
        return {"error": "No active context"}

    def _auto_discover_trunk_context(self) -> bool:
        """Auto-discover trunk context file when trunk_context_file is not set"""
        try:
            if self.orchestrator:
                # Search in container
                result = self.orchestrator.execute_command(
                    f"find {self.contexts_dir} -name 'trunk_*.json' -type f 2>/dev/null | head -1"
                )
                if (result.get("success") or result.get("exit_code") == 0) and result.get("output", "").strip():
                    found_file = result["output"].strip()
                    if found_file:
                        self.trunk_context_file = found_file
                        logger.info(f"Auto-discovered trunk context file: {found_file}")
                        return True
            else:
                # Fallback to local file system
                trunk_files = list(self.contexts_dir.glob("trunk_*.json"))
                if trunk_files:
                    # Use the most recent trunk context file
                    trunk_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                    self.trunk_context_file = str(trunk_files[0])
                    logger.info(f"Auto-discovered trunk context file: {self.trunk_context_file}")
                    return True
        except Exception as e:
            logger.warning(f"Failed to auto-discover trunk context: {e}")
        
        return False

    def find_existing_trunk_context(self, project_name: str) -> Optional[str]:
        """Find existing trunk context file path"""
        if self.orchestrator:
            # Search in container
            result = self.orchestrator.execute_command(
                f"find {self.contexts_dir} -name 'trunk_*.json' -type f 2>/dev/null || true"
            )
            if (result.get("success") or result.get("exit_code") == 0) and result.get("output", "").strip():
                context_files = result["output"].strip().split("\n")

                for context_file in context_files:
                    if context_file.strip():
                        try:
                            cat_result = self.orchestrator.execute_command(f"cat {context_file}")
                            if cat_result.get("success") or cat_result.get("exit_code") == 0:
                                data = json.loads(cat_result["output"])
                                if data.get("project_name") == project_name:
                                    self.trunk_context_file = context_file.strip()
                                    return context_file.strip()
                        except Exception as e:
                            logger.warning(f"Failed to load context file {context_file}: {e}")
        else:
            # Fallback to local file system
            for context_file in self.contexts_dir.glob("trunk_*.json"):
                try:
                    with open(context_file, "r") as f:
                        data = json.load(f)
                        if data.get("project_name") == project_name:
                            self.trunk_context_file = str(context_file)
                            return str(context_file)
                except Exception as e:
                    logger.warning(f"Failed to load context file {context_file}: {e}")
        return None

    def load_or_create_trunk_context(self, goal: str, project_url: str, project_name: str, tasks: List[str] = None) -> TrunkContext:
        """Load or create trunk context"""
        # Try to find existing trunk context
        existing_file = self.find_existing_trunk_context(project_name)
        
        if existing_file:
            self.trunk_context_file = existing_file
            trunk_context = self.load_trunk_context()
            if trunk_context:
                logger.info(f"Loaded existing trunk context: {trunk_context.context_id}")
                return trunk_context
        
        # No existing context found, create new one
        return self.create_trunk_context(goal, project_url, project_name, tasks)
