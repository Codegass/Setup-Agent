"""Build abstract trunk/branch context maps from SAG context files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sag.web.models import ActiveBranchSummary, ContextMap, ContextTask, TrunkSummary


class ContextMapBuilder:
    def __init__(self, contexts_dir: Path):
        self.contexts_dir = contexts_dir

    def build(self) -> ContextMap | None:
        trunk_path = self._find_trunk()
        if trunk_path is None:
            return None
        trunk_data = self._read_json(trunk_path)
        raw_tasks = trunk_data.get("todo_list") or trunk_data.get("tasks") or []
        tasks = [self._task(item, index) for index, item in enumerate(raw_tasks, start=1)]
        active = next((task for task in tasks if task.status == "active"), None)
        active_branch = self._active_branch(active.id if active else None)
        done = sum(1 for task in tasks if task.status == "completed")

        return ContextMap(
            trunk=TrunkSummary(
                goal=str(trunk_data.get("goal") or trunk_data.get("project_goal") or "Unknown goal"),
                state=str(trunk_data.get("overall_status") or trunk_data.get("state") or "Unknown"),
                progress={"done": done, "total": len(tasks)},
                summary=str(trunk_data.get("summary") or trunk_data.get("latest_summary") or ""),
            ),
            tasks=tasks,
            active_branch=active_branch,
            debug={
                "trunk": str(trunk_path),
                "branches": [str(path) for path in sorted(self.contexts_dir.glob("task_*.json"))],
            },
        )

    def _find_trunk(self) -> Path | None:
        candidates = sorted(self.contexts_dir.glob("trunk*.json"))
        return candidates[0] if candidates else None

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _task(self, item: dict[str, Any], index: int) -> ContextTask:
        task_id = str(item.get("id") or item.get("task_id") or f"T{index}")
        return ContextTask(
            id=task_id,
            title=str(item.get("task") or item.get("title") or "Untitled task"),
            status=str(item.get("status") or "pending"),
            summary=str(item.get("summary") or ""),
            refs=[str(ref) for ref in item.get("refs", [])],
            recovered=bool(item.get("recovered", False)),
        )

    def _active_branch(self, task_id: str | None) -> ActiveBranchSummary:
        if task_id is None:
            return ActiveBranchSummary()
        branch_path = self.contexts_dir / f"task_{task_id}.json"
        data = self._read_json(branch_path)
        return ActiveBranchSummary(
            task=str(data.get("task") or ""),
            why=str(data.get("why") or ""),
            memory=[str(item) for item in data.get("memory", [])],
            last_refs=[dict(item) for item in data.get("last_refs", [])],
            pressure=float(data.get("context_pressure") or data.get("pressure") or 0.0),
        )
