"""Build frontend evidence groups from trusted runtime evidence records."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sag.ui.state import UIEvidenceRecord
from sag.web.models import EvidenceGroup, EvidenceRecord


_STATUS_SEVERITY = {
    "info": 0,
    "success": 1,
    "unknown": 2,
    "partial": 3,
    "failure": 4,
    "conflict": 5,
    "blocked": 6,
}
_STATUS_ALIASES = {
    "block": "blocked",
    "complete": "success",
    "completed": "success",
    "conflicted": "conflict",
    "fail": "failure",
    "failed": "failure",
    "error": "failure",
    "incomplete": "partial",
    "ok": "success",
    "pass": "success",
    "passed": "success",
}


class EvidenceIndex:
    """Group runtime-vetted evidence records for the web UI."""

    def from_ui_records(self, records: Iterable[UIEvidenceRecord]) -> list[EvidenceGroup]:
        grouped: OrderedDict[str, list[EvidenceRecord]] = OrderedDict()
        group_status: dict[str, str] = {}

        for record in records:
            metadata = _metadata(record.metadata)
            source = _source(record, metadata)
            status = _status(metadata)
            detail = _detail(record)

            evidence_record = EvidenceRecord(
                time=_time(record.timestamp),
                status=status,
                title=_title(record),
                detail=detail,
                ref=_ref(metadata),
            )

            if source not in grouped:
                grouped[source] = []
                group_status[source] = "info"

            grouped[source].append(evidence_record)
            group_status[source] = _merge_status(group_status[source], status)

        return [
            EvidenceGroup(
                source=source,
                status=group_status[source],
                counts=f"{len(source_records)} records",
                time=source_records[-1].time,
                summary=source_records[-1].detail,
                records=source_records,
            )
            for source, source_records in grouped.items()
        ]


def _metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _source(record: UIEvidenceRecord, metadata: dict[str, Any]) -> str:
    explicit_source = _text(metadata.get("source"))
    if explicit_source:
        return explicit_source

    tool_name = _text(metadata.get("tool_name"))
    if tool_name:
        normalized_tool = tool_name.lower()
        if normalized_tool == "maven":
            return "Build tool · Maven"
        if normalized_tool == "gradle":
            return "Build tool · Gradle"
        return tool_name.title()

    return _text(record.kind, fallback="Evidence").title()


def _status(metadata: dict[str, Any]) -> str:
    explicit_evidence_status = _text(
        metadata.get("evidence_status") or metadata.get("evidenceStatus")
    )
    if explicit_evidence_status:
        return _status_for_severity(explicit_evidence_status.lower())
    return _status_for_severity(_text(metadata.get("status"), fallback="info").lower())


def _merge_status(left: str, right: str) -> str:
    left_status = _status_for_severity(left)
    right_status = _status_for_severity(right)
    if _STATUS_SEVERITY[right_status] > _STATUS_SEVERITY[left_status]:
        return right_status
    return left_status


def _status_for_severity(status: str) -> str:
    normalized = _STATUS_ALIASES.get(status, status)
    if normalized in _STATUS_SEVERITY:
        return normalized
    return "info"


def _title(record: UIEvidenceRecord) -> str:
    return _text(record.kind, fallback="Evidence").title()


def _detail(record: UIEvidenceRecord) -> str:
    return _text(record.summary, fallback="Evidence")


def _ref(metadata: dict[str, Any]) -> str:
    return _text(metadata.get("ref")) or _text(metadata.get("output_ref")) or "runtime"


def _time(value: datetime) -> str:
    try:
        return value.strftime("%H:%M")
    except AttributeError:
        return "—"


def _text(value: Any, *, fallback: str = "") -> str:
    if value is None:
        return fallback

    text = str(value).strip()
    return text or fallback
