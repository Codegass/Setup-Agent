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
        tasks = self._tasks(trunk_data)
        active = next((task for task in tasks if self._is_active_status(task.status)), None)
        active_branch = self._active_branch(active.id if active else None)
        done = sum(1 for task in tasks if task.status == "completed")

        return ContextMap(
            trunk=TrunkSummary(
                goal=str(
                    trunk_data.get("goal") or trunk_data.get("project_goal") or "Unknown goal"
                ),
                state=str(
                    trunk_data.get("overall_status")
                    or trunk_data.get("state")
                    or self._derived_state(tasks)
                ),
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
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _tasks(self, trunk_data: dict[str, Any]) -> list[ContextTask]:
        raw_tasks = self._raw_tasks(trunk_data)
        if raw_tasks is None:
            return []
        return [
            self._task(item, index)
            for index, item in enumerate(raw_tasks, start=1)
            if isinstance(item, dict)
        ]

    def _raw_tasks(self, trunk_data: dict[str, Any]) -> list[Any] | None:
        for key in ("todo_list", "tasks"):
            value = trunk_data.get(key)
            if isinstance(value, list):
                return value
        return None

    def _task(self, item: dict[str, Any], index: int) -> ContextTask:
        task_id = str(item.get("id") or item.get("task_id") or f"T{index}")
        return ContextTask(
            id=task_id,
            title=str(
                item.get("task") or item.get("title") or item.get("description") or "Untitled task"
            ),
            status=str(item.get("status") or "pending"),
            summary=str(item.get("summary") or ""),
            refs=[str(ref) for ref in item.get("refs", [])],
            recovered=bool(item.get("recovered", False)),
        )

    def _is_active_status(self, status: str) -> bool:
        return status.strip().lower() in {"active", "running", "in_progress"}

    def _derived_state(self, tasks: list[ContextTask]) -> str:
        statuses = {task.status.strip().lower() for task in tasks}
        if not statuses:
            return "unknown"
        if statuses & {"failed", "error"}:
            return "failed"
        if statuses & {"active", "running", "in_progress"}:
            return "running"
        if statuses <= {"completed"}:
            return "completed"
        if "completed" in statuses:
            return "partial"
        return "pending"

    def _memory(self, value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item) for item in value]

    def _last_refs(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        return [
            {str(key): str(item_value) for key, item_value in item.items()}
            for item in value
            if isinstance(item, dict)
        ]

    def _pressure(self, data: dict[str, Any]) -> float:
        value = data.get("context_pressure") or data.get("pressure") or 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _active_branch(self, task_id: str | None) -> ActiveBranchSummary:
        if task_id is None:
            return ActiveBranchSummary()
        filename = f"{task_id}.json" if task_id.startswith("task_") else f"task_{task_id}.json"
        branch_path = self.contexts_dir / filename
        data = self._read_json(branch_path)
        return ActiveBranchSummary(
            task=str(data.get("task") or ""),
            why=str(data.get("why") or ""),
            memory=self._memory(data.get("memory")),
            last_refs=self._last_refs(data.get("last_refs")),
            pressure=self._pressure(data),
        )
