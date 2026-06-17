"""Read local SAG session index artifacts for the web dashboard."""

from __future__ import annotations

import json
import re
import shlex
import time
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from sag.web.context_trace import ContextTraceBuilder
from sag.web.models import (
    BuildSummary,
    ContextTrace,
    EvidenceGroup,
    EvidenceRecord,
    ExecutionSessionDetail,
    ExecutionSessionSummary,
    ModuleRollup,
    ModuleSummary,
    ReportDocument,
    TestSummary,
    WorkspaceSummary,
)

SESSION_INDEX_PATH = "/workspace/.setup_agent/sessions/index.json"
_EVIDENCE_STATUS_VALUES = {"success", "partial", "blocked", "conflict", "unknown"}
_EVIDENCE_STATUS_PRECEDENCE = ("blocked", "conflict", "partial", "unknown", "success")
_CONTEXTS_DIR = "/workspace/.setup_agent/contexts"


class SessionRegistry:
    def read_index(self, workspace_root: Path, workspace_id: str) -> list[ExecutionSessionSummary]:
        index_path = workspace_root / ".setup_agent" / "sessions" / "index.json"

        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return []

        return parse_session_index(raw, workspace_id)


class ContainerSessionRegistry:
    # Unresolvable session ids are remembered for this long: the dashboard
    # polls session details every ~3s, and a stale id (e.g. a removed
    # container's session) used to trigger a FULL fleet scan — several docker
    # execs per workspace — on every poll, hammering the docker daemon.
    NEGATIVE_SESSION_TTL_SECONDS = 15.0

    def __init__(
        self,
        orchestrator_factory: Callable[[str], Any] | None = None,
        workspace_registry_factory: Callable[[], Any] | None = None,
        logs_root: Path | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.workspace_registry_factory = workspace_registry_factory
        self.logs_root = logs_root if logs_root is not None else Path("logs")
        self._now = now_fn if now_fn is not None else time.monotonic
        self._missing_sessions: dict[str, float] = {}

    def list_workspace_sessions(self, workspace: WorkspaceSummary) -> list[ExecutionSessionSummary]:
        orchestrator = self._orchestrator(workspace.id)
        raw = _read_container_file(orchestrator, SESSION_INDEX_PATH)
        rows = parse_session_index(raw, workspace.id) if raw is not None else []
        return _merge_session_summaries(
            [
                *_setup_artifact_summaries(orchestrator, workspace.id, self.logs_root),
                *_legacy_session_summaries(orchestrator, workspace.id),
                *rows,
            ]
        )

    def get_session_detail(self, session_id: str) -> ExecutionSessionDetail:
        expiry = self._missing_sessions.get(session_id)
        if expiry is not None:
            if self._now() < expiry:
                # Known-missing: answer the 404 instantly, no docker execs.
                raise KeyError(session_id)
            del self._missing_sessions[session_id]

        for workspace in self._workspaces():
            detail = self.get_workspace_session_detail(workspace, session_id)
            if detail is not None:
                self._missing_sessions.pop(session_id, None)
                return detail

        logger.warning(
            f"Session {session_id} not found in any workspace; suppressing "
            f"re-scans for {self.NEGATIVE_SESSION_TTL_SECONDS:.0f}s"
        )
        self._missing_sessions[session_id] = self._now() + self.NEGATIVE_SESSION_TTL_SECONDS
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
            if item is not None and item.get("id") != session_id:
                item = None

        if item is None:
            item = _setup_artifact_item(orchestrator, workspace.id, self.logs_root)
            if item is None or item.get("id") != session_id:
                return None

        context = _read_context_trace(orchestrator)
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
            "evidence_status": "unknown",
            "evidence_status_source": "default",
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
        if _should_set_finished_evidence_status(item):
            item["evidence_status"] = "success" if success else "blocked"
            item["evidence_status_source"] = "finish"
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
        evidence_status=_evidence_status(item),
        entry=_text(item.get("entry"), default="external"),
        start=_text(item.get("start"), default="—"),
        finish=_optional_text(item.get("finish")),
        duration=_text(item.get("duration"), default="—"),
        build=_build_state_value(item.get("build")),
        test=TestSummary(
            state=_text(test.get("state"), default="none"),
            pass_count=_to_int(_value_for_keys(test, "pass", "pass_count")),
            fail_count=_to_int(_value_for_keys(test, "fail", "fail_count")),
            skip_count=_to_int(_value_for_keys(test, "skip", "skip_count")),
            total=_to_int(test.get("total")),
            pass_rate=_to_float_or_none(_value_for_keys(test, "pass_rate", "passRate")),
            execution_rate=_to_float_or_none(
                _value_for_keys(test, "execution_rate", "executionRate")
            ),
            errors=_to_int(test.get("errors")),
            report_file_count=test.get("report_file_count"),
            unique_total=test.get("unique_total"),
            unique_passed=test.get("unique_passed"),
            unique_failed=test.get("unique_failed"),
            unique_errors=test.get("unique_errors"),
            unique_skipped=test.get("unique_skipped"),
            declared_total=test.get("declared_total"),
            method_execution_rate=_to_float_or_none(test.get("method_execution_rate")),
            failing_names=test.get("failing_names") or [],
            conflicts=test.get("conflicts") or [],
            evidence_refs=test.get("evidence_refs") or [],
        ),
        report=_text(item.get("report"), default="none"),
        files=_to_int(item.get("files")),
        evidence=_to_int(item.get("evidence")),
    )


def _module_summaries(value: Any) -> list[ModuleSummary]:
    """Validate per-module records, dropping any that are malformed.

    A present-but-partially-malformed module_metrics.json (e.g. class_count as a
    non-numeric string from a hand-edit or version skew) must not crash the whole
    detail endpoint; mirror _build_summary and degrade gracefully.
    """
    out: list[ModuleSummary] = []
    for m in value if isinstance(value, list) else []:
        try:
            out.append(ModuleSummary.model_validate(m))
        except Exception:
            continue
    return out


def _module_rollup(value: Any) -> ModuleRollup | None:
    if not value:
        return None
    try:
        return ModuleRollup.model_validate(value)
    except Exception:
        return None


def _session_detail(
    item: dict[str, Any],
    workspace_id: str,
    context: ContextTrace | None,
) -> ExecutionSessionDetail:
    summary = _session_summary(item, workspace_id)
    outcome = _text(item.get("outcome"), default=summary.title)
    build = _build_summary(item.get("build"))
    report_doc = _report_document(item)

    return ExecutionSessionDetail(
        id=summary.id,
        workspace=summary.workspace,
        title=summary.title,
        status=summary.status,
        evidence_status=summary.evidence_status,
        entry=summary.entry,
        start=summary.start,
        duration=summary.duration,
        outcome=outcome,
        build=build,
        test=summary.test,
        modules=_module_summaries(item.get("modules")),
        module_summary=_module_rollup(item.get("module_summary")),
        report=summary.report,
        report_doc=report_doc,
        blocker=None,
        evidence=_evidence(item, outcome),
        files=None,
        context=context,
        logs=_log_lines(item.get("logs")),
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


def _setup_artifact_summaries(
    orchestrator: Any,
    workspace_id: str,
    logs_root: Path | None = None,
) -> list[ExecutionSessionSummary]:
    item = _setup_artifact_item(orchestrator, workspace_id, logs_root)
    if item is None:
        return []
    return [_session_summary(item, workspace_id)]


def _merge_session_summaries(
    rows: list[ExecutionSessionSummary],
) -> list[ExecutionSessionSummary]:
    merged: dict[str, ExecutionSessionSummary] = {}
    for row in rows:
        merged[row.id] = row
    return sorted(merged.values(), key=_session_sort_key)


def _session_sort_key(row: ExecutionSessionSummary) -> tuple[str, str]:
    timestamp = _normalize_timestamp(row.finish or row.start)
    return (timestamp or row.finish or row.start or "", row.id)


def _setup_artifact_item(
    orchestrator: Any,
    workspace_id: str,
    logs_root: Path | None = None,
) -> dict[str, Any] | None:
    trunk = _read_latest_trunk(orchestrator)
    if trunk is None:
        return None

    trunk_path, trunk_data = trunk
    report_path = _latest_setup_report_path(orchestrator)
    report_raw = _read_container_file(orchestrator, report_path) if report_path else None
    created = _text(trunk_data.get("created_at"), default="")
    updated = _text(trunk_data.get("last_updated"), default=created)
    finish = _report_generated_at(report_raw) or updated
    tasks = _raw_task_dicts(trunk_data)
    status = _setup_status(tasks, report_path)
    metrics = _read_report_metrics(orchestrator)
    module_metrics = _read_module_metrics(orchestrator)
    test = _test_payload_from_metrics(metrics) or _test_payload_from_report(report_raw)
    build_payload = _build_payload_from_metrics(metrics) or _build_payload_from_report(report_raw)
    evidence_status = _setup_evidence_status(trunk_data, tasks, report_raw)
    context_id = _text(trunk_data.get("context_id"), default=Path(trunk_path).stem)
    project_name = _text(
        trunk_data.get("project_name"),
        default=workspace_id.removeprefix("sag-"),
    )

    return {
        "id": _setup_session_id(context_id, created, workspace_id),
        "workspace": workspace_id,
        "title": _text(trunk_data.get("goal"), default="Project setup"),
        "status": status,
        "evidence_status": evidence_status,
        "entry": "CLI",
        "start": _normalize_timestamp(created) or created or "—",
        "finish": _normalize_timestamp(finish) if status == "completed" else None,
        "duration": _duration(
            _normalize_timestamp(created) or created,
            _normalize_timestamp(finish) or finish,
        ),
        "build": build_payload,
        "test": test,
        "modules": _modules_payload_from_metrics(module_metrics),
        "module_summary": _module_rollup_from_metrics(module_metrics),
        "report": "ready" if report_path else "none",
        "files": len(tasks),
        "evidence": len(tasks) + (1 if report_path else 0),
        "outcome": _setup_outcome(trunk_data, report_raw, status),
        "updated": _normalize_timestamp(finish) or finish or "unknown",
        "report_path": report_path,
        "report_raw": report_raw,
        "logs": _setup_logs(logs_root or Path("logs"), project_name),
    }


def _read_latest_trunk(orchestrator: Any) -> tuple[str, dict[str, Any]] | None:
    filenames = _context_filenames(orchestrator)
    trunk_names = sorted(filename for filename in filenames if filename.startswith("trunk"))
    if not trunk_names:
        return None

    filename = trunk_names[-1]
    path = f"/workspace/.setup_agent/contexts/{filename}"
    raw = _read_container_file(orchestrator, path)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    return path, data


def _context_filenames(orchestrator: Any) -> list[str]:
    command = (
        "find /workspace/.setup_agent/contexts -maxdepth 2 -type f "
        "\\( -name 'trunk*.json' -o -name 'phase_*.json' "
        "-o -name 'full_outputs.jsonl' -o -path '*/journal/phase_*.journal.jsonl' \\) "
        "-printf '%P\\n' 2>/dev/null || true"
    )
    try:
        result = orchestrator.execute_command(command, timeout=5)
    except TypeError:
        result = orchestrator.execute_command(command)
    except Exception:
        return []

    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return []

    output = result.get("output")
    if not isinstance(output, str):
        return []

    filenames = [_safe_context_filename(line) for line in output.splitlines()]
    return [filename for filename in filenames if filename is not None]


def _latest_setup_report_path(orchestrator: Any) -> str | None:
    command = (
        "find /workspace -maxdepth 1 -name 'setup-report-*.md' -type f "
        "2>/dev/null | sort | tail -1"
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

    report_path = output.strip().splitlines()[-1] if output.strip() else ""
    if not report_path.startswith("/workspace/setup-report-") or not report_path.endswith(".md"):
        return None
    return report_path


def _raw_task_dicts(trunk_data: dict[str, Any]) -> list[dict[str, Any]]:
    value = trunk_data.get("todo_list")
    if not isinstance(value, list):
        value = trunk_data.get("tasks")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _setup_status(tasks: list[dict[str, Any]], report_path: str | None) -> str:
    if report_path is not None:
        return "completed"

    statuses = {_text(task.get("status"), default="").lower() for task in tasks}
    if statuses & {"active", "running", "in_progress"}:
        return "running"
    if statuses and statuses <= {"completed"}:
        return "completed"
    return "running" if tasks else "unknown"


def _setup_evidence_status(
    trunk_data: dict[str, Any],
    tasks: list[dict[str, Any]],
    report_raw: str | None,
) -> str:
    if report_raw:
        explicit_result = _evidence_status_from_result_line(report_raw)
        if explicit_result is not None:
            return explicit_result

        snapshot_status = _evidence_status_from_report_snapshot(report_raw)
        if snapshot_status is not None:
            return snapshot_status

    explicit_trunk_status = _explicit_evidence_status(trunk_data)
    if explicit_trunk_status is not None:
        return explicit_trunk_status

    task_status = _aggregate_evidence_status(
        _explicit_evidence_status(task) for task in tasks if isinstance(task, dict)
    )
    return task_status or "unknown"


def _evidence_status_from_result_line(report_raw: str) -> str | None:
    for line in report_raw.splitlines():
        text = line.strip()
        lowered = text.lower()
        if lowered.startswith("**result:**") or lowered.startswith("result:"):
            return _extract_prefixed_evidence_status(text.split(":", 1)[1])
    return None


def _evidence_status_from_report_snapshot(report_raw: str) -> str | None:
    structured_status = _evidence_status_from_json(report_raw)
    if structured_status is not None:
        return structured_status

    for row in _report_rows(report_raw):
        if len(row) < 2:
            continue
        label = _clean_status_label(row[0])
        if label in {"evidencestatus", "evidenceresult", "evidencestate"}:
            status = _extract_evidence_status_token(" ".join(row[1:]))
            if status is not None:
                return status

    patterns = [
        r"evidence[_\s-]*status\s*[:=]\s*([A-Za-z]+)",
        r'"evidenceStatus"\s*:\s*"([A-Za-z]+)"',
        r'"evidence_status"\s*:\s*"([A-Za-z]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, report_raw, flags=re.IGNORECASE)
        if match:
            status = _extract_evidence_status(match.group(1))
            if status is not None:
                return status

    return None


def _evidence_status_from_json(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _evidence_status_from_json_value(payload)


def _evidence_status_from_json_value(value: Any) -> str | None:
    if isinstance(value, dict):
        status = _explicit_evidence_status(value)
        if status is not None:
            return status
        for child in value.values():
            status = _evidence_status_from_json_value(child)
            if status is not None:
                return status
    elif isinstance(value, list):
        return _aggregate_evidence_status(
            _evidence_status_from_json_value(child) for child in value
        )
    return None


def _clean_status_label(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def _extract_evidence_status(value: Any) -> str | None:
    text = _text(value, default="").lower()
    match = re.search(r"\b(success|partial|blocked|conflict|unknown)\b", text)
    if match:
        return match.group(1)
    return None


def _extract_evidence_status_token(value: Any) -> str | None:
    tokens = re.findall(r"[a-z]+", _text(value, default="").lower())
    statuses = [token for token in tokens if token in _EVIDENCE_STATUS_VALUES]
    if len(statuses) == 1 and len(tokens) == 1:
        return statuses[0]
    return None


def _extract_prefixed_evidence_status(value: Any) -> str | None:
    tokens = re.findall(r"[a-z]+", _text(value, default="").lower())
    if tokens and tokens[0] in _EVIDENCE_STATUS_VALUES:
        return tokens[0]
    return None


def _explicit_evidence_status(item: dict[str, Any]) -> str | None:
    return _normalize_evidence_status(_value_for_keys(item, "evidence_status", "evidenceStatus"))


def _evidence_status(item: dict[str, Any]) -> str:
    return _explicit_evidence_status(item) or "unknown"


def _should_set_finished_evidence_status(item: dict[str, Any]) -> bool:
    status = _explicit_evidence_status(item)
    if status is None:
        return True
    source = _text(item.get("evidence_status_source"), default="")
    return status == "unknown" and source in {"", "default"}


def _normalize_evidence_status(value: Any) -> str | None:
    text = _text(value, default="").lower()
    if text in _EVIDENCE_STATUS_VALUES:
        return text
    return _extract_evidence_status_token(text)


def _aggregate_evidence_status(statuses: Any) -> str | None:
    normalized = [
        status
        for status in (_normalize_evidence_status(status) for status in statuses)
        if status is not None
    ]
    if not normalized:
        return None
    for candidate in _EVIDENCE_STATUS_PRECEDENCE:
        if candidate in normalized:
            return candidate
    return "unknown"


def _setup_session_id(context_id: str, created: str, workspace_id: str) -> str:
    # Session detail lookups are global, while trunk timestamps have seconds
    # granularity and collide across workspaces launched in the same second.
    # Scoping the id by workspace keeps each setup resolvable to its own
    # container.
    label = workspace_id.removeprefix("sag-")

    match = re.search(r"(\d{8})_(\d{6})", context_id)
    if match:
        return f"SETUP-{label}-{match.group(1)}-{match.group(2)}"

    normalized = _normalize_timestamp(created)
    if normalized:
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return f"SETUP-{label}-latest"
        return f"SETUP-{label}-{parsed.strftime('%Y%m%d-%H%M%S')}"

    return f"SETUP-{label}-latest"


def _read_report_metrics(orchestrator: Any) -> dict[str, Any] | None:
    """Read the structured metrics artifact, or None when absent/invalid."""
    from sag.tools.report_metrics import METRICS_PATH

    raw = _read_container_file(orchestrator, METRICS_PATH)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_module_metrics(orchestrator: Any) -> dict[str, Any] | None:
    """Read module_metrics.json from the container, or None when absent/invalid."""
    from sag.tools.module_metrics import MODULE_METRICS_PATH

    raw = _read_container_file(orchestrator, MODULE_METRICS_PATH)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _modules_payload_from_metrics(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metrics, dict):
        return []
    modules = metrics.get("modules")
    return [m for m in modules if isinstance(m, dict)] if isinstance(modules, list) else []


def _module_rollup_from_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metrics, dict):
        return None
    summary = metrics.get("module_summary")
    return summary if isinstance(summary, dict) else None


def _test_payload_from_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metrics, dict):
        return None
    test = metrics.get("test")
    if not isinstance(test, dict):
        return None
    return {
        "state": _text(test.get("state"), default="none"),
        "pass": _to_int(test.get("passed")),
        "fail": _to_int(test.get("failed")),
        "skip": _to_int(test.get("skipped")),
        "total": _to_int(test.get("total")),
        "errors": _to_int(test.get("errors")),
        "pass_rate": _to_float_or_none(test.get("pass_rate")),
        "execution_rate": _to_float_or_none(test.get("method_execution_rate")),
        "report_file_count": test.get("report_file_count"),
        "unique_total": test.get("unique_total"),
        "unique_passed": test.get("unique_passed"),
        "unique_failed": test.get("unique_failed"),
        "unique_errors": test.get("unique_errors"),
        "unique_skipped": test.get("unique_skipped"),
        "declared_total": test.get("declared_total"),
        "method_execution_rate": _to_float_or_none(test.get("method_execution_rate")),
        "failing_names": test.get("failing_names") or [],
        "conflicts": test.get("conflicts") or [],
        "evidence_refs": test.get("evidence_refs") or [],
    }


def _build_payload_from_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metrics, dict):
        return None
    build = metrics.get("build")
    if not isinstance(build, dict):
        return None
    return {
        "state": _text(build.get("state"), default="none"),
        "system": build.get("system"),
        "tool": _text(build.get("tool"), default="—") if build.get("tool") else "—",
        "time": _text(build.get("time"), default="—") if build.get("time") else "—",
        "note": build.get("note"),
        "artifact": build.get("artifact"),
        "class_count": build.get("class_count"),
        "jar_count": build.get("jar_count"),
        "module_output_count": build.get("module_output_count"),
        "artifact_samples": build.get("artifact_samples") or [],
        "warnings": build.get("warnings") or [],
        "evidence_refs": build.get("evidence_refs") or [],
    }


def _test_payload_from_report(report_raw: str | None) -> dict[str, Any]:
    if not report_raw:
        return {
            "state": "none",
            "pass": 0,
            "fail": 0,
            "skip": 0,
            "total": 0,
            "pass_rate": None,
            "execution_rate": None,
        }

    breakdown = _test_breakdown_from_report(report_raw)
    if breakdown is not None:
        return breakdown

    executed = _report_int(report_raw, "Tests Executed")
    passed = _report_int(report_raw, "Tests Passed")
    failed = _report_int(report_raw, "Failed") or _report_int(report_raw, "Failures")
    skipped = _report_int(report_raw, "Skipped")
    total = executed or passed or 0
    pass_count = passed or 0
    fail_count = failed or max(total - pass_count - skipped, 0)
    state = "success" if total and fail_count == 0 else "partial" if total else "none"

    return {
        "state": state,
        "pass": pass_count,
        "fail": fail_count,
        "skip": skipped,
        "total": total,
        "pass_rate": _rate(pass_count, total),
        "execution_rate": None,
    }


def _test_breakdown_from_report(report_raw: str) -> dict[str, Any] | None:
    lines = report_raw.splitlines()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not (
            "total available" in lowered
            and "executed" in lowered
            and "passed" in lowered
            and "failed" in lowered
            and "skipped" in lowered
        ):
            continue

        for candidate in lines[index + 1 : index + 4]:
            values = [
                int(value.replace(",", "")) for value in re.findall(r"\b[0-9][0-9,]*\b", candidate)
            ]
            if len(values) < 6:
                continue

            total_available, executed, passed, failed, errors, skipped = values[:6]
            fail_count = failed + errors
            state = "success" if executed and fail_count == 0 else "partial" if executed else "none"
            return {
                "state": state,
                "pass": passed,
                "fail": fail_count,
                "skip": skipped,
                "total": executed,
                "pass_rate": _rate(passed, executed),
                "execution_rate": _rate(executed, total_available),
            }

    return None


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 1)


def _report_int(report_raw: str, label: str) -> int:
    patterns = [
        rf"\|\s*\*\*{re.escape(label)}\*\*\s*\|\s*([0-9,]+)",
        rf"{re.escape(label)}\s*[:|]\s*([0-9,]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, report_raw, flags=re.IGNORECASE)
        if match:
            return _to_int(match.group(1).replace(",", ""))
    return 0


def _build_state_from_report(report_raw: str | None) -> str:
    if not report_raw:
        return "none"

    # Real reports encode build status as a ✅/❌ marker in the "Build" row of
    # the Build & Test Overview table (e.g. "✅ 115 classes, 0 JARs") and never
    # contain the literal phrase "build success". Read that marker first so a
    # successful build is not reported as "unknown".
    marker_state = _status_from_marker(_build_note_from_report(report_raw))
    if marker_state != "unknown":
        return marker_state

    lowered = report_raw.lower()
    if "build failed" in lowered or "build failure" in lowered:
        return "failed"
    if (
        "build passed" in lowered
        or "build success" in lowered
        # Reports write "**Result:** ✅ SUCCESS"; tolerate the emoji between
        # the label and the word so the plain-text fallback still matches.
        or re.search(r"result:\*\*\s*[^\n]*success", lowered) is not None
    ):
        return "success"
    return "unknown"


def _status_from_marker(text: str) -> str:
    """Map a ✅/❌ status marker (as used in the report tables) to a state."""

    if "❌" in text or "🔴" in text:
        return "failed"
    if "✅" in text:
        return "success"
    return "unknown"


def _build_payload_from_report(report_raw: str | None) -> dict[str, Any]:
    state = _build_state_from_report(report_raw)
    if not report_raw:
        return {"state": state}

    return {
        "state": state,
        "tool": _build_tool_from_report(report_raw),
        "time": "—",
        "note": _build_note_from_report(report_raw),
    }


def _build_tool_from_report(report_raw: str) -> str:
    for line in report_raw.splitlines():
        text = line.strip()
        if not text or text.startswith("|"):
            continue
        parts = [_strip_markdown_cell(part) for part in text.split("|")]
        if len(parts) >= 3 and parts[-1]:
            return parts[-1]
    return "—"


def _build_note_from_report(report_raw: str) -> str:
    for row in _report_rows(report_raw):
        if len(row) >= 2 and row[0].strip().lower() == "build":
            return row[1]
    return ""


def _build_state_value(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("state"), default="none")
    return _text(value, default="none")


def _report_generated_at(report_raw: str | None) -> str:
    if not report_raw:
        return ""
    match = re.search(r"\*\*Generated:\*\*\s*([^\n]+)", report_raw)
    return match.group(1).strip() if match else ""


def _setup_outcome(
    trunk_data: dict[str, Any],
    report_raw: str | None,
    status: str,
) -> str:
    if report_raw:
        result_line = next(
            (
                line.strip()
                for line in report_raw.splitlines()
                if line.strip().lower().startswith("**result:**")
            ),
            "",
        )
        if result_line:
            return result_line.removeprefix("**Result:**").strip()

    summary = _text(trunk_data.get("progress_summary"), default="")
    if summary:
        return summary
    return f"Project setup {status}."


def _report_document(item: dict[str, Any]) -> ReportDocument | None:
    report_path = _optional_text(item.get("report_path"))
    report_raw = _optional_text(item.get("report_raw"))
    if report_path is None or report_raw is None:
        return None

    title = Path(report_path).name
    generated = _report_generated_at(report_raw) or _text(item.get("finish"), default="unknown")
    return ReportDocument(
        title=title,
        path=report_path.removeprefix("/workspace/"),
        generated=generated,
        blocks=_report_blocks(report_raw),
    )


def _report_blocks(report_raw: str) -> list[dict[str, Any]]:
    max_blocks = 200
    blocks: list[dict[str, Any]] = []
    lines = report_raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        text = line.strip()
        if not text:
            index += 1
            continue

        if text.startswith("#"):
            level = min(len(text) - len(text.lstrip("#")), 2)
            blocks.append(
                {"type": f"h{level}", "text": _clean_inline_markdown(text.lstrip("#").strip())}
            )
        elif text.lower().startswith("**generated:**"):
            blocks.append({"type": "meta", "text": _clean_inline_markdown(text)})
        elif text.lower().startswith("**result:**"):
            result = _clean_inline_markdown(text.removeprefix("**Result:**").strip())
            blocks.append(
                {
                    "type": "status",
                    "text": result,
                    "ok": "success" in result.lower() or "passed" in result.lower(),
                }
            )
        elif text.startswith("|") or text.startswith("\u2502"):
            table_lines: list[str] = []
            while index < len(lines) and (
                lines[index].strip().startswith("|") or lines[index].strip().startswith("\u2502")
            ):
                table_lines.append(lines[index])
                index += 1
            rows = _report_rows("\n".join(table_lines))
            if rows:
                blocks.append({"type": "table", "rows": rows})
            continue
        elif not text.startswith("```"):
            blocks.append({"type": "p", "text": _clean_inline_markdown(text)})

        if len(blocks) >= max_blocks:
            break
        index += 1
    return blocks


def _markdown_table_rows(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        text = line.strip()
        if not text.startswith("|"):
            continue
        cells = [_strip_markdown_cell(cell) for cell in text.strip("|").split("|")]
        if not cells or all(_is_table_separator(cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _box_table_rows(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        text = line.strip()
        if not text.startswith("\u2502"):
            continue
        cells = [_strip_markdown_cell(cell) for cell in text.strip("\u2502").split("\u2502")]
        if cells:
            rows.append(cells)
    return rows


def _report_rows(markdown: str) -> list[list[str]]:
    return [*_markdown_table_rows(markdown), *_box_table_rows(markdown)]


def _strip_markdown_cell(value: str) -> str:
    return _clean_inline_markdown(value)


def _clean_inline_markdown(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip().strip("*").strip()


def _is_table_separator(value: str) -> bool:
    stripped = value.replace(" ", "")
    return bool(stripped) and set(stripped) <= {"-", ":"}


def _normalize_timestamp(value: str) -> str:
    if not value:
        return ""
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _setup_logs(logs_root: Path, project_name: str) -> list[str]:
    session_dir = _matching_log_session_dir(logs_root, project_name)
    if session_dir is None:
        return []
    return _read_log_file(session_dir / "agent_execution.log", max_lines=500)


def _matching_log_session_dir(logs_root: Path, project_name: str) -> Path | None:
    # Pick the most recent host log session that ran this project, identified by
    # its per-project command log. Ordering is by that file's mtime — an absolute
    # epoch — rather than by parsing the session-dir name string. The dir name is
    # host-local time while the setup's `created_at` is written inside the
    # container (commonly UTC), so a name-vs-created comparison wrongly skips
    # every dir on hosts east of UTC. mtime is timezone-independent.
    if not logs_root.exists():
        return None

    candidates: list[tuple[float, Path]] = []
    for session_dir in logs_root.glob("session_*"):
        if not session_dir.is_dir():
            continue
        command_log = session_dir / f"command_project_{project_name}.log"
        if not command_log.exists():
            continue
        try:
            mtime = command_log.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, session_dir))

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _read_log_file(path: Path, max_lines: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _log_lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(line) for line in value]


def _build_summary(value: Any) -> BuildSummary:
    if isinstance(value, dict):
        try:
            return BuildSummary.model_validate(value)
        except Exception:
            return BuildSummary()

    return BuildSummary(state=_text(value, default="none"))


def _evidence(item: dict[str, Any], outcome: str) -> list[EvidenceGroup]:
    time = _display_time(_optional_text(item.get("finish")) or _text(item.get("start"), default=""))
    status = _status_for_evidence(_evidence_status(item))
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
    if normalized in {"success", "completed", "succeeded"}:
        return "success"
    if normalized in {"blocked", "conflict", "unknown"}:
        return normalized
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


def _read_context_trace(orchestrator: Any) -> ContextTrace | None:
    command = (
        "find /workspace/.setup_agent/contexts -maxdepth 2 -type f "
        "\\( -name 'trunk*.json' -o -name 'phase_*.json' "
        "-o -name 'full_outputs.jsonl' -o -path '*/journal/phase_*.journal.jsonl' \\) "
        "-printf '%P\\n' 2>/dev/null || true"
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

        for filename in filenames[:100]:
            raw = _read_container_file(
                orchestrator,
                f"/workspace/.setup_agent/contexts/{filename}",
            )
            if raw is None:
                continue
            target = contexts_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(raw, encoding="utf-8")

        return ContextTraceBuilder(contexts_dir).build()


def _is_context_filename(filename: str) -> bool:
    """Context artifacts the dashboard may read for the phase trace model."""
    if filename == "full_outputs.jsonl":
        return True
    if filename.startswith("journal/"):
        return bool(re.fullmatch(r"journal/phase_[A-Za-z0-9_-]+\.journal\.jsonl", filename))
    if not filename.endswith(".json"):
        return False
    return filename.startswith(("trunk", "phase_"))


def _safe_context_filename(value: str) -> str | None:
    filename = value.strip()
    if not filename or filename.startswith("/") or ".." in Path(filename).parts:
        return None
    if not _is_context_filename(filename):
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


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value_for_keys(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


__all__ = [
    "ContainerSessionRegistry",
    "ContainerSessionStore",
    "SessionRegistry",
    "parse_session_index",
]
