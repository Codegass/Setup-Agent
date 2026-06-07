"""Read local SAG session index artifacts for the web dashboard."""

from __future__ import annotations

import json
import shlex
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from sag.web.context_map import ContextMapBuilder
from sag.web.models import (
    BuildSummary,
    ContextMap,
    EvidenceGroup,
    EvidenceRecord,
    ExecutionSessionDetail,
    ExecutionSessionSummary,
    TestSummary,
    WorkspaceSummary,
)

SESSION_INDEX_PATH = "/workspace/.setup_agent/sessions/index.json"


class SessionRegistry:
    def read_index(self, workspace_root: Path, workspace_id: str) -> list[ExecutionSessionSummary]:
        index_path = workspace_root / ".setup_agent" / "sessions" / "index.json"

        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return []

        return parse_session_index(raw, workspace_id)


class ContainerSessionRegistry:
    def __init__(
        self,
        orchestrator_factory: Callable[[str], Any] | None = None,
        workspace_registry_factory: Callable[[], Any] | None = None,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.workspace_registry_factory = workspace_registry_factory

    def list_workspace_sessions(self, workspace: WorkspaceSummary) -> list[ExecutionSessionSummary]:
        orchestrator = self._orchestrator(workspace.id)
        raw = _read_container_file(orchestrator, SESSION_INDEX_PATH)
        if raw is None:
            return _legacy_session_summaries(orchestrator, workspace.id)

        rows = parse_session_index(raw, workspace.id)
        return rows or _legacy_session_summaries(orchestrator, workspace.id)

    def get_session_detail(self, session_id: str) -> ExecutionSessionDetail:
        for workspace in self._workspaces():
            detail = self.get_workspace_session_detail(workspace, session_id)
            if detail is not None:
                return detail

        raise KeyError(session_id)

    def get_workspace_session_detail(
        self,
        workspace: WorkspaceSummary,
        session_id: str,
    ) -> ExecutionSessionDetail | None:
        orchestrator = self._orchestrator(workspace.id)
        raw = _read_container_file(orchestrator, SESSION_INDEX_PATH)

        item = _find_session_item(raw, session_id) if raw is not None else None
        if item is None:
            item = _legacy_session_item(orchestrator, workspace.id)
            if item is None or item.get("id") != session_id:
                return None

        context = _read_context_map(orchestrator)
        return _session_detail(item, workspace.id, context)

    def _orchestrator(self, workspace_id: str) -> Any:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(workspace_id)

        from sag.docker_orch.orch import DockerOrchestrator

        return DockerOrchestrator(project_name=workspace_id.removeprefix("sag-"))

    def _workspaces(self) -> list[WorkspaceSummary]:
        if self.workspace_registry_factory is not None:
            registry = self.workspace_registry_factory()
        else:
            from sag.web.workspace_registry import WorkspaceRegistry

            registry = WorkspaceRegistry()

        return registry.list_workspaces()


class ContainerSessionStore:
    def __init__(
        self,
        orchestrator_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.clock = clock if clock is not None else datetime.now

    def mark_started(
        self,
        *,
        workspace_id: str,
        session_id: str,
        task: str,
        source_session: str | None,
    ) -> None:
        now = self.clock().isoformat(timespec="seconds")
        item = {
            "id": session_id,
            "workspace": workspace_id,
            "title": task,
            "status": "running",
            "entry": "Web UI",
            "start": now,
            "finish": None,
            "duration": "running",
            "build": "none",
            "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
            "report": "none",
            "files": 0,
            "evidence": 1,
            "outcome": "Task is running.",
            "source_session": source_session,
            "updated": now,
        }
        self._upsert(workspace_id, item)

    def mark_finished(
        self,
        *,
        workspace_id: str,
        session_id: str,
        success: bool,
        outcome: str,
    ) -> None:
        now = self.clock().isoformat(timespec="seconds")
        orchestrator = self._orchestrator(workspace_id)
        payload = _read_index_payload(orchestrator)
        sessions = _session_items(payload)
        item = next(
            (
                candidate
                for candidate in sessions
                if isinstance(candidate, dict) and candidate.get("id") == session_id
            ),
            None,
        )
        if item is None:
            item = {
                "id": session_id,
                "workspace": workspace_id,
                "title": outcome,
                "entry": "Web UI",
                "start": now,
            }
            sessions.append(item)

        item["status"] = "completed" if success else "failed"
        item["finish"] = now
        item["duration"] = _duration(str(item.get("start") or now), now)
        item["outcome"] = outcome
        item["updated"] = now
        item["evidence"] = max(_to_int(item.get("evidence")), 1)

        _write_index_payload(orchestrator, {"sessions": sessions})

    def _upsert(self, workspace_id: str, item: dict[str, Any]) -> None:
        orchestrator = self._orchestrator(workspace_id)
        payload = _read_index_payload(orchestrator)
        sessions = _session_items(payload)
        sessions = [
            candidate
            for candidate in sessions
            if not (isinstance(candidate, dict) and candidate.get("id") == item["id"])
        ]
        sessions.append(item)
        _write_index_payload(orchestrator, {"sessions": sessions})

    def _orchestrator(self, workspace_id: str) -> Any:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(workspace_id)

        from sag.docker_orch.orch import DockerOrchestrator

        return DockerOrchestrator(project_name=workspace_id.removeprefix("sag-"))


def parse_session_index(raw: str, workspace_id: str) -> list[ExecutionSessionSummary]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    sessions = _session_items(payload)

    rows: list[ExecutionSessionSummary] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue

        rows.append(_session_summary(item, workspace_id))

    return rows


def _session_summary(item: dict[str, Any], workspace_id: str) -> ExecutionSessionSummary:
    test = item.get("test") or {}
    if not isinstance(test, dict):
        test = {}

    return ExecutionSessionSummary(
        id=_text(item.get("id"), default="unknown"),
        workspace=_text(item.get("workspace"), default=workspace_id),
        title=_text(item.get("title"), default="Untitled task"),
        status=_text(item.get("status"), default="unknown"),
        entry=_text(item.get("entry"), default="external"),
        start=_text(item.get("start"), default="—"),
        finish=_optional_text(item.get("finish")),
        duration=_text(item.get("duration"), default="—"),
        build=_text(item.get("build"), default="none"),
        test=TestSummary(
            state=_text(test.get("state"), default="none"),
            pass_count=_to_int(test.get("pass")),
            fail_count=_to_int(test.get("fail")),
            skip_count=_to_int(test.get("skip")),
            total=_to_int(test.get("total")),
        ),
        report=_text(item.get("report"), default="none"),
        files=_to_int(item.get("files")),
        evidence=_to_int(item.get("evidence")),
    )


def _session_detail(
    item: dict[str, Any],
    workspace_id: str,
    context: ContextMap | None,
) -> ExecutionSessionDetail:
    summary = _session_summary(item, workspace_id)
    outcome = _text(item.get("outcome"), default=summary.title)
    build = _build_summary(item.get("build"))

    return ExecutionSessionDetail(
        id=summary.id,
        workspace=summary.workspace,
        title=summary.title,
        status=summary.status,
        entry=summary.entry,
        start=summary.start,
        duration=summary.duration,
        outcome=outcome,
        build=build,
        test=summary.test,
        report=summary.report,
        report_doc=None,
        blocker=None,
        evidence=_evidence(item, outcome),
        files=None,
        context=context,
        logs=[],
        partial=False,
    )


def _legacy_session_summaries(
    orchestrator: Any,
    workspace_id: str,
) -> list[ExecutionSessionSummary]:
    item = _legacy_session_item(orchestrator, workspace_id)
    if item is None:
        return []
    return [_session_summary(item, workspace_id)]


def _legacy_session_item(orchestrator: Any, workspace_id: str) -> dict[str, Any] | None:
    raw = _read_container_file(orchestrator, "/workspace/.sag_last_comment.json")
    if raw is None:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    comment = _text(payload.get("comment"), default="")
    if not comment:
        return None

    timestamp = _text(payload.get("timestamp"), default="")
    session_id = _legacy_session_id(timestamp)
    status = _legacy_status(comment)

    return {
        "id": session_id,
        "workspace": workspace_id,
        "title": _legacy_title(comment),
        "status": status,
        "entry": "SAG",
        "start": timestamp or "—",
        "finish": timestamp if status == "completed" else None,
        "duration": "unknown",
        "build": "none",
        "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
        "report": "none",
        "files": 0,
        "evidence": 1,
        "outcome": comment,
        "updated": timestamp or "unknown",
    }


def _legacy_session_id(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return "LEGACY-latest"
    return f"LEGACY-{parsed.strftime('%Y%m%d-%H%M%S')}"


def _legacy_status(comment: str) -> str:
    normalized = comment.strip().lower()
    if normalized.startswith("task completed:"):
        return "completed"
    if normalized.startswith("task in progress:"):
        return "running"
    if normalized.startswith("task failed:"):
        return "failed"
    return "unknown"


def _legacy_title(comment: str) -> str:
    for prefix in ("Task completed:", "Task in progress:", "Task failed:"):
        if comment.startswith(prefix):
            title = comment.removeprefix(prefix).strip()
            return title or comment
    return comment


def _build_summary(value: Any) -> BuildSummary:
    if isinstance(value, dict):
        try:
            return BuildSummary.model_validate(value)
        except Exception:
            return BuildSummary()

    return BuildSummary(state=_text(value, default="none"))


def _evidence(item: dict[str, Any], outcome: str) -> list[EvidenceGroup]:
    time = _display_time(_optional_text(item.get("finish")) or _text(item.get("start"), default=""))
    status = _status_for_evidence(_text(item.get("status"), default="info"))
    record = EvidenceRecord(
        time=time,
        status=status,
        title=_text(item.get("title"), default="Workspace task"),
        detail=outcome,
        ref=f"{SESSION_INDEX_PATH}#{_text(item.get('id'), default='session')}",
    )
    return [
        EvidenceGroup(
            source="SAG session",
            status=status,
            counts="1 record",
            time=record.time,
            summary=outcome,
            records=[record],
        )
    ]


def _status_for_evidence(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"completed", "success", "succeeded"}:
        return "success"
    if normalized in {"failed", "failure", "error"}:
        return "failure"
    if normalized in {"partial", "incomplete"}:
        return "partial"
    return "info"


def _display_time(value: str) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return value


def _duration(start: str, finish: str) -> str:
    try:
        start_time = datetime.fromisoformat(start)
        finish_time = datetime.fromisoformat(finish)
    except ValueError:
        return "—"

    seconds = max(int((finish_time - start_time).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"

    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"

    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _read_container_file(orchestrator: Any, path: str) -> str | None:
    try:
        result = orchestrator.execute_command(f"cat {shlex.quote(path)} 2>/dev/null", timeout=5)
    except TypeError:
        result = orchestrator.execute_command(f"cat {shlex.quote(path)} 2>/dev/null")
    except Exception:
        logger.debug("Failed to read container artifact {}", path)
        return None

    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return None

    output = result.get("output")
    if not isinstance(output, str) or not output.strip():
        return None

    return output


def _read_index_payload(orchestrator: Any) -> dict[str, Any]:
    raw = _read_container_file(orchestrator, SESSION_INDEX_PATH)
    if raw is None:
        return {"sessions": []}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"sessions": []}

    return payload if isinstance(payload, dict) else {"sessions": []}


def _write_index_payload(orchestrator: Any, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, indent=2, sort_keys=True)
    command = (
        "mkdir -p /workspace/.setup_agent/sessions && "
        f"printf %s {shlex.quote(raw)} > {shlex.quote(SESSION_INDEX_PATH)}"
    )
    try:
        result = orchestrator.execute_command(command, timeout=5)
    except TypeError:
        result = orchestrator.execute_command(command)

    if isinstance(result, dict) and result.get("exit_code", 0) != 0:
        output = result.get("output") or result.get("stderr") or "unknown error"
        raise RuntimeError(f"Failed to write SAG web session index: {output}")


def _session_items(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []

    sessions = payload.get("sessions")
    return sessions if isinstance(sessions, list) else []


def _find_session_item(raw: str, session_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    for item in _session_items(payload):
        if isinstance(item, dict) and item.get("id") == session_id:
            return item

    return None


def _read_context_map(orchestrator: Any) -> ContextMap | None:
    command = (
        "find /workspace/.setup_agent/contexts -maxdepth 1 -type f "
        "\\( -name 'trunk*.json' -o -name 'task_*.json' \\) "
        "-printf '%f\\n' 2>/dev/null || true"
    )
    try:
        result = orchestrator.execute_command(command, timeout=5)
    except TypeError:
        result = orchestrator.execute_command(command)
    except Exception:
        return None

    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return None

    output = result.get("output")
    if not isinstance(output, str):
        return None

    filenames = [_safe_context_filename(line) for line in output.splitlines()]
    filenames = [filename for filename in filenames if filename is not None]
    if not filenames:
        return None

    with tempfile.TemporaryDirectory() as temp_dir:
        contexts_dir = Path(temp_dir) / "contexts"
        contexts_dir.mkdir()

        for filename in filenames[:50]:
            raw = _read_container_file(
                orchestrator,
                f"/workspace/.setup_agent/contexts/{filename}",
            )
            if raw is None:
                continue
            (contexts_dir / filename).write_text(raw, encoding="utf-8")

        return ContextMapBuilder(contexts_dir).build()


def _safe_context_filename(value: str) -> str | None:
    filename = value.strip()
    if not filename or "/" in filename or not filename.endswith(".json"):
        return None
    if not (filename.startswith("trunk") or filename.startswith("task_")):
        return None
    return filename


def _text(value: Any, default: str) -> str:
    if value is None:
        return default

    text = str(value).strip()
    if not text:
        return default

    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    return text


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ContainerSessionRegistry",
    "ContainerSessionStore",
    "SessionRegistry",
    "parse_session_index",
]
