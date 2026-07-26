"""Prompt construction for the native executor loop.

`build_initial_system_prompt` is the ONLY prompt render: it becomes the
`{"role": "system"}` message of every request, and the conversation itself is
rendered from `self.steps` by `native_messages.render_messages`. The flat
per-iteration rebuild (`build_next_prompt`), the THINK/ACTION mode wrapper
(`build_mode_prompt`) and the "stuck thinking" nudge died with the text
protocol in Plan 2 Task 8.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sag.config.prompt_loader import PromptConfig
from sag.tools.base import BaseTool

if TYPE_CHECKING:
    from .phase_handoff import HandoffProjection


class ReActPromptBuilder:
    """Render the system prompt and the per-phase intro guidance."""

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

    def build_initial_system_prompt(
        self,
        *,
        repository_url: str | None,
        repository_ref: str | None = None,
        workflow_mode: str = "setup",
    ) -> str:
        """Build the system prompt sent with EVERY native request.

        Names the tools, the phase workflow and the evidence rules; the
        conversation itself (assistant tool calls and their tool results) is
        rendered from `self.steps`, so this text carries no response-format
        protocol."""

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
                    "initial_system.repository_url_notice",
                    repository_url=repository_url,
                    repository_ref_notice=self._repository_ref_notice(repository_ref),
                )
            )

        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:37 initial_system.run_task_context_management
            parts.append(self.prompts.get("initial_system.run_task_context_management"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:15 initial_system.context_management
            parts.append(self.prompts.get("initial_system.context_management"))

        # Add tool descriptions with usage examples
        tool_lines = []
        for tool in self.tools.values():
            tool_lines.append(f"- {tool.name}: {tool.description}")
            if hasattr(tool, "get_usage_example"):
                tool_lines.append(f"  Usage: {tool.get_usage_example()}")
        if tool_lines:
            parts.append("\n".join(tool_lines))

        # Add explicit tool name clarification. The clarification is
        # mode-specific: setup runs teach the phase lifecycle surface (the
        # engine owns phase order; manage_context is not registered there),
        # run-task keeps the legacy manage_context surface.
        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:85 initial_system.run_task_tool_clarification
            parts.append(self.prompts.get("initial_system.run_task_tool_clarification"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:50 initial_system.tool_clarification
            parts.append(self.prompts.get("initial_system.tool_clarification"))
            # Prompt: src/sag/config/prompts/react_engine.yaml:114 initial_system.intelligent_setup_workflow
            parts.append(self.prompts.get("initial_system.intelligent_setup_workflow"))
            # Prompt: src/sag/config/prompts/react_engine.yaml:142 initial_system.maven_pom_recovery
            parts.append(self.prompts.get("initial_system.maven_pom_recovery"))
            # Prompt: src/sag/config/prompts/react_engine.yaml:178 initial_system.maven_multimodule_testing
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

        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:248 initial_system.run_task_response_format
            parts.append(self.prompts.get("initial_system.run_task_response_format"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:220 initial_system.response_format
            parts.append(self.prompts.get("initial_system.response_format"))

        # Add repository URL reminder if available
        if repository_url and not is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:261 initial_system.repository_url_reminder
            parts.append(
                self.prompts.format(
                    "initial_system.repository_url_reminder",
                    repository_url=repository_url,
                    repository_ref_clone_args=self._repository_ref_clone_args(repository_ref),
                    repository_ref_notice=self._repository_ref_notice(repository_ref),
                )
            )

        if is_run_task:
            # Prompt: src/sag/config/prompts/react_engine.yaml:270 initial_system.run_task_completion_reminder
            parts.append(self.prompts.get("initial_system.run_task_completion_reminder"))
        else:
            # Prompt: src/sag/config/prompts/react_engine.yaml:265 initial_system.continuous_cycle_reminder
            parts.append(self.prompts.get("initial_system.continuous_cycle_reminder"))

        return "\n\n".join(part.rstrip() for part in parts if part).rstrip() + "\n"

    def _repository_ref_notice(self, repository_ref: str | None) -> str:
        if not repository_ref:
            return ""
        return (
            f"Repository ref: {repository_ref}\n"
            f'When cloning, call project(action="clone") with ref="{repository_ref}". '
            "Do not continue on the default branch if checkout fails."
        )

    def _repository_ref_clone_args(self, repository_ref: str | None) -> str:
        return f', ref="{repository_ref}"' if repository_ref else ""

    @staticmethod
    def build_phase_intro_guidance(
        *,
        phase_contract: str,
        handoff_projection: "HandoffProjection | None",
    ) -> str:
        """Join the engine-owned contract to a typed, explicitly untrusted digest."""
        contract = str(phase_contract).rstrip()
        if handoff_projection is None:
            return contract
        return f"{contract}\n\n{handoff_projection.to_prompt_text()}"
