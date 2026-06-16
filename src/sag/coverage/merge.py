"""Merge a coverage map into an existing module_metrics dict (pure)."""

from typing import Any, Dict

_COV_FIELDS = (
    "line_covered", "line_total", "line_rate",
    "branch_covered", "branch_total", "branch_rate", "coverage_source",
)


def _rate(covered: int, total: int):
    return round(100.0 * covered / total, 1) if total > 0 else None


def merge_coverage_into_metrics(
    metrics: Dict[str, Any], coverage_map: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Set per-module coverage fields by reactor path and recompute the
    lines-weighted rollup. Coverage is cleared first, so a re-merge reflects only
    the current coverage_map (no stale values linger on modules that dropped out).
    Returns the same dict (mutated) for convenience."""
    coverage_map = coverage_map or {}
    modules = metrics.get("modules") or []

    line_c = line_t = branch_c = branch_t = 0
    sources: set = set()
    for module in modules:
        cov = coverage_map.get(module.get("path"))
        if not cov:
            # Clear any stale coverage from a previous merge.
            for field in _COV_FIELDS:
                module[field] = None
            continue
        for field in _COV_FIELDS:
            module[field] = cov.get(field)
        line_c += int(cov.get("line_covered") or 0)
        line_t += int(cov.get("line_total") or 0)
        branch_c += int(cov.get("branch_covered") or 0)
        branch_t += int(cov.get("branch_total") or 0)
        if cov.get("coverage_source"):
            sources.add(cov["coverage_source"])

    summary = metrics.setdefault("module_summary", {})
    has_any = bool(sources)
    summary["line_covered"] = line_c if has_any else None
    summary["line_total"] = line_t if has_any else None
    summary["line_rate"] = _rate(line_c, line_t) if has_any else None
    summary["branch_covered"] = branch_c if has_any else None
    summary["branch_total"] = branch_t if has_any else None
    summary["branch_rate"] = _rate(branch_c, branch_t) if has_any else None
    # If any module needed injection, the aggregate provenance is "injected".
    summary["coverage_source"] = (
        "jacoco-injected" if "jacoco-injected" in sources
        else "jacoco-existing" if "jacoco-existing" in sources
        else None
    )
    return metrics
