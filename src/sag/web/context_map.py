"""Build abstract trunk/branch context maps from SAG context files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sag.web.models import (
    ActiveBranchSummary,
    ContextMap,
    ContextReference,
    ContextTask,
    TrunkSummary,
)


class ContextMapBuilder:
    def __init__(self, contexts_dir: Path):
        self.contexts_dir = contexts_dir
        self._output_records: dict[str, dict[str, Any]] | None = None

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
        branch_data = self._branch_data(task_id)
        summary = str(item.get("summary") or self._branch_summary(branch_data) or "")
        evidence_refs = [
            self._context_ref(ref)
            for ref in self._list_value(item.get("evidence_refs") or item.get("evidenceRefs"))
        ]
        return ContextTask(
            id=task_id,
            title=str(
                item.get("task")
                or item.get("title")
                or item.get("description")
                or branch_data.get("task_description")
                or branch_data.get("task")
                or "Untitled task"
            ),
            status=str(item.get("status") or "pending"),
            evidence_status=str(
                item.get("evidence_status") or item.get("evidenceStatus") or "unknown"
            ),
            evidence_refs=self._dedupe_refs(evidence_refs),
            conflicts=self._string_list(item.get("conflicts")),
            summary=summary,
            refs=self._dedupe_refs(
                [
                    *[self._context_ref(ref) for ref in item.get("refs", [])],
                    *evidence_refs,
                    *self._branch_refs(branch_data),
                    *[self._context_ref(ref) for ref in self._output_refs_from_text(summary)],
                ]
            ),
            recovered=bool(item.get("recovered", False)),
        )

    def _branch_path(self, task_id: str) -> Path:
        filename = f"{task_id}.json" if task_id.startswith("task_") else f"task_{task_id}.json"
        return self.contexts_dir / filename

    def _branch_data(self, task_id: str) -> dict[str, Any]:
        return self._read_json(self._branch_path(task_id))

    def _branch_summary(self, data: dict[str, Any]) -> str:
        parts: list[str] = []
        previous = self._clean_internal_output_markers(
            str(data.get("previous_task_summary") or "")
        )
        if previous:
            parts.append(previous)

        action = self._latest_history_action(data)
        if action is not None:
            tool_name = str(action.get("tool_name") or "action")
            outcome = "succeeded" if action.get("success") is True else "failed"
            output = self._full_output_for_history_item(action) or str(action.get("output") or "")
            output = self._clean_internal_output_markers(output)
            output = self._compact_text(output)
            parts.append(
                f"{tool_name} {outcome}:\n{output}" if output else f"{tool_name} {outcome}."
            )

        if parts:
            return "\n".join(parts)

        thought = self._latest_history_entry(data, "thought")
        if thought is not None:
            return self._compact_text(str(thought.get("content") or ""))

        return ""

    def _latest_history_action(self, data: dict[str, Any]) -> dict[str, Any] | None:
        history = data.get("history")
        if not isinstance(history, list):
            return None
        return next(
            (
                item
                for item in reversed(history)
                if isinstance(item, dict) and item.get("type") == "action"
            ),
            None,
        )

    def _latest_history_entry(
        self,
        data: dict[str, Any],
        entry_type: str,
    ) -> dict[str, Any] | None:
        history = data.get("history")
        if not isinstance(history, list):
            return None
        return next(
            (
                item
                for item in reversed(history)
                if isinstance(item, dict) and item.get("type") == entry_type
            ),
            None,
        )

    def _branch_refs(self, data: dict[str, Any]) -> list[ContextReference]:
        history = data.get("history")
        if not isinstance(history, list):
            return []

        refs: list[ContextReference] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            refs.extend(
                self._context_ref(match)
                for match in self._output_refs_from_text(str(item.get("output") or ""))
            )
        return refs

    def _compact_text(self, value: str) -> str:
        return "\n".join(" ".join(line.split()) for line in value.splitlines()).strip()

    def _clean_internal_output_markers(self, value: str) -> str:
        lines: list[str] = []
        for line in value.splitlines():
            stripped = line.strip()
            if re.match(r"^\.\.\. \[Output truncated:.*\] \.\.\.$", stripped, re.IGNORECASE):
                continue
            if re.match(r"^\.\.\. \[Search with:.*\] \.\.\.$", stripped, re.IGNORECASE):
                continue
            ref_match = re.match(
                r"^\.\.\. \[Full output ref:\s*(output_[A-Za-z0-9_-]+)\] \.\.\.$",
                stripped,
                re.IGNORECASE,
            )
            if ref_match:
                lines.append(f"Full output ref: {ref_match.group(1)}")
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _output_refs_from_text(self, value: str) -> list[str]:
        return re.findall(r"\boutput_[A-Za-z0-9_-]+\b", value)

    def _full_output_for_history_item(self, item: dict[str, Any]) -> str | None:
        for ref in self._output_refs_from_text(str(item.get("output") or "")):
            record = self._output_record(ref)
            content = record.get("output")
            if isinstance(content, str) and content:
                return content
        return None

    def _context_ref(self, value: Any) -> ContextReference:
        if isinstance(value, dict):
            ref = str(value.get("ref") or value.get("id") or value.get("path") or "")
            label = str(value.get("label") or ref)
            return ContextReference(
                ref=ref,
                label=label,
                kind=str(value.get("kind") or "reference"),
                tool=str(value.get("tool")) if value.get("tool") is not None else None,
                task_id=str(value.get("task_id") or value.get("taskId"))
                if value.get("task_id") or value.get("taskId")
                else None,
                timestamp=str(value.get("timestamp")) if value.get("timestamp") is not None else None,
                content=str(value.get("content")) if value.get("content") is not None else None,
                content_length=self._int_or_none(value.get("content_length") or value.get("contentLength")),
            )

        ref = str(value)
        record = self._output_record(ref)
        content = record.get("output") if record else None
        return ContextReference(
            ref=ref,
            label=ref,
            kind="output" if ref.startswith("output_") else "reference",
            tool=str(record.get("tool_name")) if record.get("tool_name") is not None else None,
            task_id=str(record.get("task_id")) if record.get("task_id") is not None else None,
            timestamp=str(record.get("timestamp")) if record.get("timestamp") is not None else None,
            content=content if isinstance(content, str) else None,
            content_length=self._int_or_none(record.get("output_length"))
            or (len(content) if isinstance(content, str) else None),
        )

    def _dedupe_refs(self, refs: list[ContextReference]) -> list[ContextReference]:
        deduped: list[ContextReference] = []
        seen: set[str] = set()
        for ref in refs:
            key = ref.ref
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped

    def _output_record(self, ref: str) -> dict[str, Any]:
        return self._output_records_by_ref().get(ref, {})

    def _output_records_by_ref(self) -> dict[str, dict[str, Any]]:
        if self._output_records is not None:
            return self._output_records

        records: dict[str, dict[str, Any]] = {}
        path = self.contexts_dir / "full_outputs.jsonl"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if isinstance(record, dict) and record.get("ref_id"):
                    records[str(record["ref_id"])] = record
        except (OSError, json.JSONDecodeError):
            pass

        self._output_records = records
        return records

    def _int_or_none(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _list_value(self, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

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
        data = self._branch_data(task_id)
        return ActiveBranchSummary(
            task=str(data.get("task") or ""),
            why=str(data.get("why") or ""),
            memory=self._memory(data.get("memory")),
            last_refs=self._last_refs(data.get("last_refs")),
            pressure=self._pressure(data),
        )
