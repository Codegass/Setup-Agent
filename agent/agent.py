"""Main Setup Agent that orchestrates project setup."""

import re
from typing import Optional, List
from urllib.parse import urlparse

from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

from config import Config
from tools import BashTool, FileIOTool, WebSearchTool, ContextTool
from docker_orch.orch import DockerOrchestrator
from .context_manager import ContextManager
from .react_engine import ReActEngine


class SetupAgent:
    """Main agent that orchestrates project setup."""
    
    def __init__(self, config: Config, orchestrator: DockerOrchestrator, max_iterations: int = 50):
        self.config = config
        self.orchestrator = orchestrator
        self.max_iterations = max_iterations
        self.console = Console()
        
        # Initialize context manager
        self.context_manager = ContextManager(workspace_path=config.workspace_path)
        
        # Initialize tools
        self.tools = self._initialize_tools()
        
        # Initialize ReAct engine
        self.react_engine = ReActEngine(
            context_manager=self.context_manager,
            tools=self.tools
        )
        
        logger.info("Setup Agent initialized")
    
    def _initialize_tools(self) -> List:
        """Initialize all available tools."""
        tools = [
            BashTool(),
            FileIOTool(),
            WebSearchTool(),
            ContextTool(self.context_manager)
        ]
        
        logger.info(f"Initialized {len(tools)} tools: {[tool.name for tool in tools]}")
        return tools
    
    def setup_project(self, project_url: str, project_name: str, goal: str, 
                     interactive: bool = False) -> bool:
        """Setup a project from scratch."""
        
        self.console.print(Panel.fit(
            f"[bold blue]Setting up project: {project_name}[/bold blue]\n"
            f"[dim]Repository: {project_url}[/dim]\n"
            f"[dim]Goal: {goal}[/dim]",
            border_style="blue"
        ))
        
        try:
            # Step 1: Setup Docker environment
            if not self._setup_docker_environment(project_name):
                return False
            
            # Step 2: Initialize trunk context
            trunk_context = self.context_manager.create_trunk_context(
                goal=goal,
                project_url=project_url,
                project_name=project_name
            )
            
            # Step 3: Analyze project and create initial TODO list
            if not self._analyze_project_and_create_todos(project_url, project_name):
                return False
            
            # Step 4: Run the main setup loop
            success = self._run_setup_loop(interactive)
            
            # Step 5: Cleanup and summary
            self._provide_setup_summary(success)
            
            return success
            
        except Exception as e:
            logger.error(f"Setup failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Setup failed: {e}[/bold red]")
            return False
    
    def continue_project(self, project_name: str, additional_request: Optional[str] = None) -> bool:
        """Continue working on an existing project."""
        
        self.console.print(Panel.fit(
            f"[bold green]Continuing work on: {project_name}[/bold green]\n"
            f"[dim]Additional request: {additional_request or 'General improvements'}[/dim]",
            border_style="green"
        ))
        
        try:
            # Step 1: Ensure Docker container is running
            if not self._ensure_container_running(project_name):
                return False
            
            # Step 2: Load existing trunk context
            trunk_context = self.context_manager.load_or_create_trunk_context(
                goal=f"Continue working on {project_name}",
                project_url="",  # Will be loaded from existing context
                project_name=project_name
            )
            
            # Step 3: Add additional request as new task if provided
            if additional_request:
                trunk_context.add_task(f"Handle additional request: {additional_request}")
            
            # Step 4: Run the continuation loop
            success = self._run_setup_loop(interactive=True)
            
            # Step 5: Provide summary
            self._provide_setup_summary(success)
            
            return success
            
        except Exception as e:
            logger.error(f"Continue project failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Continue project failed: {e}[/bold red]")
            return False
    
    def run_task(self, project_name: str, task_description: str) -> bool:
        """Run a specific task on an existing project."""
        
        self.console.print(Panel.fit(
            f"[bold cyan]Running task on: {project_name}[/bold cyan]\n"
            f"[dim]Task: {task_description}[/dim]",
            border_style="cyan"
        ))
        
        try:
            # Step 1: Ensure Docker container is running
            if not self._ensure_container_running(project_name):
                return False
            
            # Step 2: Load existing trunk context
            trunk_context = self.context_manager.load_or_create_trunk_context(
                goal=f"Complete task: {task_description}",
                project_url="",  # Will be loaded from existing context
                project_name=project_name
            )
            
            # Step 3: Add the specific task
            trunk_context.add_task(task_description)
            
            # Step 4: Create task-specific prompt
            task_prompt = f"""
I need to work on the project '{project_name}' and complete the following task:

TASK: {task_description}

I should:
1. First check my current context using manage_context tool
2. Understand the current state of the project
3. Plan the approach for completing this task
4. Execute the necessary steps
5. Verify the task is completed successfully

Please start by checking the current context and then proceed with the task.
"""
            
            self.console.print(f"[dim]🔧 Executing task: {task_description[:50]}...[/dim]")
            
            # Step 5: Run the task execution loop
            success = self.react_engine.run_react_loop(
                initial_prompt=task_prompt,
                max_iterations=self.max_iterations
            )
            
            # Step 6: Update last comment in container
            if success:
                comment = f"Task completed: {task_description}"
                self.orchestrator.update_last_comment(comment)
                self.console.print(f"[bold green]✅ Task completed successfully![/bold green]")
            else:
                comment = f"Task in progress: {task_description}"
                self.orchestrator.update_last_comment(comment)
                self.console.print(f"[bold yellow]⚠️ Task may be incomplete.[/bold yellow]")
            
            # Step 7: Provide execution summary
            self._provide_task_summary(success, task_description)
            
            return success
            
        except Exception as e:
            logger.error(f"Task execution failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Task execution failed: {e}[/bold red]")
            
            # Update last comment with error
            error_comment = f"Task failed: {task_description} - Error: {str(e)[:100]}"
            self.orchestrator.update_last_comment(error_comment)
            
            return False
    
    def _setup_docker_environment(self, project_name: str) -> bool:
        """Setup the Docker environment for the project."""
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console
        ) as progress:
            task = progress.add_task("Setting up Docker environment...", total=None)
            
            try:
                # Create and start container
                success = self.orchestrator.create_and_start_container()
                
                if success:
                    progress.update(task, description="✅ Docker environment ready")
                    logger.info("Docker environment setup completed")
                    return True
                else:
                    progress.update(task, description="❌ Docker environment setup failed")
                    logger.error("Docker environment setup failed")
                    return False
                    
            except Exception as e:
                progress.update(task, description=f"❌ Docker setup error: {e}")
                logger.error(f"Docker setup error: {e}")
                return False
    
    def _ensure_container_running(self, project_name: str) -> bool:
        """Ensure the Docker container is running."""
        
        try:
            if not self.orchestrator.container_exists():
                self.console.print(f"[bold red]❌ No container found for project: {project_name}[/bold red]")
                return False
            
            if not self.orchestrator.is_container_running():
                self.console.print("[yellow]⚠️ Container is not running. Starting...[/yellow]")
                return self.orchestrator.start_container()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to ensure container running: {e}")
            return False
    
    def _analyze_project_and_create_todos(self, project_url: str, project_name: str) -> bool:
        """Analyze the project and create initial TODO list."""
        
        # Create initial prompt for project analysis
        initial_prompt = f"""
I need to setup the project '{project_name}' from the repository: {project_url}

My goal is to analyze this project and create a comprehensive TODO list for setting it up.

First, I should:
1. Use manage_context tool to check my current context
2. Clone the repository using bash tool
3. Analyze the project structure (README, package files, etc.)
4. Create a detailed TODO list based on the project requirements
5. Begin working on the tasks systematically

Please start by checking the current context and then proceed with the setup.
"""
        
        self.console.print("[dim]🔍 Analyzing project and creating TODO list...[/dim]")
        
        # Run initial analysis
        success = self.react_engine.run_react_loop(
            initial_prompt=initial_prompt,
            max_iterations=10  # Limit iterations for initial analysis
        )
        
        if success:
            self.console.print("[green]✅ Project analysis completed[/green]")
            return True
        else:
            self.console.print("[red]❌ Project analysis failed[/red]")
            return False
    
    def _run_setup_loop(self, interactive: bool = False) -> bool:
        """Run the main setup loop."""
        
        self.console.print("[dim]🚀 Starting main setup process...[/dim]")
        
        # Create main setup prompt
        main_prompt = """
Continue with the project setup process. 

Current status:
- You should have a trunk context with a TODO list
- Work through the TODO list systematically
- Use branch contexts for focused work on individual tasks
- Return to trunk context after completing each task with a summary

Your approach should be:
1. Check current context and TODO list status
2. If in trunk context: select next pending task and create branch context
3. If in branch context: work on the current task with focus
4. Switch back to trunk when task is complete with summary
5. Repeat until all tasks are completed

Be thorough and methodical. Use web search when you encounter unknown issues.
"""
        
        # Run the main setup loop
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console
        ) as progress:
            task = progress.add_task("Running setup process...", total=None)
            
            success = self.react_engine.run_react_loop(
                initial_prompt=main_prompt,
                max_iterations=self.max_iterations
            )
            
            if success:
                progress.update(task, description="✅ Setup process completed")
            else:
                progress.update(task, description="❌ Setup process incomplete")
        
        return success
    
    def _provide_setup_summary(self, success: bool):
        """Provide a summary of the setup process."""
        
        # Get execution summary
        exec_summary = self.react_engine.get_execution_summary()
        
        # Get context info
        context_info = self.context_manager.get_current_context_info()
        
        # Create summary panel
        if success:
            status = "[bold green]✅ COMPLETED[/bold green]"
            border_style = "green"
        else:
            status = "[bold yellow]⚠️ INCOMPLETE[/bold yellow]"
            border_style = "yellow"
        
        summary_text = f"""
{status}

[bold]Execution Statistics:[/bold]
• Total Steps: {exec_summary['total_steps']}
• Iterations: {exec_summary['iterations']}
• Thoughts: {exec_summary['thoughts']}
• Actions: {exec_summary['actions']}
• Successful Actions: {exec_summary['successful_actions']}
• Failed Actions: {exec_summary['failed_actions']}

[bold]Final Context:[/bold]
• Context Type: {context_info.get('context_type', 'Unknown')}
• Context ID: {context_info.get('context_id', 'Unknown')}
"""
        
        if context_info.get('context_type') == 'trunk':
            summary_text += f"• Progress: {context_info.get('progress', 'Unknown')}"
        
        self.console.print(Panel(
            summary_text,
            title="[bold]Setup Summary[/bold]",
            border_style=border_style
        ))
        
        # Show TODO list status if in trunk context
        if (context_info.get('context_type') == 'trunk' and 
            self.context_manager.trunk_context):
            
            self.console.print("\n[bold]Final TODO List Status:[/bold]")
            
            for task in self.context_manager.trunk_context.todo_list:
                status_icon = {
                    "pending": "⏳",
                    "in_progress": "🔄",
                    "completed": "✅", 
                    "failed": "❌"
                }.get(task.status, "❓")
                
                self.console.print(f"  {status_icon} {task.description}")
                if task.notes:
                    self.console.print(f"    [dim]Notes: {task.notes}[/dim]")
        
        # Log detailed summary
        logger.info(f"Setup summary: {exec_summary}")
        
        # Provide next steps
        if success:
            self.console.print(f"\n[bold green]🎉 Project setup completed successfully![/bold green]")
            self.console.print(f"[dim]You can now connect to the container using:[/dim]")
            self.console.print(f"  setup-agent connect {context_info.get('project_name', 'project')}")
        else:
            self.console.print(f"\n[bold yellow]⚠️ Setup process incomplete.[/bold yellow]")
            self.console.print(f"[dim]You can continue the setup using:[/dim]")
            self.console.print(f"  setup-agent continue {context_info.get('project_name', 'project')}")
    
    def _provide_task_summary(self, success: bool, task_description: str):
        """Provide a summary of task execution."""
        
        # Get execution summary
        summary = self.react_engine.get_execution_summary()
        
        if success:
            self.console.print(Panel.fit(
                f"[bold green]✅ Task Completed Successfully[/bold green]\n"
                f"[dim]Task: {task_description}[/dim]\n"
                f"[dim]Execution Summary:[/dim]\n"
                f"[dim]• Total steps: {summary['total_steps']}[/dim]\n"
                f"[dim]• Iterations: {summary['iterations']}[/dim]\n"
                f"[dim]• Thinking model calls: {summary.get('thinking_model_calls', 0)}[/dim]\n"
                f"[dim]• Action model calls: {summary.get('action_model_calls', 0)}[/dim]\n"
                f"[dim]• Successful actions: {summary['successful_actions']}/{summary['actions']}[/dim]",
                border_style="green"
            ))
        else:
            self.console.print(Panel.fit(
                f"[bold yellow]⚠️ Task May Be Incomplete[/bold yellow]\n"
                f"[dim]Task: {task_description}[/dim]\n"
                f"[dim]Execution Summary:[/dim]\n"
                f"[dim]• Total steps: {summary['total_steps']}[/dim]\n"
                f"[dim]• Iterations: {summary['iterations']}[/dim]\n"
                f"[dim]• Thinking model calls: {summary.get('thinking_model_calls', 0)}[/dim]\n"
                f"[dim]• Action model calls: {summary.get('action_model_calls', 0)}[/dim]\n"
                f"[dim]• Successful actions: {summary['successful_actions']}/{summary['actions']}[/dim]",
                border_style="yellow"
            ))
        
        logger.info(f"Task execution completed for {self.orchestrator.project_name}: {task_description}")
    
    def _extract_project_name_from_url(self, project_url: str) -> str:
        """Extract project name from Git URL."""
        try:
            # Parse URL
            parsed = urlparse(project_url)
            
            # Get the last part of the path
            path_parts = parsed.path.strip('/').split('/')
            if path_parts:
                project_name = path_parts[-1]
                # Remove .git extension if present
                if project_name.endswith('.git'):
                    project_name = project_name[:-4]
                return project_name
            
            return "unknown-project"
            
        except Exception as e:
            logger.warning(f"Failed to extract project name from URL: {e}")
            return "unknown-project"
    
    def get_status(self) -> dict:
        """Get current agent status."""
        return {
            "context_info": self.context_manager.get_current_context_info(),
            "execution_summary": self.react_engine.get_execution_summary(),
            "container_status": self.orchestrator.get_container_info() if self.orchestrator else None
        }
