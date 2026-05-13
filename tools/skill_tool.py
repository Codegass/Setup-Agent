"""SkillTool — agent-facing entry point to the skill engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import BaseTool, ToolError, ToolResult

if TYPE_CHECKING:
    from agent.skill_engine import SkillRegistry


class SkillTool(BaseTool):
    """Invoke a named skill — returns its body as guidance.

    Skills are markdown files discovered at agent init. The tool itself does
    no workflow tracking; the agent calls `skill(name="...")` whenever the
    skill's `description` matches the current situation, receives the body
    as observation text, and is expected to follow that guidance using the
    other tools.
    """

    def __init__(self, registry: "SkillRegistry"):
        super().__init__(
            name="skill",
            description=(
                "Invoke a named skill from the skill library. Returns the skill's "
                "guidance body for you to follow using the other tools. Use this "
                "instead of relying on memory for project-setup workflows. Call "
                "with skill(name='<skill-name>')."
            ),
        )
        self._registry = registry

    def get_usage_example(self) -> str:
        skills = self._registry.list()
        if not skills:
            return 'skill(name="<skill-name>")'
        sample = skills[0].name
        return f'skill(name="{sample}")  # one of: {", ".join(s.name for s in skills)}'

    def execute(self, name: Optional[str] = None) -> ToolResult:
        if not name:
            available = ", ".join(s.name for s in self._registry.list()) or "(none loaded)"
            raise ToolError(
                message="The `name` parameter is required.",
                category="validation",
                error_code="SKILL_NAME_REQUIRED",
                suggestions=[
                    f"Available skills: {available}",
                    'Call as skill(name="<skill-name>")',
                ],
                retryable=True,
            )

        skill = self._registry.get(name)
        if skill is None:
            available = ", ".join(s.name for s in self._registry.list()) or "(none loaded)"
            raise ToolError(
                message=f"No skill named {name!r} is registered.",
                category="validation",
                error_code="SKILL_NOT_FOUND",
                suggestions=[f"Available skills: {available}"],
                retryable=False,
            )

        # Inject a small header so the agent can tell the body is skill guidance
        # rather than ambient tool output.
        output = f"# Skill: {skill.name}\n\n{skill.body}"
        return ToolResult(
            success=True,
            output=output,
            metadata={"skill_name": skill.name, "skill_path": str(skill.path)},
        )
