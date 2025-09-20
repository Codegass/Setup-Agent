"""Utility helpers for rendering setup summaries and attention items."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

DEFAULT_MAX_LIST_ITEMS = 3
DEFAULT_MAX_ATTENTION_ITEMS = 5


def truncate_list(items: Iterable[Any], max_items: int = DEFAULT_MAX_LIST_ITEMS) -> str:
    """Return a comma-separated string capped at *max_items* with a suffix when truncated."""
    if not items:
        return ""

    materialized = [str(item) for item in items if item is not None and str(item).strip()]
    if not materialized:
        return ""

    if len(materialized) <= max_items:
        return ", ".join(materialized)

    visible = materialized[:max_items]
    remaining = len(materialized) - max_items
    return f"{', '.join(visible)} (+{remaining} more)"


def format_percentage(value: Optional[float], precision: int = 1) -> str:
    """Format a numeric ratio as a percentage string, handling None gracefully."""
    if value is None:
        return "N/A"
    return f"{round(float(value), precision):.{precision}f}%"


def format_attention_items(attention_items: List[str], max_items: int = DEFAULT_MAX_ATTENTION_ITEMS) -> List[str]:
    """Limit attention lines to a manageable number while preserving order."""
    if not attention_items:
        return []

    if len(attention_items) <= max_items:
        return attention_items

    trimmed = attention_items[:max_items]
    trimmed.append(f"… (+{len(attention_items) - max_items} more)")
    return trimmed


def _status_icon(status: Optional[str]) -> str:
    normalized = (status or "").lower()
    if normalized in {"success", "ok", "pass", "passed"}:
        return "✅", "SUCCESS"
    if normalized in {"partial", "warning"}:
        return "⚠️", status.upper() if status else "PARTIAL"
    if normalized in {"info", "pending"}:
        return "ℹ️", status.upper() if status else "INFO"
    return "❌", (status or "FAIL").upper()


def _phase_icon(flag: Optional[bool]) -> str:
    if flag is True:
        return "✅"
    if flag is False:
        return "❌"
    return "⚪"


def render_condensed_summary(snapshot: Dict[str, Any]) -> str:
    """Render a compact multi-line summary for console/log surfaces."""
    status = snapshot.get("status", {})
    project = snapshot.get("project", {})
    phases = snapshot.get("phases", {})
    attention = snapshot.get("attention", {})
    evidence = snapshot.get("physical_evidence", {})

    icon, label = _status_icon(status.get("overall"))
    clone_icon = _phase_icon(phases.get("clone"))
    build_icon = _phase_icon(phases.get("build"))
    test_icon = _phase_icon(phases.get("test"))

    project_type = project.get("type", "Unknown")
    build_system = project.get("build_system", "Unknown")
    report_path = snapshot.get("report_path", "unknown")

    lines = [
        f"🎯 SETUP COMPLETED: {icon} {label}",
        f"📋 Core Status: {clone_icon} Clone, {build_icon} Build, {test_icon} Test",
        f"📂 Project: {project_type} ({build_system})",
        f"📄 Full report saved to: {report_path}",
    ]

    attention_items = format_attention_items(attention.get("items", []))
    for item in attention_items:
        lines.append(f"⚠️ {item}")

    if evidence:
        class_files = evidence.get("class_files")
        jar_files = evidence.get("jar_files")
        if class_files is not None or jar_files is not None:
            details = []
            if class_files is not None:
                details.append(f"{class_files} .class")
            if jar_files is not None:
                details.append(f"{jar_files} .jar")
            if details:
                lines.append(f"🧾 Build artifacts: {', '.join(details)}")
        if evidence.get("tests_total") is not None:
            pass_pct = evidence.get("tests_pass_pct")
            execution_rate = status.get("execution_rate")
            test_line = f"🧪 Tests: {evidence['tests_total']} executed"
            
            # Add pass rate
            test_line += f" (pass rate {format_percentage(pass_pct)}"
            
            # Add execution rate if available
            if execution_rate is not None:
                test_line += f", execution rate {format_percentage(execution_rate)}"
            
            test_line += ")"
            lines.append(test_line)

    if attention.get("ignored_lines"):
        lines.append(f"ℹ️ Ignored telemetry lines: {attention['ignored_lines']}")

    return "\n".join(lines)
