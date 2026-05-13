"""Skill engine — Claude-Code-style skills for SAG.

A *skill* is a self-contained markdown file (`SKILL.md`) with YAML-ish
frontmatter that names the skill and gives a one-line "when to use" hint,
followed by a body of guidance the agent invokes situationally instead of
reading on every turn.

See `docs/SKILL_ENGINE_DESIGN.md` for the full motivation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from loguru import logger

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_KEY_VALUE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*?)\s*$")


@dataclass(frozen=True)
class Skill:
    """A single loaded skill."""

    name: str
    description: str
    body: str
    path: Path


class SkillLoadError(Exception):
    """Raised when a SKILL.md is malformed; non-fatal for the registry."""


class SkillRegistry:
    """Discovers and serves skills from one or more search paths.

    The registry is intentionally minimal: no caching of body content beyond
    the in-memory dict, no skill-lifecycle tracking, no "active skill" stack.
    The agent treats every `skill(name=...)` call as a one-shot retrieval.
    """

    def __init__(self, search_paths: Iterable[Path]):
        self._search_paths: List[Path] = [Path(p) for p in search_paths]
        self._skills: Dict[str, Skill] = {}

    def load(self) -> None:
        """Walk search paths and load every `*/SKILL.md`.

        Later paths override earlier ones for the same skill name (lets a user
        skill in `~/.sag/skills/` shadow a built-in one).
        """
        loaded = 0
        for root in self._search_paths:
            if not root.is_dir():
                logger.debug(f"Skill search path missing, skipping: {root}")
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                try:
                    skill = self._parse_skill_file(skill_file)
                except SkillLoadError as e:
                    logger.warning(f"Skipping malformed skill at {skill_file}: {e}")
                    continue
                if skill.name in self._skills:
                    logger.debug(f"Skill '{skill.name}' overridden by {skill.path}")
                self._skills[skill.name] = skill
                loaded += 1
        logger.info(f"SkillRegistry loaded {loaded} skill(s) from {len(self._search_paths)} path(s)")

    def list(self) -> List[Skill]:
        """Return all loaded skills, sorted by name for stable output."""
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    @staticmethod
    def _parse_skill_file(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            raise SkillLoadError("missing YAML frontmatter (expected --- ... --- header)")

        front = match.group("front")
        body = match.group("body").strip()

        fields: Dict[str, str] = {}
        for raw_line in front.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            kv = _KEY_VALUE_RE.match(line)
            if not kv:
                raise SkillLoadError(f"unparseable frontmatter line: {raw_line!r}")
            fields[kv.group(1)] = kv.group(2)

        name = fields.get("name") or path.parent.name
        description = fields.get("description")
        if not description:
            raise SkillLoadError("frontmatter missing required 'description' field")
        if not body:
            raise SkillLoadError("skill body is empty")

        return Skill(name=name, description=description, body=body, path=path)


def default_search_paths(project_root: Optional[Path] = None) -> List[Path]:
    """Standard skill search paths, in lowest-to-highest precedence order.

    The project root (where built-in skills ship) comes first, the user-local
    overrides directory comes last so users can shadow built-ins.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    paths = [root / "config" / "skills"]
    user_skills = Path.home() / ".sag" / "skills"
    paths.append(user_skills)
    return paths
