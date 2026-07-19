"""Island/module coverage: ONE computation, two consumers.

The verdict finalizer folds these conflicts at evidence-close; the phase gates
render the checklist MID-RUN so the agent can see what remains (live 2026-07-18
probes: one bigtop run gave up with islands unattempted because nothing named
them; another fixated on a broken island for 7 calls while three healthy ones
sat untouched). Both consumers call the same function — if the in-run guidance
and the sealed verdict computed coverage differently, they would eventually
disagree, which is the split-brain this campaign just cured.

Python projects are exempt (July rule: packages-as-modules is future work).
Never raises: coverage is guidance and honesty, not a failure mode.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _record_richness(module: dict[str, Any]) -> int:
    return (
        int(module.get("class_count") or 0)
        + int(module.get("jar_count") or 0)
        + len(module.get("report_dirs") or [])
        + (1 if module.get("has_test_sources") else 0)
    )


def module_coverage(validator, project_name) -> dict[str, Any] | None:
    """Scan both JVM systems, merge per path (richer record wins — the July
    mixed-layout semantics), parse per-module test reports, and roll up.

    Returns ``{"summary": ..., "modules": ..., "project_dir": ...}`` or None
    for non-JVM projects / unavailable validators.
    """
    if validator is None:
        return None
    try:
        from sag.tools.module_metrics import assemble_module_metrics

        project_path = str(getattr(validator, "project_path", "/workspace") or "/workspace")
        project_dir = f"{project_path}/{project_name}" if project_name else project_path
        primary = str(validator._detect_build_system(project_dir) or "").strip().lower()
        if primary not in ("maven", "gradle"):
            return None

        merged: dict[str, dict[str, Any]] = {}
        for system in ("maven", "gradle"):
            try:
                modules = validator.scan_modules(project_dir, system) or []
            except Exception:
                continue
            for module in modules:
                if not isinstance(module, dict):
                    continue
                path = str(module.get("path") or ".")
                current = merged.get(path)
                if current is None or _record_richness(module) > _record_richness(current):
                    merged[path] = module
        if not merged:
            return None

        tests: dict[str, Any] = {}
        for path, module in merged.items():
            report_dirs = module.get("report_dirs") or []
            if not report_dirs:
                continue
            module_dir = f"{project_dir}/{path}" if path != "." else project_dir
            try:
                parsed = validator.parse_module_test_reports(module_dir, report_dirs)
            except Exception:
                parsed = {}
            if parsed:
                tests[path] = parsed

        metrics = assemble_module_metrics(
            modules=list(merged.values()),
            reactor_status={},
            tests=tests,
            build_systems=[primary],
            build_error_samples={},
            generated_at="coverage",
        )
        return {
            "summary": metrics.get("module_summary") or {},
            "modules": metrics.get("modules") or [],
            "project_dir": project_dir,
        }
    except Exception as exc:
        logger.debug(f"module coverage unavailable: {exc}")
        return None


def coverage_conflicts(coverage: dict[str, Any] | None) -> tuple[str, ...]:
    """The two July coverage caps, from a coverage rollup."""
    if not coverage:
        return ()
    summary = coverage.get("summary") or {}
    conflicts: list[str] = []
    total = int(summary.get("modules_total") or 0)
    built = int(summary.get("modules_built") or 0)
    if total and built < total:
        conflicts.append("build_modules_incomplete")
    bearing = int(summary.get("modules_test_bearing") or 0)
    tested = summary.get("modules_tested")
    if bearing and tested is not None and 0 < int(tested) < bearing:
        conflicts.append("reactor_scope_narrowed")
    return tuple(conflicts)


def coverage_checklist_line(coverage: dict[str, Any] | None, *, limit: int = 6) -> str | None:
    """A one-line, agent-facing checklist: what built, what has no output yet.

    The agent's window is seven steps; a ratio alone ("1/4") tells it nothing
    actionable. NAME the modules so 'keep going' has a target.
    """
    if not coverage:
        return None
    modules = coverage.get("modules") or []
    summary = coverage.get("summary") or {}
    total = int(summary.get("modules_total") or 0)
    if not total:
        return None
    built = [str(m.get("path")) for m in modules if m.get("build_status") == "success"]
    unbuilt = [str(m.get("path")) for m in modules if m.get("build_status") != "success"]

    def _names(paths: list[str]) -> str:
        shown = [p.rsplit("/", 1)[-1] or p for p in paths[:limit]]
        suffix = f" +{len(paths) - limit} more" if len(paths) > limit else ""
        return ", ".join(shown) + suffix

    line = f"Module coverage: {len(built)}/{total} built"
    if built:
        line += f" [{_names(built)}]"
    if unbuilt:
        line += f" · no output yet: [{_names(unbuilt)}]"
    tested = summary.get("modules_tested")
    bearing = summary.get("modules_test_bearing")
    if bearing:
        line += f" · tests ran in {int(tested or 0)}/{int(bearing)} test-bearing modules"
    return line
