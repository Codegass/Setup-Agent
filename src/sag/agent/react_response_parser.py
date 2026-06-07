"""Parser for LLM responses in ReAct format."""

import json
import re
from typing import Callable

from loguru import logger

from .react_types import ReActStep, StepType


class ReActResponseParser:
    """Parse LLM text responses into trusted ReAct steps."""

    def __init__(self, timestamp_factory: Callable[[], str]):
        self.timestamp_factory = timestamp_factory

    def parse(
        self,
        response: str,
        *,
        model_used: str,
        was_thinking_model: bool,
    ) -> list[ReActStep]:
        """Parse LLM response into ReAct steps."""
        steps = []

        logger.debug(f"Parsing LLM response: {repr(response)}")

        trusted_response = self._strip_model_observations(response)

        sections = re.split(r"\n\n(?=THOUGHT:|ACTION:|OBSERVATION:)", trusted_response.strip())

        logger.debug(f"Split response into {len(sections)} sections")
        for i, section in enumerate(sections):
            logger.debug(f"Section {i+1}: {section}")

        for section in sections:
            section = section.strip()
            if not section:
                continue

            if section.startswith("THOUGHT:"):
                thought_content = section[8:].strip()
                steps.append(
                    ReActStep(
                        step_type=StepType.THOUGHT,
                        content=thought_content,
                        timestamp=self.timestamp_factory(),
                        model_used=model_used,
                    )
                )
            elif section.startswith("ACTION:"):
                self._parse_action_section(section, steps, model_used)
            elif section.startswith("OBSERVATION:"):
                logger.debug("Ignoring model-generated OBSERVATION section")

        if not steps and trusted_response.strip():
            content = trusted_response.strip()
            logger.info("Parsing failed, treating entire response as thought")
            logger.info(f"Full response content: {content}")

            if was_thinking_model:
                enhanced_content = (
                    content
                    + "\n\n[SYSTEM: This was pure analysis. Next step should be action execution by action model.]"
                )
            else:
                enhanced_content = (
                    content
                    + "\n\n[SYSTEM: Action model must use proper tool call format: ACTION: tool_name, PARAMETERS: {...}]"
                )

            steps.append(
                ReActStep(
                    step_type=StepType.THOUGHT,
                    content=enhanced_content,
                    timestamp=self.timestamp_factory(),
                    model_used=model_used,
                )
            )

        return steps

    def _strip_model_observations(self, response: str) -> str:
        trusted_lines = []
        skipping_observation = False

        for line in response.splitlines():
            stripped = line.strip()

            if stripped.startswith("OBSERVATION:"):
                skipping_observation = True
                logger.debug("Stripping model-generated OBSERVATION content")
                continue

            if skipping_observation:
                if stripped.startswith("THOUGHT:") or stripped.startswith("ACTION:"):
                    skipping_observation = False
                    if trusted_lines and trusted_lines[-1] != "":
                        trusted_lines.append("")
                else:
                    continue

            trusted_lines.append(line)

        return "\n".join(trusted_lines)

    def _parse_action_section(
        self,
        section: str,
        steps: list[ReActStep],
        model_used: str,
    ) -> None:
        action_lines = section.split("\n")
        tool_name = action_lines[0][7:].strip()

        if not tool_name or tool_name.lower() in ["none", "null", ""]:
            thought_content = "I need to take action but haven't specified a valid tool."
            steps.append(
                ReActStep(
                    step_type=StepType.THOUGHT,
                    content=thought_content,
                    timestamp=self.timestamp_factory(),
                    model_used=model_used,
                )
            )
            return

        if len(action_lines) < 2:
            return

        params = {}
        for line in action_lines[1:]:
            if line.startswith("PARAMETERS:"):
                params_str = line[11:].strip()
                try:
                    if params_str:
                        params = json.loads(params_str)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse parameters: {params_str}")
                break

        steps.append(
            ReActStep(
                step_type=StepType.ACTION,
                content=f"Using tool: {tool_name}",
                tool_name=tool_name,
                tool_params=params,
                timestamp=self.timestamp_factory(),
                model_used=model_used,
            )
        )
