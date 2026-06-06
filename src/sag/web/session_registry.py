"""Read local SAG session index artifacts for the web dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sag.web.models import ExecutionSessionSummary, TestSummary


class SessionRegistry:
    def read_index(self, workspace_root: Path, workspace_id: str) -> list[ExecutionSessionSummary]:
        index_path = workspace_root / ".setup_agent" / "sessions" / "index.json"

        try:
            raw = index_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(payload, dict):
            return []

        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            return []

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
        title=_text(item.get("title"), default=""),
        status=_text(item.get("status"), default="unknown"),
        entry=_text(item.get("entry"), default="unknown"),
        start=_text(item.get("start"), default=""),
        finish=_optional_text(item.get("finish")),
        duration=_text(item.get("duration"), default=""),
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


def _text(value: Any, default: str) -> str:
    if value is None:
        return default

    return str(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None

    return str(value)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
