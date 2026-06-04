"""ReAct Engine for Setup-Agent (SAG)."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from sag.config import create_agent_logger, create_verbose_logger, get_config
from sag.config.prompt_loader import load_react_engine_prompts
from sag.reporting import render_condensed_summary
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import EventType, PhaseType, UIEvent, UIEventEmitter

from .agent_state_evaluator import AgentStateAnalysis, AgentStateEvaluator, AgentStatus
from .context_manager import BranchContext, BranchContextHistory, ContextManager, TrunkContext
from .output_storage import OutputStorageManager
from .physical_validator import PhysicalValidator
from .react_llm import ReactLLMClient
from .react_prompt_builder import ReActPromptBuilder
from .react_response_parser import ReActResponseParser
from .react_types import ReactModelMode, ReActStep, StepType
from .token_tracker import TokenTracker
from .tool_orchestration import (
    ToolCall,
    ToolExecution,
    ToolLifecycleEvent,
    ToolOrchestrator,
)
from .tool_orchestration import format_tool_result as format_orchestrated_tool_result


class ReActEngine(UIEventEmitter):
    """Core ReAct (Reasoning and Acting) engine with dual model support."""

    def __init__(
        self, context_manager: ContextManager, tools: List[BaseTool], repository_url: str = None
    ):
        super().__init__()  # Initialize UIEventEmitter
        self.context_manager = context_manager
        self.tools = {tool.name: tool for tool in tools}
        self.config = get_config()
        self.prompts = load_react_engine_prompts()
        self.repository_url = repository_url
        self.prompt_builder = ReActPromptBuilder(
            prompts=self.prompts,
            context_manager=self.context_manager,
            tools=self.tools,
        )

        # ReAct state
        self.steps: List[ReActStep] = []
        self.current_iteration = 0
        self.max_iterations = self.config.max_iterations

        # Context switching guidance
        self.steps_since_context_switch = 0
        self.context_switch_threshold = self.config.context_switch_threshold

        # Tool execution tracking to avoid repetitive calls
        self.recent_tool_executions = []
        self.max_recent_executions = 10
        self._force_thinking_next = False

        # CRITICAL: Flag to force thinking after successful tool execution
        self._force_thinking_after_success = False

        # State memory for successful operations
        self.successful_states = {
            "working_directory": None,  # Last successful working directory
            "cloned_repos": set(),  # Set of successfully cloned repo URLs
            "project_type": None,  # Detected project type
            "maven_success": False,  # Whether maven operations succeeded
            "excluded_modules": set(),
            "excluded_tests": set(),
            "report_snapshot": None,
        }

        # Agent logger for detailed traces
        self.agent_logger = create_agent_logger("react_engine")

        # Initialize the centralized state evaluator (will be updated with physical validator after initialization)
        self.state_evaluator = AgentStateEvaluator(self.context_manager)

        # Initialize output storage manager
        from pathlib import Path

        contexts_dir = (
            Path(self.context_manager.contexts_dir)
            if hasattr(self.context_manager, "contexts_dir")
            else Path("/workspace/.setup_agent/contexts")
        )
        # Pass orchestrator to OutputStorageManager for container file operations
        orchestrator = (
            self.context_manager.orchestrator
            if hasattr(self.context_manager, "orchestrator")
            else None
        )
        self.output_storage = OutputStorageManager(contexts_dir, orchestrator=orchestrator)

        # Initialize physical validator for fact-based validation
        self.physical_validator = PhysicalValidator(
            docker_orchestrator=orchestrator, project_path="/workspace"
        )

        # Update state evaluator with physical validator
        self.state_evaluator.physical_validator = self.physical_validator

        # Initialize token tracker and LLM client for monitoring model usage
        self.token_tracker = TokenTracker()
        self.llm_client = ReactLLMClient(
            config=self.config,
            tools=self.tools,
            token_tracker=self.token_tracker,
            trace_context=lambda: {
                "iteration": self.current_iteration,
                "timestamp": self._get_timestamp(),
                "agent_logger": self.agent_logger,
            },
        )
        self.llm_client.setup()
        self.response_parser = ReActResponseParser(timestamp_factory=self._get_timestamp)

        logger.info(
            "ReAct Engine initialized with dual model support, physical validation, and token tracking"
        )
        logger.info(f"Thinking model: {self.config.get_litellm_model_name('thinking')}")
        logger.info(f"Action model: {self.config.get_litellm_model_name('action')}")
        if repository_url:
            logger.info(f"Repository URL: {repository_url}")

    def set_repository_url(self, repository_url: str):
        """Set the repository URL for the current project."""
        self.repository_url = repository_url
        logger.info(f"Repository URL set: {repository_url}")

    def run_react_loop(self, initial_prompt: str, max_iterations: Optional[int] = None) -> bool:
        """Run the main ReAct loop."""
        max_iter = max_iterations or self.max_iterations

        self.agent_logger.info(f"Starting ReAct loop with max {max_iter} iterations")

        # Initialize with the initial prompt
        self.steps = []
        self.current_iteration = 0

        # PERFORMANCE: Initialize trunk context cache at start
        self.prompt_builder.invalidate_trunk_cache()  # Ensure fresh start

        # Start with initial thought using thinking model
        current_prompt = (
            self.prompt_builder.build_initial_system_prompt(
                repository_url=self.repository_url,
                tool_calling_enabled=self.llm_client.capabilities_for(
                    ReactModelMode.ACTION
                ).supports_function_calling,
            )
            + "\n\n"
            + initial_prompt
        )

        try:
            while self.current_iteration < max_iter:
                self.current_iteration += 1
                self.agent_logger.info(f"ReAct iteration {self.current_iteration}/{max_iter}")

                # Update token tracker with current iteration
                self.token_tracker.set_iteration(self.current_iteration)

                # Determine if this should be a thinking step or action step
                is_thinking_step = self._should_use_thinking_model()
                mode = ReactModelMode.THINKING if is_thinking_step else ReactModelMode.ACTION

                # Get LLM response
                wrapped_prompt = self.prompt_builder.build_mode_prompt(current_prompt, mode)
                response = self.llm_client.get_response(wrapped_prompt, mode)

                if not response:
                    logger.error("Failed to get LLM response")
                    # Export token usage before early return due to failed LLM response
                    self._export_token_usage_csv()
                    return False

                # Parse the response
                model_used = self.llm_client.capabilities_for(mode).model
                parsed_steps = self.response_parser.parse(
                    response,
                    model_used=model_used,
                    was_thinking_model=is_thinking_step,
                )

                if not parsed_steps:
                    logger.warning("No valid steps parsed from LLM response")
                    logger.warning(f"Raw response was: {repr(response)}")
                    continue

                # Execute the steps
                success = self._execute_steps(parsed_steps)

                # CENTRALIZED STATE EVALUATION: Replace all scattered checks
                state_analysis = self.state_evaluator.evaluate(
                    steps=self.steps,
                    current_iteration=self.current_iteration,
                    recent_tool_executions=self.recent_tool_executions,
                    steps_since_context_switch=self.steps_since_context_switch,
                )

                # Handle guidance based on state analysis
                if state_analysis.needs_guidance:
                    self._add_system_guidance(
                        state_analysis.guidance_message, state_analysis.guidance_priority
                    )

                # Check for task completion
                if state_analysis.is_task_complete:
                    self.agent_logger.info("Task completed successfully")
                    # Export token usage before successful completion
                    self._export_token_usage_csv()
                    return True

                # DEPRECATED: Legacy checks now handled by state_evaluator
                # Check for context switching guidance
                # self._check_context_switching_guidance()

                # Check if model needs explicit action guidance
                # if self._needs_action_guidance():
                #     self._add_action_guidance()

                # Build prompt for next iteration
                current_prompt = self.prompt_builder.build_next_prompt(
                    steps=self.steps,
                    repository_url=self.repository_url,
                    tool_calling_enabled=self.llm_client.capabilities_for(
                        ReactModelMode.ACTION
                    ).supports_function_calling,
                    successful_states=self.successful_states,
                )

                # Step count is now automatically managed by branch history updates
                # No manual step increment needed in new design

                # FIX: Only increment counter when actual work (ACTION steps) was done
                # Don't count pure thinking steps toward context switch threshold
                if parsed_steps and any(step.step_type == StepType.ACTION for step in parsed_steps):
                    self.steps_since_context_switch += 1
                    logger.debug(
                        f"Incremented steps_since_context_switch to {self.steps_since_context_switch} after ACTION step"
                    )

            logger.warning(f"ReAct loop completed without success after {max_iter} iterations")
            # Export token usage before max iterations completion
            self._export_token_usage_csv()
            return False

        except Exception as e:
            logger.error(f"ReAct loop failed: {e}", exc_info=True)
            # Export token usage before exception completion
            self._export_token_usage_csv()
            return False

    def _should_use_thinking_model(self) -> bool:
        """Determine if we should use the thinking model for this step - ENFORCE REACT ARCHITECTURE."""
        # CRITICAL: Check if thinking model was requested after successful tool execution
        if self._force_thinking_after_success:
            self._force_thinking_after_success = False  # Reset the flag
            logger.info("Using thinking model to analyze successful tool execution results")
            return True

        # Check if thinking model was explicitly requested due to repetitive execution
        if self._force_thinking_next:
            self._force_thinking_next = False  # Reset the flag
            logger.info("Using thinking model due to repetitive execution detection")
            return True

        # CRITICAL: ReAct Architecture Enforcement
        # Thinking model = ANALYSIS and PLANNING (after observations)
        # Action model = EXECUTION (after thinking)

        # Always start with thinking model for initial analysis
        if len(self.steps) == 0:
            logger.info("Using thinking model for initial analysis")
            return True

        # ENFORCE PROPER REACT SEQUENCE: OBSERVATION → THINKING → ACTION → OBSERVATION
        last_step = self.steps[-1] if self.steps else None

        if last_step and last_step.step_type == StepType.OBSERVATION:
            # After observation, always analyze with thinking model
            logger.info("Using thinking model to analyze observation results")
            return True

        if last_step and last_step.step_type == StepType.THOUGHT:
            # After thinking, switch to action model for execution
            logger.info("Switching to action model for tool execution after analysis")
            return False

        # Use thinking model when we encounter errors (need analysis)
        recent_steps = self.steps[-3:] if len(self.steps) >= 3 else self.steps
        recent_errors = [
            s
            for s in recent_steps
            if s.step_type == StepType.ACTION and s.tool_result and not s.tool_result.success
        ]

        if len(recent_errors) >= 2:  # Lower threshold for quicker analysis
            logger.info("Using thinking model due to recent errors requiring analysis")
            return True

        # Default to action model for execution
        return False

    def _get_tool_orchestrator(self) -> ToolOrchestrator:
        """Build the orchestration adapter for delegated tool execution."""
        return ToolOrchestrator(
            tools=self.tools,
            context_manager=self.context_manager,
            recent_tool_executions=self.recent_tool_executions,
            successful_states=self.successful_states,
            repository_url=self.repository_url,
            track_tool_execution=self._track_tool_execution,
            update_successful_states=self._update_successful_states,
            add_system_guidance=self._add_system_guidance,
            get_timestamp=self._get_timestamp,
            event_sink=self._handle_tool_lifecycle_event,
            logger=logger,
        )

    def _handle_tool_lifecycle_event(self, event: ToolLifecycleEvent) -> None:
        """Map orchestration lifecycle events into typed UI events."""
        lifecycle_event_map = {
            "tool_start": EventType.TOOL_START,
            "tool_parameters_fixed": EventType.TOOL_PARAMETERS_FIXED,
            "tool_result": EventType.TOOL_RESULT,
            "tool_recovery": EventType.TOOL_RECOVERY,
            "tool_error": EventType.TOOL_ERROR,
        }
        event_type = lifecycle_event_map.get(event.event_type)
        if event_type is None:
            return None

        metadata = dict(event.metadata)
        metadata.setdefault("tool_name", event.call.name)
        metadata.setdefault("tool_params", event.call.validated_params or event.call.raw_params)
        metadata.setdefault("tool_message", event.message)

        self.emit_event(
            UIEvent(
                event_type,
                event.message,
                level=event.level,
                metadata=metadata,
            )
        )

    def _build_tool_call_from_step(self, step: ReActStep) -> ToolCall:
        """Translate a parsed ReAct action step into an orchestration tool call."""
        return ToolCall(
            name=step.tool_name or "",
            raw_params=step.tool_params or {},
            raw_action_text=step.content,
            source_step_index=self.current_iteration,
            model_used=step.model_used,
        )

    def _apply_tool_execution_loop_effects(self, execution: ToolExecution) -> None:
        """Apply loop-level side effects requested by orchestration metadata."""
        metadata = execution.metadata or {}

        if metadata.get("force_thinking_next"):
            self._force_thinking_next = True

        if metadata.get("invalidate_trunk_cache"):
            self.prompt_builder.invalidate_trunk_cache()

        if metadata.get("force_next_task") and hasattr(self.context_manager, "force_next_task"):
            self.context_manager.force_next_task()

    def _execute_steps(self, steps: List[ReActStep]) -> bool:
        """Execute a list of ReAct steps."""
        for step in steps:
            self.steps.append(step)

            if step.step_type == StepType.THOUGHT:
                self.agent_logger.info(f"💭 THOUGHT ({step.model_used}): {step.content}")
                logger.info(f"💭 THOUGHT: {step.content}")

                # Emit UI event for thought
                self.emit(
                    EventType.AGENT_THOUGHT,
                    message=step.content[:200]
                    + ("..." if len(step.content) > 200 else ""),  # Truncate for display
                    step_num=self.current_iteration,
                )

                # Detailed logging in verbose mode
                if self.config.verbose:
                    self._log_react_step_verbose(step)

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add thought to branch history using new context management system
                    try:
                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {"type": "thought", "content": step.content},
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log thought to branch history: {e}")

            elif step.step_type == StepType.ACTION:
                self.agent_logger.info(f"🔧 ACTION: {step.content}")
                logger.info(f"🔧 ACTION: {step.content}")

                # Emit UI event for action with parameters
                self.emit(
                    EventType.AGENT_ACTION,
                    message=f"Using {step.tool_name or 'tool'}",
                    step_num=self.current_iteration,
                    tool_name=step.tool_name or "unknown",
                    tool_params=step.tool_params or {},
                )

                # Update token tracker with actual tool name for the last action token record
                if step.tool_name:
                    self.token_tracker.update_last_tool_name(step.tool_name)

                # Detailed logging in verbose mode
                if self.config.verbose:
                    self._log_react_step_verbose(step)

                call = self._build_tool_call_from_step(step)
                execution = self._get_tool_orchestrator().execute(call)
                result = execution.result
                step.tool_result = result
                self._apply_tool_execution_loop_effects(execution)

                # Log tool result in verbose mode
                if self.config.verbose:
                    self._log_tool_result_verbose(step.tool_name, result)

                # Add observation step with improved formatting
                self._add_observation_step(execution.observation_text)

                # CRITICAL: Force thinking after successful tool execution to prevent cognitive rush
                if result.success:
                    self._force_thinking_after_success = True
                    logger.debug(
                        f"✅ Tool {step.tool_name} succeeded - forcing thinking on next iteration"
                    )

                # Log to branch context if we're in one
                if self.context_manager.current_task_id:
                    # Add action result to branch history using new context management system
                    try:
                        output_to_store = result.output if result.output else ""
                        from datetime import datetime

                        timestamp = datetime.now().isoformat()

                        # Store full output and get reference if output is large
                        if len(output_to_store) > 800:
                            # Store the full output
                            ref_id = self.output_storage.store_output(
                                task_id=self.context_manager.current_task_id,
                                tool_name=step.tool_name,
                                output=output_to_store,
                                timestamp=timestamp,
                                metadata={
                                    "success": result.success,
                                    "iteration": self.current_iteration,
                                },
                            )

                            # Get truncated version with reference
                            output_to_store = self.output_storage.get_truncation_with_reference(
                                output=output_to_store,
                                ref_id=ref_id,
                                max_length=800,
                                tool_name=step.tool_name,
                            )

                        self.context_manager.add_to_branch_history(
                            self.context_manager.current_task_id,
                            {
                                "type": "action",
                                "tool_name": step.tool_name,
                                "success": result.success,
                                "output": output_to_store,
                            },
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log action to branch history: {e}")

        return True

    def _update_successful_states(self, tool_name: str, params: Dict[str, Any], result: ToolResult):
        """Update successful states based on tool execution results."""
        try:
            # CRITICAL FIX: Reset context switch counter when context actually switches
            # Reset on BOTH successful AND failed attempts to prevent accumulation
            if tool_name == "manage_context":
                action = params.get("action", "")
                # Include all context-changing actions
                context_changing_actions = [
                    "start_task",
                    "complete_with_results",
                    "complete_task",
                    "switch_to_trunk",
                    "create_branch",
                    "switch_to_branch",
                ]
                if action in context_changing_actions:
                    # Reset the counter regardless of success/failure
                    self.steps_since_context_switch = 0
                    if result.success:
                        logger.info(
                            f"✅ Reset steps_since_context_switch counter after successful {action}"
                        )
                    else:
                        logger.info(
                            f"⚠️ Reset steps_since_context_switch counter after failed {action} attempt"
                        )

            if tool_name == "bash":
                # CRITICAL FIX: Get actual working directory from tool result metadata
                # This handles cases where bash tool had to fall back to alternative directories
                actual_working_dir = None

                # First try to get the actual working directory from metadata
                if hasattr(result, "metadata") and result.metadata:
                    actual_working_dir = result.metadata.get("working_directory")

                # Fallback to parameter if metadata not available
                if not actual_working_dir:
                    actual_working_dir = params.get("working_directory")

                if actual_working_dir:
                    # Check if working directory changed (fallback occurred)
                    original_dir = params.get("working_directory", "/workspace")
                    if actual_working_dir != original_dir:
                        # PRIORITY CHECK: Is this a workspace-related fallback?
                        if original_dir.startswith(
                            "/workspace"
                        ) and not actual_working_dir.startswith("/workspace"):
                            logger.error(
                                f"🚨 WORKSPACE FALLBACK: Failed to use {original_dir}, fell back to {actual_working_dir}"
                            )
                            logger.error(
                                f"🚨 This is a MAJOR ISSUE - projects should be in /workspace"
                            )
                            logger.error(
                                f"🚨 Clone operations may not work correctly in {actual_working_dir}"
                            )

                            # Mark this as an abnormal state
                            self.successful_states["workspace_fallback"] = True
                            self.successful_states["fallback_reason"] = (
                                f"Could not establish {original_dir}"
                            )
                        else:
                            logger.warning(
                                f"🔧 Working directory change: {original_dir} → {actual_working_dir}"
                            )

                        # CRITICAL: Update all related tools to use the new working directory
                        self._propagate_working_directory_change(actual_working_dir, original_dir)
                    else:
                        # Normal operation - workspace is working correctly
                        if actual_working_dir.startswith("/workspace"):
                            logger.debug(f"✅ Workspace operation normal: {actual_working_dir}")
                            # Clear any previous fallback flags
                            self.successful_states.pop("workspace_fallback", None)
                            self.successful_states.pop("fallback_reason", None)

                    self.successful_states["working_directory"] = actual_working_dir
                    logger.debug(f"Updated successful working directory: {actual_working_dir}")

            elif tool_name == "maven" and params.get("working_directory"):
                # Remember successful Maven working directory
                if "BUILD SUCCESS" in (result.output or ""):
                    # Get working_directory parameter (standardized across all tools)
                    maven_workdir = params.get("working_directory", "/workspace")
                    self.successful_states["working_directory"] = maven_workdir
                    self.successful_states["maven_success"] = True

                    # Check if Maven is working outside workspace (concerning)
                    if not maven_workdir.startswith("/workspace"):
                        logger.warning(f"⚠️ Maven succeeded outside workspace: {maven_workdir}")
                        logger.warning(f"⚠️ This may indicate workspace issues")
                    else:
                        logger.info(f"✅ Maven success in workspace: {maven_workdir}")

                    logger.info(f"Maven success recorded for directory: {maven_workdir}")

            elif tool_name == "project_setup":
                # Remember cloned repositories and project type
                if params.get("repository_url"):
                    self.successful_states["cloned_repos"].add(params["repository_url"])
                    logger.debug(f"Recorded cloned repo: {params['repository_url']}")

                    # Set working directory based on cloned repository
                    if params.get("action") == "clone":
                        repo_name = params["repository_url"].split("/")[-1].replace(".git", "")

                        # PRIORITY: Always try to clone in /workspace first
                        if self.successful_states.get("workspace_fallback"):
                            # We're in fallback mode - this is not ideal for cloning
                            current_workdir = self.successful_states.get(
                                "working_directory", "/root"
                            )
                            clone_dir = f"{current_workdir}/{repo_name}"
                            logger.error(f"🚨 CLONING IN FALLBACK LOCATION: {clone_dir}")
                            logger.error(f"🚨 This is SUBOPTIMAL - prefer /workspace for projects")
                        else:
                            # Normal case - clone in workspace
                            clone_dir = f"/workspace/{repo_name}"
                            logger.info(f"✅ Cloning in proper workspace location: {clone_dir}")

                        self.successful_states["working_directory"] = clone_dir
                        logger.info(f"Updated working directory after clone: {clone_dir}")

                # Check for project type detection in output
                output = result.output or ""
                if "maven" in output.lower() or "pom.xml" in output.lower():
                    self.successful_states["project_type"] = "maven"
                    logger.debug("Detected Maven project type")
                elif "gradle" in output.lower() or "build.gradle" in output.lower():
                    self.successful_states["project_type"] = "gradle"
                    logger.debug("Detected Gradle project type")

            elif tool_name == "report":
                snapshot = {}
                if hasattr(result, "metadata") and result.metadata:
                    snapshot = result.metadata.get("report_snapshot") or {}
                if snapshot:
                    self.successful_states["report_snapshot"] = dict(snapshot)
                    logger.debug("Stored report snapshot for completion guidance")

        except Exception as e:
            logger.warning(f"Failed to update successful states: {e}")

    def _propagate_working_directory_change(self, new_workdir: str, old_workdir: str):
        """
        Propagate working directory changes to ensure consistency across all tools.

        When bash tool falls back to a different directory, we need to update
        Agent's understanding of where the project is located.
        """
        try:
            logger.info(f"📁 Propagating working directory change: {old_workdir} → {new_workdir}")

            # Update successful states
            self.successful_states["working_directory"] = new_workdir

            # PRIORITY CHECK: Warn about workspace fallbacks
            if old_workdir.startswith("/workspace") and not new_workdir.startswith("/workspace"):
                logger.error(
                    f"🚨 WORKSPACE LOST: Propagating fallback from {old_workdir} to {new_workdir}"
                )
                logger.error(f"🚨 Future clone operations will be affected")
                logger.error(f"🚨 Consider fixing the underlying workspace issue")

                # Mark this propagation as problematic
                self.successful_states["workspace_fallback"] = True
                self.successful_states["fallback_reason"] = f"Propagated from failed {old_workdir}"
            elif new_workdir.startswith("/workspace"):
                logger.info(f"✅ Workspace propagation successful: {new_workdir}")
                # Clear fallback flags if we're back in workspace
                self.successful_states.pop("workspace_fallback", None)
                self.successful_states.pop("fallback_reason", None)

            # If we have cloned repositories, we might need to adjust their paths
            if self.successful_states.get("cloned_repos"):
                logger.info(
                    f"📁 Note: Cloned repositories may need path adjustment for new working directory"
                )

                # If we're falling back from workspace, this is a major concern
                if self.successful_states.get("workspace_fallback"):
                    logger.error(
                        f"🚨 CRITICAL: Cloned repositories were in workspace, now using {new_workdir}"
                    )
                    logger.error(
                        f"🚨 Project files may be in /workspace but operations will run in {new_workdir}"
                    )

            # Log for debugging
            logger.debug(f"📁 Agent state updated - new working directory: {new_workdir}")
            logger.debug(
                f"📁 All future operations will use this directory unless explicitly overridden"
            )

        except Exception as e:
            logger.error(f"Failed to propagate working directory change: {e}")

    def _format_tool_result(self, tool_name: str, result: ToolResult) -> str:
        """Delegate tool result formatting to the orchestration layer."""
        return format_orchestrated_tool_result(tool_name, result)

    def _track_tool_execution(self, tool_signature: str, success: bool):
        """Track tool execution to detect repetitive patterns."""
        execution_info = {
            "signature": tool_signature,
            "success": success,
            "timestamp": self._get_timestamp(),
        }

        self.recent_tool_executions.append(execution_info)

        # Keep only recent executions to prevent memory bloat
        if len(self.recent_tool_executions) > self.max_recent_executions:
            self.recent_tool_executions.pop(0)

    def _add_observation_step(self, observation: str):
        """Add an observation step, enriched with physical validation state."""
        # Get physical validation state if relevant
        physical_state = self._get_physical_validation_state(observation)

        # Enrich observation with physical state if available
        if physical_state:
            observation = self._enrich_observation_with_physical_state(observation, physical_state)

        obs_step = ReActStep(
            step_type=StepType.OBSERVATION, content=observation, timestamp=self._get_timestamp()
        )
        self.steps.append(obs_step)

        # FIXED: Only log once to prevent duplicate output in logs
        # Use logger.info for main logging, agent_logger for internal tracking only
        logger.info(f"👁️ OBSERVATION: {observation}")

        # Emit UI event for observation
        self.emit(
            EventType.AGENT_OBSERVATION,
            message=observation[:200]
            + ("..." if len(observation) > 200 else ""),  # Truncate for display
            step_num=self.current_iteration,
        )

        # DEPRECATED: Task completion detection now handled by state_evaluator
        # self._check_task_completion_opportunity(observation)

    def _is_task_complete(self) -> bool:
        """Check if the current task is complete."""
        # Check for report tool completion signal (highest priority)
        recent_steps = self.steps[-3:] if len(self.steps) >= 3 else self.steps

        for step in recent_steps:
            if step.step_type == StepType.ACTION and step.tool_name == "report":
                if step.tool_result and step.tool_result.success:
                    metadata = step.tool_result.metadata or {}
                    if metadata.get("completion_signal") or metadata.get("task_completed"):
                        logger.info("Task completion detected via report tool")
                        return True

        # Check for successful Maven test completion (rule-based completion)
        if self._check_maven_completion():
            logger.info("Task completion detected via Maven success criteria")
            return True

        # Look at recent steps for EXPLICIT completion indicators
        recent_steps = self.steps[-5:] if len(self.steps) >= 5 else self.steps

        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                content_lower = step.content.lower()
                # Only consider overall completion phrases, not individual task completion
                if any(
                    phrase in content_lower
                    for phrase in [
                        "all tasks completed",
                        "project setup complete",
                        "setup finished",
                        "build and test complete",
                        "maven build successful and report generated",
                        "final report completed",
                    ]
                ):
                    logger.info(f"Task completion detected via thought: {step.content[:100]}...")
                    return True

            elif step.step_type == StepType.ACTION and step.tool_name == "manage_context":
                # Only consider trunk context operations that indicate ALL tasks are done
                if step.tool_params and step.tool_params.get("action") == "complete_task":
                    # Check if this was the completion of the LAST task
                    if step.tool_result and step.tool_result.success and step.tool_result.metadata:
                        metadata = step.tool_result.metadata
                        # Only complete if there are no more tasks OR if explicit completion signal
                        if metadata.get("all_tasks_completed") or not metadata.get("next_task"):
                            logger.info("Task completion detected: all TODO tasks completed")
                            return True

                # Don't treat individual branch task completion as overall completion
                # The agent should continue with the next task in the TODO list

        return False

    def _check_maven_completion(self) -> bool:
        """Check if Maven project has been successfully built and tested."""
        # Look for successful Maven test execution in recent steps
        recent_steps = self.steps[-10:] if len(self.steps) >= 10 else self.steps

        maven_compile_success = False
        maven_test_success = False

        for step in recent_steps:
            if (
                step.step_type == StepType.ACTION
                and step.tool_name == "maven"
                and step.tool_result
                and step.tool_result.success
            ):

                output = step.tool_result.output or ""
                command = step.tool_params.get("command", "") if step.tool_params else ""

                # Check for successful compilation
                if "compile" in command.lower() and "BUILD SUCCESS" in output:
                    maven_compile_success = True
                    logger.debug("Maven compile success detected")

                # Check for successful test execution
                if (
                    "test" in command.lower()
                    and "BUILD SUCCESS" in output
                    and "Tests run:" in output
                ):

                    # Parse test results
                    import re

                    test_match = re.search(
                        r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", output
                    )
                    if test_match:
                        total, failures, errors = map(int, test_match.groups())
                        if failures == 0 and errors == 0 and total > 0:
                            maven_test_success = True
                            logger.info(
                                f"Maven test success detected: {total} tests, 0 failures, 0 errors"
                            )

        # Consider task complete if test succeeded (test usually includes compilation)
        # OR if both compile and test succeeded explicitly
        if maven_test_success or (maven_compile_success and maven_test_success):
            logger.info("Maven project completion criteria met: test successful")
            # Add completion guidance for the agent
            self._add_completion_guidance("Maven build and test completed successfully")
            return True

        return False

    def _add_completion_guidance(self, reason: str):
        """Add guidance to help agent recognize task completion."""
        guidance_segments: List[str] = []

        snapshot = self.successful_states.get("report_snapshot")
        if snapshot:
            try:
                guidance_segments.append(render_condensed_summary(snapshot))
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug(f"Failed to render report snapshot for completion guidance: {exc}")

        guidance_segments.append(
            f"SYSTEM GUIDANCE: Task completion detected! {reason}. "
            f"You should now generate a completion report using the report tool "
            f"with a summary of what was accomplished, then the system will stop."
        )

        guidance = "\n".join(guidance_segments)

        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=guidance,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)

        self.agent_logger.info(f"🏁 COMPLETION GUIDANCE: {guidance}")
        logger.info(f"🏁 COMPLETION GUIDANCE: Task completion detected - {reason}")

    def _check_completion_suggestion(self) -> str:
        """Check if we should strongly suggest task completion."""
        # Check if Maven build and test succeeded but no report generated yet
        if self.successful_states["maven_success"] and not self._has_report_been_generated():

            # Look for recent Maven test success
            recent_steps = self.steps[-10:] if len(self.steps) >= 10 else self.steps
            for step in recent_steps:
                if (
                    step.step_type == StepType.ACTION
                    and step.tool_name == "maven"
                    and step.tool_result
                    and step.tool_result.success
                ):

                    output = step.tool_result.output or ""
                    if (
                        "test" in step.tool_params.get("command", "").lower()
                        and "BUILD SUCCESS" in output
                        and "Tests run:" in output
                    ):

                        # Parse test results to confirm no failures
                        import re

                        test_match = re.search(
                            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", output
                        )
                        if test_match:
                            total, failures, errors = map(int, test_match.groups())
                            if failures == 0 and errors == 0 and total > 0:
                                return f"Maven build and test completed successfully ({total} tests passed)"

        # Check if we've been running for many iterations without progress
        if self.current_iteration >= 25 and not self._has_report_been_generated():
            # Check if we have any clear successes
            if self.successful_states["cloned_repos"] or self.successful_states["maven_success"]:
                return "Task has been running for many iterations with some successes"

        return None

    def _has_report_been_generated(self) -> bool:
        """Check if a report has already been generated."""
        for step in self.steps:
            if (
                step.step_type == StepType.ACTION
                and step.tool_name == "report"
                and step.tool_result
                and step.tool_result.success
            ):
                return True
        return False

    def _get_timestamp(self) -> str:
        """Get current timestamp string."""
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log_react_step_verbose(self, step: ReActStep):
        """Log detailed ReAct step information in verbose mode."""

        verbose_logger = create_verbose_logger("react_steps")

        step_entry = {
            "event": "react_step",
            "step_type": step.step_type,
            "iteration": self.current_iteration,
            "step_number": len(self.steps),
            "model_used": step.model_used,
            "content_length": len(step.content),
            "content": step.content,
            "tool_name": step.tool_name,
            "tool_params": step.tool_params,
            "timestamp": step.timestamp,
        }

        verbose_logger.info(f"📝 REACT STEP: {json.dumps(step_entry, indent=2, default=str)}")

    def _log_tool_execution_verbose(self, tool_name: str, params: dict):
        """Log detailed tool execution information in verbose mode."""

        verbose_logger = create_verbose_logger("react_tools")

        execution_entry = {
            "event": "tool_execution_start",
            "tool_name": tool_name,
            "iteration": self.current_iteration,
            "parameters": params,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(
            f"🔧 TOOL EXECUTION: {json.dumps(execution_entry, indent=2, default=str)}"
        )

    def _get_physical_validation_state(self, observation: str) -> Optional[Dict[str, any]]:
        """
        Get physical validation state for build/test related observations.

        Args:
            observation: The observation text

        Returns:
            Physical validation state dict or None
        """
        # Only validate for build/test related observations
        obs_lower = observation.lower()
        if not any(
            keyword in obs_lower
            for keyword in ["build", "compile", "test", "maven", "gradle", "success", "fail"]
        ):
            return None

        try:
            # Get project name from context or use default
            project_name = None
            if hasattr(self.context_manager, "project_name"):
                project_name = self.context_manager.project_name

            # Run physical validation
            validation_result = self.physical_validator.validate_build_artifacts(project_name)

            # Check if we need to replay commands
            if "build success" in obs_lower or "build fail" in obs_lower:
                # Try to get the last build command from command tracker if available
                if hasattr(self, "command_tracker") and self.command_tracker:
                    last_build = self.command_tracker.get_last_build_command()
                    if last_build:
                        replay_result = self.physical_validator.replay_last_build_command(
                            last_build["command"], last_build.get("working_dir")
                        )
                        validation_result["build_replay"] = replay_result

            return validation_result

        except Exception as e:
            logger.warning(f"Physical validation failed: {e}")
            return None

    def _enrich_observation_with_physical_state(
        self, observation: str, physical_state: Dict[str, any]
    ) -> str:
        """
        Enrich observation with physical validation facts.

        Args:
            observation: Original observation text
            physical_state: Physical validation state dict

        Returns:
            Enriched observation text
        """
        # Build physical evidence summary
        evidence_lines = []

        if physical_state.get("class_files", 0) > 0:
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {physical_state['class_files']} .class files exist]"
            )
        else:
            evidence_lines.append(
                "[PHYSICAL EVIDENCE: No .class files found - compilation may have failed]"
            )

        if physical_state.get("jar_files", 0) > 0:
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {physical_state['jar_files']} JAR files exist]"
            )

        if physical_state.get("missing_classes"):
            count = len(physical_state["missing_classes"])
            evidence_lines.append(
                f"[PHYSICAL EVIDENCE: {count} Java files have no corresponding .class files]"
            )

        if "build_replay" in physical_state:
            if physical_state["build_replay"]:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay succeeded]")
            else:
                evidence_lines.append("[PHYSICAL EVIDENCE: Build command replay failed]")

        # Add evidence to observation
        if evidence_lines:
            return observation + "\n" + "\n".join(evidence_lines)

        return observation

    def _log_tool_result_verbose(self, tool_name: str, result):
        """Log detailed tool result information in verbose mode."""

        verbose_logger = create_verbose_logger("react_tools")

        result_entry = {
            "event": "tool_execution_result",
            "tool_name": tool_name,
            "iteration": self.current_iteration,
            "success": result.success,
            "output_length": len(result.output) if result.output else 0,
            "full_output": result.output,  # Show full output instead of preview
            "error": result.error if hasattr(result, "error") else None,
            "timestamp": self._get_timestamp(),
        }

        verbose_logger.info(f"🔧 TOOL RESULT: {json.dumps(result_entry, indent=2, default=str)}")

        # Save full tool output to container file if we have access
        if (
            hasattr(self.context_manager, "orchestrator")
            and self.context_manager.orchestrator
            and result.output
        ):
            output_file = f"/workspace/.setup_agent/tool_traces/iteration_{self.current_iteration}_{tool_name}_output.txt"
            escaped_output = result.output.replace("'", "'\"'\"'")
            self.context_manager.orchestrator.execute_command(
                f"mkdir -p /workspace/.setup_agent/tool_traces && echo '{escaped_output}' > {output_file}"
            )

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get a summary of the execution."""
        thinking_actions = len([s for s in self.steps if s.model_used and "o1" in s.model_used])
        action_actions = len(
            [s for s in self.steps if s.model_used and "o1" not in (s.model_used or "")]
        )

        return {
            "total_steps": len(self.steps),
            "iterations": self.current_iteration,
            "thoughts": len([s for s in self.steps if s.step_type == StepType.THOUGHT]),
            "actions": len([s for s in self.steps if s.step_type == StepType.ACTION]),
            "observations": len([s for s in self.steps if s.step_type == StepType.OBSERVATION]),
            "thinking_model_calls": thinking_actions,
            "action_model_calls": action_actions,
            "successful_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION and s.tool_result and s.tool_result.success
                ]
            ),
            "failed_actions": len(
                [
                    s
                    for s in self.steps
                    if s.step_type == StepType.ACTION
                    and s.tool_result
                    and not s.tool_result.success
                ]
            ),
        }

    @staticmethod
    def _normalize_guidance_priority(priority: Any) -> int:
        """Convert guidance priority labels to the numeric scale used for display."""
        if isinstance(priority, str):
            priority_label = priority.strip().lower()
            return {
                "critical": 9,
                "high": 8,
                "important": 8,
                "normal": 5,
                "medium": 5,
                "low": 3,
            }.get(priority_label, 5)

        return priority

    def _add_system_guidance(self, guidance_message: str, priority: int | str = 5):
        """
        Add system guidance with priority handling.
        Higher priority messages are more prominent.
        """
        priority = self._normalize_guidance_priority(priority)

        # Add visual emphasis based on priority
        if priority >= 9:
            prefix = "🚨 CRITICAL GUIDANCE"
        elif priority >= 7:
            prefix = "⚠️ IMPORTANT GUIDANCE"
        else:
            prefix = "💡 SYSTEM GUIDANCE"

        full_message = f"{prefix} (Priority: {priority}):\n{guidance_message}"

        guidance_step = ReActStep(
            step_type=StepType.SYSTEM_GUIDANCE,
            content=full_message,
            timestamp=self._get_timestamp(),
        )
        self.steps.append(guidance_step)

        self.agent_logger.info(f"{prefix}: {guidance_message[:100]}...")
        logger.info(f"{prefix} added with priority {priority}")

    def test_state_evaluator_integration(self):
        """Test method to verify state evaluator is working correctly."""
        logger.info("Testing state evaluator integration...")

        # Simulate some steps
        test_steps = [
            ReActStep(
                step_type=StepType.THOUGHT, content="Test thought", timestamp=self._get_timestamp()
            ),
            ReActStep(
                step_type=StepType.ACTION, content="Test action", timestamp=self._get_timestamp()
            ),
            ReActStep(
                step_type=StepType.OBSERVATION,
                content="BUILD SUCCESS",
                timestamp=self._get_timestamp(),
            ),
        ]

        # Test evaluation
        analysis = self.state_evaluator.evaluate(
            steps=test_steps,
            current_iteration=1,
            recent_tool_executions=[],
            steps_since_context_switch=5,
        )

        logger.info(f"State analysis: {analysis.status}, needs_guidance: {analysis.needs_guidance}")
        return analysis

    def _export_token_usage_csv(self):
        """Export token usage to CSV file when ReAct loop completes."""
        try:
            # Get session logger for CSV path
            from sag.config.logger import get_session_logger

            session_logger = get_session_logger()

            if session_logger:
                # Save to session directory
                csv_path = session_logger.session_log_dir / "token_usage.csv"
            else:
                # Fallback to logs directory
                from datetime import datetime
                from pathlib import Path

                logs_dir = Path("logs")
                logs_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_path = logs_dir / f"token_usage_{timestamp}.csv"

            # Export the CSV
            success = self.token_tracker.export_to_csv(str(csv_path))

            if success:
                # Log summary stats
                self.token_tracker.log_summary()
                logger.info(f"📊 Token usage exported to: {csv_path}")
            else:
                logger.warning("Failed to export token usage CSV")

        except Exception as e:
            logger.warning(f"Failed to export token usage CSV: {e}")
