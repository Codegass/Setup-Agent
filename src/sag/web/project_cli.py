"""Build CLI-equivalent ``sag project`` commands for Web-triggered launches."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectCliCommand:
    """The exact ``sag project ...`` invocation for one launch row."""

    repo_url: str
    name: str | None = None
    ref: str | None = None
    goal: str | None = None
    record: bool = False
    coverage: bool = False

    def project_args(self) -> list[str]:
        """Arguments exactly as a user would type them after ``sag``."""

        args = ["project", self.repo_url]
        if self.name:
            args.extend(["--name", self.name])
        if self.ref:
            args.extend(["--ref", self.ref])
        if self.goal:
            args.extend(["--goal", self.goal])
        if self.record:
            args.append("--record")
        if self.coverage:
            args.append("--coverage")
        return args

    def argv(self) -> list[str]:
        """Full subprocess argv through the active Python environment."""

        return [sys.executable, "-m", "sag.main", *self.project_args()]
