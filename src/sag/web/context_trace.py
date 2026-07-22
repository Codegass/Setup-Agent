"""Build phase-centric context traces from SAG context files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sag.agent.history_state import HistoryActionState, decode_history_action_state

from sag.web.models import (
    ContextReference,
    ContextTrace,
    ContextTraceAction,
    ContextTraceIteration,
    ContextTracePhase,
    ContextTraceTask,
    ContextTraceTrunk,
    ContextTraceWindow,
)


class ContextTraceBuilder:
    def __init__(self, contexts_dir: Path):
        self.contexts_dir = contexts_dir
        self._output_records: dict[str, dict[str, Any]] | None = None

    def build(self) -> ContextTrace | None:
        trunk_path = self._find_trunk()
        if trunk_path is None:
            return None

        trunk_data = self._read_json(trunk_path)
        phase_items = self._phase_items(trunk_data)
        phases = [self._phase(item, index) for index, item in enumerate(phase_items, start=1)]
        done = sum(1 for phase in phases if phase.status.strip().lower() == "completed")

        return ContextTrace(
            trunk=ContextTraceTrunk(
                goal=str(
                    trunk_data.get("goal") or trunk_data.get("project_goal") or "Unknown goal"
                ),
                state=str(
                    trunk_data.get("overall_status")
                    or trunk_data.get("state")
                    or self._derived_state([phase.status for phase in phases])
                ),
                progress={"done": done, "total": len(phases)},
                summary=str(trunk_data.get("summary") or trunk_data.get("latest_summary") or ""),
            ),
            phases=phases,
            debug={
                "trunk": str(trunk_path),
                "phases": [str(self._phase_path(phase.id)) for phase in phases],
                "journals": [
                    str(self._journal_path(phase.name))
                    for phase in phases
                    if self._journal_path(phase.name).exists()
                ],
                "outputs": len(self._output_records_by_ref()),
            },
        )

    def _find_trunk(self) -> Path | None:
        candidates = sorted(self.contexts_dir.glob("trunk*.json"))
        return candidates[-1] if candidates else None

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _phase_items(self, trunk_data: dict[str, Any]) -> list[dict[str, Any]]:
        raw = trunk_data.get("todo_list")
        if not isinstance(raw, list):
            return []
        return [
            item
            for item in raw
            if isinstance(item, dict) and str(item.get("id") or "").startswith("phase_")
        ]

    def _phase(self, item: dict[str, Any], index: int) -> ContextTracePhase:
        phase_id = str(item.get("id") or f"phase_{index}")
        name = phase_id.removeprefix("phase_")
        title = str(item.get("description") or item.get("title") or item.get("task") or name)
        branch_data = self._read_json(self._phase_path(phase_id))
        journal_records = self._journal_records(name)
        history = self._history_entries(branch_data)
        task = self._task(
            phase_id, title, str(item.get("status") or "pending"), history, journal_records
        )
        evidence_refs = [
            self._context_ref(ref)
            for ref in self._list_value(item.get("evidence_refs") or item.get("evidenceRefs"))
        ]
        action_refs = [
            ref
            for iteration in task.iterations
            for action in iteration.actions
            for ref in action.refs
        ]
        refs = self._dedupe_refs([*evidence_refs, *action_refs])
        thought_count = sum(len(iteration.thoughts) for iteration in task.iterations)
        action_count = sum(len(iteration.actions) for iteration in task.iterations)

        return ContextTracePhase(
            id=phase_id,
            name=name,
            title=title,
            status=str(item.get("status") or "pending"),
            notes=str(item.get("notes") or ""),
            key_results=str(item.get("key_results") or item.get("keyResults") or ""),
            evidence_status=str(
                item.get("evidence_status") or item.get("evidenceStatus") or "unknown"
            ),
            evidence_refs=evidence_refs,
            conflicts=self._string_list(item.get("conflicts")),
            refs=refs,
            progress={
                "iterations": len(task.iterations),
                "thoughts": thought_count,
                "actions": action_count,
            },
            tasks=[task],
        )

    def _phase_path(self, phase_id: str) -> Path:
        return self.contexts_dir / f"{phase_id}.json"

    def _journal_path(self, phase_name: str) -> Path:
        return self.contexts_dir / "journal" / f"phase_{phase_name}.journal.jsonl"

    def _journal_records(self, phase_name: str) -> list[dict[str, Any]]:
        try:
            raw = self._journal_path(phase_name).read_text(encoding="utf-8")
        except OSError:
            return []

        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _history_entries(self, branch_data: dict[str, Any]) -> list[dict[str, Any]]:
        history = branch_data.get("history")
        if not isinstance(history, list):
            return []
        return [entry for entry in history if isinstance(entry, dict)]

    def _task(
        self,
        phase_id: str,
        title: str,
        status: str,
        history: list[dict[str, Any]],
        journal_records: list[dict[str, Any]],
    ) -> ContextTraceTask:
        buckets: dict[int, dict[str, Any]] = {}
        synthetic: list[dict[str, Any]] = []

        for record in journal_records:
            iteration = self._int_or_none(record.get("iteration"))
            if iteration is None:
                continue
            buckets.setdefault(iteration, {"iteration": iteration, "history": [], "window": None})
            buckets[iteration]["window"] = self._window(record)

        for entry in history:
            iteration = self._int_or_none(entry.get("iteration"))
            if iteration is None:
                synthetic.append({"iteration": None, "history": [entry], "window": None})
                continue
            buckets.setdefault(iteration, {"iteration": iteration, "history": [], "window": None})
            buckets[iteration]["history"].append(entry)

        # A journal record is a context-window snapshot written every iteration;
        # history (thoughts + actions) flushes slightly behind. Buckets that exist
        # ONLY because of a journal record — no history entries — are not
        # trajectory steps: mid-run they are iterations the journal reached before
        # history was flushed, and rendering them shows misleading "No action
        # taken — reasoning step" rows (live TVM run: iterations 14–52 all blank).
        # Keep only buckets with real history; genuine reasoning steps carry a
        # `thought` history entry and survive.
        ordered = [buckets[key] for key in sorted(buckets) if buckets[key]["history"]]
        ordered.extend(synthetic)
        iterations = [
            self._iteration(bucket, sequence) for sequence, bucket in enumerate(ordered, start=1)
        ]

        return ContextTraceTask(
            id=f"{phase_id}/work",
            title=title,
            status=status,
            iterations=iterations,
        )

    def _iteration(self, bucket: dict[str, Any], sequence: int) -> ContextTraceIteration:
        thoughts: list[str] = []
        actions: list[ContextTraceAction] = []
        for entry in bucket["history"]:
            entry_type = str(entry.get("type") or "")
            if entry_type == "thought":
                content = str(entry.get("content") or "").strip()
                if content:
                    thoughts.append(content)
            elif entry_type == "action":
                actions.append(self._action(entry))

        return ContextTraceIteration(
            iteration=bucket["iteration"],
            sequence=sequence,
            thoughts=thoughts,
            actions=actions,
            window=bucket["window"],
        )

    def _action(self, entry: dict[str, Any]) -> ContextTraceAction:
        output = str(entry.get("output") or "")
        refs = self._dedupe_refs(
            [
                self._context_ref(ref)
                for ref in [
                    *self._list_value(entry.get("output_refs") or entry.get("outputRefs")),
                    *self._output_refs_from_text(output),
                ]
            ]
        )
        parameters = entry.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        return ContextTraceAction(
            tool_name=str(entry.get("tool_name") or entry.get("toolName") or "tool"),
            success=(
                True
                if decode_history_action_state(entry) is HistoryActionState.SUCCESS
                else (
                    False
                    if decode_history_action_state(entry) is HistoryActionState.FAILED
                    else None
                )
            ),
            parameters=parameters,
            output=self._clean_internal_output_markers(output),
            observation=str(entry.get("observation") or ""),
            refs=refs,
            dispatch_status=(
                str(entry.get("dispatch_status") or entry.get("dispatchStatus"))
                if entry.get("dispatch_status") or entry.get("dispatchStatus")
                else None
            ),
        )

    def _window(self, record: dict[str, Any]) -> ContextTraceWindow:
        segments = record.get("segments")
        delta = record.get("delta")
        return ContextTraceWindow(
            total_chars=self._int_or_none(record.get("total_chars") or record.get("totalChars"))
            or 0,
            step_span=self._int_or_none(record.get("step_span") or record.get("stepSpan")),
            segments=segments if isinstance(segments, dict) else {},
            delta=delta if isinstance(delta, dict) else {},
            intro_text=(
                str(record.get("intro_text")) if record.get("intro_text") is not None else None
            ),
            ledger_text=(
                str(record.get("ledger_text")) if record.get("ledger_text") is not None else None
            ),
        )

    def _context_ref(self, value: Any) -> ContextReference:
        if isinstance(value, dict):
            ref = str(value.get("ref") or value.get("id") or value.get("path") or "")
            record = self._output_record(ref)
            content = value.get("content")
            if content is None:
                content = record.get("output")
            label = str(value.get("label") or ref)
            return ContextReference(
                ref=ref,
                label=label,
                kind=str(
                    value.get("kind")
                    or ("output" if record or ref.startswith("output_") else "reference")
                ),
                tool=(
                    str(value.get("tool") or record.get("tool_name"))
                    if value.get("tool") is not None or record.get("tool_name") is not None
                    else None
                ),
                task_id=(
                    str(value.get("task_id") or value.get("taskId"))
                    if value.get("task_id") or value.get("taskId")
                    else str(record.get("task_id")) if record.get("task_id") is not None else None
                ),
                timestamp=(
                    str(value.get("timestamp") or record.get("timestamp"))
                    if value.get("timestamp") is not None or record.get("timestamp") is not None
                    else None
                ),
                content=str(content) if content is not None else None,
                content_length=self._int_or_none(
                    value.get("content_length")
                    or value.get("contentLength")
                    or record.get("output_length")
                )
                or (len(content) if isinstance(content, str) else None),
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
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("ref_id"):
                records[str(record["ref_id"])] = record

        self._output_records = records
        return records

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

    def _list_value(self, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def _int_or_none(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _derived_state(self, statuses: list[str]) -> str:
        normalized = {status.strip().lower() for status in statuses}
        if not normalized:
            return "unknown"
        if normalized & {"failed", "error"}:
            return "failed"
        if normalized & {"active", "running", "in_progress"}:
            return "running"
        if normalized <= {"completed"}:
            return "completed"
        if "completed" in normalized:
            return "partial"
        return "pending"
