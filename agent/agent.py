"""Main Setup Agent that orchestrates project setup."""

import re
from typing import List, Optional
from urllib.parse import urlparse

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from config import Config, create_agent_logger, create_command_logger, get_session_logger
from docker_orch.orch import DockerOrchestrator

from .context_manager import ContextManager
from .react_engine import ReActEngine


class SetupAgent:
    """Main agent that orchestrates project setup."""

    def __init__(self, config: Config, orchestrator: DockerOrchestrator, max_iterations: Optional[int] = None):
        self.config = config
        self.orchestrator = orchestrator
        self.max_iterations = max_iterations if max_iterations is not None else config.max_iterations
        self.console = Console()

        # Context manager will be initialized after Docker setup
        self.context_manager = None
        self.tools = None
        self.react_engine = None

        # Create specialized agent logger
        self.agent_logger = create_agent_logger("setup_agent")
        self.agent_logger.info(
            "Setup Agent initialized (context manager will be initialized after Docker setup)"
        )

    def _initialize_context_and_tools(self):
        """Initialize context manager, tools, and react engine after Docker is ready."""
        if self.context_manager is not None:
            return  # Already initialized

        # Initialize context manager with container-based workspace
        self.context_manager = ContextManager(
            workspace_path=self.config.workspace_path,
            orchestrator=self.orchestrator,  # Pass orchestrator for container operations
        )

        # Initialize tools
        self.tools = self._initialize_tools()

        # Initialize ReAct engine (repository URL will be set later)
        self.react_engine = ReActEngine(context_manager=self.context_manager, tools=self.tools)

        self.agent_logger.info("Context manager, tools, and ReAct engine initialized")

    def _initialize_tools(self) -> List:
        """Initialize all available tools."""
        from tools.context_tool import ContextTool
        from tools.bash import BashTool, BashToolConfig
        from tools.file_io import FileIOTool
        from tools.web_search import WebSearchTool
        from tools.maven_tool import MavenTool
        from tools.gradle_tool import GradleTool
        from tools.project_setup_tool import ProjectSetupTool
        from tools.system_tool import SystemTool
        from tools.report_tool import ReportTool
        from tools.project_analyzer import ProjectAnalyzerTool
        from agent.physical_validator import PhysicalValidator
        
        # Configure bash tool with enhanced features
        bash_config = BashToolConfig(
            enable_background_processes=True,
            block_interactive_commands=True,
            audit_command_execution=False,  # Can be enabled for debugging
            add_sag_cli_marker=True
        )
        
        # Create PhysicalValidator for accurate build/test status validation
        physical_validator = PhysicalValidator(
            docker_orchestrator=self.orchestrator,
            project_path=self.config.workspace_path
        )
        
        tools = [
            BashTool(self.orchestrator, config=bash_config),
            FileIOTool(self.orchestrator),  # 传递DockerOrchestrator
            WebSearchTool(),
            ContextTool(self.context_manager),
            MavenTool(self.orchestrator),
            GradleTool(self.orchestrator),  # 添加Gradle工具
            ProjectSetupTool(self.orchestrator),
            SystemTool(self.orchestrator),
            ProjectAnalyzerTool(self.orchestrator, self.context_manager),  # 🆕 添加项目分析工具
            ReportTool(
                self.orchestrator, 
                execution_history_callback=self._get_execution_history, 
                context_manager=self.context_manager,
                physical_validator=physical_validator  # 🆕 注入物理校验器统一真值来源
            )
        ]

        logger.info(f"Initialized {len(tools)} tools: {[tool.name for tool in tools]}")
        return tools

    def _get_execution_history(self):
        """Get execution history for report verification."""
        if hasattr(self, 'react_engine') and self.react_engine:
            return self.react_engine.steps
        return []

    def setup_project(
        self, project_url: str, project_name: str, goal: str, interactive: bool = False
    ) -> bool:
        """Setup a project from scratch."""

        # Create command-specific logger
        cmd_logger, cmd_logger_id = create_command_logger("project", project_name)
        cmd_logger.info(f"Starting project setup: {project_name}")

        self.console.print(
            Panel.fit(
                f"[bold blue]Setting up project: {project_name}[/bold blue]\n"
                f"[dim]Repository: {project_url}[/dim]\n"
                f"[dim]Goal: {goal}[/dim]",
                border_style="blue",
            )
        )

        try:
            # Step 1: Setup Docker environment
            if not self._setup_docker_environment(project_name):
                return False

            # Step 1.5: Initialize context manager and tools now that Docker is ready
            self._initialize_context_and_tools()

            # Step 1.6: Set repository URL for ReAct engine
            self.react_engine.set_repository_url(project_url)

            # Step 2: Initialize trunk context with intelligent planning approach
            # 🆕 ENHANCED PLANNING: Split complex task into clear, executable steps
            # This ensures each critical step is executed independently and cannot be skipped
            # 🎯 CORE FOCUS: Build and Test success are the PRIMARY objectives of setup
            initial_tasks = [
                "Clone repository and setup basic environment (use project_setup tool)",
                "CRITICAL: Analyze project structure using project_analyzer tool and generate intelligent execution plan",
                "🚨 CORE SETUP: Execute build tasks and ensure compilation success (use maven/gradle tools)",
                "🚨 CORE SETUP: Execute test suite and ensure all tests pass (use maven/gradle tools)", 
                "Generate final completion report with build and test results (use report tool)"
            ]
            
            logger.info("Creating trunk context with enhanced multi-step planning approach...")
            self.agent_logger.info(f"Creating trunk context for project: {project_name}")
            
            try:
                trunk_context = self.context_manager.create_trunk_context(
                    goal=goal, project_url=project_url, project_name=project_name, tasks=initial_tasks
                )
                
                # Verify trunk context was created successfully
                context_info = self.context_manager.get_current_context_info()
                if context_info.get("error"):
                    raise Exception(f"Failed to verify trunk context: {context_info['error']}")
                
                self.agent_logger.info(f"✅ Trunk context created successfully: {trunk_context.context_id}")
                logger.info(f"Trunk context created with {len(initial_tasks)} explicit tasks (project_analyzer will be called in task_2)")
                
            except Exception as e:
                self.agent_logger.error(f"❌ Failed to create trunk context: {e}")
                logger.error(f"Trunk context creation failed: {e}")
                self.console.print(f"[bold red]❌ Failed to create project context: {e}[/bold red]")
                return False

            # Step 3: Run the unified setup process
            success = self._run_unified_setup(project_url, project_name, goal, interactive)

            # Step 5: Cleanup and summary
            self._provide_setup_summary(success)

            cmd_logger.info(f"Project setup completed: success={success}")

            # Cleanup command logger
            session_logger = get_session_logger()
            if session_logger:
                session_logger.cleanup_command_logger(cmd_logger_id)

            return success

        except Exception as e:
            cmd_logger.error(f"Setup failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Setup failed: {e}[/bold red]")

            # Cleanup command logger
            session_logger = get_session_logger()
            if session_logger:
                session_logger.cleanup_command_logger(cmd_logger_id)

            return False

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
                
                self.agent_logger.info(f"✅ Context loaded successfully: {context_info.get('context_id', 'unknown')}")
                logger.info(f"Trunk context ready for project: {project_name}")
                
            except Exception as e:
                self.agent_logger.error(f"❌ Failed to load/create context: {e}")
                logger.error(f"Context loading failed: {e}")
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
            self.console.print(f"[bold red]❌ Continue project failed: {e}[/bold red]")
            return False

    def run_task(self, project_name: str, task_description: str) -> bool:
        """Run a specific task on an existing project."""

        # Create command-specific logger
        cmd_logger, cmd_logger_id = create_command_logger("run", project_name)
        cmd_logger.info(f"Starting task execution: {task_description}")

        self.console.print(
            Panel.fit(
                f"[bold cyan]Running task on: {project_name}[/bold cyan]\n"
                f"[dim]Task: {task_description}[/dim]",
                border_style="cyan",
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
                    goal=f"Complete task: {task_description}",
                    project_url="",  # Will be loaded from existing context
                    project_name=project_name,
                )
                
                # Verify context was loaded/created successfully
                context_info = self.context_manager.get_current_context_info()
                if context_info.get("error"):
                    raise Exception(f"Failed to load/create context: {context_info['error']}")
                
                self.agent_logger.info(f"✅ Context loaded successfully: {context_info.get('context_id', 'unknown')}")
                logger.info(f"Trunk context ready for project: {project_name}")
                
            except Exception as e:
                self.agent_logger.error(f"❌ Failed to load/create context: {e}")
                logger.error(f"Context loading failed: {e}")
                self.console.print(f"[bold red]❌ Failed to load project context: {e}[/bold red]")
                return False

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

            self.console.print(f"[dim]🔧 Executing task: {task_description}[/dim]")

            # Step 5: Run the task execution loop
            success = self.react_engine.run_react_loop(
                initial_prompt=task_prompt, max_iterations=self.max_iterations
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

            cmd_logger.info(f"Task execution completed: success={success}")

            # Cleanup command logger
            session_logger = get_session_logger()
            if session_logger:
                session_logger.cleanup_command_logger(cmd_logger_id)

            return success

        except Exception as e:
            cmd_logger.error(f"Task execution failed: {e}", exc_info=True)
            self.console.print(f"[bold red]❌ Task execution failed: {e}[/bold red]")

            # Update last comment with error
            error_comment = f"Task failed: {task_description} - Error: {str(e)[:100]}"

            # Cleanup command logger
            session_logger = get_session_logger()
            if session_logger:
                session_logger.cleanup_command_logger(cmd_logger_id)
            self.orchestrator.update_last_comment(error_comment)

            return False

    def _setup_docker_environment(self, project_name: str) -> bool:
        """Setup the Docker environment for the project."""

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
                self.console.print(
                    f"[bold red]❌ No container found for project: {project_name}[/bold red]"
                )
                return False

            if not self.orchestrator.is_container_running():
                self.console.print("[yellow]⚠️ Container is not running. Starting...[/yellow]")
                return self.orchestrator.start_container()

            return True

        except Exception as e:
            logger.error(f"Failed to ensure container running: {e}")
            return False

    def _run_unified_setup(self, project_url: str, project_name: str, goal: str, interactive: bool = False) -> bool:
        """Run the unified project setup process."""

        # Create comprehensive setup prompt with intelligent planning approach
        setup_prompt = f"""
I need to setup the project '{project_name}' from the repository: {project_url}

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

        self.console.print("[dim]🚀 Starting intelligent project setup process...[/dim]")

        # Run the unified setup process
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

    def _verify_build_artifacts(self, project_name: str) -> bool:
        """Verify build artifacts exist as ultimate success indicator."""
        logger.info(f"Verifying build artifacts for project: {project_name}")
        
        if not self.orchestrator:
            return False
        
        # Check for different build system artifacts
        artifact_checks = [
            # Maven artifacts
            (f"find /workspace/{project_name} -name '*.jar' -path '*/target/*' 2>/dev/null | head -5", 
             lambda output: bool(output.strip()) and len(output.strip().split('\n')) > 0),
            # Gradle artifacts
            (f"find /workspace/{project_name} -name '*.jar' -path '*/build/*' 2>/dev/null | head -5",
             lambda output: bool(output.strip()) and len(output.strip().split('\n')) > 0),
            # NPM/Node artifacts
            (f"test -d /workspace/{project_name}/node_modules && test -f /workspace/{project_name}/package-lock.json && echo SUCCESS",
             lambda output: 'SUCCESS' in output),
            # Python artifacts
            (f"test -d /workspace/{project_name}/venv || test -d /workspace/{project_name}/.venv && echo SUCCESS",
             lambda output: 'SUCCESS' in output),
            # Generic compiled output
            (f"find /workspace/{project_name} -name '*.class' -o -name '*.o' -o -name '*.so' 2>/dev/null | head -5",
             lambda output: bool(output.strip()))
        ]
        
        for check_cmd, validator in artifact_checks:
            result = self.orchestrator.execute_command(check_cmd)
            if result.get('exit_code') == 0 and validator(result.get('output', '')):
                logger.info(f"✅ Build artifacts found with command: {check_cmd[:50]}...")
                return True
        
        logger.info("❌ No build artifacts found")
        return False
    
    def _get_verified_final_status(self, react_engine_success: bool) -> bool:
        """Get the verified final status with multi-level verification."""
        
        # CRITICAL FIX: Check for explicit FAILED status first
        # Don't let artifact presence override explicit failure
        if hasattr(self.context_manager, 'get_current_context'):
            context = self.context_manager.get_current_context()
            if context:
                # Check for explicit failure markers in context
                context_str = str(context)
                if any(marker in context_str for marker in ['status": "FAILED"', 'BUILD FAILED', 'BUILD FAILURE', 'compilation_errors']):
                    logger.warning("❌ Explicit FAILED status detected in context - not overriding")
                    return False
        
        # Priority 1: Check physical build artifacts (but don't override FAILED)
        project_name = getattr(self.orchestrator, 'project_name', None) or getattr(self, 'project_name', None)
        if not project_name and hasattr(self.context_manager, 'project_name'):
            project_name = self.context_manager.project_name
        
        if project_name:
            build_artifacts_exist = self._verify_build_artifacts(project_name)
            # FIXED: Only mark as success if no failure detected
            if build_artifacts_exist and react_engine_success:
                logger.info("✅ Build artifacts verified and no failures detected - marking as SUCCESS")
                return True
            elif not build_artifacts_exist:
                logger.warning("❌ No build artifacts found - cannot mark as SUCCESS")
                return False
        
        # Priority 2: Check context for task completion
        if hasattr(self.context_manager, 'get_current_context'):
            context = self.context_manager.get_current_context()
            if context and 'todo_list' in context:
                tasks = context['todo_list']
                completed_tasks = [t for t in tasks if t.get('status') == 'completed']
                total_tasks = len(tasks)
                completed_count = len(completed_tasks)
                
                # Check if all core tasks completed
                if completed_count == total_tasks:
                    logger.info(f"✅ All {total_tasks} tasks completed - marking as SUCCESS")
                    return True
                
                # Allow success if only reporting/documentation is incomplete
                if completed_count >= total_tasks - 1:
                    incomplete_tasks = [t for t in tasks if t.get('status') != 'completed']
                    # Check if only non-critical tasks are incomplete
                    non_critical_keywords = ['report', 'document', 'summary', 'cleanup', 'optimize']
                    all_non_critical = all(
                        any(keyword in t.get('description', '').lower() for keyword in non_critical_keywords)
                        for t in incomplete_tasks
                    )
                    
                    if all_non_critical:
                        logger.info(f"✅ {completed_count}/{total_tasks} tasks completed (only reporting incomplete) - marking as SUCCESS")
                        return True
                
                # 80% rule for substantial completion
                if completed_count >= total_tasks * 0.8:
                    logger.info(f"⚠️ {completed_count}/{total_tasks} tasks completed (80% threshold) - checking for critical failures")
                    
                    # Check for BUILD SUCCESS in context
                    if 'BUILD SUCCESS' in str(context):
                        logger.info("✅ BUILD SUCCESS found in context - marking as SUCCESS")
                        return True
        
        # Priority 3: Fall back to original ReAct engine success if no other evidence
        if not react_engine_success:
            return False
        
        # Priority 4: Check if a report was generated and what its verified status was
        if hasattr(self, 'react_engine') and self.react_engine and self.react_engine.steps:
            for step in reversed(self.react_engine.steps):
                if (hasattr(step, 'step_type') and step.step_type == 'action' and 
                    hasattr(step, 'tool_name') and step.tool_name == 'report' and
                    hasattr(step, 'tool_result') and step.tool_result and step.tool_result.success):
                    
                    # Check the metadata for the verified status
                    metadata = step.tool_result.metadata or {}
                    verified_status = metadata.get('verified_status')
                    
                    if verified_status:
                        logger.info(f"🔍 Using verified status from report tool: {verified_status}")
                        
                        # CRITICAL FIX: Accept both 'success' and 'partial' as successful completion
                        # 'partial' often means all core tasks completed but with minor issues/warnings
                        if verified_status == 'success':
                            return True
                        elif verified_status == 'partial':
                            # For 'partial' status, check if there are actual failures or just warnings
                            # Look at the report output to assess the severity
                            output = step.tool_result.output or ""
                            
                            # Check for evidence of major failures
                            major_failures = [
                                "❌ Repository clone failed",
                                "❌ Build failed", 
                                "❌ Compilation failed",
                                "Status: FAILED",
                                "BUILD FAILURE",
                                "compilation error"
                            ]
                            
                            has_major_failures = any(failure in output for failure in major_failures)
                            
                            # Check for evidence of successful completion
                            success_indicators = [
                                "✅ All tasks completed successfully",
                                "tests_run=",
                                "failures=0",
                                "errors=0",
                                "BUILD SUCCESS",
                                "✅ Clone repository",
                                "✅ Compile project",
                                "✅ Run tests"
                            ]
                            
                            has_success_indicators = any(indicator in output for indicator in success_indicators)
                            
                            if not has_major_failures and has_success_indicators:
                                logger.info("🔍 Partial status assessment: No major failures + success indicators = treating as SUCCESS")
                                return True
                            else:
                                logger.warning(f"🔍 Partial status assessment: Major failures detected or insufficient success evidence = treating as FAILURE")
                                logger.debug(f"Major failures found: {has_major_failures}")
                                logger.debug(f"Success indicators found: {has_success_indicators}")
                                return False
                        else:
                            # Status is 'failed' or unknown
                            return False
                    else:
                        # Fallback to checking report output for status indicators
                        output = step.tool_result.output or ""
                        if "Status: FAILED" in output:
                            logger.info("🔍 Report shows FAILED status - treating as failure")
                            return False
                        elif "Status: SUCCESS" in output:
                            logger.info("🔍 Report shows SUCCESS status - treating as success")
                            return True
                        elif "Status: PARTIAL" in output:
                            logger.info("🔍 Report shows PARTIAL status - applying enhanced assessment")
                            # Apply the same enhanced assessment as above
                            major_failures = [
                                "❌ Repository clone failed",
                                "❌ Build failed", 
                                "❌ Compilation failed",
                                "BUILD FAILURE"
                            ]
                            success_indicators = [
                                "✅ All tasks completed successfully",
                                "tests_run=",
                                "failures=0",
                                "BUILD SUCCESS"
                            ]
                            
                            has_major_failures = any(failure in output for failure in major_failures)
                            has_success_indicators = any(indicator in output for indicator in success_indicators)
                            
                            return not has_major_failures and has_success_indicators
        
        # If no report was found, fall back to ReAct engine result
        logger.warning("🔍 No report tool result found, using ReAct engine result")
        return react_engine_success

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
                        }.get(str(task.status).split('.')[-1].lower(), "❓")

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

    def _extract_project_name_from_url(self, project_url: str) -> str:
        """Extract project name from Git URL."""
        try:
            # Parse URL
            parsed = urlparse(project_url)

            # Get the last part of the path
            path_parts = parsed.path.strip("/").split("/")
            if path_parts:
                project_name = path_parts[-1]
                # Remove .git extension if present
                if project_name.endswith(".git"):
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
            "container_status": (
                self.orchestrator.get_container_info() if self.orchestrator else None
            ),
        }
