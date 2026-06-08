"""Main Setup Agent that orchestrates project setup."""

import json
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from sag.config import Config, create_agent_logger, create_command_logger, get_session_logger
from sag.docker_orch.orch import DockerOrchestrator
from sag.ui import EventType, PhaseType, UIEvent, UIManager

from .context_manager import ContextManager
from .react_engine import ReActEngine


class SetupAgent:
    """Main agent that orchestrates project setup."""

    def __init__(
        self, config: Config, orchestrator: DockerOrchestrator, max_iterations: Optional[int] = None
    ):
        self.config = config
        self.orchestrator = orchestrator
        self.max_iterations = (
            max_iterations if max_iterations is not None else config.max_iterations
        )
        self.console = Console()

        # UI Manager for enhanced UI mode
        self.ui_manager: Optional[UIManager] = None

        # Context manager will be initialized after Docker setup
        self.context_manager = None
        self.tools = None
        self.react_engine = None
        # PhysicalValidator is set during _initialize_tools() once orchestrator
        # is available; declared here so attribute access is always safe.
        self.physical_validator = None

        # Create specialized agent logger
        self.agent_logger = create_agent_logger("setup_agent")
        self.agent_logger.info(
            "Setup Agent initialized (context manager will be initialized after Docker setup)"
        )

    def _emit(
        self,
        event_type: "EventType",
        message: str,
        *,
        phase: "Optional[PhaseType]" = None,
        details: Optional[str] = None,
        level: str = "info",
    ) -> None:
        """Emit a UI event when UI mode is active; no-op otherwise.

        Replaces the repeated `if self.config.ui_mode: self.ui_manager.handle_event(UIEvent(...))`
        pattern so call sites stay focused on event intent, not lifecycle.
        """
        if not (self.config.ui_mode and self.ui_manager):
            return
        self.ui_manager.handle_event(
            UIEvent(
                event_type=event_type,
                message=message,
                phase=phase,
                details=details,
                level=level,
            )
        )

    def _initialize_context_and_tools(self):
        """Initialize context manager, tools, and react engine after Docker is ready."""
        if self.context_manager is not None:
            return  # Already initialized

        # Initialize ErrorLogger with container workspace path
        from sag.agent.error_logger import ErrorLogger

        error_logger = ErrorLogger.get_instance(
            workspace_path=self.config.workspace_path,
            session_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        self.agent_logger.info(
            f"ErrorLogger initialized with workspace: {self.config.workspace_path}"
        )

        # Initialize context manager with container-based workspace
        self.context_manager = ContextManager(
            workspace_path=self.config.workspace_path,
            orchestrator=self.orchestrator,  # Pass orchestrator for container operations
        )

        # Initialize tools
        self.tools = self._initialize_tools()

        # Initialize ReAct engine (repository URL will be set later)
        self.react_engine = ReActEngine(context_manager=self.context_manager, tools=self.tools)

        # Pass UIManager to ReActEngine if in UI mode
        if self.config.ui_mode and self.ui_manager:
            self.react_engine.set_ui_manager(self.ui_manager)

            # Also set UI manager for all tools that support it
            from sag.ui.events import UIEventEmitter

            for tool in self.tools:
                if isinstance(tool, UIEventEmitter):
                    tool.set_ui_manager(self.ui_manager)
                    logger.debug(f"Set UI manager for tool: {tool.name}")

        self.agent_logger.info("Context manager, tools, and ReAct engine initialized")

    def _initialize_tools(self) -> List:
        """Initialize all available tools."""
        from sag.agent.physical_validator import PhysicalValidator
        from sag.tools.bash import BashTool, BashToolConfig
        from sag.tools.context_tool import ContextTool
        from sag.tools.env_tool import EnvTool
        from sag.tools.file_io import FileIOTool
        from sag.tools.gradle_tool import GradleTool
        from sag.tools.maven_tool import MavenTool
        from sag.tools.output_search_tool import OutputSearchTool
        from sag.tools.project_analyzer import ProjectAnalyzerTool
        from sag.tools.project_setup_tool import ProjectSetupTool
        from sag.tools.report_tool import ReportTool
        from sag.tools.system_tool import SystemTool
        from sag.tools.web_search import WebSearchTool

        # Configure bash tool with enhanced features
        bash_config = BashToolConfig(
            enable_background_processes=True,
            block_interactive_commands=True,
            audit_command_execution=False,  # Can be enabled for debugging
            add_sag_cli_marker=True,
        )

        # PhysicalValidator is the canonical source of truth for build/test
        # status — store it on the instance so other methods can reuse it
        # without paying the cost of re-initialising it lazily.
        self.physical_validator = PhysicalValidator(
            docker_orchestrator=self.orchestrator, project_path=self.config.workspace_path
        )

        tools = [
            BashTool(self.orchestrator, config=bash_config),
            FileIOTool(self.orchestrator),
            WebSearchTool(),
            ContextTool(self.context_manager),
            MavenTool(self.orchestrator),
            GradleTool(self.orchestrator),
            ProjectSetupTool(self.orchestrator),
            SystemTool(self.orchestrator),
            EnvTool(self.orchestrator),
            ProjectAnalyzerTool(self.orchestrator, self.context_manager),
            OutputSearchTool(
                orchestrator=self.orchestrator, contexts_dir=self.context_manager.contexts_dir
            ),
            ReportTool(
                self.orchestrator,
                execution_history_callback=self._get_execution_history,
                context_manager=self.context_manager,
                physical_validator=self.physical_validator,
            ),
        ]

        logger.info(f"Initialized {len(tools)} tools: {[tool.name for tool in tools]}")
        return tools

    def _get_execution_history(self):
        """Get execution history for report verification."""
        if hasattr(self, "react_engine") and self.react_engine:
            try:
                summary = self.react_engine.get_execution_summary()
            except Exception:
                summary = {}

            return {
                "steps": list(self.react_engine.steps),
                "summary": summary,
                "current_iteration": getattr(self.react_engine, "current_iteration", 0),
            }
        return []

    def _save_project_metadata(
        self,
        project_url: str,
        project_name: str,
        docker_label: str,
        goal: str,
        project_ref: Optional[str] = None,
    ) -> bool:
        """
        Save project metadata to /workspace/.setup_agent/project_meta.json.

        This file allows the 'run' command to know the actual project directory
        without needing to probe the filesystem, especially important when
        --name was used to give a custom Docker container name.

        Args:
            project_url: The Git repository URL
            project_name: The actual project directory name (from URL)
            docker_label: The Docker container label (from --name or project_name)
            goal: The setup goal description

        Returns:
            True if metadata was saved successfully, False otherwise
        """
        try:
            metadata = {
                "project_name": project_name,
                "project_url": project_url,
                "project_ref": project_ref,
                "docker_label": docker_label,
                "goal": goal,
                "created_at": datetime.now().isoformat(),
                "version": "1.0",
            }

            # Ensure .setup_agent directory exists
            mkdir_result = self.orchestrator.execute_command("mkdir -p /workspace/.setup_agent")

            if mkdir_result.get("exit_code") != 0:
                logger.warning(
                    f"Failed to create .setup_agent directory: {mkdir_result.get('output')}"
                )
                return False

            # Write metadata as JSON
            metadata_json = json.dumps(metadata, indent=2)
            # Escape single quotes for shell command
            escaped_json = metadata_json.replace("'", "'\\''")

            write_result = self.orchestrator.execute_command(
                f"echo '{escaped_json}' > /workspace/.setup_agent/project_meta.json"
            )

            if write_result.get("exit_code") == 0:
                logger.info(
                    f"✅ Saved project metadata: project_name={project_name}, docker_label={docker_label}"
                )
                self.agent_logger.info(
                    f"Project metadata saved to /workspace/.setup_agent/project_meta.json"
                )
                return True
            else:
                logger.warning(f"Failed to write project metadata: {write_result.get('output')}")
                return False

        except Exception as e:
            logger.warning(f"Failed to save project metadata: {e}")
            return False

    def setup_project(
        self,
        project_url: str,
        project_name: str,
        goal: str,
        interactive: bool = False,
        docker_label: Optional[str] = None,
        project_ref: Optional[str] = None,
    ) -> bool:
        """Setup a project from scratch.

        Args:
            project_url: Git repository URL
            project_name: Actual project directory name (extracted from URL)
            goal: Setup goal description
            interactive: Whether to run in interactive mode
            docker_label: Docker container label (from --name flag).
                         If None, defaults to project_name.
            project_ref: Optional Git ref-ish handle to checkout during clone.
        """
        # Default docker_label to project_name if not provided
        if docker_label is None:
            docker_label = project_name

        # Create command-specific logger
        cmd_logger, cmd_logger_id = create_command_logger("project", project_name)
        cmd_logger.info(f"Starting project setup: {project_name} (docker_label={docker_label})")

        try:
            # Initialize UI Manager if in UI mode
            if self.config.ui_mode:
                self.ui_manager = UIManager(project_name=project_name, console=self.console)
                self.ui_manager.start()
            else:
                ref_line = f"\n[dim]Repository Ref: {project_ref}[/dim]" if project_ref else ""
                self.console.print(
                    Panel.fit(
                        f"[bold blue]Setting up project: {project_name}[/bold blue]\n"
                        f"[dim]Repository: {project_url}[/dim]\n"
                        f"[dim]Goal: {goal}[/dim]"
                        f"{ref_line}",
                        border_style="blue",
                    )
                )

            # Step 1: Setup Docker environment
            self._emit(EventType.PHASE_START, "Setting up environment", phase=PhaseType.SETUP)

            if not self._setup_docker_environment(project_name):
                return False

            # Step 1.5: Initialize context manager and tools now that Docker is ready
            self._emit(
                EventType.STEP_START,
                "Context Initialization",
                phase=PhaseType.SETUP,
                details="Initializing context system...",
            )
            self._emit(EventType.STATUS_UPDATE, "Loading tools...", phase=PhaseType.SETUP)

            self._initialize_context_and_tools()

            # Step 1.6: Set repository URL for ReAct engine
            self.react_engine.set_repository_url(project_url, repository_ref=project_ref)

            self._emit(
                EventType.STATUS_UPDATE, "Configuring ReAct engine...", phase=PhaseType.SETUP
            )
            self._emit(
                EventType.STEP_COMPLETE,
                "Context Initialization",
                phase=PhaseType.SETUP,
                details="Context manager ready",
                level="success",
            )

            # Step 2: Initialize trunk context with intelligent planning approach
            # Here we provide the initial steps. Split complex task into clear, executable steps
            # This ensures each critical step is executed independently and cannot be skipped
            # 🎯 CORE FOCUS: Build and Test success are the PRIMARY objectives of setup
            initial_tasks = [
                "Clone repository and setup basic environment (use project_setup tool)",
                "CRITICAL: Run project_analyzer tool with action='analyze' to analyze project structure, count static tests, and generate intelligent execution plan (MUST use project_analyzer tool, do NOT manually read files)",
                "CORE SETUP: Execute build tasks and ensure compilation success (use maven/gradle tools)",
                "CORE SETUP: Execute test suite and ensure all tests pass (use maven/gradle tools)",
                "Generate final completion report with build and test results (use report tool)",
            ]

            logger.info("Creating trunk context with enhanced multi-step planning approach...")
            self.agent_logger.info(f"Creating trunk context for project: {project_name}")

            try:
                trunk_context = self.context_manager.create_trunk_context(
                    goal=goal,
                    project_url=project_url,
                    project_name=project_name,
                    tasks=initial_tasks,
                )

                # Verify trunk context was created successfully
                context_info = self.context_manager.get_current_context_info()
                if context_info.get("error"):
                    raise Exception(f"Failed to verify trunk context: {context_info['error']}")

                self.agent_logger.info(
                    f"✅ Trunk context created successfully: {trunk_context.context_id}"
                )
                logger.info(
                    f"Trunk context created with {len(initial_tasks)} explicit tasks (project_analyzer will be called in task_2)"
                )

                # Step 2.5: Save project metadata for future reference
                # This allows 'run' command to find project directory without probing
                self._save_project_metadata(
                    project_url=project_url,
                    project_name=project_name,
                    docker_label=docker_label,
                    goal=goal,
                    project_ref=project_ref,
                )

            except Exception as e:
                self.agent_logger.error(f"❌ Failed to create trunk context: {e}")
                logger.error(f"Trunk context creation failed: {e}")
                # Always show critical errors
                self.console.print(f"[bold red]❌ Failed to create project context: {e}[/bold red]")
                return False

            # Step 2.5: Complete setup phase
            self._emit(
                EventType.PHASE_COMPLETE,
                "Setup phase completed",
                phase=PhaseType.SETUP,
                level="success",
            )

            # Step 3: Run the unified setup process
            success = self._run_unified_setup(
                project_url,
                project_name,
                goal,
                interactive,
                project_ref=project_ref,
            )

            # Step 5: Handle final status
            if self.config.ui_mode:
                if success:
                    self._emit(
                        EventType.SUCCESS, "Project setup completed successfully", level="success"
                    )
                else:
                    self._emit(EventType.FAILURE, "Project setup incomplete", level="error")
                self.ui_manager.display_final_summary()
            else:
                self._provide_setup_summary(success)

            cmd_logger.info(f"Project setup completed: success={success}")
            return success

        except Exception as e:
            cmd_logger.error(f"Setup failed: {e}", exc_info=True)
            # Always show critical errors
            self.console.print(f"[bold red]❌ Setup failed: {e}[/bold red]")
            return False

        finally:
            # Tear down the live UI on every exit path. display_final_summary
            # is idempotent and abort_running_phases is a no-op if everything
            # already completed, so the happy path tolerates this too.
            if self.ui_manager:
                try:
                    self.ui_manager.abort_running_phases("Phase aborted")
                    self.ui_manager.display_final_summary()
                except Exception as e:
                    logger.warning(f"UI teardown failed during setup_project cleanup: {e}")
                    try:
                        self.ui_manager.stop()
                    except Exception as inner:
                        logger.warning(f"UIManager.stop() also failed: {inner}")

            # Always release the command-specific loguru handler.
            session_logger = get_session_logger()
            if session_logger:
                try:
                    session_logger.cleanup_command_logger(cmd_logger_id)
                except Exception as e:
                    logger.warning(f"cleanup_command_logger failed: {e}")

    def continue_project(self, project_name: str, additional_request: Optional[str] = None) -> bool:
        """Continue working on an existing project."""

        self.console.print(
            Panel.fit(
                f"[bold green]Continuing work on: {project_name}[/bold green]\n"
                f"[dim]Additional request: {additional_request or 'General improvements'}[/dim]",
                border_style="green",
            )
        )

        try:
            # Step 1: Ensure Docker container is running
            if not self._ensure_container_running(project_name):
                return False

            # Step 1.5: Initialize context manager and tools now that Docker is ready
            self._initialize_context_and_tools()

            # Step 2: Load existing trunk context
            logger.info(f"Loading or creating trunk context for project: {project_name}")
            self.agent_logger.info(f"Loading context for project: {project_name}")

            try:
                trunk_context = self.context_manager.load_or_create_trunk_context(
                    goal=f"Continue working on {project_name}",
                    project_url="",  # Will be loaded from existing context
                    project_name=project_name,
                )

                # Verify context was loaded/created successfully
                context_info = self.context_manager.get_current_context_info()
                if context_info.get("error"):
                    raise Exception(f"Failed to load/create context: {context_info['error']}")

                self.agent_logger.info(
                    f"✅ Context loaded successfully: {context_info.get('context_id', 'unknown')}"
                )
                logger.info(f"Trunk context ready for project: {project_name}")

            except Exception as e:
                self.agent_logger.error(f"❌ Failed to load/create context: {e}")
                logger.error(f"Context loading failed: {e}")
                # Always show critical errors
                self.console.print(f"[bold red]❌ Failed to load project context: {e}[/bold red]")
                return False

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
            # Always show critical errors
            self.console.print(f"[bold red]❌ Continue project failed: {e}[/bold red]")
            return False

    def run_task(self, project_name: str, task_description: str) -> bool:
        """Run a specific task on an existing project."""

        # Create command-specific logger
        cmd_logger, cmd_logger_id = create_command_logger("run", project_name)
        cmd_logger.info(f"Starting task execution: {task_description}")

        try:
            # Initialize UI Manager if in UI mode
            if self.config.ui_mode:
                self.ui_manager = UIManager(project_name=project_name, console=self.console)
                self.ui_manager.start()
            else:
                self.console.print(
                    Panel.fit(
                        f"[bold cyan]Running task on: {project_name}[/bold cyan]\n"
                        f"[dim]Task: {task_description}[/dim]",
                        border_style="cyan",
                    )
                )

            # Step 1: Ensure Docker container is running
            self._emit(EventType.PHASE_START, "Preparing environment", phase=PhaseType.SETUP)

            if not self._ensure_container_running(project_name):
                return False

            # Step 1.5: Initialize context manager and tools now that Docker is ready
            self._initialize_context_and_tools()

            # Step 2: Load existing trunk context
            logger.info(f"Loading or creating trunk context for project: {project_name}")
            self.agent_logger.info(f"Loading context for project: {project_name}")

            try:
                trunk_context = self.context_manager.load_or_create_trunk_context(
                    goal=f"Complete task: {task_description}",
                    project_url="",  # Will be loaded from existing context
                    project_name=project_name,
                )

                # Verify context was loaded/created successfully
                context_info = self.context_manager.get_current_context_info()
                if context_info.get("error"):
                    raise Exception(f"Failed to load/create context: {context_info['error']}")

                self.agent_logger.info(
                    f"✅ Context loaded successfully: {context_info.get('context_id', 'unknown')}"
                )
                logger.info(f"Trunk context ready for project: {project_name}")

            except Exception as e:
                self.agent_logger.error(f"❌ Failed to load/create context: {e}")
                logger.error(f"Context loading failed: {e}")
                # Always show critical errors
                self.console.print(f"[bold red]❌ Failed to load project context: {e}[/bold red]")
                return False

            # Step 2.5: Complete setup phase
            self._emit(
                EventType.PHASE_COMPLETE,
                "Environment ready",
                phase=PhaseType.SETUP,
                level="success",
            )

            # Step 3: Create task-specific prompt
            task_prompt = self._build_run_task_prompt(project_name, task_description)

            # Step 5: Execute task
            if self.config.ui_mode:
                self._emit(EventType.PHASE_START, "Executing task", phase=PhaseType.BUILD)
            else:
                self.console.print(f"[dim]🔧 Executing task: {task_description}[/dim]")

            # Run the task execution loop
            success = self.react_engine.run_react_loop(
                initial_prompt=task_prompt,
                max_iterations=self.max_iterations,
                completion_mode="run_task",
            )

            # Step 6: Update last comment in container and handle completion
            if success:
                self.orchestrator.update_last_comment(f"Task completed: {task_description}")
                if self.config.ui_mode:
                    self._emit(
                        EventType.PHASE_COMPLETE,
                        "Task completed",
                        phase=PhaseType.BUILD,
                        level="success",
                    )
                    self._emit(
                        EventType.SUCCESS, f"Task completed: {task_description}", level="success"
                    )
                else:
                    self.console.print(f"[bold green]✅ Task completed successfully![/bold green]")
            else:
                self.orchestrator.update_last_comment(f"Task in progress: {task_description}")
                if self.config.ui_mode:
                    self._emit(
                        EventType.PHASE_ERROR,
                        "Task incomplete",
                        phase=PhaseType.BUILD,
                        level="error",
                    )
                    self._emit(
                        EventType.FAILURE, f"Task incomplete: {task_description}", level="error"
                    )
                else:
                    self.console.print(f"[bold yellow]⚠️ Task may be incomplete.[/bold yellow]")

            # Step 7: Provide execution summary
            if self.config.ui_mode:
                self.ui_manager.display_final_summary()
            else:
                self._provide_task_summary(success, task_description)

            cmd_logger.info(f"Task execution completed: success={success}")
            return success

        except Exception as e:
            cmd_logger.error(f"Task execution failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Task execution failed: {e}[/bold red]")

            try:
                self.orchestrator.update_last_comment(
                    f"Task failed: {task_description} - Error: {str(e)[:100]}"
                )
            except Exception as inner:
                logger.warning(f"Failed to update last comment after task failure: {inner}")

            return False

        finally:
            if self.ui_manager:
                try:
                    self.ui_manager.abort_running_phases("Phase aborted")
                    self.ui_manager.display_final_summary()
                except Exception as e:
                    logger.warning(f"UI teardown failed during run_task cleanup: {e}")
                    try:
                        self.ui_manager.stop()
                    except Exception as inner:
                        logger.warning(f"UIManager.stop() also failed: {inner}")

            session_logger = get_session_logger()
            if session_logger:
                try:
                    session_logger.cleanup_command_logger(cmd_logger_id)
                except Exception as e:
                    logger.warning(f"cleanup_command_logger failed: {e}")

    def _build_run_task_prompt(self, project_name: str, task_description: str) -> str:
        """Build the prompt for a one-off CLI task on an existing project."""
        return f"""
I need to work on the project '{project_name}' and complete this sag run --task request:

TASK: {task_description}

This run is not the project setup workflow. The TASK above is the active objective for
this command. Existing setup TODO items may be useful context, but do not start,
complete, or continue existing setup TODO tasks unless the TASK explicitly requires it.

I should:
1. Use manage_context only if I need project state or prior findings
2. Inspect the project only as needed for this TASK
3. Execute the requested command or verification directly
4. Verify the requested TASK with tool output
5. After the TASK is satisfied, write a thought starting with:
   TASK COMPLETE: <brief evidence-backed summary>

Do not generate a final setup report unless the TASK explicitly asks for one.
"""

    def _setup_docker_environment(self, project_name: str) -> bool:
        """Setup the Docker environment for the project."""

        if self.config.ui_mode:
            self._emit(
                EventType.STEP_START,
                "Docker Environment",
                phase=PhaseType.SETUP,
                details="Checking Docker availability...",
            )
            self._emit(EventType.STATUS_UPDATE, "Creating container...", phase=PhaseType.SETUP)

            try:
                success = self.orchestrator.create_and_start_container()

                if success:
                    self._emit(
                        EventType.STATUS_UPDATE,
                        "Configuring environment...",
                        phase=PhaseType.SETUP,
                    )
                    self._emit(
                        EventType.STEP_COMPLETE,
                        "Docker Environment",
                        phase=PhaseType.SETUP,
                        details="Container ready",
                        level="success",
                    )
                    logger.info("Docker environment setup completed")
                    return True
                else:
                    self._emit(
                        EventType.STEP_ERROR,
                        "Docker Environment",
                        phase=PhaseType.SETUP,
                        details="Failed to create container",
                        level="error",
                    )
                    logger.error("Docker environment setup failed")
                    return False

            except Exception as e:
                self._emit(
                    EventType.ERROR,
                    f"Docker setup error: {e}",
                    phase=PhaseType.SETUP,
                    level="error",
                )
                logger.error(f"Docker setup error: {e}")
                return False
        else:
            # Normal Mode: use Rich Progress
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
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
                # Always show critical errors
                self.console.print(
                    f"[bold red]❌ No container found for project: {project_name}[/bold red]"
                )
                return False

            if not self.orchestrator.is_container_running():
                # Show container starting message in non-UI mode only
                if not self.config.ui_mode:
                    self.console.print("[yellow]⚠️ Container is not running. Starting...[/yellow]")
                return self.orchestrator.start_container()

            return True

        except Exception as e:
            logger.error(f"Failed to ensure container running: {e}")
            return False

    def _run_unified_setup(
        self,
        project_url: str,
        project_name: str,
        goal: str,
        interactive: bool = False,
        project_ref: Optional[str] = None,
    ) -> bool:
        """Run the unified project setup process."""

        # Create comprehensive setup prompt with intelligent planning approach
        ref_instruction = ""
        if project_ref:
            ref_instruction = f"""

Repository version handle: {project_ref}
When cloning, pass ref="{project_ref}" to project_setup. Do not set up the default branch if this ref cannot be checked out.
"""

        setup_prompt = f"""
I need to setup the project '{project_name}' from the repository: {project_url}
{ref_instruction}

My goal: {goal}

🧠 INTELLIGENT SETUP WORKFLOW - I should complete this setup using smart analysis:

1. INITIAL CONTEXT CHECK:
   - Check my current context using manage_context tool
   - Understand the current task plan and proceed with task execution

2. REPOSITORY CLONING (if not done):
   - Clone the repository from {project_url} using project_setup tool
   - Verify the project was cloned successfully

3. 🔍 CRITICAL: INTELLIGENT PROJECT ANALYSIS:
   - Use project_analyzer tool to comprehensively analyze the cloned project
   - This will automatically:
     • Read README.md and documentation files
     • Analyze build configurations (Maven pom.xml, Gradle build.gradle/build.gradle.kts, package.json, etc.)
     • Detect Java version requirements, dependencies, and test frameworks for Maven and Gradle projects
     • Identify project type and build system (Maven, Gradle, npm, etc.)
     • Generate optimized execution plan based on project specifics
     • Update the trunk context with intelligent task list

4. EXECUTE INTELLIGENT PLAN:
   - After project analysis, the trunk context will be updated with specific tasks
   - Execute each task in the generated plan systematically
   - Use appropriate tools for each detected project type:
     • Maven projects: maven tool for compile/test
     • Node.js projects: bash tool for npm commands
     • Python projects: bash/system tools for pip/poetry
   - Follow the project's own documented setup instructions

5. COMPLETION:
   - Generate comprehensive report using report tool
   - Include summary of analysis findings and setup results

🎯 KEY ADVANTAGES OF THIS APPROACH:
- Reads project documentation BEFORE making assumptions
- Adapts to specific project requirements automatically
- Uses project's own recommended build/test commands
- Generates optimal task sequence for each unique project

The repository URL is already provided: {project_url}
START by checking context, then clone if needed, then IMMEDIATELY analyze the project!
"""

        if self.config.ui_mode:
            self._emit(EventType.PHASE_START, "Running project setup", phase=PhaseType.BUILD)

            success = self.react_engine.run_react_loop(
                initial_prompt=setup_prompt, max_iterations=self.max_iterations
            )

            verified_success = self._get_verified_final_status(success)

            if verified_success:
                self._emit(
                    EventType.PHASE_COMPLETE,
                    "Build and test completed",
                    phase=PhaseType.BUILD,
                    level="success",
                )
            else:
                self._emit(
                    EventType.PHASE_ERROR,
                    "Build or test failed",
                    phase=PhaseType.BUILD,
                    level="error",
                )

            return verified_success
        else:
            # Normal Mode: use Rich Progress
            self.console.print("[dim]🚀 Starting intelligent project setup process...[/dim]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                task = progress.add_task("Running setup process...", total=None)

                success = self.react_engine.run_react_loop(
                    initial_prompt=setup_prompt, max_iterations=self.max_iterations
                )

                # Check if a report was generated and get the verified status
                verified_success = self._get_verified_final_status(success)

                if verified_success:
                    progress.update(task, description="✅ Setup process completed")
                else:
                    progress.update(task, description="❌ Setup process incomplete")

            return verified_success

    def _get_verified_final_status(self, react_engine_success: bool) -> bool:
        """Get the verified final status using separate build and test validation."""

        project_name = self._get_project_name_for_validation()

        if not project_name:
            logger.warning("🔍 No project name available for verification")
            return False

        # self.physical_validator is set during _initialize_tools(); guard
        # against unusual call orderings (e.g. direct unit-test invocations).
        if not getattr(self, "physical_validator", None):
            logger.warning("PhysicalValidator not initialized; cannot verify status")
            return False

        # Get BUILD status (primary concern)
        build_status = self.physical_validator.validate_build_status(project_name)

        # Get TEST status separately so known test suites cannot disappear behind build artifacts.
        test_status = self.physical_validator.validate_test_status(project_name)
        analysis_status = {}
        try:
            analysis_status = self.physical_validator.validate_project_analysis_status(
                project_name
            )
        except Exception as exc:
            logger.warning(f"Could not validate project analysis status: {exc}")

        tests_expected = False
        static_test_count = analysis_status.get("static_test_count")
        if isinstance(static_test_count, int) and static_test_count > 0:
            tests_expected = True
        elif test_status.get("total_tests", 0) > 0:
            tests_expected = True

        if not react_engine_success:
            logger.warning(
                "ReAct loop did not report success; final status will depend on physical validation"
            )

        # Log comprehensive status
        if build_status["success"]:
            logger.info(f"✅ Build validation: SUCCESS - {build_status['reason']}")

            # Report test status and fail when a known test suite was not successfully verified.
            if test_status["has_test_reports"]:
                failed_or_error_tests = test_status.get("failed_tests", 0) + test_status.get(
                    "error_tests", 0
                )
                if test_status["pass_rate"] == 100.0:
                    logger.info(
                        f"✅ Test validation: ALL PASSED - {test_status['total_tests']} tests (100% pass rate)"
                    )
                elif failed_or_error_tests == 0:
                    logger.info(
                        "⚠️ Test validation: PASSED WITH SKIPS - "
                        f"{test_status['passed_tests']}/{test_status['total_tests']} tests passed, "
                        f"{test_status.get('skipped_tests', 0)} skipped "
                        f"({test_status['pass_rate']:.1f}% pass rate)"
                    )
                elif test_status["pass_rate"] > 0:
                    logger.info(
                        f"⚠️ Test validation: PARTIAL - {test_status['passed_tests']}/{test_status['total_tests']} tests passed ({test_status['pass_rate']:.1f}% pass rate)"
                    )
                    logger.error(
                        "❌ Test validation failed: "
                        f"{test_status.get('failed_tests', 0)} failed, "
                        f"{test_status.get('error_tests', 0)} errors"
                    )
                    return False
                else:
                    logger.warning(
                        f"❌ Test validation: ALL FAILED - 0/{test_status['total_tests']} tests passed (0% pass rate)"
                    )
                    return False

                # Log test exclusions if detected
                if test_status["test_exclusions"]:
                    logger.warning(
                        f"⚠️ Detected test exclusions: {', '.join(test_status['test_exclusions'])}"
                    )

                # Log module coverage if some modules weren't tested
                if test_status.get("modules_without_tests"):
                    module_count = len(test_status["modules_without_tests"])
                    logger.info(
                        f"📊 {module_count} modules not tested: {', '.join(test_status['modules_without_tests'][:3])}"
                    )
            else:
                if tests_expected:
                    logger.error(
                        "❌ Test validation: No test reports found despite detected tests"
                    )
                    return False
                logger.info("⚠️ Test validation: No test reports found")

            return True
        else:
            logger.error(f"❌ Build validation: FAILED - {build_status['reason']}")

            # Even if tests would pass, build failure means overall failure
            if test_status["has_test_reports"]:
                logger.info(
                    f"📊 Test status (informational): {test_status['passed_tests']}/{test_status['total_tests']} tests, {test_status['pass_rate']:.1f}% pass rate"
                )

            return False

    def _get_project_name_for_validation(self) -> Optional[str]:
        """Resolve the real workspace project directory for final validation."""

        project_name = self._read_project_name_from_metadata()
        if project_name:
            return project_name

        project_name = getattr(self.orchestrator, "project_name", None) or getattr(
            self, "project_name", None
        )
        if not project_name and hasattr(self.context_manager, "project_name"):
            project_name = self.context_manager.project_name

        return project_name

    def _read_project_name_from_metadata(self) -> Optional[str]:
        """Read actual repo directory from project metadata when --name used a custom label."""

        try:
            result = self.orchestrator.execute_command(
                "cat /workspace/.setup_agent/project_meta.json 2>/dev/null"
            )
        except Exception as exc:
            logger.debug(f"Could not read project metadata for validation: {exc}")
            return None

        if result.get("exit_code") != 0:
            return None

        try:
            metadata = json.loads((result.get("output") or "").strip())
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse project metadata for validation: {exc}")
            return None

        if not isinstance(metadata, dict):
            return None

        project_name = metadata.get("project_name")
        if isinstance(project_name, str):
            project_name = project_name.strip()
            if project_name:
                logger.info(f"Using project metadata for validation: project_name={project_name}")
                return project_name

        return None

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

        if context_info.get("context_type") == "trunk":
            summary_text += f"• Progress: {context_info.get('progress', 'Unknown')}"

        self.console.print(
            Panel(summary_text, title="[bold]Setup Summary[/bold]", border_style=border_style)
        )

        # Show TODO list status if in trunk context
        if context_info.get("context_type") == "trunk":
            # Load trunk context to show TODO list
            try:
                trunk_context = self.context_manager.load_trunk_context()
                if trunk_context and trunk_context.todo_list:
                    self.console.print("\n[bold]Final TODO List Status:[/bold]")

                    for task in trunk_context.todo_list:
                        status_icon = {
                            "pending": "⏳",
                            "in_progress": "🔄",
                            "completed": "✅",
                            "failed": "❌",
                        }.get(str(task.status).split(".")[-1].lower(), "❓")

                        self.console.print(f"  {status_icon} {task.description}")
                        if task.notes:
                            self.console.print(f"    [dim]Notes: {task.notes}[/dim]")
            except Exception as e:
                logger.warning(f"Failed to load trunk context for TODO display: {e}")
                self.console.print(f"\n[dim]Could not load TODO list status[/dim]")

        # Log detailed summary
        logger.info(f"Setup summary: {exec_summary}")

        # Provide next steps
        if success:
            self.console.print(
                f"\n[bold green]🎉 Project setup completed successfully![/bold green]"
            )
            self.console.print(f"[dim]You can now connect to the container using:[/dim]")
            self.console.print(
                f"  setup-agent connect {context_info.get('project_name', 'project')}"
            )
        else:
            self.console.print(f"\n[bold yellow]⚠️ Setup process incomplete.[/bold yellow]")
            self.console.print(f"[dim]You can continue the setup using:[/dim]")
            self.console.print(
                f"  setup-agent continue {context_info.get('project_name', 'project')}"
            )

    def _provide_task_summary(self, success: bool, task_description: str):
        """Provide a summary of task execution in the terminal final output, this will not shown in the log"""

        # Get execution summary
        summary = self.react_engine.get_execution_summary()

        if success:
            self.console.print(
                Panel.fit(
                    f"[bold green]✅ Task Completed Successfully[/bold green]\n"
                    f"[dim]Task: {task_description}[/dim]\n"
                    f"[dim]Execution Summary:[/dim]\n"
                    f"[dim]• Total steps: {summary['total_steps']}[/dim]\n"
                    f"[dim]• Iterations: {summary['iterations']}[/dim]\n"
                    f"[dim]• Thinking model calls: {summary.get('thinking_model_calls', 0)}[/dim]\n"
                    f"[dim]• Action model calls: {summary.get('action_model_calls', 0)}[/dim]\n"
                    f"[dim]• Successful actions: {summary['successful_actions']}/{summary['actions']}[/dim]",
                    border_style="green",
                )
            )
        else:
            self.console.print(
                Panel.fit(
                    f"[bold yellow]⚠️ Task May Be Incomplete[/bold yellow]\n"
                    f"[dim]Task: {task_description}[/dim]\n"
                    f"[dim]Execution Summary:[/dim]\n"
                    f"[dim]• Total steps: {summary['total_steps']}[/dim]\n"
                    f"[dim]• Iterations: {summary['iterations']}[/dim]\n"
                    f"[dim]• Thinking model calls: {summary.get('thinking_model_calls', 0)}[/dim]\n"
                    f"[dim]• Action model calls: {summary.get('action_model_calls', 0)}[/dim]\n"
                    f"[dim]• Successful actions: {summary['successful_actions']}/{summary['actions']}[/dim]",
                    border_style="yellow",
                )
            )

        logger.info(
            f"Task execution completed for {self.orchestrator.project_name}: {task_description}"
        )

    def get_status(self) -> dict:
        """Get current agent status."""
        return {
            "context_info": self.context_manager.get_current_context_info(),
            "execution_summary": self.react_engine.get_execution_summary(),
            "container_status": (
                self.orchestrator.get_container_info() if self.orchestrator else None
            ),
        }
