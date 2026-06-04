"""Prompt construction for the ReAct engine."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from sag.config.prompt_loader import PromptConfig
from sag.tools.base import BaseTool

from .react_types import ReactModelMode, ReActStep, StepType


class ReActPromptBuilder:
    """Build ReAct prompts and own prompt-related context caching."""

    def __init__(
        self,
        *,
        prompts: PromptConfig,
        context_manager: Any,
        tools: dict[str, BaseTool],
    ):
        self.prompts = prompts
        self.context_manager = context_manager
        self.tools = tools
        self._cached_trunk_context = None
        self._trunk_context_cache_timestamp = None

    def build_initial_system_prompt(
        self,
        *,
        repository_url: str | None,
        tool_calling_enabled: bool,
        workflow_mode: str = "setup",
    ) -> str:
        """Build the initial system prompt with context and tool information."""

        # Get current context info
        context_info = self.context_manager.get_current_context_info()
        parts = []
        is_run_task = workflow_mode == "run_task"

        # Prompt: src/sag/config/prompts/react_engine.yaml:2 initial_system.identity
        parts.append(self.prompts.get("initial_system.identity"))

        # Add repository URL at the very beginning if available
        if repository_url and not is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:9 initial_system.repository_url_notice
            parts.append(
                self.prompts.format(
                    "initial_system.repository_url_notice", repository_url=repository_url
                )
            )

        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:33 initial_system.run_task_context_management
            parts.append(self.prompts.get("initial_system.run_task_context_management"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:14 initial_system.context_management
            parts.append(self.prompts.get("initial_system.context_management"))

        # Add tool descriptions with usage examples
        tool_lines = []
        for tool in self.tools.values():
            tool_lines.append(f"- {tool.name}: {tool.description}")
            if hasattr(tool, "get_usage_example"):
                tool_lines.append(f"  Usage: {tool.get_usage_example()}")
        if tool_lines:
            parts.append("\n".join(tool_lines))

        # Add explicit tool name clarification
        # Prompt: src/sag/config/prompts/react_engine.yaml:42 initial_system.tool_clarification
        parts.append(self.prompts.get("initial_system.tool_clarification"))
        if not is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:70 initial_system.intelligent_setup_workflow
            parts.append(self.prompts.get("initial_system.intelligent_setup_workflow"))
            # Prompt: src/sag/config/prompts/react_engine.yaml:98 initial_system.maven_pom_recovery
            parts.append(self.prompts.get("initial_system.maven_pom_recovery"))
            # Prompt: src/sag/config/prompts/react_engine.yaml:133 initial_system.maven_multimodule_testing
            parts.append(self.prompts.get("initial_system.maven_multimodule_testing"))

        context_part = f"""

CURRENT CONTEXT:
Context Type: {context_info.get('context_type', 'unknown')}
Context ID: {context_info.get('context_id', 'unknown')}
"""

        if context_info.get("context_type") == "trunk" and not is_run_task:
            context_part += f"""
Goal: {context_info.get('goal', 'Not specified')}
Progress: {context_info.get('progress', 'Not available')}
Next Task: {context_info.get('next_task', 'No pending tasks')}
"""
        elif context_info.get("context_type") == "branch" and not is_run_task:
            context_part += f"""
Current Task: {context_info.get('task', 'Not specified')}
Current Focus: {context_info.get('focus', 'Not specified')}
"""
        parts.append(context_part.strip())

        # Add different instructions based on function calling support
        if tool_calling_enabled:
            if is_run_task:
                # Prompt: src/sag/config/prompts/react_engine.yaml:214 initial_system.run_task_function_calling_response_format
                parts.append(
                    self.prompts.get("initial_system.run_task_function_calling_response_format")
                )
            else:
                # Prompt: src/sag/config/prompts/react_engine.yaml:175 initial_system.function_calling_response_format
                parts.append(self.prompts.get("initial_system.function_calling_response_format"))
        else:
            if is_run_task:
                # Prompt: src/sag/config/prompts/react_engine.yaml:259 initial_system.run_task_prompt_based_response_format
                parts.append(
                    self.prompts.get("initial_system.run_task_prompt_based_response_format")
                )
            else:
                # Prompt: src/sag/config/prompts/react_engine.yaml:225 initial_system.prompt_based_response_format
                parts.append(self.prompts.get("initial_system.prompt_based_response_format"))

        # Add repository URL reminder if available
        if repository_url and not is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:275 initial_system.repository_url_reminder
            parts.append(
                self.prompts.format(
                    "initial_system.repository_url_reminder", repository_url=repository_url
                )
            )

        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:283 initial_system.run_task_completion_reminder
            parts.append(self.prompts.get("initial_system.run_task_completion_reminder"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:278 initial_system.continuous_cycle_reminder
            parts.append(self.prompts.get("initial_system.continuous_cycle_reminder"))

        return "\n\n".join(part.rstrip() for part in parts if part).rstrip() + "\n"

    def build_next_prompt(
        self,
        *,
        steps: list[ReActStep],
        repository_url: str | None,
        tool_calling_enabled: bool,
        successful_states: dict[str, Any],
        workflow_mode: str = "setup",
    ) -> str:
        """Build the prompt for the next iteration."""
        is_run_task = workflow_mode == "run_task"

        # Prompt: src/sag/config/prompts/react_engine.yaml:288 next_prompt.conversation_header
        prompt = self.prompts.get("next_prompt.conversation_header").rstrip() + "\n\n"

        # Limit recent steps to avoid context window overflow
        # Keep the most recent steps, but cap the total length
        max_steps = 7  # Start with fewer steps to stay within context window

        # If we have more steps, take the first few and the most recent ones
        if len(steps) > max_steps * 2:
            # Take first 2 steps (usually context and first action) and last max_steps
            recent_steps = steps[:2] + steps[-max_steps:]
            # Prompt: src/sag/config/prompts/react_engine.yaml:290 next_prompt.omitted_steps_notice
            prompt += self.prompts.get("next_prompt.omitted_steps_notice").rstrip() + "\n\n"
        elif len(steps) > max_steps:
            # Just take the most recent steps
            recent_steps = steps[-max_steps:]
        else:
            recent_steps = steps

        for step in recent_steps:
            if step.step_type == StepType.THOUGHT:
                # Truncate very long thoughts to keep context manageable
                content = step.content[:5000] + "..." if len(step.content) > 5000 else step.content
                prompt += f"THOUGHT: {content}\n\n"
            elif step.step_type == StepType.ACTION:
                prompt += f"ACTION: {step.tool_name}\n"
                if step.tool_params:
                    prompt += f"PARAMETERS: {json.dumps(step.tool_params)}\n\n"
            elif step.step_type == StepType.OBSERVATION:
                # Truncate very long observations to keep context manageable
                content = step.content[:5000] + "..." if len(step.content) > 5000 else step.content
                prompt += f"OBSERVATION: {content}\n\n"
            elif step.step_type == StepType.SYSTEM_GUIDANCE:
                prompt += f"SYSTEM GUIDANCE: {step.content}\n\n"

        # Check if we need to provide format guidance
        thoughts_without_actions = 0
        for step in reversed(recent_steps):
            if step.step_type == StepType.THOUGHT:
                thoughts_without_actions += 1
            elif step.step_type == StepType.ACTION:
                break

        if thoughts_without_actions >= 3:
            # Model seems stuck in thinking without acting
            if is_run_task:
                # Prompt: src/sag/config/prompts/react_engine.yaml:302 next_prompt.run_task_stuck_function_calling_guidance
                prompt += self.prompts.get(
                    "next_prompt.run_task_stuck_function_calling_guidance"
                ).rstrip()
                prompt += "\n\n"
            elif tool_calling_enabled:
                # Prompt: src/sag/config/prompts/react_engine.yaml:292 next_prompt.stuck_function_calling_guidance
                prompt += self.prompts.get("next_prompt.stuck_function_calling_guidance").rstrip()
                prompt += "\n\n"
                # Add specific guidance based on repository URL
                if repository_url:
                    # Prompt: src/sag/config/prompts/react_engine.yaml:307 next_prompt.stuck_repository_url_guidance
                    prompt += self.prompts.format(
                        "next_prompt.stuck_repository_url_guidance",
                        repository_url=repository_url,
                    ).rstrip()
                    prompt += "\n"
            else:
                # Prompt: src/sag/config/prompts/react_engine.yaml:331 next_prompt.stuck_prompt_based_guidance
                prompt += self.prompts.get("next_prompt.stuck_prompt_based_guidance").rstrip()
                prompt += "\n\n"

        # Prompt: src/sag/config/prompts/react_engine.yaml:340 next_prompt.continuation
        prompt += self.prompts.get("next_prompt.continuation").rstrip() + "\n\n"

        # Apply memory protection to prevent critical info loss due to context pollution
        prompt = self._inject_memory_protection(
            prompt,
            steps=steps,
            repository_url=repository_url,
            successful_states=successful_states,
            workflow_mode=workflow_mode,
        )

        return prompt

    def build_mode_prompt(
        self,
        base_prompt: str,
        mode: ReactModelMode,
        workflow_mode: str = "setup",
    ) -> str:
        """Build specialized prompt for a model mode."""
        is_run_task = workflow_mode == "run_task"
        if mode == ReactModelMode.THINKING:
            if is_run_task:
                # Prompt: src/sag/config/prompts/react_engine.yaml:386 mode_prompts.run_task_thinking
                return (
                    self.prompts.get("mode_prompts.run_task_thinking").rstrip() + "\n" + base_prompt
                )
            # Prompt: src/sag/config/prompts/react_engine.yaml:343 mode_prompts.thinking
            return self.prompts.get("mode_prompts.thinking").rstrip() + "\n" + base_prompt

        if mode == ReactModelMode.ACTION:
            if is_run_task:
                # Prompt: src/sag/config/prompts/react_engine.yaml:445 mode_prompts.run_task_action
                return (
                    self.prompts.get("mode_prompts.run_task_action").rstrip() + "\n" + base_prompt
                )
            # Prompt: src/sag/config/prompts/react_engine.yaml:401 mode_prompts.action
            return self.prompts.get("mode_prompts.action").rstrip() + "\n" + base_prompt

        raise ValueError(f"Unsupported React model mode: {mode}")

    def invalidate_trunk_cache(self) -> None:
        """Invalidate trunk context cache when we know it has changed."""
        self._cached_trunk_context = None
        self._trunk_context_cache_timestamp = None
        logger.debug("Trunk context cache invalidated")

    def _preserve_critical_info(
        self,
        *,
        steps: list[ReActStep],
        repository_url: str | None,
        successful_states: dict[str, Any],
        workflow_mode: str = "setup",
    ) -> str:
        """
        Preserve critical information that should not be forgotten due to context pollution.
        This acts as a 'short-term memory' protection mechanism.
        """
        critical_info = []

        # Always preserve repository URL if we have it
        if repository_url:
            critical_info.append(f"🔗 Repository URL: {repository_url}")

        # ENHANCED: Preserve dynamic project state information
        if successful_states.get("working_directory"):
            workdir = successful_states["working_directory"]
            critical_info.append(f"📁 Working Directory: {workdir}")
            # Add explicit reminder for Maven/Gradle projects
            if workdir != "/workspace" and successful_states.get("project_type") in [
                "maven",
                "gradle",
            ]:
                critical_info.append(
                    f"⚠️ IMPORTANT: All Maven/Gradle commands must run in: {workdir}"
                )

        # Preserve project structure awareness
        if successful_states.get("project_name"):
            critical_info.append(f"📦 Project Name: {successful_states['project_name']}")

        # Preserve build system information with more details
        build_system = successful_states.get("build_system")
        if build_system:
            status_indicator = " (working)" if successful_states.get("maven_success") else ""
            critical_info.append(f"🔧 Build System: {build_system}{status_indicator}")

        # Preserve critical project structure findings
        if successful_states.get("has_pom_xml"):
            critical_info.append("📄 Found: pom.xml (Maven project confirmed)")

        if successful_states.get("repository_cloned"):
            critical_info.append("✅ Repository: Successfully cloned")

        # Preserve successful tool states
        successful_tools = []
        for tool, success in [
            ("project_setup", "project_setup" in successful_states.get("tools_used", [])),
            ("maven", successful_states.get("maven_success", False)),
            ("git", "git" in successful_states.get("tools_used", [])),
        ]:
            if success:
                successful_tools.append(tool)

        if successful_tools:
            critical_info.append(f"✅ Working Tools: {', '.join(successful_tools)}")

        if workflow_mode != "run_task":
            # CRITICAL: Preserve task plan to prevent context pollution from causing hallucinated task IDs
            try:
                # Use cached trunk context to avoid frequent file I/O
                trunk_context = self._get_cached_trunk_context(current_step=len(steps))
                if trunk_context and trunk_context.todo_list:
                    current_task = None
                    next_task = None
                    completed_count = 0

                    for task in trunk_context.todo_list:
                        if task.status.value == "completed":
                            completed_count += 1
                        elif task.status.value == "in_progress":
                            current_task = task
                        elif task.status.value == "pending" and not next_task:
                            next_task = task

                    plan_summary = [
                        f"📋 TASK PLAN ({completed_count}/{len(trunk_context.todo_list)} completed):"
                    ]

                    if current_task:
                        plan_summary.append(
                            f"  🔄 CURRENT: {current_task.id} - {current_task.description}"
                        )

                    if next_task:
                        plan_summary.append(f"  ⏭️ NEXT: {next_task.id} - {next_task.description}")

                    # Show available task IDs to prevent hallucinations
                    all_task_ids = [task.id for task in trunk_context.todo_list]
                    plan_summary.append(f"  📝 VALID IDs: {', '.join(all_task_ids)}")

                    # CRITICAL: Add previous task's key results as context for next task
                    previous_task_results = []
                    for task in trunk_context.todo_list:
                        if task.status.value == "completed" and task.key_results:
                            previous_task_results.append(f"    - {task.id}: {task.key_results}")

                    if previous_task_results:
                        plan_summary.append("  🔑 PREVIOUS_TASK_RESULTS:")
                        plan_summary.extend(previous_task_results)

                    # CRITICAL: Add clear workflow guidance to prevent mental model confusion
                    plan_summary.append(
                        '  💡 WORKFLOW: manage_context(action="start_task", task_id="...") → [work on task] → manage_context(action="complete_with_results", summary="...", key_results="...")'
                    )
                    plan_summary.append(
                        '  ⚠️ USE manage_context(action="complete_with_results", summary="...", key_results="...") - NOT a separate tool!'
                    )
                    plan_summary.append(
                        "  ⚠️ NO 'branch_start' or 'branch_end' - context switching is automatic!"
                    )

                    critical_info.extend(plan_summary)

            except Exception:
                # Don't let context loading errors break the memory protection
                critical_info.append(
                    "⚠️ Task plan unavailable - use manage_context(action='get_info')"
                )

        if critical_info:
            return (
                "\n🧠 CRITICAL MEMORY (preserved to prevent context pollution losses):\n"
                + "\n".join(critical_info)
                + "\n"
            )
        return ""

    def _inject_memory_protection(
        self,
        prompt: str,
        *,
        steps: list[ReActStep],
        repository_url: str | None,
        successful_states: dict[str, Any],
        workflow_mode: str = "setup",
    ) -> str:
        """
        Inject critical information preservation into prompts to combat context pollution.
        """
        critical_memory = self._preserve_critical_info(
            steps=steps,
            repository_url=repository_url,
            successful_states=successful_states,
            workflow_mode=workflow_mode,
        )
        if critical_memory:
            # Insert critical memory after the initial system prompt but before the current situation
            insertion_point = prompt.find("Current situation:")
            if insertion_point != -1:
                return prompt[:insertion_point] + critical_memory + "\n" + prompt[insertion_point:]
            else:
                # Fallback: add at the beginning
                return critical_memory + "\n" + prompt
        return prompt

    def _get_cached_trunk_context(self, *, current_step: int):
        """
        Get trunk context with intelligent caching to avoid frequent file I/O.
        Only reloads when necessary (every 5 steps or after context changes).
        """

        # Cache for 5 steps or if cache is empty
        if (
            self._cached_trunk_context is None
            or self._trunk_context_cache_timestamp is None
            or current_step - self._trunk_context_cache_timestamp >= 5
        ):

            try:
                self._cached_trunk_context = self.context_manager.load_trunk_context()
                self._trunk_context_cache_timestamp = current_step
                logger.debug(f"Refreshed trunk context cache at step {current_step}")
            except Exception as e:
                logger.warning(f"Failed to refresh trunk context cache: {e}")
                # Keep using old cache if refresh fails

        return self._cached_trunk_context
