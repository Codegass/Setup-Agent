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


def format_attention_items(
    attention_items: List[str], max_items: int = DEFAULT_MAX_ATTENTION_ITEMS
) -> List[str]:
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


def _python_evidence_rungs(fingerprints: Dict[str, Any]) -> List[str]:
    """Render the python evidence-ladder rungs (validate_build_status's
    fingerprint_details) as compact summary parts.

    Boolean rungs render ✓/✗; rungs the ladder could not measure (None —
    e.g. no declared packages to import, no declared C-extensions) are
    omitted rather than invented.
    """
    parts: List[str] = []
    tick = lambda flag: "✓" if flag else "✗"  # noqa: E731 - tiny local helper
    if fingerprints.get("venv_exists") is not None:
        parts.append(f"venv {tick(fingerprints['venv_exists'])}")
    if fingerprints.get("pip_check_clean") is not None:
        parts.append(f"pip check {tick(fingerprints['pip_check_clean'])}")
    if fingerprints.get("imports_ok") is not None:
        parts.append(f"imports {tick(fingerprints['imports_ok'])}")
    coverage = fingerprints.get("compileall_coverage")
    if coverage is not None:
        try:
            parts.append(f"compileall {float(coverage) * 100:.0f}%")
        except (TypeError, ValueError):
            pass
    if fingerprints.get("ext_modules_ok") is not None:
        parts.append(f"C-extensions {tick(fingerprints['ext_modules_ok'])}")
    return parts


def render_condensed_summary(snapshot: Dict[str, Any]) -> str:
    """Render a compact multi-line summary for console/log surfaces."""
    status = snapshot.get("status", {})
    project = snapshot.get("project", {})
    phases = snapshot.get("phases", {})
    attention = snapshot.get("attention", {})
    evidence = snapshot.get("physical_evidence", {})

    # The kernel verdict stored in the snapshot (spec §6) is the ONLY source
    # for the banner; 'overall' is the raw physical status and can sit above
    # the kernel (round-6 review: '🎯 SETUP COMPLETED: ✅ SUCCESS' printed
    # beside a '**Result:** ⚠️ PARTIAL' report header for the same snapshot).
    icon, label = _status_icon(status.get("verdict") or status.get("overall"))
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
        # Python projects have no .class/JAR analog — "0 .class, 0 .jar" on a
        # green python run is a Java-ism. The evidence ladder the validator
        # already produced (venv -> pip check -> imports -> compileall ->
        # C-extensions, see PhysicalValidator._verify_python_build) IS the
        # build evidence there; unknown rungs (None) are skipped, never
        # invented. Java/Maven/Gradle keep the artifacts line unchanged.
        build_system = str(
            evidence.get("build_system") or project.get("build_system") or ""
        ).strip().lower()
        if build_system in ("python", "pip/poetry"):
            rungs = _python_evidence_rungs(evidence.get("fingerprint_details") or {})
            if rungs:
                lines.append(f"🧾 Build evidence: {', '.join(rungs)}")
        else:
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

    # Tests line: surface the DETECTED (static) total alongside the executed
    # count. The static total is vital and must never silently drop from the
    # logs — even when nothing executed (e.g. "57 detected, 0 executed"), which
    # the old executed-only line hid entirely.
    tests_total = evidence.get("tests_total") if evidence else None
    static_count = status.get("static_test_count")
    if static_count or tests_total is not None:
        pass_pct = (evidence or {}).get("tests_pass_pct")
        execution_rate = status.get("execution_rate")
        executed = tests_total if tests_total is not None else 0

        parts = []
        if static_count:
            parts.append(f"{static_count} detected")
        parts.append(f"{executed} executed")
        test_line = f"🧪 Tests: {', '.join(parts)}"

        quals = []
        if tests_total:
            quals.append(f"pass rate {format_percentage(pass_pct)}")
        if execution_rate is not None:
            quals.append(f"execution rate {format_percentage(execution_rate)}")
        if quals:
            test_line += f" ({', '.join(quals)})"
        lines.append(test_line)

    # Module build completeness: how many ACTIVE modules built vs were detected
    # (mirrors the tests line). The core "build all modules" signal — kept in the
    # log, not just the markdown report.
    modules_detected = status.get("modules_detected")
    if modules_detected:
        modules_built = status.get("modules_built") or 0
        modules_tested = status.get("modules_tested") or 0
        modules_not_tested = status.get("modules_not_tested")
        if modules_not_tested is None:
            modules_not_tested = modules_detected - modules_tested
        module_line = (
            f"🧩 Modules: {modules_built} built / {modules_detected} detected"
            f" · {modules_tested} tested / {modules_not_tested} not tested"
        )
        extra = []
        if status.get("modules_failed_count"):
            extra.append(f"{status['modules_failed_count']} failed")
        if status.get("modules_skipped_count"):
            extra.append(f"{status['modules_skipped_count']} skipped")
        if extra:
            module_line += f" ({', '.join(extra)})"
        lines.append(module_line)

    if attention.get("ignored_lines"):
        lines.append(f"ℹ️ Ignored telemetry lines: {attention['ignored_lines']}")

    return "\n".join(lines)
